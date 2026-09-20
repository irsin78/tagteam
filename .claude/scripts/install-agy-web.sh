#!/usr/bin/env bash
# Install the agy web lane's global pieces.
#
# agy 1.2.7 does not activate workspace agents or hooks in headless runs, so
# the checked-in definitions have to be copied under ~/.gemini/config. The
# launcher then refuses to start unless the installed copies byte-match the
# checked-in ones.
#
# The hook REGISTRATION is merged, never copied over. That file is shared:
# other tools register their own hooks there, and overwriting it silently
# unregisters them (observed — a terminal manager's integration was lost this
# way). Only our own key is touched.
set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
GLOBAL_DIR=${AGY_CONFIG_DIR:-$HOME/.gemini/config}
AGENT_SRC="$REPO_ROOT/.agents/agents/agy-summarizer.md"
GUARD_SRC="$REPO_ROOT/.agents/hooks/agy_web_no_tools.py"
HOOK_SRC="$REPO_ROOT/.agents/hooks.json"
HOOK_KEY=agy-web-no-tools

PY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null) || true
[ -n "$PY" ] || { echo "install-agy-web: python3 not found" >&2; exit 2; }
for f in "$AGENT_SRC" "$GUARD_SRC" "$HOOK_SRC"; do
    [ -f "$f" ] || { echo "install-agy-web: missing $f" >&2; exit 2; }
done

mkdir -p "$GLOBAL_DIR/agents" "$GLOBAL_DIR/hooks"
cp "$AGENT_SRC" "$GLOBAL_DIR/agents/agy-summarizer.md"
cp "$GUARD_SRC" "$GLOBAL_DIR/hooks/agy_web_no_tools.py"

"$PY" - "$HOOK_SRC" "$GLOBAL_DIR/hooks.json" "$HOOK_KEY" <<'PY'
import json, sys
from pathlib import Path
source_path, target_path, key = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
entry = json.loads(Path(source_path).read_text(encoding="utf-8"))[key]
existing = {}
if target_path.is_file():
    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"install-agy-web: {target_path} is not valid JSON; refusing to "
              f"replace it. Fix or move it, then rerun.", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(existing, dict):
        print(f"install-agy-web: {target_path} is not a JSON object; refusing "
              f"to replace it.", file=sys.stderr)
        raise SystemExit(2)
# The predecessor key points at a guard file this script deletes below. Left
# behind it would make every agy session on the machine run a missing command.
stale = existing.pop("agy-fetch-cache-only", None)
kept = sorted(k for k in existing if k != key)
existing[key] = entry
target_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
if stale is not None:
    print("removed the superseded agy-fetch-cache-only registration")
print(f"registered {key}; left untouched: {', '.join(kept) if kept else '(none)'}")
PY

# Remove the predecessor, whose agent carried a file-read tool.
rm -f "$GLOBAL_DIR/agents/agy-fetcher.md" "$GLOBAL_DIR/hooks/agy_fetch_view_guard.py"

echo "installed agy-summarizer and agy_web_no_tools into $GLOBAL_DIR"
