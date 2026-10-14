import asyncio
import hashlib
import json
import re
import shlex
import time
from urllib.parse import urlencode

from maxwell.assessment import current_assessments, finding_factors
from maxwell.contracts import DEFAULT_SCAN_CANDIDATES, FACTORS, SelectionStats
from maxwell.github import PublicationUncertain
from maxwell.impact import fix_queue, queue_report
from maxwell.measurement import (
    compatible,
    measurement_contributions,
    measurement_report,
    measurement_score,
    projected_measurements,
)
from maxwell.records import (
    RECORD_ADAPTER,
    AgentSelection,
    AssessmentRecord,
    ExecutionRecord,
    FindingRecord,
    MeasurementRecord,
    RepairRecord,
    decode_record,
    encode_record,
    execution_record,
    public_agent,
    record_digest,
)
from maxwell.reputation import agent_reputation, reputation_report


def token_summary(usage):
    if not usage.get("reported"):
        return "Token usage unavailable"
    total = usage["input_tokens"] + usage["output_tokens"]
    return (
        f"{total:,} tokens: input {usage['input_tokens']:,} "
        f"(cached {usage.get('cached_input_tokens', 0):,}; "
        f"cache writes {usage.get('cache_write_tokens', 0):,}), "
        f"output {usage['output_tokens']:,}"
    )


def agent_selection_report(value):
    if not value:
        return ""
    decision = AgentSelection.model_validate(value)
    basis = {
        "explicit": "explicit model override",
        "thompson-source-v1": "Thompson sampling from provisional source support or reviewer–auditor agreement",
        "uniform-unrated-v1": "uniform sampling; quality is unrated",
    }[decision.method]
    body = f"\nAgent selection: {basis}; {decision.candidates} eligible public identities. Independence: {decision.independence}.\n"
    if decision.method == "thompson-source-v1":
        body += f"Provisional observations: {decision.successes} positive, {decision.failures} negative. Beta prior: (2, 2); selected draw: {decision.draw:.6f}. These are source proxies, not validated accuracy.\n"
    return body


def scan_report(scan, finding_ids=(), *, record=None):
    usage = json.loads(scan["usage"] or "{}")
    tokens = token_summary(usage)
    agent = public_agent(scan)
    identity = agent.display() if agent else "Unknown legacy agent"
    stage = scan.get("stage", "discovery")
    label = "Discovery" if stage == "discovery" else stage.title()
    result = (
        f"{scan['admitted']} new candidates"
        if stage == "discovery"
        else "Local repair attempt; awaiting independent validation"
        if stage == "repair"
        else "Source assessment; not reproduced"
    )
    body = f"""<!-- maxwell:scan:{scan["id"]} -->
### {label} scan `{scan["id"][:12]}` — {scan["state"]}

{scan["summary"] or ""}

Revision: `{scan["sha"] or "not resolved"}`  
Agent: `{identity}`

| Stage | Token usage | Result |
| --- | --- | --- |
| {label} | {tokens} | {result} |
| Reproduction | Not run | Later milestone |
| Fixing | {"See repair record" if stage == "repair" else "Not run"} | {"Local branch retained" if stage == "repair" else "Explicit repair stage"} |
| Validation / integration | Not run | Later milestone |

**{tokens}.** Includes the failed attempt, if any.
Usage complete: {usage.get("complete", False)}.
Cached input and cache writes are included in input; total is input plus output.
Missing usage is unknown, not zero. Tokens are recorded once per execution, not per finding.
"""
    execution = json.loads(scan.get("execution") or "{}")
    body += agent_selection_report(execution.get("agent_selection"))
    if target := execution.get("target"):
        body += f"\nFinding issue: #{target['issue']}; finding: `{target['finding']['id']}`.\n"
        if target.get("reviews"):
            body += (
                "Referenced reviews: "
                + ", ".join(f"`{item['id']}`" for item in target["reviews"])
                + ".\n"
            )
    if scan.get("selection"):
        selection = SelectionStats.model_validate_json(scan["selection"])
        execution = json.loads(scan.get("execution") or "{}")
        candidate_limit = execution.get("limits", {}).get(
            "max_candidates_per_scan", DEFAULT_SCAN_CANDIDATES
        )
        body += f"""
### Candidate selection

Collected: **{selection.collected}**. Selected: **{selection.selected}** new candidates.
Skipped: **{selection.skipped}**.

| Skip reason | Candidates |
| --- | --- |
| Already known | {selection.already_known} |
| Duplicate within this scan | {selection.duplicate} |
| Below confidence threshold | {selection.below_confidence} |
| Outside top publication limit | {selection.publication_limit} |
| No review queue capacity | {selection.review_queue_limit} |

Ranking: priority (P0 first), confidence (highest first), issue kind (bugs first
when tied), then stable finding identity. Known findings do not consume publication slots.
Skipped candidates are retained in the local provider result artifact; they are not queued
for automatic publication on a later scan. Collection is bounded at {candidate_limit} candidates.
"""
        if selection.collected == candidate_limit:
            body += "\nCollection reached the candidate ceiling; further issues may remain.\n"
    if scan.get("finished") and scan["state"] in (
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "skipped",
    ):
        body += "\n" + encode_record(record or execution_record(scan, finding_ids)) + "\n"
    return body


def record_report(record):
    marker = f"<!-- maxwell:{record.type}:{record.id} -->"
    if isinstance(record, AssessmentRecord):
        identity = record.agent.display() if record.agent else record.actor
        body = (
            f"{marker}\n### {record.role.title()} assessment `{record.id[:12]}`\n\n"
            f"Finding: `{record.finding_id}`; revision: `{record.sha}`\n\n"
            f"Agent / actor: `{identity}`\n\n"
            f"Verdict: **{record.verdict}**; actionability: {record.actionability}\n\n"
            f"{record.evidence}\n"
            + (
                f"\nAssessed factors: {', '.join(record.factors) or 'none established'}.\n"
                if record.factors is not None
                else ""
            )
        )
        if record.repair:
            repair = record.repair
            body += (
                f"\n#### Proposed repair (estimate, not observed)\n\n{repair.plan}\n\n"
                f"Validation plan: {repair.validation}\n\n"
                f"Effort: {repair.effort_hours.low:g}–{repair.effort_hours.high:g} engineer-hours; "
                f"uncalibrated confidence: {repair.confidence:.0%}.\n\n"
                f"Measurement: `{repair.measurement_id or 'unknown'}`; "
                f"protocol: `{repair.protocol or 'unknown'}`.\n"
            )
            for projection in repair.projections:
                scenarios = projection.architecture or projection.scenarios
                body += (
                    f"\n**{projection.factor}** counterfactual scenarios: "
                    + json.dumps([item.model_dump() for item in scenarios])
                    + f"\n\n{projection.evidence}\n\nAssumptions: {projection.assumptions}\n"
                )
            body += f"\nDeclared unchanged: {', '.join(repair.unchanged) or 'none'}.\n"
            if repair.unchanged_evidence:
                body += f"\n{repair.unchanged_evidence}\n"
        if record.execution_id:
            body += f"\nExecution: `{record.execution_id}`.\n"
        if record.supersedes:
            body += f"\nSupersedes: `{record.supersedes}`.\n"
        if record.references:
            body += "\nReferences: " + ", ".join(f"`{item}`" for item in record.references) + ".\n"
    elif isinstance(record, RepairRecord):
        body = (
            f"{marker}\n### Repair — {record.outcome}\n\n"
            f"Agent: `{record.agent.display()}`; base: `{record.sha}`\n\n"
            f"{record.summary}\n\n{record.explanation}\n\nLocal branch: `{record.branch}`\n\n"
            f"Local worktree: `{record.worktree}`\n\n"
            f"Local commit: `{record.commit or 'none'}`. Local-only; cannot be fetched from GitHub.\n\n"
            "Awaiting human inspection and independent validation; no observed entropy reduction.\n\n"
            f"Changed files: {', '.join(record.files) or 'none'}.\n\n"
            f"Regression coverage claimed by fixer: {', '.join(record.regression_tests) or 'none'}.\n\n"
            f"Execution: `{record.execution_id}`; assessment inputs: {', '.join(record.references)}.\n"
        )
        for check in record.checks:
            body += (
                f"\nHost check: **{check.state}**, exit {check.returncode}, {check.seconds:.2f}s.\n\n"
                f"Command: `{json.dumps(check.command)}`\n\n{check.detail}\n"
            )
        for concern in record.concerns:
            body += f"\n- {concern}\n"
    elif isinstance(record, ExecutionRecord):
        identity = record.agent.display() if record.agent else "Unknown legacy agent"
        body = (
            f"{marker}\n### Execution `{record.id[:12]}` — {record.state}\n\n"
            f"Agent: `{identity}`; revision: `{record.sha or 'not resolved'}`\n\n"
            f"{record.summary}\n\n{token_summary(record.usage.model_dump())}.\n"
            f"Usage complete: {record.usage.complete}.\n"
        )
        body += agent_selection_report(record.agent_selection)
    else:
        body = marker + measurement_report(record)
    return body + "\n" + encode_record(record) + "\n"


def hunter_ratings(store, repo=None):
    rows = store.rows(
        "SELECT DISTINCT s.profile, f.id, f.state FROM findings f "
        "JOIN (SELECT finding_id,scan_id FROM finding_observations UNION "
        "SELECT id AS finding_id,scan_id FROM findings) o ON o.finding_id=f.id "
        "JOIN scans s ON s.id=o.scan_id "
        "WHERE (? IS NULL OR f.repository=?) AND f.state IN ('confirmed','rejected')",
        (repo, repo),
    )
    profiles = {}
    for row in rows:
        tally = profiles.setdefault(row["profile"], {"confirmed": 0, "rejected": 0})
        tally[row["state"]] += 1
    return [
        {
            "profile": profile,
            "stage": "hunter",
            "assessments": sum(tally.values()),
            "rating": round(100 * (tally["confirmed"] + 2) / (sum(tally.values()) + 4), 1),
            "confidence": "provisional; human assessments, Beta(2,2) smoothing",
            **tally,
        }
        for profile, tally in profiles.items()
    ]


def component_dashboard(repository, measurement, baseline=None):
    contributions = (measurement_contributions(measurement) or {}) if measurement else {}
    body = f"Revision: `{measurement.sha}`.\n\n" if measurement else ""
    score = measurement_score(measurement) if measurement else None
    baseline_score = measurement_score(baseline) if baseline else None
    if (
        measurement
        and baseline
        and compatible(baseline, measurement)
        and score is not None
        and baseline_score is not None
    ):
        body += f"Change from baseline: **{score - baseline_score:+.2f} points**.\n\n"
    body += "| Component | Entropy contribution |\n| --- | --- |\n"
    for factor in FACTORS:
        value = f"{contributions[factor]:.2f} points" if factor in contributions else "Unknown"
        query = f'repo:{repository} is:issue is:open label:"maxwell:factor:{factor}" -label:"maxwell:state:rejected"'
        url = "https://github.com/issues?" + urlencode({"q": query})
        reason = ""
        if factor not in contributions and measurement:
            status = measurement.factor_status.get(factor, "Unavailable evidence")
            if status == "measured":
                status = "Incompatible analyzer or policy; remeasure"
            reason = " — " + status.replace("|", " ").replace("\n", " ")
        label = (
            "Coupling / cohesion" if factor == "coupling" else factor.replace("_", " ").capitalize()
        )
        body += f"| {label} | [{value}]({url}){reason} |\n"
    body += "\nContributions use policy weights; missing weights are not redistributed. Values are rounded to two decimals. Links show associated findings, not guaranteed reductions.\n"
    if measurement:
        body += (
            "\n<details>\n<summary>Measurement evidence and calculation</summary>\n\n"
            + measurement_report(measurement, baseline)
            + "\n</details>\n"
        )
    return body


class Reporter:
    def __init__(self, store, github, config, paths):
        self.store, self.github, self.config, self.paths = store, github, config, paths
        self.labels_ready = set()
        self.author = None

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    def error(self, table, key, value, error):
        text = self.config.redact(str(error))[:1000]
        with self.store.db:
            self.store.db.execute(f"UPDATE {table} SET error=? WHERE {key}=?", (text, value))

    async def authorized_authors(self):
        if self.author is None:
            identity = await self.call(self.github.request, "GET", "user")
            self.author = identity["login"]
        return {self.author, *self.github.allowed_users}

    async def project_issue_records(self, repo, issue):
        authors = await self.authorized_authors()
        comments = (
            []
            if issue.get("comments") == 0
            else await self.call(self.github.comments, repo, issue["number"])
        )
        sources = [(0, issue), *((comment["id"], comment) for comment in comments)]
        seen = set()
        finding_id = None
        match = re.search(r"<!-- maxwell:finding:([a-f0-9]{32}) -->", issue.get("body") or "")
        if match:
            finding_id = match[1]
        for comment_id, source in sources:
            body = source.get("body") or ""
            author = source.get("user", {}).get("login", "")
            if author not in authors:
                continue
            try:
                record = decode_record(body)
                if record is None:
                    continue
                if record.repository != repo:
                    raise ValueError("Record belongs to a different repository")
                if isinstance(record, FindingRecord):
                    if comment_id or record.id != finding_id:
                        raise ValueError("Finding record must match its issue marker")
                elif isinstance(record, (AssessmentRecord, RepairRecord)):
                    if not comment_id or record.finding_id != finding_id:
                        raise ValueError("Assessment belongs to a different finding")
                elif not comment_id:
                    raise ValueError("Execution and measurement records belong in comments")
                if not isinstance(record, FindingRecord):
                    marker = f"<!-- maxwell:{record.type}:{record.id} -->"
                    scan_marker = f"<!-- maxwell:scan:{record.id} -->"
                    if marker not in body and not (
                        isinstance(record, ExecutionRecord) and scan_marker in body
                    ):
                        raise ValueError("Record does not match its publication marker")
                self.store.project_record(
                    record,
                    issue["number"],
                    comment_id,
                    author,
                    hashlib.sha256(body.encode()).hexdigest(),
                )
                seen.add(comment_id)
            except ValueError:
                with self.store.db:
                    self.store.event(
                        "github_record_invalid",
                        {"repository": repo, "issue": issue["number"], "comment": comment_id},
                    )
        self.store.reconcile_records(repo, issue["number"], seen)

    async def project_repository_records(self, repo):
        issues = await self.call(
            lambda: list(self.github.paginate(f"repos/{repo}/issues", {"state": "all"}))
        )
        seen = set()
        for issue in issues:
            if "pull_request" not in issue and (
                "<!-- maxwell:finding:" in (issue.get("body") or "")
                or "<!-- maxwell:dashboard:v1 -->" in (issue.get("body") or "")
                or "<!-- maxwell:history:v1 -->" in (issue.get("body") or "")
            ):
                await self.project_issue_records(repo, issue)
                seen.add(issue["number"])
        for row in self.store.rows(
            "SELECT DISTINCT issue FROM github_records WHERE repository=?", (repo,)
        ):
            if row["issue"] not in seen:
                self.store.reconcile_records(repo, row["issue"], set())
        return issues

    async def reconcile_assessment_labels(self, repo, issues):
        remote = {issue["number"]: issue for issue in issues if "pull_request" not in issue}
        managed_states = {
            "maxwell:state:suspected",
            "maxwell:state:reviewed",
            "maxwell:state:audited",
        }
        updated = []
        for row in self.store.rows(
            "SELECT issue,data FROM github_records WHERE repository=? "
            "AND type='finding' AND status='current'",
            (repo,),
        ):
            issue = remote.get(row["issue"])
            finding = RECORD_ADAPTER.validate_json(row["data"])
            if not issue or finding.finding.identity(repo) != finding.id:
                continue
            reviews, audits = current_assessments(self.store, repo, row["issue"], finding)
            assessment_labels = set()
            for stage, records in (("review", reviews), ("audit", audits)):
                if records:
                    verdicts = {record.verdict for record in records}
                    verdict = next(iter(verdicts)) if len(verdicts) == 1 else "inconclusive"
                    assessment_labels.add(f"maxwell:{stage}:{verdict}")
            state = "audited" if audits else "reviewed" if reviews else "suspected"

            def desired_labels(source):
                labels = {label["name"] for label in source.get("labels", [])}
                retained = {
                    label
                    for label in labels
                    if label not in managed_states
                    and not label.startswith(
                        ("maxwell:review:", "maxwell:audit:", "maxwell:factor:")
                    )
                }
                if not retained.intersection(
                    {"maxwell:state:human-confirmed", "maxwell:state:rejected"}
                ):
                    retained.add(f"maxwell:state:{state}")
                factors = finding_factors(finding, reviews, audits)
                if "maxwell:state:rejected" in retained:
                    factors = []
                return labels, retained | assessment_labels | {
                    f"maxwell:factor:{factor}" for factor in factors
                }

            before, desired = desired_labels(issue)
            if before == desired:
                continue
            current = await self.call(
                self.github.request, "GET", f"repos/{repo}/issues/{row['issue']}"
            )
            if current.get("body") != issue.get("body"):
                raise ValueError(
                    "Finding changed during label reconciliation; retry synchronization"
                )
            before, desired = desired_labels(current)
            if before == desired:
                continue
            if repo not in self.labels_ready:
                await self.call(self.github.ensure_labels, repo)
                self.labels_ready.add(repo)
            await self.call(
                self.github.request,
                "PATCH",
                f"repos/{repo}/issues/{row['issue']}",
                json={"labels": sorted(desired)},
            )
            updated.append(row["issue"])
        return updated

    async def discovery_context(self, repo):
        issues = await self.project_repository_records(repo)
        authors = await self.authorized_authors()
        context, identities = [], set()
        for issue in issues:
            if "pull_request" in issue or any(
                marker in (issue.get("body") or "")
                for marker in ("<!-- maxwell:dashboard:v1 -->", "<!-- maxwell:history:v1 -->")
            ):
                continue
            entry = {
                "issue": issue["number"],
                "title": issue["title"][:180],
                "state": issue["state"],
            }
            match = re.search(r"<!-- maxwell:finding:([a-f0-9]{32}) -->", issue.get("body") or "")
            if match and issue.get("user", {}).get("login") in authors:
                try:
                    record = decode_record(issue.get("body"))
                    if record is not None and (
                        not isinstance(record, FindingRecord)
                        or record.repository != repo
                        or record.id != match[1]
                        or record.finding.identity(repo) != record.id
                    ):
                        raise ValueError("Finding record does not match its identity")
                except ValueError:
                    record = None
                else:
                    identities.add(match[1])
                    entry["finding_id"] = match[1]
                    if record:
                        entry.update(
                            kind=record.finding.kind,
                            path=record.finding.path,
                            symbol=record.finding.symbol,
                        )
            if issue["state"] == "open" or "finding_id" in entry:
                context.append(entry)
        context.sort(key=lambda item: (item["state"] != "open", -item["issue"]))
        return {
            "issues": context[:100],
            "total_issues": len(context),
            "shown_issues": min(len(context), 100),
        }, identities

    async def record_comment(self, repo, issue, marker, record, authors):
        comments = await self.call(self.github.comments, repo, issue)
        existing = [item for item in comments if marker in (item.get("body") or "")]
        if len(existing) > 1:
            raise ValueError("Duplicate record markers require reconciliation")
        if not existing:
            return None
        comment = existing[0]
        remote = decode_record(comment["body"])
        if (
            comment.get("user", {}).get("login") not in authors
            or remote is None
            or record_digest(remote) != record_digest(record)
        ):
            raise ValueError("Existing record conflicts; publish a superseding record")
        return comment

    async def publish_records(self, repo):
        authors = await self.authorized_authors()
        for row in self.store.rows(
            "SELECT * FROM record_publications WHERE repository=? AND state!='published'",
            (repo,),
        ):
            try:
                record = RECORD_ADAPTER.validate_json(row["data"])
                if isinstance(record, AssessmentRecord) and record.role == "human":
                    raise ValueError("Human assessments must be authored directly on GitHub")
                scan = (
                    self.store.one("SELECT * FROM scans WHERE id=?", (record.id,))
                    if isinstance(record, ExecutionRecord)
                    else None
                )
                rendered = (
                    f"<!-- maxwell:execution:{record.id} -->\n"
                    + scan_report(scan, record.finding_ids, record=record)
                    if scan
                    else record_report(record)
                )
                body = self.config.redact(rendered)
                record = decode_record(body)
                marker = f"<!-- maxwell:{record.type}:{record.id} -->"
                comment = await self.record_comment(repo, row["issue"], marker, record, authors)
                if (
                    comment is None
                    and row["state"] != "uncertain"
                    and isinstance(record, (ExecutionRecord, MeasurementRecord))
                ):
                    dashboard = self.store.one(
                        "SELECT issue FROM dashboards WHERE repository=?", (repo,)
                    )
                    if dashboard and row["issue"] == dashboard["issue"]:
                        destination = await self.history(repo)
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE record_publications SET issue=? WHERE repository=? AND type=? AND id=?",
                                (destination, repo, record.type, record.id),
                            )
                        row = {**row, "issue": destination}
                        comment = await self.record_comment(
                            repo, destination, marker, record, authors
                        )
                if comment is None:
                    if row["state"] == "uncertain":
                        continue
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE record_publications SET state='uncertain' "
                            "WHERE repository=? AND type=? AND id=?",
                            (repo, row["type"], row["id"]),
                        )
                    try:
                        comment = await self.call(
                            self.github.create_comment, repo, row["issue"], body
                        )
                    except PublicationUncertain:
                        comment = await self.record_comment(
                            repo, row["issue"], marker, record, authors
                        )
                        if comment is None:
                            raise
                    except Exception:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE record_publications SET state='pending' "
                                "WHERE repository=? AND type=? AND id=?",
                                (repo, row["type"], row["id"]),
                            )
                        raise
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE record_publications SET state='published',error=NULL "
                        "WHERE repository=? AND type=? AND id=?",
                        (repo, row["type"], row["id"]),
                    )
            except Exception as exc:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE record_publications SET error=? WHERE repository=? AND type=? AND id=?",
                        (self.config.redact(str(exc))[:1000], repo, row["type"], row["id"]),
                    )
        if self.store.one(
            "SELECT id FROM record_publications WHERE repository=? "
            "AND (error IS NOT NULL OR state='uncertain') LIMIT 1",
            (repo,),
        ):
            raise ValueError("GitHub record publication requires reconciliation")

    async def publish_finding(self, finding):
        repo, identity = finding["repository"], finding["id"]
        marker = f"<!-- maxwell:finding:{identity} -->"
        try:
            issue = await self.call(self.github.find_issue, repo, marker)
            if issue and issue.get("user", {}).get("login") not in await self.authorized_authors():
                raise ValueError("Finding marker belongs to an unauthorized author")
            if not issue and finding["state"] == "uncertain":
                return
            if not issue:
                if repo not in self.labels_ready:
                    await self.call(self.github.ensure_labels, repo)
                    self.labels_ready.add(repo)
                data = json.loads(finding["data"])
                scan = self.store.one("SELECT * FROM scans WHERE id=?", (finding["scan_id"],))
                agent = public_agent(scan)
                body = f"""{marker}
**Suspected {data["kind"]}; not reproduced or independently validated.**

Scanned revision: `{scan["sha"]}`  
Location: `{data["path"]}:{data["line"]}` (`{data["symbol"]}`)  
Suggested priority: {data["priority"]}; hunter confidence: {data["confidence"]:.0%}
Agent: `{agent.display() if agent else "Unknown legacy agent"}`
Proposed factors: {", ".join(data.get("factors", [])) or "none established"}.

### Evidence
{data["evidence"]}

### Expected behavior / benefit
{data["expected"]}

Discovery scan: `{finding["scan_id"]}`. Token usage is recorded on that execution;
it is not counted again for this finding.
Reproduction, fixing, and validation: **not run**.

Authorized users may comment `/maxwell confirm`, `/maxwell reject <reason>`,
or `/maxwell priority P1`. Confirmation here is a human assessment, not a reproduced test.
"""
                body += (
                    "\n"
                    + encode_record(
                        FindingRecord(
                            id=identity,
                            repository=repo,
                            sha=scan["sha"],
                            execution_id=scan["id"],
                            agent=agent,
                            finding=data,
                        )
                    )
                    + "\n"
                )
                # Persist uncertainty before crossing the external side-effect boundary.
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE findings SET state='uncertain' WHERE id=?", (identity,)
                    )
                try:
                    issue = await self.call(
                        self.github.create_issue,
                        repo,
                        data["title"],
                        self.config.redact(body),
                        [
                            "maxwell",
                            "maxwell:state:suspected",
                            f"maxwell:priority:{data['priority']}",
                            *[f"maxwell:factor:{factor}" for factor in data.get("factors", [])],
                        ],
                    )
                except PublicationUncertain:
                    issue = await self.call(self.github.find_issue, repo, marker)
                    if issue is None:
                        raise
                except Exception:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE findings SET state='pending' WHERE id=?", (identity,)
                        )
                    raise
            if issue.get("user", {}).get("login") not in await self.authorized_authors():
                raise ValueError("Finding marker belongs to an unauthorized author")
            with self.store.db:
                self.store.db.execute(
                    "UPDATE findings SET state=?,issue=?,url=?,error=NULL WHERE id=?",
                    (
                        "closed" if issue["state"] == "closed" else "published",
                        issue["number"],
                        issue["html_url"],
                        identity,
                    ),
                )
                self.store.event("finding_published", {"id": identity, "issue": issue["number"]})
        except Exception as exc:
            self.error("findings", "id", identity, exc)

    async def publish_scan_reports(self, repo, issue, scans):
        published = False
        if any(scan["finished"] and not scan["report_sent"] for scan in scans):
            await self.project_repository_records(repo)
        for scan in scans:
            if scan["finished"] and not scan["report_sent"]:
                finding_ids = [
                    row["finding_id"]
                    for row in self.store.rows(
                        "SELECT finding_id FROM finding_observations WHERE scan_id=? ORDER BY finding_id",
                        (scan["id"],),
                    )
                ]
                report = self.config.redact(scan_report(scan, finding_ids))
                (self.paths.root / "reports" / f"{scan['id']}.md").write_text(report)
                existing = self.store.one(
                    "SELECT status FROM github_records WHERE repository=? AND type='execution' AND id=?",
                    (repo, scan["id"]),
                )
                if existing and existing["status"] != "current":
                    raise ValueError("Existing execution record requires reconciliation")
                if not existing:
                    publication = self.store.one(
                        "SELECT id FROM record_publications WHERE repository=? AND type='execution' AND id=?",
                        (repo, scan["id"]),
                    )
                    if not publication:
                        self.store.queue_record(execution_record(scan, finding_ids), issue)
                    await self.publish_records(repo)
                    publication = self.store.one(
                        "SELECT state FROM record_publications WHERE repository=? AND type='execution' AND id=?",
                        (repo, scan["id"]),
                    )
                    if publication["state"] != "published":
                        raise ValueError("Execution publication requires reconciliation")
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE scans SET report_sent=1 WHERE id=?", (scan["id"],)
                    )
                published = True
        return published

    async def channel_issue(self, repo, marker):
        matches = await self.call(
            lambda: [
                item
                for item in self.github.paginate(f"repos/{repo}/issues", {"state": "all"})
                if "pull_request" not in item and marker in (item.get("body") or "")
            ]
        )
        if len(matches) > 1:
            raise ValueError("Duplicate channel markers require reconciliation")
        issue = matches[0] if matches else None
        if issue and issue.get("user", {}).get("login") not in await self.authorized_authors():
            raise ValueError("Channel marker belongs to an unauthorized author")
        return issue

    async def channel(self, repo, table, marker, title):
        with self.store.db:
            self.store.db.execute(f"INSERT OR IGNORE INTO {table}(repository) VALUES(?)", (repo,))
        record = self.store.one(f"SELECT * FROM {table} WHERE repository=?", (repo,))
        if not record["issue"]:
            issue = await self.channel_issue(repo, marker)
            if not issue and record["state"] == "uncertain":
                raise ValueError("Issue creation is uncertain; reconciliation required")
            if not issue:
                with self.store.db:
                    self.store.db.execute(
                        f"UPDATE {table} SET state='uncertain' WHERE repository=?", (repo,)
                    )
                try:
                    body = marker
                    if table == "histories":
                        body += "\n\nAppend-only execution and measurement records. Current entropy and component links are shown on the Maxwell status issue. Assessments remain on their finding issues.\n"
                    issue = await self.call(self.github.create_issue, repo, title, body)
                except PublicationUncertain as exc:
                    issue = await self.channel_issue(repo, marker)
                    if issue is None:
                        self.error(table, "repository", repo, exc)
                        raise
                except Exception:
                    with self.store.db:
                        self.store.db.execute(
                            f"UPDATE {table} SET state='pending' WHERE repository=?", (repo,)
                        )
                    raise
            with self.store.db:
                self.store.db.execute(
                    f"UPDATE {table} SET issue=?,url=?,state='published',error=NULL WHERE repository=?",
                    (issue["number"], issue["html_url"], repo),
                )
            record = self.store.one(f"SELECT * FROM {table} WHERE repository=?", (repo,))
        else:
            issue = await self.call(
                self.github.request, "GET", f"repos/{repo}/issues/{record['issue']}"
            )
            if (
                marker not in (issue.get("body") or "")
                or issue.get("user", {}).get("login") not in await self.authorized_authors()
            ):
                raise ValueError("Channel identity changed; reconciliation required")
        return record

    async def history(self, repo):
        record = await self.channel(
            repo, "histories", "<!-- maxwell:history:v1 -->", "Maxwell history"
        )
        return record["issue"]

    async def clear_status_reports(self, repo, issue):
        authors = await self.authorized_authors()
        comments = await self.call(self.github.comments, repo, issue)
        removed = set()
        for comment in comments:
            if comment.get("user", {}).get("login") in authors and re.match(
                r"<!-- maxwell:(?:scan|execution|measurement):[a-f0-9]{32} -->",
                comment.get("body") or "",
            ):
                try:
                    record = decode_record(comment.get("body") or "")
                except ValueError:
                    record = None
                if (
                    isinstance(record, (ExecutionRecord, MeasurementRecord))
                    and record.repository == repo
                ):
                    publication = self.store.one(
                        "SELECT issue,data FROM record_publications WHERE repository=? AND type=? AND id=?",
                        (repo, record.type, record.id),
                    )
                    if not publication or (
                        publication["issue"] == issue
                        and record_digest(RECORD_ADAPTER.validate_json(publication["data"]))
                        == record_digest(record)
                    ):
                        self.store.queue_record(record, issue)
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE record_publications SET state='published',error=NULL WHERE repository=? AND type=? AND id=?",
                                (repo, record.type, record.id),
                            )
                            if isinstance(record, ExecutionRecord):
                                self.store.db.execute(
                                    "UPDATE scans SET report_sent=1 WHERE id=? AND repository=?",
                                    (record.id, repo),
                                )
                await self.call(
                    self.github.request, "DELETE", f"repos/{repo}/issues/comments/{comment['id']}"
                )
                removed.add(comment["id"])
        if removed:
            self.store.reconcile_records(
                repo, issue, {comment["id"] for comment in comments} - removed
            )
        return len(removed)

    async def dashboard(self, repo):
        marker = "<!-- maxwell:dashboard:v1 -->"
        record = await self.channel(repo, "dashboards", marker, "Maxwell status")
        history_issue = await self.history(repo)
        scans = self.store.rows(
            "SELECT * FROM scans WHERE repository=? ORDER BY created DESC", (repo,)
        )
        if await self.publish_scan_reports(repo, history_issue, scans):
            await self.project_repository_records(repo)
        findings = self.store.rows("SELECT * FROM findings WHERE repository=?", (repo,))
        scan_history = {}
        finding_ids = {finding["id"] for finding in findings}
        for row in self.store.rows(
            "SELECT data FROM github_records WHERE repository=? AND status='current' "
            "AND type IN ('finding','execution')",
            (repo,),
        ):
            public_record = RECORD_ADAPTER.validate_json(row["data"])
            if isinstance(public_record, FindingRecord):
                finding_ids.add(public_record.id)
            else:
                scan_history[public_record.id] = {
                    "id": public_record.id,
                    "state": public_record.state,
                    "usage": public_record.usage.model_dump_json(),
                    "executed": public_record.usage.reported
                    or (public_record.started is not None and public_record.state != "skipped"),
                    "created": public_record.started or public_record.finished,
                    "agent": public_record.agent,
                }
        scan_history.update((scan["id"], scan) for scan in scans)
        history = sorted(scan_history.values(), key=lambda scan: scan["created"], reverse=True)
        usages = [json.loads(scan["usage"] or "{}") for scan in history]
        totals = {
            key: sum(usage.get(key, 0) for usage in usages if usage.get("reported"))
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
            )
        }
        unknown = sum(
            bool(scan["executed"]) and not usage.get("reported")
            for scan, usage in zip(history, usages, strict=True)
        )
        incomplete = sum(
            bool(scan["executed"]) and not usage.get("complete")
            for scan, usage in zip(history, usages, strict=True)
        )
        reputation = reputation_report(agent_reputation(self.store, repo))
        measurements = projected_measurements(self.store, repo)
        measurement = measurements[-1] if measurements else None
        entropy = "Unavailable — run `maxwell measure` to establish a baseline"
        baseline = None
        if measurement:
            score = measurement_score(measurement)
            entropy = (
                f"{score:.2f}/100 (lower is better)"
                if score is not None
                else "Unknown — required measurements unavailable"
            )
            baseline = next(
                (item for item in measurements if item.id == measurement.baseline_id), None
            )
        source_supported = human_confirmed = 0
        issues = await self.call(
            lambda: list(self.github.paginate(f"repos/{repo}/issues", {"state": "all"}))
        )
        remote = {item["number"]: item for item in issues if "pull_request" not in item}
        measurement_body = component_dashboard(repo, measurement, baseline)
        for row in self.store.rows(
            "SELECT issue,data FROM github_records WHERE repository=? AND type='finding' AND status='current'",
            (repo,),
        ):
            finding = RECORD_ADAPTER.validate_json(row["data"])
            issue = remote.get(row["issue"])
            if (
                not issue
                or issue["state"] != "open"
                or finding.finding.kind != "bug"
                or finding.finding.identity(repo) != finding.id
            ):
                continue
            labels = {label["name"] for label in issue.get("labels", [])}
            if "maxwell:state:rejected" in labels:
                continue
            if "maxwell:state:human-confirmed" in labels:
                human_confirmed += 1
            else:
                reviews, audits = current_assessments(self.store, repo, row["issue"], finding)
                decisions = audits or reviews
                if decisions and all(record.verdict == "supported" for record in decisions):
                    source_supported += 1
        maintenance, bugs = fix_queue(self.store, repo, remote, measurements)
        queue_body = queue_report(repo, maintenance, bugs)
        repairs = self.store.rows(
            "SELECT issue,comment,data FROM github_records WHERE repository=? "
            "AND type='repair' AND status='current' ORDER BY observed DESC LIMIT 20",
            (repo,),
        )
        if repairs:
            queue_body += "\n### Local repair attempts\n\n"
            for row in repairs:
                repair = RECORD_ADAPTER.validate_json(row["data"])
                url = (
                    f"https://github.com/{repo}/issues/{row['issue']}#issuecomment-{row['comment']}"
                )
                queue_body += f"- [#{row['issue']}: {repair.outcome}]({url}) · `{repair.branch}` · local-only; awaiting validation.\n"
        body = f"""{marker}
# Maxwell status

**Repository entropy: {entropy}.**

Open bug burden: **{source_supported} source-supported**, **{human_confirmed} human-confirmed**.
Reported separately from structural entropy; source support does not imply reproduction.

{measurement_body}

{queue_body}

[Execution and measurement history](https://github.com/{repo}/issues/{history_issue}).

<details>
<summary>Activity, tokens, and agent reputation</summary>

Hunting, review, audit, and repair use separate executions. Source assessments do not imply reproduction.

| Metric | Value |
| --- | --- |
| Entropy | {entropy} |
| Recorded input tokens | {totals["input_tokens"]:,} |
| Cached input tokens (included in input) | {totals["cached_input_tokens"]:,} |
| Cache write tokens (included in input) | {totals["cache_write_tokens"]:,} |
| Recorded output tokens | {totals["output_tokens"]:,} |
| Recorded total tokens (input + output) | {totals["input_tokens"] + totals["output_tokens"]:,} |
| Executions with unavailable token usage | {unknown} |
| Executions with incomplete token usage | {incomplete} |
| Findings | {len(finding_ids)} |
| Scans | {len(history)} |

Token totals include reported usage from failed attempts. Missing usage is unknown,
not zero; totals may be partial when execution usage is unavailable or incomplete.

{reputation}

### Recent scans
"""
        for scan in history[:10]:
            tokens = token_summary(json.loads(scan["usage"] or "{}"))
            agent = scan.get("agent") or public_agent(scan)
            identity = agent.display() if agent else "Unknown legacy agent"
            counts = ""
            if scan.get("selection"):
                selection = SelectionStats.model_validate_json(scan["selection"])
                counts = f", {selection.collected} collected, {selection.skipped} skipped"
            body += f"- `{scan['id'][:12]}`: {scan['state']}, {tokens}, {identity}{counts}\n"
        body += "\n</details>\n"
        body = self.config.redact(body)
        snapshot = self.paths.root / "reports" / (repo.replace("/", "--") + ".md")
        snapshot.write_text(body)
        existing = await self.call(
            self.github.request, "GET", f"repos/{repo}/issues/{record['issue']}"
        )
        if existing["body"] != body:
            await self.call(
                self.github.request,
                "PATCH",
                f"repos/{repo}/issues/{record['issue']}",
                json={"body": body},
            )
        await self.clear_status_reports(repo, record["issue"])
        return record["issue"]

    async def commands(self, repo, issue, finding=None):
        for comment in await self.call(self.github.comments, repo, issue):
            if self.store.one("SELECT id FROM comments WHERE id=?", (comment["id"],)):
                continue
            actor = comment.get("user", {}).get("login", "")
            body = comment["body"].strip()
            if actor not in self.github.allowed_users or not body.startswith("/maxwell "):
                continue
            command = body.splitlines()[0].split(maxsplit=2)
            action = command[1] if len(command) > 1 else ""
            argument = command[2] if len(command) > 2 else ""
            if finding and action in ("confirm", "reject"):
                state = "confirmed" if action == "confirm" else "rejected"
                remote = await self.call(self.github.request, "GET", f"repos/{repo}/issues/{issue}")
                labels = [
                    label["name"]
                    for label in remote.get("labels", [])
                    if not label["name"].startswith("maxwell:state:")
                ]
                labels.append(
                    "maxwell:state:human-confirmed"
                    if action == "confirm"
                    else "maxwell:state:rejected"
                )
                await self.call(self.github.ensure_labels, repo)
                await self.call(
                    self.github.request,
                    "PATCH",
                    f"repos/{repo}/issues/{issue}",
                    json={"state": "open" if action == "confirm" else "closed", "labels": labels},
                )
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE findings SET state=? WHERE id=?", (state, finding["id"])
                    )
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO comments(id) VALUES(?)", (comment["id"],)
                    )
                    self.store.event(
                        "human_assessment",
                        {"finding": finding["id"], "state": state, "actor": actor},
                    )
            elif finding and action == "priority" and re.fullmatch("P[0-3]", argument):
                remote = await self.call(self.github.request, "GET", f"repos/{repo}/issues/{issue}")
                labels = [
                    label["name"]
                    for label in remote.get("labels", [])
                    if not label["name"].startswith("maxwell:priority:")
                ]
                labels.append(f"maxwell:priority:{argument}")
                await self.call(
                    self.github.request,
                    "PATCH",
                    f"repos/{repo}/issues/{issue}",
                    json={"labels": labels},
                )
                data = json.loads(finding["data"])
                data["priority"] = argument
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE findings SET data=? WHERE id=?", (json.dumps(data), finding["id"])
                    )
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO comments(id) VALUES(?)", (comment["id"],)
                    )
            elif not finding and action in ("scan", "pause", "resume"):
                execution = None
                if action == "scan":
                    try:
                        options = shlex.split(argument)
                        if (
                            not options
                            or len(options) % 2
                            or any(
                                option not in ("--model", "--provider", "--effort")
                                for option in options[::2]
                            )
                            or len(set(options[::2])) != len(options[::2])
                        ):
                            raise ValueError(
                                "Use /maxwell scan --model MODEL [--provider PROVIDER] [--effort EFFORT]"
                            )
                        values = dict(zip(options[::2], options[1::2], strict=True))
                        if "--model" not in values:
                            raise ValueError("Manual scans require --model")
                        record = self.store.one("SELECT * FROM repositories WHERE name=?", (repo,))
                        route = self.config.route(
                            values.get("--provider"),
                            values["--model"],
                            effort=values.get("--effort"),
                        )
                        execution = self.config.execution(route, record)
                    except ValueError as exc:
                        marker = f"<!-- maxwell:command:{comment['id']} -->"
                        await self.call(
                            self.github.upsert_comment,
                            repo,
                            issue,
                            marker,
                            f"{marker}\nCommand rejected: {self.config.redact(str(exc))}",
                        )
                        with self.store.db:
                            self.store.db.execute(
                                "INSERT OR IGNORE INTO comments(id) VALUES(?)", (comment["id"],)
                            )
                        continue
                with self.store.db:
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO comments(id) VALUES(?)", (comment["id"],)
                    )
                    if action == "scan":
                        self.store.enqueue(repo, execution=execution)
                    else:
                        self.store.pause(repo, action == "pause")
            elif (
                not finding
                and action == "retry-publication"
                and re.fullmatch("[a-f0-9]{32}", argument)
            ):
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE findings SET state='pending',error=NULL "
                        "WHERE id=? AND repository=? AND state='uncertain'",
                        (argument, repo),
                    )
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO comments(id) VALUES(?)", (comment["id"],)
                    )
                    self.store.event(
                        "publication_retry_authorized", {"finding": argument, "actor": actor}
                    )

    async def refresh_findings(self, repository, *, process_commands=True, issues=None):
        remote_issues = {issue["number"]: issue for issue in issues or []}
        for finding in self.store.rows(
            "SELECT * FROM findings WHERE repository=? AND issue IS NOT NULL", (repository,)
        ):
            if process_commands:
                await self.commands(repository, finding["issue"], finding)
            remote = remote_issues.get(finding["issue"]) if not process_commands else None
            if remote is None:
                remote = await self.call(
                    self.github.request, "GET", f"repos/{repository}/issues/{finding['issue']}"
                )
            with self.store.db:
                self.store.db.execute(
                    "UPDATE findings SET state='closed' WHERE id=? "
                    "AND state='published' AND ?='closed'",
                    (finding["id"], remote["state"]),
                )
                self.store.db.execute(
                    "UPDATE findings SET checked=? WHERE id=?", (time.time(), finding["id"])
                )

    async def sync(self, repository=None, *, process_commands=True):
        for finding in self.store.rows(
            "SELECT * FROM findings WHERE state IN ('pending','uncertain') "
            "AND (? IS NULL OR repository=?) ORDER BY scan_id,id",
            (repository, repository),
        ):
            await self.publish_finding(finding)
        for repo in self.store.rows(
            "SELECT name FROM repositories WHERE ? IS NULL OR name=?", (repository, repository)
        ):
            name = repo["name"]
            try:
                await self.publish_records(name)
                issues = await self.project_repository_records(name)
                issue = await self.dashboard(name)
                if issue and process_commands:
                    await self.commands(name, issue)
                await self.refresh_findings(name, process_commands=process_commands, issues=issues)
                issues = await self.project_repository_records(name)
                await self.reconcile_assessment_labels(name, issues)
                await self.dashboard(name)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dashboards SET error=NULL WHERE repository=?", (name,)
                    )
            except Exception as exc:
                self.error("dashboards", "repository", name, exc)
