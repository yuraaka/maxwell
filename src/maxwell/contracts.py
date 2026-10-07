import hashlib
import json
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

DEFAULT_SCAN_CANDIDATES = 100


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
    findings: list[Finding]

    @classmethod
    def bounded(cls, limit: int):
        return create_model(
            "BoundedScanResult", __base__=cls, findings=(list[Finding], Field(max_length=limit))
        )


class SelectionStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collected: int = Field(ge=0)
    selected: int = Field(ge=0)
    skipped: int = Field(ge=0)
    below_confidence: int = Field(default=0, ge=0)
    duplicate: int = Field(default=0, ge=0)
    already_known: int = Field(default=0, ge=0)
    publication_limit: int = Field(default=0, ge=0)
    review_queue_limit: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_counts(self):
        reasons = (
            self.below_confidence
            + self.duplicate
            + self.already_known
            + self.publication_limit
            + self.review_queue_limit
        )
        if self.collected != self.selected + self.skipped or reasons != self.skipped:
            raise ValueError("Candidate selection counts do not reconcile")
        return self


class AssessmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=6000)
    verdict: Literal["supported", "rejected", "inconclusive"]
    actionability: Literal["actionable", "not_actionable", "unknown"]
    priority: Literal["P0", "P1", "P2", "P3"] | None
    evidence: str = Field(min_length=20, max_length=6000)


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
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
    written = min(usage.cache_write_tokens, usage.input_tokens - cached)
    cache_price = provider.cached_input_usd_per_mtok
    if cache_price is None:
        cache_price = provider.input_usd_per_mtok
    cost = (
        Decimal(usage.input_tokens - cached - written) * Decimal(str(provider.input_usd_per_mtok))
        + Decimal(cached) * Decimal(str(cache_price))
        + Decimal(written)
        * Decimal(
            str(
                provider.cache_write_usd_per_mtok
                if provider.cache_write_usd_per_mtok is not None
                else provider.input_usd_per_mtok
            )
        )
        + Decimal(usage.output_tokens) * Decimal(str(provider.output_usd_per_mtok))
    )
    return int(cost.to_integral_value(rounding=ROUND_CEILING))
