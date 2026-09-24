#!/usr/bin/env bash
# Quota-free regression tests for codex-run.sh, agy-run.sh, and run-state.sh.
# Every launcher runs in a throwaway git repository with an isolated HOME.

set -u

# Select whole independent sections; omitted cases are not reported as passes.
TEST_GROUPS=all
list_groups() {
    printf '%s\n' 'core: input/availability, shared status, stop gate, state setup' \
        'codex: successful run, hooks, bindings and effort guards' \
        'claude: result parsing, tool scope, change evidence and permissions' \
        'agy: grants, output identity and result/retry mapping' \
        'evidence: control-plane, workspace, commit and verifier evidence' \
        'lifecycle: active-writer guard, forget, detached runs, signals and timeout' \
        'nongit: non-Git workspace, cross-tool state and retry evidence'
}
while [ $# -gt 0 ]; do
    case "$1" in
        --group)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo '--group requires comma-separated names' >&2; exit 2; }
            TEST_GROUPS=$2; shift 2 ;;
        --list) list_groups; exit 0 ;;
        --help|-h) echo 'Usage: test_launchers.sh [--group all|core,codex,claude,agy,evidence,lifecycle,nongit] [--list]'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$TEST_GROUPS" in ,*|*,|*,,*) echo 'Empty test group' >&2; exit 2 ;; esac
IFS=',' read -r -a REQUESTED_GROUPS <<< "$TEST_GROUPS"
for test_group in "${REQUESTED_GROUPS[@]}"; do
    case "$test_group" in all|core|codex|claude|agy|evidence|lifecycle|nongit) ;;
        *) echo "Unknown test group: $test_group" >&2; exit 2 ;;
    esac
done
selected() {
    local requested tag
    for requested in "${REQUESTED_GROUPS[@]}"; do
        [ "$requested" != all ] || return 0
        for tag in "$@"; do [ "$requested" != "$tag" ] || return 0; done
    done
    return 1
}
echo "GROUPS: $TEST_GROUPS"


# The suite may itself run inside a launcher (as a -v verify script, or
# under a detached run): scrub every launcher marker and the detach
# plumbing so the launchers under test start from a clean environment.
# Cases set what they need explicitly per invocation.
unset HARNESS_SAVE_BASELINE HARNESS_ALLOW_CONTROL_PLANE HARNESS_ALLOW_AGY_COMMAND HARNESS_ALLOW_FULL_ACCESS       HARNESS_ALLOW_FORGET HARNESS_AGY_SETTINGS HARNESS_STATE_DIR HARNESS_TREE_KEY       HARNESS_RUN_ID HARNESS_RUN_CHILD STUB_ACTION STUB_VERIFY_PATH

if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    echo "FAIL: bash 4+ required"
    echo
    echo "1 FAILURES / 1 cases"
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
CODEX_RUN="$SCRIPT_DIR/codex-run.sh"
AGY_RUN="$SCRIPT_DIR/agy-run.sh"
CLAUDE_RUN="$SCRIPT_DIR/claude-run.sh"
RUN_STATE="$SCRIPT_DIR/run-state.sh"
TEST_ROOT="$REPO_ROOT/.claude/.test-tmp/$$"
# True non-Git fixtures must live outside the template's enclosing repository.
NON_GIT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/harness-nongit.XXXXXX") || exit 1
STUB_BIN="$TEST_ROOT/bin"
NO_CODEX_BIN="$TEST_ROOT/no-codex-bin"
ORIGINAL_PATH=$PATH
ORIGINAL_GIT_DIR=$(dirname "$(command -v git)")
REAL_PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)

TOTAL=0
FAILS=0
SKIPS=0
CASE_NO=0
CASE_REPO=
CASE_HOME=
LAST_OUT=
LAST_RC=0
BG_PIDS=()

cleanup() {
    local p
    for p in "${BG_PIDS[@]}"; do
        kill -TERM -- "-$p" 2>/dev/null || true
        kill -TERM "$p" 2>/dev/null || true
    done
    for p in "${BG_PIDS[@]}"; do
        wait "$p" 2>/dev/null || true
    done
    rm -rf -- "$TEST_ROOT"
    case "$NON_GIT_ROOT" in "${TMPDIR:-/tmp}/harness-nongit."*) rm -rf -- "$NON_GIT_ROOT" ;; esac
    # The parent is ours too when nothing else is left in it.
    rmdir "$REPO_ROOT/.claude/.test-tmp" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

# Leftovers of a hard-killed earlier run: the EXIT trap never runs on
# SIGKILL / a tool-timeout kill, so sweep sibling <pid>
# fixture dirs whose owner is gone. `kill -0` alone cannot tell a reused
# or foreign pid from a dead one, so a dir is only removed when its pid
# is dead AND it is older than 5 minutes (a fresh leftover waits for the
# next run). Non-numeric names are someone's ad-hoc repro dirs — left
# alone and reported.
NOW=$(date +%s)
for d in "$REPO_ROOT"/.claude/.test-tmp/*/; do
    [ -d "$d" ] || continue
    p=$(basename "$d")
    [ "$p" = "$$" ] && continue
    case "$p" in *[!0-9]*) echo "INFO: leaving non-fixture dir .claude/.test-tmp/$p"; continue ;; esac
    if ! kill -0 "$p" 2>/dev/null; then
        age=$(( NOW - $(stat -c %Y "$d" 2>/dev/null || stat -f %m "$d" 2>/dev/null || echo "$NOW") ))
        [ "$age" -gt 300 ] && rm -rf -- "$d"
    fi
done
mkdir -p "$STUB_BIN" "$NO_CODEX_BIN"

cat > "$STUB_BIN/codex" <<'STUB'
#!/usr/bin/env bash
set -u
if [ "${1:-}" = "--version" ]; then
    echo "codex-cli 0.152.1"
    exit 0
fi
printf '%s\n' "$*" >> "$FIXTURE_ROOT/codex-args.log"
out=
probe=0
prev=
for arg in "$@"; do
    [ "$prev" = "--output-last-message" ] && out=$arg
    case "$arg" in *SANDBOX_OK*) probe=1 ;; esac
    prev=$arg
done
if [ "$probe" -eq 1 ]; then
    echo SANDBOX_OK
    exit 0
fi
cat > "$FIXTURE_ROOT/codex-stdin.log"
echo "stub codex running"
printf '%s\n' "stub final message" > "${out:-$FIXTURE_ROOT/codex-last-message.log}"
case "${STUB_ACTION:-none}" in
    notice) printf '{}\n' > .claude/.preflight-status ;;
    none) ;;
    remove:*) rm -f -- "${STUB_ACTION#remove:}" ;;
    init-git) git init -q ;;
    break-index) printf 'broken index' > .git/index ;;
    touch-control-plane) printf '%s\n' '# stub changed control plane' >> .claude/settings.json ;;
    touch-settings-local) printf '%s\n' '{"stub":true}' > .claude/settings.local.json ;;
    # The codex TUI rewrites config.toml whenever it records hook trust.
    # That record is normalized out of the hash; anything else in the file
    # is not.
    touch-codex-trust)
        printf '\n[hooks.state.%s]\ntrusted_hash = "sha256:stub"\n' "'/x/.codex/hooks.json:stop:0:0'" \
            >> "$HOME/.codex/config.toml" ;;
    touch-codex-config)
        printf '\n[features]\nstub_added = true\n' >> "$HOME/.codex/config.toml" ;;
    # TOML allows whitespace before a table header. Anchoring the skip
    # terminator at column 0 hid everything after an indented header
    # (push review 2026-09-04, BLOCKER).
    touch-codex-indented)
        printf '\n [projects."/repo"]\ntrust_level = "trusted"\n' >> "$HOME/.codex/config.toml" ;;
    delayed-write:*) sleep 1.1; printf '%s\n' 'stub-output' > "${STUB_ACTION#delayed-write:}" ;;
    write:*) printf '%s\n' 'stub-output' > "${STUB_ACTION#write:}" ;;
    commit) git -c user.name=Stub -c user.email=stub@example.invalid commit --allow-empty -m stub >/dev/null ;;
    sleep:*)
        printf '%s\n' "$$" > "$FIXTURE_ROOT/stub-child.pid"
        sleep "${STUB_ACTION#sleep:}"
        ;;
    exit:*) exit "${STUB_ACTION#exit:}" ;;
    # Codex prints one `hook: <Event> <outcome>` line per hook it runs;
    # the launcher's canary reads them back out of the run log.
    hooks:both) echo "hook: PreToolUse Completed"; echo "hook: Stop Completed" ;;
    hooks:pre) echo "hook: PreToolUse Completed" ;;
    hooks:fixture) cat "$FIXTURE_ROOT/hook-output.log" ;;
    tamper-verify) printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$STUB_VERIFY_PATH" ;;
    # Any launcher copy of the verifier in TMPDIR is inside Codex's write scope.
    tamper-verify-tmp)
        grep -lx 'exit 23' "$TMPDIR"/* 2>/dev/null | while IFS= read -r copy; do printf 'exit 0\n' > "$copy"; done ;;
    *) echo "unknown STUB_ACTION: $STUB_ACTION" >&2; exit 98 ;;
esac
echo "tokens used"
echo "42"
STUB
chmod +x "$STUB_BIN/codex"

cat > "$STUB_BIN/agy" <<'STUB'
#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FIXTURE_ROOT/agy-args.log"
case "${STUB_ACTION:-none}" in
    notice) printf '{}\n' > .claude/.preflight-status ;;
    none) ;;
    remove:*) rm -f -- "${STUB_ACTION#remove:}" ;;
    init-git) git init -q ;;
    break-index) printf 'broken index' > .git/index ;;
    delayed-write:*) sleep 1.1; printf '%s\n' 'stub-output' > "${STUB_ACTION#delayed-write:}" ;;
    write:*) printf '%s\n' 'stub-output' > "${STUB_ACTION#write:}" ;;
    touch-control-plane) printf '%s\n' '# stub changed control plane' >> .claude/settings.json ;;
    # agy widening its OWN grant list: must BLOCK.
    touch-agy-settings) printf '%s\n' '{"permissions":{"allow":["write_file(*)","command(*)"]}}' > "$HOME/.gemini/antigravity-cli/settings.json" ;;
    error:quota)
        printf '%s\n' '{"status":"ERROR","response":"quota unavailable","num_turns":1,"usage":{"total_tokens":0},"error":"quota exceeded"}'
        exit 0
        ;;
    nojson-write) printf 'partial' > app.txt; echo 'not json'; exit 0 ;;
    nojson-delete) rm -f app.txt; echo 'not json'; exit 0 ;;
    nojson-external) printf 'partial' > "$STUB_EXPECTED"; echo 'not json'; exit 0 ;;
    nojson-break-git) mkdir .git; echo 'not json'; exit 0 ;;
    nojson) echo 'not json'; exit 0 ;;
    empty)
        printf '%s\n' '{"status":"SUCCESS","response":"","num_turns":1,"usage":{"total_tokens":3}}'
        exit 0
        ;;
    exit:*) exit "${STUB_ACTION#exit:}" ;;
    *) echo "unknown STUB_ACTION: $STUB_ACTION" >&2; exit 98 ;;
esac
printf '%s\n' '{"status":"SUCCESS","response":"stub agy response","num_turns":1,"usage":{"total_tokens":17}}'
STUB
chmod +x "$STUB_BIN/agy"

# Stub `claude` (claude-run.sh): prompt arrives on STDIN, the result is a
# single JSON envelope on stdout -- the shape the launcher parses.
cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
set -u
echo "$*" >> "$FIXTURE_ROOT/claude-args.log"
echo "${HARNESS_DELEGATE_RUN:-unset}" > "$FIXTURE_ROOT/claude-role.log"
cat > "$FIXTURE_ROOT/claude-stdin.log"
case "${STUB_ACTION:-none}" in
    notice) printf '{}\n' > .claude/.preflight-status ;;
    none) ;;
    tamper-verify-source) printf 'exit 0\n' > verify.sh ;;
    tamper-verify-copy) printf 'exit 0\n' > .claude/claude-logs/verify-verifycopy.sh ;;
    remove:*) rm -f -- "${STUB_ACTION#remove:}" ;;
    init-git) git init -q ;;
    break-index) printf 'broken index' > .git/index ;;
    touch-control-plane) echo '# stub changed control plane' >> .claude/settings.json ;;
    write:*) echo 'stub-output' > "${STUB_ACTION#write:}" ;;
    commit) git -c user.name=Stub -c user.email=stub@example.invalid commit --allow-empty -m stub >/dev/null ;;
    nojson) echo 'not json'; exit 0 ;;
    errorjson) echo '{"is_error":true,"result":"quota exhausted"}'; exit 0 ;;
    rewind) git update-ref HEAD HEAD~1 ;;
    delete-head) git update-ref -d HEAD ;;
    emptyjson) echo '{"result":""}'; exit 0 ;;
    # Real CLI envelope shapes for the web role. web-denied is the reproduced
    # false-DONE: process exit 0, subtype success, and the fetch never happened.
    web-denied)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"FETCH_UNAVAILABLE","permission_denials":[{"tool_name":"WebFetch","tool_use_id":"toolu_stub","tool_input":{"url":"https://example.com","prompt":"read it"}}],"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    web-nostruct)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"SUMMARY: stub says it fetched the page","usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    web-malformed)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"x","structured_output":{"status":"ok","summary":"no sources key at all"},"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    web-unavailable)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"x","structured_output":{"status":"unavailable","summary":"stub reader could not reach the page","sources":[{"url":"https://example.com","fetched":false}]},"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    web-nofetch)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"x","structured_output":{"status":"ok","summary":"stub summary written from memory","sources":[{"url":"https://example.com","fetched":false}]},"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    web-partial)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"x","structured_output":{"status":"ok","summary":"one page missing","sources":[{"url":"https://example.com","fetched":true},{"url":"https://docs.python.org","fetched":false}]}}'
        exit 0 ;;
    web-empty-summary)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"plausible prose","structured_output":{"status":"ok","summary":"","sources":[{"url":"https://example.com","fetched":true}]}}'
        exit 0 ;;
    # A genuine non-web success whose PROSE contains the web sentinels.
    prose-sentinel)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"Report: the page said FETCH_UNAVAILABLE and \"status\":\"unavailable\", quoted here as prose.","usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    # Incidental denial outside the web role: the run still succeeded.
    bash-denied)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"stub final message","permission_denials":[{"tool_name":"Bash","tool_use_id":"toolu_stub","tool_input":{"command":"ls"}}],"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
    exit:*) exit "${STUB_ACTION#exit:}" ;;
    *) echo "unknown STUB_ACTION: $STUB_ACTION" >&2; exit 98 ;;
esac
# Default success. When the launcher asked for a schema (web role), answer in
# that shape -- STUB_ACTION=none falls through to here, which is what keeps
# claude-web and the web-model effort scenario at rc 0.
case "$*" in
    *--json-schema*)
        printf '%s\n' '{"is_error":false,"subtype":"success","result":"","structured_output":{"status":"ok","summary":"stub web summary quoting FETCH_UNAVAILABLE and status=unavailable as page text","sources":[{"url":"https://example.com","fetched":true}]},"usage":{"input_tokens":10,"output_tokens":5}}'
        exit 0 ;;
esac
echo '{"result":"stub final message","usage":{"input_tokens":10,"output_tokens":5}}'
STUB
chmod +x "$STUB_BIN/claude"

if [ -n "$REAL_PY" ]; then
    printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$REAL_PY" > "$NO_CODEX_BIN/python"
    chmod +x "$NO_CODEX_BIN/python"
fi

pass_case() {
    TOTAL=$((TOTAL + 1))
    echo "PASS: $1"
}

fail_case() {
    TOTAL=$((TOTAL + 1))
    FAILS=$((FAILS + 1))
    echo "FAIL: $1"
    if [ -n "${2:-}" ]; then echo "      $2"; fi
    if [ -n "$LAST_OUT" ] && [ -f "$LAST_OUT" ]; then
        tail -n 8 "$LAST_OUT" | sed 's/^/      /'
    fi
}

skip_case() {
    TOTAL=$((TOTAL + 1))
    SKIPS=$((SKIPS + 1))
    echo "SKIP: $1 ($2)"
}

expect_case() {
    local name=$1 ok=$2 detail=${3:-}
    if [ "$ok" -eq 1 ]; then pass_case "$name"; else fail_case "$name" "$detail"; fi
}

fresh_case() {
    CASE_NO=$((CASE_NO + 1))
    CASE_REPO="$TEST_ROOT/case-$CASE_NO/repo"
    CASE_HOME="$TEST_ROOT/case-$CASE_NO/home"
    [ "${1:-}" != nongit ] || CASE_REPO="$NON_GIT_ROOT/case-$CASE_NO/repo"
    mkdir -p "$CASE_REPO/.claude" "$CASE_HOME/.gemini/antigravity-cli"
    printf '%s\n' '{"permissions":{"allow":["write_file(*)"],"deny":[]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
    printf '%s\n' '{"permissions":{}}' > "$CASE_REPO/.claude/settings.json"
    printf '%s\n' '.claude/codex-logs/' '.claude/agy-logs/' '.claude/claude-logs/' '.claude/.probe-cache' > "$CASE_REPO/.gitignore"
    printf '%s\n' 'test prompt' > "$CASE_REPO/prompt.txt"
    [ "${1:-}" != nongit ] || return 0
    (
        cd "$CASE_REPO" || exit 1
        git init -q
        # On an autocrlf=true machine every case printed three "LF will be
        # replaced by CRLF" lines and FAIL: lines drowned in ~100 warnings
        # Silence the WARNING only — the machine's
        # autocrlf setting itself is kept, so the launchers' porcelain
        # parsing still runs under the CRLF conditions it must survive.
        git config core.safecrlf false
        git add .claude/settings.json .gitignore prompt.txt
        if [ "${1:-}" != unborn ]; then
            git -c user.name=Fixture -c user.email=fixture@example.invalid commit -q -m fixture
        fi
    )
}

run_capture() {
    local label=$1
    shift
    LAST_OUT="$TEST_ROOT/$label.out"
    (
        cd "$CASE_REPO" || exit 99
        env HOME="$CASE_HOME" PATH="$STUB_BIN:$ORIGINAL_PATH" FIXTURE_ROOT="$TEST_ROOT" "$@"
    ) > "$LAST_OUT" 2>&1
    LAST_RC=$?
}

has() { grep -Eq -- "$2" "$1"; }

# r2 N2: Windows argv delivery turned `\\` into `\`, so this verifier passed
# through the launcher while failing when run directly. Intact text exits 31.
write_escape_verifier() {
    printf '%s\n' '{"home": "C:\Users\dev"}' > "$CASE_REPO/result.json"
    cat > "$CASE_REPO/verify.sh" <<'EOF'
grep -q 'C:\\Users' result.json || exit 41
[ "a\"b" = 'a"b' ] || exit 42
[ 'a\b' = "a\\b" ] || exit 43
[ "$(printf '%s' '한글' | wc -c)" -eq 6 ] || exit 44
exit 31
EOF
    ESCAPE_DIRECT_RC=0
    (cd "$CASE_REPO" && bash verify.sh </dev/null >/dev/null 2>&1) || ESCAPE_DIRECT_RC=$?
}

# Check observed report phases, including readback and failed-verifier runs.
timing_ok() {
    "$REAL_PY" - "$1" "${2:-1}" "${3:-0}" <<'PYTIME'
import re, sys
header = open(sys.argv[1], encoding="utf-8").read().split("FINAL_MESSAGE:", 1)[0].split("RESPONSE:", 1)[0]
match = re.search(r"^TIMING: (.+)$", header, re.M)
if not match:
    sys.exit(1)
fields = dict(item.split("=", 1) for item in match.group(1).split())
phases = [int(fields[key]) for key in ("preflight_ms", "cli_ms", "postflight_ms", "verify_ms")]
total = int(fields["total_ms"])
assert all(value >= 0 for value in phases) and sum(phases) == total
assert fields["resolution"] == "seconds" or total > 0
assert int(fields["attempts"]) == int(sys.argv[2])
assert f"ELAPSED: {total // 1000}s" in header
if sys.argv[3] == "0":
    assert phases[3] == 0
elif fields["resolution"] == "ms":
    assert phases[3] > 0
PYTIME
}

state_path() {
    find "$CASE_HOME/.claude/harness-runs" -type f -name "state-$1.json" -print 2>/dev/null | head -n 1
}

state_field_is() {
    local rid=$1 field=$2 expected=$3 file
    file=$(state_path "$rid")
    [ -n "$file" ] || return 1
    "$REAL_PY" - "$file" "$field" "$expected" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
actual = d.get(sys.argv[2])
expected = sys.argv[3]
if isinstance(actual, int): expected = int(expected)
raise SystemExit(0 if actual == expected else 1)
PY
}

plant_record() {
    local rid=$1 pid=$2 sandbox=$3
    (
        cd "$CASE_REPO" || exit 1
        env HOME="$CASE_HOME" PATH="$STUB_BIN:$ORIGINAL_PATH" RID="$rid" PLANT_PID="$pid" PLANT_SANDBOX="$sandbox" \
            RUN_STATE_PATH="$RUN_STATE" "$BASH" -c '
                TOOL=codex RUN_ID=$RID TIMEOUT=570 SANDBOX=$PLANT_SANDBOX
                . "$RUN_STATE_PATH" || exit 1
                LAUNCHER_PID=$PLANT_PID
                LAUNCHER_STIME=$(pid_stime "$PLANT_PID")
                STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
                STARTED_EPOCH=$(date +%s)
                CHILD_PID=
                state_write running "" planted
            '
    )
}

start_live_shell() {
    set -m
    "$BASH" -c 'trap "exit 0" TERM; while :; do sleep 300 & wait $!; done' &
    LIVE_PID=$!
    set +m
    BG_PIDS+=("$LIVE_PID")
}

stop_live_shell() {
    local pid=$1
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

if [ -z "$REAL_PY" ]; then
    fail_case "python available" "python is required by the launchers"
    echo
    echo "$FAILS FAILURES / $TOTAL cases"
    exit 1
fi

if selected core; then
# Policy and availability: these must not invoke either CLI stub.
fresh_case
rm -f "$TEST_ROOT/codex-args.log"
run_capture policy-no-p bash "$CODEX_RUN"
ok=0; [ "$LAST_RC" -eq 4 ] && [ ! -e "$TEST_ROOT/codex-args.log" ] && ok=1
expect_case "codex missing -p is usage denial" "$ok" "exit=$LAST_RC"

fresh_case
run_capture policy-missing-p bash "$CODEX_RUN" -p missing.txt
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" 'CODEX_UNAVAILABLE' && ok=1
expect_case "codex missing prompt file" "$ok" "exit=$LAST_RC"

for spec in 'effort|-e|bogus' 'sandbox|-s|bogus' 'timeout|-t|0' 'log dir|-l|../x' 'verify string|-v|echo hi'; do
    IFS='|' read -r name flag value <<< "$spec"
    fresh_case
    run_capture "policy-$CASE_NO" bash "$CODEX_RUN" -p prompt.txt "$flag" "$value"
    ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED' && ok=1
    expect_case "codex rejects invalid $name" "$ok" "exit=$LAST_RC"
done

fresh_case
LAST_OUT="$TEST_ROOT/codex-absent.out"
(
    cd "$CASE_REPO" || exit 99
    env HOME="$CASE_HOME" PATH="$NO_CODEX_BIN:$ORIGINAL_GIT_DIR:/usr/bin:/bin" "$BASH" "$CODEX_RUN" -p prompt.txt
) > "$LAST_OUT" 2>&1
LAST_RC=$?
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" 'CODEX_UNAVAILABLE: codex binary not found' && ok=1
expect_case "codex binary absent" "$ok" "exit=$LAST_RC"

fresh_case
run_capture status-codex bash "$CODEX_RUN" --status nonexistent
rc1=$LAST_RC
run_capture status-agy bash "$AGY_RUN" --status nonexistent
rc2=$LAST_RC
ok=0; [ "$rc1" -eq 7 ] && [ "$rc2" -eq 7 ] && ok=1
expect_case "unknown status is exit 7 for both launchers" "$ok" "codex=$rc1 agy=$rc2"

fresh_case
run_capture forget-denied bash "$CODEX_RUN" --forget nonexistent
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED' && ok=1
expect_case "forget requires approval marker" "$ok" "exit=$LAST_RC"


fi

if selected agy; then
fresh_case
printf '%s\n' '{"permissions":{"allow":["write_file(*)","command(*)"]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
rm -f "$TEST_ROOT/agy-args.log"
run_capture agy-command-denied bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED' && [ ! -e "$TEST_ROOT/agy-args.log" ] && ok=1
expect_case "agy command grant denied" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' '{"permissions":{"allow":["write_file(*)","command(*)"]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
run_capture agy-command-approved env HARNESS_ALLOW_AGY_COMMAND=1 STUB_ACTION=none HARNESS_RUN_ID=agycmd bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'AGY_COMMAND_APPROVED' && ok=1
expect_case "agy command grant explicit approval" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' '{"permissions":{"allow":[]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
run_capture agy-no-write bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" 'no write_file grant' && ok=1
expect_case "agy requires write_file allow" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' '{"permissions":{"allow":["write_file(*)"],"deny":["command(*)"]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
run_capture agy-command-deny-only env STUB_ACTION=none HARNESS_RUN_ID=agydeny bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && ! has "$LAST_OUT" 'HARNESS_DENIED' && ok=1
expect_case "agy command entry in deny is ignored by grant gate" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' '{"permissions":{"allow":["command(*)"]}}' > "$CASE_HOME/.gemini/antigravity-cli/settings.json"
decoy="$CASE_HOME/decoy-settings.json"
printf '%s\n' '{"permissions":{"allow":["write_file(*)"]}}' > "$decoy"
run_capture agy-settings-override env HARNESS_AGY_SETTINGS="$decoy" STUB_ACTION=none HARNESS_RUN_ID=agyoverride bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'SETTINGS_OVERRIDE' && ok=1
expect_case "agy settings override is honored" "$ok" "exit=$LAST_RC"

fresh_case
dd if=/dev/zero of="$CASE_REPO/large.txt" bs=30001 count=1 2>/dev/null
rm -f "$TEST_ROOT/agy-args.log"
run_capture agy-large-prompt bash "$AGY_RUN" -p large.txt
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" '> 30000' && [ ! -e "$TEST_ROOT/agy-args.log" ] && ok=1
expect_case "agy rejects prompt over 30 KB" "$ok" "exit=$LAST_RC"


fi

if selected codex; then
# Successful paths and persisted state.
fresh_case
: > "$TEST_ROOT/codex-args.log"
run_capture codex-done env STUB_ACTION=none HARNESS_RUN_ID=codexdone bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" '^CHANGED: none$' \
   && has "$LAST_OUT" '^FINAL_MESSAGE:$' && has "$LAST_OUT" 'stub final message' \
   && state_field_is codexdone state done && state_field_is codexdone exit 0 \
   && has "$TEST_ROOT/codex-args.log" 'You are a DELEGATE.*Role is already resolved.*Skip the Orchestrator workflow' \
   && cmp -s "$CASE_REPO/prompt.txt" "$TEST_ROOT/codex-stdin.log"; then ok=1; fi
run_capture codex-done-status bash "$CODEX_RUN" --status codexdone
[ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' || ok=0
timing_ok "$LAST_OUT" 1 0 || ok=0
expect_case "codex DONE report and saved state" "$ok" "status exit=$LAST_RC"

fresh_case
: > "$TEST_ROOT/codex-args.log"
run_capture codex-resume-role env STUB_ACTION=none bash "$CODEX_RUN" -p prompt.txt -r fixture-session
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$TEST_ROOT/codex-args.log" 'exec resume fixture-session.*You are a DELEGATE.*Role is already resolved.*Follow the correction' \
   && cmp -s "$CASE_REPO/prompt.txt" "$TEST_ROOT/codex-stdin.log"; then ok=1; fi
expect_case "codex resume retains delegate context and original stdin" "$ok" "rc=$LAST_RC"

# Hook canary reports observed status lines or unknown activity. It cannot
# authenticate merged output and must never change the launcher's exit code.
fresh_case
mkdir -p "$CASE_REPO/.codex"
printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
run_capture canary-both env HARNESS_ALLOW_UNTRUSTED_HOOKS=1 STUB_ACTION=hooks:both HARNESS_RUN_ID=canaryboth bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && ! has "$LAST_OUT" '^HOOKS_(UNKNOWN|FAILED):'; then ok=1; fi
expect_case "canary silent when both hook outcomes are observed" "$ok" "rc=$LAST_RC"

fresh_case
mkdir -p "$CASE_REPO/.codex"
printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
run_capture canary-partial env HARNESS_ALLOW_UNTRUSTED_HOOKS=1 STUB_ACTION=hooks:pre HARNESS_RUN_ID=canarypre bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^HOOKS_UNKNOWN: no recognized outcome lines for: Stop '; then ok=1; fi
expect_case "canary names the event with unknown activity" "$ok" "rc=$LAST_RC"

fresh_case
mkdir -p "$CASE_REPO/.codex"
printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
run_capture canary-none env HARNESS_ALLOW_UNTRUSTED_HOOKS=1 STUB_ACTION=none HARNESS_RUN_ID=canarynone bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^HOOKS_UNKNOWN: no recognized outcome lines for: PreToolUse Stop '; then ok=1; fi
expect_case "canary reports unknown activity without outcome lines" "$ok" "rc=$LAST_RC"

# Reproduce document/tool-output contamination without calling a real model.
for hook_case in quoted-failure quoted-success failed; do
    fresh_case
    mkdir -p "$CASE_REPO/.codex"
    printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
    case "$hook_case" in
        quoted-failure)
            printf '%s\r\n' 'hook: PreToolUse Completed' 'hook: Stop Blocked' > "$TEST_ROOT/hook-output.log"
            cat "$REPO_ROOT/docs/harness-manual.md" >> "$TEST_ROOT/hook-output.log"
            printf '%s\n' '1602:  **exit code 2: `hook: PreToolUse Failed`**' \
                'hook: Stop Failed is an example, not a status line' >> "$TEST_ROOT/hook-output.log" ;;
        quoted-success)
            printf '%s\n' '  Example: `hook: PreToolUse Completed`' \
                '2624:1759- `hook: Stop Blocked`' 'hook: Stop Completed is an example' > "$TEST_ROOT/hook-output.log" ;;
        failed)
            printf '%s\n' 'hook: PreToolUse Completed' 'hook: PreToolUse Failed' > "$TEST_ROOT/hook-output.log"
            printf '%s\r\n' 'hook: Stop Failed' >> "$TEST_ROOT/hook-output.log" ;;
    esac
    run_capture "canary-$hook_case" env HARNESS_ALLOW_UNTRUSTED_HOOKS=1 STUB_ACTION=hooks:fixture bash "$CODEX_RUN" -p prompt.txt
    ok=0
    if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE'; then
        case "$hook_case" in
            quoted-failure) ! has "$LAST_OUT" '^HOOKS_(UNKNOWN|FAILED):' && ok=1 ;;
            quoted-success) has "$LAST_OUT" '^HOOKS_UNKNOWN: no recognized outcome lines for: PreToolUse Stop ' \
                && ! has "$LAST_OUT" '^HOOKS_FAILED:' && ok=1 ;;
            failed) has "$LAST_OUT" '^HOOKS_FAILED: failure status lines observed for: PreToolUse Stop ' \
                && ! has "$LAST_OUT" '^HOOKS_UNKNOWN:' && ok=1 ;;
        esac
    fi
    expect_case "canary $hook_case keeps status text advisory" "$ok" "rc=$LAST_RC"
done

# Hook trust preflight: a project hooks.json with no trust
# entry means the mirrored guards are inert, so the run is refused BEFORE
# codex starts -- the canary can only report after every command has run.
fresh_case
mkdir -p "$CASE_REPO/.codex"
printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
run_capture untrusted-hooks env STUB_ACTION=none HARNESS_RUN_ID=untrusted bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED: Codex hook trust is not confirmed'; then ok=1; fi
expect_case "untrusted codex hooks refuse the run" "$ok" "exit=$LAST_RC"

fresh_case
mkdir -p "$CASE_REPO/.codex"
printf '%s\n' '{"hooks":{}}' > "$CASE_REPO/.codex/hooks.json"
run_capture untrusted-override env STUB_ACTION=none HARNESS_ALLOW_UNTRUSTED_HOOKS=1 HARNESS_RUN_ID=untrustedok bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE'; then ok=1; fi
expect_case "explicit override runs deliberately unguarded" "$ok" "exit=$LAST_RC"


fi

if selected core; then
# Stop gate left behind = verification never passed, on either launcher.
fresh_case
printf '%s\n' 'verify.sh' > "$CASE_REPO/.claude/.stop-gate"
run_capture codex-stopgate env STUB_ACTION=none HARNESS_RUN_ID=codexstopgate bash "$CODEX_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'STATUS: FAILED\(stop-gate unsatisfied' \
   && has "$LAST_OUT" '^STOP_GATE_UNSATISFIED'; then ok=1; fi
expect_case "codex unsatisfied stop-gate is not DONE" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' 'verify.sh' > "$CASE_REPO/.claude/.stop-gate"
run_capture claude-stopgate env STUB_ACTION=none HARNESS_RUN_ID=claudestopgate bash "$CLAUDE_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'STATUS: FAILED\(stop-gate unsatisfied' \
   && has "$LAST_OUT" '^STOP_GATE_UNSATISFIED'; then ok=1; fi
expect_case "claude unsatisfied stop-gate is not DONE" "$ok" "exit=$LAST_RC"

fresh_case
printf '%s\n' 'verify.sh' > "$CASE_REPO/.claude/.stop-gate"
run_capture agy-stopgate env STUB_ACTION=none bash "$AGY_RUN" -p prompt.txt
ok=0
[ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(stop-gate unsatisfied' && has "$LAST_OUT" '^STOP_GATE_UNSATISFIED' && ok=1
expect_case "agy unsatisfied stop-gate is not DONE" "$ok" "exit=$LAST_RC"


fi

if selected claude; then
# claude-run.sh: the Claude twin of codex-run.sh, so a
# Codex-orchestrated session can delegate here under the same contract.
fresh_case
run_capture claude-done env STUB_ACTION=none HARNESS_RUN_ID=claudedone bash "$CLAUDE_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" 'stub final message'    && has "$LAST_OUT" '^TOKENS: 15$' && state_field_is claudedone state done; then ok=1; fi
timing_ok "$LAST_OUT" 1 0 || ok=0
expect_case "claude DONE report and saved state" "$ok" "exit=$LAST_RC"

ok=0
if has "$TEST_ROOT/claude-role.log" '^1$' && has "$TEST_ROOT/claude-args.log" '--tools Read,Write,Edit,Bash,Grep,Glob' \
   && has "$TEST_ROOT/claude-args.log" '--append-system-prompt You are a DELEGATE.*Role is already resolved.*Skip the Orchestrator workflow' \
   && cmp -s "$CASE_REPO/prompt.txt" "$TEST_ROOT/claude-stdin.log"; then ok=1; fi
expect_case "Claude process receives delegate role and no Agent tool" "$ok" "missing worker contract"

for action in nojson errorjson emptyjson; do
    fresh_case
    run_capture "claude-$action" env STUB_ACTION="$action" bash "$CLAUDE_RUN" -p prompt.txt
    ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED' && ok=1
    expect_case "claude rejects $action despite process exit 0" "$ok" "exit=$LAST_RC"
done

fresh_case
printf 'exit 7\n' > "$CASE_REPO/verify.sh"
run_capture claude-verify-fail env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(verification' && has "$LAST_OUT" '^VERIFY: exit 7' && ok=1
timing_ok "$LAST_OUT" 1 1 || ok=0
expect_case "Claude passing report cannot override failed verifier" "$ok" "exit=$LAST_RC"

fresh_case
printf 'read -r ignored\nexit 23\n' > "$CASE_REPO/verify.sh"
run_capture claude-verify-stdin env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'claude_exit=0' && has "$LAST_OUT" '^VERIFY: exit 23' && ok=1
expect_case "Claude verifier stdin cannot consume its own remaining source" "$ok" "exit=$LAST_RC"

for action in none tamper-verify-source tamper-verify-copy; do
    fresh_case
    printf 'exit 23\n' > "$CASE_REPO/verify.sh"
    run_capture "claude-verify-$action" env STUB_ACTION="$action" HARNESS_RUN_ID=verifycopy bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
    ok=0
    if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(verification' && has "$LAST_OUT" 'claude_exit=0'; then
        if [ "$action" = tamper-verify-source ]; then
            has "$LAST_OUT" 'VERIFY_INTEGRITY_FAILED' && ok=1
        else
            has "$LAST_OUT" '^VERIFY: exit 23' && ok=1
        fi
    fi
    expect_case "Claude captured verifier survives $action" "$ok" "exit=$LAST_RC"
done
fresh_case
printf 'printf ORIGINAL_VERIFIER\nexit 0\n' > "$CASE_REPO/verify.sh"
run_capture claude-verify-pass env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" 'ORIGINAL_VERIFIER' && ok=1
expect_case "Claude intact passing verifier succeeds" "$ok" "exit=$LAST_RC"

fresh_case
write_escape_verifier
run_capture claude-verify-escapes env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$ESCAPE_DIRECT_RC" -eq 31 ] && has "$LAST_OUT" '^VERIFY: exit 31$' && ok=1
expect_case "Claude verifier text keeps backslashes, quotes and Korean" "$ok" "direct=$ESCAPE_DIRECT_RC exit=$LAST_RC"

# Bash refuses `exit 0<NUL>` as a binary file; $(cat) would drop the NUL.
fresh_case
printf 'exit 0\000\n' > "$CASE_REPO/verify.sh"
run_capture claude-verify-nul env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(verification' && has "$LAST_OUT" 'VERIFY_EXECUTION_FAILED' && ok=1
expect_case "Claude verifier with a NUL byte fails" "$ok" "exit=$LAST_RC"

# Web role: the launcher's verdict comes from the structured contract and
# runtime denial metadata, never from result prose. A fetch that did not
# happen must never report DONE (reproduced false-DONE, 2026-09-22).
fresh_case
: > "$TEST_ROOT/claude-args.log"
run_capture claude-web env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt -a web -s read-only
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" '^WEB_FETCH: ok' \
   && has "$LAST_OUT" 'mode=plan' \
   && has "$TEST_ROOT/claude-args.log" '\-\-tools WebFetch ' \
   && has "$TEST_ROOT/claude-args.log" '\-\-json-schema .*required.*status.*summary.*sources' \
   && has "$TEST_ROOT/claude-args.log" 'Fetch only the URLs the task names' \
   && ! has "$TEST_ROOT/claude-args.log" '\-\-tools [A-Za-z,]*(Agent|Task|Bash|Write)'; then ok=1; fi
expect_case "Claude web success keeps WebFetch-only tools, plan mode and the reader contract" "$ok" "exit=$LAST_RC"

fresh_case
run_capture claude-web-denied env STUB_ACTION=web-denied bash "$CLAUDE_RUN" -p prompt.txt -a web -s read-only
ok=0
if [ "$LAST_RC" -eq 1 ] && ! has "$LAST_OUT" '^STATUS: DONE' \
   && has "$LAST_OUT" '^STATUS: FAILED\(web fetch permission denied, was DONE\)' \
   && has "$LAST_OUT" '^WEB_FETCH: denied' && has "$LAST_OUT" 'FETCH_UNAVAILABLE'; then ok=1; fi
expect_case "Claude web WebFetch denial is never DONE" "$ok" "exit=$LAST_RC"

for spec in 'web-nostruct|web structured_output missing|missing' \
            'web-malformed|web structured_output malformed|malformed' \
            'web-unavailable|web fetch unavailable|unavailable' \
            'web-nofetch|web fetch produced no fetched source|nofetch' \
            'web-partial|web fetch produced no fetched source|nofetch' \
            'web-empty-summary|web structured_output malformed|malformed'; do
    IFS='|' read -r action reason state <<< "$spec"
    fresh_case
    run_capture "claude-$action" env STUB_ACTION="$action" bash "$CLAUDE_RUN" -p prompt.txt -a web -s read-only
    ok=0
    if [ "$LAST_RC" -eq 1 ] && ! has "$LAST_OUT" '^STATUS: DONE' \
       && has "$LAST_OUT" "^STATUS: FAILED\\($reason, was DONE\\)" \
       && has "$LAST_OUT" "^WEB_FETCH: $state"; then ok=1; fi
    expect_case "Claude web $action is not DONE" "$ok" "exit=$LAST_RC"
done

# A failed fetch still shows what the reader said, so the parent can read the
# cause without opening the raw JSON.
fresh_case
run_capture claude-web-summary env STUB_ACTION=web-unavailable bash "$CLAUDE_RUN" -p prompt.txt -a web -s read-only
ok=0
[ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'stub reader could not reach the page' && ok=1
expect_case "Claude web failure keeps the reader summary visible" "$ok" "exit=$LAST_RC"

# The web contract must not leak into the implement role.
fresh_case
: > "$TEST_ROOT/claude-args.log"
run_capture claude-bash-denied env STUB_ACTION=bash-denied bash "$CLAUDE_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && ! has "$LAST_OUT" '^WEB_FETCH' \
   && ! has "$TEST_ROOT/claude-args.log" 'json-schema'; then ok=1; fi
expect_case "non-web run with an incidental denied Bash still completes" "$ok" "exit=$LAST_RC"

fresh_case
run_capture claude-prose env STUB_ACTION=prose-sentinel bash "$CLAUDE_RUN" -p prompt.txt
ok=0
[ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && ok=1
expect_case "genuine success whose prose matches the web sentinels is not misclassified" "$ok" "exit=$LAST_RC"

fresh_case
run_capture claude-readonly env STUB_ACTION=none HARNESS_RUN_ID=claudero bash "$CLAUDE_RUN" -p prompt.txt -s read-only
ok=0
if [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'mode=plan'; then ok=1; fi
expect_case "claude read-only maps to plan mode" "$ok" "exit=$LAST_RC"

fresh_case
run_capture claude-cp env STUB_ACTION=touch-control-plane HARNESS_RUN_ID=claudecp bash "$CLAUDE_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'STATUS: BLOCKED\(control-plane, was DONE\)'    && has "$LAST_OUT" '^CONTROL_PLANE_WARNING'; then ok=1; fi
expect_case "claude control-plane edit blocks the run" "$ok" "exit=$LAST_RC"


fi

if selected claude evidence; then
# Claude workspace evidence: compare actual bytes across each starting state.
for area in control app; do
    for initial in clean dirty untracked; do
        for action in none write; do
            fresh_case
            if [ "$area" = control ]; then
                target='.claude/rules/sample rule.md'
                mkdir -p "$CASE_REPO/.claude/rules"
            else
                target='app file.txt'
            fi
            printf 'original000\n' > "$CASE_REPO/$target"
            if [ "$initial" != untracked ]; then
                git -C "$CASE_REPO" add -- "$target"
                git -C "$CASE_REPO" -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm baseline
            fi
            [ "$initial" != dirty ] || printf 'baseline000\n' > "$CASE_REPO/$target"
            stub=none
            [ "$action" != write ] || stub="write:$target"
            run_capture "claude-evidence-$area-$initial-$action" env STUB_ACTION="$stub" bash "$CLAUDE_RUN" -p prompt.txt
            expected=0
            [ "$area:$action" != control:write ] || expected=1
            ok=0
            if [ "$LAST_RC" -eq "$expected" ]; then
                if [ "$action" = none ]; then
                    has "$LAST_OUT" '^CHANGED: none$' && ok=1
                elif has "$LAST_OUT" "^CHANGED: $target$"; then
                    if [ "$area" = app ]; then ok=1
                    elif has "$LAST_OUT" '^STATUS: BLOCKED' && has "$LAST_OUT" '^CONTROL_PLANE_WARNING'; then ok=1; fi
                fi
            fi
            expect_case "Claude $area $initial $action preserves exact change evidence" "$ok" "exit=$LAST_RC expected=$expected"
        done
    done
done
fresh_case
printf '\n' >> "$CASE_REPO/.claude/settings.json"
run_capture claude-dirty-approved env STUB_ACTION=touch-control-plane HARNESS_ALLOW_CONTROL_PLANE=1 bash "$CLAUDE_RUN" -p prompt.txt
ok=0
[ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^CHANGED: .claude/settings.json$' && has "$LAST_OUT" '^CONTROL_PLANE_APPROVED' && ok=1
expect_case "Claude approved dirty control change still has evidence" "$ok" "exit=$LAST_RC"

fresh_case
printf 'existing project work\n' > "$CASE_REPO/app.txt"
run_capture claude-root-log env STUB_ACTION=write:app.txt bash "$CLAUDE_RUN" -p prompt.txt -l .
ok=0
[ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^CHANGED: app.txt$' && ok=1
expect_case "Claude root log directory does not hide project changes" "$ok" "exit=$LAST_RC"

# Approval permits changes, never missing before/after evidence.
for evidence_stage in before after; do
    for approved in 0 1; do
        fresh_case
        driver="$TEST_ROOT/driver-$CASE_NO"
        mkdir -p "$driver"
        cp "$CLAUDE_RUN" "$SCRIPT_DIR/run-state.sh" "$SCRIPT_DIR/workspace-snapshot.py" "$SCRIPT_DIR/workspace-evidence.sh" "$SCRIPT_DIR/launcher-common.sh" "$driver/"
        if [ "$evidence_stage" = after ]; then
            cat > "$driver/control-plane-hash.sh" <<'CP_STUB'
#!/usr/bin/env bash
[ ! -e "$FIXTURE_ROOT/cp-already-called" ] || exit 7
touch "$FIXTURE_ROOT/cp-already-called"
sha256sum .claude/settings.json
CP_STUB
        fi
        rm -f "$TEST_ROOT/cp-already-called"
        run_capture "claude-evidence-$evidence_stage-$approved" env STUB_ACTION=none HARNESS_ALLOW_CONTROL_PLANE="$approved" bash "$driver/claude-run.sh" -p prompt.txt
        ok=0
        if [ "$evidence_stage" = before ]; then
            [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'cannot snapshot the starting control plane' && ok=1
        else
            [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(control-plane evidence unavailable' && has "$LAST_OUT" '^CONTROL_PLANE_EVIDENCE: unavailable' && ok=1
        fi
        expect_case "Claude $evidence_stage evidence failure is incomplete with approval=$approved" "$ok" "exit=$LAST_RC"
    done
done

fi

if selected claude; then
fresh_case unborn
run_capture claude-first-commit env STUB_ACTION=commit bash "$CLAUDE_RUN" -p prompt.txt
ok=0
[ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^NEW_COMMIT: [0-9a-f]' && ok=1
expect_case "Claude first unauthorized commit is detected in an unborn repository" "$ok" "exit=$LAST_RC"

fresh_case
run_capture claude-commit env STUB_ACTION=commit HARNESS_RUN_ID=claudecommit bash "$CLAUDE_RUN" -p prompt.txt
ok=0
if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: SCOPE_WARNING' && has "$LAST_OUT" '^NEW_COMMIT:'; then ok=1; fi
expect_case "claude unauthorized commit fails" "$ok" "exit=$LAST_RC"

for action in rewind delete-head; do
    fresh_case
    git -C "$CASE_REPO" -c user.name=Stub -c user.email=stub@example.invalid commit --allow-empty -qm second
    run_capture "claude-$action" env STUB_ACTION="$action" bash "$CLAUDE_RUN" -p prompt.txt
    ok=0
    [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: .*SCOPE_WARNING' && has "$LAST_OUT" '^HEAD_CHANGED:' && ok=1
    expect_case "Claude detects $action even without new commits" "$ok" "exit=$LAST_RC"
done

for bad in "-e banana" "-s danger-full-access" "-e max" "-l /abs/log" "-t 900"; do
    fresh_case
    run_capture "claude-bad-$RANDOM" env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt $bad
    ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED' && ok=1
    expect_case "claude rejects $bad" "$ok" "exit=$LAST_RC"
done

fresh_case
LAST_OUT="$TEST_ROOT/claude-absent.out"
(
    cd "$CASE_REPO" || exit 99
    env HOME="$CASE_HOME" PATH="$NO_CODEX_BIN:$ORIGINAL_GIT_DIR:/usr/bin:/bin" "$BASH" "$CLAUDE_RUN" -p prompt.txt
) > "$LAST_OUT" 2>&1
LAST_RC=$?
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" '^CLAUDE_UNAVAILABLE: claude is not on PATH' && ok=1
expect_case "claude binary absent is CLAUDE_UNAVAILABLE" "$ok" "exit=$LAST_RC"

# Check the actual CLI arguments: missing effort must not borrow a different
# model's default, and explicit effort must still win over a nullable binding.
for scenario in default-high default-null default-missing local-null explicit-model explicit-both explicit-effort web-model empty-explicit; do
    fresh_case
    : > "$TEST_ROOT/claude-args.log"
    bind_effort=',"effort":"high"'
    case "$scenario" in
        default-null|explicit-effort) bind_effort=',"effort":null' ;;
        default-missing) bind_effort= ;;
    esac
    printf '%s\n' "{\"bindings\":{\"B\":{\"claude\":{\"model\":\"bound-model\"$bind_effort}}},\"roles\":{\"implement\":{\"claude_ladder\":[{\"binding\":\"B/claude\"}]}}}" > "$CASE_REPO/.claude/model-bindings.json"
    args=(); expected_model=bound-model; expected_effort=; expected_role=implement
    case "$scenario" in
        default-high) expected_effort=high ;;
        local-null) printf '%s\n' '{"bindings":{"B":{"claude":{"effort":null}}}}' > "$CASE_REPO/.claude/model-bindings.local.json" ;;
        explicit-model) args=(-m selected-model); expected_model=selected-model ;;
        explicit-both) args=(-m selected-model -e low); expected_model=selected-model; expected_effort=low ;;
        explicit-effort) args=(-e medium); expected_effort=medium ;;
        web-model) args=(-a web -s read-only -m selected-model); expected_model=selected-model; expected_role=web ;;
        empty-explicit) args=(-e "") ;;
    esac
    run_capture "claude-effort-$scenario" env STUB_ACTION=none bash "$CLAUDE_RUN" -p prompt.txt "${args[@]}"
    ok=0
    if [ "$scenario" = empty-explicit ]; then
        [ "$LAST_RC" -eq 4 ] && [ ! -s "$TEST_ROOT/claude-args.log" ] && ok=1
    elif [ "$LAST_RC" -eq 0 ] && has "$TEST_ROOT/claude-args.log" "--model $expected_model" && has "$LAST_OUT" "role=$expected_role"; then
        if [ -n "$expected_effort" ]; then
            has "$TEST_ROOT/claude-args.log" "--effort $expected_effort" && ok=1
        else
            ! has "$TEST_ROOT/claude-args.log" '--effort' && has "$LAST_OUT" 'effort=unspecified' && ok=1
        fi
    fi
    expect_case "Claude effort arguments: $scenario" "$ok" "exit=$LAST_RC"
done


fi

if selected agy; then
fresh_case
target="$CASE_REPO/agy-output.txt"
run_capture agy-produced env STUB_ACTION="write:$target" HARNESS_RUN_ID=agyproduced bash "$AGY_RUN" -p prompt.txt -x "$target"
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" '^PRODUCED:' && state_field_is agyproduced exit 0 && ok=1
timing_ok "$LAST_OUT" 1 0 || ok=0
expect_case "agy expected output produced" "$ok" "exit=$LAST_RC"

fresh_case
target="$CASE_REPO/agy-missing.txt"
run_capture agy-missing env STUB_ACTION=none HARNESS_RUN_ID=agymissing bash "$AGY_RUN" -p prompt.txt -x "$target"
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^MISSING:' && ok=1
expect_case "agy missing expected output fails" "$ok" "exit=$LAST_RC"

fresh_case
target="$CASE_REPO/same-size.txt"
printf '%s\n' 'stub-output' > "$target"
run_capture agy-same-size env STUB_ACTION="delayed-write:$target" HARNESS_RUN_ID=agysamesize bash "$AGY_RUN" -p prompt.txt -x "$target"
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^PRODUCED:' && ok=1
expect_case "agy delayed same-size rewrite is produced on second-resolution stat" "$ok" "exit=$LAST_RC"


fi

if selected evidence; then
# Control-plane, scope, and verify gates.
fresh_case
run_capture codex-cp-block env STUB_ACTION=touch-control-plane HARNESS_RUN_ID=codexcp bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'CONTROL_PLANE_WARNING' && has "$LAST_OUT" 'STATUS: BLOCKED\(control-plane, was DONE\)' && ok=1
expect_case "codex control-plane edit blocks" "$ok" "exit=$LAST_RC"

# Recording hook trust rewrites the codex config, and that
# alone must not read as a settings change; any OTHER edit to the same
# file still must (normalize the section, do not drop
# the file from the hash).
fresh_case
mkdir -p "$CASE_HOME/.codex"
printf 'model = "stub"\n\n[hooks.state]\n' > "$CASE_HOME/.codex/config.toml"
run_capture codex-trust-quiet env STUB_ACTION=touch-codex-trust HARNESS_RUN_ID=codextrust bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && ! has "$LAST_OUT" 'CONTROL_PLANE_WARNING' && ok=1
expect_case "codex hook-trust record does not raise a control-plane alarm" "$ok" "exit=$LAST_RC"

fresh_case
mkdir -p "$CASE_HOME/.codex"
printf 'model = "stub"\n\n[hooks.state]\n' > "$CASE_HOME/.codex/config.toml"
# An out-of-repo settings file is reported (NOTICE with
# the path) but never BLOCKS — every real-use firing of the gate had been
# codex rewriting its own config.toml. The file stays in the hash, so the
# change is still VISIBLE; only the verdict changed.
run_capture codex-config-loud env STUB_ACTION=touch-codex-config HARNESS_RUN_ID=codexcfg bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CONTROL_PLANE_NOTICE.*config.toml' && ! has "$LAST_OUT" 'CONTROL_PLANE_WARNING' && ok=1
expect_case "any other codex config edit is reported as a notice, not a block" "$ok" "exit=$LAST_RC"

# An INDENTED header must end the normalized region: TOML allows the
# whitespace, and anchoring the terminator at column 0 let a project trust
# entry hide behind the hook-trust section — it must still be VISIBLE.
fresh_case
mkdir -p "$CASE_HOME/.codex"
printf 'model = "stub"\n\n[hooks.state]\ntrusted_hash = "sha256:x"\n' > "$CASE_HOME/.codex/config.toml"
run_capture codex-indented-loud env STUB_ACTION=touch-codex-indented HARNESS_RUN_ID=codexind bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CONTROL_PLANE_NOTICE.*config.toml' && ok=1
expect_case "an indented header after the trust section is not hidden" "$ok" "exit=$LAST_RC"

fresh_case
run_capture agy-cp-block env STUB_ACTION=touch-control-plane HARNESS_RUN_ID=agycp bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'CONTROL_PLANE_WARNING' && has "$LAST_OUT" 'STATUS: BLOCKED\(control-plane, was DONE\)' && ok=1
expect_case "agy control-plane edit blocks" "$ok" "exit=$LAST_RC"

# Review r12 BLOCKER 1: the launcher's own grant list lives OUTSIDE the
# repo, and the r11 "out-of-repo → NOTICE" demotion had swept it along.
# A delegate that widens its own grants must still BLOCK; only codex's
# self-rewritten config.toml is a notice.
# Legacy approval marker files are inert; scoped launcher grants still apply.
fresh_case
touch "$CASE_REPO/.claude/.control-plane-approved"
run_capture codex-stale-marker env HARNESS_RUN_ID=codexstale bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'STATUS: DONE' && ok=1
expect_case "legacy approval files do not gate authorized work" "$ok" "exit=$LAST_RC"

fresh_case
touch "$CASE_REPO/.claude/.control-plane-approved"
run_capture codex-marker-approved env HARNESS_ALLOW_CONTROL_PLANE=1 HARNESS_RUN_ID=codexmarkok bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && [ -e "$CASE_REPO/.claude/.control-plane-approved" ] && ok=1
expect_case "an approved run accepts an existing marker and leaves it" "$ok" "exit=$LAST_RC"

fresh_case
run_capture agy-self-grant env STUB_ACTION=touch-agy-settings HARNESS_RUN_ID=agygrant bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'CONTROL_PLANE_WARNING.*antigravity-cli/settings.json' && has "$LAST_OUT" 'STATUS: BLOCKED\(control-plane' && ok=1
expect_case "agy widening its own grant list blocks" "$ok" "exit=$LAST_RC"

fresh_case
run_capture codex-cp-approved env STUB_ACTION=touch-control-plane HARNESS_ALLOW_CONTROL_PLANE=1 HARNESS_RUN_ID=codexcpa bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CONTROL_PLANE_APPROVED' && has "$LAST_OUT" '^STATUS: DONE' && ok=1
expect_case "approved control-plane edit stays DONE" "$ok" "exit=$LAST_RC"

fresh_case
run_capture codex-cp-notice env STUB_ACTION=touch-settings-local HARNESS_RUN_ID=codexnotice bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CONTROL_PLANE_NOTICE' && has "$LAST_OUT" '^--- ' && has "$LAST_OUT" '^\+\+\+ ' && ok=1
expect_case "settings.local edit is notice with diff" "$ok" "exit=$LAST_RC"

fresh_case
run_capture codex-commit env STUB_ACTION=commit HARNESS_RUN_ID=codexcommit bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'SCOPE_WARNING: unauthorized commit' && has "$LAST_OUT" 'FAILED\(unauthorized-commit\)' && ok=1
expect_case "unauthorized commit fails" "$ok" "exit=$LAST_RC"

# r2 N1: a delegate that rewrites the original or any TMPDIR copy of an
# `exit 23` verifier must still end in failed verification.
for action in none tamper-verify tamper-verify-tmp; do
    fresh_case
    case_tmp="$TEST_ROOT/codex-verify-tmp-$action"; rm -rf "$case_tmp"; mkdir -p "$case_tmp"
    printf 'exit 23\n' > "$CASE_REPO/verify.sh"
    run_capture "codex-verify-$action" env STUB_ACTION="$action" STUB_VERIFY_PATH="$CASE_REPO/verify.sh" TMPDIR="$case_tmp" \
        HARNESS_RUN_ID=codexverify bash "$CODEX_RUN" -p prompt.txt -v verify.sh
    ok=0
    if [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(verification' && has "$LAST_OUT" 'codex_exit=0'; then
        if [ "$action" = tamper-verify ]; then
            has "$LAST_OUT" 'VERIFY_INTEGRITY_FAILED' && ok=1
        else
            has "$LAST_OUT" '^VERIFY: exit 23' && ok=1
        fi
    fi
    expect_case "Codex captured verifier survives $action" "$ok" "exit=$LAST_RC"
done

fresh_case
printf 'printf ORIGINAL_VERIFIER\nexit 0\n' > "$CASE_REPO/verify.sh"
run_capture codex-verify-pass env STUB_ACTION=none HARNESS_RUN_ID=codexverifypass bash "$CODEX_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" 'ORIGINAL_VERIFIER' && ok=1
expect_case "Codex intact passing verifier succeeds" "$ok" "exit=$LAST_RC"

fresh_case
write_escape_verifier
run_capture codex-verify-escapes env STUB_ACTION=none HARNESS_RUN_ID=codexescapes bash "$CODEX_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$ESCAPE_DIRECT_RC" -eq 31 ] && has "$LAST_OUT" '^VERIFY: exit 31$' && ok=1
expect_case "Codex verifier text keeps backslashes, quotes and Korean" "$ok" "direct=$ESCAPE_DIRECT_RC exit=$LAST_RC"

fresh_case
printf 'exit 7\n' > "$CASE_REPO/verify.sh"
run_capture codex-verify-fail env STUB_ACTION=none HARNESS_RUN_ID=codexverifyfail bash "$CODEX_RUN" -p prompt.txt -v verify.sh
ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^STATUS: FAILED\(verification' && has "$LAST_OUT" '^VERIFY: exit 7' && state_field_is codexverifyfail exit 1 && ok=1
timing_ok "$LAST_OUT" 1 1 || ok=0
expect_case "Codex success cannot override a failed task verifier" "$ok" "exit=$LAST_RC"


fi

if selected lifecycle; then
# The parent is gone, but the native Claude child still owns this write run.
for observation in live dead reused; do
    fresh_case
    cat > "$CASE_REPO/identity.sh" <<'IDENTITY'
TOOL=claude
SANDBOX=workspace-write
. "$1"
RUN_ID=old
LAUNCHER_PID=111111
CHILD_PID=222222
LAUNCHER_STIME=10:00:00
CHILD_STIME=10:00:00
STARTED_EPOCH=1
state_write running "" "worker still alive"
pid_alive() { [ "$1" = 222222 ] && [ "$OBSERVATION" != dead ]; }
pid_stime() { if [ "$OBSERVATION" = reused ]; then echo 11:00:00; else echo 10:00:00; fi; }
pid_comm() { echo claude; }
classify_record "$(state_file old)"
echo "CLASSIFIED=$RS_STATE"
RUN_ID=new
stale_run_check
echo WRITER_ADMITTED
IDENTITY
    # Sources run-state.sh directly (no launcher prologue): use this suite's bash >= 4.
    run_capture native-claude-identity env OBSERVATION="$observation" "$BASH" identity.sh "$RUN_STATE"
    ok=0
    if [ "$observation" = live ]; then
        [ "$LAST_RC" -eq 5 ] && has "$LAST_OUT" 'CLASSIFIED=running' && has "$LAST_OUT" 'HARNESS_BUSY' && ok=1
    else
        [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CLASSIFIED=aborted' && has "$LAST_OUT" 'WRITER_ADMITTED' && ok=1
    fi
    expect_case "native Claude child identity: $observation" "$ok" "exit=$LAST_RC"
done
# An absent/non-GNU timeout must refuse before foreground or detached launch.
mkdir -p "$TEST_ROOT/no-timeout"
printf '#!/usr/bin/env bash\nexit 1\n' > "$TEST_ROOT/no-timeout/timeout"
chmod +x "$TEST_ROOT/no-timeout/timeout"
for launcher in "$CODEX_RUN" "$CLAUDE_RUN"; do
    for mode in foreground detached; do
        fresh_case
        args=(); [ "$mode" != detached ] || args=(-b)
        run_capture no-timeout env PATH="$TEST_ROOT/no-timeout:$STUB_BIN:$ORIGINAL_PATH" bash "$launcher" -p prompt.txt "${args[@]}"
        ok=0
        [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'GNU coreutils timeout is required' && ! has "$LAST_OUT" '^DETACHED:' && ok=1
        expect_case "$(basename "$launcher") refuses $mode without GNU timeout" "$ok" "exit=$LAST_RC"
    done
done

# Busy guard: live, cross-tool, read-only exemptions, dead cleanup.
fresh_case
start_live_shell
plant_record liveguard "$LIVE_PID" workspace-write
run_capture busy-codex env STUB_ACTION=none HARNESS_RUN_ID=blockedcodex bash "$CODEX_RUN" -p prompt.txt
busy_codex=$LAST_RC; out_busy_codex=$LAST_OUT
run_capture busy-agy env STUB_ACTION=none HARNESS_RUN_ID=blockedagy bash "$AGY_RUN" -p prompt.txt
busy_agy=$LAST_RC; out_busy_agy=$LAST_OUT
run_capture busy-readonly env STUB_ACTION=none HARNESS_RUN_ID=readonlystart bash "$CODEX_RUN" -p prompt.txt -s read-only
readonly_rc=$LAST_RC
run_capture forget-liveguard env HARNESS_ALLOW_FORGET=1 bash "$CODEX_RUN" --forget liveguard
plant_record readonlyguard "$LIVE_PID" read-only
run_capture workspace-beside-readonly env STUB_ACTION=none HARNESS_RUN_ID=workspacebeside bash "$CODEX_RUN" -p prompt.txt
beside_rc=$LAST_RC
plant_record deadguard "$LIVE_PID" workspace-write
stop_live_shell "$LIVE_PID"
run_capture dead-status bash "$CODEX_RUN" --status deadguard
dead_rc=$LAST_RC; dead_out=$LAST_OUT
run_capture stale-cleaned env STUB_ACTION=none HARNESS_RUN_ID=afterdead bash "$CODEX_RUN" -p prompt.txt
clean_rc=$LAST_RC; clean_out=$LAST_OUT
ok=0
if [ "$busy_codex" -eq 5 ] && has "$out_busy_codex" 'HARNESS_BUSY: STALE_RUN' \
   && [ "$busy_agy" -eq 5 ] && has "$out_busy_agy" 'HARNESS_BUSY: STALE_RUN' \
   && [ "$readonly_rc" -eq 0 ] && [ "$beside_rc" -eq 0 ] \
   && [ "$dead_rc" -eq 1 ] && has "$dead_out" '^STATE: aborted' \
   && [ "$clean_rc" -eq 0 ] && has "$clean_out" 'STALE_RUN_CLEANED: earlier run\(s\) deadguard'; then ok=1; fi
LAST_OUT=$clean_out
expect_case "stale-run guard, exemptions, and dead cleanup" "$ok" "busy codex=$busy_codex agy=$busy_agy readonly=$readonly_rc beside=$beside_rc dead=$dead_rc clean=$clean_rc"

fresh_case
start_live_shell
plant_record forgetguard "$LIVE_PID" workspace-write
run_capture forget-approved env HARNESS_ALLOW_FORGET=1 bash "$AGY_RUN" --forget forgetguard
forget_rc=$LAST_RC
sleep_alive=0; kill -0 "$LIVE_PID" 2>/dev/null && sleep_alive=1
run_capture forget-released env STUB_ACTION=none HARNESS_RUN_ID=afterforget bash "$AGY_RUN" -p prompt.txt
released_rc=$LAST_RC
ok=0; [ "$forget_rc" -eq 0 ] && [ "$sleep_alive" -eq 1 ] && [ "$released_rc" -eq 0 ] && state_field_is forgetguard state aborted && ok=1
expect_case "approved forget aborts record without killing process" "$ok" "forget=$forget_rc alive=$sleep_alive released=$released_rc"
stop_live_shell "$LIVE_PID"

# Detached lifecycle.
fresh_case
run_capture detach-start env STUB_ACTION=sleep:3 HARNESS_RUN_ID=detachrun bash "$CODEX_RUN" -p prompt.txt -b -e max
detach_rc=$LAST_RC; detach_out=$LAST_OUT
run_capture detach-status bash "$CODEX_RUN" --status detachrun
status_rc=$LAST_RC; status_out=$LAST_OUT
run_capture detach-wait bash "$CODEX_RUN" --wait detachrun -t 60
wait_rc=$LAST_RC; wait_out=$LAST_OUT
ok=0
if [ "$detach_rc" -eq 0 ] && has "$detach_out" '^RUN_ID: detachrun$' && has "$detach_out" '^WAIT:' \
   && [ "$status_rc" -eq 6 ] && has "$status_out" '^STATE: (running|starting)' \
   && [ "$wait_rc" -eq 0 ] && has "$wait_out" '^STATUS: DONE' && state_field_is detachrun state done; then ok=1; fi
LAST_OUT=$wait_out
timing_ok "$LAST_OUT" 1 0 || ok=0
expect_case "detached max run status and wait lifecycle" "$ok" "detach=$detach_rc status=$status_rc wait=$wait_rc"


fi

if selected codex; then
# Bindings resolution (-m/-e defaults) and worker effort guards.
# write_bindings <path> <ladder0-model-json> <ladder0-effort-json>: a
# minimal bindings file with the two role entry points the launcher reads.
write_bindings() {
    printf '%s\n' "{\"roles\":{\"implement\":{\"ladder\":[{\"vendor\":\"openai\",\"model\":$2,\"effort\":$3}]},\"image_verify\":{\"default\":{\"vendor\":\"openai\",\"model\":\"gpt-5.6-terra\",\"effort\":\"medium\"}}}}" > "$1"
}
codex_args() { cat "$TEST_ROOT/codex-args.log" 2>/dev/null; }

fresh_case
rm -f "$TEST_ROOT/codex-args.log"
run_capture bindings-builtin env HARNESS_RUN_ID=bindbuiltin bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: builtin \(no ' && codex_args | grep -q 'model_reasoning_effort=high' && codex_args | grep -q -- '-m gpt-5.6-sol' && ok=1
expect_case "codex bindings: no file falls back to builtin Terra/high" "$ok" "exit=$LAST_RC"

fresh_case
rm -f "$TEST_ROOT/codex-args.log"
write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-5.6-terra"' '"high"'
printf '%s\n' '{"roles":{"implement":{"ladder":[{"vendor":"openai","model":"gpt-5.6-terra","effort":"low"}]}}}' > "$CASE_REPO/.claude/model-bindings.local.json"
run_capture bindings-local env HARNESS_RUN_ID=bindlocal bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: public\+local role=implement model=gpt-5.6-terra effort=low$' && codex_args | grep -q 'model_reasoning_effort=low' && ok=1
expect_case "codex bindings: local ladder overrides public" "$ok" "exit=$LAST_RC"

fresh_case
rm -f "$TEST_ROOT/codex-args.log"
write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-5.6-terra"' '"high"'
printf 'not-really-a-png\n' > "$CASE_REPO/shot.png"
run_capture bindings-image env HARNESS_RUN_ID=bindimage bash "$CODEX_RUN" -p prompt.txt -i shot.png
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: public role=image_verify model=gpt-5.6-terra effort=medium$' && codex_args | grep -q 'model_reasoning_effort=medium' && codex_args | grep -q -- '-i shot.png' && ok=1
expect_case "codex bindings: -i selects the image_verify default" "$ok" "exit=$LAST_RC"

fresh_case
rm -f "$TEST_ROOT/codex-args.log"
write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-5.6-terra"' '"high"'
run_capture bindings-explicit env HARNESS_RUN_ID=bindexplicit bash "$CODEX_RUN" -p prompt.txt -m custom-model
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: public role=implement model=custom-model \(explicit\) effort=high$' && codex_args | grep -q -- '-m custom-model' && codex_args | grep -q 'model_reasoning_effort=high' && ok=1
expect_case "codex bindings: explicit -m wins, -e still from bindings" "$ok" "exit=$LAST_RC"

fresh_case
rm -f "$TEST_ROOT/codex-args.log"
printf '%s\n' '{' > "$CASE_REPO/.claude/model-bindings.json"
run_capture bindings-malformed env HARNESS_RUN_ID=bindbroken bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: builtin \(bindings unusable: ERR JSONDecodeError' && codex_args | grep -q 'model_reasoning_effort=high' && ok=1
expect_case "codex bindings: malformed JSON falls back to builtin" "$ok" "exit=$LAST_RC"

schema_ok=1
for bad in '""' '42' '"has space"'; do
    fresh_case
    rm -f "$TEST_ROOT/codex-args.log"
    write_bindings "$CASE_REPO/.claude/model-bindings.json" "$bad" '"high"'
    run_capture "bindings-schema-$CASE_NO" env HARNESS_RUN_ID="bindschema$CASE_NO" bash "$CODEX_RUN" -p prompt.txt
    [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: builtin \(bindings unusable: ERR ValueError model' && codex_args | grep -q -- '-m gpt-5.6-sol' || schema_ok=0
done
fresh_case
rm -f "$TEST_ROOT/codex-args.log"
write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-5.6-terra"' '"turbo"'
run_capture bindings-schema-effort env HARNESS_RUN_ID=bindschemaeffort bash "$CODEX_RUN" -p prompt.txt
[ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^BINDINGS: builtin \(bindings unusable: ERR ValueError effort' && codex_args | grep -q 'model_reasoning_effort=high' || schema_ok=0
expect_case "codex bindings: schema-invalid model/effort falls back to builtin" "$schema_ok" "exit=$LAST_RC"

guard_ok=1
for eff in max; do
    fresh_case
    rm -f "$TEST_ROOT/codex-args.log"
    run_capture "guard-$eff" bash "$CODEX_RUN" -p prompt.txt -e "$eff"
    # Regression: the explicit branch once printed "the -e flag1".
    [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" "HARNESS_DENIED: -e $eff requires -b" && has "$LAST_OUT" 'effort came from the -e flag)' && [ ! -e "$TEST_ROOT/codex-args.log" ] || guard_ok=0
done
expect_case "codex max without -b is denied before any codex call" "$guard_ok" "exit=$LAST_RC"

# Ultra enables subagents: detached/resumed/image runs are still workers.
ultra_ok=1
for mode in foreground detached resume image; do
    fresh_case
    rm -f "$TEST_ROOT/codex-args.log"
    args=()
    case "$mode" in
        detached) args=(-b) ;;
        resume) args=(-b -r last) ;;
        image) printf 'stub image\n' > "$CASE_REPO/shot.png"; args=(-b -i shot.png) ;;
    esac
    run_capture "guard-ultra-$mode" bash "$CODEX_RUN" -p prompt.txt -e ultra "${args[@]}"
    [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED: Codex worker ultra enables re-delegation' && [ ! -e "$TEST_ROOT/codex-args.log" ] || ultra_ok=0
done
expect_case "codex worker ultra is denied before launch in every mode" "$ultra_ok" "exit=$LAST_RC"

ultra_ok=1
for source in public local image; do
    fresh_case
    rm -f "$TEST_ROOT/codex-args.log"
    write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-6-astra"' '"ultra"'
    args=(-b)
    if [ "$source" = local ]; then
        write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-6-astra"' '"medium"'
        write_bindings "$CASE_REPO/.claude/model-bindings.local.json" '"gpt-6-astra"' '"ultra"'
    elif [ "$source" = image ]; then
        printf '%s\n' '{"roles":{"image_verify":{"default":{"model":"gpt-6-astra","effort":"ultra"}}}}' > "$CASE_REPO/.claude/model-bindings.local.json"
        printf 'stub image\n' > "$CASE_REPO/shot.png"
        args+=(-i shot.png)
    fi
    run_capture "guard-ultra-$source" bash "$CODEX_RUN" -p prompt.txt "${args[@]}"
    [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED: Codex worker ultra enables re-delegation' && has "$LAST_OUT" '^BINDINGS: public.*effort=ultra$' && [ ! -e "$TEST_ROOT/codex-args.log" ] || ultra_ok=0
done
expect_case "codex bindings-sourced ultra is denied without a silent builtin fallback" "$ultra_ok" "exit=$LAST_RC"

# The guard also fires on an effort that CAME FROM the bindings (no -e).
fresh_case
rm -f "$TEST_ROOT/codex-args.log"
write_bindings "$CASE_REPO/.claude/model-bindings.json" '"gpt-5.6-terra"' '"max"'
run_capture guard-bindings-max bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'HARNESS_DENIED: -e max requires -b.*from the bindings: public' && has "$LAST_OUT" '^BINDINGS: public role=implement model=gpt-5.6-terra effort=max$' && [ ! -e "$TEST_ROOT/codex-args.log" ] && ok=1
expect_case "codex bindings-sourced max without -b is denied and names the source" "$ok" "exit=$LAST_RC"


fi

if selected core; then
# run-state.sh (sourced first) classifies infrastructure conditions as
# UNAVAILABLE (exit 2) so they trigger the launcher fallback instead of
# reading as a policy denial. A missing or Store-alias
# python therefore never reaches the bindings step; the launcher's
# bash-side token re-validation is defense in depth only. bash < 4 is the
# one infrastructure branch NOT exercised here (no dev box has bash 3:
# Git Bash 5.x, WSL 5.3) — verified by reading; check-posix.sh's
# bash-version WARN is the operational guard.
STUB_PY_BIN="$TEST_ROOT/stub-py-bin"
mkdir -p "$STUB_PY_BIN"
for py in python python3; do
    # Store alias stub behaviour: resolves on PATH, prints nothing, exit 9009.
    printf '#!/usr/bin/env bash\nexit 9009\n' > "$STUB_PY_BIN/$py"
    chmod +x "$STUB_PY_BIN/$py"
done
fresh_case
rm -f "$TEST_ROOT/codex-args.log" "$TEST_ROOT/agy-args.log"
stub_codex_out="$TEST_ROOT/rs-stub-py-codex.out"
(
    cd "$CASE_REPO" || exit 99
    env HOME="$CASE_HOME" PATH="$STUB_PY_BIN:$STUB_BIN:$ORIGINAL_PATH" FIXTURE_ROOT="$TEST_ROOT" bash "$CODEX_RUN" -p prompt.txt
) > "$stub_codex_out" 2>&1
stub_codex_rc=$?
LAST_OUT="$TEST_ROOT/rs-stub-py-agy.out"
(
    cd "$CASE_REPO" || exit 99
    env HOME="$CASE_HOME" PATH="$STUB_PY_BIN:$STUB_BIN:$ORIGINAL_PATH" FIXTURE_ROOT="$TEST_ROOT" bash "$AGY_RUN" -p prompt.txt
) > "$LAST_OUT" 2>&1
LAST_RC=$?
ok=0; [ "$stub_codex_rc" -eq 2 ] && has "$stub_codex_out" '^CODEX_UNAVAILABLE: run-state: python on PATH .* did not run a script' \
    && [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" '^AGY_UNAVAILABLE: run-state: python on PATH .* did not run a script' \
    && [ ! -e "$TEST_ROOT/codex-args.log" ] && [ ! -e "$TEST_ROOT/agy-args.log" ] && ok=1
expect_case "run-state: Store-alias python stub is UNAVAILABLE (exit 2) for both launchers" "$ok" "codex=$stub_codex_rc agy=$LAST_RC"

fresh_case
LAST_OUT="$TEST_ROOT/rs-nohome.out"
(
    cd "$CASE_REPO" || exit 99
    env HOME= PATH="$STUB_BIN:$ORIGINAL_PATH" FIXTURE_ROOT="$TEST_ROOT" bash "$CODEX_RUN" -p prompt.txt
) > "$LAST_OUT" 2>&1
LAST_RC=$?
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" '^CODEX_UNAVAILABLE: run-state: needs python .* and HOME set' && ok=1
expect_case "run-state: empty HOME is UNAVAILABLE (exit 2)" "$ok" "exit=$LAST_RC"

fresh_case
run_capture rs-badkey env HARNESS_TREE_KEY='bad key!' bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" '^HARNESS_DENIED: bad HARNESS_TREE_KEY' && ok=1
expect_case "run-state: explicit malformed HARNESS_TREE_KEY stays HARNESS_DENIED (exit 4)" "$ok" "exit=$LAST_RC"

fresh_case
printf 'x' > "$CASE_HOME/not-a-dir"
run_capture rs-statedir env HARNESS_STATE_DIR="$CASE_HOME/not-a-dir" bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" '^HARNESS_DENIED: cannot create' && ok=1
expect_case "run-state: uncreatable STATE_DIR stays HARNESS_DENIED (exit 4)" "$ok" "exit=$LAST_RC"


fi

if selected lifecycle; then
fresh_case
run_capture max-detach env STUB_ACTION=sleep:1 HARNESS_RUN_ID=maxdetach bash "$CODEX_RUN" -p prompt.txt -e max -b
maxd_rc=$LAST_RC; maxd_out=$LAST_OUT
run_capture max-wait bash "$CODEX_RUN" --wait maxdetach -t 60
ok=0; [ "$maxd_rc" -eq 0 ] && has "$maxd_out" '^RUN_ID: maxdetach$' && [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" '^STATUS: DONE.*effort=max' && has "$LAST_OUT" 'effort=max \(explicit\)' && ok=1
expect_case "codex max with -b runs detached and completes" "$ok" "detach=$maxd_rc wait=$LAST_RC"

# Signal forwarding and aborted state. Skip only when the platform cannot
# deliver/observe POSIX signals through its Bash process layer.
fresh_case
signal_out="$TEST_ROOT/signal.out"
rm -f "$TEST_ROOT/stub-child.pid"
(
    cd "$CASE_REPO" || exit 99
    exec env HOME="$CASE_HOME" PATH="$STUB_BIN:$ORIGINAL_PATH" FIXTURE_ROOT="$TEST_ROOT" \
        STUB_ACTION=sleep:60 HARNESS_RUN_ID=signalrun bash "$CODEX_RUN" -p prompt.txt
) > "$signal_out" 2>&1 &
launcher_pid=$!
BG_PIDS+=("$launcher_pid")
for _ in {1..100}; do [ -s "$TEST_ROOT/stub-child.pid" ] && break; sleep 0.1; done
if [ ! -s "$TEST_ROOT/stub-child.pid" ]; then
    LAST_OUT=$signal_out
    skip_case "TERM aborts run and kills stub child" "stub child PID was not observable"
    kill -TERM "$launcher_pid" 2>/dev/null || true
    wait "$launcher_pid" 2>/dev/null || true
else
    stub_pid=$(tr -d '\r\n' < "$TEST_ROOT/stub-child.pid")
    kill -TERM "$launcher_pid" 2>/dev/null
    wait "$launcher_pid" 2>/dev/null
    signal_rc=$?
    child_gone=0; kill -0 "$stub_pid" 2>/dev/null || child_gone=1
    LAST_OUT=$signal_out
    ok=0
    if [ "$signal_rc" -eq 143 ] && state_field_is signalrun state aborted && state_field_is signalrun exit 143 && [ "$child_gone" -eq 1 ]; then ok=1; fi
    if [ "$child_gone" -ne 1 ] && case "${OS:-}:$(uname -s 2>/dev/null)" in Windows_NT:*|*:MINGW*|*:MSYS*|*:CYGWIN*) true;; *) false;; esac; then
        skip_case "TERM aborts run and kills stub child" "MSYS could not verify native child termination"
    else
        expect_case "TERM aborts run and kills stub child" "$ok" "launcher=$signal_rc child_gone=$child_gone"
    fi
fi

# GNU timeout path.
if timeout --version 2>/dev/null | grep -qi coreutils; then
    fresh_case
    run_capture codex-timeout env STUB_ACTION=sleep:10 HARNESS_RUN_ID=codextimeout bash "$CODEX_RUN" -p prompt.txt -t 2
    ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" '^TIMEOUT:' && has "$LAST_OUT" 'codex_exit=124' && ok=1
    expect_case "GNU timeout maps to codex exit 124" "$ok" "exit=$LAST_RC"
else
    skip_case "GNU timeout maps to codex exit 124" "GNU coreutils timeout is not first on PATH"
fi


fi

if selected agy; then
# agy result mapping and retry count.
fresh_case
run_capture agy-quota env STUB_ACTION=error:quota HARNESS_RUN_ID=agyquota bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 2 ] && has "$LAST_OUT" '^AGY_ERROR:.*quota' && has "$LAST_OUT" '^STATUS: AGY_UNAVAILABLE' && ok=1
expect_case "agy quota error is unavailable" "$ok" "exit=$LAST_RC"

fresh_case
rm -f "$TEST_ROOT/agy-args.log"
run_capture agy-nojson env STUB_ACTION=nojson HARNESS_RUN_ID=agynojson bash "$AGY_RUN" -p prompt.txt
invocations=$(wc -l < "$TEST_ROOT/agy-args.log" 2>/dev/null || echo 0)
ok=0; [ "$LAST_RC" -eq 2 ] && [ "$invocations" -eq 2 ] && has "$LAST_OUT" 'attempts=2' && ok=1
timing_ok "$LAST_OUT" 2 0 || ok=0
expect_case "agy invalid JSON retries exactly once" "$ok" "exit=$LAST_RC invocations=$invocations"

fresh_case
run_capture agy-empty env STUB_ACTION=empty HARNESS_RUN_ID=agyempty bash "$AGY_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 1 ] && ! has "$LAST_OUT" '^STATUS: DONE' && has "$LAST_OUT" 'attempts=2' && ok=1
expect_case "agy empty response is not DONE" "$ok" "exit=$LAST_RC"



fi

if selected nongit; then
# Non-Git support retains evidence, raw CLI policy, and shared run records.
for launcher in "$CODEX_RUN" "$CLAUDE_RUN" "$AGY_RUN"; do
    name=$(basename "$launcher" .sh)
    for action in none write:new.txt write:existing.txt remove:existing.txt touch-control-plane init-git; do
        fresh_case nongit
        printf 'old-content\n' > "$CASE_REPO/existing.txt"
        run_capture "nongit-$name-${action//:/_}" env STUB_ACTION="$action" bash "$launcher" -p prompt.txt
        ok=0
        case "$action" in
            none) [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CHANGED: none' && ok=1 ;;
            write:*) [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" "CHANGED: .*${action#write:}" && ok=1 ;;
            remove:*) [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CHANGED: .*existing.txt' && ok=1 ;;
            touch-control-plane) [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'BLOCKED.control-plane' && ok=1 ;;
            init-git) [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'workspace evidence unavailable' && has "$LAST_OUT" 'CHANGED: unknown' && ok=1 ;;
        esac
        has "$LAST_OUT" 'WORKSPACE: .*"mode": "files"' || ok=0
        expect_case "non-Git $name $action" "$ok" "exit=$LAST_RC"
    done
    fresh_case
    run_capture "broken-after-$name" env STUB_ACTION=break-index bash "$launcher" -p prompt.txt
    ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'workspace evidence unavailable' && ok=1
    expect_case "$name cannot report success after Git inspection fails" "$ok" "exit=$LAST_RC"
    fresh_case nongit
    mkdir "$CASE_REPO/.git"
    run_capture "broken-before-$name" bash "$launcher" -p prompt.txt
    ok=0; [ "$LAST_RC" -eq 4 ] && has "$LAST_OUT" 'cannot identify the workspace root' && ok=1
    expect_case "$name refuses corrupt Git instead of falling back" "$ok" "exit=$LAST_RC"
done

for launcher in "$CODEX_RUN" "$CLAUDE_RUN"; do
    fresh_case nongit
    run_capture "nongit-readonly-$(basename "$launcher")" env STUB_ACTION=write:app.txt bash "$launcher" -s read-only -p prompt.txt
    ok=0; [ "$LAST_RC" -eq 1 ] && has "$LAST_OUT" 'read-only workspace changed' && ok=1
    expect_case "non-Git $(basename "$launcher") detects writes in read-only mode" "$ok" "exit=$LAST_RC"
done

fresh_case nongit
: > "$TEST_ROOT/codex-args.log"
run_capture nongit-codex-resume bash "$CODEX_RUN" -r last -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && has "$TEST_ROOT/codex-args.log" 'exec resume.*--skip-git-repo-check' && ok=1
expect_case "Codex non-Git resume gets the internal repository option" "$ok" "exit=$LAST_RC"
fresh_case
: > "$TEST_ROOT/codex-args.log"
run_capture git-codex-no-skip bash "$CODEX_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 0 ] && ! has "$TEST_ROOT/codex-args.log" 'skip-git-repo-check' && ok=1
expect_case "Codex Git runs do not receive the non-Git option" "$ok" "exit=$LAST_RC"

fresh_case nongit
run_capture nongit-root-record bash "$CLAUDE_RUN" -p prompt.txt
rid=$(sed -n 's/^RUN_ID: \([^ ]*\).*/\1/p' "$LAST_OUT" | head -n 1)
mkdir -p "$CASE_REPO/src/nested"
CASE_REPO="$CASE_REPO/src/nested"
run_capture nongit-subdir-status bash "$CLAUDE_RUN" --status "$rid"
ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'STATUS: DONE' && ok=1
expect_case "non-Git subdirectory status finds the root run record" "$ok" "exit=$LAST_RC"

# Follow-up cases for the common evidence adapter and retry decision.
for launcher in "$CODEX_RUN" "$CLAUDE_RUN"; do
    fresh_case nongit
    run_capture "nongit-notice-$(basename "$launcher")" env STUB_ACTION=notice bash "$launcher" -s read-only -p prompt.txt
    ok=0; [ "$LAST_RC" -eq 0 ] && has "$LAST_OUT" 'CONTROL_PLANE_NOTICE' && ok=1
    expect_case "non-Git $(basename "$launcher") keeps session updates as notices" "$ok" "exit=$LAST_RC"
done

for mode in git nongit; do
    for action in nojson nojson-write nojson-delete nojson-external; do
        fresh_case "$mode"
        printf 'before' > "$CASE_REPO/app.txt"
        : > "$TEST_ROOT/agy-args.log"
        expected="$TEST_ROOT/retry-$CASE_NO.txt"
        run_capture "retry-$mode-$action" env STUB_ACTION="$action" STUB_EXPECTED="$expected" bash "$AGY_RUN" -p prompt.txt -x "$expected"
        invocations=$(wc -l < "$TEST_ROOT/agy-args.log" | tr -d ' ')
        ok=0
        if [ "$action" = nojson ]; then
            [ "$LAST_RC" -eq 2 ] && [ "$invocations" -eq 2 ] && ok=1
        else
            [ "$LAST_RC" -eq 1 ] && [ "$invocations" -eq 1 ] && has "$LAST_OUT" 'STATUS: FAILED' && ok=1
        fi
        expect_case "agy $mode retry gate $action" "$ok" "exit=$LAST_RC invocations=$invocations"
    done
done
fresh_case nongit
: > "$TEST_ROOT/agy-args.log"
run_capture retry-broken-git env STUB_ACTION=nojson-break-git bash "$AGY_RUN" -p prompt.txt
invocations=$(wc -l < "$TEST_ROOT/agy-args.log" | tr -d ' ')
ok=0; [ "$LAST_RC" -eq 1 ] && [ "$invocations" -eq 1 ] && has "$LAST_OUT" 'workspace evidence unavailable' && ok=1
expect_case "agy never retries when starting evidence cannot be compared" "$ok" "exit=$LAST_RC invocations=$invocations"

fresh_case nongit
start_live_shell
plant_record nongitbusy "$LIVE_PID" workspace-write
run_capture nongit-busy bash "$CLAUDE_RUN" -p prompt.txt
ok=0; [ "$LAST_RC" -eq 5 ] && has "$LAST_OUT" 'HARNESS_BUSY: STALE_RUN' && ok=1
expect_case "non-Git cross-tool writer shares the busy guard" "$ok" "exit=$LAST_RC"
stop_live_shell "$LIVE_PID"


fi


echo
if [ "$FAILS" -eq 0 ]; then
    if [ "$TEST_GROUPS" = all ]; then echo "ALL PASS / $TOTAL cases"
    else echo "SELECTED PASS / $TOTAL cases (groups: $TEST_GROUPS)"; fi
    exit 0
else
    echo "$FAILS FAILURES / $TOTAL cases"
    exit 1
fi
