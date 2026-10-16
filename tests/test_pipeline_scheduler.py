import asyncio
import hashlib
import json
from types import SimpleNamespace as Namespace

import pytest
from pydantic import ValidationError
from test_pipeline import automatic as automatic_fixture
from test_pipeline import backlog as backlog_fixture
from test_repair import repairable as repairable_fixture
from test_validation import prepared as prepared_fixture

from maxwell.cli import parser
from maxwell.config import Config, ModelSettings, StagePoolSizes
from maxwell.contracts import Finding
from maxwell.manual import run_scans
from maxwell.pipeline import (
    AttemptLedger,
    PipelineItem,
    PipelineSnapshot,
    admission_blocker,
    classify_item,
    fingerprint,
    has_room,
    run_pipeline,
)
from maxwell.records import AssessmentRecord, FindingRecord, PublicAgent
from maxwell.store import Store

_automatic = pytest.fixture(name="automatic")(automatic_fixture.__wrapped__)
_backlog = pytest.fixture(name="backlog")(backlog_fixture.__wrapped__)
_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)
_prepared = pytest.fixture(name="prepared")(prepared_fixture.__wrapped__)


@pytest.fixture
def subject(finding):
    value = Finding(**finding)
    record = FindingRecord(
        id=value.identity("acme/repo"),
        repository="acme/repo",
        sha="a" * 40,
        execution_id="0" * 32,
        finding=value,
        agent=PublicAgent(adapter="codex", model="hunter", role="hunter"),
    )
    review = AssessmentRecord(
        id="1" * 32,
        repository="acme/repo",
        finding_id=record.id,
        sha=record.sha,
        role="reviewer",
        agent=PublicAgent(adapter="codex", model="reviewer", role="reviewer"),
        execution_id="2" * 32,
        verdict="supported",
        actionability="actionable",
        evidence="app.py:2 divides by zero for empty input.",
    )
    audit = review.model_copy(
        update={
            "id": "3" * 32,
            "role": "auditor",
            "references": [review.id],
            "agent": PublicAgent(adapter="codex", model="auditor", role="auditor"),
        }
    )
    return record, review, audit


def classify(
    subject, *, assessments=None, repair=None, pull=None, votes=(), integrations=(), head="head"
):
    finding, review, audit = subject
    return classify_item(
        1,
        finding,
        assessments if assessments is not None else [review, audit],
        repair,
        Namespace(id="link") if pull else None,
        pull,
        votes,
        integrations,
        head,
        Config().repair.auto_merge.model_copy(update={"enabled": True}),
    )


def test_classification_requires_audit_before_new_repair_and_keeps_attention_visible(subject):
    _, review, audit = subject
    assert classify(subject, assessments=[]).stage == "review"
    assert classify(subject, assessments=[review]).stage == "audit"
    assert classify(subject).stage == "repair"
    inconclusive = audit.model_copy(update={"verdict": "inconclusive"})
    result = classify(subject, assessments=[review, inconclusive])
    assert result.pool == "audit" and result.stage is None
    rejected = audit.model_copy(update={"verdict": "rejected"})
    result = classify(subject, assessments=[review, rejected])
    assert result.pool is None and result.outcome == "rejected"
    stale = audit.model_copy(update={"references": ["f" * 32]})
    assert classify(subject, assessments=[review, stale]).stage == "audit"


def test_partial_validation_quorum_and_amendments_count_one_issue(subject):
    _, review, audit = subject
    repair = Namespace(references=[review.id, audit.id])
    pull = {"state": "open"}
    vote = Namespace(
        state="completed", outcome="accepted", agent=Namespace(adapter="codex", model="v1")
    )
    first = classify(subject, repair=repair, pull=pull, votes=[vote])
    assert (first.pool, first.stage) == ("repair", "validate")
    duplicate = Namespace(**vars(vote))
    assert classify(subject, repair=repair, pull=pull, votes=[vote, duplicate]).stage == "validate"
    second = Namespace(
        state="completed", outcome="accepted", agent=Namespace(adapter="codex", model="v2")
    )
    ready = classify(subject, repair=repair, pull=pull, votes=[vote, second])
    assert ready.pool == "validate" and ready.stage is None
    second.outcome = "rejected"
    amendment = classify(subject, repair=repair, pull=pull, votes=[second])
    assert (amendment.pool, amendment.stage) == ("repair", "repair")
    second.outcome = "inconclusive"
    assert classify(subject, repair=repair, pull=pull, votes=[second]).stage is None


def test_existing_provisional_repair_can_resume_and_merged_repairs_get_integration(subject):
    _, review, _ = subject
    repair = Namespace(references=[review.id])
    item = classify(subject, assessments=[review], repair=repair, pull={"state": "open"})
    assert item.stage == "validate"
    integrated = Namespace(sha="old", state="completed", outcome="accepted")
    item = classify(
        subject, repair=repair, pull={"state": "closed", "merged": True}, integrations=[integrated]
    )
    assert item.stage == "integrate"
    integrated.sha = "head"
    assert (
        classify(subject, repair=repair, pull={"merged": True}, integrations=[integrated]).stage
        is None
    )


def test_blocked_fixer_is_not_automatically_retried_with_unchanged_evidence(subject):
    _, review, audit = subject
    repair = Namespace(references=[review.id, audit.id], outcome="blocked")
    assert classify(subject, repair=repair).stage is None
    repair.references = ["old"]
    assert classify(subject, repair=repair).stage == "repair"


def test_output_capacities_allow_drain_and_in_place_amendment_but_bound_admission():
    review = PipelineItem(1, "one", "hunt", "review")
    amendment = PipelineItem(2, "two", "repair", "repair")
    snapshot = PipelineSnapshot([], [review, amendment], "head", reservations={"hunt": 1})
    caps = StagePoolSizes(hunt=2, review=0, repair=1, integrate=0).model_dump(by_alias=True)
    assert not has_room(snapshot, None, "hunt", caps)
    assert not has_room(snapshot, review, "review", caps)
    assert has_room(snapshot, amendment, "repair", caps)
    assert has_room(snapshot, None, "integrate", caps)
    assert snapshot.occupancy()["hunt"] == 2
    assert caps["validate"] == 3
    with pytest.raises(ValidationError):
        StagePoolSizes(hunt=-1)


def test_attempts_survive_restart_cool_down_and_allow_changed_inputs(home, monkeypatch):
    store = Store(home.db)
    try:
        store.register("acme/repo", "/tmp/source", "main")
        config = home.load()
        now = [1000.0]
        monkeypatch.setattr("maxwell.pipeline.time.time", lambda: now[0])
        ledger = AttemptLedger(store, config, True)
        ledger.save("acme/repo", "review", "finding", "same", "failed", "agent failed")
        reopened = AttemptLedger(store, config, True)
        assert reopened.blocker("acme/repo", "review", "finding", "same") == "agent failed"
        assert reopened.blocker("acme/repo", "review", "finding", "new") is None
        now[0] += config.pipeline.retry_seconds
        assert reopened.blocker("acme/repo", "review", "finding", "same") is None
        reopened.save("acme/repo", "review", "finding", "same", "completed")
        now[0] += config.pipeline.retry_seconds * 10
        assert "already attempted" in reopened.blocker("acme/repo", "review", "finding", "same")
        reopened.save("acme/repo", "hunt", "hunt", "constant", "completed")
        assert "cooldown" in reopened.blocker("acme/repo", "hunt", "hunt", "other")
    finally:
        store.close()


def test_failed_validation_execution_ids_do_not_bypass_backoff():
    class Target:
        def model_dump(self, **kwargs):
            return {"supersedes": self.supersedes, "repair": "same", "head": "same"}

    target = Target()
    target.supersedes = ["failure-one"]
    first = fingerprint("validate", target, "head")
    target.supersedes = ["failure-two"]
    assert fingerprint("validate", target, "head") == first


@pytest.mark.parametrize(
    "options",
    [
        {"stage": "hunt"},
        {"issue": 1},
        {"limit": 2},
        {"force": True},
        {"model": "pinned"},
        {"skip_stages": ["hunt"]},
    ],
)
def test_pipeline_rejects_ambiguous_overrides_before_execution(home, options):
    with pytest.raises(ValueError, match="--repair-all/--infinite"):
        asyncio.run(run_scans(home, home.load(), repair_all=True, **options))


def test_cli_modes_are_mutually_exclusive():
    assert parser().parse_args(["scan", "--infinite"]).infinite
    assert parser().parse_args(["scan", "--repair-all"]).repair_all
    with pytest.raises(SystemExit):
        parser().parse_args(["scan", "--repair-all", "--infinite"])


def test_admission_precheck_does_not_create_failed_jobs(home, monkeypatch):
    store = Store(home.db)
    try:
        config = home.load()
        store.register("acme/repo", "/tmp/source", "main")
        monkeypatch.setattr(store, "unpriced_started_today", lambda: config.limits.daily_scan_limit)
        execution = {"stage": "review", "settings": {}}
        reason = admission_blocker(store, config, "acme/repo", execution)
        assert "Daily scan limit" in reason and "next daily reset" in reason
        assert not store.rows("SELECT * FROM scans")
    finally:
        store.close()


def test_real_batch_reviews_and_audits_all_existing_findings_without_hunting(
    automatic, backlog, github, source
):
    from maxwell.repositories import git

    home, store, config = automatic
    github["branches"]["main"] = git("rev-parse", "HEAD", cwd=source)
    config.pipeline.pools.repair = 0
    result = asyncio.run(run_scans(home, config, "acme/repo", repair_all=True))
    assert result["valid"], json.dumps(result, indent=2)
    assert [item["stage"] for item in result["scans"]] == ["review", "audit", "review", "audit"]
    assert result["pipeline_repositories"][0]["remaining"] == 2
    assert store.one("SELECT COUNT(*) AS n FROM scans")["n"] == 4
    again = asyncio.run(run_scans(home, config, "acme/repo", repair_all=True))
    assert again["valid"] and not again["scans"]


def cache_models(home, store, config):
    config.provider.models = {
        name: ModelSettings() for name in ("fake-model", "validator-one", "validator-two")
    }
    store.apply_models(
        {
            "valid": True,
            "checks": [],
            "providers": [
                {
                    "provider": "default",
                    "models": [
                        {
                            "model": model,
                            "access": "verified",
                            "selectable": True,
                            "efforts": None,
                            "default_effort": None,
                        }
                        for model in config.provider.models
                    ],
                }
            ],
        },
        hashlib.sha256(home.config.read_bytes()).hexdigest(),
    )


def test_real_batch_resumes_prepared_pr_with_independent_validation(
    home, prepared, repairable, github
):
    store, config, _, _, _, _ = repairable
    cache_models(home, store, config)
    result = asyncio.run(run_scans(home, config, "acme/repo", repair_all=True))
    assert result["valid"], json.dumps(result, indent=2)
    assert [item["stage"] for item in result["scans"]] == ["validate"]
    scan = result["scans"][0]
    assert scan["model"] != prepared["agent"]["model"]
    assert scan["validations"][0]["outcome"] == "accepted"
    assert result["pipeline_repositories"][0]["pools"]["validate"] == 1
    assert any(
        "auto-merge disabled" in reason for reason in result["pipeline_repositories"][0]["waiting"]
    )
    assert not github["merges"]


@pytest.fixture
def scheduler(home, monkeypatch, subject):
    store = Store(home.db)
    store.register("acme/repo", "/tmp/source", "main")
    config = home.load()
    state = {1: ("hunt", "review"), 2: ("hunt", "review")}
    executions = []
    finding = subject[0]

    class Target:
        def __init__(self, issue, stage):
            self.issue, self.stage = issue, stage
            self.finding, self.reviews = finding, []

        def model_dump(self, **kwargs):
            return {"issue": self.issue, "stage": self.stage}

    async def snapshot(reporter, repository):
        items = [
            PipelineItem(number, str(number), pool, stage)
            for number, (pool, stage) in state.items()
            if pool
        ]
        issues = [
            {"number": number, "state": "open" if pool else "closed"}
            for number, (pool, _) in state.items()
        ]
        return PipelineSnapshot(issues, items, "a" * 40)

    async def targets(reporter, repository, snapshot, stage, issue):
        return [Target(issue, stage)] if issue else [None]

    async def measure(*args):
        return {"repository": "acme/repo", "state": "completed"}

    monkeypatch.setattr("maxwell.pipeline.load_snapshot", snapshot)
    monkeypatch.setattr("maxwell.pipeline.targets_for", targets)
    monkeypatch.setattr("maxwell.pipeline.fix_queue", lambda *_: ([], []))
    monkeypatch.setattr("maxwell.pipeline.measure_one", measure)
    selector = Namespace(
        select=lambda *_args, **_kwargs: (
            config.route("default", "fake-model"),
            {"independence": "different_model"},
        )
    )

    async def run(execute, **kwargs):
        return await run_pipeline(
            home,
            config,
            store,
            None,
            kwargs.pop("registrations", [store.repository("acme/repo")]),
            selector,
            infinite=kwargs.pop("infinite", False),
            priority=2,
            on_progress=kwargs.pop("on_progress", None),
            execute=execute,
            **kwargs,
        )

    def result(stage, error=None):
        return {
            "stage": stage,
            "state": "failed" if error else "completed",
            "publication_errors": [],
            "error": error,
        }

    yield Namespace(
        run=run, state=state, executions=executions, result=result, config=config, store=store
    )
    store.close()


def test_snapshot_batch_continues_past_failure_and_excludes_new_findings(scheduler):
    async def execute(*args, stage, **kwargs):
        issue = args[5]["target"]["issue"]
        scheduler.executions.append((issue, stage))
        scheduler.state[3] = ("hunt", "review")
        if issue == 1:
            return scheduler.result(stage, "agent exited")
        scheduler.state[issue] = {
            "review": ("review", "audit"),
            "audit": ("audit", "repair"),
            "repair": ("repair", "validate"),
            "validate": ("integrate", "integrate"),
            "integrate": (None, None),
        }[stage]
        return scheduler.result(stage)

    report = asyncio.run(scheduler.run(execute))
    assert scheduler.executions == [
        (1, "review"),
        (2, "review"),
        (2, "audit"),
        (2, "repair"),
        (2, "validate"),
        (2, "integrate"),
    ]
    assert not report["valid"]
    assert report["pipeline_repositories"][0]["closed"] == 1
    assert report["pipeline_repositories"][0]["remaining"] == 1


def test_scheduler_drains_downstream_before_admitting_upstream(scheduler):
    scheduler.config.pipeline.pools.review = 1
    scheduler.state[2] = ("review", "audit")

    async def execute(*args, stage, **kwargs):
        issue = args[5]["target"]["issue"]
        scheduler.executions.append((issue, stage))
        scheduler.state[issue] = (None, None)
        return scheduler.result(stage)

    report = asyncio.run(scheduler.run(execute))
    assert report["valid"]
    assert scheduler.executions == [(2, "audit"), (1, "review")]


def test_existing_backlog_refills_bounded_pool_before_new_reviews(scheduler):
    scheduler.config.pipeline.pools.review = 2
    scheduler.state.update({number: ("review", "audit") for number in range(3, 8)})
    events = []

    async def execute(*args, stage, **kwargs):
        issue = args[5]["target"]["issue"]
        scheduler.executions.append((issue, stage))
        scheduler.state[issue] = (None, None)
        return scheduler.result(stage)

    report = asyncio.run(scheduler.run(execute, on_progress=events.append))
    pools = [event for event in events if event["state"] == "pools"]
    assert pools[0]["pools"]["review"] == 2
    assert pools[0]["backlog"]["review"] == 3
    assert [event["backlog"]["review"] for event in pools[:4]] == [3, 2, 1, 0]
    assert all(event["pools"]["review"] <= 2 for event in pools)
    assert scheduler.executions == [(n, "audit") for n in range(3, 8)] + [
        (1, "review"),
        (2, "review"),
    ]
    assert report["valid"]


def test_failed_active_issue_yields_slot_to_backlog(scheduler):
    scheduler.config.pipeline.pools.hunt = 1

    async def execute(*args, stage, **kwargs):
        issue = args[5]["target"]["issue"]
        scheduler.executions.append(issue)
        if issue == 1:
            return scheduler.result(stage, "provider failed")
        scheduler.state[issue] = (None, None)
        return scheduler.result(stage)

    asyncio.run(scheduler.run(execute))
    assert scheduler.executions == [1, 2]


def test_zero_pool_leaves_existing_findings_in_backlog(scheduler):
    scheduler.config.pipeline.pools.hunt = 0

    async def execute(*args, **kwargs):
        pytest.fail("a disabled pool must not admit existing issues")

    report = asyncio.run(scheduler.run(execute))
    assert report["pipeline_repositories"][0]["pools"]["hunt"] == 0
    assert report["pipeline_repositories"][0]["backlog"]["hunt"] == 2


def test_infinite_hunter_waits_for_existing_hunted_backlog(scheduler, monkeypatch):
    scheduler.config.pipeline.pools.hunt = 1
    scheduler.config.pipeline.pools.review = 0
    events = []

    async def execute(*args, **kwargs):
        pytest.fail("existing hunted findings fill the pool; no hunt or review may run")

    async def wait(seconds, progress):
        raise asyncio.CancelledError

    monkeypatch.setattr("maxwell.pipeline.wait_for_work", wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run(execute, infinite=True, on_progress=events.append))
    pools = next(event for event in events if event["state"] == "pools")
    assert pools["pools"]["hunt"] == 1
    assert pools["backlog"]["hunt"] == 1


def test_pending_publications_reserve_slots_before_backlog_admission():
    snapshot = PipelineSnapshot(
        [],
        [PipelineItem(n, str(n), "review", "audit") for n in range(3)],
        "head",
        reservations={"review": 1},
    )
    capacities = StagePoolSizes(review=2).model_dump(by_alias=True)
    admitted, pools, backlog = snapshot.admit(capacities, snapshot.items)
    assert len(admitted) == 1
    assert pools["review"] == 2
    assert backlog["review"] == 2


def test_infinite_hunt_is_clipped_then_waits_for_cadence(scheduler, monkeypatch):
    scheduler.state.clear()
    scheduler.config.pipeline.pools.hunt = 2
    waits = []

    async def execute(*args, stage, **kwargs):
        assert stage == "hunt"
        execution = args[5]
        assert execution["publication_slots"] == 2
        assert execution["limits"]["max_findings_per_scan"] == 2
        scheduler.executions.append(stage)
        return scheduler.result(stage)

    async def wait(seconds, progress):
        waits.append(progress.phase)
        raise asyncio.CancelledError

    monkeypatch.setattr("maxwell.pipeline.wait_for_work", wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run(execute, infinite=True))
    assert scheduler.executions == ["hunt"]
    assert "hunt cooldown" in waits[0]


def test_infinite_daily_limit_waits_without_enqueuing_and_is_interruptible(scheduler, monkeypatch):
    scheduler.config.provider.input_usd_per_mtok = None
    scheduler.config.provider.output_usd_per_mtok = None
    monkeypatch.setattr(scheduler.store, "unpriced_started_today", lambda: 999)
    waits = []

    async def execute(*args, **kwargs):
        pytest.fail("no agent may run without admission")

    async def wait(seconds, progress):
        waits.append(progress.phase)
        raise asyncio.CancelledError

    monkeypatch.setattr("maxwell.pipeline.wait_for_work", wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run(execute, infinite=True))
    assert "Daily scan limit" in waits[0] and "next daily reset" in waits[0]
    assert not scheduler.store.rows("SELECT * FROM scans")
    assert not scheduler.store.rows("SELECT * FROM pipeline_attempts")


@pytest.mark.parametrize("already_validated", [False, True])
def test_real_batch_validates_quorum_merges_and_verifies_main(
    home, prepared, repairable, github, source, monkeypatch, already_validated
):
    from maxwell.github import GitHub
    from maxwell.repositories import Repository, git

    store, config, _, _, _, issue = repairable
    cache_models(home, store, config)
    if already_validated:
        from test_validation import validate

        assert validate(home)["valid"]
        config.repair.auto_merge.required_validations = 1
    config.repair.auto_merge.enabled = True
    original = GitHub.merge_pull
    repository = Repository(home, store.repository("acme/repo"))

    def merge(client, repo, number, head, method):
        result = original(client, repo, number, head, method)
        git("fetch", str(repository.mirror), head, cwd=source)
        git("merge", "--ff-only", head, cwd=source)
        return result

    monkeypatch.setattr(GitHub, "merge_pull", merge)
    result = asyncio.run(run_scans(home, config, "acme/repo", repair_all=True))
    assert result["valid"], json.dumps(result, indent=2)
    assert [run["stage"] for run in result["scans"]] == (
        ["integrate"] if already_validated else ["validate", "validate", "integrate"]
    )
    if not already_validated:
        assert len({run["model"] for run in result["scans"][:2]}) == 2
    assert result["pipeline_repositories"][0]["closed"] == 1
    assert github["issues"][issue]["state"] == "closed"
    assert len(github["merges"]) == 1


def test_real_batch_creates_second_repair_and_validates_both(home, prepared, repairable, github):
    from maxwell.records import encode_record
    from maxwell.reporting import record_report

    store, config, reporter, original, review, _ = repairable
    cache_models(home, store, config)
    value = original.finding.model_copy(update={"title": "Another existing empty-input finding"})
    finding = original.model_copy(update={"id": value.identity("acme/repo"), "finding": value})
    second = reporter.github.create_issue(
        "acme/repo",
        value.title,
        f"<!-- maxwell:finding:{finding.id} -->\n" + encode_record(finding),
    )["number"]
    review = review.model_copy(update={"id": "b" * 32, "finding_id": finding.id})
    audit = review.model_copy(
        update={
            "id": "c" * 32,
            "role": "auditor",
            "references": [review.id],
            "agent": PublicAgent(adapter="codex", model="auditor", role="auditor"),
        }
    )
    reporter.github.create_comment("acme/repo", second, record_report(review))
    reporter.github.create_comment("acme/repo", second, record_report(audit))
    result = asyncio.run(run_scans(home, config, "acme/repo", repair_all=True))
    assert result["valid"], json.dumps(result, indent=2)
    assert [run["stage"] for run in result["scans"]] == ["validate", "repair", "validate"]
    assert len(github["pulls"]) == 2
    assert result["pipeline_repositories"][0]["pools"]["validate"] == 2
    assert not github["merges"]


def test_unpublished_assessment_reserves_destination_and_prevents_duplicate_execution(
    home, subject
):
    from maxwell.pipeline import load_snapshot

    store = Store(home.db)
    store.register("acme/repo", "/tmp/source", "main")
    finding, review, _ = subject
    store.project_record(finding, 1, 0, "maxwell", "source-digest")
    store.queue_record(review, 1)

    async def sync(*args, **kwargs):
        return None

    async def project(*args):
        return [{"number": 1, "state": "open"}]

    async def call(*args):
        return {"sha": finding.sha}

    reporter = Namespace(
        store=store,
        config=home.load(),
        sync=sync,
        project_repository_records=project,
        call=call,
        github=Namespace(request=None),
        merge_statuses={},
    )
    try:
        snapshot = asyncio.run(load_snapshot(reporter, "acme/repo"))
        assert snapshot.occupancy()["review"] == 1
        assert snapshot.items[0].stage is None
        assert "publication" in snapshot.items[0].reason
    finally:
        store.close()


def test_blocked_repository_does_not_prevent_another_repository(scheduler, monkeypatch):
    scheduler.store.register("acme/other", "/tmp/other", "main")
    done = set()

    async def snapshot(reporter, repository):
        return PipelineSnapshot(
            [{"number": 1, "state": "closed" if repository in done else "open"}],
            [] if repository in done else [PipelineItem(1, repository, "hunt", "review")],
            "head",
        )

    async def execute(*args, stage, **kwargs):
        repository = args[4]["name"]
        scheduler.executions.append(repository)
        if repository == "acme/repo":
            return scheduler.result(stage, "failed provider")
        done.add(repository)
        return scheduler.result(stage)

    monkeypatch.setattr("maxwell.pipeline.load_snapshot", snapshot)
    report = asyncio.run(
        scheduler.run(
            execute,
            registrations=scheduler.store.rows("SELECT * FROM repositories ORDER BY name DESC"),
        )
    )
    assert scheduler.executions == ["acme/repo", "acme/other"]
    assert report["pipeline_repositories"][1]["closed"] == 1


def test_infinite_resumes_after_daily_admission_becomes_available(scheduler, monkeypatch):
    scheduler.config.provider.input_usd_per_mtok = None
    scheduler.config.provider.output_usd_per_mtok = None
    scheduler.config.pipeline.pools.hunt = 2
    spent = [999]
    monkeypatch.setattr(scheduler.store, "unpriced_started_today", lambda: spent[0])
    waits = []

    async def execute(*args, stage, **kwargs):
        if stage == "hunt":
            raise asyncio.CancelledError
        issue = args[5]["target"]["issue"]
        scheduler.executions.append(issue)
        scheduler.state[issue] = (None, None)
        return scheduler.result(stage)

    async def wait(seconds, progress):
        waits.append(progress.phase)
        if len(waits) == 1:
            spent[0] = 0
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr("maxwell.pipeline.wait_for_work", wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run(execute, infinite=True))
    assert "Daily scan limit" in waits[0]
    assert scheduler.executions == [1, 2]
