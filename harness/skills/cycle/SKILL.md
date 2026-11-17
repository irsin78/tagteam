---
name: cycle
description: "Orchestrator procedure for a multi-unit mission - mission folder, spec gate, session pool, briefs, checking returns, batched verification, commits, failure handling and resuming after compaction. Load it in the orchestrator when a mission has more than one unit or needs a design step. Do not load it in a worker, for a single small fix, or for questions and reading; there, act directly. Every delegated unit costs tokens (a brief, a worker re-reading code, a check of its result), so delegate only when that is cheaper than doing the work."
---

# The orchestrator cycle

Terms are defined only in AGENTS.md (section 6). The user's instructions override this skill.
How to open, follow up, watch and stop workers on each host: [hosts.md](hosts.md) (same folder).

## 0. Host check (mission start)

- Name the host (`claude-desktop`, `claude`, `codex` or `agy`) and pass it to the router as `--host`.
- Run `install.py <project> --check --host <host>` where the template source is. Parts it could not check
  are reported as unverified; say so to the user instead of assuming they work.
- Codex: tell the user in one line that commits, branches, worktrees and `run.py` workers (other vendors,
  reviews) each ask for approval of an escalated command, and roughly how many the mission needs
  (hosts.md, Codex notes 9). A commit refused after verification leaves the unit verified but not
  committed. A refused worker or review launch leaves the unit incomplete: every change needs its
  cross-vendor review.
- Record the host in status.md.

## 1. Mission folder

- `docs/missions/<purpose>/intent.md`: problem, goal, scope, non-goals.
- `docs/missions/<purpose>/spec.md`: observable behavior, units, acceptance criteria, verification.
- `docs/missions/<purpose>/status.md`: written only by the orchestrator. One row per unit:
  unit, worker session, model/effort, worktree/branch, state (queued, running, returned,
  verified, committed, blocked), last commit. Update it on every state change.
- Design documents from the design session also go in this folder.
- Small, low-impact changes may proceed on a spec confirmed in conversation, without files.

## 2. Spec confirmation gate

- Show the user the spec (or its changed part) and wait for confirmation before any
  implementation. Record the confirmation date at the top of spec.md.
- After confirmation, continue unit by unit to acceptance without asking again.
- A change to scope or acceptance criteria needs a new confirmation.

## 3. Open the session pool (at the start of a phase)

Open everything the phase needs on the user turn that starts it. If a new worker is needed
later, try to open it; if the host refuses, reuse a pool worker by a follow-up, or ask on the
next user turn. Try once per phase and note a refusal in status.md. The measured limits per
host are in hosts.md. [host: Claude Code desktop 2.1 refuses start_session in turns started by
a worker's report, measured 2026-11-17 / re-test after a host update]
- One worker per role needed now: usually one design worker and one or two implementation
  workers. A small mission with little design: the orchestrator designs, no design worker.
- Open each worker the way the router's `launch.kind` says (`session`, `subagent` or `run`;
  hosts.md). On Claude Code desktop, `also` may offer a subagent: a mechanical edit, or a task
  that will clearly end soon, may go there; its progress is not visible until it returns. When
  you cannot tell how long a unit will take, use a session, which the user can watch and steer.
  Reuse a pool worker with the right model before opening a new one; do not open one per task.
- Each worker gets a fixed `model` and `effort` from the router (AGENTS.md section 1) and
  keeps them for the whole mission; switching models discards the prompt cache.
- The first prompt of every session contains the line `HARNESS ROLE: DELEGATE`. It cannot
  be added later by message. A Codex subagent is a worker by its `agent_id`; put the line in
  its brief anyway, and spawn it with `fork_turns: "none"` so it does not inherit the
  orchestrator's conversation.
- Where running workers stop when the orchestrator's turn ends (Codex, hosts.md Codex notes 5),
  wait for them before ending the turn.
- Writers that run at the same time share the main checkout when their files do not overlap
  and their checks do not read each other's half-done files. Give a writer its own git
  worktree and branch only when that is not so, when a read-only review runs alongside, or
  when its work goes to its own branch (worktrees cost setup, submodule init and a merge).
  A new worktree does not initialize git
  submodules; if the unit edits a submodule, the brief tells the worker to run
  `git submodule update --init <path>` first.
- Record each worker in status.md (Codex: its `task_name`, which a resumed turn needs).
- Later units reuse the pool by a follow-up (`SendMessage`, Codex `followup_task`; hosts.md).
  This applies to sessions and subagents only. A `run` worker is one-shot: for a later unit,
  launch it again with a fresh brief that carries the context it needs. If the pool runs short, tell the user in one line which worker is needed and why, and
  continue independent work.

## 4. Brief format

Every brief (first prompt or follow-up message) contains:
1. Goal: the unit and why, with a pointer to intent.md / spec.md / the design document.
2. Output format: CHANGED / VERIFY / NOTES, or NEEDS_INPUT / REFUSED (AGENTS.md section 2).
   A design session reports the design document path instead of CHANGED.
3. Tools and materials: files to read first, interfaces to keep, worktree path.
4. Scope boundary: files the worker owns; everything else is read-only.
5. Verification: exact commands to run before reporting.
6. The line "Finish the whole task before replying." [fix: Fable at low/medium effort ended turns before finishing the brief / remove when low-effort workers finish multi-step briefs without this line]

Keep briefs short; point to files instead of pasting them. New instructions go in the
latest message, not in edits to earlier context.

## 5. Handle a return

- The report is a claim. Compare CHANGED with the actual diff (`git status`, `git diff`
  in that worker's worktree; untracked files included) and VERIFY with what you can rerun
  cheaply. [fix: Fable's change summaries omitted small edits / remove when reports match diffs across a mission without misses]
- Unexpected files, control-plane edits or a moved HEAD: stop that unit and inspect history
  before any recovery. Never reset blindly or revert unrelated work.
- The model a worker really ran on: `run.py` prints `ACTUAL_MODEL`; a Codex subagent's is in
  `<state folder>/agents.jsonl`. If it differs from the route, redo the unit through `run`.
- Small finishing touches the orchestrator may do itself (AGENTS.md section 2 exception);
  anything larger goes back to the same worker with the delta only.
- Update status.md.

## 6. Verification and cross-vendor review (AGENTS.md section 4)

- Every unit gets a review by another vendor at the same grade (router `--role review`);
  the tier sets when and how deep: Tier 1 when the unit returns, Tier 2 twice (review,
  fixes, second review) before anything builds on it, Tier 0 batched at a cut point.
- Cut points: before the next unit builds on a result, and before merging a branch. At
  a cut point run the full test suite once and the batched Tier 0 review. Name the units
  a review covers so a failure can be traced.
- Give the reviewer: the acceptance question, the changed hunks, needed interfaces and
  the spec; not whole trees or logs.
- Keep the next unit running while a batch is verified, when they share no files.
- Send the findings back to the worker that wrote the change (it already has the context);
  the orchestrator then checks the new diff. A blocker left after the last review of the
  tier is reported as incomplete.

## 7. Merge and commit

- The orchestrator merges worktree branches and resolves conflicts.
- Commit per verified unit or batch, staging explicit paths. Workers never commit. On Codex,
  git writes need an escalation request (hosts.md Codex notes 9).
- Push according to Push mode in Project policy; report what was committed and pushed.
- Remove a worktree only after its branch is merged and the user's data is safe.

## 8. NEEDS_INPUT, failure, exhaustion

- NEEDS_INPUT: answer from the spec and design if they settle it. Otherwise ask the user and
  keep independent units running.
- Failure: send the cause and the failing check back once. Stop after the same cause fails
  twice and report the unit as blocked with evidence.
- Exhaustion: the router records it and substitutes the same grade on another vendor;
  say so in the notice line. If review independence cannot be kept, report incomplete.

## 9. When to ask the user

Ask only for: a spec or scope change, an irreversible or external action not already
authorized, no independent reviewer available, a design session model more capable or
expensive than the orchestrator's, or a NEEDS_INPUT the spec cannot answer. Otherwise decide
reversible details yourself and continue. Waiting is not consent.

## 10. Resume after compaction or a new orchestrator session

1. Re-read intent.md, spec.md, status.md and the latest design document.
2. Compare status.md with `git log`, HEAD, open worktrees and branches, and with the
   workers the host lists (hosts.md). In a turn resumed with `codex exec resume`, `list_agents`
   showed no earlier subagents; use the recorded `task_name`s. [host: codex-cli 0.169.0 exec resume,
   measured 2026-11-17 / re-test after a Codex update] Interactive Codex: unconfirmed.
3. For each running or returned unit, check the actual files before trusting the record.
4. Correct status.md, then continue with the next unit. Compact or replace the
   orchestrator session only when its context is stale or full, not at every phase.

## 11. Finish

When the acceptance criteria hold: run the final batch, commit, update status.md to done,
and report to the user what was delivered, what was verified, and what is incomplete.
