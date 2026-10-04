import json
import os
import shutil
import tomllib
import uuid
from pathlib import Path

BASE_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SYSTEMROOT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "CODEX_CA_CERTIFICATE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}

CODEX_CONNECTION_KEYS = {
    "model_provider",
    "openai_base_url",
    "chatgpt_base_url",
    "cli_auth_credentials_store",
    "forced_login_method",
    "forced_chatgpt_workspace_id",
    "model_catalog_json",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_supports_reasoning_summaries",
    "service_tier",
    "features",
}


def read_settings(path, adapter):
    try:
        data = (
            {}
            if not path.exists()
            else (
                tomllib.loads(path.read_text())
                if adapter == "codex"
                else json.loads(path.read_text())
            )
        )
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("Native provider configuration could not be read or parsed") from None
    return data


def overlay_settings(base, overlay):
    result = dict(base)
    for key, value in overlay.items():
        result[key] = (
            overlay_settings(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else value
        )
    return result


def native_settings(settings):
    home = Path(settings.home).expanduser().resolve()
    if not home.is_dir():
        raise ValueError("Provider home is not an existing directory")
    path = home / ("config.toml" if settings.adapter == "codex" else "settings.json")
    data = read_settings(path, settings.adapter)
    if settings.adapter == "codex":
        if "profile" in data or "profiles" in data:
            raise ValueError(
                "Legacy Codex profiles are unsupported; move settings to NAME.config.toml "
                "and set profile: NAME in the Maxwell provider"
            )
        if settings.profile is not None:
            profile_path = home / f"{settings.profile}.config.toml"
            if not profile_path.is_file():
                raise ValueError("Codex profile file is missing; expected HOME/NAME.config.toml")
            data = overlay_settings(data, read_settings(profile_path, settings.adapter))
            if "profile" in data or "profiles" in data:
                raise ValueError("Codex profile files must use top-level settings")
    return data, home


def dependencies(settings, data):
    names = set(settings.env)
    if settings.adapter == "codex":
        provider = data.get("model_providers", {}).get(data.get("model_provider", "openai"), {})
        if isinstance(provider, dict):
            if provider.get("env_key") and not provider.get("requires_openai_auth"):
                names.add(provider["env_key"])
            names.update(provider.get("env_http_headers", {}).values())
    if not all(isinstance(name, str) and name for name in names):
        raise ValueError("Native provider environment references are invalid")
    return names


def native_errors(settings):
    try:
        data, _ = native_settings(settings)
        if settings.adapter == "claude":
            defaults = data.get("env", {})
            if not isinstance(defaults, dict) or not all(
                isinstance(value, str) for value in defaults.values()
            ):
                raise ValueError("Native provider environment settings are invalid")
        errors = [
            f"Missing gateway environment variable {name}"
            for name in sorted(dependencies(settings, data))
            if not os.environ.get(name)
        ]
        if settings.adapter == "codex":
            provider = data.get("model_providers", {}).get(data.get("model_provider", "openai"), {})
            helper = provider.get("auth", {}).get("command") if isinstance(provider, dict) else None
        else:
            helper = data.get("apiKeyHelper")
        if helper and settings.adapter == "codex" and not shutil.which(helper):
            errors.append("Gateway credential helper executable was not found")
        return errors
    except ValueError as exc:
        return [str(exc)]
    except (TypeError, AttributeError):
        return ["Provider home or native configuration is invalid"]


def toml_value(value):
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(f"{json.dumps(key)}={toml_value(child)}" for key, child in value.items())
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(toml_value(child) for child in value) + "]"
    if isinstance(value, str) or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int) or isinstance(value, float):
        return str(value)
    raise ValueError("Native connection configuration contains an unsupported value type")


def codex_connection_overrides(settings, native, environment):
    connection = {key: value for key, value in native.items() if key in CODEX_CONNECTION_KEYS}
    if "model_catalog_json" in connection:
        if not isinstance(connection["model_catalog_json"], str):
            raise ValueError("Native model catalog path must be a string")
        path = Path(connection["model_catalog_json"]).expanduser()
        connection["model_catalog_json"] = str(
            path if path.is_absolute() else Path(settings.home) / path
        )
    name = native.get("model_provider", "openai")
    definition = native.get("model_providers", {}).get(name)
    secrets = []
    if definition is not None:
        definition = dict(definition)

        def credential(value):
            if not isinstance(value, str):
                raise ValueError("Native provider credential must be a string")
            key = f"MAXWELL_CODEX_{uuid.uuid4().hex.upper()}"
            environment[key] = value
            secrets.append(value)
            return key

        if "experimental_bearer_token" in definition:
            definition["env_key"] = credential(definition.pop("experimental_bearer_token"))
        headers = dict(definition.get("env_http_headers", {}))
        for header, value in definition.pop("http_headers", {}).items():
            if header not in headers:
                headers[header] = credential(value)
        if headers:
            definition["env_http_headers"] = headers
        connection["model_providers"] = {name: definition}
    return [f"{key}={toml_value(value)}" for key, value in connection.items()], secrets


def secret_values(value, key=""):
    values = []
    if isinstance(value, dict):
        for name, child in value.items():
            values.extend(secret_values(child, str(name)))
    elif isinstance(value, list):
        for child in value:
            values.extend(secret_values(child, key))
    elif isinstance(value, str) and any(
        word in key.lower() for word in ("token", "key", "secret", "authorization", "password")
    ):
        values.append(value)
    return values


def runtime_environment(paths, settings):
    environment = {key: value for key, value in os.environ.items() if key in BASE_ENV}
    secrets = []
    data = {}
    if settings.home is not None:
        errors = native_errors(settings)
        if errors:
            raise ValueError("; ".join(errors))
        data, home = native_settings(settings)
        defaults = data.get("env", {}) if settings.adapter == "claude" else {}
        if not isinstance(defaults, dict) or not all(
            isinstance(value, str) for value in defaults.values()
        ):
            raise ValueError("Native provider environment settings are invalid")
        environment.update(defaults)
        for name in dependencies(settings, data):
            environment[name] = os.environ[name]
            secrets.append(os.environ[name])
        environment["CODEX_HOME" if settings.adapter == "codex" else "CLAUDE_CONFIG_DIR"] = str(
            home
        )
        secrets.extend(secret_values(data))
        credential_file = home / (
            "auth.json" if settings.adapter == "codex" else ".credentials.json"
        )
        if credential_file.is_file():
            try:
                secrets.extend(secret_values(json.loads(credential_file.read_text())))
            except (OSError, ValueError):
                pass
    else:
        home = paths.root / "runtimes" / "codex"
        home.mkdir(parents=True, exist_ok=True)
        environment.update(CODEX_HOME=str(home), CODEX_API_KEY=settings.api_key.get_secret_value())
    environment["NO_COLOR"] = "1"
    secrets.extend(
        value
        for name, value in environment.items()
        if any(word in name.lower() for word in ("token", "key", "secret", "password", "proxy"))
    )
    return environment, data, secrets


def redact(text, config, secrets=()):
    text = config.redact(text)
    for secret in sorted(set(secrets) - {""}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text
