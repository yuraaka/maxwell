import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from maxwell.config import ReviewCI
from maxwell.review_ci import read_checks, wait_for_checks
from maxwell.review_ci import test_measurement as ci_measurement

SHA = "a" * 40
OTHER = "b" * 40


def run(**updates):
    return {
        "id": 1,
        "name": "tests",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "app": {"id": 99},
        **updates,
    }


def client(runs=(), statuses=()):
    return SimpleNamespace(
        check_runs=lambda repo, sha: runs,
        request=lambda *args, **kwargs: {"sha": SHA, "statuses": statuses},
        pull=lambda *args: {"state": "open", "head": {"sha": SHA}, "base": {"sha": OTHER}},
    )


@pytest.mark.parametrize(
    "conclusion,state",
    [
        ("success", "passed"),
        ("failure", "failed"),
        ("timed_out", "failed"),
        ("action_required", "unavailable"),
        ("neutral", "unavailable"),
        ("skipped", "unavailable"),
        ("cancelled", "unavailable"),
        (None, "unavailable"),
    ],
)
def test_ci_conclusions(conclusion, state):
    evidence = read_checks(
        client([run(conclusion=conclusion)]), "acme/repo", SHA, ReviewCI(checks=["tests"])
    )
    assert evidence["state"] == state


@pytest.mark.parametrize(
    "runs", [[], [run(head_sha=OTHER)], [run(name="lint")], [run(app={"id": 98})]]
)
def test_missing_stale_wrong_job_and_wrong_publisher_do_not_pass(runs):
    evidence = read_checks(client(runs), "acme/repo", SHA, ReviewCI(checks=["tests"], app_id=99))
    assert evidence["state"] == "pending"


def test_ambiguous_publishers_are_rejected():
    with pytest.raises(ValueError, match="Ambiguous CI check"):
        read_checks(
            client([run(), run(id=2, app={"id": 98})]), "acme/repo", SHA, ReviewCI(checks=["tests"])
        )


def test_status_context_uses_latest_result_and_checks_pagination():
    pages = []

    def response(*args, **kwargs):
        page = kwargs["params"]["page"]
        pages.append(page)
        return {
            "sha": SHA,
            "statuses": (
                [{"id": 200, "context": "tests", "state": "pending"}]
                + [{"id": number, "context": "lint", "state": "success"} for number in range(99)]
                if page == 1
                else [{"id": 1, "context": "tests", "state": "success"}]
            ),
        }

    github = client()
    github.request = response
    assert read_checks(github, "acme/repo", SHA, ReviewCI(checks=["tests"]))["state"] == "pending"
    assert pages == [1, 2]


def test_all_designated_checks_must_pass():
    evidence = read_checks(
        client([run()], [{"id": 1, "context": "integration", "state": "failure"}]),
        "acme/repo",
        SHA,
        ReviewCI(checks=["tests", "integration"]),
    )
    assert evidence["state"] == "failed"


@pytest.mark.parametrize(
    "values",
    [
        {"checks": []},
        {"checks": ["tests", "tests"]},
        {"checks": [" "]},
        {"checks": ["tests"], "measurement_check": "untrusted"},
        {"checks": ["tests"], "poll_seconds": 0},
    ],
)
def test_invalid_ci_configuration(values):
    with pytest.raises(ValidationError):
        ReviewCI(**values)


def test_wait_detects_pr_movement_and_is_cancellable(tmp_path):
    link = SimpleNamespace(repository="acme/repo", number=1, head_sha=SHA, base_sha=OTHER)
    settings = ReviewCI(checks=["tests"])
    github = client()
    github.pull = lambda *args: {"state": "open", "head": {"sha": OTHER}, "base": {"sha": OTHER}}
    with pytest.raises(ValueError, match="PR changed"):
        asyncio.run(wait_for_checks(github, link, SHA, settings, tmp_path, lambda phase: None))

    async def cancel():
        waiting = asyncio.Event()
        task = asyncio.create_task(
            wait_for_checks(client(), link, SHA, settings, tmp_path, lambda phase: waiting.set())
        )
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())


def report(**updates):
    return {
        "version": 1,
        "sha": SHA,
        "environment_fingerprint": "c" * 64,
        "protocol": "pytest-v1",
        "runs": [
            {"duration_seconds": 10, "outcomes": {"test_a": "passed", "test_b": "failed"}},
            {"duration_seconds": 20, "outcomes": {"test_a": "passed", "test_b": "passed"}},
        ],
        **updates,
    }


def observation(data):
    return ci_measurement(
        {
            "sha": SHA,
            "checks": [{"name": "tests", "state": "passed", "output": {"text": json.dumps(data)}}],
        },
        ReviewCI(checks=["tests"], measurement_check="tests"),
        60,
    )


def test_structured_ci_reports_measure_runtime_and_observed_flakes():
    factors, status, details, fingerprint = observation(report())
    assert not status
    assert factors["test_runtime"].numerator == 15
    assert factors["test_runtime"].denominator == 60
    assert factors["test_flakiness"].numerator == 1
    assert factors["test_flakiness"].denominator == 2
    assert details["valid_runs"] == 2
    assert fingerprint != observation(report(environment_fingerprint="d" * 64))[3]
    assert fingerprint != observation(report(protocol="different-suite"))[3]


@pytest.mark.parametrize(
    "data",
    [
        report(sha=OTHER),
        report(runs=[]),
        report(runs=report()["runs"][:1]),
        report(runs=[{"duration_seconds": float("nan"), "outcomes": {"a": "passed"}}] * 2),
        report(
            runs=[
                {"duration_seconds": 1, "outcomes": {"a": "passed"}},
                {"duration_seconds": 1, "outcomes": {"b": "passed"}},
            ]
        ),
        report(environment_fingerprint="unknown"),
    ],
)
def test_invalid_ci_measurements_stay_unknown(data):
    factors, status, _, fingerprint = observation(data)
    assert not factors and fingerprint is None
    assert all("Invalid CI" in reason for reason in status.values())


def test_wait_timeout_reports_configuration_and_preserves_evidence(tmp_path, monkeypatch):
    link = SimpleNamespace(repository="acme/repo", number=1, head_sha=SHA, base_sha=OTHER)
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr("maxwell.review_ci.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(ValueError, match="GitHub CI wait exceeded 1s"):
        asyncio.run(
            wait_for_checks(
                client(),
                link,
                SHA,
                ReviewCI(checks=["tests"], timeout_seconds=1),
                tmp_path,
                lambda phase: None,
            )
        )
    assert json.loads((tmp_path / "github-ci.json").read_text())["state"] == "pending"


@pytest.mark.parametrize(
    "head_matches,merge_matches,expected",
    [
        (True, False, SHA),
        (False, True, "c" * 40),
        (False, False, None),
    ],
)
def test_only_matching_git_trees_are_covered(
    tmp_path, monkeypatch, head_matches, merge_matches, expected
):
    from maxwell.review_ci import covered_revision

    link = SimpleNamespace(repository="acme/repo", number=1, head_sha=SHA, base_sha=OTHER)
    fetched = []
    repo = SimpleNamespace(mirror=tmp_path, fetch_revision=lambda sha, token: fetched.append(sha))
    github = client()
    github.pull = lambda *args: {
        "head": {"sha": SHA},
        "base": {"sha": OTHER},
        "merge_commit_sha": "c" * 40,
    }

    def tree(*args):
        sha = args[-1].split("^")[0]
        if sha == "tested" or sha == SHA and head_matches or sha == "c" * 40 and merge_matches:
            return "same-tree"
        return "other-tree"

    monkeypatch.setattr("maxwell.review_ci.git", tree)
    result = asyncio.run(covered_revision(repo, github, link, "tested", None, lambda phase: None))
    assert result == expected
    assert fetched == ([] if head_matches else ["c" * 40])


def test_api_quota_wait_does_not_consume_ci_wait_budget(tmp_path, monkeypatch):
    link = SimpleNamespace(repository="acme/repo", number=1, head_sha=SHA, base_sha=OTHER)
    github = client()
    ticks = iter([0, 0, 3600, 3600, 3600, 3601, 3601])
    monkeypatch.setattr("maxwell.review_ci.time", SimpleNamespace(monotonic=lambda: next(ticks)))

    async def finish(delay):
        github.check_runs = lambda *args: [run()]

    monkeypatch.setattr("maxwell.review_ci.asyncio.sleep", finish)
    evidence = asyncio.run(
        wait_for_checks(
            github,
            link,
            SHA,
            ReviewCI(checks=["tests"], timeout_seconds=2),
            tmp_path,
            lambda phase: None,
        )
    )
    assert evidence["state"] == "passed"
