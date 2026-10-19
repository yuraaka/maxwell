from maxwell.records import ValidationRecord
from maxwell.repair import collect_repair_targets
from maxwell.validation import collect_validation_targets


def feedback(result):
    return [
        ValidationRecord.model_validate(
            {key: value for key, value in item.items() if key in ValidationRecord.model_fields}
        )
        for item in result["validations"]
    ]


async def complete_review(reporter, repository, target, initial, launch, progress, max_fixes):
    attempts = [initial]
    current = initial
    for number in range(max_fixes + 1):
        records = feedback(current)
        if not records or current["publication_errors"]:
            return attempts, None
        if all(record.outcome == "accepted" for record in records):
            for previous in attempts[:-1]:
                if previous["stage"] == "review":
                    previous["resolved_by"] = current["id"]
            return attempts, None
        if not any(record.requires_fix for record in records):
            return attempts, (
                f"PR #{target.pull_request.number}: review is unresolved without actionable fix "
                "feedback; no automatic edit was attempted"
            ) if current["state"] == "completed" else None
        if number == max_fixes:
            return attempts, (
                f"PR #{target.pull_request.number}: review still requires changes after "
                f"{max_fixes} fix attempts (review.max_fix_attempts); all attempts retained"
            )
        reason = next(record.summary for record in records if record.requires_fix)
        progress(
            {
                "repository": repository,
                "state": "recovery",
                "stage": "fix",
                "detail": f"PR #{target.pull_request.number}: fix attempt {number + 1}/{max_fixes}; {reason[:300]}",
            }
        )
        active_stage = "fix"
        try:
            fixes = await collect_repair_targets(reporter, repository, issue=target.issue)
            if not fixes:
                raise ValueError("No current fix target for the review feedback")
            repaired = await launch(
                "fix",
                fixes[0],
                avoid={(record.agent.adapter, record.agent.model) for record in records},
            )
            attempts.append(repaired)
            if (
                repaired["state"] != "completed"
                or repaired["publication_errors"]
                or not any(item["outcome"] == "prepared" for item in repaired["repairs"])
            ):
                progress(
                    {"repository": repository, "state": "stage", "stage": "fix", "status": "failed"}
                )
                return (
                    attempts,
                    f"PR #{target.pull_request.number}: fix failed or was not published; review was not retried",
                )
            progress(
                {
                    "repository": repository,
                    "state": "stage",
                    "stage": "fix",
                    "status": "completed",
                }
            )
            following = await collect_validation_targets(
                reporter, repository, issue=target.issue, force=True
            )
            if not following:
                raise ValueError("Updated PR is not eligible for independent review")
            target = following[0]
            active_stage = "review"
            progress(
                {
                    "repository": repository,
                    "state": "stage",
                    "stage": "review",
                    "status": "running",
                    "detail": f"PR #{target.pull_request.number}: reviewing revision after fix {number + 1}/{max_fixes}",
                }
            )
            current = await launch("review", target)
            attempts.append(current)
        except Exception as exc:
            progress(
                {
                    "repository": repository,
                    "state": "stage",
                    "stage": active_stage,
                    "status": "failed",
                }
            )
            return (
                attempts,
                f"PR #{target.pull_request.number}: review recovery stopped: {reporter.config.redact(str(exc))}",
            )
    return attempts, None
