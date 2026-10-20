import asyncio
import json
import sys

import pytest

from maxwell.config import Config, Paths
from maxwell.contracts import estimate_cost
from maxwell.provider import create_provider

PRICES = {"input_usd_per_mtok": 2, "cached_input_usd_per_mtok": 0.2, "output_usd_per_mtok": 8}
COUNTERS = {
    "codex": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 100},
    "claude": {
        "input_tokens": 500,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 0,
        "output_tokens": 100,
    },
}
FAKE_CLI = """import json, sys
from pathlib import Path
args = sys.argv[1:]
usage = json.loads(Path(__file__).with_name("usage.json").read_text())
sys.stdin.read()
result = {"summary": "No findings.", "findings": []}
if args[0] == "exec":
    Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(result))
    print(json.dumps({"type": "turn.completed", "usage": usage}), flush=True)
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": result, "usage": usage}), flush=True)
"""


def configure(tmp_path, adapter, usage):
    executable = tmp_path / "fake-cli"
    executable.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
    executable.chmod(0o700)
    (tmp_path / "usage.json").write_text(json.dumps(usage))
    provider = {"executable": str(executable), "model": "fake-model", **PRICES}
    if adapter == "claude":
        home = tmp_path / "claude-home"
        home.mkdir()
        (home / "settings.json").write_text("{}")
        provider.update(adapter="claude", home=str(home))
    else:
        provider["api_key"] = "fake-key"
    return Config.model_validate({"provider": provider, "limits": {"scan_timeout_seconds": 5}})


def scan_cost(tmp_path, adapter, usage):
    config = configure(tmp_path, adapter, usage)
    provider = create_provider(Paths(tmp_path / "home"), config)
    workspace, artifact = tmp_path / "workspace", tmp_path / "artifact"
    workspace.mkdir()
    artifact.mkdir()
    result = asyncio.run(provider.scan(workspace, artifact, "Inspect.", lambda pid: None))
    assert result.findings == []
    return estimate_cost(provider.usage, config.provider)


@pytest.mark.parametrize("adapter", ["codex", "claude"])
@pytest.mark.parametrize("change", ["empty", "missing", "string", "negative", "boolean"])
def test_incomplete_usage_counters_leave_cost_unknown(tmp_path, adapter, change):
    usage = dict(COUNTERS[adapter])
    if change == "empty":
        usage = {}
    elif change == "missing":
        del usage["output_tokens"]
    elif change == "string":
        usage["output_tokens"] = "100"
    elif change == "negative":
        usage["output_tokens"] = -5
    elif change == "boolean":
        usage["output_tokens"] = True
    assert scan_cost(tmp_path, adapter, usage) is None


@pytest.mark.parametrize("adapter", ["codex", "claude"])
@pytest.mark.parametrize("zero", [False, True])
def test_complete_usage_counters_settle_known_cost(tmp_path, adapter, zero):
    usage = dict.fromkeys(COUNTERS[adapter], 0) if zero else COUNTERS[adapter]
    assert scan_cost(tmp_path, adapter, usage) == (0 if zero else 1900)
