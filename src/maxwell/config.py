import base64
import os
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
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


class ProviderSettings(Settings):
    adapter: Literal["codex"] = "codex"
    executable: str = "codex"
    model: str = ""
    api_key: SecretStr = SecretStr("")
    input_usd_per_mtok: float | None = Field(default=None, ge=0)
    cached_input_usd_per_mtok: float | None = Field(default=None, ge=0)
    output_usd_per_mtok: float | None = Field(default=None, ge=0)


class Limits(Settings):
    daily_budget_usd: float = Field(default=20, gt=0)
    scan_reservation_usd: float = Field(default=1, gt=0)
    scan_timeout_seconds: float = Field(default=600, ge=1)
    scan_interval_seconds: float = Field(default=1800, ge=1)
    max_findings_per_scan: int = Field(default=3, ge=1, le=20)
    human_queue_max: int = Field(default=6, ge=1)
    minimum_confidence: float = Field(default=0.8, ge=0, le=1)
    shutdown_grace_seconds: float = Field(default=10, ge=0, le=300)


class Config(Settings):
    version: Literal[1] = 1
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def reservation_fits(self):
        if self.limits.scan_reservation_usd > self.limits.daily_budget_usd:
            raise ValueError("scan reservation must fit within the daily budget")
        return self

    def readiness_errors(self):
        errors = []
        if not shutil.which("git"):
            errors.append("git: executable was not found on PATH")
        if not shutil.which(self.provider.executable):
            errors.append("provider.executable: executable was not found on PATH")
        if not any(secret.get_secret_value().strip() for secret in self.github_tokens()):
            errors.append("github: set github.token or a github.accounts token in config.yaml")
        if not self.provider.api_key.get_secret_value().strip():
            errors.append("provider.api_key: set a nonempty API key")
        if not self.provider.model.strip():
            errors.append("provider.model: set a nonempty model identifier")
        for field in ("input_usd_per_mtok", "output_usd_per_mtok"):
            if getattr(self.provider, field) is None:
                errors.append(f"provider.{field}: set the model price")
        return errors

    def ready(self):
        errors = self.readiness_errors()
        if errors:
            raise ValueError("; ".join(errors))

    def github_tokens(self):
        return [self.github.token, *(account.token for account in self.github.accounts.values())]

    def redact(self, text: str) -> str:
        values = {
            secret.get_secret_value() for secret in (*self.github_tokens(), self.provider.api_key)
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
            raise ValueError("Run `maxwell init` first")
        if self.config.stat().st_mode & 0o077:
            raise ValueError(f"Restrict configuration permissions: chmod 600 {self.config}")
        try:
            data = yaml.safe_load(self.config.read_text())
            return Config.model_validate({} if data is None else data)
        except Exception as exc:
            # Validation errors can embed credential values.
            if detailed and isinstance(exc, ValidationError):
                models = (Config, GitHubSettings, GitHubAccount, ProviderSettings, Limits)
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
        with self.config.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(
                Config().model_dump(mode="json", exclude_none=True), stream, sort_keys=False
            )
        self.config.chmod(0o600)
