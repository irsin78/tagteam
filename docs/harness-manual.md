# Harness manual — operation and diagnosis

> For an introduction, see [README](../README.md); for goals, responsibility
> boundaries, and model selection rationale, see [Design principles](design-principles.md).

- [Installation](harness-install.md) — reading order, copy targets, platforms, hook trust and CLI updates.
- [Launchers](harness-launchers.md) — execution recipes, flags, reports, exit codes and WSL isolation.
- [Maintenance guide](harness-maintenance.md) — template regression procedures.

Delegates focus on their assigned task and necessary platform guidance. Do not read the
entire manual for onboarding.

## Configuration summary

> `.claude/rules/`, `.claude/agents/`, and `.claude/skills/` are authoritative.
> Keep only configuration-key reasons, hook behavior, and observation dates
> absent from rules here; use one-line rule references to avoid drift.

### Directory layout

```
.claude/
  agents/     Six subagent definitions (2 external CLI + 4 Claude)
  rules/      Shared policies: roles, verification, security boundaries (Claude auto-loads; Codex reads through AGENTS.md)
  skills/     Recipes loaded only when needed (codex delegation, agy delegation, safety guard verification)
  hooks/      Host-specific hooks and regression tests for dangerous commands, startup, stop, delegation results
  scripts/    codex·agy·claude·local delegation launchers (preflight/invocation/postflight), control-plane hashes, WSL isolation lane helpers
  settings.json · sandbox-sensitive.json · model-bindings.json (tier table) · model-bindings.local.json.example (local override skeleton; .local.json is gitignored)
AGENTS.md.template          Single entry instructions for both hosts (install as AGENTS.md; Codex reads it directly, Claude Code through the import)
CLAUDE.md.template          One-line `@AGENTS.md` import stub (install as CLAUDE.md)
.codex/hooks.json           Codex SessionStart/PreToolUse/UserPromptSubmit/Stop wiring
check-windows-aliases.ps1   Windows installation preflight (aliases + hook liveness + agy grant)
check-posix.sh              macOS/Linux installation preflight (bash ≥ 4 · python · hook liveness · agy grant)
tools/linux-probe.sh        Platform measurements (check unverified notes; no installation or credentials needed)
docs/                       Manual, runtime-boundary.md, platform notes
  orchestration/            Delegation matrix and retry policy, read only by the orchestrator when needed
  missions/                 Intent, confirmed specs, necessary plans and status by objective (Git optional)
```

### Roles and model selection

See [Design principles](design-principles.md#basis-for-model-selection) for rationale.
The [bindings JSON](../.claude/model-bindings.json) defines models, effort, benchmarks,
sources, and measurement dates. The router (`harness-route.py`) selects among `workers`
by band floor, latency class and measured cost (docs/orchestration/delegation-matrix.md,
"Assign a worker"); use the [host-specific execution recipes](harness-launchers.md#host-specific-execution).
With assessment volume, automatic selection compares worker and eligible direct work by USD plus declared waiting-time cost; session and CLI time values override bindings defaults.

Schema v3 gives each worker (one row per model/effort) an `id`, `vendor`, `model`,
`effort`, `tier` (band `S|A|B|C|D|E`), `roles`, `status`, `probe` and `launcher` (or
`native: true`), plus `metrics` (`index`, `cost`, `ttft_s`, `tps`, `secondary_pass1` with
`secondary_effort`/`secondary_source`/`secondary_read`, optional `eci`, `provisional`) or `scored: false` for rows the router never picks automatically.
Top-level `bands` (capability labels independent of index; S is fixed by vendor line:
Fable on the Claude lane, Astra on the OpenAI lane), assessment policy and `latency`
(interactive/foreground TTFT limits) drive selection. OpenAI image verification
defaults to GPT-6.1 Sol/medium; Opus 5.5/high remains the second image worker.
Role priorities are positive integers or per-host maps and remain for host eligibility
and `--worker`; a role can also contain `priority`, `hosts` and `requires`. `tier_cell`
is kept as documentation only. Status is `active`, `optional`, `conditional` (explicit
`--worker <id>`) or `unverified` (excluded from automatic selection, e.g. a model not yet
rolled out to the account). `workers_local` recursively merges
matching IDs and appends new ones; a local `workers` array replaces the public list. For
example, in local bindings:

```json
{
  "workers_local": [{
    "id": "project-reader", "vendor": "local",
    "model": "auto", "effort": null,
    "tier": "D", "status": "optional",
    "launcher": ".claude/scripts/local-run.sh",
    "roles": {"explore": 3},
    "requires": {"local_endpoint": true}
  }]
}
```

Requirement keys are `local_endpoint` (declared endpoint with base URL/model),
`agy_grant` (named global allow grant), `agent_file` (project-relative file), and
`binary` (executable on PATH); worker and role requirements both apply. Checks use local
declarations/files only, with no CLI or endpoint probe. The launchers'
`--launcher-default` router mode reads active vendor/role defaults; it does not select
another delegation. Reader inputs and local model `auto` behavior are defined in
[Runtime boundary](runtime-boundary.md#restricted-file-reads-by-a-local-model).

| Role | Default route and selection criteria |
|---|---|
| Orchestration and decisions | Keep the host the user started. Choose a tier matching the judgment required |
| Implementation | Claude orchestration routes to the OpenAI lane; Codex orchestration routes to the Claude lane. The router picks the cheapest row meeting band floor B in the latency class. Explicitly lower the floor for mechanical work |
| Writing, HTML, experimental code | Match the tier to the audience and task. Apply binding exceptions for specialties such as HTML |
| Review | Verify according to risk. Choose gate and deep-review routes per host |
| Image verification | Explicit binding in `roles.image_verify`. Does not always match the orchestrator/implementer's vendor |
| Exploration, logs, external documents | Handle a few file lookups directly. Explicitly select D for simple extraction, C/B for structural or dependency analysis. Web isolation is a separate requirement |
| Build/test execution | Run checks needed for the task and inspect exit codes/results. Explicitly save large output to logs |

For unavailable CLIs, follow delegation-matrix fallback; report irreplaceable
requirements (such as independent review) incomplete. Choose exploration by the
question, not blindly by the default D floor: e.g. `--role explore --tier C`. Do not replace
models wholesale after one benchmark/failure. Authentication/quota failures may exit 1;
inspect original CLI errors and partial artifacts before fallback or reauthentication.
Printed document error strings are not authentication failures; never repeat a
revoked-token call.

### Assignment announcement before delegation

Follow the
[delegation matrix](orchestration/delegation-matrix.md#user-facing-delegation-announcement)
and AGENTS.md on both hosts. Immediately before implementation, review, or exploration
delegation, announce the task, tier rationale, actual model/effort, and selection
reason. For example:

> The cache invalidation fix is a tier B task requiring reasoning across modules.
> I will assign implementation to `<assigned model>` (effort: `<configured value>`)
> from a different vendor than the designer to cross-check assumptions.

Fill the angle brackets from routing results.
Capability tiers A–D differ from verification risk Tiers 0–2. Announce model changes and
reasons; this message requires no renewed approval.

### Separating shared contracts, host mechanisms, model traits, and roles

Distinguish four layers; do not change another layer wholesale because of a problem in
one.

| Layer | Content | Where to apply it |
|---|---|---|
| Shared contract | Preserve existing work, approval/permission scope, actual artifacts and verification evidence, delegate limits | Shared rules and launchers. Applies regardless of host/model |
| Host mechanism | Instruction loading, hook wiring/trust registration, permission settings, launcher arguments/exit codes | That host's entry instructions, launcher, and platform section |
| Model traits | Official guidance and observations for a model family (effort values, response format, etc.) | Routes using that model and bindings |
| Role | Amount of instruction actually needed by orchestrators, implementers, reviewers, read-only workers | Role-specific instructions. Do not make delegates read orchestrator-only documents |

Fix evidence/approval defects in shared rules. Add host/model adaptations only for
demonstrated gains in failures, rework, or delay. Do not generalize one vendor's
prompting advice to another, or assign procedural strength by vendor; use task risk and
observations.

### Applying the GPT-6 Astra official guide

The [official OpenAI guide](https://developers.openai.com/api/docs/guides/latest-model)
applies to OpenAI routes, not other vendors' procedures. Both hosts follow
`session-role.md`, `verification-tiering.md`, and
`docs/orchestration/delegation-matrix.md` for shared contracts. Check scope and
duplication in AGENTS.md and instructions actually read; point to required
sources instead of restating them. No full instruction audit is needed per task.
Codex execution conditions also apply to Codex workers directed by Claude Code. For installation,
see [Method A](harness-install.md#installation); keep behavioral principles out of `.codex/hooks.json`.
[Official AGENTS.md loading guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

| Category | Official basis and template handling |
|---|---|
| Astra API effort | The [model specification](https://developers.openai.com/api/docs/models/gpt-6-astra) lists low/medium/high/xhigh/max. none/minimal are not Astra options |
| Codex app modes | The [model guide](https://learn.chatgpt.com/docs/models) distinguishes Max as extra reasoning for one task and Ultra as automatic subagent parallelization |
| Codex delegation launcher | For the single-worker contract, Ultra exits 4 for explicit arguments, bindings, detached execution, and resume. Max uses `-b` and `--wait`. The human's main settings remain unchanged |
| CLI-verified scope | Enumerated values in the [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) differ from app choices. Documentation, local catalogs, and stub checks alone do not establish successful real model execution |

Retain baseline models/effort and benchmarks. Before changing routes, compare
representative tasks for role quality and full completion cost/time (startup,
rework, parent inspection); external benchmarks only inform initial selection.
Set native subagent model/effort explicitly; if unavailable, count inheritance
cost when deciding to delegate. Same-vendor parallelism is not independent review.
[Official subagent guide](https://learn.chatgpt.com/docs/agent-configuration/subagents).

**API boundary**: This template uses `codex exec`. Asynchronous calls, WebSocket
steering, and cache-preserving `configuration_update` require API implementation,
not hook/prompt options. Use Responses for Astra tool calls, remove unsupported
sampling arguments, and consult migration guidance for cache settings.
`configuration_update` supports only Astra standard single-agent mode, with
automatic-compaction constraints. CLI resume still reapplies settings explicitly.
[Changing reasoning settings](https://developers.openai.com/api/docs/guides/reasoning#change-reasoning-mid-conversation).

Experimental context management is optional for supported Plus/Pro clients;
Business/Enterprise/API-key login is excluded at launch. Check project support
before personal selection; never enable/generate shared config, and retain
explicit cross-host handoff contracts.
[Official context management guide](https://learn.chatgpt.com/docs/models#experimental-context-management).

### Policy documents — pointers

- Roles, progress, conflicts, requirements, communication: `rules/session-role.md`
- Routing, switching-cost exceptions, fallback:
  `docs/orchestration/delegation-matrix.md`
- Read-only routing brief: `docs/orchestration/scout-brief.md`
- Actual artifacts, existing changes, orchestrator-only commits:
  `rules/delegate-output-trust.md`
- Risk, Git timing, mutation checks: `rules/verification-tiering.md` +
  `skills/verify-safety-guard`
- Retry/escalation: `docs/orchestration/retry-policy.md`
- Restricted web routes, agy grant, prompt-file handoff, Read deny:
  `rules/security-boundary.md`
- Spec confirmation, execution, handoff, context: `rules/mission-artifacts.md`
- Platform notes: `docs/platform-notes-*.md`, outside `.claude/rules/` to avoid
  loading on other platforms. SessionStart's `HARNESS PLATFORM:` points to this
  `docs/harness-install.md` Windows installation section or `docs/platform-notes-macos.md`/
  `docs/platform-notes-linux.md`; platform rule files are not part of the harness.
- `rules/mission-artifacts.md` and `rules/plan-check-gate.md` are scoped to
  `docs/missions/**` with `paths:`. The orchestrator reads them before mission
  creation as directed by AGENTS.md; do not rely on Claude's path auto-loading.
  Codex also follows AGENTS.md's explicit read path. Examples:
  [Mission operation](missions/README.md).
- Recipes: `skills/delegate-codex/SKILL.md`, `skills/delegate-agy/SKILL.md`.
  Entry instructions resolve roles and required reading.

### Rationale for rules and scope of reconsideration

Reconsider only affected policies per
[Design principles](design-principles.md#change-and-re-review). The table is decision
evidence, not extra approval gates or measurement obligations.

| Rule | Problem addressed | Observations for reconsideration |
|---|---|---|
| delegate-output-trust | Mismatch between completion reports and actual files/work scope | Verifiable artifact evidence from the host, repeated mismatches |
| delegation-matrix | Model selection mismatched to capability, cost, or latency | Actual model adoption, repeated failures/delays, availability changes |
| plan-check-gate | Major design omissions hard to find through execution checks alone | Omissions found by review and review cost, project requirements |
| mission-artifacts | Missing spec confirmation, lost agreements during stepwise stops/handoffs | Implementation before confirmation, unnecessary session switches, preservation of agreements on resume |
| retry-policy | Waste from escalating models for environmental failures | Actual outcome changes from failure classification/retries |
| security-boundary | Expanded scope/permissions, damage to existing work | Protection contract defects or host protection changes |
| verification-tiering | False completion or repeated verification costing more than the task | Missed defects/rework and actual utility of extra verification |

### Configuration and default hooks

[Runtime boundary](runtime-boundary.md) is authoritative for default enforcement,
removed procedures, and optional tools.

- settings.json holds shared defaults such as permissions, delegation depth, and
  concurrency. The adopting project defines sensitive paths and approval conditions.
  Verified checkpoint commits are authorized within a confirmed task. Ask once
  at completion before pushing, or earlier when the next verification needs the
  remote (CI, deployment). `permissions.allow` includes git add/commit, but not
  `Bash(git push:*)`; the explicit single-owner repository alternative in AGENTS.md
  requires adding that allow rule. Force push, history rewrite, tag/release/publish,
  branch or remote deletion, a new remote/upstream, and other external services
  still require asking first.
- PreToolUse directly runs deny_dangerous.py. It performs limited checks on direct
  commands and structured writes; it does not interpret entire shell programs.
- SessionStart leaves only platform guidance and minimal records. It does not
  inspect usage, local servers, or all files. Diagnose installation issues with
  check-windows-aliases.ps1/check-posix.sh.
- SubagentStop records only metadata such as delegate and work paths. The parent
  checks actual artifacts, reusing external launcher file-change evidence.
- Stop requires successful verification only when the task has a .stop-gate.
  Registered multi-item missions separately get one early-stop recovery.
  UserPromptSubmit clears the previous session marker; re-register when continuing
  existing work. Usage/scope:
  [Mission operation](missions/README.md#recovering-a-multi-item-mission-from-an-early-stop).
- No ConfigChange approval marker files, automatic output transformation, or
  permission-request audit hooks are provided.
- Declare budgets in environment variables and local bindings. No automatic
  usage detection is provided.

Do not clean logs at delegation startup. Preview with harness-clean.py, then explicitly
execute with --apply. Execution tracking, duplicate-write prevention, and Git/non-Git
file comparisons remain in place.

### Default execution cost

Default hooks provide platform guidance, limited direct checks, delegate metadata, and
optional task verification. Run diagnostics/statistics as needed; other configurations'
probe/compression measurements do not show current cost.

## Git/non-Git workspaces

Git is optional: the starting app orchestrates the confirmed Mission either way.
Distinguish session operation from CLI requirements; do not change hosts or
automatically run `git init` for a non-Git workspace.

Run all four process launchers from the project root: Git top level, or for non-Git, the
first ancestor with `.claude/` or installed `AGENTS.md`/`CLAUDE.md`, failing that the
current folder. New tasks below root receive root guidance. `--status`/`--wait` use the
same normalized root from subfolders; Codex/Claude/ Antigravity share its concurrency
guard. Local reads run separately without change measurement: `CHANGED: not measured`
does not mean unchanged. The next snapshot contract covers the three external model
launchers.

`workspace-snapshot.py` and `workspace-evidence.sh` collect before/after evidence.
Root/key lookup does not scan contents. Start combines saving/description; end combines
saving/comparison; control hashes include comparison/classification. These reduce
startup/repeated reads without changing detection scope.

- **Git**: Compare status/content hashes of non-ignored modified/untracked files
  and index blob/mode/stage. Includes further edits to already-dirty files and
  deletion of existing untracked files. Detects staged-content changes even if
  status letters and work files match. External model launchers separately check
  HEAD advancing, moving backward, or disappearing.
- **Non-Git**: Compare creation/modification/deletion using file lists, types,
  and content SHA-256 under the root. Does not interpret `.gitignore`. Excludes
  `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `.cache` directories at any depth, root
  `.claude/{codex,claude,agy,local}-logs/`, and `.claude/.probe-cache`. Includes
  other artifacts/documents. Arbitrary `logs/` or build folders are not excluded
  automatically.
- **Run artifacts**: External model launchers exclude exact log files owned by
  that run, not the whole custom `-l` folder. Report `WORKSPACE` records the
  actual root, method, and exclusions.
- **Failures and limits**: Do not downgrade existing Git corruption/access
  failures to non-Git. Do not mark complete if root/method/exclusions change
  during execution or files cannot be read. A non-Git scan encountering a nested
  Git repository fails, requiring a separate workspace check. Record symbolic
  links/Windows junctions themselves without following targets; do not open
  devices/FIFOs. Hashing large non-Git files reads all content and has a cost.
  Snapshot time is outside the child CLI's `-t` limit but inside the invoking
  tool's own timeout. Start-check failure exits 4; post-execution check failure
  exits 1. Neither justifies automatic delegation fallback; inspect the cause.

Inspection grants no write permission and cannot guarantee verification of excluded
files, link targets, external paths, or reverted changes. External launchers separately
hash control files. Snapshots are not backups; the project defines non-Git rollback,
sharing, backups, and extra inspection. Run independent large subprojects as separate
roots with required checkers.

`HARNESS_SAVE_BASELINE=1` optionally saves `baseline-<timestamp>.diff` in the launcher
log folder: tracked Git modifications only, excluding untracked/ignored files, not a
backup. No baseline diff is generated by default or for non-Git; change-detection
snapshots always run.

Codex's
[official non-interactive execution documentation](https://learn.chatgpt.com/docs/non-interactive-mode#git-repository-required)
provides a non-Git execution option. `codex-run.sh` adds `--skip-git-repo-check` to new
runs, resume, and Windows probes only after valid non-Git detection and the start
snapshot. User addition of this option through raw CLI remains blocked; it does not
relax sandbox, hook trust, or permissions.

Read-only launchers fail on work-file changes and separately identify normal existing
session-owned NOTICE updates. Antigravity retries an empty response once only if files,
commits, and declared external artifacts are unchanged. Unavailable responses after
partial writes fail with exit 1, not fallback exit 2.

`claude-implementer`/`opus-architect` with `isolation: worktree` require Git. Non-Git
uses process launchers with the same role/binding; isolation needs a copy with an
explicit baseline. commit/push/merge, history, worktrees, and installing
`.gitattributes`/`.gitignore` apply only to Git. Bash/Python, selected CLIs, and
platform checks apply to both.

Verification scope: Windows snapshot/router and all four launchers' Git/non-Git stub
checks, plus a real read-only Claude round trip in a temporary non-Git installation.
Installed `exec`/`exec resume` help and argument checks validate Codex options, not
real non-Git Codex/Antigravity execution or other OSes.

## Mission operation

In `docs/missions/<purpose>/`, record objectives in `intent.md` and behavior,
constraints, and acceptance in `spec.md`; obtain user confirmation or record existing
conversational confirmation. Continue planning, implementation, verification, and fixes
to completion without renewed approval at stage, delegation, or review boundaries.
Report blockers and issues requiring user judgment. Follow
[Mission operation guide](missions/README.md) for structure, staging,
cross-verification/resume, and Git management. Preserve confirmation; do not arbitrarily
approve new specs or require hook changes/per-stage commits.

## Identifier audit before publication

Before publishing or pushing a harness copy, search the tracked tree for personal
identifiers such as usernames, machine names, home paths, IPs, and e-mail domains with
`git grep -n -i -P -f <pattern-file>`. A pattern file containing your username or
domain must not be tracked; keep it in a local gitignored file. Run the same check in
acceptance scripts. This is a procedure, not a script included in the template.

## Operation

- The user selects the main model/effort. Within a confirmed Mission, continue
  implementation, verification, and fixes; stage transitions alone do not require
  model changes or session restarts.
- Use local bindings for per-account model access, subscriptions, and budget
  declarations. Do not automatically query usage at startup or economize by
  skipping mandatory verification.
- For long delegation, use launcher `-b` and `--wait <RUN_ID> -t 570`. Wait exit
  code 6 means still running, so continue checking status. Follow each launcher's
  recipe for specific options.
- If actual costs/delays repeatedly miss expectations, inspect existing execution
  records and [TIMING](harness-launchers.md#meaning-of-launcher-statistics). Do not treat older measurements as current performance.

Personal settings such as subscription plans and global output preferences are optional.
Adopting the template does not require copying another user's global `CLAUDE.md` or
account settings. When changing model routes, use existing `model-bindings.local.json`
and check support in the current CLI.

## Time value (waiting cost)

With assessment `volume`, the router scores eligible direct work and delegates as `total = usd + time_cost`: estimated money plus a dollar value for waiting.
A cheaper agent can cost more overall if its delay blocks you or makes you switch projects. Capability, author separation, availability and trust still constrain selection.

For estimated minutes `m`, let `T = tolerance_min`, `s = switch_after_min`, `P = refocus_usd`, and `slope = slope_usd_per_tolerance`. The default exponent is 2:

| State (`--time-mode`) | Waiting cost |
|---|---|
| Waiting (`attended`), `m <= s` | `k * (m/T)^2` |
| Waiting initially, then switching (`attended`), `m > s` | `P + slope * (m-s)/T` |
| Already switched away (`background`) | `P + slope * m/T` |
| Away (`unattended`) | `P + slope * m/T` |

Defaults: `T=30` is the wait you consider "long"; `s=3` means you switch to other work after three minutes. `k=5` prices a full T of continuous waiting at $5 on the convex curve (the default switches sooner). `P=5` prices coming back after a switch, representing roughly 30 minutes of lost focus. Each further T costs $1.50 while on other work, or $0.25 while away. Background/away start with `k=0, P=0`:
they do not charge an initial switch that has already happened or is irrelevant.

These are the maintainer's preferences from switching projects while an agent works and needing about 30 minutes to regain focus, rather than measured constants.
The rationale follows convex waiting cost ([Osuna, 1985](https://doi.org/10.1016/0022-2496(85)90020-3)),
the roughly 10-second attention limit ([Miller/Nielsen](https://www.nngroup.com/articles/response-times-3-important-limits/)),
and refocus effort after interruption ([developer interruption studies](https://www.microsoft.com/en-us/research/video/programmer-interrupted-data-brains-and-tools/)).
Transport economics values waiting roughly 1.5–2 times in-vehicle time as a [directional analogy](https://trid.trb.org/View/2680681) for active time, not an AI-task calibration.
None of these sources measures the shipped dollar values or three-minute threshold.

Example: a Claude host with eligible A-band direct work and B-floor implementation, using shipped seeds, normal availability/trust, and no matching history. The best
worker is `sol61-medium`: worker money is `0.21 * task_units[volume]`; minutes are
`base_minutes[volume] * clamp(sqrt(5.7/58), 0.5, 3)`; add delegate overhead to both.

| Task/option | Money; minutes | Attended total | Unattended total |
|---|---|---|---|
| Small (volume 0), direct | $3.80; 6.5 | $8.98 | $3.85 |
| Small, delegate | $2.81; 9.6 | $8.14 | $2.89 |
| Large (volume 2), direct | $13.60; 30.4 | $19.97 | $13.85 |
| Large, delegate | $7.23; 23.2 | $13.24 | $7.42 |

Delegation wins both states: small-task savings exceed the extra delay; the large delegate is cheaper and faster. Small attended direct is `3.8 + 5 + 1.5*(6.5-3)/30`.
Away prices the same wait much less. These provisional seeds are not promises; matching DONE records can replace worker minutes, and different floors change eligibility.

Change values in precedence order: router `--time-mode`, `--time-tolerance-min`,
`--time-cost` (k), `--time-refocus` (P), `--time-slope` for one call; then the session;
then `.claude/model-bindings.local.json` for the person/machine; public defaults last.
The orchestrator declares the session from the user's words, such as "I am leaving this running overnight": `python .claude/scripts/harness-session.py time --session <id> --mode unattended --reason "user leaving overnight"`.
Set `HARNESS_SESSION_ID=<id>` for routing. Session options also accept `--tolerance-min`, `--cost-at-tolerance`, `--refocus-usd`, `--slope`; `--clear` restores bindings.
A CLI mode loads that mode's bindings values unless individually overridden; other CLI values preserve unspecified session values. The score reports values/source.

A complete local override (dictionaries merge; `exponent` changes the curve):
```json
{"selection_policy":{"time_cost":{
  "tolerance_min":30,"switch_after_min":3,"exponent":2,"default_mode":"attended",
  "modes":{
    "attended":{"k":5,"refocus_usd":5,"slope_usd_per_tolerance":1.5},
    "background":{"k":0,"refocus_usd":0,"slope_usd_per_tolerance":1.5},
    "unattended":{"k":0,"refocus_usd":0,"slope_usd_per_tolerance":0.25}
  }
}}}
```
Legacy numeric modes, e.g. `"attended": 5`, retain pure convex `k*(m/T)^exponent` throughout.
To tune, ask yourself "30 minutes of waiting versus how many dollars?" Keep `refocus_usd >= k*(s/T)^2` so cost does not drop just past the switch; revisit after a few tasks.

## Verification and reconsideration

Follow `.claude/rules/verification-tiering.md`, `plan-check-gate.md`, and
`skills/verify-safety-guard/SKILL.md` for verification, plan reviews, and guard changes.
Start with affected behavior checks/diff inspection; add independent review by
consequences/project requirements. Default once, maximum twice including
unresolved-blocker fixes.

For route/evidence changes, compare actual artifacts/reports. Use real delegation for
CLI integration questions unresolved by fixtures/reproductions; do not require paid
drills, full checks, or LLM reviews for document edits/every commit. Reuse evidence with
unchanged relevant inputs. Pair acceptance with observable evidence, for example:

| Acceptance item | Evidence to check |
|---|---|
| Skip invalid CSV rows and record reasons | Passing output from `python -m unittest tests.test_import` and logs containing skipped rows/reasons |
| Reimporting the same file does not store duplicates | Row-count query result after running twice |
| Monthly report totals reflect only valid rows even with invalid rows present | Compare actual output from import → report execution against expected totals |

Check cross-module connections: individual passes do not prove combined results. Report
commands/results, not just "passed"; actual files/checks override conflicting worker
reports. Mandatory checks that cannot run leave work incomplete.

For reviews, follow `verification-tiering.md`: provide changed sections, necessary
surrounding code, contracts, and verification summaries. Match effort to the question
(e.g. `plan_review --tier B` lowers the default A floor for closed-scope planning), preserving
capability/independence floors. File count alone does not justify full context or high
effort.

Reconsider affected policies for repeated failures/delays or shared-contract defects,
including artifact mismatches, rework, and user intervention—not just guard denials. Fix
clear defects without waiting for a consuming-project incident; do not endlessly explore
hypothetical bypasses or add project domain constraints.

## Features not enabled by default

Broad shell-bypass analysis, automatic command rewriting, time-limited approval files,
startup usage/server detection, full memory inspection for every native delegation, and
log deletion at delegation startup are outside the default contract. The adopting
project chooses additional isolation and auditing.

## Known limitations

- Usage limits/credit/billing errors do not trigger the fallbackModel chain.
  This harness handles exhausted credits through a "protocol" (manual switching
  rules).
- The agy write launcher uses the CLI's default model and medium effort unless
  `-e` is supplied; it does not select the model from the binding JSON. Candidate
  bindings are not evidence of the model actually used. After a diagnosed
  availability failure, use the current host's implementation fallback, preserving
  the task's capability/risk floor. The launcher retries empty output once only
  when the first attempt left no changes. agy versions without
  `--output-format json`/`--effort` are unsupported.
- Attaching Codex/Antigravity as subagents is outside Anthropic's official
  documentation (a community pattern). Official scope covers subagents + Bash tools.
- This template **need not set** `permissions.defaultMode`: Pro/Max has built-in
  auto mode, project-scoped explicit `auto` is ignored (intentional security
  design), and `acceptEdits` disables auto, so it is not set (see configuration
  summary). Other user-scoped modes belong in personal `~/.claude/settings.json`,
  outside template scope.
- **Codex Windows execution environment**: Follow the [Windows install section](harness-install.md#windows-specific-details)
  to diagnose/replace Store pwsh aliases. Check environment issues first when
  actual execution fails. check-codex-sandbox.sh is an explicitly selected
  diagnostic; default delegation has no model probe. No automatic full-access
  fallback. Successful diagnostics do not undo file changes or external side
  effects already incurred; also inspect actual change evidence.
- (Optional pattern) Rule-file protection: Add entries such as `Edit(CLAUDE.md)`
  to `permissions.ask` to keep rule-file edits manually approved even in
  acceptEdits/auto mode (`deny` blocks completely, so `ask` fits this purpose).
  `.claude/` is already a built-in protected path, making additional rules partly
  redundant. This is not default because frequent harness maintenance would
  prompt on every edit; optionally use it in projects with stable rules. It also
  does not apply to external CLI (codex/agy) file writes.
- Like codex's Windows sandbox override, agy requires one manual setup of its
  own global settings (`~/.gemini/antigravity-cli/settings.json`), outside this
  harness's control, for headless (`agy -p`) auto-approval; see [Installation](harness-install.md#installation).
  Headless mode has no TTY and automatically DENYs permission requests it cannot
  prompt for instead of waiting. Without settings, file writes/command execution
  silently fail. `--dangerously-skip-permissions` is also a NO-OP in `-p` mode
  and does not help.

## Appendix: Measured route profiles

Keep a measurement date per tag; update tags when remeasuring.

Follow [runtime-boundary.md](runtime-boundary.md) for the current execution boundary,
and the [bindings JSON](../.claude/model-bindings.json) for current model-selection
figures, sources, and measurement dates.

## Sources
Consult each tool's official documentation for detailed behavior and version requirements.
