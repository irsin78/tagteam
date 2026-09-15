#!/usr/bin/env bash
# Shared run-state machinery for the delegation launchers (sourced by
# codex-run.sh and agy-run.sh). A launcher that dies — Bash-tool 600 s
# kill, lost session, SIGTERM — used to leave nothing to wait on except a
# growing log. Every run now has a deterministic record:
#
#   $STATE_DIR/<tree-key>/state-<RUN_ID>.json   starting|running|done|aborted
#   $STATE_DIR/<tree-key>/report-<RUN_ID>.txt   the full report, even if
#                                               stdout was lost
#
# STATE_DIR is OUTSIDE the workspace on purpose (~/.claude/harness-runs):
# codex under workspace-write cannot write there, so a delegate cannot
# forge a "done" record for a launcher that dies mid-run. (agy's
# write_file(*) reaches everything — for agy this is detection-grade, like
# the control-plane records.) Records are sharded per working tree (key =
# sha1 of the canonical project root, Git or non-Git) so a launcher only scans its own
# tree's runs. Old records are removed only by explicit maintenance.
#
# LIMITS (stated so they can be checked): the guard is per state dir AND
# per tree key. A WSL-lane run (`/mnt/d/...`) and a Windows-side run
# (`D:/...`) on the same tree canonicalize to different keys, so they see
# each other only when BOTH `HARNESS_STATE_DIR` and `HARNESS_TREE_KEY`
# are pinned to the same values on both sides (orchestrator-only
# overrides — the hook denies them from subagents, because relocating
# the records or the key voids the busy guard). `subst` drives / UNC
# views of one tree get different keys the same way.
#
# Consumers: `<launcher> --status <RUN_ID>` (one look), `<launcher> --wait
# <RUN_ID> [-t N]` (poll up to N seconds) and `<launcher> --forget
# <RUN_ID>` (orchestrator-only: mark a record aborted by hand when a PID
# was reused by an unrelated process and the guard stays busy).
#
# Contract for the sourcing launcher: set TOOL (codex|agy|claude), RUN_ID,
# TIMEOUT before calling state_*; set CHILD_PID once the CLI is launched.
# TOOL must be set BEFORE this file is sourced -- rs_unavailable can fire
# during the source itself, and an unset TOOL degrades its tag to
# HARNESS_UNAVAILABLE, which no fallback rule names.

# Exit contract: INFRASTRUCTURE conditions -- no usable
# python, HOME unset, bash < 4, a python that does not run scripts (the
# Windows Store alias stub) -- are `<TOOL>_UNAVAILABLE` + exit 2, the
# launcher's fallback trigger, never a policy denial. An EXPLICIT malformed
# HARNESS_TREE_KEY and a STATE_DIR that cannot be created stay
# HARNESS_DENIED (exit 4). `exit` inside a sourced file ends the launcher
# shell itself, so the launcher's `|| exit 4` after its source line is
# never reached for these.
rs_unavailable() {
    local tag
    case "${TOOL:-}" in
        codex) tag=CODEX_UNAVAILABLE ;;
        agy) tag=AGY_UNAVAILABLE ;;
        claude) tag=CLAUDE_UNAVAILABLE ;;
        *) tag=HARNESS_UNAVAILABLE ;;
    esac
    echo "$tag: run-state: $1" >&2
    exit 2
}
RS_PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
if [ -z "$RS_PY" ] || [ -z "${HOME:-}" ]; then
    rs_unavailable "needs python (or python3) on PATH and HOME set"
fi
# Associative arrays and process substitution: bash 4+ (bash 3.2 would
# silently degrade `declare -A` into an indexed array). Not exercised by
# test_launchers.sh (no dev box has bash 3); check-posix.sh warns about
# it at install time.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    rs_unavailable "needs bash >= 4 (found ${BASH_VERSION:-?})"
fi
RS_IS_WINDOWS=0
case "${OS:-}:$(uname -s 2>/dev/null)" in Windows_NT:*|*:MINGW*|*:MSYS*|*:CYGWIN*) RS_IS_WINDOWS=1 ;; esac

# Resolve root, canonical path and key together. The marker distinguishes a
# Python alias that never runs scripts from a failed workspace inspection.
RS_CONTEXT=$("$RS_PY" "${BASH_SOURCE[0]%/*}/workspace-snapshot.py" --context)
RS_CONTEXT_EXIT=$?
RS_CONTEXT=${RS_CONTEXT//$'\r'/}
[[ "$RS_CONTEXT" == HARNESS_CONTEXT_V1$'\n'* ]] || {
    [ "$RS_CONTEXT" != HARNESS_CONTEXT_V1 ] || {
        echo "HARNESS_DENIED: cannot identify the workspace root" >&2; exit 4;
    }
    rs_unavailable "python on PATH ($RS_PY) did not run a script (Windows Store alias stub?)"
}
[ "$RS_CONTEXT_EXIT" -eq 0 ] || { echo "HARNESS_DENIED: cannot identify the workspace root" >&2; exit 4; }
{
    IFS= read -r _rs_marker
    IFS= read -r RS_TOP
    IFS= read -r RS_CWD
    IFS= read -r RS_TREE_KEY
    IFS= read -r RS_WORKSPACE_MODE
} <<< "$RS_CONTEXT"
RS_TREE_KEY=${HARNESS_TREE_KEY:-$RS_TREE_KEY}
case "$RS_TREE_KEY" in
    ""|*[!A-Za-z0-9_-]*)
        if [ -n "${HARNESS_TREE_KEY:-}" ]; then
            echo "HARNESS_DENIED: bad HARNESS_TREE_KEY" >&2; exit 4
        fi
        rs_unavailable "could not derive a tree key from '$RS_CWD'" ;;
esac
STATE_DIR="${HARNESS_STATE_DIR:-$HOME/.claude/harness-runs}/$RS_TREE_KEY"
mkdir -p "$STATE_DIR" 2>/dev/null || { echo "HARNESS_DENIED: cannot create $STATE_DIR" >&2; exit 4; }

state_file() { echo "$STATE_DIR/state-$1.json"; }
report_file() { echo "$STATE_DIR/report-$1.txt"; }

# state_write <state> <exit-or-empty> <status-text>
# Atomic: unique temp file in the same directory, then mv.
state_write() {
    local st=$1 ex=${2:-} status=${3:-} tmp cwd=${RECORD_CWD:-$RS_CWD}
    case "$cwd" in ""|null) cwd=$RS_CWD ;; esac
    tmp=$(mktemp "$(state_file "$RUN_ID").XXXXXX") || return 1
    "$RS_PY" - "$tmp" "$RUN_ID" "$TOOL" "$st" "${LAUNCHER_PID:-$$}" "${CHILD_PID:-}" \
        "${STARTED:-}" "${STARTED_EPOCH:-}" "$ex" "$status" "$(report_file "$RUN_ID")" \
        "$cwd" "${TIMEOUT:-}" "${LAUNCHER_STIME:-}" "${CHILD_STIME:-}" "${SANDBOX:-}" <<'PY' || { rm -f "$tmp"; return 1; }
import json, sys, time
(tmp, rid, tool, st, lp, cp, started, started_epoch, ex, status, report, cwd, budget, lst, cst, sandbox) = sys.argv[1:17]
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def num(s):
    return int(s) if s.strip().isdigit() else None
rec = {"run_id": rid, "tool": tool, "state": st, "launcher_pid": num(lp), "child_pid": num(cp),
       "launcher_stime": lst or None, "child_stime": cst or None,
       "started": started or now, "started_epoch": num(started_epoch) or int(time.time()),
       "updated": now, "updated_epoch": int(time.time()), "exit": num(ex),
       "status": " ".join(status.split()), "report": report, "cwd": cwd, "budget": num(budget),
       "sandbox": sandbox or None}
json.dump(rec, open(tmp, "w", encoding="utf-8"))
PY
    mv -f "$tmp" "$(state_file "$RUN_ID")"
}

# Hold an OS lock only across check + starting-record publication. The small
# helper releases it on pipe EOF (also when this shell dies); no stale lock
# directory or lock spanning model execution. Same-run -b handoff stays valid.
admit_run() {
    local admission_in admission_out admission_pid reply
    if [ "${SANDBOX:-}" != read-only ]; then
        coproc ADMISSION { "$RS_PY" -c '
import errno, os, sys, time
try:
    with open(sys.argv[1], "a+b") as lock:
        if not lock.tell():
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        deadline = time.monotonic() + (10 if sys.argv[2] == "1" else 0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    print("BUSY", flush=True); sys.exit(0)
                time.sleep(0.05)  # Only a reserved detached handoff waits.
        print("LOCKED", flush=True)
        sys.stdin.read()
except OSError:
    print("ERROR", flush=True)
' "$STATE_DIR/admission.lock" "${HARNESS_RUN_CHILD:-}"; }
        admission_in=${ADMISSION[1]} admission_out=${ADMISSION[0]} admission_pid=$ADMISSION_PID
        IFS= read -r reply <&"$admission_out" || reply=ERROR
        case "${reply%$'\r'}" in
            LOCKED) ;;
            BUSY) echo "HARNESS_BUSY: another launcher is registering in this tree; retry after it reports RUN_ID" >&2; exit 5 ;;
            *) echo "HARNESS_DENIED: cannot lock run admission" >&2; exit 4 ;;
        esac
    fi
    stale_run_check
    state_write starting "" preflight || { echo "HARNESS_DENIED: cannot register this run" >&2; exit 4; }
    if [ -n "${admission_pid:-}" ]; then
        exec {admission_in}>&-
        exec {admission_out}<&-
        wait "$admission_pid" || { echo "HARNESS_DENIED: admission helper failed" >&2; exit 4; }
    fi
}

# read_record <file>: ONE python call loads the record into the REC
# associative array (`null` for null/missing). Returns 1 for junk.
declare -A REC
read_record() {
    local line k v
    REC=()
    while IFS=$'\t' read -r k v; do k=${k%$'\r'}; v=${v%$'\r'}; REC[$k]=$v; done < <("$RS_PY" - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    sys.stdout.reconfigure(newline="\n")
except Exception:
    pass
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    for k in ("run_id", "tool", "state", "launcher_pid", "child_pid", "launcher_stime", "child_stime",
              "started", "started_epoch", "updated", "updated_epoch", "exit", "status", "report", "cwd", "budget",
              "sandbox"):
        v = d.get(k)
        print("%s\t%s" % (k, "null" if v is None else str(v).replace("\t", " ").replace("\n", " ")))
except Exception:
    pass
PY
)
    [ -n "${REC[run_id]:-}" ] && [ "${REC[run_id]}" != null ]
}

pid_alive() { [ -n "$1" ] && [ "$1" != null ] && kill -0 "$1" 2>/dev/null; }
# Process identity for PID-reuse defense: the start time as `ps` reports
# it (MSYS: STIME column; elsewhere: lstart). Recorded when a pid is
# written into a record and compared on classification.
pid_stime() {
    [ -n "$1" ] && [ "$1" != null ] || return 0
    if [ "$RS_IS_WINDOWS" -eq 1 ]; then
        # MSYS ps: STIME is HH:MM:SS for processes started today and a
        # "Mon dd" date afterwards (two tokens) — capture whichever form.
        ps -p "$1" 2>/dev/null | awk 'NR==2 { if ($7 ~ /^[0-9:]+$/) print $7; else print $7 " " $8 }'
    elif [ -r "/proc/$1/stat" ]; then
        # starttime in clock ticks since boot (field 22, after the comm
        # field which may contain spaces): stable, no procps dependency.
        sed 's/^.*) //' "/proc/$1/stat" | awk '{print $20}'
    else
        ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed 's/^ //'
    fi
}
# Command basename of a pid (MSYS prints full Windows paths for native
# processes, so strip both separator kinds).
pid_comm() { ps -p "$1" 2>/dev/null | awk 'NR==2 {print $NF}' | sed -e 's#.*[/\\]##' -e 's/\.exe$//'; }
# pid_is_ours <pid> <recorded-stime>: alive AND the same process. Start
# times are compared when both are known AND in the same form; a form
# change (a run that crossed midnight on MSYS) is undecidable and does
# NOT demote — the guard fails toward "busy". Then the command basename
# must be one of the launcher family, anchored.
pid_is_ours() {
    local pid=$1 rec_st=${2:-} now_st
    pid_alive "$pid" || return 1
    now_st=$(pid_stime "$pid")
    if [ -n "$rec_st" ] && [ "$rec_st" != null ] && [ -n "$now_st" ]; then
        local rec_is_time=0 now_is_time=0
        [[ "$rec_st" =~ ^[0-9]{1,2}:[0-9]{2}:[0-9]{2}$ ]] && rec_is_time=1
        [[ "$now_st" =~ ^[0-9]{1,2}:[0-9]{2}:[0-9]{2}$ ]] && now_is_time=1
        if [ "$rec_is_time" -eq "$now_is_time" ]; then
            [ "$now_st" = "$rec_st" ] || return 1      # same form: must be equal
        fi                                             # different forms: undecidable, keep
    fi
    [[ "$(pid_comm "$pid")" =~ ^(bash|sh|dash|zsh|timeout|codex|agy|node|python[0-9.]*)$ ]]
}
pid_winpid() { ps -p "$1" 2>/dev/null | awk 'NR==2 && $4 ~ /^[0-9]+$/ {print $4}'; }

# Lifecycle: starting (record exists, CLI not yet spawned — written by the
# detaching parent and again by the launcher itself) → running (CLI pid
# known) → done | aborted.
#
# classify_record <state-file>: sets RS_STATE and retains REC for readback.
# A live, identity-matching PID is ALWAYS running — never demoted on age
# (a long ultra run must keep the guard up). No live PID: a young
# `starting` record (parent→child handoff window) is running; anything
# else is promoted to `aborted` and persisted with the record's own
# tool/cwd. A live PID that no longer looks like ours is treated as dead
# (PID reuse) — `--forget` exists for the cases this cannot decide.
classify_record() {
    RS_STATE=
    local f=$1 age
    read_record "$f" || return 0
    case "${REC[state]}" in running|starting) ;; *) RS_STATE=${REC[state]}; return ;; esac
    if pid_is_ours "${REC[launcher_pid]}" "${REC[launcher_stime]}" || pid_is_ours "${REC[child_pid]}" "${REC[child_stime]}"; then
        RS_STATE=running; return
    fi
    age=$(( $(date +%s) - $(printf '%s' "${REC[started_epoch]}" | grep -E '^[0-9]+$' || echo 0) ))
    if [ "${REC[state]}" = starting ] && [ "$age" -lt 60 ]; then
        RS_STATE=running; return
    fi
    # The PID looks dead — but a run finishing at the instant we sample it
    # writes `done` a moment AFTER its PID exits, so a single observation
    # races that write. Confirm once after a short pause: if the record has
    # since reached a final state (or a live PID reappeared), defer to it
    # rather than clobbering a clean `done` with `aborted`. (Race
    # reproduced by scripts/test_launchers.sh — detached run + concurrent
    # --wait.)
    sleep 1
    read_record "$f" || return 0
    case "${REC[state]}" in running|starting) ;; *) RS_STATE=${REC[state]}; return ;; esac
    if pid_is_ours "${REC[launcher_pid]}" "${REC[launcher_stime]}" || pid_is_ours "${REC[child_pid]}" "${REC[child_stime]}"; then
        RS_STATE=running; return
    fi
    ( RUN_ID=${REC[run_id]} TOOL=${REC[tool]} LAUNCHER_PID=${REC[launcher_pid]} CHILD_PID=${REC[child_pid]} \
      LAUNCHER_STIME=${REC[launcher_stime]} CHILD_STIME=${REC[child_stime]} \
      STARTED=${REC[started]} STARTED_EPOCH=${REC[started_epoch]} RECORD_CWD=${REC[cwd]} TIMEOUT=${REC[budget]} SANDBOX=${REC[sandbox]} \
      state_write aborted 1 "launcher died before writing a final state (classified at age ${age}s)" ) || { RS_STATE=running; return; }
    read_record "$f" || return 0
    RS_STATE=${REC[state]}
}

# Refuse to start while ANOTHER run — codex or agy alike — is still
# executing in THIS tree (two file-writing delegates on one tree is the
# accident delegate-output-trust §2 forbids; the tool does not matter).
# The one exemption is a read-only codex sandbox on
# either side: an advisory run writes nothing and reads only its inline
# prompt. Exit 5 = HARNESS_BUSY: the right action is --wait, not fixing
# the call. Dead leftovers are marked aborted and reported, not fatal.
STALE_CLEANED=
stale_run_check() {
    local f st scan kind value
    [ "${SANDBOX:-}" = read-only ] && return 0
    # Read terminal history once. Only potentially live records require the
    # existing PID/race checks; they are re-read before any decision or write.
    scan=$("$RS_PY" - "$STATE_DIR" "${RUN_ID:-}" <<'PYSCAN'
import json
from pathlib import Path
import sys
sys.stdout.reconfigure(newline="\n")
for path in sorted(Path(sys.argv[1]).glob("state-*.json")):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        rid = record.get("run_id")
        if not rid or rid == sys.argv[2] or record.get("sandbox") == "read-only":
            continue
        state = record.get("state")
        if state in ("starting", "running"):
            print("pending\t" + path.name)
        elif state == "aborted" and str(record.get("status", "")).startswith("launcher died before writing a final state"):
            print("cleaned\t" + str(rid).replace("\t", " ").replace("\n", " "))
    except (OSError, ValueError, AttributeError):
        continue  # Same unreadable-record handling as read_record.
PYSCAN
    ) || { echo "HARNESS_DENIED: cannot inspect run records" >&2; exit 4; }
    while IFS=$'\t' read -r kind value; do
        [ -n "$kind" ] || continue
        if [ "$kind" = cleaned ]; then
            STALE_CLEANED="$STALE_CLEANED${STALE_CLEANED:+,}$value"
            continue
        fi
        f="$STATE_DIR/$value"
        [ -f "$f" ] || continue
        read_record "$f" || continue
        [ "${REC[run_id]}" = "${RUN_ID:-}" ] && continue     # the record handed to us by -b
        [ "${REC[sandbox]}" = read-only ] && continue
        classify_record "$f"; st=$RS_STATE
        case "$st" in
            running)
                echo "HARNESS_BUSY: STALE_RUN ${REC[run_id]} is still executing in this tree (launcher pid ${REC[launcher_pid]}, child pid ${REC[child_pid]}) — bash $0 --wait ${REC[run_id]} before starting another delegation (or --forget ${REC[run_id]} if the pid was reused by an unrelated process)" >&2
                exit 5 ;;
            aborted)
                read_record "$f"
                case "${REC[status]}" in
                    "launcher died before writing a final state"*) STALE_CLEANED="$STALE_CLEANED${STALE_CLEANED:+,}${REC[run_id]}" ;;
                esac ;;
        esac
    done <<< "$scan"
}

# Shared argument/exit contract for the three launchers; no worker call.
launcher_readback() {
    local MODE RID WAIT_BUDGET opt OPTIND=1
    if [ "${1:-}" = "--status" ] || [ "${1:-}" = "--wait" ] || [ "${1:-}" = "--forget" ]; then
        MODE=$1; RID=${2:-}; shift 2 2>/dev/null || usage
        case "$RID" in ""|*[!A-Za-z0-9TZ_-]*) usage ;; esac
        WAIT_BUDGET=570
        while getopts ":t:" opt; do
            case "$opt" in
                t) WAIT_BUDGET=$OPTARG ;;
                *) usage ;;
            esac
        done
        case "$WAIT_BUDGET" in ""|*[!0-9]*|0*) usage ;; esac
        case "$MODE" in
            --status) cmd_status "$RID" ;;
            --wait) cmd_wait "$RID" "$WAIT_BUDGET" ;;
            --forget) cmd_forget "$RID" ;;
        esac
        exit $?
    fi
}

# --status <RUN_ID>: print STATE and, when finished, the saved report.
# Exit: the run's recorded exit when done/aborted, 6 while running,
# 7 when no such record exists (distinct from the CLI-unavailable code 2).
cmd_status() {
    local rid=$1 f
    f=$(state_file "$rid")
    if [ ! -f "$f" ]; then echo "STATE: unknown (no $f)"; return 7; fi
    classify_record "$f"
    print_status "$rid"
}

print_status() {
    local rid=$1 st=$RS_STATE ex
    echo "STATE: ${st:-corrupt} (run $rid, launcher pid ${REC[launcher_pid]:-?}, child pid ${REC[child_pid]:-?}, started ${REC[started]:-?}, updated ${REC[updated]:-?})"
    case "$st" in
        done|aborted)
            if [ -s "$(report_file "$rid")" ]; then cat "$(report_file "$rid")"; else echo "REPORT: (none written — ${REC[status]:-?})"; fi
            ex=${REC[exit]:-1}; case "$ex" in ''|null|*[!0-9]*) ex=1 ;; esac
            return "$ex" ;;
        running) return 6 ;;
        *) echo "REPORT: record unreadable"; return 7 ;;
    esac
}

# --wait <RUN_ID> [-t N]: poll (5 s, backing off to 15 s after a minute)
# N wall-clock seconds including record/PID checks (capped at 570).
# One final classification, including its exit-race check, may extend the deadline;
# exit = the run's recorded exit, 6 while still running (call --wait
# again), 7 for no record.
cmd_wait() {
    local rid=$1 budget=${2:-570} waited=0 f st step remaining
    local started=$SECONDS deadline
    case "$budget" in ''|*[!0-9]*) budget=570 ;; esac
    [ "$budget" -gt 570 ] && budget=570
    deadline=$((started + budget))
    f=$(state_file "$rid")
    if [ ! -f "$f" ]; then echo "STATE: unknown (no $f)"; return 7; fi
    while :; do
        classify_record "$f"; st=$RS_STATE
        case "$st" in
            running) ;;
            *) print_status "$rid"; return $? ;;
        esac
        waited=$((SECONDS - started))
        remaining=$((deadline - SECONDS))
        if [ "$remaining" -le 0 ]; then
            echo "STATE: running (waited ${waited}s; run $rid, child pid ${REC[child_pid]:-?}) — call --wait $rid again"
            return 6
        fi
        step=5
        [ "$waited" -ge 60 ] && step=15
        [ "$step" -le "$remaining" ] || step=$remaining
        sleep "$step"
    done
}

# --forget <RUN_ID>: orchestrator-only manual override for a record whose
# PID was reused by an unrelated process (the hook denies this from
# subagents). Marks it aborted; it never touches a process.
cmd_forget() {
    local rid=$1 f
    # Gate on an env marker rather than on the command spelling: the hook
    # denies `HARNESS_ALLOW_FORGET=` from subagents, and an assignment is a
    # far narrower surface than every way of naming this script.
    if [ "${HARNESS_ALLOW_FORGET:-}" != "1" ]; then
        echo "HARNESS_DENIED: --forget releases the busy guard; run it as HARNESS_ALLOW_FORGET=1 bash $0 --forget $rid (orchestrator only, after confirming no delegate is running)" >&2
        return 4
    fi
    f=$(state_file "$rid")
    if [ ! -f "$f" ]; then echo "STATE: unknown (no $f)"; return 7; fi
    read_record "$f" || { echo "STATE: corrupt record"; return 7; }
    ( RUN_ID=$rid TOOL=${REC[tool]} LAUNCHER_PID=${REC[launcher_pid]} CHILD_PID=${REC[child_pid]} \
      STARTED=${REC[started]} STARTED_EPOCH=${REC[started_epoch]} RECORD_CWD=${REC[cwd]} TIMEOUT=${REC[budget]} SANDBOX=${REC[sandbox]} \
      state_write aborted 1 "marked aborted by --forget (orchestrator override)" )
    echo "STATE: aborted (run $rid forgotten by --forget; any live process was NOT touched)"
    return 0
}

# terminate_child_tree: TERM the child's process group, wait up to 15 s,
# KILL, and on Windows finish with taskkill /T on the Windows pid so
# native descendants the MSYS process table cannot see are covered.
terminate_child_tree() {
    local n=0 winpid
    pid_alive "$CHILD_PID" || return 0
    winpid=$(pid_winpid "$CHILD_PID")
    CHILD_TREE_NOTE="direct child dead; descendants signalled via process group"
    # Windows: the graceful tree signal goes out NOW, while the child is
    # provably ours; the forced sweep below runs only if it is still alive
    # (a dead-and-recycled winpid must never be force-killed).
    if [ "$RS_IS_WINDOWS" -eq 1 ] && [ -n "$winpid" ]; then
        taskkill //T //PID "$winpid" >/dev/null 2>&1
        CHILD_TREE_NOTE="direct child dead; Windows descendants signalled with taskkill /T (winpid $winpid)"
    fi
    kill -TERM -- "-$CHILD_PID" 2>/dev/null || kill -TERM "$CHILD_PID" 2>/dev/null
    while pid_alive "$CHILD_PID" && [ "$n" -lt 15 ]; do sleep 1; n=$((n + 1)); done
    if pid_alive "$CHILD_PID"; then
        if [ "$RS_IS_WINDOWS" -eq 1 ] && [ -n "$winpid" ]; then
            taskkill //T //F //PID "$winpid" >/dev/null 2>&1
            CHILD_TREE_NOTE="direct child needed taskkill /T /F (winpid $winpid)"
        fi
        kill -KILL -- "-$CHILD_PID" 2>/dev/null || kill -KILL "$CHILD_PID" 2>/dev/null
    fi
    wait "$CHILD_PID" 2>/dev/null
}
