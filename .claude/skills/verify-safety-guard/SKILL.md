---
name: verify-safety-guard
description: Focused verification of changed safety-guard behavior using realistic inputs and a disposable mutation check. Use for a guard whose protection changes, not every validation edit, documentation change or ordinary review.
---

# Verify changed guard behavior

Use rules/verification-tiering.md to decide whether the change warrants
this procedure. Test the affected protection, not an expanding catalog of
hypothetical threats. Project instructions define domain-specific risks.

## Focused mutation check
1. Run the affected test on the actual candidate and confirm it passes.
2. Make a disposable copy of THAT candidate, including intended uncommitted
   changes. A worktree at HEAD alone does not contain a pending guard fix.
3. Remove or invert the affected guard in the copy, leaving the test intact.
4. Run the same test: it must fail for the intended protection. If it still
   passes, fix the test or investigate whether another guard masks this path.
5. Restore/discard only the disposable copy and confirm the primary tree
   remains intact. Report the mutation, baseline pass and expected failure.

Use realistic command/API/data shapes from existing fixtures or captured
sanitized evidence. A new live account call is needed only if an unresolved
external-type uncertainty affects the test and the call is authorized.
Do not require credentials or a live service for every guard edit.

## Review when required
For Tier 2, use the independent deep-review route at B or above. Add the
separate grading gate only when spec validity needs independent assessment
or the user/project requires that protocol. Resolve by current host AND
--author-vendor for each target (designer for spec, implementer for code).
Use a fresh worker; required pairs must also differ from each other. Direct
work or fallback changes authorship, so never hardcode the reviewer by host.

Give focused inline hunks and context or an exact readable file/range. Check
that the reviewer actually saw the target; recover from truncation with
smaller context or bounded reads, rather than more speculative review rounds.
No universal token/line ceiling is assumed across tools. A required pair
uses different vendors and neither reviewer sees the other's verdict.

The review-round limit includes confirmation. Use parallel review only when
independence and coordination allow it. Do not broaden hardening in a closing
check. Historical examples and prior measurements are in the manual.
