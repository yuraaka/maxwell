import httpx

from maxwell.github import PublicationUncertain
from maxwell.impact import input_assessments


def evidence_blocker(policy, link, finding, repair, validations, assessments):
    if not finding or not repair:
        return "finding or repair provenance is missing"
    references = {item.id for item in input_assessments(finding, assessments)}
    if not references or set(repair.references) != references:
        return "finding assessments changed; repair inputs are stale"
    if any(set(record.references) != references for record in validations):
        return "finding assessments changed; validation is stale"
    if any(record.state != "completed" or record.outcome != "accepted" for record in validations):
        return "a current validation is rejected, inconclusive, or incomplete"
    repair_agent = (repair.agent.adapter, repair.agent.model)
    agents = {(record.agent.adapter, record.agent.model) for record in validations}
    if repair_agent in agents:
        return "a review agent uses the fix agent's model"
    if len(agents) < policy.required_reviews:
        return f"waiting for independent validations ({len(agents)}/{policy.required_reviews})"
    for record in validations:
        if record.result and record.result.concerns:
            return "blocking validation concern: " + record.result.concerns[0][:250]
        if (
            record.pr_id != link.id
            or record.pr_head != link.head_sha
            or record.pr_base != link.base_sha
            or not record.tested_sha
            or not record.result
            or record.result.verdict != "repaired"
            or not record.checks
            or any(check.state != "passed" or check.returncode != 0 for check in record.checks)
        ):
            return "validation lacks passing host evidence or has unresolved concerns"
        if finding.finding.kind == "bug" and (
            record.result.reproduction != "reproduced"
            or not record.replay
            or record.replay.state != "failed"
            or record.replay.returncode != 1
            or not any(failure.get("phase") == "call" for failure in record.failures)
        ):
            return "original-defect regression evidence is incomplete"
    return None


async def github_blocker(reporter, repo, pull):
    url = pull.get("html_url") or f"https://github.com/{repo}/pull/{pull.get('number', '')}"
    state = pull.get("mergeable_state")
    if pull.get("mergeable") is False or state == "dirty":
        return f"PR has merge conflicts; update the repair against its target branch: {url}"
    if pull.get("mergeable") is None or state in (None, "unknown"):
        return f"GitHub is calculating mergeability; Maxwell will recheck: {url}"
    if state == "behind":
        return f"Update the PR branch to satisfy GitHub's up-to-date requirement, then revalidate: {url}"
    if state == "blocked":
        return f"GitHub blocks merging; resolve pending or failed required checks, required reviews, or repository rules: {url}"
    if state not in ("clean", "unstable", "has_hooks"):
        return f"GitHub reports merge state {state!r}; inspect the PR's merge requirements: {url}"
    return None


def merge_error_message(response, repo, number, method):
    try:
        detail = response.json().get("message") or "GitHub returned no reason"
    except (ValueError, AttributeError):
        detail = "GitHub returned no reason"
    instructions = {
        401: "Replace the expired or invalid GitHub token and run maxwell config apply",
        403: "Check repository write access, token Contents and Pull requests write permissions, SSO authorization, and organization rules; run maxwell config apply",
        404: "Check that this token can access the repository and PR; run maxwell config apply",
        405: "Resolve the required checks, reviews, merge queue, or branch rules shown on the PR",
        409: "The PR head changed; synchronize and run fresh validation",
        422: f"Check that {method!r} merging is enabled in Settings > General > Pull Requests, or change fix.auto_merge.merge_method and run maxwell config apply",
    }
    advice = instructions.get(response.status_code, "Inspect the PR's merge requirements")
    return f"GitHub denied the merge (HTTP {response.status_code}): {detail}. {advice}. https://github.com/{repo}/pull/{number}"


def save_action(reporter, repo, identity, state, error=None):
    with reporter.store.db:
        reporter.store.db.execute(
            "INSERT INTO pr_actions(repository,id,action,state,error) VALUES(?,?,'merge',?,?) "
            "ON CONFLICT(repository,id,action) DO UPDATE SET state=excluded.state,error=excluded.error",
            (repo, identity, state, error),
        )


def projected_blocker(reporter, repo, link):
    from maxwell.repair_pull_requests import active_validations, records

    finding = next(
        (item for item in records(reporter.store, repo, "finding") if item.id == link.finding_id),
        None,
    )
    repair = next(
        (item for item in records(reporter.store, repo, "repair") if item.id == link.repair_id),
        None,
    )
    return evidence_blocker(
        reporter.config.fix.auto_merge,
        link,
        finding,
        repair,
        active_validations(reporter.store, repo, link),
        records(reporter.store, repo, "assessment"),
    )


async def integration_pool_blocker(reporter, repo):
    from maxwell.repair_pull_requests import records

    capacity = reporter.config.pipeline.pools.integrate
    if capacity == 0:
        return "integration pool is paused (pipeline.pools.integrate=0)"
    latest = {}
    for link in records(reporter.store, repo, "pull_request"):
        latest.setdefault(link.number, link)
    occupied = set()
    for link in latest.values():
        pull = await reporter.call(reporter.github.pull, repo, link.number)
        if not pull.get("merged"):
            continue
        finding = await reporter.call(
            reporter.github.request, "GET", f"repos/{repo}/issues/{link.finding_issue}"
        )
        if finding["state"] == "open":
            occupied.add(link.finding_id)
    if len(occupied) >= capacity:
        return f"integration pool full ({len(occupied)}/{capacity}); awaiting maintained-branch verification"
    return None


async def try_auto_merge(reporter, repo, link, verdict):
    from maxwell.repair_pull_requests import check_pull, records

    policy = reporter.config.fix.auto_merge
    identity = str(link.number)
    if not policy.enabled:
        return "disabled"
    pull = await reporter.call(reporter.github.pull, repo, link.number)
    check_pull(pull, repo, link.branch, link.base_branch, await reporter.authorized_authors())
    if pull.get("merged"):
        save_action(reporter, repo, identity, "published")
        return "merged; waiting for independent integration verification"
    allowed = getattr(reporter, "merge_issues", None)
    if allowed is not None and link.finding_issue not in allowed:
        return "outside this pipeline's initial finding snapshot"
    previous = reporter.store.one(
        "SELECT state FROM pr_actions WHERE repository=? AND id=? AND action='merge'",
        (repo, identity),
    )
    if previous and previous["state"] in ("uncertain", "published"):
        return "merge outcome awaiting GitHub reconciliation; no duplicate merge will be sent"
    if pull["state"] != "open" or pull["draft"] or verdict != "accepted":
        return "waiting for an accepted, ready PR"
    if (pull["head"]["sha"], pull["base"]["sha"]) != (link.head_sha, link.base_sha):
        return "head or base changed; fresh validation required"
    blocker = projected_blocker(reporter, repo, link)
    if blocker:
        return blocker
    await reporter.project_repository_records(repo)
    latest = next(
        (
            item
            for item in records(reporter.store, repo, "pull_request")
            if item.number == link.number
        ),
        None,
    )
    if latest != link:
        return "PR revision association changed; fresh validation required"
    blocker = projected_blocker(reporter, repo, link)
    if blocker:
        return blocker
    if reporter.store.one(
        "SELECT id FROM scans WHERE repository=? AND stage IN ('fix','repair','validate') AND state IN ('queued','running') "
        "AND json_extract(execution,'$.target.finding.id')=?",
        (repo, link.finding_id),
    ):
        return "a repair amendment or validation is queued or running"
    if reporter.store.one(
        "SELECT id FROM record_publications WHERE repository=? AND type IN ('repair','validation') "
        "AND state!='published' AND json_extract(data,'$.finding_id')=?",
        (repo, link.finding_id),
    ):
        return "repair or validation evidence is awaiting GitHub publication"
    issue = await reporter.call(
        reporter.github.request, "GET", f"repos/{repo}/issues/{link.finding_issue}"
    )
    if issue.get("state") != "open":
        return "finding is no longer open"
    if "maxwell:state:rejected" in {label["name"] for label in issue.get("labels", [])}:
        return "finding is marked rejected"
    refreshed = await reporter.call(reporter.github.pull, repo, link.number)
    if (
        refreshed["state"] != "open"
        or refreshed["draft"]
        or (refreshed["head"]["sha"], refreshed["base"]["sha"]) != (link.head_sha, link.base_sha)
    ):
        return "PR changed before merge; fresh validation required"
    blocker = await github_blocker(reporter, repo, refreshed)
    if blocker:
        return blocker
    final = await reporter.call(reporter.github.pull, repo, link.number)
    check_pull(final, repo, link.branch, link.base_branch, await reporter.authorized_authors())
    if (
        final["state"] != "open"
        or final["draft"]
        or (final["head"]["sha"], final["base"]["sha"]) != (link.head_sha, link.base_sha)
    ):
        return "PR changed before merge; fresh validation required"
    blocker = await github_blocker(reporter, repo, final)
    if blocker:
        return blocker
    blocker = await integration_pool_blocker(reporter, repo)
    if blocker:
        return blocker
    # Persist uncertainty before the mutation so a crash cannot cause a blind retry.
    with reporter.store.db:
        reporter.store.db.execute(
            "INSERT OR IGNORE INTO pr_actions(repository,id,action) VALUES(?,?,'merge')",
            (repo, identity),
        )
        claimed = reporter.store.db.execute(
            "UPDATE pr_actions SET state='uncertain',error=NULL WHERE repository=? AND id=? "
            "AND action='merge' AND state IN ('pending','blocked')",
            (repo, identity),
        ).rowcount
    if not claimed:
        return "merge outcome awaiting GitHub reconciliation; no duplicate merge will be sent"
    try:
        result = await reporter.call(
            reporter.github.merge_pull, repo, link.number, link.head_sha, policy.merge_method
        )
    except PublicationUncertain as exc:
        save_action(reporter, repo, identity, "uncertain", str(exc))
        actual = await reporter.call(reporter.github.pull, repo, link.number)
        if actual.get("merged"):
            save_action(reporter, repo, identity, "published")
            return "merged; waiting for independent integration verification"
        return "merge response lost; waiting for GitHub reconciliation, without retrying the merge"
    except httpx.HTTPStatusError as exc:
        save_action(reporter, repo, identity, "blocked", f"HTTP {exc.response.status_code}")
        if exc.response.status_code in (401, 403, 404, 405, 409, 422):
            return reporter.config.redact(
                merge_error_message(exc.response, repo, link.number, policy.merge_method)
            )[:2000]
        raise
    if not result.get("merged"):
        save_action(reporter, repo, identity, "blocked", "GitHub did not merge the PR")
        detail = reporter.config.redact(str(result.get("message") or "GitHub returned no reason"))[
            :600
        ]
        return f"GitHub did not merge the PR: {detail}. Inspect required checks, reviews and repository rules: https://github.com/{repo}/pull/{link.number}"
    actual = await reporter.call(reporter.github.pull, repo, link.number)
    if not actual.get("merged") or not actual.get("merge_commit_sha"):
        save_action(reporter, repo, identity, "uncertain", "Merge response awaits PR confirmation")
        return "GitHub reported a merge; waiting for PR confirmation without sending another merge"
    save_action(reporter, repo, identity, "published")
    return "merged; waiting for independent integration verification"
