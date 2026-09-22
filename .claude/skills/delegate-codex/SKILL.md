---
name: delegate-codex
description: Codex delegation recipe for this harness — load BEFORE composing any codex-run.sh launcher call (prompt/verify file conventions, flags, report-line interpretation, exit codes, existing-work preservation).
---

# Codex delegation recipe (launcher path)

Routing (WHO handles a task) lives in `docs/orchestration/delegation-matrix.md`. This
skill is the HOW: compose and interpret a `codex-run.sh` delegation.

Before using this recipe, decide that delegation is worth its overhead
(docs/orchestration/delegation-matrix.md). Resolve the host/role and optional --tier in
harness-route.py; use its model/effort, not an assumed maximum model.

## Invocation

1. Write the fully self-contained task prompt to a scratch file: goal,
   exact file paths, constraints (always including "git commit/push are
   FORBIDDEN"), conventions to match, and the verification recipe. Never
   inline task text into shell arguments (security-boundary.md — the
   launcher delivers the prompt via stdin redirect).
2. If verification is wanted, write the verify commands to a SCRIPT FILE.
   `-v` takes a PATH (repo-relative or Windows-style absolute; MSYS
   `/tmp/...` paths are invisible to the launcher's existence check) —
   never a command string (HARNESS_DENIED otherwise). The launcher
   snapshots the script BEFORE codex runs and executes the snapshot, so a
   delegate cannot edit its own grading gate mid-run.
3. Call the launcher DIRECTLY with Bash, `timeout: 600000`:

       bash .claude/scripts/codex-run.sh -p <prompt-file> [-m MODEL] [-e EFFORT] [-s SANDBOX] [-v <verify-script>] [-i <image>] [-o <schema.json>] [-t <seconds>]

   Defaults: `workspace-write` / `-t 570`; `-m`/`-e` default from
   `.claude/model-bindings.json` (+ `.claude/model-bindings.local.json`,
   local wins key-by-key): without `-i` → `roles.implement.ladder[0]`
   (Sol/high), with `-i` → `roles.image_verify.default` (Terra/medium);
   the report's `BINDINGS:` line names the source (`public`,
   `public+local`, `builtin` when no file/python, `explicit` for a given
   flag). Worker efforts: low|medium|high|xhigh|max; `max` requires `-b`.
   `ultra` is HARNESS_DENIED even with `-b` or `-r`: automatic subagents
   conflict with the worker's no-redelegation contract. A bindings-sourced
   `ultra` is refused too, not replaced with builtin settings.
   API support, Codex app modes and installed-CLI validation are separate;
   see the manual's "Applying the GPT-6 Astra official guide" section.
   Model support alone is not runtime evidence.
   The launcher is the ONLY unprompted codex path: `codex exec` is not on
   `permissions.allow`, so a direct call goes through the
   permission prompt / auto-mode classifier — use the launcher for every
   lane, including advisory (`-s read-only -m <bindings.A.openai.model>
   -e high` — the second-opinion binding; pass `-m`/`-e` explicitly,
   because without them the launcher defaults to the implementation entry,
   not the selected advisory binding) and
   image input (`-i <file>`, codex's fallback route for visual checks).
   `-o` passes a JSON Schema to `--output-schema`; the shipped
   `.claude/scripts/codex-report.schema.json` forces the final message
   into `{status, changed[], verify, notes}` so the report is parseable
   instead of free text. `-l` must be a repo-relative directory.
   Long runs (expected past ~9 minutes) or anything you want to survive
   a lost Bash call: add `-b`. The launcher returns at once with
   `RUN_ID:` and `WAIT:` lines and keeps running detached (nohup); then
   poll with `bash .claude/scripts/codex-run.sh --wait <RUN_ID> -t 570`
   (exit 6 = still running, call again; otherwise the saved report is
   printed and the run's own exit code returned) or peek with `--status
   <RUN_ID>`. Every run — foreground too — records
   `~/.claude/harness-runs/state-<RUN_ID>.json` (starting → running →
   done | aborted, launcher/child PIDs) and `report-<RUN_ID>.txt` — OUTSIDE
   the workspace, so a codex delegate cannot forge a "done" record for a
   launcher that dies mid-run. A launcher killed mid-run is classified
   `aborted` instead of leaving you guessing. A new launcher refuses to
   start while ANOTHER run — codex or agy — in the same tree is still
   `running` (`HARNESS_BUSY: STALE_RUN <id>`, exit 5 — `--wait` for it);
   only a `read-only` sandbox on either side is exempt (an advisory run
   writes nothing). Dead leftovers are marked aborted and listed as
   `STALE_RUN_CLEANED`. A live PID is never demoted on age (long
   runs keep the guard up); identity is the process start time recorded
   with the PID, so a recycled PID reads as dead. If the guard still
   stays busy wrongly, `HARNESS_ALLOW_FORGET=1 bash
   .claude/scripts/codex-run.sh --forget <RUN_ID>` marks the record
   aborted (orchestrator-only — the hook denies the marker from
   subagents; it never touches a process). Records are per tree key; the
   WSL lane and the Windows side see each other's runs only when both
   `HARNESS_STATE_DIR` and `HARNESS_TREE_KEY` (orchestrator-only) are
   pinned identically on both sides. Regression coverage for these
   branches: `bash .claude/scripts/test_launchers.sh` (stub CLIs, no
   quota).
4. Corrective resume (one per task, retry-policy.md): re-invoke the
   launcher with `-r <SESSION_ID|last>` and the correction as the prompt
   file, keeping the SAME -m/-e/-s as the original — the launcher
   re-applies every override (a bare `codex exec resume` silently reverts
   to the global config; resume has no --sandbox/--profile flags).

## What the launcher does deterministically

- Preflight: Git or non-Git workspace/content snapshot and configuration snapshot.
  No automatic model-based sandbox probe. Use check-codex-sandbox.sh explicitly
  for a Windows shell diagnostic after environment changes or an actual failure.
- Postflight: actual content changes (including dirty/untracked files), unexpected
  commits in Git workspaces, read-only mutations, and the requested verifier.
  Missing evidence is incomplete, including an approved configuration-edit run.
- Control plane: a `control-plane-hash.sh` snapshot before the
  run is diffed afterwards, together with `CHANGED` entries under
  `.claude/{hooks,scripts,rules,agents,skills}/`, `settings*.json`,
  `sandbox-sensitive.json`, `model-bindings*.json` (they set the
  launcher's default -m/-e), `CLAUDE.md`, `AGENTS.md`,
  `check-windows-aliases.ps1`, `check-posix.sh`, `.stop-gate`, `.preflight-status`,
  `.mcp.json`, `.codex/hooks.json` and the out-of-repo Claude/agy/codex
  settings. Any hit —
  an unauthorized configuration change — prints `CONTROL_PLANE_WARNING`, turns
  `STATUS` into `BLOCKED(control-plane, was DONE|FAILED)` and exits 1: a
  changed hook is live on the NEXT Bash call, so inspect/revert before
  doing anything else. Harness-development delegations that are MEANT
  to edit these files run with `HARNESS_ALLOW_CONTROL_PLANE=1` (explicit
  user approval; orchestrator-only — the hook denies the marker from
  subagents); the report then shows `CONTROL_PLANE_APPROVED` instead.
  Three files the orchestrator SESSION itself writes during a detached
  run (`.preflight-status`, `.stop-gate`, `settings.local.json`) report
  as `CONTROL_PLANE_NOTICE` instead — never blocking, with the
  `settings.local.json` diff attached: read it, it is where a delegate
  would plant a `Bash(*)` allow entry.
- Report: `STATUS` carries codex exit + sandbox/model/effort (audit
  trail); `STOP_GATE_UNSATISFIED` turns DONE into FAILED when
  `.claude/.stop-gate` is still present (the verifier never passed —
  the Codex Stop hook asks for one continuation and then stops);
  `HOOKS_UNKNOWN:` names events without recognized outcome lines;
  `HOOKS_FAILED:` reports failure status lines. Both are advisory observations
  from merged output, not proof of hook execution or enforcement.
  `TIMEOUT` appears when the launcher killed codex at `-t`
  seconds (codex_exit=124). Inspect onboarding reads, API/tool waits and task
  progress to diagnose the cause before retrying; `TIMEOUT_WRAPPER: none`
  means GNU coreutils `timeout` was not first on PATH (Windows'
  `timeout.exe` would have killed the run), so only the Bash tool's
  600 s cap applied; `FULL_ACCESS_APPROVED`
  appears when a danger-full-access run was env-approved; `SCHEMA`
  names the `-o` schema when one was used; `FINAL_MESSAGE` is capped at
  60 lines (full text persists at `.claude/codex-logs/lastmsg-<ts>.txt`);
  the complete merged stream is `.claude/codex-logs/run-<ts>.log`.

## Exit codes

- 0 = DONE, clean. 1 = FAILED or SCOPE_WARNING — read the report.
- 2 = CODEX_UNAVAILABLE (binary/prompt-file problem) → codex fallback
  applies at the required capability/risk floor (delegation matrix). Required
  independent review cannot use an implementation fallback.
- The optional sandbox diagnostic is separate; ordinary launchers no longer emit
  exit 3 for a startup model probe. Classify actual execution failures before retry.
- 4 = HARNESS_DENIED: policy refusal (invalid sandbox/effort/verify/
  log-dir/image/schema/timeout args, bad flag, `max` without `-b`, any
  worker `ultra`, or a project `.codex/hooks.json` with no trust entry — the
  mirrored guards would be inert, so the run is refused before codex
  starts; `HARNESS_ALLOW_UNTRUSTED_HOOKS=1` is the explicit override). NOT a fallback trigger — fix the call and rerun.
- 5 = HARNESS_BUSY: another run is still executing in this tree —
  `--wait` for it, never start beside it.
- 6 = `--wait`/`--status` only: the run is still executing.
- 7 = `--wait`/`--status` only: no record for that RUN_ID (typo) —
  deliberately not 2, so it never reads as CODEX_UNAVAILABLE.
- `--status` on an `aborted` run returns the recorded exit (143 = the
  launcher was signalled, 1 = it died without a final state).

## Never delegate edits of the launcher itself through the launcher

Bash reads a running script incrementally: when the delegate edits
`codex-run.sh` mid-run, the interpreter's read offset lands in shifted
content and the run dies with a spurious syntax error AFTER codex
finishes (observed 2026-09-01 — an infrastructure failure per
retry-policy, not an implementation failure; the delegate's edit itself
was fine). Changes to `codex-run.sh` are made directly by the
orchestrator or by a worker in a separate immutable launcher copy — never through the launcher.

## Unauthorized commit recovery (delegate-output-trust.md §3)

`SCOPE_WARNING: unauthorized commit` / `NEW_COMMIT:` in the report, or a
new line in `git log --oneline -3`, means codex committed despite the
prompt. Recover in this order:
1. Confirm the commit is a descendant of the launcher's preflight
   baseline HEAD (the `HEAD` recorded before the run).
2. Inspect the actual configured remote/upstream and whether these commits
   were published; do not assume origin/main or a private non-deploying remote.
3. If recovery is authorized, `git reset --soft <baseline SHA>` preserves
   changes staged. Reset only to the RECORDED SHA, never a
   blind `HEAD~1`: more than one commit, or someone else's, may be
   involved. The changes stay staged for normal review and grading.

## Rules that still bind (pointers, not restatements)

- Preserve the baseline and existing work; use isolation for overlapping
  writes (delegate-output-trust.md §2). Do not create an extra commit merely
  to satisfy a delegation ritual. A `danger-full-access` sandbox needs
  BOTH explicit per-task user approval and `HARNESS_ALLOW_FULL_ACCESS=1`
  in the environment.
- After every run: delegation-fact probe + risk classification
  (delegate-output-trust.md, verification-tiering.md).
- Retry/escalation and resume-with-overrides: docs/orchestration/retry-policy.md.
- Benchmark sources and historical route measurements live in bindings/docs.

Shared checks live in launcher-common.sh/workspace-evidence.sh. Configuration
maintenance uses the already authorized launcher grant, never a timed approval
file. Log cleanup is explicit with harness-clean.py; starting a worker does not
delete past logs. Runtime scope: docs/runtime-boundary.md.
