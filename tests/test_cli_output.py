import json
import subprocess
import sys

import pytest

from maxwell.cli import execute, parser
from maxwell.cli_output import check_line
from maxwell.config_check import check_config
from maxwell.github import GitHub
from maxwell.store import Store


@pytest.mark.parametrize(
    "arguments",
    [
        ["--json", "config", "check", "--offline"],
        ["config", "--json", "check", "--offline"],
        ["config", "check", "--offline", "--json"],
        ["--json", "repo", "list"],
        ["repo", "--json", "list"],
        ["repo", "list", "--json"],
        ["scan", "--model", "model-a", "--json"],
    ],
)
def test_json_flag_works_at_each_command_level(arguments):
    assert parser().parse_args(arguments).json


def test_default_config_output_is_readable_with_summary(home, capsys):
    execute(parser().parse_args(["--home", str(home.root), "config", "check", "--offline"]))
    result = capsys.readouterr().out
    assert result.startswith("Checking config (offline)...\n")
    assert "[OK] config" in result
    assert "[SKIP] default / fake-model: offline" in result
    assert "Valid:" in result
    assert "YAML, settings" not in result
    assert "fake-github-secret" not in result and "fake-provider-secret" not in result


def test_streamed_checks_are_redacted_before_callback(home, monkeypatch):
    monkeypatch.setattr(GitHub, "request", lambda *args, **kwargs: {"login": "fake-github-secret"})
    streamed = []
    report = check_config(home, on_check=streamed.append)
    assert report["valid"], report
    assert streamed == report["checks"]
    assert "fake-github-secret" not in json.dumps(streamed)
    assert any("Authenticated as [REDACTED]" in check["message"] for check in streamed)


def test_human_config_failure_keeps_nonzero_exit(directory):
    result = subprocess.run(
        [sys.executable, "-m", "maxwell", "--home", str(directory / "missing"), "config", "check"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "[FAIL] config:" in result.stdout
    assert "Invalid:" in result.stdout
    assert "maxwell init" in result.stdout
    assert result.stderr == ""


def test_repository_and_status_output_preserve_useful_settings(home, source, capsys):
    store = Store(home.db)
    store.register(
        "acme/repo", str(source), "main", route={"provider": "default", "model": "fake-model"}
    )
    store.pause("acme/repo", True)
    store.close()
    prefix = ["--home", str(home.root)]
    execute(parser().parse_args([*prefix, "repo", "list"]))
    text = capsys.readouterr().out
    for expected in (
        "acme/repo",
        "Branch: main",
        "Scans: paused",
        "Discovery: default / fake-model",
    ):
        assert expected in text
    execute(parser().parse_args([*prefix, "status"]))
    text = capsys.readouterr().out
    assert "Daemon stopped" in text and "Committed scan budget: $0.00" in text
    assert "Discovery: default / fake-model" in text
    execute(parser().parse_args([*prefix, "status", "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert report["daemon"] == "stopped"
    assert report["repositories"][0]["stages"]["discovery"]["model"] == "fake-model"


def test_log_output_is_text_unless_json_requested(home, capsys):
    home.log.write_text("First log line\nSecond log line\n")
    prefix = ["--home", str(home.root), "logs"]
    execute(parser().parse_args(prefix))
    assert capsys.readouterr().out == "First log line\nSecond log line\n"
    execute(parser().parse_args([*prefix, "--json"]))
    assert json.loads(capsys.readouterr().out) == {"logs": ["First log line", "Second log line"]}


@pytest.mark.parametrize("status,code", [("ok", "32"), ("error", "31"), ("skipped", "33")])
def test_checks_use_terminal_colors(status, code, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    check_line({"name": "configuration", "status": status, "message": "detail"})
    assert f"\033[{code}m" in capsys.readouterr().out


@pytest.mark.parametrize(
    "terminal,no_color,term",
    [(False, False, "xterm"), (True, True, "xterm"), (True, False, "dumb")],
)
def test_checks_respect_plain_output(terminal, no_color, term, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    check_line(
        {
            "name": "provider:claude:default",
            "status": "error",
            "message": "Access denied\nTry again",
        }
    )
    text = capsys.readouterr().out
    assert "\033[" not in text
    assert "Access denied" in text and "Try again" in text


def test_json_stays_uncolored_in_terminal(home, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    execute(
        parser().parse_args(["--home", str(home.root), "config", "check", "--offline", "--json"])
    )
    text = capsys.readouterr().out
    assert "\033[" not in text
    assert json.loads(text)["valid"]
