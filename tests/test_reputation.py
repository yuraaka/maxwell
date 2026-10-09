import pytest

from maxwell.contracts import Finding, Usage
from maxwell.records import (
    AssessmentRecord,
    ExecutionRecord,
    FindingRecord,
    PublicAgent,
    record_digest,
)
from maxwell.reputation import agent_reputation, reputation_report
from maxwell.store import Store


@pytest.fixture
def evidence(home, finding):
    store = Store(home.db)
    store.register("acme/repo", "unused", "main")
    record = FindingRecord(
        id=Finding(**finding).identity("acme/repo"),
        repository="acme/repo",
        sha="f" * 40,
        execution_id="1" * 32,
        agent=PublicAgent(adapter="codex", model="hunter", role="hunter"),
        finding=finding,
    )

    def publish(item, issue=1):
        comment = 0 if isinstance(item, FindingRecord) else int(item.id, 16)
        return store.project_record(item, issue, comment, "maintainer", record_digest(item))

    publish(record)
    yield store, record, publish
    store.close()


def assessment(finding, *, number=2, role="reviewer", verdict="supported", **changes):
    values = dict(
        id=f"{number:032x}",
        repository=finding.repository,
        finding_id=finding.id,
        sha=finding.sha,
        role=role,
        agent=PublicAgent(adapter="claude", model=role, role=role, effort="high"),
        execution_id=f"{number + 100:032x}",
        verdict=verdict,
        evidence="Concrete source evidence at app.py:2 supports this source assessment.",
    )
    return AssessmentRecord(**{**values, **changes})


def row(store, role, **identity):
    return next(
        item
        for item in agent_reputation(store, "acme/repo")
        if item["role"] == role
        and all(item["agent"][key] == value for key, value in identity.items())
    )


@pytest.mark.parametrize(
    "review,audit,outcome",
    [
        ("supported", "supported", "agreement"),
        ("rejected", "rejected", "agreement"),
        ("supported", "rejected", "disagreement"),
        ("rejected", "supported", "disagreement"),
        ("inconclusive", "supported", "unresolved"),
        ("supported", "inconclusive", "unresolved"),
        ("supported", None, "unresolved"),
    ],
)
def test_source_support_and_reviewer_agreement_are_separate(evidence, review, audit, outcome):
    store, finding, publish = evidence
    review_record = assessment(finding, verdict=review)
    publish(review_record)
    if audit:
        publish(
            assessment(
                finding, number=3, role="auditor", verdict=audit, references=[review_record.id]
            )
        )
    hunter = row(store, "hunter")
    assert hunter["findings"] == 1
    assert hunter[f"review_{review if review != 'inconclusive' else 'unresolved'}"] == 1
    assert (
        hunter[
            f"audit_{'unassessed' if audit is None else 'unresolved' if audit == 'inconclusive' else audit}"
        ]
        == 1
    )
    reviewer = row(store, "reviewer")
    assert reviewer["assessments"] == 1 and reviewer[outcome] == 1
    assert reviewer["agreement"] + reviewer["disagreement"] + reviewer["unresolved"] == 1
    report = reputation_report(agent_reputation(store, "acme/repo"))
    assert "Quality: unrated until independent validation" in report
    assert "no numerical score" in report and "not proven accuracy" in report


def test_reassessment_removes_old_observation_and_invalidates_audit(evidence):
    store, finding, publish = evidence
    review = assessment(finding)
    publish(review)
    publish(assessment(finding, number=3, role="auditor", references=[review.id]))
    assert row(store, "reviewer")["agreement"] == 1
    replacement = assessment(finding, number=4, verdict="rejected", supersedes=review.id)
    publish(replacement)
    assert row(store, "hunter")["review_rejected"] == 1
    assert row(store, "hunter")["audit_unassessed"] == 1
    assert row(store, "reviewer")["assessments"] == 1
    assert row(store, "reviewer")["unresolved"] == 1
    assert not any(item["role"] == "auditor" for item in agent_reputation(store, "acme/repo"))
    publish(
        assessment(
            finding, number=5, role="auditor", verdict="rejected", references=[replacement.id]
        )
    )
    assert row(store, "reviewer")["agreement"] == 1


def test_conflicts_and_duplicate_votes_do_not_inflate_quality(evidence):
    store, finding, publish = evidence
    reviews = [
        assessment(finding, number=number, verdict=verdict)
        for number, verdict in ((2, "supported"), (3, "supported"), (4, "rejected"))
    ]
    for review in reviews:
        publish(review)
    references = [review.id for review in reviews]
    publish(assessment(finding, number=5, role="auditor", references=references))
    assert row(store, "hunter")["review_unresolved"] == 1
    assert row(store, "reviewer")["assessments"] == 1
    assert row(store, "reviewer")["unresolved"] == 1
    assert row(store, "auditor")["assessments"] == 1
    publish(
        assessment(finding, number=6, role="auditor", verdict="rejected", references=references)
    )
    assert row(store, "hunter")["audit_unresolved"] == 1
    assert row(store, "auditor")["assessments"] == 1


def test_agents_and_efforts_are_separate_without_prompt_or_private_routing(evidence):
    store, finding, publish = evidence
    reviews = []
    for number, adapter, effort, prompt in (
        (2, "claude", "high", "v1"),
        (3, "claude", "high", "v2"),
        (4, "claude", "low", "v1"),
        (5, "codex", "high", "v1"),
    ):
        agent = PublicAgent(
            adapter=adapter,
            model="review-model",
            role="reviewer",
            effort=effort,
            prompt_version=prompt,
        )
        reviews.append(assessment(finding, number=number, agent=agent))
        publish(reviews[-1])
    publish(assessment(finding, number=6, role="auditor", references=[item.id for item in reviews]))
    rows = [item for item in agent_reputation(store, "acme/repo") if item["role"] == "reviewer"]
    assert len(rows) == 3 and all(item["agreement"] == 1 for item in rows)
    assert all(set(item["agent"]) == {"adapter", "model", "role", "effort"} for item in rows)


@pytest.mark.parametrize(
    "invalid", ["changed", "missing", "wrong_sha", "wrong_finding", "wrong_issue"]
)
def test_invalid_assessments_cannot_supply_support(evidence, invalid):
    store, finding, publish = evidence
    record = assessment(finding)
    if invalid == "wrong_sha":
        record = assessment(finding, sha="e" * 40)
    elif invalid == "wrong_finding":
        record = assessment(finding, finding_id="e" * 32)
    publish(record, issue=2 if invalid == "wrong_issue" else 1)
    if invalid == "changed":
        assert publish(record.model_copy(update={"verdict": "rejected"})) == "changed"
    elif invalid == "missing":
        store.reconcile_records("acme/repo", 1, {0})
    assert row(store, "hunter")["review_unassessed"] == 1
    assert not any(item["role"] == "reviewer" for item in agent_reputation(store, "acme/repo"))


def test_invalid_finding_and_unattributed_hunter(evidence):
    store, finding, publish = evidence
    assert publish(finding.model_copy(update={"sha": "e" * 40})) == "changed"
    assert agent_reputation(store, "acme/repo") == []
    publish(finding)
    store.db.execute("DELETE FROM github_records")
    publish(finding.model_copy(update={"id": "e" * 32}))
    assert agent_reputation(store, "acme/repo") == []
    store.db.execute("DELETE FROM github_records")
    publish(finding.model_copy(update={"agent": None}))
    result = agent_reputation(store, "acme/repo")[0]
    assert result["agent"] is None and result["review_unassessed"] == 1
    assert "Unattributed (hunter)" in reputation_report([result])


def test_execution_tokens_include_failures_once_and_exclude_changed_records(evidence):
    store, finding, publish = evidence
    executions = []
    for number, state, usage in (
        (
            10,
            "completed",
            Usage(
                input_tokens=1000,
                cached_input_tokens=500,
                cache_write_tokens=100,
                output_tokens=100,
                reported=True,
                complete=True,
            ),
        ),
        (11, "failed", Usage(input_tokens=200, output_tokens=20, reported=True)),
        (12, "failed", Usage(input_tokens=999, reported=False)),
        (13, "skipped", Usage()),
    ):
        record = ExecutionRecord(
            id=f"{number:032x}",
            repository="acme/repo",
            sha=finding.sha,
            agent=finding.agent,
            state=state,
            finished=1,
            summary="Execution ended",
            accounting="tokens",
            usage=usage,
            finding_ids=[finding.id] * 2,
        )
        publish(record)
        publish(record)
        executions.append(record)
    result = row(store, "hunter")
    assert result["executions"] == 4
    assert result["input_tokens"] == 1200 and result["output_tokens"] == 120
    assert result["unknown_usage"] == 1 and result["incomplete_usage"] == 2
    assert result["findings"] == 1 and result["review_unassessed"] == 1
    publish(executions[0].model_copy(update={"usage": Usage()}))
    assert row(store, "hunter")["input_tokens"] == 200


def test_report_escapes_agent_table_separators(evidence):
    store, finding, publish = evidence
    store.db.execute("DELETE FROM github_records")
    publish(
        finding.model_copy(update={"agent": finding.agent.model_copy(update={"model": "x|y\nz"})})
    )
    report = reputation_report(agent_reputation(store, "acme/repo"))
    assert "x\\|y z" in report and "x|y\nz" not in report
