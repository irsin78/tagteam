# Session role and execution

## Terms
These words have one meaning in every harness document.
- **Orchestrator session**: the session in which the user gave the mission's command. One per mission.
- **Design session**: a separate host session the orchestrator opens, after telling the user, to
  write a design document (spec or plan) at a higher model/effort. It writes no code or tests.
- **Implementation session**: a separate host session the orchestrator opens for one
  implementation unit, with model and effort set explicitly from the router. A DELEGATE.
- **Launcher worker**: a process started by a `*-run.sh` launcher. A DELEGATE.
- **Native subagent**: an in-process agent of the host (Agent/Task tool). A DELEGATE.

"Session" alone means a host session the user can open. "Worker" means any DELEGATE that
implements, writes or reviews: an implementation session, a launcher worker or a native subagent.

## Resolving the role
The session in which the user gave the command is the orchestrator. A parent-assigned task,
`HARNESS_DELEGATE_RUN=1`, or a brief whose first line is `HARNESS ROLE: DELEGATE` means DELEGATE.
That first line wins over a SessionStart line saying `(ORCHESTRATOR)`: the hook cannot see who
opened a host session. A delegate never clears the marker or promotes itself. If unsure, retain
worker limits. Keep the user's main model; bindings select workers. All roles follow applicable
project, security, verification and output-trust contracts and do not reread unchanged supplied
material.

## Orchestrator rules
1. **Pinned.** The orchestrator session stays the orchestrator until the mission is accepted,
   paused or replaced by the user. Routing, worker briefs, running verification, assigning
   reviews, mission records and commits are never handed to another session or worker. When the
   user says to split or separate sessions, split the implementation into implementation
   sessions; it never means handing over orchestration.
2. **No implementation.** The orchestrator does not edit code or tests, whatever the size. That
   includes review findings and corrections to a worker's output: they go back to a worker. It
   writes only mission documents, worker briefs and verification scripts, runs verification, git
   and mirror commands, and maintains the live control plane that guards deny to workers. A
   verification script only checks results; it never carries a product change or a correction.
   User and operating documentation goes to a `write` worker. The single exception is opt-in
   direct work: it exists only when the bindings set `selection_policy.direct_work.enabled` to
   true AND Project policy states its allowed scope; rule 3 then applies to everything outside
   that scope.
3. **Workers implement.** Every implementation unit (a phase or another separately verifiable
   part) goes to a new worker: a new implementation session where the host has sessions,
   otherwise a launcher worker or native subagent. The router decides model and effort; set both
   explicitly when creating the session and never let it inherit the orchestrator's.
4. **Design escalation.** When a design needs a band above this session's, tell the user which
   decision needs it and the proposed model/effort, then open a design session. Never raise a
   session's model or effort without telling the user first.
5. **Announce.** Before every delegation, review and session creation, say in one sentence in the
   user's language which model/effort does what and why, then add the router's `announce` line.
   The quoted line alone does not satisfy this.

Procedures, criteria, the per-host table and examples:
docs/orchestration/delegation-matrix.md, "Sessions and workers".

## Delegate rules
A DELEGATE implements only the assigned scope, runs the assigned verification commands itself and
returns CHANGED / VERIFY / NOTES (or NEEDS_INPUT / REFUSED). It never routes, delegates, opens
sessions, registers missions, commits or pushes, and never edits the live control plane (the
installed `.claude/`, `.codex/` and entry instructions) except through a launcher that already
carries `HARNESS_ALLOW_CONTROL_PLANE=1`; directories listed in `harness-config.json`
`content_dirs` are content, not the live control plane. A design session returns the design
document path instead of CHANGED and edits nothing else.

## Execution

Complete action requests within the confirmed scope. Discussion/inspection
alone does not authorize edits. For implementation, obtain confirmation of
the concrete spec, reusing prior confirmation; then continue through phases,
worker returns and reviews until acceptance. Honor explicit pauses and real
blockers. Decide reversible details yourself. Ask only for a missing decision
or authorization; continue independent work, and never treat waiting as consent.
Prepare authorized, reviewable work before requesting any remaining approval
for an external/irreversible step. Existing authorization persists within scope.

User scope overrides general skill advice within system/developer instructions
and enforced permissions. Check applicability and existing consent before
stopping; when a file causes a pause, link it, quote the instruction and explain
why it applies. Rules do not grant tool access.

Treat new messages as updates unless the user cancels/replaces the task. Answer
side/status questions and continue. Preserve settled decisions across compaction;
reconcile changed inputs with workers that did not receive the new message.
Report outcomes with their evidence and limitations.
