---
paths:
  - "docs/missions/**"
---

# Design review before implementation

Every design unit that implementation will build on (the confirmed spec, or a
phase's design section in plan.md) gets one external plan review from a vendor
other than its author before it is handed to implementation (user decision
2026-10-20: design errors cost a whole phase, so design is cross-reviewed like
implementation). The only exception is a small, reversible, Tier 0 design
(verification-tiering.md); skipping it requires the line "design review skipped:
<reason>" in the design section or evaluation record, and a skip without a reason
leaves the phase incomplete. An agent review cannot replace user spec confirmation
(mission-artifacts.md). Within confirmed scope, reviewed designs proceed without
reapproval.

Check paths, dependencies, ownership and verification commands directly; use
a scout when its saved work exceeds the coordination cost (delegation matrix).

Resolve harness-route.py --host <current-host>
--role plan_review --author-vendor <actual-designer> --author-tier <designer band>
(the reviewer takes the same band on another vendor); pass --tier only to
override that. Repeat the author flag for coauthors. Record the verdict (or the
skip reason) at the head of the design section so the implementer can see it.
Supply intent, spec confirmation status, plan, key decisions and an exact
question; use verification-tiering.md's bounded evidence-packet contract.
If material is inaccessible, provide it or report the limitation; do not accept
a review of unseen material.

Review acceptance coverage, task contracts, dependencies, interfaces, verification
and costly-to-reverse choices. Do not invent product requirements or expand risk scope.

Return VERDICT: PASS | BLOCKED. BLOCKED names a concrete acceptance or
execution failure and a fix. At most three useful optional notes.
One revision/recheck counts within verification-tiering.md's two-round total.
If still blocked after the recheck, ask the user for the missing decision; do
not loop and do not hand a blocked design to implementation.
