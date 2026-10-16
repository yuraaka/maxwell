# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `yuraaka/maxwell`.
Use the `gh` CLI for tracker operations.

## Operations

- Create: `gh issue create --repo yuraaka/maxwell --title "..." --body-file <file>`.
- Read: `gh issue view <number> --repo yuraaka/maxwell --json number,title,body,labels,comments`.
- List: `gh issue list --repo yuraaka/maxwell --state open --json number,title,body,labels`, with appropriate filters.
- Comment: `gh issue comment <number> --repo yuraaka/maxwell --body-file <file>`.
- Label: `gh issue edit <number> --repo yuraaka/maxwell --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> --repo yuraaka/maxwell`.

For multiline issue bodies and comments, write the exact text to a temporary file and use `--body-file`.

When a skill says “publish to the issue tracker,” create a GitHub issue.
When it says “fetch the relevant ticket,” read the GitHub issue.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding

A map is an issue labelled `wayfinder:map`.
Child tickets use `wayfinder:<type>`, where type is research, prototype, grilling, or task.

Use GitHub sub-issues to connect children to a map.
If unavailable, add children to a task list in the map and put `Part of #<parent>` in each child.

Use native GitHub issue dependencies for blocking relationships.
Dependency operations require numeric issue database IDs, rather than issue numbers.
If unavailable, put `Blocked by: #<number>, ...` at the top of the child body.

The ready frontier contains open, unassigned children with no open blockers.
Select in map order and claim with `gh issue edit <number> --add-assignee @me`.
Resolve by commenting with the answer, closing the ticket, and recording a summary and link in the map’s decisions.
