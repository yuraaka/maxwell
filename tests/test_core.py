import sqlite3
import time

import pytest

from maxwell.config import Config, Paths
from maxwell.contracts import Finding, Usage, estimate_cost
from maxwell.repositories import Repository, git
from maxwell.store import Store


def test_init_is_private_and_credentials_are_not_dumped(directory):
    paths = Paths(directory / "init")
    paths.init_config()
    config = paths.load()
    assert config.github.token.get_secret_value() == ""
    assert paths.config.stat().st_mode & 0o777 == 0o600
    assert paths.root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        paths.init_config()


@pytest.mark.parametrize(
    "body",
    [
        "unknown: secret-value",
        "limits: {daily_budget_usd: -1}",
        "limits: {daily_budget_usd: .inf}",
        "[not, a, mapping]",
    ],
)
def test_bad_config_never_echoes_values(directory, body):
    paths = Paths(directory)
    paths.config.write_text(body)
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid config.yaml") as error:
        paths.load()
    assert "secret-value" not in str(error.value)


def test_cost_excludes_cached_tokens_from_regular_input_rate():
    config = Config(
        provider={
            "input_usd_per_mtok": 2,
            "cached_input_usd_per_mtok": 0.2,
            "output_usd_per_mtok": 8,
        }
    )
    usage = Usage(
        input_tokens=1000, cached_input_tokens=500, output_tokens=100, reported=True, complete=True
    )
    assert estimate_cost(usage, config.provider) == 1900
    usage.complete = False
    assert estimate_cost(usage, config.provider) is None


def test_worktrees_pin_commits_and_preserve_dirty_source(home, source):
    original = git("rev-parse", "HEAD", cwd=source)
    (source / "app.py").write_text("uncommitted changes")
    repo = Repository(home, {"name": "acme/repo", "source": str(source), "ref": "HEAD"})
    sha, workspace = repo.prepare("test-scan")
    assert sha == original
    assert "def average" in (workspace / "app.py").read_text()
    assert repo.unchanged(workspace, sha)
    (workspace / "new.txt").write_text("unexpected")
    assert not repo.unchanged(workspace, sha)
    repo.cleanup(workspace)
    assert not workspace.exists()
    assert (source / "app.py").read_text() == "uncommitted changes"


@pytest.mark.parametrize("path", ["../config.yaml", "/etc/passwd", ".git/config", "missing.py"])
def test_finding_paths_must_reference_snapshot(source, finding, path):
    finding["path"] = path
    with pytest.raises(ValueError):
        Finding(**finding).validate_location(source)


def test_finding_identity_survives_line_movement(finding):
    first = Finding(**finding)
    finding["line"] = 50
    assert first.identity("acme/repo") == Finding(**finding).identity("acme/repo")


def test_queue_priority_atomic_claims_and_fencing(directory):
    store = Store(directory / "state.sqlite")
    for name in ("low", "urgent", "aged"):
        store.register(f"acme/{name}", "/unused", "HEAD")
    store.enqueue("acme/low", 2)
    urgent = store.enqueue("acme/urgent", 0)
    aged = store.enqueue("acme/aged", 3)
    with store.db:
        store.db.execute("UPDATE scans SET created=? WHERE id=?", (time.time() - 4 * 86400, aged))
    job = store.claim(10_000_000, 1_000_000, 6, "profile")
    assert job["id"] == urgent
    second = Store(directory / "state.sqlite")
    assert second.claim(10_000_000, 1_000_000, 6, "profile")["id"] == aged
    with pytest.raises(ValueError, match="Stale"):
        store.finish({**job, "lease": "expired"}, "completed", "bad", Usage(), 0)
    store.finish(job, "completed", "ok", Usage(), 0)
    with pytest.raises(ValueError, match="Stale"):
        store.finish(job, "completed", "duplicate", Usage(), 0)
    second.close()
    store.close()


def test_manual_scan_upgrades_queued_periodic_work(directory):
    store = Store(directory / "state.sqlite")
    store.register("acme/repo", "/unused", "HEAD")
    identifier = store.enqueue("acme/repo", priority=3, forced=False)
    assert store.enqueue("acme/repo", priority=0, forced=True) == identifier
    row = store.one("SELECT priority,forced FROM scans WHERE id=?", (identifier,))
    assert row == {"priority": 0, "forced": 1}
    store.close()


def test_accounting_allocations_dedup_and_unknown_holds(directory, finding):
    store = Store(directory / "state.sqlite")
    store.register("acme/repo", "/unused", "HEAD")
    store.enqueue("acme/repo")
    job = store.claim(1_000_000, 1_000_000, 6, "profile")
    one = Finding(**finding)
    two = Finding(**{**finding, "title": "Another distinct division issue"})
    store.finish(job, "completed", "ok", Usage(), 1901, [one, two])
    assert sum(row["allocated"] for row in store.rows("SELECT allocated FROM findings")) == 1901
    store.enqueue("acme/repo")
    assert store.claim(1_000_000, 1_000_000, 6, "profile") is None
    job = store.claim(2_000_000, 1_000_000, 6, "profile")
    store.finish(job, "failed", "unknown cost", Usage(), None, [one])
    assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 2
    assert store.committed_today() == 1_001_901
    with store.db:
        store.db.execute("UPDATE scans SET started=0 WHERE cost IS NULL")
    assert store.committed_today() == 1_001_901
    store.close()


def test_newer_database_refused(directory):
    path = directory / "new.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"):
        Store(path)
