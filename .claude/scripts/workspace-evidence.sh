#!/usr/bin/env bash
# Shared before/after evidence; callers own temp files and cleanup.
WORKSPACE_SNAPSHOT="$SCRIPT_DIR/workspace-snapshot.py"
PY=${PY:-${RS_PY:-}}

workspace_before() {
    local before=$1 metadata
    shift
    if ! metadata=$("$PY" "$WORKSPACE_SNAPSHOT" --require-root --save "$before" "$@"); then
        echo "HARNESS_DENIED: cannot snapshot the starting workspace" >&2
        exit 4
    fi
    metadata=${metadata//$'\r'/}
    IFS= read -r WORKSPACE_MODE <<< "$metadata"
    [ "$WORKSPACE_MODE" = git ] || [ "$WORKSPACE_MODE" = files ] || exit 4
    WORKSPACE_DESCRIPTION=${metadata#*$'\n'}
}

workspace_after() {
    local before=$1 after=$2 changed=$3
    shift 3
    WORKSPACE_OK=1
    if ! "$PY" "$WORKSPACE_SNAPSHOT" --require-root --save "$after" --since "$before" "$@" > "$changed"; then
        WORKSPACE_OK=0
        : > "$changed"
        CHANGED=unknown
        echo "WORKSPACE_WARNING: cannot compare workspace contents" >&2
    elif [ -s "$changed" ]; then
        CHANGED=$(tr -d '\r' < "$changed" | paste -sd, -)
    else
        CHANGED=none
    fi
}
