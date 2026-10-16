import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from maxwell.completion import bash_completion
from maxwell.store import Store


def complete(directory, command):
    script = directory / "completion.bash"
    script.write_text(bash_completion())
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            """
source "$1"
COMP_LINE=$2
COMP_POINT=${#COMP_LINE}
COMP_TYPE=9
_python_argcomplete maxwell
printf '%s\n' "${COMPREPLY[@]}"
""",
            "bash",
            str(script),
            command,
        ],
        cwd=directory,
        env={
            **os.environ,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        },
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return {value.strip() for value in result.stdout.splitlines() if value.strip()}


@pytest.fixture
def catalog(home, source):
    data = yaml.safe_load(home.config.read_text())
    data.pop("provider")
    data["providers"] = {
        "codex-jb": {"adapter": "codex", "home": str(home.root / "codex"), "model": "gpt-default"},
        "claude": {"adapter": "claude", "home": str(home.root / "claude")},
    }
    data["github"]["accounts"] = {"work": {"token": "fake-work-secret"}}
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.register("acme/other", str(source), "main")
    store.apply_models(
        {
            "valid": True,
            "checks": [],
            "providers": [
                {
                    "provider": "codex-jb",
                    "models": [
                        {
                            "model": "gpt-a",
                            "efforts": ["low", "high"],
                            "default_effort": "low",
                            "access": "verified",
                        },
                        {
                            "model": "gpt-b",
                            "efforts": ["medium", "high"],
                            "default_effort": "medium",
                            "access": "verified",
                        },
                        {"model": "blocked-model", "efforts": ["high"], "access": "unavailable"},
                    ],
                },
                {
                    "provider": "claude",
                    "models": [
                        {
                            "model": "claude-a",
                            "efforts": ["high"],
                            "default_effort": "high",
                            "access": "verified",
                        },
                    ],
                },
            ],
        },
        "fixture",
    )
    store.close()
    return home


@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("", {"config", "models", "repo", "scan", "completion"}),
        ("config ", {"init", "apply"}),
        ("repo ", {"add", "change", "list"}),
        ("models ", {"list"}),
        ("completion ", {"bash"}),
        ("scan --stage ", {"hunt", "validate", "audit", "fix", "review", "integrate"}),
        ("scan --stage r", {"review"}),
        ("scan --stage hunt-r", {"hunt-review"}),
        ("scan --stage fix-", {"fix-", "fix-review", "fix-integrate"}),
        ("scan --stage=a", {"audit"}),
        ("scan --priority ", {"P0", "P1", "P2", "P3"}),
        ("scan --st", {"--stage"}),
    ],
)
def test_static_completion_without_configuration(directory, suffix, expected):
    values = complete(directory, f"maxwell --home {directory / 'missing'} {suffix}")
    assert expected <= values
    assert not (directory / "missing").exists()


@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("scan --provider ", {"codex-jb", "claude"}),
        ("models list --provider cod", {"codex-jb"}),
        ("scan --provider codex-jb --model gpt-", {"gpt-a", "gpt-b", "gpt-default"}),
        ("scan --provider=claude --model ", {"claude-a"}),
        ("scan --model ", {"gpt-a", "gpt-b", "gpt-default", "claude-a"}),
        ("scan --provider codex-jb --model gpt-a --effort ", {"default", "low", "high"}),
        ("scan --provider codex-jb --model gpt-b --effort ", {"default", "medium", "high"}),
        ("scan --provider missing --model gpt", set()),
        ("scan ac", {"acme/repo", "acme/other"}),
        ("repo change ac", {"acme/repo", "acme/other"}),
        ("repo add acme/new --account ", {"default", "work"}),
    ],
)
def test_dynamic_completion_is_local_and_contextual(directory, catalog, github, suffix, expected):
    before = catalog.db.read_bytes()
    assert complete(directory, f"maxwell --home {catalog.root} {suffix}") == expected
    assert catalog.db.read_bytes() == before
    assert github["requests"] == []
    assert not (catalog.root / "codex").exists() and not (catalog.root / "claude").exists()


def test_configuration_model_restrictions_and_efforts_override_cached_metadata(directory, catalog):
    config = yaml.safe_load(catalog.config.read_text())
    config["providers"]["codex-jb"]["models"] = {
        "gpt-a": {"efforts": ["high"]},
        "gpt-custom": {"efforts": []},
    }
    catalog.config.write_text(yaml.safe_dump(config))
    command = f"maxwell --home {catalog.root} scan --provider codex-jb"
    assert complete(directory, command + " --model ") == {"gpt-a", "gpt-custom"}
    assert complete(directory, command + " --model gpt-a --effort ") == {"default", "high"}
    assert complete(directory, command + " --model gpt-custom --effort ") == {"default"}


def test_configured_models_complete_before_first_apply(directory, home):
    config = yaml.safe_load(home.config.read_text())
    config["provider"]["models"] = {"custom-model": {"efforts": ["high"]}}
    home.config.write_text(yaml.safe_dump(config))
    command = f"maxwell --home {home.root} scan"
    assert complete(directory, command + " --provider ") == {"default"}
    assert complete(directory, command + " --model ") == {"custom-model"}
    assert complete(directory, command + " --model custom-model --effort ") == {"default", "high"}
    assert not home.db.exists()


def test_missing_or_invalid_config_does_not_leak_diagnostics(directory, home):
    home.config.write_text("provider: [fake-secret-value")
    command = f"maxwell --home {home.root} scan"
    assert complete(directory, command + " --provider ") == set()
    assert complete(directory, command + " --stage ") == {
        "hunt",
        "validate",
        "audit",
        "fix",
        "review",
        "integrate",
    }
    assert not home.db.exists()


def test_quoted_home_path_is_used_for_dynamic_completion(directory, catalog):
    from maxwell.config import Paths

    paths = Paths(directory / "home with spaces")
    paths.root.mkdir()
    paths.config.write_bytes(catalog.config.read_bytes())
    paths.config.chmod(0o600)
    assert complete(directory, f"maxwell --home '{paths.root}' scan --provider cod") == {"codex-jb"}
    assert not paths.db.exists()


def test_corrupt_catalog_is_ignored(directory, catalog):
    store = Store(catalog.db)
    with store.db:
        store.db.execute(
            "UPDATE model_catalogs SET data=?",
            (json.dumps({"provider": "codex-jb", "models": "bad"}),),
        )
    store.close()
    assert complete(
        directory, f"maxwell --home {catalog.root} scan --provider codex-jb --model "
    ) == {"gpt-default"}


@pytest.mark.parametrize("json_output", [False, True])
def test_completion_script_command_does_not_initialize_home(directory, json_output):
    home = directory / "missing"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "maxwell",
            "--home",
            str(home),
            "completion",
            "bash",
            *(["--json"] if json_output else []),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stderr == ""
    script = json.loads(result.stdout)["script"] if json_output else result.stdout
    assert script == bash_completion()
    assert not home.exists()
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, timeout=5)
    assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize("bash", sorted({"/bin/bash", shutil.which("bash")} - {None}))
def test_command_substitution_enables_completion_in_available_bash_versions(directory, bash):
    home = directory / "missing"
    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-c",
            """
eval "$("$1" -m maxwell --home "$2" completion bash)"
complete -p maxwell
COMP_LINE="maxwell scan --stage r"
COMP_POINT=${#COMP_LINE}
COMP_TYPE=9
_python_argcomplete maxwell
printf '%s\n' "${COMPREPLY[@]}"
""",
            "bash",
            sys.executable,
            str(home),
        ],
        env={
            **os.environ,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert "-F _python_argcomplete maxwell" in result.stdout
    assert "review" in result.stdout
    assert not home.exists()


def test_closed_stdout_pipe_exits_without_shutdown_warning():
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "maxwell", "completion", "bash"],
            stdout=writer,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    finally:
        os.close(writer)
    assert result.returncode == 1 and result.stderr == ""
