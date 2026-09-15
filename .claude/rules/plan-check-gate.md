---
paths:
  - "docs/missions/**"
---

# Focused plan review

Use external plan review when a consequential design uncertainty remains
that implementation checks will not cheaply resolve, or the project/user
requires it. A mission folder or phase alone does not activate this gate.
An agent review cannot replace user spec confirmation (mission-artifacts.md).
Within confirmed scope, closed reversible plans proceed after grounding without
reapproval.

Check paths, dependencies, ownership and verification commands directly; use
the matrix's overhead criteria for broad grounding, never a ceremonial scout.

If review is useful, resolve harness-route.py --host <current-host>
--role plan_review --author-vendor <actual-designer>, with --tier B for a
bounded judgment or A when needed. Repeat the author flag for coauthors.
Supply intent, spec confirmation status, plan, key decisions and an exact
question; use verification-tiering.md's bounded evidence-packet contract.
If material is inaccessible, provide it or report the limitation; do not accept
a review of unseen material.

Review acceptance coverage, task contracts, dependencies, interfaces, verification
and costly-to-reverse choices. Do not invent product requirements or expand risk scope.

Return VERDICT: PASS | BLOCKED. BLOCKED names a concrete acceptance or
execution failure and a fix. At most three useful optional notes.
One revision/recheck counts within verification-tiering.md's two-round total.
If still blocked, fix the known cause or report the missing decision; do not loop.
