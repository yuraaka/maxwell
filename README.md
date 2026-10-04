# Maxwell

Maxwell is a local repository maintenance daemon. Milestone 1 runs a discovery agent
against committed source snapshots, creates deduplicated **suspected** findings as
GitHub issues, and reports spending. It does not reproduce, fix, or merge changes yet.

## Install

Requires macOS or Linux, Python 3.12+, Git, and a current Codex CLI supporting
`exec --json --output-schema --ignore-user-config --ignore-rules --ephemeral`.
Discovery supports Codex and Claude Code through their existing native configuration
homes, including gateway configuration and native login. The legacy Codex API-key
configuration remains supported. Gemini is not implemented.

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
- `providers`: named Codex or Claude Code connections, each pointing to its existing
  configuration home. GitHub credential profiles are configured independently.
- Optional prices for each model, in USD per million tokens. Without prices, usage is
  reported as unpriced and admissions use `limits.daily_scan_limit` (default: 20).

Credentials may be literal values in this file. Configuration files must have mode
`0600`; runtime directories use `0700`. `init` never overwrites an existing config.
Restart after configuration changes. No credentials are required for the test suite.

Check configuration before starting the daemon:

```bash
maxwell config check
maxwell config check --offline
maxwell config check --json
```

The command checks YAML structure, field values, file permissions, required credentials,
model prices when supplied, Git, provider executables/homes, gateway environment
dependencies, and saved repository provider/model assignments.
By default it also authenticates each configured GitHub account, reports its login, and
checks access to registered repositories. It sends a small structured request through
each configured provider/model pair to verify authentication and model access. The
pairs include catalog models, provider defaults, and saved repository stage models;
duplicates are checked once. Claude providers without a selected model are checked
using the Claude CLI's default (including its native settings and gateway environment).
Codex providers need a model in their native settings, Maxwell config, catalog, or saved
stage assignment. Manual scans still require `--model`.

Provider checks use the native home/profile and gateway environment in an empty
temporary workspace, with a timeout of 30 seconds per pair (or the configured scan
timeout if shorter). Temporary check files are removed; the repository registry and
scan history are unchanged. Native credential refresh remains owned by the CLI.
These requests consume provider usage and are outside the daemon's scan budget.
`--offline` skips all GitHub and provider requests. The default
account may be empty when repositories use named profiles; named profiles need tokens.
Errors identify the settings to fix without printing credential values. The command
prints each result as soon as the check finishes, followed by a summary, and exits
with status 0 on success or 1 on failure. `--json` prints one complete JSON report
after all checks finish, without progress text. Terminal output uses colored status
labels and brief success lines; failures keep their diagnostics. Piped output and
JSON stay plain. Set `NO_COLOR` to disable terminal colors.

GitHub checks verify identity and repository read access; write permissions are not
tested.

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

Configure native provider homes, using the directories your CLIs already use:

```yaml
providers:
  codex-work:
    adapter: codex
    home: ~/.codex-work
  codex-second:
    adapter: codex
    home: ~/.codex-second
  claude-work:
    adapter: claude
    home: ~/.claude
```

`home` is the CLI configuration/state directory, rather than a repository directory.
Maxwell sets `CODEX_HOME` or `CLAUDE_CONFIG_DIR` for that process and reuses native
authentication. It does not copy credentials or change the global login. Keychain
credentials and external gateway helpers still need to work in the daemon environment.
Relative home and executable paths resolve against the Maxwell configuration directory.

For Codex, optional `profile: work` selects `HOME/work.config.toml`, overlaid on
`HOME/config.toml`. Maxwell uses the merged settings to resolve the default model and
gateway environment dependencies. It passes connection/model settings as explicit CLI
overrides with `--ignore-user-config`, while authentication continues to use the original
`CODEX_HOME`. Native MCP servers and other interactive integrations are excluded before
Codex parses configuration, so incompatible MCP transports cannot block scans or checks.
Static gateway headers and bearer tokens are passed in the child environment rather than
command-line arguments. Configuration and credential files stay in place. Native profiles
require Codex 0.134 or newer; legacy `[profiles.work]` and `profile = "work"` settings in the
native config must be migrated to separate profile files.

The native configuration supplies the endpoint, credential helper, and default model.
Codex gateway `env_key` and `env_http_headers` dependencies are forwarded automatically.
Claude's native `settings.json` environment is reused. For additional variables needed
by a gateway, proxy, or login helper, list their names under the provider's `env`, for
example `env: [COMPANY_GATEWAY_TOKEN]`. Their values must be available when starting
the daemon. Shell aliases are not executables; use an executable wrapper if needed.
Unrelated shell credentials are not inherited.

The model catalog is optional. Without it, explicit model IDs/aliases are passed to
the selected CLI. A catalog restricts selections and can attach model-specific prices:

```yaml
providers:
  codex-work:
    adapter: codex
    home: ~/.codex-work
    models:
      your-model-id:
        input_usd_per_mtok: 2
        cached_input_usd_per_mtok: 0.2
        output_usd_per_mtok: 8
```

These prices are illustrative; use your gateway's actual rates. Claude cache creation
can be priced with `cache_write_usd_per_mtok`; it otherwise uses the regular input rate.
Both input and output prices must be set together. A plain list such as
`models: [model-a, model-b]` restricts models without assigning prices.

Existing singleton `provider` configurations keep working as the provider named
`default`. Use either the old `provider` form or the new `providers` form.

## Run against a repository

`maxwell start` can run from any directory. The daemon manages the shared registry
under `~/.maxwell`; it is not attached to the shell's current repository. Register
repositories before or after starting it. A running daemon notices new registrations
automatically; GitHub synchronization follows its configured interval. Configure
profiles before startup, or restart after adding/editing profiles in the YAML file.

```bash
maxwell start
maxwell repo add owner/repository --account personal --provider codex-work --model model-a
maxwell repo add https://github.com/company/backend --account work --provider codex-work --model model-a
maxwell repo add company/service --directory /path/to/service --ref develop --account work --provider codex-work --model model-a
maxwell repo add /path/to/checkout --github company/other-project --account work --ref main --provider codex-work --model model-a
maxwell repo list
maxwell status
maxwell scan owner/repository --model model-a --priority P1
maxwell pause owner/repository
maxwell resume owner/repository
maxwell logs --follow
maxwell stop
```

No existing checkout is needed. For `owner/repository` or a GitHub URL, registration
checks access using the selected profile. The base branch defaults to `main`; use
`--ref develop` (for example) to choose another branch. The daemon fetches that branch
into its own bare repository at
`~/.maxwell/repos/<repository-id>/mirror.git` before each scan, then creates a detached
worktree for the pinned revision. GitHub HTTPS and `git@github.com:owner/repo.git`
input URLs are accepted; remote transport uses HTTPS with the profile's token.

To maintain an existing checkout, pass `--directory /path/to/checkout`. The directory
must be the checkout root and its `origin` must identify the registered GitHub repository.
Before each scan, Maxwell fetches the configured branch using the selected account,
switches the checkout to it (creating the local branch if needed), and applies a
fast-forward-only update. Dirty checkouts and diverged branches fail the scan without
resetting or stashing local work. Local commits ahead of the remote are preserved and
included in the scan. Discovery still runs in a separate, detached worktree.

The positional local-path form remains available for scanning locally committed source
without maintaining the checkout. It infers the GitHub identity from a GitHub `origin`
when `--github` is omitted, defaults to `main`, and accepts `--ref HEAD` for the current
commit. It does not fetch upstream or include uncommitted edits. Existing registrations
keep their ref and source behavior until changed. All modes require trusted content.

Change any of an existing registration's settings with:

```bash
maxwell repo change company/backend --directory /path/to/backend --ref develop --account work
maxwell repo change company/backend --ref main
maxwell repo change company/backend --no-directory
```

Omitted options retain their current values. `--no-directory` stops maintaining the
checkout and resumes fetching directly into Maxwell's mirror. Profiles must already be
configured. Settings apply to subsequent scans and synchronization; an active scan can
finish with its previous settings. The daemon notices changes automatically, and the
repository identity and history remain intact. `repo set-account` is retained for
compatibility; use `repo change --account` for new commands.

Each repository has a saved provider/model pair for each configured stage:

```bash
maxwell repo change owner/repository --stage discovery --provider codex-work --model model-a
maxwell repo change owner/repository --stage review --provider claude-work --model model-b
maxwell scan owner/repository --model model-c
maxwell scan owner/repository --provider codex-second --model model-c
```

With multiple providers, registration requires `--provider`. With one, it is selected
and saved automatically. Registration accepts the native default model when no model
catalog is given; otherwise use `--model` or configure a single catalog entry. Saved
assignments remain stable when more providers are added. `repo list` shows stage routes.
Legacy registrations acquire their discovery route when it can be resolved uniquely.

Every manual scan requires `--model`, including GitHub control comments. An omitted
`--provider` uses the saved discovery provider. Overrides affect that run only; scheduled
discovery uses the saved pair. All selections are validated before queuing multi-repo
requests, and queued runs retain their provider, model, source settings, limits, and
prices. Authentication remains a live reference so native token refresh can work.

Review, reproduction, fix, and validation routes may be stored now; their execution
workflows are later milestones. `scan` executes discovery only.

Starting the daemon immediately schedules discovery for unpaused repositories and
can consume provider usage and publish GitHub issues. Scheduled scans skip a previously
scanned revision with the same execution settings; `scan` requests a fresh inspection
even when the revision is unchanged.
Each scan currently inspects a whole pinned revision rather than only a diff.

`scan`, `pause`, and `resume` target all registered repositories when the repository
argument is omitted. They require a running daemon. Pause stops new work; active
scans may finish and publish. Status works both online and offline and shows readable
repository settings, recent scans, spending, and publication problems. Commands use
human-readable output by default; pass `--json` before or after the command for the
structured format, for example `maxwell status --json` or `maxwell --json repo list`.
`logs --json` returns a JSON log list; `logs --follow --json` streams one JSON object
per log line. Runtime errors use JSON on stderr when `--json` is selected.
An explicit scan request coalesces with already queued/running work only when its
execution settings match. Requests using different providers or models remain separate.

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
/maxwell scan --model model-a
/maxwell scan --provider codex-second --model model-a
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

For priced models, the controller reserves the configured amount before launching a scan. Daily budget
windows use UTC. An execution with unknown spending retains its reservation, including
across days; known spending settles the reservation. Unknown usage is not zero usage.
The Codex CLI does not provide a controller-enforced hard dollar ceiling here: a call
may cost more than its reservation before usage is available. After an overrun, new
dispatch stops when the remaining budget cannot cover another reservation. Use a
provider billing limit for a hard account-wide cap. Unknown reservations currently
require operator investigation; there is no automatic financial settlement command.

For unpriced models, tokens are recorded without claiming a dollar estimate.
These runs do not reserve dollars or retain unknown-dollar holds. The global
`limits.daily_scan_limit` caps unpriced model executions per UTC day, including failed
or interrupted calls. Preparation failures and skipped revisions consume no count.
The same configured scan timeout applies. Known-priced models continue using the dollar
budget; both modes share the single discovery worker and human review queue limit.

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

The provider does not inherit Maxwell's GitHub token through its environment. Native
providers use the selected existing home; legacy API-key Codex uses a managed home under
`~/.maxwell/runtimes/`. Discovery disables native hooks and external tools; Claude is
restricted to Read/Grep/Glob and Codex uses its read-only sandbox. Exact configured and
resolved native credential values are redacted
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
