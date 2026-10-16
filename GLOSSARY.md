# Maxwell

Maxwell discovers repository problems, assesses evidence, and carries repairs through independent checks.

## Language

**measure**:
The baseline measurement that establishes repository entropy and its components from source and test observations.

**hunt**:
The stage that identifies candidate findings in a repository.
_Avoid_: Discovery as a stage name.

**validate**:
The stage that independently assesses a finding's evidence and actionability.
_Avoid_: review for finding assessment, Val.

**audit**:
The stage that independently checks a finding and its previous assessments.
_Avoid_: Aud.

**fix**:
The stage that implements a finding's proposed correction or addresses actionable feedback on its existing repair PR.
_Avoid_: repair as a stage name.

**review**:
The stage that independently evaluates a repair PR before merge.
_Avoid_: validate for PR assessment, Rev.

**integrate**:
The stage that verifies the merged repair on the maintained branch before closing its finding.
_Avoid_: Int, Integration as a stage name.

**stage agent**:
An agent performing a named stage: hunt agent, validate agent, audit agent, fix agent, review agent, or integrate agent.
_Avoid_: Hunter, Reviewer, Auditor, Fixer, Validator, Integration validator as separate role names.

**stage pool**:
The findings occupying a stage up to its configured capacity. Findings waiting for a slot form its backlog.

**merge**:
The act of incorporating a validated repair PR into its target branch. It precedes the integrate stage.
