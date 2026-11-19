# Workers on each host

Part of the skill `cycle` (installed as `.claude/skills/cycle/hosts.md` and `.agents/skills/cycle/hosts.md`).
AGENTS.md and SKILL.md say what to do with a worker; this file says how on each host. The *host* is the
CLI the orchestrator runs in. Rules tagged `[host: ...]` record a measured limit of one host version;
re-test them after a host update. "Unconfirmed" marks behaviour nobody has measured yet: try it, and
record what happened in status.md.

## Launch kinds (`route.py --json`, field `launch`)

| `kind` | How to open the worker |
|---|---|
| `session` | `start_session` with the returned `model` and `effort`; the first prompt carries the line `HARNESS ROLE: DELEGATE`. `also` may offer a `subagent` alternative for a short mechanical task |
| `subagent` (`host: claude`) | the `Agent` tool with `subagent_type` = `agent`; the definition fixes model and effort |
| `subagent` (`host: codex`) | `spawn_agent` with `agent_type` = `agent`, a `task_name`, `fork_turns: "none"` and the brief as `message`. For `agent: "default"` also pass the returned `model` and `reasoning_effort`; for a custom agent pass neither (the hook denies them, see Codex notes) |
| `run` | the returned `command` with `<PROMPT_FILE>` replaced by the brief's file. `escalate: true` means: request escalated permissions for this command (Codex notes) |

The first line `run.py` prints is `RUN <id> started run_dir=...`; the run's logs are in that folder while it runs.

## Operations

| Operation | Claude Code desktop | Claude Code CLI | Codex CLI | agy |
|---|---|---|---|---|
| Open | `start_session` | `Agent` (definition), else `run.py` | `spawn_agent` (definition, or `default` with `model` and `reasoning_effort`); another vendor through `run.py` with escalation | `run.py` only, started with the orchestrator's `run_command` (each command may ask the user to approve; agy notes 2, 8) |
| Follow-up | `SendMessage` | resume the subagent | `followup_task` (starts a new turn of an idle worker); `send_message` only queues text and starts no turn | none: one-shot runs; relaunch with a fresh brief (routing policy) |
| Progress | sidebar | not visible until it returns | `list_agents` (`running`, or `completed` with the last answer); `wait_agent` (10 s to 1 h) | `RUN <id> started` line and its run folder; `run.py --status <id>` |
| Wait | the worker's report arrives as a message | the call returns | `wait_agent` before ending the turn (Codex notes 5) | the completion notice agy sends when its background command ends, or `run.py --wait <id>` (agy notes 3) |
| Stop | stop the session | - | `interrupt_agent` | `run.py` ends the worker when the `--timeout` given at launch has passed; to stop earlier, end the `run.py` process by hand |
| Later turns | automatic turns: try `start_session`; if refused, reuse the pool (Claude notes 1) | - | resumed turns can spawn and follow up (Codex notes 6); an interactive turn started by a finishing subagent: not observed (notes 5) | unconfirmed (agy notes 8) |
| Parallel writers | a worktree per session when needed | same | a subagent works in the orchestrator's workspace; give it a worktree inside the workspace in the brief, or use `run.py --cwd <worktree>` with escalation (unconfirmed) | `run.py --cwd <worktree>`; the launcher copies the `tagteam` hooks into it (agy notes 4) |
| Commit, branch, worktree | as usual | as usual | escalation request (Codex notes 9) | ordinary commands; each may ask the user to approve (agy notes 8) |
| Read-only review | `run.py --read-only` or a session | `run.py --read-only` | `run.py --read-only` with escalation (Codex notes 4) | `run.py --read-only` |

## Claude Code notes

1. `start_session` is refused in a turn that a worker's report started; `SendMessage` to an existing
   session works there. So open the pool on a user turn and reuse it later. [host: Claude Code desktop 2.1,
   measured 2026-11-17 / re-test after a host update]
2. A task sent by `SendMessage` does not pass the prompt hook, so the worker marker must be in the first
   prompt. [host: Claude Code desktop 2.1, measured 2026-11-16 / re-test after a host update]

## Codex notes

1. Start the Codex orchestrator in the main checkout. Its sandbox writes only inside the workspace, and the
   state folder `.tagteam/` is in the main checkout; a session started in a worktree needs `--add-dir <state
   folder>`. [host: codex-cli 0.169.0 and 0.170.0 Windows workspace-write sandbox, measured 2026-11-17 and
   2026-11-19 (0.170.0: worktree only) / re-test after a Codex update]
2. On Windows, `windows.sandbox = "elevated"` made every command of `codex exec` fail with
   `CreateProcessWithLogonW failed: 1385` in codex-cli 0.169.0; in 0.170.0 the same commands ran on one PC (two runs,
   `codex exec` only). Interactive Codex with `elevated`: unconfirmed. tagteam keeps `unelevated` as its default and
   `run.py` passes it itself. `install.py --check --host codex` warns about `elevated`; the setting is the user's. [host:
   codex-cli 0.169.0 and 0.170.0 Windows, codex exec, measured 2026-11-18 / re-test after a Codex update]
3. A custom agent (`.codex/agents/*.toml`) runs on its definition's model and effort; `model` and
   `reasoning_effort` passed to `spawn_agent` are silently ignored for it, and honoured for `default`.
   The real model is recorded per subagent in `<state folder>/agents.jsonl`; compare it with the route.
   A project Codex does not trust loads none of its `.codex/agents` definitions: the same `spawn_agent` then behaves as
   for `default` (arguments honoured, parent sandbox). [host: codex-cli 0.169.0 and 0.170.0, measured 2026-11-17 and
   2026-11-19 (0.170.0 Windows `codex exec`; the arguments test ran with hooks disabled, the real model also with hooks on) /
   re-test after a Codex update]
4. A subagent inherits the orchestrator's sandbox; `sandbox_mode` in an agent definition was not applied.
   A review that must not write goes through `run.py --read-only`. [host: codex-cli 0.169.0 and 0.170.0, measured
   2026-11-17 and 2026-11-19 / re-test after a Codex update]
5. When the orchestrator's turn ends, subagents still running are interrupted (measured in `codex exec`).
   Wait for them with `wait_agent` before ending the turn. In interactive Codex a subagent's completion arrived
   in the same turn while the orchestrator was in `wait_agent`; a new orchestrator turn started by a finishing
   subagent was not observed, and whether it happens is unconfirmed. [host: codex-cli 0.169.0 exec 2026-11-17 and
   interactive 2026-11-18; 0.170.0 exec only, measured 2026-11-19 / re-test after a Codex update]
6. In a turn resumed with `codex exec resume`, `spawn_agent` works and `followup_task` reaches a subagent of an
   earlier turn by its `task_name`, but `list_agents` does not show earlier subagents. Record each `task_name` in
   status.md. [host: codex-cli 0.169.0 and 0.170.0 exec resume, measured 2026-11-17 and 2026-11-19 / re-test after a
   Codex update]
7. Subagents get no SessionStart or UserPromptSubmit hook, and the task text of `spawn_agent` reaches hooks
   encrypted; the hook knows a subagent by its `agent_id`, so a subagent is always a worker. [host: codex-cli
   0.169.0, measured 2026-11-17; 0.170.0 measured 2026-11-19: no SessionStart context in the subagent, encrypted task
   text and `agent_id` seen, UserPromptSubmit not re-observed / re-test after a Codex update]
8. The Stop hook did not run reliably in `codex exec` with 0.169.0 (8 of 14 turns); in 0.170.0 the Stop hook that Codex
   printed ran in 11 of 11 turns and failed once in a twelfth, a different method (output lines of a user-level hook, not
   a probe record). Nothing here depends on it. [host: codex-cli 0.169.0 exec, measured 2026-11-17; codex-cli 0.170.0
   exec, measured 2026-11-19 / re-test after a Codex update]
9. Inside the sandbox, `git add`, `git commit`, `git branch`, `git worktree add` and `run.py` failed (`.git` is
   protected; the nested Claude and Codex CLIs did not start), and so did temporary-folder access for `install.py
   --check` and the tests (`WinError 5`). Run them with an escalation request and a one-line justification. In
   interactive Codex each request went ahead after the user's approval in the prompt: six in one small mission (host
   check, review launch, test run, three commits), none refused. With `codex exec --approve-for-me` an automatic
   reviewer approved them; that is not the user's approval. If the commit of a verified unit is refused, report
   the unit as verified but not committed. If a worker or review launch through `run.py` is refused, the unit
   is incomplete: every change needs its cross-vendor review. If the user's Codex rules (`rules/*.rules`) allow `git add`
   or `git commit`, those commands run without a prompt and outside the sandbox; the protection that is left is the
   tagteam hooks and `run.py`'s `git_head_moved` check (`install.py --check --host codex` warns when a rule allows
   `git commit` or `git push`). [host: codex-cli 0.169.0 Windows interactive, measured 2026-11-18 (exec 2026-11-17);
   the rules case codex-cli 0.170.0 Windows, measured 2026-11-18 / re-test after a Codex update]
10. Codex trusts each hook entry by its definition; a changed or new entry is skipped silently until it is
    trusted again in interactive Codex. After `install.py --update` or `--repair-hooks`, open Codex in the
    project and trust the hooks. Changing a hook script needs no new trust. Trusting the project (`codex exec
    --trust`, or the question in interactive Codex) and trusting the hooks (`/hooks`) are two separate steps.
    In a project Codex does not trust, the project's `.codex/hooks.json` is skipped with the start line `warning:
    Untrusted project-local configuration is disabled.`, and no role record is written;
    `--dangerously-bypass-hook-trust` skips only the hook trust and does not stand in for the project trust. `run.py`
    reports this as `hooks_not_run` (exit 11; in a linked worktree too, note 12) and `install.py --check --host codex` warns when the project is not in
    `config.toml`'s trusted projects. Whether the two flags exist in 0.169.0 is unknown. [host: codex-cli 0.169.0,
    measured 2026-11-17; project trust and the two flags codex-cli 0.170.0 Windows, measured 2026-11-18 / re-test after
    a Codex update]
11. On macOS, `/usr/bin/git` inside the Seatbelt sandbox prints `xcrun: couldn't create cache file ...` on stderr but
    exits 0 and its result is right: do not read it as a failure. Whether a Homebrew git avoids it is unconfirmed. [host:
    codex-cli 0.170.0 macOS `codex exec`, measured 2026-11-19 / re-test after a Codex update]
12. In a linked git worktree the project's Codex hooks ran although the worktree has no `.codex/hooks.json` (git ignores
    it). Observed: a `run.py` openai worker in a worktree under the main checkout and in one outside it wrote its role
    record in the main checkout's `.tagteam/sessions/`, and a `spawn_agent` call was denied by the PreToolUse hook. Only
    the trust key of the main checkout's hooks file is in `config.toml`, so Codex seems to load the main checkout's
    project configuration there (inferred; the mechanism is unconfirmed). Seen in a project Codex trusts; an untrusted
    project's worktree was not measured (it should leave no record). `run.py` therefore judges `hooks_not_run` (exit 11)
    in a worktree as in the main checkout. [host: codex-cli 0.170.0 Windows, trusted project, linked worktrees under and
    outside the main tree, measured 2026-11-19 / re-test after a Codex update]

## agy notes

agy is an orchestrator in its interactive mode only, with the built-in Orchestrator model. Every worker of the
harness is a `run.py` run (for vendor google, `agy -p`): one-shot, no follow-up. That is the harness's routing
policy, not a measured agy limit. agy's own subagents are not harness roles (note 5).

1. A headless `agy -p` parent waits at most three minutes for a background command, so it cannot collect a worker
   that runs for minutes; agy as the orchestrator is therefore interactive only. [host: agy 1.5.10 headless,
   Windows and Linux, measured 2026-11-18 / re-test after an agy update]
2. The built-in Orchestrator model plans (the agy TUI labels the planner "Neon" or "Argon"; the label is a UI text, not a
   measurement) and hands the plan to an execution model inside the same conversation. `agy models` (1.5.12) lists two
   ids, `gemini-pro-orchestrator-neon` "Orchestrator (Neon)" and `gemini-pro-orchestrator-g1-pro` "Orchestrator
   (Argon)"; the interactive `/model` list offered only Neon, and a session on it really ran on `neon`
   (`agy-models.jsonl`). Pass the id of your own conversation: the most recent `agy-models.jsonl` line with your `cid`
   (the `ANTIGRAVITY_CONVERSATION_ID` of your shell) whose `model` is one of the `orchestrators` ids of `models.json`; the
   log alternates with the execution model, so the plain last line can name that one. [host: agy 1.5.12 Linux, measured
   2026-11-19 / re-test after an agy update]
   For the harness that whole conversation is one orchestrator, so the execution side does the
   orchestrator's work: routing, `run.py` launches, comparing a report with the diff, commits. In the one run
   observed the execution side was `gemini-flash-worker-agy`, a Flash-class model; whether a long or complex
   mission does the same is unconfirmed. In an interactive mission on 1.5.12 the plan phase ran on `neon` and the
   execution phase on `gemini-flash-worker-agy`, alternating every turn. The model takes no effort: `/effort` reports it as not adjustable for the
   older id (`--effort` is refused) and for `neon` (`agy -p "/effort" --model gemini-pro-orchestrator-neon`; a `--effort`
   rejection was not probed for `neon`), so pass no `--orchestrator-effort`. [host: agy 1.5.10 Windows, one
   simple instruction, measured 2026-11-18; agy 1.5.12 Linux, one mission, measured 2026-11-19 / re-test after an agy
   update] The harness therefore adds:
   - Rule: the plan launches every implementation unit through `run.py`; the execution side edits only mission
     records (status.md) and makes no small fix itself.
   - The review brief asks the reviewer to compare the worker's `CHANGED` with `git diff`, and the router raises
     every review on `--host agy` to at least grade A.
   - The role hook appends `{ts, cid, model}` to `<state folder>/agy-models.jsonl` whenever a conversation's model
     changes, so the user and the next orchestrator can see which model did what.
   - If this is not enough, the user can pick one model (for example the Argon model) instead of the Orchestrator
     model; it is an alternative, not the default, and has its own weekly limit. The orchestrator then passes the real
     model and effort to the router (`--orchestrator-model gemini-4-argon --orchestrator-effort <effort>`) instead of
     the Orchestrator id.
3. `run_command` hands a command to the background after about ten seconds, and `WaitMsBeforeAsync` does not lengthen
   that. When the command ends, agy delivers its output as a completion notice. For a `run.py` worker that takes
   longer, wait for the notice, or call `run.py --wait <id>` (it returns within nine seconds; exit 9 means the run is
   still going) and repeat; `run.py --status <id>` prints the stored report at any time, also in a later session.
   [host: agy 1.5.10 headless, Windows and Linux, measured 2026-11-18 / re-test after an agy update]
4. Hooks live in `.agents/hooks.json`, in the `tagteam` key. The file is per machine because the guard path in it is
   absolute; `run.py` copies the key into a git worktree before it runs a google worker there. agy runs a hook
   command with `cmd /c` on Windows and `sh -c` on Linux, in the folder `.agents`. A PreToolUse hook that fails
   (exit code 1 or 2, invalid JSON, timeout) or prints nothing blocks the tool; a failing SessionStart or PreInvocation
   hook does not stop the session. So the guard answers `ask` (the user's permission rules decide) on an internal
   error, the hook is matched only to `run_command` and the delegation tools, and a guard that cannot start (no
   Python, a wrong guard path) blocks exactly those tools. Recovery: manual section 1.3. [host: agy 1.5.10 Windows and
   Linux, measured 2026-11-18 / re-test after an agy update]
5. agy's own subagents (`invoke_subagent`) take a model tier and no effort, so they cannot meet AGENTS.md section 1
   and are not used for harness roles. A subagent that the orchestrator starts opens a new conversation whose first
   model call has no user input; the guard records that as a worker, so it can neither commit nor delegate. The
   orchestrator's own `invoke_subagent` gets `ask`. A subagent inherits `HARNESS_DELEGATE_RUN=1` from its parent's
   environment. The other delegation tool names (`define_subagent`, `run_workflow`, `swarm`) come from agy's change
   log and were not seen. In an interactive session the subagent was recorded as a worker as described, and its
   `run_command` waited for an approval for 60 seconds and then timed out, which ended the subagent with `REFUSED`:
   a user who is busy elsewhere makes the subagent fail. [host: agy 1.5.10-1.5.11 headless, measured 2026-11-18;
   interactive agy 1.5.12 Linux, measured 2026-11-19 / re-test after an agy update]
6. A headless agy in `accept-edits` mode denies a shell command that no allow rule covers, whatever a hook answers, so
   `run.py` gives a google worker `--settings` with its own allow list (it replaces the user's list for that run). In
   the default mode a command runs without a rule but cannot write, and `--mode plan` runs no commands; Windows needs
   an exact allow rule (windows.md). [host: agy 1.5.10, measured 2026-11-18 / re-test after an agy update]
7. Git Bash rewrites an argument that starts with `/` (for example `agy -p /permissions`); prefix the command with
   `MSYS_NO_PATHCONV=1` (windows.md).
8. Interactive agy, observed once on Linux (agy 1.5.12, one small mission; Windows interactive is not measured):
   - the first model call of the orchestrator's conversation carried the user's input, so the guard recorded the
     orchestrator as an orchestrator (role `orchestrator`, no `basis`);
   - a long `run.py` worker went to the background after about ten seconds, and `run.py --wait <id>` and the completion
     notice both came back in the same turn without user input;
   - the planner and the execution model alternate each turn (`agy-models.jsonl`);
   - `ask` opened approval prompts, several per mission, and "always allow" did not stop them (the user's
     recollection; the count was not recorded). An agy orchestrator therefore needs the user present for the whole
     mission to approve commands (an inference from that).
   [host: agy 1.5.12 Linux interactive, measured 2026-11-19 / re-test after an agy update]
   Still unconfirmed; record what happens in status.md the first time it matters:
   - whether "always allow" also covers the same command with other arguments;
   - whether an interactive agy in an untrusted folder loads `.agents/hooks.json` (headless runs do) and whether it asks
     for trust first;
   - whether `ask` on the orchestrator's `invoke_subagent` opens a prompt;
   - whether the edit tools (`write_file`, `edit_file`) ask for approval when only `write_file(*)` is allowed: the
     baseline per tool is open (the hook matcher does not include them).
