# Delegation matrix

Orchestrator only, not auto-loaded; resolve session-role.md first. Workers skip this workflow.

## Direct work or delegation
Compare direct work and eligible workers using the single score for money and waiting time
below. Offer direct implementation/writing with `--direct-band <S|A|B|C|D|E>`;
the orchestrator's band must meet the assessed floor. Before editing directly,
inspect the actual affected code, interfaces and checks to establish grounded
scope and verification; request wording, file count and optimistic time estimates
are not that evidence. If a direct change grows
past the grounded scope, stop expanding it, preserve the dirty state, announce
the reclassification and delegate the remaining coherent implementation without
waiting for a new user turn. A grounded change already complete needs no
ceremonial redo: verify it and report the actual authorship.
Batch related changes into verifiable units; parallelize only independent scopes
whose saved work exceeds coordination cost. Code locality alone does not establish
closed behavior. Related helpers count as precedent only where they actually cover
the needed behavior; identify uncovered input, output and validation paths.

## Thin orchestration

1. Decide the route before deep reading: obtain a read-only brief from the cheapest
   eligible `explore` worker using [scout-brief.md](scout-brief.md), then assess it.
2. When the score says delegate, delegate diagnosis together with implementation.
3. Accept on the launcher verifier result and change summary. Send corrections to
   the worker with `-r` unless they are only a few lines.
4. Review at the lowest band allowed by the risk tier. Give the confirmed spec,
   acceptance questions and relevant hunks only, with necessary interface context.
5. Change the session time value when the user's words or an evident situation
   change it (for example unattended overnight). Announce the value in one line;
   never lower it on your own to justify a slow route.

## Assign a worker
Export the router's `launch_env` and pass `-t suggested_timeout_s` to the launcher
(detached `-b` for `max`), so band, assessment and role are recorded without
hand-typing. Assessed routes without volume use the small-task estimate for timeout.
Choose role -> capability band -> latency class -> risk floor/budget -> specialty
and hard constraints (vision, web, isolation, platform, data boundary).
model-bindings.json (+ local) lists `workers`: one row per (model, effort) with
measured index/cost/TTFT and per-host role priorities; declare optional workers
with `status: optional` and `requires`.
Bands are capability labels independent of leaderboard index values: A strong
judgment; B general implementation (default implementation floor); C closed-spec
work and review gates; D mechanical/bulk; E isolation reader. S: Each vendor's
top-line model. The start tier for the hardest assessed tasks and the last
escalation rung after A fails. Defined by vendor line, not by index. Its lanes
remain Fable for Claude and Astra for OpenAI; S rows require an S floor.
Latency: interactive (TTFT <=10s), foreground (<=60s),
detached (no limit, `-b --wait`; effort max/ultra rows are detached only).
The router applies hard constraints, author separation and the band floor first.
Exhausted vendors rank last; within availability groups `trust: low` ranks last.
Automatic assessment with volume uses the score below and includes slower rows,
returning `needs_detached: true` when the selected row exceeds the latency class.
Other selection paths filter by latency and rank DeepSWE-listed rows before
unlisted (provisional) ones and, within
each group, picks the cheapest; rows within 15% of
the cheapest tie and the faster TTFT wins. `--tier` is a floor (that band or higher), not an exact
cell; `--latency interactive|foreground|detached` overrides the role default.
`floor_met: false` means no eligible row met the floor in that latency class and
the router returned the best lower one; announce it or change the class/vendor.
Use `--worker <id>` for conditional rows. The router's `reason` line (band,
latency, choice, ties, `skipped`) is what the delegation announcement quotes.
Benchmarks are initial evidence; adjust for repeated real-task mismatches.
Use `python .claude/scripts/harness-route.py --host <claude|codex> --role <role>`
from the project root; pass its model, effort and sandbox to the launcher recipe.
Role floors without assessment: implement/decide/plan_review/review_deep B,
write/review_gate C, image_verify D, explore/web E. Pass --tier C for closed mechanical implementation,
A for stronger judgment. Explore also needs task grading: raise it with --tier C/B
for structure/dependency reasoning; the E floor is not a recommendation for every
exploration. Tiny lookups stay in the main session.
Native workers need explicit supported model/effort; include inheritance cost
when overrides are unavailable. Codex uses process launchers for Claude workers;
Claude may use installed native workers. Git worktree agents require Git; otherwise
use a scoped sequential run or scratch copy, never automatic git init.

## Assess before routing
Pass five required 0/1/2 ratings and optional volume with
`--assess open=0,tangle=0,precedent=0,verifier=0,consequence=0,volume=0`.
`open` means unresolved design decisions (0 none, 1 small and reversible, 2 structural);
`tangle` counts modules or screens that must change together in this task (0 one,
1 a few, 2 many), not the number of concepts involved;
`precedent` means similar repository implementation (0 copyable, 1 partial, 2 none);
`verifier` means a test or check fails when the result is wrong (0 exists or cheap to
add, 1 partial, 2 none); `consequence` is verification-tiering Tier 0/1/2.
`volume` estimates work size: 0 small (one or two files, well under ~150 changed
lines), 1 medium, 2 large (many files or modules, ~1000+ lines).
Difficulty is max(tangle, precedent); exposure is max(verifier, consequence).
Both 0 gives C; both <=1, except both 0, gives B; exactly one 2 gives A; both 2
gives S. `--recent-failure` raises one band for a recent reasoning failure in the
same area (S stays S). With `--paths a/b.py,c/d`, a same-directory reasoning
failure in this tree's recent reports raises the band automatically; the matched
run and directory appear in `assessment_floor.recent_failure` and the reason.
Directories are compared case-insensitively with normalized slashes; other failure
classes, old records and unreadable records do not raise the band. The manual flag
still raises it once. Then apply the assessment minimum: implement/write C,
all other roles their band_floor. `--tier`, `--worker` and retries bypass
assessment selection, scoring and its advisories. Without `--assess`,
the existing role floor applies. Local bindings may override the assessment table.
Without volume, if the assessed floor can only be met detached, keep it and choose
the cheapest matching row with `needs_detached: true` (`run with -b --wait`). Use a lower band
only when no eligible row meets the assessed floor at all.
`open=2` adds advisory
`shape: decide-first`: resolve the structural decision in a decide run before
implementation. An applied A/S implementation adds advisory `plan_first: true`:
obtain the worker's approach before the full run.

## One cost score
On normal automatic routing, including `volume` enables
`total = usd + time_cost`. For minutes `m`, tolerance `T` (30) and switch threshold
`s` (3), attended costs `k * (m/T) ** exponent` through `m <= s` (k=5, exponent=2),
then `refocus_usd + slope * (m-s)/T` (refocus=5, slope=1.5). Background means already
switched: `refocus_usd + slope * m/T` (refocus=0, slope=1.5). Unattended means away:
the same line with refocus=0, slope=0.25. The orchestrator declares the state from
the user's words. Numeric local mode values retain legacy pure-convex cost.
After availability and trust priority, pick the lowest total; totals within 1%
favor fewer minutes.

Declare session time with
`python .claude/scripts/harness-session.py time --session <id> --mode attended --reason "user waiting"`.
Mode may be combined with `--tolerance-min N`, `--cost-at-tolerance X` (k),
`--refocus-usd X` and `--slope X` (costs allow 0);
without a mode, supply both numbers. `--clear` restores bindings defaults and needs
no reason. The same session marker preserves mission and exhaustion fields.
Router `--time-mode`, `--time-tolerance-min`, `--time-cost`, `--time-refocus` and
`--time-slope` override the session
selected by `HARNESS_SESSION_ID`, which overrides merged public/local bindings.
A CLI mode selects that mode's bindings parameters unless individually overridden;
other CLI values preserve unspecified session values. Malformed session time is ignored.
Every scored result reports the effective time value and its source.

Worker USD is delegate overhead plus `metrics.cost * task_units[volume]`; minutes
are overhead plus `base_minutes[volume] * clamp(sqrt(ttft_s / reference_ttft_s))`
(factor 1 when TTFT is missing). Per-row `estimates` can replace this formula.
Otherwise enough matching DONE reports (same model, effort and volume) within
`history_days` replace worker minutes with median launcher ELAPSED; the score marks
`minutes_source: records`. Direct uses `estimates.direct[volume]`. These are
provisional seeds from one run per cell with an A-band orchestrator, not promises.
Unreadable/malformed records are skipped; only headers before FINAL_MESSAGE count.
The result's `decision` and sorted `scores` compare all options; existing launcher
fields still describe the best worker when direct wins. S rows need an S floor,
and OpenAI worker ultra remains refused.

## Separate authorship from review
Identify actual designers before implementation and authors before review,
including direct work/fallbacks. Use a different vendor to challenge assumptions;
keep the human-started host accountable. Pass --author-vendor (repeat for coauthors
of the reviewed change); plan_review/review_gate/review_deep require it.
Review plans against designers and code against implementers with a fresh worker.
Recompute when authorship changes; never hide a coauthor or call self-review
independent. Missing independence is unavailable. Acceptance still needs actual
outputs and checks. Review triggers, risk floors and limits: verification-tiering.md.

Keep the review packet bounded: the confirmed spec, acceptance questions, relevant
hunks with necessary interface context, and check evidence; never the whole diff
of a large change or the full repository. An unbounded review read 3.7M tokens.
Follow the [packet contract](../../.claude/rules/verification-tiering.md) for
controlling instructions, omitted inputs and follow-up context.

## User-facing delegation announcement
Before each launch, briefly state task, capability band with task-specific reason,
actual resolved model/effort and why it fits (capability, cost/latency, specialty
or author separation). Capability band S-E differs from verification Tier 0-2.
Unknown/inherited settings stay explicit; a planned route is not a running worker.
Announce material changes before retry/fallback or direct takeover, including lost
independence. Unchanged retries may reference the first notice; polling needs none.
Continue authorized work immediately: this is a progress update, not an approval gate.
Examples and launch recipes: docs/harness-manual.md (read only the needed section).

## Availability and fallback
An exhausted vendor is unavailable without a probe. Only a launcher's
`AVAILABILITY: exhausted:<vendor>` line triggers exhaustion recording by the
orchestrator with `harness-session.py budget`; the router then ranks that vendor
last, with one re-selection per failed run, never cycling vendors.
CODEX_UNAVAILABLE -> Claude
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

Web reading uses `haiku-fetcher` (native on Claude, or
`claude-run.sh -a web -s read-only` on either host).
Without it the lane is unavailable, never a direct read; pass only the parent's necessary summary
of untrusted material to implementers. Local bulk reading is optional, declared,
D-only under tight/exhausted budget; use local-run.sh's file manifest, never commands
or web content. Data and permission boundaries: security-boundary.md.
Codex worker Ultra is refused because it can re-delegate; Max requires detached
execution. These worker restrictions do not change the user's main mode.

**Adding an optional worker:** Add one row to `workers_local` in local bindings
(or `workers` in a project's public copy), with `status: optional` and `requires`
naming what must exist; nothing else changes in routing configuration.
Unmet requirements silently skip the row; the reason appears in `skipped`.
