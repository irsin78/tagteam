# Harness installation

> For an introduction, see [README](../README.md); for goals, responsibility
> boundaries, and model selection rationale, see [Design principles](design-principles.md).

When first adopting the harness, read only [Copy targets](#copy-targets),
[Installation](#installation), and the section for your platform. During work, look up
the [host-specific execution](harness-launchers.md#host-specific-execution) recipe needed.
Delegates focus on their assigned task and necessary platform guidance. Do not read the
entire manual for onboarding. Template regression procedures are separate in
[Maintenance guide](harness-maintenance.md). For operation and diagnosis, see
[Harness manual](harness-manual.md).

## Support status

| Axis | Item | Status |
|---|---|---|
| Platform | Native Windows | Verified |
| Platform | WSL2 (Ubuntu) | Isolation lane demonstrated |
| Platform | macOS | Separate notes, partially verified |
| Platform | Native Linux (Ubuntu) | Partially verified (24.04 aarch64: hooks/launchers passed); isolation lane unavailable on that host. See platform notes for item-specific scope |
| Host | Claude Code orchestration | Verified |
| Host | Codex orchestration | Codex → Claude implementation/parent verification and routing/role regressions passed. Separately verify automatic hook firing and SessionStart trust registration in each installed environment; CLI guard observations retain their original scope |
| Model | Three cloud vendors | Rationale listed in the tier table |
| Model | Local endpoint | Tier D bulk reads only, budget-linked (OpenAI-compatible server on LAN; declared only in local bindings) |

## Copy targets

Default: bidirectional Claude Code/Codex delegation. Copy shared files and your platform
files at the same relative paths; add only needed optional bundles. Copying all of
`.claude/` is unnecessary. Do not copy local settings, logs, caches, or individual
missions. Merge existing settings/instructions without overwriting.

| Shared required files | Required when | Reason |
|---|---|---|
| `AGENTS.md.template` → `AGENTS.md`, `CLAUDE.md.template` → `CLAUDE.md` | Shared | One entry instruction file for both hosts plus the `@AGENTS.md` import stub for Claude Code. Fill Project policy in AGENTS.md only |
| `.claude/settings.json`, `.claude/model-bindings.json`, `.claude/model-bindings.local.json.example`, `.claude/rules/*.md` | Shared | Hook wiring, permissions, shared contracts; model-bindings.json uses schema v2. Create personal settings in the target project from the example |
| `docs/orchestration/delegation-matrix.md`, `docs/orchestration/retry-policy.md`, `docs/orchestration/scout-brief.md` | Shared | Orchestrator reads only needed documents when selecting delegation/diagnosing failures. Separate from Claude's automatic rules loading |
| `.claude/hooks/deny_dangerous.py`, `session_preflight.py`, `stop_gate.py`, `verify_delegation.py`, `evidence.py` | Shared (all in the same hooks folder) | Shared dependencies of default hooks and delegation records |
| `.claude/scripts/harness-route.py`, `harness-session.py`, `harness_records.py`, `launcher-common.sh`, `control-plane-hash.sh`, `run-state.sh`, `workspace-evidence.sh`, `workspace-snapshot.py` | Shared (all in the same scripts folder) | Routing, run records, stop checks, configuration/change evidence, execution tracking. Keep with each launcher |
| `.claude/scripts/codex-run.sh`, `claude-run.sh`, `codex-report.schema.json`, `.claude/skills/delegate-codex/SKILL.md` | Bidirectional delegation | Per-app execution and Codex results/recipes |
| `.codex/hooks.json`, `.claude/scripts/gen-codex-hooks.sh` | Using Codex | Hook wiring and installation-path generation. Machine-specific `/hooks` trust registration required. Installation checks also use the generator |
| `.claude/scripts/test_host_routes.py` | Using installation checks | Current dependency of both platform installation checks. Unlike other `test_*` files, copy this one too |
| `.gitattributes` | Required with Git | Fix LF. Without it, checkout changes line endings and destabilizes control-plane hashes without content changes |
| Harness entries in `.gitignore` | Required with Git | Prevent committing `settings.local.json`, which accumulates local absolute paths/usernames, and hook evidence files |
| `check-windows-aliases.ps1` | Required on Windows | Installation step 0. Must be at project root because it finds hooks relative to itself; covered by control-plane hashes |
| `check-posix.sh` | Required on macOS/Linux | Installation step 0. Must be at project root because it finds hooks relative to itself; covered by control-plane hashes |
| `docs/harness-install.md`, `docs/harness-launchers.md`, `docs/harness-manual.md` | Required | Installation/platform guidance, launcher procedures, and operations referenced by installed instructions and SessionStart |
| `docs/missions/README.md` | Required | Guidance on work by objective, spec confirmation, continuous execution, and resume. Do not copy this repository's individual mission folders; create them in the target project |
| `docs/runtime-boundary.md` | Required | Current hook responsibilities/detection limits, local-read input format, optional tools, update procedures |
| `docs/dependencies.md` | Required | Per-platform prerequisites and their failure modes, linked from installation |
| `docs/design-principles.md` | Required | Goals, responsibility boundaries, and cross-verification rationale referenced by installed entry instructions |
| `docs/platform-notes-macos.md` | Using macOS | SessionStart hook points here in macOS sessions |
| `docs/platform-notes-linux.md` | Using Linux/WSL2 | SessionStart hook points here in Linux sessions (evidence level per item) |

| Optional bundle | Additional files | When to use |
|---|---|---|
| Claude native workers | `claude-implementer.md`, `opus-architect.md`, `haiku-scout.md`, `haiku-fetcher.md`, `codex-delegate.md` in `.claude/agents/` | Native delegation/CLI controllers. Worktree roles require Git |
| Antigravity | `.claude/scripts/agy-run.sh`, `.claude/agents/antigravity-delegate.md`, `.claude/skills/delegate-agy/SKILL.md` | Configure CLI/permissions only when selecting agy. Shared launcher dependencies also required |
| Local reads | `.claude/scripts/local-run.sh`, `local-read.py` | Declare only when using a local endpoint |
| Enhanced WSL isolation | `.claude/scripts/lane-sensitive.sh`, `.claude/sandbox-sensitive.json` | When selecting a separate isolation lane |
| Statistics, cleanup, Codex diagnostics | Needed files among `.claude/scripts/harness-stats.sh`, `harness-clean.py`, `check-codex-sandbox.sh` | Explicitly invoked tools. Statistics require shared `harness_records.py`; diagnostics require shared `workspace-snapshot.py` |

| Maintenance only | Application in consuming projects |
|---|---|
| `.claude/hooks/test_*`, `.claude/scripts/test_*`, excluding `test_host_routes.py` | Regression checks in the template repository. No general obligation to copy/run for project work |
| `.claude/skills/verify-safety-guard/`, `tools/`, `docs/harness-maintenance.md`, individual missions in this repository | Consult in the template repository for harness changes, platform measurements, and historical evidence |

For Windows process delegation, only shared requirements and the Windows check are
needed; agy, local servers, native workers, and WSL need no preparation. INFO notices
for unused CLIs are not requirements. Before trimming a copy, check selected
hooks/agents/recipes; do not arbitrarily delete shared files. Consult absent maintenance
files in the original template. See Installation for key merging, TODOs, and trust
dialogs.

## Installation

The starting host is Claude Code or Codex; external implementation needs the other CLI.
Antigravity/local inference are optional. Hooks need Python; shell launchers need Bash.
See [Dependencies](dependencies.md) for platform prerequisites and failure modes;
prepare only selected routes. [Support status](#support-status) defines verified scope;
operation on unverified platforms is not promised.

**Method A — project drop-in (independent setup per project)**
1. Copy [shared/platform/selected optional files](#copy-targets). Merge existing
   `.claude/settings.json` keys: `env`, `permissions`, `hooks`, worktree/cache.
   Do not configure unselected features or overwrite user model/effort. Git
   projects also need root `.gitattributes` (shared step 0-1).
1-1. Put `check-windows-aliases.ps1` (Windows) or `check-posix.sh` (macOS/Linux)
   at project root. Both are control-plane-hashed and locate hooks relative to
   themselves (`$PSScriptRoot` on Windows).
2. Copy `AGENTS.md.template` → `AGENTS.md`; fill Project policy with verification/
   completion criteria, conventions, protected paths, risks/reviews, and external
   approval criteria. Model exceptions go in local bindings. Copy `CLAUDE.md.template`
   → `CLAUDE.md`, or add `@AGENTS.md` to an existing file. Claude imports AGENTS.md;
   Codex reads it directly. On upgrade, consolidate formerly split instructions
   into AGENTS.md, leaving only the checked import line in CLAUDE.md. No host is
   named: SessionStart's `HARNESS PLATFORM:` reports
   `host: claude|codex (ORCHESTRATOR|DELEGATE)`.

2-1. **[Git project installation step]** Add these entries to project `.gitignore`.
   The repository's `.gitignore` has the same entries, ready to copy:
   ```
   .claude/settings.local.json
   .claude/.delegation-log.jsonl
   .claude/.preflight-status
   .claude/.mission-open/
   ```
   `settings.local.json` accumulates local absolute paths, usernames, scratch
   paths, and session lookup traces. Global gitignore protection does not travel
   with clones/drop-ins; the other entries are local hook evidence.
2-1a. Copy `docs/missions/README.md` to the same path. Git projects track
   `docs/missions/` and exclude only raw logs/temporary material. Non-Git projects also preserve Mission documents and exclude
   logs/local settings from sharing:
   ```gitignore
   docs/missions/**/logs/
   docs/missions/**/scratch/
   ```
2-2. Copy `docs/platform-notes-macos.md` and/or `docs/platform-notes-linux.md` for
   your platforms. They live outside rules; SessionStart points to them by OS.
2-3. Codex reads the shared AGENTS.md each run, including the delegate commit ban;
   keep it short. Global `AGENTS.override.md`/`AGENTS.md` in `$CODEX_HOME` (default
   `~/.codex`) precede project instructions; combined content truncates at
   `project_doc_max_bytes` (default 32KiB). Check these and global `config.toml`
   first if persistent constraints behave strangely.

2-4. Check [Project protection policy](runtime-boundary.md#project-protection-policy)
   for protected files and allowed external work, and reflect it in Project policy
   and host permissions. A ban on reading `.env.*` may include public examples;
   the current guard does not support force pushes.
   The default Project policy authorizes verified checkpoint commits without
   asking. At task completion, ask once and push on a yes; ask earlier only when
   the next verification needs the remote (CI, deployment). `permissions.allow`
   includes `Bash(git add:*)` and `Bash(git commit:*)`, but omits
   `Bash(git push:*)`. The explicit single-owner repository alternative in
   AGENTS.md also requires adding `Bash(git push:*)` to `permissions.allow`.
   Force push, history rewrite, tag/release/publish, branch or remote deletion,
   a new remote/upstream, and other external services still require asking first.
3. Project `.claude/settings.json` allow rules require workspace trust. Accept
   the first-session dialog for prompt-free codex/agy; until then,
   `Ignoring N permissions.allow entries ...` means the entire allow list is
   ineffective. `claude -p` cannot show the dialog; check smoke test B-3 for the
   warning. Hooks and `env` still apply before trust, preserving prohibitions.
4. Separate shared `settings.json` from local `settings.local.json`; exclude
   `.claude/settings.local.json` per step 2-1. Claude accumulates "Yes, don't ask again" rules each
   session, including paths, usernames, scratch paths, and searches/queries.

**Method B — user scope (shared across all projects)**
1. `.claude/settings.json` → `~/.claude/settings.json` (Windows:
   `%USERPROFILE%\.claude\settings.json`; merge keys).
2. Selected agents from the copy table → `~/.claude/agents/`; shared/optional
   hooks → `~/.claude/hooks/`. No need to move all maintenance tests. Keep
   launchers, bindings, and operating documents in the project per the copy table.
   Note: in user scope, `${CLAUDE_PROJECT_DIR}` in settings.json hook paths points
   to the project. Change script paths in hook `command`/`args` to the absolute
   user `.claude/hooks/deny_dangerous.py` path.
3. Copy `AGENTS.md.template` and `CLAUDE.md.template` per project as in Method A
   (user-scoped allow rules apply immediately without trust).

**Shared steps (after Method A/B)**
0-1. **[Git project line endings: no machine setting needed]** `.gitattributes`
   sets `* text=auto eol=lf` for repository/worktree, overriding `core.autocrlf`
   without machine changes. `git ls-files --eol` must show zero `w/crlf` entries;
   otherwise re-checkout from a clean tree with `git rm -q --cached -r . &&
   git reset -q --hard`. See `.gitattributes` comments for evidence.
0. **[Platform preflight]** First run
   `powershell -NoProfile -ExecutionPolicy Bypass -File check-windows-aliases.ps1`
   (Windows PS 5.1/7) or `bash check-posix.sh` (macOS/Linux, bash 3.2 compatible;
   accepts `--launchers`, `--template-dir`). Both are read-only: exit 0 healthy,
   1 warning. Fix exit 1 first; a dead hook invalidates prohibitions. Checks cover
   hook self-tests and agy grants; missing codex/agy binaries/write_file grants
   are INFO because fallbacks exist. Drift compares installed files and counts
   uninstalled ones separately, not installation completeness. See Windows-specific
   details for warning causes/replacement. Each session, codex-delegate repeats
   SANDBOX HEALTH PROBE / ALIAS CHECK, reporting only
   `CODEX_UNAVAILABLE: sandbox broken` or `ALIAS_WARNING`, never repairing.

1. Prepare selected external CLIs (versions are minima; check only for symptoms
   such as rejected flags, not every run):
   - Codex: `npm install -g @openai/codex`, then `codex login`
     (update: `npm install -g @openai/codex@latest`). Stale logon with desktop-owned
     `[windows] sandbox = "elevated"` in `~/.codex/config.toml` can cause
     `CreateProcessAsUserW failed: 1312`. Delegation always overrides with
     `-c windows.sandbox=unelevated`; use it manually for 1312 too. Do not edit
     global config.
   - Antigravity (only if selected): Install/login with agy on PATH. Headless
     `agy -p` auto-approval needs agy's own
     `~/.gemini/antigravity-cli/settings.json` (agy global settings), not harness
     `settings.json`. Minimum configuration:

     ```json
     {
       "permissions": { "allow": ["write_file(*)"] }
     }
     ```
     (Headless auto-approval requires only `permissions.allow`; `trustedWorkspaces`
     may relate only to the interactive UI trust dialog.) Launcher `agy-run.sh`
     reads this file: missing `write_file` grant enforces AGY_UNAVAILABLE; a
     `command(...)` grant enforces HARNESS_DENIED (passes only with task-specific
     `HARNESS_ALLOW_AGY_COMMAND=1` + user approval). Recipe:
     `.claude/skills/delegate-agy/SKILL.md`.

   - **agy grant conclusion** (agy 1.1.24): The only
     global grant is `write_file(*)`. Narrowing `command(<pattern>)` acts as total
     denial in this version; `agy --sandbox` blocks only commands and adds nothing
     after removing the grant. See `skills/delegate-agy` for evidence/probe methods.
2. Prepare a Claude Code CLI supporting the selected model. If model/effort is
   rejected, check support in that installation and use `claude update` if needed.
3. If native workers were installed, check that selected workers load in a new
   session. Confirm the main model/effort match the user's choice. The template
   does not fix them or automatically switch them for budget reasons.
4. **[Record installation provenance]** After installation/update, record source
   revision (commit hash or date), copied configuration (included/excluded
   options), and project changes (Project policy, local bindings, permissions,
   renamed files). Use a project-chosen location such as installed
   CLAUDE.md/AGENTS.md or `docs/`. This identifies future merge targets; no
   separate form, generator, or automatic comparison check is provided.

### Codex host (hook mirror and trust registration)

Establish Codex guards through `.codex/hooks.json`, sharing Claude's decision
files: SessionStart → `session_preflight.py`, PreToolUse →
`deny_dangerous.py --host codex`, Stop → `stop_gate.py --host codex`.
Writes arrive as `apply_patch` envelopes; see step 4 for delegate identity.

1. **Trust registration (once per machine, interactive)**: Start `codex` at the
   repository root and trust all four entries (SessionStart, PreToolUse,
   UserPromptSubmit, Stop) through `/hooks`. Registration is stored in
   `~/.codex/config.toml` as
   `[hooks.state.'<absolute hooks.json path>:<event>:0:0'] trusted_hash`.
2. **Detect Python during installation**: Run
   `bash .claude/scripts/gen-codex-hooks.sh --force`, then register trust. It tries
   `python3`, then `python`, validates Python 3 (also for `--python <exe>`), and
   writes absolute interpreter/project paths. Failure leaves definitions unchanged.
   Regenerate per machine, project move, Python path change, or distributed
   definition upgrade; retrust via `/hooks` even if previously trusted. Keep
   generated paths out of the distributable template. Defaults use `python3` on
   macOS/Linux and `python` via `commandWindows` on Windows. Installation-time
   detection avoids dependence on the desktop app's inherited shell PATH.

3. **Three layers of checks**:
   - `check-posix.sh` / `check-windows-aliases.ps1` check `.codex/hooks.json`
     events/registration entries, not hashes, enabled state, or execution.
     Retrust changed definitions separately. Both run `test_host_routes.py`,
     which detects missing `UserPromptSubmit -> stop_gate.py --new-prompt` wiring.
   - Before execution, `codex-run.sh` queries app-server `hooks/list`: every
     configured handler must be enabled/trusted for its current definition.
     Modified/untrusted/disabled/missing handlers, disabled hooks, unsupported APIs,
     and inconclusive queries exit 4. This checks configuration, not successful
     execution. Unguarded runs require explicit `HARNESS_ALLOW_UNTRUSTED_HOOKS=1`
     authorization.
   - After execution, only full `hook: <Event> Completed|Blocked|Failed` lines
     (LF/CRLF) count: `Failed` → `HOOKS_FAILED:`, missing → `HOOKS_UNKNOWN:`.
     Quotes/line numbers are excluded, but tools can print indistinguishable old
     status lines. This supplementary report proves neither execution, guard
     application, nor trust errors; it changes no exit codes or retries/checks.
     Pre-execution trust checks remain separate.

4. **Delegate identity**: `codex-run.sh` and `claude-run.sh` export
   `HARNESS_DELEGATE_RUN=1`; `codex exec`/`claude -p` are main sessions without
   `agent_id`, so this marker applies delegate commit/push/control-plane bans.
   Manual sessions lack it and orchestrate. Authorized
   `HARNESS_ALLOW_CONTROL_PLANE=1` lifts only control-plane protection, never
   commit/push bans; postflight cannot undo pushes.
   An outer project's `.claude/harness-config.json` may set
   `"content_dirs": ["template"]` to make its root `template/` content rather than
   live control plane; delegates may then edit it without a control-plane grant.
   The published default is `"content_dirs": []` (no exemption); missing, unreadable
   or invalid configuration exempts nothing. See the
   [configuration rules](runtime-boundary.md#content-directory-configuration). Open the outer
   project so hooks load from its `.claude/`; opening the copy activates its own
   protections. The realpath exception fails closed: no outside links/junctions,
   missing configured folders, or relative paths after anything except simple
   `cd <existing directory>` (excluded: `cd -`, `cd ~`, failed cd, after `||`).
   Ignore `cd` inside pipelines; roll back any `cd` effects in lists ending with `&`; and disable
   the exception after unclassified separators such as `;;`. Check raw and
   cwd-resolved paths against control-plane patterns.

5. **Stop gate**: `.claude/.stop-gate` is removed only after verification passes.
   If the marker remains after execution, both launchers report
   `STOP_GATE_UNSATISFIED` and `STATUS: FAILED(stop-gate unsatisfied…)`. The Codex
   Stop branch requests continuation only once, then stops (avoiding infinite
   loops); launcher checks prevent interpreting an unsatisfied gate as success.

Hook properties (Codex CLI 0.152.1/0.153.2):
- **Changing definitions breaks trust, silently.** Hook lines simply disappear
  without warnings: a dead guard and absent hook look identical in output.
  Always retrust through `/hooks` after editing `.codex/hooks.json`.
- **Hashes sign only definitions.** Target script contents such as `hook.py` are
  not signed; changing the script while keeping the definition executes new code
  with trust retained. Trust approves running this command line, not code safety.
- **Exit code 2 does not block in Codex.** For both PreToolUse and Stop, it logs
  `hook: … Failed` and continues (fail-open). Only JSON blocks
  (`permissionDecision:"deny"` or `decision:"block"`). This differs from Claude's
  exit 2 contract, so do not omit `--host`.
- **On Windows, hook commands run in pwsh.** Commands starting with quotes fail
  parsing. Leave paths without spaces unquoted; for paths with spaces, the
  generator also adds a call-operator form in `commandWindows`.
- Codex reads both `.codex/hooks.json` and the `.codex/hooks/` directory form.
  Both are treated as control plane.

### Windows-specific details

**This section is authoritative** for warning causes and permanent replacement
procedures.

**Store pwsh issue**: If the machine's only pwsh is a Microsoft Store alias
(`...\Microsoft\WindowsApps\pwsh.exe`, special MSIX ACL), the codex sandbox's
restricted token cannot execute that shell. **All sandbox modes** fail with
`CreateProcessAsUserW failed: 5` (unelevated) / `1312` (elevated); only native
apply_patch writes, `-i` images, and danger-full-access work (upstream
openai/codex #22880/#26803/#26896). The permanent solution is to replace Store
PowerShell with the official GitHub release:
1. Install PowerShell 7 with normal ACLs: as administrator,
   `winget install Microsoft.PowerShell --scope machine`; otherwise unpack the
   GitHub release ZIP into `%LOCALAPPDATA%\Programs\pwsh7` and prepend that folder
   to USER Path (winget without `--scope machine` may reinstall Store MSIX).
2. Remove the Store package:
   `Get-AppxPackage Microsoft.PowerShell | Remove-AppxPackage`
3. Delete the dead execution alias left after removal,
   `%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe`. While it remains, anything
   finding pwsh through PATH hits the dead alias.
Note: perform removal outside active work; a shell started through the Store
alias dies on its next spawn if removed during work. UTF-8 output is clean only
in pwsh 7 (powershell 5.1 fallback garbles CJK). For intermittent 1312 from stale
logon, restart the Codex desktop app or reboot.

**python3 alias issue**: **On Windows, use `python` or `py`, not `python3`.**
**App Installer updates regenerate alias stubs**, so deletion is temporary.
Some versions omit Python from the Settings app execution alias list. Durable
fix: **create `python3.exe` in the actual Python folder and prioritize its PATH**:
`copy %LOCALAPPDATA%\Programs\Python\Python3XX\python.exe
%LOCALAPPDATA%\Programs\Python\Python3XX\python3.exe`. Regenerated stubs remain
lower priority (`python3` verified with 3.12.10). Diagnose recurrence with
`check-windows-aliases.ps1`; retain `python`/`py` in delegate prompts for machines
without this shim.

**Verification failures inside the sandbox**: Python 3.14 `TemporaryDirectory`
access and Git Bash `CreateFileMapping` can fail with permission errors in Codex
`workspace-write` even with normal PowerShell. Changing the temporary location
did not help; parent checks passed. This observation is environment-specific.
Return changes and the failed command to the parent, who decides whether existing
permissions allow verification. Workers must not change ACLs/sandbox settings or
repeatedly clean temporary files.

### macOS

SessionStart points to `docs/platform-notes-macos.md` for reading before work. Launchers
require bash 4 or later and GNU coreutils `timeout`; hooks require unversioned `python`. Launchers started under bash 3.2 re-exec into a Homebrew bash, so PATH order does not
matter for them. Install commands and why a `gnubin` entry is optional are in
[Dependencies](dependencies.md#bash--4-and-gnu-timeout). `check-posix.sh` detects these
three items. The support table classifies macOS as partially verified.

### Linux (partial verification by environment and item)

SessionStart points to `docs/platform-notes-linux.md`. Install `python-is-python3` for
hooks and run `check-posix.sh`. Hooks/launchers have verification records on Ubuntu
24.04 LTS aarch64. The isolation lane is unavailable on this native host. This does not
imply verification of other distributions/architectures; platform notes are
authoritative for per-item observations and unverified scope.

### WSL2

Follow [WSL2 isolation lane usage](harness-launchers.md#wsl2-isolation-lane-usage-optional-execution-environment-for-untrusted-content) and the lane-sensitive entry section. Clones under
`/mnt/` need `safe.directory`; `check-posix.sh` prints the exact command. Enter with
`wsl -u <user> bash -lc`.

## Checks after CLI updates

Check installation with `check-windows-aliases.ps1` on Windows or `check-posix.sh` on
POSIX. Normal project work uses that project's verification commands. If changing the
harness itself or CLI integration, select affected checks from the template repository's
[Maintenance guide](harness-maintenance.md). A CLI update alone does not require full
regressions/paid delegation every time.

For Codex shell execution failures, the optional `check-codex-sandbox.sh` tool can
diagnose the issue. This diagnostic calls a real model. After hook definition changes,
the user retrusts through `/hooks`. Necessary real delegation checks must also inspect
file changes and verification results.
