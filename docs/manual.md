# tagteam manual

How to install, run and maintain the harness. For why it is built this way, see
[design.md](design.md). The rules the agents follow are in
[AGENTS.md.template](../AGENTS.md.template); terms in *italics* there (orchestrator,
worker, router, launcher, session pool, ...) are defined once in its term table and
not repeated here.

Paths: `install.py`, `harness/...` and `docs/...` are in the cloned repository.
`.claude/...`, `.codex/...` and `AGENTS.md` are in the project you installed into.

## 1. Install and update

    python install.py <project dir> [--update] [--check] [--yes] [--source DIR]
    python install.py <project dir> --schedule | --unschedule [--dry-run]

A plain run creates the project folder if needed, copies the files and prints one line
per file as `created`, `updated` or `kept`.

| Source | Goes to | On update |
|---|---|---|
| `harness/claude/settings.json` | `.claude/settings.json` | only the `hooks` and `subagentPromptCacheTtl` keys are replaced; other keys stay |
| `harness/claude/agents`, `skills` | `.claude/agents`, `.claude/skills` | overwritten |
| `harness/tagteam/*` (not `tests/`) | `.claude/tagteam/` | overwritten; files with `.local.` in the name are not copied (the example file only seeds `models.local.json`) |
| `harness/codex/hooks.json` | `.codex/hooks.json` | written once, then kept |
| `harness/codex/agents` | `.codex/agents` | overwritten |
| `AGENTS.md.template` | `AGENTS.md` | the part above the `## Project policy` heading is replaced by the template; that heading and everything below it is kept (see the marker rule below) |
| `CLAUDE.md.template` | `CLAUDE.md` | created, or the line `@AGENTS.md` is appended if missing |

**Customizations in harness-named files are lost.** A plain install and `--update`
both overwrite any differing file in `.claude/agents`, `.claude/skills`,
`.claude/tagteam/` (including `models.json`) and `.codex/agents`, and replace the
`hooks` and `subagentPromptCacheTtl` keys of an existing `.claude/settings.json`.
Back up or commit anything you changed there before installing. Change per-machine
behaviour in `.claude/tagteam/models.local.json` instead (section 5.3); edit
`models.json` only in a copy of the repository you maintain yourself.

**The `AGENTS.md` marker rule.** The installer looks for the exact text
`## Project policy`. If your `AGENTS.md` contains it, everything from that line down is
kept and everything above it is replaced by the template. If it does not contain it,
the whole old file is kept, unchanged, under a new `## Project policy` heading placed
after the template body, and the install prints `kept (moved under Project policy)`.
Either way, review `AGENTS.md` afterwards and move your rules into the layout you
want; commit or copy the old file first if you cannot afford a surprise.

Other things the installer does:

- **`models.local.json`.** If missing: in a terminal it asks the plan for each vendor
  (`none`, `free`, `pro`, `max`; Enter keeps the default, `pro`; `none` removes that
  vendor from routing). With `--yes` or without a terminal it copies `models.local.json.example`
  instead. Check the result and edit it (section 5.3).
- **Benchmark file.** If `TAGTEAM_AA_API_KEY` is set and `bench.local.json` does not
  exist, it runs `models_collect.py --write-bench` once (15 second timeout). Failure
  prints a warning and the install continues.
- **Available models.** If some vendor with a plan other than `none` has no `available`
  list in `models.local.json`, it runs `models_collect.py --write-available` once.
- **`.gitignore`.** Adds `.claude/tagteam/models.local.json` and
  `.claude/tagteam/bench.local.json`, and `.tagteam/` when the target is not a git
  repository.
- **Weekly check.** In a terminal, a plain install (not `--update`) asks whether to
  register it (section 8.2).

`--update` runs the same copy as a plain install; it only changes the wording and
skips the weekly-check question. `--source DIR` installs from another copy of the
repository.

### 1.1 Check

    python install.py <project dir> --check

Needs a finished install. It looks for `python`, `git`, `claude`, `codex` and `agy` on
PATH (a missing tool is a warning, not an error) and warns when `python` resolves to a
Windows Store alias. It then feeds three fake payloads to the installed
`role_guard.py` (start_session without model, a worker `git commit`, a worker
`git status`) and expects deny, deny, allow. It ends with `check: ok` or
`check: FAILED` (exit code 1).

### 1.2 Moving a project

`.codex/hooks.json` holds the absolute path of the guard, and Codex ties its trust of a
hook to that path and to the hook definition. The installer never rewrites an existing
file, so after moving or copying a project, or in a git worktree that received a copy
of the file, the path inside may still point at the old location. In each moved
project or worktree, open `.codex/hooks.json` and check that the guard path is the one
of that project.

If it is wrong: copy any custom entries you added to a safe place, delete
`.codex/hooks.json`, run `python install.py <project dir> --update` (it writes a fresh
file from the shipped one), merge your entries back and trust the hook again when
Codex asks. A new worktree path always asks for trust again.

## 2. Where things live

| Location | Contents | Committed |
|---|---|---|
| `AGENTS.md` | rules; `## Project policy` is yours | yes |
| `.claude/settings.json`, `.codex/hooks.json` | hook wiring | settings yes; `hooks.json` is machine-local by design |
| `.claude/agents`, `.codex/agents` | worker definitions with fixed model and effort | yes |
| `.claude/skills/cycle`, `model-update` | orchestrator procedures | yes |
| `.claude/tagteam/models.json` | vendors, roles, models, grades, defaults | yes |
| `.claude/tagteam/models.local.json` | plans, hidden models, available lists, role overrides | no |
| `.claude/tagteam/bench.local.json` | benchmark values from Artificial Analysis | no |
| state folder | records below | no |

The *state folder* is `<git common dir>/tagteam/`, so all worktrees of one repository
share it. Outside git it is `<project>/.tagteam/`. It holds:

| Entry | Written by | Meaning |
|---|---|---|
| `sessions/<id>.json` | hook | role of each session (`worker` or `orchestrator`), recorded once |
| `exhausted.json` | `run.py`, `route.py` | vendors temporarily unusable, with `until` |
| `runs/<id>/` | `run.py` | one launcher run (section 7) |
| `model-update.json` | `models_collect.py` | pending proposal (section 8) |
| `model-update.applied` | `models_collect.py --mark-applied` | digest of the last handled proposal |
| `collect.log` | `models_collect.py` | one line per collector run |

## 3. Running a mission

The procedure is the skill `cycle`
([SKILL.md](../harness/claude/skills/cycle/SKILL.md)); the rules are AGENTS.md
section 3. In short:

1. For anything with more than one unit, the orchestrator creates
   `docs/missions/<purpose>/` with `intent.md` and `spec.md`, and waits for your
   confirmation of the spec.
2. On a turn you started, it opens the session pool: one worker session per role it
   needs, each with model and effort taken from the router.
3. A design session writes the design document; implementation sessions implement
   units; the orchestrator checks every return against the real diff and commits.
4. Expensive checks and the cross-vendor review run at chosen cut points.

### 3.1 The session pool and automatic turns

A turn that starts because a worker reported back is an *automatic turn*. Measured on
Claude Code 2.1: `start_session` is refused in an automatic turn, but a message to an
already open session works. So sessions are opened while you are present and reused by
message afterwards. Consequences:

- Say "go" on a user turn when new roles are needed; otherwise the orchestrator can
  only continue with sessions it already has.
- A reused session keeps the cache of its prompt for 1 hour. After a longer pause the
  next task pays for the prompt again; a brand-new session costs roughly 90,000
  tokens of system and tool prompt before any work starts. Keep sessions per role,
  not per task.
- The worker marker line `HARNESS ROLE: DELEGATE` counts only in the **first** prompt
  of a session. A task sent later by message is not seen by the hook that records the
  role. The orchestrator therefore puts the line in every session's first prompt.
- A new git worktree does not initialize submodules. If a unit edits a submodule, its
  brief must start with `git submodule update --init <path>`.
- Most of a long mission's cost is the orchestrator re-reading its own growing context
  on every call, not the workers. An option, not a rule: at the end of a phase (never
  mid-unit), hand the mission to a fresh orchestrator session. Before that, make sure
  `status.md` holds the decisions, open questions, work in flight and next step. A
  lighter orchestrator knows less, so try it once and compare before relying on it.

### 3.2 Worker definitions

Subagents with a fixed definition carry their own model and effort. They are not chosen
by the router and an update restores them to the shipped values.

| Host | Name | Model, effort | Tools or limits |
|---|---|---|---|
| Claude | `designer` | `opus`, high | Read, Grep, Glob, Write; writes one design document |
| Claude | `implementer` | `sonnet`, high | Read, Write, Edit, Bash, Grep, Glob |
| Claude | `reader` | `haiku`, high | Read, Grep, Glob, Bash; read-only |
| Codex | `implementer` | `gpt-6.1-sol`, medium | `.codex/agents/implementer.toml` |
| Codex | `reader` | `gpt-6.1-terra`, low | `.codex/agents/reader.toml` |

Codex has no designer definition; use the launcher for a design or review worker on
another vendor. On a Codex host the loop runs through Codex subagents and the
launcher, because Codex sessions could not message each other in the tested version
and, on Windows, `.git` is read-only inside its sandbox, so Codex workers cannot commit.

## 4. Verification and review

AGENTS.md section 4 defines three levels (Tier 0, 1, 2), review-and-fix and batching.
Operationally:

- Fill in `Verification commands` in `## Project policy` with the exact commands and
  what counts as passing. Workers run the commands named in their brief.
- A reviewer must be another vendor at the author's grade. Ask the router (section 5)
  with `--author-vendor` and `--grade`. If it exits 3, report the review as
  incomplete rather than reviewing with the author's vendor.
- Read-only review: the router adds `--read-only` only to a `run.py` launch, and the
  launcher passes it to the vendor CLI. A native review session (a Claude or Codex
  subagent or session) gets no such setting from the router; its read-only behaviour
  comes from the brief and from the worker definition's tools, so state it in the brief.

## 5. The router

Run it before every new session or launcher run (AGENTS.md section 1).

    python .claude/tagteam/route.py --role ROLE [--host claude|codex]
        [--author-vendor VENDOR] [--grade S|A|B|C]
        [--orchestrator-model ID] [--orchestrator-effort EFFORT] [--json]

Without `--json` it prints one notice line, which the orchestrator relays to you:
`<role> -> <model> (<effort>, <vendor>, grade <g>) via native session|run.py.`
`--host` defaults to `claude` when the environment variable `CLAUDECODE` is `1`, else
`codex`.

| Role | Default grade | Read-only | Notes |
|---|---|---|---|
| `design` | A | no | |
| `implement` | B | no | |
| `review` | A | yes | `--author-vendor` required; pass `--grade` of the author's model and effort |
| `read` | C | yes | |
| `translate` | B | no | vendors in the order google, openai, claude |

With `--json` the result has `status`, `role`, `model`, `effort`, `vendor`, `grade`,
`launch`, `reason` and `notice`. `launch.kind` is `native` (open a session or subagent
with that model and effort) or `run` (execute `launch.command`, a `run.py` call with
`<PROMPT_FILE>` to fill in).

Exit codes: 0 chosen; 2 usage or configuration error; 3 no independent reviewer
(`status` is `incomplete`); 4 no usable model.

### 5.1 How a choice is made

1. **Vendor order.** From `hosts.<host>` in `models.json` (Claude host: claude, openai,
   google; Codex host: openai, claude, google), with plan `free` vendors moved last. A
   role that has its own `vendors` list in `models.json` (only `translate`: google,
   openai, claude) uses that list instead: it wins over the host order and is not
   reordered for `free` plans. In both cases vendors with plan `none` are dropped, the
   author's vendor is dropped for a review, and vendors in `exhausted.json` and vendors
   whose CLI is missing are skipped. The first vendor that has a candidate wins.
2. **Candidates.** Every registered model and effort of that vendor whose entry in
   `grades` equals the role's grade, that is usable: plan not `none`, not hidden by
   `models.<id>.enabled`, and listed in `available` (a vendor without an `available`
   list counts as offering everything). A model marked `"candidate": true` in
   `models.json` (registered but not yet cleared for default routes) is never a
   candidate here; only a `roles.<role>.model` pin can select it.
3. **Pick order** among candidates. The first rule that matches any candidate decides:
   cost known and within `grading.caps` for the grade: highest index; no cost but an
   index: highest index; cost above the cap: lowest cost; nothing known: the first
   usable entry of `defaults.<vendor>.<grade>`, else the first model in `models.json`
   at its lowest effort. Values come from `bench.local.json` at exactly that effort.
4. **Orchestrator cap.** A native Claude session may not use a model listed earlier in
   `models.json` than the orchestrator's (earlier means stronger) or a higher effort.
   Candidates inside the cap are preferred; if none is, the launch becomes a
   `run.py` command. If a native Claude launch is chosen without
   `--orchestrator-model` and `--orchestrator-effort`, the notice adds "Check
   orchestrator cap."

`reason` lists what happened, for example `default`, `substituted:<from>→<to>`,
`plan_free_last:<vendor>`, `author_vendor_excluded:<vendor>`, `exhausted_skip:<vendor>`,
`cli_missing:<vendor>`, `native_model_cap`, `effort_clamped`, `user_override`,
`bench:local` or `bench:none`.

### 5.2 Grades

A grade (S, A, B, C) belongs to a model **at an effort**: a table `grades` per model in
`models.json`. The rule that produced the table is `grading.floors` applied to the
Artificial Analysis index: S at 54 or more, A at 48, B at 30, otherwise C.
`grading.caps` gives a relative cost ceiling per task in US dollars: S 10, A 4, B 2,
C 0.2. Both are this project's settings, not vendor figures. The collector flags a
model whose index and `grades` disagree (section 8).

### 5.3 Per-machine settings: `models.local.json`

| Key | Effect |
|---|---|
| `plans.<vendor>` | `none` removes the vendor; `free` moves it last; `pro`, `max` have no routing effect; a missing vendor counts as `pro` |
| `models.<id>.enabled` | `false` hides a model; `true` skips the `available` check for it |
| `available.<vendor>.ids` | ids the vendor CLI really offers (`checked` records the date); written by `--write-available` |
| `roles.<role>` | `model`, `effort`, `vendor`, `grade` to pin for a role |

A pinned model is used when it is usable, its vendor is not exhausted, and it is not
the author's vendor for a review. If the launch would go through `run.py` (another
vendor's model, or a native Claude session over the orchestrator cap) its vendor CLI
must also be present; a native launch of the host's own vendor needs no check.
Otherwise the router falls back to the normal choice and `reason` says why
(`override_unusable`, `override_exhausted`, `override_same_vendor_as_author`,
`cli_missing:<vendor>`). A `roles.<role>.effort` pin keeps limiting the candidates of
that fallback. A pinned reviewer of another
grade than the author's is allowed with `override_grade_mismatch` in the notice.

### 5.4 Exhaustion

A vendor with a usage limit is recorded in `exhausted.json` and the router substitutes
the same grade on another vendor.

    python .claude/tagteam/route.py --mark-exhausted VENDOR --evidence TEXT [--until ISO8601]
    python .claude/tagteam/route.py --clear-exhausted VENDOR

Without `--until` the entry lasts `cooldown_minutes` of that vendor (60). The launcher
records exhaustion itself when a run shows a usage-limit message, and clears the entry
after a successful run.

## 6. The role hook

The single hook is `.claude/tagteam/role_guard.py`, called as
`role_guard.py <SessionStart|UserPromptSubmit|PreToolUse> [--host claude|codex]` with
the host's JSON payload on stdin. A denial is exit code 2 with a one-line reason on
stderr. What it blocks and what it cannot block is in [design.md](design.md).

At session start it prints `TAGTEAM: role=<role> host=<host>`, and for an
orchestrator one more line when a model-update proposal is waiting (section 8).

If a session was recorded with the wrong role (for example an orchestrator whose first
prompt quoted the marker line), delete `sessions/<id>.json` in the state folder and
restart that session. The role is otherwise fixed for the life of a session.

## 7. The launcher

Runs one worker through another vendor's CLI.

    python .claude/tagteam/run.py --vendor claude|openai|google --model ID --effort E
        --prompt-file PATH [--cwd DIR] [--timeout SECONDS] [--read-only] [--run-commands]
        [--allow-command "<exact command>" ...]

The router prints this command ready to fill in. Checks before the run (exit 2 on
failure): the process is not itself a launcher worker (`HARNESS_DELEGATE_RUN=1`), the
model is registered for the vendor, the effort is one of its `grades`, a Google model
has a `label`, the CLI exists, the prompt file is readable. The prompt gets the marker
line `HARNESS ROLE: DELEGATE` on top if its first line is not already that, and the
child runs with `HARNESS_DELEGATE_RUN=1`. The default timeout is 1800 seconds.

| Vendor | CLI | Notable arguments |
|---|---|---|
| `claude` | `claude` | `-p --output-format json`, `--permission-mode plan` (read-only) or `auto` |
| `openai` | `codex` | `exec -s read-only|workspace-write`, `-C <cwd>`; on Windows also `-c windows.sandbox=unelevated` |
| `google` | `agy` | `-p`, `--mode plan` (read-only) or `accept-edits`, `--settings <json>`, `--log-file`; a very long prompt is passed as a file reference |

**Commands in `agy` runs.** In headless mode `agy` denies every shell command unless it is
allowed, and `--mode plan` never runs commands. A Google run always gets
`--settings '{"permissions":{"allow":["command(*)"]}}'` (applies to that run only). Modes:

- `--read-only`: `plan`. Truly read-only, but the worker cannot run tests or `git diff`.
- `--read-only --run-commands`: `accept-edits` with the allow list. The worker can run
  commands (a reviewer running tests and `git diff`); read-only is then enforced only by
  the git check below.
- neither: `accept-edits` with the allow list (implementation runs).

`--run-commands` and `--allow-command "<exact command>"` are Google only (exit 2 for other
vendors). `--allow-command` is repeatable and replaces the wildcard with
`command(<cmd>)` for each given command, for example only `git diff` and the test
command. On Windows the rule must match the command exactly (see
`docs/platform/windows.md`). If the allow list makes the command line too long, the
launcher exits 2.

**Git check (every vendor).** When `--cwd` is inside a git work tree, the launcher records
before and after the run: `git rev-parse HEAD`, the symbolic ref (`git symbolic-ref -q
HEAD`, empty when detached), `git status --porcelain` (untracked files included) and a
sha256 of every listed file (at most 2000 files and 50 MB per snapshot; past that the
check is partial and the report says so). Results:

- HEAD or the branch moved: status `git_head_moved` (exit 7).
- With `--read-only`, the status listing or the content of a listed file changed (this
  includes overwriting a file that was already modified): status `wrote_files` (exit 8).
- Each adds an `ERROR:` line naming the change. If git itself fails (timeout, error) the
  report gets `WARNING: git check unavailable (...)` and `result.json` gets
  `"git_check": "unavailable"`; the run is not failed for it. Outside a work tree, or
  without `git`, the check is skipped silently.
- The launcher's own files (the run folder and the state folder) are left out when they
  lie inside the work tree.

This is an accident check, **not a security boundary**, like the role guard's commit
check: ignored files, paths outside the repository, stashes and other refs are not
looked at. It also sees every change in the checkout during the run, including other
workers'. Run read-only reviews while no other writer uses the checkout, or in a separate
worktree.

**Model check.** After a clean exit the launcher compares the model that answered with
the one requested, from `modelUsage` in the Claude JSON, the `model:` line in the
Codex stderr header, or the model label in the `agy` log. A model it cannot confirm
counts as a mismatch. This exists because sessions opened without a model ran on the
wrong one.

**Output.** Records go to `runs/<id>/` in the state folder: `prompt.txt`,
`stdout.txt`, `stderr.txt`, `cmd.json`, `last.txt` (the worker's final text),
`result.json`, and `agy.log` for Google. The launcher prints a short report:

    RUN <id> vendor=... model=... effort=... status=... exit=... secs=...
    ACTUAL_MODEL: <model> (match)          (on status ok or model_mismatch)
    EXHAUSTED: <vendor> until <time> (<evidence>)   (only when exhausted)
    ERROR: could not start the CLI (...)            (only when the CLI did not start)
    ERROR: git HEAD moved ... / a read-only run changed the work tree: ...   (git check)
    WARNING: git check unavailable (...) / git content check is partial (...)  (git check)
    OUTPUT: <path to last.txt>
    ---
    <first 4000 characters of the final text>

Exit codes: 0 ok; 1 CLI failed; 2 usage, configuration or pre-run check; 3 vendor
exhausted; 4 model differs or cannot be confirmed; 5 timeout; 6 the CLI exited cleanly with
an empty answer (the report names any tools the CLI denied in headless mode); 7 git HEAD
or branch moved during the run; 8 a read-only run changed the work tree. When several apply, 3
wins, then 7, 5, 4, 8, 1, 6. A CLI error that is not a usage-limit message (for example a
short-lived authentication failure) counts as `cli_failed`, not as exhaustion; run it again.

The launcher is an accident check, not a sandbox: `--read-only` asks the CLI for its
read-only mode (Google with `--run-commands`: the git check) but does not replace review of
what a worker changed.

## 8. Keeping the model list current

Vendors rename and add models often. The collector asks each CLI what it offers and
what changed; a skill applies the result. No language model is involved in collecting.

    python .claude/tagteam/models_collect.py [--project DIR] [--timeout 60] [--mark-applied]
    python .claude/tagteam/models_collect.py --write-bench [--timeout 60]
    python .claude/tagteam/models_collect.py --write-available [--timeout 60]

- Without flags it lists models through `codex debug models`, `agy models` and
  `claude -p /model` (Claude Code lists model families only, so Claude availability is
  per family; it runs in a temporary folder), compares them with `models.json`, and
  writes `model-update.json` in the state folder. It never edits `models.json`.
  Exit: 0 collected; 1 some vendor failed or was missing, or a key is set and the
  benchmark fetch failed (even when every CLI answered); 2 usage or configuration
  error; 3 every vendor CLI failed.
- With `TAGTEAM_AA_API_KEY` set, it also reads the free Artificial Analysis list
  (at most 10 pages per run) and adds benchmark changes to the proposal. The key is
  never written to a file. The free API has a daily request quota.
- `--write-bench` writes `bench.local.json` from Artificial Analysis for every
  registered model and effort that has a value, and keeps entries from other
  sources. Without a key it writes nothing and exits 0.
- `--write-available` writes the `available` lists into `models.local.json` for the
  vendors whose CLI answered.
- `--mark-applied` records the current proposal as handled.

Change kinds in a proposal: `new_model`, `now_available`, `no_longer_available`,
`efforts_dropped`, `missing_registered`, `bench_changed`, `bench_added`,
`grade_changed`.

### 8.1 Applying a proposal

When a proposal is waiting, the next orchestrator session starts with
`TAGTEAM: model update proposal (...)`. The orchestrator then loads the skill
`model-update` ([SKILL.md](../harness/claude/skills/model-update/SKILL.md)): it
records the route table before and after, refreshes benchmark values, registers new
models (as `candidate: true` until a probe and two agreeing sources allow a route
change), holds deletions and default-route changes that lack evidence, commits
`models.json` only if it changed, and marks the proposal applied. Workers never apply
a proposal. After a default-route change the skill also lists the
model-compensation rules to reconsider (section 10).

If the proposal is older than 14 days, the start line instead warns that collection
stopped; check the scheduled task.

### 8.2 Scheduling

    python install.py <project dir> --schedule [--dry-run]
    python install.py <project dir> --unschedule [--dry-run]

Registers a weekly run (Monday 09:00) of `models_collect.py --project <project dir>`
under the name `tagteam-models-<8 hex digits>` (Windows Task Scheduler), as a launchd
agent `net.tagteam.<name>` (macOS) or a crontab line ending in `# <name>` (Linux).
`--dry-run` prints what would be registered. `--unschedule` removes the task and
deletes `model-update.json` and `model-update.applied`. The scheduled run reads the
user's environment, so set `TAGTEAM_AA_API_KEY` as a user-level variable if you want
benchmark data. Per-platform details are in [platform/](platform/windows.md).

## 9. When something goes wrong

| Symptom | Likely cause | Action |
|---|---|---|
| `start_session needs an explicit model and effort` | session opened without both | take both from `route.py`; a stored proposal started by `proposal_id` alone is allowed |
| `workers may not run git commit or git push` | worker tried to commit | the orchestrator commits; keep the worker's files and commit them yourself |
| `worker sessions may not open sessions` or `spawn subagents` | worker tried to delegate | do the split in the orchestrator |
| Router exit 3 | no vendor other than the author's is usable at that grade | start a review elsewhere, or report the review as incomplete |
| Router exit 4 | no usable model at that grade | check `plans`, `enabled`, `available` and `exhausted.json` |
| Notice says "Check orchestrator cap" | router was not told the orchestrator's model or effort | pass `--orchestrator-model` and `--orchestrator-effort` |
| Launcher status `model_mismatch` (exit 4) | CLI used another model, or the model line was not found | read `runs/<id>/stderr.txt` and `cmd.json`; do not use the output as if it came from the requested model |
| Launcher status `exhausted` (exit 3) | usage limit | wait for `until`, or let the router use another vendor |
| Automatic turn cannot open a session | measured platform limit | open it on your next turn, or reuse a pool session by message |
| `--check` reports `FAILED` | hook missing or changed | run `install.py <project dir> --update`, then `--check` again |

### 9.1 Recovering from a broken hook

If a change to a hook or to `.claude/settings.json` leaves Claude Code unable to work,
repair it from a terminal, outside Claude Code. **`git checkout <commit> -- <path>`
overwrites the working copy of that path, including changes you have not committed.**
Run `git status` first, and commit or copy any changes you want to keep.

Restore only the file that is broken, from a commit you know worked
(`git log --oneline -- <path>` lists candidates):

    git checkout <good commit> -- .claude/settings.json

The same form works for `.claude/tagteam/role_guard.py` or `.codex/hooks.json`. If you
cannot tell which file is broken, the wider form restores everything in the harness
paths at once, and also reverts your own edits to `AGENTS.md` and the agent
definitions:

    git checkout HEAD~1 -- .claude .codex AGENTS.md

`HEAD~1` assumes the previous commit was a working state. Then start a new session.
These commands need the paths to be tracked in git. The hook script itself fails open:
an internal error prints a warning and allows the action, so a bug in `role_guard.py`
should not block work.

## 10. Model-compensation rules

Some rules exist only to correct one model's repeated mistake. Each ends with
`[fix: <reason> / remove when <condition>]`. List them with:

    grep -rn "\[fix:" AGENTS.md .claude .codex

After a model change, check each against its removal condition and delete the rule when
the new model no longer needs it. Rules without the tag are harness structure and stay.

## 11. Developing the harness

In this repository:

    python -m unittest discover -s harness/tagteam/tests
    python -m unittest discover -s tests

The first covers the router, launcher, hook and collector (with fake CLIs); the second
covers the installer. Both are expected to pass before a change is merged.

There is no uninstall command. Remove the installed files by hand, run
`--unschedule` first if you registered the weekly check.
