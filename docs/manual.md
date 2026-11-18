# tagteam manual

How to install, run and maintain the harness. Why it is built this way: [design.md](design.md). The rules the agents follow,
and the term table (orchestrator, worker, router, launcher, session pool, ...): [AGENTS.md.template](../AGENTS.md.template).
Terms are defined only there.

Paths: `install.py`, `harness/...` and `docs/...` are in the cloned repository. `.claude/...`, `.codex/...` and `AGENTS.md`
are in the project you installed into. Commands are written `python3`; on Windows use `python`.

## What is where

| Section | Answers |
|---|---|
| [1. Install and update](#1-install-and-update) | What is overwritten? What else does the installer do? 1.1 is it working? 1.2 Codex hook trust, repair after a move |
| [2. Where things live](#2-where-things-live) | Which file holds what; which are committed; the state folder |
| [3. Running a mission](#3-running-a-mission) | How a mission runs; when sessions open and what to do if the host refuses (3.1); worker definitions (3.2); Codex as orchestrator (3.3) |
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
| Antigravity CLI (`agy`, Gemini models) | not yet ([design gaps](design.md#10-known-gaps)) | launcher only | |

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
| `harness/codex/hooks.json` | `.codex/hooks.json` | written once, then kept |
| `harness/codex/agents` | `.codex/agents` | overwritten |
| `AGENTS.md.template` | `AGENTS.md` | the part above the `## Project policy` heading is replaced; that heading and everything below it is kept (marker rule below) |
| `CLAUDE.md.template` | `CLAUDE.md` | created, or the line `@AGENTS.md` is appended if missing |

Also kept as they are: `models.local.json`, `bench.local.json`, the machine-local `.codex/hooks.json`.

**The `AGENTS.md` marker rule.** The installer looks for the exact text `## Project policy`. Present: that line and
everything below it is kept, everything above is replaced by the template. Absent: the whole old file is kept, unchanged,
under a new `## Project policy` heading after the template body (`kept (moved under Project policy)`). Either way, review
`AGENTS.md` afterwards and move your rules where you want them; commit or copy the old file first.

Other things the installer does:

| What | Behaviour |
|---|---|
| `models.local.json` | If missing: in a terminal it asks the plan for each vendor (`none`, `free`, `pro`, `max`; Enter keeps `pro`; `none` removes that vendor from routing). With `--yes` or without a terminal it copies `models.local.json.example`. Check and edit the result (section 5.2). |
| Benchmark and available models | If `TAGTEAM_AA_API_KEY` is set and `bench.local.json` does not exist, runs `models_collect.py --write-bench` once (15 second timeout); if a vendor with a plan other than `none` has no `available` list, runs `--write-available` once. A failure prints a warning; the install continues. |
| `.gitignore` | Adds `.claude/tagteam/models.local.json`, `.claude/tagteam/bench.local.json` and `.tagteam/`. |
| State folder | Moves records of an old `<git common dir>/tagteam/` into `.tagteam/` once (section 2). |
| Weekly check | In a terminal, a plain install (not `--update`) asks whether to register it (section 8.2). |

### 1.1 Check

    python3 install.py <project dir> --check [--host claude|codex|agy] [--live]

Needs a finished install. `--host` (default `claude`) picks the host to check. What was not run is reported `unverified`;
a failure ends with `check: FAILED` (exit code 1), otherwise `check: ok`.

- **static**: the tools on PATH (a missing CLI is a warning), the interpreter the host's hooks call (`python3` or `python`
  for Claude; `python3`, on Windows `python`, for Codex; a missing one is an error, one found only through your shell
  PATH a warning), the installed files, the state folder (path, `.gitignore`, an old folder left over), and for Codex the
  drift of `.codex/hooks.json`.
- **handler**: fake payloads in the host's own format through the installed `role_guard.py`.
  Claude: `start_session` without model (denied), a worker `git commit` (denied), a worker `git status` (allowed); it also
  runs **hook commands**: each tagteam command of `.claude/settings.json`, exactly as written, in the shell Claude Code uses
  (`sh`; Git Bash on Windows) with your PATH, from the project folder, within the hook's timeout. A command that finds no
  Python, or does not answer in time, is a failure: the guard would be off without a word.
  Codex: `spawn_agent` by a worker, without model and effort, or with them for a custom agent; a subagent `git commit`; a
  worker `apply_patch` on `AGENTS.md` (all denied); `git status` and a plain custom-agent spawn (allowed).
- **live** (`--live`, uses tokens): runs the real CLI once as a marked worker and requires a valid role record. On Codex
  run it only after trusting the hooks (section 1.2): untrusted hooks write no record.

`--check` cannot see whether Codex trusts the hooks (section 1.2). For agy only the static part exists.

### 1.2 Moving a project

Codex ties its trust of a hook to the hook definition and the guard path, and **skips a new or changed entry without any
message**. After `install.py` (first run), `--update` or `--repair-hooks`, open `codex` in the project and trust the hooks
with `/hooks`; until then the role guard is off there. `.codex/hooks.json` is machine-local: it holds the absolute path
of the guard and the installer never rewrites an existing file.

After moving or copying a project, or in a worktree that received a copy of the file, or when `--check --host codex`
reports drift (an entry missing, or a wrong path):

    python3 install.py <project dir> --repair-hooks          # preview
    python3 install.py <project dir> --repair-hooks --yes    # apply

It touches only the tagteam entries, writes `hooks.json.bak` first and prints the diff. Then trust the hooks again.

## 2. Where things live

| Location | Contents | Committed |
|---|---|---|
| `AGENTS.md` | rules; `## Project policy` is yours | yes |
| `.claude/settings.json`, `.codex/hooks.json` | hook wiring | settings yes; `hooks.json` is machine-local by design |
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
message that names it (plain routing only reads it); the hook only warns and allows the action, so a role record that cannot be written leaves the guard off for that session.

| Entry | Written by | Meaning |
|---|---|---|
| `sessions/<id>.json` | hook | role of each session (`worker` or `orchestrator`), recorded once |
| `agents.jsonl` | hook (Codex `SubagentStart`) | the model each Codex subagent really runs on |
| `model-update.log` | the scheduled job (macOS) | output of the weekly collector run |
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
reuse one by message.** A turn that starts because a worker reported back is an *automatic turn*. Measured on Claude Code
desktop 2.1 (2026-11-17): `start_session` is refused in an automatic turn, but a message to an already open session works.
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
   (design, implementation, one review, two commits) took six, all approved by the user. If a commit is refused the unit is
   verified but not committed; a refused review launch leaves it incomplete.
5. On Windows, `windows.sandbox = "elevated"` breaks `codex exec` and temporary folders may be denied inside the sandbox
   (`--check` and the tests then need escalation): [platform/windows.md](platform/windows.md#codex).

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

Always pass `--host`; a guess from the environment can be wrong. Without `--json` it prints one notice line, which the
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
`user_override`, `bench:local`, `bench:none`.

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
another grade than the author's is allowed, with `override_grade_mismatch` in the notice.

### 5.3 Exhaustion

A vendor with a usage limit is recorded in `exhausted.json`, and the router substitutes the same grade on another vendor.

    python3 .claude/tagteam/route.py --mark-exhausted VENDOR --evidence TEXT [--until ISO8601]
    python3 .claude/tagteam/route.py --clear-exhausted VENDOR

Without `--until` the entry lasts `cooldown_minutes` of that vendor (60). The launcher records exhaustion itself when a run
shows a usage-limit message, and clears the entry after a successful run.

## 6. The role hook

The single hook is `.claude/tagteam/role_guard.py`, called as `role_guard.py <SessionStart|UserPromptSubmit|PreToolUse|
SubagentStart> [--host claude|codex]` with the host's JSON payload on stdin. A Claude denial is exit code 2 with a one-line
reason on stderr; a Codex denial is a JSON answer with exit 0 (Codex ignores exit 2). What it blocks, host by host, and
what it cannot block: [design.md](design.md#5-enforcement).

At session start it prints `TAGTEAM: role=<role> host=<host>`, and for an orchestrator one more line when a model-update
proposal is waiting (section 8).

If a session was recorded with the wrong role (for example an orchestrator whose first prompt quoted the marker line),
delete `sessions/<id>.json` in the state folder and restart that session; otherwise the role is fixed.

## 7. The launcher

Runs one worker through another vendor's CLI.

    python3 .claude/tagteam/run.py --vendor claude|openai|google --model ID --effort E
        --prompt-file PATH [--cwd DIR] [--timeout SECONDS] [--read-only] [--run-commands]
        [--allow-command "<exact command>" ...]

The router prints this command ready to fill in. Checks before the run (exit 2 on failure): the process is not itself a
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

**Commands in `agy` runs.** In headless mode `agy` denies every shell command unless it is allowed, and `--mode plan` never
runs commands. A Google run always gets `--settings '{"permissions":{"allow":["command(*)"]}}'` (that run only). So
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

**Model check.** After a clean exit the launcher compares the model that answered with the one requested, from `modelUsage`
in the Claude JSON, the `model:` line in the Codex stderr header, or the model label in the `agy` log. A model it cannot
confirm counts as a mismatch. This exists because sessions opened without a model ran on the wrong one.

**Output.** Records go to `runs/<id>/` in the state folder: `prompt.txt`, `stdout.txt`, `stderr.txt`, `cmd.json`, `last.txt`
(the worker's final text), `result.json`, and `agy.log` for Google. The first line printed is `RUN <id> started
run_dir=...`, so a running worker's logs can be found. The report is `RUN <id> vendor=... model=... effort=... status=...
exit=... secs=...`, then `ACTUAL_MODEL: <model> (match)`, `EXHAUSTED:`, `ERROR:` or `WARNING:` lines where they apply, the
`OUTPUT:` path of `last.txt`, and the first 4000 characters of the final text.

| Exit | Status | Meaning |
|---|---|---|
| 0 | `ok` | |
| 1 | `cli_failed` | CLI failed; an error that is not a usage-limit message (for example a short-lived authentication failure) is this, not exhaustion; run it again |
| 2 | | usage, configuration or pre-run check |
| 3 | `exhausted` | vendor exhausted |
| 4 | `model_mismatch` | model differs or cannot be confirmed |
| 5 | `timeout` | timeout |
| 6 | `no_output` | the CLI exited cleanly with an empty answer (the report names any tools the CLI denied in headless mode) |
| 7 | `git_head_moved` | git HEAD or branch moved during the run |
| 8 | `wrote_files` | a read-only run changed the work tree |

When several apply, 3 wins, then 7, 5, 4, 8, 1, 6.

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
(Linux). `--dry-run` prints what would be registered and starts nothing. `--unschedule` removes the task and deletes
`model-update.json`, `model-update.applied` and `model-update.log`. The scheduled run reads the user's environment, so set
`TAGTEAM_AA_API_KEY` as a user-level variable if you want benchmark data (launchd does not pass it). Per-platform
details: [platform/](platform/windows.md).

On macOS and Linux a launchd or cron job gets only `/usr/bin:/bin:/usr/sbin:/sbin`, so the installer gives the job its own
`PATH`: the folders of the CLIs, of the runtime a CLI script needs (for example `node`), of the Python that runs the
collector, and those system folders. `--schedule` prints that PATH and runs each CLI's `--version` with it (a failure is a
WARN; with `--dry-run` it only says what it would run). launchd also writes the job's output to `model-update.log` in the
state folder.

## 9. When something goes wrong

| Symptom | Likely cause | Action |
|---|---|---|
| `start_session needs an explicit model and effort` | session opened without both | take both from `route.py`; a stored proposal started by `proposal_id` alone is allowed |
| `workers may not run git commit or git push` | worker tried to commit | the orchestrator commits; keep the worker's files and commit them yourself |
| `worker sessions may not open sessions` or `spawn subagents` | worker tried to delegate | do the split in the orchestrator |
| Router exit 3 or 4 | no vendor other than the author's can review at that grade (3), or no usable model (4) | start a review elsewhere or report it incomplete (3); check `plans`, `enabled`, `available`, `exhausted.json` (4) |
| Notice says "Check orchestrator cap" | router was not told the orchestrator's model or effort | pass `--orchestrator-model` and `--orchestrator-effort` |
| Launcher `model_mismatch` (4) | CLI used another model, or the model line was not found | read `runs/<id>/stderr.txt` and `cmd.json`; do not use the output as if it came from the requested model |
| Launcher `exhausted` (3), `git_head_moved` (7), `wrote_files` (8) | usage limit; a worker committed; a read-only run wrote | wait for `until` or let the router use another vendor; read the `ERROR:` line and review the diff (section 7) |
| Automatic turn cannot open a session | measured platform limit | open it on your next turn, or reuse a pool session by message |
| `tagteam: python3/python not found on PATH; tool calls are blocked ...` | no working Python for the Claude hooks | install Python 3.9+ (`python3`, on Windows `python`); a Windows Store stub does not count |
| `--check` reports `FAILED` | hook missing or changed, or no interpreter | read the line; `install.py <project dir> --update` (Codex: `--repair-hooks`, section 1.2), then `--check` again |
| Codex ignores a worker's `git commit` | hooks not trusted, or `python3` missing | `/hooks` in Codex (section 1.2); `--check --host codex` |

### 9.1 Recovering from a broken hook

If a change to a hook or to `.claude/settings.json` leaves Claude Code unable to work, repair it from a terminal, outside
Claude Code. **`git checkout <commit> -- <path>` overwrites that path's working copy, including uncommitted changes**: run
`git status` first. The paths must be tracked in git. Restore only the broken file, from a commit you know worked
(`git log --oneline -- <path>` lists candidates):

    git checkout <good commit> -- .claude/settings.json

The same works for `.claude/tagteam/role_guard.py`; for `.codex/hooks.json` prefer `--repair-hooks`. If you cannot tell
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
