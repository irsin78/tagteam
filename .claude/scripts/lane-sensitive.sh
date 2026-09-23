#!/usr/bin/env bash
# Optional WSL lane. Probe two specific restrictions through Claude's actual
# Bash tool results. This is a smoke check, not proof of universal isolation.
set -u
HARDENED=".claude/sandbox-sensitive.json"
[ -f "$HARDENED" ] || { echo "lane-sensitive: hardened settings missing" >&2; exit 2; }
CLAUDE=$(command -v claude || true)
[ -n "$CLAUDE" ] || CLAUDE="$HOME/.local/bin/claude"
[ -x "$CLAUDE" ] || { echo "lane-sensitive: claude binary not found" >&2; exit 2; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] && command -v curl >/dev/null 2>&1 && timeout --version 2>/dev/null | grep -qi coreutils || {
    echo "lane-sensitive: REFUSING — working Python, curl and GNU timeout are required" >&2; exit 2;
}
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || { echo "lane-sensitive: REFUSING — HOME unavailable" >&2; exit 2; }
# Establish that this path is writable outside the sandbox before interpreting
# a denied write. Keep one unique canary and remove only that file.
CANARY=$(mktemp "$HOME/lane-sensitive-preflight.XXXXXX") || exit 2
PROBE=$(mktemp "${TMPDIR:-/tmp}/lane-sensitive-output.XXXXXX") || { rm -f -- "$CANARY"; exit 2; }
PROBE_ERR="$PROBE.stderr"
trap 'rm -f -- "$CANARY" "$PROBE" "$PROBE_ERR"' EXIT
rm -f -- "$CANARY" || exit 2
printf -v WRITE_COMMAND 'printf x > %q' "$CANARY"
NET_COMMAND='curl -sS --max-time 8 -D - https://api.github.com -o /dev/null'
echo "lane-sensitive: checking one proxy denial and one outside write denial..." >&2
printf '%s\n' \
    'Run exactly these two Bash commands once each, without changing them or bypassing the sandbox. Do not run other tools. Report when done.' \
    "1. $NET_COMMAND" "2. $WRITE_COMMAND" \
    | timeout -k 10 180 "$CLAUDE" -p --model haiku --tools Bash --allowedTools Bash \
        --settings "$HARDENED" --output-format stream-json --verbose > "$PROBE" 2> "$PROBE_ERR"
PROBE_EXIT=$?
if [ "$PROBE_EXIT" -ne 0 ]; then
    echo "lane-sensitive: REFUSING — probe process failed (exit $PROBE_EXIT):" >&2
    tail -6 "$PROBE_ERR" >&2
    exit 1
fi
if [ -e "$CANARY" ]; then
    echo "lane-sensitive: REFUSING — outside write succeeded" >&2
    exit 1
fi
# Inspect runtime tool results, not the assistant's final claim. Unknown output,
# DNS/TLS/timeouts, skipped commands and generic 403 responses are inconclusive.
"$PY" - "$PROBE" "$NET_COMMAND" "$WRITE_COMMAND" <<'PY'
import json, re, sys
calls, results, completed = {}, {}, False
try:
    for line in open(sys.argv[1], encoding='utf-8'):
        event = json.loads(line)
        if event.get('type') == 'result':
            completed = event.get('subtype') == 'success' and not event.get('is_error')
        role = event.get('type')
        for block in event.get('message', {}).get('content', []) if role in ('assistant', 'user') else []:
            if role == 'assistant' and block.get('type') == 'tool_use':
                args = block.get('input', {})
                command = args.get('command')
                if block.get('name') != 'Bash' or command not in sys.argv[2:] or args.get('dangerouslyDisableSandbox'):
                    raise ValueError('unexpected or unsandboxed command')
                calls[block['id']] = command
            if role == 'user' and block.get('type') == 'tool_result':
                command = calls[block['tool_use_id']]
                if command in results: raise ValueError('duplicate probe result')
                content = block.get('content', '')
                results[command] = content if isinstance(content, str) else '\n'.join(b['text'] for b in content if b.get('type') == 'text')
    if not completed or len(calls) != 2 or len(results) != 2:
        raise ValueError('missing successful run or actual tool results')
    network, write = (results[command] for command in sys.argv[2:])
    # Anthropic sandbox-runtime's HTTP allowlist refusal, not just curl != 0.
    if not re.search(r'(?im)^HTTP/1\.[01] 403 Forbidden\s*$', network) or not re.search(r'(?im)^X-Proxy-Error: blocked-by-allowlist\s*$', network):
        raise ValueError('network result lacks a sandbox allowlist denial')
    if not re.search(r'Permission denied|Read-only file system', write):
        raise ValueError('write result lacks an OS denial')
except (OSError, ValueError, KeyError, TypeError) as exc:
    print('lane-sensitive: REFUSING — inconclusive probe: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
[ "$?" -eq 0 ] || exit 1
echo "lane-sensitive: two restriction probes passed; this does not prove all egress paths are closed. Launching..." >&2
rm -f -- "$PROBE" "$PROBE_ERR"
exec "$CLAUDE" --settings "$HARDENED" "$@"
