import asyncio
import json

import pytest

from maxwell.provider import ClaudeProvider


def test_missing_structured_output_has_actionable_diagnostic(home, directory):
    provider = ClaudeProvider(home, home.load())
    provider.event(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "I have completed my independent audit and provided the structured assessment above.",
        }
    )
    with pytest.raises(ValueError, match="Claude Code.*structured output.*artifacts"):
        provider.write_result(directory / "result.json")


@pytest.fixture
def formatting_cli(home, directory):
    import sys

    import yaml

    executable = directory / "formatting-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
from pathlib import Path
args=sys.argv
schema=json.loads(args[args.index('--json-schema')+1])
settings=Path(args[args.index('--settings')+1])
retry=settings.parent.name=='format-retry'
prompt=sys.stdin.read()
(settings.parent/'observed.json').write_text(json.dumps({'args':args,'prompt':prompt}))
assessment={'summary':'Audited empty input handling.', 'verdict':'supported',
            'actionability':'actionable','priority':'P1',
            'evidence':'app.py:2 divides by a zero denominator for empty input.'}
mode=Path(__file__).parent/'format-mode'
mode=mode.read_text() if mode.exists() else 'recover'
if mode=='exit':sys.exit(1)
print(json.dumps({'type':'assistant','message':{'content':[
    {'type':'text','text':'<StructuredOutput>'+json.dumps(assessment)+'</StructuredOutput>'}]}}))
result={'type':'result','subtype':'success','is_error':False,
        'result':'I provided the assessment above.',
        'usage':{'input_tokens':100,'cache_read_input_tokens':0,
                 'cache_creation_input_tokens':0,'output_tokens':10}}
if retry and mode!='fail':result['structured_output']=assessment
print(json.dumps(result))
"""
    )
    executable.chmod(0o700)
    data = yaml.safe_load(home.config.read_text())
    native = directory / "claude-home"
    native.mkdir()
    (native / "settings.json").write_text("{}")
    data["provider"].pop("api_key")
    data["provider"]["home"] = str(native)
    data["provider"]["adapter"] = "claude"
    data["provider"]["executable"] = str(executable)
    home.config.write_text(yaml.safe_dump(data))
    artifact = directory / "assessment"
    artifact.mkdir()
    return ClaudeProvider(home, home.load()), artifact


def test_missing_output_is_corrected_once_without_source_tools(formatting_cli, source):
    provider, artifact = formatting_cli
    usage_events = []
    provider.on_usage = usage_events.append
    result = asyncio.run(provider.assess(source, artifact, "Audit the finding.", lambda pid: None))
    assert result.verdict == "supported"
    assert provider.usage.input_tokens == 200 and provider.usage.output_tokens == 20
    assert provider.usage.reported and provider.usage.complete
    assert provider.usage.spending is not None and provider.usage.spending.complete
    assert provider.usage.spending.micros == 560
    costs = [usage.spending.micros if usage.spending else 0 for usage in usage_events]
    assert costs == sorted(costs) and costs[-1] == 560
    assert usage_events[-1].input_tokens == 200 and usage_events[-1].complete
    original = json.loads((artifact / "observed.json").read_text())
    retry = json.loads((artifact / "format-retry/observed.json").read_text())
    assert original["args"][original["args"].index("--tools") + 1] == "Read,Grep,Glob"
    assert retry["args"][retry["args"].index("--tools") + 1] == ""
    assert retry["args"][retry["args"].index("--allowedTools") + 1] == "StructuredOutput"
    assert "app.py:2" in retry["prompt"] and "Do not add" in retry["prompt"]
    assert json.loads((artifact / "result.json").read_text())["verdict"] == "supported"


def test_repeated_missing_output_stops_with_artifacts(formatting_cli, source, directory):
    provider, artifact = formatting_cli
    (directory / "format-mode").write_text("fail")
    with pytest.raises(ValueError, match="formatting retry.*artifacts"):
        asyncio.run(provider.assess(source, artifact, "Audit the finding.", lambda pid: None))
    assert provider.usage.input_tokens == 200
    assert provider.usage.spending.micros == 560
    assert (artifact / "events.jsonl").exists() and (
        artifact / "format-retry/events.jsonl"
    ).exists()
    assert not (artifact / "format-retry/format-retry").exists()


def test_failed_process_does_not_trigger_formatting_retry(formatting_cli, source, directory):
    provider, artifact = formatting_cli
    (directory / "format-mode").write_text("exit")
    with pytest.raises(ValueError, match="status 1"):
        asyncio.run(provider.assess(source, artifact, "Audit the finding.", lambda pid: None))
    assert not (artifact / "format-retry").exists()


def test_probes_do_not_retry_missing_structured_output(formatting_cli, source):
    provider, artifact = formatting_cli
    with pytest.raises(ValueError, match="structured output"):
        asyncio.run(provider.probe(source, artifact))
    assert not (artifact / "format-retry").exists()


def test_format_retry_does_not_inherit_repair_edit_permissions(formatting_cli, source):
    provider, artifact = formatting_cli
    from maxwell.contracts import AssessmentResult

    asyncio.run(
        provider.run(
            source,
            artifact,
            "Assess existing edits.",
            lambda pid: None,
            result_type=AssessmentResult,
            writable=True,
        )
    )
    first = json.loads((artifact / "observed.json").read_text())["args"]
    retry = json.loads((artifact / "format-retry/observed.json").read_text())["args"]
    assert "Edit" in first[first.index("--tools") + 1].split(",")
    assert retry[retry.index("--tools") + 1] == ""
    assert "Edit" in retry[retry.index("--disallowedTools") + 1].split(",")


def test_retry_orphan_is_stopped(directory, monkeypatch):
    from types import SimpleNamespace

    from maxwell.repositories import stop_orphan

    calls = []
    monkeypatch.setattr(
        "maxwell.repositories.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            stdout=f"claude --settings {directory}/format-retry/settings.json"
        ),
    )
    monkeypatch.setattr("maxwell.repositories.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("maxwell.repositories.os.killpg", lambda *args: calls.append(args))
    stop_orphan(12345, str(directory))
    assert len(calls) == 1 and calls[0][0] == 12345
