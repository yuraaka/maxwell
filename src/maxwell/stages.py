AGENT_STAGES = ("hunt", "validate", "audit", "fix", "review", "integrate")
SKIPPABLE_STAGES = ("source", "tests", *AGENT_STAGES)

# Published role identifiers remain stable for record digests and reputation history.
STAGE_ROLES = {
    "hunt": "hunter",
    "validate": "reviewer",
    "audit": "auditor",
    "fix": "fixer",
    "review": "validator",
    "integrate": "validator",
}
LEGACY_STAGES = {"discovery": "hunt", "repair": "fix", "validation": "review"}
ROLE_STAGES = {role: stage for stage, role in STAGE_ROLES.items() if stage != "integrate"}


def canonical_stage(stage):
    return LEGACY_STAGES.get(stage, stage)


def stage_name(stage):
    return canonical_stage(stage)


def agent_name(role, *, stage=None):
    if role == "human":
        return "human"
    return f"{stage_name(stage or ROLE_STAGES.get(role, role))} agent"


def stage_role(stage):
    if stage == "reproduction":
        return "reproducer"
    return STAGE_ROLES[LEGACY_STAGES.get(stage, stage)]


STORED_STAGES = {
    "hunt": "discovery",
    "validate": "review",
    "audit": "audit",
    "fix": "repair",
    "review": "validate",
    "integrate": "integrate",
}


def stored_stage(stage):
    return STORED_STAGES.get(stage, stage)


def stage_from_storage(stage):
    return next(
        (name for name, stored in STORED_STAGES.items() if stored == stage), canonical_stage(stage)
    )


def select_stages(value=None):
    if value is None:
        return AGENT_STAGES
    pieces = value.split("-")
    if len(pieces) > 2 or not pieces[0]:
        raise ValueError(
            "Use --stage NAME, START-END, or START-; stages: " + ", ".join(AGENT_STAGES)
        )
    start = canonical_stage(pieces[0])
    end = canonical_stage(pieces[1]) if len(pieces) == 2 and pieces[1] else None
    if start not in AGENT_STAGES or end is not None and end not in AGENT_STAGES:
        raise ValueError("Unknown stage; choose from: " + ", ".join(AGENT_STAGES))
    first = AGENT_STAGES.index(start)
    last = AGENT_STAGES.index(end) if end else len(AGENT_STAGES) - 1 if len(pieces) == 2 else first
    if last < first:
        raise ValueError("Stage interval is reversed; order: " + " → ".join(AGENT_STAGES))
    return AGENT_STAGES[first : last + 1]
