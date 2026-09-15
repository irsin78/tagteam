#!/usr/bin/env bash
# Shared launcher evidence. App-specific options and result decoding stay in adapters.
CONTROL_PLANE_RE='^(docs/orchestration/(delegation-matrix|retry-policy)\.md$|\.claude/(hooks|scripts|rules|agents|skills|commands)/|\.claude/settings\.json$|\.claude/sandbox-sensitive\.json$|\.claude/model-bindings(\.local)?\.json$|\.codex/(hooks(\.json$|/)|config\.toml$|AGENTS(\.override)?\.md$)|\.mcp\.json$|CLAUDE\.md$|AGENTS(\.override)?\.md$|check-windows-aliases\.ps1$|check-posix\.sh$)'
CONTROL_PLANE_NOTICE_RE='^(\.claude/settings\.local\.json$|\.claude/\.stop-gate$|\.claude/\.preflight-status$)'

control_before() {
    CP_HASH="$SCRIPT_DIR/control-plane-hash.sh"
    CP_SNAP_OK=0
    if [ -f "$CP_HASH" ] && bash "$CP_HASH" > "$CP_BEFORE" 2>/dev/null && [ -s "$CP_BEFORE" ]; then
        CP_SNAP_OK=1
    else
        echo "HARNESS_DENIED: cannot snapshot the starting control plane" >&2
        exit 4
    fi
}

control_after() {
    local result key value
    CP_HITS=; CP_NOTICE=; CP_EVIDENCE_OK=1
    if [ "$CP_SNAP_OK" -ne 1 ] || ! result=$(bash "$CP_HASH" --compare "$CP_BEFORE" "$CP_AFTER" "$CHANGED_FILE" "$CONTROL_PLANE_RE" "$CONTROL_PLANE_NOTICE_RE" 2>/dev/null) ||
       [[ "$result" != hits$'\t'*$'\n'notice$'\t'* ]]; then
        CP_EVIDENCE_OK=0
        return
    fi
    while IFS=$'\t' read -r key value; do
        case "$key" in hits) CP_HITS=${value%$'\r'} ;; notice) CP_NOTICE=${value%$'\r'} ;; esac
    done <<< "$result"
}

# Optional recovery reference for tracked dirty files; change detection always
# uses workspace evidence. This diff is not a backup of untracked/ignored files.
save_git_baseline() {
    [ "${HARNESS_SAVE_BASELINE:-}" = 1 ] && [ -n "$HEAD" ] || return 0
    git diff HEAD > "$LOG_DIR/baseline-$TIMESTAMP.diff" || {
        echo "HARNESS_DENIED: cannot save the requested Git baseline" >&2; exit 4;
    }
}

launcher_timeout() {
    RUNNER=(); TIMEOUT_WRAPPER=none
    if timeout --version 2>/dev/null | grep -qi coreutils; then
        RUNNER=(timeout -k 10 "$TIMEOUT")
        TIMEOUT_WRAPPER="gnu timeout ${TIMEOUT}s"
    fi
}

# Local wall-clock phases; Bash 4 falls back to whole-second resolution.
# No timer subprocess per mark. CLI time includes the worker's tools/hooks,
# while verifier time is only the launcher's explicitly attached -v script.
timing_clock() {
    local value=${1:-${EPOCHREALTIME:-$SECONDS}} seconds fraction
    value=${value/,/.}  # EPOCHREALTIME may use the locale decimal separator.
    seconds=${value%%.*}
    fraction=0
    if [[ "$value" == *.* ]]; then fraction=${value#*.}000; fraction=${fraction:0:3}; fi
    TIMING_NOW_MS=$((10#$seconds * 1000 + 10#$fraction))
}

timing_init() {
    timing_clock "$1"
    TIMING_LAST_MS=$TIMING_NOW_MS
    TIMING_PHASE=preflight
    TIMING_PREFLIGHT_MS=0; TIMING_CLI_MS=0; TIMING_POSTFLIGHT_MS=0; TIMING_VERIFY_MS=0
    TIMING_ATTEMPTS=0
    TIMING_RESOLUTION=seconds
    [[ "$1" != *[.,]* ]] || TIMING_RESOLUTION=ms
}

timing_enter() {
    local delta
    timing_clock
    delta=$((TIMING_NOW_MS - TIMING_LAST_MS))
    [ "$delta" -ge 0 ] || delta=0
    case "$TIMING_PHASE" in
        preflight) TIMING_PREFLIGHT_MS=$((TIMING_PREFLIGHT_MS + delta)) ;;
        cli) TIMING_CLI_MS=$((TIMING_CLI_MS + delta)) ;;
        postflight) TIMING_POSTFLIGHT_MS=$((TIMING_POSTFLIGHT_MS + delta)) ;;
        verify) TIMING_VERIFY_MS=$((TIMING_VERIFY_MS + delta)) ;;
    esac
    TIMING_LAST_MS=$TIMING_NOW_MS
    TIMING_PHASE=$1
    [ "$1" != cli ] || TIMING_ATTEMPTS=$((TIMING_ATTEMPTS + 1))
    return 0
}

timing_report() {
    timing_enter finished
    local total=$((TIMING_PREFLIGHT_MS + TIMING_CLI_MS + TIMING_POSTFLIGHT_MS + TIMING_VERIFY_MS))
    echo "ELAPSED: $((total / 1000))s"
    echo "TIMING: preflight_ms=$TIMING_PREFLIGHT_MS cli_ms=$TIMING_CLI_MS postflight_ms=$TIMING_POSTFLIGHT_MS verify_ms=$TIMING_VERIFY_MS total_ms=$total attempts=$TIMING_ATTEMPTS resolution=$TIMING_RESOLUTION"
}
