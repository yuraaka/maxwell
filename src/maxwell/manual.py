import asyncio
import fcntl
import json
import signal

from maxwell.contracts import Usage, micros
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.progress import ScanProgress
from maxwell.reporting import Reporter
from maxwell.store import Store


async def run_scans(
    paths,
    config,
    repository,
    model,
    *,
    provider=None,
    effort=None,
    priority=2,
    on_progress=None,
):
    config.ready()
    with paths.lock.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Maxwell run is active in this home") from exc
        store = Store(paths.db)
        engine = Daemon(paths, config)
        engine.store = store
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            records = store.rows(
                "SELECT * FROM repositories WHERE ? IS NULL OR name=? ORDER BY name",
                (repository, repository),
            )
            if repository and not records:
                raise ValueError("Unknown repository")
            executions = []
            for record in records:
                account = config.github.for_account(record["github_account"])
                if not account.token.get_secret_value().strip():
                    raise ValueError(
                        f"Configure a token for GitHub account {record['github_account']!r}"
                    )
                route = config.route(provider, model, effort=effort)
                executions.append(config.execution(route, record))
            engine.recover()
            results = []
            for record, execution in zip(records, executions, strict=True):
                pending = store.one(
                    "SELECT COUNT(*) AS n FROM findings WHERE repository=? "
                    "AND state NOT IN ('closed','confirmed','rejected')",
                    (record["name"],),
                )["n"]
                if pending >= config.limits.human_queue_max:
                    client = GitHub(config.github.for_account(record["github_account"]))
                    try:
                        await Reporter(store, client, config, paths).refresh_findings(
                            record["name"], process_commands=False
                        )
                    finally:
                        client.close()
                identifier = store.enqueue(record["name"], priority, execution=execution)
                job = store.claim(
                    micros(config.limits.daily_budget_usd),
                    micros(config.limits.scan_reservation_usd),
                    config.limits.human_queue_max,
                    daily_scan_limit=config.limits.daily_scan_limit,
                    scan_id=identifier,
                    manual=True,
                )
                if job is None:
                    reasons = store.admission_blockers(
                        identifier,
                        micros(config.limits.daily_budget_usd),
                        micros(config.limits.scan_reservation_usd),
                        config.limits.human_queue_max,
                        config.limits.daily_scan_limit,
                    )
                    store.fail_queued(
                        identifier,
                        "Scan blocked: " + "; ".join(reasons)
                        if reasons
                        else "Scan could not be admitted; retry the command",
                    )
                else:
                    if on_progress:
                        on_progress({**job, "effort": execution["settings"].get("effort")})
                    progress = ScanProgress(job, on_progress)
                    async with progress.watch():
                        await engine.scan(job, on_progress=progress.update)
                    if task.cancelling():
                        raise asyncio.CancelledError
                current = store.one("SELECT * FROM scans WHERE id=?", (identifier,))
                publication_errors = []
                if job is not None:
                    if on_progress:
                        on_progress({**current, "state": "publishing"})
                    progress.phase = "publishing results to GitHub"
                    client = GitHub(config.github.for_account(record["github_account"]))
                    try:
                        async with progress.watch():
                            await Reporter(store, client, config, paths).sync(
                                repository=record["name"], process_commands=False
                            )
                    except Exception as exc:
                        publication_errors.append(config.redact(str(exc)))
                    finally:
                        client.close()
                    publication_errors.extend(
                        row["error"] or "GitHub publication outcome is uncertain"
                        for row in store.rows(
                            "SELECT error FROM findings WHERE repository=? "
                            "AND (error IS NOT NULL OR state='uncertain') "
                            "UNION ALL SELECT error FROM dashboards WHERE repository=? "
                            "AND (error IS NOT NULL OR state='uncertain')",
                            (record["name"], record["name"]),
                        )
                    )
                report = paths.root / "reports" / f"{identifier}.md"
                result = {
                    key: current[key]
                    for key in (
                        "id",
                        "repository",
                        "state",
                        "provider",
                        "model",
                        "sha",
                        "summary",
                        "error",
                        "admitted",
                    )
                }
                result.update(
                    accounting="tokens",
                    usage=Usage.model_validate_json(current["usage"] or "{}").model_dump(),
                    selection=json.loads(current["selection"]) if current["selection"] else None,
                    effort=execution["settings"].get("effort"),
                    report=str(report) if report.exists() else None,
                    publication_errors=publication_errors,
                    findings=store.rows(
                        "SELECT f.id,f.state,f.url FROM findings f "
                        "JOIN finding_observations o ON o.finding_id=f.id WHERE o.scan_id=?",
                        (identifier,),
                    ),
                )
                results.append(result)
                if on_progress:
                    on_progress(result)
            return {
                "scans": results,
                "valid": all(
                    result["state"] == "completed" and not result["publication_errors"]
                    for result in results
                ),
            }
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            store.close()
