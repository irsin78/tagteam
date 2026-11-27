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
4. **Measure, then rely.** Platform behaviour the design uses (what a hook sees, whether a turn may open sessions, how the
   cache behaves) is checked on real installs; a limit of one host version carries a `[host: ...]` tag in the instructions
   so it is re-tested after an update. What cannot be confirmed is a gap (section 10).
5. **Grades per model and effort.** The same model is a different tool at `low` and at `max`. A grade names a (model,
   effort) pair, and the router picks within a grade.
6. **No language model in the update path.** Collecting model lists is a script; a skill applies the changes.
7. **Fail open on internal errors, fail closed on rules.** A bug in the hook must not lock a session; a rule violation
   is denied. On agy a PreToolUse hook that does not answer blocks the tool; the guard therefore answers `ask` (the
   user's permissions decide) on internal errors, and only a guard that cannot start fails closed.
8. **Rules that compensate for a model are labelled and removable** (section 8).

## 3. Scope

| Does | Does not (by decision) |
|---|---|
| Installs one rule file, one hook, two skills, worker definitions and the scripts into a project, and updates them without touching project policy or machine-local files | No daemon, server, GUI or message relay between sessions (each host's own follow-up tools are used) |
| Chooses model and effort per role, substitutes another vendor (or an agy-served stand-in) when a quota is exhausted, keeps review independent of the author's maker | No automatic change to `models.json`; a person or the orchestrator applies a proposal |
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
| Hook | `harness/tagteam/role_guard.py` | role record and denials (Claude settings, Codex hooks and agy hooks all call it, section 5.2) |
| agy hook wiring | `harness/agy/hooks.json` | the `tagteam` key of `.agents/hooks.json`, written by the installer with this machine's paths |
| State | `harness/tagteam/state.py` | locates, creates and migrates the state folder; every script asks it |
| Router | `harness/tagteam/route.py` | model, effort and launch method per role |
| Launcher | `harness/tagteam/run.py` | one worker through another vendor's CLI, model check, exhaustion detection, git check |
| Collector | `harness/tagteam/models_collect.py` | model lists, benchmark values, proposal file |
| Data | `harness/tagteam/models.json` | vendors, roles, models, grades, defaults, and an `orchestrators` block for a model that can only orchestrate (agy's built-in one): the router compares it as a stated model and effort, and the launcher never starts it |
| Skills | `harness/skills/` | `cycle` (its `hosts.md` holds the per-host worker handling), `model-update`, `tagteam-update` (user-invoked); installed to `.claude/skills` and `.agents/skills` |
| Worker definitions | `harness/claude/agents`, `harness/codex/agents` | fixed model and effort, limited tools |
| Installer | `install.py` | install, update (merges only tagteam's Claude hooks, records the install source and version), check, repair Codex hooks, schedule (not installed) |
## 5. Enforcement

Every row is an accident check. The right-hand column is what it does not stop.

| Rule | Mechanism | What it does not stop |
|---|---|---|
| New session needs `model` and `effort` | PreToolUse on `mcp__ccd_session__start_session`: deny unless both are set; a call carrying only a `proposal_id` is allowed | any session opened through another tool |
| Workers do not commit or push | PreToolUse on shell tools (agy: `run_command`): deny when the command matches `git commit` or `git push` in plain forms, including after `git -C <path>` or other leading options | aliases, variables, scripts, other tools |
| Workers do not delegate | PreToolUse: deny `start_session`, `Agent`, `spawn_agent` and agy's `invoke_subagent` for a worker | a worker using another route to a CLI |
| Workers do not edit the control plane | PreToolUse on the edit tools (Claude), on `apply_patch` (Codex) and on `write_file` and `edit_file` (agy): deny a path in `.claude/`, `.codex/`, `.agents/`, `AGENTS.md`, `CLAUDE.md` | writes through a shell, scripts |
| Codex agents get a model and effort | PreToolUse on `spawn_agent`: a custom agent must omit them (its definition fixes them), any other type needs both | an agent opened by another tool |
| Launcher workers do not launch workers | `run.py` refuses when `HARNESS_DELEGATE_RUN=1` | a session worker (the hook covers it, not the launcher) |
| Worker is identified | any line of the first prompt starting with `HARNESS ROLE: DELEGATE`, or `HARNESS_DELEGATE_RUN=1`, or running as a subagent (agy: a first model call with no user input); recorded once per session | tasks sent later by message cannot add the marker |
| Read-only reading | the `reader` definition lists no edit tools and tells the worker to run only read-only commands | its Bash tool is limited by instruction, not by the hook |
| Launcher commits and read-only writes | `run.py` compares local HEAD, branch, status and file hashes before and after a run (manual section 7) | pushes, ignored files, paths outside the repository, stashes, other refs; changes by other writers in the same checkout are seen too and can be blamed on the run |
| Launcher workers do not change the control plane; Codex hooks ran | `run.py` compares, before and after, the content hash of every control-plane path `git status` lists and of the machine-local hook and setting files that git ignores: a write run that changed a control-plane path or a hook file ends `control_plane_changed` (the setting files and the main checkout of a worktree only warn). After a clean Codex run it looks for the role record the hooks write: none ends `hooks_not_run` (manual section 7) | a change made and undone inside the run; ignored files outside those lists; the orchestrator or another process writing in the same checkout is blamed on the run; the Codex hook check in a linked worktree is confirmed for a trusted project only |

Notes:

- The hook runs for every session of the project, workers included, because the checkout is shared. The first prompt
  decides the role, and the launcher adds the marker itself; the marker may sit on any line because the desktop app
  prepends a background paragraph to a worker's first prompt.
- Internal errors in the hook exit 0 with a warning on stderr (fail open; on agy the answer is `ask`). A hook that
  cannot start at all, because no Python is found, is a different failure and fails closed where the host allows it
  (section 5.2).
- Claude Code settings reference the hook through `${CLAUDE_PROJECT_DIR}`. Codex hook commands run in the session's
  working directory, so the installer writes the guard's absolute path into `.codex/hooks.json` once and then keeps the
  file. Codex ties its trust to that path and to the hook definition, so the file is not rewritten on update (what to do
  after a move: manual section 1.2).
- Review independence (by maker) rests on the router (`--author-vendor`) and the orchestrator's duty to use it. There is no hook
  for it.

### 5.1 What is stopped on each host

**stops**: blocked by the hook. *test*: blocked in handler tests (fake payloads) only; the real path is unconfirmed.
**after**: noticed after the run (only for workers started by `run.py`). **no**: not stopped.

| Action | Claude Code | Codex CLI | agy |
|---|---|---|---|
| Worker runs `git commit` (plain forms) | **stops** (with no Python on PATH every tool call is blocked) | **stops**. In a project Codex does not trust the hooks are skipped and the commit can pass where a user rule allows it: `git_head_moved` (exit 7) catches the commit **after**, and wins over `hooks_not_run` (11), which is the status when the run ended cleanly with no HEAD move | **stops** (main checkout, worktree root and a subfolder of it; an orchestrator's subagent is recorded as a worker by its empty first input); a `run.py` worker's commit or branch move is also **after** (`git_head_moved`) |
| Worker runs `git push` (plain forms) | *test* | *test* | **stops** |
| Worker opens a session or a subagent | *test* | **stops** (`spawn_agent`) | **stops** for `invoke_subagent`; *test* for `define_subagent`, `run_workflow` and `swarm` (names from agy's change log) |
| Worker edits the control plane | *test* (edit tools); shell writes **no**; **after** (`control_plane_changed`, `run.py` workers) | **stops** (`apply_patch` on `AGENTS.md`); shell writes **no**; **after** (`control_plane_changed`, `run.py` workers) | **stops** (`write_file` and `edit_file` on the control plane, in the main tree, a linked worktree and a worktree subfolder; ordinary edits run; agy 1.5.12 headless on Linux and Windows); shell writes **no**; **after** (`control_plane_changed`, `run.py` workers) |
| Orchestrator opens a session without model and effort | *test* (`start_session`); other tools **no** | *test* (`spawn_agent`) | not applicable: agy has no session tool, and `invoke_subagent` takes only a model tier, so harness roles use `run.py` (a rule, not a hook) |
| `run.py` worker starts another `run.py` worker | `run.py` refuses (test), on every host; a session worker calling `run.py` is not stopped (translation needs it) | same | same |
| `run.py` read-only run changes the work tree | **after** (git check, `wrote_files`), on every host; a concurrent change by another writer can be blamed on the run, so use a quiet checkout or a separate worktree; ignored files and other refs are not seen | same | same |

Codex hooks protect only after the user has trusted the project and then the hooks in `/hooks` (a changed entry, or an
untrusted project, is skipped silently; `run.py` and `install.py --check --host codex` report it). agy hooks are
machine-local (`.agents/hooks.json`); `run.py` copies them into a worktree it runs a google worker in. A PreToolUse hook
failure blocks the tool on agy.

[host: Claude Code 2.1.326 and 2.1.327 Windows, Linux and macOS; codex-cli 0.170.0 Windows and macOS; agy 1.5.11 and 1.5.12
Windows and Linux; measured 2026-11-18 and 2026-11-19 / re-test after a host update]

### 5.2 One guard, several hosts

- **Adapter per host.** `role_guard.py --host claude|codex|agy` turns the host's payload into one internal form, applies
  one set of rules, and answers in the host's format: exit 2 with the reason on stderr for Claude Code, a JSON
  `permissionDecision: deny` with exit 0 for Codex. Codex names its tools without a separator
  (`collaborationspawn_agent`), tags a subagent's calls with `agent_id` (so a subagent is always a worker), and sends no
  SessionStart or UserPromptSubmit for it, so a subagent cannot overwrite the root session's role. A SubagentStart hook
  records the model a subagent really runs on in `agents.jsonl`.
- **The agy adapter.** agy wants exactly one JSON object on stdout for every event, and blocks a PreToolUse hook that
  prints nothing, so every path ends in `{"decision":"ask"}` (no opinion: the user's permission rules decide),
  `{"decision":"deny","reason":...}` or, for the other events, `{}`; the exit code is always 0 and an internal error also
  answers `ask`. The role is not decided at SessionStart (an interactive session has no user input yet, and a resumed
  conversation has no SessionStart): the first model call (PreInvocation, or the first PreToolUse of a conversation
  without a record) decides it. A launcher variable or a marker line makes a worker; an empty input makes a worker
  too, because an agy subagent starts that way; anything else is the orchestrator. If that first decision was lost
  or the record is unreadable, the role is `unresolved` and protected calls get a worker's rules. The matcher is
  `run_command`, the delegation tools and the two edit tools (`write_file`, `edit_file`; the guard judges their `Path`
  with the same control-plane rule as the other hosts). A worker's or an unresolved role's edit of the control plane
  is denied [host: agy 1.5.12 Linux headless, measured 2026-11-19 / re-test after an agy update], every other edit is
  `ask`, and the orchestrator's edits are `ask` too. With allow rules for both tools, `ask` on an edit adds no approval
  prompt and does not stop the edit [host: agy 1.5.12 Windows interactive, measured 2026-11-19 / re-test after an agy
  update]; on other platforms, and without allow rules (for example `request-review` and an empty list), the effect of
  `ask` is unconfirmed. The hook never answers `allow`. The price is that
  a guard that cannot start blocks the edit tools as well as shell commands and delegation, so agy cannot repair its own
  hooks from the inside; recovery is from a terminal or another editor (manual section 1.3). The wiring is
  `.agents/hooks.json`, machine-local because the guard path is absolute; the installer owns only its `tagteam` key. The guard also appends a line to
  `agy-models.jsonl` whenever a conversation's model changes, since the execution half of the Orchestrator model may
  be a weaker model than the planning half (section 10).
- **Interpreter chosen at run time, closed if absent.** Claude Code runs the hook commands in `settings.json` with `sh`
  (Git Bash on Windows). The shell tries `python3`, then `python`, by running each (so a Windows Store stub or a
  broken shim is skipped), and `exec`s the guard so its exit code is not masked. With no Python, PreToolUse exits 2 with a
  message (tool calls stay blocked until Python is installed); SessionStart and UserPromptSubmit warn and continue, so the
  user can still repair it. A bare `python` that is missing exits 127, which Claude Code reads as "no objection"; that is why
  the commands look like this. Codex hooks name `python3` (`python` on Windows) directly and do
  not run without it; `--check` says so. The agy commands name one interpreter, chosen when the machine-local file is
  written (`python` on Windows, else `python3` when it exists), and `--check --host agy` looks for it on PATH.
- **Project hooks stay.** The installer owns tagteam's hook handlers, not the hook file: it removes the handlers that are
  exact guard calls and puts the template's back, so a project's own hooks survive an update (manual section 1). Claude Code runs
  the hooks of one event in parallel and a deny wins; the guard only denies, so a project hook can neither lift its deny nor be
  lifted by it. A project hook that rewrites tool input is the one case where the guard may judge a different command than
  runs (section 10).
- **Updates are the user's act.** `tagteam-update` moves the checkout by fast-forward, installs, checks and commits, and runs
  only on request and never while `run.py --running` finds a run. Its commit is exempt from cross-vendor review only when
  the installer's own check passed on every host the project uses (AGENTS.md section 4). The session-start notice compares
  local commits only.
- **One state folder, outside `.git`**, because Codex sandboxes cannot write there. **One table for host handling**:
  `AGENTS.md` and `cycle` name only host-neutral steps; how each host does them is in `hosts.md` beside the skill.

## 6. Roles and sessions

The orchestrator is the session you commanded; every other session is a worker. Three facts shape the cycle; their
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
| S | 57.85 | 10 |
| A | 49.05 | 4 |
| B | 32.5 | 2.5 |
| C | below B | 0.2 |

The floors and ceilings are `grading.floors` and `grading.caps`, our own settings, not vendor figures. Each floor sits in the middle of the empty stretch between two neighbouring registered cells (S only above the top group), so a small rescoring by the benchmark does not flip a grade. The A floor sits above Claude Opus at medium effort and the B ceiling admits its cost, so that cell is the Claude implementation pick (grade B). The benchmark values
themselves live only in the machine-local `bench.local.json` and are not committed. `grades` are the committed result of
applying the floors, so routing works without benchmark data; the collector reports `grade_changed` when fresh values would
give another grade.

### 7.2 Why the choice is ordered this way

- Only a benchmark value at exactly the candidate's effort is used, because values at other efforts would skew a
  comparison.
- The order of models in `models.json` carries meaning: earlier means stronger within a vendor. The router uses it to
  keep a native Claude session from outranking its orchestrator and as the last tie-break. When the orchestrator's model or
  effort is exceeded, the launch moves to `run.py`.
- A candidate is usable only if the plan allows the vendor and the model (`min_plan`: a model above the vendor's plan is never
  routed), the model is not hidden, and the vendor CLI lists it. The
  `available` lists are written by the collector; a vendor with no list is assumed to offer everything, which keeps an
  install usable before its first collection.
- Plans are an optional override, not an installer question: availability decides, and the router infers only what it can
  read. agy's list with the id of an `ultra` model means the Ultra plan, one with a Pro-class id (from `models.json`, not names in
  code) Pro, any other the free tier, so google goes last (not as the host vendor: see below). With no fresh list google is taken as free too: an unknown list must
  not let a Pro account be routed to a model only Ultra offers (Carbon). Claude and Codex lists carry no such signal and are
  never inferred (no plan named means Pro). A named plan always wins.
- agy's built-in Orchestrator model has no effort and no grade of its own. The router compares it as a stated pair
  (`compare_as` in `orchestrators`, an assumption the notice repeats), and refuses an effort given with it.
- On `--host agy` a review is raised to at least grade A: the execution half of that model may be weaker than the
  planning half, and nothing else watches its comparison of reports with diffs. A pinned reviewer below A is
  replaced, and a task preference is judged against the raised grade.
- A vendor may set `max_effort` (an effort word) in `models.json`, and the router then drops that vendor's cells above it from
  the candidates. The reason is quota: a subscription is spent by effort (how long the model reasons), while
  the choice by grade and cost reads the benchmark's cost per task, which can be low for a model that reasons a lot. Without the
  limit the rule "best index within the cost cap" can keep choosing the top efforts of a cheap-per-task model and use up the
  quota. `openai` carries `"max_effort": "high"`. An effort the user fixed (`roles.<role>.effort`, a task's `effort`) is not
  limited: the user asked for it.
- Design, implementation and reading stay with the host vendor (the first vendor of `hosts.<host>`), because the user chose that
  host. So the author's maker is excluded only from a
  review; a free plan does not move the host vendor last for these roles; and a `roles.<role>` pin of another vendor applies
  only on the hosts its `hosts` list names (without the list it applies only where that vendor is the host vendor, and the
  notice says it was not applied). When the result still leaves the host vendor (a task, a pin with `hosts`, exhaustion, a
  missing CLI, no usable model), the result carries `off_host_vendor: true` and `off_host_cause`, and the notice ends "Not
  the host vendor (<vendor>): <cause>." so the orchestrator can tell the user before opening the worker.

### 7.3 Exhaustion

A usage-limit message from a run, or a manual mark, writes the quota (pool) and an `until` time to `exhausted.json` (manual
section 5.3). agy has two quotas, Google's models (`google`) and the other companies' models it serves (`google-partner`),
so the pool, not the vendor, is the unit. The router takes the same grade from a stand-in model (an `@agy` Claude, chosen
only when the quota the slot wants is used up) or from the next vendor. Review independence is by maker, so a stand-in
never reviews its own maker's work. If no other maker can review, the review is reported as incomplete (router exit 3)
instead of relaxing independence.

## 8. Model-compensation rules

A rule written for one model's repeated mistake ends with `[fix: <reason> / remove when <condition>]`; the command that
lists them is in manual [section 10](manual.md#10-model-compensation-rules). Add a rule only after the mistake has been
seen, with its removal condition. After a model change (the `model-update` skill lists them when the default route moves),
test each condition and delete the rules whose condition holds. A rule that fits no single model has no tag. Old caution
does not accumulate: a rule nobody can explain is not kept "just in case".

## 9. Model list maintenance

Commands and the apply procedure: manual [section 8](manual.md#8-keeping-the-model-list-current). Weekly, one scheduled script per machine
asks each vendor CLI for its model list and, with an Artificial Analysis API key, fetches scores and prices, once, into a
cache shared by every project on the machine (`~/.tagteam/cache/`). It then writes each registered project's proposal from
that cache (no further network call) and nothing else; the `model-update` skill applies each change kind by a fixed rule (automatic for availability
and benchmark values; held for deletions and for a default-route change that lacks two agreeing sources, a probe run and
passing route tests). So the update path needs no model, the collector cannot change a route, and only `models.json`
changes. Claude Code lists model families, not versions. Data from Artificial Analysis must be credited; the README and the
collector carry the notice.

## 10. Known gaps

| Gap | Effect | Status |
|---|---|---|
| No `run.py` adapter for CLIs of other companies (for example xAI) | such models cannot be launched as workers; the collector can still compare their benchmarks through `aa_creator` | open |
| Claude `/model` output differs by plan | availability on other plans is unconfirmed | open |
| macOS and Linux: unconfirmed are the sleep and missed-run behaviour of the weekly job, the desktop app's tool name, interactive Codex on macOS, other Linux distributions and CPUs | the weekly job may skip a week on a machine that sleeps or is off | open; [platform/macos.md](platform/macos.md) and [platform/linux.md](platform/linux.md) |
| agy: unconfirmed are whether "always allow" covers the same command with other arguments, how many approvals a mission needs, hook loading in an untrusted folder, and `ask` on `invoke_subagent`; interactive behaviour was observed once on Linux and once on Windows | commands ask for approval often enough that the user has to stay present; the rest may differ between machines | open; [hosts.md](../harness/skills/cycle/hosts.md#agy-notes) note 8 lists them |
| agy: the hook judges only the edit tools known so far (`write_file`, `edit_file`), not a tool that deletes or renames a file if agy has one, and not shell writes; on a guard error the answer is `ask`, and a headless worker's edit then runs | a `run.py` worker on agy can still change `.claude/`, `.codex/`, `.agents/` and the instruction files through such a path; the control-plane check then ends the run `control_plane_changed` | open; the check and the review of the diff are the net |
| The control-plane check sees only what `git status` lists plus the named machine-local files, compares a file over 16 MiB by size and time, and shares the checkout with every other writer (the orchestrator, another worker) | a change made and undone inside the run, or in an ignored file the lists do not name, is missed; another writer's edit can be blamed on the run | accepted; manual section 7 |
| agy: the execution half of the Orchestrator model may be a weaker model than the planning half (a Flash-class model) | it does the orchestrator's checks and commits; a weak comparison of a report with the diff can pass a bad change | mitigated, not closed: a rule against direct fixes, the diff comparison repeated in the review, review grade at least A, `agy-models.jsonl` |
| Git detection of commits is textual | a worker can evade the hook with indirection | accepted; review the diff |
| The hook does not know the vendor of a reviewer | independence depends on the orchestrator using the router | accepted |
| Codex: commits, branches, worktrees, `run.py` launches and test runs in the sandbox need the user's approval (`codex exec --approve-for-me` approves them automatically, which is not the user's approval); whether a finishing subagent starts an orchestrator turn is unconfirmed | more interaction than on Claude Code; the orchestrator waits for its subagents in the same turn | accepted; [hosts.md](../harness/skills/cycle/hosts.md#codex-notes) |
| `start_session` is the Claude Code desktop app's session tool | the session pool needs it; elsewhere, native subagents and the launcher remain | accepted |
| A project hook that rewrites tool input (`updatedInput`) next to the Claude guard | the guard may judge the command before the rewrite (parallel hooks; not confirmed against Claude Code) | open; the installer lists other hooks on tagteam's events |
| `tagteam-update` and `run.py --running` see launcher runs only | a session or subagent working in the project or the checkout is not found | the orchestrator lists its own pool and asks the user once |
| Hook recovery needs git | a broken hook is repaired outside Claude Code | see manual section 9.1 |

Windows measurements: [platform/windows.md](platform/windows.md).
