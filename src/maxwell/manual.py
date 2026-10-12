import asyncio
import fcntl
import json
import signal

from maxwell.agent_selection import AgentSelector
from maxwell.assessment import collect_targets
from maxwell.contracts import Usage, micros
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.progress import ScanProgress
from maxwell.reporting import Reporter
from maxwell.store import Store


async def execute_job(
    paths, config, store, engine, record, execution, *, stage, priority, on_progress
):
    task = asyncio.current_task()
    pending = store.one(
        "SELECT COUNT(*) AS n FROM findings WHERE repository=? "
        "AND state NOT IN ('closed','confirmed','rejected')",
        (record["name"],),
    )["n"]
    if stage == "hunt" and pending >= config.limits.human_queue_max:
        preparation = ScanProgress({"repository": record["name"]}, on_progress)
        preparation.update("refreshing GitHub review queue")
        client = GitHub(
            config.github.for_account(record["github_account"]),
            on_status=preparation.update,
            account=record["github_account"],
        )
        try:
            async with preparation.watch():
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
            on_progress(
                {
                    **job,
                    "effort": execution["settings"].get("effort"),
                    "agent_selection": execution.get("agent_selection"),
                }
            )
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
        client = GitHub(
            config.github.for_account(record["github_account"]),
            on_status=progress.update,
            account=record["github_account"],
        )
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
                "AND (scan_id=? OR id IN "
                "(SELECT finding_id FROM finding_observations WHERE scan_id=?)) "
                "AND (error IS NOT NULL OR state='uncertain') "
                "UNION ALL SELECT error FROM dashboards WHERE repository=? "
                "AND (error IS NOT NULL OR state='uncertain') "
                "UNION ALL SELECT error FROM histories WHERE repository=? "
                "AND (error IS NOT NULL OR state='uncertain')",
                (record["name"], identifier, identifier, record["name"], record["name"]),
            )
        )
        publication_errors.extend(
            row["error"] or "GitHub record publication outcome is uncertain"
            for row in store.rows(
                "SELECT error FROM record_publications WHERE repository=? "
                "AND (id=? OR json_extract(data,'$.execution_id')=?) "
                "AND (error IS NOT NULL OR state='uncertain')",
                (record["name"], identifier, identifier),
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
        agent_selection=execution.get("agent_selection"),
        stage=stage,
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
        assessments=[
            {
                **json.loads(row["data"]),
                "issue": row["issue"],
                "publication_state": row["state"],
            }
            for row in store.rows(
                "SELECT data,issue,state FROM record_publications WHERE repository=? "
                "AND type='assessment' AND json_extract(data,'$.execution_id')=?",
                (record["name"], identifier),
            )
        ],
    )
    if on_progress:
        on_progress(result)
    return result


def independent_agents(target):
    agents = [target.finding.agent, *(review.agent for review in target.reviews)] if target else []
    return {(agent.adapter, agent.model) for agent in agents if agent}


async def run_scans(
    paths,
    config,
    repository=None,
    model=None,
    *,
    provider=None,
    effort=None,
    priority=2,
    stage=None,
    issue=None,
    limit=None,
    force=False,
    on_progress=None,
):
    if stage not in (None, "hunt", "review", "audit"):
        raise ValueError("Stage must be hunt, review, or audit")
    if stage in (None, "hunt") and (issue is not None or limit is not None or force):
        raise ValueError("--issue, --limit, and --force apply to an explicit review or audit only")
    if issue is not None and repository is None:
        raise ValueError("--issue requires a repository")
    if (issue is not None and issue < 1) or (limit is not None and limit < 1):
        raise ValueError("--issue and --limit must be positive integers")
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
            if not records:
                return {"scans": [], "skipped": [], "errors": [], "valid": True}
            selector = AgentSelector(paths, config, provider=provider, model=model, effort=effort)
            for record in records:
                account = config.github.for_account(record["github_account"])
                if not account.token.get_secret_value().strip():
                    raise ValueError(
                        f"Configure a token for GitHub account {record['github_account']!r}"
                    )
            engine.recover()
            results, skipped, errors = [], [], []
            stages = (stage,) if stage else ("hunt", "review", "audit")
            for record in records:
                client = GitHub(
                    config.github.for_account(record["github_account"]),
                    account=record["github_account"],
                )
                reporter = Reporter(store, client, config, paths)
                try:
                    for current_stage in stages:
                        if on_progress and stage is None:
                            on_progress(
                                {
                                    "repository": record["name"],
                                    "state": "progress",
                                    "phase": f"starting {current_stage} stage",
                                    "elapsed": 0,
                                    "heartbeat": False,
                                }
                            )
                        try:
                            if current_stage == "hunt":
                                targets = [None]
                            else:
                                preparation = ScanProgress(
                                    {"repository": record["name"]}, on_progress
                                )
                                preparation.update(f"loading {current_stage} findings from GitHub")
                                client.on_status = preparation.update
                                async with preparation.watch():
                                    targets = await collect_targets(
                                        reporter,
                                        record["name"],
                                        current_stage,
                                        issue=issue,
                                        force=force,
                                    )
                                configured_limit = (
                                    config.limits.max_reviews_per_scan
                                    if current_stage == "review"
                                    else config.limits.max_audits_per_scan
                                )
                                targets = targets[
                                    : limit if limit is not None else configured_limit
                                ]
                                if not targets:
                                    skipped.append(
                                        {
                                            "repository": record["name"],
                                            "stage": current_stage,
                                            "reason": "No eligible findings",
                                        }
                                    )
                            for target in targets:
                                if not selector.explicit:
                                    preparation = ScanProgress(
                                        {"repository": record["name"]}, on_progress
                                    )
                                    preparation.update("loading agent evidence from GitHub")
                                    client.on_status = preparation.update
                                    async with preparation.watch():
                                        await reporter.project_repository_records(record["name"])
                                route, decision = selector.select(
                                    store,
                                    record["name"],
                                    current_stage,
                                    avoid=independent_agents(target),
                                )
                                execution = {
                                    **config.execution(route, record),
                                    "agent_selection": decision,
                                }
                                if target:
                                    execution.update(
                                        stage=current_stage, target=target.model_dump(mode="json")
                                    )
                                result = await execute_job(
                                    paths,
                                    config,
                                    store,
                                    engine,
                                    record,
                                    execution,
                                    stage=current_stage,
                                    priority=priority,
                                    on_progress=on_progress,
                                )
                                results.append(result)
                        except Exception as exc:
                            if stage is not None:
                                raise
                            errors.append(
                                {
                                    "repository": record["name"],
                                    "stage": current_stage,
                                    "error": config.redact(str(exc)),
                                }
                            )
                finally:
                    client.close()
            return {
                "scans": results,
                "skipped": skipped,
                "errors": errors,
                "valid": not errors
                and all(
                    result["state"] == "completed" and not result["publication_errors"]
                    for result in results
                ),
            }
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            store.close()
