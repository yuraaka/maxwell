import asyncio
import json
from pathlib import Path

import pytest
from test_repair import repairable as repairable_fixture
from test_repair import run

from maxwell.cli import parser
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.measurement import validation_changes
from maxwell.records import RECORD_ADAPTER, ValidationRecord, decode_record
from maxwell.reporting import Reporter, record_report
from maxwell.repositories import Repository, git
from maxwell.reputation import agent_reputation, selection_evidence
from maxwell.store import Store
from maxwell.validation import collect_validation_targets

_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)


@pytest.fixture
def prepared(home, repairable, executable, directory):
    result = run(home)
    assert result["valid"], json.dumps(result, indent=2)
    repair = result["scans"][0]["repairs"][0]
    script = executable.read_text().replace(
        "elif 'verdict' in json.loads",
        """elif 'regression_evidence' in json.loads(Path(args[args.index('--output-schema') + 1]).read_text())['properties']:
    assert args[args.index('--sandbox') + 1] == 'read-only'
    behavior = Path(__file__).parent / 'validation-mode'
    behavior = behavior.read_text() if behavior.exists() else 'accepted'
    if behavior == 'malformed':
        mode = 'malformed'
    if behavior == 'tamper':
        (workspace / 'app.py').write_text('changed source')
    result = {'verdict': 'not_repaired' if behavior == 'rejected' else 'inconclusive' if behavior == 'inconclusive' else 'repaired',
              'summary': 'Independent inspection of the empty input guard.',
              'evidence': 'The guard handles empty input while preserving the nonempty average; callers were inspected.',
              'reproduction': 'not_reproduced' if behavior == 'unrelated' else 'reproduced',
              'regression_evidence': 'test_empty fails in the original average with ZeroDivisionError and passes after the empty guard.',
              'concerns': []}
elif 'verdict' in json.loads""",
    )
    executable.write_text(script)
    yield repair


def validate(home, **kwargs):
    return asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="review", model="validator-model", **kwargs)
    )


def test_validation_notes_are_advisory_and_blockers_are_inconclusive(
    home, prepared, repairable, executable, directory
):
    from maxwell.contracts import ValidationResult
    from maxwell.reporting import record_report

    note = "repair metadata predates the passing host checks; the fix is unaffected."
    result = ValidationResult(
        verdict="repaired",
        summary="The empty-input guard fixes the defect.",
        evidence="app.py:2 returns zero for empty input and preserves nonempty averages.",
        reproduction="reproduced",
        regression_evidence="test_empty fails on the original source and passes after repair.",
        notes=[note],
    )
    assert result.notes == [note]
    executable.write_text(
        executable.read_text().replace("'concerns': []}", f"'concerns': [], 'notes': [{note!r}]}}")
    )
    run_result = validate(home)
    assert run_result["valid"], run_result
    record = RECORD_ADAPTER.validate_json(
        repairable[0].one("SELECT data FROM github_records WHERE type='validation'")["data"]
    )
    body = record_report(record)
    assert "### review agent · accepted" in body
    assert "**Note:** " + note in body
    assert "<details>" in body and body.index("<details>") < body.index("Execution:")
    import hashlib

    from maxwell.records import record_digest

    legacy = record.model_copy(update={"result": record.result.model_copy(update={"notes": []})})
    values = legacy.model_dump(mode="json", exclude={"merge_conflicts"})
    values.pop("spending", None)
    for name in ("before", "after"):
        if values.get(name):
            values[name].pop("spending", None)
    values["result"].pop("notes")
    values["result"].pop("comments")
    expected = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert record_digest(legacy) == expected
    executable.write_text(
        executable.read_text().replace(
            "'concerns': []", "'concerns': ['The zero return lacks a required API contract.']"
        )
    )
    forced = validate(home, force=True)
    assert forced["scans"][0]["validations"][0]["outcome"] == "inconclusive"


def test_integration_reports_awaiting_merge(home, prepared, repairable, github):
    diagnostics = []
    assert (
        asyncio.run(
            collect_validation_targets(
                repairable[2], "acme/repo", integrate=True, diagnostics=diagnostics
            )
        )
        == []
    )
    number = next(iter(github["pulls"]))
    assert any(f"PR #{number} awaiting merge" in reason for reason in diagnostics)
    report = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="integrate", model="validator-model")
    )
    assert report["valid"] and not report["scans"]
    assert f"PR #{number} awaiting merge" in report["skipped"][0]["reason"]


def test_validation_end_to_end_local_snapshots_and_repeat(
    home, source, repairable, prepared, github
):
    store, config, reporter, finding, review, issue = repairable
    retained = Path(prepared["worktree"])
    original_files = {p.name: p.read_bytes() for p in retained.iterdir() if p.is_file()}
    result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "accepted" and record["state"] == "completed"
    assert record["repair_id"] == prepared["id"] and record["base_sha"] == finding.sha
    assert record["sha"] == prepared["commit"] and record["references"] == [review.id]
    assert record["checks"][0]["state"] == "passed"
    assert record["replay"]["returncode"] == 1 and record["failures"][0]["phase"] == "call"
    assert "ZeroDivisionError" in record["failures"][0]["detail"]
    assert record["before"]["sha"] == finding.sha and record["after"]["sha"] == record["tested_sha"]
    assert {p.name: p.read_bytes() for p in retained.iterdir() if p.is_file()} == original_files
    repo = Repository(home, store.repository("acme/repo"))
    assert list(repo.worktrees.iterdir()) == [retained]
    assert github["issues"][issue]["state"] == "open"
    assert not store.one("SELECT id FROM measurements")
    assert not store.one("SELECT id FROM github_records WHERE type='measurement'")
    published = next(
        decode_record(comment["body"])
        for comment in github["comments"][record["issue"]]
        if isinstance(decode_record(comment["body"]), ValidationRecord)
    )
    assert (
        validation_changes(published)["components"]
        and validation_changes(published)["reduction"] is None
    )
    dashboard = store.one("SELECT issue FROM dashboards")["issue"]
    assert not github["comments"].get(dashboard)
    assert "Independent repair validations" in github["issues"][dashboard]["body"]
    assert selection_evidence(store, "acme/repo", "fixer") == {
        ("codex", "fake-model", None): [1, 0]
    }
    assert selection_evidence(store, "acme/repo", "validator") == {}
    assert validate(home)["scans"] == []
    forced = validate(home, force=True)
    assert forced["valid"], forced
    assert forced["scans"][0]["validations"][0]["supersedes"] == [published.id]
    assert selection_evidence(store, "acme/repo", "fixer") == {
        ("codex", "fake-model", None): [1, 0]
    }


@pytest.mark.parametrize(
    "mode,outcome,state",
    [
        ("rejected", "rejected", "completed"),
        ("inconclusive", "inconclusive", "completed"),
        ("unrelated", "inconclusive", "completed"),
        ("malformed", "inconclusive", "failed"),
        ("tamper", "inconclusive", "failed"),
    ],
)
def test_verdicts_and_operational_failures(
    home, repairable, prepared, directory, mode, outcome, state
):
    (directory / "validation-mode").write_text(mode)
    result = validate(home)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == outcome and record["state"] == state, result
    store = repairable[0]
    assert selection_evidence(store, "acme/repo", "fixer") == (
        {("codex", "fake-model", None): [0, 1]} if outcome == "rejected" else {}
    )
    assert Path(prepared["worktree"]).exists()
    if state == "failed":
        assert not result["valid"]
        (directory / "validation-mode").unlink()
        retry = validate(home)
        assert retry["valid"], retry
        assert retry["scans"][0]["validations"][0]["outcome"] == "accepted"


def test_independence_rejects_same_public_model(home, prepared):
    with pytest.raises(ValueError, match="different public adapter/model"):
        asyncio.run(run_scans(home, home.load(), "acme/repo", stage="review", model="fake-model"))


def test_local_branch_movement_does_not_change_remote_pr_validation(home, prepared, repairable):
    store, config, reporter, finding, review, issue = repairable
    repository = Repository(home, store.repository("acme/repo"))
    git("-C", str(repository.mirror), "update-ref", "refs/heads/" + prepared["branch"], finding.sha)
    assert validate(home, issue=issue)["valid"]


def test_stale_and_closed_inputs_are_skipped(home, prepared, repairable, github):
    store, config, reporter, finding, review, issue = repairable
    changed = review.model_copy(update={"id": "c" * 32, "supersedes": review.id})
    reporter.github.create_comment("acme/repo", issue, record_report(changed))
    with pytest.raises(ValueError, match="stale"):
        validate(home, issue=issue)
    github["issues"][issue]["state"] = "closed"
    assert validate(home)["scans"] == []


def test_validation_provenance_and_fresh_projection(home, prepared, repairable, directory):
    assert validate(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    row = store.one("SELECT data FROM github_records WHERE type='validation'")
    record = RECORD_ADAPTER.validate_json(row["data"])
    for changes in (
        {"sha": "f" * 40},
        {"agent": prepared["agent"] | {"role": "validator"}},
        {"repair_id": "f" * 32},
    ):
        with pytest.raises(ValueError, match="repair|agent|commits|revision"):
            wrong = ValidationRecord.model_validate(
                record.model_dump() | {"id": "e" * 32, **changes}
            )
            store.project_record(
                wrong,
                record.pr_id
                and store.one("SELECT issue FROM github_records WHERE type='validation'")["issue"],
                9999,
                "maintainer",
                "changed",
            )
    fresh = Store(directory / "fresh.sqlite")
    fresh.register("acme/repo", str(directory / "source"), "main")
    client = GitHub(config.github)
    try:
        projected = Reporter(fresh, client, config, home)
        assert asyncio.run(collect_validation_targets(projected, "acme/repo")) == []
        assert fresh.one("SELECT id FROM github_records WHERE type='validation'")["id"] == record.id
        assert selection_evidence(fresh, "acme/repo", "fixer") == {
            ("codex", "fake-model", None): [1, 0]
        }
    finally:
        fresh.close()
        client.close()


def test_recovery_preserves_repair_and_removes_validation_snapshots(home, prepared, repairable):
    assert validate(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    job = store.one("SELECT * FROM scans WHERE stage='validate'")
    artifact = Path(job["artifact"])
    manifest = json.loads((artifact / "validation.json").read_text())
    manifest["record"]["id"] = "e" * 32
    (artifact / "validation.json").write_text(json.dumps(manifest))
    repository = Repository(home, store.repository("acme/repo"))
    _, snapshot = repository.prepare_local(job["id"] + "-leftover", revision=prepared["commit"])
    with store.db:
        store.db.execute(
            "UPDATE scans SET state='running',finished=NULL,pid=NULL WHERE id=?", (job["id"],)
        )
    engine = Daemon(home, config)
    engine.store = store
    engine.recover()
    recovered = json.loads(
        store.one(
            "SELECT data FROM record_publications WHERE type='validation' AND id=?", ("e" * 32,)
        )["data"]
    )
    assert recovered["state"] == "interrupted" and recovered["outcome"] == "inconclusive"
    assert not snapshot.exists() and Path(prepared["worktree"]).exists()


def test_validate_cli_is_single_and_explicit(home):
    args = parser().parse_args(["scan", "--stage", "review", "--issue", "1", "acme/repo"])
    assert args.stage == "review"
    with pytest.raises(ValueError, match="one finding"):
        validate(home, limit=2)


def test_missing_runner_is_inconclusive(home, prepared, repairable):
    import yaml

    data = yaml.safe_load(home.config.read_text())
    data.pop("measurement")
    home.config.write_text(yaml.safe_dump(data))
    result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive"
    assert record["checks"][0]["state"] == "unavailable"
    assert record["replay"]["state"] == "unavailable"
    assert selection_evidence(repairable[0], "acme/repo", "fixer") == {}


def test_publication_retry_reuses_completed_validation(home, prepared, repairable, monkeypatch):
    original = Reporter.publish_records

    async def unavailable(self, repository=None):
        if self.store.one(
            "SELECT id FROM record_publications WHERE type='validation' AND state!='published'"
        ):
            raise ValueError("GitHub publication temporarily unavailable")
        return await original(self, repository)

    with monkeypatch.context() as patch:
        patch.setattr(Reporter, "publish_records", unavailable)
        result = validate(home)
    assert not result["valid"] and result["scans"][0]["state"] == "completed"
    store = repairable[0]
    assert (
        store.one("SELECT state FROM record_publications WHERE type='validation'")["state"]
        == "pending"
    )
    retry = validate(home)
    assert retry["valid"] and retry["scans"] == []
    assert store.one("SELECT COUNT(*) AS n FROM scans WHERE stage='validate'")["n"] == 1
    assert (
        store.one("SELECT state FROM record_publications WHERE type='validation'")["state"]
        == "published"
    )


def test_cancellation_preserves_repair_and_unrated_outcome(home, prepared, repairable, monkeypatch):
    from maxwell.provider import CodexProvider

    async def cancel_run():
        started = asyncio.Event()

        async def paused(self, *args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(CodexProvider, "validate", paused)
        task = asyncio.create_task(
            run_scans(home, home.load(), "acme/repo", stage="review", model="validator-model")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())
    store = repairable[0]
    public = json.loads(
        store.one("SELECT data FROM record_publications WHERE type='validation'")["data"]
    )
    assert public["state"] == "cancelled" and public["outcome"] == "inconclusive"
    repo = Repository(home, store.repository("acme/repo"))
    assert list(repo.worktrees.iterdir()) == [Path(prepared["worktree"])]
    assert selection_evidence(store, "acme/repo", "fixer") == {}


def test_conflicting_validation_quality_and_supersession(home, prepared, repairable):
    assert validate(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    accepted = RECORD_ADAPTER.validate_json(
        store.one("SELECT data FROM github_records WHERE type='validation'")["data"]
    )
    rejected = accepted.model_copy(
        update={
            "id": "e" * 32,
            "outcome": "rejected",
            "result": accepted.result.model_copy(
                update={
                    "verdict": "not_repaired",
                    "evidence": "A concrete independent source rejection for a missed caller.",
                }
            ),
        }
    )
    reporter.github.create_comment(
        "acme/repo",
        store.one("SELECT issue FROM github_records WHERE type='validation'")["issue"],
        record_report(rejected),
    )
    asyncio.run(reporter.project_repository_records("acme/repo"))
    assert selection_evidence(store, "acme/repo", "fixer") == {}
    assert validate(home)["scans"] == []
    resolved = validate(home, force=True)
    assert resolved["valid"], resolved
    assert set(resolved["scans"][0]["validations"][0]["supersedes"]) == {accepted.id, rejected.id}
    assert selection_evidence(store, "acme/repo", "fixer") == {
        ("codex", "fake-model", None): [1, 0]
    }


def test_component_comparison_preserves_source_when_test_environment_changes(
    home, prepared, repairable
):
    assert validate(home)["valid"]
    record = RECORD_ADAPTER.validate_json(
        repairable[0].one("SELECT data FROM github_records WHERE type='validation'")["data"]
    )
    after = record.after
    before = after.model_copy(
        update={"sha": record.base_sha, "environment_fingerprint": "other-environment"}
    )
    comparison = record.model_copy(update={"before": before})
    changes = validation_changes(comparison)
    assert set(changes["components"]) == {"complexity", "duplication", "coupling"}
    assert changes["reduction"] is None
    reputation = agent_reputation(repairable[0], "acme/repo")
    validator = next(row for row in reputation if row["role"] == "validator")
    assert validator["validation_accepted"] == 1 and validator["executions"] == 1


@pytest.mark.parametrize("failure", ["collection", "setup"])
def test_original_collection_and_setup_failures_are_not_reproduction(
    home, prepared, repairable, github, failure
):
    store, config, reporter, finding, review, issue = repairable
    retained = Path(prepared["worktree"])
    if failure == "collection":
        with (retained / "app.py").open("a") as stream:
            stream.write("\ndef empty_default():\n    return 0\n")
        (retained / "test_app.py").write_text(
            "from app import average, empty_default\ndef test_empty():\n    assert average([]) == empty_default()\n"
        )
    else:
        (retained / "test_app.py").write_text(
            "import pytest\nfrom app import average\n@pytest.fixture\ndef empty():\n    return average([])\ndef test_empty(empty):\n    assert empty == 0\n"
        )
    git("add", "app.py", "test_app.py", cwd=retained)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--amend",
        "--no-edit",
        cwd=retained,
    )
    changed = RECORD_ADAPTER.validate_python(
        {key: value for key, value in prepared.items() if key not in ("issue", "publication_state")}
    ).model_copy(update={"id": "e" * 32, "commit": git("rev-parse", "HEAD", cwd=retained)})
    pull = next(iter(github["pulls"].values()))
    pull["head"]["sha"] = changed.commit
    github["branches"][pull["head"]["ref"]] = changed.commit
    result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive" and record["replay"]["state"] == "unavailable"
    assert all(check["state"] == "passed" for check in record["checks"])
    assert selection_evidence(store, "acme/repo", "fixer") == {}


def test_public_worktree_path_is_never_used(home, prepared, repairable):
    store, config, reporter, finding, review, issue = repairable
    repair = RECORD_ADAPTER.validate_python(
        {key: value for key, value in prepared.items() if key not in ("issue", "publication_state")}
    )
    hostile = repair.model_copy(update={"id": "e" * 32, "worktree": "/outside/untrusted/path"})
    assert hostile.worktree == "/outside/untrusted/path"
    result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    assert result["scans"][0]["validations"][0]["repair_id"] == prepared["id"]
    assert Path(prepared["worktree"]).exists()


def test_passing_suite_does_not_accept_incomplete_repair(
    home, prepared, repairable, directory, github
):
    store, config, reporter, finding, review, issue = repairable
    retained = Path(prepared["worktree"])
    (retained / "app.py").write_text("def average(values):\n    return 0\n")
    git("add", "app.py", cwd=retained)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--amend",
        "--no-edit",
        cwd=retained,
    )
    repair = RECORD_ADAPTER.validate_python(
        {key: value for key, value in prepared.items() if key not in ("issue", "publication_state")}
    ).model_copy(update={"id": "e" * 32, "commit": git("rev-parse", "HEAD", cwd=retained)})
    pull = next(iter(github["pulls"].values()))
    pull["head"]["sha"] = repair.commit
    github["branches"][pull["head"]["ref"]] = repair.commit
    (directory / "validation-mode").write_text("rejected")
    result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "rejected"
    assert all(check["state"] == "passed" for check in record["checks"])
    assert record["replay"]["state"] == "failed"
    assert selection_evidence(store, "acme/repo", "fixer") == {}


def test_stale_inputs_after_investigation_are_not_quality_observations(
    home, prepared, repairable, monkeypatch
):
    import maxwell.validation as validation

    store, config, reporter, finding, review, issue = repairable
    original = validation.compute_measurement

    async def changed_inputs(*args, **kwargs):
        measurement = await original(*args, **kwargs)
        if str(args[1]).endswith("-after"):
            reporter.github.create_comment(
                "acme/repo",
                issue,
                record_report(review.model_copy(update={"id": "e" * 32, "supersedes": review.id})),
            )
        return measurement

    monkeypatch.setattr(validation, "compute_measurement", changed_inputs)
    result = validate(home)
    assert not result["valid"]
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive" and record["state"] == "failed"
    assert record["result"]["verdict"] == "repaired"
    assert "stale" in result["scans"][0]["error"]
    assert selection_evidence(store, "acme/repo", "fixer") == {}


def test_automatic_validator_excludes_fixer_aliases(home, prepared, repairable):
    from maxwell.agent_selection import AgentSelector

    store, config, reporter, finding, review, issue = repairable
    selector = AgentSelector(home, config, model="fake-model")
    selector.explicit = False
    selector.add(config.route(None, "validator-model"))
    selector.add(config.route(None, "fake-model", effort="high"))
    route, decision = selector.select(store, "acme/repo", "review", avoid={("codex", "fake-model")})
    assert route["model"] == "validator-model"
    assert decision["method"] == "uniform-unrated-v1"
    assert decision["independence"] == "different_model" and decision["candidates"] == 1


def test_fixer_selection_uses_independent_validation_evidence(home, prepared, repairable):
    from maxwell.agent_selection import AgentSelector

    assert validate(home)["valid"]
    store = repairable[0]
    selector = AgentSelector(home, home.load(), model="fake-model")
    selector.explicit = False
    route, decision = selector.select(store, "acme/repo", "repair")
    assert route["model"] == "fake-model" and decision["method"] == "thompson-repair-v1"
    assert decision["successes"] == 1 and decision["failures"] == 0


def test_complete_comparable_measurements_report_total_delta(
    home, source, repairable, executable, github
):
    import yaml

    store, config, reporter, finding, review, issue = repairable
    (source / "test_nonempty.py").write_text(
        "from app import average\ndef test_nonempty():\n    assert average([2, 4]) == 3\n"
    )
    git("add", "test_nonempty.py", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "existing test suite",
        cwd=source,
    )
    sha = git("rev-parse", "HEAD", cwd=source)
    github["branches"]["main"] = sha
    github["issues"][issue]["body"] = github["issues"][issue]["body"].replace(finding.sha, sha)
    for comment in github["comments"][issue]:
        comment["body"] = comment["body"].replace(finding.sha, sha)
    data = yaml.safe_load(home.config.read_text())
    data["measurement"]["tests"]["acme/repo"].update(runs=2, warmups=0, runtime_budget_seconds=60)
    home.config.write_text(yaml.safe_dump(data))
    assert run(home)["valid"]
    from unittest.mock import patch

    from maxwell.contracts import ValidationResult
    from maxwell.provider import CodexProvider

    async def independent(self, *args):
        return ValidationResult(
            verdict="repaired",
            summary="Correct empty guard and unchanged nonempty average.",
            evidence="Source and callers preserve the existing nonempty average while handling the empty input.",
            reproduction="reproduced",
            regression_evidence="The base test_empty call fails in average with ZeroDivisionError; repaired regression tests pass.",
        )

    with patch.object(CodexProvider, "validate", independent):
        result = validate(home)
    assert result["valid"], json.dumps(result, indent=2)
    record = RECORD_ADAPTER.validate_json(
        store.one("SELECT data FROM github_records WHERE type='validation'")["data"]
    )
    assert record.outcome == "accepted"
    assert set(validation_changes(record)["components"]) == {
        "complexity",
        "duplication",
        "coupling",
        "test_runtime",
        "test_flakiness",
    }
    assert validation_changes(record)["reduction"] is not None
    assert record.before.evidence["tests"]["collected_tests"] == 1
    assert record.after.evidence["tests"]["collected_tests"] == 2
    assert not store.one("SELECT id FROM measurements")
