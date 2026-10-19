import asyncio
import json
from pathlib import Path

import pytest
from test_repair import repairable as repairable_fixture
from test_repair import run
from test_validation import prepared as prepared_fixture
from test_validation import validate

from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.records import IntegrationRecord, decode_record
from maxwell.reporting import Reporter
from maxwell.repositories import Repository, git
from maxwell.store import Store

_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)
_prepared = pytest.fixture(name="prepared")(prepared_fixture.__wrapped__)


def test_draft_publication_recovers_lost_response_without_new_fixer(home, repairable, github):
    github["drop_pull"] = True
    first = run(home)
    assert first["valid"], first
    link = first["scans"][0]["pull_requests"][0]
    pull = github["pulls"][link["number"]]
    assert pull["draft"] and "Closes #" not in pull["body"]
    assert github["issues"][1]["state"] == "open"
    assert run(home)["scans"] == []
    assert len(github["pulls"]) == 1
    assert repairable[0].one("SELECT COUNT(*) AS n FROM scans WHERE stage='repair'")["n"] == 1


def test_validator_updates_one_summary_and_comment_review(
    home, prepared, repairable, executable, github
):
    import yaml

    config = yaml.safe_load(home.config.read_text())
    config["provider"]["models"] = {
        "fake-model": {},
        "validator-model": {
            "input_usd_per_mtok": 2,
            "cached_input_usd_per_mtok": 0.2,
            "output_usd_per_mtok": 8,
        },
    }
    home.config.write_text(yaml.safe_dump(config))
    script = executable.read_text().replace(
        "'concerns': []}\nelif 'verdict'",
        "'concerns': [], 'comments': [{'path': 'app.py', 'line': 2, 'explanation': 'The empty input guard preserves the existing nonempty average.'}]}\nelif 'verdict'",
    )
    executable.write_text(script)
    first = validate(home)
    assert first["valid"], first
    record = first["scans"][0]["validations"][0]
    assert record["spending"]["micros"] == 1900 and record["spending"]["complete"]
    number = record["issue"]
    assert first["scans"][0]["pull_requests"][0]["number"] == number
    assert not github["pulls"][number]["draft"]
    assert "maxwell:validation:accepted" in {
        label["name"] for label in github["pulls"][number]["labels"]
    }
    review = github["reviews"][number][0]
    assert review["event"] == "COMMENT" and review["commit_id"] == prepared["commit"]
    assert "### review agent · accepted" in review["body"]
    assert "$0 USD" in review["body"]
    assert "$0 USD" in review["comments"][0]["body"]
    assert review["comments"][0]["body"].startswith("**review agent**")
    assert review["comments"][0]["line"] == 2
    review["body"] = review["body"].replace("### review agent ·", "### Validator ·", 1)
    assert validate(home)["valid"]
    assert len(github["reviews"][number]) == 1
    current_record = decode_record(
        next(
            comment["body"]
            for comment in github["comments"][number]
            if f"<!-- maxwell:validation:{record['id']} -->" in comment["body"]
        )
    )
    review["body"] = (
        f"<!-- maxwell:pr-review:{record['id']} -->\nIndependent model verdict: accepted. {current_record.result.summary}"
    )
    assert validate(home, force=True)["valid"]
    summaries = [
        comment
        for comment in github["comments"][number]
        if "<!-- maxwell:validation-summary:v1 -->" in comment["body"]
    ]
    assert len(summaries) == 1 and decode_record(summaries[0]["body"]) is None
    assert github["issues"][1]["state"] == "open"
    assert not github["comments"].get(repairable[0].one("SELECT issue FROM dashboards")["issue"])


def test_base_movement_stales_validation_and_requires_new_check(
    home, source, prepared, repairable, github
):
    assert validate(home)["valid"]
    pull = next(iter(github["pulls"].values()))
    (source / "readme.txt").write_text("new unrelated base file")
    git("add", "readme.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "advance base",
        cwd=source,
    )
    head = git("rev-parse", "HEAD", cwd=source)
    github["branches"]["main"] = head
    pull["base"]["sha"] = head
    reporter = repairable[2]
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert pull["draft"]
    assert "maxwell:validation:stale" in {label["name"] for label in pull["labels"]}
    result = validate(home)
    assert result["valid"], result
    current = result["scans"][0]["validations"][0]
    assert current["pr_base"] == head and current["before"]["sha"] == head
    assert current["replay"]["returncode"] == 1
    assert not pull["draft"]


def test_amendment_reuses_pr_branch_and_preserves_predecessor(
    home, prepared, repairable, github, executable, directory
):
    (directory / "validation-mode").write_text("rejected")
    assert validate(home)["valid"]
    executable.write_text(
        executable.read_text().replace(
            "    if behavior == 'tamper':\n        import subprocess",
            "    if behavior == 'revise':\n        (workspace / 'app.py').write_text('def average(values):\\n    return sum(values) / len(values) if values else 0.0\\n')\n        (workspace / 'test_app.py').write_text('from app import average\\ndef test_empty():\\n    assert average([]) == 0\\ndef test_nonempty():\\n    assert average([2, 4]) == 3\\n')\n    if behavior == 'tamper':\n        import subprocess",
        )
    )
    (directory / "repair-mode").write_text("revise")
    result = run(home)
    assert result["valid"], json.dumps(result, indent=2)
    revision = result["scans"][0]["repairs"][0]
    assert revision["previous_id"] == prepared["id"]
    assert revision["parent_sha"] == prepared["commit"]
    assert revision["branch"] == prepared["branch"]
    assert revision["commit"] != prepared["commit"]
    assert len(github["pulls"]) == 1
    pull = next(iter(github["pulls"].values()))
    assert pull["draft"] and pull["head"]["sha"] == revision["commit"]
    assert Path(prepared["worktree"]).exists()
    (directory / "validation-mode").unlink()
    assert validate(home)["valid"]


@pytest.mark.parametrize("merge_style", ["fast_forward", "squash"])
def test_human_merge_requires_independent_main_verification_before_closure(
    home, source, prepared, repairable, github, merge_style
):
    assert validate(home)["valid"]
    store, config, reporter, finding, review, issue = repairable
    pull = next(iter(github["pulls"].values()))
    repository = Repository(home, store.repository("acme/repo"))
    git("fetch", str(repository.mirror), prepared["commit"], cwd=source)
    if merge_style == "squash":
        git("merge", "--squash", prepared["commit"], cwd=source)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "human squash merge",
            cwd=source,
        )
    else:
        git("merge", "--ff-only", prepared["commit"], cwd=source)
    maintained = git("rev-parse", "HEAD", cwd=source)
    github["branches"]["main"] = maintained
    pull.update(state="closed", merged=True, merge_commit_sha=maintained)
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    assert github["issues"][issue]["state"] == "open"
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", stage="integrate", model="validator-model")
    )
    assert result["valid"], json.dumps(result, indent=2)
    record = result["scans"][0]["validations"][0]
    assert record["type"] == "integration" and record["sha"] == maintained
    assert record["outcome"] == "accepted"
    assert github["issues"][issue]["state"] == "closed"
    assert "maxwell:integration:verified" in {
        label["name"] for label in github["issues"][issue]["labels"]
    }
    measurement = store.one(
        "SELECT data FROM github_records WHERE type='measurement' AND status='current'"
    )
    assert json.loads(measurement["data"])["sha"] == maintained
    assert any(
        isinstance(decode_record(comment["body"]), IntegrationRecord)
        for comment in github["comments"][pull["number"]]
    )
    assert any(
        "### integrate agent · accepted" in comment["body"]
        for comment in github["comments"][pull["number"]]
    )
    fresh = Store(home.root / "fresh.sqlite")
    fresh.register("acme/repo", str(source), "main")
    client = GitHub(config.github)
    try:
        asyncio.run(Reporter(fresh, client, config, home).project_repository_records("acme/repo"))
        assert fresh.one("SELECT id FROM github_records WHERE type='integration'")
    finally:
        fresh.close()
        client.close()


def test_head_movement_during_validation_is_not_accepted(
    home, prepared, repairable, github, monkeypatch
):
    import maxwell.validation as validation

    original = validation.compute_measurement
    pull = next(iter(github["pulls"].values()))

    async def moved(*args, **kwargs):
        record = await original(*args, **kwargs)
        if str(args[1]).endswith("-after"):
            pull["head"]["sha"] = repairable[3].sha
            github["branches"][pull["head"]["ref"]] = repairable[3].sha
        return record

    monkeypatch.setattr(validation, "compute_measurement", moved)
    result = validate(home)
    assert not result["valid"]
    record = result["scans"][0]["validations"][0]
    assert record["state"] == "failed" and record["outcome"] == "inconclusive"
    assert pull["draft"] and github["issues"][1]["state"] == "open"
    from maxwell.reputation import selection_evidence

    assert selection_evidence(repairable[0], "acme/repo", "fixer") == {}


@pytest.mark.parametrize("concurrent_edit", [False, True])
def test_merge_conflict_returns_pr_to_fixer_on_current_base(
    home, source, prepared, repairable, github, monkeypatch, concurrent_edit
):
    (source / "app.py").write_text("def average(values):\n    return 42\n")
    (source / "unrelated.txt").write_text("preserve maintained-branch changes\n")
    git("add", "app.py", "unrelated.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "conflicting main",
        cwd=source,
    )
    base = git("rev-parse", "HEAD", cwd=source)
    pull = next(iter(github["pulls"].values()))
    github["branches"]["main"] = base
    pull["base"]["sha"] = base
    result = validate(home)
    assert not result["valid"]
    record = result["scans"][0]["validations"][0]
    assert record["outcome"] == "inconclusive" and record["state"] == "failed"
    assert record["merge_conflicts"] == ["app.py"]
    assert "maxwell scan acme/repo --stage fix --issue 1" in result["scans"][0]["error"]
    assert not result["scans"][0]["usage"]["reported"]
    assert pull["draft"] and github["issues"][1]["state"] == "open"
    from maxwell.repair import collect_repair_targets

    targets = asyncio.run(collect_repair_targets(repairable[2], "acme/repo"))
    assert len(targets) == 1, "A merge conflict must return the existing PR to the fixer"
    assert targets[0].feedback[0].model_copy(update={"merge_conflicts": []}).requires_rebase
    assert (
        not targets[0]
        .feedback[0]
        .model_copy(
            update={
                "merge_conflicts": [],
                "summary": "Validation failed: provider unavailable",
            }
        )
        .requires_rebase
    )
    from maxwell.pipeline import load_snapshot

    snapshot = asyncio.run(load_snapshot(repairable[2], "acme/repo"))
    assert snapshot.items[0].stage == "fix"
    assert "maxwell:validation:conflicted" in {label["name"] for label in pull["labels"]}
    if concurrent_edit:
        original = Repository.push_repair

        def moved(repository, branch, commit, token, expected=None):
            assert expected == prepared["commit"]
            github["branches"][branch] = "f" * 40
            pull["head"]["sha"] = "f" * 40
            return original(repository, branch, commit, token, expected)

        monkeypatch.setattr(Repository, "push_repair", moved)
    repaired = run(home)
    if concurrent_edit:
        assert not repaired["valid"]
        assert pull["head"]["sha"] == "f" * 40
        assert github["branches"][prepared["branch"]] == "f" * 40
        assert "refusing to overwrite" in json.dumps(repaired)
        return
    assert repaired["valid"], json.dumps(repaired, indent=2)
    revision = repaired["scans"][0]["repairs"][0]
    assert revision["parent_sha"] == base
    assert revision["previous_head_sha"] == prepared["commit"]
    assert revision["previous_id"] == prepared["id"]
    assert revision["branch"] == prepared["branch"]
    assert len(github["pulls"]) == 1 and pull["head"]["sha"] == revision["commit"]
    assert (Path(revision["worktree"]) / "unrelated.txt").read_text() == (
        "preserve maintained-branch changes\n"
    )
    validated = validate(home)
    assert validated["valid"], json.dumps(validated, indent=2)
    assert validated["scans"][0]["validations"][0]["outcome"] == "accepted"


def test_failed_pr_push_is_retried_without_repeating_fixer(home, repairable, monkeypatch):
    original = Repository.push_repair

    def fail(*args, **kwargs):
        raise ValueError("remote temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "push_repair", fail)
        first = run(home)
    assert not first["valid"] and first["scans"][0]["state"] == "completed"
    assert "remote temporarily unavailable" in " ".join(first["scans"][0]["publication_errors"])
    assert Repository.push_repair is original
    retry = run(home)
    assert retry["valid"] and retry["scans"] == []
    assert repairable[0].one("SELECT COUNT(*) AS n FROM scans WHERE stage='repair'")["n"] == 1


def test_foreign_summary_marker_cannot_be_modified(home, prepared, repairable, github):
    pull = next(iter(github["pulls"].values()))
    summary = next(
        comment
        for comment in github["comments"][pull["number"]]
        if "<!-- maxwell:validation-summary:v1 -->" in comment["body"]
    )
    summary["user"]["login"] = "stranger"
    before = summary["body"]
    with pytest.raises(ValueError, match="summary marker conflicts"):
        validate(home)
    assert summary["body"] == before and pull["draft"]


def test_full_scan_dispatches_pending_validation_and_can_skip_it(
    home, prepared, repairable, monkeypatch
):
    import maxwell.manual as manual

    async def measured(*args, **kwargs):
        return {"state": "completed"}

    monkeypatch.setattr(manual, "measure_one", measured)
    skipped = ("hunt", "validate", "audit", "review", "integrate")
    result = asyncio.run(
        run_scans(home, home.load(), "acme/repo", model="validator-model", skip_stages=skipped)
    )
    assert result["valid"] and result["scans"] == []
    result = asyncio.run(
        run_scans(
            home,
            home.load(),
            "acme/repo",
            model="validator-model",
            skip_stages=("hunt", "validate", "audit"),
        )
    )
    assert result["valid"], result
    assert [scan["stage"] for scan in result["scans"]] == ["review"]


def test_pr_returning_to_previous_revision_gets_new_movement_record(
    home, prepared, repairable, github
):
    from maxwell.repair_pull_requests import records

    pull = next(iter(github["pulls"].values()))
    reporter = repairable[2]
    original = records(repairable[0], "acme/repo", "pull_request")[0]
    assert validate(home)["valid"]
    pull["head"]["sha"] = repairable[3].sha
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    moved = records(repairable[0], "acme/repo", "pull_request")[0]
    assert moved.id != original.id
    pull["head"]["sha"] = original.head_sha
    asyncio.run(reporter.sync_pull_requests("acme/repo"))
    returned = records(repairable[0], "acme/repo", "pull_request")[0]
    assert returned.head_sha == original.head_sha and returned.id != original.id
    assert returned.previous_id == moved.id
    result = validate(home)
    assert result["valid"], result
    assert result["scans"][0]["validations"][0]["pr_id"] == returned.id


@pytest.mark.parametrize("kind", ["checks", "behind", "conflicts"])
def test_merge_blocker_routes_to_fixer_and_updates_same_pr(
    home, prepared, repairable, github, source, executable, kind
):
    from maxwell.pipeline import load_snapshot
    from maxwell.repair import collect_repair_targets

    pull = next(iter(github["pulls"].values()))
    if kind == "checks":
        github["statuses"][0].update(
            state="failure", description="tests/test_app.py: expected floating-point zero"
        )
    else:
        (source / "unrelated.txt").write_text("keep current main\n")
        if kind == "conflicts":
            (source / "app.py").write_text("def average(values):\n    return 42\n")
        git("add", ".", cwd=source)
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
        base = git("rev-parse", "HEAD", cwd=source)
        github["branches"]["main"] = base
        pull["base"]["sha"] = base
        pull["mergeable_state"] = "behind" if kind == "behind" else "dirty"
        pull["mergeable"] = kind != "conflicts"
    targets = asyncio.run(collect_repair_targets(repairable[2], "acme/repo"))
    assert len(targets) == 1 and targets[0].merge_problem.kind == kind
    if kind == "checks":
        assert "expected floating-point zero" in targets[0].merge_problem.checks[0]["summary"]
    snapshot = asyncio.run(load_snapshot(repairable[2], "acme/repo"))
    assert snapshot.items[0].stage == "fix"
    executable.write_text(executable.read_text().replace("if values else 0", "if values else 0.0"))
    result = run(home)
    assert result["valid"], json.dumps(result, indent=2)
    revision = result["scans"][0]["repairs"][0]
    assert revision["previous_head_sha"] == prepared["commit"]
    assert revision["parent_sha"] == (prepared["commit"] if kind == "checks" else base)
    assert len(github["pulls"]) == 1 and pull["head"]["sha"] == revision["commit"]
    prompt = (home.root / "artifacts" / result["scans"][0]["id"] / "prompt.txt").read_text()
    assert "Current GitHub merge blocker:" in prompt and '"kind":"' + kind + '"' in prompt
    if kind != "checks":
        assert (Path(revision["worktree"]) / "unrelated.txt").read_text() == "keep current main\n"
    github["statuses"][0]["state"] = "success"
    pull.update(mergeable=True, mergeable_state="clean")
    validated = validate(home)
    assert validated["valid"], json.dumps(validated, indent=2)
    assert validated["scans"][0]["validations"][0]["outcome"] == "accepted"


@pytest.mark.parametrize("state", ["pending", "optional_failure", "review_required"])
def test_non_code_merge_blockers_do_not_start_fixer(home, prepared, repairable, github, state):
    from maxwell.repair import collect_repair_targets

    pull = next(iter(github["pulls"].values()))
    pull["mergeable_state"] = "blocked"
    if state == "pending":
        github["statuses"][0]["state"] = "pending"
    elif state == "optional_failure":
        github["statuses"].append({"context": "optional", "state": "failure"})
    assert asyncio.run(collect_repair_targets(repairable[2], "acme/repo")) == []
