import json

import pytest

from maxwell.cli import execute, parser
from maxwell.config import Limits, MeasurementSettings, Paths
from maxwell.hunt_context import measurement_context, previous_hunt, revision_changes
from maxwell.measurement import ANALYZER, POLICY, digest, measurement_policy
from maxwell.records import ExecutionRecord, MeasurementFactor, MeasurementRecord, PublicAgent
from maxwell.reporting import record_report
from maxwell.repositories import git
from maxwell.store import Store


def execution(sha, number=1, **changes):
    return ExecutionRecord(
        **{
            "id": f"{number:032x}",
            "repository": "acme/repo",
            "sha": sha,
            "agent": PublicAgent(adapter="codex", model="hunter", role="hunter"),
            "state": "completed",
            "summary": "Repository inspected",
            "finished": number,
            "accounting": "tokens",
            **changes,
        }
    )


def measurement(sha, settings=None, number=10, **changes):
    policy = measurement_policy(settings or MeasurementSettings(), "acme/repo")
    return MeasurementRecord(
        **{
            "id": f"{number:032x}",
            "repository": "acme/repo",
            "sha": sha,
            "analyzer_version": ANALYZER,
            "policy_version": POLICY,
            "protocol": digest(policy),
            "policy": policy,
            "measured_at": number,
            "coverage": 0.6,
            "factors": {
                name: MeasurementFactor(numerator=1, denominator=10, unit="ratio")
                for name in ("complexity", "duplication", "coupling")
            },
            "factor_status": {
                "complexity": "measured",
                "duplication": "measured",
                "coupling": "measured",
                "test_runtime": "No configured test profile",
                "test_flakiness": "No configured test profile",
            },
            "evidence": {
                "source": {
                    "complex_functions": [
                        {"path": "app.py", "line": 1, "symbol": "average", "complexity": 15}
                    ],
                    "clone_examples": [
                        {"first": "app.py", "first_line": 1, "second": "other.py", "second_line": 2}
                    ],
                    "cycles": [["app", "other"]],
                    "eligible_files": 2,
                    "analyzed_files": 2,
                }
            },
            **changes,
        }
    )


def project(store, record):
    store.project_record(record, 1, int(record.id, 16), "maintainer", record.id)


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
        "Change fixture",
        cwd=source,
    )
    return git("rev-parse", "HEAD", cwd=source)


@pytest.fixture
def store(home, source):
    value = Store(home.db)
    value.register("acme/repo", str(source), "main")
    yield value
    value.close()


def test_previous_hunt_uses_only_current_successful_hunter_records(store, source):
    sha = git("rev-parse", "HEAD", cwd=source)
    eligible = execution(
        sha, number=2, agent=PublicAgent(adapter="claude", model="other-hunter", role="hunter")
    )
    records = [
        execution(sha),
        eligible,
        execution(sha, number=3, state="failed"),
        execution(
            sha, number=4, agent=PublicAgent(adapter="codex", model="reviewer", role="reviewer")
        ),
        execution(sha, number=5, agent=None),
        execution(None, number=6),
    ]
    for record in records:
        project(store, record)
    assert previous_hunt(store, "acme/repo") == eligible
    assert previous_hunt(store, "acme/other") is None
    with store.db:
        store.db.execute("UPDATE github_records SET status='missing' WHERE id=?", (eligible.id,))
    assert previous_hunt(store, "acme/repo").id == records[0].id


def test_latest_applicable_measurement_includes_baseline_hotspots_and_provenance(store, source):
    sha = git("rev-parse", "HEAD", cwd=source)
    baseline = measurement(sha)
    current = measurement(
        sha,
        number=11,
        evidence={
            **baseline.evidence,
            "tests": {
                "slow_tests": [{"test": "test_app.py::test_average", "median_seconds": 4}],
                "observed_flaky_tests": ["test_app.py::test_random"],
                "persistent_failures": ["test_app.py::test_empty"],
            },
            "reused": {"source": {"id": baseline.id, "sha": "e" * 40, "measured_at": 5}},
        },
    )
    for record in [
        baseline,
        current,
        measurement(sha, number=12, policy_version="other"),
        measurement(sha, number=13, protocol="invalid"),
    ]:
        project(store, record)
    result = measurement_context(store, "acme/repo", sha, MeasurementSettings())
    assert result["id"] == current.id and result["baseline"]["id"] == baseline.id
    assert result["at_current_revision"]
    assert not result["stage_provenance"]["source"]["at_current_revision"]
    assert result["stage_provenance"]["source"]["measured_at"] == 5
    assert result["stage_provenance"]["source"]["age_seconds"] > 0
    assert result["hotspots"]["complex_functions"][0]["symbol"] == "average"
    assert result["hotspots"]["import_cycles"] == [["app", "other"]]
    assert result["hotspots"]["slow_tests"][0]["median_seconds"] == 4
    assert result["hotspots"]["observed_flaky_tests"] == ["test_app.py::test_random"]
    assert result["score"] is None
    assert result["factors"]["test_runtime"]["raw"] is None


@pytest.mark.parametrize("status", ["changed", "missing"])
def test_unavailable_or_incompatible_measurements_stay_unknown(store, source, status):
    sha = git("rev-parse", "HEAD", cwd=source)
    record = measurement(sha)
    project(store, record)
    assert not measurement_context(
        store, "acme/repo", sha, MeasurementSettings(complexity_threshold=20)
    )["available"]
    with store.db:
        store.db.execute("UPDATE github_records SET status=?", (status,))
    result = measurement_context(store, "acme/repo", sha, MeasurementSettings())
    assert not result["available"] and "No published measurement" in result["reason"]


def test_malformed_optional_hotspots_remain_unknown_without_blocking_context(store, source):
    sha = git("rev-parse", "HEAD", cwd=source)
    project(store, measurement(sha, evidence={"source": {"complex_functions": None}}))
    result = measurement_context(store, "acme/repo", sha, MeasurementSettings())
    assert result["available"] and result["hotspots"]["complex_functions"] is None


def test_first_and_unchanged_hunts_have_explicit_change_context(source):
    sha = git("rev-parse", "HEAD", cwd=source)
    first = revision_changes(source, sha, None, Limits())
    assert not first["available"] and "First hunt" in first["reason"]
    same = revision_changes(source, sha, execution(sha), Limits())
    assert same["available"] and same["changed_files"] == [] and same["diff"] == ""


def test_diff_includes_deletions_additions_and_explicit_truncation(source):
    base = git("rev-parse", "HEAD", cwd=source)
    (source / "app.py").unlink()
    (source / "new file.py").write_text("VALUE = 1\n")
    head = commit(source)
    complete = revision_changes(source, head, execution(base), Limits())
    assert complete["available"] and complete["total_changed_files"] == 2
    assert complete["changed_files"] == [
        {"status": "D", "path": "app.py"},
        {"status": "A", "path": "new file.py"},
    ]
    assert "-def average" in complete["diff"] and "+VALUE = 1" in complete["diff"]
    limited = revision_changes(
        source, head, execution(base), Limits(max_hunt_diff_chars=10, max_hunt_changed_files=1)
    )
    assert limited["diff_truncated"] and len(limited["diff"]) == 10
    assert limited["diff_chars"] == len(complete["diff"])
    assert limited["shown_changed_files"] == 1 and limited["total_changed_files"] == 2


def test_missing_or_divergent_previous_revision_does_not_block_hunting(source):
    base = git("rev-parse", "HEAD", cwd=source)
    missing = revision_changes(source, base, execution("f" * 40), Limits())
    assert not missing["available"] and "unavailable" in missing["reason"]
    (source / "old.py").write_text("OLD = 1\n")
    prior = commit(source)
    git("switch", "-c", "alternate", base, cwd=source)
    (source / "new.py").write_text("NEW = 1\n")
    head = commit(source)
    divergent = revision_changes(source, head, execution(prior), Limits())
    assert not divergent["available"] and "not an ancestor" in divergent["reason"]


def test_git_comparison_disables_external_diff_and_textconv(source):
    base = git("rev-parse", "HEAD", cwd=source)
    marker = source / "executed"
    git("config", "diff.external", f"touch {marker}", cwd=source)
    git("config", "diff.hostile.textconv", f"touch {marker}", cwd=source)
    (source / ".gitattributes").write_text("*.py diff=hostile\n")
    (source / "app.py").write_text("VALUE = 1\n")
    head = commit(source)
    result = revision_changes(source, head, execution(base), Limits())
    assert result["available"] and "+VALUE = 1" in result["diff"]
    assert not marker.exists()


def scan(home, capsys):
    execute(
        parser().parse_args(
            [
                "--home",
                str(home.root),
                "scan",
                "acme/repo",
                "--stage",
                "hunt",
                "--model",
                "fake-model",
                "--json",
            ]
        )
    )
    return json.loads(capsys.readouterr().out)["scans"][0]


def test_hunt_context_reconstructs_from_github_in_a_fresh_home(
    home, source, github, directory, capsys
):
    original = Store(home.db)
    original.register("acme/repo", str(source), "main")
    original.close()
    first = scan(home, capsys)
    artifact = home.root / "artifacts" / first["id"]
    context = json.loads((artifact / "hunt-context.json").read_text())
    assert not context["measurement"]["available"]
    assert not context["changes_since_previous_hunt"]["available"]
    dashboard = next(
        issue
        for issue in github["issues"].values()
        if "<!-- maxwell:dashboard:v1 -->" in issue["body"]
    )
    record = measurement(first["sha"], home.load().measurement)
    github["comments"].setdefault(dashboard["number"], []).append(
        {"id": 9999, "body": record_report(record), "user": {"login": "maintainer"}}
    )
    (source / "app.py").write_text(
        "def average(values):\n    return sum(values) / len(values)\n\ndef introduced():\n    return 1 / 0\n"
    )
    head = commit(source)
    fresh = Paths(directory / "fresh")
    fresh.initialize()
    fresh.config.write_text(home.config.read_text())
    fresh_store = Store(fresh.db)
    fresh_store.register("acme/repo", str(source), "main")
    fresh_store.close()
    second = scan(fresh, capsys)
    artifact = fresh.root / "artifacts" / second["id"]
    context = json.loads((artifact / "hunt-context.json").read_text())
    assert context["measurement"]["id"] == record.id
    assert not context["measurement"]["at_current_revision"]
    changes = context["changes_since_previous_hunt"]
    assert changes["available"] and changes["current_sha"] == head
    assert changes["previous_hunt"]["id"] == first["id"]
    assert "+    return 1 / 0" in changes["diff"]
    prompt = (artifact / "prompt.txt").read_text()
    assert "introduced bugs and regressions" in prompt
    assert "never restricts the whole-repository search" in prompt
    assert "Empty input causes division by zero" in prompt
    assert "average" in prompt and record.id in prompt
    assert "fake-github-secret" not in prompt and "fake-provider-secret" not in prompt


def test_auto_test_policy_and_architecture_hotspots_reach_hunters(store, source):
    import sys

    from maxwell.config import MeasurementTests

    settings = MeasurementSettings(
        tests={
            "acme/repo": MeasurementTests(
                command=[sys.executable, "-m", "pytest", "-q"],
                runs=2,
                warmups=0,
                runtime_budget_seconds=60,
            )
        }
    )
    sha = git("rev-parse", "HEAD", cwd=source)
    record = measurement(
        sha,
        settings=settings,
        evidence={
            "source": {
                "architecture": {
                    "coupling_hotspots": [{"module": "app", "reachable_dependencies": 3}],
                    "cohesion_hotspots": [{"symbol": "Mixed", "lack_of_cohesion": 1}],
                }
            }
        },
    )
    project(store, record)
    context = measurement_context(store, "acme/repo", sha, MeasurementSettings())
    assert context["available"]
    assert context["hotspots"]["coupling_hotspots"][0]["reachable_dependencies"] == 3
    assert context["hotspots"]["cohesion_hotspots"][0]["symbol"] == "Mixed"
