# Peer sessions — operating notes (experimental)

Companion to `.claude/skills/peer-sessions/SKILL.md` (the procedure). This page is the
setup, the prompt skeletons, the manual recovery steps and the observations behind the
rules. Status: experimental; see the skill's header for what was and was not measured.

## Supported combination

- Two Claude desktop sessions on the same machine: the design session opened by the user
  in the main checkout, the implementation session started from it as a child in its own
  worktree (or opened by hand, trial shape), using the app's cross-session messages (`SendMessage` to the other session's title; the inbound
  message carries a `from` address to copy into `to`).
- A bounded native Codex parent/child workflow when the app exposes `create_thread`,
  `create_worktree`, child-scoped `list_worktrees` and native messages. Follow the separate
  native bootstrap below; Claude's `start_session` recipe does not describe these APIs.
- One mission with a confirmed `spec.md` (`rules/mission-artifacts.md`), either in one
  checkout (trial shape) or with one worktree per implementation lane (default shape).
- Both sessions in the same permission mode, with the harness hooks active in that
  checkout (SessionStart prints `HARNESS MISSION SESSION`; without it the guard steps are
  logged as unavailable, never faked).
- Not supported here: an exec-driven relay, undocumented live-app RPC, proxy commits, or peer
  sessions created by another session acting as orchestrators (they are delegates).

## Setup

1. Confirm the task spec and commit it: `mission --state active` needs
   `<mission>/spec.md` on disk, and the implementation session treats it as confirmed
   input.
2. Put the first line of `state.md` in place:
   `Baton: design · phase 0 · since <ISO> · based_on <HEAD 7>`, then
   `Unattended-default: off`, then the "sends" table header and an empty observations
   section.
3. Open the **design** session in the main checkout and paste its bootstrap prompt.
4. The design session starts the **implementation** session (`start_session` with
   `initiation: "user_asked"`, `use_worktree: true`, `model` and `effort` from the
   implement route (band B: Opus 5.5 medium; omitted, the child inherits the parent's
   model), the fixed title, the implementation bootstrap prompt as the brief; an `offer`
   or `own_initiative` start shows a card and stops the flow) and confirms it in
   ListAgents. The implementation session sets its title, arms
   the guard in its worktree and ends with
   `needs-input "waiting on peer-design for [PEER DESIGN] phase 1"`; the design session
   then writes the phase 1 design and sends `[PEER DESIGN]`.
5. Lanes: one child worktree session per lane, started the same way; the design session
   stays in the main checkout, which is the integration branch. Opening sessions by hand
   (implementation first, then design) remains the trial shape.

## Bootstrap prompt skeletons

Keep them short; the sessions read the skill and the mission documents themselves.

Claude implementation session:

```text
You are the implementation session <impl-title> of a peer-session loop (DELEGATE). Read
.claude/skills/peer-sessions/SKILL.md sections 1–6 and docs/missions/<m>/{intent,spec}.md.
Rules: write only the deliverables and the result rows named by the design; never commit,
push, delegate to workers, edit .claude/** or ask the user to confirm the spec. Reply to
[PEER DESIGN] with [PEER DONE] (first line ends with files: <comma list> [id:<msg_id>]) or
[PEER ASK]; log every receipt and send with tools/peer-event.py; re-arm the mission guard
on every receipt; end waits with needs-input (ASCII reason).
Start: set your title to <impl-title>, arm the guard
(python .claude/scripts/harness-session.py mission --session <id> --state active --mission
docs/missions/<m>) then needs-input "waiting on <design-title> for [PEER DESIGN] phase 1",
report title/branch/permission mode/id in the chat and end the turn. Write nothing before
the design arrives.
```

Claude design session:

```text
You are the design and judgment session <design-title> of a peer-session loop and the
responsible orchestrator. Read .claude/skills/peer-sessions/SKILL.md and
docs/missions/<m>/{intent,spec,state}.md. Confirm <impl-title> is in ListAgents.
Rules: you own intent/spec/plan design sections/state first line and the acceptance checks;
you never write deliverables or result rows. Inside the confirmed spec send designs without
asking the user; ask the user only for spec changes and stop reports (unattended default
stays off unless I switch it on here). Commit only the files declared in [PEER DONE], by
explicit path, after comparing the report with the diff; preserve and compare anything
outside the declaration. Log every send/receipt/guard/eval/verdict/cost with
tools/peer-event.py and paste its report into state.md.
Start: set your title to <design-title>, arm the guard. Start the implementation session
yourself: run `python .claude/scripts/harness-route.py --host claude --role implement
--assess <…,volume=2>`, then start_session with initiation "user_asked", use_worktree true,
the route's model and effort (never omit them: the child would inherit your model), the
title <impl-title> and the implementation prompt as the brief; quote the announce line.
Confirm it in ListAgents, write plan.md §phase 1 (brief + design), flip the baton, send
[PEER DESIGN] phase 1 [id:…], log it, end with needs-input.
```

## Native Codex bootstrap

This is a bounded native workflow, not an alternative launcher/transport backend. The
human-started root remains the responsible orchestrator. Every parent-created child is a
delegate, including a design coordinator: it cannot spawn workers, commit, push, repair
hooks/trust or grant itself control-plane access. Root owns preparation and acceptance.

1. Root requests the child model explicitly through the available `create_thread` wrapper
   and records the actual task/parent IDs. The observed native wrapper has no effort
   override: record actual model/effort evidence or `unknown`, never infer `high` from the
   model name or reuse evidence from a separate AppServer probe. No live-app RPC wiring,
   effort mutation or new relay is part of this procedure. Record thread metadata cwd
   separately; creating the thread does not select a command cwd or attach a worktree.
2. The child queries its own task-scoped `list_worktrees`. Under root's explicit authority,
   it reuses the authorized attachment or calls `create_worktree` once with the full pinned
   ref supplied by root, waits for operation completion, then records the returned
   `workspaceRoot` and attachment identity. Use only arguments exposed by the actual
   wrapper. On timeout or registration error report the exact outcome and wait; do not
   automatically create a second worktree. Root's empty listing says nothing about a
   child's attachments.
3. Every command passes the returned root explicitly; every file read/write uses a path
   rooted there. A `cd` in one shell does not persist into another tool call. For example,
   these are separate calls, each with its own `workdir`:

   ```text
   exec_command({cmd: "git rev-parse --show-toplevel", workdir: workspaceRoot})
   exec_command({cmd: "git rev-parse HEAD", workdir: workspaceRoot})
   exec_command({cmd: "git rev-parse --path-format=absolute --git-common-dir", workdir: workspaceRoot})
   exec_command({cmd: "git status --short", workdir: workspaceRoot})
   ```

   Verify that toplevel is exactly the returned worktree, HEAD equals the pinned full SHA
   and common directory matches root's expected repository. Caller cwd and Git override
   environment are not identity evidence. The child reports READY with its task/parent IDs,
   metadata cwd, workspaceRoot, own attachment evidence, expected/actual HEAD, common
   directory, status and actual model/effort evidence. READY means attachment verified;
   the child now waits for root preparation and DESIGN without product writes.
4. Root independently validates READY against the repository/pin it already knows, then
   alone invokes the installed preparation interface. If that interface is unavailable,
   setup remains incomplete; the child never repairs it. Pass all identity arguments and
   a real Bash path; do not derive the expected common directory or SHA from the child.

   ```text
   exec_command({
     cmd: 'python "<parentHarnessRoot>/.claude/scripts/harness-session.py" prepare-codex-child --workspace-root "<workspaceRoot>" --expected-common-dir "<expectedGitCommonDir>" --expected-head <fullPinnedSHA> --bash "<absoluteRealBash>" --json',
     workdir: workspaceRoot
   })
   ```

   The command rejects `HARNESS_DELEGATE_RUN=1` even with a control-plane allowance;
   native children without that marker remain bound by the parent-only role instruction.
   Preparation also rejects main/subfolder
   targets, identity mismatches and redirected `.codex` outputs before writing. It may
   generate a missing child-local definition through the pinned, unmodified tracked
   generator and workspace-snapshot dependency, never force
   or overwrite an existing file. Always check effective engine trust afterward. Root
   sends GO/DESIGN only for `status: trusted` with a positive handler count and accepted
   effective sources. `generated` alone is partial setup, not permission to implement;
   zero/missing/disabled/untrusted hooks or query failures keep the gate closed. Trust
   changes remain the user's step. Child-local definitions may exist while Codex loads
   main's source; preserve and report that distinction.
5. After DESIGN, the child uses the same thread/worktree for implementation and every
   ASK/DECIDE continuation. Re-arm a mission guard only with a real emitted mission-session
   ID; absent markers are `unavailable`, never invented. The bootstrap grants no hook
   self-repair, permission repair, delegation or control-plane access. The final READY/DONE
   evidence includes child-scoped `list_worktrees` again, actual HEAD/status and explicit
   command workdirs. Root owns review, integration and commits.

Native implementation prompt skeleton:

```text
You are the implementation DELEGATE for <parent-task-id>, child <task-id>. Use only the
root-authorized attachment at full pinned ref <SHA>. Query your own list_worktrees;
create_worktree once if authorized and needed, wait for completion and record workspaceRoot.
For EVERY command specify workdir: workspaceRoot; for EVERY file operation use a path under
that explicit root. Thread metadata cwd is separate. Verify actual toplevel/HEAD/common-dir
and status; send READY with path, task/parent IDs, pin, attachment and model/effort evidence.
Then wait for root preparation and DESIGN. No product writes before DESIGN, no hook/trust
self-repair, grants, workers, commits or pushes. Missing mission ID/effort is unavailable or
unknown, never fabricated. On DESIGN implement only its owned files and test them; ASK when
blocked and stop product writes while retaining the impl baton. Resume in THIS same thread
and worktree after DECIDE. New messages have fresh IDs; receipts copy outbound ID/kind and
reply_to. Reply with DONE plus exact files/check evidence, or ASK; verify own attachments
again at completion. Root remains responsible for preparation, review and commits.
```

## Message identity and reply logging

Every new outbound message, including ACK, ASK and DECIDE, has a fresh nonempty ID.
Generate the ID before submission, include it in the native message, submit, then log the
actual successful send/result immediately. A failed submission is a note with its exact
outcome, not a successful send. A successful submission whose log was missed stays
disclosed; never backfill an invented timestamp. Transport submission is not receipt.

For example, `[PEER ASK] phase 1 direction? [id:ask-UUID] [reply-to:design-UUID]` gets a
fresh reply `[PEER DECIDE] phase 1 ascending [id:decide-UUID] [reply-to:ask-UUID]`.
Both sender and receiver log that DECIDE's ID, kind and reference exactly:

```text
exec_command({cmd: 'python tools/peer-event.py log --mission docs/missions/<m> --session root --kind send --transport codex --msg DECIDE --phase 1 --msg-id decide-UUID --reply-to ask-UUID --note "native submission succeeded"', workdir: workspaceRoot})
exec_command({cmd: 'python tools/peer-event.py log --mission docs/missions/<m> --session impl --kind recv --transport codex --msg DECIDE --phase 1 --msg-id decide-UUID --reply-to ask-UUID', workdir: workspaceRoot})
```

Each session uses its own explicit workspaceRoot and event file. A reply must not reuse
the inbound ID, and a receipt must not turn DESIGN into ACK under DESIGN's ID. `--reply-to`
is the additive message reference; `--ref` remains USER-ANSWER human provenance. Neither
field grants authority. Intentionally ignored duplicate deliveries are logged as notes (not counted as duplicate_recv anomalies); accidentally repeated recv observations are reported as duplicate_recv and never processed twice.
ASK retains the impl baton and stops product writes; DECIDE resumes the same session.

## What the responsible orchestrator does with a `[PEER DONE]`

1. Log `recv`, re-arm the guard, re-read `plan.md` and `state.md`, run `git status`.
2. Compare the declaration (`files:`) with the diff. Declared and changed → review, run the
   acceptance checks, commit by explicit path together with the peer's event file.
   Changed but not declared → leave it, record it in the evaluation, do not commit.
   Declared but unchanged → ask in the next message. No declaration → the phase is
   incomplete; ask for it.
3. Evaluate (skill section 8), write the verdict line, then either design phase n+1 (citing
   every open id) or post the `[LOOP STOP]` report.

## Manual recovery

- Peer gone (closed, archived): write the reason after the baton line, ask the user to
  reopen it with the bootstrap prompt; the new session re-arms from `state.md` and the event
  log, and re-processing a message is safe because receipts are idempotent by `msg_id`.
- Messages stopped arriving: check the event log first (the peer's last `recv`/`send`), then
  ListAgents. A delayed wake-up is not a stall; a `recv` with no later `send` for an
  unreasonable time is. Tell the user; do not resend.
- Guard says "armed mission required" on `needs-input`: the receipt released the mark; run
  `--state active` first.
- Baton disagreement: the later `since` yields; stash or copy the losing uncommitted
  writes, compare them, log the incident.
- Plan-usage limit: `state.md` gets "how far, resume when", the turn ends with
  `--state paused`, and the session resumes on its reminder or when the user returns.
- Lost reminder (session restarted): the resume time is in `state.md`; the user or the
  session re-arms from there.

## Observations behind the rules (development workspace, 2026-10)

Facts, not benchmarks; the trial series was small and had no serial control group.

- `SendMessage` to a session title delivers; the reply address is the inbound `from`.
  The sender's turn continues after sending.
- An inbound message and an idle notice both pass through UserPromptSubmit and release the
  mission guard, hence the unconditional re-arm. Idle notices arrived later than the
  protocol reply and bundled, hence "not a receipt".
- A receiving session wakes on its next refresh, not instantly.
- A third-party commit during the loop (the user's other session) made the baton line's
  `based_on` disagree with HEAD, hence "nobody else commits".
- Hand-copied observation tables had mixed time anchors and one wrong line; the event log
  with `correction` records replaced them.
- Non-ASCII `needs-input` reasons were garbled on a Windows host through Bash.
- Every trial pair was created by a parent session, which is why the responsible
  orchestrator is defined by origin, not by role.

## Limits of the event tool

`tools/peer-event.py` (standard library; tests in `tools/test_peer_event.py`): one writer
per file, append only; corrections are new records and never count as new messages.
Unambiguous send/receive pairs retain their timing fields, including missing/reversed
receipt flags. Reports also expose missing IDs/sends/reply targets, duplicate sends/receipts,
metadata mismatches and invalid reply references in deterministic JSON/Markdown; duplicate
observations are never reduced to the last one. Legacy logs without `reply_to` remain
readable. Missing sends mean missing log observations, not proven transport loss; a clean
report does not prove delivery. Default reporting stays informational exit0 for nonempty
readable logs even with anomalies. Logging never scans other session files or couples
writers to enforce global uniqueness. Costs still sum once per run ID and keep unmeasured
runs apart; unmatched corrections change nothing. `--transport codex` records separately
submitted native messages or an external deliverer; the tool supplies neither transport.
`--body` stores message text; `--ref` and `--trust-dir` retain USER-ANSWER provenance rules.
