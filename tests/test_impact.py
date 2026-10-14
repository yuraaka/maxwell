import hashlib
import json

import pytest
from pydantic import ValidationError

from maxwell.contracts import FACTORS, Finding, RepairEstimate
from maxwell.impact import evaluate_repair, fix_queue, queue_report, repair_decision
from maxwell.measurement import ANALYZER, POLICY, digest
from maxwell.records import (
    AssessmentRecord,
    FindingRecord,
    MeasurementFactor,
    MeasurementRecord,
    PublicAgent,
    decode_record,
    encode_record,
    record_digest,
)
from maxwell.reporting import record_report
from maxwell.store import Store


@pytest.fixture
def inputs(finding):
    item = Finding(**{**finding, "factors": ["complexity"]})
    claim = FindingRecord(
        id=item.identity("acme/repo"),
        repository="acme/repo",
        sha="f" * 40,
        execution_id="e" * 32,
        finding=item,
    )
    policy = {"weights": dict.fromkeys(FACTORS, 0.2)}
    baseline = MeasurementRecord(
        id="b" * 32,
        repository=claim.repository,
        sha=claim.sha,
        analyzer_version=ANALYZER,
        policy_version=POLICY,
        coverage=1,
        factors={
            name: MeasurementFactor(numerator=50, denominator=100, unit="ratio") for name in FACTORS
        },
        factor_status=dict.fromkeys(FACTORS, "measured"),
        policy=policy,
        protocol=digest(policy),
    )
    repair = RepairEstimate(
        plan="Extract shared source logic while preserving caller behavior.",
        validation="Run regression cases covering each affected caller and remeasure.",
        confidence=0.8,
        effort_hours={"low": 1, "high": 2},
        measurement_id=baseline.id,
        protocol=baseline.protocol,
        projections=[
            {
                "factor": "complexity",
                "scenarios": [
                    {"numerator": 10, "denominator": 50},
                    {"numerator": 30, "denominator": 60},
                ],
                "evidence": "source.py:1 branches removed from the shared implementation.",
                "assumptions": "Total complexity changes along with excess complexity.",
            }
        ],
        unchanged=[name for name in FACTORS if name != "complexity"],
        unchanged_evidence="No token clones, dependency relationships, or tests are changed.",
    )
    review = AssessmentRecord(
        id="a" * 32,
        repository=claim.repository,
        sha=claim.sha,
        finding_id=claim.id,
        role="reviewer",
        agent=PublicAgent(adapter="codex", model="model", role="reviewer"),
        execution_id="d" * 32,
        verdict="supported",
        actionability="actionable",
        priority="P1",
        evidence="source.py:1 contains the concrete source behavior described.",
        factors=["complexity"],
        repair=repair,
    )
    return claim, baseline, review


def test_changing_denominator_signed_bounds_and_effort(inputs):
    claim, baseline, review = inputs
    impact = evaluate_repair(claim, review, [baseline], ["complexity"])
    assert impact.status == "usable"
    assert (impact.low, impact.high, impact.rate) == (0, 6, 0)
    projection = review.repair.projections[0].model_copy(
        update={
            "scenarios": [
                type(review.repair.projections[0].scenarios[0])(numerator=40, denominator=50)
            ]
            * 2,
        }
    )
    worse = review.model_copy(
        update={"repair": review.repair.model_copy(update={"projections": [projection]})}
    )
    impact = evaluate_repair(claim, worse, [baseline], ["complexity"])
    assert (impact.low, impact.high, impact.rate) == (-6, -6, 0)


@pytest.mark.parametrize(
    "change,status",
    [
        ({"sha": "e" * 40}, "stale"),
        ({"protocol": "e" * 64}, "incompatible"),
        ({"factor_status": {"complexity": "measured"}}, "partial"),
        ({"evidence": {"reused": {"source": {"id": "e" * 32, "sha": "e" * 40}}}}, "stale"),
    ],
)
def test_invalid_baselines_cannot_rank(inputs, change, status):
    claim, baseline, review = inputs
    assert (
        evaluate_repair(claim, review, [baseline.model_copy(update=change)], ["complexity"]).status
        == status
    )


def test_partial_missing_unassociated_and_new_measurement(inputs):
    claim, baseline, review = inputs
    assert evaluate_repair(claim, review, [], ["complexity"]).status == "missing"
    assert evaluate_repair(claim, review, [baseline], []).status == "invalid"
    partial = review.model_copy(
        update={"repair": review.repair.model_copy(update={"unchanged": []})}
    )
    impact = evaluate_repair(claim, partial, [baseline], ["complexity"])
    assert impact.status == "partial" and impact.low is None and impact.rate is None
    assert impact.reductions == {"complexity": (0, 6)}
    assert (
        evaluate_repair(
            claim, review, [baseline, baseline.model_copy(update={"id": "c" * 32})], ["complexity"]
        ).status
        == "stale"
    )


def test_coupling_uses_pair_populations(inputs):
    claim, baseline, review = inputs
    projection = {
        "factor": "coupling",
        "scenarios": None,
        "architecture": [
            {
                "reachable_pairs": 1,
                "possible_dependency_pairs": 10,
                "disconnected_pairs": 3,
                "possible_member_pairs": 10,
            }
        ]
        * 2,
        "evidence": "source.py:1 narrows reachability and joins disconnected members.",
        "assumptions": "Two packages retain the same public behavior and population.",
    }
    repair = RepairEstimate.model_validate(
        {
            **review.repair.model_dump(),
            "projections": [projection],
            "unchanged": [name for name in FACTORS if name != "coupling"],
        }
    )
    impact = evaluate_repair(
        claim, review.model_copy(update={"repair": repair}), [baseline], ["coupling"]
    )
    assert (impact.low, impact.high, impact.rate) == pytest.approx((6, 6, 2.4))


def test_audit_missing_estimate_blocks_fallback_and_supersession(inputs):
    claim, _, review = inputs
    audit = review.model_copy(
        update={
            "id": "c" * 32,
            "role": "auditor",
            "agent": PublicAgent(adapter="claude", model="model2", role="auditor"),
            "references": [review.id],
            "repair": None,
        }
    )
    decision, level, _, _ = repair_decision(claim, [review, audit])
    assert decision.repair is None and level == "audited"
    assert (
        repair_decision(claim, [review, audit.model_copy(update={"verdict": "inconclusive"})])[1]
        == "not_actionable"
    )
    assert (
        repair_decision(claim, [review, audit.model_copy(update={"references": ["e" * 32]})])[0]
        == review
    )
    updated = review.model_copy(update={"id": "e" * 32, "supersedes": review.id, "repair": None})
    assert repair_decision(claim, [review, updated, audit])[0] == updated
    competitor = review.model_copy(update={"id": "e" * 32, "repair": None})
    assert repair_decision(claim, [review, competitor])[1] == "conflicting"


def test_estimate_contracts_and_legacy_digest(inputs):
    _, _, review = inputs
    for changes in (
        {"effort_hours": {"low": 3, "high": 2}},
        {"confidence": float("nan")},
        {"unchanged_evidence": None},
        {"unchanged": ["complexity"]},
    ):
        with pytest.raises(ValidationError):
            RepairEstimate.model_validate({**review.repair.model_dump(), **changes})
    assert decode_record(encode_record(review)) == review
    legacy = review.model_copy(update={"repair": None, "factors": None})
    payload = legacy.model_dump(mode="json", exclude={"repair", "factors"})
    assert (
        record_digest(legacy)
        == hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert "estimate, not observed" in record_report(review)


def test_queue_reconstructs_only_public_records_and_retains_severity(inputs, directory):
    claim, baseline, review = inputs
    store = Store(directory / "queue.sqlite")
    try:
        store.register("acme/repo", "/unused", "HEAD")
        for item, issue, comment in ((claim, 7, 0), (review, 7, 8), (baseline, 32, 9)):
            store.project_record(item, issue, comment, "authorized", record_digest(item))
        maintenance, bugs = fix_queue(store, claim.repository, {7: {"state": "open"}}, [baseline])
        assert maintenance[0]["level"] == "provisional review"
        assert bugs[0]["priority"] == "P1"
        body = queue_report(claim.repository, maintenance, bugs)
        assert "https://github.com/acme/repo/issues/7" in body
        assert "never added together" in body and "Unranked" in body
        assert not fix_queue(store, claim.repository, {7: {"state": "closed"}}, [baseline])[0]
    finally:
        store.close()


@pytest.mark.parametrize("factor", ["duplication", "test_runtime", "test_flakiness"])
def test_other_factor_normalization_and_runtime_budget(inputs, factor):
    claim, baseline, review = inputs
    repair = RepairEstimate.model_validate(
        {
            **review.repair.model_dump(),
            "projections": [
                {
                    "factor": factor,
                    "scenarios": [{"numerator": 10, "denominator": 100}] * 2,
                    "evidence": "source.py:1 removes the measured burden while preserving behavior.",
                    "assumptions": "Population remains the same for these bounded scenarios.",
                }
            ],
            "unchanged": [name for name in FACTORS if name != factor],
        }
    )
    decision = review.model_copy(update={"repair": repair})
    impact = evaluate_repair(claim, decision, [baseline], [factor])
    assert (impact.low, impact.high, impact.rate) == pytest.approx((8, 8, 3.2))
    if factor == "test_runtime":
        scenarios = [type(repair.projections[0].scenarios[0])(numerator=500, denominator=100)] * 2
        projection = repair.projections[0].model_copy(update={"scenarios": scenarios})
        decision = review.model_copy(
            update={"repair": repair.model_copy(update={"projections": [projection]})}
        )
        assert evaluate_repair(claim, decision, [baseline], [factor]).low == -10
        projection = projection.model_copy(
            update={"scenarios": [type(scenarios[0])(numerator=10, denominator=200)] * 2}
        )
        decision = review.model_copy(
            update={"repair": repair.model_copy(update={"projections": [projection]})}
        )
        assert evaluate_repair(claim, decision, [baseline], [factor]).status == "invalid"


def test_queue_ranks_benefit_by_effort_independently_of_bug_priority(inputs, directory):
    claim, baseline, review = inputs
    projection = review.repair.projections[0].model_copy(
        update={
            "scenarios": [
                type(review.repair.projections[0].scenarios[0])(numerator=10, denominator=50)
            ]
            * 2,
        }
    )
    repair = review.repair.model_copy(update={"projections": [projection]})
    review = review.model_copy(update={"repair": repair})
    other_item = claim.finding.model_copy(
        update={"title": "Another high severity bug at this location", "priority": "P0"}
    )
    other_claim = claim.model_copy(
        update={"id": other_item.identity(claim.repository), "finding": other_item}
    )
    other_review = review.model_copy(
        update={
            "id": "c" * 32,
            "finding_id": other_claim.id,
            "priority": "P0",
            "repair": repair.model_copy(
                update={"effort_hours": type(repair.effort_hours)(low=4, high=8)}
            ),
        }
    )
    store = Store(directory / "ranking.sqlite")
    try:
        store.register(claim.repository, "/unused", "HEAD")
        for record, issue, comment in (
            (claim, 7, 0),
            (review, 7, 10),
            (other_claim, 3, 0),
            (other_review, 3, 11),
        ):
            store.project_record(record, issue, comment, "authorized", record_digest(record))
        remote = {number: {"state": "open"} for number in (7, 3)}
        queue, bugs = fix_queue(store, claim.repository, remote, [baseline])
        assert [item["issue"] for item in queue] == [7, 3]
        assert [item["issue"] for item in bugs] == [3, 7]
        assert fix_queue(store, claim.repository, remote, [baseline]) == (queue, bugs)
    finally:
        store.close()
