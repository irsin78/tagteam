# Scout brief

Use the cheapest eligible `explore` worker before deep orchestration reading.
The scout is read-only: inspect the smallest useful scope; never edit files,
run model CLIs, delegate, or choose an implementation route.

Prompt contract: return these fields, with repository evidence:
- Affected paths: project-relative files expected to change.
- Volume: 0 small, 1 medium, 2 large, with a one-line basis.
- Checks: existing tests/checks that would fail on a wrong result, or "none".
- Precedent: the closest implementation to copy, or "none".
- Open decisions: unresolved choices and the modules/screens that must change together.
- Risk: anything risky, including state, external effects or weak verification.

The orchestrator maps decisions to `open`, modules/screens that must change
together to `tangle`, precedent to `precedent`, checks to `verifier`, and risk to
verification Tier 0/1/2 for `consequence`. Volume 0 means one or two files well
under ~150 changed lines; 1 is medium; 2 means many files/modules, ~1000+ lines.
Pass all five ratings plus volume to `--assess`, in that order, and pass the
affected paths comma-separated to `--paths`. Declare the orchestrator's own band
with `--direct-band` when direct implementation/writing is an eligible option.
Keep the brief and confirmed acceptance questions in the worker's task packet;
delegate diagnosis and implementation together when the score says delegate.
