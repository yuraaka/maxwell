import asyncio
import json
import statistics
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from maxwell.measurement import digest
from maxwell.records import MeasurementFactor, RepairCheck
from maxwell.repositories import git


def read_checks(client, repository, sha, settings):
    runs = [
        item
        for item in client.check_runs(repository, sha)
        if item.get("head_sha") == sha
        and item.get("name") in settings.checks
        and (settings.app_id is None or item.get("app", {}).get("id") == settings.app_id)
    ]
    statuses = {}
    if settings.app_id is None:
        for page in range(1, 101):
            response = client.request(
                "GET",
                f"repos/{repository}/commits/{sha}/status",
                params={"per_page": 100, "page": page},
            )
            if response.get("sha", sha) != sha:
                raise ValueError("GitHub returned CI status for a different revision")
            batch = response["statuses"]
            for item in batch:
                name = item["context"]
                if name not in statuses or item.get("id", 0) > statuses[name].get("id", 0):
                    statuses[name] = item
            if len(batch) < 100:
                break
        else:
            raise ValueError("CI status pagination limit reached; evidence incomplete")
    selected = []
    for name in settings.checks:
        matches = [item for item in runs if item["name"] == name]
        if len(matches) > 1 or (matches and name in statuses):
            raise ValueError(
                f"Ambiguous CI check {name!r}; select its publisher with review.ci.<owner/repo>.app_id"
            )
        if matches:
            item = matches[0]
            conclusion = item.get("conclusion")
            state = (
                "pending"
                if item.get("status") != "completed"
                else "passed"
                if conclusion == "success"
                else "failed"
                if conclusion in ("failure", "timed_out", "startup_failure")
                else "unavailable"
            )
            selected.append(
                {
                    "name": name,
                    "state": state,
                    "id": item.get("id"),
                    "conclusion": conclusion,
                    "url": item.get("html_url", ""),
                    "output": item.get("output") or {},
                }
            )
        elif name in statuses:
            item = statuses[name]
            selected.append(
                {
                    "name": name,
                    "state": {"success": "passed", "failure": "failed", "error": "failed"}.get(
                        item.get("state"), "pending"
                    ),
                    "id": item.get("id"),
                    "url": item.get("target_url", ""),
                    "output": {"summary": item.get("description", "")},
                }
            )
        else:
            selected.append({"name": name, "state": "pending", "output": {}, "url": "", "id": None})
    states = {item["state"] for item in selected}
    state = next(
        (value for value in ("failed", "unavailable", "pending") if value in states), "passed"
    )
    return {"sha": sha, "state": state, "checks": selected}


def check_record(evidence, artifact):
    path = artifact / "github-ci.json"
    path.write_text(json.dumps(evidence, indent=2))
    detail = f"GitHub CI for {evidence['sha']}: " + "; ".join(
        f"{item['name']}: {item['state']} (id {item['id']}) {item['url']} "
        f"{str(item['output'].get('summary', ''))[:400]}"
        for item in evidence["checks"]
    )
    return RepairCheck(
        command=["github-ci", evidence["sha"]],
        state=evidence["state"] if evidence["state"] != "pending" else "unavailable",
        returncode=0 if evidence["state"] == "passed" else None,
        detail=detail[:2000],
        log=str(path),
    )


async def covered_revision(repository, client, link, tested, token, progress):
    pull = await asyncio.to_thread(client.pull, link.repository, link.number)
    if (pull["head"]["sha"], pull["base"]["sha"]) != (link.head_sha, link.base_sha):
        raise ValueError("PR moved while selecting CI evidence; synchronize and retry")
    tree = git("-C", str(repository.mirror), "rev-parse", f"{tested}^{{tree}}")
    for sha in dict.fromkeys([link.head_sha, pull.get("merge_commit_sha")]):
        if not sha:
            continue
        if sha != link.head_sha:
            await asyncio.to_thread(repository.fetch_revision, sha, token)
        if git("-C", str(repository.mirror), "rev-parse", f"{sha}^{{tree}}") == tree:
            return sha
    progress("GitHub CI does not cover the current merge candidate; using local tests")
    return None


async def wait_for_checks(client, link, sha, settings, artifact, progress):
    start = time.monotonic()
    deadline = start + settings.timeout_seconds
    while True:
        request_started = time.monotonic()
        pull = await asyncio.to_thread(client.pull, link.repository, link.number)
        if pull["state"] != "open" or (pull["head"]["sha"], pull["base"]["sha"]) != (
            link.head_sha,
            link.base_sha,
        ):
            raise ValueError("PR changed while waiting for CI; synchronize and retry")
        evidence = await asyncio.to_thread(read_checks, client, link.repository, sha, settings)
        deadline += time.monotonic() - request_started
        check_record(evidence, artifact)
        if evidence["state"] != "pending":
            return evidence
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(
                f"GitHub CI wait exceeded {settings.timeout_seconds:g}s; check review.ci.<owner/repo>.checks and timeout_seconds"
            )
        names = ", ".join(item["name"] for item in evidence["checks"] if item["state"] == "pending")
        progress(
            f"waiting for GitHub CI: {names} · {sha[:12]} · {int(time.monotonic() - start)}s elapsed"
        )
        await asyncio.sleep(min(settings.poll_seconds, remaining))


class CIRun(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    duration_seconds: float = Field(ge=0)
    outcomes: dict[
        Annotated[str, Field(min_length=1, max_length=1000)], Literal["passed", "failed", "skipped"]
    ] = Field(min_length=1, max_length=20000)


class CIReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1]
    sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    environment_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    protocol: str = Field(min_length=1, max_length=200)
    runs: list[CIRun] = Field(min_length=2, max_length=20)


def test_measurement(evidence, settings, budget):
    reason = "CI test measurement report unavailable; local repeated tests skipped"
    factors, details, fingerprint = {}, {"source": "github-ci", "sha": evidence["sha"]}, None
    selected = [
        item
        for item in evidence["checks"]
        if item["name"] == settings.measurement_check and item["state"] == "passed"
    ]
    if selected:
        raw = str(selected[0]["output"].get("text", ""))
        try:
            if len(raw) > 1_000_000:
                raise ValueError("report too large")
            report = CIReport.model_validate_json(raw)
            if report.sha != evidence["sha"]:
                raise ValueError("report revision does not match CI")
            tests = set(report.runs[0].outcomes)
            if any(set(run.outcomes) != tests for run in report.runs):
                raise ValueError("collected test IDs changed between runs")
            median = statistics.median(run.duration_seconds for run in report.runs)
            values = {test: {run.outcomes[test] for run in report.runs} for test in tests}
            eligible = {
                test: results for test, results in values.items() if results <= {"passed", "failed"}
            }
            flaky = sum(len(results) > 1 for results in eligible.values())
            if budget:
                factors["test_runtime"] = MeasurementFactor(
                    numerator=median, denominator=budget, unit="seconds/budget_seconds"
                )
            if eligible:
                factors["test_flakiness"] = MeasurementFactor(
                    numerator=flaky, denominator=len(eligible), unit="observed_flaky_tests/tests"
                )
            details.update(
                valid_runs=len(report.runs),
                median_seconds=median,
                observed_tests=len(eligible),
                observed_flaky_count=flaky,
                protocol=report.protocol,
            )
            fingerprint = digest(
                {
                    "source": "github-ci",
                    "environment": report.environment_fingerprint,
                    "protocol": report.protocol,
                    "runs": len(report.runs),
                }
            )
            reason = (
                "CI runtime budget not configured"
                if not budget
                else "CI report has no eligible test outcomes"
            )
        except (ValueError, TypeError) as exc:
            reason = f"Invalid CI test measurement report: {str(exc)[:300]}"
    return (
        factors,
        {name: reason for name in ("test_runtime", "test_flakiness") if name not in factors},
        details,
        fingerprint,
    )
