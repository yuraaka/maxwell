import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from maxwell.daemon import Daemon, request
from maxwell.github import GitHub
from maxwell.reporting import Reporter, hunter_ratings
from maxwell.store import Store


def eventually(function, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("Expected condition did not become true")


def control(home, command, **kwargs):
    return asyncio.run(request(home, command, **kwargs))


def launch(home):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from maxwell.config import Paths; "
            "from maxwell.daemon import Daemon; paths=Paths(sys.argv[1]); "
            "asyncio.run(Daemon(paths,paths.load()).serve())",
            str(home.root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def shutdown(home, process):
    if process.poll() is None:
        try:
            control(home, "stop")
            process.wait(timeout=10)
        except Exception:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    process.stderr.close()


@pytest.fixture
def registered(home, source):
    store = Store(home.db)
    store.register("acme/repo", str(source), "HEAD")
    yield store
    store.close()


def test_end_to_end_controls_issue_costs_dedup_and_human_commands(home, registered, github):
    process = launch(home)
    try:
        eventually(lambda: control(home, "status"))
        assert home.socket.stat().st_mode & 0o777 == 0o600
        job = eventually(lambda: registered.one("SELECT * FROM scans WHERE state='completed'"))
        finding = eventually(
            lambda: registered.one("SELECT * FROM findings WHERE state='published'")
        )
        assert job["cost"] == 1900
        assert finding["allocated"] == 1900
        assert "not reproduced" in github["issues"][finding["issue"]]["body"]
        eventually(
            lambda: registered.one("SELECT report_sent FROM scans WHERE id=?", (job["id"],))[
                "report_sent"
            ]
        )
        assert "Discovery" in next(iter(github["comments"].values()))[0]["body"]
        control(home, "pause", repository="acme/repo")
        queued = control(home, "scan", repository="acme/repo", priority=1, model="fake-model")[
            "scans"
        ][0]
        assert registered.one("SELECT state FROM scans WHERE id=?", (queued,))["state"] == "queued"
        control(home, "resume", repository="acme/repo")
        eventually(
            lambda: (
                registered.one("SELECT state FROM scans WHERE id=?", (queued,))["state"]
                == "completed"
            )
        )
        assert registered.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1
        comments = github["comments"].setdefault(finding["issue"], [])
        comments.extend(
            [
                {"id": 9001, "body": "/maxwell reject fake", "user": {"login": "outsider"}},
                {"id": 9002, "body": "/maxwell confirm", "user": {"login": "maintainer"}},
            ]
        )
        eventually(lambda: registered.one("SELECT state FROM findings")["state"] == "confirmed")
        assert hunter_ratings(registered)[0]["assessments"] == 1
        assert not registered.one("SELECT id FROM comments WHERE id=9001")
        control(home, "stop")
        assert process.wait(timeout=10) == 0
        assert not home.socket.exists()
        for directory in (home.root / "artifacts", home.root / "reports", home.root / "logs"):
            for file in directory.rglob("*"):
                if file.is_file():
                    assert "fake-provider-secret" not in file.read_text(errors="replace")
                    assert "fake-github-secret" not in file.read_text(errors="replace")
    finally:
        shutdown(home, process)


@pytest.mark.parametrize(
    "mode,state,cost,findings",
    [
        ("empty", "completed", 1900, 0),
        ("malformed", "failed", 1900, 0),
        ("modify", "failed", 1900, 0),
        ("unknown", "completed", None, 1),
    ],
)
def test_discovery_failures_and_empty_scans(
    home, registered, directory, mode, state, cost, findings
):
    (directory / "mode").write_text(mode)
    registered.enqueue("acme/repo")
    job = registered.claim(20_000_000, 1_000_000, 6, "test-profile")
    daemon = Daemon(home, home.load())
    daemon.store = registered
    asyncio.run(daemon.scan(job))
    scan = registered.one("SELECT * FROM scans WHERE id=?", (job["id"],))
    assert scan["state"] == state
    assert scan["cost"] == cost
    assert registered.one("SELECT COUNT(*) AS n FROM findings")["n"] == findings
    assert (home.root / "reports" / f"{job['id']}.md").exists()
    if cost is None:
        assert registered.committed_today() == 1_000_000
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_ambiguous_issue_creation_is_reconciled(home, registered, github):
    registered.enqueue("acme/repo")
    job = registered.claim(20_000_000, 1_000_000, 6, "test-profile")
    daemon = Daemon(home, home.load())
    daemon.store = registered
    asyncio.run(daemon.scan(job))
    github["drop_create"] = True
    client = GitHub(home.load().github)
    try:
        reporter = Reporter(registered, client, home.load(), home)
        finding = registered.one("SELECT * FROM findings")
        asyncio.run(reporter.publish_finding(finding))
        assert registered.one("SELECT state FROM findings")["state"] == "uncertain"
        asyncio.run(reporter.publish_finding(registered.one("SELECT * FROM findings")))
        assert registered.one("SELECT state FROM findings")["state"] == "published"
        assert github["creates"] == 1
    finally:
        client.close()


def test_unknown_publication_does_not_blindly_retry(home, registered, finding, github):
    registered.enqueue("acme/repo")
    job = registered.claim(20_000_000, 1_000_000, 6, "test-profile")
    from maxwell.contracts import Finding, Usage

    registered.finish(job, "completed", "ok", Usage(), 1, [Finding(**finding)])
    with registered.db:
        registered.db.execute("UPDATE findings SET state='uncertain'")
    client = GitHub(home.load().github)
    try:
        reporter = Reporter(registered, client, home.load(), home)
        asyncio.run(reporter.publish_finding(registered.one("SELECT * FROM findings")))
        assert github["creates"] == 0
        assert registered.one("SELECT state FROM findings")["state"] == "uncertain"
    finally:
        client.close()


def test_daemon_singleton_and_hard_crash_recovery(home, registered, directory):
    (directory / "mode").write_text("wait")
    first = launch(home)
    second = None
    try:
        eventually(lambda: control(home, "status"))
        running = eventually(lambda: registered.one("SELECT * FROM scans WHERE pid IS NOT NULL"))
        duplicate = launch(home)
        assert duplicate.wait(timeout=5) == 1
        assert "already running" in duplicate.stderr.read().decode()
        duplicate.stderr.close()
        first.kill()
        first.wait(timeout=5)
        registered.pause("acme/repo", True)
        second = launch(home)
        eventually(lambda: control(home, "status"))
        eventually(
            lambda: (
                registered.one("SELECT state FROM scans WHERE id=?", (running["id"],))["state"]
                == "interrupted"
            )
        )
        assert registered.committed_today() == 1_000_000
        assert registered.one("SELECT COUNT(*) AS n FROM scans")["n"] == 1
        eventually(
            lambda: (
                not subprocess.run(
                    ["ps", "-p", str(running["pid"])], capture_output=True
                ).returncode
                == 0
            )
        )
    finally:
        shutdown(home, first)
        if second:
            shutdown(home, second)


def test_timeout_retains_unknown_spending_and_cleans_workspace(home, registered, directory):
    (directory / "mode").write_text("wait")
    config = home.load()
    config.limits.scan_timeout_seconds = 1
    registered.enqueue("acme/repo")
    job = registered.claim(20_000_000, 1_000_000, 6, "test-profile")
    daemon = Daemon(home, config)
    daemon.store = registered
    asyncio.run(daemon.scan(job))
    record = registered.one("SELECT * FROM scans WHERE id=?", (job["id"],))
    assert record["state"] == "failed"
    assert record["cost"] is None
    assert registered.committed_today() == 1_000_000
    assert not list((home.root / "repos").glob("*/worktrees/*"))
