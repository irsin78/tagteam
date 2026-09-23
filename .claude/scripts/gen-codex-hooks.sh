#!/usr/bin/env bash
# Write a Codex hooks definition with ABSOLUTE paths.
#
# The distributed template uses repo-relative commands. Run this generator
# during installation to bind hooks to the target interpreter and project.
# Regenerate after moving the project or changing the Python installation.
# Generated definitions are machine-local; do not distribute their paths.
#
# Codex records hook trust against the ABSOLUTE PATH of the hooks file plus
# the definition's hash, so a generated file must be trusted again (`/hooks`
# in the codex TUI) and re-trusted after every edit -- an untrusted or
# stale definition is skipped SILENTLY (docs/harness-manual.md, install
# section, Codex host).
#
# Usage:
#   bash .claude/scripts/gen-codex-hooks.sh [--out <path>] [--python <exe>] [--force]
#
# Defaults: --out <repo>/.codex/hooks.json, --python a working Python 3
# (tries python3, then python). Refuses to overwrite an existing file without
# --force. bash 3.2 compatible (macOS) -- no associative arrays, no mapfile.
set -u

OUT=""
PY=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            [ $# -ge 2 ] || { echo "gen-codex-hooks: --out needs a path" >&2; exit 2; }
            OUT=$2; shift 2 ;;
        --out=*) OUT=${1#--out=}; shift ;;
        --python)
            [ $# -ge 2 ] || { echo "gen-codex-hooks: --python needs a path" >&2; exit 2; }
            PY=$2; shift 2 ;;
        --python=*) PY=${1#--python=}; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "gen-codex-hooks: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

if [ -z "$PY" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info.major != 3)' >/dev/null 2>&1; then
            PY=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "gen-codex-hooks: no working Python 3 on PATH -- pass --python <exe>" >&2
        exit 2
    fi
fi
# Resolve aliases/shims to the actual interpreter and reject Python 2 or
# non-working Store aliases before creating or overwriting any definition.
PY=$("$PY" -c 'import sys; sys.exit("Python 3 is required") if sys.version_info.major != 3 else None; print(sys.executable)') || exit 2
PY=${PY%$'\r'}
[ -n "$PY" ] || { echo "gen-codex-hooks: empty Python executable" >&2; exit 2; }

# Reuse the launchers' Git/non-Git root discovery; do not scan contents.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$("$PY" "$SCRIPT_DIR/workspace-snapshot.py" --root) || exit 2
ROOT=${ROOT%$'\r'}
[ -n "$ROOT" ] || { echo "gen-codex-hooks: Python did not return a workspace root" >&2; exit 2; }
cd "$ROOT" || exit 2

[ -n "$OUT" ] || OUT="$ROOT/.codex/hooks.json"

if [ -e "$OUT" ] && [ "$FORCE" -ne 1 ]; then
    echo "gen-codex-hooks: $OUT exists -- pass --force to overwrite" >&2
    exit 2
fi

OUT_DIR=$(dirname "$OUT")
[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR" || exit 2

# Serialize JSON separately from shell quoting. POSIX and PowerShell use
# different quoting rules; never infer safety only from the presence of spaces.
"$PY" - "$PY" "$ROOT" "$OUT" <<'PY'
import json
import shlex
import sys
from pathlib import Path

python, root, output = (value.replace('\\', '/') for value in sys.argv[1:])
def windows_quote(value):
    return "'" + value.replace("'", "''") + "'"

hooks = {}
for event, script, timeout, message, flags in (
    ('SessionStart', 'session_preflight.py', 30, 'harness preflight', []),
    ('PreToolUse', 'deny_dangerous.py', 30, 'harness guard', []),
    ('UserPromptSubmit', 'stop_gate.py', 10, 'harness mission scope', ['--new-prompt']),
    ('Stop', 'stop_gate.py', 180, 'harness stop gate', []),
):
    argv = [python, root + '/.claude/hooks/' + script, '--host', 'codex', *flags]
    entry = {'hooks': [{'type': 'command', 'command': shlex.join(argv),
                       'commandWindows': '& ' + ' '.join(windows_quote(arg) for arg in argv),
                       'timeout': timeout, 'statusMessage': message}]}
    if event == 'PreToolUse':
        entry['matcher'] = 'Bash|PowerShell|exec_command|shell_command|apply_patch|Write|Edit|MultiEdit|NotebookEdit'
    hooks[event] = [entry]
Path(output).write_text(json.dumps({'hooks': hooks}, indent=2) + '\n', encoding='utf-8')
PY
[ "$?" -eq 0 ] || exit 2

echo "gen-codex-hooks: wrote $OUT"
echo "gen-codex-hooks: trust it once with /hooks in an interactive codex session at $ROOT"
