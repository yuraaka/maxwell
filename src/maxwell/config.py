import base64
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GitHubAccount(Settings):
    token: SecretStr = SecretStr("")
    allowed_users: list[str] = Field(default_factory=list)


class GitHubSettings(Settings):
    token: SecretStr = SecretStr("")
    api_url: str = "https://api.github.com"
    allowed_users: list[str] = Field(default_factory=list)
    sync_seconds: float = Field(default=60, ge=1)
    accounts: dict[str, GitHubAccount] = Field(default_factory=dict)

    @field_validator("api_url")
    @classmethod
    def valid_api_url(cls, value):
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in ("http", "https")
                and url.hostname
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(character.isspace() for character in value)
            )
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTP(S) API URL without credentials, query, or fragment")
        return value

    @model_validator(mode="after")
    def reserved_account(self):
        if "default" in self.accounts:
            raise ValueError("The default account uses github.token and github.allowed_users")
        return self

    def for_account(self, name="default"):
        if name == "default":
            return self.model_copy(update={"accounts": {}})
        if name not in self.accounts:
            raise ValueError(f"Unknown GitHub account {name!r}; configure github.accounts first")
        account = self.accounts[name]
        return self.model_copy(
            update={"token": account.token, "allowed_users": account.allowed_users, "accounts": {}}
        )


Effort = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]


class ModelSettings(Settings):
    effort: Effort | None = None
    efforts: list[Effort] | None = None
    input_usd_per_mtok: float | None = Field(default=None, ge=0)
    cached_input_usd_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_usd_per_mtok: float | None = Field(default=None, ge=0)
    output_usd_per_mtok: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def complete_prices(self):
        if (self.input_usd_per_mtok is None) != (self.output_usd_per_mtok is None):
            raise ValueError("Set both input and output prices, or leave both unpriced")
        if self.effort == "default":
            raise ValueError("Omit effort to use the provider default")
        if self.efforts is not None and self.effort is not None and self.effort not in self.efforts:
            raise ValueError("Effort is not in the model's supported efforts")
        return self


class ProviderSettings(ModelSettings):
    adapter: Literal["codex", "claude"] = "codex"
    executable: str = ""
    home: str | None = None
    profile: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    env: list[str] = Field(default_factory=list)
    model: str = ""
    models: dict[str, ModelSettings] = Field(default_factory=dict)
    api_key: SecretStr = SecretStr("")

    @field_validator("home", "executable")
    @classmethod
    def connection_paths(cls, value, info: ValidationInfo):
        if value and (info.field_name == "home" or "/" in value):
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = Path((info.context or {}).get("config_root", Path.cwd())) / path
            return str(path.resolve())
        return value

    @field_validator("models", mode="before")
    @classmethod
    def model_catalog(cls, value):
        if isinstance(value, list):
            return {name: {} for name in value}
        return value

    @model_validator(mode="after")
    def connection(self):
        if not self.executable:
            self.executable = "codex" if self.adapter == "codex" else "claude"
        if self.home is not None and not self.home.strip():
            raise ValueError("Provider home must not be empty")
        if self.home is not None and self.api_key.get_secret_value():
            raise ValueError("Use either a native home or an API key for a provider")
        if self.adapter == "claude" and self.home is None:
            raise ValueError("Claude Code requires its existing configuration home")
        if self.profile is not None and (self.adapter != "codex" or self.home is None):
            raise ValueError("A native profile requires a Codex provider with a home")
        if any(not name or not name.replace("_", "").isalnum() for name in self.env):
            raise ValueError("Provider environment entries must be variable names")
        return self

    def for_model(self, model):
        if (
            not isinstance(model, str)
            or not model.strip()
            or model.startswith("-")
            or any(character.isspace() for character in model)
        ):
            raise ValueError("Specify a valid --model")
        if self.models and model not in self.models:
            raise ValueError("Model is not in the selected provider's model catalog")
        prices = self.models.get(model)
        if prices is None:
            prices = (
                ModelSettings.model_validate(
                    self.model_dump(include=set(ModelSettings.model_fields))
                )
                if model == self.model
                else ModelSettings()
            )
        values = prices.model_dump()
        for key in ("effort", "efforts"):
            if values[key] is None:
                values[key] = getattr(self, key)
        ModelSettings.model_validate(values)
        return self.model_copy(update={"model": model, **values})

    def default_model(self):
        if self.model:
            return self.model
        if len(self.models) == 1:
            return next(iter(self.models))
        if self.models:
            raise ValueError("Specify --model when the provider has multiple models")
        if self.home is not None:
            from maxwell.provider_runtime import native_settings

            model = native_settings(self)[0].get("model")
            if isinstance(model, str) and model:
                return model
        raise ValueError("Specify --model; the provider has no configured default model")


class Limits(Settings):
    daily_budget_usd: float = Field(default=20, gt=0)
    scan_reservation_usd: float = Field(default=1, gt=0)
    scan_timeout_seconds: float = Field(default=600, ge=1)
    scan_interval_seconds: float = Field(default=1800, ge=1)
    max_findings_per_scan: int = Field(default=3, ge=1, le=20)
    human_queue_max: int = Field(default=6, ge=1)
    minimum_confidence: float = Field(default=0.8, ge=0, le=1)
    shutdown_grace_seconds: float = Field(default=10, ge=0, le=300)
    daily_scan_limit: int = Field(default=20, ge=1)


class Config(Settings):
    version: Literal[1] = 1
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="before")
    @classmethod
    def provider_forms(cls, data):
        if isinstance(data, dict) and data.get("providers") and "provider" in data:
            raise ValueError("Use either provider or providers in config.yaml")
        return data

    @model_validator(mode="after")
    def reservation_fits(self):
        if self.limits.scan_reservation_usd > self.limits.daily_budget_usd:
            raise ValueError("scan reservation must fit within the daily budget")
        return self

    def readiness_errors(self):
        errors = []
        if not shutil.which("git"):
            errors.append("git: executable was not found on PATH")
        for name, provider in self.all_providers().items():
            label = "provider" if not self.providers else f"providers.{name}"
            if not shutil.which(provider.executable):
                errors.append(f"{label}.executable: executable was not found on PATH")
            if provider.home is not None:
                from maxwell.provider_runtime import native_errors

                errors.extend(f"{label}: {error}" for error in native_errors(provider))
            elif not provider.api_key.get_secret_value().strip():
                errors.append(f"{label}.api_key: set an API key or an existing provider home")
        if not any(secret.get_secret_value().strip() for secret in self.github_tokens()):
            errors.append("github: set github.token or a github.accounts token in config.yaml")
        if not self.providers and self.provider.home is None:
            if not self.provider.model.strip():
                errors.append("provider.model: set a nonempty model identifier")
            for field in ("input_usd_per_mtok", "output_usd_per_mtok"):
                if getattr(self.provider, field) is None:
                    errors.append(f"provider.{field}: set the model price")
        return errors

    def all_providers(self):
        return self.providers or {"default": self.provider}

    def provider_name(self, name=None):
        providers = self.all_providers()
        if name is None:
            if len(providers) != 1:
                raise ValueError("Specify --provider when multiple providers are configured")
            return next(iter(providers))
        if name not in providers:
            raise ValueError(f"Unknown provider {name!r}")
        return name

    def route(self, provider=None, model=None, current=None, effort=None):
        name = self.provider_name(provider or (current["provider"] if current else None))
        settings = self.all_providers()[name]
        if model is None:
            model = (
                current["model"]
                if current and current["provider"] == name
                else settings.default_model()
            )
        selected = settings.for_model(model)
        if effort == "default":
            effort = None
        elif (
            effort is None and current and current["provider"] == name and current["model"] == model
        ):
            effort = current.get("effort")
        self.with_effort(selected, effort)
        return {"provider": name, "model": model, **({"effort": effort} if effort else {})}

    @staticmethod
    def with_effort(settings, effort):
        if effort is None:
            return settings
        if settings.efforts is not None and effort not in settings.efforts:
            raise ValueError("Effort is not in the model's supported efforts")
        try:
            selected = ModelSettings.model_validate({"effort": effort})
        except ValidationError:
            raise ValueError("Specify a valid --effort") from None
        return settings.model_copy(update={"effort": selected.effort})

    def execution(self, route, record):
        name = self.provider_name(route["provider"])
        settings = self.with_effort(
            self.all_providers()[name].for_model(route["model"]), route.get("effort")
        )
        return {
            **route,
            "stage": "discovery",
            "settings": settings.model_dump(mode="json", exclude={"api_key"}),
            "repository": {
                key: record.get(key)
                for key in ("name", "source", "source_kind", "ref", "directory", "github_account")
            },
            "limits": self.limits.model_dump(),
        }

    def ready(self):
        errors = self.readiness_errors()
        if errors:
            raise ValueError("; ".join(errors))

    def github_tokens(self):
        return [self.github.token, *(account.token for account in self.github.accounts.values())]

    def redact(self, text: str) -> str:
        values = {
            secret.get_secret_value()
            for secret in (
                *self.github_tokens(),
                *(provider.api_key for provider in self.all_providers().values()),
            )
        }
        values.update(
            base64.b64encode(f"x-access-token:{secret.get_secret_value()}".encode()).decode()
            for secret in self.github_tokens()
            if secret.get_secret_value()
        )
        for value in sorted(values - {""}, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text


class Paths:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else Path.home() / ".maxwell"
        self.config = self.root / "config.yaml"
        self.db = self.root / "state.sqlite"
        self.socket = self.root / "run" / "daemon.sock"
        self.lock = self.root / "run" / "daemon.lock"
        self.log = self.root / "logs" / "daemon.log"

    def initialize(self):
        os.umask(0o077)
        for name in ("", "run", "repos", "artifacts", "logs", "runtimes", "cache", "reports"):
            path = self.root / name
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def load(self, detailed=False) -> Config:
        if not self.config.exists():
            raise ValueError("Run `maxwell config init` first")
        if self.config.stat().st_mode & 0o077:
            raise ValueError(f"Restrict configuration permissions: chmod 600 {self.config}")
        try:
            data = yaml.safe_load(self.config.read_text())
            return Config.model_validate(
                {} if data is None else data, context={"config_root": self.root}
            )
        except Exception as exc:
            # Validation errors can embed credential values.
            if detailed and isinstance(exc, ValidationError):
                models = (
                    Config,
                    GitHubSettings,
                    GitHubAccount,
                    ProviderSettings,
                    ModelSettings,
                    Limits,
                )
                fields = {name for model in models for name in model.model_fields}
                errors = []
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                ):
                    location = (
                        ".".join(str(key) if key in fields else "<key>" for key in error["loc"])
                        or "config"
                    )
                    errors.append(f"{location}: {error['msg']}")
                raise ValueError("Invalid config.yaml: " + "; ".join(errors)) from exc
            if detailed and isinstance(exc, yaml.YAMLError):
                mark = getattr(exc, "problem_mark", None)
                location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
                raise ValueError(f"Invalid config.yaml: malformed YAML{location}") from exc
            raise ValueError("Invalid config.yaml; check its keys and value types") from exc

    def init_config(self):
        self.initialize()
        data = Config().model_dump(mode="json", exclude_none=True, exclude={"provider"})
        data["providers"] = {"codex": {"adapter": "codex", "home": "~/.codex"}}
        with self.config.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, sort_keys=False)
        self.config.chmod(0o600)
