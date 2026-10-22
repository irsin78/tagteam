# Peer sessions — operating notes (experimental)

Companion to `.claude/skills/peer-sessions/SKILL.md` (the procedure). This page is the
setup, the prompt skeletons, the manual recovery steps and the observations behind the
rules. Status: experimental; see the skill's header for what was and was not measured.

## Supported combination

- Two Claude desktop sessions on the same machine: the design session opened by the user
  in the main checkout, the implementation session started from it as a child in its own
  worktree (or opened by hand, trial shape), using the app's cross-session messages (`SendMessage` to the other session's title; the inbound
  message carries a `from` address to copy into `to`).
- One mission with a confirmed `spec.md` (`rules/mission-artifacts.md`), either in one
  checkout (trial shape) or with one worktree per implementation lane (default shape).
- Both sessions in the same permission mode, with the harness hooks active in that
  checkout (SessionStart prints `HARNESS MISSION SESSION`; without it the guard steps are
  logged as unavailable, never faked).
- Not supported here: Codex session pairs, an exec-driven relay, proxy commits, or peer
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
   `initiation: "user_asked"`, `use_worktree: true`, the fixed title, the implementation
   bootstrap prompt as the brief; an `offer` or `own_initiative` start shows a card and
   stops the flow) and confirms it in ListAgents. The implementation session sets its title, arms
   the guard in its worktree and ends with
   `needs-input "waiting on peer-design for [PEER DESIGN] phase 1"`; the design session
   then writes the phase 1 design and sends `[PEER DESIGN]`.
5. Lanes: one child worktree session per lane, started the same way; the design session
   stays in the main checkout, which is the integration branch. Opening sessions by hand
   (implementation first, then design) remains the trial shape.

## Bootstrap prompt skeletons

Keep them short; the sessions read the skill and the mission documents themselves.

Implementation session:

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

Design session:

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
Start: set your title to <design-title>, arm the guard, write plan.md §phase 1 (brief +
design), flip the baton, send [PEER DESIGN] phase 1 [id:…], log it, end with needs-input.
```

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
per file, append only; corrections are new records; pairing starts from sends; orphan
receipts are not flagged; duplicate ids keep the last; costs sum once per run id and keep
unmeasured runs apart; `--transport codex`, `--body`, `--ref` and `--trust-dir` serve an
external deliverer that is not part of this template.
