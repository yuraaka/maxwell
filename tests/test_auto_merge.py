import asyncio
import hashlib
import json
from types import SimpleNamespace as Namespace

import pytest
import yaml
from pydantic import ValidationError
from test_repair import repairable as repairable_fixture
from test_validation import prepared as prepared_fixture
from test_validation import validate

from maxwell.auto_merge import evidence_blocker, github_blocker, try_auto_merge
from maxwell.config import AutoMergeSettings, Config
from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.repair_pull_requests import active_validations, records

_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)
_prepared = pytest.fixture(name="prepared")(prepared_fixture.__wrapped__)


@pytest.fixture
def votes(monkeypatch):
    monkeypatch.setattr("maxwell.auto_merge.input_assessments", lambda *_: [Namespace(id="review")])
    link = Namespace(id="link", head_sha="head", base_sha="base")
    finding = Namespace(finding=Namespace(kind="bug"))
    repair = Namespace(references=["review"], agent=Namespace(adapter="codex", model="fixer"))
    validations = [
        Namespace(
            state="completed",
            outcome="accepted",
            references=["review"],
            agent=Namespace(adapter="codex", model=f"validator-{index}", effort=None),
            pr_id="link",
            pr_head="head",
            pr_base="base",
            tested_sha="candidate",
            result=Namespace(verdict="repaired", concerns=[], reproduction="reproduced"),
            checks=[Namespace(state="passed", returncode=0)],
            replay=Namespace(state="failed", returncode=1),
            failures=[{"phase": "call"}],
        )
        for index in range(2)
    ]
    return AutoMergeSettings(enabled=True), link, finding, repair, validations, []


def test_auto_merge_config_is_opt_in_and_allows_one_validation():
    assert not Config().repair.auto_merge.enabled
    assert Config().repair.auto_merge.required_validations == 2
    assert AutoMergeSettings(required_validations=1).required_validations == 1
    for values in ({"required_validations": 0}, {"merge_method": "override"}):
        with pytest.raises(ValidationError):
            AutoMergeSettings(**values)


def test_independent_votes_cannot_be_increased_by_effort_or_alias(votes):
    assert evidence_blocker(*votes) is None
    validations = votes[4]
    validations[1].agent.model = validations[0].agent.model
    validations[1].agent.effort = "high"
    assert "1/2" in evidence_blocker(*votes)
    validations[1].agent.model = "fixer"
    assert "fixer's model" in evidence_blocker(*votes)


def test_informational_notes_do_not_block_auto_merge(votes):
    votes[4][0].result.notes = ["Metadata predates passing host checks; repair is unaffected."]
    assert evidence_blocker(*votes) is None


def test_disabled_policy_makes_no_github_calls():
    reporter = Namespace(config=Config())
    status = asyncio.run(try_auto_merge(reporter, "acme/repo", Namespace(number=2), "accepted"))
    assert status == "disabled"


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda vote: setattr(vote, "outcome", "rejected"), "rejected"),
        (lambda vote: setattr(vote, "outcome", "inconclusive"), "inconclusive"),
        (lambda vote: setattr(vote, "state", "failed"), "incomplete"),
        (lambda vote: setattr(vote, "references", ["old-review"]), "stale"),
        (lambda vote: setattr(vote, "pr_base", "old-base"), "host evidence"),
        (
            lambda vote: setattr(vote.result, "concerns", ["Missed caller"]),
            "blocking validation concern: Missed caller",
        ),
        (lambda vote: setattr(vote.checks[0], "state", "failed"), "host evidence"),
        (lambda vote: setattr(vote, "replay", None), "regression evidence"),
        (lambda vote: setattr(vote, "failures", [{"phase": "setup"}]), "regression evidence"),
        (lambda vote: setattr(vote.result, "reproduction", "unavailable"), "regression evidence"),
    ],
)
def test_uncertain_or_stale_evidence_never_qualifies(votes, change, reason):
    change(votes[4][1])
    assert reason in evidence_blocker(*votes)


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("pending", "pending"),
        ("failed", "unsuccessful"),
        ("missing", "not reported"),
        ("checks_pending", "pending"),
        ("unprotected", "protection"),
        ("not_strict", "up-to-date"),
        ("admin_bypass", "administrators"),
        ("wrong_app", "not reported"),
        ("unknown_mergeability", "mergeability"),
        ("no_required_checks", "no required"),
    ],
)
def test_github_rules_and_checks_fail_closed(home, github, condition, reason):
    pull = {
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "a" * 40},
        "base": {"ref": "main"},
    }
    if condition in ("pending", "failed"):
        github["statuses"][0]["state"] = "pending" if condition == "pending" else "failure"
    elif condition == "missing":
        github["statuses"] = []
    elif condition == "checks_pending":
        github["check_runs"] = [{"name": "lint", "status": "in_progress", "conclusion": None}]
    elif condition == "unprotected":
        github["protection"] = None
    elif condition == "not_strict":
        github["protection"]["required_status_checks"]["strict"] = False
    elif condition == "admin_bypass":
        github["protection"]["enforce_admins"]["enabled"] = False
    elif condition == "wrong_app":
        github["protection"]["required_status_checks"]["checks"] = [
            {"context": "tests", "app_id": 42}
        ]
        github["check_runs"] = [
            {"name": "tests", "status": "completed", "conclusion": "success", "app": {"id": 7}}
        ]
    elif condition == "unknown_mergeability":
        pull["mergeable"] = None
    elif condition == "no_required_checks":
        github["protection"]["required_status_checks"]["contexts"] = []
    client = GitHub(home.load().github)

    async def call(function, *args, **kwargs):
        return function(*args, **kwargs)

    try:
        reporter = Namespace(github=client, call=call)
        assert reason in asyncio.run(github_blocker(reporter, "acme/repo", pull))
        assert not github["merges"]
    finally:
        client.close()


def enable(home, reporter):
    data = yaml.safe_load(home.config.read_text())
    data["repair"] = {"auto_merge": {"enabled": True}}
    home.config.write_text(yaml.safe_dump(data))
    reporter.config = home.load()


def summary(github, number):
    return next(
        comment["body"]
        for comment in github["comments"][number]
        if "<!-- maxwell:validation-summary:v1 -->" in comment["body"]
    )


def test_one_required_validation_can_merge(home, prepared, repairable, github):
    _, _, reporter, _, _, issue = repairable
    enable(home, reporter)
    data = yaml.safe_load(home.config.read_text())
    data["repair"]["auto_merge"]["required_validations"] = 1
    home.config.write_text(yaml.safe_dump(data))
    result = validate(home)
    assert result["valid"], result
    assert len(result["scans"]) == 1
    assert github["merges"] == [{"sha": prepared["commit"], "merge_method": "squash"}]
    assert github["issues"][issue]["state"] == "open"


def test_automatic_validation_collects_two_models_and_merges_once(
    home, prepared, repairable, github
):
    store, _, reporter, _, _, issue = repairable
    enable(home, reporter)
    data = yaml.safe_load(home.config.read_text())
    data["provider"]["models"] = ["fake-model", "validator-a", "validator-b"]
    home.config.write_text(yaml.safe_dump(data))
    config = home.load()
    reporter.config = config
    store.apply_models(
        {
            "valid": True,
            "checks": [],
            "providers": [
                {
                    "provider": "default",
                    "models": [
                        {"model": model, "access": "verified", "selectable": True}
                        for model in config.provider.models
                    ],
                }
            ],
        },
        hashlib.sha256(home.config.read_bytes()).hexdigest(),
    )
    result = asyncio.run(run_scans(home, config, "acme/repo", stage="validate"))
    assert result["valid"], json.dumps(result, indent=2)
    assert len(result["scans"]) == 2
    link = records(store, "acme/repo", "pull_request")[0]
    current = active_validations(store, "acme/repo", link)
    assert len(current) == 2 and not any(record.supersedes for record in current)
    assert len({record.agent.model for record in current}) == 2
    assert github["merges"] == [{"sha": prepared["commit"], "merge_method": "squash"}]
    assert "waiting for independent integration" in summary(github, link.number)
    assert github["issues"][issue]["state"] == "open"
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert len(github["merges"]) == 1
    assert store.one("SELECT state FROM pr_actions WHERE action='merge'")["state"] == "published"


def test_pinned_model_needs_another_model_and_checks_can_finish_later(
    home, prepared, repairable, github
):
    store, _, reporter, _, _, issue = repairable
    enable(home, reporter)
    github["statuses"][0]["state"] = "pending"
    assert validate(home)["valid"]
    pull = next(iter(github["pulls"].values()))
    assert "1/2" in summary(github, pull["number"]) and not github["merges"]
    with pytest.raises(ValueError, match="previous validators"):
        validate(home)
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="validate", model="other-validator")
    )
    assert result["valid"], result
    link = records(store, "acme/repo", "pull_request")[0]
    assert len(active_validations(store, "acme/repo", link)) == 2
    assert "pending" in summary(github, pull["number"]) and not github["merges"]
    github["statuses"][0]["state"] = "success"
    github["issues"][issue]["labels"].append({"name": "maxwell:state:rejected"})
    blocked = asyncio.run(try_auto_merge(reporter, "acme/repo", link, "accepted"))
    assert "marked rejected" in blocked and not github["merges"]
    github["issues"][issue]["labels"].pop()
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert len(github["merges"]) == 1


@pytest.mark.parametrize(
    "failure", ["drop_merge", "drop_unmerged", "denied", "server_error", "report_merge_only"]
)
def test_merge_response_reconciliation_never_blindly_retries(
    home, prepared, repairable, github, failure
):
    store, _, reporter, _, _, _ = repairable
    enable(home, reporter)
    assert validate(home)["valid"]
    if failure == "denied":
        github["merge_error"] = 405
    elif failure == "server_error":
        github["merge_error"] = 502
    else:
        github[failure] = True
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="validate", model="other-validator")
    )
    assert result["valid"], result
    state = store.one("SELECT state FROM pr_actions WHERE action='merge'")["state"]
    assert (
        state
        == {
            "drop_merge": "published",
            "drop_unmerged": "uncertain",
            "denied": "blocked",
            "server_error": "uncertain",
            "report_merge_only": "uncertain",
        }[failure]
    )
    count = len(github["merges"])
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    if failure != "denied":
        assert len(github["merges"]) == count
    else:
        assert "GitHub denied" in summary(github, next(iter(github["pulls"])))
    if failure == "drop_unmerged":
        pull = next(iter(github["pulls"].values()))
        pull.update(state="closed", merged=True, merge_commit_sha=prepared["commit"])
        asyncio.run(reporter.sync_pull_requests("acme/repo"))
        assert len(github["merges"]) == count
        assert (
            store.one("SELECT state FROM pr_actions WHERE action='merge'")["state"] == "published"
        )


@pytest.mark.parametrize("movement", ["head", "base"])
def test_revision_movement_during_checks_prevents_merge(
    home, prepared, repairable, github, monkeypatch, movement
):
    store, _, reporter, _, _, _ = repairable
    enable(home, reporter)
    github["statuses"][0]["state"] = "pending"
    assert validate(home)["valid"]
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="validate", model="other-validator")
    )
    assert result["valid"], result
    github["statuses"][0]["state"] = "success"
    pull = next(iter(github["pulls"].values()))
    original = reporter.github.check_runs

    def moved(*args):
        checks = list(original(*args))
        pull[movement]["sha"] = "f" * 40
        return iter(checks)

    monkeypatch.setattr(reporter.github, "check_runs", moved)
    link = records(store, "acme/repo", "pull_request")[0]
    status = asyncio.run(try_auto_merge(reporter, "acme/repo", link, "accepted"))
    assert "changed before merge" in status and not github["merges"]


def test_forced_revalidation_cannot_merge_using_old_votes(
    home, prepared, repairable, github, directory
):
    store, _, reporter, _, _, _ = repairable
    enable(home, reporter)
    github["statuses"][0]["state"] = "pending"
    assert validate(home)["valid"]
    second = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="validate", model="other-validator")
    )
    assert second["valid"], second
    github["statuses"][0]["state"] = "success"
    (directory / "validation-mode").write_text("rejected")
    forced = validate(home, force=True)
    assert forced["valid"], forced
    link = records(store, "acme/repo", "pull_request")[0]
    current = active_validations(store, "acme/repo", link)
    assert len(current) == 1 and current[0].outcome == "rejected"
    assert len(current[0].supersedes) == 2
    assert not github["merges"] and github["pulls"][link.number]["draft"]


def test_integration_pool_blocks_generic_sync_until_capacity_available(
    home, prepared, repairable, github
):
    store, _, reporter, _, _, _ = repairable
    enable(home, reporter)
    data = yaml.safe_load(home.config.read_text())
    data["repair"]["auto_merge"]["required_validations"] = 1
    data["pipeline"] = {"pools": {"integrate": 0}}
    home.config.write_text(yaml.safe_dump(data))
    assert validate(home)["valid"]
    reporter.config = home.load()
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert not github["merges"]
    number = next(iter(github["pulls"]))
    assert "integration pool is paused" in summary(github, number)
    reporter.config.pipeline.pools.integrate = 1
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert len(github["merges"]) == 1
    assert store.one("SELECT COUNT(*) AS n FROM scans WHERE stage='validate'")["n"] == 1


def test_integration_pool_counts_distinct_open_findings_and_external_merges(monkeypatch):
    from maxwell.auto_merge import integration_pool_blocker

    links = [
        Namespace(number=number, finding_issue=issue, finding_id=str(issue))
        for number, issue in ((1, 10), (2, 10), (3, 11))
    ]
    monkeypatch.setattr("maxwell.repair_pull_requests.records", lambda *_: links)
    closed = set()

    async def call(method, *args):
        if method == "pull":
            return {"merged": True}
        number = int(args[-1].split("/")[-1])
        return {"state": "closed" if number in closed else "open"}

    reporter = Namespace(
        config=Config(), store=None, github=Namespace(pull="pull", request="request"), call=call
    )
    reporter.config.pipeline.pools.integrate = 2
    assert "full (2/2)" in asyncio.run(integration_pool_blocker(reporter, "acme/repo"))
    closed.add(10)
    assert asyncio.run(integration_pool_blocker(reporter, "acme/repo")) is None
