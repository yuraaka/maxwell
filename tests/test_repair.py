import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from maxwell.cli import parser
from maxwell.config import MeasurementTests
from maxwell.contracts import Finding
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.provider import ClaudeProvider, CodexProvider
from maxwell.records import (
    AssessmentRecord,
    FindingRecord,
    PublicAgent,
    RepairRecord,
    decode_record,
    encode_record,
)
from maxwell.repair import collect_repair_targets, run_checks
from maxwell.reporting import Reporter, record_report
from maxwell.repositories import Repository, git
from maxwell.store import Store


@pytest.fixture
def repairable(home, source, finding, executable, github, monkeypatch):
    github["branches"]["main"] = git("rev-parse", "HEAD", cwd=source)

    def push(repository, branch, commit, token, expected=None):
        actual = github["branches"].get(branch)
        if actual == commit:
            return
        if actual != expected:
            raise ValueError("Remote repair branch changed; refusing to overwrite it")
        github["branches"][branch] = commit
        for pull in github["pulls"].values():
            if pull["head"]["ref"] == branch:
                pull["head"]["sha"] = commit

    def fetch(repository, number, head, base_branch, base_sha, token):
        repository.worktrees.mkdir(parents=True, exist_ok=True)
        if not repository.mirror.exists():
            git("init", "--bare", str(repository.mirror))
        git("-C", str(repository.mirror), "fetch", "--no-tags", str(source), "main")
        pull = github["pulls"][number]
        if pull["head"]["sha"] != head or pull["base"]["sha"] != base_sha:
            raise ValueError("PR moved while fetching")
        git("-C", str(repository.mirror), "cat-file", "-e", head)
        git("-C", str(repository.mirror), "cat-file", "-e", base_sha)

    def fetch_revision(repository, sha, token):
        repository.worktrees.mkdir(parents=True, exist_ok=True)
        if not repository.mirror.exists():
            git("init", "--bare", str(repository.mirror))
        git("-C", str(repository.mirror), "fetch", "--no-tags", str(source), sha)
        assert git("-C", str(repository.mirror), "rev-parse", "FETCH_HEAD") == sha

    monkeypatch.setattr(Repository, "fetch_revision", fetch_revision)
    monkeypatch.setattr(Repository, "push_repair", push)
    monkeypatch.setattr(Repository, "fetch_pull", fetch)
    data = yaml.safe_load(home.config.read_text())
    data["measurement"] = {
        "tests": {
            "acme/repo": {
                "command": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            }
        }
    }
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    config = home.load()
    client = GitHub(config.github)
    item = FindingRecord(
        id=Finding(**finding).identity("acme/repo"),
        repository="acme/repo",
        sha=git("rev-parse", "HEAD", cwd=source),
        execution_id="1" * 32,
        agent=PublicAgent(adapter="codex", model="hunter-model", role="hunter"),
        finding=Finding(**finding),
    )
    body = f"<!-- maxwell:finding:{item.id} -->\n" + encode_record(item)
    issue = client.create_issue("acme/repo", item.finding.title, body)["number"]
    review = AssessmentRecord(
        id="a" * 32,
        repository="acme/repo",
        finding_id=item.id,
        sha=item.sha,
        role="reviewer",
        agent=PublicAgent(adapter="claude", model="review-model", role="reviewer"),
        execution_id="2" * 32,
        verdict="supported",
        actionability="actionable",
        evidence="Empty input reaches app.py:2 with a zero denominator; add explicit empty behavior.",
    )
    client.create_comment("acme/repo", issue, record_report(review))
    script = executable.read_text()
    script = script.replace(
        "output.write_text('not json'",
        """elif 'outcome' in json.loads(Path(args[args.index('--output-schema') + 1]).read_text())['properties']:
    assert args[args.index('--sandbox') + 1] == 'workspace-write'
    behavior = Path(__file__).parent / 'repair-mode'
    behavior = behavior.read_text() if behavior.exists() else 'pass'
    if behavior != 'noop':
        (workspace / 'app.py').write_text('def average(values):\\n    return sum(values) / len(values) if values else 0\\n')
        (workspace / 'test_app.py').write_text('from app import average\\ndef test_empty():\\n    assert average([]) == ' + ('1' if behavior == 'fail' else '0') + '\\n')
    if behavior == 'tamper':
        import subprocess
        subprocess.run(['git', 'add', 'app.py'], cwd=workspace, check=True)
    if behavior == 'timeout':
        time.sleep(60)
    result = {'outcome': 'edited', 'summary': 'Handled empty input with regression coverage.',
              'explanation': 'Explicitly return zero for an empty sequence and preserve the nonempty average.',
              'regression_tests': ['test_app.py'] if behavior != 'noop' else [], 'concerns': []}
output.write_text('not json' """,
    )
    executable.write_text(script)
    reporter = Reporter(store, client, config, home)
    yield store, config, reporter, item, review, issue
    client.close()
    store.close()


def run(home, stage="repair", **kwargs):
    return asyncio.run(
        run_scans(home, home.load(), "acme/repo", model="fake-model", stage=stage, **kwargs)
    )


def test_repair_end_to_end_pinned_local_branch_and_repeat(home, source, repairable, github):
    store, config, reporter, finding, review, issue = repairable
    (source / "other.txt").write_text("new main state")
    git("add", "other.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "advance main",
        cwd=source,
    )
    original_head = git("rev-parse", "HEAD", cwd=source)
    result = run(home)
    assert result["valid"], result
    scan = result["scans"][0]
    public = scan["repairs"][0]
    assert public["outcome"] == "prepared" and public["publication_state"] == "published"
    assert public["sha"] == finding.sha and public["references"] == [review.id]
    assert public["agent"]["role"] == "fixer" and public["availability"] == "local-only"
    assert public["spending"]["micros"] == 1900 and public["spending"]["complete"]
    assert "$0.0019 USD" in next(iter(github["pulls"].values()))["body"]
    worktree = Path(public["worktree"])
    assert worktree.is_dir() and git("rev-parse", "HEAD", cwd=worktree) == public["commit"]
    assert git("rev-parse", "HEAD^", cwd=worktree) == finding.sha
    assert git("branch", "--show-current", cwd=worktree) == public["branch"]
    assert git("rev-parse", "HEAD", cwd=source) == original_head
    assert "if values else 0" not in (source / "app.py").read_text()
    assert public["checks"][0]["state"] == "passed"
    assert public["checks"][0]["returncode"] == 0
    assert "1 passed" in Path(public["checks"][0]["log"]).read_text()
    assert public["files"] == ["app.py", "test_app.py"]
    assert github["issues"][issue]["state"] == "open"
    published = [decode_record(item["body"]) for item in github["comments"][issue]]
    assert any(isinstance(record, RepairRecord) for record in published)
    dashboard = store.one("SELECT issue FROM dashboards")["issue"]
    assert not github["comments"].get(dashboard)
    assert "fix pull requests" in github["issues"][dashboard]["body"]
    assert public["branch"] in github["issues"][dashboard]["body"]
    again = run(home)
    assert again["valid"] and not again["scans"]
    forced = run(home, issue=issue, force=True)
    assert not forced["valid"]
    assert len(github["pulls"]) == 1
    assert forced["scans"][0]["repairs"][0]["branch"] == public["branch"]


@pytest.mark.parametrize(
    "behavior,outcome,committed",
    [
        ("fail", "checks_failed", True),
        ("noop", "blocked", False),
        ("tamper", "failed", False),
        ("timeout", "failed", False),
    ],
)
def test_unsuccessful_repairs_preserve_evidence(
    home, directory, repairable, behavior, outcome, committed
):
    (directory / "repair-mode").write_text(behavior)
    result = run(home)
    assert not result["valid"]
    scan = result["scans"][0]
    public = scan["repairs"][0]
    assert public["outcome"] == outcome and public["publication_state"] == "published"
    assert bool(public["commit"]) == committed
    assert Path(public["worktree"]).is_dir()
    if behavior == "fail":
        assert public["checks"][0]["state"] == "failed"
        assert "1 failed" in Path(public["checks"][0]["log"]).read_text()
    if behavior == "timeout":
        assert "timed out" in scan["error"]
        assert (Path(public["worktree"]) / "test_app.py").exists()


def test_fresh_projection_skips_prepared_repairs(home, directory, repairable):
    assert run(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    fresh = Store(directory / "fresh.sqlite")
    fresh.register("acme/repo", str(directory / "source"), "main")
    client = GitHub(config.github)
    try:
        projected = Reporter(fresh, client, config, home)
        targets = asyncio.run(collect_repair_targets(projected, "acme/repo"))
        assert targets == []
        assert fresh.one("SELECT id FROM github_records WHERE type='repair'")
        targets = asyncio.run(collect_repair_targets(projected, "acme/repo", force=True))
        assert len(targets) == 1 and targets[0].finding == finding
    finally:
        client.close()
        fresh.close()


@pytest.mark.parametrize(
    "verdict,actionability",
    [("rejected", "actionable"), ("inconclusive", "unknown"), ("supported", "not_actionable")],
)
def test_repair_requires_supported_actionable_current_inputs(repairable, verdict, actionability):
    store, config, reporter, finding, review, issue = repairable
    changed = review.model_copy(
        update={
            "id": "b" * 32,
            "supersedes": review.id,
            "verdict": verdict,
            "actionability": actionability,
        }
    )
    reporter.github.create_comment("acme/repo", issue, record_report(changed))
    assert asyncio.run(collect_repair_targets(reporter, "acme/repo")) == []
    with pytest.raises(ValueError, match="supported actionable"):
        asyncio.run(collect_repair_targets(reporter, "acme/repo", issue=issue))


def test_audited_repair_retains_review_references(home, repairable):
    store, config, reporter, finding, review, issue = repairable
    audit = review.model_copy(
        update={
            "id": "b" * 32,
            "role": "auditor",
            "agent": PublicAgent(adapter="codex", model="audit-model", role="auditor"),
            "references": [review.id],
        }
    )
    reporter.github.create_comment("acme/repo", issue, record_report(audit))
    result = run(home)
    assert result["valid"], result
    assert result["scans"][0]["repairs"][0]["references"] == [review.id, audit.id]


def test_closed_and_conflicting_findings_are_not_repaired(repairable, github):
    store, config, reporter, finding, review, issue = repairable
    conflicting = review.model_copy(update={"id": "b" * 32, "verdict": "rejected"})
    reporter.github.create_comment("acme/repo", issue, record_report(conflicting))
    assert asyncio.run(collect_repair_targets(reporter, "acme/repo")) == []
    github["issues"][issue]["state"] = "closed"
    assert asyncio.run(collect_repair_targets(reporter, "acme/repo", force=True)) == []


def test_provider_write_capability_is_explicit(home, source, directory):
    config = home.load()
    artifact = directory / "provider-artifacts"
    artifact.mkdir()
    schema = artifact / "schema.json"
    schema.write_text("{}")
    codex = CodexProvider(home, config)
    readonly = codex.arguments(source, artifact, schema, {})
    writable = codex.arguments(source, artifact, schema, {}, writable=True)
    assert readonly[readonly.index("--sandbox") + 1] == "read-only"
    assert writable[writable.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in writable
    claude = ClaudeProvider(home, config)
    readonly = claude.arguments(source, artifact, schema, {})
    writable = claude.arguments(source, artifact, schema, {}, writable=True)
    assert readonly[readonly.index("--tools") + 1] == "Read,Grep,Glob"
    assert writable[writable.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write"
    assert "Bash" in writable[writable.index("--disallowedTools") + 1]
    assert "Edit" not in writable[writable.index("--disallowedTools") + 1].split(",")


def test_host_check_timeout_and_no_runner(home, source, directory):
    artifact = directory / "checks"
    artifact.mkdir()
    config = home.load()
    profile = MeasurementTests(
        command=[sys.executable, "-c", "import time; time.sleep(60)"], timeout_seconds=0.1
    )
    result = asyncio.run(run_checks(source, artifact, profile, config, lambda pid: None))
    assert result.state == "timeout" and result.returncode != 0
    assert not (artifact / "check-process.json").exists()
    result = asyncio.run(run_checks(source, artifact, None, config, lambda pid: None))
    assert result.state == "unavailable"


def test_repair_options_are_explicit_and_single_finding(home):
    args = parser().parse_args(["scan", "--stage", "repair", "--issue", "7", "acme/repo"])
    assert args.stage == "repair" and args.issue == 7
    with pytest.raises(ValueError, match="one finding"):
        run(home, limit=2)
    with pytest.raises(ValueError, match="single explicit validate"):
        run(home, stage=None, force=True)


def test_repair_issue_record_must_match_finding(home, repairable):
    assert run(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    public = decode_record(
        next(
            comment["body"]
            for comment in reporter.github.comments("acme/repo", issue)
            if isinstance(decode_record(comment["body"]), RepairRecord)
        )
    )
    wrong = public.model_copy(update={"id": "c" * 32, "sha": "f" * 40})
    with pytest.raises(ValueError, match="different finding or revision"):
        store.project_record(wrong, issue, 999999, "maintainer", "new-digest")
    with pytest.raises(ValueError, match="finding issue"):
        store.queue_record(public.model_copy(update={"id": "d" * 32}), issue + 99)


def test_interrupted_repair_recovers_branch_and_record(home, repairable):
    assert run(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    row = store.one("SELECT * FROM scans WHERE stage='repair'")
    manifest = json.loads((Path(row["artifact"]) / "repair.json").read_text())
    public = RepairRecord.model_validate(manifest["record"]).model_copy(update={"id": "e" * 32})
    manifest["record"] = public.model_dump(mode="json")
    (Path(row["artifact"]) / "repair.json").write_text(json.dumps(manifest))
    with store.db:
        store.db.execute(
            "UPDATE scans SET state='running',finished=NULL,pid=NULL WHERE id=?", (row["id"],)
        )
    engine = Daemon(home, config)
    engine.store = store
    engine.recover()
    assert store.one("SELECT state FROM scans WHERE id=?", (row["id"],))["state"] == "interrupted"
    assert Path(public.worktree).exists()
    recovered = store.one(
        "SELECT data FROM record_publications WHERE type='repair' AND id=?", (public.id,)
    )
    assert json.loads(recovered["data"])["outcome"] == "interrupted"


def test_changed_assessments_block_prepared_repair(home, repairable, monkeypatch):
    import maxwell.repair as repair

    store, config, reporter, finding, review, issue = repairable
    original = repair.run_checks

    async def change_after_checks(*args, **kwargs):
        result = await original(*args, **kwargs)
        rejection = review.model_copy(
            update={
                "id": "c" * 32,
                "supersedes": review.id,
                "verdict": "rejected",
            }
        )
        reporter.github.create_comment("acme/repo", issue, record_report(rejection))
        return result

    monkeypatch.setattr(repair, "run_checks", change_after_checks)
    result = run(home)
    assert not result["valid"]
    public = result["scans"][0]["repairs"][0]
    assert public["outcome"] == "failed" and public["commit"] is None
    assert Path(public["worktree"]).exists() and public["checks"][0]["state"] == "passed"


def test_missing_runner_retains_unready_draft(home, repairable):
    data = yaml.safe_load(home.config.read_text())
    data.pop("measurement")
    home.config.write_text(yaml.safe_dump(data))
    result = run(home)
    assert not result["valid"]
    public = result["scans"][0]["repairs"][0]
    assert public["outcome"] == "checks_failed" and public["commit"]
    assert public["checks"][0]["state"] == "unavailable"


def test_host_check_cancellation_terminates_process(home, source, directory):
    artifact = directory / "cancel-checks"
    artifact.mkdir()
    profile = MeasurementTests(command=[sys.executable, "-c", "import time; time.sleep(60)"])
    config = home.load()

    async def cancel():
        launched = asyncio.Event()
        task = asyncio.create_task(
            run_checks(source, artifact, profile, config, lambda pid: launched.set())
        )
        await launched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    evidence = json.loads((artifact / "check-result.json").read_text())
    assert evidence["state"] == "cancelled" and evidence["returncode"] != 0
    assert not (artifact / "check-process.json").exists()


def test_repair_selection_uses_neutral_rating_prior(home, repairable):
    from maxwell.agent_selection import AgentSelector

    store, config, reporter, finding, review, issue = repairable
    selector = AgentSelector(
        home, config, model="fake-model", evidence_provider=lambda store, repository, role: {}
    )
    selector.explicit = False
    route, decision = selector.select(store, "acme/repo", "repair")
    assert decision["method"] == "uniform-unrated-v1"
    assert decision["successes"] == decision["failures"] == 0


def test_repair_prepared_requires_host_evidence():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="passing host checks"):
        RepairRecord(
            id="a" * 32,
            repository="acme/repo",
            finding_id="b" * 32,
            sha="f" * 40,
            execution_id="c" * 32,
            agent=PublicAgent(adapter="codex", model="model", role="fixer"),
            references=["d" * 32],
            outcome="prepared",
            summary="Claimed fix.",
            branch="maxwell/repair/" + "c" * 32,
            worktree="/tmp/repair",
        )


def test_repair_stops_after_one_repository(home, source, repairable):
    store, config, reporter, finding, review, issue = repairable
    store.register("zzzz/repo", str(source), "main")
    events = []
    result = asyncio.run(
        run_scans(home, config, model="fake-model", stage="repair", on_progress=events.append)
    )
    assert result["valid"] and len(result["scans"]) == 1
    assert [event["repository"] for event in events if event.get("state") == "plan"] == [
        "acme/repo"
    ]


def test_higher_rank_is_selected_before_lower_rank(repairable, monkeypatch):
    from types import SimpleNamespace

    import maxwell.repair as repair

    store, config, reporter, finding, review, issue = repairable
    other_finding = finding.finding.model_copy(update={"title": "Average accepts invalid values"})
    other = finding.model_copy(
        update={"id": other_finding.identity("acme/repo"), "finding": other_finding}
    )
    second = reporter.github.create_issue(
        "acme/repo",
        other_finding.title,
        f"<!-- maxwell:finding:{other.id} -->\n" + encode_record(other),
    )["number"]
    second_review = review.model_copy(update={"id": "b" * 32, "finding_id": other.id})
    reporter.github.create_comment("acme/repo", second, record_report(second_review))
    monkeypatch.setattr(
        repair,
        "fix_queue",
        lambda *args: (
            [
                {"issue": second, "impact": SimpleNamespace(rate=2)},
                {"issue": issue, "impact": SimpleNamespace(rate=1)},
            ],
            [],
        ),
    )
    targets = asyncio.run(collect_repair_targets(reporter, "acme/repo"))
    assert len(targets) == 1 and targets[0].issue == second
