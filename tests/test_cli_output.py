import io
import json
import re
import subprocess
import sys

import pytest

from maxwell.cli import execute, parser
from maxwell.cli_output import check_line, human_lines, model_lines
from maxwell.config_validation import validate_config
from maxwell.github import GitHub
from maxwell.scan_display import ScanDisplay
from maxwell.store import Store


def test_quota_wait_output_identifies_account_and_preserves_diagnostics(capsys):
    check_line(
        {
            "name": "github:default",
            "status": "waiting",
            "message": "remaining 0; retry at 2026-10-12 15:00:00 UTC; job retained",
        }
    )
    output = capsys.readouterr().out
    assert "[WAIT] GitHub / default" in output
    assert "remaining 0" in output and "15:00:00 UTC" in output and "job retained" in output


def test_redirected_scan_output_does_not_repeat_phases_or_heartbeats():
    stream = io.StringIO()
    display = ScanDisplay(stream)
    display({"repository": "acme/repo", "state": "plan", "stages": ["validate"]})
    display({"state": "stage", "stage": "validate", "status": "running"})
    display({"state": "batch", "total": 9, "completed": 0})
    for index in range(9):
        for heartbeat in (False, True):
            display(
                {
                    "state": "progress",
                    "phase": "agent running",
                    "elapsed": 75,
                    "heartbeat": heartbeat,
                }
            )
        display({"state": "batch", "total": 9, "completed": index + 1})
    display(
        {"state": "stage", "stage": "validate", "status": "completed", "total": 9, "completed": 9}
    )
    display.finish()
    text = stream.getvalue()
    assert len(text.splitlines()) == 2
    assert "validate>" in text and "validate+ 9/9" in text
    assert "agent running" not in text and "\033" not in text


def test_interactive_scan_rewrites_colored_panel_and_preserves_stage_failure(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLUMNS", "90")
    stream = Terminal()
    display = ScanDisplay(stream)
    display(
        {
            "repository": "acme/repo",
            "state": "plan",
            "stages": ["measure", "hunt", "validate", "audit"],
        }
    )
    for stage in ("measure", "hunt", "validate", "audit"):
        display({"state": "stage", "stage": stage, "status": "running"})
        display({"state": "batch", "total": 3, "completed": 1})
        display({"state": "running", "model": "model-a", "effort": "high"})
        display({"state": "progress", "phase": "agent running", "elapsed": 75, "heartbeat": True})
        display(
            {
                "state": "stage",
                "stage": stage,
                "status": "failed" if stage == "hunt" else "completed",
                "total": 3,
                "completed": 3,
            }
        )
    display.finish()
    text = stream.getvalue()
    frames = re.split(r"\033\[\d+A\r\033\[0J", text)
    assert len(frames) > 1
    for frame in frames:
        plain = re.sub(r"\033\[[0-9;]*m", "", frame)
        lines = plain.splitlines()
        assert len(lines) == 6
        assert all(len(line) == 89 for line in lines)
        assert lines[0].startswith("╭ Maxwell · acme/repo")
        assert lines[-1].startswith("╰")
    assert "\033[1;35mvalidate 1/3" in text
    assert "\033[1;31m✗ hunt" in text
    assert "\033[32m✓ audit" in text
    assert "━━━━━━━━" in text and "100%" in text
    assert "validate>" not in text and "hunt!" not in text


@pytest.mark.parametrize("terminal", [False, True])
def test_scan_quota_diagnostics_are_deduplicated(terminal, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    stream = io.StringIO()
    stream.isatty = lambda: terminal
    display = ScanDisplay(stream)
    display({"repository": "acme/repo", "state": "plan", "stages": ["hunt"]})
    for _ in range(20):
        display(
            {
                "state": "progress",
                "phase": "waiting for GitHub quota; remaining 0; retry at 15:00 UTC; job retained",
            }
        )
    text = stream.getvalue()
    milestones = [line for line in text.splitlines() if "acme/repo: waiting" in line]
    assert len(milestones) == 1
    assert "remaining 0; retry at 15:00 UTC; job retained" in milestones[0]


@pytest.mark.parametrize("terminal", [False, True])
def test_test_names_containing_retry_stay_inside_progress_panel(terminal, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    stream = io.StringIO()
    stream.isatty = lambda: terminal
    display = ScanDisplay(stream)
    display({"repository": "acme/repo", "state": "plan", "stages": ["review"]})
    display({"state": "stage", "stage": "review", "status": "running"})
    for test in ("test_formatting_retry", "test_quota_wait_obeys_retry_after"):
        display(
            {
                "state": "progress",
                "phase": f"test run 1/2 · 2/10 tests · tests/test_provider.py::{test}",
            }
        )
    assert "acme/repo: test run" not in stream.getvalue()


def test_scan_summary_groups_failures_and_links_main_status():
    scan = {
        "repository": "acme/repo",
        "stage": "validate",
        "state": "completed",
        "admitted": 0,
        "publication_errors": [],
        "report": "/private/report.md",
        "findings": [],
        "assessments": [{"verdict": "supported", "issue": 2}],
        "error": None,
    }
    report = {
        "scans": [scan] * 9 + [{**scan, "state": "failed", "error": "Agent timed out"}] * 3,
        "statuses": [{"repository": "acme/repo", "url": "https://github.com/acme/repo/issues/4"}],
    }
    text = "\n".join(human_lines(report, "scan"))
    assert "validate: 9 supported" in text
    assert text.count("Agent timed out") == 1 and "(3 executions)" in text
    assert text.count("Status:") == 1 and text.endswith("https://github.com/acme/repo/issues/4")
    assert "Report:" not in text and "Assessment:" not in text
    assert "private" not in text


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
    store.register("acme/repo", str(source), "main")
    store.pause("acme/repo", True)
    store.close()
    prefix = ["--home", str(home.root)]
    execute(parser().parse_args([*prefix, "repo", "list"]))
    text = capsys.readouterr().out
    for expected in (
        "acme/repo",
        "Branch: main",
        "GitHub account: default",
    ):
        assert expected in text
    assert "Scans: paused" not in text
    assert "Discovery:" not in text
    execute(parser().parse_args([*prefix, "repo", "list", "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert "stages" not in report[0]


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


@pytest.mark.parametrize("columns", [40, 80, 120])
@pytest.mark.parametrize("no_color", [False, True])
def test_scan_panel_fits_terminal_and_respects_no_color(columns, no_color, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    stream = io.StringIO()
    stream.isatty = lambda: True
    display = ScanDisplay(stream)
    display(
        {
            "repository": "yuraaka/maxwell",
            "state": "plan",
            "stages": ["measure", "hunt", "validate", "audit"],
        }
    )
    display({"state": "stage", "stage": "measure", "status": "completed"})
    display({"state": "stage", "stage": "hunt", "status": "completed"})
    display({"state": "stage", "stage": "validate", "status": "running"})
    display({"state": "batch", "total": 9, "completed": 4})
    display({"state": "running", "model": "gpt-6.1-sol", "effort": "high"})
    display({"state": "progress", "phase": "agent running"})
    frame = display.panel("2:21")
    plain = [re.sub(r"\033\[[0-9;]*m", "", line) for line in frame]
    assert all(len(line) == min(100, columns - 1) for line in plain)
    assert "4/9" in "".join(plain[1:-3]) and "61%" in plain[-3]
    assert "2:21" in plain[-2]
    assert any(re.search(r"\033\[[0-9;]*m", line) for line in frame) != no_color


def test_dumb_terminal_scan_uses_plain_output(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    stream = io.StringIO()
    stream.isatty = lambda: True
    display = ScanDisplay(stream)
    display({"repository": "acme/repo", "state": "plan", "stages": ["hunt"]})
    display({"state": "stage", "stage": "hunt", "status": "running"})
    display.finish()
    assert "\033" not in stream.getvalue()
    assert "╭" not in stream.getvalue()


def test_scan_animation_changes_without_inventing_completed_work(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    now = [100.0]
    monkeypatch.setattr("maxwell.scan_display.time.monotonic", lambda: now[0])
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLUMNS", "90")
    stream = io.StringIO()
    stream.isatty = lambda: True
    display = ScanDisplay(stream)
    display(
        {
            "repository": "acme/repo",
            "state": "plan",
            "stages": ["measure", "hunt", "validate", "audit"],
        }
    )
    display({"state": "stage", "stage": "measure", "status": "running"})
    display({"state": "progress", "phase": "test run 1/2"})
    first = display.panel("0:00")
    now[0] += 0.25
    second = display.panel("0:00")
    assert first[-3] != second[-3] and first[-2] != second[-2]
    assert display.progress() == 0
    assert display.refresh_interval < 0.2
    display(
        {
            "state": "progress",
            "phase": "test run 1/2 · 2/4 tests · 0 failed",
            "stage_fraction": 0.375,
        }
    )
    assert display.progress() == 0.09375
    assert "2/4 tests" in "".join(display.panel("0:01"))


def test_continuous_panel_shows_pool_capacity_and_aggregates_spending(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLUMNS", "100")
    stream = io.StringIO()
    stream.isatty = lambda: True
    display = ScanDisplay(stream)
    stages = ["hunt", "validate", "audit", "fix", "review", "integrate"]
    display({"repository": "acme/repo", "state": "plan", "stages": stages, "pipeline": "infinite"})
    display(
        {
            "repository": "acme/repo",
            "state": "pools",
            "pools": dict.fromkeys(stages, 1),
            "capacities": dict.fromkeys(stages, 3),
            "backlog": {"validate": 23},
            "status_url": "https://github.com/acme/repo/issues/4",
        }
    )
    for index in range(100):
        display({"state": "running", "id": str(index), "model": "model"})
        display(
            {
                "state": "completed",
                "id": str(index),
                "executed": 1,
                "usage": {"spending": {"micros": 1000, "complete": True}},
            }
        )
    display({"state": "progress", "phase": "Waiting: integration pool full; Ctrl+C to stop"})
    panel = "\n".join(display.panel("2:00"))
    assert "continuous" in panel and "integrate 1/3" in panel
    assert "100 executions" in panel and "$0.1000 USD" in panel
    assert "100%" not in panel and "integration pool full" in panel
    assert "https://github.com/acme/repo/issues/4" in panel
    assert "Backlog:" in panel and "validate 23" in panel
    assert not display.spending


def test_pipeline_counts_preparation_failures_without_inventing_agent_spending():
    display = ScanDisplay(io.StringIO())
    stages = ["fix", "review"]
    display(
        {"repository": "acme/repo", "state": "plan", "stages": stages, "pipeline": "repair-all"}
    )
    for index in range(4):
        display(
            {
                "state": "completed" if index == 0 else "failed",
                "id": str(index),
                "executed": int(index == 0),
                "usage": {"spending": {"micros": 1000, "complete": True}} if index == 0 else {},
            }
        )
    event = {
        "repository": "acme/repo",
        "state": "pools",
        "pools": {},
        "capacities": {},
        "counts": {"review": 3, "fix": 1},
    }
    display(event)
    display(event)
    panel = "\n".join(display.panel("1:00"))
    assert "4 executions" in panel and "$0.0010 USD" in panel
    assert not display.unknown_spending
    display({**event, "repository": "acme/other", "counts": {"fix": 2}})
    assert display.execution_count == 6


def test_batch_summary_uses_dashboard_without_per_execution_link_wall():
    result = {
        "pipeline": "repair-all",
        "scans": [{"report": "/private/report.md"}],
        "pipeline_repositories": [
            {
                "repository": "acme/repo",
                "executions": {"fix": 8, "review": 16},
                "closed": 8,
                "rejected": 2,
                "remaining": 1,
                "failed": 0,
                "waiting": ["GitHub checks pending"],
            }
        ],
        "statuses": [{"repository": "acme/repo", "url": "https://github.com/acme/repo/issues/4"}],
    }
    text = "\n".join(human_lines(result, "scan"))
    assert "8 closed" in text and "1 remaining" in text
    assert "GitHub checks pending" in text and "issues/4" in text
    assert "/private/report" not in text and "repair PR:" not in text
