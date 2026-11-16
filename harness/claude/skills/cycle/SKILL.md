---
name: cycle
description: "Orchestrator procedure for a multi-unit mission - mission folder, spec gate, session pool, briefs, checking returns, batched verification, commits, failure handling and resuming after compaction. Load it in the orchestrator when a mission has more than one unit or needs a design step. Do not load it in a worker, for a single small fix, or for questions and reading; there, act directly. Every delegated unit costs tokens (a brief, a worker re-reading code, a check of its result), so delegate only when that is cheaper than doing the work."
---

# The orchestrator cycle

Terms are defined only in AGENTS.md (section 6). The user's instructions override this skill.

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

## 3. Open the session pool (on a user turn)

Sessions can be opened only on a turn the user started; turns started by a worker's report
cannot open them. So open everything the phase needs while the user is present.
- One session per role needed now: usually one design session and one or two implementation
  sessions. A small mission with little design: the orchestrator designs, no design session.
- Each session gets a fixed `model` and `effort` from the router (AGENTS.md section 1) and
  keeps them for the whole mission; switching models discards the prompt cache.
- The first prompt of every session contains the line `HARNESS ROLE: DELEGATE`. It cannot
  be added later by message.
- Writers that run at the same time each get their own git worktree and branch. A single
  writer at a time may share the main checkout.
- Record each session in status.md.
- Later units reuse the pool by message (SendMessage). If the pool runs short, tell the
  user in one line which session is needed and why, and continue independent work.

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
- Small finishing touches the orchestrator may do itself (AGENTS.md section 2 exception);
  anything larger goes back to the same worker with the delta only.
- Update status.md.

## 6. Batched verification and cross-vendor review

- Choose cut points: before the next unit builds on this result, right after a risky
  (Tier 2) change, and before merging a branch. Not after every unit.
- At a cut point run the expensive checks once over the batch: full test suite, then a
  review by another vendor at the same grade (router `--role review`). Name the units
  in the batch so a failure can be traced.
- Give the reviewer: the acceptance question, the changed hunks, needed interfaces and
  the spec; not whole trees or logs.
- Keep the next unit running while a batch is verified, when they share no files.
- Review-and-fix: one round, a second only for a remaining blocker, then report incomplete.

## 7. Merge and commit

- The orchestrator merges worktree branches and resolves conflicts.
- Commit per verified unit or batch, staging explicit paths. Workers never commit.
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
2. Compare status.md with `git log`, HEAD, open worktrees and branches.
3. For each running or returned unit, check the actual files before trusting the record.
4. Correct status.md, then continue with the next unit. Compact or replace the
   orchestrator session only when its context is stale or full, not at every phase.

## 11. Finish

When the acceptance criteria hold: run the final batch, commit, update status.md to done,
and report to the user what was delivered, what was verified, and what is incomplete.
