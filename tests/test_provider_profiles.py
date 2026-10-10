import asyncio
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from maxwell.cli import execute, parser, register_repository
from maxwell.config import Config, ModelSettings, ProviderSettings
from maxwell.config_apply import config_digest
from maxwell.config_validation import validate_config
from maxwell.contracts import Usage, estimate_cost
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.provider_runtime import native_errors, native_settings, runtime_environment
from maxwell.reporting import Reporter, hunter_ratings, scan_report
from maxwell.repositories import stop_orphan
from maxwell.store import Store


@pytest.fixture
def native_cli(directory, finding):
    executable = directory / "native-cli"
    executable.write_text(
        """#!PYTHON
import json, os, sys, time, tomllib
from pathlib import Path
args = sys.argv[1:]
codex = args[0] in ('exec', 'app-server')
metadata = args[0] == 'app-server' or '--input-format' in args
home = Path(os.environ['CODEX_HOME' if codex else 'CLAUDE_CONFIG_DIR'])
if metadata:
    observed = {'pid':os.getpid(), 'workspace':str(Path.cwd()), 'args':args, 'model':None, 'probe':False}
    (home / 'observed.json').write_text(json.dumps(observed))
    mode = (home / 'mode').read_text() if (home / 'mode').exists() else 'normal'
    if mode == 'wait':
        time.sleep(60)
    native = tomllib.loads((home / 'config.toml').read_text()) if codex else json.loads((home / 'settings.json').read_text())
    model = native.get('model') or ('default' if not codex else None)
    for line in sys.stdin:
        message = json.loads(line)
        if codex:
            if message['method'] == 'initialized':
                continue
            result = {} if message['method'] == 'initialize' else {'data':[{'model':model}] if model else [], 'nextCursor':None}
            print(json.dumps({'id':message['id'], 'result':result}), flush=True)
        else:
            print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':message['request_id'],'response':{'models':[{'value':model}]}}}),flush=True)
    sys.exit(0)
model = args[args.index('--model') + 1] if '--model' in args else os.environ.get('ANTHROPIC_MODEL', 'cli-default')
workspace = Path(args[args.index('--cd') + 1]) if codex else Path.cwd()
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text()) if codex else json.loads(args[args.index('--json-schema') + 1])
probe = 'ok' in schema['properties']
connection = {}
if codex:
    if '--ignore-user-config' not in args:
        connection = tomllib.loads((home / 'config.toml').read_text())
        if any('transport' in server for server in connection.get('mcp_servers', {}).values()):
            print('Error loading config.toml: invalid transport', file=sys.stderr)
            sys.exit(1)
    for position, argument in enumerate(args):
        if argument == '-c':
            for key, value in tomllib.loads(args[position + 1]).items():
                if key in ('model_provider', 'model_providers', 'cli_auth_credentials_store', 'model_catalog_json', 'model_reasoning_effort'):
                    connection[key] = value
observed = {
    'model': model, 'home': str(home), 'args': args,
    'pid': os.getpid(), 'probe': probe,
    'api_key_present': 'CODEX_API_KEY' in os.environ,
    'github_env_present': 'GH_TOKEN' in os.environ,
    'gateway_env_present': 'COMPANY_GATEWAY_TOKEN' in os.environ,
    'workspace': str(workspace),
    'connection': connection,
    'native_auth_file_present': (home / 'auth.json').is_file(),
}
gateway = connection.get('model_providers', {}).get(connection.get('model_provider'), {})
observed['gateway_auth_received'] = os.environ.get(gateway.get('env_key', '')) == 'gateway-secret'
observed['gateway_header_received'] = os.environ.get(gateway.get('env_http_headers', {}).get('X-Company-Key', '')) == 'header-secret'
(home / 'observed.json').write_text(json.dumps(observed))
with (home / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(observed) + '\\n')
sys.stdin.read()
mode = (home / 'mode').read_text() if (home / 'mode').exists() else 'normal'
if mode == 'wait':
    time.sleep(60)
if mode == 'fail':
    print(os.environ.get('COMPANY_GATEWAY_TOKEN', ''), file=sys.stderr)
    sys.exit(1)
if mode == 'modify':
    (workspace / 'app.py').write_text('changed by model')
result = {'summary': 'Native discovery ' + os.environ.get('COMPANY_GATEWAY_TOKEN', ''),
          'findings': json.loads((home / 'findings.json').read_text())
              if (home / 'findings.json').exists() else [FINDING]}
if probe:
    result = {'ok': mode != 'invalid-probe'}
elif 'verdict' in schema['properties']:
    result = {'summary': 'Source investigation complete.', 'verdict': 'supported',
              'actionability': 'actionable', 'priority': 'P1',
              'evidence': 'app.py:2 divides by len(values), which is zero for an empty list.'}
if mode == 'probe-error':
    message = 'Model access denied ' + os.environ.get('COMPANY_GATEWAY_TOKEN', '')
    if codex:
        print(json.dumps({'type': 'turn.failed', 'error': {'message': message}}), flush=True)
        sys.exit(1)
    else:
        print(json.dumps({'type': 'result', 'is_error': True, 'result': message}), flush=True)
        sys.exit(0)
print(json.dumps({'type': 'diagnostic', 'text': os.environ.get('COMPANY_GATEWAY_TOKEN', '')}), flush=True)
if codex:
    Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(result))
    if mode != 'unknown':
        print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1000,
              'cached_input_tokens': 500, 'output_tokens': 100}}), flush=True)
else:
    print(json.dumps({'type': 'result', 'is_error': mode == 'error',
        'structured_output': result, 'usage': {'input_tokens': 400,
        'cache_read_input_tokens': 500, 'cache_creation_input_tokens': 100,
        'output_tokens': 100}}), flush=True)
""".replace("PYTHON", os.sys.executable).replace("FINDING", repr(finding))
    )
    executable.chmod(0o700)
    return executable


@pytest.fixture
def native(home, directory, native_cli):
    homes = {}
    providers = {}
    for name, adapter in (("first", "codex"), ("second", "codex"), ("claude", "claude")):
        path = directory / name
        path.mkdir()
        homes[name] = path
        if adapter == "codex":
            (path / "config.toml").write_text('model = "native-default"\n')
            (path / "auth.json").write_text('{"tokens":{"access_token":"native-auth-secret"}}')
        else:
            (path / "settings.json").write_text('{"model":"native-default"}')
        providers[name] = {"adapter": adapter, "home": str(path), "executable": str(native_cli)}
    data = yaml.safe_load(home.config.read_text())
    del data["provider"]
    data["providers"] = providers
    home.config.write_text(yaml.safe_dump(data))
    return homes, home.load()


def add_repository(home, config, source, *options):
    args = parser().parse_args(["repo", "add", str(source), "--github", "acme/repo", *options])
    return register_repository(args, home, config)


def request_scan(daemon, *, provider="first", **arguments):
    class Reader:
        async def readline(self):
            return json.dumps(
                {"version": 1, "command": "scan", "provider": provider, **arguments}
            ).encode()

    class Writer:
        def write(self, value):
            self.value = value

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    writer = Writer()
    asyncio.run(daemon.handle(Reader(), writer))
    return json.loads(writer.value)


def run_next(daemon):
    job = daemon.store.claim(
        20_000_000, 1_000_000, 6, daily_scan_limit=daemon.config.limits.daily_scan_limit
    )
    assert job is not None
    asyncio.run(daemon.scan(job))
    return daemon.store.one("SELECT * FROM scans WHERE id=?", (job["id"],))


@pytest.fixture
def native_daemon(home, native, source):
    homes, config = native
    add_repository(home, config, source)
    daemon = Daemon(home, config)
    daemon.store = Store(home.db)
    yield daemon, homes
    daemon.store.close()


def test_manual_scan_requires_model_even_with_one_provider():
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["scan", "acme/repo"])
    assert error.value.code == 2


def test_registration_does_not_require_provider_selection(home, native, source):
    _, config = native
    record = add_repository(home, config, source)
    assert "stages" not in record
    store = Store(home.db)
    try:
        assert "stages" not in store.repository("acme/repo")
        assert not store.one("SELECT name FROM sqlite_master WHERE name='repository_stage_routes'")
    finally:
        store.close()


def test_remote_registration_with_multiple_connections_only_saves_repository_settings(
    home, native, capsys
):
    execute(
        parser().parse_args(["--home", str(home.root), "repo", "add", "yuraaka/maxwell", "--json"])
    )
    result = json.loads(capsys.readouterr().out)
    assert result["repository"] == "yuraaka/maxwell"
    assert "stages" not in result
    store = Store(home.db)
    try:
        record = store.repository("yuraaka/maxwell")
        assert record["source_kind"] == "github"
        assert record["github_account"] == "default"
        assert not {"provider", "model", "effort", "stages"}.intersection(record)
        assert not store.rows("SELECT * FROM scans")
    finally:
        store.close()


def test_scan_requires_connection_when_multiple_providers_are_configured(native_daemon):
    daemon, _ = native_daemon
    result = request_scan(daemon, provider=None, repository="acme/repo", model="model-a")
    assert "Specify --provider" in result["error"]
    assert not daemon.store.rows("SELECT * FROM scans")


def test_registration_does_not_require_a_native_model_default(home, native, source):
    homes, config = native
    for name in ("first", "second"):
        (homes[name] / "config.toml").write_text("")
    (homes["claude"] / "settings.json").write_text("{}")
    assert add_repository(home, config, source)["repository"] == "acme/repo"


def test_model_catalog_is_optional_and_prices_are_model_specific():
    provider = ProviderSettings(
        api_key="secret", model="old", input_usd_per_mtok=2, output_usd_per_mtok=8
    )
    assert provider.for_model("old").input_usd_per_mtok == 2
    assert provider.for_model("new").input_usd_per_mtok is None
    catalog = ProviderSettings(
        models={"cheap": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 2}, "unpriced": {}}
    )
    assert catalog.for_model("cheap").input_usd_per_mtok == 1
    assert catalog.for_model("unpriced").input_usd_per_mtok is None
    with pytest.raises(ValueError, match="model catalog"):
        catalog.for_model("unknown")
    with pytest.raises(ValueError, match="multiple models"):
        catalog.default_model()
    assert ProviderSettings(models=["only"]).default_model() == "only"


def test_manual_requests_keep_distinct_models_and_do_not_change_routing(native_daemon):
    daemon, _ = native_daemon
    before = daemon.store.repository("acme/repo")
    first = request_scan(daemon, repository="acme/repo", model="model-a")["scans"][0]
    same = request_scan(daemon, repository="acme/repo", model="model-a")["scans"][0]
    second = request_scan(daemon, repository="acme/repo", model="model-b")["scans"][0]
    other = request_scan(daemon, repository="acme/repo", provider="second", model="model-a")[
        "scans"
    ][0]
    assert first == same
    assert len({first, second, other}) == 3
    assert daemon.store.repository("acme/repo") == before
    assert "error" in request_scan(daemon, repository="acme/repo")


def test_all_repository_requests_validate_before_queueing(native_daemon, source):
    daemon, _ = native_daemon
    daemon.config.providers["first"].models = {"different": ModelSettings()}
    daemon.store.register("acme/other", str(source), "main")
    result = request_scan(daemon, model="model-a")
    assert "error" in result
    assert daemon.store.rows("SELECT * FROM scans") == []


@pytest.mark.parametrize("provider", ["first", "second", "claude"])
def test_native_cli_uses_selected_home_and_gateway_without_api_key(
    home, native_daemon, provider, monkeypatch
):
    daemon, homes = native_daemon
    monkeypatch.setenv("GH_TOKEN", "unrelated-github-secret")
    monkeypatch.setenv("CODEX_API_KEY", "unrelated-api-secret")
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    daemon.config.providers[provider].env = ["COMPANY_GATEWAY_TOKEN"]
    before = {path.name: path.read_bytes() for path in homes[provider].iterdir() if path.is_file()}
    request_scan(daemon, repository="acme/repo", provider=provider, model="chosen-model")
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    observed = json.loads((homes[provider] / "observed.json").read_text())
    assert observed["home"] == str(homes[provider])
    assert observed["model"] == "chosen-model"
    assert observed["gateway_env_present"]
    assert not observed["api_key_present"] and not observed["github_env_present"]
    if provider == "claude":
        assert "--bare" not in observed["args"]
        assert observed["args"][observed["args"].index("--tools") + 1] == "Read,Grep,Glob"
        assert "--strict-mcp-config" in observed["args"]
        assert scan["usage"] and json.loads(scan["usage"])["cache_write_tokens"] == 100
    else:
        assert "--ignore-user-config" in observed["args"]
        assert "read-only" in observed["args"]
    for filename, content in before.items():
        assert (homes[provider] / filename).read_bytes() == content
    assert scan["cost"] is None and scan["accounting"] == "unpriced"
    assert daemon.store.committed_today() == 0
    assert "1,100 tokens" in scan_report(scan)
    assert "$" not in scan_report(scan)
    for path in (home.root / "artifacts").rglob("*"):
        if path.is_file():
            assert "gateway-secret" not in path.read_text(errors="replace")
            assert "native-auth-secret" not in path.read_text(errors="replace")


def test_queued_run_keeps_home_model_and_prices_when_config_changes(native_daemon):
    daemon, homes = native_daemon
    from maxwell.config import ModelSettings

    daemon.config.providers["first"].models = {
        "priced": ModelSettings(
            input_usd_per_mtok=2, output_usd_per_mtok=8, cached_input_usd_per_mtok=0.2
        )
    }
    request_scan(daemon, repository="acme/repo", model="priced")
    daemon.config.providers["first"].home = str(homes["second"])
    daemon.config.providers["first"].models["priced"].input_usd_per_mtok = 200
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    assert scan["cost"] == 1900
    assert (homes["first"] / "observed.json").exists()
    assert not (homes["second"] / "observed.json").exists()
    assert "native-auth-secret" not in scan["execution"]


def test_different_models_scan_same_revision_and_keep_duplicate_evidence(native_daemon):
    daemon, _ = native_daemon
    for model in ("model-a", "model-b"):
        record = daemon.store.one("SELECT * FROM repositories")
        execution = daemon.config.execution({"provider": "first", "model": model}, record)
        daemon.store.enqueue("acme/repo", forced=False, execution=execution)
        assert run_next(daemon)["state"] == "completed"
    assert len(daemon.store.rows("SELECT * FROM findings")) == 1
    assert len(daemon.store.rows("SELECT * FROM finding_observations")) == 2
    with daemon.store.db:
        daemon.store.db.execute("UPDATE findings SET state='confirmed'")
    assert len(hunter_ratings(daemon.store)) == 2
    daemon.store.enqueue("acme/repo", forced=False, execution=execution)
    assert run_next(daemon)["state"] == "skipped"


def test_existing_observations_leave_capacity_for_new_findings(native_daemon, finding):
    daemon, homes = native_daemon
    request_scan(daemon, repository="acme/repo", model="first-model")
    assert run_next(daemon)["admitted"] == 1
    daemon.config.limits.human_queue_max = 2
    daemon.config.limits.max_findings_per_scan = 1
    new = {**finding, "symbol": "another_issue", "title": "Another issue", "priority": "P2"}
    (homes["first"] / "findings.json").write_text(json.dumps([finding, new]))
    request_scan(daemon, repository="acme/repo", model="second-model")
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    assert scan["admitted"] == 1
    assert len(daemon.store.rows("SELECT * FROM findings")) == 2
    assert len(daemon.store.rows("SELECT * FROM finding_observations")) == 3
    assert json.loads(scan["selection"])["already_known"] == 1


@pytest.mark.parametrize("count,limit", [(60, 100), (125, 125)])
def test_collection_exceeds_twenty_and_publishes_ranked_top_with_skipped_report(
    native_daemon, finding, home, count, limit
):
    daemon, homes = native_daemon
    daemon.config.limits.max_findings_per_scan = 1
    daemon.config.limits.max_candidates_per_scan = limit
    candidates = [
        {
            **finding,
            "title": f"Concrete maintenance candidate {index:03d}",
            "evidence": "E" * 6000,
            "priority": "P1" if index == count - 1 else "P2",
        }
        for index in range(count)
    ]
    (homes["first"] / "findings.json").write_text(json.dumps(candidates))
    request_scan(daemon, repository="acme/repo", model="chosen-model")
    daemon.config.limits.max_candidates_per_scan = 1
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    assert scan["admitted"] == 1
    assert (
        json.loads(daemon.store.one("SELECT data FROM findings")["data"])["title"]
        == candidates[-1]["title"]
    )
    stats = json.loads(scan["selection"])
    assert stats["collected"] == count and stats["selected"] == 1
    assert stats["skipped"] == count - 1 and stats["publication_limit"] == count - 1
    artifact = home.root / "artifacts" / scan["id"]
    assert (artifact / "result.json").stat().st_size > 256 * 1024
    assert (
        json.loads((artifact / "schema.json").read_text())["properties"]["findings"]["maxItems"]
        == limit
    )
    assert "Do not stop after finding 1 issues" in (artifact / "prompt.txt").read_text()
    assert f"up to {limit}, using the supplied schema" in (artifact / "prompt.txt").read_text()
    assert f"Skipped: **{count - 1}**" in scan_report(scan)
    assert f"Collection is bounded at {limit} candidates" in scan_report(scan)
    assert "Already known | 0" in scan_report(scan)


@pytest.mark.parametrize("provider", ["first", "claude"])
def test_provider_rejects_results_over_configured_candidate_limit(native_daemon, finding, provider):
    daemon, homes = native_daemon
    daemon.config.limits.max_candidates_per_scan = 1
    (homes[provider] / "findings.json").write_text(json.dumps([finding, finding]))
    request_scan(daemon, repository="acme/repo", provider=provider, model="chosen-model")
    scan = run_next(daemon)
    assert scan["state"] == "failed"
    assert scan["admitted"] == 0 and scan["selection"] is None
    assert "at most 1 item" in scan["error"]


@pytest.mark.parametrize("provider", ["first", "claude"])
def test_review_and_audit_use_both_provider_adapters(native_daemon, home, capsys, provider):
    daemon, _ = native_daemon
    for stage in ("hunt", "review", "audit"):
        execute(
            parser().parse_args(
                [
                    "--home",
                    str(home.root),
                    "scan",
                    "acme/repo",
                    "--stage",
                    stage,
                    "--provider",
                    provider,
                    "--model",
                    "chosen-model",
                    "--effort",
                    "high",
                    "--json",
                ]
            )
        )
        result = json.loads(capsys.readouterr().out)
        assert result["valid"], result
        scan = result["scans"][0]
        assert scan["stage"] == stage and scan["effort"] == "high"
        if stage != "hunt":
            assert scan["assessments"][0]["agent"]["adapter"] == (
                "codex" if provider == "first" else "claude"
            )
            assert scan["assessments"][0]["verdict"] == "supported"
            assert scan["usage"]["cache_write_tokens"] == (0 if provider == "first" else 100)
            assert scan["accounting"] == "tokens"
    assert daemon.store.one("SELECT COUNT(*) AS n FROM scans")["n"] == 3


def test_codex_profile_resolves_model_and_gateway_and_is_passed_to_cli(
    home, native_daemon, monkeypatch
):
    daemon, homes = native_daemon
    provider = daemon.config.providers["first"]
    provider.profile = "work"
    (homes["first"] / "config.toml").write_text(
        'model="base-model"\nmodel_provider="old"\n'
        '[model_providers.old]\nenv_key="UNUSED_OLD_TOKEN"\n'
        '[model_providers.gateway]\nbase_url="https://company.invalid"\n'
        'env_http_headers={"X-Company"="COMPANY_HEADER"}\n'
    )
    (homes["first"] / "work.config.toml").write_text(
        'model="profile-model"\nmodel_provider="gateway"\n'
        '[model_providers.gateway]\nenv_key="COMPANY_GATEWAY_TOKEN"\n'
    )
    monkeypatch.delenv("COMPANY_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("UNUSED_OLD_TOKEN", raising=False)
    monkeypatch.setenv("COMPANY_HEADER", "header-secret")
    assert provider.default_model() == "profile-model"
    assert native_errors(provider) == ["Missing gateway environment variable COMPANY_GATEWAY_TOKEN"]
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    environment, native, _ = runtime_environment(home, provider)
    assert native["model_providers"]["gateway"]["base_url"] == "https://company.invalid"
    assert environment["COMPANY_HEADER"] == "header-secret"
    assert "UNUSED_OLD_TOKEN" not in environment
    request_scan(daemon, repository="acme/repo", model="manual-model")
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    observed = json.loads((homes["first"] / "observed.json").read_text())
    assert "--profile" not in observed["args"]
    assert observed["connection"]["model_provider"] == "gateway"
    assert (
        observed["connection"]["model_providers"]["gateway"]["env_key"] == "COMPANY_GATEWAY_TOKEN"
    )
    assert observed["model"] == "manual-model"
    assert json.loads(scan["execution"])["settings"]["profile"] == "work"


def test_missing_codex_profile_fails_validation(home, native):
    _, config = native
    provider = config.providers["first"].model_copy(update={"profile": "missing"})
    with pytest.raises(ValueError, match="profile file is missing"):
        native_settings(provider)
    assert "profile file is missing" in native_errors(provider)[0]


@pytest.mark.parametrize(
    "settings",
    [
        {"profile": "work"},
        {"adapter": "claude", "home": "/existing", "profile": "work"},
        {"home": "/existing", "profile": "../work"},
        {"home": "/existing", "profile": ""},
    ],
)
def test_invalid_profile_selection_is_rejected(settings):
    with pytest.raises(ValueError):
        ProviderSettings.model_validate(settings)


@pytest.mark.parametrize("content", ['profile="work"', '[profiles.work]\nmodel="old"'])
def test_legacy_codex_profiles_have_actionable_errors(native, content):
    homes, config = native
    (homes["first"] / "config.toml").write_text(content)
    assert "move settings to NAME.config.toml" in native_errors(config.providers["first"])[0]


def test_unpriced_limits_count_started_calls_and_preserve_unknown_usage(native_daemon):
    daemon, homes = native_daemon
    daemon.config.limits.daily_scan_limit = 1
    (homes["first"] / "mode").write_text("unknown")
    request_scan(daemon, repository="acme/repo", model="first")
    scan = run_next(daemon)
    assert scan["accounting"] == "unpriced" and scan["executed"] == 1
    assert scan["cost"] is None and scan["reserved"] == 0
    request_scan(daemon, repository="acme/repo", model="second")
    assert daemon.store.claim(1, 1, 6, daily_scan_limit=1) is None
    with daemon.store.db:
        daemon.store.db.execute("UPDATE scans SET started=0 WHERE executed=1")
    assert daemon.store.committed_today() == 0
    assert daemon.store.claim(1, 1, 6, daily_scan_limit=1) is not None


def test_preparation_failure_does_not_use_unpriced_scan_allowance(native_daemon):
    daemon, _ = native_daemon
    daemon.config.limits.daily_scan_limit = 1
    with daemon.store.db:
        daemon.store.db.execute("UPDATE repositories SET ref='missing'")
    request_scan(daemon, repository="acme/repo", model="first")
    scan = run_next(daemon)
    assert scan["state"] == "failed" and scan["executed"] == 0
    request_scan(daemon, repository="acme/repo", model="second")
    assert daemon.store.claim(1, 1, 6, daily_scan_limit=1) is not None


@pytest.mark.parametrize("adapter", ["first", "claude"])
@pytest.mark.parametrize("mode", ["modify", "fail", "wait"])
def test_native_failures_are_fenced_and_bounded(native_daemon, adapter, mode):
    daemon, homes = native_daemon
    daemon.config.limits.scan_timeout_seconds = 1
    (homes[adapter] / "mode").write_text(mode)
    request_scan(daemon, repository="acme/repo", provider=adapter, model="chosen")
    scan = run_next(daemon)
    assert scan["state"] == "failed"
    assert daemon.store.rows("SELECT * FROM findings") == []
    assert not daemon.store.rows("SELECT pid FROM scans WHERE pid IS NOT NULL")
    assert scan["executed"] == 1


def test_native_configuration_checks_dependencies_without_repository_routes(
    home, native_daemon, monkeypatch
):
    daemon, homes = native_daemon
    (homes["first"] / "config.toml").write_text(
        'model="native-default"\nmodel_provider="gateway"\n'
        '[model_providers.gateway]\nbase_url="https://company.invalid"\nenv_key="COMPANY_GATEWAY_TOKEN"\n'
    )
    monkeypatch.delenv("COMPANY_GATEWAY_TOKEN", raising=False)
    report = validate_config(home, online=False)
    assert not report["valid"]
    assert "COMPANY_GATEWAY_TOKEN" in json.dumps(report)
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    report = validate_config(home, online=False)
    assert report["valid"], report
    assert "gateway-secret" not in json.dumps(report)
    with daemon.store.db:
        daemon.store.db.executescript(
            "CREATE TABLE repository_stage_routes(repository TEXT,stage TEXT,provider TEXT,model TEXT);"
            "INSERT INTO repository_stage_routes VALUES('acme/repo','discovery','missing','chosen');"
        )
    assert validate_config(home, online=False)["valid"]


def test_native_provider_definitions_do_not_mix_with_legacy_config():
    with pytest.raises(ValueError, match="either provider or providers"):
        Config.model_validate({"provider": {}, "providers": {"native": {"home": "/existing"}}})


def test_declared_environment_dependencies_only_are_forwarded(home, native, monkeypatch):
    _, config = native
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "company-token")
    monkeypatch.setenv("GH_TOKEN", "github-token")
    settings = config.providers["first"].model_copy(update={"env": ["COMPANY_GATEWAY_TOKEN"]})
    environment, _, secrets = runtime_environment(home, settings)
    assert environment["COMPANY_GATEWAY_TOKEN"] == "company-token"
    assert "company-token" in secrets
    assert "GH_TOKEN" not in environment


def test_claude_cache_costs_include_cache_creation():
    usage = Usage(
        input_tokens=1000,
        cached_input_tokens=500,
        cache_write_tokens=100,
        output_tokens=100,
        reported=True,
        complete=True,
    )
    settings = ProviderSettings(
        input_usd_per_mtok=2,
        cached_input_usd_per_mtok=0.2,
        cache_write_usd_per_mtok=2.5,
        output_usd_per_mtok=8,
    )
    assert estimate_cost(usage, settings) == 1950


def test_gateway_paths_resolve_against_config_directory(home, native):
    homes, _ = native
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["first"]["home"] = "../first"
    home.config.write_text(yaml.safe_dump(data))
    assert home.load().providers["first"].home == str(homes["first"])


@pytest.mark.parametrize("content", ['{"env":{"ANTHROPIC_AUTH_TOKEN":1}}', '["invalid"]', "{bad"])
def test_invalid_native_settings_fail_config_validation(home, native, content):
    homes, _ = native
    (homes["claude"] / "settings.json").write_text(content)
    report = validate_config(home, online=False)
    assert not report["valid"]


def test_online_check_probes_catalog_and_defaults_once(home, native, source, github, monkeypatch):
    homes, config = native
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["first"].update(models=["model-a", "model-b"], env=["COMPANY_GATEWAY_TOKEN"])
    data["providers"]["claude"]["models"] = ["native-default", "stage-model"]
    home.config.write_text(yaml.safe_dump(data))
    config = home.load()
    add_repository(home, config, source)
    database_before = home.db.read_bytes()
    files_before = home.config.read_bytes()
    report = validate_config(home)
    assert report["valid"], report
    assert home.db.read_bytes() == database_before
    assert home.config.read_bytes() == files_before
    assert not list((home.root / "artifacts").iterdir())
    assert not list((home.root / "runtimes").iterdir())
    selected = {
        (name, call["model"])
        for name, path in homes.items()
        for call in map(json.loads, (path / "calls.jsonl").read_text().splitlines())
    }
    assert selected == {
        ("first", "model-a"),
        ("first", "model-b"),
        ("second", "native-default"),
        ("claude", "native-default"),
        ("claude", "stage-model"),
    }
    calls = [
        call
        for path in homes.values()
        for call in map(json.loads, (path / "calls.jsonl").read_text().splitlines())
    ]
    assert len(calls) == len(selected)
    assert all(call["probe"] for call in calls)
    assert all(not os.path.exists(call["workspace"]) for call in calls)
    for call in calls:
        if call["home"] == str(homes["claude"]):
            assert call["args"][call["args"].index("--tools") + 1] == ""
        else:
            assert "--skip-git-repo-check" in call["args"]
    assert "gateway-secret" not in json.dumps(report)
    assert {
        check["name"]
        for check in report["checks"]
        if check["status"] == "ok" and "model access verified" in check["message"]
    } == {f"provider:{name}:{model}" for name, model in selected}


@pytest.mark.parametrize("adapter", ["first", "claude"])
@pytest.mark.parametrize("mode", ["fail", "probe-error", "invalid-probe", "wait"])
def test_online_probe_failures_identify_pair_and_redact_secrets(
    home, native, github, monkeypatch, adapter, mode
):
    homes, _ = native
    monkeypatch.setenv("COMPANY_GATEWAY_TOKEN", "gateway-secret")
    data = yaml.safe_load(home.config.read_text())
    data["providers"] = {adapter: {**data["providers"][adapter], "env": ["COMPANY_GATEWAY_TOKEN"]}}
    data["limits"]["scan_timeout_seconds"] = 1
    home.config.write_text(yaml.safe_dump(data))
    (homes[adapter] / "mode").write_text(mode)
    report = validate_config(home)
    assert not report["valid"]
    failures = [
        check
        for check in report["checks"]
        if check["name"] == f"provider:{adapter}:native-default" and check["status"] == "error"
    ]
    assert len(failures) == 1
    if mode == "probe-error":
        assert "Model access denied" in failures[0]["message"]
    if mode == "wait":
        assert "timed out after 1 seconds" in failures[0]["message"]
        observed = json.loads((homes[adapter] / "observed.json").read_text())
        assert (
            subprocess.run(["ps", "-p", str(observed["pid"])], capture_output=True).returncode != 0
        )
    assert "gateway-secret" not in json.dumps(report)
    assert not home.db.exists()
    observed = json.loads((homes[adapter] / "observed.json").read_text())
    assert not os.path.exists(observed["workspace"])


def test_offline_check_never_invokes_native_providers(home, native):
    homes, _ = native
    report = validate_config(home, online=False)
    assert report["valid"], report
    assert all(not (path / "observed.json").exists() for path in homes.values())
    assert any("not checked offline" in check["message"] for check in report["checks"])


def test_config_apply_streams_results_while_native_probe_is_running(home, native):
    homes, _ = native
    (homes["first"] / "mode").write_text("wait")
    process = subprocess.Popen(
        [os.sys.executable, "-m", "maxwell", "--home", str(home.root), "config", "apply"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    received = b""
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.1):
                received += os.read(key.fd, 8192)
            if (homes["first"] / "observed.json").exists() and b"[OK] config" in received:
                break
        assert (homes["first"] / "observed.json").exists()
        assert process.poll() is None
        assert b"[OK] config" in received
        assert b"Applied:" not in received
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=6)
        assert process.returncode == 130
        observed = json.loads((homes["first"] / "observed.json").read_text())
        assert not os.path.exists(observed["workspace"])
    finally:
        selector.close()
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        if (homes["first"] / "observed.json").exists():
            observed = json.loads((homes["first"] / "observed.json").read_text())
            stop_orphan(observed["pid"], Path(observed["workspace"]).parent / "artifacts")


def test_online_check_needs_a_model_when_provider_has_no_default(home, native, github):
    homes, _ = native
    (homes["first"] / "config.toml").write_text("")
    assert validate_config(home, online=False)["valid"]
    report = validate_config(home)
    assert not report["valid"]
    assert any(
        check["name"] == "provider:first:model" and check["status"] == "error"
        for check in report["checks"]
    )
    assert not (homes["first"] / "observed.json").exists()


@pytest.mark.parametrize("provider", ["first", "claude"])
def test_configured_model_is_probed_without_a_native_default(home, native, source, provider):
    homes, _ = native
    data = yaml.safe_load(home.config.read_text())
    data["providers"] = {provider: data["providers"][provider]}
    data["providers"][provider]["model"] = "stage-model"
    home.config.write_text(yaml.safe_dump(data))
    if provider == "first":
        (homes[provider] / "config.toml").write_text("")
    else:
        (homes[provider] / "settings.json").write_text("{}")
    add_repository(home, home.load(), source)
    report = validate_config(home)
    assert report["valid"], report
    calls = list(map(json.loads, (homes[provider] / "calls.jsonl").read_text().splitlines()))
    assert [call["model"] for call in calls] == ["stage-model"]


def test_local_gateway_errors_prevent_online_probe(home, native, monkeypatch):
    homes, _ = native
    monkeypatch.delenv("MISSING_GATEWAY_TOKEN", raising=False)
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["first"]["env"] = ["MISSING_GATEWAY_TOKEN"]
    home.config.write_text(yaml.safe_dump(data))
    report = validate_config(home)
    assert not report["valid"]
    assert "MISSING_GATEWAY_TOKEN" in json.dumps(report)
    assert not (homes["first"] / "observed.json").exists()
    assert (homes["second"] / "observed.json").exists()


def test_provider_denial_causes_cli_failure(home, native, capsys):
    homes, _ = native
    (homes["claude"] / "mode").write_text("probe-error")
    with pytest.raises(SystemExit) as error:
        execute(parser().parse_args(["--home", str(home.root), "config", "apply", "--json"]))
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["valid"]
    assert any("Model access denied" in check["message"] for check in report["checks"])


def test_short_secrets_preserve_native_probe_response_types(home, native):
    data = yaml.safe_load(home.config.read_text())
    data["github"]["token"] = "true"
    home.config.write_text(yaml.safe_dump(data))
    report = validate_config(home)
    assert report["valid"], report
    assert sum("model access verified" in check["message"] for check in report["checks"]) == 3


@pytest.mark.parametrize("profile", [None, "work"])
@pytest.mark.parametrize("operation", ["scan", "check"])
def test_incompatible_native_mcp_does_not_block_gateway_or_copy_credentials(
    home, native_daemon, monkeypatch, profile, operation
):
    daemon, homes = native_daemon
    native_home = homes["first"]
    monkeypatch.setenv("COMPANY_HEADER", "dynamic-header-secret")
    (native_home / "config.toml").write_text(
        'model="native-default"\nmodel_provider="gateway"\ncli_auth_credentials_store="file"\n'
        'model_catalog_json="catalog.json"\n'
        '[model_providers.gateway]\nname="Company gateway"\n'
        'base_url="https://company.invalid/v1"\nexperimental_bearer_token="gateway-secret"\n'
        'http_headers={"X-Company-Key"="header-secret","X-Dynamic"="unused-static"}\n'
        'env_http_headers={"X-Dynamic"="COMPANY_HEADER"}\n'
        '[mcp_servers."computer-use"]\ntransport="company-specific"\nenabled=true\n'
    )
    (native_home / "catalog.json").write_text('{"models": []}')
    if profile:
        (native_home / "work.config.toml").write_text(
            '[model_providers.gateway]\nbase_url="https://profile.company.invalid/v1"\n'
            '[mcp_servers."profile-mcp"]\ntransport="company-specific"\n'
        )
        data = yaml.safe_load(home.config.read_text())
        data["providers"]["first"]["profile"] = profile
        home.config.write_text(yaml.safe_dump(data))
    daemon.config = home.load()
    before = {path.name: path.read_bytes() for path in native_home.iterdir()}
    if operation == "scan":
        request_scan(daemon, repository="acme/repo", model="gpt-6.1-sol")
        scan = run_next(daemon)
        assert scan["state"] == "completed", scan["error"]
    else:
        report = validate_config(home)
        assert report["valid"], report
    observed = json.loads((native_home / "observed.json").read_text())
    assert observed["home"] == str(native_home)
    assert observed["native_auth_file_present"]
    assert observed["gateway_auth_received"] and observed["gateway_header_received"]
    assert "--ignore-user-config" in observed["args"]
    assert "--profile" not in observed["args"]
    assert not any("mcp_servers" in argument for argument in observed["args"])
    assert observed["connection"]["cli_auth_credentials_store"] == "file"
    assert observed["connection"]["model_catalog_json"] == str(native_home / "catalog.json")
    gateway = observed["connection"]["model_providers"]["gateway"]
    assert gateway["base_url"] == (
        "https://profile.company.invalid/v1" if profile else "https://company.invalid/v1"
    )
    assert gateway["env_key"].startswith("MAXWELL_CODEX_")
    assert gateway["env_http_headers"]["X-Company-Key"].startswith("MAXWELL_CODEX_")
    assert gateway["env_http_headers"]["X-Dynamic"] == "COMPANY_HEADER"
    assert "experimental_bearer_token" not in gateway and "http_headers" not in gateway
    for secret in (
        "gateway-secret",
        "header-secret",
        "dynamic-header-secret",
        "native-auth-secret",
    ):
        assert secret not in json.dumps(observed["args"])
    for name, content in before.items():
        assert (native_home / name).read_bytes() == content


@pytest.mark.parametrize("marker", ["result.json", "settings.json"])
def test_recovery_stops_native_process_with_its_artifact_marker(directory, marker):
    artifact = directory / "artifacts"
    artifact.mkdir()
    process = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(60)", str(artifact / marker)],
        start_new_session=True,
    )
    try:
        stop_orphan(process.pid, artifact)
        assert process.wait(timeout=5) < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_rejected_github_scan_does_not_block_later_explicit_request(home, native_daemon, github):
    daemon, _ = native_daemon
    github["comments"][42] = [
        {"id": 12001, "body": "/maxwell scan", "user": {"login": "maintainer"}},
        {
            "id": 12002,
            "body": "/maxwell scan --provider first --model chosen --effort high",
            "user": {"login": "maintainer"},
        },
    ]
    client = GitHub(daemon.config.github)
    try:
        reporter = Reporter(daemon.store, client, daemon.config, home)
        asyncio.run(reporter.commands("acme/repo", 42))
        scans = daemon.store.rows("SELECT * FROM scans")
        assert len(scans) == 1 and scans[0]["model"] == "chosen"
        assert json.loads(scans[0]["execution"])["settings"]["effort"] == "high"
        assert len(daemon.store.rows("SELECT id FROM comments")) == 2
        assert any("Command rejected" in comment["body"] for comment in github["comments"][42])
    finally:
        client.close()


def test_missing_execution_provider_fails_run_without_fallback(native_daemon):
    daemon, homes = native_daemon
    request_scan(daemon, repository="acme/repo", model="chosen")
    del daemon.config.providers["first"]
    scan = run_next(daemon)
    assert scan["state"] == "failed"
    assert "Unknown provider" in scan["error"]
    assert scan["executed"] == 0
    assert not (homes["second"] / "observed.json").exists()


@pytest.mark.parametrize(
    "settings,expected",
    [({}, "cli-default"), ({"env": {"ANTHROPIC_MODEL": "gateway-default"}}, "gateway-default")],
)
def test_claude_check_uses_cli_default_without_selected_model(home, native, settings, expected):
    homes, _ = native
    (homes["claude"] / "settings.json").write_text(json.dumps(settings))
    report = validate_config(home)
    assert report["valid"], report
    assert {
        "name": "provider:claude:default",
        "status": "ok",
        "message": "Authenticated request succeeded; model access verified",
    } in report["checks"]
    observed = json.loads((homes["claude"] / "observed.json").read_text())
    assert observed["model"] == expected
    assert "--model" not in observed["args"]
    assert observed["probe"]
    assert observed["args"][observed["args"].index("--tools") + 1] == ""


def test_claude_default_probe_reports_denial(home, native):
    homes, _ = native
    (homes["claude"] / "settings.json").write_text("{}")
    (homes["claude"] / "mode").write_text("probe-error")
    report = validate_config(home)
    assert not report["valid"]
    failure = next(
        check for check in report["checks"] if check["name"] == "provider:claude:default"
    )
    assert failure["status"] == "error"
    assert "Model access denied" in failure["message"]


def test_offline_claude_default_does_not_make_request(home, native):
    homes, _ = native
    (homes["claude"] / "settings.json").write_text("{}")
    report = validate_config(home, online=False)
    assert report["valid"], report
    assert any(
        check["name"] == "provider:claude:default" and check["status"] == "skipped"
        for check in report["checks"]
    )
    assert not (homes["claude"] / "observed.json").exists()


@pytest.mark.parametrize("provider", ["first", "claude"])
def test_scan_effort_overrides_native_default_and_survives_config_changes(native_daemon, provider):
    daemon, homes = native_daemon
    if provider == "first":
        (homes[provider] / "config.toml").write_text(
            'model="native-default"\nmodel_reasoning_effort="high"\n'
        )
    low = request_scan(
        daemon, repository="acme/repo", provider=provider, model="chosen-model", effort="low"
    )
    high = request_scan(
        daemon, repository="acme/repo", provider=provider, model="chosen-model", effort="high"
    )
    assert "error" not in low and "error" not in high
    assert low["scans"] != high["scans"]
    daemon.config.providers[provider].effort = "high"
    scan = run_next(daemon)
    assert scan["state"] == "completed", scan["error"]
    snapshot = json.loads(scan["execution"])
    assert snapshot["effort"] == "low" and snapshot["settings"]["effort"] == "low"
    observed = json.loads((homes[provider] / "observed.json").read_text())
    if provider == "first":
        assert observed["connection"]["model_reasoning_effort"] == "low"
    else:
        assert observed["args"][observed["args"].index("--effort") + 1] == "low"
    assert (
        next(row for row in daemon.store.status()["scans"] if row["id"] == scan["id"])["effort"]
        == "low"
    )


def test_known_efforts_are_validated_before_queueing(native_daemon):
    daemon, _ = native_daemon
    daemon.config.providers["first"].models = {
        "native-default": ModelSettings(efforts=["low", "high"])
    }
    result = request_scan(daemon, repository="acme/repo", model="native-default", effort="max")
    assert "supported efforts" in result["error"]
    assert not daemon.store.rows("SELECT * FROM scans")
    assert "error" not in request_scan(
        daemon, repository="acme/repo", model="native-default", effort="low"
    )


@pytest.mark.parametrize("effort", ["", "--bad", "HIGH", "high low", "\nlow"])
def test_invalid_efforts_are_rejected(effort):
    config = Config(providers={"only": ProviderSettings(model="model-a")})
    with pytest.raises(ValueError):
        config.route(model="model-a", effort=effort)


def test_explicit_effort_and_provider_model_defaults_have_precedence():
    config = Config(
        providers={
            "only": ProviderSettings(
                effort="medium",
                models={
                    "a": ModelSettings(efforts=["low", "medium", "high"], effort="high"),
                    "b": ModelSettings(),
                },
            )
        }
    )
    route = config.route(model="a", effort="low")
    assert config.execution(route, {})["settings"]["effort"] == "low"
    assert "effort" not in config.route(model="a")
    reset = config.route(model="a", effort="default")
    assert "effort" not in reset
    assert config.execution(reset, {})["settings"]["effort"] == "high"
    changed = config.route(model="b")
    assert "effort" not in changed
    assert config.execution(changed, {})["settings"]["effort"] == "medium"


def test_config_validation_probes_configured_effort(home, native, source):
    homes, config = native
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["claude"]["models"] = {"stage-model": {"effort": "high"}}
    home.config.write_text(yaml.safe_dump(data))
    add_repository(home, config, source)
    report = validate_config(home)
    assert report["valid"], report
    assert any(
        check["name"] == "provider:claude:stage-model / high" and check["status"] == "ok"
        for check in report["checks"]
    )
    calls = list(map(json.loads, (homes["claude"] / "calls.jsonl").read_text().splitlines()))
    stage = next(call for call in calls if call["model"] == "stage-model")
    assert stage["args"][stage["args"].index("--effort") + 1] == "high"


@pytest.mark.parametrize("version", [4, 7, 8])
def test_legacy_routes_are_removed_without_changing_execution_history(home, native_daemon, version):
    daemon, _ = native_daemon
    identifier = request_scan(daemon, repository="acme/repo", model="chosen")["scans"][0]
    before = daemon.store.one("SELECT * FROM scans WHERE id=?", (identifier,))
    repository = daemon.store.repository("acme/repo")
    with daemon.store.db:
        daemon.store.db.executescript(
            "CREATE TABLE repository_stage_routes(repository TEXT,stage TEXT,provider TEXT,model TEXT);"
            "INSERT INTO repository_stage_routes VALUES('acme/repo','discovery','missing','chosen');"
        )
        daemon.store.db.execute(f"PRAGMA user_version={version}")
    report = validate_config(home, online=False)
    assert report["valid"], report
    migrated = Store(home.db)
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not migrated.one(
            "SELECT name FROM sqlite_master WHERE name='repository_stage_routes'"
        )
        assert migrated.one("SELECT * FROM scans WHERE id=?", (identifier,)) == before
        assert migrated.repository("acme/repo") == repository
    finally:
        migrated.close()


def test_daemon_reloads_applied_config_without_changing_queued_execution(home, native_daemon):
    daemon, _ = native_daemon
    queued = request_scan(daemon, repository="acme/repo", model="chosen", effort="low")["scans"][0]
    before = daemon.store.one("SELECT execution FROM scans WHERE id=?", (queued,))["execution"]
    data = yaml.safe_load(home.config.read_text())
    data["providers"]["first"]["model"] = "new-default"
    home.config.write_text(yaml.safe_dump(data))
    digest = config_digest(home)
    daemon.store.apply_models({"providers": [], "valid": True, "checks": []}, digest)
    response = request_scan(daemon, command="reload_config", config_digest=digest)
    assert response == {"daemon": "reloaded"}
    assert daemon.config.providers["first"].model == "new-default"
    assert daemon.config_generation == 1 and daemon.config_changed.is_set()
    assert (
        daemon.store.one("SELECT execution FROM scans WHERE id=?", (queued,))["execution"] == before
    )


def test_daemon_rejects_unapplied_configuration(home, native_daemon):
    daemon, _ = native_daemon
    before = daemon.config
    response = request_scan(daemon, command="reload_config", config_digest=config_digest(home))
    assert "successfully" in response["error"]
    assert daemon.config is before
    digest = config_digest(home)
    daemon.store.apply_models({"providers": [], "valid": False, "checks": []}, digest)
    response = request_scan(daemon, command="reload_config", config_digest=digest)
    assert "error" in response and daemon.config is before
