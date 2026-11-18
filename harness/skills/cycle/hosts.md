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
| Open | `start_session` | `Agent` (definition), else `run.py` | `spawn_agent` (definition, or `default` with `model` and `reasoning_effort`); another vendor through `run.py` with escalation | `run.py` only (routing policy, agy notes) |
| Follow-up | `SendMessage` | resume the subagent | `followup_task` (starts a new turn of an idle worker); `send_message` only queues text and starts no turn | none: one-shot runs; relaunch with a fresh brief (routing policy) |
| Progress | sidebar | not visible until it returns | `list_agents` (`running`, or `completed` with the last answer); `wait_agent` (10 s to 1 h) | `RUN <id> started` line and its run folder |
| Wait | the worker's report arrives as a message | the call returns | `wait_agent` before ending the turn (Codex notes 5) | `run.py` returns |
| Stop | stop the session | - | `interrupt_agent` | `run.py --timeout` |
| Later turns | automatic turns: try `start_session`; if refused, reuse the pool (Claude notes 1) | - | resumed turns can spawn and follow up (Codex notes 6); an interactive turn started by a finishing subagent: not observed (notes 5) | - |
| Parallel writers | a worktree per session when needed | same | a subagent works in the orchestrator's workspace; give it a worktree inside the workspace in the brief, or use `run.py --cwd <worktree>` with escalation (unconfirmed) | `run.py --cwd` |
| Commit, branch, worktree | as usual | as usual | escalation request (Codex notes 9) | - |
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

The harness currently routes agy only as a one-shot `run.py` worker (no follow-up, no subagents). That is
the harness's routing policy, not a measured agy limit. Unconfirmed until the agy adapter is done: agy's own
subagents, resuming an agy worker, and agy as the orchestrator. agy hooks are not installed yet.
