# tagteam manual

How to install, run and maintain the harness. Why it is built this way: [design.md](design.md). The rules the agents follow,
and the term table (orchestrator, worker, router, launcher, session pool, ...): [AGENTS.md.template](../AGENTS.md.template).
Terms are defined only there.

Paths: `install.py`, `harness/...` and `docs/...` are in the cloned repository. `.claude/...`, `.codex/...` and `AGENTS.md`
are in the project you installed into. Commands are written `python3`; on Windows use `python`.

## What is where

| Section | Answers |
|---|---|
| [1. Install and update](#1-install-and-update) | What is overwritten? What else does the installer do? 1.1 is it working? 1.2 Codex hook trust, repair after a move; 1.3 agy hooks and their recovery |
| [2. Where things live](#2-where-things-live) | Which file holds what; which are committed; the state folder |
| [3. Running a mission](#3-running-a-mission) | How a mission runs; when sessions open and what to do if the host refuses (3.1); worker definitions (3.2); Codex as orchestrator (3.3); agy as orchestrator (3.4) |
| [4. Verification and review](#4-verification-and-review) | Who reviews; how a review is made read-only |
| [5. The router](#5-the-router) | How a model is chosen (5.1); grades; pin or hide a model (5.2); mark a vendor exhausted (5.3) |
| [6. The role hook](#6-the-role-hook) | What the hook does; fix a wrong session role |
| [7. The launcher](#7-the-launcher) | Run a worker on another vendor; read-only limits; git check; exit codes |
| [8. Keeping the model list current](#8-keeping-the-model-list-current) | Collect, apply (8.1) and schedule (8.2) model updates |
| [9. When something goes wrong](#9-when-something-goes-wrong) | What a message means; recover from a broken hook (9.1) |
| [10. Model-compensation rules](#10-model-compensation-rules) | List the rules that exist for one model's mistake |
| [11. Developing the harness](#11-developing-the-harness) | Tests; uninstall |

## 1. Install and update

    python3 install.py <project dir> [--update] [--yes] [--source DIR]
    python3 install.py <project dir> --check [--host claude|codex|agy] [--live]
    python3 install.py <project dir> --repair-hooks [--yes]
    python3 install.py <project dir> --schedule | --unschedule [--dry-run]

Supported CLIs (each optional except that you need one orchestrator):

| CLI | Orchestrator (host) | Worker | Notes |
|---|---|---|---|
| Claude Code (`claude`) | yes | native subagent or session, or launcher | the session pool needs the session tool of the Claude Code desktop app; without it, subagents and the launcher still work |
| Codex CLI (`codex`) | yes (section 3.3) | native subagent, or launcher | |
| Antigravity CLI (`agy`, Gemini models) | yes, interactive only, with the built-in Orchestrator model (section 3.4) | launcher only | headless `agy -p` is a worker, not an orchestrator |

**Warning: install and update overwrite harness-owned files, in a plain run as well.** Customizations in the files marked
"overwritten" below are lost, and the `hooks` and `subagentPromptCacheTtl` keys of an existing `.claude/settings.json` are
replaced. Commit or back up the project first. Put per-machine changes in `.claude/tagteam/models.local.json` (section 5.2);
edit `models.json` only in a copy of the repository you maintain yourself.

A plain run creates the project folder if needed, copies the files and prints one line per file as `created`, `updated` or
`kept`. `--update` runs the same copy (only the wording changes, and it skips the weekly-check question). `--source DIR`
installs from another copy of the repository.

| Source | Goes to | On update |
|---|---|---|
| `harness/claude/settings.json` | `.claude/settings.json` | only the `hooks` and `subagentPromptCacheTtl` keys are replaced; other keys stay |
| `harness/claude/agents` | `.claude/agents` | overwritten |
| `harness/skills` | `.claude/skills` and `.agents/skills` | overwritten (Codex reads `.agents/skills`) |
| `harness/tagteam/*` (not `tests/`) | `.claude/tagteam/` | overwritten, `models.json` included; files with `.local.` in the name are not copied (the example file only seeds `models.local.json`) |
| `harness/codex/hooks.json` | `.codex/hooks.json` | written once, then kept; the installer adds an ignore rule for it; a file git tracks is never written, untracked or backed up (section 1.2) |
| `harness/codex/agents` | `.codex/agents` | overwritten |
| `harness/agy/hooks.json` | `.agents/hooks.json` | only the `tagteam` key is written, with this machine's interpreter and the absolute guard path; rewritten on every install and `--update` (section 1.3) |
| `AGENTS.md.template` | `AGENTS.md` | the part above the `## Project policy` heading is replaced; that heading and everything below it is kept (marker rule below) |
| `CLAUDE.md.template` | `CLAUDE.md` | created, or the line `@AGENTS.md` is appended if missing |

Also kept as they are: `models.local.json`, `bench.local.json`, the machine-local `.codex/hooks.json`, and every key of
`.agents/hooks.json` except `tagteam`.

**The `AGENTS.md` marker rule.** The installer looks for the exact text `## Project policy`. Present: that line and
everything below it is kept, everything above is replaced by the template. Absent: the whole old file is kept, unchanged,
under a new `## Project policy` heading after the template body (`kept (moved under Project policy)`). Either way, review
`AGENTS.md` afterwards and move your rules where you want them; commit or copy the old file first.

Other things the installer does:

| What | Behaviour |
|---|---|
| `models.local.json` | If missing: in a terminal it asks the plan for each vendor (`none`, `free`, `pro`, `max`; Enter keeps `pro`; `none` removes that vendor from routing). With `--yes` or without a terminal it copies `models.local.json.example`. Check and edit the result (section 5.2). |
| Benchmark and available models | If `TAGTEAM_AA_API_KEY` is set and `bench.local.json` does not exist, runs `models_collect.py --write-bench` once (15 second timeout); if a vendor with a plan other than `none` has no `available` list, runs `--write-available` once. A failure prints a warning; the install continues. |
| `.gitignore` | Adds `.claude/tagteam/models.local.json`, `.claude/tagteam/bench.local.json`, `.codex/hooks.json`, `.agents/hooks.json` and `.tagteam/`. |
| Line endings | A file whose content differs from the new one only in `\r\n` versus `\n` (a clone with `core.autocrlf=true`) is reported `kept` and not rewritten, so `--update` makes no change that git shows. Only valid UTF-8 text without a NUL byte is compared this way; any other file is compared exactly. |
| State folder | Moves records of an old `<git common dir>/tagteam/` into `.tagteam/` once (section 2). |
| Weekly check | In a terminal, a plain install (not `--update`) asks whether to register it (section 8.2). |

### 1.1 Check

    python3 install.py <project dir> --check [--host claude|codex|agy] [--live]

Needs a finished install. `--host` (default `claude`) picks the host to check. What was not run is reported `unverified`;
a failure ends with `check: FAILED` (exit code 1), otherwise `check: ok`.

- **static**: the tools on PATH (a missing CLI is a warning), the interpreter the host's hooks call (`python3` or `python`
  for Claude; `python3`, on Windows `python`, for Codex; a missing one is an error, one found only through your shell
  PATH a warning), the installed files, the state folder (path, `.gitignore`, an old folder left over), and for Codex the
  drift of `.codex/hooks.json` and three warnings about your own Codex setup (section 1.2). Your Codex files are only read.
- **handler**: fake payloads in the host's own format through the installed `role_guard.py`.
  Claude: `start_session` without model (denied), a worker `git commit` (denied), a worker `git status` (allowed); it also
  runs **hook commands**: each tagteam command of `.claude/settings.json`, exactly as written, in the shell Claude Code uses
  (`sh`; Git Bash on Windows) with your PATH, from the project folder, within the hook's timeout. A command that finds no
  Python, or does not answer in time, is a failure: the guard would be off without a word.
  Codex: `spawn_agent` by a worker, without model and effort, or with them for a custom agent; a subagent `git commit`; a
  worker `apply_patch` on `AGENTS.md` (all denied); `git status` and a plain custom-agent spawn (allowed).
  agy: fake agy payloads through the installed guard, each of which must answer one JSON object with exit 0: a worker's
  `git commit` and `invoke_subagent` (`deny`), a worker's `git status` and the orchestrator's `invoke_subagent` (`ask`), a
  broken payload (`ask`), SessionStart (an object), the first model call of an orchestrator (`{}`), and the first model
  call without user input (`{}`, and a worker record is written). It uses a temporary state folder.
- **hook commands**: for agy, the PreToolUse commands of `.agents/hooks.json` run exactly as written, the way agy runs them
  (`cmd /c` on Windows, `sh -c` elsewhere), from the folder `.agents`, within each hook's own timeout. A worker's `git commit`
  must be denied and `git status` must get `ask`; a bad quote, a wrong guard path, a missing interpreter or an answer that
  comes too late is a failure.
- **live** (`--live`, uses tokens): runs the real CLI once as a marked worker and requires a valid role record. On Codex
  run it only after trusting the hooks (section 1.2): untrusted hooks write no record. On agy it starts `agy -p` with the
  first google C-grade default model of `models.json`, mode `accept-edits`, an empty allow list and no host variables, and
  looks for a role record written after the start. If agy prints a conversation id the record named by it must say
  `worker` with host `agy`; if not, exactly one new agy record, a worker, counts as ok, and several new records leave the
  result `unverified`. An account that cannot use that model also gives `unverified`, not a failure.

`--check` cannot see whether Codex trusts the hooks (section 1.2). For agy the static part reads `.agents/hooks.json`: the
interpreter on PATH, the `tagteam` key equal to the template's (after the paths are filled in), the guard files present,
and the file git-ignored (not ignored: a warning; tracked: an error). `--check --host agy` prints `live (agy): unverified`
unless `--live` is given.

### 1.2 Moving a project

Codex ties its trust of a hook to the hook definition and the guard path, and **skips a new or changed entry without any
message**. After `install.py` (first run), `--update` or `--repair-hooks`, open `codex` in the project and trust the hooks
with `/hooks`; until then the role guard is off there. Two steps, in this order: trust the **project** (the question
codex asks in a new folder, or `codex exec --trust`), then trust the **hooks**; without the first, Codex does not load the
project's `.codex/hooks.json` at all. `.codex/hooks.json` is machine-local: it holds the absolute path of the guard, the
installer adds an ignore rule for it, and the installer never rewrites an existing file.

**On a new machine or a fresh clone:** `python3 install.py <project dir> --update --yes`, then `--check --host <host>` for
each host you use (Codex also needs the two trust steps above: the project, then the hooks).

**What `--check --host codex` warns about.** These are warnings, not errors; the proof that the hooks run is still
`--check --live`. Your files under `$CODEX_HOME` (default `~/.codex`) are read, never written; a file that is missing,
larger than 1 MB, not a regular file or not valid is reported `unverified`, quietly.

- `your Codex rules allow "git commit" (<file>): ...` (also `"git push"`): a `prefix_rule` in `rules/*.rules` that allows
  those commands (decision `allow` or none, a pattern that starts with `git` and names the word or stops at `git`). Codex
  runs such a command outside its sandbox, so a worker's commit is stopped only by the tagteam hooks: keep the project and
  its hooks trusted. The rules are read as Starlark through Python's parser; a file that does not parse is `unverified`.
- `this project is not in Codex's trusted projects (<config>): ...`: `config.toml` has no `[projects.'<path>']` table with
  `trust_level = "trusted"` for exactly this path (Windows ignores case). Whether Codex also trusts a project below a
  trusted parent folder is unverified, so the installer counts only the exact path and merely mentions a trusted parent.
  Codex skips the project's hooks when the project is not trusted. This needs Python 3.11 or newer (`tomllib`); on an older Python, such as the
  3.9 that macOS ships, the answer is `unverified`.
- `.codex/hooks.json is tracked by git; ...`: the file holds this machine's absolute guard path, so it should not be in
  a commit. Untrack it yourself with `git rm --cached .codex/hooks.json` and commit; tagteam never changes your repository.
  While git tracks it, install and `--update` do not write it (a tracked file that was deleted is not recreated; the
  installer says so, and `--check` then reports the missing file as an error, as for any missing file). The same holds when
  git cannot say whether it tracks the file (missing, timed out, unreadable index). `--repair-hooks` with nothing to repair
  says so and exits 0; when it would write, it asks git first and, for a tracked file or an unanswered question, writes
  nothing, makes no backup and exits 1 (the preview still works). The check that an ignore rule really applies (a later
  rule can undo it) exists for `.agents/hooks.json` only; for `.codex/hooks.json` the installer adds the rule.

After moving or copying a project, or in a worktree that received a copy of the file, or when `--check --host codex`
reports drift (an entry missing, or a wrong path):

    python3 install.py <project dir> --repair-hooks          # preview
    python3 install.py <project dir> --repair-hooks --yes    # apply

It touches only the tagteam entries, writes `hooks.json.bak` first when it overwrites an existing file, and prints the
diff. Then trust the hooks again.

### 1.3 agy hooks

agy reads `.agents/hooks.json`; tagteam owns the key `tagteam` in it and nothing else. The file is machine-local: the
guard path in it is absolute and the interpreter is the one of the machine that wrote it (`python` on Windows, else
`python3` when it exists, else `python`). It is git-ignored. agy has no hook trust to confirm, so install and `--update`
rewrite the key every time; after moving the project, run `--update` again.

    python3 install.py <project dir> --update
    python3 install.py <project dir> --check --host agy [--live]

The install report shows `.agents/hooks.json` as `created`, `updated` or `kept`. The installer **writes nothing** and prints
`warning: agy hooks not installed: <reason>; --check --host agy fails until this is fixed` (the rest of the install goes on)
when:

- git tracks `.agents/hooks.json`, or git cannot say whether it does (missing, timed out, unreadable index): the file would
  end up in a commit with machine paths in it;
- `.agents` or the file is a link or resolves elsewhere, or the file has several hard links;
- the file is not a JSON object (a file of yours that tagteam does not understand);
- the guard path holds a character the hook shell would still expand inside double quotes (`%` or `"` on Windows; `$`,
  a backtick, `"` or `\` elsewhere): move the project;
- the file cannot be replaced (for example another program holds it open).

The `tagteam` entry has three events: SessionStart and PreInvocation (10 second timeout each) and PreToolUse, whose matcher
is `run_command|invoke_subagent|define_subagent|run_workflow|swarm|write_file|edit_file` (10 seconds): the edit tools are
matched, so a worker's `write_file` or `edit_file` on `.claude/`, `.codex/`, `.agents/`, `AGENTS.md` or `CLAUDE.md` is
denied, and every other edit stays with your permission rules.

**Recovery.** On agy a PreToolUse hook that fails to start or answer blocks the tool, so a broken guard (no Python, a wrong
guard path, a moved project) blocks `run_command`, the delegation tools **and the edit tools**: agy cannot fix it from the
inside, and the orchestrator cannot write its mission records either. From a terminal or another editor:

1. `python3 install.py <project dir> --update` rewrites the key; `--check --host agy` then says what is still wrong.
2. To switch the hooks off, delete the `"tagteam"` key from `.agents/hooks.json` (your other keys stay), or add
   `"enabled": false` inside the key (`/hooks` then lists them as disabled; agy 1.5.11). The opening screen of the
   interactive `/hooks` view shows a count per event type, not hook names, and SessionStart is not listed as a type
   although it runs (agy 1.5.12 Windows; the detail screens were not looked at).
3. A role recorded wrongly. The guard decides the role at the first model call of a conversation and never changes it.
   If it refuses the orchestrator's `git commit` and the reason ends with `role recorded as worker: the first model call had
   no user input`, or the role is `unresolved` (the first decision was lost or the record is unreadable), delete the record
   `<state folder>/sessions/<conversation id>.json` (the newest file there with `"host": "agy"`; for `unresolved` the reason
   names the path) and **start a new conversation**: in the same conversation the next model call is no longer the first
   one, so the guard would record `unresolved` again. Deleting it is your decision, not the agent's. Whether an interactive conversation's first model call carries your input is unconfirmed.

## 2. Where things live

| Location | Contents | Committed |
|---|---|---|
| `AGENTS.md` | rules; `## Project policy` is yours | yes |
| `.claude/settings.json`, `.codex/hooks.json`, `.agents/hooks.json` | hook wiring | settings yes; both `hooks.json` files are machine-local by design (absolute guard path; git-ignored) |
| `.claude/agents`, `.codex/agents` | worker definitions with fixed model and effort | yes |
| `.claude/skills`, `.agents/skills` (`cycle`, `model-update`) | orchestrator procedures, the same copy for each host | yes |
| `.claude/tagteam/models.json` | vendors, roles, models, grades, defaults | yes |
| `.claude/tagteam/models.local.json` | plans, hidden models, available lists, role overrides | no |
| `.claude/tagteam/bench.local.json` | benchmark values from Artificial Analysis | no |
| state folder | records below | no |

The *state folder* is `<main work tree>/.tagteam/` (git-ignored), so all worktrees of one repository share it; a project
inside a submodule uses the superproject's work tree, and outside git it is `<project>/.tagteam/`. It is outside `.git`
because Codex sandboxes cannot write there. `TAGTEAM_STATE_DIR` overrides the location. `--update` moves the contents of an
old `<git common dir>/tagteam/` here once (nothing is deleted: the old folder is renamed `tagteam.moved`); `--check` warns
while one remains. If the folder cannot be written, `run.py`, `route.py --mark-exhausted`/`--clear-exhausted` and the collector stop with a
message that names it (plain routing only reads it); on Claude Code and Codex the hook only warns and allows the action, so a
role record that cannot be written leaves the guard off for that session. On agy, if the state folder is not writable the guard
cannot keep the first role, so it denies git commit/push and delegation even for the orchestrator; fix the folder (or set
`TAGTEAM_STATE_DIR`), or remove `<state>/sessions/<cid>.json` to decide the role again.

| Entry | Written by | Meaning |
|---|---|---|
| `sessions/<id>.json` | hook | role of each session (`worker`, `orchestrator`, or on agy `unresolved`: the first decision was lost, so protected calls get a worker's rules), recorded once |
| `agents.jsonl` | hook (Codex `SubagentStart`) | the model each Codex subagent really runs on |
| `agy-models.jsonl` | hook (agy PreInvocation) | one line `{ts, cid, model}` each time an agy conversation's model changes: which half of the Orchestrator model did what |
| `model-update.log` | the scheduled job (macOS, Linux) | output of the weekly collector run |
| `exhausted.json` | `run.py`, `route.py` | vendors temporarily unusable, with `until` |
| `runs/<id>/` | `run.py` | one launcher run (section 7) |
| `model-update.json` | `models_collect.py` | pending proposal (section 8) |
| `model-update.applied` | `models_collect.py --mark-applied` | digest of the last handled proposal |
| `collect.log` | `models_collect.py` | one line per collector run |

## 3. Running a mission

The procedure is the skill `cycle` ([SKILL.md](../harness/skills/cycle/SKILL.md)); the rules are AGENTS.md section 3.
In short: for anything with more than one unit the orchestrator writes `docs/missions/<purpose>/intent.md` and `spec.md`
and waits for your confirmation of the spec, then opens the session pool on a turn you started (one worker per role, model
and effort from the router). The orchestrator checks every return against the real diff and commits; expensive checks and
the cross-vendor review run at chosen cut points.

### 3.1 The session pool and automatic turns

**Rule: open the sessions a phase needs at its start; when another is needed later, try first, and if the host refuses,
reuse one by message.** A turn that starts because a worker reported back is an *automatic turn*. On Claude Code
desktop 2.1, `start_session` is refused in an automatic turn, but a message to an already open session works.
This is a host limit, not a harness rule; the instructions tag it `[host: ...]` so it is re-tested after host updates.
Consequences:

- While the host refuses new sessions in automatic turns, say "go" on a user turn when new roles are needed; until then
  the orchestrator continues with the sessions it already has.
- A reused session keeps its prompt cache for 1 hour; after a longer pause the next task pays for the prompt again, and a
  brand-new session costs roughly 90,000 tokens before any work. Keep sessions per role, not per task.
- The worker marker line `HARNESS ROLE: DELEGATE` counts only in the **first** prompt of a session; a task sent later by
  message is not seen by the hook that records the role. The orchestrator puts the line in every session's first prompt.
- A new git worktree does not initialize submodules. If a unit edits a submodule, its brief must start with `git
  submodule update --init <path>`.
- Most of a long mission's cost is the orchestrator re-reading its own growing context. An option, not a rule: at the end
  of a phase (never mid-unit), hand the mission to a fresh orchestrator session after `status.md` holds the decisions,
  open questions, work in flight and next step. Try it once and compare before relying on it.

### 3.2 Worker definitions

Subagents with a fixed definition carry their own model and effort. The router does not choose them, and an update restores
them to the shipped values.

| Host | Name | Model, effort | Tools or limits |
|---|---|---|---|
| Claude | `designer` | `opus`, high | Read, Grep, Glob, Write; writes one design document |
| Claude | `implementer` | `sonnet`, high | Read, Write, Edit, Bash, Grep, Glob |
| Claude | `reader` | `haiku`, high | Read, Grep, Glob, Bash; read-only |
| Codex | `designer` | `gpt-6.1-sol`, xhigh | `.codex/agents/designer.toml`; writes one design document |
| Codex | `implementer` | `gpt-6.1-sol`, medium | `.codex/agents/implementer.toml` |
| Codex | `reader` | `gpt-6.1-terra`, low | `.codex/agents/reader.toml` |

A review by another vendor goes through the launcher (section 7); the router says which to use (`launch.kind`, section 5).

### 3.3 Codex as the orchestrator

Details per operation: [hosts.md](../harness/skills/cycle/hosts.md). What to do as the user:

1. Install (or `--update`), then run `python3 install.py <project dir> --check --host codex`.
2. Start `codex` in the project's **main checkout** (a worktree session needs `--add-dir <state folder>`), run `/hooks` and
   trust the tagteam entries (section 1.2). Optionally run `--check --host codex --live` now, then give it the mission.
3. Workers are Codex subagents (`spawn_agent`, `followup_task`, `wait_agent`; the orchestrator must wait for them before
   ending its turn) or `run.py` workers on another vendor.
4. The Codex sandbox protects `.git` and does not let nested CLIs start, so `git add/commit`, branches, worktrees and
   every `run.py` launch are escalation requests. Interactive Codex shows each as an approval prompt: a small mission
   (design, implementation, one review, two commits) shows one prompt per commit, branch, worktree, `run.py` launch and
   test run. If a commit is refused the unit is
   verified but not committed; a refused review launch leaves it incomplete.
5. On Windows, `windows.sandbox = "elevated"` breaks `codex exec` and temporary folders may be denied inside the sandbox
   (`--check` and the tests then need escalation): [platform/windows.md](platform/windows.md#codex).

### 3.4 agy as the orchestrator

Details per operation and the limits: [hosts.md](../harness/skills/cycle/hosts.md#agy-notes). What to do as the user:

1. Install (or `--update`), then run `python3 install.py <project dir> --check --host agy`. Optionally `--live` (one model
   call). Recovery if the hooks misbehave: section 1.3.
2. Start `agy` **interactively** in the project and pick the built-in "Orchestrator" model. A headless `agy -p` cannot
   orchestrate: it waits only three minutes for a background command. The built-in Orchestrator has more than one id
   (agy 1.5.12 lists `gemini-pro-orchestrator-neon` "Orchestrator (Neon)" and `gemini-pro-orchestrator-g1-pro`
   "Orchestrator (Argon)"; the interactive model list offers one entry). The orchestrator passes `--host agy
   --orchestrator-model <the id of its own conversation>` to the router, for example `gemini-pro-orchestrator-neon`.
   It takes it from `<state folder>/agy-models.jsonl`: the most recent line with its conversation id (`cid`, the
   `ANTIGRAVITY_CONVERSATION_ID` of its shell) whose `model` is one of the `orchestrators` ids of `models.json`. The log also
   records the execution model (`gemini-flash-worker-agy` alternates with `neon`), so the last line may name that one;
   `agy models` lists both Orchestrator ids but not which one is selected. It passes no `--orchestrator-effort` (the model has none; giving one is a usage error,
   exit 2). The router compares either id as `gemini-4-argon` at `high`, an assumption, and says so in the notice. Your
   account must list the model (`agy models`); some accounts do not.
3. Every worker is a `run.py` run (the router returns `run` for every role on agy), started by the orchestrator's shell
   command. Which worker vendor does what is the router's decision as on any host. Reviews are at least grade A.
4. Expect approval prompts: the hook answers `ask` for shell commands that are neither refused nor covered by an allow rule, so
   agy's own permission rules decide. In interactive agy on Linux it asks several times per mission and
   "always allow" does not stop the questions [host: agy 1.5.12 Linux interactive, measured 2026-11-19 / re-test after an
   agy update]. On Windows interactive agy runs commands in a sandbox that cannot write the work folder or `.git`, so
   `run.py` launches and commits go out with `BypassSandbox: true`; the user recalled approval prompts on those
   commands, whether every such call asks is unconfirmed [host: agy 1.5.12 Windows interactive, one session, measured
   2026-11-19 / re-test after an agy update]. **Stay present for
   the whole mission**; on Linux a subagent's command that waits for your approval for 60 seconds fails. Whether "always
   allow" also covers the same command with other arguments, and how many approvals a mission needs, is
   unconfirmed; on Windows an allow rule has to match the
   command exactly ([platform/windows.md](platform/windows.md#agy-google)). The orchestrator tells you at the start of a
   mission roughly how many to expect.
5. The Orchestrator model hands its plan to an execution model that can be a Flash-class model (for a long or complex mission: unconfirmed). The
   harness mitigates it with a rule (the execution side edits only mission records and launches every implementation unit
   through `run.py`), a review that repeats the comparison of the worker's report with `git diff`, the review grade floor A,
   and `<state folder>/agy-models.jsonl`, which shows the model of each conversation. If you need more, pick a single model
   such as the Argon model instead of the Orchestrator model (an alternative, not the default; it has its own weekly limit).
   The orchestrator then gives the router that real model and effort (`--orchestrator-model gemini-4-argon
   --orchestrator-effort <effort>`) instead of the Orchestrator id.
6. The orchestrator and its google workers draw on the same agy account limits. A google worker that uses the limit up also
   stops the orchestrator; `exhausted.json` only sends later workers to another vendor.

## 4. Verification and review

**Every change gets a review by another vendor; nothing skips it.** AGENTS.md section 4 sets how: Tier 0 units are
reviewed together in one batch, a Tier 1 unit once, a Tier 2 unit twice (review, fixes, second review). Operationally:

- Fill in `Verification commands` in `## Project policy` with the exact commands and what counts as passing. Workers run
  the commands named in their brief.
- A reviewer must be another vendor at the author's grade. Ask the router (section 5) with `--author-vendor` and
  `--grade`. If it exits 3, report the review as incomplete rather than reviewing with the author's vendor.
- **Read-only review.** The router adds `--read-only` only to a `run.py` launch, and the launcher passes it to the
  vendor CLI (limits: section 7). A native review session (a Claude or Codex subagent or session) gets no such setting from
  the router; its read-only behaviour comes from the brief and from the worker definition's tools, so state it in the brief.

## 5. The router

Run it before every new session or launcher run (AGENTS.md section 1).

    python3 .claude/tagteam/route.py (--role ROLE | --task NAME) --host claude-desktop|claude|codex|agy
        [--author-vendor VENDOR] [--grade S|A|B|C | --author-model ID --author-effort EFFORT]
        [--orchestrator-model ID] [--orchestrator-effort EFFORT] [--json]

Always pass `--host`; a guess from the environment can be wrong (without it, `ANTIGRAVITY_CONVERSATION_ID` in the environment
means `agy`, checked before `CLAUDECODE`, which an agy started from Claude inherits). Without `--json` it prints one notice line, which the
orchestrator relays to you: `<role> -> <model> (<effort>, <vendor>, grade <g>) via session|subagent <name>|run.py, host=<h>.`
`--task NAME` routes a preference you recorded in `models.local.json` (section 5.2) as its base role.

| Role | Default grade | Read-only | Why / notes |
|---|---|---|---|
| `design` | A | no | judgement is the product |
| `implement` | B | no | bounded units with a brief and checks |
| `review` | A | yes | needs the author's grade, other vendor; `--author-vendor` required; pass `--grade` of the author's model and effort |
| `read` | C | yes | bulk reading; conclusions carry `path:line` evidence |
| `translate` | B | no | vendors in the order google, openai, claude |

With `--json` the result has `status`, `role`, `model`, `effort`, `vendor`, `grade`, `launch`, `ask_user`, `reason` and
`notice`. `launch.kind` is `session` (Claude Code desktop: `start_session` with that model and effort), `subagent` (a worker
definition that fixes model and effort, or on Codex the `default` agent with `model` and `reasoning_effort`) or `run`
(execute `launch.command`, a `run.py` call with `<PROMPT_FILE>` to fill in, run by the Python that runs the router; on Codex
`escalate: true` means request escalated permissions). A `subagent` is offered only when the definition's model and effort
equal the chosen ones. `ask_user: true` means a design worker would outrank or cost more than the orchestrator: ask first;
`null` means the router lacked the orchestrator's model and effort. How to open each kind: [hosts.md](../harness/skills/cycle/hosts.md).

Exit codes: 0 chosen; 2 usage or configuration error; 3 no independent reviewer (`status` is `incomplete`); 4 no usable
model.

**agy host.** `--host agy` returns `launch.kind` `run` for every role. A review there is raised to at least grade A when the
author's grade is lower (`agy_review_floor_A` in `reason`, and the notice says "review grade raised from C to A"), because the
execution half of agy's Orchestrator model may be a weaker model; a pinned reviewer or task preference below A is replaced
(`override_below_floor`). `--orchestrator-model gemini-pro-orchestrator-neon` (or `-g1-pro`) names a model of the
`orchestrators` block of `models.json`: the router compares it as that entry's `compare_as` model and effort, appends `Orchestrator compared as
gemini-4-argon high (assumed, not measured).` to the notice, and takes no `--orchestrator-effort` (exit 2). `run.py` refuses
that id (`not registered`): it can orchestrate, not be launched. On Claude hosts a missing `--orchestrator-effort` falls back
to `CLAUDE_EFFORT`; on other hosts it does not, since an agy started from Claude inherits that variable, and `ask_user` stays
`null` when there is nothing to compare with.

**Grades.** A grade (S, A, B, C) belongs to a model **at an effort**: a table `grades` per model in `models.json`. The
floors and cost ceilings that produced it (`grading.floors`, `grading.caps`) are in
[design.md](design.md#7-grading-and-routing). The collector flags a model whose index and `grades` disagree (section 8).

### 5.1 How a choice is made

1. **Vendor order.** From `hosts.<host>` in `models.json` (Claude host: claude, openai, google; Codex host: openai,
   claude, google), with plan `free` vendors moved last. A role with its own `vendors` list in `models.json` (only
   `translate`: google, openai, claude) uses that list instead: it wins over the host order and is not reordered for `free`
   plans. In both cases vendors with plan `none` are dropped, the author's vendor is dropped for a review, and vendors in
   `exhausted.json` and vendors whose CLI is missing are skipped. The first vendor that has a candidate wins.
2. **Candidates.** Every registered model and effort of that vendor whose `grades` entry equals the role's grade and that
   is usable: plan not `none`, not hidden by `models.<id>.enabled`, and listed in `available` (no list counts as offering
   everything). A model marked `"candidate": true` in `models.json` is never a candidate here; only a `roles.<role>.model`
   pin can select it.
3. **Pick order.** The first rule that matches any candidate decides: cost known and within `grading.caps` for the
   grade: highest index; no cost but an index: highest index; cost above the cap: lowest cost; nothing known: the first
   usable entry of `defaults.<vendor>.<grade>`, else the first model in `models.json` at its lowest effort. Values come from
   `bench.local.json` at exactly that effort.
4. **Orchestrator cap.** A native Claude session may not use a model listed earlier in `models.json` than the
   orchestrator's (earlier means stronger) or a higher effort. Candidates inside the cap are preferred; if none is, the
   launch becomes a `run.py` command. Without `--orchestrator-model` and `--orchestrator-effort` the notice adds "Check
   orchestrator cap."

`reason` lists what happened: `default`, `substituted:<from>→<to>`, `plan_free_last:<vendor>`,
`author_vendor_excluded:<vendor>`, `exhausted_skip:<vendor>`, `cli_missing:<vendor>`, `native_model_cap`, `effort_clamped`,
`user_override`, `agy_review_floor_A`, `override_below_floor`, `bench:local`, `bench:none`.

### 5.2 Per-machine settings: `models.local.json`

| Key | Effect |
|---|---|
| `plans.<vendor>` | `none` removes the vendor; `free` moves it last; `pro`, `max` have no routing effect; a missing vendor counts as `pro` |
| `models.<id>.enabled` | `false` hides a model; `true` skips the `available` check for it |
| `available.<vendor>.ids` | ids the vendor CLI really offers (`checked` records the date); written by `--write-available` |
| `roles.<role>` | `model`, `effort`, `vendor`, `grade` to pin for a role |
| `tasks.<name>` | `{about, base, vendor?, model?, effort?, grade?}`: a preference for one kind of work (for example web pages on one vendor), used by `route.py --task <name>` on top of the `base` role; an unusable preference falls back to the role and the result says `task_applied: false`. Session start lists each `about` |

A pinned model is used when it is usable, its vendor is not exhausted, and it is not the author's vendor for a review. If
the launch would go through `run.py` its vendor CLI must also be present; a native launch of the host's own vendor needs no
check. Otherwise the router falls back to
the normal choice and `reason` says why (`override_unusable`, `override_exhausted`, `override_same_vendor_as_author`,
`cli_missing:<vendor>`). A `roles.<role>.effort` pin keeps limiting the candidates of that fallback. A pinned reviewer of
another grade than the author's is allowed, with `override_grade_mismatch` in the notice (on `--host agy` not below grade A).

### 5.3 Exhaustion

A vendor with a usage limit is recorded in `exhausted.json`, and the router substitutes the same grade on another vendor.

    python3 .claude/tagteam/route.py --mark-exhausted VENDOR --evidence TEXT [--until ISO8601]
    python3 .claude/tagteam/route.py --clear-exhausted VENDOR

Without `--until` the entry lasts `cooldown_minutes` of that vendor (60). The launcher records exhaustion itself when a run
shows a usage-limit message, and clears the entry after a successful run.

## 6. The role hook

The single hook is `.claude/tagteam/role_guard.py`, called as `role_guard.py <SessionStart|UserPromptSubmit|PreToolUse|
SubagentStart|PreInvocation> [--host claude|codex|agy]` with the host's JSON payload on stdin. A Claude denial is exit code
2 with a one-line reason on stderr; a Codex denial is a JSON answer with exit 0 (Codex ignores exit 2); an agy answer is always
one JSON object with exit 0: `{"decision":"deny","reason":"tagteam: ..."}` for a refusal, `{"decision":"ask"}` when the
guard has no objection (agy's permission rules then decide), `{}` for the other events. What it blocks, host by host, and
what it cannot block: [design.md](design.md#5-enforcement).

At session start it prints `TAGTEAM: role=<role> host=<host>`, and for an orchestrator one more line when a model-update
proposal is waiting (section 8). On agy the role is not known at session start (`role=pending`): the first model call of the
conversation decides it, and that first call is final. A line starting with `HARNESS ROLE: DELEGATE` makes a worker; so does an
empty input (an agy subagent starts that way) and `HARNESS_DELEGATE_RUN=1`; anything else is the orchestrator. A leading
slash command such as `/plan ` is ignored when looking for the marker, on every host. The role `unresolved` (agy only) means
the first decision was lost or the record is unreadable; protected calls then get a worker's rules and the reason says so.

If a session was recorded with the wrong role (for example an orchestrator whose first prompt quoted the marker line),
delete `sessions/<id>.json` in the state folder and restart that session; otherwise the role is fixed. On agy, delete the
record and start a new conversation (section 1.3).

## 7. The launcher

Runs one worker through another vendor's CLI.

    python3 .claude/tagteam/run.py --vendor claude|openai|google --model ID --effort E
        --prompt-file PATH [--cwd DIR] [--timeout SECONDS] [--read-only] [--run-commands]
        [--allow-command "<exact command>" ...]
    python3 .claude/tagteam/run.py --status ID
    python3 .claude/tagteam/run.py --wait ID [--secs N]

The router prints the first command ready to fill in; `--status` and `--wait` look a run up (below). Checks before the run (exit 2 on failure): the process is not itself a
launcher worker (`HARNESS_DELEGATE_RUN=1`), the model is registered for the vendor, the effort is one of its `grades`, a
Google model has a `label`, the CLI exists, the prompt file is readable. The prompt gets the marker line `HARNESS ROLE:
DELEGATE` on top if its first line is not already that, and the child runs with `HARNESS_DELEGATE_RUN=1`. The default
timeout is 1800 seconds.

**Read-only limits (read first).** The launcher is an accident check, not a sandbox: `--read-only` asks the CLI for its
read-only mode and does not replace review of what a worker changed. For Google, only `--read-only` alone is truly read-only
(it cannot run tests or `git diff`); `--read-only --run-commands` is enforced only by the git check, which is **not a
security boundary** (mode table and git check below).

| Vendor | CLI | Notable arguments |
|---|---|---|
| `claude` | `claude` | `-p --output-format json`, `--permission-mode plan` (read-only) or `auto` |
| `openai` | `codex` | `exec -s read-only|workspace-write`, `-C <cwd>`; on Windows also `-c windows.sandbox=unelevated` |
| `google` | `agy` | `-p`, `--mode plan` (read-only) or `accept-edits`, `--settings <json>`, `--log-file`; a very long prompt is passed as a file reference |

**Commands in `agy` runs.** In headless `accept-edits` mode `agy` denies every shell command unless it is allowed (the default
mode of 1.5.10 runs a command without a rule but cannot write), and `--mode plan` never runs commands. A Google run always gets `--settings '{"permissions":{"allow":["command(*)"]}}'` (that run only). So
`--read-only` alone is mode `plan` (no tests, no `git diff`); `--read-only --run-commands` is `accept-edits` with the allow
list (a reviewer that runs tests; read-only only by the git check); neither is an implementation run.
`--run-commands` and `--allow-command "<exact command>"` are Google only (exit 2 for other vendors). `--allow-command` is
repeatable and replaces the wildcard with `command(<cmd>)` for each given command. On Windows the rule must match the
command exactly ([platform/windows.md](platform/windows.md#agy-google)). An allow list that makes the command line too
long: exit 2.

**Git check (every vendor).** It catches common accidents and is **not a security boundary**, like the role guard's commit
check: ignored files, paths outside the repository, stashes and other refs are not looked at. It also sees every change in
the checkout during the run, other workers' included, so run read-only reviews while no other writer uses the checkout, or
in a separate worktree.

When `--cwd` is inside a git work tree, the launcher records before and after the run: `git rev-parse HEAD`, the symbolic
ref (`git symbolic-ref -q HEAD`, empty when detached), `git status --porcelain` (untracked files included) and a sha256 of
every listed file (at most 2000 files and 50 MB per snapshot; past that the check is partial and the report says so).
Results:

- HEAD or the branch moved: status `git_head_moved` (exit 7).
- With `--read-only`, the status listing or the content of a listed file changed (this includes overwriting a file that
  was already modified): status `wrote_files` (exit 8).
- Each adds an `ERROR:` line naming the change. If git itself fails the report gets `WARNING: git check unavailable (...)`
  and `result.json` gets `"git_check": "unavailable"`; the run is not failed for it. Outside a work tree, or without
  `git`, the check is skipped silently. The run folder and the state folder are left out.

**Control-plane check (every vendor).** The `git` check cannot see files git ignores, and the machine-local hook files are
exactly those. So the launcher also compares, before and after the run, the content hash of every control-plane path that
`git status` lists (`.claude/`, `.codex/`, `.agents/`, `AGENTS.md`, `CLAUDE.md`, the lists of the role guard; Python caches
and `.claude/worktrees/` are left out) and of these machine-local files: the hook files `.agents/hooks.json` and
`.codex/hooks.json`, and the setting files `.claude/settings.local.json`, `.claude/tagteam/models.local.json` and
`.claude/tagteam/bench.local.json`. It also notes whether a top-level control folder became a link. The snapshot is taken
after the agy hooks are copied into a worktree (above), so that copy is not a finding. Only a difference between the two
snapshots counts; an edit made before the run does not. Without git, or outside a work tree, the `git` check is skipped but
the named machine-local files and the link markers are still compared (the paths `git status` would list are not).

- A run without `--read-only` that changed a control-plane path or a hook file in its own checkout: status
  `control_plane_changed` (exit 10, subject to the order below) and an `ERROR: the control plane changed during the run
  (...)` line. The cause may be the worker or any other process in
  this checkout, **the orchestrator included**: do not edit the control plane yourself while a worker runs.
- A setting file that changed, or a hook file changed in a `--read-only` run, only gives a `WARNING:` line. Setting files
  change when the host writes an "always allow" approval, or when someone runs the collector with `--write-available` or
  `--write-bench` (the installer does it too); the scheduled weekly run writes a proposal under the state folder and none
  of these files. A read-only run that changed another
  control-plane path gets `ERROR` if the git check also saw a write (`wrote_files`, exit 8), else a `WARNING`; it never
  gets exit 10.
- In a linked worktree the main checkout is compared as well, and a change there only warns (it may be someone else's).
- A file over 16 MiB is compared by size and modification time; the `WARNING` saying so appears when that marker is the
  same before and after (a changed marker is a change). A file of the run's own checkout that cannot be read is named in
  `WARNING: the control-plane check is incomplete`; the main checkout of a worktree gets no such warning.
- Limits: a change made and undone inside the run is not seen, and so are changes to files that git ignores and the lists
  above do not name. Like the git check, this is an accident check, not a security boundary. `result.json` keeps the list
  under `control_plane`.

**Codex hooks check.** After a clean exit of an `openai` run the launcher looks in the state folder for a new Codex role
record written since the start. None means the hooks did not run, or ran and could not write the record (an unwritable
state folder); the usual cause is that Codex skips project hooks until the project is trusted (the report quotes Codex's
own `Untrusted project-local configuration is disabled` line when it is there), but a missing record does not prove it. A
new record that cannot be read is treated like an unreadable sessions folder (`hook check unavailable`, below), not as
"hooks did not run". A missing record is status `hooks_not_run` (exit 11) with an `ERROR:` line that says to run `codex`
once in the project (in a linked worktree: the main checkout), trust it, then trust the hooks with `/hooks`; the worker ran
without the role guard, so review its changes. This holds in a linked worktree too: in a trusted project the hooks run
there and leave a record (Codex seems to load the main checkout's project configuration; unconfirmed); a worktree of a project
Codex does not trust is unconfirmed (it should leave no record and end `hooks_not_run`). If the sessions folder cannot be
read the report says `WARNING: hook check unavailable`. The check is made only when the CLI exited cleanly. [host: codex-cli
0.170.0 Windows, trusted project, linked worktrees under and outside the main tree, measured 2026-11-19 / re-test after a
Codex update]

**Model check.** After a clean exit the launcher compares the model that answered with the one requested, from `modelUsage`
in the Claude JSON, the `model:` line in the Codex stderr header, or the model label in the `agy` log. A model it cannot
confirm counts as a mismatch. This exists because sessions opened without a model ran on the wrong one.

**Environment of the child.** Besides `HARNESS_DELEGATE_RUN=1`, the child gets no variable that names another host:
`CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` are removed unless the vendor is `claude`, and `ANTIGRAVITY_CONVERSATION_ID` unless
it is `google`, so a tool run inside a worker does not take the host for another CLI.

**Git worktrees and agy hooks.** When a `google` run starts in a linked git worktree of a checkout whose
`.agents/hooks.json` has the `tagteam` key, the launcher writes that key into the worktree's `.agents/hooks.json` before the
first git snapshot (other keys stay; the guard path is absolute, so the copy calls the main checkout's guard) and the report
gets `HOOKS copied to <path>`. If git tracks that file there, or it is a link, or cannot be read or written, nothing is
written and the report gets `WARNING: agy hooks not copied (...)`: the worker may then run without the guard. A run in the main
checkout, or outside git, copies nothing and says nothing.

**Output.** Records go to `runs/<id>/` in the state folder: `prompt.txt`, `started.json` (written before the CLI starts: the
launcher's pid, the start time, vendor, model, effort and timeout), `stdout.txt`, `stderr.txt`, `cmd.json`, `last.txt` (the
worker's final text), `result.json` (it holds `run_exit`, the launcher's own exit code), `report.txt` (the report below,
written last, so a file that exists is complete), and `agy.log` for Google. The first line printed is `RUN <id> started
run_dir=...`, so a running worker's logs can be found. The report is `RUN <id> vendor=... model=... effort=... status=...
exit=... secs=...`, then `ACTUAL_MODEL: <model> (match)`, `EXHAUSTED:`, `ERROR:` or `WARNING:` lines where they apply
(`HOOKS copied to` or `WARNING: agy hooks not copied` among them), the `OUTPUT:` path of `last.txt`, and the first 4000
characters of the final text.

**Looking a run up.** The report goes to stdout only once, and a long command may leave the orchestrator's shell before it
ends (agy hands a command to the background after about ten seconds), so the stored report can be fetched again. The id is
the name after `RUN` in the first line, and the lookup works from any folder of the same repository, also in a later session:

- `run.py --status ID` prints the stored report and exits with the launcher's original exit code. If there is no report and the
  launcher process named in `started.json` is alive, it prints `RUN <id> running secs=<n> run_dir=<dir>` and exits **9**. If
  there is no report and that process is gone, it prints `RUN <id> ended without a report` and exits 6.
- `run.py --wait ID [--secs N]` does the same, but waits up to N seconds (default 8, at most 9) for the report, looking once
  a second. The limit is below the ten seconds after which agy moves a command to the background, so a call returns inside
  one wait; repeat it while it exits 9. [host: agy 1.5.10, measured 2026-11-18 / re-test after an agy update]
- A bad or unknown id, or another option together with `--status`, exits 2. Whether the process is alive is asked of the
  operating system (on Windows through the Windows API: `os.kill` is not a liveness check there). A pid that the system
  reused for another program after the launcher died looks like a running worker until the run's timeout plus two minutes
  have passed.

| Exit | Status | Meaning |
|---|---|---|
| 0 | `ok` | |
| 1 | `cli_failed` | CLI failed; an error that is not a usage-limit message (for example a short-lived authentication failure) is this, not exhaustion; run it again |
| 2 | | usage, configuration or pre-run check |
| 3 | `exhausted` | vendor exhausted |
| 4 | `model_mismatch` | model differs or cannot be confirmed |
| 5 | `timeout` | timeout |
| 6 | `no_output` | the CLI exited cleanly with an empty answer (the report names any tools the CLI denied in headless mode); also `--status`/`--wait` on a run that ended without a report |
| 7 | `git_head_moved` | git HEAD or branch moved during the run |
| 8 | `wrote_files` | a read-only run changed the work tree |
| 9 | (`running`) | `--status`/`--wait` only: the run has not reported yet |
| 10 | `control_plane_changed` | a write run changed the control plane (below) |
| 11 | `hooks_not_run` | a Codex worker's hooks wrote no role record (below) |
| 12 | `refused` | the CLI exited cleanly and the answer's first line starts with `REFUSED:` (the worker's refusal, AGENTS.md section 2) |

When several apply, 3 wins, then 7, 10, 11, 12, 5, 4, 8, 1, 6.

`refused` is judged on a clean CLI exit only, from the first line of the answer (a leading BOM or blank space is ignored; the
text must be written `REFUSED:` as in AGENTS.md), for every vendor and for read-only runs. The report shows an `ERROR:` line with
the refusal; read the reason, it is often a blocked tool or a missing input, not a failed task. A first line `NEEDS_INPUT:` is a
normal question: the status stays `ok`, and that line is repeated in the report header so it is not missed.

## 8. Keeping the model list current

Vendors rename and add models often. The collector (no language model) asks each CLI what it offers; a skill applies the
result. Benchmark scores and prices come from the free Data API of [Artificial Analysis](https://artificialanalysis.ai/);
its terms require crediting it wherever the data is shown or shared. The values stay in the machine-local
`bench.local.json` (its `source` field names them) and are not part of this repository.

    python3 .claude/tagteam/models_collect.py [--project DIR] [--timeout 60] [--mark-applied]
    python3 .claude/tagteam/models_collect.py --write-bench [--timeout 60]
    python3 .claude/tagteam/models_collect.py --write-available [--timeout 60]

- Without flags it lists models through `codex debug models`, `agy models` and `claude -p /model` (Claude Code lists
  model families only, so Claude availability is per family; it runs in a temporary folder), compares them with
  `models.json`, and writes `model-update.json` in the state folder. It never edits `models.json`. Exit: 0 collected; 1 some
  vendor failed or was missing, or a key is set and the benchmark fetch failed (even when every CLI answered); 2 usage or
  configuration error; 3 every vendor CLI failed.
- With `TAGTEAM_AA_API_KEY` set, it also reads the free Artificial Analysis list (at most 10 pages per run, daily quota) and
  adds benchmark changes to the proposal. The key is never written to a file.
- `--write-bench` writes `bench.local.json` for every registered model and effort that has a value, keeping entries from
  other sources; without a key it writes nothing and exits 0. `--write-available` writes the `available` lists into
  `models.local.json` for the vendors whose CLI answered. `--mark-applied` records the current proposal as handled.

Change kinds in a proposal: `new_model`, `now_available`, `no_longer_available`, `efforts_dropped`, `missing_registered`,
`bench_changed`, `bench_added`, `grade_changed`.

### 8.1 Applying a proposal

When a proposal is waiting, the next orchestrator session starts with `TAGTEAM: model update proposal (...)`. The
orchestrator then loads the skill `model-update` ([SKILL.md](../harness/skills/model-update/SKILL.md)): it records
the route table before and after, refreshes benchmark values, registers new models (as `candidate: true` until a probe and
two agreeing sources allow a route change), holds deletions and default-route changes that lack evidence, commits
`models.json` only if it changed, and marks the proposal applied. Workers never apply a proposal. A proposal older than 14
days makes the start line warn that collection stopped; check the scheduled task.

### 8.2 Scheduling

    python3 install.py <project dir> --schedule [--dry-run]
    python3 install.py <project dir> --unschedule [--dry-run]

Registers a weekly run (Monday 09:00) of `models_collect.py --project <project dir>` under the name `tagteam-models-<8 hex
digits>` (Windows Task Scheduler), as a launchd agent `net.tagteam.<name>` (macOS) or a crontab line ending in `# <name>`
(Linux). `--dry-run` prints what would be registered and starts nothing (on Linux the crontab line, as `  crontab  <line>`). `--unschedule` removes the task and deletes
`model-update.json`, `model-update.applied` and `model-update.log`. The scheduled run reads the user's environment, so set
`TAGTEAM_AA_API_KEY` as a user-level variable if you want benchmark data (launchd does not pass it). Per-platform
details: [platform/](platform/windows.md).

On macOS and Linux a launchd or cron job gets only `/usr/bin:/bin:/usr/sbin:/sbin`, so the installer gives the job its own
`PATH`: the folders of the CLIs, of the runtime a CLI script needs (for example `node`), of the Python that runs the
collector, and those system folders. `--schedule` prints that PATH and runs each CLI's `--version` with it (a failure is a
WARN; with `--dry-run` it only says what it would run). launchd and cron also write the job's output to `model-update.log`
in the state folder (the crontab line ends in `>> <state folder>/model-update.log 2>&1`; the folder is made when the job is
registered), so a failing run leaves a trace: without it cron mails the output, and a server without a mail system loses it.

## 9. When something goes wrong

| Symptom | Likely cause | Action |
|---|---|---|
| `start_session needs an explicit model and effort` | session opened without both | take both from `route.py`; a stored proposal started by `proposal_id` alone is allowed |
| `workers may not run git commit or git push` | worker tried to commit | the orchestrator commits; keep the worker's files and commit them yourself |
| the same message on the **agy orchestrator**, ending `role recorded as worker: the first model call had no user input` or `role unresolved: ...` | the guard recorded the interactive conversation as a worker, or lost the first decision | section 1.3, item 3 (delete the conversation's record, start a new conversation); if it names the state folder, fix the folder first (section 2) |
| `worker sessions may not open sessions` or `spawn subagents` | worker tried to delegate | do the split in the orchestrator |
| agy: every `run_command` is blocked, with a message from the hook (or none) | the guard cannot start or answer (no Python, wrong guard path, moved project, timeout) | section 1.3: `install.py <project dir> --update`, `--check --host agy`; or delete the `"tagteam"` key |
| `run.py --status`/`--wait` exits 9 | the run is still going | repeat `--wait`; read `runs/<id>/stderr.txt` if it never ends. `run.py` ends the worker itself when the `--timeout` given at launch has passed (default 1800 s); to stop it earlier, end the `run.py` process by hand and check that its CLI is gone |
| `RUN <id> ended without a report` (exit 6) | the launcher died before it could report (killed, machine restarted) | read the files in `runs/<id>/`; check the work tree with `git status`; launch again with a fresh brief |
| Router exit 3 or 4 | no vendor other than the author's can review at that grade (3), or no usable model (4) | start a review elsewhere or report it incomplete (3); check `plans`, `enabled`, `available`, `exhausted.json` (4) |
| Notice says "Check orchestrator cap" | router was not told the orchestrator's model or effort | pass `--orchestrator-model` and `--orchestrator-effort` |
| Launcher `model_mismatch` (4) | CLI used another model, or the model line was not found | read `runs/<id>/stderr.txt` and `cmd.json`; do not use the output as if it came from the requested model |
| Launcher `exhausted` (3), `git_head_moved` (7), `wrote_files` (8) | usage limit; a worker committed; a read-only run wrote | wait for `until` or let the router use another vendor; read the `ERROR:` line and review the diff (section 7) |
| Launcher `control_plane_changed` (10) | a write run changed `.claude/`, `.codex/`, `.agents/`, `AGENTS.md`, `CLAUDE.md` or a hook file (the worker, or another process in the checkout) | read the `ERROR:` line; check `git status` and `git diff` of those paths, restore what is wrong (section 9.1), and do not edit them while a worker runs |
| Launcher `refused` (12) | the worker answered `REFUSED: <reason>`: for example a hook or the sandbox blocked every tool, or the brief asked for something outside its limits | read the reason in the `ERROR:` line; fix the cause (a broken guard: `install.py --check`) or the brief, then run it again |
| Launcher `hooks_not_run` (11) | a Codex worker's hooks wrote no role record: usually the project or its hooks are not trusted; or the hooks ran and could not write it (state folder) | run `codex` once in the project, trust it, then the hooks (`/hooks`, section 1.2); `install.py <project dir> --check --host codex --live`; review what the worker changed |
| Automatic turn cannot open a session | host limit | open it on your next turn, or reuse a pool session by message |
| `tagteam: python3/python not found on PATH; tool calls are blocked ...` | no working Python for the Claude hooks | install Python 3.9+ (`python3`, on Windows `python`); a Windows Store stub does not count |
| `--check` reports `FAILED` | hook missing or changed, or no interpreter | read the line; `install.py <project dir> --update` (Codex: `--repair-hooks`, section 1.2), then `--check` again |
| Codex ignores a worker's `git commit` | hooks not trusted, or `python3` missing | `/hooks` in Codex (section 1.2); `--check --host codex` |

### 9.1 Recovering from a broken hook

If a change to a hook or to `.claude/settings.json` leaves Claude Code unable to work, repair it from a terminal, outside
Claude Code. **`git checkout <commit> -- <path>` overwrites that path's working copy, including uncommitted changes**: run
`git status` first. The paths must be tracked in git. Restore only the broken file, from a commit you know worked
(`git log --oneline -- <path>` lists candidates):

    git checkout <good commit> -- .claude/settings.json

The same works for `.claude/tagteam/role_guard.py`; for `.codex/hooks.json` prefer `--repair-hooks`; for agy see section 1.3
(`.agents/hooks.json` is git-ignored, so git cannot restore it). If you cannot tell
which file is broken, `git checkout HEAD~1 -- .claude .codex AGENTS.md` restores them all (assuming the previous commit
worked) and also reverts your own edits to `AGENTS.md` and the agent definitions. Then start a new session. The guard script
itself fails open: an internal error prints a warning and allows the action.

## 10. Model-compensation rules

Some rules exist only to correct one model's repeated mistake. Each ends with `[fix: <reason> / remove when <condition>]`.
List them with:

    grep -rn "\[fix:" AGENTS.md .claude .codex

After a model change, check each against its removal condition and delete the rule when the new model no longer needs it.
Rules without the tag are harness structure and stay. Handling and rationale:
[design.md](design.md#8-model-compensation-rules).

## 11. Developing the harness

In this repository:

    python3 -m unittest discover -s harness/tagteam/tests
    python3 -m unittest discover -s tests

The first covers the router, launcher, hook and collector (with fake CLIs); the second covers the installer, including
the hook commands run in a real shell with fake interpreters. Both are expected to pass before a change is merged.

There is no uninstall command. Remove the installed files by hand, run `--unschedule` first if you registered the weekly
check.
