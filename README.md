# Maxwell

Maxwell is a local repository maintenance daemon. Milestone 1 runs a discovery agent
against committed source snapshots, creates deduplicated **suspected** findings as
GitHub issues, and reports spending. It does not reproduce, fix, or merge changes yet.

## Install

Requires macOS or Linux, Python 3.12+, Git, and a current Codex CLI supporting
`exec --json --output-schema --ignore-user-config --ignore-rules --ephemeral`.
The initial runtime adapter uses Codex with an OpenAI API key. Subscription login,
Claude, and Gemini adapters are not implemented in this milestone.

```bash
uv tool install --editable .
maxwell --help
maxwell init
```

For development, use `uv sync --group dev` and `uv run maxwell ...` instead.
All application configuration and runtime data live under `~/.maxwell`.
`--home /some/private/directory` overrides that root; put it before the command.
The installed executable follows your Python tool manager's usual location.

## Configure

Edit `~/.maxwell/config.yaml`, using [config.example.yaml](config.example.yaml) as a
reference. Supply:

- `github.token`, or tokens under `github.accounts`: fine-grained GitHub tokens with
  repository metadata read and Issues read/write access. Remote repositories also
  need Contents read access for fetching source. No code-write permission is needed.
- `github.allowed_users`: exact GitHub logins permitted to issue control comments.
  This setting applies to the default account. An empty list disables comment commands
  but does not disable scans, issue publication, or local CLI commands.
- `provider.api_key` and `provider.model`: an API key and the actual model identifier.
- The current input and output prices in USD per million tokens. Cached input uses
  its separate price if supplied, otherwise the regular input price conservatively.

Credentials may be literal values in this file. Configuration files must have mode
`0600`; runtime directories use `0700`. `init` never overwrites an existing config.
Restart after configuration changes. No credentials are required for the test suite.

For different GitHub accounts or repository-specific access keys, define named profiles:

```yaml
github:
  accounts:
    personal:
      token: "<personal-token>"
      allowed_users: [your-personal-login]
    work:
      token: "<work-token>"
      allowed_users: [your-work-login]
```

Select one with `repo add --account work`. Profile names are your aliases; a profile
can be shared by several repositories or hold a token scoped to just one repository.
The selected token is used for both GitHub API operations and remote Git fetching.
Named profiles have their own `allowed_users` list and never inherit the default
account's command permissions. An omitted list behaves as `[]`.

Without `--account`, the `default` profile uses the existing `github.token` and
`github.allowed_users`. Those fields can be left empty when all repositories select
named profiles. Unknown profile names are errors; Maxwell never silently falls back
to another account. Tokens stay in configuration, not CLI arguments, database rows,
remote URLs, or Git configuration. HTTPS authentication is supplied only to the fetch
process; global Git credentials and interactive authentication are disabled for it.

The model configuration still contains only the discovery profile. Later stages
will add model routing and tier policies without changing the runtime root.

## Run against a repository

`maxwell start` can run from any directory. The daemon manages the shared registry
under `~/.maxwell`; it is not attached to the shell's current repository. Register
repositories before or after starting it. A running daemon notices new registrations
automatically; GitHub synchronization follows its configured interval. Configure
profiles before startup, or restart after adding/editing profiles in the YAML file.

```bash
maxwell start
maxwell repo add owner/repository --account personal
maxwell repo add https://github.com/company/backend --account work
maxwell repo add /path/to/checkout --github company/other-project --account work --ref main
maxwell repo list
maxwell status
maxwell scan owner/repository --priority P1
maxwell pause owner/repository
maxwell resume owner/repository
maxwell logs --follow
maxwell stop
```

No existing checkout is needed. For `owner/repository` or a GitHub URL, registration
checks access using the selected profile and chooses GitHub's default branch unless
`--ref` is provided. The daemon fetches that branch into its own bare repository at
`~/.maxwell/repos/<repository-id>/mirror.git` before each scan, then creates a detached
worktree for the pinned revision. GitHub HTTPS and `git@github.com:owner/repo.git`
input URLs are accepted; remote transport uses HTTPS with the profile's token.

For an existing local path, Maxwell infers the GitHub identity from a GitHub `origin`
when `--github` is omitted. The default ref is local `HEAD`; an explicit branch such
as `main` is usually better. This mode scans the committed revision available locally.
It does not fetch the checkout's upstream, include uncommitted edits, or modify that
working tree. Keep the local ref up to date yourself, or register the GitHub URL to
let Maxwell fetch updates. Both modes currently require trusted repository content.

Change an existing registration's selected profile with:

```bash
maxwell repo set-account company/backend --account work
```

The profile must already be configured. New work and subsequent synchronization use
the selection. Old registrations automatically migrate to local mode and the default
account; their repository identity, history, and data remain intact.

Starting the daemon immediately schedules discovery for unpaused repositories and
can spend money and publish GitHub issues. Scheduled scans skip a previously scanned
revision; `scan` requests a fresh inspection even when the revision is unchanged.
Each scan currently inspects a whole pinned revision rather than only a diff.

`scan`, `pause`, and `resume` target all registered repositories when the repository
argument is omitted. They require a running daemon. Pause stops new work; active
scans may finish and publish. Status works both online and offline and prints JSON.
An explicit scan request coalesces with an already queued/running scan for that repo.

One daemon and one active discovery worker run per Maxwell home. P0 is dispatched
first; other queued scans use priority with aging. Existing active scans are not
preempted. Candidate findings carry P0–P3 labels for later agent stages.

Use `maxwell start --foreground` under a service manager. Ordinary `start` launches
that same process in the background and waits for a health handshake. M1 does not
install launchd/systemd services or restart automatically after reboot. A file lock
enforces singleton execution, and an owner-only Unix socket handles commands.
`stop` requests graceful shutdown; it never blindly signals a PID from a stale file.
Git operations and in-flight HTTP requests also have bounded timeouts. Restart a
running daemon after upgrading Maxwell so it loads the updated code and configuration.

## GitHub interaction

Maxwell creates one **Maxwell status** issue per repository and posts a summary for
every completed, failed, cancelled, interrupted, or skipped scan. You can pin this
issue in GitHub. It contains discovery spending, recent scans, and hunter assessments.

Each finding includes its scanned SHA, location, source evidence, expected behavior,
confidence, priority, and allocated discovery cost. Findings are explicitly suspected;
an LLM's confidence is not independent verification.

Authorized users can comment on a finding:

```text
/maxwell confirm
/maxwell reject explanation
/maxwell priority P1
```

Confirmation records a human assessment and leaves the issue open for future stages;
it does not claim a reproduced regression test. Rejection closes the issue. Closing an
issue directly removes it from the review queue but does not imply a quality assessment.
Confirmed/rejected findings receive corresponding state labels. Comment commands are
processed once per comment ID; edit-free new comments are required for new decisions.

On the status issue:

```text
/maxwell scan
/maxwell pause
/maxwell resume
```

Only explicitly allowlisted users can make these decisions. Instructions embedded in
source files or arbitrary issue prose do not control the daemon.

## Spending and recovery

Each scan records its configured execution profile, policy snapshot, pinned revision,
usage, result, timestamps, and artifacts. Cost calculations use integer microdollars
and the price configuration captured for that execution. Cached input is not charged
twice. All reported dollar amounts are **estimates**, not billing receipts.

The controller reserves the configured amount before launching a scan. Daily budget
windows use UTC. An execution with unknown spending retains its reservation, including
across days; known spending settles the reservation. Unknown usage is not zero usage.
The Codex CLI does not provide a controller-enforced hard dollar ceiling here: a call
may cost more than its reservation before usage is available. After an overrun, new
dispatch stops when the remaining budget cannot cover another reservation. Use a
provider billing limit for a hard account-wide cap. Unknown reservations currently
require operator investigation; there is no automatic financial settlement command.

Scan costs are allocated equally among newly admitted findings after deduplication.
Rounding is reconciled exactly. Scans with no admitted findings remain repository
overhead. The status report sums the original ledger, not allocated costs again.
Other stages are shown as **not run**, not executed for zero dollars.

The SQLite database stores jobs, events, publication intents, and assessments. On a
restart, interrupted jobs are marked as such and retain their reservations. A surviving
provider process is stopped only when its process group and unique artifact path match
the recorded attempt. Interrupted scans are not silently rerun; request another scan
explicitly. Worktrees from interrupted attempts may remain for investigation.

Findings have stable identities based on repository, kind, path, symbol, and normalized
title. Line movement alone does not create another finding. This is deterministic
deduplication, not semantic equivalence detection; differently worded reports can still
require human rejection. Previously recorded findings remain suppressed after closure.

Issue bodies contain unique machine markers. Before creating an issue, Maxwell checks
for an existing marker. If creation has an ambiguous outcome, Maxwell records
`uncertain` and reconciles it rather than blindly retrying. If you have independently
verified that a finding was not published, authorize another attempt on the status issue:

```text
/maxwell retry-publication <full-finding-id-from-maxwell-status>
```

If creation of the status issue itself was ambiguous and no such issue exists, create
a status issue containing `<!-- maxwell:dashboard:v1 -->`; Maxwell will adopt it.
Comment publication also searches for its marker before retrying. GitHub writes are
reconciled and idempotent where possible; exactly-once external delivery is not promised.
Transient publication failures remain visible in `status` and are retried at the
configured synchronization interval.

## Ratings and Entropy

Hunter ratings begin as **unrated**. Explicit authorized human confirmations/rejections
provide independent assessments. The provisional quality estimate per execution profile
is `100 × (confirmations + 2) / (assessments + 4)` with sample count and a provisional
confidence label. Scans that discover nothing are not automatically quality failures.
Model routing remains configured rather than adaptive.

**Entropy is unavailable in M1**: there is no complete, reproducible baseline for the
agreed defect, complexity, duplication, dependency, and test-health measurements yet.
Suspected findings are not substituted for confirmed defects. Later stages will provide
the measurement adapters and calibrated scoring policy. No fabricated score is shown.

## Execution boundary

Worktrees provide isolated files, not a hostile-code sandbox. M1 targets trusted local
repositories and uses Codex's read-only sandbox and non-interactive approval policy.
Discovery is instructed not to run repository builds, tests, or installation scripts;
that semantic restriction is not an operating-system guarantee. Output is validated,
paths/line numbers must exist in the snapshot, and modified worktrees are rejected.

The provider does not inherit Maxwell's GitHub token through its environment. It uses
a managed Codex home under `~/.maxwell/runtimes/`. Exact configured secrets are redacted
from collected output and reports. This is not a defense against encoded secrets or
arbitrary filesystem reads; provider credentials are available to the CLI process.
Do not use this milestone on hostile repositories. Container/VM isolation and stronger
credential separation are prerequisites before adding execution of repository code.

## Develop and validate

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

Tests use temporary Git repositories, a fake Codex executable, and a local GitHub HTTP
server. They verify the full issue/report flow, priority and budget admission, privacy,
duplicate suppression, stale results, singleton execution, background startup/shutdown,
hard-crash recovery, and ambiguous publication. No paid model calls or real issues are
created. Focused tests own edge cases; the end-to-end test exercises the integration.

## Next milestones

1. Discovery daemon and issue publication — this release.
2. Independent tester/reproducer consuming discovery issues.
3. Fixer consuming confirmed issues and producing patches.
4. Independent validator, integration checks, and GitHub-based merge approval.

The durable issue/artifact handoffs remain the boundary between agents. Add measured
Entropy, richer stage ratings, further provider adapters, and optional GitHub Pages
reporting as the necessary evidence and capabilities become available.
