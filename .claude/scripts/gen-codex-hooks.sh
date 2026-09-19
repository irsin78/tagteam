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

# Windows paths: the hook command runs through pwsh, where a command that
# STARTS with a quoted string is a parse error (measured 2026-09-04). Emit
# forward slashes and no leading quote; a path with spaces gets the call
# operator so pwsh still executes it.
to_native() {
    printf '%s' "$1" | sed -e 's|^/\([a-zA-Z]\)/|\1:/|' -e 's|\\|/|g'
}

PY_NATIVE=$(to_native "$PY")
ROOT_NATIVE=$(to_native "$ROOT")

# Quoting is only needed when a path contains a space, and it is exactly
# what breaks pwsh (a command starting with a quoted string is a parse
# error there, so it needs the call operator `&`, which in turn is invalid
# in a POSIX shell). Unquoted whenever possible keeps ONE string valid on
# both; when a space forces quotes, `commandWindows` carries the pwsh form.
NEEDS_QUOTES=0
case "$PY_NATIVE$ROOT_NATIVE" in
    *\ *) NEEDS_QUOTES=1 ;;
esac

emit_command() {   # $1 = hook file, $2 = "posix" | "windows"
    if [ "$NEEDS_QUOTES" -eq 0 ]; then
        printf '%s %s/.claude/hooks/%s --host codex' "$PY_NATIVE" "$ROOT_NATIVE" "$1"
    elif [ "$2" = "windows" ]; then
        printf '& \\"%s\\" \\"%s/.claude/hooks/%s\\" --host codex' \
            "$PY_NATIVE" "$ROOT_NATIVE" "$1"
    else
        printf '\\"%s\\" \\"%s/.claude/hooks/%s\\" --host codex' \
            "$PY_NATIVE" "$ROOT_NATIVE" "$1"
    fi
}

emit_windows_line() {   # $1 = hook file, $2 = optional fixed flag
    [ "$NEEDS_QUOTES" -eq 1 ] || return 0
    printf '            "commandWindows": "%s%s",\n' "$(emit_command "$1" windows)" "${2:+ $2}"
}

{
    printf '{\n  "hooks": {\n'
    printf '    "SessionStart": [\n      {\n        "hooks": [\n          {\n'
    printf '            "type": "command",\n'
    printf '            "command": "%s",\n' "$(emit_command session_preflight.py posix)"
    emit_windows_line session_preflight.py
    printf '            "timeout": 30,\n            "statusMessage": "harness preflight"\n'
    printf '          }\n        ]\n      }\n    ],\n'
    printf '    "PreToolUse": [\n      {\n        "matcher": "Bash|PowerShell|exec_command|shell_command|apply_patch|Write|Edit|MultiEdit|NotebookEdit",\n'
    printf '        "hooks": [\n          {\n            "type": "command",\n'
    printf '            "command": "%s",\n' "$(emit_command deny_dangerous.py posix)"
    emit_windows_line deny_dangerous.py
    printf '            "timeout": 30,\n            "statusMessage": "harness guard"\n'
    printf '          }\n        ]\n      }\n    ],\n'
    printf '    "UserPromptSubmit": [\n      {\n        "hooks": [\n          {\n'
    printf '            "type": "command",\n'
    printf '            "command": "%s --new-prompt",\n' "$(emit_command stop_gate.py posix)"
    emit_windows_line stop_gate.py --new-prompt
    printf '            "timeout": 10,\n            "statusMessage": "harness mission scope"\n'
    printf '          }\n        ]\n      }\n    ],\n'
    printf '    "Stop": [\n      {\n        "hooks": [\n          {\n'
    printf '            "type": "command",\n'
    printf '            "command": "%s",\n' "$(emit_command stop_gate.py posix)"
    emit_windows_line stop_gate.py
    printf '            "timeout": 180,\n            "statusMessage": "harness stop gate"\n'
    printf '          }\n        ]\n      }\n    ]\n'
    printf '  }\n}\n'
} > "$OUT" || exit 2

echo "gen-codex-hooks: wrote $OUT"
echo "gen-codex-hooks: trust it once with /hooks in an interactive codex session at $ROOT_NATIVE"
