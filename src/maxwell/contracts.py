import hashlib
import json
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=8, max_length=180)
    kind: Literal["bug", "simplification", "duplication", "documentation"]
    priority: Literal["P0", "P1", "P2", "P3"]
    path: str = Field(min_length=1, max_length=500)
    line: int = Field(ge=1)
    symbol: str = Field(max_length=300)
    evidence: str = Field(min_length=20, max_length=6000)
    expected: str = Field(min_length=10, max_length=3000)
    confidence: float = Field(ge=0, le=1)

    def validate_location(self, workspace: Path):
        relative = PurePosixPath(self.path)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("Finding refers outside repository source")
        target = (workspace / self.path).resolve()
        if not target.is_relative_to(workspace.resolve()) or not target.is_file():
            raise ValueError("Finding path does not exist in the snapshot")
        if self.line > len(target.read_text(errors="replace").splitlines()):
            raise ValueError("Finding line does not exist in the snapshot")

    def identity(self, repository: str) -> str:
        title = re.sub(r"\W+", " ", self.title.casefold()).strip()
        value = [repository.casefold(), self.kind, self.path, self.symbol, title]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:32]


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=6000)
    findings: list[Finding] = Field(max_length=20)


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reported: bool = False
    complete: bool = False


def micros(amount) -> int:
    return int((Decimal(str(amount)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def estimate_cost(usage: Usage, provider) -> int | None:
    if not usage.reported or not usage.complete:
        return None
    if provider.input_usd_per_mtok is None or provider.output_usd_per_mtok is None:
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cache_price = provider.cached_input_usd_per_mtok
    if cache_price is None:
        cache_price = provider.input_usd_per_mtok
    cost = (
        Decimal(usage.input_tokens - cached) * Decimal(str(provider.input_usd_per_mtok))
        + Decimal(cached) * Decimal(str(cache_price))
        + Decimal(usage.output_tokens) * Decimal(str(provider.output_usd_per_mtok))
    )
    return int(cost.to_integral_value(rounding=ROUND_CEILING))
