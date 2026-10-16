from collections import defaultdict

from maxwell.assessment import finding_assessments
from maxwell.impact import input_assessments
from maxwell.records import (
    RECORD_ADAPTER,
    AssessmentRecord,
    ExecutionRecord,
    FindingRecord,
    RepairRecord,
    ValidationRecord,
)


def consensus(records):
    verdicts = {record.verdict for record in records}
    if not verdicts:
        return "unassessed"
    if len(verdicts) == 1 and "inconclusive" not in verdicts:
        return next(iter(verdicts))
    return "unresolved"


def current_reputation_records(store, repository):
    return [
        (row["issue"], RECORD_ADAPTER.validate_json(row["data"]))
        for row in store.rows(
            "SELECT issue,data FROM github_records WHERE repository=? AND status='current' "
            "AND type IN ('finding','assessment','execution','repair','validation')",
            (repository,),
        )
    ]


def agent_identity(agent):
    return (agent.adapter, agent.model, agent.effort) if agent else None


def fixer_observations(records):
    assessments = [record for _, record in records if isinstance(record, AssessmentRecord)]
    findings = {record.id: record for _, record in records if isinstance(record, FindingRecord)}
    repairs = {record.id: record for _, record in records if isinstance(record, RepairRecord)}
    validations = [
        record
        for _, record in records
        if isinstance(record, ValidationRecord) and record.type == "validation"
    ]
    superseded = {identity for record in validations for identity in record.supersedes}
    groups = defaultdict(list)
    for record in validations:
        repair = repairs.get(record.repair_id)
        finding = findings.get(record.finding_id)
        if not repair or not finding or record.id in superseded or record.state != "completed":
            continue
        if (
            finding.finding.identity(finding.repository) != finding.id
            or repair.outcome != "prepared"
            or repair.commit != (record.pr_head or record.sha)
            or finding.sha != record.base_sha
            or set(repair.references)
            != {item.id for item in input_assessments(finding, assessments)}
            or (repair.agent.adapter, repair.agent.model)
            == (record.agent.adapter, record.agent.model)
        ):
            continue
        groups[(finding.id, record.base_sha, record.sha, agent_identity(repair.agent))].append(
            record
        )
    for key, current in groups.items():
        outcomes = {record.outcome for record in current}
        if len(outcomes) == 1 and outcomes <= {"accepted", "rejected"}:
            yield key[-1], next(iter(outcomes))


def selection_evidence(store, repository, role):
    records = current_reputation_records(store, repository)
    if role == "fixer":
        outcomes = defaultdict(lambda: [0, 0])
        for identity, outcome in fixer_observations(records):
            outcomes[identity][outcome == "rejected"] += 1
        return dict(outcomes)
    assessments = defaultdict(list)
    for issue, record in records:
        if isinstance(record, AssessmentRecord):
            assessments[issue].append(record)
    outcomes = defaultdict(lambda: [0, 0])
    observations = set()
    for issue, finding in records:
        if (
            not isinstance(finding, FindingRecord)
            or finding.finding.identity(repository) != finding.id
        ):
            continue
        reviews, audits = finding_assessments(assessments[issue], finding)
        if role == "hunter":
            outcome = consensus(audits or reviews)
            identity = agent_identity(finding.agent)
            if identity and outcome in ("supported", "rejected"):
                outcomes[identity][outcome == "rejected"] += 1
        elif role == "reviewer":
            audit_verdict = consensus(audits)
            grouped = defaultdict(list)
            for review in reviews:
                if identity := agent_identity(review.agent):
                    grouped[identity].append(review)
            for identity, group in grouped.items():
                observation = (finding.id, finding.sha, identity)
                verdict = consensus(group)
                if (
                    observation in observations
                    or verdict not in ("supported", "rejected")
                    or audit_verdict not in ("supported", "rejected")
                ):
                    continue
                observations.add(observation)
                outcomes[identity][verdict != audit_verdict] += 1
    return dict(outcomes)


def agent_reputation(store, repository):
    records = current_reputation_records(store, repository)
    assessments = defaultdict(list)
    for issue, record in records:
        if isinstance(record, AssessmentRecord):
            assessments[issue].append(record)
    agents = {}

    def tally(agent, role):
        key = (
            (role, agent.adapter, agent.model, agent.effort) if agent else (role, None, None, None)
        )
        if key not in agents:
            agents[key] = {
                "agent": agent.model_dump(include={"adapter", "model", "effort", "role"})
                if agent
                else None,
                "role": role,
                "findings": 0,
                "review_supported": 0,
                "review_rejected": 0,
                "review_unresolved": 0,
                "review_unassessed": 0,
                "audit_supported": 0,
                "audit_rejected": 0,
                "audit_unresolved": 0,
                "audit_unassessed": 0,
                "assessments": 0,
                "repairs": 0,
                "validation_accepted": 0,
                "validation_rejected": 0,
                "validation_inconclusive": 0,
                "fixer_successes": 0,
                "fixer_failures": 0,
                "agreement": 0,
                "disagreement": 0,
                "unresolved": 0,
                "executions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "unknown_usage": 0,
                "incomplete_usage": 0,
            }
        return key, agents[key]

    observations = set()
    for issue, finding in records:
        if not isinstance(finding, FindingRecord):
            continue
        if finding.finding.identity(repository) != finding.id:
            continue
        reviews, audits = finding_assessments(assessments[issue], finding)
        key, hunter = tally(finding.agent, "hunter")
        observation = (finding.id, finding.sha, key)
        if observation not in observations:
            observations.add(observation)
            hunter["findings"] += 1
            hunter[f"review_{consensus(reviews)}"] += 1
            hunter[f"audit_{consensus(audits)}"] += 1
        for role, current in (("reviewer", reviews), ("auditor", audits)):
            grouped = defaultdict(list)
            for assessment in current:
                key, _ = tally(assessment.agent, role)
                grouped[key].append(assessment)
            for key, group in grouped.items():
                observation = (finding.id, finding.sha, key)
                if observation in observations:
                    continue
                observations.add(observation)
                result = agents[key]
                result["assessments"] += 1
                if role == "reviewer":
                    review_verdict, audit_verdict = consensus(group), consensus(audits)
                    if review_verdict not in ("supported", "rejected") or audit_verdict not in (
                        "supported",
                        "rejected",
                    ):
                        outcome = "unresolved"
                    else:
                        outcome = "agreement" if review_verdict == audit_verdict else "disagreement"
                    result[outcome] += 1

    seen_repairs = set()
    seen_validations = set()
    superseded = {
        identity
        for _, record in records
        if isinstance(record, ValidationRecord)
        for identity in record.supersedes
    }
    for _, record in records:
        if isinstance(record, RepairRecord) and record.id not in seen_repairs:
            seen_repairs.add(record.id)
            _, result = tally(record.agent, "fixer")
            result["repairs"] += 1
        elif (
            isinstance(record, ValidationRecord)
            and record.id not in seen_validations
            and record.id not in superseded
        ):
            seen_validations.add(record.id)
            _, result = tally(record.agent, "validator")
            result[f"validation_{record.outcome}"] += 1
    for identity, outcome in fixer_observations(records):
        result = agents.get(("fixer", *identity))
        if result:
            result["fixer_failures" if outcome == "rejected" else "fixer_successes"] += 1

    executions = set()
    for _, record in records:
        if not isinstance(record, ExecutionRecord) or record.id in executions:
            continue
        executions.add(record.id)
        _, result = tally(record.agent, record.agent.role if record.agent else "unattributed")
        result["executions"] += 1
        if record.usage.reported:
            result["input_tokens"] += record.usage.input_tokens
            result["output_tokens"] += record.usage.output_tokens
        if record.state != "skipped":
            result["unknown_usage"] += not record.usage.reported
            result["incomplete_usage"] += not record.usage.complete
    return [
        agents[key] for key in sorted(agents, key=lambda key: tuple(part or "" for part in key))
    ]


def reputation_report(rows):
    def identity(row):
        agent = row["agent"]
        if not agent:
            return f"Unattributed ({row['role']})"
        value = f"{agent['adapter']} / {agent['model']} / {agent['effort'] or 'default'}"
        return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|")

    def table(role, headers, fields):
        selected = [row for row in rows if row["role"] == role]
        if not selected:
            return "No current records.\n"
        lines = [
            "| Agent | " + " | ".join(headers) + " |",
            "| --- | " + " | ".join("---" for _ in headers) + " |",
        ]
        for row in selected:
            values = [f"{row[field]:,}" for field in fields]
            lines.append("| " + identity(row) + " | " + " | ".join(values) + " |")
        return "\n".join(lines) + "\n"

    body = "### Agent reputation\n\nProvisional counts from current GitHub records; no numerical score.\n"
    for stage in ("review", "audit"):
        body += f"\n#### Hunters: {stage} source support\n\n"
        body += table(
            "hunter",
            ["Findings", "Supported", "Rejected", "Unresolved", "Unassessed"],
            [
                "findings",
                *(
                    f"{stage}_{outcome}"
                    for outcome in ("supported", "rejected", "unresolved", "unassessed")
                ),
            ],
        )
    body += "\n#### Reviewers: agreement with auditors\n\n"
    body += table(
        "reviewer",
        ["Assessments", "Agreement", "Disagreement", "Unresolved"],
        ["assessments", "agreement", "disagreement", "unresolved"],
    )
    body += "\n#### Auditors\n\nQuality: unrated until independent validation.\n\n"
    body += table("auditor", ["Current assessments"], ["assessments"])
    body += "\n#### Fixers: independent repair validation\n\n"
    body += table(
        "fixer",
        ["Attempts", "Accepted", "Rejected"],
        ["repairs", "fixer_successes", "fixer_failures"],
    )
    body += "\n#### Validators\n\nQuality: unrated until separately adjudicated.\n\n"
    body += table(
        "validator",
        ["Accepted", "Rejected", "Inconclusive"],
        ["validation_accepted", "validation_rejected", "validation_inconclusive"],
    )
    body += "\n#### Investigation tokens\n\n"
    body += "| Agent | Role | Executions | Input | Output | Total | Unknown usage | Incomplete usage |\n"
    body += "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    for row in rows:
        values = [row[field] for field in ("executions", "input_tokens", "output_tokens")]
        values += [
            row["input_tokens"] + row["output_tokens"],
            row["unknown_usage"],
            row["incomplete_usage"],
        ]
        body += (
            "| "
            + identity(row)
            + " | "
            + row["role"]
            + " | "
            + " | ".join(f"{value:,}" for value in values)
            + " |\n"
        )
    body += (
        "\nCounts describe source support and reviewer–auditor agreement, not proven accuracy. "
        "Audits are selectively chosen and can share agent biases. Inconclusive or conflicting "
        "verdicts remain unresolved. Missing audits are unresolved for reviewers. "
        "Each finding/revision/agent contributes at most one current observation; "
        "superseded or invalid evidence is excluded.\n\n"
        "Tokens include historical and failed executions, counted once per execution; "
        "cached tokens are included in input. Unknown usage is not zero. "
        "Tokens do not change quality counts.\n"
    )
    return body
