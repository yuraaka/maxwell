import asyncio
import json
from types import SimpleNamespace

import pytest

from maxwell.cli import parser
from maxwell.config import Config
from maxwell.manual import run_scans
from maxwell.records import public_agent
from maxwell.stages import AGENT_STAGES, agent_name, select_stages, stored_stage
from maxwell.store import Store


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, AGENT_STAGES),
        ("hunt", ("hunt",)),
        ("hunt-review", AGENT_STAGES[:-1]),
        ("hunt-", AGENT_STAGES),
        ("fix-", ("fix", "review", "integrate")),
        ("review-review", ("review",)),
        ("repair", ("fix",)),
        ("repair-review", ("fix", "review")),
    ],
)
def test_stage_selection(value, expected):
    assert select_stages(value) == expected


@pytest.mark.parametrize("value", ["review-hunt", "hunt-nope", "-review", "hunt--", "Hunt"])
def test_invalid_intervals_fail_before_configuration_or_execution(value):
    with pytest.raises(SystemExit):
        parser().parse_args(["scan", "--stage", value])


def test_config_uses_canonical_names_without_swapping_pool_meanings():
    config = Config.model_validate(
        {
            "version": 2,
            "pipeline": {"pools": {"validate": 7, "review": 2, "fix": 4}},
            "limits": {"max_validations_per_scan": 8},
            "fix": {"auto_merge": {"enabled": True, "required_reviews": 1}},
        }
    )
    assert config.pipeline.pools.validation == 7
    assert config.pipeline.pools.review == 2
    assert config.pipeline.pools.fix == 4
    assert config.limits.max_validations_per_scan == 8
    assert config.fix.auto_merge.enabled
    assert config.fix.auto_merge.required_reviews == 1
    assert Config.model_validate(config.model_dump(by_alias=True)) == config


@pytest.mark.parametrize(
    "body, expected",
    [
        ("version: 1", "version: Set version: 2"),
        ("pipeline: {pools: {review: 7, validate: 2}}", "version: Set version: 2"),
        ("", "version: Set version: 2"),
        ("version: 2\nrepair: {}", "repair: Obsolete key; use fix"),
        (
            "version: 2\nlimits: {max_reviews_per_scan: 8}",
            "limits.max_reviews_per_scan: Obsolete key; use limits.max_validations_per_scan",
        ),
        (
            "version: 2\nfix: {auto_merge: {required_validations: 1}}",
            "fix.auto_merge.required_validations: Obsolete key; use fix.auto_merge.required_reviews",
        ),
        (
            "version: 2\npipeline: {pools: {repair: 4}}",
            "pipeline.pools.repair: Obsolete key; use pipeline.pools.fix",
        ),
    ],
)
def test_legacy_config_is_rejected_with_migration_diagnostics(home, body, expected):
    home.config.write_text(body)
    with pytest.raises(ValueError) as error:
        home.load(detailed=True)
    assert expected in str(error.value)
    assert home.config.read_text() == body


@pytest.mark.parametrize("key", AGENT_STAGES)
def test_pool_errors_name_the_config_key(home, key):
    home.config.write_text(f"version: 2\npipeline:\n  pools:\n    {key}: -1\n")
    with pytest.raises(ValueError, match=rf"pipeline.pools.{key}: Input should be greater than"):
        home.load(detailed=True)


@pytest.mark.parametrize(
    "stage, role",
    [
        ("hunt", "hunter"),
        ("validate", "reviewer"),
        ("audit", "auditor"),
        ("fix", "fixer"),
        ("review", "validator"),
        ("integrate", "validator"),
    ],
)
def test_new_stage_names_preserve_published_agent_roles(stage, role):
    agent = public_agent(
        {
            "stage": stored_stage(stage),
            "execution": json.dumps({"settings": {"adapter": "codex"}}),
            "model": "model",
        }
    )
    assert agent.role == role
    assert agent_name(role, stage=stage) == f"{stage} agent"


def test_existing_pipeline_attempts_keep_their_cooldown(home):
    from maxwell.pipeline import AttemptLedger

    store = Store(home.db)
    try:
        store.register("acme/repo", "/tmp/source", "main")
        ledger = AttemptLedger(store, home.load(), True)
        for canonical, legacy in (
            ("hunt", "hunt"),
            ("validate", "review"),
            ("review", "validate"),
            ("fix", "repair"),
        ):
            store.db.execute(
                "INSERT INTO pipeline_attempts VALUES(?,?,?,?,?,?,?)",
                ("acme/repo", legacy, "finding", "same", 0, "completed", None),
            )
            if canonical != "hunt":
                assert "waiting for new evidence" in ledger.blocker(
                    "acme/repo", canonical, "finding", "same"
                )
        ledger.save("acme/repo", "hunt", "finding", "same", "completed")
        assert "cooldown" in ledger.blocker("acme/repo", "hunt", "finding", "same")
        assert store.one("SELECT COUNT(*) AS count FROM pipeline_attempts")["count"] == 4
    finally:
        store.close()


@pytest.mark.parametrize(
    "selection", [None, "hunt", "hunt-review", "hunt-", "fix-", "validate-audit"]
)
def test_scan_executes_selected_stages_in_order(home, source, github, monkeypatch, selection):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    store.close()
    calls = []

    class Target:
        finding = SimpleNamespace(agent=SimpleNamespace(adapter="codex", model="different"))
        reviews = []

        def model_dump(self, **kwargs):
            return {"issue": 1}

    async def collect(*args, **kwargs):
        return [Target()]

    async def measure(*args, **kwargs):
        calls.append("measure")
        return {"state": "completed"}

    async def execute(*args, stage, **kwargs):
        calls.append(stage)
        return {"state": "completed", "stage": stage, "publication_errors": [], "validations": []}

    monkeypatch.setattr("maxwell.manual.collect_targets", collect)
    monkeypatch.setattr("maxwell.manual.collect_repair_targets", collect)
    monkeypatch.setattr("maxwell.manual.collect_validation_targets", collect)
    monkeypatch.setattr("maxwell.manual.measure_one", measure)
    monkeypatch.setattr("maxwell.manual.execute_job", execute)
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", model="fake-model", stage=selection)
    )
    assert result["valid"], result
    assert calls == (["measure"] if selection is None else []) + list(select_stages(selection))
