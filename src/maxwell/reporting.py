import asyncio
import json
import re
import shlex
import time

from maxwell.github import PublicationUncertain


def money(amount):
    return "unknown" if amount is None else f"${amount / 1_000_000:.6f} estimated"


def scan_report(scan):
    usage = json.loads(scan["usage"] or "{}")
    if not usage.get("reported"):
        usage = {"complete": False}
    cost = (
        "unpriced"
        if scan.get("accounting") == "unpriced" and scan["cost"] is None
        else money(scan["cost"])
    )
    accounting = (
        "Unpriced usage is limited by the daily scan count; no dollar estimate is available."
        if scan.get("accounting") == "unpriced"
        else "Unknown spending retains its budget reservation; it is never recorded as zero."
    )
    return f"""<!-- maxwell:scan:{scan["id"]} -->
### Discovery scan `{scan["id"][:12]}` — {scan["state"]}

{scan["summary"] or ""}

Revision: `{scan["sha"] or "not resolved"}`  
Profile: `{scan["profile"] or "not started"}`
Provider: `{scan.get("provider") or "legacy"}`; model: `{scan.get("model") or "legacy"}`

| Stage | Spending | Result |
| --- | --- | --- |
| Discovery | {cost} | {scan["admitted"]} new candidates |
| Reproduction | Not run | Later milestone |
| Fixing | Not run | Later milestone |
| Validation / integration | Not run | Later milestone |

**Total: {cost}.** Includes the failed attempt, if any.
Reported tokens: input {usage.get("input_tokens", "unknown")}, cached input
{usage.get("cached_input_tokens", "unknown")}, output {usage.get("output_tokens", "unknown")}.
Usage complete: {usage.get("complete", False)}.
Allocated to findings: {money(scan["allocated"])}; remaining known spending is discovery overhead.
{accounting}
"""


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

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    def error(self, table, key, value, error):
        text = self.config.redact(str(error))[:1000]
        with self.store.db:
            self.store.db.execute(f"UPDATE {table} SET error=? WHERE {key}=?", (text, value))

    async def publish_finding(self, finding):
        repo, identity = finding["repository"], finding["id"]
        marker = f"<!-- maxwell:finding:{identity} -->"
        try:
            issue = await self.call(self.github.find_issue, repo, marker)
            if not issue and finding["state"] == "uncertain":
                return
            if not issue:
                if repo not in self.labels_ready:
                    await self.call(self.github.ensure_labels, repo)
                    self.labels_ready.add(repo)
                data = json.loads(finding["data"])
                scan = self.store.one("SELECT * FROM scans WHERE id=?", (finding["scan_id"],))
                body = f"""{marker}
**Suspected {data["kind"]}; not reproduced or independently validated.**

Scanned revision: `{scan["sha"]}`  
Location: `{data["path"]}:{data["line"]}` (`{data["symbol"]}`)  
Suggested priority: {data["priority"]}; hunter confidence: {data["confidence"]:.0%}

### Evidence
{data["evidence"]}

### Expected behavior / benefit
{data["expected"]}

Discovery scan: `{finding["scan_id"]}`. Allocated discovery spending:
**{money(finding["allocated"])}** (equal allocation across new findings).
Reproduction, fixing, and validation: **not run**.

Authorized users may comment `/maxwell confirm`, `/maxwell reject <reason>`,
or `/maxwell priority P1`. Confirmation here is a human assessment, not a reproduced test.
"""
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
        findings = self.store.rows("SELECT * FROM findings WHERE repository=?", (repo,))
        total = sum(scan["cost"] or 0 for scan in scans)
        unknown = sum(
            scan["accounting"] == "usd" and scan["cost"] is None and scan["started"] is not None
            for scan in scans
        )
        unpriced = sum(scan["accounting"] == "unpriced" and scan["executed"] for scan in scans)
        allocated = sum(scan["allocated"] for scan in scans)
        ratings = hunter_ratings(self.store, repo)
        body = f"""{marker}
# Maxwell status

Milestone 1: discovery only. Findings remain suspected until human assessment or future reproduction.

| Metric | Value |
| --- | --- |
| Entropy | Unavailable — complete measurement baseline required |
| Known discovery spending | {money(total)} |
| Allocated discovery spending | {money(allocated)} |
| Unallocated known overhead | {money(total - allocated)} |
| Executions with unknown spending | {unknown} |
| Unpriced executions | {unpriced} |
| Findings | {len(findings)} |
| Scans | {len(scans)} |

### Hunter ratings
{json.dumps(ratings) if ratings else "Unrated: no independent assessments yet."}

### Recent scans
"""
        for scan in scans[:10]:
            cost = (
                "unpriced"
                if scan["accounting"] == "unpriced" and scan["cost"] is None
                else money(scan["cost"])
            )
            body += f"- `{scan['id'][:12]}`: {scan['state']}, {cost}, {scan['provider'] or 'legacy'}/{scan['model'] or 'legacy'}\n"
        body += "\nAuthorized commands: `/maxwell scan --model MODEL [--provider PROVIDER]`, `/maxwell pause`, `/maxwell resume`.\n"
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
        for scan in scans:
            if scan["finished"] and not scan["report_sent"]:
                report = self.config.redact(scan_report(scan))
                (self.paths.root / "reports" / f"{scan['id']}.md").write_text(report)
                await self.call(
                    self.github.upsert_comment,
                    repo,
                    record["issue"],
                    f"<!-- maxwell:scan:{scan['id']} -->",
                    report,
                )
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE scans SET report_sent=1 WHERE id=?", (scan["id"],)
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
                                option not in ("--model", "--provider") for option in options[::2]
                            )
                            or len(set(options[::2])) != len(options[::2])
                        ):
                            raise ValueError(
                                "Use /maxwell scan --model MODEL [--provider PROVIDER]"
                            )
                        values = dict(zip(options[::2], options[1::2], strict=True))
                        if "--model" not in values:
                            raise ValueError("Manual scans require --model")
                        record = self.store.one("SELECT * FROM repositories WHERE name=?", (repo,))
                        route = self.config.route(
                            values.get("--provider"),
                            values["--model"],
                            self.store.saved_route(repo),
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

    async def sync(self, repository=None):
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
                issue = await self.dashboard(name)
                if issue:
                    await self.commands(name, issue)
                for finding in self.store.rows(
                    "SELECT * FROM findings WHERE repository=? AND issue IS NOT NULL", (name,)
                ):
                    await self.commands(name, finding["issue"], finding)
                    remote = await self.call(
                        self.github.request, "GET", f"repos/{name}/issues/{finding['issue']}"
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
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE dashboards SET error=NULL WHERE repository=?", (name,)
                    )
            except Exception as exc:
                self.error("dashboards", "repository", name, exc)
