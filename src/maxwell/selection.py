from maxwell.contracts import SelectionStats

KIND_ORDER = {"bug": 0, "duplication": 1, "simplification": 2, "documentation": 3}


def select_candidates(
    candidates,
    repository,
    workspace,
    *,
    publication_limit,
    slots,
    minimum_confidence,
    local_ids,
    remote_ids,
):
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.priority,
            -item.confidence,
            KIND_ORDER[item.kind],
            item.identity(repository),
        ),
    )
    selected, observations, seen = [], [], set()
    counts = dict.fromkeys(
        (
            "below_confidence",
            "duplicate",
            "already_known",
            "publication_limit",
            "review_queue_limit",
        ),
        0,
    )
    for finding in ranked:
        finding.validate_location(workspace)
        identity = finding.identity(repository)
        if finding.confidence < minimum_confidence:
            counts["below_confidence"] += 1
            continue
        if identity in seen:
            counts["duplicate"] += 1
            continue
        seen.add(identity)
        if identity in local_ids or identity in remote_ids:
            counts["already_known"] += 1
            if identity in local_ids:
                observations.append(finding)
        elif len(selected) >= publication_limit:
            counts["publication_limit"] += 1
        elif len(selected) >= max(0, slots):
            counts["review_queue_limit"] += 1
        else:
            selected.append(finding)
    stats = SelectionStats(
        collected=len(candidates),
        selected=len(selected),
        skipped=sum(counts.values()),
        **counts,
    )
    return selected, observations, stats
