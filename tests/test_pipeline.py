import asyncio
import hashlib
import json
import sqlite3

import httpx
import pytest
import yaml

from maxwell.cli import execute, parser
from maxwell.contracts import Finding, Usage
from maxwell.github import GitHub, PublicationUncertain
from maxwell.manual import run_scans
from maxwell.records import (
    AssessmentRecord,
    ExecutionRecord,
    FindingRecord,
    PublicAgent,
    decode_record,
    encode_record,
)
from maxwell.repositories import git
from maxwell.store import Store


@pytest.fixture
def automatic(home, source):
    data = yaml.safe_load(home.config.read_text())
    data["provider"]["models"] = ["fake-model", "model-b", "model-c"]
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    config = home.load()
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
    yield home, store, config
    store.close()


@pytest.fixture
def backlog(automatic, finding, source):
    _, _, config = automatic
    client = GitHub(config.github)
    try:
        for number in range(2):
            item = Finding(**{**finding, "title": f"Prior finding {number}"})
            record = FindingRecord(
                id=item.identity("acme/repo"),
                repository="acme/repo",
                sha=git("rev-parse", "HEAD", cwd=source),
                execution_id=f"{number:032x}",
                finding=item,
                agent=PublicAgent(adapter="codex", model="prior-hunter", role="hunter"),
            )
            client.create_issue(
                "acme/repo",
                item.title,
                f"<!-- maxwell:finding:{record.id} -->\n" + encode_record(record),
            )
    finally:
        client.close()
    config.limits.max_reviews_per_scan = 3
    config.limits.max_audits_per_scan = 3


def test_no_argument_scan_publishes_hunt_review_audit_in_order(automatic, github, capsys):
    home, store, _ = automatic
    execute(parser().parse_args(["--home", str(home.root), "scan", "--json"]))
    result = json.loads(capsys.readouterr().out)
    assert result["valid"]
    scans = result["scans"]
    assert [item["stage"] for item in scans] == ["hunt", "review", "audit"]
    assert len({item["model"] for item in scans}) == 3
    assert all(item["state"] == "completed" for item in scans)
    assert scans[1]["agent_selection"]["independence"] == "different_model"
    assert scans[2]["agent_selection"]["method"] == "uniform-unrated-v1"
    finding_issue = store.one("SELECT issue FROM findings")["issue"]
    assessments = [decode_record(item["body"]) for item in github["comments"][finding_issue]]
    assert [item.role for item in assessments] == ["reviewer", "auditor"]
    assert assessments[1].references == [assessments[0].id]
    dashboard = store.one("SELECT issue FROM dashboards")["issue"]
    history = store.one("SELECT issue FROM histories")["issue"]
    assert not github["comments"].get(dashboard)
    executions = [decode_record(item["body"]) for item in github["comments"][history]]
    assert all(isinstance(item, ExecutionRecord) for item in executions)
    assert {item.id: item.agent_selection.model_dump() for item in executions} == {
        item["id"]: item["agent_selection"] for item in scans
    }
    jobs = store.rows("SELECT started,finished,report_sent FROM scans ORDER BY created")
    assert all(job["report_sent"] for job in jobs)
    assert all(first["finished"] <= second["started"] for first, second in zip(jobs, jobs[1:]))
    execute(parser().parse_args(["--home", str(home.root), "scan", "--json"]))
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["valid"] and [item["stage"] for item in repeated["scans"]] == ["hunt"]
    assert {item["stage"] for item in repeated["skipped"]} == {"review", "audit"}


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pipeline_retains_job_during_quota_wait_and_resumes_without_duplicates(
    automatic, github, monkeypatch, method
):
    home, store, config = automatic
    send = httpx.Client.send
    waits, progress = [], []
    rejected = False

    def latest_job():
        connection = sqlite3.connect(home.db)
        try:
            return connection.execute(
                "SELECT state FROM scans ORDER BY created DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()

    def limited(client, request, **kwargs):
        nonlocal rejected
        job = latest_job()
        expected = "running" if method == "GET" else "completed"
        if (
            request.method == method
            and request.url.path.endswith("/issues")
            and job
            and job[0] == expected
            and not rejected
        ):
            rejected = True
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
                request=request,
            )
        return send(client, request, **kwargs)

    def wait(client, response, delay):
        waits.append(latest_job())
        if client.on_status:
            client.on_status("waiting for GitHub primary quota; remaining 0; job retained")

    monkeypatch.setattr(httpx.Client, "send", limited)
    monkeypatch.setattr(GitHub, "wait_for_quota", wait)
    result = asyncio.run(run_scans(home, config, on_progress=progress.append))
    assert result["valid"], result
    assert len(waits) == 1
    assert waits[0][0] == ("running" if method == "GET" else "completed")
    assert any("waiting for GitHub" in item.get("phase", "") for item in progress)
    assert [scan["state"] for scan in result["scans"]] == ["completed"] * 3
    assert github["creates"] == 3


@pytest.mark.parametrize("lost_kind", ["finding", "dashboard", "history", "comment"])
def test_pipeline_recovers_transient_github_failures_without_duplicate_creates(
    automatic, github, monkeypatch, lost_kind
):
    home, store, config = automatic
    send = httpx.Client.send
    faults = {"lost_issue_response": 0, "comments_502": 0}

    def unreliable(client, request, **kwargs):
        path = request.url.path
        if (
            request.method == "POST"
            and (
                path.endswith("/comments")
                if lost_kind == "comment"
                else path.endswith("/issues")
                and f"maxwell:{lost_kind}:".encode() in request.content
            )
            and not faults["lost_issue_response"]
        ):
            faults["lost_issue_response"] += 1
            send(client, request, **kwargs)
            return httpx.Response(502, request=request)
        if request.method == "GET" and path.endswith("/comments") and not faults["comments_502"]:
            faults["comments_502"] += 1
            return httpx.Response(502, request=request)
        return send(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", unreliable)
    report = asyncio.run(run_scans(home, config))
    assert report["valid"], report
    assert [item["stage"] for item in report["scans"]] == ["hunt", "review", "audit"]
    assert faults == {"lost_issue_response": 1, "comments_502": 1}
    assert github["creates"] == 3
    assert all(not item["publication_errors"] for item in report["scans"])
    assert all(row["report_sent"] for row in store.rows("SELECT report_sent FROM scans"))
    assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1


def test_explicit_stage_can_use_automatic_model_selection(automatic):
    home, _, config = automatic
    report = asyncio.run(run_scans(home, config, stage="hunt"))
    assert report["valid"] and [item["stage"] for item in report["scans"]] == ["hunt"]
    assert report["scans"][0]["agent_selection"]["method"] == "thompson-source-v1"


def test_model_override_without_stage_still_runs_pipeline(automatic):
    home, _, config = automatic
    report = asyncio.run(run_scans(home, config, model="fake-model"))
    assert report["valid"] and len(report["scans"]) == 3
    assert {item["model"] for item in report["scans"]} == {"fake-model"}
    assert report["scans"][1]["agent_selection"]["independence"] == "overridden"


@pytest.mark.parametrize("mode", ["malformed", "wait"])
def test_pipeline_checks_remaining_stages_on_hunter_failure(automatic, directory, mode):
    home, store, config = automatic
    if mode == "wait":
        config.limits.scan_timeout_seconds = 1
    (directory / "mode").write_text(mode)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"] and [item["stage"] for item in report["scans"]] == ["hunt"]
    if mode == "wait":
        assert "codex agent timed out after 1s" in report["scans"][0]["error"]
    assert {item["stage"] for item in report["skipped"]} == {"review", "audit"}
    assert all("No eligible findings" == item["reason"] for item in report["skipped"])
    assert not store.rows("SELECT * FROM scans WHERE state='queued'")


def test_pipeline_reconciles_uncertain_publication_without_repeating_agent(automatic, monkeypatch):
    home, store, config = automatic
    create = GitHub.create_comment

    def lost(*args):
        create(*args)
        raise PublicationUncertain("Response lost")

    monkeypatch.setattr(GitHub, "create_comment", lost)
    report = asyncio.run(run_scans(home, config))
    assert report["valid"] and len(report["scans"]) == 3
    assert all(not item["publication_errors"] for item in report["scans"])
    assert all(row["report_sent"] for row in store.rows("SELECT report_sent FROM scans"))


@pytest.mark.parametrize("failed_stage", ["discovery", "review", "audit"])
def test_pipeline_continues_other_jobs_and_stages_after_execution_failure(
    automatic, backlog, monkeypatch, failed_stage
):
    from maxwell.daemon import Daemon

    home, _, config = automatic
    scan = Daemon.scan
    failed = False

    async def fail_one(engine, job, **kwargs):
        nonlocal failed
        if json.loads(job["execution"])["stage"] == failed_stage and not failed:
            failed = True
            engine.store.finish(job, "failed", "Fixture failure", Usage(), 0)
        else:
            await scan(engine, job, **kwargs)

    monkeypatch.setattr(Daemon, "scan", fail_one)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"] and failed
    assert sum(item["state"] == "failed" for item in report["scans"]) == 1
    assert sum(item["stage"] == "review" for item in report["scans"]) >= 2
    assert sum(item["stage"] == "audit" for item in report["scans"]) == (
        3 if failed_stage == "audit" else 2
    )
    assert (
        sum(item["state"] == "completed" and item["stage"] == "audit" for item in report["scans"])
        == 2
    )
    assert not report["skipped"]


def test_unresolved_hunt_publication_does_not_fail_reviews_or_repost_issue(
    automatic, backlog, monkeypatch
):
    home, store, config = automatic
    send = httpx.Client.send
    posts = 0

    def unavailable(client, request, **kwargs):
        nonlocal posts
        if request.method == "POST" and b"maxwell:finding:" in request.content:
            posts += 1
            return httpx.Response(502, request=request)
        return send(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", unavailable)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"] and posts == 1
    assert report["scans"][0]["publication_errors"]
    assert [item["stage"] for item in report["scans"]] == [
        "hunt",
        "review",
        "review",
        "audit",
        "audit",
    ]
    assert all(
        item["state"] == "completed" and not item["publication_errors"]
        for item in report["scans"][1:]
    )
    assert store.one("SELECT state FROM findings")["state"] == "uncertain"
    repeat = asyncio.run(run_scans(home, config, stage="hunt"))
    assert not repeat["valid"] and posts == 1
    assert repeat["scans"][0]["publication_errors"]


def test_pipeline_respects_admission_limit_and_checks_audit_eligibility(automatic):
    home, store, config = automatic
    config.limits.daily_scan_limit = 1
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"]
    assert [item["stage"] for item in report["scans"]] == ["hunt", "review"]
    assert report["scans"][1]["state"] == "failed"
    assert "daily_scan_limit" in report["scans"][1]["error"]
    assert report["skipped"] == [
        {"repository": "acme/repo", "stage": "audit", "reason": "No eligible findings"}
    ]
    assert not store.rows("SELECT * FROM scans WHERE state='queued'")


def test_pipeline_continues_other_repositories_after_failure(automatic, source, monkeypatch):
    from maxwell.daemon import Daemon

    home, store, config = automatic
    store.register("acme/second", str(source), "main")
    scan = Daemon.scan

    async def fail_first(engine, job, **kwargs):
        if job["repository"] == "acme/repo":
            engine.store.finish(job, "failed", "Fixture failure", Usage(), 0)
        else:
            await scan(engine, job, **kwargs)

    monkeypatch.setattr(Daemon, "scan", fail_first)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"]
    assert [(item["repository"], item["stage"]) for item in report["scans"]] == [
        ("acme/repo", "hunt"),
        ("acme/second", "hunt"),
        ("acme/second", "review"),
        ("acme/second", "audit"),
    ]


def test_pipeline_reports_context_failure_without_launching_agent(automatic, monkeypatch):
    home, store, config = automatic

    def unavailable(*args, **kwargs):
        raise ValueError("GitHub unavailable")

    monkeypatch.setattr(GitHub, "paginate", unavailable)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"] and report["scans"] == []
    assert report["errors"] == [
        {"repository": "acme/repo", "stage": stage, "error": "GitHub unavailable"}
        for stage in ("hunt", "review", "audit")
    ]
    assert not store.rows("SELECT * FROM scans")


def test_later_stages_run_after_hunt_preparation_error(automatic, backlog, monkeypatch):
    home, _, config = automatic
    paginate = GitHub.paginate
    failed = False

    def unavailable_once(client, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ValueError("GitHub unavailable")
        return paginate(client, *args, **kwargs)

    monkeypatch.setattr(GitHub, "paginate", unavailable_once)
    report = asyncio.run(run_scans(home, config))
    assert not report["valid"]
    assert report["errors"] == [
        {"repository": "acme/repo", "stage": "hunt", "error": "GitHub unavailable"}
    ]
    assert [item["stage"] for item in report["scans"]] == ["review", "review", "audit", "audit"]
    assert all(item["state"] == "completed" for item in report["scans"])


def test_pipeline_handles_empty_hunt_but_reviews_backlog_with_stage_limits(
    automatic, finding, source, github, directory
):
    home, store, config = automatic
    (directory / "mode").write_text("empty")
    client = GitHub(config.github)
    try:
        for number in range(3):
            item = Finding(**{**finding, "title": f"{finding['title']} case {number}"})
            record = FindingRecord(
                id=item.identity("acme/repo"),
                repository="acme/repo",
                sha=git("rev-parse", "HEAD", cwd=source),
                execution_id=f"{number:032x}",
                finding=item,
                agent=PublicAgent(adapter="codex", model="prior-hunter", role="hunter"),
            )
            client.create_issue(
                "acme/repo",
                item.title,
                f"<!-- maxwell:finding:{record.id} -->\n" + encode_record(record),
            )
    finally:
        client.close()
    config.limits.max_reviews_per_scan = 2
    config.limits.max_audits_per_scan = 1
    report = asyncio.run(run_scans(home, config))
    assert report["valid"]
    assert [item["stage"] for item in report["scans"]] == ["hunt", "review", "review", "audit"]
    assert report["scans"][0]["admitted"] == 0
    assessments = [
        decode_record(item["body"]) for comments in github["comments"].values() for item in comments
    ]
    assert sum(isinstance(item, AssessmentRecord) for item in assessments) == 3
