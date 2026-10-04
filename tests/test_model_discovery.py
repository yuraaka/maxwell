import asyncio
import json
import os
import subprocess
import sys

import pytest
import yaml

from maxwell.cli import execute, parser
from maxwell.config import ModelSettings
from maxwell.config_apply import apply_config, config_digest
from maxwell.model_discovery import discover_models
from maxwell.store import Store


@pytest.fixture
def discovery(home, directory):
    executable = directory / "metadata-cli"
    executable.write_text(
        """#!PYTHON
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
codex = args[0] in ('app-server', 'exec')
home = Path(os.environ['CODEX_HOME' if codex else 'CLAUDE_CONFIG_DIR'])
observed = {'args': args, 'home': str(home), 'workspace': str(Path.cwd()),
    'pid': os.getpid(), 'github_env': 'GH_TOKEN' in os.environ,
    'gateway_env': 'COMPANY_GATEWAY_TOKEN' in os.environ}
(home / 'observed.json').write_text(json.dumps(observed))
mode = (home / 'mode').read_text() if (home / 'mode').exists() else 'normal'
if args[0] == 'exec' or (not codex and '--input-format' not in args):
    schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text()) if codex else json.loads(args[args.index('--json-schema') + 1])
    assert 'ok' in schema['properties'], 'Repository scan requested during apply'
    model = args[args.index('--model') + 1]
    effort = args[args.index('--effort') + 1] if '--effort' in args else None
    with (home / 'probes.jsonl').open('a') as stream:
        stream.write(json.dumps({'model':model, 'effort':effort, 'args':args}) + '\\n')
    sys.stdin.read()
    denied = (home / 'denied').read_text().splitlines() if (home / 'denied').exists() else []
    if model in denied:
        print('Access denied native-secret', file=sys.stderr)
        sys.exit(1)
    if codex:
        Path(args[args.index('--output-last-message') + 1]).write_text('{"ok":true}')
    else:
        print(json.dumps({'type':'result','is_error':False,'structured_output':{'ok':True}}),flush=True)
    sys.exit(0)
for line in sys.stdin:
    message = json.loads(line)
    with (home / 'messages.jsonl').open('a') as stream:
        stream.write(json.dumps(message) + '\\n')
    if mode == 'wait':
        time.sleep(60)
    if mode == 'error':
        print('metadata denied ' + os.environ.get('COMPANY_GATEWAY_TOKEN', '') + ' native-secret', file=sys.stderr)
        sys.exit(1)
    if codex:
        method = message['method']
        if method == 'initialized':
            continue
        assert method in ('initialize', 'model/list'), 'Model execution requested'
        if method == 'initialize':
            result = {}
        elif 'cursor' not in message['params']:
            result = {'data': [{'id':'one', 'model':'one', 'displayName':'One native-secret',
                'defaultReasoningEffort':'low', 'supportedReasoningEfforts':[{'reasoningEffort':'low'}, {'reasoningEffort':'high'}]}], 'nextCursor':'next'}
        else:
            result = {'data': [{'id':'hidden', 'model':'hidden', 'hidden':True}], 'nextCursor':None}
        if mode == 'cycle' and method == 'model/list':
            result['nextCursor'] = 'next'
        print(json.dumps({'method':'notification'}), flush=True)
        print(json.dumps({'id':message['id'], 'result':result}), flush=True)
    else:
        assert message['type'] == 'control_request'
        assert message['request']['subtype'] == 'initialize', 'Model execution requested'
        result = {} if mode == 'missing' else {'models':[
            {'value':'sonnet', 'resolvedModel':'claude-sonnet-example', 'displayName':'Sonnet',
             'supportsEffort':True, 'supportedEffortLevels':['low','high']},
            {'value':'legacy', 'displayName':'Legacy'},
            {'value':'no-effort', 'supportsEffort':False}]}
        print(json.dumps({'type':'control_response', 'response':{'subtype':'success',
             'request_id':message['request_id'], 'response':result}}), flush=True)
""".replace("PYTHON", sys.executable)
    )
    executable.chmod(0o700)
    homes = {}
    providers = {}
    for name, adapter in (
        ("codex-work", "codex"),
        ("codex-personal", "codex"),
        ("claude-work", "claude"),
    ):
        path = directory / name
        path.mkdir()
        homes[name] = path
        if adapter == "codex":
            (path / "config.toml").write_text(
                'model="one"\nmodel_provider="gateway"\n[model_providers.gateway]\nname="Gateway"\nbase_url="https://company.invalid/v1"\nexperimental_bearer_token="native-secret"\n'
            )
            (path / "auth.json").write_text('{"tokens":{"access_token":"native-auth-secret"}}')
        else:
            (path / "settings.json").write_text(
                json.dumps(
                    {
                        "env": {"ANTHROPIC_API_KEY": "native-secret"},
                        "enabledPlugins": {"company": True},
                    }
                )
            )
        providers[name] = {"adapter": adapter, "home": str(path), "executable": str(executable)}
    data = yaml.safe_load(home.config.read_text())
    data.pop("provider")
    data["providers"] = providers
    home.config.write_text(yaml.safe_dump(data))
    return homes, home.load()


def test_discovery_keeps_accounts_separate_and_paginates(home, discovery, monkeypatch):
    homes, config = discovery
    monkeypatch.setenv("GH_TOKEN", "do-not-forward")
    completed = []
    report = asyncio.run(discover_models(config, on_provider=completed.append))
    assert completed == report["providers"]
    assert [row["provider"] for row in completed] == list(config.providers)
    for name in ("codex-work", "codex-personal"):
        result = next(row for row in completed if row["provider"] == name)
        assert "error" not in result
        assert [model["model"] for model in result["models"]] == ["hidden", "one"]
        model = result["models"][1]
        assert model["efforts"] == ["low", "high"]
        assert model["default_effort"] == "low"
        assert model["access"] == "unverified"
        assert model["name"] == "One [REDACTED]"
        observed = json.loads((homes[name] / "observed.json").read_text())
        assert observed["home"] == str(homes[name])
        assert not observed["github_env"]
        assert "native-secret" not in json.dumps(observed["args"])
        assert "--profile" not in observed["args"]
        assert not os.path.exists(observed["workspace"])
        requests = list(map(json.loads, (homes[name] / "messages.jsonl").read_text().splitlines()))
        listing = [request for request in requests if request["method"] == "model/list"]
        assert len(listing) == 2
        assert all(request["params"]["includeHidden"] for request in listing)
        assert listing[1]["params"]["cursor"] == "next"
        assert (
            json.loads((homes[name] / "auth.json").read_text())["tokens"]["access_token"]
            == "native-auth-secret"
        )
    assert not home.db.exists()


def test_claude_metadata_preserves_aliases_and_unknown_efforts(discovery):
    homes, config = discovery
    result = asyncio.run(discover_models(config, "claude-work"))["providers"][0]
    models = {row["model"]: row for row in result["models"]}
    assert models["sonnet"]["efforts"] == ["low", "high"]
    assert models["sonnet"]["resolved_model"] == "claude-sonnet-example"
    assert models["legacy"]["efforts"] is None
    assert models["no-effort"]["efforts"] == []
    observed = json.loads((homes["claude-work"] / "observed.json").read_text())
    args = observed["args"]
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in args
    assert "--json-schema" not in args and "--model" not in args
    assert not (homes["codex-work"] / "observed.json").exists()


@pytest.mark.parametrize("name", ["codex-work", "claude-work"])
def test_native_gateway_default_supplements_generic_cli_catalog(discovery, name):
    homes, config = discovery
    if name == "codex-work":
        (homes[name] / "config.toml").write_text('model="company-default"\n')
    else:
        (homes[name] / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_MODEL": "company-default"}})
        )
    result = asyncio.run(discover_models(config, name))["providers"][0]
    row = next(model for model in result["models"] if model["model"] == "company-default")
    assert row["source"] == "native settings"
    assert row["efforts"] is None and row["access"] == "unverified"
    assert "error" not in result


def test_explicit_catalog_supplements_discovery_and_marks_restrictions(discovery):
    _, config = discovery
    config.providers["claude-work"].models = {
        "company-model": ModelSettings(efforts=["medium"], effort="medium"),
        "sonnet": ModelSettings(),
    }
    result = asyncio.run(discover_models(config, "claude-work"))["providers"][0]
    models = {row["model"]: row for row in result["models"]}
    assert models["company-model"]["source"] == "config"
    assert models["company-model"]["efforts"] == ["medium"]
    assert models["company-model"]["default_effort"] == "medium"
    assert models["company-model"]["access"] == "unverified"
    assert models["sonnet"]["selectable"]
    assert not models["legacy"]["selectable"]


@pytest.mark.parametrize(
    "name,mode,detail",
    [
        ("codex-work", "error", "metadata denied"),
        ("codex-work", "cycle", "pagination"),
        ("claude-work", "missing", "did not return a model catalog"),
        ("claude-work", "wait", "timed out"),
    ],
)
def test_discovery_failure_keeps_config_catalog_and_cleans_up(
    discovery, name, mode, detail, monkeypatch
):
    homes, config = discovery
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    config.providers[name].env = ["COMPANY_GATEWAY_TOKEN"]
    config.providers[name].models = {"configured": ModelSettings(efforts=["high"])}
    config.providers[name].model = ""
    config.limits.scan_timeout_seconds = 1
    (homes[name] / "mode").write_text(mode)
    report = asyncio.run(discover_models(config))
    result = next(row for row in report["providers"] if row["provider"] == name)
    assert detail in result["error"]
    assert any(row["model"] == "configured" for row in result["models"])
    assert "native-secret" not in json.dumps(report) and "gateway-secret" not in json.dumps(report)
    assert all("error" not in row for row in report["providers"] if row["provider"] != name)
    observed = json.loads((homes[name] / "observed.json").read_text())
    assert not os.path.exists(observed["workspace"])
    assert subprocess.run(["ps", "-p", str(observed["pid"])], capture_output=True).returncode != 0


def test_unknown_provider_does_not_fall_back(discovery):
    homes, config = discovery
    with pytest.raises(ValueError, match="Unknown provider"):
        asyncio.run(discover_models(config, "missing"))
    assert all(not (path / "observed.json").exists() for path in homes.values())


def test_models_command_human_and_json(home, discovery, capsys):
    _, config = discovery
    report = asyncio.run(discover_models(config))
    store = Store(home.db)
    store.apply_models({**report, "valid": True, "checks": []}, "fixture")
    store.close()
    home.config.write_text("invalid: [")
    prefix = ["--home", str(home.root), "models", "list", "--provider", "claude-work"]
    execute(parser().parse_args(prefix))
    human = capsys.readouterr().out
    assert "claude-work" in human and "low, high" in human and "Unverified" in human
    assert "legacy" not in human and "no-effort" not in human
    execute(parser().parse_args([*prefix, "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert len(report["providers"]) == 1
    assert "error" not in report["providers"][0]
    assert home.db.exists()


def test_models_command_shows_cached_partial_failure(home, discovery, capsys):
    homes, config = discovery
    (homes["claude-work"] / "mode").write_text("missing")
    report = asyncio.run(discover_models(config))
    store = Store(home.db)
    store.apply_models({**report, "valid": False, "checks": []}, "fixture")
    store.close()
    execute(parser().parse_args(["--home", str(home.root), "models", "list", "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert len(report["providers"]) == 3
    assert any(row.get("error") for row in report["providers"])


def test_apply_probes_all_models_and_persists_models_with_efforts(home, discovery):
    homes, _ = discovery
    streamed = []
    report = apply_config(home, on_check=streamed.append)
    assert report["valid"] and report["applied"], report
    assert streamed == report["checks"]
    stored = Store.cached_models(home.db)
    assert len(stored["providers"]) == 3
    for catalog in stored["providers"]:
        assert all(model["access"] == "verified" for model in catalog["models"])
        calls = list(
            map(json.loads, (homes[catalog["provider"]] / "probes.jsonl").read_text().splitlines())
        )
        discovered = next(
            row for row in report["providers"] if row["provider"] == catalog["provider"]
        )
        assert {call["model"] for call in calls} == {
            model["model"] for model in discovered["models"]
        }
        assert len(calls) == len(discovered["models"])
        assert all(model["efforts"] or model["default_effort"] for model in catalog["models"])
        assert catalog["checked_at"] == stored["checked_at"]
    assert "native-secret" not in json.dumps(stored)
    store = Store(home.db)
    try:
        assert not store.rows("SELECT * FROM scans")
    finally:
        store.close()


def test_apply_checks_models_outside_allowlist_and_excludes_failures(home, discovery):
    homes, _ = discovery
    assert apply_config(home)["valid"]
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["codex-work"]["models"] = ["one"]
    home.config.write_text(yaml.safe_dump(data))
    (homes["codex-work"] / "denied").write_text("hidden\none")
    report = apply_config(home)
    assert report["applied"] and not report["valid"]
    stored = Store.cached_models(home.db)
    catalog = next(row for row in stored["providers"] if row["provider"] == "codex-work")
    models = {model["model"]: model for model in catalog["models"]}
    assert models == {}
    store = Store(home.db)
    try:
        raw = json.loads(
            store.one("SELECT data FROM model_catalogs WHERE provider=?", ("codex-work",))["data"]
        )
        assert raw["models"] == []
    finally:
        store.close()
    checked = next(row for row in report["providers"] if row["provider"] == "codex-work")
    hidden = next(model for model in checked["models"] if model["model"] == "hidden")
    assert hidden["access"] == "unavailable"
    assert not hidden["selectable"]
    assert "Access denied [REDACTED]" in hidden["checks"][0]["message"]
    assert all(
        model["access"] == "verified"
        for row in stored["providers"]
        if row["provider"] != "codex-work"
        for model in row["models"]
    )


def test_apply_rereads_config_and_removes_old_provider_and_model_rows(home, discovery):
    _, _ = discovery
    assert apply_config(home)["valid"]
    data = yaml.safe_load(home.config.read_text())
    del data["providers"]["codex-personal"]
    data["providers"]["claude-work"]["models"] = {"company-model": {"effort": "high"}}
    home.config.write_text(yaml.safe_dump(data))
    assert apply_config(home)["valid"]
    stored = Store.cached_models(home.db)
    assert {row["provider"] for row in stored["providers"]} == {"claude-work", "codex-work"}
    claude = next(row for row in stored["providers"] if row["provider"] == "claude-work")
    company = next(model for model in claude["models"] if model["model"] == "company-model")
    assert company["access"] == "verified" and company["checks"][0]["effort"] == "high"
    del data["providers"]["claude-work"]["models"]
    home.config.write_text(yaml.safe_dump(data))
    assert apply_config(home)["valid"]
    assert "company-model" not in {
        model["model"]
        for row in Store.cached_models(home.db)["providers"]
        for model in row["models"]
    }


def test_config_changes_during_apply_keep_previous_catalog(home, discovery):
    _, _ = discovery
    assert apply_config(home)["valid"]
    before = Store.cached_models(home.db)

    def edited(check):
        if check["name"] == "models:claude-work":
            home.config.write_text(home.config.read_text() + "\n# changed during apply\n")

    report = apply_config(home, on_check=edited)
    assert not report["valid"] and not report["applied"]
    assert "changed during apply" in report["checks"][-1]["message"]
    assert Store.cached_models(home.db) == before


def test_models_list_reads_database_without_configuration_or_provider_calls(
    home, discovery, monkeypatch, capsys
):
    homes, _ = discovery
    assert apply_config(home)["valid"]
    before = {name: (path / "probes.jsonl").read_bytes() for name, path in homes.items()}
    home.config.write_text("invalid: [")
    monkeypatch.setattr(
        "maxwell.config.Paths.load",
        lambda *args, **kwargs: pytest.fail("Configuration read during models list"),
    )
    execute(parser().parse_args(["--home", str(home.root), "models", "list", "--json"]))
    assert json.loads(capsys.readouterr().out)["providers"]
    assert before == {name: (path / "probes.jsonl").read_bytes() for name, path in homes.items()}


def test_models_list_before_apply_has_clear_error_and_creates_nothing(home):
    with pytest.raises(ValueError, match="maxwell config apply"):
        execute(parser().parse_args(["--home", str(home.root), "models", "list"]))
    assert not home.db.exists()


def test_apply_reloads_running_daemon_after_persisting_results(home, discovery, monkeypatch):
    home.socket.touch()
    requests = []

    async def reload(paths, command, **arguments):
        assert Store.cached_models(paths.db)["valid"]
        assert command == "reload_config"
        assert arguments["config_digest"] == config_digest(paths)
        requests.append(command)
        return {"daemon": "reloaded"}

    monkeypatch.setattr("maxwell.config_apply.request", reload)
    report = apply_config(home)
    assert report["valid"] and requests == ["reload_config"]
    assert report["checks"][-1]["name"] == "daemon"
