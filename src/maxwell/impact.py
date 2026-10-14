from dataclasses import dataclass, field

from maxwell.assessment import active_assessments, finding_assessments, finding_factors
from maxwell.contracts import FACTORS
from maxwell.measurement import measurement_contributions
from maxwell.records import RECORD_ADAPTER

POLICY = "maintenance-impact-v1"
DASHBOARD_LIMIT = 50


@dataclass(frozen=True)
class Impact:
    status: str
    reason: str
    reductions: dict[str, tuple[float, float]] = field(default_factory=dict)
    low: float | None = None
    high: float | None = None
    rate: float | None = None


def evaluate_repair(finding, assessment, measurements, associations):
    if (
        assessment.repository != finding.repository
        or assessment.finding_id != finding.id
        or assessment.sha != finding.sha
    ):
        return Impact("stale", "Assessment targets another finding or revision")
    repair = assessment.repair
    if repair is None:
        return Impact("missing", "No repair estimate")
    baseline = next((item for item in measurements if item.id == repair.measurement_id), None)
    if baseline is None:
        return Impact("missing", "Referenced measurement unavailable")
    if baseline.repository != finding.repository or baseline.sha != finding.sha:
        return Impact("stale", "Measurement is from a different repository or revision")
    contributions = measurement_contributions(baseline)
    if contributions is None or repair.protocol != baseline.protocol:
        return Impact("incompatible", "Measurement policy or protocol does not match")
    if measurements and measurements[-1].id != baseline.id:
        return Impact("stale", "Estimate predates the latest measurement")
    projections = {item.factor: item for item in repair.projections}
    if set(projections) - set(associations):
        return Impact("invalid", "Projection lacks an established factor association")
    reductions = {}
    for name in FACTORS:
        if name not in projections and name not in repair.unchanged:
            continue
        if name not in contributions:
            return Impact("partial", f"{name} baseline is unknown")
        stage = "tests" if name.startswith("test_") else "source"
        origin = baseline.evidence.get("reused", {}).get(stage)
        if origin and origin["sha"] != finding.sha:
            return Impact("stale", f"{stage} evidence was reused from another revision")
        if name in repair.unchanged:
            reductions[name] = (0.0, 0.0)
            continue
        projection = projections[name]
        if name == "coupling":
            ratios = [
                (
                    (
                        item.reachable_pairs / item.possible_dependency_pairs
                        if item.possible_dependency_pairs
                        else 0
                    )
                    + (
                        item.disconnected_pairs / item.possible_member_pairs
                        if item.possible_member_pairs
                        else 0
                    )
                )
                / 2
                for item in projection.architecture
            ]
        else:
            scenarios = projection.scenarios
            if name == "test_runtime" and any(
                item.denominator != baseline.factors[name].denominator for item in scenarios
            ):
                return Impact("invalid", "Runtime budget must remain unchanged")
            if name != "test_runtime" and any(
                item.numerator > item.denominator for item in scenarios
            ):
                return Impact("invalid", "Projected burden exceeds its population")
            ratios = [min(item.numerator / item.denominator, 1) for item in scenarios]
        points = [100 * baseline.policy["weights"][name] * ratio for ratio in ratios]
        reductions[name] = (contributions[name] - max(points), contributions[name] - min(points))
    if set(reductions) != set(FACTORS):
        return Impact("partial", "Unspecified factors remain unknown", reductions)
    low = sum(value[0] for value in reductions.values())
    high = sum(value[1] for value in reductions.values())
    return Impact(
        "usable",
        "Projected, not observed",
        reductions,
        low,
        high,
        max(0, low) * repair.confidence / repair.effort_hours.high,
    )


def repair_decision(finding, assessments):
    relevant = [
        item for item in assessments if item.finding_id == finding.id and item.sha == finding.sha
    ]
    reviews, audits = finding_assessments(relevant, finding)
    humans = active_assessments(relevant, "human")
    decisions = humans or audits or reviews
    if not decisions or any(
        item.verdict != "supported" or item.actionability != "actionable" for item in decisions
    ):
        return None, "not_actionable", [], None
    level = "human" if humans else "audited" if audits else "provisional review"
    priority = min((item.priority or finding.finding.priority) for item in decisions)
    repairs = [item.repair for item in decisions]
    if any(item != repairs[0] for item in repairs[1:]):
        return None, "conflicting", [], priority
    associations = finding_factors(finding, reviews, audits)
    if humans:
        associations = finding_factors(finding, humans)
    return decisions[0], level, associations, priority


def fix_queue(store, repository, remote, measurements):
    rows = store.rows(
        "SELECT issue,data FROM github_records WHERE repository=? AND status='current' "
        "AND type IN ('finding','assessment')",
        (repository,),
    )
    records = [(row["issue"], RECORD_ADAPTER.validate_json(row["data"])) for row in rows]
    assessments = [record for _, record in records if record.type == "assessment"]
    maintenance, bugs = [], []
    for issue, finding in records:
        if finding.type != "finding" or finding.finding.identity(repository) != finding.id:
            continue
        source = remote.get(issue)
        if not source or source["state"] != "open":
            continue
        if "maxwell:state:rejected" in {label["name"] for label in source.get("labels", [])}:
            continue
        decision, level, associations, priority = repair_decision(finding, assessments)
        if level == "not_actionable":
            continue
        if finding.finding.kind == "bug":
            bugs.append({"issue": issue, "priority": priority, "title": finding.finding.title})
        impact = (
            evaluate_repair(finding, decision, measurements, associations)
            if decision
            else Impact("conflicting", "Current assessments propose different repair plans")
        )
        maintenance.append(
            {
                "issue": issue,
                "title": finding.finding.title,
                "level": level,
                "impact": impact,
                "repair": decision.repair if decision else None,
            }
        )
    maintenance.sort(
        key=lambda item: (
            not (item["impact"].status == "usable" and (item["impact"].rate or 0) > 0),
            -(item["impact"].rate or 0),
            -(item["repair"].confidence if item["repair"] else 0),
            item["issue"],
        )
    )
    bugs.sort(key=lambda item: (item["priority"], item["issue"]))
    return maintenance, bugs


def queue_report(repository, maintenance, bugs):
    def title(item):
        return item["title"][:120].replace("|", "\\|").replace("\n", " ")

    body = "### Fix queue\n\n"
    body += f"Projected impact uses `{POLICY}`; confidence is uncalibrated. "
    body += "Ranking = conservative point reduction × confidence / upper effort hours. "
    body += "Estimates may overlap and are never added together.\n\n"
    body += (
        "| Maintenance finding | Assessment | Estimated reduction | Effort hours | Rank score |\n"
    )
    body += "| --- | --- | --- | --- | --- |\n"
    for item in maintenance[:DASHBOARD_LIMIT]:
        impact, repair = item["impact"], item["repair"]
        reduction = (
            f"{impact.low:.2f}–{impact.high:.2f} points"
            if impact.status == "usable"
            else f"{impact.status}: {impact.reason}"
        )
        effort = (
            f"{repair.effort_hours.low:g}–{repair.effort_hours.high:g}" if repair else "Unknown"
        )
        rate = f"{impact.rate:.3f}" if impact.status == "usable" and impact.rate > 0 else "Unranked"
        body += f"| [#{item['issue']}: {title(item)}](https://github.com/{repository}/issues/{item['issue']}) | {item['level']} | {reduction} | {effort} | {rate} |\n"
    if not maintenance:
        body += "| No supported actionable findings | — | — | — | — |\n"
    if len(maintenance) > DASHBOARD_LIMIT:
        body += (
            f"\n{len(maintenance) - DASHBOARD_LIMIT} additional findings omitted from this table.\n"
        )
    body += "\nBug priority is independent of estimated structural benefit.\n\n"
    for item in bugs[:DASHBOARD_LIMIT]:
        body += f"- {item['priority']}: [#{item['issue']}: {title(item)}](https://github.com/{repository}/issues/{item['issue']})\n"
    if len(bugs) > DASHBOARD_LIMIT:
        body += f"\n{len(bugs) - DASHBOARD_LIMIT} additional bugs omitted from this list.\n"
    return body
