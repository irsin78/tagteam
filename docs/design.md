# tagteam design

Why the harness is shaped as it is, what it leaves out on purpose, and where it is known to be incomplete. Operation
(commands, flags, exit codes, recovery): [manual.md](manual.md). The rules agents follow, and the term table:
[AGENTS.md.template](../AGENTS.md.template).

## What is where

| Section | Answers |
|---|---|
| [1. Problem](#1-problem) | Which failures does tagteam address? |
| [2. Principles](#2-principles) | What rules guided the design (enforce in code, honest about strength, measure then rely, ...)? |
| [3. Scope](#3-scope) | What does it do, and what does it deliberately not do? |
| [4. Components](#4-components) | Which file does what? |
| [5. Enforcement](#5-enforcement) | What does the hook enforce, and what can it not stop? What does it stop on each host (5.1)? How do the hosts share it (5.2)? |
| [6. Roles and sessions](#6-roles-and-sessions) | Why a session pool, why the marker in the first prompt, why one session per role? |
| [7. Grading and routing](#7-grading-and-routing) | How are grades defined (7.1)? Why is the choice ordered as it is (7.2)? Why is a review reported incomplete instead of reviewed by the author's vendor (7.3)? |
| [8. Model-compensation rules](#8-model-compensation-rules) | What is a `[fix: ...]` rule and when is it removed? |
| [9. Model list maintenance](#9-model-list-maintenance) | Why is the weekly update a script and not a model? |
| [10. Known gaps](#10-known-gaps) | What is unverified, unsupported or accepted as weak? |

## 1. Problem

A developer who uses several coding agents (Claude Code, Codex, optionally Gemini through `agy`) runs into the same
failures:

- A delegated session inherits the orchestrator's model by default, or does not run at all, so the model in use is not
  the model that was meant.
- Workers commit, push or start further workers on their own.
- A review by the same vendor that wrote the change shares its blind spots.
- Model names, efforts and availability change every few weeks, and per-model notes in prompts go stale.
- Instructions that only say "do not" are followed unevenly.

tagteam addresses these with a few mechanisms and keeps the rest as plain text a person can read in minutes.

## 2. Principles

1. **One instruction file.** `AGENTS.md` is read by Codex directly and by Claude Code through an `@AGENTS.md` import.
   Skills hold procedures; they do not redefine terms.
2. **Enforce in code what text cannot guarantee.** Model and effort on new sessions, no worker commits, no worker
   delegation: one hook. Everything else is guidance.
3. **Honest about strength.** The hook and the launcher are accident checks. They catch the plain forms of a mistake;
   they are not a security boundary (section 5).
4. **Measure, then rely.** Platform behaviour used by the design (what a hook sees, whether a turn may open sessions,
   how the cache behaves) was tested on real installs; a limit of one host version carries a `[host: ...]` tag in the
   instructions so it is re-tested after an update. What the tests could not confirm is a gap (section 10).
5. **Grades per model and effort.** The same model is a different tool at `low` and at `max`. A grade names a (model,
   effort) pair, and the router picks within a grade.
6. **No language model in the update path.** Collecting model lists is a script; a skill applies the changes.
7. **Fail open on internal errors, fail closed on rules.** A bug in the hook must not lock a session; a rule violation
   is denied.
8. **Rules that compensate for a model are labelled and removable** (section 8).

## 3. Scope

| Does | Does not (by decision) |
|---|---|
| Installs one rule file, one hook, two skills, worker definitions and the scripts into a project, and updates them without touching project policy or machine-local files | No daemon, server, GUI or message relay between sessions (each host's own follow-up tools are used) |
| Chooses model and effort per role, substitutes another vendor when one is exhausted, keeps review independent of the author's vendor | No automatic change to `models.json`; a person or the orchestrator applies a proposal |
| Runs a worker through another vendor's CLI and verifies the model that answered | No cost accounting; `grading.caps` are relative ceilings used to order candidates, not budgets |
| Proposes model-list and benchmark updates on a weekly schedule | No sandbox or security boundary against a hostile worker |
| | No adapter for CLIs of other companies as workers (section 10), no uninstaller, no multi-user coordination beyond `Push mode` in AGENTS.md |

## 4. Components

Installed locations: [README](../README.md#what-is-in-this-repository). The scripts use only the Python standard library and
need Python 3.9 or newer. They share one state folder, `<main work tree>/.tagteam/`, whose location only `state.py`
computes.

| Component | Source | Role |
|---|---|---|
| Rules | `AGENTS.md.template` | roles, cycle, verification, terms; `## Project policy` is project-owned |
| Claude import | `CLAUDE.md.template` | `@AGENTS.md` |
| Hook | `harness/tagteam/role_guard.py` | role record and denials (Claude settings and Codex hooks both call it, section 5.2) |
| State | `harness/tagteam/state.py` | locates, creates and migrates the state folder; every script asks it |
| Router | `harness/tagteam/route.py` | model, effort and launch method per role |
| Launcher | `harness/tagteam/run.py` | one worker through another vendor's CLI, model check, exhaustion detection, git check |
| Collector | `harness/tagteam/models_collect.py` | model lists, benchmark values, proposal file |
| Data | `harness/tagteam/models.json` | vendors, roles, models, grades, defaults |
| Skills | `harness/skills/` | `cycle` (its `hosts.md` holds the per-host worker handling), `model-update`; installed to `.claude/skills` and `.agents/skills` |
| Worker definitions | `harness/claude/agents`, `harness/codex/agents` | fixed model and effort, limited tools |
| Installer | `install.py` | install, update, check, repair Codex hooks, schedule (not installed) |

## 5. Enforcement

Every row is an accident check. The right-hand column is what it does not stop.

| Rule | Mechanism | What it does not stop |
|---|---|---|
| New session needs `model` and `effort` | PreToolUse on `mcp__ccd_session__start_session`: deny unless both are set; a call carrying only a `proposal_id` is allowed | any session opened through another tool |
| Workers do not commit or push | PreToolUse on shell tools: deny when the command matches `git commit` or `git push` in plain forms, including after `git -C <path>` or other leading options | aliases, variables, scripts, other tools |
| Workers do not delegate | PreToolUse: deny `start_session`, `Agent`, `spawn_agent` for a worker | a worker using another route to a CLI |
| Workers do not edit the control plane | PreToolUse on the edit tools (Claude) and on `apply_patch` (Codex): deny a path in `.claude/`, `.codex/`, `.agents/`, `AGENTS.md`, `CLAUDE.md` | writes through a shell, scripts |
| Codex agents get a model and effort | PreToolUse on `spawn_agent`: a custom agent must omit them (its definition fixes them), any other type needs both | an agent opened by another tool |
| Launcher workers do not launch workers | `run.py` refuses when `HARNESS_DELEGATE_RUN=1` | a session worker (the hook covers it, not the launcher) |
| Worker is identified | any line of the first prompt starting with `HARNESS ROLE: DELEGATE`, or `HARNESS_DELEGATE_RUN=1`, or running as a subagent; recorded once per session | tasks sent later by message cannot add the marker |
| Read-only reading | the `reader` definition lists no edit tools and tells the worker to run only read-only commands | its Bash tool is limited by instruction, not by the hook |
| Launcher commits and read-only writes | `run.py` compares local HEAD, branch, status and file hashes before and after a run (manual section 7) | pushes, ignored files, paths outside the repository, stashes, other refs; changes by other writers in the same checkout are seen too and can be blamed on the run |

Notes:

- The hook runs for every session of the project, workers included, because the checkout is shared. The first prompt
  decides the role, and the launcher adds the marker itself; the marker may sit on any line because the desktop app
  prepends a background paragraph to a worker's first prompt.
- Internal errors in the hook exit 0 with a warning on stderr (fail open). A hook that cannot start at all, because
  no Python is found, is a different failure and fails closed where the host allows it (section 5.2).
- Claude Code settings reference the hook through `${CLAUDE_PROJECT_DIR}`. Codex hook commands run in the session's
  working directory, so the installer writes the guard's absolute path into `.codex/hooks.json` once and then keeps the
  file. Codex ties its trust to that path and to the hook definition, so the file is not rewritten on update (what to do
  after a move: manual section 1.2).
- Review independence rests on the router (`--author-vendor`) and the orchestrator's duty to use it. There is no hook
  for it.

### 5.1 What is stopped on each host

**stops**: a real worker was refused in a measured run. *test*: refused in handler tests (fake payloads), not yet seen
in a real run. **after**: noticed after the run. **no**: not stopped.

| Action | Claude Code | Codex CLI | agy |
|---|---|---|---|
| Worker runs `git commit` (plain forms) | **stops** (real worker, Windows 2.1.327); macOS 2.1.326: the old hook let it through without Python, re-check of the fix pending | **stops** (real worker, 0.169.0 Windows, after repair and trust) | no hook installed; for a `run.py` worker a commit or branch move is **after** (`git_head_moved`) |
| Worker runs `git push` (plain forms) | *test* | *test* | no hook installed; **no**: the git check does not see a push |
| Worker opens a session or a subagent | *test* | **stops** (`spawn_agent`) | no hook installed |
| Worker edits the control plane | *test* (edit tools); shell writes **no** | **stops** (`apply_patch` on `AGENTS.md`); shell writes **no** | no hook installed |
| Orchestrator opens a session without model and effort | *test* (`start_session`); other tools **no** | *test* (`spawn_agent`) | no hook installed |
| `run.py` worker starts another `run.py` worker | `run.py` refuses (test), on every host; a session worker calling `run.py` is not stopped (translation needs it) | same | same |
| `run.py` read-only run changes the work tree | **after** (git check, `wrote_files`), on every host; a concurrent change by another writer can be blamed on the run, so use a quiet checkout or a separate worktree; ignored files and other refs are not seen | same | same |

Codex hooks protect only after the user has trusted them in `/hooks` (a changed entry is skipped silently). agy has no
adapter yet; routing keeps it a one-shot launcher worker.

### 5.2 One guard, several hosts

- **Adapter per host.** `role_guard.py --host claude|codex` turns the host's payload into one internal form, applies
  one set of rules, and answers in the host's format: exit 2 with the reason on stderr for Claude Code, a JSON
  `permissionDecision: deny` with exit 0 for Codex. Codex names its tools without a separator
  (`collaborationspawn_agent`), tags a subagent's calls with `agent_id` (so a subagent is always a worker), and sends no
  SessionStart or UserPromptSubmit for it, so a subagent cannot overwrite the root session's role. A SubagentStart hook
  records the model a subagent really runs on in `agents.jsonl`.
- **Interpreter chosen at run time, closed if absent.** Claude Code runs the hook commands in `settings.json` with `sh`
  (Git Bash on Windows, measured). The shell tries `python3`, then `python`, by running each (so a Windows Store stub or a
  broken shim is skipped), and `exec`s the guard so its exit code is not masked. With no Python, PreToolUse exits 2 with a
  message (tool calls stay blocked until Python is installed); SessionStart and UserPromptSubmit warn and continue, so the
  user can still repair it. A bare `python` that is missing exits 127, which Claude Code reads as "no objection": measured
  on a stock macOS, and why the commands look like this. Codex hooks name `python3` (`python` on Windows) directly and do
  not run without it; `--check` says so.
- **One state folder, outside `.git`**, because Codex sandboxes cannot write there. **One table for host handling**:
  `AGENTS.md` and `cycle` name only host-neutral steps; how each host does them is in `hosts.md` beside the skill.

## 6. Roles and sessions

The orchestrator is the session you commanded; every other session is a worker. Three measured facts shaped the cycle; their
operational consequences are in [manual section 3.1](manual.md#31-the-session-pool-and-automatic-turns):

- A turn started by a worker's report cannot open sessions, but it can message an existing one. Hence the **session
  pool**: open the sessions a phase needs on a user turn, reuse them by message.
- A message sent to a session does not pass the prompt hook. Hence the marker belongs in the first prompt.
- The prompt cache of a reused session lasts one hour (`subagentPromptCacheTtl` is set to `1h` for subagents), and a
  fresh session costs on the order of 90,000 tokens before any work. Hence one session per role, kept for the mission.

Independent units may run in parallel, sharing the main checkout unless their files or checks would collide (then a
separate git worktree); without git, one at a time. The mission folder (`intent.md`, `spec.md`, `status.md`, design
documents) survives compaction or a new orchestrator session.

## 7. Grading and routing

How a choice is made, step by step, and the role-to-grade table: manual [section 5](manual.md#5-the-router). This section
keeps the reasons.

### 7.1 Grades

`models.json` stores, for each model, a grade for every effort it offers. The rule used to produce them is applied to the
Artificial Analysis index at that effort:

| Grade | Index at least | Relative cost ceiling per task (USD) |
|---|---|---|
| S | 54 | 10 |
| A | 48 | 4 |
| B | 30 | 2 |
| C | below B | 0.2 |

The floors and ceilings are `grading.floors` and `grading.caps`, our own settings, not vendor figures. The benchmark values
themselves live only in the machine-local `bench.local.json` and are not committed. `grades` are the committed result of
applying the floors, so routing works without benchmark data; the collector reports `grade_changed` when fresh values would
give another grade.

### 7.2 Why the choice is ordered this way

- Only a benchmark value at exactly the candidate's effort is used, because values at other efforts would skew a
  comparison.
- The order of models in `models.json` carries meaning: earlier means stronger within a vendor. The router uses it to
  keep a native Claude session from outranking its orchestrator and as the last tie-break. When the orchestrator's model or
  effort is exceeded, the launch moves to `run.py`.
- A candidate is usable only if the plan allows the vendor, the model is not hidden, and the vendor CLI lists it. The
  `available` lists are written by the collector; a vendor with no list is assumed to offer everything, which keeps an
  install usable before its first collection.

### 7.3 Exhaustion

A usage-limit message from a run, or a manual mark, writes the vendor and an `until` time to `exhausted.json` (manual
section 5.3); the router takes the same grade from the next vendor. If no other vendor can review, the review is
reported as incomplete (router exit 3) instead of relaxing independence.

## 8. Model-compensation rules

A rule written for one model's repeated mistake ends with `[fix: <reason> / remove when <condition>]`; the command that
lists them is in manual [section 10](manual.md#10-model-compensation-rules). Add a rule only after the mistake has been
seen, with its removal condition. After a model change (the `model-update` skill lists them when the default route moves),
test each condition and delete the rules whose condition holds. A rule that fits no single model has no tag. Old caution
does not accumulate: a rule nobody can explain is not kept "just in case".

## 9. Model list maintenance

Commands and the apply procedure: manual [section 8](manual.md#8-keeping-the-model-list-current). Weekly, a scheduled script
asks each vendor CLI for its model list and, with an Artificial Analysis API key, fetches scores and prices. It writes a
proposal and nothing else; the `model-update` skill applies each change kind by a fixed rule (automatic for availability
and benchmark values; held for deletions and for a default-route change that lacks two agreeing sources, a probe run and
passing route tests). So the update path needs no model, the collector cannot change a route, and only `models.json`
changes. Claude Code lists model families, not versions. Data from Artificial Analysis must be credited; the README and the
collector carry the notice.

## 10. Known gaps

| Gap | Effect | Status |
|---|---|---|
| No `run.py` adapter for CLIs of other companies (for example xAI) | such models cannot be launched as workers; the collector can still compare their benchmarks through `aa_creator` | open |
| Claude `/model` output differs by plan | availability was verified on one plan only | open |
| macOS: measured on one machine before the interpreter and launchd fixes; Linux: nothing run | the fixes are verified on Windows only | open; [platform/macos.md](platform/macos.md) lists what to re-check, [platform/linux.md](platform/linux.md) is a checklist |
| agy has no hook adapter | on an agy host, and for agy workers, nothing is stopped; the git check after a `run.py` run (commits and branch moves, not pushes) is the only net | open |
| Git detection of commits is textual | a worker can evade the hook with indirection | accepted; review the diff |
| The hook does not know the vendor of a reviewer | independence depends on the orchestrator using the router | accepted |
| Codex: commits and `run.py` launches need the user's approval (6 prompts in one small mission); whether a finishing subagent starts an orchestrator turn is unconfirmed | more interaction than on Claude Code; the orchestrator waits for its subagents in the same turn | accepted; [hosts.md](../harness/skills/cycle/hosts.md#codex-notes) |
| `start_session` is the Claude Code desktop app's session tool | the session pool needs it; elsewhere, native subagents and the launcher remain | accepted |
| Hook recovery needs git | a broken hook is repaired outside Claude Code | see manual section 9.1 |

Windows measurements: [platform/windows.md](platform/windows.md).
