import asyncio
import fcntl
import json
import logging
import signal

from maxwell.agent_selection import AgentSelector
from maxwell.assessment import collect_targets
from maxwell.contracts import Usage, micros
from maxwell.daemon import Daemon
from maxwell.github import GitHub
from maxwell.measurement_runner import measure_one, recover_measurements
from maxwell.progress import ScanProgress
from maxwell.repair import collect_repair_targets
from maxwell.reporting import Reporter
from maxwell.stages import (
    SKIPPABLE_STAGES,
    select_stages,
    stored_stage,
)
from maxwell.store import Store
from maxwell.validation import collect_validation_targets


async def execute_job(
    paths,
    config,
    store,
    engine,
    record,
    execution,
    *,
    stage,
    priority,
    on_progress,
    merge_issues=None,
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
    execution = {
        **execution,
        "stage": stored_stage(stage),
        "merge_issues": sorted(merge_issues) if merge_issues is not None else None,
    }
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
                    "stage": stage,
                    "effort": execution["settings"].get("effort"),
                    "agent_selection": execution.get("agent_selection"),
                }
            )
        progress = ScanProgress({**job, "stage": stage}, on_progress)
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
                await Reporter(store, client, config, paths, merge_issues=merge_issues).sync(
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
            "executed",
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
        validations=[
            {**json.loads(row["data"]), "issue": row["issue"], "publication_state": row["state"]}
            for row in store.rows(
                "SELECT data,issue,state FROM record_publications WHERE repository=? "
                "AND type IN ('validation','integration') AND json_extract(data,'$.execution_id')=?",
                (record["name"], identifier),
            )
        ],
        pull_requests=[
            json.loads(row["data"])
            for row in store.rows(
                "SELECT data FROM github_records WHERE repository=? AND type='pull_request' AND status='current' "
                "AND (json_extract(data,'$.repair_id') IN (SELECT id FROM record_publications WHERE repository=? AND type='repair' AND json_extract(data,'$.execution_id')=?) "
                "OR id IN (SELECT json_extract(data,'$.pr_id') FROM record_publications WHERE repository=? AND type IN ('validation','integration') AND json_extract(data,'$.execution_id')=?))",
                (record["name"], record["name"], identifier, record["name"], identifier),
            )
        ],
        repairs=[
            {**json.loads(row["data"]), "issue": row["issue"], "publication_state": row["state"]}
            for row in store.rows(
                "SELECT data,issue,state FROM record_publications WHERE repository=? "
                "AND type='repair' AND json_extract(data,'$.execution_id')=?",
                (record["name"], identifier),
            )
        ],
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
    if target and hasattr(target, "repair"):
        agents = [target.repair.agent, *getattr(target, "validator_agents", [])]
        return {(agent.adapter, agent.model) for agent in agents}
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
    skip_stages=(),
    drain=False,
    infinite=False,
):
    stages = select_stages(stage)
    single_stage = stages[0] if stage is not None and len(stages) == 1 else None
    if drain and infinite:
        raise ValueError("Choose either --drain or --infinite")
    if (drain or infinite) and (issue is not None or limit is not None or force or skip_stages):
        raise ValueError(
            "--drain/--infinite cannot be combined with --issue, --limit, --force, or --skip"
        )
    if (drain or infinite) and model is not None:
        raise ValueError(
            "--drain/--infinite require automatic model selection for independent review; omit --model"
        )
    if single_stage in (None, "hunt") and (issue is not None or limit is not None or force):
        raise ValueError(
            "--issue, --limit, and --force require a single explicit validate, audit, fix, review, or integrate stage"
        )
    if single_stage in ("fix", "review", "integrate") and limit is not None:
        raise ValueError(
            "fix, review, and integrate handle one finding per run; --limit is not supported"
        )
    if issue is not None and repository is None:
        raise ValueError("--issue requires a repository")
    if (issue is not None and issue < 1) or (limit is not None and limit < 1):
        raise ValueError("--issue and --limit must be positive integers")
    if set(skip_stages) - set(SKIPPABLE_STAGES):
        raise ValueError("Unknown skipped stage")
    if stage is not None and skip_stages:
        raise ValueError(
            "--skip applies to the default full scan; use --stage to select a stage or interval"
        )
    config.ready()
    with paths.lock.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another Maxwell run is active in this home") from exc
        store = Store(paths.db)
        engine = Daemon(paths, config)
        engine.store = store
        log_handler = logging.FileHandler(paths.log)
        log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        engine.log = logging.Logger("maxwell.manual")
        engine.log.addHandler(log_handler)
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
            recover_measurements(store, paths)
            if drain or infinite:
                from maxwell.pipeline import run_pipeline

                return await run_pipeline(
                    paths,
                    config,
                    store,
                    engine,
                    records,
                    selector,
                    infinite=infinite,
                    stages=stages,
                    measure_before_hunt=stage is None,
                    priority=priority,
                    on_progress=on_progress,
                    execute=execute_job,
                )
            results, skipped, errors, measurements = [], [], [], []
            for record in records:
                if single_stage in ("fix", "review", "integrate") and results:
                    break

                def stage_event(current_stage, status, **details):
                    if on_progress:
                        on_progress(
                            {
                                "repository": record["name"],
                                "state": "stage",
                                "stage": current_stage,
                                "status": status,
                                **details,
                            }
                        )

                if on_progress:
                    on_progress(
                        {
                            "repository": record["name"],
                            "state": "plan",
                            "stages": ["measure", *stages] if stage is None else list(stages),
                        }
                    )
                client = GitHub(
                    config.github.for_account(record["github_account"]),
                    account=record["github_account"],
                )
                reporter = Reporter(store, client, config, paths)

                async def launch(job_stage, target, *, avoid=()):
                    selected = AgentSelector(paths, config) if avoid else selector
                    if not selected.explicit:
                        preparation = ScanProgress(
                            {"repository": record["name"], "stage": job_stage},
                            on_progress,
                        )
                        preparation.update("loading agent evidence from GitHub")
                        client.on_status = preparation.update
                        async with preparation.watch():
                            await reporter.project_repository_records(record["name"])
                    route, decision = selected.select(
                        store,
                        record["name"],
                        job_stage,
                        avoid=avoid or independent_agents(target),
                    )
                    if (job_stage in ("review", "integrate") or avoid) and decision[
                        "independence"
                    ] != "different_model":
                        raise ValueError(
                            "Independent validation requires a different public adapter/model from the fix agent and previous review agents; select another model"
                        )
                    execution = {
                        **config.execution(route, record),
                        "agent_selection": decision,
                    }
                    if target:
                        execution.update(stage=job_stage, target=target.model_dump(mode="json"))
                    return await execute_job(
                        paths,
                        config,
                        store,
                        engine,
                        record,
                        execution,
                        stage=job_stage,
                        priority=priority,
                        on_progress=on_progress,
                    )

                try:
                    if stage is None:
                        stage_event("measure", "running")
                        progress = ScanProgress(
                            {"repository": record["name"], "stage": "measure"}, on_progress
                        )
                        try:
                            async with progress.watch():
                                measured = await measure_one(
                                    paths,
                                    config,
                                    store,
                                    record,
                                    None,
                                    "tests" not in skip_stages,
                                    "source" in skip_stages,
                                    progress.update,
                                )
                            measurements.append(measured)
                            if measured["state"] != "completed" or measured.get(
                                "publication_error"
                            ):
                                errors.append(
                                    {
                                        "repository": record["name"],
                                        "stage": "measure",
                                        "error": measured.get("error")
                                        or measured["publication_error"],
                                    }
                                )
                        except Exception as exc:
                            errors.append(
                                {
                                    "repository": record["name"],
                                    "stage": "measure",
                                    "error": config.redact(str(exc)),
                                }
                            )
                        stage_event(
                            "measure",
                            "failed"
                            if any(
                                item["repository"] == record["name"] and item["stage"] == "measure"
                                for item in errors
                            )
                            else "completed",
                        )
                    for current_stage in stages:
                        if current_stage in skip_stages:
                            stage_event(current_stage, "skipped", detail="explicitly skipped")
                            skipped.append(
                                {
                                    "repository": record["name"],
                                    "stage": current_stage,
                                    "reason": "Explicitly skipped",
                                }
                            )
                            continue
                        stage_event(current_stage, "running")
                        stage_results = []
                        stage_recovery_failed = False
                        skip_reasons = []
                        try:
                            if current_stage == "hunt":
                                targets = [None]
                            else:
                                preparation = ScanProgress(
                                    {"repository": record["name"], "stage": current_stage},
                                    on_progress,
                                )
                                preparation.update(f"loading {current_stage} findings from GitHub")
                                client.on_status = preparation.update
                                async with preparation.watch():
                                    collector = (
                                        collect_validation_targets
                                        if current_stage in ("review", "integrate")
                                        else collect_repair_targets
                                        if current_stage == "fix"
                                        else collect_targets
                                    )
                                    arguments = (
                                        {}
                                        if current_stage in ("fix", "review", "integrate")
                                        else {"stage": current_stage}
                                    )
                                    if current_stage == "integrate":
                                        arguments["integrate"] = True
                                    if current_stage in ("review", "integrate"):
                                        arguments["diagnostics"] = skip_reasons
                                    targets = await collector(
                                        reporter,
                                        record["name"],
                                        **arguments,
                                        issue=issue,
                                        force=force,
                                    )
                                configured_limit = (
                                    config.limits.max_validations_per_scan
                                    if current_stage == "validate"
                                    else config.limits.max_audits_per_scan
                                )
                                if current_stage in ("fix", "review", "integrate"):
                                    configured_limit = 1
                                targets = targets[
                                    : limit if limit is not None else configured_limit
                                ]
                                if not targets:
                                    skipped.append(
                                        {
                                            "repository": record["name"],
                                            "stage": current_stage,
                                            "reason": config.redact(
                                                "; ".join(skip_reasons[:3])
                                                + (
                                                    f" (+{len(skip_reasons) - 3} more)"
                                                    if len(skip_reasons) > 3
                                                    else ""
                                                )
                                            )
                                            if skip_reasons
                                            else "No eligible findings",
                                        }
                                    )
                            if on_progress:
                                on_progress(
                                    {
                                        "repository": record["name"],
                                        "state": "batch",
                                        "stage": current_stage,
                                        "total": len(targets),
                                        "completed": 0,
                                    }
                                )
                            for target in targets:
                                result = await launch(current_stage, target)
                                attempts = [result]
                                if (
                                    current_stage == "review"
                                    and config.review.max_fix_attempts
                                    and "fix" not in skip_stages
                                ):
                                    from maxwell.review_loop import complete_review

                                    attempts, recovery_error = await complete_review(
                                        reporter,
                                        record["name"],
                                        target,
                                        result,
                                        launch,
                                        on_progress or (lambda event: None),
                                        config.review.max_fix_attempts,
                                    )
                                    if recovery_error:
                                        errors.append(
                                            {
                                                "repository": record["name"],
                                                "stage": "review",
                                                "error": recovery_error,
                                            }
                                        )
                                        stage_recovery_failed = True
                                    result = attempts[-1]
                                results.extend(attempts)
                                stage_results.extend(attempts)
                                if (
                                    current_stage == "review"
                                    and config.fix.auto_merge.enabled
                                    and not selector.explicit
                                    and result["state"] == "completed"
                                    and not result["publication_errors"]
                                    and sum(
                                        item["stage"] == "review" and not item.get("resolved_by")
                                        for item in stage_results
                                    )
                                    < config.fix.auto_merge.required_reviews
                                    and any(
                                        item["outcome"] == "accepted"
                                        for item in result["validations"]
                                    )
                                ):
                                    following = await collect_validation_targets(
                                        reporter,
                                        record["name"],
                                        issue=target.issue,
                                        missing_ok=True,
                                    )
                                    targets.extend(following)
                                if on_progress:
                                    on_progress(
                                        {
                                            "repository": record["name"],
                                            "state": "batch",
                                            "stage": current_stage,
                                            "total": len(targets),
                                            "completed": sum(
                                                item["stage"] == current_stage
                                                and not item.get("resolved_by")
                                                for item in stage_results
                                            ),
                                        }
                                    )
                            stage_event(
                                current_stage,
                                "skipped"
                                if not targets
                                else "failed"
                                if stage_recovery_failed
                                or any(
                                    (item["state"] != "completed" or item["publication_errors"])
                                    and not item.get("resolved_by")
                                    for item in stage_results
                                )
                                else "completed",
                                total=len(targets),
                                completed=len(stage_results),
                            )
                        except Exception as exc:
                            stage_event(current_stage, "failed")
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
                "measurements": measurements,
                "statuses": [
                    {
                        "repository": record["name"],
                        "url": f"https://github.com/{record['name']}/issues/{dashboard['issue']}",
                    }
                    for record in records
                    if (
                        dashboard := store.one(
                            "SELECT issue FROM dashboards WHERE repository=?", (record["name"],)
                        )
                    )
                    and dashboard["issue"]
                ],
                "skipped": skipped,
                "errors": errors,
                "valid": not errors
                and all(
                    (result["state"] == "completed" and not result["publication_errors"])
                    or result.get("resolved_by")
                    for result in results
                ),
            }
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            store.close()
            log_handler.close()
