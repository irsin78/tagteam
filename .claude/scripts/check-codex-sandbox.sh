#!/usr/bin/env bash
# Optional live Codex shell diagnostic. This spends one model call.
# Run after install/CLI or shell changes, or when an actual execution fails.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -n "$PY" ] || { echo "Python is required" >&2; exit 2; }
CONTEXT=$("$PY" "$SCRIPT_DIR/workspace-snapshot.py" --context) || exit 2
CONTEXT=${CONTEXT//$'\r'/}
WORKSPACE_MODE=${CONTEXT##*$'\n'}
case "$WORKSPACE_MODE" in git|files) ;; *) echo "Workspace mode unavailable" >&2; exit 2 ;; esac
CODEX_GIT_ARGS=()
[ "$WORKSPACE_MODE" != files ] || CODEX_GIT_ARGS+=(--skip-git-repo-check)
codex exec "${CODEX_GIT_ARGS[@]}" -c windows.sandbox=unelevated -c model_reasoning_effort=low -m gpt-5.6-terra --sandbox workspace-write "Run this shell command and report its output: echo SANDBOX_OK" </dev/null
