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
   folder>`. [host: codex-cli 0.169.0 Windows workspace-write sandbox, measured 2026-11-17 / re-test after a
   Codex update]
2. On Windows, `windows.sandbox = "elevated"` made every command of `codex exec` fail with
   `CreateProcessWithLogonW failed: 1385`; `unelevated` worked, and `run.py` passes it itself. Interactive Codex with
   `elevated`: unconfirmed. `install.py --check --host codex` warns about it; the setting is the user's. [host: codex-cli 0.169.0 Windows,
   measured 2026-11-17 / re-test after a Codex update]
3. A custom agent (`.codex/agents/*.toml`) runs on its definition's model and effort; `model` and
   `reasoning_effort` passed to `spawn_agent` are silently ignored for it, and honoured for `default`.
   The real model is recorded per subagent in `<state folder>/agents.jsonl`; compare it with the route.
   [host: codex-cli 0.169.0, measured 2026-11-17 / re-test after a Codex update]
4. A subagent inherits the orchestrator's sandbox; `sandbox_mode` in an agent definition was not applied.
   A review that must not write goes through `run.py --read-only`. [host: codex-cli 0.169.0, measured 2026-11-17 /
   re-test after a Codex update]
5. When the orchestrator's turn ends, subagents still running are interrupted (measured in `codex exec`).
   Wait for them with `wait_agent` before ending the turn. In interactive Codex a subagent's completion arrived
   in the same turn while the orchestrator was in `wait_agent`; a new orchestrator turn started by a finishing
   subagent was not observed, and whether it happens is unconfirmed. [host: codex-cli 0.169.0 exec 2026-11-17 and
   interactive 2026-11-18 / re-test after a Codex update]
6. In a turn resumed with `codex exec resume`, `spawn_agent` works and `followup_task` reaches a subagent of an
   earlier turn by its `task_name`, but `list_agents` does not show earlier subagents. Record each `task_name` in
   status.md. [host: codex-cli 0.169.0 exec resume, measured 2026-11-17 / re-test after a Codex update]
7. Subagents get no SessionStart or UserPromptSubmit hook, and the task text of `spawn_agent` reaches hooks
   encrypted; the hook knows a subagent by its `agent_id`, so a subagent is always a worker. [host: codex-cli
   0.169.0, measured 2026-11-17 / re-test after a Codex update]
8. The Stop hook did not run reliably in `codex exec` (8 of 14 turns); nothing here depends on it.
   [host: codex-cli 0.169.0 exec, measured 2026-11-17 / re-test after a Codex update]
9. Inside the sandbox, `git add`, `git commit`, `git branch`, `git worktree add` and `run.py` failed (`.git` is
   protected; the nested Claude and Codex CLIs did not start), and so did temporary-folder access for `install.py
   --check` and the tests (`WinError 5`). Run them with an escalation request and a one-line justification. In
   interactive Codex each request went ahead after the user's approval in the prompt: six in one small mission (host
   check, review launch, test run, three commits), none refused. With `codex exec --approve-for-me` an automatic
   reviewer approved them; that is not the user's approval. If the commit of a verified unit is refused, report
   the unit as verified but not committed. If a worker or review launch through `run.py` is refused, the unit
   is incomplete: every change needs its cross-vendor review. [host: codex-cli 0.169.0 Windows interactive, measured
   2026-11-18 (exec 2026-11-17) / re-test after a Codex update]
10. Codex trusts each hook entry by its definition; a changed or new entry is skipped silently until it is
    trusted again in interactive Codex. After `install.py --update` or `--repair-hooks`, open Codex in the
    project and trust the hooks. Changing a hook script needs no new trust. [host: codex-cli 0.169.0, measured
    2026-11-17 / re-test after a Codex update]

## agy notes

agy is an orchestrator in its interactive mode only, with the built-in Orchestrator model. Every worker of the
harness is a `run.py` run (for vendor google, `agy -p`): one-shot, no follow-up. That is the harness's routing
policy, not a measured agy limit. agy's own subagents are not harness roles (note 5).

1. A headless `agy -p` parent waits at most three minutes for a background command, so it cannot collect a worker
   that runs for minutes; agy as the orchestrator is therefore interactive only. [host: agy 1.5.10 headless,
   Windows and Linux, measured 2026-11-18 / re-test after an agy update]
2. The Orchestrator model plans on an Argon-class model and hands the plan to an execution model inside the same
   conversation. For the harness that whole conversation is one orchestrator, so the execution side does the
   orchestrator's work: routing, `run.py` launches, comparing a report with the diff, commits. In the one run
   observed the execution side was `gemini-flash-worker-agy`, a Flash-class model; whether a long or complex
   mission does the same is unconfirmed. The model takes no effort (`--effort` is refused as not adjustable), so
   pass no `--orchestrator-effort`. [host: agy 1.5.10, one simple instruction, measured 2026-11-18 / re-test after
   an agy update] The harness therefore adds:
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
   log and were not seen. [host: agy 1.5.10-1.5.11 headless, measured 2026-11-18 / re-test after an agy update]
6. A headless agy in `accept-edits` mode denies a shell command that no allow rule covers, whatever a hook answers, so
   `run.py` gives a google worker `--settings` with its own allow list (it replaces the user's list for that run). In
   the default mode a command runs without a rule but cannot write, and `--mode plan` runs no commands; Windows needs
   an exact allow rule (windows.md). [host: agy 1.5.10, measured 2026-11-18 / re-test after an agy update]
7. Git Bash rewrites an argument that starts with `/` (for example `agy -p /permissions`); prefix the command with
   `MSYS_NO_PATHCONV=1` (windows.md).
8. Unconfirmed in interactive agy; record what happens in status.md the first time it matters:
   - whether the first model call of a conversation carries the user's input (if not, the guard records the
     orchestrator as a worker and refuses its commits; manual section 1.3 has the recovery);
   - whether `ask` opens an approval prompt for each command, and whether "always allow" also covers the next
     command with other arguments (tell the user at the mission start roughly how many approvals to expect);
   - whether the completion notice of a background command arrives in the same turn, opens a new turn, or comes
     after the turn has ended (until known, use `run.py --wait <id>` or `--status <id>` and do not end the turn
     without a result);
   - whether an interactive agy in an untrusted folder loads `.agents/hooks.json` (headless runs do);
   - whether `ask` on the orchestrator's `invoke_subagent` opens a prompt, and which model takes the later turns of a
     long mission (`agy-models.jsonl` shows it).
