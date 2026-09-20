# Delegation matrix

Orchestrator-only reference outside automatic rules loading; resolve session-role.md first. Workers skip this workflow.

## Direct work or delegation
Consider preparation, startup, handoff and verification cost before delegating.
Handle targeted reads directly. Implement directly only when inspecting the
actual affected code and interfaces establishes a closed local change with known
verification; request wording, file count and optimistic time estimates are not
that evidence. Unknown behavior, cross-module contracts or newly needed design
take the cross-vendor route by default. A same-vendor native worker is not a
substitute merely because the implementation seems easy (explicit project
exceptions and diagnosed unavailability still apply). If a direct change grows
past the grounded scope, stop expanding it, preserve the dirty state, announce
the reclassification and delegate the remaining coherent implementation without
waiting for a new user turn. A grounded change already complete needs no
ceremonial redo: verify it and report the actual authorship.
Batch related changes into verifiable units; parallelize only independent scopes
whose saved work exceeds coordination cost. Do not add a scout/controller by ritual.

Code locality is not sufficient. Multiple new behaviors with interacting input,
output or validation paths take the cross-vendor route even when they fit one
module. Related helpers count as reuse only where they actually cover the needed
behavior; identify the uncovered part before choosing direct work. Routine
one-operation changes or repeated mechanical edits can still qualify after
inspection, while unresolved design or state behavior cannot.

## Assign a worker
Choose role -> capability tier -> risk floor/budget -> specialty and hard constraints
(vision, web, isolation, platform, data boundary). Use model-bindings.json (+ local)
for current models/efforts and routes. Benchmarks are initial evidence; adjust for
repeated real-task mismatches, not frequency or one anecdote. No synthetic score.
Use `python .claude/scripts/harness-route.py --host <claude|codex> --role <role>`
from the project root; pass its model, effort and sandbox to the launcher recipe.
Implementation defaults to B; --tier C is for closed mechanical work, B/A for
stronger judgment. Explore also needs task grading: choose --tier D for bounded
extraction, C/B for structure/dependency reasoning. The configured D default is
not a recommendation for every exploration. Tiny lookups stay in the main session.
Native workers need explicit supported model/effort; include inheritance cost
when overrides are unavailable. Codex uses process launchers for Claude workers;
Claude may use installed native workers. Git worktree agents require Git; otherwise
use a scoped sequential run or scratch copy, never automatic git init.

## Separate authorship from review
Identify actual designers before implementation and authors before review,
including direct work/fallbacks. Use a different vendor to challenge assumptions;
keep the human-started host accountable. Pass --author-vendor (repeat for coauthors
of the reviewed change); plan_review/review_gate/review_deep require it.
Review plans against designers and code against implementers with a fresh worker.
Recompute when authorship changes; never hide a coauthor or call self-review
independent. Missing independence is unavailable. Acceptance still needs actual
outputs and checks. Review triggers, risk floors and limits: verification-tiering.md.

## User-facing delegation announcement
Before each launch, briefly state task, capability tier with task-specific reason,
actual resolved model/effort and why it fits (capability, cost/latency, specialty
or author separation). Capability A-D differs from verification Tier 0-2.
Unknown/inherited settings stay explicit; a planned route is not a running worker.
Announce material changes before retry/fallback or direct takeover, including lost
independence. Unchanged retries may reference the first notice; polling needs none.
Continue authorized work immediately: this is a progress update, not an approval gate.
Examples and detailed launch recipes: docs/harness-manual.md (read only the needed section).

## Availability and fallback
An exhausted vendor is unavailable without a probe. CODEX_UNAVAILABLE -> Claude
native implementation; CLAUDE_UNAVAILABLE -> Codex native implementation; if native
workers are unavailable, the host handles it directly at the same risk floor.
AGY_UNAVAILABLE -> the host's implementation route, then its one native fallback.
Preserve scope, intended dirty inputs and verification; no CLI fallback loops.
If the main host is exhausted, report it; never switch the user's host/model.
Exit 4 is policy/configuration refusal, not availability. Exit 6 means wait on the
same run. Exit 5 is busy: wait on the reported run, or retry after its registration.
Exit 1 normally needs diagnosis under retry-policy.md, but confirmed
CLI authentication/quota failures may also use exit 1: treat those as availability
only after inspecting partial output/changes. Unknown errors remain failures.
Never classify arbitrary merged-log text as a CLI error or blindly retry revoked
credentials. Required independent advice/review cannot use a same-vendor fallback.

Web goes to agy first: the launcher validates and fetches the caller's URLs and
hands the text to `agy-summarizer`, an agent with no tools, when its installed
definition matches the checked-in source and it is discoverable. Web output must
carry EVIDENCE quotations the launcher can find in the text it fetched; an
unmatched quotation, `FETCH_INCOMPLETE`, or any other AGY_UNAVAILABLE result
falls back once to Claude's `haiku-fetcher`. Explore/web may otherwise fall back
to a constrained native reader (read-only/web-only).
No isolated web reader means unavailable; pass only the parent's necessary summary
of untrusted material to implementers. Local bulk reading is optional, declared,
D-only under tight/exhausted budget; use local-run.sh's file manifest, never commands
or web content. Data and permission boundaries: security-boundary.md.
Codex worker Ultra is refused because it can re-delegate; Max requires detached
execution. These worker restrictions do not change the user's main mode.
