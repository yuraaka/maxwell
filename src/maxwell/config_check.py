import sqlite3

import httpx

from maxwell.github import GitHub


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


def check_config(paths, online=True):
    checks = []

    def add(name, status, message):
        checks.append({"name": name, "status": status, "message": message})

    def report():
        return {
            "config": str(paths.config),
            "valid": not any(check["status"] == "error" for check in checks),
            "online": online,
            "checks": checks,
        }

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
    add(
        "provider",
        "skipped",
        "Provider credentials and model access are not verified; no model call was made",
    )
    for check in checks:
        for key in ("name", "message"):
            check[key] = config.redact(check[key])
    result = report()
    result["config"] = config.redact(result["config"])
    return result


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
