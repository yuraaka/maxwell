import asyncio
import shutil
import sqlite3
import tempfile
from pathlib import Path

import httpx

from maxwell.config import Paths
from maxwell.github import GitHub
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


def registered_routes(paths):
    if not paths.db.exists():
        return []
    connection = sqlite3.connect(paths.db.as_uri() + "?mode=ro", uri=True)
    try:
        if not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repository_stage_routes'"
        ).fetchone():
            return []
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(repository_stage_routes)")
        }
        effort = "effort" if "effort" in columns else "NULL"
        return connection.execute(
            f"SELECT repository,stage,provider,model,{effort} FROM repository_stage_routes ORDER BY repository,stage"
        ).fetchall()
    finally:
        connection.close()


def validate_config(paths, online=True, on_check=None, discover=False):
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

    try:
        routes = registered_routes(paths)
    except sqlite3.Error:
        add("stage_routes", "error", "Cannot read saved provider/model assignments")
        routes = []
    assigned = set()
    pairs = set()
    for repository, stage, provider, model, effort in routes:
        if stage == "discovery":
            assigned.add(repository)
        try:
            route = config.route(provider, model, effort=effort)
            selected = config.with_effort(
                config.all_providers()[provider].for_model(model), route.get("effort")
            )
            pairs.add((provider, model, selected.effort))
            add(
                f"stage:{repository}:{stage}",
                "ok",
                "Saved provider, model, and effort are configured",
            )
        except ValueError as exc:
            add(f"stage:{repository}:{stage}", "error", str(exc))
    for repository, _ in repositories:
        if repository not in assigned:
            try:
                route = config.route()
                selected = config.all_providers()[route["provider"]].for_model(route["model"])
                pairs.add((route["provider"], route["model"], selected.effort))
            except ValueError as exc:
                add(f"stage:{repository}:discovery", "error", str(exc))
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
        catalog = None
        if discover:
            catalog = asyncio.run(discover_models(config, name))["providers"][0]
            known = {entry["model"] for entry in catalog["models"]}
            for provider, model, effort in pairs:
                if provider == name and model and model not in known:
                    catalog["models"].append(
                        {
                            "model": model,
                            "name": model,
                            "efforts": None,
                            "default_effort": effort,
                            "resolved_model": None,
                            "source": "stage",
                            "access": "unverified",
                            "selectable": not settings.models or model in settings.models,
                            "configured": False,
                        }
                    )
                    known.add(model)
            catalog["models"].sort(key=lambda entry: entry["model"])
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
                            "Set a provider model, model catalog, or repository stage model to verify access",
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
            client = GitHub(settings)
        except (httpx.InvalidURL, ValueError):
            add(label, "error", "Cannot create the GitHub client; check github.api_url")
            continue
        try:
            identity = github_check(client, "user", label, add)
            if identity is not None:
                for repository, account in repositories:
                    if account == name:
                        github_check(client, f"repos/{repository}", f"repository:{repository}", add)
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


def github_check(client, path, label, add):
    try:
        result = client.request("GET", path)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        messages = {
            401: "GitHub rejected the token; check whether it is invalid or expired",
            403: "GitHub denied access; check permissions, organization authorization, or rate limits",
            404: "Repository does not exist or this account cannot access it",
        }
        add(label, "error", messages.get(code, f"GitHub returned HTTP {code}"))
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
