import hashlib
import json
import re
import time
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from maxwell.contracts import (
    FactorAssociations,
    Finding,
    RepairEstimate,
    Spending,
    Usage,
    ValidationResult,
)

Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
Role = Literal["hunter", "reviewer", "auditor", "reproducer", "fixer", "validator", "human"]


class PublicAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    adapter: Literal["codex", "claude"]
    model: str = Field(min_length=1, max_length=200)
    role: Role
    effort: str | None = Field(default=None, max_length=100)
    prompt_version: str | None = Field(default=None, max_length=100)

    def display(self):
        return f"{self.adapter}/{self.model}, {self.role}"


class Record(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )
    version: Literal[1] = 1
    id: Identifier
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$", max_length=300)
    spending: Spending | None = None


class FindingRecord(Record):
    type: Literal["finding"] = "finding"
    sha: Revision
    execution_id: Identifier
    agent: PublicAgent | None = None
    finding: Finding
    allocated_cost_micros: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_agent(self):
        if self.agent and self.agent.role != "hunter":
            raise ValueError("Finding agent must be a hunter")
        return self


class AssessmentRecord(Record):
    type: Literal["assessment"] = "assessment"
    finding_id: Identifier
    sha: Revision
    role: Literal["reviewer", "auditor", "human"]
    agent: PublicAgent | None = None
    actor: str | None = Field(default=None, min_length=1, max_length=100)
    execution_id: Identifier | None = None
    verdict: Literal["supported", "rejected", "inconclusive"]
    actionability: Literal["actionable", "not_actionable", "unknown"] = "unknown"
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    evidence: str = Field(min_length=20, max_length=6000)
    factors: FactorAssociations | None = None
    repair: RepairEstimate | None = None
    supersedes: Identifier | None = None
    references: list[Identifier] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_actor(self):
        if self.role == "human":
            if not self.actor or self.agent or self.execution_id:
                raise ValueError("Human assessments require an actor, not an agent execution")
        elif not self.agent or self.agent.role != self.role or not self.execution_id or self.actor:
            raise ValueError("Agent assessments require a matching role and execution")
        if self.supersedes == self.id or self.id in self.references:
            raise ValueError("An assessment cannot reference itself")
        return self


class AgentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    method: Literal["explicit", "thompson-source-v1", "thompson-repair-v1", "uniform-unrated-v1"]
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    draw: float | None = Field(default=None, ge=0, le=1)
    candidates: int = Field(ge=1)
    independence: Literal["not_applicable", "different_model", "constrained", "overridden"]

    @model_validator(mode="after")
    def check_draw(self):
        if (self.method in ("thompson-source-v1", "thompson-repair-v1")) != (self.draw is not None):
            raise ValueError("Only Thompson sampling requires a selection draw")
        return self


class ExecutionRecord(Record):
    type: Literal["execution"] = "execution"
    sha: Revision | None = None
    agent: PublicAgent | None = None
    state: Literal["completed", "failed", "cancelled", "interrupted", "skipped"]
    summary: str = Field(min_length=1, max_length=6000)
    started: float | None = Field(default=None, ge=0)
    finished: float = Field(ge=0)
    accounting: Literal["tokens", "usd", "unpriced"]
    cost_micros: int | None = Field(default=None, ge=0)
    usage: Usage = Field(default_factory=Usage)
    finding_ids: list[Identifier] = Field(default_factory=list)
    agent_selection: AgentSelection | None = None


class RepairCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    command: list[str] = Field(max_length=100)
    state: Literal["passed", "failed", "timeout", "unavailable", "cancelled"]
    returncode: int | None = None
    seconds: float = Field(default=0, ge=0)
    detail: str = Field(default="", max_length=2000)
    log: str | None = None


class RepairRecord(Record):
    type: Literal["repair"] = "repair"
    finding_id: Identifier
    sha: Revision
    execution_id: Identifier
    agent: PublicAgent
    references: list[Identifier] = Field(min_length=1, max_length=20)
    plan: RepairEstimate | None = None
    outcome: Literal["prepared", "checks_failed", "blocked", "failed", "cancelled", "interrupted"]
    summary: str = Field(min_length=1, max_length=6000)
    explanation: str = Field(default="", max_length=6000)
    branch: str = Field(pattern=r"^maxwell/repair/[a-f0-9]{32}$")
    worktree: str = Field(min_length=1, max_length=2000)
    commit: Revision | None = None
    files: list[str] = Field(default_factory=list, max_length=1000)
    regression_tests: list[str] = Field(default_factory=list, max_length=100)
    checks: list[RepairCheck] = Field(default_factory=list, max_length=10)
    concerns: list[str] = Field(default_factory=list, max_length=50)
    availability: Literal["local-only"] = "local-only"
    parent_sha: Revision | None = None
    previous_head_sha: Revision | None = None
    previous_id: Identifier | None = None
    lineage_id: Identifier | None = None

    @model_validator(mode="after")
    def check_prepared(self):
        if self.previous_head_sha and not self.previous_id:
            raise ValueError("A previous PR head requires a repair predecessor")
        if bool(self.previous_id) != bool(self.parent_sha) or bool(self.previous_id) != bool(
            self.lineage_id
        ):
            raise ValueError("Repair amendments require predecessor, parent and lineage")
        if self.branch != f"maxwell/repair/{self.lineage_id or self.execution_id}":
            raise ValueError("Repair branch must match its lineage")
        if self.agent.role != "fixer":
            raise ValueError("Repair agent must be a fixer")
        if self.outcome == "prepared" and (
            not self.commit
            or not self.files
            or not self.checks
            or any(check.state != "passed" for check in self.checks)
        ):
            raise ValueError("Prepared repairs require a commit, changes, and passing host checks")
        return self


class RepairPullRequestRecord(Record):
    type: Literal["pull_request"] = "pull_request"
    finding_id: Identifier
    finding_issue: int = Field(ge=1)
    repair_id: Identifier
    sha: Revision
    head_sha: Revision
    base_sha: Revision
    branch: str = Field(pattern=r"^maxwell/repair/[a-f0-9]{32}$")
    base_branch: str = Field(min_length=1, max_length=200)
    number: int = Field(ge=1)
    provenance: Literal["fixer", "external"] = "fixer"
    previous_id: Identifier | None = None


class MeasurementFactor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    numerator: float = Field(ge=0)
    denominator: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=100)


class ReusedMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    id: Identifier
    sha: Revision
    measured_at: float | None = Field(default=None, ge=0, le=253402300799)
    environment_fingerprint: str | None = None


class MeasurementRecord(Record):
    type: Literal["measurement"] = "measurement"
    sha: Revision
    analyzer_version: str = Field(min_length=1, max_length=100)
    policy_version: str = Field(min_length=1, max_length=100)
    coverage: float = Field(ge=0, le=1)
    factors: dict[str, MeasurementFactor]
    exclusions: list[str] = Field(default_factory=list, max_length=1000)
    measured_at: float | None = Field(default=None, ge=0)
    protocol: str | None = None
    policy: dict = Field(default_factory=dict)
    factor_status: dict[str, str] = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    environment_fingerprint: str | None = None
    baseline_id: Identifier | None = None

    @model_validator(mode="after")
    def check_evidence(self):
        try:
            json.dumps([self.policy, self.evidence], allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("Measurement metadata must contain finite JSON values") from None
        tests = self.evidence.get("tests", {})
        source = self.evidence.get("source", {})
        if not isinstance(source, dict) or not isinstance(source.get("excluded_files", []), list):
            raise ValueError("Source evidence must be an object with an exclusion list")
        if not isinstance(tests, dict):
            raise ValueError("Test evidence must be an object")
        reused = self.evidence.get("reused", {})
        if not isinstance(reused, dict) or set(reused) - {"source", "tests"}:
            raise ValueError("Reused evidence must identify source or tests stages")
        for origin in reused.values():
            ReusedMeasurement.model_validate(origin)
        for key in (
            "requested_runs",
            "valid_runs",
            "warmups",
            "collected_tests",
            "observed_tests",
            "skipped_tests",
            "observed_flaky_count",
            "persistent_failure_count",
        ):
            if key in tests and (not isinstance(tests[key], int) or tests[key] < 0):
                raise ValueError("Test evidence counts must be nonnegative integers")
        if "median_seconds" in tests and (
            not isinstance(tests["median_seconds"], (int, float)) or tests["median_seconds"] < 0
        ):
            raise ValueError("Test duration must be a nonnegative number")
        slow = tests.get("slow_tests", [])
        if not isinstance(slow, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("test"), str)
            or not isinstance(item.get("median_seconds"), (int, float))
            or item["median_seconds"] < 0
            for item in slow
        ):
            raise ValueError("Slow-test evidence needs test IDs and nonnegative durations")
        return self


class ValidationRecord(Record):
    type: Literal["validation"] = "validation"
    finding_id: Identifier
    repair_id: Identifier
    sha: Revision
    base_sha: Revision
    execution_id: Identifier
    agent: PublicAgent
    references: list[Identifier] = Field(min_length=1, max_length=20)
    state: Literal["completed", "failed", "cancelled", "interrupted"]
    outcome: Literal["accepted", "rejected", "inconclusive"]
    summary: str = Field(min_length=1, max_length=6000)
    result: ValidationResult | None = None
    checks: list[RepairCheck] = Field(default_factory=list, max_length=10)
    replay: RepairCheck | None = None
    failures: list[dict] = Field(default_factory=list, max_length=50)
    before: MeasurementRecord | None = None
    after: MeasurementRecord | None = None
    supersedes: list[Identifier] = Field(default_factory=list, max_length=100)
    pr_id: Identifier | None = None
    pr_head: Revision | None = None
    pr_base: Revision | None = None
    tested_sha: Revision | None = None
    comparison_base_sha: Revision | None = None
    merge_conflicts: list[str] = Field(default_factory=list)

    @property
    def requires_rebase(self):
        return (
            self.type == "validation"
            and self.state == "failed"
            and (
                bool(self.merge_conflicts)
                or self.summary.startswith(
                    "Validation failed: Repair conflicts with target base in: "
                )
            )
        )

    @model_validator(mode="after")
    def check_validation(self):
        if len({bool(self.pr_id), bool(self.pr_head), bool(self.pr_base)}) != 1:
            raise ValueError("PR validation requires a revision reference and pinned head/base")
        if self.agent.role != "validator":
            raise ValueError("Validation agent must be a validator")
        if self.id in self.supersedes:
            raise ValueError("Validation cannot supersede itself")
        for measurement, sha in (
            (self.before, self.comparison_base_sha or self.base_sha),
            (self.after, self.tested_sha or self.sha),
        ):
            if measurement and (
                measurement.repository != self.repository or measurement.sha != sha
            ):
                raise ValueError("Validation measurements must match its repository and commits")
        if self.state != "completed" and self.outcome != "inconclusive":
            raise ValueError("Incomplete validation must remain inconclusive")
        if self.outcome == "accepted" and (
            not self.result
            or self.result.verdict != "repaired"
            or not self.checks
            or any(check.state != "passed" for check in self.checks)
        ):
            raise ValueError("Accepted validation requires source support and passing host checks")
        if self.outcome == "rejected" and (
            not self.result or self.result.verdict != "not_repaired"
        ):
            raise ValueError("Rejected validation requires concrete independent source evidence")
        return self


class IntegrationRecord(ValidationRecord):
    type: Literal["integration"] = "integration"
    merge_sha: Revision


class StageExecution(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )
    id: Identifier = Field(default_factory=lambda: uuid.uuid4().hex)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$", max_length=300)
    provider: str = Field(min_length=1, max_length=200)
    agent: PublicAgent
    sha: Revision | None = None
    finding_id: Identifier | None = None
    input_record_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    state: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"] = (
        "queued"
    )
    created: float = Field(default_factory=time.time, ge=0)
    started: float | None = Field(default=None, ge=0)
    finished: float | None = Field(default=None, ge=0)
    lease: Identifier | None = None
    artifact: str | None = None
    accounting: Literal["usd", "unpriced"] = "usd"
    reserved_micros: int = Field(default=0, ge=0)
    cost_micros: int | None = Field(default=None, ge=0)
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def check_state(self):
        if self.agent.role in ("hunter", "human"):
            raise ValueError("Discovery uses scans; humans do not have stage executions")
        if self.state == "running" and (self.started is None or self.lease is None):
            raise ValueError("Running executions require a start time and lease")
        if self.state not in ("queued", "running") and self.finished is None:
            raise ValueError("Finished executions require a finish time")
        if self.accounting == "unpriced" and self.reserved_micros:
            raise ValueError("Unpriced executions cannot reserve dollars")
        return self


PublicRecord = Annotated[
    FindingRecord
    | AssessmentRecord
    | ExecutionRecord
    | MeasurementRecord
    | RepairRecord
    | ValidationRecord
    | RepairPullRequestRecord
    | IntegrationRecord,
    Field(discriminator="type"),
]
RECORD_ADAPTER = TypeAdapter(PublicRecord)
RECORD_PATTERN = re.compile(r"^<!-- maxwell:record:v1\n(.*?)\n-->$", re.MULTILINE | re.DOTALL)


def encode_record(record: PublicRecord):
    validated = RECORD_ADAPTER.validate_python(record)
    payload = validated.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- maxwell:record:v1\n{payload}\n-->"


def decode_record(body):
    matches = RECORD_PATTERN.findall(body or "")
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Expected one Maxwell record")
    return RECORD_ADAPTER.validate_json(matches[0])


def record_digest(record):
    values = record.model_dump(mode="json")

    if values["spending"] is None:
        values.pop("spending")
    if isinstance(record, ExecutionRecord) and values["usage"]["spending"] is None:
        values["usage"].pop("spending")
    if isinstance(record, ValidationRecord):
        if not values["merge_conflicts"]:
            del values["merge_conflicts"]
        for name in ("before", "after"):
            if values[name] and values[name]["spending"] is None:
                values[name].pop("spending")
    if isinstance(record, ExecutionRecord) and values["agent_selection"] is None:
        del values["agent_selection"]
    if isinstance(record, FindingRecord) and not values["finding"]["factors"]:
        del values["finding"]["factors"]
    if isinstance(record, AssessmentRecord):
        for key in ("factors", "repair"):
            if values[key] is None:
                del values[key]
    if isinstance(record, MeasurementRecord):
        for key in (
            "measured_at",
            "protocol",
            "policy",
            "factor_status",
            "evidence",
            "environment_fingerprint",
            "baseline_id",
        ):
            if values[key] is None or values[key] == {}:
                del values[key]
    if isinstance(record, RepairRecord):
        for key in ("parent_sha", "previous_head_sha", "previous_id", "lineage_id"):
            if values[key] is None:
                del values[key]
    if isinstance(record, ValidationRecord):
        for key in ("pr_id", "pr_head", "pr_base", "tested_sha", "comparison_base_sha"):
            if values[key] is None:
                del values[key]
        if values.get("result"):
            for key in ("comments", "notes"):
                if not values["result"].get(key):
                    values["result"].pop(key, None)
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def public_agent(scan):
    execution = json.loads(scan.get("execution") or "{}")
    settings = execution.get("settings", {})
    adapter = settings.get("adapter")
    if adapter not in ("codex", "claude"):
        return None
    roles = {
        "discovery": "hunter",
        "review": "reviewer",
        "audit": "auditor",
        "reproduction": "reproducer",
        "fix": "fixer",
        "repair": "fixer",
        "validation": "validator",
        "validate": "validator",
        "integrate": "validator",
    }
    return PublicAgent(
        adapter=adapter,
        model=scan.get("model") or execution.get("model") or "unknown",
        role=roles[scan.get("stage", "discovery")],
        effort=settings.get("effort"),
    )


def execution_record(scan, finding_ids=()):
    execution = json.loads(scan.get("execution") or "{}")
    if not finding_ids and execution.get("target"):
        finding_ids = [execution["target"]["finding"]["id"]]
    return ExecutionRecord(
        id=scan["id"],
        repository=scan["repository"],
        sha=scan.get("sha"),
        agent=public_agent(scan),
        state=scan["state"],
        summary=scan.get("summary") or "Execution finished.",
        started=scan.get("started"),
        finished=scan["finished"],
        accounting="tokens",
        usage=Usage.model_validate_json(scan.get("usage") or "{}"),
        spending=Usage.model_validate_json(scan.get("usage") or "{}").spending,
        finding_ids=list(finding_ids),
        agent_selection=execution.get("agent_selection"),
    )
