import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import time
import uuid

from maxwell.github import GitHub
from maxwell.measurement import (
    ANALYZER,
    FACTORS,
    POLICY,
    compatible,
    digest,
    historical_stage,
    measurement_report,
    measurement_score,
    projected_measurements,
)
from maxwell.measurement_python import python_measurement
from maxwell.measurement_tests import prepare_snapshot, run_test_measurement
from maxwell.records import RECORD_ADAPTER, MeasurementRecord
from maxwell.reporting import Reporter
from maxwell.repositories import Repository
from maxwell.store import Store


def recover_measurements(store, paths):
    for row in store.rows("SELECT * FROM measurements WHERE state='running'"):
        artifact = paths.root / "artifacts" / row["id"]
        try:
            process = json.loads((artifact / "process.json").read_text())
            pid = int(process["pid"])
            args = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            if (
                str(artifact) in process["report"]
                and process["report"] in args
                and os.getpgid(pid) == pid
            ):
                os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        repository = Repository(paths, store.repository(row["repository"]))
        for worktree in repository.worktrees.glob(row["id"] + "*"):
            repository.cleanup(worktree)
        with store.db:
            store.db.execute(
                "UPDATE measurements SET state='interrupted',finished=?,error=? WHERE id=?",
                (time.time(), "Previous measurement was interrupted", row["id"]),
            )


async def measure_one(
    paths, config, store, repository_record, revision, run_tests, skip_source, progress
):
    identity = uuid.uuid4().hex
    artifact = paths.root / "artifacts" / identity
    artifact.mkdir(mode=0o700)
    with store.db:
        store.db.execute(
            "INSERT INTO measurements(id,repository,state,created,artifact) VALUES(?,?,'running',?,?)",
            (identity, repository_record["name"], time.time(), str(artifact)),
        )
    repository = Repository(paths, {**repository_record, "directory": None})
    client = GitHub(config.github.for_account(repository_record["github_account"]))
    reporter = Reporter(store, client, config, paths)
    worktree = None
    try:
        progress("preparing pinned snapshot")
        sha, worktree = await prepare_snapshot(
            repository,
            identity,
            config.github.for_account(repository_record["github_account"]).token.get_secret_value(),
            revision,
        )
        with store.db:
            store.db.execute("UPDATE measurements SET sha=? WHERE id=?", (sha, identity))
        factors, status, evidence, fingerprint = {}, {}, {}, None
        profile = config.measurement.tests.get(repository_record["name"])
        policy = config.measurement.model_dump(exclude={"tests"})
        policy["runtime_normalization"] = "min(median_seconds/budget_seconds,1)"
        if profile:
            policy["tests"] = {
                **profile.model_dump(exclude={"command", "environment"}),
                "command_fingerprint": digest(profile.command),
                "environment_settings_fingerprint": digest(profile.environment),
            }
        if skip_source or not run_tests:
            progress("loading historical stage results from GitHub")
            await reporter.project_repository_records(repository_record["name"])
            history = projected_measurements(store, repository_record["name"])
            for stage, names, skipped in (
                ("source", FACTORS[:3], skip_source),
                ("tests", FACTORS[3:], not run_tests),
            ):
                if not skipped:
                    continue
                previous = historical_stage(history, policy, names)
                if previous is None:
                    status.update(
                        dict.fromkeys(names, "Stage skipped; no compatible GitHub history")
                    )
                    continue
                factors.update(
                    {
                        name: previous.factors[name]
                        for name in names
                        if previous.factor_status.get(name) == "measured"
                        and name in previous.factors
                    }
                )
                status.update({name: previous.factor_status.get(name, "unknown") for name in names})
                evidence[stage] = previous.evidence.get(stage, {})
                evidence.setdefault("reused", {})[stage] = previous.evidence.get("reused", {}).get(
                    stage,
                    {
                        "id": previous.id,
                        "sha": previous.sha,
                        "measured_at": previous.measured_at,
                        "environment_fingerprint": previous.environment_fingerprint,
                    },
                )
                if stage == "tests":
                    fingerprint = previous.environment_fingerprint
                progress(f"reusing {stage} results from {previous.id[:12]}")
        if not skip_source:
            progress("measuring Python source")
            analysis = asyncio.create_task(
                asyncio.to_thread(python_measurement, worktree, config.measurement)
            )
            try:
                added, reasons, source = await asyncio.shield(analysis)
            except asyncio.CancelledError:
                await analysis
                raise
            factors.update(added)
            status.update(reasons)
            evidence["source"] = source
        if run_tests and profile:
            added, reasons, tests, fingerprint = await run_test_measurement(
                repository,
                identity,
                config.github.for_account(
                    repository_record["github_account"]
                ).token.get_secret_value(),
                sha,
                profile,
                artifact,
                progress,
                config.measurement.source_roots,
            )
            factors.update(added)
            status.update(reasons)
            evidence["tests"] = tests
        elif run_tests:
            status.update(
                dict.fromkeys(
                    ("test_runtime", "test_flakiness"),
                    "No configured test profile",
                )
            )
        record = MeasurementRecord(
            id=identity,
            repository=repository_record["name"],
            sha=sha,
            analyzer_version=ANALYZER,
            policy_version=POLICY,
            coverage=min(sum(policy["weights"][name] for name in factors), 1),
            factors=factors,
            exclusions=config.measurement.exclude,
            measured_at=time.time(),
            protocol=digest(policy),
            policy=policy,
            factor_status={
                name: "measured" if name in factors else status.get(name, "unknown")
                for name in FACTORS
            },
            evidence=evidence,
            environment_fingerprint=fingerprint,
        )
        report_path = paths.root / "reports" / f"{identity}.md"

        def save(baseline=None):
            (artifact / "measurement.json").write_text(record.model_dump_json(indent=2))
            report_path.write_text(measurement_report(record, baseline))
            with store.db:
                store.db.execute(
                    "UPDATE measurements SET state='completed',finished=?,data=?,error=NULL WHERE id=?",
                    (time.time(), record.model_dump_json(), identity),
                )

        save()
        progress("reconciling GitHub measurements")
        await reporter.project_repository_records(record.repository)
        baseline = next(
            (
                previous
                for previous in projected_measurements(store, record.repository)
                if compatible(previous, record)
            ),
            None,
        )
        if baseline:
            record = record.model_copy(update={"baseline_id": baseline.id})
        save(baseline)
        progress("publishing measurement")
        issue = await reporter.dashboard(record.repository)
        for row in store.rows(
            "SELECT m.data FROM measurements m LEFT JOIN record_publications p ON p.repository=m.repository AND p.type='measurement' AND p.id=m.id WHERE m.repository=? AND m.state='completed' AND m.data IS NOT NULL AND p.id IS NULL",
            (record.repository,),
        ):
            store.queue_record(RECORD_ADAPTER.validate_json(row["data"]), issue)
        await reporter.publish_records(record.repository)
        await reporter.project_repository_records(record.repository)
        await reporter.dashboard(record.repository)
        with store.db:
            store.db.execute(
                "UPDATE measurements SET error=NULL WHERE repository=? AND id IN (SELECT id FROM record_publications WHERE repository=? AND type='measurement' AND state='published')",
                (record.repository, record.repository),
            )
        publication = store.one(
            "SELECT state,error FROM record_publications WHERE repository=? AND type='measurement' AND id=?",
            (record.repository, identity),
        )
        error = (
            publication["error"] or "Measurement publication is uncertain"
            if publication["state"] != "published"
            else None
        )
        return {
            "repository": record.repository,
            "id": identity,
            "state": "completed",
            "sha": sha,
            "score": measurement_score(record),
            "coverage": record.coverage,
            "factor_status": record.factor_status,
            "reused": evidence.get("reused", {}),
            "report": str(report_path),
            "artifact": str(artifact),
            "publication_error": error,
            "url": store.one("SELECT url FROM dashboards WHERE repository=?", (record.repository,))[
                "url"
            ],
        }
    except asyncio.CancelledError:
        with store.db:
            store.db.execute(
                "UPDATE measurements SET state='cancelled',finished=?,error=? WHERE id=? AND state='running'",
                (time.time(), "Measurement cancelled", identity),
            )
        raise
    except Exception as exc:
        message = config.redact(str(exc))[:1000]
        with store.db:
            store.db.execute(
                "UPDATE measurements SET state=CASE WHEN state='completed' THEN state ELSE 'failed' END,finished=?,error=? WHERE id=?",
                (time.time(), message, identity),
            )
        return {
            "repository": repository_record["name"],
            "id": identity,
            "state": "failed",
            "error": message,
            "artifact": str(artifact),
        }
    finally:
        await asyncio.to_thread(repository.cleanup, worktree)
        client.close()


async def run_measurements(
    paths,
    config,
    repository=None,
    *,
    revision=None,
    run_tests=True,
    skip_source=False,
    on_progress=None,
):
    if revision is not None and repository is None:
        raise ValueError("--revision requires a repository")
    if revision is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError("Measurement revision must be a full commit SHA")
    with paths.lock.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Maxwell run is active in this home") from None
        store = Store(paths.db)
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            recover_measurements(store, paths)
            repositories = store.rows(
                "SELECT * FROM repositories WHERE ? IS NULL OR name=? ORDER BY name",
                (repository, repository),
            )
            if not repositories:
                raise ValueError("No registered repositories match")
            for item in repositories:
                if not config.github.for_account(item["github_account"]).token.get_secret_value():
                    raise ValueError("Configure a GitHub token for measurement publication")
            results = []
            for item in repositories:
                phase, start = "starting measurement", time.monotonic()

                def progress(value):
                    nonlocal phase
                    phase = value
                    if on_progress:
                        on_progress(
                            {
                                "repository": item["name"],
                                "phase": phase,
                                "elapsed": int(time.monotonic() - start),
                                "heartbeat": False,
                            }
                        )

                job = asyncio.create_task(
                    measure_one(
                        paths, config, store, item, revision, run_tests, skip_source, progress
                    )
                )
                try:
                    while True:
                        try:
                            result = await asyncio.wait_for(asyncio.shield(job), 15)
                            break
                        except TimeoutError:
                            if on_progress:
                                on_progress(
                                    {
                                        "repository": item["name"],
                                        "phase": phase,
                                        "elapsed": int(time.monotonic() - start),
                                        "heartbeat": True,
                                    }
                                )
                except asyncio.CancelledError:
                    job.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await job
                    raise
                results.append(result)
            return {
                "valid": all(
                    item["state"] == "completed" and not item.get("publication_error")
                    for item in results
                ),
                "measurements": results,
            }
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            store.close()
