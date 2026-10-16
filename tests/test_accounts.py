import asyncio
import base64
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from maxwell.cli import parser, register_repository
from maxwell.config import Config
from maxwell.daemon import Daemon
from maxwell.repositories import Repository, git, github_name
from maxwell.store import Store


def accounts(home):
    data = yaml.safe_load(home.config.read_text())
    data["github"]["accounts"] = {
        "personal": {"token": "personal-token", "allowed_users": ["personal-owner"]},
        "work": {"token": "work-token", "allowed_users": ["work-owner"]},
    }
    home.config.write_text(yaml.safe_dump(data))
    return home.load()


def test_account_resolution_is_explicit_and_secrets_are_redacted(home):
    config = accounts(home)
    assert config.github.for_account().token.get_secret_value() == "fake-github-secret"
    access = config.github.for_account("work")
    assert access.token.get_secret_value() == "work-token"
    assert access.allowed_users == ["work-owner"]
    assert config.github.for_account("personal").allowed_users == ["personal-owner"]
    with pytest.raises(ValueError, match="Unknown GitHub account"):
        config.github.for_account("typo")
    encoded = base64.b64encode(b"x-access-token:work-token").decode()
    assert (
        config.redact(f"work-token personal-token {encoded}") == "[REDACTED] [REDACTED] [REDACTED]"
    )
    assert "work-token" not in config.model_dump_json()
    profiles_only = config.model_copy(deep=True)
    from pydantic import SecretStr

    profiles_only.github.token = SecretStr("")
    profiles_only.ready()


def test_named_account_has_no_implicit_global_comment_permissions():
    config = Config(
        github={"allowed_users": ["global-owner"], "accounts": {"work": {"token": "key"}}}
    )
    assert config.github.for_account("work").allowed_users == []


@pytest.mark.parametrize(
    "target",
    [
        "owner/project",
        "https://github.com/owner/project",
        "https://github.com/owner/project.git/",
        "git@github.com:owner/project.git",
    ],
)
def test_remote_identifiers(target):
    assert github_name(target) == "owner/project"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/owner/project",
        "https://key@github.com/owner/project",
        "owner/..",
        "./missing",
        "https://github.com/o/r?token=secret",
    ],
)
def test_invalid_remote_identifiers(target):
    with pytest.raises(ValueError):
        github_name(target)


def test_remote_registration_defaults_to_main_without_checkout(home, github):
    config = accounts(home)
    github["default_branch"] = "develop"
    github["tokens"]["company/backend"] = "Bearer work-token"
    args = parser().parse_args(
        ["repo", "add", "https://github.com/company/backend.git", "--account", "work"]
    )
    result = register_repository(args, home, config)
    store = Store(home.db)
    try:
        record = store.one("SELECT * FROM repositories")
        assert record["github_account"] == "work"
        assert record["source_kind"] == "github"
        assert record["source"] == "https://github.com/company/backend.git"
        assert record["ref"] == "main"
        assert record["directory"] is None
    finally:
        store.close()
    assert not list((home.root / "repos").iterdir())
    assert "work-token" not in json.dumps(result)
    assert github["requests"] == [("GET", "company/backend", "Bearer work-token")]


def test_manual_run_and_repo_account_change_from_another_directory(home, source, directory, github):
    accounts(home)
    github["tokens"]["company/late"] = "Bearer work-token"
    prefix = [sys.executable, "-m", "maxwell", "--home", str(home.root)]
    registered = subprocess.run(
        [*prefix, "repo", "add", str(source), "--github", "company/late", "--account", "work"],
        capture_output=True,
        text=True,
        cwd=directory,
        timeout=5,
    )
    assert registered.returncode == 0, registered.stderr
    scanned = subprocess.run(
        [*prefix, "scan", "company/late", "--model", "fake-model", "--json"],
        capture_output=True,
        text=True,
        cwd=directory,
        timeout=10,
    )
    assert scanned.returncode == 0, scanned.stderr
    assert json.loads(scanned.stdout)["scans"][0]["state"] == "completed"
    updated = subprocess.run(
        [*prefix, "repo", "change", "company/late", "--account", "personal"],
        capture_output=True,
        text=True,
        cwd=directory,
        timeout=5,
    )
    assert updated.returncode == 0, updated.stderr
    store = Store(home.db)
    try:
        assert store.one("SELECT github_account FROM repositories")["github_account"] == "personal"
        assert store.one("SELECT state FROM findings")["state"] == "published"
    finally:
        store.close()
    assert not home.socket.exists()


def test_remote_fetch_refreshes_managed_worktree_without_persisting_auth(home, source, monkeypatch):
    record = {
        "name": "company/backend",
        "source_kind": "github",
        "ref": "main",
        "source": "https://github.com/company/backend.git",
        "github_account": "work",
    }
    original = subprocess.run
    calls = []
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: wrong-account")

    def run(command, **kwargs):
        if "fetch" in command and record["source"] in command:
            calls.append((command, dict(kwargs["env"])))
            command = [str(source) if item == record["source"] else item for item in command]
            kwargs["env"] = {
                key: value
                for key, value in kwargs["env"].items()
                if not key.startswith("GIT_CONFIG_")
            }
        return original(command, **kwargs)

    monkeypatch.setattr("maxwell.repositories.subprocess.run", run)
    repo = Repository(home, record)
    sha, worktree = repo.prepare("first", token="work-token")
    assert (worktree / "app.py").exists()
    repo.cleanup(worktree)
    (source / "added.txt").write_text("new commit")
    git("add", "added.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "next",
        cwd=source,
    )
    next_sha, worktree = repo.prepare("second", token="work-token")
    assert next_sha != sha
    assert (worktree / "added.txt").read_text() == "new commit"
    for argv, environment in calls:
        assert all("work-token" not in arg for arg in argv)
        assert "GIT_TRACE_CURL" not in environment
        assert environment["GIT_ASKPASS"] == "/usr/bin/false"
        settings = {
            environment[f"GIT_CONFIG_KEY_{i}"]: environment[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(environment["GIT_CONFIG_COUNT"]))
        }
        assert settings["credential.helper"] == ""
        assert settings["http.followRedirects"] == "false"
        header = settings["http.https://github.com/.extraHeader"]
        encoded = header.split()[-1]
        assert base64.b64decode(encoded) == b"x-access-token:work-token"
        assert encoded not in (repo.mirror / "config").read_text()
    assert "work-token" not in (repo.mirror / "config").read_text()
    repo.cleanup(worktree)


def test_git_errors_redact_auth(home, monkeypatch):
    encoded = base64.b64encode(b"x-access-token:work-token").decode()
    monkeypatch.setattr(
        "maxwell.repositories.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr=f"work-token {encoded}", stdout=""
        ),
    )
    with pytest.raises(ValueError) as error:
        git("fetch", token="work-token")
    assert "work-token" not in str(error.value)
    assert encoded not in str(error.value)


def test_v1_registration_migrates_without_changing_identity(directory):
    path = directory / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE repositories(name TEXT PRIMARY KEY,source TEXT NOT NULL,ref TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0,next_scan REAL NOT NULL DEFAULT 0,last_sha TEXT);
            INSERT INTO repositories(name,source,ref) VALUES('owner/repo','/existing','main');
            PRAGMA user_version=1;
        """)
    store = Store(path)
    try:
        record = store.one("SELECT * FROM repositories")
        assert record["name"] == "owner/repo"
        assert record["source"] == "/existing"
        assert record["source_kind"] == "local"
        assert record["github_account"] == "default"
        assert record["directory"] is None
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        store.close()


def test_synchronization_routes_api_calls_and_comment_authority_per_repository(
    home, source, github
):
    config = accounts(home)
    github["tokens"].update(
        {"personal/project": "Bearer personal-token", "company/project": "Bearer work-token"}
    )
    store = Store(home.db)
    for name, account in (("personal/project", "personal"), ("company/project", "work")):
        store.register(name, str(source), "HEAD", github_account=account)
    daemon = Daemon(home, config)
    daemon.store = store

    async def synchronize_once():
        task = asyncio.create_task(daemon.synchronize())
        try:
            async with asyncio.timeout(5):
                while len(store.rows("SELECT issue FROM dashboards WHERE issue IS NOT NULL")) < 2:
                    await asyncio.sleep(0.02)
            company_issue = store.one(
                "SELECT issue FROM dashboards WHERE repository='company/project'"
            )["issue"]
            github["comments"].setdefault(company_issue, []).extend(
                [
                    {"id": 9901, "body": "/maxwell pause", "user": {"login": "personal-owner"}},
                    {"id": 9902, "body": "/maxwell pause", "user": {"login": "work-owner"}},
                ]
            )
            async with asyncio.timeout(5):
                while not store.one("SELECT paused FROM repositories WHERE name='company/project'")[
                    "paused"
                ]:
                    await asyncio.sleep(0.02)
            assert not store.one("SELECT paused FROM repositories WHERE name='personal/project'")[
                "paused"
            ]
            assert not store.one("SELECT id FROM comments WHERE id=9901")
            assert store.one("SELECT id FROM comments WHERE id=9902")
        finally:
            daemon.stopping.set()
            await task

    try:
        asyncio.run(synchronize_once())
        for _, repo, token in github["requests"]:
            if repo:
                assert token == github["tokens"][repo]
            else:
                assert token in github["tokens"].values()
    finally:
        store.close()
