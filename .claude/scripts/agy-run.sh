#!/usr/bin/env bash
LAUNCH_CLOCK=${EPOCHREALTIME:-$SECONDS}
# Deterministic launcher for Antigravity CLI (`agy -p`) delegations — the
# agy twin of codex-run.sh: preflight (grant sanity, baseline, control-plane
# snapshot) / call (prompt-file delivery, timeout) / postflight (scope,
# control plane, expected outputs) / compact report. Straightforward doc,
# HTML, comment, scaffolding-test and image-verification tasks call this
# directly; the antigravity-delegate agent exists only for pipelines that
# need judgment between runs.

# Associative arrays (-x bookkeeping) need bash 4+; macOS ships 3.2.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    echo "AGY_UNAVAILABLE: bash >= 4 required (found ${BASH_VERSION:-?}) — install a newer bash (docs/platform-notes-macos.md)" >&2
    exit 2
fi
EFFORT=medium
MODEL=
WORKER_ROLE=write
WEB_AGENT=agy-fetcher
WEB_AGENT_SOURCE=.agents/agents/agy-fetcher.md
WEB_HOOK_CONFIG=.agents/hooks.json
WEB_HOOK_GUARD=.agents/hooks/agy_fetch_view_guard.py
WEB_HOOK_GLOBAL_CONFIG=$HOME/.gemini/config/hooks.json
WEB_HOOK_INSTALLED=$HOME/.gemini/config/hooks/agy_fetch_view_guard.py
LOG_DIR=.claude/agy-logs
PROMPT_FILE=
EXPECTED=
TIMEOUT=570
DETACH=0
TOOL=agy
AGY_SETTINGS=${HARNESS_AGY_SETTINGS:-$HOME/.gemini/antigravity-cli/settings.json}
PROMPT_MAX_BYTES=30000   # Windows 32K command-line limit; -p is an argument
ORIG_ARGS=("$@")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# Fail closed: without the state library the concurrency guard is gone.
[ -f "$SCRIPT_DIR/run-state.sh" ] || { echo "HARNESS_DENIED: $SCRIPT_DIR/run-state.sh missing" >&2; exit 4; }
. "$SCRIPT_DIR/launcher-common.sh" || exit 4
timing_init "$LAUNCH_CLOCK"
. "$SCRIPT_DIR/run-state.sh" || exit 4

usage() {
    echo 'Usage: agy-run.sh -p <prompt-file> [-a write|web] [-m MODEL] [-e low|medium|high] [-x <expected-output-file>[,...]] [-l LOG_DIR] [-t TIMEOUT_SECONDS] [-b]' >&2
    echo '       agy-run.sh --status <RUN_ID>' >&2
    echo '       agy-run.sh --wait <RUN_ID> [-t SECONDS<=570]' >&2
    echo 'HARNESS_DENIED: bad invocation (a typo is a policy error, not an availability failure)' >&2
    exit 4
}

# Read-back commands: no delegation, no quota — only the state record.
launcher_readback "$@"

while getopts ":p:a:m:e:x:l:t:b" opt; do
    case "$opt" in
        p) PROMPT_FILE=$OPTARG ;;
        a) WORKER_ROLE=$OPTARG ;;
        m) MODEL=$OPTARG ;;
        e) EFFORT=$OPTARG ;;
        x) EXPECTED=$OPTARG ;;
        l) LOG_DIR=$OPTARG ;;
        t) TIMEOUT=$OPTARG ;;
        b) DETACH=1 ;;
        *) usage ;;
    esac
done

# ---- policy checks (exit 4 = HARNESS_DENIED, never a fallback trigger) ----
case "$EFFORT" in
    low|medium|high) ;;
    *) echo "HARNESS_DENIED: unknown effort '$EFFORT' (low|medium|high)" >&2; exit 4 ;;
esac
case "$WORKER_ROLE" in
    write|web) ;;
    *) echo "HARNESS_DENIED: unknown agy role '$WORKER_ROLE' (write|web)" >&2; exit 4 ;;
esac
case "$MODEL" in
    "") ;;
    [A-Za-z0-9]*)
        case "$MODEL" in
            *[!A-Za-z0-9._-]*) echo "HARNESS_DENIED: invalid model token '$MODEL'" >&2; exit 4 ;;
        esac ;;
    *) echo "HARNESS_DENIED: invalid model token '$MODEL'" >&2; exit 4 ;;
esac
[ "$WORKER_ROLE" != web ] || [ -z "$EXPECTED" ] || {
    echo "HARNESS_DENIED: agy web mode cannot declare output files (-x)" >&2
    exit 4
}
case "$LOG_DIR" in
    ""|/*|*\\*|[A-Za-z]:*|..|../*|*/..|*/../*)
        echo "HARNESS_DENIED: -l must be a repo-relative directory without '..' or backslashes (got '$LOG_DIR')" >&2
        exit 4 ;;
esac
case "$TIMEOUT" in
    ""|*[!0-9]*|0*)
        echo "HARNESS_DENIED: -t must be a positive integer number of seconds (got '$TIMEOUT')" >&2
        exit 4 ;;
esac
[ -n "$PROMPT_FILE" ] || usage          # no -p at all is a bad invocation (exit 4)
if [ ! -s "$PROMPT_FILE" ]; then
    echo "AGY_UNAVAILABLE: prompt file is missing or empty" >&2
    exit 2
fi
PROMPT_BYTES=$(wc -c < "$PROMPT_FILE")
if [ "$PROMPT_BYTES" -gt "$PROMPT_MAX_BYTES" ]; then
    echo "HARNESS_DENIED: prompt file is $PROMPT_BYTES bytes (> $PROMPT_MAX_BYTES; agy takes the prompt as a command-line argument — split the task)" >&2
    exit 4
fi
# Grant sanity (security-boundary.md): write-mode agy auto-approves only what
# its GLOBAL settings allow. The harness write grant is write_file(*) alone; a
# `command` grant turns any injection into command execution, so it needs
# the same per-task user approval as codex full access.
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
if [ -z "$PY" ]; then
    echo "AGY_UNAVAILABLE: python/python3 not found on PATH (the launcher parses agy's JSON with it; on Windows the Store alias stub does not count — docs/harness-manual.md, install section step 0)" >&2
    exit 2
fi
HOOK_PATH_PREFIX=
HOOK_PY=
WEB_URL_HOSTS=
if [ ! -f "$AGY_SETTINGS" ]; then
    echo "AGY_UNAVAILABLE: agy global settings not found at $AGY_SETTINGS (headless auto-approval unconfigured — docs/harness-manual.md, install section)" >&2
    exit 2
fi
# Parse permissions.allow structurally (a grep would confuse deny entries,
# escaped keys and comments). Output: "<has_command> <has_write_file>".
GRANTS=$("$PY" - "$AGY_SETTINGS" <<'PY'
import json, sys
try:
    allow = json.load(open(sys.argv[1], encoding="utf-8")).get("permissions", {}).get("allow", [])
    allow = [str(a).strip() for a in allow] if isinstance(allow, list) else []
    print(int(any(a.startswith("command(") for a in allow)), int(any(a.startswith("write_file(") for a in allow)))
except Exception:
    print("parse-error")
PY
)
if [ "$GRANTS" = parse-error ]; then
    echo "AGY_UNAVAILABLE: could not parse $AGY_SETTINGS as JSON" >&2
    exit 2
fi
if [ "${GRANTS%% *}" = 1 ] && [ "${HARNESS_ALLOW_AGY_COMMAND:-}" != "1" ]; then
    echo "HARNESS_DENIED: $AGY_SETTINGS grants command(...) to headless agy — remove it, or (per-task, with explicit user approval) run with HARNESS_ALLOW_AGY_COMMAND=1" >&2
    exit 4
fi
if [ "$WORKER_ROLE" = write ] && [ "${GRANTS##* }" != 1 ]; then
    echo "AGY_UNAVAILABLE: no write_file grant in permissions.allow of $AGY_SETTINGS — required for agy write mode (docs/harness-manual.md, install section)" >&2
    exit 2
fi
if ! command -v agy >/dev/null 2>&1; then
    echo "AGY_UNAVAILABLE: agy binary not found" >&2
    exit 2
fi
if [ "$WORKER_ROLE" = web ]; then
    URL_INFO=$("$PY" - "$PROMPT_FILE" <<'PY_WEB_URLS'
import ipaddress, re, sys
from pathlib import Path
from urllib.parse import urlsplit
text = Path(sys.argv[1]).read_text(encoding="utf-8")
hosts = []
local = []
for raw in re.findall(r"https?://[^\s<>\"']+", text, flags=re.I):
    host = (urlsplit(raw.rstrip(".,);]}")).hostname or "").lower().rstrip(".")
    if not host or "," in host:
        raise SystemExit(4)
    if host in hosts:
        continue
    hosts.append(host)
    blocked = host == "localhost" or host.endswith(".localhost") or host.endswith(".local") \
        or host == "metadata.google.internal"
    try:
        address = ipaddress.ip_address(host)
        blocked = blocked or address.is_private or address.is_loopback or address.is_link_local \
            or address.is_multicast or address.is_reserved or address.is_unspecified
    except ValueError:
        pass
    if blocked:
        local.append(host)
if not hosts:
    raise SystemExit(3)
print(",".join(hosts) + "\t" + ",".join(local))
PY_WEB_URLS
    ) || {
        echo "HARNESS_DENIED: agy web prompt must name at least one explicit http(s) URL" >&2
        exit 4
    }
    WEB_URL_HOSTS=${URL_INFO%%$'\t'*}
    WEB_LOCAL_HOSTS=${URL_INFO#*$'\t'}
    if [ -n "$WEB_LOCAL_HOSTS" ]; then
        echo "HARNESS_DENIED: agy web mode refuses local/private URL hosts: $WEB_LOCAL_HOSTS" >&2
        exit 4
    fi
    HOOK_PY=$(command -v python 2>/dev/null || true)
    if [ -z "$HOOK_PY" ] || ! "$HOOK_PY" -c 'raise SystemExit(0)' >/dev/null 2>&1; then
        HOOK_SHIM_DIR=$LOG_DIR/.hook-bin
        mkdir -p "$HOOK_SHIM_DIR" || exit 2
        printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$PY" > "$HOOK_SHIM_DIR/python" || exit 2
        chmod +x "$HOOK_SHIM_DIR/python" || exit 2
        HOOK_PY=$HOOK_SHIM_DIR/python
        HOOK_PATH_PREFIX=$(cd "$HOOK_SHIM_DIR" && pwd):
    fi
    WEB_AGENT_INSTALLED=$HOME/.gemini/config/agents/$WEB_AGENT.md
    if [ ! -f "$WEB_AGENT_SOURCE" ] || [ ! -f "$WEB_AGENT_INSTALLED" ] \
       || ! cmp -s "$WEB_AGENT_SOURCE" "$WEB_AGENT_INSTALLED"; then
        echo "AGY_UNAVAILABLE: install the exact checked-in $WEB_AGENT_SOURCE at $WEB_AGENT_INSTALLED" >&2
        exit 2
    fi
    if [ ! -f "$WEB_HOOK_CONFIG" ] || [ ! -f "$WEB_HOOK_GUARD" ] \
       || [ ! -f "$WEB_HOOK_GLOBAL_CONFIG" ] || [ ! -f "$WEB_HOOK_INSTALLED" ] \
       || ! cmp -s "$WEB_HOOK_GUARD" "$WEB_HOOK_INSTALLED"; then
        echo "AGY_UNAVAILABLE: install the checked-in web cache-read hook in $WEB_HOOK_GLOBAL_CONFIG and $WEB_HOOK_INSTALLED" >&2
        exit 2
    fi
    # Check the exact hook wiring and exercise both decisions before granting
    # non-interactive tool permission. This catches stale wiring, interpreter
    # failures and a guard that no longer fails closed.
    if ! PATH="${HOOK_PATH_PREFIX}${PATH}" "$PY" - "$WEB_HOOK_CONFIG" "$WEB_HOOK_GLOBAL_CONFIG" "$WEB_HOOK_GUARD" "$HOOK_PY" <<'PY_WEB_GUARD'
import json, os, pathlib, subprocess, sys, tempfile
source_config_path, global_config_path, guard_path = map(pathlib.Path, sys.argv[1:4])
hook_python = sys.argv[4]
try:
    source_config = json.loads(source_config_path.read_text(encoding="utf-8"))
    global_config = json.loads(global_config_path.read_text(encoding="utf-8"))
    hook = source_config["agy-fetch-cache-only"]
    assert global_config["agy-fetch-cache-only"] == hook
    item, = hook["PreToolUse"]
    handler, = item["hooks"]
    assert hook.get("enabled", True) is True
    assert item["matcher"] == "view_file|read_url_content"
    assert handler["type"] == "command"
    assert handler["command"] == "python ~/.gemini/config/hooks/agy_fetch_view_guard.py"
    assert int(handler["timeout"]) > 0
    with tempfile.TemporaryDirectory(prefix="agy-web-guard-") as tmp:
        artifact = pathlib.Path(tmp) / "conversation"
        cache = artifact / ".system_generated" / "steps" / "1" / "content.md"
        cache.parent.mkdir(parents=True)
        cache.write_text("fetched", encoding="utf-8")
        outside = pathlib.Path(tmp) / "workspace.txt"
        outside.write_text("private", encoding="utf-8")
        web_env = os.environ.copy()
        web_env["HARNESS_AGY_WEB"] = "1"
        web_env["HARNESS_AGY_URL_HOSTS"] = "docs.example.com"
        direct_env = os.environ.copy()
        direct_env.pop("HARNESS_AGY_WEB", None)
        def decision(payload, env):
            result = subprocess.run([hook_python, str(guard_path)], input=json.dumps(payload),
                                    text=True, capture_output=True, env=env, timeout=5, check=True)
            return json.loads(result.stdout)["decision"]
        view = lambda target: {"toolCall": {"name": "view_file", "args": {"AbsolutePath": str(target)}},
                               "artifactDirectoryPath": str(artifact)}
        fetch = lambda url: {"toolCall": {"name": "read_url_content", "args": {"Url": url}}}
        assert decision(view(cache), web_env) == "allow"
        assert decision(view(outside), web_env) == "deny"
        assert decision(fetch("https://docs.example.com/page"), web_env) == "allow"
        assert decision(fetch("https://other.example/page"), web_env) == "deny"
        assert decision(fetch("http://127.0.0.1/x"), web_env) == "deny"
        assert decision(view(outside), direct_env) == "allow"
except Exception as error:
    print(f"web guard preflight failed: {type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(1)
PY_WEB_GUARD
    then
        echo "AGY_UNAVAILABLE: web cache-read hook wiring or fail-closed probe failed" >&2
        exit 2
    fi
    # `agy --agent` otherwise falls back silently to the default agent. Prove
    # both the installed definition and discovery before using
    # --dangerously-skip-permissions: with this named agent the only exposed
    # content tool is read_url_content.
    AGENT_LIST=$(agy agent 2>/dev/null) || {
        echo "AGY_UNAVAILABLE: cannot inspect installed agy agents" >&2
        exit 2
    }
    if ! printf '%s\n' "$AGENT_LIST" | grep -Fxq "$WEB_AGENT"; then
        echo "AGY_UNAVAILABLE: required web-only agent '$WEB_AGENT' is not discoverable; refusing agy's default-agent fallback" >&2
        exit 2
    fi
fi

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
# and return at once with the RUN_ID; the child skips this block via
# HARNESS_RUN_CHILD=1 and writes its report to report-<RUN_ID>.txt.
if [ "$DETACH" -eq 1 ] && [ "${HARNESS_RUN_CHILD:-}" != "1" ]; then
    LAUNCHER_OUT="$LOG_DIR/launcher-$RUN_ID.out"
    HARNESS_RUN_CHILD=1 HARNESS_RUN_ID=$RUN_ID nohup bash "$0" "${ORIG_ARGS[@]}" > "$LAUNCHER_OUT" 2>&1 < /dev/null &
    DETACHED_PID=$!
    disown "$DETACHED_PID" 2>/dev/null
    echo "RUN_ID: $RUN_ID"
    echo "DETACHED: launcher pid $DETACHED_PID, stdout → $LAUNCHER_OUT"
    echo "STATE: $(state_file "$RUN_ID")"
    echo "WAIT: bash $0 --wait $RUN_ID -t 570   (exit 6 = still running, call again; --status for one look)"
    exit 0
fi
# Plumbing only: consumed above, never meant for the CLI child or a -v
# verify script (a nested launcher inheriting HARNESS_RUN_CHILD=1 would
# skip its own detach and adopt this RUN_ID — seen in test_launchers.sh).
unset HARNESS_RUN_CHILD HARNESS_RUN_ID

REPORT_FILE=$(report_file "$RUN_ID")
RUN_JSON="$LOG_DIR/run-$TIMESTAMP.json"
RUN_ERR="$LOG_DIR/run-$TIMESTAMP.err"
AGY_LOG="$LOG_DIR/agy-$TIMESTAMP.log"
RESPONSE_FILE="$LOG_DIR/response-$TIMESTAMP.txt"
CHANGED_FILE=$(mktemp "${TMPDIR:-/tmp}/agy-changed.XXXXXX") || exit 2
TREE_BEFORE=$(mktemp "${TMPDIR:-/tmp}/agy-tree-before.XXXXXX") || exit 2
TREE_AFTER=$(mktemp "${TMPDIR:-/tmp}/agy-tree-after.XXXXXX") || exit 2
CP_BEFORE=$(mktemp "${TMPDIR:-/tmp}/agy-cp-before.XXXXXX") || exit 2
CP_AFTER=$(mktemp "${TMPDIR:-/tmp}/agy-cp-after.XXXXXX") || exit 2
# State bookkeeping (run-state.sh): `running` from the moment agy is
# spawned; `done` only after the report is written; anything else ends
# as `aborted` so --wait never hangs on a record nobody will finish.
FINAL_STATE_WRITTEN=0
cleanup() { rm -f "$CHANGED_FILE" "$TREE_BEFORE" "$TREE_AFTER" "$CP_BEFORE" "$CP_AFTER" ; }
on_exit() {
    if [ "$FINAL_STATE_WRITTEN" -eq 0 ] && [ -f "$(state_file "$RUN_ID")" ]; then
        state_write aborted 1 "launcher exited before postflight"
    fi
    cleanup
}
# Signal: TERM the child's whole process group, wait up to 15 s for it to
# die (KILL after that), and only then record `aborted`.
on_signal() {
    CHILD_TREE_NOTE="no child was running"
    terminate_child_tree
    state_write aborted 143 "launcher received a termination signal; agy child pid ${CHILD_PID:-none}: $CHILD_TREE_NOTE"
    FINAL_STATE_WRITTEN=1
    cleanup
    exit 143
}
trap on_exit EXIT
trap on_signal HUP INT TERM

# ---- preflight ----
SETTINGS_LOCAL=.claude/settings.local.json
SETTINGS_LOCAL_SNAP="$LOG_DIR/settings-local-$TIMESTAMP.json"
[ -f "$SETTINGS_LOCAL" ] && cp -p "$SETTINGS_LOCAL" "$SETTINGS_LOCAL_SNAP" 2>/dev/null
control_before
. "$SCRIPT_DIR/workspace-evidence.sh" || exit 4
WORKSPACE_EXCLUDES=(--exclude "$RUN_JSON" --exclude "$RUN_ERR" --exclude "$AGY_LOG" --exclude "$RESPONSE_FILE"
    --exclude "$SETTINGS_LOCAL_SNAP" --exclude "$LOG_DIR/baseline-$TIMESTAMP.diff"
    --exclude "$LOG_DIR/launcher-$RUN_ID.out")
workspace_before "$TREE_BEFORE" "${WORKSPACE_EXCLUDES[@]}"
if [ "$WORKSPACE_MODE" = git ]; then
    HEAD=$(git rev-parse --verify HEAD 2>/dev/null || true)
    save_git_baseline
fi

# Expected outputs (-x): record pre-run identity so postflight can prove
# THIS run wrote them (a pre-existing file merely existing is not proof).
# GNU stat first (`%y` = human mtime WITH nanoseconds, so a same-second
# same-size rewrite still counts), BSD/macOS stat second
# (`%m` is whole seconds: a same-second same-size rewrite reads as
# MISSING there — documented limit); "absent" when neither sees it.
stat_id() { stat -c '%y:%s' "$1" 2>/dev/null || stat -f '%m:%z' "$1" 2>/dev/null || echo absent; }
declare -A EXP_BEFORE
EXP=()
if [ -n "$EXPECTED" ]; then
    IFS=',' read -r -a RAW_EXP <<< "$EXPECTED"
    for f in "${RAW_EXP[@]}"; do
        [ -n "$f" ] || continue          # tolerate a trailing comma
        EXP+=("$f"); EXP_BEFORE["$f"]=$(stat_id "$f")
    done
fi
# Retry only with positive evidence that the first attempt left nothing to
# build on. Inspection failure, content changes, commits and external -x
# outputs all prevent a second invocation.
retry_is_clean() {
    local f retry_head
    workspace_after "$TREE_BEFORE" "$TREE_AFTER" "$CHANGED_FILE" "${WORKSPACE_EXCLUDES[@]}"
    [ "$WORKSPACE_OK" -eq 1 ] && [ ! -s "$CHANGED_FILE" ] || return 1
    if [ "$WORKSPACE_MODE" = git ]; then
        retry_head=$(git rev-parse --verify HEAD 2>/dev/null || true)
        [ "$retry_head" = "$HEAD" ] || return 1
    fi
    for f in "${EXP[@]}"; do
        [ "$(stat_id "$f")" = "${EXP_BEFORE[$f]}" ] || return 1
    done
    return 0
}
# ---- call ----
# The prompt reaches agy as ONE argument via "$(cat file)": command
# substitution output is not re-parsed for metacharacters, so task text
# containing $(), backticks or quotes cannot execute (security-boundary).
RUNNER=()
TIMEOUT_WRAPPER=none
if timeout --version 2>/dev/null | grep -qi coreutils; then
    RUNNER=(timeout -k 10 $((TIMEOUT + 15)))
    TIMEOUT_WRAPPER="gnu timeout $((TIMEOUT + 15))s"
fi
# The CLI runs as a background child so its PID is recorded and a signal
# to the launcher can be forwarded; `wait` keeps the call synchronous.
run_agy() {
    local -a agy_args run_env
    run_env=(env "PATH=${HOOK_PATH_PREFIX}${PATH}")
    agy_args=(--log-file "$AGY_LOG" --effort "$EFFORT" --output-format json
        --print-timeout "${TIMEOUT}s")
    [ -z "$MODEL" ] || agy_args+=(--model "$MODEL")
    if [ "$WORKER_ROLE" = web ]; then
        run_env+=(HARNESS_AGY_WEB=1 "HARNESS_AGY_URL_HOSTS=$WEB_URL_HOSTS")
        agy_args+=(--agent "$WEB_AGENT" --dangerously-skip-permissions --disable-slash-commands)
    fi
    timing_enter cli
    set -m   # own process group, so a signal reaches agy and its children
    "${run_env[@]}" "${RUNNER[@]}" agy "${agy_args[@]}" -p "$(cat "$PROMPT_FILE")" > "$RUN_JSON" 2> "$RUN_ERR" < /dev/null &
    CHILD_PID=$!
    set +m
    CHILD_STIME=$(pid_stime "$CHILD_PID")
    state_write running "" "agy running (attempt ${ATTEMPTS:-1})"
    wait "$CHILD_PID"
    local child_exit=$?
    timing_enter postflight
    return "$child_exit"
}
# Parse the JSON result: last JSON line of stdout (agy may print banners
# first); the auto-denied banner is looked for on BOTH streams and the
# matching line itself is reported. Output: status, turns, tokens,
# response line count, denied line — tab separated; "invalid" when no
# JSON line parses.
parse_result() {
    "$PY" - "$RUN_JSON" "$RUN_ERR" "$RESPONSE_FILE" <<'PY'
import json, sys
src, err, out = sys.argv[1], sys.argv[2], sys.argv[3]
status = "invalid"; response = ""; turns = "?"; tokens = "?"; denied = ""; error = ""
try:
    lines = [l for l in open(src, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
    try:
        err_lines = open(err, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        err_lines = []
    # Banner lines only — the JSON line carries the model's response,
    # which may legitimately contain the words "auto-denied".
    for l in [x for x in lines if not x.startswith("{")] + err_lines:
        if "auto-denied" in l:
            denied = l.strip()[:200]
            break
    for l in reversed(lines):
        if l.startswith("{"):
            d = json.loads(l)
            status = str(d.get("status"))
            response = d.get("response") or ""
            turns = str(d.get("num_turns", "?"))
            tokens = str((d.get("usage") or {}).get("total_tokens", "?"))
            error = str(d.get("error") or "").replace("\t", " ").replace("\n", " ")[:200]
            break
except Exception:
    status = "invalid"
open(out, "w", encoding="utf-8").write(response)
print("\t".join([status, turns, tokens, str(response.count("\n") + 1 if response else 0), denied, error]))
PY
}
run_agy
AGY_EXIT=$?
ATTEMPTS=1
PARSED=$(parse_result)
# Empty output (no stdout, or a SUCCESS with an empty response) is agy's
# known intermittent failure mode: ONE retry, and only when the first
# attempt changed nothing in the tree (a retry on top of a half-written
# result would double the writes and the quota).
if [ "$AGY_EXIT" -eq 0 ] && { [ ! -s "$RUN_JSON" ] || [ "$(printf '%s' "$PARSED" | cut -f4)" = 0 ]; } \
   && retry_is_clean; then
    ATTEMPTS=2
    run_agy
    AGY_EXIT=$?
    PARSED=$(parse_result)
fi
AGY_STATUS=$(printf '%s' "$PARSED" | cut -f1)
TURNS=$(printf '%s' "$PARSED" | cut -f2)
TOKENS=$(printf '%s' "$PARSED" | cut -f3)
RESPONSE_LINES=$(printf '%s' "$PARSED" | cut -f4)
DENIED=$(printf '%s' "$PARSED" | cut -f5)
AGY_ERROR=$(printf '%s' "$PARSED" | cut -f6)
[ -z "$AGY_STATUS" ] && AGY_STATUS=invalid
WEB_RECEIPT_OK=1
if [ "$WORKER_ROLE" = web ]; then
    if grep -qiE '^[[:space:]]*([*_`>#-][[:space:]]*)*FETCH_INCOMPLETE([[:space:]]|:)' "$RESPONSE_FILE" 2>/dev/null \
       || ! grep -qE '^[[:space:]]*EVIDENCE[[:space:]]*:' "$RESPONSE_FILE" 2>/dev/null \
       || ! grep -qE '^[[:space:]]*SOURCES[[:space:]]*:' "$RESPONSE_FILE" 2>/dev/null; then
        WEB_RECEIPT_OK=0
    fi
fi

# ---- postflight ----
workspace_after "$TREE_BEFORE" "$TREE_AFTER" "$CHANGED_FILE" "${WORKSPACE_EXCLUDES[@]}"
DIRTY_EDITS=0
[ ! -s "$CHANGED_FILE" ] || DIRTY_EDITS=1

POST_HEAD=
[ "$WORKSPACE_MODE" != git ] || POST_HEAD=$(git rev-parse --verify HEAD 2>/dev/null || true)
SCOPE_WARNING=0
if [ "$POST_HEAD" != "$HEAD" ]; then
    SCOPE_WARNING=1
    NEW_COMMIT=$(git log --oneline -1 2>/dev/null)
fi

control_after

CP_BLOCK=0
if [ -n "$CP_HITS" ] && [ "${HARNESS_ALLOW_CONTROL_PLANE:-}" != "1" ]; then
    CP_BLOCK=1
fi

# Expected outputs (-x): exist, non-empty, AND written by this run (new,
# or mtime/size changed since preflight). agy resolves RELATIVE paths
# into its own scratch dir and still reports success, so the prompt must
# use absolute paths and this check is what proves they landed.
PRODUCED=
MISSING=
if [ -n "$EXPECTED" ]; then
    for f in "${EXP[@]}"; do
        if [ -s "$f" ] && [ "$(stat_id "$f")" != "${EXP_BEFORE[$f]}" ]; then
            PRODUCED="$PRODUCED${PRODUCED:+,}$f"
        else
            MISSING="$MISSING${MISSING:+,}$f"
        fi
    done
fi

# DONE needs evidence of work: a non-empty response, or every expected
# output proven written — a SUCCESS with nothing to show is the
# documented auto-denied/empty failure shape, never DONE.
if [ "$AGY_EXIT" -eq 0 ] && [ "$AGY_STATUS" = SUCCESS ] && [ -z "$DENIED" ] && [ -z "$MISSING" ] \
   && { [ "$WORKER_ROLE" != web ] || [ "$DIRTY_EDITS" -eq 0 ]; } \
   && { [ "$WORKER_ROLE" != web ] || [ "$WEB_RECEIPT_OK" -eq 1 ]; } \
   && { [ -s "$RESPONSE_FILE" ] || [ -n "$PRODUCED" ]; }; then
    STATUS=DONE
elif [ "$AGY_STATUS" = invalid ]; then
    STATUS=AGY_UNAVAILABLE
elif [ "$WORKER_ROLE" = web ] && { [ -n "$DENIED" ] || [ ! -s "$RESPONSE_FILE" ]; }; then
    STATUS=AGY_UNAVAILABLE
elif [ "$WORKER_ROLE" = web ] && [ "$WEB_RECEIPT_OK" -ne 1 ]; then
    STATUS=AGY_UNAVAILABLE
elif printf '%s' "$AGY_ERROR" | grep -qiE 'quota|rate limit|not logged|unauthenticated|auth'; then
    # Plan quota / login problems are the documented fallback trigger
    # (claude-implementer), not a task failure to retry or escalate.
    STATUS=AGY_UNAVAILABLE
else
    STATUS=FAILED
fi
if [ "$STATUS" = AGY_UNAVAILABLE ] && { [ -s "$CHANGED_FILE" ] || [ -n "$PRODUCED" ]; }; then
    STATUS="FAILED(unusable result after workspace changes)"
fi
if [ "$CP_EVIDENCE_OK" -ne 1 ]; then
    STATUS="FAILED(control-plane evidence unavailable, was $STATUS)"
fi
if [ "$WORKSPACE_OK" -ne 1 ]; then
    STATUS="FAILED(workspace evidence unavailable, was $STATUS)"
fi
STOP_GATE_LEFT=
if [ -f .claude/.stop-gate ]; then
    STOP_GATE_LEFT=1
    STATUS="FAILED(stop-gate unsatisfied, was $STATUS)"
fi
if [ "$SCOPE_WARNING" -eq 1 ]; then
    STATUS="FAILED(unauthorized-commit)"
fi
if [ "$CP_BLOCK" -eq 1 ]; then
    STATUS="BLOCKED(control-plane, was $STATUS)"
fi

case "$STATUS" in
    DONE) EXIT_CODE=0 ;;
    AGY_UNAVAILABLE) EXIT_CODE=2 ;;
    *) EXIT_CODE=1 ;;
esac

# ---- report ----
report() {
echo "STATUS: $STATUS (agy_exit=$AGY_EXIT, agy_status=$AGY_STATUS, attempts=$ATTEMPTS, role=$WORKER_ROLE, model=${MODEL:-default}, effort=$EFFORT, turns=$TURNS)"
echo "RUN_ID: $RUN_ID (state: $(state_file "$RUN_ID"), report: $REPORT_FILE)"
if [ -n "$STALE_CLEANED" ]; then
    echo "STALE_RUN_CLEANED: earlier run(s) $STALE_CLEANED had died without a final state — marked aborted"
fi
if [ "$AGY_EXIT" -eq 124 ]; then
    echo "TIMEOUT: agy killed by the launcher wrapper (${TIMEOUT}s + 15s grace)"
fi
if [ "$TIMEOUT_WRAPPER" = none ]; then
    echo "TIMEOUT_WRAPPER: none (GNU coreutils timeout not first on PATH) — only agy's own --print-timeout ${TIMEOUT}s applies"
fi
if [ -n "$DENIED" ]; then
    echo "AGY_DENIED: $DENIED"
fi
if [ -n "$AGY_ERROR" ]; then
    echo "AGY_ERROR: $AGY_ERROR"
fi
if [ "${HARNESS_ALLOW_AGY_COMMAND:-}" = "1" ]; then
    echo "AGY_COMMAND_APPROVED: HARNESS_ALLOW_AGY_COMMAND=1 was present for this run"
fi
if [ "$WORKER_ROLE" = web ]; then
    echo "WEB_AGENT: $WEB_AGENT (read_url_content + guarded cache view; installed definition and discovery verified before launch)"
    echo "WEB_FILE_GUARD: $WEB_HOOK_GUARD (current conversation generated content.md only)"
    echo "WEB_RECEIPT: $([ "$WEB_RECEIPT_OK" -eq 1 ] && echo complete || echo incomplete) (requires EVIDENCE and SOURCES; FETCH_INCOMPLETE falls back)"
fi
if [ -n "${HARNESS_AGY_SETTINGS:-}" ]; then
    echo "SETTINGS_OVERRIDE: grant gate read $AGY_SETTINGS instead of the real agy global settings (test/diagnostic use only)"
fi
echo "$WORKSPACE_DESCRIPTION"
echo "CHANGED: $CHANGED"
if [ "$DIRTY_EDITS" -eq 1 ]; then
    echo "CHANGED_CONTENT: file changes detected by workspace snapshots; inspect the reported paths"
fi
if [ -n "$EXPECTED" ]; then
    echo "PRODUCED: ${PRODUCED:-none}"
    [ -n "$MISSING" ] && echo "MISSING: $MISSING (expected output absent or empty — check the prompt used ABSOLUTE paths)"
fi
READ_COUNT=$(grep -cE '^READ:' "$RESPONSE_FILE" 2>/dev/null | head -1)
READ_COUNT=${READ_COUNT:-0}
READ_LINES=$(grep -E '^READ:' "$RESPONSE_FILE" 2>/dev/null | sed 's/^READ:[[:space:]]*//' | head -8 | paste -sd' ' -)
if [ "$READ_COUNT" -gt 8 ]; then
    echo "READ: ($READ_COUNT lines, first 8 shown — full list in $RESPONSE_FILE) $READ_LINES"
else
    echo "READ: ${READ_LINES:-(not reported — the prompt must ask agy to list the files it read)}"
fi
echo "TOKENS: $TOKENS"
timing_report
if [ "$SCOPE_WARNING" -eq 1 ]; then
    echo "SCOPE_WARNING: unauthorized commit"
    echo "NEW_COMMIT: $NEW_COMMIT"
fi
if [ -n "$STOP_GATE_LEFT" ]; then
    echo "STOP_GATE_UNSATISFIED: .claude/.stop-gate is still present; the run is not done"
fi
if [ -n "$CP_HITS" ]; then
    if [ "$CP_BLOCK" -eq 1 ]; then
        echo "CONTROL_PLANE_WARNING: harness enforcement files changed: $CP_HITS — review/revert before any further Bash call; rerun with HARNESS_ALLOW_CONTROL_PLANE=1 only with explicit user approval"
    else
        echo "CONTROL_PLANE_APPROVED: HARNESS_ALLOW_CONTROL_PLANE=1 was present; changed: $CP_HITS"
    fi
fi
if [ -n "$CP_NOTICE" ]; then
    echo "CONTROL_PLANE_NOTICE: session-owned files changed (not blocking — the orchestrator session writes these itself; judge the diff): $CP_NOTICE"
    case ",$CP_NOTICE," in *",$SETTINGS_LOCAL,"*)
        SL_DIFF=$(diff -u "$([ -f "$SETTINGS_LOCAL_SNAP" ] && echo "$SETTINGS_LOCAL_SNAP" || echo /dev/null)" "$SETTINGS_LOCAL" 2>/dev/null)
        printf '%s\n' "$SL_DIFF" | head -n 40
        # A permission/allow line ADDED to settings.local.json is the one
        # escalation shape worth a loud flag; a user's own
        # approval looks identical, so this stays a NOTICE, not a block.
        if printf '%s\n' "$SL_DIFF" | grep -qiE '^\+.*("allow"|Bash\(|"permissions")'; then
            echo "CONTROL_PLANE_NOTICE: settings.local.json ADDED a permission/allow entry — confirm this is YOUR approval, not a delegate escalation, before trusting this run"
        fi ;;
    esac
fi
echo "LOG: $RUN_JSON (stderr: $RUN_ERR, agy log: $AGY_LOG)"
if [ -s "$RESPONSE_FILE" ]; then
    echo "RESPONSE:"
    head -n 60 "$RESPONSE_FILE"
    if [ "$RESPONSE_LINES" -gt 60 ] 2>/dev/null; then
        echo "[truncated at 60 lines — full response: $RESPONSE_FILE]"
    fi
else
    echo "RESPONSE: (empty)"
    tail -n 10 "$RUN_ERR" 2>/dev/null
fi
}
# Persist first (so --wait/--status can serve it even when stdout is
# gone), then echo; the state flips to done only after that.
report > "$REPORT_FILE"
cat "$REPORT_FILE"
state_write done "$EXIT_CODE" "$STATUS" && FINAL_STATE_WRITTEN=1
exit "$EXIT_CODE"
