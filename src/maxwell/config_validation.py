import asyncio
import shutil
import sqlite3
import tempfile
from pathlib import Path

import httpx

from maxwell.config import Paths
from maxwell.github import GitHub, rate_limit_message
from maxwell.model_discovery import discover_models, redact_metadata
from maxwell.provider import create_provider
from maxwell.provider_runtime import native_errors


def registered_repositories(paths):
    if not paths.db.exists():
        return []
    connection = sqlite3.connect(paths.db.as_uri() + "?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(repositories)")}
        account = "github_account" if "github_account" in columns else "'default'"
        return connection.execute(
            f"SELECT name,{account} FROM repositories ORDER BY name"
        ).fetchall()
    finally:
        connection.close()


def validate_config(
    paths, online=True, on_check=None, discover=False, check_models=True, cached_catalogs=None
):
    checks = []
    catalogs = []
    config = None

    def add(name, status, message):
        check = {
            "name": config.redact(name) if config else name,
            "status": status,
            "message": config.redact(message) if config else message,
        }
        checks.append(check)
        if on_check is not None:
            on_check(dict(check))

    def report():
        result = {
            "config": str(paths.config),
            "valid": not any(check["status"] == "error" for check in checks),
            "online": online,
            "checks": checks,
        }
        if discover and config is not None:
            result["providers"] = catalogs
        return result

    try:
        config = paths.load(detailed=True)
    except (ValueError, OSError) as exc:
        add("configuration", "error", str(exc))
        return report()
    add("configuration", "ok", "YAML, settings, and file permissions are valid")
    errors = config.readiness_errors()
    for error in errors:
        add("runtime", "error", error)
    if not errors:
        add("runtime", "ok", "Required settings and executables are available")

    profiles = {
        "default": config.github.for_account(),
        **{name: config.github.for_account(name) for name in config.github.accounts},
    }
    try:
        repositories = registered_repositories(paths)
    except sqlite3.Error:
        add("repositories", "error", "Cannot read the repository registry")
        repositories = []
    for repository, account in repositories:
        if account not in profiles:
            add(f"repository:{repository}", "error", f"Unknown GitHub account {account!r}")
        elif not profiles[account].token.get_secret_value().strip():
            add(f"repository:{repository}", "error", f"GitHub account {account!r} has no token")

    pairs = set()
    for name, settings in config.all_providers().items():
        errors = []
        if not shutil.which(settings.executable):
            errors.append("Provider executable was not found")
        if settings.home is not None:
            errors.extend(native_errors(settings))
        elif not settings.api_key.get_secret_value().strip():
            errors.append("Set an API key or an existing provider home")
        add(
            f"provider:{name}",
            "error" if errors else "ok",
            "; ".join(errors)
            if errors
            else "Provider configuration and dependencies are available",
        )
        if discover and cached_catalogs and name in cached_catalogs:
            catalogs.append(cached_catalogs[name])
            add(
                f"models:{name}",
                "skipped",
                "Reusing saved model metadata and access checks; use --force to refresh",
            )
            continue
        if not check_models:
            if discover:
                add(
                    f"models:{name}",
                    "error",
                    "No saved model catalog; run `maxwell config apply` without --skip-model-checks",
                )
            continue
        catalog = None
        if discover:
            catalog = asyncio.run(discover_models(config, name))["providers"][0]
            catalogs.append(catalog)
            add(
                f"models:{name}",
                "error" if catalog.get("error") else "ok",
                catalog.get("error") or f"Discovered {len(catalog['models'])} models",
            )
        models = set(settings.models)
        if catalog:
            models.update(model["model"] for model in catalog["models"])
        if settings.model:
            models.add(settings.model)
        if not models and not errors:
            try:
                models.add(settings.default_model())
            except ValueError:
                if not any(provider == name for provider, _, _ in pairs):
                    if settings.adapter == "claude":
                        pairs.add((name, None, settings.effort))
                    else:
                        add(
                            f"provider:{name}:model",
                            "error" if online else "skipped",
                            "Set a provider model or model catalog to verify access",
                        )
        for model in models:
            try:
                model_settings = settings
                if discover and model not in settings.models:
                    model_settings = settings.model_copy(update={"models": {}})
                selected = model_settings.for_model(model)
                pairs.add((name, model, selected.effort))
            except ValueError as exc:
                add(f"provider:{name}:{model}", "error", str(exc))
        for provider, model, effort in sorted(
            pairs, key=lambda pair: (pair[0], pair[1] or "", pair[2] or "")
        ):
            if provider != name:
                continue
            label = f"provider:{name}:{model or 'default'}" + (f" / {effort}" if effort else "")
            if errors:
                add(label, "skipped", "Online check blocked by provider configuration errors")
            elif not online:
                add(label, "skipped", "Authentication and model access were not checked offline")
            else:
                outcome = []

                def checked(name, status, message):
                    outcome.append(
                        {"effort": effort, "status": status, "message": config.redact(message)}
                    )
                    add(name, status, message)

                provider_check(
                    config, name, model, label, checked, effort=effort, unrestricted=discover
                )
                if catalog:
                    entry = next(
                        (
                            entry
                            for entry in catalog["models"]
                            if entry["model"] == (model or "default")
                        ),
                        None,
                    )
                    if entry:
                        entry.setdefault("checks", []).extend(outcome)
                        entry["access"] = (
                            "verified"
                            if any(check["status"] == "ok" for check in entry["checks"])
                            else "unavailable"
                        )

    for name, settings in profiles.items():
        label = f"github:{name}"
        if not settings.token.get_secret_value().strip():
            if name != "default":
                add(label, "error", "Set a nonempty token for this GitHub account")
            else:
                add(label, "skipped", "No default token configured; select a named account")
            continue
        if not online:
            add(label, "skipped", "Token is configured; authentication was not checked offline")
            continue
        try:
            client = GitHub(
                settings,
                account=name,
                on_status=(
                    lambda message: on_check(
                        {"name": label, "status": "waiting", "message": config.redact(message)}
                    )
                )
                if on_check
                else None,
            )
        except (httpx.InvalidURL, ValueError):
            add(label, "error", "Cannot create the GitHub client; check github.api_url")
            continue
        try:
            identity = github_check(client, "user", label, add)
            if identity is not None:
                for repository, account in repositories:
                    if account == name:
                        metadata = github_check(
                            client, f"repos/{repository}", f"repository:{repository}", add
                        )
                        if metadata is not None:
                            repository_checks(metadata, repository, config.fix.auto_merge, add)
        finally:
            client.close()
    for check in checks:
        for key in ("name", "message"):
            check[key] = config.redact(check[key])
    result = report()
    if discover:
        result["providers"] = redact_metadata(catalogs, config, [])
    result["config"] = config.redact(result["config"])
    return result


def provider_check(config, name, model, label, add, effort=None, unrestricted=False):
    settings = config.all_providers()[name]
    if unrestricted and model not in settings.models:
        settings = settings.model_copy(update={"models": {}})
    settings = (
        settings.for_model(model)
        if model is not None
        else settings.model_copy(update={"model": ""})
    )
    settings = config.with_effort(settings, effort)
    timeout = min(30, config.limits.scan_timeout_seconds)
    probe_config = config.model_copy(
        update={
            "provider": settings,
            "limits": config.limits.model_copy(update={"scan_timeout_seconds": timeout}),
        }
    )
    try:
        with tempfile.TemporaryDirectory(prefix="maxwell-check-") as directory:
            root = Path(directory).resolve()
            workspace, artifact = root / "workspace", root / "artifacts"
            workspace.mkdir()
            artifact.mkdir()
            provider = create_provider(Paths(root / "runtime"), probe_config)
            asyncio.run(provider.probe(workspace, artifact))
    except TimeoutError:
        add(label, "error", f"Provider/model request timed out after {timeout:g} seconds")
    except (ValueError, OSError) as exc:
        add(label, "error", f"Provider/model request failed: {exc}")
    else:
        add(label, "ok", "Authenticated request succeeded; model access verified")


def repository_checks(metadata, repository, policy, add):
    label = f"repository:{repository}"
    if metadata.get("archived"):
        add(
            label + ":writes",
            "error",
            "Repository is archived and read-only; unarchive it in Settings > General or remove it from Maxwell",
        )
    if metadata.get("disabled"):
        add(
            label + ":writes",
            "error",
            "GitHub has disabled this repository; ask its owner or GitHub Support to restore access",
        )
    if metadata.get("has_issues") is False:
        add(
            label + ":findings",
            "error",
            "GitHub Issues are disabled; enable Issues in Settings > General > Features to publish Maxwell findings",
        )
    if not policy.enabled:
        return
    permission = (metadata.get("permissions") or {}).get("push")
    if permission is False:
        add(
            label + ":merge_access",
            "error",
            "GitHub reports no repository write access for this account; grant Write access and token Contents and Pull requests write permissions, authorize SSO if needed, then rerun maxwell config apply",
        )
    else:
        add(
            label + ":merge_access",
            "ok" if permission else "skipped",
            "Repository write access is reported; token-specific write grants and PR rules are checked when merging"
            if permission
            else "GitHub did not report write permissions; ensure token Contents and Pull requests write access. Actual merge authorization is checked when merging",
        )
    method_flags = {
        "merge": "allow_merge_commit",
        "squash": "allow_squash_merge",
        "rebase": "allow_rebase_merge",
    }
    allowed = metadata.get(method_flags[policy.merge_method])
    if allowed is False:
        alternatives = [
            method for method, flag in method_flags.items() if metadata.get(flag) is True
        ]
        advice = (
            f"set fix.auto_merge.merge_method to {', '.join(alternatives)}"
            if alternatives
            else "enable a merge method in Settings > General > Pull Requests"
        )
        add(
            label + ":merge_method",
            "error",
            f"Repository disables {policy.merge_method} merging; {advice}, then rerun maxwell config apply",
        )
    elif allowed is True:
        add(
            label + ":merge_method",
            "ok",
            f"Repository allows {policy.merge_method} merges; CI and branch protection are not required by Maxwell",
        )
    else:
        add(
            label + ":merge_method",
            "skipped",
            "GitHub did not report allowed merge methods; the merge API will verify the configured method",
        )


def github_check(client, path, label, add):
    try:
        result = client.request("GET", path)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        messages = {
            401: "GitHub rejected the token; check whether it is invalid or expired",
            403: "GitHub denied access; grant the token access to this repository, authorize organization SSO if required, and check token permissions or organization policy",
            404: "Repository does not exist or this account cannot access it",
        }
        add(
            label,
            "error",
            rate_limit_message(exc.response) or messages.get(code, f"GitHub returned HTTP {code}"),
        )
        return None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        add(label, "error", "Could not complete the GitHub check; check the API URL and connection")
        return None
    if not isinstance(result, dict) or not result.get("login" if path == "user" else "full_name"):
        add(label, "error", "GitHub returned an unexpected response; check github.api_url")
        return None
    message = (
        f"Authenticated as {result['login']}"
        if path == "user"
        else "Repository metadata is accessible; write permissions were not tested"
    )
    add(label, "ok", message)
    return result
