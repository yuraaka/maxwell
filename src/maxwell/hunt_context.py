import asyncio
import subprocess
import time

from maxwell.measurement import (
    ANALYZER,
    FACTORS,
    POLICY,
    compatible,
    digest,
    measurement_policy,
    measurement_score,
    projected_measurements,
)
from maxwell.records import RECORD_ADAPTER
from maxwell.repositories import git


def provenance(record, sha, now, origin=None):
    origin = origin or {
        "id": record.id,
        "sha": record.sha,
        "measured_at": record.measured_at,
        "environment_fingerprint": record.environment_fingerprint,
    }
    measured = origin.get("measured_at")
    return {
        **origin,
        "age_seconds": max(0, now - measured) if measured is not None else None,
        "at_current_revision": origin["sha"] == sha,
    }


def measurement_context(store, repository, sha, settings):
    history = projected_measurements(store, repository)
    policy = measurement_policy(settings, repository)
    current = next(
        (
            record
            for record in reversed(history)
            if record.analyzer_version == ANALYZER
            and record.policy_version == POLICY
            and {k: v for k, v in record.policy.items() if k != "tests"}
            == {k: v for k, v in policy.items() if k != "tests"}
            and ("tests" not in policy or record.policy.get("tests") == policy["tests"])
            and record.protocol == digest(record.policy)
        ),
        None,
    )
    if current is None:
        return {"available": False, "reason": "No published measurement matches the current policy"}
    now = time.time()
    source, tests = current.evidence.get("source", {}), current.evidence.get("tests", {})
    baseline = next((record for record in history if compatible(record, current)), None)
    architecture = source.get("architecture", {})
    architecture = architecture if isinstance(architecture, dict) else {}
    hotspot_sources = {
        "complex_functions": (source, "complex_functions", 20),
        "clone_examples": (source, "clone_examples", 20),
        "import_cycles": (source, "cycles", 20),
        "module_fanout": (source, "module_fanout", 20),
        "coupling_hotspots": (architecture, "coupling_hotspots", 20),
        "cohesion_hotspots": (architecture, "cohesion_hotspots", 20),
        "slow_tests": (tests, "slow_tests", 20),
        "observed_flaky_tests": (tests, "observed_flaky_tests", 50),
        "persistent_failures": (tests, "persistent_failures", 50),
    }
    result = {
        "available": True,
        **provenance(current, sha, now),
        "policy_version": current.policy_version,
        "analyzer_version": current.analyzer_version,
        "score": measurement_score(current),
        "coverage": current.coverage,
        "factors": {
            name: {
                "status": current.factor_status.get(name, "unknown"),
                "raw": current.factors[name].model_dump() if name in current.factors else None,
            }
            for name in FACTORS
        },
        "stage_provenance": {
            stage: provenance(current, sha, now, current.evidence.get("reused", {}).get(stage))
            for stage in ("source", "tests")
        },
        "hotspots": {
            name: evidence.get(key, [])[:limit] if isinstance(evidence.get(key, []), list) else None
            for name, (evidence, key, limit) in hotspot_sources.items()
        },
        "source_coverage": {
            key: source.get(key)
            for key in (
                "eligible_files",
                "analyzed_files",
                "incomplete_file_count",
                "excluded_file_count",
            )
        },
        "test_observation": {
            key: tests.get(key)
            for key in (
                "valid_runs",
                "requested_runs",
                "median_seconds",
                "observed_flaky_count",
                "persistent_failure_count",
            )
        },
    }
    if baseline:
        result["baseline"] = {
            **provenance(baseline, sha, now),
            "score": measurement_score(baseline),
        }
    return result


def previous_hunt(store, repository):
    records = (
        RECORD_ADAPTER.validate_json(row["data"])
        for row in store.rows(
            "SELECT data FROM github_records WHERE repository=? AND type='execution' AND status='current'",
            (repository,),
        )
    )
    return max(
        (
            record
            for record in records
            if record.state == "completed"
            and record.sha
            and record.agent
            and record.agent.role == "hunter"
        ),
        key=lambda record: (record.finished, record.id),
        default=None,
    )


def revision_changes(worktree, sha, previous, limits):
    result = {"available": False, "current_sha": sha}
    if previous is None:
        return {**result, "reason": "First hunt: no successful published hunt is available"}
    result["previous_hunt"] = {
        "id": previous.id,
        "sha": previous.sha,
        "finished": previous.finished,
    }
    try:
        ancestor = git("merge-base", previous.sha, sha, cwd=worktree)
        if ancestor != previous.sha:
            return {
                **result,
                "reason": "Previous hunt revision is not an ancestor of the current revision",
            }
        paths = git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-status",
            "-z",
            previous.sha,
            sha,
            "--",
            cwd=worktree,
            strip=False,
        ).split("\0")
        files = [
            {"status": paths[index], "path": paths[index + 1]}
            for index in range(0, len(paths) - 1, 2)
        ]
        patch = git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-color",
            "--unified=3",
            previous.sha,
            sha,
            "--",
            cwd=worktree,
            strip=False,
        )
    except (ValueError, OSError, UnicodeError, subprocess.TimeoutExpired):
        return {
            **result,
            "reason": "Previous revision or Git comparison is unavailable; inspect the whole repository",
        }
    return {
        **result,
        "available": True,
        "changed_files": files[: limits.max_hunt_changed_files],
        "total_changed_files": len(files),
        "shown_changed_files": min(len(files), limits.max_hunt_changed_files),
        "diff": patch[: limits.max_hunt_diff_chars],
        "diff_chars": len(patch),
        "diff_truncated": len(patch) > limits.max_hunt_diff_chars,
    }


async def build_hunt_context(store, repository, sha, worktree, config):
    measurement = measurement_context(store, repository, sha, config.measurement)
    changes = await asyncio.to_thread(
        revision_changes, worktree, sha, previous_hunt(store, repository), config.limits
    )
    return {"measurement": measurement, "changes_since_previous_hunt": changes}
