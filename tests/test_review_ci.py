import asyncio

import pytest
import yaml
from test_repair import repairable as repairable_fixture
from test_validation import prepared as prepared_fixture
from test_validation import validate

from maxwell.github import GitHub
from maxwell.repair import collect_repair_targets

_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)
_prepared = pytest.fixture(name="prepared")(prepared_fixture.__wrapped__)


def configure(home):
    data = yaml.safe_load(home.config.read_text())
    data["review"] = {"max_fix_attempts": 0, "ci": {"acme/repo": {"checks": ["tests"]}}}
    home.config.write_text(yaml.safe_dump(data))


def test_review_reuses_ci_without_local_suite_or_test_measurements(
    home, prepared, repairable, monkeypatch, github
):
    configure(home)
    github["check_runs"] = [
        {
            "id": 10,
            "name": "tests",
            "head_sha": prepared["commit"],
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/acme/repo/actions/runs/10",
        }
    ]
    github["statuses"] = []

    async def unexpected(*args, **kwargs):
        pytest.fail("review reran the full suite despite current successful CI")

    monkeypatch.setattr("maxwell.validation.run_checks", unexpected)
    monkeypatch.setattr("maxwell.validation.run_test_measurement", unexpected)
    from maxwell.validation import regression_replay

    async def replay(*args, **kwargs):
        from maxwell.repair import run_checks

        with monkeypatch.context() as patch:
            patch.setattr("maxwell.validation.run_checks", run_checks)
            return await regression_replay(*args, **kwargs)

    monkeypatch.setattr("maxwell.validation.regression_replay", replay)
    result = validate(home)
    assert result["valid"], result
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "accepted"
    assert record["checks"][0]["command"] == ["github-ci", prepared["commit"]]
    assert "test_runtime" not in record["after"]["factors"]
    assert "CI" in record["after"]["factor_status"]["test_runtime"]
    assert record["replay"]["returncode"] == 1

    from maxwell.auto_merge import ci_evidence_blocker
    from maxwell.repair_pull_requests import records

    reporter = repairable[2]
    reporter.config = home.load()
    link = records(reporter.store, "acme/repo", "pull_request")[0]
    pull = github["pulls"][link.number]
    assert asyncio.run(ci_evidence_blocker(reporter, "acme/repo", link, pull)) is None
    github["check_runs"][0].update(status="in_progress", conclusion=None)
    assert "pending" in asyncio.run(ci_evidence_blocker(reporter, "acme/repo", link, pull))
    github["check_runs"][0].update(status="completed", conclusion="failure")
    assert "failed" in asyncio.run(ci_evidence_blocker(reporter, "acme/repo", link, pull))
    targets = asyncio.run(collect_repair_targets(reporter, "acme/repo"))
    assert len(targets) == 1 and targets[0].merge_problem.kind == "checks"
    assert targets[0].merge_problem.checks[0]["name"] == "tests"


def test_ci_failure_returns_finding_to_fix_without_agent(
    home, prepared, repairable, monkeypatch, github
):
    configure(home)
    github["check_runs"] = [
        {
            "id": 10,
            "name": "tests",
            "head_sha": prepared["commit"],
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/acme/repo/actions/runs/10",
            "output": {"summary": "test_empty failed", "text": "AssertionError"},
        }
    ]
    github["statuses"] = []
    result = validate(home)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive"
    assert record["checks"][0]["state"] == "failed"
    assert "test_empty failed" in record["checks"][0]["detail"]
    assert result["scans"][0]["executed"] == 0
    reporter = repairable[2]
    reporter.config = home.load()
    targets = asyncio.run(collect_repair_targets(reporter, "acme/repo"))
    assert len(targets) == 1
    assert targets[0].feedback[0].checks[0].state == "failed"
    from maxwell.pipeline import load_snapshot

    snapshot = asyncio.run(load_snapshot(reporter, "acme/repo"))
    assert snapshot.items[0].stage == "fix"


def test_ci_waits_for_pending_check_then_accepts_it(home, prepared, monkeypatch, github):
    configure(home)
    github["statuses"] = []
    github["check_runs"] = [
        {
            "id": 12,
            "name": "tests",
            "head_sha": prepared["commit"],
            "status": "in_progress",
            "conclusion": None,
        }
    ]
    from maxwell.repair_pull_requests import records
    from maxwell.review_ci import wait_for_checks
    from maxwell.store import Store

    with_store = Store(home.db)
    link = records(with_store, "acme/repo", "pull_request")[0]
    with_store.close()
    progress = []

    async def completed(delay):
        github["check_runs"][0].update(status="completed", conclusion="success")

    monkeypatch.setattr("maxwell.review_ci.asyncio.sleep", completed)
    artifact = home.root / "ci-wait"
    artifact.mkdir()
    client = GitHub(home.load().github)
    try:
        evidence = asyncio.run(
            wait_for_checks(
                client,
                link,
                prepared["commit"],
                home.load().review.ci["acme/repo"],
                artifact,
                progress.append,
            )
        )
    finally:
        client.close()
    assert evidence["state"] == "passed"
    assert any("waiting for GitHub CI: tests" in item for item in progress)


def test_review_does_not_accept_ci_rerun_started_during_agent_review(
    home, prepared, monkeypatch, github
):
    configure(home)
    github["statuses"] = []
    github["check_runs"] = [
        {
            "id": 10,
            "name": "tests",
            "head_sha": prepared["commit"],
            "status": "completed",
            "conclusion": "success",
        }
    ]
    from maxwell.provider import CodexProvider

    original = CodexProvider.validate

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        github["check_runs"][0].update(id=11, conclusion="failure")
        return result

    monkeypatch.setattr(CodexProvider, "validate", changed)
    result = validate(home)
    assert not result["valid"]
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive"
    assert record["checks"][0]["state"] == "failed"
    assert "CI evidence changed" in result["scans"][0]["error"]


def test_review_consumes_ci_measurements_for_both_snapshots(home, prepared, monkeypatch):
    import json

    from test_review_ci_evidence import report

    from maxwell.measurement import compatible
    from maxwell.records import MeasurementRecord

    configure(home)
    data = yaml.safe_load(home.config.read_text())
    data["review"]["ci"]["acme/repo"]["measurement_check"] = "tests"
    data["measure"]["tests"]["acme/repo"]["runtime_budget_seconds"] = 60
    home.config.write_text(yaml.safe_dump(data))

    def checks(self, repo, sha):
        return [
            {
                "id": 1,
                "name": "tests",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
                "output": {"text": json.dumps(report(sha=sha))},
            }
        ]

    monkeypatch.setattr(GitHub, "check_runs", checks)
    original = GitHub.request

    def without_statuses(self, method, path, **kwargs):
        if path.endswith("/status"):
            return {"statuses": []}
        return original(self, method, path, **kwargs)

    monkeypatch.setattr(GitHub, "request", without_statuses)
    result = validate(home)
    assert result["valid"], result
    record = result["scans"][0]["validations"][0]
    before = MeasurementRecord.model_validate(record["before"])
    after = MeasurementRecord.model_validate(record["after"])
    assert before.factors["test_runtime"].numerator == 15
    assert after.factors["test_flakiness"].numerator == 1
    assert compatible(before, after)
    assert before.policy["tests"]["source"] == "github-ci"


def test_review_uses_local_suite_when_ci_does_not_cover_merge_candidate(
    home, source, prepared, github, monkeypatch
):
    from maxwell.repair import run_checks
    from maxwell.repositories import git

    configure(home)
    (source / "new_main.txt").write_text("unrelated main change")
    git("add", "new_main.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "advance main",
        cwd=source,
    )
    base = git("rev-parse", "HEAD", cwd=source)
    pull = next(iter(github["pulls"].values()))
    pull["base"]["sha"] = base
    github["branches"]["main"] = base
    calls = []

    async def checked(*args, **kwargs):
        calls.append(args[1].name)
        return await run_checks(*args, **kwargs)

    monkeypatch.setattr("maxwell.validation.run_checks", checked)
    result = validate(home)
    assert result["valid"], result
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "accepted"
    assert "fixed-checks" in calls
    assert record["checks"][0]["command"][0] != "github-ci"
