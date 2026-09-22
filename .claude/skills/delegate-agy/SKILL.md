---
name: delegate-agy
description: Antigravity (agy) delegation recipe for this harness — load BEFORE composing any agy-run.sh launcher call (prompt-file conventions, absolute-path rule, report lines, exit codes, control-plane gate).
---

# agy delegation recipe (launcher path)

Routing lives in `docs/orchestration/delegation-matrix.md` and the current
model bindings. The declared specialty is HTML/CSS/static prototypes; other
pure-write tasks need an explicit assignment. Images use the codex launcher
`-i` route, never agy. This skill is the HOW. Use it only when a pure-write task fits the tool
capabilities and handoff is worthwhile. The worker must not own its grading
gate; tasks requiring execution use a capable route instead. Domain-specific
file restrictions come from the project. No raw untrusted content enters
the prompt (rules/security-boundary.md).

## Invocation

1. Write the fully self-contained task prompt to a scratch file
   (single-quoted heredoc). It must: give every file agy WRITES an
   ABSOLUTE path (headless agy resolves relative paths and "the current
   directory" into `~/.gemini/antigravity-cli/scratch/` and reports
   success anyway); NAME the files agy may read; end with "List the
   files you read as lines starting with `READ:`"; state that
   `git commit` / `git push` are forbidden. Keep it under 30 KB (the
   prompt travels as one command-line argument on Windows).
2. Call the launcher DIRECTLY with Bash, `timeout: 600000`:

       bash .claude/scripts/agy-run.sh -p <prompt-file> [-e low|medium|high] [-x <expected-file>[,...]] [-t <seconds>]

   The model is the agy CLI default; this write launcher does not select a model
   from the binding JSON. Do not report a binding candidate as the model used.
   Defaults: `medium` / `-t 570` / logs in `.claude/agy-logs/`. `-e low`
   for mechanical bulk (comments, boilerplate, rote restructuring);
   `high` only when a failure diagnosis shows reasoning was the blocker
   (one bump, then return to the host's implementation fallback). `-x` lists the
   absolute output paths the task must produce; the launcher verifies
   they exist, are non-empty, and were written BY THIS RUN (new, or
   mtime/size changed since preflight) — `PRODUCED` / `MISSING` is the
   only proof the writes landed where intended.
   Long or lost-call-proof runs: add `-b` — the launcher returns
   `RUN_ID:`/`WAIT:` at once and continues detached; poll with
   `bash .claude/scripts/agy-run.sh --wait <RUN_ID> -t 570` (exit 6 =
   still running) or `--status <RUN_ID>`. Every run records
   `~/.claude/harness-runs/state-<RUN_ID>.json` (starting → running →
   done | aborted) and `report-<RUN_ID>.txt` outside the workspace (for
   agy, whose grant reaches every path, this is detection-grade); a new
   launcher refuses to start while another run — agy OR codex — in this
   tree is `running` (`HARNESS_BUSY: STALE_RUN <id>`, exit 5; a
   read-only codex advisory run is the one exemption);
   `HARNESS_ALLOW_FORGET=1 … --forget <RUN_ID>` (orchestrator-only)
   releases a record the PID identity check could not settle.

## What the launcher does deterministically

- Policy checks (exit 4 `HARNESS_DENIED`): effort not in low|medium|high,
  `-l` outside the repo, bad `-t`, prompt over 30 KB, and — the agy grant
  gate — a `command(...)` entry in agy's global settings without
  `HARNESS_ALLOW_AGY_COMMAND=1` (per-task, explicit user approval; the
  report then shows `AGY_COMMAND_APPROVED`).
- Availability (exit 2 `AGY_UNAVAILABLE`): missing binary or python,
  missing prompt file, unparseable global settings or no `write_file`
  entry in its `permissions.allow`, an agy `error` naming quota / rate
  limit / login (echoed as `AGY_ERROR:`), no parseable JSON result (one
  automatic retry when the first attempt produced no output AND changed
  nothing) → the orchestrator uses the host's implementation fallback.
  `HARNESS_ALLOW_AGY_COMMAND=1` and the diagnostic
  `HARNESS_AGY_SETTINGS=` override are orchestrator-only (the hook
  denies them from subagents); an override is echoed as
  `SETTINGS_OVERRIDE`.
- Preflight: porcelain baseline + `git diff HEAD` snapshot,
  `control-plane-hash.sh` snapshot (fail-closed).
- Call: `agy --log-file … --effort … --output-format json
  --print-timeout <t>s -p "$(cat prompt)"`, wrapped in GNU `timeout`
  when coreutils is first on PATH (`TIMEOUT_WRAPPER: none` otherwise).
- Postflight: `CHANGED` / `CHANGED_CONTENT`, `SCOPE_WARNING`/`NEW_COMMIT`,
  `PRODUCED`/`MISSING`, `READ:` lines extracted from the response,
  `AGY_DENIED` when the banner shows a tool was auto-denied (a `command`
  reach → re-scope as a pure write task, never widen the grant on your
  own), and the control-plane gate: any enforcement-file change or a
  failed snapshot → `CONTROL_PLANE_WARNING`, `STATUS:
  BLOCKED(control-plane, was …)`, exit 1 (`HARNESS_ALLOW_CONTROL_PLANE=1`
  is orchestrator-only, for approved harness work). Session-owned files
  (`.preflight-status`, `.stop-gate`, `settings.local.json`) report as a
  non-blocking `CONTROL_PLANE_NOTICE` with the settings.local.json diff
  attached — read that diff. `-x` proof uses nanosecond mtime on GNU
  stat; on macOS (`stat -f %m`, whole seconds) a same-second same-size
  rewrite still reads as MISSING.
- Report: `STATUS` (agy exit, agy status, attempts, effort, turns),
  `TOKENS` (Google plan usage), `RESPONSE` capped at 60 lines (full text
  in `.claude/agy-logs/response-<ts>.txt`).

## Exit codes

- 0 = DONE (agy SUCCESS, nothing denied, all `-x` outputs proven
  written, a non-empty response or produced output, no unauthorized
  commit, control plane untouched). 1 = FAILED (including
  `FAILED(unauthorized-commit)`) / BLOCKED — read the report. 2 = AGY_UNAVAILABLE → the host's implementation fallback (delegation matrix).
  4 = HARNESS_DENIED (bad args, bad flag) → fix the call.
  5 = HARNESS_BUSY (another run still executing in this tree) → `--wait`.
  6 = `--wait`/`--status` only: still running. 7 = no such RUN_ID.

## Rules that still bind (pointers)

- Record existing work before delegating; actual-output verification + risk
  classification after (delegate-output-trust.md, verification-tiering.md).
- Noise to ignore: "You are not logged into Antigravity" (language-server
  subprocess) and `transcript.jsonl: The system cannot find the path
  specified` in agy's own log — neither means the run failed.
