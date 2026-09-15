---
paths:
  - "docs/missions/**"
---

# Missions: confirmed spec through completion

Orchestrator-only workflow; delegates execute their assigned part of an
existing mission without creating approval gates or new missions.
For substantial or multi-session work, use one persistent
`docs/missions/<purpose>/` folder per independent outcome.
Track it when the project uses Git; non-Git projects keep the same documents
under their own persistence/backup policy. Do not initialize Git as a prerequisite.
Keep related fixes, phases and worker handoffs in that mission. A new authorized
purpose with its own acceptance criteria gets a new folder.

A small, grounded, low-consequence standalone change may use an explicitly
confirmed conversational spec without mission files. Ground it in the actual
affected code/interfaces and known verification, not request length. Existing
mission work stays in its mission. Recording prior confirmation adds no approval.

When mission files are needed, start with `intent.md` (problem, desired outcome, scope/exclusions, constraints)
and `spec.md` (observable behavior, important choices, acceptance and verification).
With intent alone, investigate and draft the spec; do not implement yet.
Present the concrete spec for user confirmation before implementation. Record
what was confirmed and the conversation/date or review reference in `spec.md`.
If the user already confirmed that same spec in conversation, preserve that
decision without asking again. Waiting, an agent review or a status label is
not user consent. Do not infer spec approval from a vague implementation request.

After confirmation, follow session-role.md's continuous execution and decision
contract. Ask when scope, observable behavior, constraints or acceptance must
change; carry confirmed changes into the affected spec/plan and workers. Never
weaken acceptance to fit the implementation. Announcing the next unit is not
performing it: do it before ending the turn, or explain the actual blocker.

For a confirmed multi-step mission, arm the session's continuation guard with
`python .claude/scripts/harness-session.py mission --session <id> --state active --mission <folder>`.
Use the id from HARNESS MISSION SESSION (UserPromptSubmit), not a worker or another
session. New user input releases the old guard; re-arm when continuing the mission,
including after a status answer. On acceptance use `--state complete`; for an explicit
pause, real blocker/decision/authorization wait or replaced request use `--state paused`,
`needs-input` or `switched` with `--reason` and explain it to the user. Keep the actual
remaining items/evidence in the mission, not a second checklist. No session-id output
means automatic recovery is unavailable; do not invent an id or claim enforcement.
This guard gives one recovery, not completion proof; details: docs/missions/README.md.

Add plan/state, designs, roadmaps or phase detail only when useful, without new
approval or session boundaries. Record work order, ownership, current/next/blockers
and evidence (also based_on HEAD and dirty files in Git projects). Keep one current
record per fact; link rather than duplicate. Bookkeeping alone needs no worker.

On handoff/resume, reload host instructions, the active intent, confirmed spec
and needed plan/state. Compare with actual files and checks (also HEAD/dirty changes
in Git); history is not filesystem evidence. Preserve the mission and consent,
refresh stale facts and give workers relevant intent/spec and evidence. Read only
the active mission and referenced dependencies. Conditional plan review follows
plan-check-gate.md. Compact or start a fresh session only for stale/noisy/near-full
context or at the user's request, never merely for a phase boundary. Avoid
unnecessary model/tool/config changes; batching follows the delegation matrix.

Complete with acceptance evidence under verification-tiering.md. Record outcome,
deviations and material limitations in state.md or a short spec completion note;
update affected user/operating docs. Commit/push need their existing authorization.
Keep finished missions as history; legacy `.planning/` is not active state.
Examples, optional artifacts and migration: docs/missions/README.md.
