#!/usr/bin/env bash
# Optional local-reader entry point. -i is a file manifest; -c is unsupported.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -n "$PY" ] || { echo "LOCAL_UNAVAILABLE: Python is required" >&2; exit 2; }
exec "$PY" "$SCRIPT_DIR/local-read.py" "$@"
