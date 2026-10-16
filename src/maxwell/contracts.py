import hashlib
import json
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model, model_validator

DEFAULT_SCAN_CANDIDATES = 100
CONCISE_OUTPUT = (
    "Write very concise explanations. Summary: one sentence. Other prose fields: at most two "
    "short sentences each. Cite decisive path:line evidence and the action needed; omit "
    "narration, repeated facts, and restating the finding. Aim for at most 120 words of "
    "explanatory prose per finding or decision, preserving necessary evidence and uncertainty."
)
Factor = Literal["complexity", "duplication", "coupling", "test_runtime", "test_flakiness"]
FACTORS = get_args(Factor)


def unique_factors(values):
    if len(values) != len(set(values)):
        raise ValueError("Factor associations must be unique")
    return sorted(values)


FactorAssociations = Annotated[list[Factor], Field(max_length=5), AfterValidator(unique_factors)]


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
    factors: FactorAssociations = Field(default_factory=list)

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


class EstimateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    low: float = Field(ge=0)
    high: float = Field(ge=0)

    @model_validator(mode="after")
    def check_order(self):
        if self.low > self.high:
            raise ValueError("Estimate range must be ordered")
        return self


class RatioProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    numerator: float = Field(ge=0)
    denominator: float = Field(gt=0)


class ArchitectureProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    reachable_pairs: float = Field(ge=0)
    possible_dependency_pairs: float = Field(ge=0)
    disconnected_pairs: float = Field(ge=0)
    possible_member_pairs: float = Field(ge=0)

    @model_validator(mode="after")
    def check_counts(self):
        if (
            self.reachable_pairs > self.possible_dependency_pairs
            or self.disconnected_pairs > self.possible_member_pairs
        ):
            raise ValueError("Architecture pairs cannot exceed their populations")
        return self


class FactorProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    factor: Factor
    scenarios: list[RatioProjection] | None = Field(default=None, min_length=2, max_length=2)
    architecture: list[ArchitectureProjection] | None = Field(
        default=None, min_length=2, max_length=2
    )
    evidence: str = Field(min_length=20, max_length=3000)
    assumptions: str = Field(min_length=10, max_length=3000)

    @model_validator(mode="after")
    def check_architecture(self):
        if (self.factor == "coupling") != (self.architecture is not None) or (
            (self.factor != "coupling") != (self.scenarios is not None)
        ):
            raise ValueError("Coupling projections require architecture pair scenarios only")
        return self


class RepairEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    plan: str = Field(min_length=20, max_length=4000)
    validation: str = Field(min_length=20, max_length=3000)
    confidence: float = Field(ge=0, le=1)
    effort_hours: EstimateRange
    measurement_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    protocol: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    projections: list[FactorProjection] = Field(default_factory=list, max_length=5)
    unchanged: FactorAssociations = Field(default_factory=list)
    unchanged_evidence: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def check_factors(self):
        factors = [projection.factor for projection in self.projections]
        if len(factors) != len(set(factors)) or set(factors) & set(self.unchanged):
            raise ValueError("Projected and unchanged factors must be unique and disjoint")
        if self.unchanged and len(self.unchanged_evidence or "") < 20:
            raise ValueError("Unchanged factors require evidence")
        if self.effort_hours.low <= 0:
            raise ValueError("Repair effort must be positive")
        return self


class AssessmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=6000)
    repair: RepairEstimate | None = None
    factors: FactorAssociations | None = None
    verdict: Literal["supported", "rejected", "inconclusive"]
    actionability: Literal["actionable", "not_actionable", "unknown"]
    priority: Literal["P0", "P1", "P2", "P3"] | None
    evidence: str = Field(min_length=20, max_length=6000)


class Spending(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    micros: int = Field(ge=0)
    upper_micros: int | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    source: str = Field(min_length=1, max_length=500)
    as_of: str | None = None
    rates: dict[str, float] = Field(default_factory=dict)
    complete: bool = False

    @model_validator(mode="after")
    def cost_range(self):
        if self.upper_micros is not None and self.upper_micros < self.micros:
            raise ValueError("Upper cost must not be less than the lower estimate")
        return self


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reported: bool = False
    complete: bool = False
    spending: Spending | None = None


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


class RepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["edited", "blocked"]
    summary: str = Field(min_length=1, max_length=3000)
    explanation: str = Field(min_length=20, max_length=6000)
    regression_tests: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=100
    )
    concerns: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=20
    )


class ValidationComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    line: int = Field(ge=1)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    explanation: str = Field(min_length=20, max_length=2000)
    suggestion: str | None = Field(default=None, max_length=2000)


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["repaired", "not_repaired", "inconclusive"]
    summary: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=20, max_length=4000)
    reproduction: Literal["reproduced", "not_reproduced", "unavailable"]
    regression_evidence: str = Field(min_length=20, max_length=2000)
    concerns: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=10
    )
    notes: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=5
    )

    comments: list[ValidationComment] = Field(default_factory=list, max_length=20)
