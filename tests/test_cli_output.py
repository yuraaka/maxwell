import json
import re
import subprocess
import sys

import pytest

from maxwell.cli import execute, parser
from maxwell.cli_output import check_line, model_lines
from maxwell.config_validation import validate_config
from maxwell.github import GitHub
from maxwell.store import Store


@pytest.mark.parametrize(
    "arguments",
    [
        ["--json", "config", "apply"],
        ["config", "--json", "apply"],
        ["config", "apply", "--json"],
        ["--json", "repo", "list"],
        ["repo", "--json", "list"],
        ["repo", "list", "--json"],
        ["scan", "--model", "model-a", "--json"],
    ],
)
def test_json_flag_works_at_each_command_level(arguments):
    assert parser().parse_args(arguments).json


def test_default_config_output_is_readable_with_summary(home, capsys):
    execute(parser().parse_args(["--home", str(home.root), "config", "apply"]))
    result = capsys.readouterr().out
    assert result.startswith("Applying config...\n")
    assert "[OK] config" in result
    assert "[OK] default / fake-model" in result
    assert "Applied:" in result
    assert "YAML, settings" not in result
    assert "fake-github-secret" not in result and "fake-provider-secret" not in result


def test_streamed_checks_are_redacted_before_callback(home, monkeypatch):
    monkeypatch.setattr(GitHub, "request", lambda *args, **kwargs: {"login": "fake-github-secret"})
    streamed = []
    report = validate_config(home, on_check=streamed.append)
    assert report["valid"], report
    assert streamed == report["checks"]
    assert "fake-github-secret" not in json.dumps(streamed)
    assert any("Authenticated as [REDACTED]" in check["message"] for check in streamed)


def test_human_config_failure_keeps_nonzero_exit(directory):
    result = subprocess.run(
        [sys.executable, "-m", "maxwell", "--home", str(directory / "missing"), "config", "apply"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "[FAIL] config:" in result.stdout
    assert "Apply has errors:" in result.stdout
    assert "maxwell config init" in result.stdout
    assert result.stderr == ""


def test_repository_output_preserves_useful_settings(home, source, capsys):
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
        "Discovery: default / fake-model",
    ):
        assert expected in text
    assert "Scans: paused" not in text
    execute(parser().parse_args([*prefix, "repo", "list", "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert report[0]["stages"]["discovery"]["model"] == "fake-model"


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
    execute(parser().parse_args(["--home", str(home.root), "config", "apply", "--json"]))
    text = capsys.readouterr().out
    assert "\033[" not in text
    assert json.loads(text)["valid"]


@pytest.mark.parametrize("terminal", [False, True])
def test_model_tables_align_with_and_without_colors(terminal, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    catalog = {
        "provider": "codex-work",
        "adapter": "codex",
        "models": [
            {
                "model": name,
                "efforts": efforts,
                "default_effort": "medium" if name == "configured" else None,
                "source": "native",
                "selectable": True,
                "access": access,
            }
            for name, efforts, access in (
                ("short", ["low", "high"], "verified"),
                ("long-model-name", None, "unavailable"),
                ("default", [], "unverified"),
                ("configured", None, "verified"),
            )
        ],
    }
    other = {**catalog, "provider": "claude-personal", "models": catalog["models"][:1]}
    text = "\n".join(model_lines({"providers": [catalog, other], "checked_at": 1_791_000_000}))
    plain = re.sub(r"\033\[[0-9;]*m", "", text)
    rows = [line for line in plain.splitlines() if line.startswith("  │")]
    assert len(rows) == 6
    positions = [[index for index, char in enumerate(row) if char == "│"] for row in rows]
    assert all(row == positions[0] for row in positions)
    assert plain.count("┌") == plain.count("└") == 1
    assert "codex-work" in plain and "claude-personal" in plain
    assert plain.splitlines()[0].startswith("Models — ")
    assert "low, high" in plain and "unknown" in plain
    assert "configured" in plain and "medium" in plain
    for heading in ("Default", "Source", "Selectable", "Access"):
        assert heading not in plain
    if terminal:
        assert "\033[36mshort" in text
    else:
        assert text == plain
