# Maxwell

Maxwell runs **hunt → validate → audit → fix → review → integrate** from your terminal.
It measures repository entropy, publishes findings as GitHub issues, independently assesses
them, and opens fix PRs with host-executed check evidence. PR review checks the patch before
merge; integrate verifies the merged result before closing the finding. Automatic merging
requires the configured opt-in policy.

## Install

Requires macOS or Linux, Python 3.12+, Git, and a current Codex CLI supporting
`exec --json --output-schema --ignore-user-config --ignore-rules --ephemeral`.
hunt supports Codex and Claude Code through their existing native configuration
homes, including gateway configuration and native login. The legacy Codex API-key
configuration remains supported. Gemini is not implemented.

```bash
uv tool install --editable .
maxwell --help
maxwell config init
```

For development, use `uv sync --group dev` and `uv run maxwell ...` instead.
All application configuration and runtime data live under `~/.maxwell`.
`--home /some/private/directory` overrides that root; put it before the command.
The installed executable follows your Python tool manager's usual location.

## Bash completion

Enable completion in the current Bash session:

```bash
eval "$(maxwell completion bash)"
```

Add that line to `~/.bashrc` to enable it in future interactive sessions. Command
substitution reads the complete script before evaluating it and also works with Bash 3.2.
If upgrading
an existing editable installation, refresh its dependencies first with
`uv tool install --editable . --force` from the Maxwell checkout.

Completion covers commands, subcommands, options, stages, priorities, registered
repositories, GitHub account names, and local provider aliases. After selecting
`--provider`, `--model` suggestions follow that provider's configured catalog and
locally cached models. `--effort` suggestions follow the selected provider/model's
configured or cached capabilities, including `default` to use the provider default.
Without a selected provider, suggestions combine the configured connections.

Completion honors `--home` and only reads local configuration and SQLite; pressing
Tab never contacts GitHub or a model provider. Configured models work before the first
apply; run `maxwell config apply` to refresh discovered model/effort suggestions.
Missing or invalid configuration still allows command, option, and stage completion.

## Configure

Edit `~/.maxwell/config.yaml`, using [config.example.yaml](config.example.yaml) as a
reference. Supply:

- `github.token`, or tokens under `github.accounts`: fine-grained GitHub tokens with
  repository metadata read and Issues read/write access. Remote repositories also
  need Contents read access for fetching source. No code-write permission is needed.
- Optional `github.allowed_users`: authors allowed to submit structured GitHub records.
  Manual runs do not process comment commands.
- `providers`: named Codex or Claude Code connections, each pointing to its existing
  configuration home. GitHub credential profiles are configured independently.
- Optional prices for each model, in USD per million tokens. Without configured prices,
  admissions use `limits.daily_scan_limit` (default: 20); supported CLIs/models can
  still display API-equivalent cost estimates.

Credentials may be literal values in this file. Configuration files must have mode
`0600`; runtime directories use `0700`. `config init` creates private configuration,
runtime directories, and the state database; it never overwrites an existing config.
After editing settings, run `config apply`. No credentials are required for the test suite.

Apply configuration before running discovery:

```bash
maxwell config apply
maxwell config apply --json
```

By default, apply reuses saved model catalogs and access results, preserving their
original check timestamps. Providers without saved data are discovered and every
model is validated. Changes to limits or other runtime settings therefore apply
without repeating model requests for existing providers.

To rediscover and review all providers, including those with saved data:

```bash
maxwell config apply --force
```

Local validation and GitHub access checks run on every apply. Use `--force` after
changing provider connections or model settings. `--skip-model-checks` remains
available to require saved data for every provider; it cannot be combined with `--force`.

The command rereads configuration and checks YAML structure, field values, file
permissions, required credentials,
model prices when supplied, Git, provider executables/homes, and gateway environment
dependencies.
It authenticates each configured GitHub account, reports its login, and checks access
to registered repositories. For providers without saved data, or with `--force`, it
discovers models and efforts, then sends a small structured request through
**every discovered model**, including models
outside Maxwell's configured selection catalog. Configured model defaults are also
checked; duplicate model/effort selections are checked once. Models are checked at the
native/configured default effort. It does not probe every advertised effort level.
Claude providers without a selected model are checked
using the Claude CLI's default (including its native settings and gateway environment).
Repository registration does not select a provider or model. `maxwell scan` selects agents automatically.

Provider checks use the native home/profile and gateway environment in an empty
temporary workspace, with a timeout of 30 seconds per pair (or the configured scan
timeout if shorter). Temporary check files are removed; the repository registry and
scan history are unchanged. Native credential refresh remains owned by the CLI.
These requests consume provider usage and are outside the scan budget.
Model metadata, probe outcomes, and the apply timestamp are stored in SQLite.
Models with unavailable access or no known effort levels/default are excluded from the
stored catalog. Failed checks remain in the apply report; other accounts are still checked.
Applying replaces the previous catalog, including removing
entries for deleted providers and models. The default account may be empty when
repositories use named profiles; named profiles need tokens.
Errors identify the settings to fix without printing credential values. The command
prints each result as soon as the check finishes, followed by a summary, and exits
with status 0 on success or 1 on failure. `--json` prints one complete JSON report
after all checks finish, without progress text. Terminal output uses colored status
labels and brief success lines; failures keep their diagnostics. Piped output and
JSON stay plain. Set `NO_COLOR` to disable terminal colors.
Apply with errors still saves eligible model metadata and exits with status 1.
Invalid YAML or configuration changes during apply preserve the previous stored catalog.
`config check` and the old
top-level `init` command have been removed. `maxwell --help` displays a command tree;
subcommand help shows the detailed options.

GitHub checks verify identity and repository read access. `config apply` also reports
archived/disabled repositories and disabled Issues with instructions. When automatic
merging is enabled, it checks reported repository write access and whether the configured
merge method is enabled. These checks reuse repository metadata; they do not create a PR
or require access to branch-protection settings. Missing permission metadata is reported
as unverified. Token-specific write grants and PR-specific rules are enforced at runtime.

During one command or daemon lifetime, GitHub reads reuse a bounded, account-specific
ETag cache. Every reuse is revalidated with GitHub; authenticated `304 Not Modified`
responses do not consume the primary API rate limit. Changed data and access errors
are still fetched. Sync reuses issue listings to refresh finding states, skips comment
requests when GitHub reports zero comments, and creates only missing Maxwell labels.
Rate-limit failures report remaining requests and the UTC reset time when supplied.
Quota exhaustion retains the active job and waits until the reset or `Retry-After`
time, then retries the same request. Status updates identify the account, HTTP status,
remaining quota, retry time, and seconds left. Secondary limits without timing headers
use increasing waits, capped at 15 minutes per wait. Quota waits do not consume the
agent's execution timeout; Ctrl-C can still stop the command. Permission errors fail
immediately, and ambiguous write failures still require reconciliation.
The cache is in memory and does not survive separate commands. GitHub quotas are
shared with other tools using the same account, so these savings cannot guarantee
that its remaining quota stays above zero.

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
credentials and external gateway helpers still need to work in the scan environment.
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
example `env: [COMPANY_GATEWAY_TOKEN]`. Their values must be available when running scans. Shell aliases are not executables; use an executable wrapper if needed.
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
        efforts: [low, medium, high]
        effort: medium
        input_usd_per_mtok: 2
        cached_input_usd_per_mtok: 0.2
        output_usd_per_mtok: 8
```

These prices are illustrative; use your gateway's actual rates. Claude cache creation
can be priced with `cache_write_usd_per_mtok`; it otherwise uses the regular input rate.
Both input and output prices must be set together. A plain list such as
`models: [model-a, model-b]` restricts models without assigning prices.
`effort` sets a provider or model default. An optional `efforts` list declares supported
levels and rejects other selections before queueing. Leave `effort` unset to retain
the native CLI's default; leave `efforts` unset when capabilities are unknown.

Read the models and reasoning efforts stored by the most recent apply:

```bash
maxwell models list
maxwell models list --provider codex-work
maxwell models list --provider claude-work --json
```

During `config apply`, each provider/account is queried separately using its configured
native home, profile,
and gateway environment. Codex uses the app-server `model/list` protocol, including
hidden models and all result pages. Claude uses the same initialization metadata as
the Agent SDK's `supportedModels()`, preserving aliases and resolved model IDs.
Discovery reads supported efforts and default efforts when supplied by the CLI.
An empty effort list means effort selection is unsupported. Native configured model
defaults and Maxwell catalog entries supplement discovery, and configured effort
metadata takes precedence. Models without known effort levels or a default effort
are excluded from the stored catalog, as are models with unavailable access.

`models list` reads only the database and makes no provider or GitHub requests. It shows
the last apply timestamp and one table of providers, model names, and supported efforts.
`--json` also includes default efforts, sources, selection restrictions, access status,
and individual probe results for stored models. Edited configuration takes effect in this list only after
the next apply; it can still be read when config.yaml is missing or invalid.
Before the first apply, the command tells you to run `maxwell config apply`.
A CLI or company gateway may return an incomplete catalog, so native configured
defaults and Maxwell catalog entries supplement discovery. Native CLIs still own
their credential refresh and caches. Each metadata request and each model probe has
a timeout of 30 seconds (or the configured scan timeout if shorter).

Existing singleton `provider` configurations keep working as the provider named
`default`. Use either the old `provider` form or the new `providers` form.

## Run against a repository

Register repositories and run scans from any directory. Maxwell uses the shared
registry under `~/.maxwell`; it is not attached to your shell's current repository.
Each `scan` runs in the foreground, publishes findings and a summary to GitHub,
saves a local report, and exits. No daemon is required.

```bash
maxwell repo add owner/repository --account personal
maxwell repo add https://github.com/company/backend --account work
maxwell repo add company/service --directory /path/to/service --ref develop --account work
maxwell repo add /path/to/checkout --github company/other-project --account work --ref main
maxwell repo list
maxwell scan owner/repository --provider codex-work --model model-a
```

No existing checkout is needed. For `owner/repository` or a GitHub URL, registration
checks access using the selected profile. The base branch defaults to `main`; use
`--ref develop` (for example) to choose another branch. Maxwell fetches that branch
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
included in the scan. hunt still runs in a separate, detached worktree.

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
configured. Settings apply to subsequent scans; an active scan retains its previous
settings. Repository identity and history remain intact. Change GitHub profiles with
`repo change --account`; there is no separate `repo set-account` command.

Repositories are independent of agent connections. Registration and repository changes
store source, branch, checkout directory, and GitHub account settings. Provider, model,
and effort are selected for each scan:

```bash
maxwell scan owner/repository --provider codex-work --model model-a
maxwell scan owner/repository --provider claude-work --model model-b --effort high
```

Stage names are **hunt → validate → audit → fix → review → integrate** across
CLI options, pool configuration, progress output, and GitHub reports. Agent names follow
the stage, such as **fix agent**. **measure** establishes the entropy baseline before
a full scan; it is a separate command, not an agent stage or finding pool. Merge is an action
after review; integrate verifies the merged result. See [the glossary](GLOSSARY.md).
Full names wrap onto another progress row when the terminal is narrow.

With no stage or model arguments, `maxwell scan` runs measure first, then hunt → validate → audit → fix → review → integrate sequentially
for each registered repository. Run `maxwell config apply` after changing configuration
so automatic selection has current verified model metadata. It does not discover models
or probe provider access during scanning.

```bash
maxwell scan
maxwell scan owner/repository
maxwell scan --stage hunt
maxwell scan --stage hunt-review
maxwell scan --stage hunt-
```

`--provider` constrains automatic selection to one connection. `--model` pins the model
for every requested stage; with multiple configured providers, also pass `--provider`.
`--effort` overrides the configured model/provider default; `--effort default` uses that
default, then the native CLI default if neither is set. Supported efforts do not create
extra sampling candidates. Automatic choices are made independently for each execution.

Selection uses repository-specific current GitHub evidence. hunt agents use Thompson
sampling with a Beta(2,2) prior and supported/rejected outcomes: current audits take
precedence over finding validations, with no double counting. Unresolved audits do not fall back
to positive finding validations. validate agents use agreement/disagreement with current audits. These
are provisional source proxies, not a validated accuracy rating. Unrated agents start
with equal priors and still have exploration opportunities. audit agent selection is uniform
until separately adjudicated. fix agents use accepted/rejected independent repair validation,
with a neutral prior when unrated. review agents remain uniformly sampled and quality-unrated.
The selector is separate from orchestration
so future agent ratings can replace these evidence inputs.

Automatic finding validations prefer a different public model from the hunt agent; audits prefer a model
other than the hunt agent and referenced validate agents. If no distinct candidate exists, selection
continues and reports constrained independence. Explicit model overrides take precedence.
Duplicate private provider aliases do not multiply a public agent's sampling weight.
Selection method, observations, sampled value, and independence outcome are recorded with
the execution. Detailed selections use public identities, never private provider aliases.

Selections are validated before any multi-repository scan begins. Each execution keeps
its own provider, model, effort, source settings, limits, and prices. Provider aliases
select local connections; published agent identities use the adapter and model.
Declared supported efforts are checked locally, while unknown capabilities are validated
by the native CLI/gateway. Authentication remains a live reference so native token
refresh can work.

Repository-level provider/model/effort routes have been removed. Existing databases
migrate automatically, retaining repositories and historical execution snapshots.
`repo add` and `repo change` no longer accept `--provider`, `--model`, `--effort`, or
`--stage`. Stage selection belongs to `scan`; full scans include fix and subsequent PR review and integration.

Use `scan --stage hunt|validate|audit|fix|review|integrate` to execute one stage at a time.
Intervals are inclusive: `--stage hunt-review` runs hunt through review, and `--stage hunt-`
runs hunt through integrate. Explicit stage selections reuse published measurements; only
the default full scan measures first. `--issue`, `--limit`, and `--force` require a single stage.
`--skip` applies to the default full scan, including `--skip fix`.

Config files must explicitly set `version: 2`; version 1 and unversioned files are rejected.
There is no automatic migration or legacy config alias support. Rename old pool `review`
to `validate`, old pool `validate` to `review`, and `repair` to `fix` together.
Also rename the root `repair` section to `fix`, `limits.max_reviews_per_scan` to
`limits.max_validations_per_scan`, and `fix.auto_merge.required_validations` to
`fix.auto_merge.required_reviews`. Rename the root `measurement` section to `measure`.
Keep the values attached to their original responsibilities, then run `maxwell config apply`. The CLI still accepts `repair` as an alias for `fix`.
CLI `review` and `validate` now always use the new meanings. Historical GitHub record roles,
labels, branch names, and SQLite stage identifiers keep their original representation so
published evidence and execution history remain valid.
Existing scripts
that should only hunt must now specify `--stage hunt`:

```bash
maxwell scan owner/repo --stage hunt --provider codex-work --model your-hunt-model
maxwell scan owner/repo --stage validate --provider claude-work --model your-validate-model --effort high
maxwell scan owner/repo --stage audit --provider codex-work --model your-audit-model --effort high
```

Provider aliases select local connections; published identities use `codex` or `claude`.
### Drain existing findings or maintain the pipeline

```bash
maxwell scan owner/repo --stage validate- --drain
maxwell scan owner/repo --stage validate- --infinite
maxwell scan owner/repo --stage review-integrate --drain
maxwell scan owner/repo --infinite
```

`--stage` selects the work; `--drain` and `--infinite` control repetition. Without either
flag, scan makes one pass through the selected stages. Both repeat modes respect pool
capacities and prioritize downstream work within the selected interval.

`--drain` snapshots the existing open structured findings on GitHub, processes the selected
stages until no further progress is possible, reports remaining blockers, and exits.
If hunt is selected, it runs at most once per repository and adds its findings to the batch.
Other findings arriving later belong to the next batch. `--stage validate- --drain` replaces
`--repair-all`, which is no longer accepted.

`--infinite` keeps reconciling GitHub until Ctrl+C, admitting new eligible findings and
waiting when idle or blocked. It hunts only when hunt is selected and its pool has capacity.
Without `--stage`, both modes select the full pipeline; before hunting they measure each
new source revision. Explicit stage selections reuse published measurements.

Each repository gets one execution per scheduling round. Models are sampled using the
existing reputation policy, with independent review agent identities. `--provider` can
constrain the pool of models; a single pinned `--model` cannot be used with these modes.
`--issue`, `--limit`, `--force`, and `--skip` cannot be combined with them.
Auto-merge runs only when review is selected and `fix.auto_merge` allows it.

Configure the maximum number of issues waiting **after** each stage:

```yaml
version: 2
pipeline:
  pools:
    hunt: 10       # awaiting validate
    validate: 10   # awaiting audit
    audit: 10      # awaiting fix, or needing attention
    fix: 3         # PRs awaiting review or amendment
    review: 3      # accepted PRs awaiting merge
    integrate: 1   # merged fixes awaiting verification on main
  poll_seconds: 60
  retry_seconds: 300
```

Run `maxwell config apply` after editing the configuration, then start the scan.
A running pipeline uses the settings it started with.

Each pool first admits existing GitHub issues for its phase, up to its capacity,
including reserved publication slots. Remaining issues stay in a separate backlog.
For example, 33 reviewed issues with capacity 10 appear as `Rev 10/10` and a validate
backlog of 23. As audit consumes the pool, existing reviewed issues refill it before
more reviews run. Hunting likewise pauses while enough hunted issues already exist.
Blocked attempts yield their slots to other existing work. Zero disables admissions
to that pool, including from backlog. These are per-repository queue sizes, not
parallel-agent counts. Integration capacity also gates automatic merging during
ordinary scans and PR synchronization; those paths can add to the pipeline backlog.

Hunting still observes `limits.scan_interval_seconds` and `max_findings_per_scan`;
the available hunt slots can lower the publication count further. Infinite mode measures
the current source/tests before hunting a new revision. Batch modes process assessments
one at a time until blocked, instead of stopping at `max_validations_per_scan` or
`max_audits_per_scan`. Daily budget/scan limits still apply. Infinite mode waits for
admission to become available, with the UTC reset time shown; a finite batch returns
its waiting summary. GitHub quota waits retain their existing diagnostics.

Completed assessments and validation votes are reused. Inconclusive assessments,
blocked fixes, failing repair checks, and inconclusive validation stay visible for
attention rather than being repeatedly sent to agents. Operational failures use a
persistent cooldown, and new evidence can make work eligible again. A finite batch
attempts repair at most once per finding; a later run can amend rejected PRs.
The progress panel shows pool occupancy, current activity, and cumulative estimated
spending. The final batch summary links to the main status issue.

For automatic merges, enable `fix.auto_merge` below and satisfy any merge requirements
configured by the repository. Otherwise PRs wait for manual merge; the next
reconciliation picks up their integration verification. New automated repairs require
an actionable audit or human decision; previously prepared PRs can resume using their
original valid assessment inputs.

### repair a supported finding

```bash
maxwell scan owner/repo --stage fix
maxwell scan owner/repo --stage fix --issue 12
maxwell scan owner/repo --stage fix --provider codex-work --model your-repair-model
```

repair handles **one finding per invocation**. Without `--issue`, it selects the first
eligible finding from the impact-ranked queue, then supported actionable bugs by
severity and unranked maintenance findings. Conflicting, rejected, closed, and
nonactionable findings are skipped. Numerical impact estimates are optional. GitHub
is authoritative for selection, assessments, and prior prepared repairs.

Maxwell creates `maxwell/repair/<execution-id>` in its local mirror and retains the
worktree. The base is the finding's original pinned revision; newer main changes
may require later integration. Your maintained checkout stays unchanged. The fix agent
edits source and adds regression coverage; Maxwell runs the configured
`measure.tests.<owner/repo>.command` **once**, or detects the existing pytest
suite. The command is selected before editing. Configure a runner for other test
frameworks. A missing runner, failing checks, no edits, or missing bug regression
coverage does not produce a prepared repair.

Codex receives `workspace-write` only for repair, with network access disabled.
Claude Code receives Read/Grep/Glob/Edit/Write; shell execution remains disabled.
Maxwell owns Git staging and commits and rejects agent changes to Git refs, HEAD,
configuration, or the index. Checks run with a private HOME and a sanitized environment.
Source assessments keep their read-only permissions.

Successful repairs push a dedicated branch and open a **draft PR** linked to the finding.
The compact output and status dashboard link to that PR. The finding retains immutable
repair and PR revision records; it remains open until integration verification.
Automatic merging requires the opt-in policy below.

Failed checks retain a local draft, clearly marked `checks_failed`. Timeouts, cancellations,
and interruptions preserve diagnostics in `~/.maxwell/artifacts/<execution-id>/repair.json`.
Publication retries reconcile existing branches and PR markers without rerunning the fix agent.
A rejected PR is amended on the **same branch and PR**. `--force` revises an existing open
PR even without rejection. The remote push uses an exact lease and refuses to overwrite
unexpected changes. Amendment records preserve predecessor, parent revision, and lineage.
Merge conflicts, outdated branches, and failed required CI checks return the PR to the fix agent
automatically in batch and continuous pipelines. For conflicts and outdated branches, the
fix agent rebuilds the repair on the current target base, preserving unrelated base changes.
For failed required checks, it receives bounded check output tied to the current PR revision.
Reading this evidence needs token Checks and Commit statuses read access. The fix agent updates
the same PR with an exact lease before fresh independent validation. A validation that finds
conflicts applies `maxwell:validation:conflicted`. Pending checks, required human approvals,
and access restrictions wait with diagnostics. A fix agent reporting blocked or failed host checks
waits for changed inputs or an explicit `--force` retry; operational failures remain retryable.
Operational review failures remain review work; actionable source feedback and merge blockers
return the existing PR to fix.
`--limit` is rejected for repair.

### Independently review a repair PR

```bash
maxwell scan owner/repo --stage review
maxwell scan owner/repo --stage review --issue 12
maxwell scan owner/repo --stage review --provider codex-work --model your-review-model
```

Review handles **one PR per invocation**, with a bounded fix/review loop, fetching each
revision's exact head and target base.
It tests a fresh merge candidate against that base and independently inspects the patch.
Public worktree paths are never used to locate source. The review agent uses a **different
public adapter/model from the fix agent**; provider aliases and effort changes do not establish
independence. review agent quality remains unrated.

For bugs with a pytest profile, Maxwell runs the declared regression files on the merge
candidate and on the original finding revision with only those tests transplanted.
Acceptance requires passing repaired checks, an original-source call-phase failure, and
independent confirmation that it demonstrates the defect. Collection, setup, unrelated
failures, or unsupported replay runners leave reproduction unresolved. Passing tests or
lower entropy alone cannot establish correctness.

Immutable validation records are published **on the PR**. One updated verdict summary
shows local or GitHub CI evidence and measured component changes. Actionable patch comments become
inline GitHub `COMMENT` reviews, pinned to the inspected head. Shared-account validate agents
use comment reviews rather than GitHub approval/request-changes events. Feedback whose
line cannot be anchored stays in the summary.

Feedback headings identify the role: **hunt agent**, **validate agent**, **audit agent**, **fix agent**,
**review agent**, or **integrate agent**. GitHub still displays the configured account
as the author. Agents are asked for one-sentence summaries, short evidence, and no repeated
explanations. Checks, revision IDs, measurements, and execution metadata are expandable.

review agents put merge-blocking correctness/security/regression risks in `concerns` and
informational observations in `notes`. Notes do not prevent acceptance or auto-merge;
blocking concerns make new validations inconclusive unless the repair is explicitly rejected.
Existing concerns retain their blocking meaning. A fresh validation is required to reclassify
an old concern; Maxwell does not rewrite immutable evidence.

Accepted PRs become ready for validate; rejected or inconclusive PRs remain drafts. Labels
`maxwell:validation:pending|stale|accepted|rejected|inconclusive` show the current verdict.
Any head or base movement invalidates the previous verdict. Completed current validations
are skipped once the configured validation requirement is met; `--force` supersedes them.
Failed attempts and publication retries are reusable.
PR rejection does not reject the original finding.

Before/after measurements compare the PR base with its tested merge candidate. Total
reduction requires all five compatible factors; partial component observations remain
visible. These measurements do not replace the main repository baseline. fix agent reputation
uses independent accepted/rejected validation of the fix agent's exact revision; external edits,
conflicting verdicts, operational failures, and integration checks add no fix agent quality credit.

### Review and fix recovery loop

`maxwell scan owner/repo --stage review --force` follows actionable review feedback through
`review → fix → review` on the same finding and PR. Conflicts, failed CI, rejected source
reviews, and inconclusive reviews with blocking concerns or actionable comments return to
`fix`. The fixer receives all current structured review feedback and merge/check blockers.
Each amendment uses the fresh target base when the PR does not already contain it, preserving
unrelated target-branch changes. A new independent review checks the published amendment.

```yaml
review:
  max_fix_attempts: 3
```

The limit counts automatic fix attempts for a review invocation. Set it to `0` to disable
recovery for review-only commands; an explicit `--skip fix` also disables nested recovery.
`--model` pins the review model; recovery chooses a different verified model from the configured
pool. If no independent model is available, Maxwell stops with a diagnostic rather than using
the review model as its own fixer. Budget limits still apply to each execution.

The progress panel announces recovery and displays `fix` while it runs. The final output
marks an earlier failed review `[RECOVERED]` only after the new revision is accepted. All
executions, costs, and immutable GitHub records remain available. Failed fix checks, failed
publication, missing actionable evidence, and exhausted retries remain explicit failures;
transport/provider failures do not cause speculative source edits.

`--stage review --drain` and `--stage review --infinite` may also run dependency fixes for
existing repair PRs. They do not start unrelated new repairs. New review feedback permits
another bounded fix attempt; repeated unchanged inputs remain blocked by the execution ledger.
Batch output retains its historical failed-execution count even if a later attempt succeeds.

### Reuse GitHub CI during review

Designate the GitHub check names that run the repository's test suite:

```yaml
review:
  ci:
    owner/repo:
      checks: [tests] # Exact check-run names or commit-status contexts; include every matrix job.
      poll_seconds: 30
      timeout_seconds: 3600
      # app_id: 15368 # Optional publisher ID for check runs; excludes legacy commit statuses.
      # measurement_check: tests # Optional structured test measurements in this check's output.text.
```

Run `maxwell config apply`, then `maxwell scan owner/repo --stage review`.
Already completed reviews require `--force` to run again. Check names are explicit because
passing lint, formatting, or an unrelated workflow cannot establish test-suite coverage.
The GitHub token needs Checks read access and, unless `app_id` is set, Commit statuses read
access. A configured check that has not appeared yet is treated as pending; correct a misspelled name
or wait for CI to start. No branch-protection settings are required.

Maxwell reuses successful checks on a commit whose Git tree matches the reviewed merge
candidate: the PR head or GitHub's synthetic merge commit. Head/base changes invalidate the
review. Missing or running designated checks are polled with progress diagnostics, without
starting the review agent. API request time, including quota waits, does not consume the
CI waiting timeout. Failed CI returns the finding to `fix`, with check names, URLs,
and failure summaries. Skipped, neutral, and cancelled checks do not count as passing.
Duplicate check names from different publishers require `app_id` to disambiguate them.
GitHub access errors remain errors; they do not silently become passing evidence.

With reusable CI, review skips the local full suite and repeated local test measurements.
It still runs targeted regression coverage on the fixed and original source and asks an
independent agent to review the patch and evidence. CI is rechecked after the agent finishes
and before auto-merge. If CI changes during review, the result remains inconclusive and needs
a retry. Repositories without a `review.ci` entry use local tests, as do merge candidates
whose content is not covered by the available CI revisions. `fix`, `measure`, and `integrate`
retain their existing local checks.

For runtime and flakiness, a green check is insufficient. An optional `measurement_check`
(must also be in `checks`) can publish raw JSON in its Check Run `output.text`:

```json
{
  "version": 1,
  "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "environment_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "protocol": "full-pytest-suite-v1",
  "runs": [
    {"duration_seconds": 12.0, "outcomes": {"tests/test_app.py::test_empty": "passed"}},
    {"duration_seconds": 13.0, "outcomes": {"tests/test_app.py::test_empty": "passed"}}
  ]
}
```

The publisher must include every collected test and only complete measured runs, excluding
warmups. Supported outcomes are `passed`, `failed`, and `skipped`. Each of the 2–20 runs must
contain the same test IDs. `sha` must match the check's commit; use actual values rather than
the placeholders above. The environment fingerprint is a SHA-256 digest of runner platform,
runtime/tool versions, dependencies, and relevant test environment. Change `protocol` when
the suite selection or measurement procedure changes. Do not include the source revision in
the environment fingerprint, since equivalent environments must compare across revisions.

Maxwell calculates median duration against `measure.tests.<owner/repo>.runtime_budget_seconds`
(or the auto-detected runtime budget), and observed flaky tests divided by eligible tests.
Before/after comparisons require compatible measurement policies, protocols, run counts, and
environments. Publish the same report format for base-branch CI to supply the before evidence.
Missing, incomplete, or invalid reports leave the affected factors unknown, with a reason;
Maxwell does not rerun the whole suite to fill them. This input accepts Maxwell's JSON format,
not arbitrary JUnit files or workflow job durations. Existing CI publishers can construct the
report from their test results; Maxwell does not install or modify workflows automatically.

### Automatically merge verified repairs

Auto-merge is disabled by default. Enable it in `config.yaml`:

```yaml
fix:
  auto_merge:
    enabled: true
    required_reviews: 2
    merge_method: squash
```

Run `maxwell config apply` after changing the configuration. This policy applies to repair
PRs in registered repositories. `required_reviews` accepts 1–10 distinct public
adapter/model identities. Provider aliases and different efforts do not add votes.
Merge methods are `squash`, `merge`, and `rebase`.

Automatic validation selects additional independent models for the same PR until the
requirement is met. Earlier votes remain current. With an explicit `--model`, each
invocation runs that model once; select another model for the next vote. A current rejected
or inconclusive verdict blocks merging. Use `--force` to replace current validations, then
collect the independent votes again. Unresolved source concerns, failed host checks, and
missing original-defect regression evidence for bugs also block auto-merge.

Maxwell uses GitHub's mergeability result and merge API to respect the repository's
configured requirements, including branch protection and rulesets. CI, required checks,
administrator enforcement, and GitHub's **Allow auto-merge** setting are not prerequisites:
Maxwell sends an ordinary merge request after its own validation passes. Non-required
checks do not introduce extra Maxwell merge restrictions unless explicitly designated in
`review.ci`; checks used as review evidence must still pass before merge. Repositories without CI or
protection can merge normally. The token needs repository access with Contents and
Pull requests write permissions; administration-read permission is not required.
Maxwell does not change repository rules or request an administrative bypass.

Head and base must still match the validated revision at the final check. The merge
request pins the head; GitHub does not offer an expected-base parameter, so without
server-enforced up-to-date rules a concurrent base change is detected during independent
integration verification. Failed required checks and conflicts route back to repair. Pending
checks, reviews, merge queues, or permission failures leave the PR waiting, with GitHub's
reason and corrective instructions in its
single updated validation summary. Later GitHub syncs recheck eligibility without rerunning
completed review agents. After a lost response, Maxwell reconciles the PR's merged state and
does not resend an uncertain merge. If GitHub still reports it open, the uncertainty remains
visible rather than triggering a duplicate request.

An automatically merged PR still requires independent integration verification before
Maxwell closes its finding or updates the repository baseline.

### Verify a merged repair

After a human or automatic merge, run:

```bash
maxwell scan owner/repo --stage integrate --issue 12
```

Full `maxwell scan` runs also execute fix and process pending PR review and integration, one PR per
stage (with additional validation models when auto-merge is enabled).
`--skip review` and `--skip integrate` disable those stages. Generic
GitHub synchronization updates PR state and reconciles publications without launching models.
Skipped validation/integration stages report the specific PR blocker, including awaiting
merge and the current auto-merge reason, instead of a generic “No eligible findings.”

Integration independently checks the current maintained branch, verifies that the recorded
merge is present, replays the original defect, and remeasures that branch. Only an accepted
verification whose revision and assessment inputs are still current publishes a new baseline
and closes the finding with `maxwell:integration:verified`. The PR body deliberately uses
“repair for #N”, so GitHub's merge keywords cannot close the finding prematurely.
Diagnostics remain in `~/.maxwell/artifacts/<execution-id>/validation.json`.

### validate and audit

Each assessment uses a fresh agent execution with automatic or explicit model/provider/effort selection.
The validate agent receives the finding evidence, without hunt agent confidence or agent identity.
The audit agent also receives the current validate verdicts and evidence, without validate agent identity.
Automatic selection prefers different public models; constrained and explicit choices are reported.

validate selects open structured finding issues without a current validate agent assessment;
audit selects reviewed findings without an audit referencing their current reviews.
Both fetch records from GitHub and prioritize findings by P0–P3, then issue number.
Assessment counts per repository come from `limits.max_validations_per_scan` and
`limits.max_audits_per_scan`, both defaulting to 1. `--limit N` overrides the selected
stage's setting for that run. `--issue`, `--limit`, and `--force` require an explicit validate
or audit stage. A failed execution, admission refusal, or uncertain publication is reported
while other jobs and stages continue. Later stages use eligible findings and assessments
available on GitHub; unavailable inputs are reported as errors or no eligible findings.
The command exits unsuccessfully if any execution, preparation, or publication failed. Previously assessed
findings are skipped, and eligible backlog can be reviewed even when a hunt finds nothing new.
Both settings accept any positive integer:

```yaml
limits:
  max_validations_per_scan: 10
  max_audits_per_scan: 3
```

Each finding has a separate execution, token record, and assessment comment.
audit selection is explicit and deterministic; automatic audit sampling is not implemented.

Use `--issue N` with a repository to target one finding, including a closed issue.
Use `--force` to reassess and supersede a previous assessment while retaining its record.
Inconclusive assessments also require `--force` to retry. A new validate makes that finding
eligible for another audit. `--issue`, `--limit`, and `--force` apply only to validate/audit;
hunting uses `limits.max_findings_per_scan` for its publication limit.
When no findings are eligible, the command succeeds without starting an agent.

`limits.scan_timeout_seconds` bounds each agent execution and defaults to 600 seconds.
If Claude exits successfully without structured output, Maxwell makes one formatting-only
retry using its existing response, with repository tools disabled. The retry is capped at
60 seconds and the remaining execution timeout; its tokens belong to the same execution.
Both responses remain in redacted artifacts (`events.jsonl` and `format-retry/events.jsonl`).
If correction fails, Maxwell reports missing structured output explicitly.

For longer investigations, increase it in `config.yaml` and run `maxwell config apply`.
A timeout stops the agent and reports the provider, timeout, and setting in the CLI
and scan report; incomplete findings are not published.

validate and audit inspect the finding's original pinned SHA, even if the branch has moved.
They do not update maintained checkouts, execute tests, or reproduce failures. Verdicts are
`supported`, `rejected`, or `inconclusive`; actionability and priority are reported separately.
Supported means source evidence supports the suspected defect, not that it was reproduced.
These stages do not close issues or turn validate agent agreement into a reputation score.
Labels follow current GitHub assessments: `maxwell:state:reviewed` with
`maxwell:review:supported|rejected|inconclusive`, then `maxwell:state:audited` with
the corresponding `maxwell:audit:*` verdict. Conflicting current verdicts are inconclusive.
Superseded, edited, missing, unauthorized, and wrong-revision assessments do not drive labels;
an audit becomes stale when its referenced reviews change. Synchronization and repeat
validate/audit commands reconcile labels even when no agent is needed. Human state labels
and unrelated labels are preserved.
The human review queue limit applies only to hunting; daily budget/execution limits still
apply to every stage. Reports, terminal progress, and `--json` identify the selected stage.

`scan --stage hunt` requests a fresh inspection even when the revision is unchanged. Each hunt
inspects a whole pinned revision, with measurement hotspots and a diff to guide attention.
hunt asks the agent to
map components, sweep correctness, configuration, recovery, resource handling, security,
and maintenance defects, then validate interactions. It collects up to
`limits.max_candidates_per_scan` concrete candidates (default 100, any positive integer)
independently of `limits.max_findings_per_scan` (the new-finding publication
limit). This is a heuristic sweep, not a guarantee of exhaustive discovery.

Maxwell filters low-confidence candidates and duplicates, then selects new findings by
priority, confidence, issue kind, and stable identity. Existing findings do not consume
new-finding slots. The finding queue can further limit selection. Reports and `--json`
show collected, selected, and skipped counts with reasons; the terminal shows the skipped
count. A report flags when collection reaches the configured candidate ceiling. Skipped candidates
remain in local provider result artifacts but are not queued for later publication.
The provider result must still fit within 2 MiB; larger collection limits may also
require a longer scan timeout and more output tokens.

Omit the repository argument to scan all registered repositories sequentially. Manual runs ignore legacy
pause settings and leave unrelated queued jobs untouched. Budget, scan-count, and
human review queue limits still apply; blocked runs fail instead of waiting in a queue.
Failure messages identify the blocking limit, include queue or daily execution counts,
and name the corresponding configuration setting. The default finding queue holds six
unresolved findings. Close reviewed finding issues on GitHub or increase
`limits.human_queue_max` to admit more discovery runs.

Progress shows repository preparation, the pinned revision, agent startup, finding
validation, cleanup, publication, and completion. While waiting, Maxwell prints the
current stage and total elapsed time every 15 seconds, including when the agent is
silent. These updates report activity stages rather than percentage completion.
Output includes local
report paths and finding URLs. Pass `--json` before or after the command for one complete
structured result, for example `maxwell scan owner/repository --model model-a --json`
or `maxwell --json repo list`. Scan or publication failures exit with status 1.
Runtime errors use JSON on stderr when `--json` is selected.

A file lock permits one active run per Maxwell home. Ctrl+C or termination cancels the
provider process, records the interruption, and cleans up its worktree. Git operations
and HTTP requests also have bounded timeouts. There are no `start`, `stop`, `status`,
`logs`, `pause`, or `resume` CLI commands in the manual workflow.

## GitHub interaction

On each publication pass, Maxwell updates one **Maxwell status** issue per repository
with recorded scan results, hunt token usage, and hunt agent assessments. You can pin
this issue in GitHub. Cancelled or blocked runs are included in the next publication pass.

Each finding includes its scanned SHA, location, source evidence, expected behavior,
confidence, priority, and the originating execution ID. New findings start as suspected;
an LLM's confidence is not independent verification.

New finding issues and completed scan comments also include versioned machine-readable
records. GitHub is the shared record for published findings, execution summaries, and
assessments. Public agent identities contain the adapter (`codex` or `claude`), model,
role, and optional effort; local provider aliases and native profiles are not published
as agent identities. One scan summary on the history issue links its finding IDs and
records input, cached input, cache writes, and output tokens once per execution.
Cached input and cache writes are included in input; total tokens are input plus output.
Missing usage is unknown, not zero, and incomplete execution usage is shown explicitly.
Finding issues reference the execution rather than allocating token usage to findings.

SQLite retains operational scans, stage executions, budget reservations, and publication
intents. It also projects authorized GitHub records and preserves observed revisions.
Structured records are accepted from the authenticated GitHub account or its configured
`allowed_users`, and must match the repository and finding issue. Human assessments must
identify their actual GitHub author. Edited records are marked changed; missing records
are marked missing. Previous observed payloads remain available locally. GitHub does not
provide an immutable assessment ledger, and edits made between synchronizations may not
be observed.

Assessment records distinguish supported, rejected, and inconclusive verdicts from
actionability and priority. Corrections use new IDs and explicitly supersede previous
assessments. Execution status and GitHub issue closure remain separate from validity.
Assessment publication is append-only: conflicting records are not overwritten, and
ambiguous comment creation is reconciled before any further write.

These contracts and projections support the validate agent and audit agent stages. Structured
assessments determine validate/audit eligibility and provisional reputation counts;
they do not trigger repairs. Measurement records can describe pinned
factors, coverage, exclusions, and analyzer/policy versions. `maxwell measure` implements
the first Python structural adapter and repeated pytest measurements. Existing issue bodies and scan reports
without structured records remain compatible and are not retroactively assigned evidence.
validate and audit require a current structured finding record; legacy issues without one
are skipped in bulk runs and produce an explanatory error when targeted explicitly.

GitHub comment commands are not processed during manual runs. Closing an issue
directly removes it from the validate queue on the next manual scan, but
does not imply a quality assessment.

## Spending and recovery

Each scan records its configured execution profile, policy snapshot, pinned revision,
usage, result, timestamps, and artifacts. Cost calculations use integer microdollars
and the price configuration captured for that execution. Cached input is not charged
twice. Scan progress, local reports, findings, agent assessments, repair PRs and
validation feedback show USD API-equivalent estimates alongside tokens. These are
usage estimates, not invoices or incremental subscription charges. The progress
total updates when the CLI reports usage; some CLIs only report it at completion.
Each execution is counted once, even when its cost appears on several findings or
inline comments. The status dashboard aggregates executions, including failed calls.

Configured per-model rates take precedence. Without configured rates, Claude Code's
reported `total_cost_usd` is used; Maxwell does not guess what an alias such as
`opus[1m]` resolves to. Codex uses a bundled standard tariff snapshot verified on
2026-10-15 for `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`,
and `gpt-5.6-luna`, including
cache reads/writes and the premium for requests above 272K input tokens. When Codex
reports an aggregate above that threshold without request-level context sizes, Maxwell
shows a short-to-long-context cost range rather than assuming a tier. Source,
verification date and rates are saved with execution evidence. Use configured rates
for gateways, other models, or nonstandard service tiers; the bundled rates estimate
standard service only. Missing tariffs or usage are unknown; failed or timed-out
attempts retain partial reported costs. Historical evidence is never repriced.

For priced models, the controller reserves the configured amount before launching a scan. Daily budget
windows use UTC. An execution with unknown spending retains its reservation, including
across days; known spending settles the reservation. Unknown usage is not zero usage.
The Codex CLI does not provide a controller-enforced hard dollar ceiling here: a call
may cost more than its reservation before usage is available. After an overrun, new
dispatch stops when the remaining budget cannot cover another reservation. Use a
provider billing limit for a hard account-wide cap. Unknown reservations currently
require operator investigation; there is no automatic financial settlement command.

Models without configured prices remain unpriced for admission even when a public
API-equivalent estimate is available.
These runs do not reserve dollars or retain unknown-dollar holds. The global
`limits.daily_scan_limit` caps unpriced model executions per UTC day, including failed
or interrupted calls. Preparation failures and skipped revisions consume no count.
The same configured scan timeout applies. Known-priced models continue using the dollar
budget; both modes share the single active run and human review queue limit.

Internal legacy scan costs are allocated equally among newly admitted findings after deduplication.
Rounding is reconciled exactly. Scans with no admitted findings remain repository
overhead. Public reports sum recorded execution tokens without counting findings again.
Other stages are shown as **not run**.

The SQLite database stores jobs, events, publication intents, and assessments. On the
next manual run, interrupted jobs are marked as such and retain their reservations. A surviving
provider process is stopped only when its process group and unique artifact path match
the recorded attempt. Interrupted scans are not silently rerun; request another scan
explicitly. Worktrees from interrupted attempts may remain for investigation.

Before discovery, Maxwell fetches GitHub issues and refreshes its local record projection.
The agent receives the latest 100 open issues and authorized Maxwell findings, with open
issues first; the context states its coverage. All fetched valid Maxwell finding identities,
including closed findings and legacy issue markers, are checked for duplicates even in a
fresh local database. Ordinary open GitHub issues provide context but cannot guarantee
semantic deduplication. Pull requests are excluded. If GitHub cannot be read completely,
discovery fails before starting the agent rather than using stale SQLite context.
After refreshing GitHub records, hunt agents also receive the latest measurement matching the
current measurement policy, its earliest compatible baseline, and published source/test
hotspots. Measurement and reused-stage evidence carries its original revision, age, and
test environment fingerprint. Missing measurements stay explicitly unknown and do not
block bug hunting; old hotspots must be checked against the current source.

hunt agents receive a Git diff from the latest successful published hunt agent execution to the
current pinned commit, independently of provider/model routing. Failed executions and
validate/audit executions cannot establish that revision. First hunts, missing commits, and
rewritten history fall back to a whole-repository investigation with an explicit reason.
The prompt asks for both existing bugs and introduced bugs or regressions, including
effects on callers outside the diff. An unchanged revision still receives a fresh hunt.
`limits.max_hunt_diff_chars` (default 30000) and `limits.max_hunt_changed_files` (default 200)
bound the inline change context; truncation and total counts are explicit. hunt agents can
inspect remaining changes using read-only Git commands. The redacted context is saved
alongside each hunt's prompt as `hunt-context.json`. Hunts do not execute tests to refresh
measurement history.

The status report combines projected GitHub execution history with local
runs, deduplicating execution IDs so a fresh home retains published token totals.

Findings have stable identities based on repository, kind, path, symbol, and normalized
title. Line movement alone does not create another finding. This is deterministic
deduplication, not semantic equivalence detection; differently worded reports can still
require human rejection. Previously recorded findings remain suppressed after closure.

Issue bodies contain unique machine markers. Before creating an issue, Maxwell checks
for an existing marker. If creation has an ambiguous outcome, Maxwell records
`uncertain` and reconciles it rather than blindly retrying. If creation of the status issue itself was ambiguous and no such issue exists, create
a status issue containing `<!-- maxwell:dashboard:v1 -->`; Maxwell will adopt it.
The history issue uses the same reconciliation rule with `<!-- maxwell:history:v1 -->`.
Comment publication also searches for its marker before retrying. GitHub writes are
reconciled and idempotent where possible; exactly-once external delivery is not promised.
Transient GitHub reads retry up to three times, with one- and two-second backoffs.
After an ambiguous creation, Maxwell immediately checks the marker and adopts the
authorized matching issue or comment when present. If no match is visible, the write
remains uncertain and is not automatically reposted.
Publication failures are reported by the scan command and retained in SQLite.
The next manual scan of that repository retries or reconciles pending publications.

## Ratings and Entropy

The status issue shows **provisional agent counts**, without a numerical reputation score. Agents are
grouped by public adapter, model, effort, and role; provider aliases are never used.
hunt agent tables show source support from reviews and audits separately: supported,
rejected, unresolved, and unassessed. validate agent tables show agreement, disagreement,
and unresolved comparisons with the current audits. audit agent quality remains unrated
until independent validation. These counts describe source judgments and agreement,
not proven correctness or representative accuracy: audits are selectively chosen and
agents can share biases.

Inconclusive or conflicting verdicts remain unresolved. An absent audit makes a
validate agent comparison unresolved. Each finding/pinned revision/agent contributes at
most one current observation, including closed issues. Superseded, edited, missing,
unauthorized, wrong-revision, and stale-reference assessments do not contribute.
Closure and legacy human commands are not substituted for structured evidence.
Counts are reconstructed from authorized GitHub records in a fresh SQLite database.
Publication and validate/audit commands refresh the tables, including runs with no
eligible findings; these updates do not require another agent execution.

Investigation tokens are shown separately, once per execution, including failures
and historical reassessments. Cached tokens are included in input; unknown and
incomplete usage are explicit. Tokens do not alter quality counts. Missing agent
identity is shown as unattributed. Scans that discover nothing are not automatically
quality failures. Model routing remains configured rather than adaptive.

## Measuring maintenance entropy

```bash
maxwell measure owner/repo
maxwell measure owner/repo --skip tests
maxwell measure owner/repo --skip source --skip tests
maxwell measure owner/repo --revision FULL_COMMIT_SHA --json
```

`measure` requires a registered repository and its GitHub credentials, with no
provider or model selection. It analyzes a pinned committed snapshot without changing
your checkout. A full `maxwell scan` measures first, then runs hunt, validate, audit, fix, review, and integrate.
By default `measure` runs source analysis and tests in separate disposable snapshots.
When no test profile is configured, a detected pytest suite uses the repository's existing
`.venv` when available (registered checkout or matching current checkout), then Maxwell's Python
if it has pytest. It performs two measured runs, with no warmup and a 60-second runtime budget. Pytest must already be
installed. Set `measure.auto_test_runtime_budget_seconds` to change the scoring budget.
Each automatically detected test run has a 600-second timeout. For longer suites, configure
`measure.tests.<owner/repo>.timeout_seconds` along with the test command. The runtime budget
affects the entropy score; the timeout stops execution. A repository test profile can also
select another interpreter, environment, run count, or budget. Structural analysis never imports source modules. Use a trusted
local test environment: a disposable worktree is not an operating-system sandbox.
Maxwell does not install dependencies or inherit provider/GitHub credentials into tests.

Use repeatable `--skip source` or `--skip tests` arguments on `measure` or `scan`
to disable measurement stages. A full scan also accepts `--skip hunt`, `validate`, or `audit`;
`--stage hunt|validate|audit` still runs just that agent stage using published measurement context.
Source covers complexity, duplication, and coupling; tests covers runtime and flakiness.
Skipped stages reuse the latest compatible usable GitHub measurement, including across
revisions. Reports mark reused factors and retain the original measurement ID, revision,
timestamp, and test environment fingerprint. This allows a score combining current and
historical evidence; it does not claim all factors were measured at the current commit.
Without compatible history, a skipped stage remains unknown. Only explicitly skipped
stages reuse history; failed or incomplete active stages retain their actual outcomes.

Configure the test profile by repository in `config.yaml`:

```yaml
measure:
  tests:
    owner/repo:
      command: [/absolute/path/to/venv/bin/python, -m, pytest, -q]
      runs: 5
      warmups: 1
      timeout_seconds: 600
      runtime_budget_seconds: 300
      environment: {}
```

The argv command must run pytest and accept appended pytest options. Environment
variables are explicit. Plugin autoload is disabled by default; load required plugins
with explicit `-p` options or configure `PYTEST_DISABLE_PLUGIN_AUTOLOAD` in the profile.
Configured source roots from the pinned snapshot precede external Python paths, so
an editable project installation does not substitute your working checkout for the commit.
The initial collector supports serial pytest runs; parallel worker reports are not
supported. Each warmup and measured run starts from the same commit in a fresh snapshot.
Warmups are excluded from timing and flakiness observations. Command and configured
environment fingerprints are published; their values are not copied into public records.
Complete local pytest reports and output logs remain under the measurement artifact.

The versioned `maintenance-v2` policy has five factors, weighted **20% each** by default:

| Factor | Raw ratio and normalization |
| --- | --- |
| Complexity | Sum of excess function complexity above threshold 10 / total function complexity |
| Duplication | Tokens covered by repeated exact token windows / analyzed tokens |
| Architecture coupling / cohesion | Equal mean of dependency propagation cost and lack of module/class cohesion |
| Test running time | Median complete-suite wall duration / configured runtime budget |
| Observed test flakiness | Tests exhibiting both pass and fail / consistently executed pass/fail tests |

Each ratio is capped at 1. The score is `100 × sum(weight × normalized factor)`,
from 0 to 100, with lower values indicating less measured maintenance burden. Runtime
scores saturate at the budget; raw duration and duration deltas remain visible so
improvements above the budget are not lost. This is a transparent maintenance policy,
separate from agent reputation and source-assessed defects, without empirical calibration.

The Python adapter includes tracked `.py`/`.pyi` files and reports its scope and exclusions.
Complexity starts at 1 per function, adding conditional branches, loops, exception
handlers, Boolean alternatives, comprehension loops/filters, and non-default match
cases/guards; nested function bodies are measured separately. Clones use exact token
strings, ignoring comments and layout, with windows of at least 50 tokens spanning
five token-bearing lines; overlapping coverage is counted once. Dependency graphs
resolve static internal imports, including relative imports and configured source roots.
Architecture analysis excludes test modules using `architecture_exclude`. Dependency propagation
cost is the fraction of ordered pairs of distinct production modules connected by an import path.
It detects broad change exposure even in acyclic graphs; cycles receive no separate penalty.
For each module and class, members form an undirected graph connected by direct calls or shared
module state / instance attributes. Imported utilities alone do not connect module members.
Class constructors are excluded so initialization cannot make unrelated methods appear cohesive.
Lack of cohesion is the fraction of member pairs in different connected components, aggregated
across eligible modules and classes. Units with fewer than two members contribute no pairs;
empty pair populations contribute zero by policy. The architecture factor is the equal mean
of these two ratios. Fan-in, fan-out, propagation and responsibility clusters are reported as
hotspots. They are static maintenance signals, not proof of an architectural violation; legitimate
facades and collections of pure functions need source assessment before a finding is produced.
The new analyzer/policy starts a new baseline; the old cycle-only values are not comparable.
Module and cohesion-member limits bound analysis and report unknown rather than truncate scores.
Dynamic imports, parse failures, ambiguous module names, empty populations, and configured
resource limits are reported as unavailable evidence rather than healthy zero values.

All thresholds, exclusions, source roots, weights, and resource limits are configurable
under `measure`; see `config.example.yaml`. All five positive weights must sum to 1.
The full aggregate is unknown until every factor has usable evidence. Missing test
profiles, absent runtime budgets, and incomplete runs never redistribute factor weights.
The report shows measured factor weight and Python file coverage separately. This first
adapter does not claim to analyze non-Python source.

Test outcomes include setup, call, and teardown. Skips/expected failures are excluded
from the observed pass/fail denominator. Persistent failures appear separately.
Timeouts, collection errors, changed collection, source modifications, and runner errors
make affected factors unknown. Zero observed flakes does not prove zero flakiness;
observation depends on the repeat count and environment.

Measurements are append-only GitHub comments on the history issue, with SQLite retaining
operational runs and publication intents. Their records contain enough evidence to
reconstruct the score in a fresh database. Existing measurement records remain readable,
but older Maxwell installations need an upgrade to read the new evidence fields.
The earliest compatible current GitHub measurement is the baseline. Comparisons require
matching analyzer, policy/protocol, command/settings, dependencies, runtime environment,
and host fingerprints. Changed or missing records cannot serve as baselines. Reports show
score, raw duration, and test-count deltas when comparable. Failures retain local evidence;
the next measurement retries pending publication. Measurements do not consume agent budgets.

## Dashboard and finding factors

The main **Maxwell status** issue is updated in place. It shows total maintenance entropy,
the measured revision and baseline change, and weighted component contributions such as
**Complexity: 3.76 points**. Each contribution links to open findings carrying the corresponding
`maxwell:factor:*` label. Missing evidence is shown as unknown; available weights are never
redistributed. Raw measurements, test evidence, tokens, reputation, and recent executions
remain available in expandable sections. Source-supported and human-confirmed open bugs
are counted separately from structural entropy.

hunt agents can associate findings with `complexity`, `duplication`, `coupling`, `test_runtime`,
and `test_flakiness`. The evidence must explain why the proposed repair would reduce that
factor's burden.
An empty list means no established structural association; ordinary bugs
need no association. validate agents and audit agents independently confirm or correct this list.
The latest applicable assessment stage controls labels; conflicting decisions conservatively
remove disputed associations. Rejected findings lose their factor labels. Legacy assessments
without a factor decision retain earlier associations. Finding identity does not change.
Associations remain qualitative; numerical estimates require a repair plan and measurement evidence.

### Scan progress output

Interactive `maxwell scan` shows **measure** as baseline preparation, then **hunt → validate → audit → fix → review → integrate**.
measure includes source analysis and test runs. validate and audit show completed/selected
finding counts; the current model, activity, and elapsed time appear as space permits.
A spinner and moving highlight refresh about eight times per second during long operations.
Test runs report live completed/total counts, failures, and the current test; the measure
segment advances through source analysis, individual test runs, and publication. Animation
is an activity indicator and never invents completed work.
Active stages use yellow, cyan, magenta, and green; completed stages show a green check,
failures a red cross, and pending/skipped stages are muted. The continuous green/gray bar
tracks stage and assessment completion, not predicted time remaining. `NO_COLOR` disables
colors. Later stages continue after failures. Redirected output prints plain stage milestones
without phase or heartbeat spam.

The final summary links to the main **Maxwell status** issue and aggregates findings,
verdicts, and repeated failures. Local report paths and individual issue links remain in
`maxwell scan --json`; they are omitted from the human output. Detailed agent diagnostics
are retained in `~/.maxwell/logs/daemon.log` and the existing per-execution artifacts.

### repair estimates and fix ranking

validate agents and audit agents can attach a behavior-preserving repair plan, proposed validation,
uncalibrated confidence, and an engineer-hour effort interval to their GitHub assessments.
They predict two bounding raw metric scenarios, including changing denominator populations.
Coupling uses dependency reachability and member cohesion pair counts; runtime keeps the
measured budget fixed. Maxwell calculates weighted point changes using the referenced
measurement policy. Negative changes mean a predicted regression, not a reduction.

The status description contains a fix queue. The `maintenance-impact-v1` ranking score is
**lower estimated point reduction × confidence / upper effort hours**. Only positive,
complete estimates receive a rank score. All five factors must be projected or explicitly
declared unchanged with evidence. Partial estimates, missing records, conflicting plans,
policy mismatches, old measurements, and reused evidence from another revision remain
unranked with an explanation. Predictions cannot be added across issues because repairs
may overlap. Bug severity has its own priority list and is never replaced by entropy impact.

The latest applicable supported, actionable assessment controls the queue: human decisions,
then audits, then provisional reviews. An audit with no estimate does not revive a validate's
estimate. GitHub records reconstruct this queue in a fresh SQLite database; no migration or
paid reassessment runs automatically. Closed and rejected issues leave the queue.

New `maxwell scan` reviews and audits request estimates automatically. Existing assessments
remain unknown until refreshed, for example:

```sh
maxwell scan --stage validate --force owner/repo
maxwell scan --stage audit --force owner/repo
```

Assessments remain read-only and never execute the validation plan. Closing an issue does
not establish a validated fix or an observed entropy reduction. Dashboard tables display the first
50 entries per track and report how many additional entries are omitted; this is a presentation
limit and does not limit collection or assessment. Independent repair validation reports
observed snapshot changes; causal per-finding attribution and integration remain separate work.

Future execution and measurement records go to a separate **Maxwell history** issue marked
`<!-- maxwell:history:v1 -->`. Assessments remain on their finding issues. Legacy generated execution and measurement comments on the status issue are removed; they are not migrated. Pending
unpublished execution/measurement records targeting the status issue are redirected to history.
Uncertain creations must first be reconciled before changing their destination. GitHub records reconstruct the dashboard and agent reputation in a fresh SQLite database.

## Execution boundary

Worktrees provide isolated files, not a hostile-code sandbox. M1 targets trusted local
repositories and uses Codex's read-only sandbox and non-interactive approval policy.
hunt is instructed not to run repository builds, tests, or installation scripts;
that semantic restriction is not an operating-system guarantee. Output is validated,
paths/line numbers must exist in the snapshot, and modified worktrees are rejected.

The provider does not inherit Maxwell's GitHub token through its environment. Native
providers use the selected existing home; legacy API-key Codex uses a managed home under
`~/.maxwell/runtimes/`. hunt disables native hooks and external tools; Claude is
restricted to Read/Grep/Glob and Codex uses its read-only sandbox. Exact configured and
resolved native credential values are redacted
from collected output and reports. This is not a defense against encoded secrets or
arbitrary filesystem reads; provider credentials are available to the CLI process.
Do not use this milestone on hostile repositories. Container/VM isolation and stronger
credential separation are prerequisites before adding execution of repository code.

## Develop and review

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

Tests use temporary Git repositories, a fake Codex executable, and a local GitHub HTTP
server. They verify the full issue/report flow, priority and budget admission, privacy,
duplicate suppression, stale results, singleton execution, foreground runs and interruption cleanup,
hard-crash recovery, and ambiguous publication. No paid model calls or real issues are
created. Focused tests own edge cases; the end-to-end test exercises the integration.

## Next milestones

1. Manual hunting, source assessment, audit, and issue/assessment publication — this release.
2. Independent tester/reproducer consuming discovery issues.
3. fix agent consuming confirmed issues and producing patches.
4. Independent review agent, integration checks, and GitHub-based merge approval.

The durable issue/artifact handoffs remain the boundary between agents. Extend measurement
adapters, add sampled audits and independently validated stage ratings, and consider
further providers and optional GitHub Pages reporting as evidence and capabilities grow.
