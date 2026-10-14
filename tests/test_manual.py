import fcntl
import json
import os
import signal
import subprocess
import sys
import time

import pytest
import yaml

from maxwell.cli import execute, parser
from maxwell.config import Paths
from maxwell.contracts import Usage
from maxwell.github import GitHub
from maxwell.progress import ScanProgress
from maxwell.store import Store


def scan_args(home, *arguments):
    return parser().parse_args(
        ["--home", str(home.root), "scan", *arguments, "--stage", "hunt", "--model", "fake-model"]
    )


def test_foreground_scan_publishes_without_daemon_and_leaves_other_jobs_alone(
    home, source, github, capsys
):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.register("acme/other", str(source), "main")
    untouched = store.enqueue("acme/other", priority=0)
    store.pause("acme/repo", True)
    try:
        execute(scan_args(home, "acme/repo"))
        text = capsys.readouterr().out
        assert "Hunt>" in text and "Hunt+" in text
        assert "Hunt+ 1/1" not in text
        assert "acme/repo: 1 new findings" in text
        assert "Status: https://github.com/acme/repo/issues/" in text
        assert "Report:" not in text and "Finding:" not in text
        assert "[WAIT]" not in text and "[STEP]" not in text
        assert "fake-provider-secret" not in text and "fake-github-secret" not in text
        scan = store.one("SELECT * FROM scans WHERE repository='acme/repo'")
        assert scan["state"] == "completed" and scan["report_sent"] == 1
        assert store.one("SELECT state FROM scans WHERE id=?", (untouched,))["state"] == "queued"
        assert store.one("SELECT state FROM findings")["state"] == "published"
        assert not home.socket.exists()
        assert not list((home.root / "repos").glob("*/worktrees/*"))
        dashboard = store.one("SELECT issue FROM dashboards WHERE repository='acme/repo'")
        github["comments"].setdefault(dashboard["issue"], []).append(
            {
                "id": 9999,
                "body": "/maxwell scan --model fake-model",
                "user": {"login": "maintainer"},
            }
        )
        execute(scan_args(home, "acme/repo", "--json"))
        result = json.loads(capsys.readouterr().out)
        assert result["valid"] and result["scans"][0]["admitted"] == 0
        assert result["scans"][0]["accounting"] == "tokens"
        assert result["scans"][0]["usage"]["input_tokens"] == 1000
        assert "cost" not in result["scans"][0]
        assert result["scans"][0]["selection"]["already_known"] == 1
        assert result["scans"][0]["selection"]["skipped"] == 1
        prompt = home.root / "artifacts" / result["scans"][0]["id"] / "prompt.txt"
        assert "Do not repeat known findings:" in prompt.read_text()
        assert "Empty input causes division by zero" in prompt.read_text()
        assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1
        assert not store.one("SELECT id FROM comments WHERE id=9999")
        assert store.one("SELECT COUNT(*) AS n FROM scans WHERE repository='acme/repo'")["n"] == 2
    finally:
        store.close()


def test_manual_multi_repo_validation_finishes_before_any_scan_is_created(home, source):
    data = yaml.safe_load(home.config.read_text())
    settings = data.pop("provider")
    data["providers"] = {
        "first": {**settings, "models": ["fake-model"]},
        "second": {**settings, "models": ["other-model"]},
    }
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/first", str(source), "main")
    store.register("acme/second", str(source), "main")
    try:
        with pytest.raises(ValueError, match="model"):
            execute(scan_args(home, "--provider", "second"))
        assert not store.rows("SELECT * FROM scans")
    finally:
        store.close()


@pytest.mark.parametrize("legacy,state", [(False, "open"), (False, "closed"), (True, "closed")])
def test_fresh_home_uses_github_findings_for_context_and_deduplication(
    home, source, directory, github, capsys, legacy, state
):
    original = Store(home.db)
    original.register("acme/repo", str(source), "main")
    original.close()
    execute(scan_args(home, "acme/repo", "--json"))
    first = json.loads(capsys.readouterr().out)
    number = int(first["scans"][0]["findings"][0]["url"].rsplit("/", 1)[1])
    issue = github["issues"][number]
    issue["state"] = state
    issue["title"] = "Existing GitHub title edited after publication"
    if legacy:
        issue["body"] = issue["body"].split("<!-- maxwell:record:v1", 1)[0]
    fresh = Paths(directory / "fresh-home")
    fresh.initialize()
    fresh.config.write_text(home.config.read_text())
    fresh.config.chmod(0o600)
    store = Store(fresh.db)
    store.register("acme/repo", str(source), "main")
    try:
        execute(scan_args(fresh, "acme/repo", "--json"))
        second = json.loads(capsys.readouterr().out)
        assert second["valid"] and second["scans"][0]["admitted"] == 0
        assert not store.rows("SELECT * FROM findings")
        prompt = fresh.root / "artifacts" / second["scans"][0]["id"] / "prompt.txt"
        assert issue["title"] in prompt.read_text()
        assert f'"state": "{state}"' in prompt.read_text()
        assert github["creates"] == 3
        if not legacy:
            assert store.one("SELECT id FROM github_records WHERE type='finding'")
            dashboard = next(
                issue["body"]
                for issue in github["issues"].values()
                if issue["title"] == "Maxwell status"
            )
            assert "| Findings | 1 |" in dashboard
            assert "| Scans | 2 |" in dashboard
            assert "| Recorded total tokens (input + output) | 2,200 |" in dashboard
    finally:
        store.close()


def test_scan_does_not_start_agent_with_stale_context_when_github_is_unavailable(
    home, source, monkeypatch, capsys
):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")

    def unavailable(*args, **kwargs):
        raise ValueError("GitHub unavailable")

    monkeypatch.setattr(GitHub, "paginate", unavailable)
    try:
        with pytest.raises(SystemExit):
            execute(scan_args(home, "acme/repo", "--json"))
        result = json.loads(capsys.readouterr().out)
        scan = store.one("SELECT * FROM scans")
        assert not result["valid"] and "GitHub unavailable" in scan["error"]
        assert scan["executed"] == 0 and scan["pid"] is None
        assert not (home.root / "artifacts" / scan["id"] / "prompt.txt").exists()
    finally:
        store.close()


def test_budget_blocked_manual_scan_fails_without_leaving_queued_work(home, source, github, capsys):
    data = yaml.safe_load(home.config.read_text())
    data["limits"]["daily_budget_usd"] = 1
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.enqueue("acme/repo")
    previous = store.claim(1_000_000, 1_000_000, 6)
    store.finish(previous, "completed", "Previous scan", Usage(), 1_000_000)
    try:
        with pytest.raises(SystemExit) as error:
            execute(scan_args(home, "acme/repo", "--json"))
        assert error.value.code == 1
        result = json.loads(capsys.readouterr().out)
        scan = result["scans"][0]
        assert not result["valid"] and scan["state"] == "failed"
        assert "budget" in scan["error"] and scan["report"] is None
        assert "human_queue_max" not in scan["error"]
        assert "daily_scan_limit" not in scan["error"]
        assert not store.rows("SELECT * FROM scans WHERE state='queued'")
        assert not github["requests"]
    finally:
        store.close()


def test_manual_scan_without_repository_runs_all_selected_models_serially(home, source, capsys):
    store = Store(home.db)
    store.register("acme/first", str(source), "main")
    store.register("acme/second", str(source), "main")
    try:
        execute(scan_args(home, "--effort", "high", "--json"))
        result = json.loads(capsys.readouterr().out)
        assert result["valid"]
        assert [scan["repository"] for scan in result["scans"]] == ["acme/first", "acme/second"]
        assert all(
            scan["model"] == "fake-model" and scan["effort"] == "high" for scan in result["scans"]
        )
        jobs = store.rows("SELECT started,finished FROM scans ORDER BY started")
        assert jobs[0]["finished"] <= jobs[1]["started"]
        assert not store.one("SELECT name FROM sqlite_master WHERE name='repository_stage_routes'")
    finally:
        store.close()


@pytest.mark.parametrize("queue_max", [1, 6])
def test_unpriced_blocked_scan_reports_daily_limit_and_only_other_active_blockers(
    home, source, capsys, queue_max
):
    data = yaml.safe_load(home.config.read_text())
    for key in ("input_usd_per_mtok", "output_usd_per_mtok", "cached_input_usd_per_mtok"):
        data["provider"].pop(key, None)
    data["providers"] = {"fake": data.pop("provider")}
    data["limits"].update(daily_scan_limit=1, human_queue_max=queue_max)
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    try:
        execute(scan_args(home, "acme/repo", "--json"))
        assert json.loads(capsys.readouterr().out)["valid"]
        with pytest.raises(SystemExit):
            execute(scan_args(home, "acme/repo", "--json"))
        blocked = json.loads(capsys.readouterr().out)
        message = blocked["scans"][0]["error"]
        assert "Daily scan limit reached: 1/1 executions today (UTC)" in message
        assert "limits.daily_scan_limit" in message and "budget" not in message
        assert ("Human review queue is full: 1/1" in message) == (queue_max == 1)
        assert store.unpriced_started_today() == 1
    finally:
        store.close()


def test_failed_model_call_returns_json_and_nonzero_exit(home, source, directory, capsys):
    (directory / "mode").write_text("malformed")
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.close()
    with pytest.raises(SystemExit) as error:
        execute(scan_args(home, "acme/repo", "--json"))
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["valid"] and result["scans"][0]["state"] == "failed"
    assert result["scans"][0]["report"]
    assert not result["scans"][0]["findings"]


def test_silent_provider_reports_waiting_until_timeout(
    home, source, directory, monkeypatch, capsys
):
    data = yaml.safe_load(home.config.read_text())
    data["limits"]["scan_timeout_seconds"] = 1
    home.config.write_text(yaml.safe_dump(data))
    (directory / "mode").write_text("wait")
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.close()
    monkeypatch.setattr(
        "maxwell.manual.ScanProgress",
        lambda job, callback: ScanProgress(job, callback, interval=0.05),
    )
    with pytest.raises(SystemExit) as error:
        execute(scan_args(home, "acme/repo"))
    assert error.value.code == 1
    text = capsys.readouterr().out
    assert "Hunt>" in text
    assert "[WAIT]" not in text
    assert "[FAIL] acme/repo" in text
    detail = "codex agent timed out after 1s (limits.scan_timeout_seconds)"
    assert detail in text
    assert detail in home.log.read_text()
    completed = Store(home.db)
    try:
        scan = completed.one("SELECT * FROM scans")
        assert detail in scan["error"]
        assert detail in scan["summary"]
        report = home.root / "reports" / f"{scan['id']}.md"
        assert detail in report.read_text()
    finally:
        completed.close()
    assert "[WAIT]" not in text[text.index("[FAIL]") :]
    assert "validating" not in text
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_manual_run_reports_publication_failure(home, source, monkeypatch, capsys):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.close()

    def unavailable(*args, **kwargs):
        raise ValueError("GitHub unavailable fake-github-secret")

    monkeypatch.setattr(GitHub, "create_issue", unavailable)
    with pytest.raises(SystemExit) as error:
        execute(scan_args(home, "acme/repo", "--json"))
    assert error.value.code == 1
    text = capsys.readouterr().out
    result = json.loads(text)
    assert result["scans"][0]["state"] == "completed"
    assert result["scans"][0]["publication_errors"]
    assert "fake-github-secret" not in text


def test_manual_scan_refuses_overlapping_runs(home):
    with home.lock.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Another Maxwell run"):
            execute(scan_args(home))


def test_closed_github_issue_frees_a_full_manual_review_queue(home, source, github, capsys):
    data = yaml.safe_load(home.config.read_text())
    data["limits"]["human_queue_max"] = 1
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    try:
        execute(scan_args(home, "acme/repo", "--json"))
        assert json.loads(capsys.readouterr().out)["valid"]
        with pytest.raises(SystemExit):
            execute(scan_args(home, "acme/repo", "--json"))
        blocked = json.loads(capsys.readouterr().out)
        assert not blocked["valid"]
        message = blocked["scans"][0]["error"]
        assert "Human review queue is full: 1/1 unresolved findings" in message
        assert "limits.human_queue_max" in message
        assert "budget" not in message and "daily_scan_limit" not in message
        issue = store.one("SELECT issue FROM findings")["issue"]
        github["issues"][issue]["state"] = "closed"
        execute(scan_args(home, "acme/repo", "--json"))
        assert json.loads(capsys.readouterr().out)["valid"]
        assert store.one("SELECT state FROM findings")["state"] == "closed"
    finally:
        store.close()


@pytest.mark.parametrize("interrupt,exit_code", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_manual_scan_interrupt_terminates_provider_and_records_cancellation(
    home, source, directory, interrupt, exit_code
):
    (directory / "mode").write_text("wait")
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "maxwell",
            "--home",
            str(home.root),
            "scan",
            "acme/repo",
            "--stage",
            "hunt",
            "--model",
            "fake-model",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    try:
        assert process.stdout.readline().startswith("acme/repo [")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            scan = store.one("SELECT * FROM scans WHERE pid IS NOT NULL")
            if scan:
                pid = scan["pid"]
                break
            time.sleep(0.02)
        assert pid
        process.send_signal(interrupt)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == exit_code, stderr
        assert "Traceback" not in stderr
        assert store.one("SELECT state FROM scans")["state"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not home.socket.exists()
        assert not list((home.root / "repos").glob("*/worktrees/*"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        store.close()
