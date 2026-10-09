import asyncio
import json
import time

import httpx
import pytest
from pydantic import ValidationError

from maxwell.contracts import Finding, Usage
from maxwell.github import GitHub
from maxwell.records import (
    AssessmentRecord,
    MeasurementRecord,
    PublicAgent,
    StageExecution,
    decode_record,
    encode_record,
)
from maxwell.reporting import Reporter, record_report
from maxwell.repositories import git
from maxwell.reputation import agent_reputation, reputation_report
from maxwell.store import Store


@pytest.fixture
def records(home, source, finding):
    store = Store(home.db)
    store.register("acme/repo", str(source), "HEAD")
    config = home.load()
    execution = config.execution(config.route(model="fake-model"), store.repository("acme/repo"))
    execution["provider"] = "private-work-alias"
    execution["settings"]["adapter"] = "claude"
    identifier = store.enqueue("acme/repo", execution=execution)
    job = store.claim(10_000_000, 1_000_000, 6)
    sha = git("rev-parse", "HEAD", cwd=source)
    with store.db:
        store.db.execute("UPDATE scans SET sha=? WHERE id=?", (sha, identifier))
    item = Finding(**finding)
    store.finish(job, "completed", "Discovery complete.", Usage(), 100, [item])
    client = GitHub(config.github)
    reporter = Reporter(store, client, config, home)
    yield store, reporter, sha, item.identity("acme/repo"), identifier
    client.close()
    store.close()


def assessment(sha, finding_id, run_id, **changes):
    values = {
        "id": "a" * 32,
        "repository": "acme/repo",
        "finding_id": finding_id,
        "sha": sha,
        "role": "reviewer",
        "agent": PublicAgent(adapter="codex", model="review-model", role="reviewer"),
        "execution_id": run_id,
        "verdict": "supported",
        "evidence": "The supplied input reaches a division with a zero denominator.",
    }
    return AssessmentRecord(**{**values, **changes})


def reconcile_labels(reporter):
    async def reconcile():
        issues = await reporter.project_repository_records("acme/repo")
        return await reporter.reconcile_assessment_labels("acme/repo", issues)

    return asyncio.run(reconcile())


def labels(github, issue):
    return {label["name"] for label in github["issues"][issue]["labels"]}


def test_assessment_labels_conflicts_supersession_and_idempotence(records, github, monkeypatch):
    store, reporter, sha, finding_id, run_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    github["issues"][issue]["labels"].append({"name": "custom"})
    review = assessment(sha, finding_id, run_id)
    reporter.github.create_comment("acme/repo", issue, record_report(review))
    assert reconcile_labels(reporter) == [issue]
    assert labels(github, issue) == {
        "maxwell",
        "custom",
        "maxwell:priority:P1",
        "maxwell:state:reviewed",
        "maxwell:review:supported",
    }
    audit = assessment(
        sha,
        finding_id,
        run_id,
        id="b" * 32,
        role="auditor",
        agent=PublicAgent(adapter="claude", model="audit-model", role="auditor"),
        references=[review.id],
        verdict="rejected",
    )
    reporter.github.create_comment("acme/repo", issue, record_report(audit))
    assert reconcile_labels(reporter) == [issue]
    assert labels(github, issue) >= {
        "maxwell:state:audited",
        "maxwell:review:supported",
        "maxwell:audit:rejected",
    }
    other = assessment(sha, finding_id, run_id, id="c" * 32, verdict="rejected")
    reporter.github.create_comment("acme/repo", issue, record_report(other))
    reconcile_labels(reporter)
    assert labels(github, issue) == {
        "maxwell",
        "custom",
        "maxwell:priority:P1",
        "maxwell:state:reviewed",
        "maxwell:review:inconclusive",
    }
    replacement = assessment(sha, finding_id, run_id, id="d" * 32, supersedes=other.id)
    reporter.github.create_comment("acme/repo", issue, record_report(replacement))
    reconcile_labels(reporter)
    assert "maxwell:review:supported" in labels(github, issue)
    original = reporter.github.request

    def no_writes(method, *args, **kwargs):
        assert method == "GET"
        return original(method, *args, **kwargs)

    monkeypatch.setattr(reporter.github, "request", no_writes)
    monkeypatch.setattr(reporter.github, "ensure_labels", lambda _: pytest.fail("Redundant labels"))
    assert reconcile_labels(reporter) == []
    assert github["issues"][issue]["state"] == "open"


@pytest.mark.parametrize("invalid", ["edited", "missing", "unauthorized", "wrong_sha"])
def test_assessment_labels_ignore_invalid_or_stale_records(records, github, invalid):
    store, reporter, sha, finding_id, run_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    record = assessment(sha, finding_id, run_id)
    comment = reporter.github.create_comment("acme/repo", issue, record_report(record))
    reconcile_labels(reporter)
    if invalid == "edited":
        comment["body"] = record_report(record.model_copy(update={"verdict": "rejected"}))
    elif invalid == "missing":
        github["comments"][issue] = []
    elif invalid == "unauthorized":
        comment["user"] = {"login": "outsider"}
    else:
        github["comments"][issue] = []
        reporter.github.create_comment(
            "acme/repo",
            issue,
            record_report(assessment("f" * 40, finding_id, run_id, id="b" * 32)),
        )
    if invalid in ("edited", "unauthorized"):
        github["comments"][issue][0] = comment
    reconcile_labels(reporter)
    assert "maxwell:state:suspected" in labels(github, issue)
    hunter = next(item for item in agent_reputation(store, "acme/repo") if item["role"] == "hunter")
    assert hunter["review_unassessed"] == 1
    assert not any(
        label.startswith(("maxwell:review:", "maxwell:audit:")) for label in labels(github, issue)
    )


@pytest.mark.parametrize("human_state", ["human-confirmed", "rejected"])
def test_assessment_labels_preserve_human_state_and_issue_closure(records, github, human_state):
    store, reporter, sha, finding_id, run_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    github["issues"][issue]["labels"].append({"name": f"maxwell:state:{human_state}"})
    github["issues"][issue]["state"] = "closed"
    reporter.github.create_comment(
        "acme/repo", issue, record_report(assessment(sha, finding_id, run_id))
    )
    reconcile_labels(reporter)
    assert labels(github, issue) >= {f"maxwell:state:{human_state}", "maxwell:review:supported"}
    assert not labels(github, issue).intersection(
        {"maxwell:state:suspected", "maxwell:state:reviewed"}
    )
    assert github["issues"][issue]["state"] == "closed"


def test_assessment_label_failure_is_reported_and_recoverable(records, github, monkeypatch):
    store, reporter, sha, finding_id, run_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    reporter.github.create_comment(
        "acme/repo", issue, record_report(assessment(sha, finding_id, run_id))
    )
    original = reporter.github.request

    def fail_labels(method, *args, **kwargs):
        if method == "PATCH" and "labels" in kwargs.get("json", {}):
            raise RuntimeError("Label update failed")
        return original(method, *args, **kwargs)

    monkeypatch.setattr(reporter.github, "request", fail_labels)
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    assert store.one("SELECT error FROM dashboards")["error"] == "Label update failed"
    monkeypatch.setattr(reporter.github, "request", original)
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    assert store.one("SELECT error FROM dashboards")["error"] is None
    assert "maxwell:state:reviewed" in labels(github, issue)


def test_reputation_reconstructs_from_github_without_local_scans_or_findings(records, home, github):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    review = assessment(sha, finding_id, execution_id)
    audit = assessment(
        sha,
        finding_id,
        execution_id,
        id="b" * 32,
        role="auditor",
        agent=PublicAgent(adapter="claude", model="audit-model", role="auditor", effort="high"),
        references=[review.id],
    )
    store.queue_record(review, issue)
    store.queue_record(audit, issue)
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    expected = agent_reputation(store, "acme/repo")
    body = next(
        item["body"] for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    assert reputation_report(expected) in body
    assert "private-work-alias" not in body and "Beta(2,2)" not in body
    with store.db:
        store.db.execute("UPDATE findings SET state='rejected'")
    assert agent_reputation(store, "acme/repo") == expected
    github["issues"][issue]["state"] = "closed"
    fresh = Store(home.root / "fresh.sqlite")
    try:
        fresh.register("acme/repo", "unused", "main")
        fresh_reporter = Reporter(fresh, reporter.github, reporter.config, home)
        asyncio.run(fresh_reporter.project_repository_records("acme/repo"))
        assert not fresh.rows("SELECT * FROM scans") and not fresh.rows("SELECT * FROM findings")
        assert agent_reputation(fresh, "acme/repo") == expected
        asyncio.run(fresh_reporter.dashboard("acme/repo"))
        actual_body = next(
            item["body"] for item in github["issues"].values() if item["title"] == "Maxwell status"
        )
        assert reputation_report(expected) in actual_body
    finally:
        fresh.close()


def test_record_contract_rejects_private_identity_fields():
    with pytest.raises(ValidationError):
        PublicAgent(adapter="claude", model="m", role="reviewer", provider="private-work")
    with pytest.raises(ValidationError):
        PublicAgent(adapter="claude-work", model="m", role="reviewer")


def test_assessment_contract_requires_attributable_evidence():
    record = assessment("f" * 40, "b" * 32, "c" * 32)
    for changes in (
        {"execution_id": None},
        {"agent": PublicAgent(adapter="codex", model="m", role="hunter")},
        {"evidence": "Probably"},
        {"supersedes": record.id},
        {"sha": "HEAD"},
        {"role": "human", "actor": "maintainer"},
    ):
        with pytest.raises(ValidationError):
            AssessmentRecord.model_validate({**record.model_dump(), **changes})


def test_record_codec_escapes_embedded_comment_delimiters():
    record = assessment(
        "f" * 40,
        "b" * 32,
        "c" * 32,
        evidence="Evidence containing --> and <!-- maxwell:record:v1 must round-trip.",
    )
    body = encode_record(record)
    assert body.count("<!--") == 1
    assert decode_record(body) == record
    assert decode_record("Legacy issue with no metadata") is None
    with pytest.raises(ValueError, match="one Maxwell"):
        decode_record(body + "\n" + body)


def test_finding_and_execution_publish_public_identities_and_project(records, github):
    store, reporter, _, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    all_bodies = [issue["body"] for issue in github["issues"].values()] + [
        comment["body"] for comments in github["comments"].values() for comment in comments
    ]
    assert all("private-work-alias" not in body for body in all_bodies)
    assert any("claude/fake-model, hunter" in body for body in all_bodies)
    projections = store.rows("SELECT type,id,status,data FROM github_records ORDER BY type")
    assert [(item["type"], item["id"], item["status"]) for item in projections] == [
        ("execution", execution_id, "current"),
        ("finding", finding_id, "current"),
    ]
    run = json.loads(projections[0]["data"])
    assert run["finding_ids"] == [finding_id]
    assert "provider" not in run["agent"]
    assert store.one("SELECT provider FROM scans")["provider"] == "private-work-alias"
    before = sum(len(items) for items in github["comments"].values())
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    assert sum(len(items) for items in github["comments"].values()) == before


def test_assessment_publication_is_append_only_and_reconciles_lost_response(
    records, github, monkeypatch
):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    record = assessment(sha, finding_id, execution_id)
    store.queue_record(record, issue)
    original = reporter.github.create_comment

    def lose_response(*args):
        original(*args)
        raise httpx.ReadError("response lost")

    from maxwell.github import PublicationUncertain

    def uncertain(*args):
        try:
            return lose_response(*args)
        except httpx.ReadError as exc:
            raise PublicationUncertain("response lost") from exc

    monkeypatch.setattr(reporter.github, "create_comment", uncertain)
    with pytest.raises(ValueError, match="reconciliation"):
        asyncio.run(reporter.publish_records("acme/repo"))
    assert store.one("SELECT state FROM record_publications")["state"] == "uncertain"
    monkeypatch.setattr(reporter.github, "create_comment", original)
    asyncio.run(reporter.publish_records("acme/repo"))
    assert len(github["comments"][issue]) == 1
    assert store.one("SELECT state FROM record_publications")["state"] == "published"
    changed = record.model_copy(update={"verdict": "rejected"})
    with pytest.raises(ValueError, match="immutable"):
        store.queue_record(changed, issue)


def test_redacted_records_remain_idempotent(records, github):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    record = assessment(
        sha,
        finding_id,
        execution_id,
        evidence="The diagnostic includes fake-github-secret and enough source evidence.",
    )
    store.queue_record(record, issue)
    asyncio.run(reporter.publish_records("acme/repo"))
    store.queue_record(record, issue)
    body = github["comments"][issue][0]["body"]
    assert "fake-github-secret" not in body
    assert "[REDACTED]" in decode_record(body).evidence
    assert len(github["comments"][issue]) == 1


def test_uncertain_publication_without_remote_record_does_not_retry(records, monkeypatch):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    store.queue_record(assessment(sha, finding_id, execution_id), issue)
    with store.db:
        store.db.execute("UPDATE record_publications SET state='uncertain'")

    def unexpected(*args):
        pytest.fail("Uncertain writes must reconcile before retrying")

    monkeypatch.setattr(reporter.github, "create_comment", unexpected)
    with pytest.raises(ValueError, match="reconciliation"):
        asyncio.run(reporter.publish_records("acme/repo"))


def test_projection_checks_authorship_subject_and_edited_deleted_history(records, github):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    original = assessment(sha, finding_id, execution_id)
    comments = github["comments"].setdefault(issue, [])
    comments.extend(
        [
            {"id": 9001, "body": record_report(original), "user": {"login": "outsider"}},
            {
                "id": 9002,
                "body": record_report(assessment(sha, "d" * 32, execution_id, id="b" * 32)),
                "user": {"login": "maintainer"},
            },
            {"id": 9003, "body": record_report(original), "user": {"login": "maintainer"}},
        ]
    )
    asyncio.run(reporter.project_repository_records("acme/repo"))
    assert store.rows("SELECT comment FROM github_records WHERE type='assessment'") == [
        {"comment": 9003}
    ]
    comments[-1]["body"] = record_report(original.model_copy(update={"verdict": "rejected"}))
    asyncio.run(reporter.project_repository_records("acme/repo"))
    cached = store.one("SELECT status,data FROM github_records WHERE type='assessment'")
    assert cached["status"] == "changed"
    assert json.loads(cached["data"])["verdict"] == "supported"
    assert store.one("SELECT COUNT(*) AS n FROM github_record_history WHERE comment=9003")["n"] == 2
    comments.pop()
    asyncio.run(reporter.project_repository_records("acme/repo"))
    assert (
        store.one("SELECT status FROM github_records WHERE type='assessment'")["status"]
        == "missing"
    )


def test_human_assessment_actor_must_match_authenticated_author(records, github):
    store, reporter, sha, finding_id, execution_id = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    issue = store.one("SELECT issue FROM findings")["issue"]
    record = assessment(
        sha,
        finding_id,
        execution_id,
        role="human",
        actor="someone-else",
        agent=None,
        execution_id=None,
    )
    github["comments"][issue] = [
        {"id": 9001, "body": record_report(record), "user": {"login": "maintainer"}}
    ]
    asyncio.run(reporter.project_repository_records("acme/repo"))
    assert not store.one("SELECT id FROM github_records WHERE type='assessment'")


def test_projection_can_rebuild_in_a_fresh_database(records, directory):
    store, reporter, _, _, _ = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    fresh = Store(directory / "fresh.sqlite")
    try:
        fresh.register("acme/repo", "/unused", "HEAD")
        other = Reporter(fresh, reporter.github, reporter.config, reporter.paths)
        asyncio.run(other.project_repository_records("acme/repo"))
        assert fresh.rows("SELECT type,id,data FROM github_records ORDER BY type") == store.rows(
            "SELECT type,id,data FROM github_records ORDER BY type"
        )
        assert not fresh.rows("SELECT * FROM scans")
    finally:
        fresh.close()


def test_discovery_context_includes_open_issues_and_excludes_pull_requests_and_spoofed_findings(
    records, monkeypatch
):
    _, reporter, _, finding_id, _ = records
    issues = [
        {
            "number": 1,
            "title": "Ordinary open issue",
            "state": "open",
            "body": "",
            "user": {"login": "outsider"},
        },
        {
            "number": 2,
            "title": "Closed ordinary issue",
            "state": "closed",
            "body": "",
            "user": {"login": "maintainer"},
        },
        {
            "number": 3,
            "title": "Closed legacy Maxwell finding",
            "state": "closed",
            "body": f"<!-- maxwell:finding:{finding_id} -->",
            "user": {"login": "maintainer"},
        },
        {
            "number": 4,
            "title": "Spoofed closed finding",
            "state": "closed",
            "body": f"<!-- maxwell:finding:{'b' * 32} -->",
            "user": {"login": "outsider"},
        },
        {
            "number": 5,
            "title": "Pull request",
            "state": "open",
            "body": "",
            "pull_request": {},
            "user": {"login": "maintainer"},
        },
        {
            "number": 6,
            "title": "Malformed closed finding",
            "state": "closed",
            "body": f"<!-- maxwell:finding:{'c' * 32} -->\n<!-- maxwell:record:v1\ninvalid\n-->",
            "user": {"login": "maintainer"},
        },
    ]
    monkeypatch.setattr(
        reporter.github,
        "paginate",
        lambda path, params=None: [] if path.endswith("/comments") else issues,
    )
    context, identities = asyncio.run(reporter.discovery_context("acme/repo"))
    assert [issue["issue"] for issue in context["issues"]] == [1, 3]
    assert identities == {finding_id}


def test_discovery_deduplicates_all_github_findings_beyond_prompt_limit(records, monkeypatch):
    _, reporter, _, _, _ = records
    issues = [
        {
            "number": number,
            "title": f"Legacy finding {number}",
            "state": "open",
            "body": f"<!-- maxwell:finding:{number:032x} -->",
            "user": {"login": "maintainer"},
        }
        for number in range(1, 106)
    ]
    monkeypatch.setattr(
        reporter.github,
        "paginate",
        lambda path, params=None: [] if path.endswith("/comments") else issues,
    )
    context, identities = asyncio.run(reporter.discovery_context("acme/repo"))
    assert context["shown_issues"] == 100 and context["total_issues"] == 105
    assert len(identities) == 105 and f"{1:032x}" in identities
    assert context["issues"][0]["issue"] == 105


def test_stage_ledger_keeps_private_routing_and_unknown_budget_holds(records):
    store, _, sha, finding_id, _ = records
    run = StageExecution(
        repository="acme/repo",
        provider="private-review-route",
        sha=sha,
        finding_id=finding_id,
        agent=PublicAgent(adapter="claude", model="review-model", role="reviewer"),
    )
    store.save_stage_execution(run)
    running = run.model_copy(
        update={
            "state": "running",
            "started": time.time(),
            "lease": "c" * 32,
            "reserved_micros": 1000,
        }
    )
    store.save_stage_execution(running)
    assert store.committed_today() == 1100
    with pytest.raises(ValueError, match="Stale"):
        store.save_stage_execution(running.model_copy(update={"lease": "d" * 32}))
    with pytest.raises(ValueError, match="identity"):
        store.save_stage_execution(running.model_copy(update={"provider": "changed"}))
    finished = running.model_copy(update={"state": "failed", "finished": time.time()})
    store.save_stage_execution(finished)
    assert store.committed_today() == 1100
    with pytest.raises(ValueError, match="Finished"):
        store.save_stage_execution(finished.model_copy(update={"cost_micros": 0}))
    assert not store.rows("SELECT * FROM record_publications")


def test_measurement_record_stores_policy_and_rejects_nonfinite_metrics(records):
    store, reporter, sha, _, _ = records
    asyncio.run(reporter.sync("acme/repo", process_commands=False))
    record = MeasurementRecord(
        id="e" * 32,
        repository="acme/repo",
        sha=sha,
        analyzer_version="python-v1",
        policy_version="structural-v1",
        coverage=0.8,
        factors={"complexity": {"numerator": 4, "denominator": 20, "unit": "lines"}},
        exclusions=["unsupported Java source"],
    )
    issue = store.one("SELECT issue FROM dashboards")["issue"]
    store.queue_record(record, issue)
    asyncio.run(reporter.publish_records("acme/repo"))
    asyncio.run(reporter.project_repository_records("acme/repo"))
    assert store.one("SELECT id FROM github_records WHERE type='measurement'")["id"] == record.id
    with pytest.raises(ValidationError):
        MeasurementRecord.model_validate({**record.model_dump(), "coverage": float("nan")})
