import json
from pathlib import Path

import pytest
import yaml

from maxwell.cli import execute, parser
from maxwell.config import Paths
from maxwell.records import AssessmentRecord, FindingRecord, decode_record, encode_record
from maxwell.repositories import git
from maxwell.reputation import agent_reputation, reputation_report
from maxwell.store import Store


def run(home, capsys, *arguments):
    if "--stage" not in arguments:
        arguments = ("--stage", "hunt", *arguments)
    execute(
        parser().parse_args(
            [
                "--home",
                str(home.root),
                "scan",
                "acme/repo",
                "--model",
                "fake-model",
                "--json",
                *arguments,
            ]
        )
    )
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def hunted(home, source, github, capsys):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    first = run(home, capsys)
    issue = store.one("SELECT issue FROM findings")["issue"]
    yield store, issue, first["scans"][0]
    store.close()


def assessments(github, issue):
    return [
        record
        for comment in github["comments"].get(issue, [])
        if isinstance(record := decode_record(comment["body"]), AssessmentRecord)
    ]


def test_hunt_review_audit_and_forced_reassessment(home, hunted, github, capsys):
    store, issue, hunt = hunted
    assert hunt["stage"] == "hunt"
    reviewed = run(home, capsys, "--stage", "review", "--effort", "high")
    review = reviewed["scans"][0]
    assert reviewed["valid"] and review["stage"] == "review"
    assert review["sha"] == hunt["sha"] and review["admitted"] == 0
    assert review["selection"] is None and review["usage"]["input_tokens"] == 1000
    record = assessments(github, issue)[0]
    assert record.role == "reviewer" and record.agent.effort == "high"
    assert record.verdict == "supported" and record.references == []
    assert record.execution_id == review["id"] and record.finding_id == hunt["findings"][0]["id"]
    prompt = (home.root / "artifacts" / review["id"] / "prompt.txt").read_text()
    assert '"confidence"' not in prompt and '"agent"' not in prompt
    assert "Do not edit files" in prompt and "Never claim to have executed tests" in prompt
    assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1
    assert github["issues"][issue]["state"] == "open"
    assert {label["name"] for label in github["issues"][issue]["labels"]} >= {
        "maxwell:state:reviewed",
        "maxwell:review:supported",
    }
    assert run(home, capsys, "--stage", "review")["scans"] == []
    audited = run(home, capsys, "--stage", "audit", "--issue", str(issue))
    audit = audited["scans"][0]
    assert audited["valid"] and audit["stage"] == "audit"
    audit_record = assessments(github, issue)[1]
    assert audit_record.role == "auditor" and audit_record.references == [record.id]
    assert audit_record.execution_id == audit["id"]
    assert {label["name"] for label in github["issues"][issue]["labels"]} >= {
        "maxwell:state:audited",
        "maxwell:audit:supported",
        "maxwell:review:supported",
    }
    assert run(home, capsys, "--stage", "audit")["scans"] == []
    replacement = run(home, capsys, "--stage", "review", "--issue", str(issue), "--force")
    latest = assessments(github, issue)[2]
    assert replacement["valid"] and latest.supersedes == record.id
    assert not any(
        label["name"].startswith("maxwell:audit:") or label["name"] == "maxwell:state:audited"
        for label in github["issues"][issue]["labels"]
    )
    assert assessments(github, issue)[0] == record
    new_audit = run(home, capsys, "--stage", "audit")
    assert new_audit["valid"] and assessments(github, issue)[3].references == [latest.id]
    runs = store.rows("SELECT * FROM scans ORDER BY created")
    assert len(runs) == 5
    assert store.committed_today() == 5 * 1900
    dashboard = next(
        item for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    assert "| Recorded total tokens (input + output) | 5,500 |" in dashboard["body"]
    history = next(item for item in github["issues"].values() if item["title"] == "Maxwell history")
    assert not github["comments"].get(dashboard["number"])
    executions = [decode_record(item["body"]) for item in github["comments"][history["number"]]]
    review_execution = next(item for item in executions if item.id == review["id"])
    assert review_execution.agent.role == "reviewer" and review_execution.finding_ids == [
        record.finding_id
    ]
    assert not list((home.root / "repos").glob("*/worktrees/*"))


@pytest.mark.parametrize("verdict", ["supported", "rejected", "inconclusive"])
def test_review_reports_each_verdict_without_closing_issue(home, hunted, github, capsys, verdict):
    _, issue, _ = hunted
    (Path(home.load().provider.executable).parent / "assessment.json").write_text(
        json.dumps({"verdict": verdict})
    )
    result = run(home, capsys, "--stage", "review")
    assert result["valid"] and result["scans"][0]["assessments"][0]["verdict"] == verdict
    assert assessments(github, issue)[0].verdict == verdict
    assert {label["name"] for label in github["issues"][issue]["labels"]} >= {
        "maxwell:state:reviewed",
        f"maxwell:review:{verdict}",
    }
    assert github["issues"][issue]["state"] == "open"


def test_noop_review_refreshes_reputation_without_running_an_agent(home, hunted, github, capsys):
    store, issue, _ = hunted
    run(home, capsys, "--stage", "review")
    record = assessments(github, issue)[0]
    comments = github["comments"][issue]
    comments[:] = [item for item in comments if decode_record(item["body"]) != record]
    before = store.one("SELECT COUNT(*) AS n FROM scans")["n"]
    result = run(home, capsys, "--stage", "audit")
    assert result["scans"] == [] and store.one("SELECT COUNT(*) AS n FROM scans")["n"] == before
    dashboard = next(
        item for item in github["issues"].values() if item["title"] == "Maxwell status"
    )
    assert reputation_report(agent_reputation(store, "acme/repo")) in dashboard["body"]
    assert "reviewer" not in {
        item["role"] for item in agent_reputation(store, "acme/repo") if item["assessments"]
    }


def test_review_uses_pinned_revision_after_branch_moves_and_queue_is_full(
    home, source, hunted, capsys
):
    store, _, hunt = hunted
    config = yaml.safe_load(home.config.read_text())
    (source / "app.py").write_text("def average(values):\n    return 0\n")
    git("add", "app.py", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Change implementation",
        cwd=source,
    )
    head = git("rev-parse", "HEAD", cwd=source)
    assert head != hunt["sha"]
    assert run(home, capsys)["scans"][0]["sha"] == head
    config["limits"]["human_queue_max"] = 1
    home.config.write_text(yaml.safe_dump(config))
    result = run(home, capsys, "--stage", "review")
    assert result["valid"] and result["scans"][0]["sha"] == hunt["sha"]
    assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1
    assert git("rev-parse", "HEAD", cwd=source) == head
    assert store.repository("acme/repo")["last_sha"] == head


def test_fresh_database_reviews_github_findings_without_local_finding_rows(
    home,
    directory,
    source,
    hunted,
    github,
    capsys,
):
    _, issue, hunt = hunted
    fresh = Paths(directory / "fresh")
    fresh.initialize()
    fresh.config.write_text(home.config.read_text())
    fresh.config.chmod(0o600)
    store = Store(fresh.db)
    store.register("acme/repo", str(source), "main")
    try:
        result = run(fresh, capsys, "--stage", "review")
        assert result["valid"] and result["scans"][0]["sha"] == hunt["sha"]
        assert not store.rows("SELECT * FROM findings")
        assert assessments(github, issue)[0].finding_id == hunt["findings"][0]["id"]
        github["issues"][issue]["labels"] = [
            {"name": "maxwell"},
            {"name": "maxwell:state:suspected"},
            {"name": "custom"},
        ]
        assert run(fresh, capsys, "--stage", "review")["scans"] == []
        assert {label["name"] for label in github["issues"][issue]["labels"]} == {
            "maxwell",
            "custom",
            "maxwell:state:reviewed",
            "maxwell:review:supported",
        }
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["malformed", "modify"])
def test_failed_review_publishes_no_assessment_and_records_usage(
    home, hunted, github, capsys, mode
):
    store, issue, _ = hunted
    (Path(home.load().provider.executable).parent / "mode").write_text(mode)
    with pytest.raises(SystemExit):
        run(home, capsys, "--stage", "review")
    result = json.loads(capsys.readouterr().out)
    assert not result["valid"] and result["scans"][0]["state"] == "failed"
    assert result["scans"][0]["usage"]["input_tokens"] == 1000
    assert not assessments(github, issue)
    assert not store.rows("SELECT * FROM record_publications WHERE type='assessment'")
    assert not list((home.root / "repos").glob("*/worktrees/*"))


def test_targeted_audit_requires_review_and_legacy_finding_cannot_be_assessed(
    home, hunted, github, capsys
):
    store, issue, _ = hunted
    with pytest.raises(ValueError, match="requires a current review"):
        run(home, capsys, "--stage", "audit", "--issue", str(issue))
    assert store.one("SELECT COUNT(*) AS n FROM scans")["n"] == 1
    github["issues"][issue]["body"] = github["issues"][issue]["body"].split(
        "<!-- maxwell:record:v1"
    )[0]
    with pytest.raises(ValueError, match="legacy findings"):
        run(home, capsys, "--stage", "review", "--issue", str(issue))
    assert store.one("SELECT COUNT(*) AS n FROM scans")["n"] == 1


def test_unauthorized_review_is_ignored(home, hunted, github, capsys):
    _, issue, _ = hunted
    run(home, capsys, "--stage", "review")
    github["comments"][issue][0]["user"] = {"login": "outsider"}
    result = run(home, capsys, "--stage", "review")
    assert result["valid"] and len(result["scans"]) == 1


def test_human_output_identifies_stage_and_verdict(home, hunted, capsys):
    execute(
        parser().parse_args(
            [
                "--home",
                str(home.root),
                "scan",
                "acme/repo",
                "--model",
                "fake-model",
                "--stage",
                "review",
            ]
        )
    )
    output = capsys.readouterr().out
    assert "Review>" in output and "review: 1 supported" in output
    assert "Status: https://github.com/acme/repo/issues/" in output
    assert "Assessment:" not in output
    execute(
        parser().parse_args(
            [
                "--home",
                str(home.root),
                "scan",
                "acme/repo",
                "--model",
                "fake-model",
                "--stage",
                "review",
            ]
        )
    )
    assert "[SKIP] acme/repo / review: No eligible findings" in capsys.readouterr().out


@pytest.mark.parametrize("options", [["--issue", "0"], ["--limit", "-1"], ["--stage", "fix"]])
def test_invalid_stage_cli_options_are_rejected(options):
    with pytest.raises(SystemExit):
        parser().parse_args(["scan", "--model", "fake-model", *options])


@pytest.mark.parametrize("options", [["--issue", "1"], ["--limit", "2"], ["--force"]])
def test_hunt_rejects_assessment_options(home, options):
    with pytest.raises(ValueError, match="explicit review, audit, repair, or validate only"):
        execute(
            parser().parse_args(
                ["--home", str(home.root), "scan", "--model", "fake-model", *options]
            )
        )


@pytest.mark.parametrize("stage", ["review", "audit"])
@pytest.mark.parametrize("configured,override,expected", [(2, None, 2), (1, 2, 2), (3, 1, 1)])
def test_stage_config_limit_and_cli_override_rank_findings(
    home,
    hunted,
    github,
    capsys,
    stage,
    configured,
    override,
    expected,
):
    store, issue, _ = hunted
    original = decode_record(github["issues"][issue]["body"])
    for number, priority in [(20, "P2"), (21, "P0")]:
        finding = original.finding.model_copy(
            update={"title": f"Distinct issue candidate {number}", "priority": priority}
        )
        record = FindingRecord(
            **{
                **original.model_dump(),
                "id": finding.identity("acme/repo"),
                "finding": finding,
            }
        )
        github["issues"][number] = {
            **github["issues"][issue],
            "number": number,
            "html_url": f"https://github.com/acme/repo/issues/{number}",
            "body": f"<!-- maxwell:finding:{record.id} -->\n" + encode_record(record),
        }
    if stage == "audit":
        assert run(home, capsys, "--stage", "review", "--limit", "3")["valid"]
    config = yaml.safe_load(home.config.read_text())
    config["limits"][f"max_{stage}s_per_scan"] = configured
    home.config.write_text(yaml.safe_dump(config))
    options = ["--limit", str(override)] if override is not None else []
    result = run(home, capsys, "--stage", stage, *options)
    assert result["valid"] and len(result["scans"]) == expected
    assert [scan["assessments"][0]["issue"] for scan in result["scans"]] == [21, issue][:expected]
    role = "reviewer" if stage == "review" else "auditor"
    assert not [record for record in assessments(github, 20) if record.role == role]
    assert store.one("SELECT COUNT(*) AS n FROM findings")["n"] == 1


def test_review_is_still_blocked_by_daily_execution_limit(home, hunted, capsys):
    store, _, _ = hunted
    config = yaml.safe_load(home.config.read_text())
    for key in ("input_usd_per_mtok", "output_usd_per_mtok", "cached_input_usd_per_mtok"):
        config["provider"].pop(key, None)
    native = home.root / "native"
    native.mkdir()
    (native / "config.toml").write_text('model="fake-model"\n')
    config["provider"].pop("api_key")
    config["provider"]["home"] = str(native)
    config["limits"].update(daily_scan_limit=1, human_queue_max=1)
    home.config.write_text(yaml.safe_dump(config))
    run(home, capsys, "--stage", "review")
    with pytest.raises(SystemExit):
        run(home, capsys, "--stage", "audit")
    result = json.loads(capsys.readouterr().out)
    assert "daily_scan_limit" in result["scans"][0]["error"]
    assert "human_queue_max" not in result["scans"][0]["error"]
    assert store.one("SELECT executed FROM scans WHERE stage='audit'")["executed"] == 0


def test_assessment_publication_is_reconciled_without_another_agent_call(
    home,
    hunted,
    github,
    capsys,
    monkeypatch,
):
    from maxwell.github import GitHub, PublicationUncertain

    store, issue, _ = hunted
    create = GitHub.create_comment

    def lost(client, repo, number, body):
        create(client, repo, number, body)
        raise PublicationUncertain("Comment creation response lost")

    with monkeypatch.context() as patch:
        patch.setattr(GitHub, "create_comment", lost)
        result = run(home, capsys, "--stage", "review")
    assert result["valid"] and result["scans"][0]["state"] == "completed"
    assert not result["scans"][0]["publication_errors"]
    assert (
        store.one("SELECT state FROM record_publications WHERE type='assessment'")["state"]
        == "published"
    )
    repeated = run(home, capsys, "--stage", "review")
    assert repeated["valid"] and repeated["scans"] == []
    assert len(assessments(github, issue)) == 1
    assert (
        store.one("SELECT state FROM record_publications WHERE type='assessment'")["state"]
        == "published"
    )
    assert store.one("SELECT COUNT(*) AS n FROM scans WHERE stage='review'")["n"] == 1
    assert store.one("SELECT report_sent FROM scans WHERE stage='review'")["report_sent"] == 1
    execution_id = assessments(github, issue)[0].execution_id
    assert store.one(
        "SELECT id FROM github_records WHERE type='execution' AND id=?", (execution_id,)
    )


def test_issue_option_requires_repository(home):
    with pytest.raises(ValueError, match="requires a repository"):
        execute(
            parser().parse_args(
                [
                    "--home",
                    str(home.root),
                    "scan",
                    "--model",
                    "fake-model",
                    "--stage",
                    "review",
                    "--issue",
                    "1",
                ]
            )
        )


def test_review_and_audit_agent_output_updates_factor_associations(home, hunted, github, capsys):
    _, issue, _ = hunted
    result = Path(home.load().provider.executable).parent / "assessment.json"
    result.write_text(json.dumps({"factors": ["test_runtime"]}))
    reviewed = run(home, capsys, "--stage", "review")
    assert reviewed["valid"]
    assert assessments(github, issue)[0].factors == ["test_runtime"]
    assert "maxwell:factor:test_runtime" in {
        label["name"] for label in github["issues"][issue]["labels"]
    }
    result.write_text(json.dumps({"factors": []}))
    audited = run(home, capsys, "--stage", "audit")
    assert audited["valid"]
    assert assessments(github, issue)[1].factors == []
    assert not any(
        label["name"].startswith("maxwell:factor:") for label in github["issues"][issue]["labels"]
    )


def test_review_publishes_repair_and_audit_receives_it(home, hunted, github, capsys):
    _, issue, _ = hunted
    repair = {
        "plan": "Guard empty input before dividing by the collection length.",
        "validation": "Add an empty-input regression test and run the affected suite.",
        "confidence": 0.8,
        "effort_hours": {"low": 0.5, "high": 1},
    }
    payload = Path(home.load().provider.executable).parent / "assessment.json"
    payload.write_text(json.dumps({"repair": repair}))
    reviewed = run(home, capsys, "--stage", "review")
    assert reviewed["valid"]
    record = assessments(github, issue)[0]
    assert record.repair.plan == repair["plan"]
    artifact = home.root / "artifacts" / record.execution_id
    assert "available" in json.loads((artifact / "assessment-context.json").read_text())
    assert "engineer-hour effort" in (artifact / "prompt.txt").read_text()
    payload.write_text(json.dumps({"repair": None}))
    audited = run(home, capsys, "--stage", "audit")
    audit = assessments(github, issue)[1]
    assert audited["valid"] and audit.repair is None
    prompt = (home.root / "artifacts" / audit.execution_id / "prompt.txt").read_text()
    assert repair["plan"] in prompt
    status = next(item for item in github["issues"].values() if item["title"] == "Maxwell status")
    assert "missing: No repair estimate" in status["body"]
    assert "audited" in status["body"]
    assert not github["comments"].get(status["number"])
