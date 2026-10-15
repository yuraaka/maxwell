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
from maxwell.contracts import Usage
from maxwell.github import GitHub
from maxwell.store import Store


def scan_args(home, *arguments):
    return parser().parse_args(
        ["--home", str(home.root), "scan", *arguments, "--model", "fake-model"]
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
        assert "[RUN] acme/repo" in text and "[PUB] acme/repo" in text
        assert "[OK] acme/repo: 1 new findings" in text
        assert "Report:" in text and "Finding:" in text
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
    store.register(
        "acme/first", str(source), "main", route={"provider": "first", "model": "fake-model"}
    )
    store.register(
        "acme/second", str(source), "main", route={"provider": "second", "model": "other-model"}
    )
    try:
        with pytest.raises(ValueError, match="model"):
            execute(scan_args(home))
        assert not store.rows("SELECT * FROM scans")
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
        assert not store.rows("SELECT * FROM repository_stage_routes")
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
        assert not json.loads(capsys.readouterr().out)["valid"]
        issue = store.one("SELECT issue FROM findings")["issue"]
        github["issues"][issue]["state"] = "closed"
        execute(scan_args(home, "acme/repo", "--json"))
        assert json.loads(capsys.readouterr().out)["valid"]
        assert store.one("SELECT state FROM findings")["state"] == "closed"
    finally:
        store.close()


def test_blocked_manual_scan_retries_unpublished_findings_that_fill_the_queue(
    home, source, github, monkeypatch, capsys
):
    data = yaml.safe_load(home.config.read_text())
    data["limits"]["human_queue_max"] = 1
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    gate = {"available": False}
    original = GitHub.create_issue

    def flaky(self, *args, **kwargs):
        if not gate["available"]:
            raise ValueError("GitHub unavailable fake-github-secret")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GitHub, "create_issue", flaky)

    def run():
        with pytest.raises(SystemExit):
            execute(scan_args(home, "acme/repo", "--json"))
        text = capsys.readouterr().out
        assert "fake-github-secret" not in text
        return json.loads(text)["scans"][0]

    try:
        first = run()
        assert first["state"] == "completed" and first["publication_errors"]
        finding = store.one("SELECT state,issue FROM findings")
        assert finding["state"] == "pending" and finding["issue"] is None
        blocked = run()
        assert blocked["state"] == "failed" and "review queue" in blocked["error"]
        assert blocked["publication_errors"]
        assert store.one("SELECT state,issue FROM findings")["issue"] is None
        gate["available"] = True
        recovered = run()
        assert recovered["state"] == "failed" and not recovered["publication_errors"]
        finding = store.one("SELECT state,issue FROM findings")
        assert finding["state"] == "published" and finding["issue"]
        assert store.one("SELECT state FROM dashboards")["state"] == "published"
        github["issues"][finding["issue"]]["state"] = "closed"
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
            "--model",
            "fake-model",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    try:
        assert process.stdout.readline().startswith("[RUN] acme/repo")
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
