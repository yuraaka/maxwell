import json
import sqlite3
import subprocess
import sys

import httpx
import pytest
import yaml

from maxwell.cli import execute, parser
from maxwell.config import Limits, Paths
from maxwell.config_validation import validate_config
from maxwell.github import GitHub
from maxwell.store import Store


def update_config(home, update):
    data = yaml.safe_load(home.config.read_text())
    update(data)
    home.config.write_text(yaml.safe_dump(data))


@pytest.fixture
def github_requests(monkeypatch):
    requests = []
    responses = {}

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        response = responses.get((request.url.path, request.headers["Authorization"]))
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "yuraaka"})
        return httpx.Response(200, json={"full_name": "yuraaka/maxwell"})

    client = httpx.Client
    monkeypatch.setattr(
        "maxwell.github.httpx.Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return requests, responses


def command(home, capsys, *options):
    if "--offline" in options:
        report = validate_config(home, online=False)
        return (0 if report["valid"] else 1), report
    try:
        execute(
            parser().parse_args(["--home", str(home.root), "config", "apply", "--json", *options])
        )
        code = 0
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_default_check_authenticates_profiles_and_registered_repositories(
    home, github_requests, capsys
):
    update_config(
        home,
        lambda data: data["github"].update(
            accounts={"work": {"token": "work-token", "allowed_users": []}}
        ),
    )
    store = Store(home.db)
    store.register("yuraaka/maxwell", "/unused", "main", github_account="work")
    store.close()
    config_before = home.config.read_bytes()
    code, report = command(home, capsys)
    assert code == 0
    assert report["valid"] and report["online"]
    assert [
        (request.url.path, request.headers["Authorization"]) for request in github_requests[0]
    ] == [
        ("/user", "Bearer fake-github-secret"),
        ("/user", "Bearer work-token"),
        ("/repos/yuraaka/maxwell", "Bearer work-token"),
    ]
    assert any("Authenticated as yuraaka" in check["message"] for check in report["checks"])
    assert home.config.read_bytes() == config_before
    assert not home.socket.exists()
    store = Store(home.db)
    try:
        assert store.rows("SELECT * FROM scans") == []
    finally:
        store.close()
    assert "fake-github-secret" not in json.dumps(report)
    assert "work-token" not in json.dumps(report)


def test_offline_check_does_not_access_network_or_create_database(home, github_requests, capsys):
    assert not home.db.exists()
    code, report = command(home, capsys, "--offline")
    assert code == 0 and report["valid"] and not report["online"]
    assert not home.db.exists()
    assert github_requests[0] == []


def test_publication_limit_above_twenty_passes_config_validation(home):
    update_config(home, lambda data: data["limits"].update(max_findings_per_scan=200))
    report = validate_config(home, online=False)
    assert report["valid"], report
    assert home.load().limits.max_findings_per_scan == 200


@pytest.mark.parametrize("limit", [0, -1, 1.5])
def test_invalid_publication_limits_are_reported(home, limit):
    update_config(home, lambda data: data["limits"].update(max_findings_per_scan=limit))
    report = validate_config(home, online=False)
    assert not report["valid"]
    assert "limits.max_findings_per_scan" in json.dumps(report)


@pytest.mark.parametrize("setting", ["max_reviews_per_scan", "max_audits_per_scan"])
def test_stage_limit_defaults_and_config_validation(home, setting):
    assert getattr(Limits(), setting) == 1
    update_config(home, lambda data: data["limits"].update({setting: 50}))
    assert validate_config(home, online=False)["valid"]
    assert getattr(home.load().limits, setting) == 50


@pytest.mark.parametrize("setting", ["max_reviews_per_scan", "max_audits_per_scan"])
@pytest.mark.parametrize("limit", [0, -1, 1.5])
def test_invalid_stage_limits_are_reported(home, setting, limit):
    update_config(home, lambda data: data["limits"].update({setting: limit}))
    report = validate_config(home, online=False)
    assert not report["valid"]
    assert f"limits.{setting}" in json.dumps(report)


def test_missing_config_has_nonzero_exit_and_does_not_initialize(directory):
    home = directory / "missing"
    result = subprocess.run(
        [sys.executable, "-m", "maxwell", "--home", str(home), "config", "apply", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert not json.loads(result.stdout)["valid"]
    assert "maxwell config init" in result.stdout
    assert not home.exists()


def test_missing_settings_are_reported_together(home, github_requests, capsys, monkeypatch):
    home.config.write_text("{}")
    monkeypatch.setattr("maxwell.config.shutil.which", lambda executable: None)
    code, report = command(home, capsys)
    assert code == 1
    message = json.dumps(report)
    for field in (
        "git:",
        "provider.executable",
        "github",
        "provider.api_key",
        "provider.model",
        "provider.input_usd_per_mtok",
        "provider.output_usd_per_mtok",
    ):
        assert field in message
    assert github_requests[0] == []


@pytest.mark.parametrize(
    "body,expected",
    [
        ("github: [secret-value", "malformed YAML at line"),
        ("secret-value: secret-value", "Extra inputs are not permitted"),
        ("limits: {daily_budget_usd: secret-value}", "limits.daily_budget_usd"),
        (
            "github: {accounts: {secret-value: {token: [secret-value]}}}",
            "github.accounts.<key>.token",
        ),
        ("provider: {api_key: [secret-value]}", "provider.api_key"),
        (
            "repair: {auto_merge: {required_validations: 0}}",
            "repair.auto_merge.required_validations: Input should be greater than or equal to 1",
        ),
        (
            "repair: {auto_merge: {merge_method: secret-value}}",
            "repair.auto_merge.merge_method",
        ),
        (
            "repair: {auto_merge: {secret-value: secret-value}}",
            "repair.auto_merge.<key>",
        ),
        ("github: {api_url: 'https://secret-value@github.com'}", "github.api_url"),
        ("false", "Invalid config.yaml"),
        ("[]", "Invalid config.yaml"),
    ],
)
def test_invalid_config_errors_are_actionable_without_echoing_secrets(
    home, github_requests, capsys, body, expected
):
    home.config.write_text(body)
    code, report = command(home, capsys)
    assert code == 1
    message = json.dumps(report)
    assert expected in message
    assert "secret-value" not in message
    assert github_requests[0] == []


def test_insecure_permissions_fail_without_network(home, github_requests, capsys):
    home.config.chmod(0o644)
    code, report = command(home, capsys)
    assert code == 1
    assert "chmod 600" in json.dumps(report)
    assert github_requests[0] == []
    assert home.config.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(401, json={"message": "fake-github-secret"}), "invalid or expired"),
        (httpx.Response(403), "denied access"),
        (httpx.Response(500), "HTTP 500"),
        (httpx.ConnectError("fake-github-secret"), "connection"),
        (httpx.Response(200, text="fake-github-secret"), "connection"),
        (httpx.Response(200, json={}), "unexpected response"),
    ],
)
def test_online_failure_is_reported_without_credentials(
    home, github_requests, capsys, response, expected
):
    github_requests[1][("/user", "Bearer fake-github-secret")] = response
    code, report = command(home, capsys)
    assert code == 1
    assert expected in json.dumps(report)
    assert "fake-github-secret" not in json.dumps(report)


def test_repository_access_failure_is_reported(home, github_requests, capsys):
    store = Store(home.db)
    store.register("yuraaka/maxwell", "/unused", "main")
    store.close()
    github_requests[1][("/repos/yuraaka/maxwell", "Bearer fake-github-secret")] = httpx.Response(
        404
    )
    code, report = command(home, capsys)
    assert code == 1
    assert any(
        check["name"] == "repository:yuraaka/maxwell" and check["status"] == "error"
        for check in report["checks"]
    )


def test_named_accounts_do_not_require_default_token(home, github_requests, capsys):
    update_config(
        home,
        lambda data: data["github"].update(token="", accounts={"work": {"token": "work-token"}}),
    )
    code, report = command(home, capsys)
    assert code == 0 and report["valid"]
    assert [request.headers["Authorization"] for request in github_requests[0]] == [
        "Bearer work-token"
    ]


@pytest.mark.parametrize("account", ["default", "unknown", "empty"])
def test_registered_account_errors_are_detected_offline(home, capsys, account):
    update_config(
        home,
        lambda data: data["github"].update(
            token="", accounts={"work": {"token": "work-token"}, "empty": {"token": ""}}
        ),
    )
    store = Store(home.db)
    store.register("yuraaka/maxwell", "/unused", "main", github_account=account)
    store.close()
    code, report = command(home, capsys, "--offline")
    assert code == 1
    assert any(
        check["name"] == "repository:yuraaka/maxwell" and check["status"] == "error"
        for check in report["checks"]
    )


def test_registry_is_read_without_migration(home, capsys):
    with sqlite3.connect(home.db) as db:
        db.executescript("""
            CREATE TABLE repositories(name TEXT PRIMARY KEY,source TEXT,ref TEXT);
            INSERT INTO repositories VALUES('yuraaka/maxwell','/unused','main');
            PRAGMA user_version=1;
        """)
    before = home.db.read_bytes()
    code, report = command(home, capsys, "--offline")
    assert code == 0 and report["valid"]
    assert home.db.read_bytes() == before


def test_missing_database_with_valid_config_does_not_create_runtime_directories(home, capsys):
    paths = Paths(home.root.parent / "config-only")
    paths.root.mkdir(mode=0o700)
    paths.config.write_bytes(home.config.read_bytes())
    paths.config.chmod(0o600)
    code, report = command(paths, capsys, "--offline")
    assert code == 0 and report["valid"]
    assert list(paths.root.iterdir()) == [paths.config]


def test_secret_redaction_cannot_corrupt_report_types(home, github_requests, capsys):
    update_config(home, lambda data: data["github"].update(token="true"))
    github_requests[1][("/user", "Bearer true")] = httpx.Response(200, json={"login": "true"})
    code, report = command(home, capsys)
    assert code == 0 and report["valid"] is True
    assert any("[REDACTED]" in check["message"] for check in report["checks"])


def test_apply_waits_for_github_quota_and_reports_status_without_failing(home, monkeypatch):
    send = httpx.Client.send
    waits, checks = [], []
    rejected = False

    def limited(client, request, **kwargs):
        nonlocal rejected
        if request.url.path == "/user" and not rejected:
            rejected = True
            return httpx.Response(429, headers={"retry-after": "60"}, request=request)
        return send(client, request, **kwargs)

    def wait(client, response, delay):
        waits.append(delay)
        client.on_status("waiting for GitHub quota; job retained")

    monkeypatch.setattr(httpx.Client, "send", limited)
    monkeypatch.setattr(GitHub, "wait_for_quota", wait)
    report = validate_config(home, on_check=checks.append)
    assert report["valid"]
    assert waits == [60]
    assert any(check["status"] == "waiting" for check in checks)
    assert not any(check["status"] == "waiting" for check in report["checks"])


def test_pipeline_pool_validation_names_the_exact_yaml_key(home):
    update_config(home, lambda data: data.update(pipeline={"pools": {"validate": -1}}))
    with pytest.raises(
        ValueError, match=r"pipeline.pools.validate: Input should be greater than or equal to 0"
    ):
        home.load(detailed=True)
