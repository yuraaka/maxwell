import asyncio
import hashlib
import json
import subprocess
import sys

import pytest
import yaml
from pydantic import ValidationError

from maxwell.cli import execute, parser
from maxwell.config import MeasurementSettings, MeasurementTests
from maxwell.github import GitHub
from maxwell.measurement import (
    ANALYZER,
    FACTORS,
    POLICY,
    compatible,
    digest,
    historical_stage,
    measurement_contributions,
    measurement_report,
    measurement_score,
    projected_measurements,
)
from maxwell.measurement_runner import recover_measurements, run_measurements
from maxwell.records import MeasurementFactor, MeasurementRecord, record_digest
from maxwell.reporting import Reporter
from maxwell.repositories import Repository, git
from maxwell.store import Store


def measured(**changes):
    policy = {"weights": dict.fromkeys(FACTORS, 0.2)}
    values = dict(
        id="a" * 32,
        repository="acme/repo",
        sha="f" * 40,
        analyzer_version=ANALYZER,
        policy_version=POLICY,
        factors={
            name: MeasurementFactor(numerator=0.5, denominator=1, unit="ratio") for name in FACTORS
        },
        coverage=1,
        factor_status=dict.fromkeys(FACTORS, "measured"),
        policy=policy,
        protocol=digest(policy),
        measured_at=1,
    )
    return MeasurementRecord(**{**values, **changes})


def test_equal_weights_unknowns_and_runtime_saturation():
    record = measured()
    assert measurement_score(record) == 50
    factors = dict(record.factors)
    factors["test_runtime"] = MeasurementFactor(numerator=20, denominator=1, unit="ratio")
    assert measurement_score(record.model_copy(update={"factors": factors})) == 60
    factors.pop("test_flakiness")
    assert measurement_score(record.model_copy(update={"factors": factors})) is None
    assert measurement_score(record.model_copy(update={"protocol": "wrong"})) is None
    assert "all five factors" in measurement_report(record.model_copy(update={"factors": factors}))


def test_baselines_require_matching_policy_environment_and_analyzer():
    baseline = measured(environment_fingerprint="same")
    assert compatible(baseline, measured(environment_fingerprint="same", sha="e" * 40))
    assert not compatible(baseline, measured(environment_fingerprint="other"))
    assert not compatible(baseline, measured(analyzer_version="other"))
    assert "Score change: +0.00" in measurement_report(baseline, baseline)


def test_legacy_measurement_digest_is_stable_and_not_a_full_baseline():
    record = MeasurementRecord(
        id="a" * 32,
        repository="acme/repo",
        sha="f" * 40,
        analyzer_version="legacy",
        policy_version="legacy",
        coverage=0.8,
        factors={"complexity": {"numerator": 1, "denominator": 2, "unit": "ratio"}},
    )
    old = record.model_dump(
        mode="json",
        exclude={
            "measured_at",
            "protocol",
            "policy",
            "factor_status",
            "evidence",
            "environment_fingerprint",
            "baseline_id",
        },
    )
    expected = hashlib.sha256(
        json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert record_digest(record) == expected and measurement_score(record) is None
    assert "Legacy reported coverage: 80%" in measurement_report(record)


@pytest.mark.parametrize(
    "weights",
    [
        {"complexity": 1},
        dict.fromkeys(FACTORS, 1),
        dict.fromkeys(FACTORS, float("nan")),
        {**dict.fromkeys(FACTORS, 0.2), "coupling": 0},
    ],
)
def test_invalid_weight_policies_are_rejected(weights):
    with pytest.raises(ValidationError):
        MeasurementSettings(weights=weights)


def configure(home, source, **profile):
    with StoreContext(home) as store:
        store.register("acme/repo", str(source), "main")
    values = yaml.safe_load(home.config.read_text())
    values["measurement"] = {"complexity_threshold": 1, "clone_tokens": 5, "clone_lines": 1}
    if profile:
        values["measurement"]["tests"] = {
            "acme/repo": {
                "command": [sys.executable, "-m", "pytest", "-q"],
                "runs": 2,
                "warmups": 0,
                "runtime_budget_seconds": 10,
                **profile,
            }
        }
    home.config.write_text(yaml.safe_dump(values))
    return home.load()


class StoreContext:
    def __init__(self, home):
        self.store = Store(home.db)

    def __enter__(self):
        return self.store

    def __exit__(self, *_):
        self.store.close()


def commit(source):
    git("add", ".", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Measurement fixture",
        cwd=source,
    )


def test_structural_measurement_publishes_without_a_model_and_reconstructs(
    home, source, github, capsys
):
    config = configure(home, source)
    execute(parser().parse_args(["--home", str(home.root), "measure", "acme/repo", "--json"]))
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] and result["measurements"][0]["score"] is None
    assert result["measurements"][0]["coverage"] == pytest.approx(0.6)
    dashboard = next(
        item for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    assert (
        "all five factors" in dashboard["body"]
        and "No configured test profile" in dashboard["body"]
    )
    with StoreContext(home) as store:
        original = projected_measurements(store, "acme/repo")
        assert len(original) == 1 and not store.rows("SELECT * FROM scans")
        assert store.one("SELECT state FROM measurements")["state"] == "completed"
    client, fresh = GitHub(config.github), Store(home.root / "fresh.sqlite")
    try:
        fresh.register("acme/repo", "unused", "main")
        asyncio.run(Reporter(fresh, client, config, home).project_repository_records("acme/repo"))
        assert projected_measurements(fresh, "acme/repo") == original
    finally:
        fresh.close()
        client.close()
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_repeated_pytest_measurement_and_compatible_baseline(home, source, github):
    (source / "test_app.py").write_text("def test_ok():\n    assert True\n")
    commit(source)
    config = configure(home, source, runs=2, warmups=1)
    results = asyncio.run(run_measurements(home, config, "acme/repo"))
    assert results["valid"]
    item = results["measurements"][0]
    assert item["score"] is not None, item
    assert item["coverage"] == pytest.approx(1)
    second = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert second["valid"]
    with StoreContext(home) as store:
        records = projected_measurements(store, "acme/repo")
        assert len(records) == 2 and records[1].baseline_id == records[0].id
        assert records[1].evidence["tests"]["valid_runs"] == 2
        assert records[1].evidence["tests"]["observed_flaky_count"] == 0
        assert not store.rows("SELECT * FROM scans")
    fresh, client = Store(home.root / "fresh-full.sqlite"), GitHub(config.github)
    try:
        fresh.register("acme/repo", "unused", "main")
        asyncio.run(Reporter(fresh, client, config, home).project_repository_records("acme/repo"))
        rebuilt = projected_measurements(fresh, "acme/repo")
        assert (
            rebuilt == records
            and measurement_score(rebuilt[-1]) == second["measurements"][0]["score"]
        )
    finally:
        client.close()
        fresh.close()
    dashboard = next(
        item for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    assert (
        "Compatible baseline" in dashboard["body"] and "Suite duration change" in dashboard["body"]
    )
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_skipped_stages_reuse_github_history_without_running_them(
    home, source, github, monkeypatch, capsys
):
    (source / "test_app.py").write_text("def test_ok():\n    assert True\n")
    commit(source)
    config = configure(home, source, runs=2)
    execute(parser().parse_args(["--home", str(home.root), "measure", "acme/repo", "--json"]))
    first = json.loads(capsys.readouterr().out)["measurements"][0]
    assert first["score"] is not None
    (source / "new.py").write_text("def added():\n    return 1\n")
    commit(source)

    def forbidden(*args, **kwargs):
        pytest.fail("A skipped stage must not execute")

    from maxwell.measurement_runner import python_measurement, run_test_measurement

    monkeypatch.setattr("maxwell.measurement_runner.python_measurement", forbidden)
    monkeypatch.setattr("maxwell.measurement_runner.run_test_measurement", forbidden)
    execute(
        parser().parse_args(
            [
                "--home",
                str(home.root),
                "measure",
                "acme/repo",
                "--skip-stage",
                "source",
                "--skip-stage",
                "tests",
                "--json",
            ]
        )
    )
    second = json.loads(capsys.readouterr().out)["measurements"][0]
    assert second["score"] == first["score"]
    assert second["sha"] != first["sha"]
    assert set(second["reused"]) == {"source", "tests"}
    assert all(origin["id"] == first["id"] for origin in second["reused"].values())
    with StoreContext(home) as store:
        records = projected_measurements(store, "acme/repo")
        assert records[-1].factors == records[0].factors
        report = measurement_report(records[-1])
        assert report.count("reused from history") == 5
        assert first["sha"] in report and "not rerun" in report
        store.db.execute("DELETE FROM github_records WHERE type='measurement'")
        store.db.commit()
    third = asyncio.run(
        run_measurements(home, config, "acme/repo", run_tests=False, skip_source=True)
    )
    assert third["valid"]
    assert third["measurements"][0]["score"] == first["score"]
    assert all(
        origin["id"] == first["id"] for origin in third["measurements"][0]["reused"].values()
    )
    monkeypatch.setattr("maxwell.measurement_runner.python_measurement", python_measurement)
    fourth = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=False))
    assert fourth["valid"] and fourth["measurements"][0]["score"] is not None
    assert set(fourth["measurements"][0]["reused"]) == {"tests"}
    with StoreContext(home) as store:
        latest = projected_measurements(store, "acme/repo")[-1]
        assert (
            latest.evidence["source"]["analyzed_files"]
            == records[0].evidence["source"]["analyzed_files"] + 1
        )
    monkeypatch.setattr("maxwell.measurement_runner.python_measurement", forbidden)
    monkeypatch.setattr("maxwell.measurement_runner.run_test_measurement", run_test_measurement)
    fifth = asyncio.run(run_measurements(home, config, "acme/repo", skip_source=True))
    assert fifth["valid"] and fifth["measurements"][0]["score"] is not None
    assert set(fifth["measurements"][0]["reused"]) == {"source"}


def test_skipped_stages_without_history_remain_unknown(home, source, github):
    config = configure(home, source)
    result = asyncio.run(
        run_measurements(home, config, "acme/repo", run_tests=False, skip_source=True)
    )
    item = result["measurements"][0]
    assert result["valid"] and item["coverage"] == 0 and item["score"] is None
    assert not item["reused"]
    assert all(
        "no compatible GitHub history" in reason for reason in item["factor_status"].values()
    )


def test_historical_stage_checks_policy_and_prefers_latest_usable_evidence():
    old = measured()
    recent = measured(id="b" * 32, measured_at=2)
    invalid = measured(id="c" * 32, measured_at=3, protocol="invalid")
    assert historical_stage([old, recent, invalid], old.policy, FACTORS[3:]) == recent
    assert historical_stage([old], {**old.policy, "tests": {"runs": 6}}, FACTORS[3:]) is None
    assert historical_stage([old], {**old.policy, "tests": {"runs": 6}}, FACTORS[:3]) == old
    assert historical_stage([old], {**old.policy, "complexity_threshold": 2}, FACTORS[:3]) is None


def test_observed_flakes_persistent_failures_and_skips_are_distinct(home, source, github):
    counter = home.root / "counter"
    (source / "test_outcomes.py").write_text("""import os
from pathlib import Path
import pytest
def test_good(): assert True
def test_persistent(): assert False
def test_alternating():
    path = Path(os.environ['COUNTER'])
    number = int(path.read_text()) if path.exists() else 0
    path.write_text(str(number + 1))
    assert number % 2 == 0
def test_skipped(): pytest.skip('skip')
@pytest.mark.xfail
def test_expected(): assert False
""")
    commit(source)
    config = configure(home, source, runs=3, warmups=1, environment={"COUNTER": str(counter)})
    result = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert result["valid"]
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert record.factors["test_flakiness"].numerator == 1
        assert record.factors["test_flakiness"].denominator == 3
        assert record.evidence["tests"]["persistent_failure_count"] == 1
        assert record.evidence["tests"]["skipped_tests"] == 2
        assert record.evidence["tests"]["warmups"] == 1
    assert counter.read_text() == "4"


@pytest.mark.parametrize("mode", ["timeout", "source_change", "collection_error"])
def test_incomplete_test_evidence_is_unknown_and_snapshots_cleaned(home, source, github, mode):
    text = {
        "timeout": "import time\ndef test_wait(): time.sleep(20)\n",
        "source_change": "from pathlib import Path\ndef test_change(): Path('app.py').write_text('changed')\n",
        "collection_error": "raise RuntimeError('collection error')\n",
    }[mode]
    (source / "test_bad.py").write_text(text)
    commit(source)
    config = configure(home, source, timeout_seconds=0.8 if mode == "timeout" else 10)
    result = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert result["valid"] and result["measurements"][0]["score"] is None
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert "test_runtime" not in record.factors and "test_flakiness" not in record.factors
        assert record.evidence["tests"]["invalid_runs"]
    assert (source / "app.py").read_text().startswith("def average")
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_test_profile_paths_and_no_population_are_not_zero():
    with pytest.raises(ValidationError):
        MeasurementTests(command=["pytest"], cwd="../escape")
    with pytest.raises(ValidationError):
        MeasurementTests(command=["pytest"], runs=1)
    assert measurement_score(measured(factors={})) is None


def test_missing_runtime_budget_preserves_raw_duration(home, source, github):
    (source / "test_app.py").write_text("def test_ok(): assert True\n")
    commit(source)
    config = configure(home, source, runtime_budget_seconds=None)
    result = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert result["valid"] and result["measurements"][0]["score"] is None
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert (
            "test_runtime" not in record.factors and record.evidence["tests"]["median_seconds"] > 0
        )
        assert record.factors["test_flakiness"].numerator == 0


def test_publication_failure_preserves_measurement_and_retries(home, source, github, monkeypatch):
    config = configure(home, source)
    original = GitHub.request

    def fail(self, method, path, **kwargs):
        if method == "GET" and path.endswith("/issues"):
            raise ValueError("Temporary GitHub failure")
        return original(self, method, path, **kwargs)

    monkeypatch.setattr(GitHub, "request", fail)
    first = asyncio.run(run_measurements(home, config, "acme/repo"))
    assert not first["valid"]
    with StoreContext(home) as store:
        row = store.one("SELECT * FROM measurements")
        assert row["state"] == "completed" and row["data"]
        assert not store.rows("SELECT * FROM record_publications")
    monkeypatch.setattr(GitHub, "request", original)
    assert asyncio.run(run_measurements(home, config, "acme/repo"))["valid"]
    with StoreContext(home) as store:
        assert len(projected_measurements(store, "acme/repo")) == 2
        assert not store.rows("SELECT * FROM measurements WHERE error IS NOT NULL")


def test_cancellation_reaps_test_process_and_cleans_snapshots(home, source, github):
    pid_file = home.root / "child.pid"
    (source / "test_wait.py").write_text("""import os, subprocess, sys, time
from pathlib import Path
def test_wait():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    Path(os.environ['CHILD_PID']).write_text(str(child.pid))
    time.sleep(60)
""")
    commit(source)
    config = configure(home, source, environment={"CHILD_PID": str(pid_file)})

    async def cancel():
        task = asyncio.create_task(run_measurements(home, config, "acme/repo", run_tests=True))
        try:
            async with asyncio.timeout(10):
                while not pid_file.exists():
                    await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(cancel())
    pid = int(pid_file.read_text())
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z")
    with StoreContext(home) as store:
        assert store.one("SELECT state FROM measurements")["state"] == "cancelled"
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_runtime_protocol_rejects_changing_test_collection(home, source, github):
    counter = home.root / "collection.counter"
    (source / "test_collection.py").write_text("""import os
from pathlib import Path
path = Path(os.environ['COLLECTION_COUNTER'])
value = int(path.read_text()) if path.exists() else 0
path.write_text(str(value + 1))
def test_first(): assert True
if value % 2:
    def test_second(): assert True
""")
    commit(source)
    config = configure(home, source, environment={"COLLECTION_COUNTER": str(counter)})
    result = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert result["valid"] and result["measurements"][0]["score"] is None
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert "test_runtime" not in record.factors and "test_flakiness" not in record.factors


def test_measurement_does_not_inherit_provider_credentials(home, source, github, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-provider-key")
    (source / "test_env.py").write_text(
        "import os\ndef test_env():\n    assert 'GITHUB_TOKEN' not in os.environ\n    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
    )
    commit(source)
    config = configure(home, source, environment={"PRIVATE_TEST_KEY": "private-test-value"})
    result = asyncio.run(run_measurements(home, config, "acme/repo", run_tests=True))
    assert result["valid"]
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert record.evidence["tests"]["persistent_failure_count"] == 0
    for issue in github["issues"].values():
        assert "private-test-value" not in issue["body"]
    for comments in github["comments"].values():
        assert all("private-test-value" not in item["body"] for item in comments)


def test_pinned_measurement_leaves_checkout_and_untracked_work_untouched(home, source, github):
    config = configure(home, source)
    original = git("rev-parse", "HEAD", cwd=source)
    (source / "app.py").write_text("def changed(): return 1\n")
    commit(source)
    current = git("rev-parse", "HEAD", cwd=source)
    (source / "private.txt").write_text("untracked work")
    result = asyncio.run(run_measurements(home, config, "acme/repo", revision=original))
    assert result["valid"] and result["measurements"][0]["sha"] == original
    assert git("rev-parse", "HEAD", cwd=source) == current
    assert (source / "private.txt").read_text() == "untracked work"


def test_interrupted_measurement_recovery_preserves_agent_ledger(home, source):
    configure(home, source)
    with StoreContext(home) as store:
        identity = "b" * 32
        repository = Repository(home, store.repository("acme/repo"))
        _, worktree = repository.prepare(identity)
        store.db.execute(
            "INSERT INTO measurements(id,repository,state,created) VALUES(?,?,'running',1)",
            (identity, "acme/repo"),
        )
        store.db.commit()
        recover_measurements(store, home)
        assert not worktree.exists()
        assert store.one("SELECT state FROM measurements")["state"] == "interrupted"
        assert not store.rows("SELECT * FROM scans")


def test_pytest_imports_pinned_source_before_external_editable_checkout(home, source, github):
    package = source / "src" / "pinned_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "api.py").write_text("def value(): return 1\n")
    (source / "test_pinned.py").write_text(
        "from pinned_package.api import value\ndef test_pinned(): assert value() == 1\n"
    )
    commit(source)
    original = git("rev-parse", "HEAD", cwd=source)
    (package / "api.py").write_text("def value(): return 2\n")
    commit(source)
    config = configure(home, source, environment={"PYTHONPATH": str(source / "src")})
    result = asyncio.run(
        run_measurements(home, config, "acme/repo", revision=original, run_tests=True)
    )
    assert result["valid"]
    with StoreContext(home) as store:
        record = projected_measurements(store, "acme/repo")[0]
        assert record.evidence["tests"]["persistent_failure_count"] == 0
        assert record.sha == original


def test_partial_contributions_preserve_weights_and_unknown_total():
    record = measured()
    factors = dict(record.factors)
    factors.pop("test_flakiness")
    partial = record.model_copy(update={"factors": factors})
    contributions = measurement_contributions(partial)
    assert contributions == {name: 10 for name in FACTORS if name != "test_flakiness"}
    assert measurement_score(partial) is None
    assert measurement_contributions(partial.model_copy(update={"protocol": "wrong"})) is None


def test_existing_measurement_publication_stays_on_status_issue(home, source, github, monkeypatch):
    from maxwell.reporting import Reporter

    config = configure(home, source)

    async def legacy_history(reporter, repo):
        channel = await reporter.channel(
            repo, "dashboards", "<!-- maxwell:dashboard:v1 -->", "Maxwell status"
        )
        return channel["issue"]

    with monkeypatch.context() as patch:
        patch.setattr(Reporter, "history", legacy_history)
        first = asyncio.run(run_measurements(home, config, "acme/repo"))
    assert first["valid"]
    dashboard = next(
        item for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    original = list(github["comments"][dashboard["number"]])
    with StoreContext(home) as store:
        with store.db:
            store.db.execute("DELETE FROM record_publications WHERE type='measurement'")
    second = asyncio.run(run_measurements(home, config, "acme/repo"))
    assert second["valid"]
    assert github["comments"][dashboard["number"]] == original
    history = next(item for item in github["issues"].values() if item["title"] == "Maxwell history")
    assert len(github["comments"][history["number"]]) == 1
    with StoreContext(home) as store:
        assert len(projected_measurements(store, "acme/repo")) == 2
        assert (
            store.one(
                "SELECT issue FROM record_publications WHERE id=?",
                (first["measurements"][0]["id"],),
            )["issue"]
            == dashboard["number"]
        )
