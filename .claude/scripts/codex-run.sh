#!/usr/bin/env bash
LAUNCH_CLOCK=${EPOCHREALTIME:-$SECONDS}
# Deterministic launcher for Codex CLI delegations: policy checks →
# preflight (baseline, control-plane snapshot) → call →
# postflight (scope, control plane, verify) → compact report. Every run
# leaves a state record and a saved report (run-state.sh) so a killed
# launcher can still be waited on; `-b` detaches the run and `--wait` /
# `--status` read it back.

# -m/-e default from the bindings, resolved after option parsing (see the
# bindings block below); the builtin constants are the last resort when
# neither a bindings file nor python is usable.
MODEL=
EFFORT=
# The fallback when the bindings file is missing or unusable. Kept equal
# to `roles.implement.ladder[0]` (Sol/high — the entry
# 44 of 52 real runs used), so a broken bindings file does not silently
# drop implementation to a tier the matrix reserves for explicit `-m`.
BUILTIN_IMPL_MODEL=gpt-5.6-sol
BUILTIN_IMPL_EFFORT=high
BUILTIN_IMAGE_MODEL=gpt-5.6-terra
BUILTIN_IMAGE_EFFORT=medium
BINDINGS_PUBLIC=.claude/model-bindings.json
BINDINGS_LOCAL=.claude/model-bindings.local.json
BINDINGS_LINE=
SANDBOX=workspace-write
LOG_DIR=.claude/codex-logs
PROMPT_FILE=
VERIFY_CMD=
VERIFY_GIVEN=0
RESUME_ID=
IMAGE=
SCHEMA=
DETACH=0
# Under the Bash tool's 600 s hard kill no report line is ever printed;
# a launcher-side timeout turns the same hang (typically codex blocking on
# an open stdin pipe) into a deterministic `STATUS: FAILED (codex_exit=124)`.
TIMEOUT=570
TOOL=codex
ORIG_ARGS=("$@")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# Fail closed: without the state library the concurrency guard is gone.
[ -f "$SCRIPT_DIR/run-state.sh" ] || { echo "HARNESS_DENIED: $SCRIPT_DIR/run-state.sh missing" >&2; exit 4; }
. "$SCRIPT_DIR/launcher-common.sh" || exit 4
timing_init "$LAUNCH_CLOCK"

extract_tokens() {
    local log_file=${1:-}

    if [ -z "$log_file" ] || [ ! -s "$log_file" ]; then
        echo "unknown"
        return
    fi

    tr -d '\000' < "$log_file" |
        sed $'s/\033\\[[0-?]*[ -/]*[@-~]//g' |
        awk '
            { sub(/\r$/, "") }
            $0 == "tokens used" { awaiting_total = 1; total = ""; next }
            awaiting_total {
                if ($0 ~ /^[0-9][0-9,]*$/) total = $0
                awaiting_total = 0
            }
            END {
                if (total != "" && !awaiting_total) print total
                else print "unknown"
            }
        '
}

usage() {
    echo 'Usage: codex-run.sh -p <prompt-file> [-m MODEL] [-e EFFORT] [-s SANDBOX] [-v VERIFY_SCRIPT_FILE] [-l LOG_DIR] [-r SESSION_ID|last] [-i IMAGE_FILE] [-o OUTPUT_SCHEMA_FILE] [-t TIMEOUT_SECONDS] [-b]' >&2
    echo '       codex-run.sh --status <RUN_ID>' >&2
    echo '       codex-run.sh --wait <RUN_ID> [-t SECONDS<=570]' >&2
    echo '       -m/-e default from .claude/model-bindings.json (+ .local.json, local wins); -e max requires -b; worker ultra is denied' >&2
    echo 'HARNESS_DENIED: bad invocation (a typo is a policy error, not an availability failure)' >&2
    exit 4
}

if [ "${1:-}" = "--extract-tokens" ]; then
    extract_tokens "${2:-}"
    exit 0
fi
. "$SCRIPT_DIR/run-state.sh" || exit 4
# Read-back commands: no delegation, no quota — only the state record.
launcher_readback "$@"

while getopts ":p:m:e:s:v:l:r:i:o:t:b" opt; do
    case "$opt" in
        p) PROMPT_FILE=$OPTARG ;;
        m) MODEL=$OPTARG ;;
        e) EFFORT=$OPTARG ;;
        s) SANDBOX=$OPTARG ;;
        v) VERIFY_CMD=$OPTARG; VERIFY_GIVEN=1 ;;
        l) LOG_DIR=$OPTARG ;;
        r) RESUME_ID=$OPTARG ;;
        i) IMAGE=$OPTARG ;;
        o) SCHEMA=$OPTARG ;;
        t) TIMEOUT=$OPTARG ;;
        b) DETACH=1 ;;
        *) usage ;;
    esac
done

# Bindings resolution: an -m/-e not given on the command line comes from
# the public bindings merged with the local file (dicts merge key-by-key,
# local wins; arrays and scalars are replaced whole). The role is picked
# by -i: image_verify → roles.image_verify.default, otherwise
# roles.implement.ladder[0]. Bindings CONTENT never reaches a shell
# argument — python receives file paths and prints three plain tokens,
# validated in python AND re-validated here in bash. Any failure (no
# file, bad JSON, bad shape, garbage from the interpreter) falls back to
# the builtin constants and is named on the BINDINGS report line; it is
# never an availability failure, because the fallback values are the
# same defaults. (A missing or Store-alias python never gets this far:
# run-state.sh, sourced above, reports it as CODEX_UNAVAILABLE and exits
# 2 — an infrastructure condition, the fallback trigger.) The resolved
# effort still passes the effort check below.
MODEL_EXPLICIT=${MODEL:+1}
EFFORT_EXPLICIT=${EFFORT:+1}
if [ -n "$IMAGE" ]; then
    BIND_ROLE=image_verify; B_MODEL=$BUILTIN_IMAGE_MODEL; B_EFFORT=$BUILTIN_IMAGE_EFFORT
else
    BIND_ROLE=implement; B_MODEL=$BUILTIN_IMPL_MODEL; B_EFFORT=$BUILTIN_IMPL_EFFORT
fi
BIND_SOURCE=explicit
if [ -z "$MODEL" ] || [ -z "$EFFORT" ]; then
    BIND_PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
    if [ -z "$BIND_PY" ]; then
        BIND_SOURCE="builtin (python not found)"
    elif [ ! -f "$BINDINGS_PUBLIC" ]; then
        BIND_SOURCE="builtin (no $BINDINGS_PUBLIC)"
    else
        BIND_OUT=$("$BIND_PY" - "$BINDINGS_PUBLIC" "$BINDINGS_LOCAL" "$BIND_ROLE" <<'PY' 2>/dev/null
import json, re, sys
pub, loc, role = sys.argv[1], sys.argv[2], sys.argv[3]
def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a[k], v) if k in a else v
        return out
    return b
try:
    with open(pub, encoding="utf-8") as f:
        data = json.load(f)
    src = "public"
    try:
        with open(loc, encoding="utf-8") as f:
            data = merge(data, json.load(f))
        src = "public+local"
    except FileNotFoundError:
        pass
    roles = data["roles"]
    cell = roles["implement"]["ladder"][0] if role == "implement" else roles["image_verify"]["default"]
    model, effort = cell["model"], cell["effort"]
    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
    if not (isinstance(model, str) and token.fullmatch(model)):
        raise ValueError("model")
    if effort not in ("low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("effort")
    print(src, model, effort)
except Exception as e:
    print("ERR", type(e).__name__, str(e).replace("\n", " ")[:60])
PY
)
        case "$BIND_OUT" in
            ""|ERR*) BIND_SOURCE="builtin (bindings unusable: ${BIND_OUT:-no output})" ;;
            *)
                # Re-validate on the bash side: an interpreter that never ran
                # the script but printed something (the Windows Store python
                # alias stub, a startup banner) must fall back too, not be
                # read as three tokens and end in HARNESS_DENIED.
                BI_MODEL=$B_MODEL; BI_EFFORT=$B_EFFORT
                read -r BIND_SOURCE B_MODEL B_EFFORT <<< "$BIND_OUT"
                bind_bad=0
                case "$BIND_SOURCE" in public|public+local) ;; *) bind_bad=1 ;; esac
                case "$B_MODEL" in ""|-*|*[!A-Za-z0-9._-]*) bind_bad=1 ;; esac
                case "$B_EFFORT" in low|medium|high|xhigh|max|ultra) ;; *) bind_bad=1 ;; esac
                if [ "$bind_bad" -eq 1 ]; then
                    BIND_SOURCE="builtin (bindings unusable: unexpected parser output)"
                    B_MODEL=$BI_MODEL; B_EFFORT=$BI_EFFORT
                fi ;;
        esac
    fi
    [ -n "$MODEL" ] || MODEL=$B_MODEL
    [ -n "$EFFORT" ] || EFFORT=$B_EFFORT
fi
BINDINGS_LINE="BINDINGS: $BIND_SOURCE role=$BIND_ROLE model=$MODEL${MODEL_EXPLICIT:+ (explicit)} effort=$EFFORT${EFFORT_EXPLICIT:+ (explicit)}"

# Sandbox/effort are validated HERE, where the value is used: the PreToolUse
# hook matches command-line literals, which any launcher indirection bypasses.
# full access needs the same explicit per-task user approval the hook
# requires, carried as an environment variable instead of a prefix marker.
# Policy denials exit 4 (HARNESS_DENIED) — distinct from CODEX_UNAVAILABLE's
# exit 2, so a denial is never misread as the claude-implementer fallback
# trigger.
case "$SANDBOX" in
    read-only|workspace-write) ;;
    danger-full-access)
        if [ "${HARNESS_ALLOW_FULL_ACCESS:-}" != "1" ]; then
            echo "HARNESS_DENIED: -s danger-full-access requires explicit per-task user approval (HARNESS_ALLOW_FULL_ACCESS=1)" >&2
            exit 4
        fi ;;
    *)
        echo "HARNESS_DENIED: unknown sandbox mode '$SANDBOX' (read-only|workspace-write|danger-full-access)" >&2
        exit 4 ;;
esac
# Ladder-only by policy (retry-policy.md); codex's `minimal` is deliberately
# excluded — the harness never routes work at that tier.
case "$EFFORT" in
    low|medium|high|xhigh|max|ultra) ;;
    *)
        echo "HARNESS_DENIED: unknown effort '$EFFORT' (low|medium|high|xhigh|max; worker ultra is denied)" >&2
        exit 4 ;;
esac
# Keep ultra parseable above so a bindings-sourced value reaches this
# policy refusal instead of silently falling back to builtin settings.
# Ultra enables automatic subagents, contrary to the worker role contract.
# --status/--wait remain usable for existing runs and never reach this point.
if [ "$EFFORT" = ultra ]; then
    echo "$BINDINGS_LINE"
    echo 'HARNESS_DENIED: Codex worker ultra enables re-delegation; select a single-agent effort (session-role.md)' >&2
    exit 4
fi
# Max runs detached so a long single-agent run survives the tool timeout.
case "$EFFORT" in
    max)
        if [ "$DETACH" -ne 1 ]; then
            echo "$BINDINGS_LINE"
            if [ -n "$EFFORT_EXPLICIT" ]; then EFFORT_SRC="the -e flag"; else EFFORT_SRC="the bindings: $BIND_SOURCE"; fi
            echo "HARNESS_DENIED: -e $EFFORT requires -b (max runs detached only — poll with --wait; retry-policy.md; effort came from $EFFORT_SRC)" >&2
            exit 4
        fi ;;
esac
# -v takes a PATH to an orchestrator-authored verify script, not a free
# command string: this launcher sits on permissions.allow, so a free string
# executed host-side would be an unprompted arbitrary-execution path.
# An empty value fails closed too — a silently-skipped verify gate from an
# unexpanded variable must not read as "verification not requested".
if [ -n "$RESUME_ID" ]; then
    case "$RESUME_ID" in
        *[!A-Za-z0-9_-]*)
            echo "HARNESS_DENIED: invalid -r session id '$RESUME_ID' (alphanumeric/dash/underscore or 'last')" >&2
            exit 4 ;;
    esac
fi
if [ "$VERIFY_GIVEN" -eq 1 ] && [ -z "$VERIFY_CMD" ]; then
    echo "HARNESS_DENIED: -v was given an empty value (unexpanded variable?)" >&2
    exit 4
fi
if [ -n "$VERIFY_CMD" ] && [ ! -f "$VERIFY_CMD" ]; then
    echo "HARNESS_DENIED: -v must be a path to an existing verify script file (free command strings are not accepted)" >&2
    exit 4
fi
# -l is created with mkdir -p by a launcher that sits on permissions.allow,
# so it must stay inside the repository: relative, no `..` component, no
# drive/UNC prefix.
case "$LOG_DIR" in
    ""|/*|*\\*|[A-Za-z]:*|..|../*|*/..|*/../*)
        echo "HARNESS_DENIED: -l must be a repo-relative directory without '..' or backslashes (got '$LOG_DIR')" >&2
        exit 4 ;;
esac
if [ -n "$IMAGE" ] && [ ! -f "$IMAGE" ]; then
    echo "HARNESS_DENIED: -i must be a path to an existing image file" >&2
    exit 4
fi
if [ -n "$SCHEMA" ] && [ ! -f "$SCHEMA" ]; then
    echo "HARNESS_DENIED: -o must be a path to an existing JSON Schema file" >&2
    exit 4
fi
# `timeout 0` means "no limit" to GNU timeout, so 0 is refused too.
case "$TIMEOUT" in
    ""|*[!0-9]*|0*)
        echo "HARNESS_DENIED: -t must be a positive integer number of seconds (got '$TIMEOUT')" >&2
        exit 4 ;;
esac

[ -n "$PROMPT_FILE" ] || usage          # no -p at all is a bad invocation (exit 4)
if [ ! -s "$PROMPT_FILE" ]; then
    echo "CODEX_UNAVAILABLE: prompt file is missing or empty" >&2
    exit 2
fi
if ! command -v codex >/dev/null 2>&1; then
    echo "CODEX_UNAVAILABLE: codex binary not found" >&2
    exit 2
fi
# Query effective trust before admission. Table headers alone do not establish
# current command hashes, enabled state, or whether the hooks feature is active.
if [ -f .codex/hooks.json ] && [ "${HARNESS_ALLOW_UNTRUSTED_HOOKS:-}" != "1" ]; then
    "$RS_PY" "$SCRIPT_DIR/harness-session.py" check-codex-hooks || exit 4
fi

launcher_timeout || exit 4

mkdir -p "$LOG_DIR" || exit 2
# Timestamp + pid: unique even for two launchers started in the same second.
RUN_ID=${HARNESS_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
case "$RUN_ID" in *[!A-Za-z0-9TZ_-]*) echo "HARNESS_DENIED: bad HARNESS_RUN_ID" >&2; exit 4 ;; esac
TIMESTAMP=$RUN_ID
# Never start on top of a run that is still executing in this tree.
LAUNCHER_PID=$$
CHILD_PID=
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
STARTED_EPOCH=$(date +%s)
LAUNCHER_STIME=$(pid_stime "$$")
# The record exists from this moment (state `starting`), so a concurrent
# launcher is refused even while preflight is still running.
admit_run

# -b: re-launch this same invocation detached (nohup, own stdio → file)
# and return at once with the RUN_ID. The child skips this block via
# HARNESS_RUN_CHILD=1 and otherwise behaves exactly like a foreground run
# — its report lands in report-<RUN_ID>.txt for --wait / --status.
if [ "$DETACH" -eq 1 ] && [ "${HARNESS_RUN_CHILD:-}" != "1" ]; then
    LAUNCHER_OUT="$LOG_DIR/launcher-$RUN_ID.out"
    HARNESS_RUN_CHILD=1 HARNESS_RUN_ID=$RUN_ID nohup bash "$0" "${ORIG_ARGS[@]}" > "$LAUNCHER_OUT" 2>&1 < /dev/null &
    DETACHED_PID=$!
    disown "$DETACHED_PID" 2>/dev/null
    echo "RUN_ID: $RUN_ID"
    echo "$BINDINGS_LINE"
    echo "DETACHED: launcher pid $DETACHED_PID, stdout → $LAUNCHER_OUT"
    echo "STATE: $(state_file "$RUN_ID")"
    echo "WAIT: bash $0 --wait $RUN_ID -t 570   (exit 6 = still running, call again; --status for one look)"
    exit 0
fi
# Plumbing only: consumed above, never meant for the CLI child or a -v
# verify script (a nested launcher inheriting HARNESS_RUN_CHILD=1 would
# skip its own detach and adopt this RUN_ID — seen in test_launchers.sh).
unset HARNESS_RUN_CHILD HARNESS_RUN_ID

RUN_LOG="$LOG_DIR/run-$TIMESTAMP.log"
REPORT_FILE=$(report_file "$RUN_ID")
VERIFY_LOG=$(mktemp "${TMPDIR:-/tmp}/codex-verify.XXXXXX") || exit 2
CHANGED_FILE=$(mktemp "${TMPDIR:-/tmp}/codex-changed.XXXXXX") || exit 2
TREE_BEFORE=$(mktemp "${TMPDIR:-/tmp}/codex-tree-before.XXXXXX") || exit 2
TREE_AFTER=$(mktemp "${TMPDIR:-/tmp}/codex-tree-after.XXXXXX") || exit 2
CP_BEFORE=$(mktemp "${TMPDIR:-/tmp}/codex-cp-before.XXXXXX") || exit 2
CP_AFTER=$(mktemp "${TMPDIR:-/tmp}/codex-cp-after.XXXXXX") || exit 2
# Persisted (not a tmpfile): when the report truncates it, the full final
# message must remain readable after the run.
LAST_MSG="$LOG_DIR/lastmsg-$TIMESTAMP.txt"

# State bookkeeping: `running` from the moment codex is spawned; `done`
# only after the report is written; anything else (signal, script error)
# ends as `aborted` so --wait never hangs on a record nobody will finish.
FINAL_STATE_WRITTEN=0
cleanup() { rm -f "$VERIFY_LOG" "$CHANGED_FILE" "$TREE_BEFORE" "$TREE_AFTER" "$CP_BEFORE" "$CP_AFTER" ; }
on_exit() {
    if [ "$FINAL_STATE_WRITTEN" -eq 0 ] && [ -f "$(state_file "$RUN_ID")" ]; then
        state_write aborted 1 "launcher exited before postflight"
    fi
    cleanup
}
# Signal: TERM the child's whole process group (timeout + codex + its
# shells), wait up to 15 s for it to actually die (KILL after that), and
# only then record `aborted` — a record must never say "not running"
# while the delegate may still be writing files.
on_signal() {
    CHILD_TREE_NOTE="no child was running"
    terminate_child_tree
    state_write aborted 143 "launcher received a termination signal; codex child pid ${CHILD_PID:-none}: $CHILD_TREE_NOTE"
    FINAL_STATE_WRITTEN=1
    cleanup
    exit 143
}
trap on_exit EXIT
trap on_signal HUP INT TERM

# Capture the verifier BEFORE codex runs: the verify path may sit inside the
# workspace or TMPDIR that codex can write to, so the bytes stay in this
# shell and the original must still match them when it runs.
VERIFY_BYTES=
[ -z "$VERIFY_CMD" ] || verify_capture "$VERIFY_CMD"

HEAD=
# Control-plane snapshot: the files that enforce this
# harness, in and out of the repo. Compared after the run. FAIL CLOSED: a
# snapshot that could not be taken is itself a finding.
control_before
SETTINGS_LOCAL=.claude/settings.local.json
SETTINGS_LOCAL_SNAP="$LOG_DIR/settings-local-$TIMESTAMP.json"
[ -f "$SETTINGS_LOCAL" ] && cp -p "$SETTINGS_LOCAL" "$SETTINGS_LOCAL_SNAP" 2>/dev/null
. "$SCRIPT_DIR/workspace-evidence.sh" || exit 4
WORKSPACE_EXCLUDES=(--exclude "$RUN_LOG" --exclude "$LAST_MSG"
    --exclude "$SETTINGS_LOCAL_SNAP" --exclude "$LOG_DIR/baseline-$TIMESTAMP.diff"
    --exclude "$LOG_DIR/launcher-$RUN_ID.out")
workspace_before "$TREE_BEFORE" "${WORKSPACE_EXCLUDES[@]}"
if [ "$WORKSPACE_MODE" = git ]; then
    HEAD=$(git rev-parse --verify HEAD 2>/dev/null || true)
    save_git_baseline
fi

CODEX_GIT_ARGS=()
[ "$WORKSPACE_MODE" != files ] || CODEX_GIT_ARGS+=(--skip-git-repo-check)

# Optional pass-through arguments, assembled as an array so paths with
# spaces survive. `-i` attaches an image (codex's native image input —
# the fallback route for visual verification); `-o` constrains codex's
# final message to a JSON Schema (`--output-schema`), which makes the
# FINAL_MESSAGE block machine-readable instead of free text.
EXTRA_ARGS=("${CODEX_GIT_ARGS[@]}")
if [ -n "$IMAGE" ]; then EXTRA_ARGS+=(-i "$IMAGE"); fi
if [ -n "$SCHEMA" ]; then EXTRA_ARGS+=(--output-schema "$SCHEMA"); fi

# The CLI runs as a background child in its OWN process group (set -m) so
# its PID is recorded and a signal to the launcher can be forwarded to the
# whole tree; `wait` keeps the call synchronous.
#
# HARNESS_DELEGATE_RUN marks this codex process as a DELEGATE for the hooks
# it spawns (.codex/hooks.json -> deny_dangerous.py --host codex). Codex
# payloads carry no agent_id -- `codex exec` is its own main session -- so
# without this marker the subagent-scoped rules (control-plane writes,
# commit/push) would not apply to a delegate. The marker lives in the codex
# PROCESS environment, which the delegate's own shell cannot rewrite for
# its parent. A codex session a human starts by hand has no marker and is
# treated as an orchestrator, exactly like a main Claude session.
export HARNESS_DELEGATE_RUN=1
# Fixed role context precedes the task; task text still arrives only via stdin.
DELEGATE_INSTRUCTION='You are a DELEGATE assigned by a parent orchestrator. HARNESS_DELEGATE_RUN=1. Role is already resolved. Skip the Orchestrator workflow and its linked reading/setup; do not run harness-route.py, spawn agents or launch model CLIs. Follow the assigned task and applicable project/security/verification rules. For guidance, read only missing task-relevant sections and the required platform subsection; do not read whole harness manuals or reread unchanged supplied material for onboarding. Never commit, push or revert existing work. Edit the control plane only if this launcher already has HARNESS_ALLOW_CONTROL_PLANE=1. If verification is blocked by the environment, report the exact failed check to the parent; do not expand into permission repair or repeated cleanup. Return CHANGED, VERIFY, NOTES or NEEDS_INPUT. Your final report is checked by the parent.'
timing_enter cli
set -m
if [ -n "$RESUME_ID" ]; then
    # Corrective resume with EVERY override re-applied: a bare resume
    # silently reverts to the global config (retry-policy.md §2), and
    # `codex exec resume` has no --sandbox/--profile flags (verified
    # 2026-09 on 0.151.0, re-checked on 0.152.1) — sandbox rides on
    # -c sandbox_mode instead.
    if [ "$RESUME_ID" = last ]; then
        set -- resume --last
    else
        set -- resume "$RESUME_ID"
    fi
    "${RUNNER[@]}" codex exec "$@" -c windows.sandbox=unelevated -c "model_reasoning_effort=$EFFORT" -m "$MODEL" -c "sandbox_mode=$SANDBOX" --output-last-message "$LAST_MSG" "${EXTRA_ARGS[@]}" "$DELEGATE_INSTRUCTION Follow the correction provided in the <stdin> block." < "$PROMPT_FILE" > "$RUN_LOG" 2>&1 &
else
    "${RUNNER[@]}" codex exec -c windows.sandbox=unelevated -c "model_reasoning_effort=$EFFORT" -m "$MODEL" --sandbox "$SANDBOX" --output-last-message "$LAST_MSG" "${EXTRA_ARGS[@]}" "$DELEGATE_INSTRUCTION Follow the task specification provided in the <stdin> block." < "$PROMPT_FILE" > "$RUN_LOG" 2>&1 &
fi
CHILD_PID=$!
set +m
CHILD_STIME=$(pid_stime "$CHILD_PID")
state_write running "" "codex running"
wait "$CHILD_PID"
CODEX_EXIT=$?
timing_enter postflight

workspace_after "$TREE_BEFORE" "$TREE_AFTER" "$CHANGED_FILE" "${WORKSPACE_EXCLUDES[@]}"
DIRTY_EDITS=0
[ ! -s "$CHANGED_FILE" ] || DIRTY_EDITS=1

# Control plane: a delegate that edits the harness's own
# enforcement files — or out-of-repo settings reachable under full
# access — can disarm the NEXT Bash call before the orchestrator reads
# this report. Treated like an unauthorized commit: the run cannot be
# DONE unless the orchestrator relayed explicit user approval through
# HARNESS_ALLOW_CONTROL_PLANE=1 (harness development is the legitimate
# case).
control_after

CP_BLOCK=0
if [ -n "$CP_HITS" ] && [ "${HARNESS_ALLOW_CONTROL_PLANE:-}" != "1" ]; then
    CP_BLOCK=1
fi

POST_HEAD=
[ "$WORKSPACE_MODE" != git ] || POST_HEAD=$(git rev-parse --verify HEAD 2>/dev/null || true)
SCOPE_WARNING=0
if [ "$POST_HEAD" != "$HEAD" ]; then
    SCOPE_WARNING=1
    NEW_COMMIT=$(git log --oneline -1 2>/dev/null)
fi

if [ -n "$VERIFY_CMD" ]; then
    timing_enter verify
    verify_run "$VERIFY_CMD" > "$VERIFY_LOG" 2>&1
    VERIFY_EXIT=$?
    timing_enter postflight
fi

if [ "$CODEX_EXIT" -eq 0 ] && [ -s "$RUN_LOG" ]; then
    STATUS=DONE
    OUTPUT_STATE=non-empty
else
    STATUS=FAILED
    if [ -s "$RUN_LOG" ]; then
        OUTPUT_STATE=non-empty
    else
        OUTPUT_STATE=empty
    fi
fi
# A process exit of zero never overrides a failed task verifier.
if [ -n "$VERIFY_CMD" ] && [ "$VERIFY_EXIT" -ne 0 ]; then
    STATUS="FAILED(verification, was $STATUS)"
fi
# Stop-gate: stop_gate.py removes the marker only when the verifier PASSES,
# so a marker still present means verification never passed -- regardless of
# what the Stop hook reported (the Codex branch stops
# asking for continuations after one round, which must not read as success).
if [ -f .claude/.stop-gate ]; then
    STATUS="FAILED(stop-gate unsatisfied, was $STATUS)"
    STOP_GATE_LEFT=1
else
    STOP_GATE_LEFT=
fi
# Session-owned notices are recorded but do not turn a read into a write.
if [ "$SANDBOX" = read-only ] && grep -qvE "$CONTROL_PLANE_NOTICE_RE" "$CHANGED_FILE"; then
    STATUS="FAILED(read-only workspace changed, was $STATUS)"
fi
if [ "$CP_EVIDENCE_OK" -ne 1 ]; then
    STATUS="FAILED(control-plane evidence unavailable, was $STATUS)"
fi
if [ "$WORKSPACE_OK" -ne 1 ]; then
    STATUS="FAILED(workspace evidence unavailable, was $STATUS)"
fi
if [ "$SCOPE_WARNING" -eq 1 ]; then
    STATUS="FAILED(unauthorized-commit)"
fi
if [ "$CP_BLOCK" -eq 1 ]; then
    STATUS="BLOCKED(control-plane, was $STATUS)"
fi
if [ "$STATUS" = DONE ]; then EXIT_CODE=0; else EXIT_CODE=1; fi

# Hook canary: advisory observations from merged CLI/tool output, never an
# exit-code gate. Whole status lines (LF or CRLF) exclude prose and numbered
# excerpts, but a tool can still print an identical line. Matches do not
# authenticate hook execution; absent matches leave activity unknown.
# Trust preflight remains separate. No automatic retest or retry here.
hooks_canary() {
    [ -f .codex/hooks.json ] || return 0
    # Report Failed even when the same event also has Completed/Blocked.
    local observed="" missing="" failed="" event
    for event in PreToolUse Stop; do
        grep -Eq "^hook: $event (Completed|Blocked)"$'\r?$' "$RUN_LOG" 2>/dev/null && observed="$observed $event"
        grep -Eq "^hook: $event Failed"$'\r?$' "$RUN_LOG" 2>/dev/null && failed="$failed $event"
        case "$observed$failed" in *" $event"*) ;; *) missing="$missing $event" ;; esac
    done
    if [ -n "$failed" ]; then
        echo "HOOKS_FAILED: failure status lines observed for:$failed — inspect the run log; hook execution and enforcement cannot be confirmed from merged output alone."
    fi
    if [ -n "$missing" ]; then
        echo "HOOKS_UNKNOWN: no recognized outcome lines for:$missing — hook activity could not be confirmed from this log."
    fi
}

report() {
    echo "STATUS: $STATUS (codex_exit=$CODEX_EXIT, output=$OUTPUT_STATE, sandbox=$SANDBOX, model=$MODEL, effort=$EFFORT)"
    echo "$BINDINGS_LINE"
    echo "RUN_ID: $RUN_ID (state: $(state_file "$RUN_ID"), report: $REPORT_FILE)"
    if [ -n "$STALE_CLEANED" ]; then
        echo "STALE_RUN_CLEANED: earlier run(s) $STALE_CLEANED had died without a final state — marked aborted"
    fi
    if [ "$CODEX_EXIT" -eq 124 ]; then
        echo "TIMEOUT: codex reached the ${TIMEOUT}s execution limit — inspect onboarding reads, API/tool waits and task progress before choosing a retry"
    fi
    if [ -n "$RESUME_ID" ]; then
        echo "RESUME: $RESUME_ID (all overrides re-applied)"
    fi
    if [ -n "${STOP_GATE_LEFT:-}" ]; then
        echo "STOP_GATE_UNSATISFIED: .claude/.stop-gate is still present — the task's verify script never passed; the run is not done regardless of what the model said"
    fi
    hooks_canary
    if [ -n "$SCHEMA" ]; then
        echo "SCHEMA: $SCHEMA (FINAL_MESSAGE is JSON constrained to it)"
    fi
    if [ "$SANDBOX" = danger-full-access ]; then
        echo "FULL_ACCESS_APPROVED: HARNESS_ALLOW_FULL_ACCESS=1 was present for this run"
    fi
    echo "$WORKSPACE_DESCRIPTION"
    echo "CHANGED: $CHANGED"
    if [ "$DIRTY_EDITS" -eq 1 ]; then
        echo "CHANGED_CONTENT: file changes detected by workspace snapshots; inspect the reported paths"
    fi
    echo "TOKENS: $(extract_tokens "$RUN_LOG")"
    timing_report
    if [ -n "$VERIFY_CMD" ]; then
        echo "VERIFY: exit $VERIFY_EXIT"
        tail -n 5 "$VERIFY_LOG"
    else
        echo "VERIFY: not requested"
    fi
    if [ "$SCOPE_WARNING" -eq 1 ]; then
        echo "SCOPE_WARNING: unauthorized commit"
        echo "NEW_COMMIT: $NEW_COMMIT"
    fi
    if [ -n "$CP_HITS" ]; then
        if [ "$CP_BLOCK" -eq 1 ]; then
            echo "CONTROL_PLANE_WARNING: harness enforcement files changed: $CP_HITS — review/revert before any further Bash call; rerun with HARNESS_ALLOW_CONTROL_PLANE=1 only with explicit user approval"
        else
            echo "CONTROL_PLANE_APPROVED: HARNESS_ALLOW_CONTROL_PLANE=1 was present; changed: $CP_HITS"
        fi
    fi
    if [ -n "$CP_NOTICE" ]; then
        echo "CONTROL_PLANE_NOTICE: session-owned or out-of-repo settings changed (not blocking — the orchestrator session and the CLIs themselves write these; judge the diff): $CP_NOTICE"
        case ",$CP_NOTICE," in *",$SETTINGS_LOCAL,"*)
            SL_DIFF=$(diff -u "$([ -f "$SETTINGS_LOCAL_SNAP" ] && echo "$SETTINGS_LOCAL_SNAP" || echo /dev/null)" "$SETTINGS_LOCAL" 2>/dev/null)
            printf '%s\n' "$SL_DIFF" | head -n 40
            # A permission/allow line ADDED to settings.local.json is the
            # one escalation shape worth a loud flag: a
            # user's own approval looks identical, so this stays a NOTICE,
            # not a block — but it must not scroll past unread.
            if printf '%s\n' "$SL_DIFF" | grep -qiE '^\+.*("allow"|Bash\(|"permissions")'; then
                echo "CONTROL_PLANE_NOTICE: settings.local.json ADDED a permission/allow entry — confirm this is YOUR approval, not a delegate escalation, before trusting this run"
            fi ;;
        esac
    fi
    echo "LOG: $RUN_LOG"
    if [ -s "$LAST_MSG" ]; then
        # Capped: this output enters (and is re-billed in) the orchestrator's
        # context every turn — the full message stays in $LAST_MSG.
        echo "FINAL_MESSAGE:"
        head -n 60 "$LAST_MSG"
        if [ "$(wc -l < "$LAST_MSG")" -gt 60 ]; then
            echo "[truncated at 60 lines — full message: $LAST_MSG]"
        fi
    else
        # --output-last-message produced nothing (failed/killed run) —
        # fall back to the merged-stream tail heuristic.
        echo "TAIL:"
        tail -n 15 "$RUN_LOG"
    fi
}
# The report is persisted first (so --wait/--status can serve it even when
# stdout is gone), then echoed; the state flips to done only after that.
report > "$REPORT_FILE"
cat "$REPORT_FILE"
state_write done "$EXIT_CODE" "$STATUS" && FINAL_STATE_WRITTEN=1
exit "$EXIT_CODE"
