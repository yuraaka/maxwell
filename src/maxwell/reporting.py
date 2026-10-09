import asyncio
import hashlib
import json
import re
import shlex
import time

from maxwell.assessment import current_assessments
from maxwell.contracts import DEFAULT_SCAN_CANDIDATES, SelectionStats
from maxwell.github import PublicationUncertain
from maxwell.records import (
    RECORD_ADAPTER,
    AssessmentRecord,
    ExecutionRecord,
    FindingRecord,
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


def scan_report(scan, finding_ids=()):
    usage = json.loads(scan["usage"] or "{}")
    tokens = token_summary(usage)
    agent = public_agent(scan)
    identity = agent.display() if agent else "Unknown legacy agent"
    stage = scan.get("stage", "discovery")
    label = "Discovery" if stage == "discovery" else stage.title()
    result = (
        f"{scan['admitted']} new candidates"
        if stage == "discovery"
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
| Fixing | Not run | Later milestone |
| Validation / integration | Not run | Later milestone |

**{tokens}.** Includes the failed attempt, if any.
Usage complete: {usage.get("complete", False)}.
Cached input and cache writes are included in input; total is input plus output.
Missing usage is unknown, not zero. Tokens are recorded once per execution, not per finding.
"""
    execution = json.loads(scan.get("execution") or "{}")
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
        body += "\n" + encode_record(execution_record(scan, finding_ids)) + "\n"
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
        )
        if record.execution_id:
            body += f"\nExecution: `{record.execution_id}`.\n"
        if record.supersedes:
            body += f"\nSupersedes: `{record.supersedes}`.\n"
        if record.references:
            body += "\nReferences: " + ", ".join(f"`{item}`" for item in record.references) + ".\n"
    elif isinstance(record, ExecutionRecord):
        identity = record.agent.display() if record.agent else "Unknown legacy agent"
        body = (
            f"{marker}\n### Execution `{record.id[:12]}` — {record.state}\n\n"
            f"Agent: `{identity}`; revision: `{record.sha or 'not resolved'}`\n\n"
            f"{record.summary}\n\n{token_summary(record.usage.model_dump())}.\n"
            f"Usage complete: {record.usage.complete}.\n"
        )
    else:
        body = (
            f"{marker}\n### Measurement `{record.id[:12]}`\n\n"
            f"Revision: `{record.sha}`; policy: `{record.policy_version}`; "
            f"analyzer: `{record.analyzer_version}`; coverage: {record.coverage:.0%}.\n"
        )
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
        comments = await self.call(self.github.comments, repo, issue["number"])
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
                elif isinstance(record, AssessmentRecord):
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
                    and not label.startswith(("maxwell:review:", "maxwell:audit:"))
                }
                if not retained.intersection(
                    {"maxwell:state:human-confirmed", "maxwell:state:rejected"}
                ):
                    retained.add(f"maxwell:state:{state}")
                return labels, retained | assessment_labels

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
            if "pull_request" in issue:
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
                body = self.config.redact(record_report(record))
                record = decode_record(body)
                marker = f"<!-- maxwell:{record.type}:{record.id} -->"
                comments = await self.call(self.github.comments, repo, row["issue"])
                existing = [item for item in comments if marker in (item.get("body") or "")]
                if len(existing) > 1:
                    raise ValueError("Duplicate record markers require reconciliation")
                if existing:
                    comment = existing[0]
                    remote = decode_record(comment["body"])
                    if (
                        comment.get("user", {}).get("login") not in authors
                        or remote is None
                        or record_digest(remote) != record_digest(record)
                    ):
                        raise ValueError("Existing record conflicts; publish a superseding record")
                elif row["state"] == "uncertain":
                    continue
                else:
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
                        ],
                    )
                except PublicationUncertain:
                    raise
                except Exception:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE findings SET state='pending' WHERE id=?", (identity,)
                        )
                    raise
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
        for scan in scans:
            if scan["finished"] and not scan["report_sent"]:
                published = True
                finding_ids = [
                    row["finding_id"]
                    for row in self.store.rows(
                        "SELECT finding_id FROM finding_observations WHERE scan_id=? ORDER BY finding_id",
                        (scan["id"],),
                    )
                ]
                report = self.config.redact(scan_report(scan, finding_ids))
                (self.paths.root / "reports" / f"{scan['id']}.md").write_text(report)
                await self.call(
                    self.github.upsert_comment,
                    repo,
                    issue,
                    f"<!-- maxwell:scan:{scan['id']} -->",
                    report,
                )
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE scans SET report_sent=1 WHERE id=?", (scan["id"],)
                    )
        return published

    async def dashboard(self, repo):
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO dashboards(repository) VALUES(?)", (repo,))
        record = self.store.one("SELECT * FROM dashboards WHERE repository=?", (repo,))
        marker = "<!-- maxwell:dashboard:v1 -->"
        if not record["issue"]:
            issue = await self.call(self.github.find_issue, repo, marker)
            if not issue and record["state"] == "uncertain":
                return None
            if not issue:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dashboards SET state='uncertain' WHERE repository=?", (repo,)
                    )
                try:
                    issue = await self.call(
                        self.github.create_issue, repo, "Maxwell status", marker
                    )
                except PublicationUncertain:
                    raise
                except Exception:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE dashboards SET state='pending' WHERE repository=?", (repo,)
                        )
                    raise
            with self.store.db:
                self.store.db.execute(
                    "UPDATE dashboards SET issue=?,url=?,state='published',error=NULL "
                    "WHERE repository=?",
                    (issue["number"], issue["html_url"], repo),
                )
            record["issue"] = issue["number"]
        scans = self.store.rows(
            "SELECT * FROM scans WHERE repository=? ORDER BY created DESC", (repo,)
        )
        if await self.publish_scan_reports(repo, record["issue"], scans):
            remote = await self.call(
                self.github.request, "GET", f"repos/{repo}/issues/{record['issue']}"
            )
            await self.project_issue_records(repo, remote)
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
        body = f"""{marker}
# Maxwell status

Hunting, review, and audit use separate executions. Source assessments do not imply reproduction.

| Metric | Value |
| --- | --- |
| Entropy | Not implemented — measurement adapters and scoring policy pending |
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
        body += "\nAuthorized commands: `/maxwell scan --model MODEL [--provider PROVIDER] [--effort EFFORT]`, `/maxwell pause`, `/maxwell resume`.\n"
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

    async def refresh_findings(self, repository, *, process_commands=True):
        for finding in self.store.rows(
            "SELECT * FROM findings WHERE repository=? AND issue IS NOT NULL", (repository,)
        ):
            if process_commands:
                await self.commands(repository, finding["issue"], finding)
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
                await self.project_repository_records(name)
                issue = await self.dashboard(name)
                if issue and process_commands:
                    await self.commands(name, issue)
                await self.refresh_findings(name, process_commands=process_commands)
                issues = await self.project_repository_records(name)
                await self.reconcile_assessment_labels(name, issues)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dashboards SET error=NULL WHERE repository=?", (name,)
                    )
            except Exception as exc:
                self.error("dashboards", "repository", name, exc)
