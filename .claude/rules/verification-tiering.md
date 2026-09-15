# Verification proportional to consequences

Judge failure consequences and reversibility, not file counts or domain keywords.
Project policy supplies acceptance tests and domain risk. Verify actual outputs.
- Tier 0: obvious reversible change without meaningful contract impact. One cheap
  relevant check, or verification: n/a with a reason.
- Tier 1: ordinary behavior change. Affected checks and diff inspection; one
  independent review if uncertainty remains or the project requires it.
- Tier 2: credible irreversible loss, unauthorized external action, weakened
  protection or other project-defined severe effect. Relevant regressions and
  independent deep review at B or above. For changed guards, check rejection and
  confirm removing that protection fails the focused test. Guard docs alone are
  not a guard-behavior change.

Review the confirmed intent/spec and actual change, not only the implementation
plan or author's verdict. The orchestrator uses
docs/orchestration/delegation-matrix.md for authorship and vendor separation;
workers do not load that routing workflow.
A gate + deep-review pair is optional unless the project/user requires it; when
required, the reviewers must also differ from each other. Never silently weaken it.

Supply the acceptance question, changed hunks, necessary surrounding interfaces,
controlling instructions and concise check evidence. For routing/activation,
include relevant host instructions, rules and bindings. Name omitted inputs;
absence from a packet is not proof of absence from the project. Use bounded reads,
not full trees/logs/mission histories. Follow-ups carry the delta and valid context.
Choose capability/effort for the unresolved question and required risk floor;
a bounded review need not be A. Do not raise effort to compensate for an unfocused
packet or silently lower a required B+ review. Announce the resolved assignment.

Before accepting or building on changed behavior, run required checks and compare
worker reports with actual files (delegate-output-trust.md). Reuse evidence while
relevant inputs are unchanged. Git events alone do not require full suites or LLM
reviews. Broaden for affected dependencies, failures or a concrete open concern.
Avoid tests that only restate low-impact edits; test meaningful failure modes.

Default one review-and-fix round; a second only for an unresolved blocker, including
confirmation of the fix. After two, stop model review calls, not approved execution:
fix known causes, continue unaffected work and report unmet requirements as incomplete.
Closing confirmation adds no speculative hardening. Environment failures do not
by themselves raise the task's risk tier.

A worker blocked by the execution environment returns the implemented changes,
the failed command and the exact limitation to the parent. Do not expand into
permission repair or repeated cleanup. The parent may run the same required
check in its already authorized environment; that evidence must identify who
ran it. A blocked worker check is not a pass, and does not require reimplementation.

Use .claude/.stop-gate for unattended/project-required completion verification.
If present, run `python .claude/scripts/harness-session.py finish` before completion;
a failed or remaining marker is incomplete. Otherwise actual task checks suffice.
Attended multi-step missions use the separate session continuation guard in
mission-artifacts.md. It checks declared execution state, not verifier success.
