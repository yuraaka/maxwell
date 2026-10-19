import asyncio
import hashlib

import pytest
import yaml
from test_repair import repairable as repairable_fixture
from test_validation import prepared as prepared_fixture

from maxwell.manual import run_scans
from maxwell.repositories import git

_repairable = pytest.fixture(name="repairable")(repairable_fixture.__wrapped__)
_prepared = pytest.fixture(name="prepared")(prepared_fixture.__wrapped__)


def models(home, store):
    data = yaml.safe_load(home.config.read_text())
    data.setdefault("review", {})["max_fix_attempts"] = 3
    data["provider"]["models"] = ["fake-model", "validator-model"]
    home.config.write_text(yaml.safe_dump(data))
    store.apply_models(
        {
            "valid": True,
            "checks": [],
            "providers": [
                {
                    "provider": "default",
                    "models": [
                        {
                            "model": model,
                            "access": "verified",
                            "selectable": True,
                            "efforts": None,
                            "default_effort": None,
                        }
                        for model in data["provider"]["models"]
                    ],
                }
            ],
        },
        hashlib.sha256(home.config.read_bytes()).hexdigest(),
    )


def conflict(source, github):
    (source / "app.py").write_text("def average(values):\n    return 42\n")
    (source / "unrelated.txt").write_text("preserve current main\n")
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
        "advance main",
        cwd=source,
    )
    base = git("rev-parse", "HEAD", cwd=source)
    pull = next(iter(github["pulls"].values()))
    pull["base"]["sha"] = base
    github["branches"]["main"] = base
    return base, pull


def review(home, events):
    return asyncio.run(
        run_scans(
            home,
            home.load(),
            "acme/repo",
            stage="review",
            model="validator-model",
            force=True,
            on_progress=events.append,
        )
    )


def test_review_conflict_runs_visible_fix_and_fresh_review(
    home, source, prepared, repairable, github
):
    models(home, repairable[0])
    base, pull = conflict(source, github)
    events = []
    result = review(home, events)
    assert result["valid"], result
    assert [item["stage"] for item in result["scans"]] == ["review", "fix", "review"]
    failed, fixed, accepted = result["scans"]
    assert failed["validations"][0]["merge_conflicts"] == ["app.py"]
    assert failed["resolved_by"] == accepted["id"]
    assert fixed["repairs"][0]["parent_sha"] == base
    assert accepted["validations"][0]["outcome"] == "accepted"
    assert fixed["model"] != accepted["model"]
    assert pull["head"]["sha"] == fixed["repairs"][0]["commit"]
    assert len(github["pulls"]) == 1
    import io

    from maxwell.cli_output import human_lines
    from maxwell.scan_display import ScanDisplay

    output = "\n".join(human_lines(result, "scan"))
    assert "[RECOVERED]" in output and "[FAIL]" not in output
    assert "review: accepted" in output
    assert output.count("fix PR:") == 1
    display = ScanDisplay(io.StringIO())
    for event in events:
        display(event)
    assert "fix" in display.stages
    assert "Recovery:" in "\n".join(display.panel("0:01"))
    assert any(event["state"] == "recovery" and event["stage"] == "fix" for event in events)


@pytest.mark.parametrize(
    "verdict,concerns",
    [("not_repaired", []), ("inconclusive", ["Add nonempty-input regression coverage."])],
)
def test_review_feedback_is_fixed_on_fresh_main_and_rechecked(
    home, source, prepared, repairable, github, monkeypatch, verdict, concerns
):
    from pathlib import Path

    from maxwell.provider import CodexProvider

    models(home, repairable[0])
    (source / "fresh.txt").write_text("keep this main change")
    git("add", "fresh.txt", cwd=source)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "new main content",
        cwd=source,
    )
    base = git("rev-parse", "HEAD", cwd=source)
    next(iter(github["pulls"].values()))["base"]["sha"] = base
    github["branches"]["main"] = base
    original_review = CodexProvider.validate
    original_fix = CodexProvider.repair
    fixed = False

    async def assess(*args, **kwargs):
        result = await original_review(*args, **kwargs)
        return (
            result
            if fixed
            else result.model_copy(update={"verdict": verdict, "concerns": concerns})
        )

    async def amend(self, worktree, *args, **kwargs):
        nonlocal fixed
        result = await original_fix(self, worktree, *args, **kwargs)
        path = worktree / "test_app.py"
        path.write_text(
            path.read_text() + "\ndef test_nonempty():\n    assert average([2, 4]) == 3\n"
        )
        fixed = True
        return result

    monkeypatch.setattr(CodexProvider, "validate", assess)
    monkeypatch.setattr(CodexProvider, "repair", amend)
    result = review(home, [])
    assert result["valid"], result
    assert [item["stage"] for item in result["scans"]] == ["review", "fix", "review"]
    fixed_record = result["scans"][1]["repairs"][0]
    assert fixed_record["parent_sha"] == base
    assert (Path(fixed_record["worktree"]) / "fresh.txt").read_text() == "keep this main change"
    assert result["scans"][-1]["validations"][0]["outcome"] == "accepted"


def test_loop_limit_keeps_unresolved_failures_visible(home, prepared, repairable, monkeypatch):
    from maxwell.provider import CodexProvider

    models(home, repairable[0])
    original_review, original_fix = CodexProvider.validate, CodexProvider.repair
    count = 0

    async def reject(*args, **kwargs):
        result = await original_review(*args, **kwargs)
        return result.model_copy(update={"verdict": "not_repaired"})

    async def amend(self, worktree, *args, **kwargs):
        nonlocal count
        result = await original_fix(self, worktree, *args, **kwargs)
        count += 1
        path = worktree / "test_app.py"
        path.write_text(
            path.read_text() + f"\ndef test_nonempty_{count}():\n    assert average([2, 4]) == 3\n"
        )
        return result

    monkeypatch.setattr(CodexProvider, "validate", reject)
    monkeypatch.setattr(CodexProvider, "repair", amend)
    result = review(home, [])
    assert not result["valid"]
    assert count == 3
    assert [item["stage"] for item in result["scans"]] == [
        "review",
        "fix",
        "review",
        "fix",
        "review",
        "fix",
        "review",
    ]
    assert "after 3 fix attempts" in result["errors"][0]["error"]
    assert not any(item.get("resolved_by") for item in result["scans"])


def test_failed_fixer_is_reported_without_retrying_review(
    home, source, directory, prepared, repairable, github
):
    models(home, repairable[0])
    conflict(source, github)
    (directory / "repair-mode").write_text("fail")
    result = review(home, [])
    assert not result["valid"]
    assert [item["stage"] for item in result["scans"]] == ["review", "fix"]
    assert result["scans"][-1]["repairs"][0]["outcome"] == "checks_failed"
    assert "review was not retried" in result["errors"][0]["error"]


def test_review_drain_recovers_existing_pr_without_creating_new_repairs(
    home, source, prepared, repairable, github
):
    models(home, repairable[0])
    conflict(source, github)
    events = []
    result = asyncio.run(
        run_scans(
            home, home.load(), "acme/repo", stage="review", drain=True, on_progress=events.append
        )
    )
    counts = result["pipeline_repositories"][0]["executions"]
    assert counts.get("fix") == 1, result
    assert counts.get("review") == 2, result
    assert len(github["pulls"]) == 1
    assert result["scans"][-1]["validations"][0]["outcome"] == "accepted"
    assert any(event["state"] == "recovery" for event in events)


def test_publication_failure_stops_loop_without_hiding_prior_conflict(
    home, source, prepared, repairable, github, monkeypatch
):
    from maxwell.repositories import Repository

    models(home, repairable[0])
    conflict(source, github)

    def fail_push(*args, **kwargs):
        raise ValueError("push unavailable")

    monkeypatch.setattr(Repository, "push_repair", fail_push)
    result = review(home, [])
    assert not result["valid"]
    assert [item["stage"] for item in result["scans"]] == ["review", "fix"]
    assert result["scans"][-1]["publication_errors"]
    assert not result["scans"][0].get("resolved_by")


def test_changed_feedback_allows_bounded_pipeline_retries(home, repairable):
    from maxwell.pipeline import AttemptLedger

    config = home.load()
    config.review.max_fix_attempts = 2
    ledger = AttemptLedger(repairable[0], config, False)
    ledger.save("acme/repo", "fix", "finding", "first-feedback", "completed")
    assert ledger.blocker("acme/repo", "fix", "finding", "first-feedback")
    assert ledger.blocker("acme/repo", "fix", "finding", "new-feedback") is None
    ledger.save("acme/repo", "fix", "finding", "new-feedback", "completed")
    assert "max_fix_attempts" in ledger.blocker("acme/repo", "fix", "finding", "third-feedback")


def test_ci_failure_is_fixed_then_reviewed_on_new_head(home, prepared, repairable, monkeypatch):
    from maxwell.github import GitHub
    from maxwell.provider import CodexProvider

    data = yaml.safe_load(home.config.read_text())
    data["review"]["ci"] = {"acme/repo": {"checks": ["tests"], "app_id": 99}}
    home.config.write_text(yaml.safe_dump(data))
    models(home, repairable[0])
    original = CodexProvider.repair

    def checks(self, repo, sha):
        return [
            {
                "id": 1,
                "name": "tests",
                "head_sha": sha,
                "app": {"id": 99},
                "status": "completed",
                "conclusion": "failure" if sha == prepared["commit"] else "success",
                "output": {"summary": "Nonempty input regression required"},
            }
        ]

    async def amend(self, worktree, *args, **kwargs):
        result = await original(self, worktree, *args, **kwargs)
        path = worktree / "test_app.py"
        path.write_text(
            path.read_text() + "\ndef test_nonempty():\n    assert average([2, 4]) == 3\n"
        )
        return result

    monkeypatch.setattr(GitHub, "check_runs", checks)
    monkeypatch.setattr(CodexProvider, "repair", amend)
    result = review(home, [])
    assert result["valid"], result
    assert [item["stage"] for item in result["scans"]] == ["review", "fix", "review"]
    initial, fixed, accepted = result["scans"]
    assert initial["executed"] == 0
    assert initial["validations"][0]["checks"][0]["state"] == "failed"
    assert accepted["validations"][0]["checks"][0]["command"] == [
        "github-ci",
        fixed["repairs"][0]["commit"],
    ]
    assert initial["resolved_by"] == accepted["id"]


def test_inconclusive_without_actionable_feedback_is_not_silently_fixed(
    home, prepared, repairable, directory
):
    models(home, repairable[0])
    (directory / "validation-mode").write_text("inconclusive")
    result = review(home, [])
    assert not result["valid"]
    assert [item["stage"] for item in result["scans"]] == ["review"]
    assert "without actionable" in result["errors"][0]["error"]
