import asyncio
import json
import sqlite3

import pytest
import yaml

from maxwell.cli import execute, parser, register_repository
from maxwell.daemon import Daemon
from maxwell.repositories import Repository, git
from maxwell.store import Store


def commit(path, filename, content):
    (path / filename).write_text(content)
    git("add", filename, cwd=path)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        filename,
        cwd=path,
    )
    return git("rev-parse", "HEAD", cwd=path)


@pytest.fixture
def checkout(directory, source):
    path = directory / "checkout"
    git("clone", str(source), str(path))
    git("remote", "set-url", "origin", "git@github.com:company/backend.git", cwd=path)
    return path


@pytest.fixture
def fetches(source, monkeypatch):
    calls = []

    def local_fetch(*args, cwd=None, token=None):
        remote = "https://github.com/company/backend.git"
        if "fetch" in args and remote in args:
            calls.append({"token": token, "directory": cwd})
            args = tuple(str(source) if arg == remote else arg for arg in args)
            token = None
        return git(*args, cwd=cwd, token=token)

    monkeypatch.setattr("maxwell.repositories.git", local_fetch)
    return calls


def register(home, checkout, *options):
    args = parser().parse_args(
        ["repo", "add", "company/backend", "--directory", str(checkout), *options]
    )
    return register_repository(args, home, home.load())


def change(home, *options):
    execute(
        parser().parse_args(
            ["--home", str(home.root), "repo", "change", "company/backend", "--json", *options]
        )
    )


def test_directory_registration_and_partial_changes_preserve_history(home, checkout, capsys):
    register(home, checkout)
    store = Store(home.db)
    try:
        record = store.one("SELECT * FROM repositories")
        assert record["directory"] == str(checkout)
        assert record["ref"] == "main"
        assert record["github_account"] == "default"
        job = store.enqueue("company/backend")
        store.pause("company/backend", True)
        change(home, "--ref", "release/next")
        changed = json.loads(capsys.readouterr().out)
        assert changed["ref"] == "release/next"
        assert changed["directory"] == str(checkout)
        assert changed["github_account"] == "default"
        assert changed["paused"] == 1
        assert store.one("SELECT id FROM scans")["id"] == job
        assert store.one("SELECT name FROM repositories")["name"] == "company/backend"
        change(home, "--no-directory")
        changed = json.loads(capsys.readouterr().out)
        assert changed["directory"] is None
        assert changed["ref"] == "release/next"
        assert changed["source_kind"] == "github"
        assert checkout.exists()
    finally:
        store.close()


def test_change_converts_local_registration_to_maintained_checkout(home, checkout, capsys):
    store = Store(home.db)
    store.register("company/backend", str(checkout), "HEAD")
    store.close()
    change(home, "--directory", str(checkout), "--ref", "main")
    record = json.loads(capsys.readouterr().out)
    assert record["source_kind"] == "github"
    assert record["source"] == "https://github.com/company/backend.git"
    assert record["directory"] == str(checkout)
    assert record["ref"] == "main"


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--ref", "develop", "--account", "missing"],
        ["--ref", "HEAD"],
        ["--ref=-bad"],
        ["--ref", "main:other"],
    ],
)
def test_invalid_changes_are_atomic(home, checkout, options):
    register(home, checkout)
    store = Store(home.db)
    before = store.one("SELECT * FROM repositories")
    try:
        with pytest.raises((ValueError, SystemExit)):
            change(home, *options)
        assert store.one("SELECT * FROM repositories") == before
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["missing", "plain", "nested", "wrong-origin", "empty"])
def test_invalid_directory_is_not_registered(home, checkout, directory, kind):
    path = checkout
    if kind == "missing":
        path = directory / "missing"
    elif kind == "plain":
        path = directory
    elif kind == "nested":
        path = checkout / "subdirectory"
        path.mkdir()
    elif kind == "wrong-origin":
        git("remote", "set-url", "origin", "https://github.com/other/project.git", cwd=path)
    else:
        path = ""
    with pytest.raises(ValueError):
        register(home, path)
    store = Store(home.db)
    try:
        assert store.rows("SELECT * FROM repositories") == []
    finally:
        store.close()


def test_managed_checkout_switches_and_pulls_before_each_scan(home, source, checkout, fetches):
    record = register(home, checkout)
    record["name"] = record.pop("repository")
    git("switch", "-c", "feature", cwd=checkout)
    sha = commit(source, "new.txt", "upstream update")
    repo = Repository(home, record)
    scanned, workspace = repo.prepare("first", token="selected-token")
    assert scanned == sha == git("rev-parse", "HEAD", cwd=checkout)
    assert git("rev-parse", "origin/main", cwd=checkout) == sha
    assert git("branch", "--show-current", cwd=checkout) == "main"
    assert (workspace / "new.txt").read_text() == "upstream update"
    assert workspace != checkout
    repo.cleanup(workspace)
    sha = commit(source, "new.txt", "second update")
    scanned, workspace = repo.prepare("second", token="other-token")
    assert scanned == sha == git("rev-parse", "HEAD", cwd=checkout)
    assert (checkout / "new.txt").read_text() == "second update"
    assert fetches == [
        {"token": "selected-token", "directory": str(checkout)},
        {"token": "other-token", "directory": str(checkout)},
    ]
    repo.cleanup(workspace)
    assert (checkout / ".git").exists()


def test_daemon_uses_changed_branch_directory_and_account(home, source, checkout, fetches):
    register(home, checkout)
    replacement = checkout.parent / "replacement"
    git("clone", str(checkout), str(replacement))
    git("remote", "set-url", "origin", "https://github.com/company/backend.git", cwd=replacement)
    git(
        "config",
        "remote.origin.fetch",
        "+refs/heads/main:refs/remotes/origin/main",
        cwd=replacement,
    )
    git("switch", "-c", "release/next", cwd=source)
    sha = commit(source, "release.txt", "new branch")
    data = yaml.safe_load(home.config.read_text())
    data["github"]["accounts"] = {"work": {"token": "work-token"}}
    home.config.write_text(yaml.safe_dump(data))
    change(home, "--directory", str(replacement), "--ref", "release/next", "--account", "work")
    config = home.load()
    daemon = Daemon(home, config)
    store = daemon.store = Store(home.db)
    try:
        store.enqueue("company/backend")
        job = store.claim(10_000_000, 1_000_000, 6, "profile")
        asyncio.run(daemon.scan(job))
        scan = store.one("SELECT * FROM scans")
        assert scan["state"] == "completed", scan["error"]
        assert scan["sha"] == sha
        assert git("branch", "--show-current", cwd=replacement) == "release/next"
        assert git("rev-parse", "origin/release/next", cwd=replacement) == sha
        assert git("branch", "--show-current", cwd=checkout) == "main"
        assert fetches == [{"token": "work-token", "directory": str(replacement)}]
    finally:
        store.close()


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "staged"])
def test_dirty_checkout_is_unchanged_when_scan_fails(home, checkout, fetches, dirty):
    record = register(home, checkout)
    record["name"] = record.pop("repository")
    git("switch", "-c", "feature", cwd=checkout)
    path = checkout / ("app.py" if dirty == "tracked" else "new.txt")
    path.write_text("local work")
    if dirty == "staged":
        git("add", path.name, cwd=checkout)
    repo = Repository(home, record)
    with pytest.raises(ValueError, match="uncommitted changes"):
        repo.prepare("dirty", token="selected-token")
    assert path.read_text() == "local work"
    assert git("branch", "--show-current", cwd=checkout) == "feature"
    assert fetches == []


def test_diverged_branch_fails_without_switching_or_resetting(home, source, checkout, fetches):
    record = register(home, checkout)
    record["name"] = record.pop("repository")
    local_sha = commit(checkout, "local.txt", "local commit")
    git("switch", "-c", "feature", cwd=checkout)
    commit(source, "upstream.txt", "upstream commit")
    repo = Repository(home, record)
    with pytest.raises(ValueError, match="diverged"):
        repo.prepare("diverged", token="selected-token")
    assert git("branch", "--show-current", cwd=checkout) == "feature"
    assert git("rev-parse", "main", cwd=checkout) == local_sha
    assert (checkout / "local.txt").read_text() == "local commit"


def test_checkout_keeps_local_commits_ahead_of_remote(home, checkout, fetches):
    record = register(home, checkout)
    record["name"] = record.pop("repository")
    sha = commit(checkout, "local.txt", "local commit")
    repo = Repository(home, record)
    scanned, workspace = repo.prepare("ahead", token="selected-token")
    assert scanned == sha == git("rev-parse", "HEAD", cwd=checkout)
    assert (workspace / "local.txt").read_text() == "local commit"
    repo.cleanup(workspace)


def test_checkout_update_does_not_overwrite_ignored_files(home, source, checkout, fetches):
    record = register(home, checkout)
    record["name"] = record.pop("repository")
    (checkout / ".git" / "info" / "exclude").write_text("local.txt\n")
    (checkout / "local.txt").write_text("ignored local data")
    commit(source, "local.txt", "upstream file")
    repo = Repository(home, record)
    with pytest.raises(ValueError, match="would be overwritten"):
        repo.prepare("ignored", token="selected-token")
    assert (checkout / "local.txt").read_text() == "ignored local data"


def test_missing_remote_branch_fails_without_switching_checkout(home, checkout, fetches):
    record = register(home, checkout, "--ref", "missing")
    record["name"] = record.pop("repository")
    sha = git("rev-parse", "HEAD", cwd=checkout)
    repo = Repository(home, record)
    with pytest.raises(ValueError, match="remote ref"):
        repo.prepare("missing", token="selected-token")
    assert git("branch", "--show-current", cwd=checkout) == "main"
    assert git("rev-parse", "HEAD", cwd=checkout) == sha


def test_v2_registration_migrates_without_enabling_checkout_management(directory):
    path = directory / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE repositories(name TEXT PRIMARY KEY,source TEXT NOT NULL,ref TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0,next_scan REAL NOT NULL DEFAULT 0,last_sha TEXT,
            source_kind TEXT NOT NULL DEFAULT 'local',github_account TEXT NOT NULL DEFAULT 'default');
            INSERT INTO repositories(name,source,ref,source_kind,github_account,paused,last_sha)
            VALUES('company/backend','https://github.com/company/backend.git','develop',
                   'github','work',1,'existing-sha');
            PRAGMA user_version=2;
        """)
    for _ in range(2):
        store = Store(path)
        try:
            record = store.one("SELECT * FROM repositories")
            assert record["directory"] is None
            assert record["ref"] == "develop"
            assert record["github_account"] == "work"
            assert record["paused"] == 1
            assert record["last_sha"] == "existing-sha"
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 13
        finally:
            store.close()


def test_pull_fetch_uses_pinned_base_when_target_branch_has_advanced(home, source, monkeypatch):
    import maxwell.repositories as repositories

    original = repositories.git("rev-parse", "HEAD", cwd=source)
    (source / "next.txt").write_text("advanced maintained branch")
    repositories.git("add", "next.txt", cwd=source)
    repositories.git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "advance maintained branch",
        cwd=source,
    )
    head = repositories.git("rev-parse", "HEAD", cwd=source)
    calls, real_git = [], repositories.git

    def local_transport(*args, **kwargs):
        if "https://github.com/acme/repo.git" in args:
            calls.append(args)
            args = tuple(
                str(source)
                if value == "https://github.com/acme/repo.git"
                else "refs/heads/main"
                if value == "refs/pull/12/head"
                else value
                for value in args
            )
            kwargs.pop("token", None)
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repositories, "git", local_transport)
    repository = repositories.Repository(
        home, {"name": "acme/repo", "source": str(source), "ref": "main"}
    )
    repository.fetch_pull(12, head, "main", original, "test-token")
    assert [call[-1] for call in calls] == [original, "refs/pull/12/head"]
    assert real_git("-C", str(repository.mirror), "rev-parse", "FETCH_HEAD") == head
    real_git("-C", str(repository.mirror), "cat-file", "-e", original)
