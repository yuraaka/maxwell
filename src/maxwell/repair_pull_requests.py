import hashlib
import json
import re

from maxwell.github import PublicationUncertain
from maxwell.impact import input_assessments
from maxwell.measurement import projected_measurements
from maxwell.records import (
    RECORD_ADAPTER,
    IntegrationRecord,
    RepairPullRequestRecord,
)
from maxwell.repositories import Repository, git


def records(store, repository, kind):
    return [
        RECORD_ADAPTER.validate_json(row["data"])
        for row in store.rows(
            "SELECT data FROM github_records WHERE repository=? AND type=? AND status='current' ORDER BY comment DESC",
            (repository, kind),
        )
    ]


def marker(repair):
    return (
        f"<!-- maxwell:repair-pr:{repair.finding_id}:{repair.lineage_id or repair.execution_id} -->"
    )


def check_pull(pull, repository, branch, base_branch, authors):
    if (
        pull.get("user", {}).get("login") not in authors
        or pull.get("head", {}).get("repo", {}).get("full_name", "").casefold()
        != repository.casefold()
        or pull.get("base", {}).get("repo", {}).get("full_name", "").casefold()
        != repository.casefold()
        or pull["head"]["ref"] != branch
        or pull["base"]["ref"] != base_branch
    ):
        raise ValueError("Repair PR ownership, repository or branches do not match")


def association(repair, finding_issue, pull, provenance="fixer", previous_id=None):
    values = (repair.id, pull["number"], pull["head"]["sha"], pull["base"]["sha"], previous_id)
    return RepairPullRequestRecord(
        id=hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32],
        repository=repair.repository,
        finding_id=repair.finding_id,
        finding_issue=finding_issue,
        repair_id=repair.id,
        sha=repair.sha,
        head_sha=pull["head"]["sha"],
        base_sha=pull["base"]["sha"],
        branch=repair.branch,
        base_branch=pull["base"]["ref"],
        number=pull["number"],
        provenance=provenance,
        previous_id=previous_id,
    )


def pull_body(repair, finding_issue):
    from maxwell.pricing import spending_summary

    checks = "; ".join(f"{check.state}, exit {check.returncode}" for check in repair.checks)
    return (
        f"{marker(repair)}\nRepair for #{finding_issue}. Finding remains open until maintained-branch verification.\n\n"
        f"{repair.summary}\n\n{repair.explanation}\n\n"
        f"{spending_summary(repair.spending)}\n\n"
        f"Regression coverage: {', '.join(repair.regression_tests) or 'none'}.\n\n"
        f"Host checks: {checks}. Independent validation pending.\n\n"
        f"Original defect revision: `{repair.sha}`; repair record: `{repair.id}`.\n"
    )


async def publish_prepared(reporter, repo):
    authors = await reporter.authorized_authors()
    for row in reporter.store.rows(
        "SELECT issue,data FROM github_records WHERE repository=? AND type='repair' AND status='current' ORDER BY comment",
        (repo,),
    ):
        repair = RECORD_ADAPTER.validate_json(row["data"])
        if repair.outcome != "prepared":
            continue
        if any(
            link.repair_id == repair.id and link.provenance == "fixer"
            for link in records(reporter.store, repo, "pull_request")
        ):
            continue
        with reporter.store.db:
            reporter.store.db.execute(
                "INSERT OR IGNORE INTO repair_publications(repository,repair_id,finding_issue) VALUES(?,?,?)",
                (repo, repair.id, row["issue"]),
            )
    registration = reporter.store.repository(repo)
    repository = Repository(reporter.paths, registration)
    token = reporter.config.github.for_account(
        registration["github_account"]
    ).token.get_secret_value()
    for pending in reporter.store.rows(
        "SELECT * FROM repair_publications WHERE repository=? AND state!='published'", (repo,)
    ):
        try:
            repair_row = reporter.store.one(
                "SELECT data FROM github_records WHERE repository=? AND type='repair' AND id=? AND status='current'",
                (repo, pending["repair_id"]),
            )
            if not repair_row:
                raise ValueError("Prepared repair record is missing or changed")
            repair = RECORD_ADAPTER.validate_json(repair_row["data"])
            if pending["data"]:
                link = RECORD_ADAPTER.validate_json(pending["data"])
            else:
                expected_head = None
                if repair.previous_id:
                    previous = next(
                        (
                            link
                            for link in records(reporter.store, repo, "pull_request")
                            if link.repair_id == repair.previous_id
                        ),
                        None,
                    )
                    if not previous:
                        raise ValueError("Repair amendment requires its existing PR association")
                    expected_head = repair.previous_head_sha or repair.parent_sha
                    pull = await reporter.call(reporter.github.pull, repo, previous.number)
                    check_pull(pull, repo, repair.branch, previous.base_branch, authors)
                    if not pull["draft"]:
                        await reporter.call(reporter.github.set_draft, pull, True)
                    if pull["state"] != "open" or pull["head"]["sha"] not in (
                        expected_head,
                        repair.commit,
                    ):
                        raise ValueError("PR changed before amendment publication")
                else:
                    pull = await reporter.call(
                        reporter.github.find_pull, repo, marker(repair), repair.branch
                    )
                if pull:
                    check_pull(pull, repo, repair.branch, registration["ref"], authors)
                elif pending["state"] == "uncertain":
                    raise ValueError("Draft PR creation remains uncertain; reconciliation required")
                from maxwell.validation import verify_repair

                verify_repair(repository, repair)
                await reporter.call(
                    repository.push_repair,
                    repair.branch,
                    repair.commit,
                    token,
                    expected_head,
                )
                if pull is None:
                    with reporter.store.db:
                        reporter.store.db.execute(
                            "UPDATE repair_publications SET state='uncertain' WHERE repository=? AND repair_id=?",
                            (repo, repair.id),
                        )
                    try:
                        pull = await reporter.call(
                            reporter.github.create_pull,
                            repo,
                            f"Repair #{pending['finding_issue']}: {repair.summary}"[:180],
                            reporter.config.redact(pull_body(repair, pending["finding_issue"])),
                            repair.branch,
                            registration["ref"],
                        )
                    except PublicationUncertain:
                        pull = await reporter.call(
                            reporter.github.find_pull, repo, marker(repair), repair.branch
                        )
                        if pull is None:
                            raise
                    except Exception:
                        with reporter.store.db:
                            reporter.store.db.execute(
                                "UPDATE repair_publications SET state='pending' WHERE repository=? AND repair_id=?",
                                (repo, repair.id),
                            )
                        raise
                pull = await reporter.call(reporter.github.pull, repo, pull["number"])
                check_pull(pull, repo, repair.branch, registration["ref"], authors)
                if pull["head"]["sha"] != repair.commit:
                    raise ValueError("Published PR head does not match prepared repair")
                link = association(
                    repair,
                    pending["finding_issue"],
                    pull,
                    previous_id=previous.id if repair.previous_id else None,
                )
                with reporter.store.db:
                    reporter.store.db.execute(
                        "UPDATE repair_publications SET number=?,data=? WHERE repository=? AND repair_id=?",
                        (link.number, link.model_dump_json(), repo, repair.id),
                    )
            reporter.store.queue_record(link, pending["finding_issue"])
            await reporter.publish_records(repo)
            with reporter.store.db:
                reporter.store.db.execute(
                    "UPDATE repair_publications SET state='published',error=NULL WHERE repository=? AND repair_id=?",
                    (repo, repair.id),
                )
        except Exception as exc:
            with reporter.store.db:
                reporter.store.db.execute(
                    "UPDATE repair_publications SET error=? WHERE repository=? AND repair_id=?",
                    (reporter.config.redact(str(exc))[:1000], repo, pending["repair_id"]),
                )
    errors = reporter.store.rows(
        "SELECT error FROM repair_publications WHERE repository=? AND state!='published'", (repo,)
    )
    if errors:
        raise ValueError(
            "Repair PR publication pending: "
            + "; ".join(row["error"] or "reconciliation required" for row in errors)
        )


async def refresh_links(reporter, repo):
    authors = await reporter.authorized_authors()
    latest = {}
    for link in records(reporter.store, repo, "pull_request"):
        latest.setdefault(link.number, link)
    repairs = {repair.id: repair for repair in records(reporter.store, repo, "repair")}
    pulls = {}
    for number, link in latest.items():
        pull = await reporter.call(reporter.github.pull, repo, number)
        check_pull(pull, repo, link.branch, link.base_branch, authors)
        repair = repairs.get(link.repair_id)
        if not repair or marker(repair) not in (pull.get("body") or ""):
            raise ValueError("Repair PR marker or repair provenance is missing")
        pulls[number] = pull
        if (link.head_sha, link.base_sha) != (pull["head"]["sha"], pull["base"]["sha"]):
            new_link = association(
                repair,
                link.finding_issue,
                pull,
                "fixer" if pull["head"]["sha"] == repair.commit else "external",
                previous_id=link.id,
            )
            reporter.store.queue_record(new_link, link.finding_issue)
    await reporter.publish_records(repo)
    return pulls


def active_validations(store, repo, link, kind="validation"):
    matching = [
        record
        for record in records(store, repo, kind)
        if record.pr_id == link.id
        and record.pr_head == link.head_sha
        and record.pr_base == link.base_sha
        and record.repair_id == link.repair_id
    ]
    superseded = {identity for record in matching for identity in record.supersedes}
    return [record for record in matching if record.id not in superseded]


def inline_comments(repository, link, result):
    patch = git(
        "-C",
        str(repository.mirror),
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        link.base_sha,
        link.head_sha,
    )
    anchors, path, old, new = set(), None, 0, 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("--- a/"):
            path = line[6:]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if match:
                old, new = map(int, match.groups())
        elif path and line.startswith("+"):
            anchors.add((path, "RIGHT", new))
            new += 1
        elif path and line.startswith("-"):
            anchors.add((path, "LEFT", old))
            old += 1
        elif path and line.startswith(" "):
            old, new = old + 1, new + 1
    return [
        {
            "path": comment.path,
            "side": comment.side,
            "line": comment.line,
            "body": comment.explanation
            + (f"\n\n```suggestion\n{comment.suggestion}\n```" if comment.suggestion else ""),
        }
        for comment in result.comments
        if (comment.path, comment.side, comment.line) in anchors
    ]


async def publish_review(reporter, repo, link, pull, record):
    from maxwell.pricing import spending_summary

    if not record.result or not record.result.comments:
        return
    legacy_body = f"<!-- maxwell:pr-review:{record.id} -->\nIndependent model verdict: {record.outcome}. {record.result.summary}"
    role = "Integration validator" if isinstance(record, IntegrationRecord) else "Validator"
    body = f"<!-- maxwell:pr-review:{record.id} -->\n### {role} · {record.outcome}\n\n`{record.agent.adapter}/{record.agent.model}`\n\n{record.result.summary}"
    previous_body = body
    body += f"\n\n{spending_summary(record.spending)}"
    authors = await reporter.authorized_authors()
    reviews = await reporter.call(
        lambda: list(reporter.github.paginate(f"repos/{repo}/pulls/{link.number}/reviews"))
    )
    matching = [
        review
        for review in reviews
        if f"<!-- maxwell:pr-review:{record.id} -->" in (review.get("body") or "")
    ]
    if matching:
        if (
            len(matching) != 1
            or matching[0].get("user", {}).get("login") not in authors
            or matching[0].get("commit_id") != record.pr_head
            or matching[0].get("body") not in (body, previous_body, legacy_body)
        ):
            raise ValueError("PR feedback marker conflicts with existing review")
        with reporter.store.db:
            reporter.store.db.execute(
                "UPDATE pr_actions SET state='published',error=NULL WHERE repository=? AND id=? AND action='review'",
                (repo, record.id),
            )
        return
    with reporter.store.db:
        reporter.store.db.execute(
            "INSERT OR IGNORE INTO pr_actions(repository,id,action) VALUES(?,?,'review')",
            (repo, record.id),
        )
    action = reporter.store.one(
        "SELECT state FROM pr_actions WHERE repository=? AND id=? AND action='review'",
        (repo, record.id),
    )
    if action["state"] == "uncertain":
        raise ValueError("PR review publication remains uncertain; reconciliation required")
    registration = reporter.store.repository(repo)
    repository = Repository(reporter.paths, registration)
    token = reporter.config.github.for_account(
        registration["github_account"]
    ).token.get_secret_value()
    await reporter.call(
        repository.fetch_pull, link.number, link.head_sha, link.base_branch, link.base_sha, token
    )
    comments = inline_comments(repository, link, record.result)
    for comment in comments:
        comment["body"] = (
            f"**{role}** · `{record.agent.adapter}/{record.agent.model}`\n\n"
            + comment["body"]
            + f"\n\n{spending_summary(record.spending)} Shared validation execution cost."
        )
    if not comments:
        return
    with reporter.store.db:
        reporter.store.db.execute(
            "UPDATE pr_actions SET state='uncertain' WHERE repository=? AND id=? AND action='review'",
            (repo, record.id),
        )
    try:
        await reporter.call(
            reporter.github.create_review, repo, link.number, record.pr_head, body, comments
        )
    except PublicationUncertain:
        raise
    except Exception:
        with reporter.store.db:
            reporter.store.db.execute(
                "UPDATE pr_actions SET state='pending' WHERE repository=? AND id=? AND action='review'",
                (repo, record.id),
            )
        raise
    with reporter.store.db:
        reporter.store.db.execute(
            "UPDATE pr_actions SET state='published',error=NULL WHERE repository=? AND id=? AND action='review'",
            (repo, record.id),
        )


async def summary_matches(reporter, repo, number):
    marker = "<!-- maxwell:validation-summary:v1 -->"
    comments = await reporter.call(reporter.github.comments, repo, number)
    matches = [comment for comment in comments if marker in (comment.get("body") or "")]
    authors = await reporter.authorized_authors()
    if len(matches) > 1 or any(
        comment.get("user", {}).get("login") not in authors for comment in matches
    ):
        raise ValueError("PR summary marker conflicts with existing comments")
    action = reporter.store.one(
        "SELECT state FROM pr_actions WHERE repository=? AND id=? AND action='summary'",
        (repo, str(number)),
    )
    if not matches and action and action["state"] == "uncertain":
        raise ValueError("PR summary creation remains uncertain; reconciliation required")
    return matches


async def publish_summary(reporter, repo, number, body):
    matches = await summary_matches(reporter, repo, number)
    identity = str(number)
    with reporter.store.db:
        reporter.store.db.execute(
            "INSERT OR IGNORE INTO pr_actions(repository,id,action) VALUES(?,?,'summary')",
            (repo, identity),
        )
    if matches:
        comment = matches[0]
        if comment["body"] != body:
            await reporter.call(
                reporter.github.request,
                "PATCH",
                f"repos/{repo}/issues/comments/{comment['id']}",
                json={"body": body},
            )
    else:
        action = reporter.store.one(
            "SELECT state FROM pr_actions WHERE repository=? AND id=? AND action='summary'",
            (repo, identity),
        )
        if action["state"] == "uncertain":
            raise ValueError("PR summary creation remains uncertain; reconciliation required")
        with reporter.store.db:
            reporter.store.db.execute(
                "UPDATE pr_actions SET state='uncertain' WHERE repository=? AND id=? AND action='summary'",
                (repo, identity),
            )
        try:
            await reporter.call(reporter.github.create_comment, repo, number, body)
        except PublicationUncertain:
            raise
        except Exception:
            with reporter.store.db:
                reporter.store.db.execute(
                    "UPDATE pr_actions SET state='pending' WHERE repository=? AND id=? AND action='summary'",
                    (repo, identity),
                )
            raise
    with reporter.store.db:
        reporter.store.db.execute(
            "UPDATE pr_actions SET state='published',error=NULL WHERE repository=? AND id=? AND action='summary'",
            (repo, identity),
        )


async def feedback(reporter, repo, *, auto_merge=True):
    from maxwell.auto_merge import try_auto_merge
    from maxwell.reporting import record_report

    authors = await reporter.authorized_authors()
    latest = {}
    for link in records(reporter.store, repo, "pull_request"):
        latest.setdefault(link.number, link)
    assessments = records(reporter.store, repo, "assessment")
    findings = {finding.id: finding for finding in records(reporter.store, repo, "finding")}
    all_validations = records(reporter.store, repo, "validation")
    for number, link in latest.items():
        pull = await reporter.call(reporter.github.pull, repo, number)
        check_pull(pull, repo, link.branch, link.base_branch, authors)
        if pull["state"] != "open":
            if reporter.config.repair.auto_merge.enabled and reporter.store.one(
                "SELECT id FROM pr_actions WHERE repository=? AND id=? AND action='merge'",
                (repo, str(number)),
            ):
                status = await try_auto_merge(reporter, repo, link, "accepted")
                reporter.merge_statuses[number] = reporter.config.redact(status)
                current = active_validations(reporter.store, repo, link)
                body = (
                    record_report(current[0])
                    .split("<!-- maxwell:record:v1", 1)[0]
                    .replace(
                        f"<!-- maxwell:{current[0].type}:{current[0].id} -->",
                        "<!-- maxwell:validation-summary:v1 -->",
                    )
                    if current
                    else "<!-- maxwell:validation-summary:v1 -->\n### Validation\n"
                )
                await publish_summary(
                    reporter,
                    repo,
                    number,
                    reporter.config.redact(body + f"\nAuto-merge: {status}.\n"),
                )
            continue
        current = active_validations(reporter.store, repo, link)
        completed = [record for record in current if record.state == "completed"]
        outcomes = {record.outcome for record in completed}
        verdict = (
            "conflicted"
            if any(record.requires_rebase for record in current)
            else next(iter(outcomes))
            if len(outcomes) == 1
            else "inconclusive"
            if outcomes
            else "stale"
            if any(
                record.pr_id and record.finding_id == link.finding_id for record in all_validations
            )
            else "pending"
        )
        finding = findings.get(link.finding_id)
        if (
            finding
            and completed
            and any(
                set(record.references)
                != {item.id for item in input_assessments(finding, assessments)}
                for record in completed
            )
        ):
            verdict = "stale"
        current_head = (pull["head"]["sha"], pull["base"]["sha"])
        if current_head != (link.head_sha, link.base_sha):
            verdict = "stale"
        if completed and verdict not in ("stale", "conflicted") and len(outcomes) == 1:
            selected = completed[0]
            for record in completed:
                await publish_review(reporter, repo, link, pull, record)
            body = record_report(selected).split("<!-- maxwell:record:v1", 1)[0]
            body = body.replace(
                f"<!-- maxwell:{selected.type}:{selected.id} -->",
                "<!-- maxwell:validation-summary:v1 -->",
            )
        else:
            stage = "repair" if verdict == "conflicted" else "validate"
            body = f"<!-- maxwell:validation-summary:v1 -->\n### Validation — {verdict}\n\nCurrent PR head: `{pull['head']['sha']}`; target base: `{pull['base']['sha']}`. Run `maxwell scan {repo} --stage {stage} --issue {link.finding_issue}`.\n"
        await summary_matches(reporter, repo, number)
        refreshed = await reporter.call(reporter.github.pull, repo, number)
        if (refreshed["head"]["sha"], refreshed["base"]["sha"]) != current_head:
            raise ValueError("PR changed during feedback publication; validation is stale")
        desired_draft = verdict != "accepted"
        if refreshed["draft"] != desired_draft:
            try:
                await reporter.call(reporter.github.set_draft, refreshed, desired_draft)
            except PublicationUncertain:
                actual = await reporter.call(reporter.github.pull, repo, number)
                if actual["draft"] != desired_draft:
                    raise
        remote = await reporter.call(
            reporter.github.request, "GET", f"repos/{repo}/issues/{number}"
        )
        labels = {
            label["name"]
            for label in remote.get("labels", [])
            if not label["name"].startswith("maxwell:validation:")
        }
        labels.add(f"maxwell:validation:{verdict}")
        if labels != {label["name"] for label in remote.get("labels", [])}:
            await reporter.call(reporter.github.ensure_labels, repo)
            await reporter.call(
                reporter.github.request,
                "PATCH",
                f"repos/{repo}/issues/{number}",
                json={"labels": sorted(labels)},
            )
        final = await reporter.call(reporter.github.pull, repo, number)
        if (final["head"]["sha"], final["base"]["sha"]) != current_head:
            if not final["draft"]:
                await reporter.call(reporter.github.set_draft, final, True)
            labels = {label for label in labels if not label.startswith("maxwell:validation:")}
            labels.add("maxwell:validation:stale")
            await reporter.call(
                reporter.github.request,
                "PATCH",
                f"repos/{repo}/issues/{number}",
                json={"labels": sorted(labels)},
            )
            raise ValueError("PR changed during readiness update; validation is stale")
        status = (
            await try_auto_merge(reporter, repo, link, verdict)
            if auto_merge
            else "deferred while selecting or rechecking forced validation"
            if reporter.config.repair.auto_merge.enabled
            else "disabled"
        )
        reporter.merge_statuses[number] = reporter.config.redact(status)
        if reporter.config.repair.auto_merge.enabled:
            identities = {
                (record.agent.adapter, record.agent.model)
                for record in current
                if record.state == "completed" and record.outcome == "accepted"
            }
            body += f"\nAccepted validation models: {len(identities)}/{reporter.config.repair.auto_merge.required_validations}.\n"
        await publish_summary(
            reporter, repo, number, reporter.config.redact(body + f"\nAuto-merge: {status}.\n")
        )


async def finish_integrations(reporter, repo):
    accepted = [
        record
        for record in records(reporter.store, repo, "integration")
        if isinstance(record, IntegrationRecord)
        and record.state == "completed"
        and record.outcome == "accepted"
    ]
    links = {link.id: link for link in records(reporter.store, repo, "pull_request")}
    for record in accepted:
        link = links.get(record.pr_id)
        if not link or record.after is None:
            continue
        pull = await reporter.call(reporter.github.pull, repo, link.number)
        if not pull.get("merged") or pull.get("merge_commit_sha") != record.merge_sha:
            raise ValueError("Integration merge metadata changed")
        registration = reporter.store.repository(repo)
        current = await reporter.call(
            reporter.github.request, "GET", f"repos/{repo}/commits/{registration['ref']}"
        )
        if current["sha"] != record.sha:
            continue
        finding = reporter.store.one(
            "SELECT data FROM github_records WHERE repository=? AND type='finding' AND id=? AND status='current'",
            (repo, record.finding_id),
        )
        if not finding:
            continue
        subject = RECORD_ADAPTER.validate_json(finding["data"])
        if set(record.references) != {
            item.id
            for item in input_assessments(subject, records(reporter.store, repo, "assessment"))
        }:
            continue
        history = await reporter.history(repo)
        measured = record.after
        previous = projected_measurements(reporter.store, repo)
        if previous:
            measured = measured.model_copy(update={"baseline_id": previous[-1].id})
        existing = reporter.store.one(
            "SELECT data FROM record_publications WHERE repository=? AND type='measurement' AND id=?",
            (repo, measured.id),
        )
        if existing:
            measured = RECORD_ADAPTER.validate_json(existing["data"])
        reporter.store.queue_record(measured, history)
        await reporter.publish_records(repo)
        issue = await reporter.call(
            reporter.github.request, "GET", f"repos/{repo}/issues/{link.finding_issue}"
        )
        labels = {label["name"] for label in issue.get("labels", [])}
        labels.add("maxwell:integration:verified")
        await reporter.call(reporter.github.ensure_labels, repo)
        final = await reporter.call(
            reporter.github.request, "GET", f"repos/{repo}/commits/{registration['ref']}"
        )
        if final["sha"] != record.sha:
            continue
        if issue["state"] != "closed" or "maxwell:integration:verified" not in {
            label["name"] for label in issue.get("labels", [])
        }:
            await reporter.call(
                reporter.github.request,
                "PATCH",
                f"repos/{repo}/issues/{link.finding_issue}",
                json={"state": "closed", "labels": sorted(labels)},
            )
