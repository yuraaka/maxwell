import asyncio
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from maxwell.assessment import active_assessments, collect_targets, finding_assessments
from maxwell.contracts import micros
from maxwell.github import GitHub
from maxwell.impact import fix_queue, input_assessments, repair_decision
from maxwell.measurement import projected_measurements
from maxwell.measurement_runner import measure_one
from maxwell.progress import ScanProgress
from maxwell.records import RECORD_ADAPTER
from maxwell.repair import collect_repair_targets, merge_repair_problem
from maxwell.repair_pull_requests import active_validations, records
from maxwell.reporting import Reporter
from maxwell.validation import collect_validation_targets

POOLS = ("hunt", "review", "audit", "repair", "validate", "integrate")
STAGES = ("integrate", "validate", "repair", "audit", "review", "hunt")


@dataclass
class PipelineItem:
    issue: int
    identity: str
    pool: str | None
    stage: str | None
    reason: str = ""
    outcome: str | None = None


@dataclass
class PipelineSnapshot:
    issues: list
    items: list[PipelineItem]
    head: str
    reservations: dict[str, int] = field(default_factory=dict)

    def occupancy(self):
        counts = Counter(item.pool for item in self.items if item.pool)
        counts.update(self.reservations)
        return {stage: counts[stage] for stage in POOLS}

    def admit(self, capacities, ordered):
        occupied = {
            stage: min(self.reservations.get(stage, 0), capacities[stage]) for stage in POOLS
        }
        admitted = []
        for item in ordered:
            if item.pool and occupied[item.pool] < capacities[item.pool]:
                admitted.append(item)
                occupied[item.pool] += 1
        totals = self.occupancy()
        backlog = {stage: totals[stage] - occupied[stage] for stage in POOLS}
        return admitted, occupied, backlog


def classify_item(
    issue, finding, assessments, repair, link, pull, votes, integrations, head, policy
):
    def item(pool, stage=None, reason="", outcome=None):
        return PipelineItem(issue, finding.id, pool, stage, reason, outcome)

    if pull and pull.get("merged"):
        completed = [
            record for record in integrations if record.sha == head and record.state == "completed"
        ]
        if completed:
            accepted = all(record.outcome == "accepted" for record in completed)
            return item(
                "integrate",
                reason="integration publication pending"
                if accepted
                else "integration requires attention",
            )
        return item("integrate", "integrate")
    if link:
        if not pull or pull["state"] != "open":
            return item("repair", reason="repair PR is closed without merge")
        if not repair or set(repair.references) != {
            record.id for record in input_assessments(finding, assessments)
        }:
            return item("repair", reason="repair assessment inputs changed")
        if any(record.state == "failed" and record.requires_rebase for record in votes):
            return item("repair", "repair")
        completed = [record for record in votes if record.state == "completed"]
        if any(record.outcome == "rejected" for record in completed):
            return item("repair", "repair")
        if any(record.outcome == "inconclusive" for record in completed):
            return item(
                "repair",
                reason="validation is inconclusive; fresh evidence or explicit --force required",
            )
        identities = {(record.agent.adapter, record.agent.model) for record in completed}
        required = policy.required_validations if policy.enabled else 1
        if not completed or len(identities) < required:
            return item("repair", "validate")
        return item(
            "validate",
            reason="awaiting merge"
            if policy.enabled
            else "auto-merge disabled; merge the PR manually or enable repair.auto_merge",
        )
    reviews, audits = finding_assessments(assessments, finding)
    humans = active_assessments(
        [
            record
            for record in assessments
            if record.finding_id == finding.id and record.sha == finding.sha
        ],
        "human",
    )
    if not humans:
        if not reviews:
            return item("hunt", "review")
        if not audits:
            return item("review", "audit")
    decision, level, _, _ = repair_decision(finding, assessments)
    if not decision:
        decisions = humans or audits
        if decisions and all(record.verdict == "rejected" for record in decisions):
            return item(None, outcome="rejected")
        return item(
            "audit", reason="current assessments are inconclusive, conflicting, or not actionable"
        )
    if level not in ("audited", "human"):
        return item("review", "audit")
    if (
        repair
        and repair.outcome in ("blocked", "checks_failed")
        and set(repair.references)
        == {record.id for record in input_assessments(finding, assessments)}
    ):
        return item(
            "audit",
            reason="fixer blocked or checks failed; new assessment evidence or explicit --force required",
        )
    return item("audit", "repair")


async def load_snapshot(reporter, repository):
    await reporter.sync(repository, process_commands=False)
    issues = await reporter.project_repository_records(repository)
    opened = {
        item["number"]
        for item in issues
        if item.get("state") == "open" and "pull_request" not in item
    }
    registration = reporter.store.repository(repository)
    commit = await reporter.call(
        reporter.github.request, "GET", f"repos/{repository}/commits/{registration['ref']}"
    )
    assessments = records(reporter.store, repository, "assessment")
    attempts = records(reporter.store, repository, "repair")
    repairs = {record.id: record for record in attempts}
    latest_repairs = {}
    for attempt in attempts:
        latest_repairs.setdefault(attempt.finding_id, attempt)
    latest = {}
    for link in records(reporter.store, repository, "pull_request"):
        latest.setdefault(link.finding_id, link)
    items = []
    for row in reporter.store.rows(
        "SELECT issue,data FROM github_records WHERE repository=? AND type='finding' AND status='current'",
        (repository,),
    ):
        finding = RECORD_ADAPTER.validate_json(row["data"])
        if row["issue"] not in opened or finding.finding.identity(repository) != finding.id:
            continue
        link = latest.get(finding.id)
        pull = await reporter.call(reporter.github.pull, repository, link.number) if link else None
        item = classify_item(
            row["issue"],
            finding,
            assessments,
            repairs.get(link.repair_id) if link else latest_repairs.get(finding.id),
            link,
            pull,
            active_validations(reporter.store, repository, link) if link else [],
            active_validations(reporter.store, repository, link, "integration") if link else [],
            commit["sha"],
            reporter.config.repair.auto_merge,
        )
        if link and (item.stage in ("repair", "validate") or item.pool == "validate"):
            try:
                problem = await merge_repair_problem(reporter, repository, pull)
            except (httpx.HTTPError, ValueError) as exc:
                item.stage = None
                item.reason = (
                    "Cannot inspect PR merge blocker; check token Checks/Commit statuses read access or retry: "
                    + reporter.config.redact(str(exc))
                )
            else:
                if problem:
                    item.pool, item.stage, item.reason = "repair", "repair", ""
        if (
            item.pool == "validate"
            and link
            and link.number in reporter.merge_statuses
            and not item.reason.startswith("Cannot inspect")
        ):
            item.reason = f"PR #{link.number}: auto-merge {reporter.merge_statuses[link.number]}"
        items.append(item)
    known = {item.identity for item in items}
    reservations = {
        "hunt": {
            row["id"]
            for row in reporter.store.rows(
                "SELECT id FROM findings WHERE repository=? AND state IN ('pending','uncertain')",
                (repository,),
            )
            if row["id"] not in known
        }
    }
    by_identity = {item.identity: item for item in items}
    for row in reporter.store.rows(
        "SELECT data FROM record_publications WHERE repository=? AND state!='published' "
        "AND type IN ('assessment','repair','validation','integration')",
        (repository,),
    ):
        record = RECORD_ADAPTER.validate_json(row["data"])
        item = by_identity.get(record.finding_id)
        if not item:
            continue
        destination = (
            {"reviewer": "review", "auditor": "audit"}.get(record.role)
            if record.type == "assessment"
            else "repair"
            if record.type == "repair" and record.outcome == "prepared"
            else "validate"
            if record.type == "validation" and record.outcome == "accepted"
            else None
        )
        if destination and item.pool != destination:
            reservations.setdefault(destination, set()).add(item.identity)
        item.stage = None
        item.reason = "agent evidence is awaiting GitHub publication"
    return PipelineSnapshot(
        issues, items, commit["sha"], {stage: len(ids) for stage, ids in reservations.items()}
    )


def has_room(snapshot, item, stage, capacities):
    if stage == "integrate":
        return True
    return (
        item is not None and item.pool == stage or snapshot.occupancy()[stage] < capacities[stage]
    )


def fingerprint(stage, target, head):
    value = target.model_dump(mode="json") if target else {"hunt": True}
    if stage == "repair":
        value["maintained_head"] = head
    # Failed execution IDs do not constitute new evidence.
    value.pop("supersedes", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class AttemptLedger:
    def __init__(self, store, config, infinite):
        self.store, self.config, self.infinite = store, config, infinite
        self.started = time.time()

    def blocker(self, repository, stage, subject, identity):
        row = self.store.one(
            "SELECT * FROM pipeline_attempts WHERE repository=? AND stage=? AND subject=? AND fingerprint=?",
            (repository, stage, subject, identity),
        )
        interval = (
            self.config.limits.scan_interval_seconds
            if stage == "hunt"
            else self.config.pipeline.retry_seconds
        )
        latest = self.store.one(
            "SELECT MAX(attempted) AS attempted FROM pipeline_attempts WHERE repository=? AND stage=? AND subject=?",
            (repository, stage, subject),
        )["attempted"]
        if stage in ("repair", "hunt") and latest is not None:
            if stage == "repair" and not self.infinite and latest >= self.started:
                return (
                    "repair already attempted in this batch; remaining feedback needs another run"
                )
            if time.time() < latest + interval:
                return f"{stage} cooldown; retry at {datetime.fromtimestamp(latest + interval, UTC):%H:%M:%S UTC}"
        if row:
            if row["state"] == "completed" and stage != "hunt":
                return "already attempted with these inputs; waiting for new evidence"
            if not self.infinite and row["attempted"] >= self.started:
                return row["error"] or "attempt failed in this batch"
            if time.time() < row["attempted"] + interval:
                return row["error"] or "retry cooldown"
        return None

    def save(self, repository, stage, subject, identity, state, error=None):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO pipeline_attempts VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(repository,stage,subject,fingerprint) DO UPDATE SET "
                "attempted=excluded.attempted,state=excluded.state,error=excluded.error",
                (repository, stage, subject, identity, time.time(), state, error),
            )


@dataclass
class PipelineRepository:
    record: dict
    reporter: Reporter
    scope: set | None = None
    initial: dict = field(default_factory=dict)
    snapshot: PipelineSnapshot | None = None
    counts: Counter = field(default_factory=Counter)
    waiting: list = field(default_factory=list)
    failed: int = 0
    measured_head: str | None = None
    deferred: set = field(default_factory=set)
    pools: dict = field(default_factory=dict)
    backlog: dict = field(default_factory=dict)


async def targets_for(reporter, repository, snapshot, stage, issue):
    if stage == "hunt":
        return [None]
    if stage in ("review", "audit"):
        return await collect_targets(
            reporter, repository, stage, issue=issue, issues=snapshot.issues
        )
    if stage == "repair":
        return await collect_repair_targets(
            reporter, repository, issue=issue, issues=snapshot.issues
        )
    return await collect_validation_targets(
        reporter,
        repository,
        issue=issue,
        integrate=stage == "integrate",
        missing_ok=True,
        issues=snapshot.issues,
    )


def admission_blocker(store, config, repository, execution):
    settings = execution["settings"]
    accounting = (
        "usd"
        if all(
            settings.get(key) is not None for key in ("input_usd_per_mtok", "output_usd_per_mtok")
        )
        else "unpriced"
    )
    reasons = store.execution_blockers(
        repository,
        execution["stage"],
        accounting,
        micros(config.limits.daily_budget_usd),
        micros(config.limits.scan_reservation_usd),
        config.limits.human_queue_max,
        config.limits.daily_scan_limit,
    )
    if any("Daily" in reason for reason in reasons):
        reset = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )
        reasons.append(f"next daily reset {reset:%Y-%m-%d %H:%M UTC}; no agent started")
    return "; ".join(reasons)


async def wait_for_work(seconds, progress):
    async with progress.watch():
        await asyncio.sleep(seconds)


async def run_pipeline(
    paths,
    config,
    store,
    engine,
    registrations,
    selector,
    *,
    infinite,
    priority,
    on_progress,
    execute,
):
    from maxwell.manual import independent_agents

    capacities = config.pipeline.pools.model_dump(by_alias=True)
    ledger = AttemptLedger(store, config, infinite)
    contexts = []
    errors = []
    recent = []
    measurements = []

    def emit(context, state, **values):
        if on_progress:
            on_progress({"repository": context.record["name"], "state": state, **values})

    try:
        for registration in registrations:
            client = GitHub(
                config.github.for_account(registration["github_account"]),
                account=registration["github_account"],
            )
            reporter = Reporter(
                store, client, config, paths, merge_issues=set() if not infinite else None
            )
            contexts.append(PipelineRepository(registration, reporter))
        if on_progress:
            on_progress(
                {
                    "repository": registrations[0]["name"]
                    if len(registrations) == 1
                    else "all repositories",
                    "state": "plan",
                    "stages": list(POOLS),
                    "pipeline": "infinite" if infinite else "repair-all",
                }
            )
        while True:
            progressed = False
            deferred_work = False
            for context in contexts:
                repository = context.record["name"]
                context.waiting = []
                preparation = ScanProgress({"repository": repository}, on_progress)
                context.reporter.github.on_status = preparation.update
                preparation.update("reconciling pipeline with GitHub")
                try:
                    async with preparation.watch():
                        snapshot = await load_snapshot(context.reporter, repository)
                    context.snapshot = snapshot
                    if context.scope is None and not infinite:
                        context.scope = {item.identity for item in snapshot.items}
                        context.initial = {item.identity: item.issue for item in snapshot.items}
                        context.reporter.merge_issues = set(context.initial.values())
                        async with preparation.watch():
                            snapshot = await load_snapshot(context.reporter, repository)
                        context.snapshot = snapshot
                    candidates = {stage: [] for stage in STAGES}
                    scoped = [
                        item
                        for item in snapshot.items
                        if context.scope is None or item.identity in context.scope
                    ]
                    remote = {item["number"]: item for item in snapshot.issues}
                    maintenance, bugs = fix_queue(
                        store, repository, remote, projected_measurements(store, repository)
                    )
                    order = [item["issue"] for item in maintenance if item["impact"].rate]
                    order += [item["issue"] for item in bugs if item["issue"] not in order]
                    order += [item["issue"] for item in maintenance if item["issue"] not in order]
                    ranks = {number: index for index, number in enumerate(order)}
                    context.deferred.intersection_update(item.identity for item in snapshot.items)
                    ordered = sorted(
                        snapshot.items,
                        key=lambda item: (
                            context.scope is not None and item.identity not in context.scope,
                            item.stage is None or item.identity in context.deferred,
                            ranks.get(item.issue, len(ranks)),
                            item.issue,
                        ),
                    )
                    admitted, context.pools, context.backlog = snapshot.admit(capacities, ordered)
                    dashboard = store.one(
                        "SELECT issue FROM dashboards WHERE repository=?", (repository,)
                    )
                    emit(
                        context,
                        "pools",
                        pools=context.pools,
                        backlog=context.backlog,
                        capacities=capacities,
                        counts=dict(context.counts),
                        status_url=f"https://github.com/{repository}/issues/{dashboard['issue']}"
                        if dashboard and dashboard["issue"]
                        else None,
                    )
                    for item in scoped:
                        if item.reason:
                            context.waiting.append(f"#{item.issue}: {item.reason}")
                    for item in admitted:
                        if context.scope is not None and item.identity not in context.scope:
                            continue
                        if item.stage:
                            candidates[item.stage].append(item)
                    for stage, count in context.backlog.items():
                        if count:
                            context.waiting.append(
                                f"{stage} backlog: {count} waiting for a pool slot"
                            )
                    if infinite:
                        candidates["hunt"] = [None]
                    executed = False
                    for stage in STAGES:
                        if executed:
                            break
                        for item in candidates[stage]:
                            if not has_room(snapshot, item, stage, capacities):
                                context.waiting.append(
                                    f"{stage} pool full ({snapshot.occupancy()[stage]}/{capacities[stage]})"
                                )
                                continue
                            subject = item.identity if item else "hunt"
                            issue = item.issue if item else None
                            try:
                                targets = await targets_for(
                                    context.reporter, repository, snapshot, stage, issue
                                )
                                if not targets:
                                    context.waiting.append(f"#{issue}: no eligible {stage} target")
                                    if subject not in context.deferred:
                                        context.deferred.add(subject)
                                        deferred_work = True
                                    continue
                                target = targets[0]
                                identity = fingerprint(stage, target, snapshot.head)
                                blocked = ledger.blocker(repository, stage, subject, identity)
                                if blocked:
                                    context.waiting.append(
                                        f"{stage} #{issue}: {blocked}" if issue else blocked
                                    )
                                    if item and subject not in context.deferred:
                                        context.deferred.add(subject)
                                        deferred_work = True
                                    continue
                                route, decision = selector.select(
                                    store, repository, stage, avoid=independent_agents(target)
                                )
                                if (
                                    stage in ("validate", "integrate")
                                    and decision["independence"] != "different_model"
                                ):
                                    context.waiting.append(
                                        f"#{issue}: no independent validation model available"
                                    )
                                    if subject not in context.deferred:
                                        context.deferred.add(subject)
                                        deferred_work = True
                                    continue
                                execution = {
                                    **config.execution(route, context.record),
                                    "agent_selection": decision,
                                }
                                if target:
                                    execution.update(
                                        stage=stage, target=target.model_dump(mode="json")
                                    )
                                if stage == "hunt":
                                    slots = capacities["hunt"] - snapshot.occupancy()["hunt"]
                                    execution["publication_slots"] = slots
                                    execution["limits"]["max_findings_per_scan"] = min(
                                        slots, config.limits.max_findings_per_scan
                                    )
                                blocked = admission_blocker(store, config, repository, execution)
                                if blocked:
                                    context.waiting.append(blocked)
                                    continue
                                if stage == "hunt" and context.measured_head != snapshot.head:
                                    context.measured_head = snapshot.head
                                    emit(context, "stage", stage="measure", status="running")
                                    async with preparation.watch():
                                        measurement = await measure_one(
                                            paths,
                                            config,
                                            store,
                                            context.record,
                                            None,
                                            True,
                                            False,
                                            preparation.update,
                                        )
                                    measurements.append(measurement)
                                    measurements[:] = measurements[-len(contexts) :]
                                    if measurement["state"] != "completed" or measurement.get(
                                        "publication_error"
                                    ):
                                        errors.append(
                                            {
                                                "repository": repository,
                                                "stage": "measure",
                                                "error": measurement.get("error")
                                                or measurement.get("publication_error"),
                                            }
                                        )
                                        errors[:] = errors[-20:]
                                emit(
                                    context,
                                    "stage",
                                    stage=stage,
                                    status="running",
                                    subject=f"issue #{issue}" if issue else "",
                                    detail="starting agent" if issue else "discovering findings",
                                )
                                ledger.save(repository, stage, subject, identity, "running")
                                try:
                                    result = await execute(
                                        paths,
                                        config,
                                        store,
                                        engine,
                                        context.record,
                                        execution,
                                        stage=stage,
                                        priority=priority,
                                        on_progress=on_progress,
                                        merge_issues=context.reporter.merge_issues,
                                    )
                                except Exception as exc:
                                    ledger.save(
                                        repository,
                                        stage,
                                        subject,
                                        identity,
                                        "failed",
                                        config.redact(str(exc)),
                                    )
                                    raise
                                success = (
                                    result["state"] == "completed"
                                    and not result["publication_errors"]
                                )
                                message = result.get("error") or "; ".join(
                                    result["publication_errors"]
                                )
                                needs_evidence = (
                                    stage == "repair"
                                    and not result["publication_errors"]
                                    and any(
                                        repair["outcome"] in ("blocked", "checks_failed")
                                        for repair in result.get("repairs", [])
                                    )
                                )
                                ledger.save(
                                    repository,
                                    stage,
                                    subject,
                                    identity,
                                    "completed" if success or needs_evidence else "failed",
                                    message,
                                )
                                context.counts[stage] += 1
                                context.failed += not success
                                if not success:
                                    context.deferred.add(subject)
                                else:
                                    context.deferred.discard(subject)
                                if not success:
                                    errors.append(
                                        {
                                            "repository": repository,
                                            "stage": stage,
                                            "error": message or result["state"],
                                        }
                                    )
                                    errors[:] = errors[-20:]
                                recent.append(result)
                                recent[:] = recent[-20:]
                                emit(
                                    context,
                                    "stage",
                                    stage=stage,
                                    status="completed" if success else "failed",
                                )
                                progressed = executed = True
                                break
                            except Exception as exc:
                                if item and subject not in context.deferred:
                                    context.deferred.add(subject)
                                    deferred_work = True
                                message = config.redact(str(exc))
                                context.waiting.append(f"{stage} #{issue}: {message}")
                                errors.append(
                                    {"repository": repository, "stage": stage, "error": message}
                                )
                                errors[:] = errors[-20:]
                except Exception as exc:
                    message = config.redact(str(exc))
                    context.waiting.append(message)
                    errors.append({"repository": repository, "stage": "pipeline", "error": message})
                    errors[:] = errors[-20:]
                context.waiting = list(dict.fromkeys(context.waiting))
            if progressed or deferred_work:
                continue
            if not infinite:
                break
            wait = config.pipeline.poll_seconds
            reasons = [
                f"{context.record['name']}: {reason}"
                for context in contexts
                for reason in context.waiting
            ]
            detail = "; ".join(reasons[:3]) or "queues idle"
            progress = ScanProgress({"repository": contexts[0].record["name"]}, on_progress)
            progress.update(f"Waiting: {detail}; checking again in {wait:g}s · Ctrl+C to stop")
            await wait_for_work(wait, progress)
        summaries = []
        for context in contexts:
            snapshot = context.snapshot
            scoped = (
                [
                    item
                    for item in snapshot.items
                    if context.scope is None or item.identity in context.scope
                ]
                if snapshot
                else []
            )
            closed = (
                {item["number"] for item in snapshot.issues if item.get("state") == "closed"}
                if snapshot
                else set()
            )
            summaries.append(
                {
                    "repository": context.record["name"],
                    "executions": dict(context.counts),
                    "closed": len(set(context.initial.values()) & closed),
                    "rejected": sum(item.outcome == "rejected" for item in scoped),
                    "remaining": sum(item.outcome != "rejected" for item in scoped),
                    "failed": context.failed,
                    "pools": context.pools,
                    "backlog": context.backlog,
                    "waiting": context.waiting[:10],
                }
            )
        return {
            "pipeline": "infinite" if infinite else "repair-all",
            "pipeline_repositories": summaries,
            "scans": recent,
            "measurements": measurements,
            "skipped": [],
            "errors": errors,
            "valid": not errors and not any(context.failed for context in contexts),
            "statuses": [
                {
                    "repository": context.record["name"],
                    "url": f"https://github.com/{context.record['name']}/issues/{row['issue']}",
                }
                for context in contexts
                if (
                    row := store.one(
                        "SELECT issue FROM dashboards WHERE repository=?", (context.record["name"],)
                    )
                )
                and row["issue"]
            ],
        }
    finally:
        for context in contexts:
            context.reporter.github.close()
