#!/usr/bin/env bash
# macOS / Linux install preflight for this harness -- the POSIX twin of
# check-windows-aliases.ps1. Read-only. Deliberately bash 3.2 compatible
# (macOS ships 3.2): it must be able to tell you that the launchers need
# bash >= 4 BEFORE you have installed one.
#
# Usage: bash check-posix.sh [--launchers] [--template-dir <path>]
#   --launchers         also run .claude/scripts/test_launchers.sh (duration depends on selected groups and environment;
#                       by default only its presence is checked)
#   --template-dir DIR  compare this project's harness files with the
#                       tagteam template checkout (or set
#                       HARNESS_TEMPLATE_DIR); every divergence is listed
#                       as DRIFT (INFO -- drift can be deliberate, so it
#                       never fails the preflight)
#
# Output lines start with OK / INFO / WARN / DRIFT. Only WARN counts toward
# the exit code: `SUMMARY: N warning(s).`, exit 1 when N > 0. The
# authoritative statement of each finding and its fix is
# docs/harness-manual.md (install section) and docs/platform-notes-*.md.
set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TEMPLATE_DIR=${HARNESS_TEMPLATE_DIR:-}
RUN_LAUNCHERS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --launchers) RUN_LAUNCHERS=1 ;;
        --template-dir) shift; [ $# -gt 0 ] || { echo "WARN: --template-dir needs a path (see --help)"; exit 1; }; TEMPLATE_DIR=$1 ;;
        --template-dir=*) TEMPLATE_DIR=${1#--template-dir=} ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "WARN: unknown option '$1' (see --help)"; exit 1 ;;
    esac
    shift
done

WARNINGS=0
warn() { WARNINGS=$((WARNINGS + 1)); echo "WARN: $1"; }
note() { echo "      $1"; }

# ---- 1. platform line
UNAME=$(uname -s 2>/dev/null || echo unknown)
IS_WSL=0
if [ "$UNAME" = "Linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then IS_WSL=1; fi
case "$UNAME" in
    Darwin) echo "INFO: platform macOS ($(sw_vers -productVersion 2>/dev/null || uname -r)) -- operational notes: docs/platform-notes-macos.md" ;;
    Linux)
        if [ "$IS_WSL" -eq 1 ]; then
            echo "INFO: platform Linux (WSL2) -- operational notes: docs/platform-notes-linux.md + the manual's WSL2 isolation-lane sections"
        else
            echo "INFO: platform Linux ($(uname -r)) -- operational notes: docs/platform-notes-linux.md (native Linux: platform facts and harness behaviour measured on 24.04 aarch64; isolation lane unproven)"
        fi ;;
    MINGW*|MSYS*|CYGWIN*) echo "INFO: platform Windows (Git Bash, $UNAME) -- the Windows preflight is check-windows-aliases.ps1 (Store aliases, pwsh); this script only covers the POSIX layer the launchers run in" ;;
    *) echo "INFO: platform $UNAME -- not a platform this harness has notes for" ;;
esac

# ---- 2. bash on PATH (the launchers and run-state.sh need >= 4)
BASH_ON_PATH=$(command -v bash 2>/dev/null || true)
if [ -z "$BASH_ON_PATH" ]; then
    warn "bash was not found on PATH."
    note "Every launcher (codex-run.sh, agy-run.sh) and the Stop-hook gate run through bash."
else
    BASH_MAJOR=$("$BASH_ON_PATH" -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null || echo 0)
    if [ "${BASH_MAJOR:-0}" -lt 4 ]; then
        warn "bash on PATH is $("$BASH_ON_PATH" -c 'echo "$BASH_VERSION"' 2>/dev/null) at $BASH_ON_PATH -- the launchers need bash >= 4."
        note "agy-run.sh and run-state.sh refuse with *_UNAVAILABLE (exit 2) under bash 3, so every delegation falls back."
        note "Fix (macOS): brew install bash, then put \$(brew --prefix)/bin ahead of /bin on PATH."
    else
        echo "OK: bash on PATH is $("$BASH_ON_PATH" -c 'echo "$BASH_VERSION"') ($BASH_ON_PATH)."
    fi
fi

# ---- 3. python: the hooks are invoked as `python` (settings.json)
PY=$(command -v python 2>/dev/null || true)
PY_OK=0
if [ -n "$PY" ] && "$PY" -c 'import sys' >/dev/null 2>&1; then PY_OK=1; fi
if [ "$PY_OK" -eq 1 ]; then
    echo "OK: python resolves and runs ($PY, $("$PY" -c 'import platform; print(platform.python_version())' 2>/dev/null))."
else
    if [ -n "$PY" ]; then
        warn "python resolves to $PY but does not run a script."
    else
        warn "python was not found on PATH."
    fi
    note "Every hook in .claude/settings.json is invoked as 'python'; without it each harness"
    note "prohibition (bypass flags, force push, subagent commits) is silently inert while"
    note "this check would otherwise look green."
    note "Fix (Ubuntu/Debian): sudo apt install python-is-python3"
    note "Fix (macOS):         brew install python, then add \$(brew --prefix)/opt/python/libexec/bin to PATH"
    note "                     (it provides the unversioned 'python')."
fi
PY3=$(command -v python3 2>/dev/null || true)
if [ -n "$PY3" ]; then
    echo "INFO: python3 resolves to $PY3."
else
    echo "INFO: python3 was not found on PATH."
fi

# ---- 4. git
if command -v git >/dev/null 2>&1; then
    echo "OK: git resolves to $(command -v git) ($(git --version 2>/dev/null | head -n 1))."
else
    warn "git was not found on PATH -- delegation preflight/postflight, the control-plane hash and the SubagentStop evidence hook cannot verify anything."
fi

# ---- 5. GNU coreutils timeout (launcher wrapper; absence is not fatal)
if timeout --version 2>/dev/null | grep -qi coreutils; then
    echo "OK: GNU coreutils timeout is first on PATH ($(command -v timeout))."
else
    echo "INFO: GNU coreutils 'timeout' is not first on PATH -- the launchers run without the wrapper (TIMEOUT_WRAPPER: none), so only the Bash tool's 600 s cap cuts a hung run."
    note "Fix (macOS): brew install coreutils, then add \$(brew --prefix)/opt/coreutils/libexec/gnubin to PATH."
fi

# ---- 6. hook liveness: a green environment check proves nothing if a hook never runs
for hook in deny_dangerous.py stop_gate.py; do
    hook_path="$SCRIPT_DIR/.claude/hooks/$hook"
    if [ ! -f "$hook_path" ]; then
        warn "Hook $hook not found at $hook_path."
        continue
    fi
    if [ "$PY_OK" -ne 1 ]; then
        warn "Hook $hook self-test skipped: no working python (see above)."
        continue
    fi
    # A SyntaxWarning today is a SyntaxError in the next Python, and a hook
    # that fails to compile is fail-open on both hosts (for example a
    # `\;` inside a docstring). `-W error` finds it now.
    if ! "$PY" -W error -m py_compile "$hook_path" >/dev/null 2>&1; then
        warn "Hook $hook does not compile under 'python -W error' -- a warning that will become a SyntaxError and silently disable the guard."
        note "Run: $PY -W error -m py_compile $hook_path"
        continue
    fi
    self_test=$("$PY" "$hook_path" --self-test 2>&1)
    if [ $? -eq 0 ] && printf '%s' "$self_test" | grep -q SELFTEST_OK; then
        echo "OK: Hook $hook passed its self-test."
    else
        warn "Hook $hook did NOT pass its self-test."
        note "Output: $(printf '%s' "$self_test" | tr '\n' ' ' | cut -c1-300)"
    fi
done

# ---- 6a. norm: markers -- CLAUDE.md and AGENTS.md carry the same normative
# items, paired by `<!-- norm:<name> -->` markers. Each marker must appear
# exactly once in BOTH files, or the two hosts' rules have drifted. The
# templates are checked where present, the rendered files otherwise (a
# consumer project has only those).
for pair in "CLAUDE.md.template AGENTS.md.template" "CLAUDE.md AGENTS.md"; do
    set -- $pair
    [ -f "$SCRIPT_DIR/$1" ] && [ -f "$SCRIPT_DIR/$2" ] || continue
    markers=$(cat "$SCRIPT_DIR/$1" "$SCRIPT_DIR/$2" | grep -o '<!-- norm:[a-z-]* -->' | sed 's/^<!-- norm://; s/ -->$//' | sort -u)
    if [ -z "$markers" ]; then
        echo "INFO: no norm: markers in $1 / $2 (files predate the pairing); skipped."
        break
    fi
    drift=""
    for m in $markers; do
        for f in "$1" "$2"; do
            n=$(grep -c "<!-- norm:$m -->" "$SCRIPT_DIR/$f")
            [ "$n" -eq 1 ] || drift="$drift $f:$m=$n"
        done
    done
    if [ -z "$drift" ]; then
        echo "OK: norm: marker names agree (not semantic equivalence) between $1 and $2 ($(printf '%s\n' $markers | wc -l | tr -d ' ') markers, each exactly once in both)."
    else
        warn "norm: markers drift between $1 and $2 (expected exactly 1 per file):$drift"
    fi
    break
done

# Host-role behavior is checked separately from the marker-name comparison.
if [ -f "$SCRIPT_DIR/.claude/scripts/test_host_routes.py" ]; then
    if python "$SCRIPT_DIR/.claude/scripts/test_host_routes.py"; then
        echo "OK: host routing and lifecycle scenarios passed."
    else
        warn "host routing/lifecycle scenarios failed; check the two entry instructions and bindings."
    fi
else
    warn "missing .claude/scripts/test_host_routes.py (host parity not checked)."
fi

# ---- 6b. Codex hook trust: the mirrored guards run only once trusted
# Codex records trust in ~/.codex/config.toml under
#   [hooks.state.'<ABSOLUTE hooks.json path>:<event>:<entry>:<handler>']
# keyed by absolute path, one entry PER EVENT. Read events from the installed
# definition so new hooks are not silently omitted. This checks registration
# presence only, not enabled state, the current definition hash, or execution.
CODEX_HOOKS="$SCRIPT_DIR/.codex/hooks.json"
CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
if [ ! -f "$CODEX_HOOKS" ]; then
    echo "INFO: no .codex/hooks.json in this checkout (Codex host guards not installed)."
elif ! command -v codex >/dev/null 2>&1; then
    echo "INFO: .codex/hooks.json present but codex is not on PATH; trust check skipped."
elif [ ! -f "$CODEX_CONFIG" ]; then
    warn "Codex hook trust not registered: $CODEX_CONFIG does not exist."
    note "Run codex in $SCRIPT_DIR once and trust the configured hooks with /hooks (manual: install section, Codex host)."
else
    # Compare case-insensitively with backslashes folded to slashes: codex
    # writes native Windows paths, this script sees POSIX ones.
    # codex writes native paths (`D:\repo\...`); this script sees the MSYS
    # form (`/d/repo/...`) on Windows, so the drive prefix is converted too.
    hooks_key=$(printf '%s' "$CODEX_HOOKS" | tr 'A-Z' 'a-z' | tr '\\' '/' |
                sed 's|^/\([a-z]\)/|\1:/|')
    if ! trust_events=$(python - "$CODEX_HOOKS" <<'PY_EVENTS' 2>/dev/null
import json, re, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    events = json.load(stream)['hooks']
if not isinstance(events, dict) or not events:
    raise ValueError('hooks must be a non-empty object')
names = []
for name in events:
    if not re.fullmatch(r'[A-Z][A-Za-z0-9]*', name):
        raise ValueError('invalid hook event name')
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    names.append(re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name).lower())
sys.stdout.write(' '.join(names))
PY_EVENTS
    ); then
        warn "Cannot read Codex hook events from $CODEX_HOOKS; registration check incomplete."
    else
        trust_missing=""
        for event in $trust_events; do
            # Fixed-string match: path metacharacters are not regex syntax.
            if ! tr 'A-Z' 'a-z' < "$CODEX_CONFIG" | tr '\\' '/' |
                    grep -qF "[hooks.state.'$hooks_key:$event:"; then
                trust_missing="$trust_missing $event"
            fi
        done
        if [ -n "$trust_missing" ]; then
            warn "Codex hook trust missing for:$trust_missing (registration entries absent)."
            note "Run codex in $SCRIPT_DIR once and trust the configured hooks with /hooks; re-trust after every edit of .codex/hooks.json."
        else
            echo "OK: Codex hook registration entries found for: $trust_events."
            note "Registration presence only; enabled state, current hash and actual hook execution are not verified."
        fi
    fi
fi

# ---- 7. optional launcher regression suite (required only when requested)
LAUNCHER_TEST="$SCRIPT_DIR/.claude/scripts/test_launchers.sh"
if [ ! -f "$LAUNCHER_TEST" ]; then
    if [ "$RUN_LAUNCHERS" -eq 1 ]; then
        warn "requested launcher regression suite not found at $LAUNCHER_TEST."
    else
        echo "INFO: optional launcher regression suite not installed; see the template maintenance guide."
    fi
elif [ "$RUN_LAUNCHERS" -ne 1 ]; then
    echo "INFO: launcher regression suite present but not run (add --launchers, duration depends on selected groups and environment; or: bash .claude/scripts/test_launchers.sh)."
elif [ "${BASH_MAJOR:-0}" -lt 4 ]; then
    echo "INFO: launcher regression suite skipped -- it needs bash >= 4 (see the bash warning above)."
else
    launcher_out=$("$BASH_ON_PATH" "$LAUNCHER_TEST" 2>&1)
    if [ $? -eq 0 ] && printf '%s\n' "$launcher_out" | grep -q 'ALL PASS'; then
        echo "OK: launcher regression suite passed ($(printf '%s\n' "$launcher_out" | grep 'ALL PASS' | tail -n 1))."
    else
        warn "launcher regression suite did NOT pass."
        note "Tail: $(printf '%s\n' "$launcher_out" | tail -n 6 | tr '\n' '|')"
    fi
fi

# ---- 8. delegation CLIs (INFO only: absence is the documented fallback)
for cli in codex agy; do
    cli_path=$(command -v "$cli" 2>/dev/null || true)
    if [ -n "$cli_path" ]; then
        echo "INFO: $cli resolves to $cli_path"
    else
        echo "INFO: $cli was not found on PATH (delegation to it falls back; see docs/orchestration/delegation-matrix.md)."
    fi
done

# ---- 9. agy global grant (security-boundary.md): command(...) is a WARN
AGY_SETTINGS=${HARNESS_AGY_SETTINGS:-$HOME/.gemini/antigravity-cli/settings.json}
if [ ! -f "$AGY_SETTINGS" ]; then
    echo "INFO: agy global settings not found at $AGY_SETTINGS (headless agy auto-approval unconfigured; agy lane -> AGY_UNAVAILABLE). Minimal file: docs/harness-manual.md install step 1."
elif [ "$PY_OK" -ne 1 ]; then
    echo "INFO: agy grant not checked (no working python to parse $AGY_SETTINGS)."
else
    # Structural parse of permissions.allow, like agy-run.sh: a text match
    # would also flag a `command(*)` DENY entry.
    grant=$("$PY" - "$AGY_SETTINGS" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    allow = [str(x).strip() for x in (d.get("permissions") or {}).get("allow") or []]
except Exception:
    print("unparseable"); sys.exit(0)
if any(a.startswith("command(") for a in allow): print("command")
elif any(a.startswith("write_file(") for a in allow): print("write_file")
else: print("none")
PY
)
    case "$grant" in
        command)
            warn "$AGY_SETTINGS grants command(...) to headless agy."
            note "An injection in any file agy reads becomes command execution (security-boundary.md)."
            note "agy-run.sh refuses to launch with this grant (HARNESS_DENIED) unless HARNESS_ALLOW_AGY_COMMAND=1 is set for that one task with user approval; running agy by hand has no such gate."
            note "Fix: remove the command(...) entry; the harness grant is write_file(*) only." ;;
        write_file) echo "OK: agy global grant is write_file only ($AGY_SETTINGS)." ;;
        unparseable) echo "INFO: $AGY_SETTINGS is not parseable JSON; agy-run.sh will report AGY_UNAVAILABLE." ;;
        *) echo "INFO: $AGY_SETTINGS has no write_file grant; every agy lane is a write task, so agy-run.sh will report AGY_UNAVAILABLE." ;;
    esac
fi

# ---- 10. WSL2: a clone under /mnt/ needs safe.directory for git to work at all
if [ "$IS_WSL" -eq 1 ]; then
    case "$SCRIPT_DIR" in
        /mnt/*)
            if git -C "$SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
                echo "INFO: WSL2 clone under /mnt/ is readable by git."
            else
                echo "INFO: WSL2 clone under /mnt/ -- git refuses it (dubious ownership). Fix: git config --global --add safe.directory $SCRIPT_DIR"
            fi ;;
    esac
fi

# ---- 11. template drift (opt-in, INFO only)
if [ -n "$TEMPLATE_DIR" ]; then
    if [ ! -d "$TEMPLATE_DIR" ]; then
        echo "INFO: TemplateDir '$TEMPLATE_DIR' does not exist; skipping template drift check."
    else
        TEMPLATE_DIR=$(cd "$TEMPLATE_DIR" && pwd)
        drift=0; compared=0; not_installed=0
        for glob in '.claude/agents/*.md' '.claude/rules/*.md' 'docs/orchestration/*.md' '.claude/skills/*/SKILL.md' \
                    '.claude/hooks/*.py' '.claude/scripts/*' '.claude/settings.json' \
                    '.claude/sandbox-sensitive.json' 'check-windows-aliases.ps1' \
                    'check-posix.sh' '.codex/hooks.json'; do
            for tf in "$TEMPLATE_DIR"/$glob; do
                [ -f "$tf" ] || continue
                rel=${tf#"$TEMPLATE_DIR"/}
                lf="$SCRIPT_DIR/$rel"
                if [ ! -f "$lf" ]; then
                    not_installed=$((not_installed + 1))
                    continue
                fi
                compared=$((compared + 1))
                # Normalize line endings so autocrlf differences are not drift.
                if ! cmp -s <(tr -d '\r' < "$tf") <(tr -d '\r' < "$lf"); then
                    drift=$((drift + 1)); echo "DRIFT: $rel differs from the template."
                fi
            done
        done
        echo "INFO: template comparison covers installed files only; $not_installed template files not installed (not an installation completeness check)."
        echo "INFO: template drift: $drift of $compared harness files differ from '$TEMPLATE_DIR' (INFO only; reconcile deliberately in either direction)."
    fi
else
    echo "INFO: template drift check skipped (pass --template-dir or set HARNESS_TEMPLATE_DIR to the tagteam checkout)."
fi

echo "SUMMARY: $WARNINGS warning(s)."
if [ "$WARNINGS" -gt 0 ]; then exit 1; fi
exit 0
