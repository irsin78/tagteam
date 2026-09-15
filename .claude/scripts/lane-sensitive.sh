#!/usr/bin/env bash
# Launch a sensitive-work Claude session in the WSL2 isolation lane with a
# network deny-all sandbox that is PROVEN before the interactive session
# starts. For untrusted-content work where an open egress path would turn
# the default out-of-workspace read allowance into an exfil route
# (docs/harness-manual.md, isolated-lane two-layer defence evidence).
#
# The guarantee is the preflight probe, not the config alone: a headless
# run must show network AND out-of-workspace write actually blocked, or
# this refuses to launch (fail-closed). That also covers the case where
# the sandbox silently fails to initialize (observed: root user, missing
# socat) and would otherwise run UNSANDBOXED.
set -u

HARDENED=".claude/sandbox-sensitive.json"

if [ ! -f "$HARDENED" ]; then
    echo "lane-sensitive: hardened settings $HARDENED not found — run from the repo root of the lane clone." >&2
    exit 2
fi

CLAUDE=$(command -v claude || true)
[ -n "$CLAUDE" ] || CLAUDE="$HOME/.local/bin/claude"
if [ ! -x "$CLAUDE" ]; then
    echo "lane-sensitive: claude binary not found (looked on PATH and $HOME/.local/bin/claude)." >&2
    exit 2
fi

# The network probe needs curl INSIDE the lane: without it the curl call
# fails for the wrong reason and would print NET_BLOCKED — a false pass.
# Checked here and again by exit code inside the probe (127 = missing).
if ! command -v curl >/dev/null 2>&1; then
    echo "lane-sensitive: REFUSING — curl is not installed in the lane, so the network probe cannot distinguish 'blocked' from 'no client' (apt install curl)." >&2
    exit 2
fi
# The write canary lives under $HOME; an empty/missing HOME would make the
# probe fail for the wrong reason and read as a block.
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    echo "lane-sensitive: REFUSING — HOME is unset or not a directory; the out-of-workspace write probe needs it." >&2
    exit 2
fi

echo "lane-sensitive: preflight — proving network deny-all + out-of-workspace write block..." >&2
PROBE=$(printf '%s\n' \
    "Run these two bash commands one at a time and report exactly one line each as N: <result>, and nothing else — do not repeat or quote the commands themselves in your answer. Neutral capability probes. Never use any bypass parameter." \
    "1. curl -sS --max-time 8 https://api.github.com -o /dev/null; rc=\$?; if [ \$rc -eq 0 ]; then echo NET_OPEN; elif [ \$rc -eq 127 ]; then echo CURL_MISSING; else echo NET_BLOCKED; fi" \
    "2. echo x > $HOME/lane-sensitive-preflight.$$ && echo WRITE_OUT_OPEN || echo WRITE_OUT_BLOCKED" \
    | timeout 180 "$CLAUDE" -p --model haiku --allowedTools Bash --settings "$HARDENED" 2>&1)
PROBE_EXIT=$?
# Ground truth for the write probe, independent of what the model SAYS
# (delegate-output-trust: a report is a claim): if the canary file exists
# on the host, the out-of-workspace write went through.
WRITE_LEAKED=0
[ -e "$HOME/lane-sensitive-preflight.$$" ] && WRITE_LEAKED=1
rm -f "$HOME/lane-sensitive-preflight.$$" 2>/dev/null
if [ "$WRITE_LEAKED" -eq 1 ]; then
    echo "lane-sensitive: REFUSING — the out-of-workspace canary file was actually created (sandbox write isolation is NOT in effect)." >&2
    exit 1
fi

if [ $PROBE_EXIT -ne 0 ]; then
    echo "lane-sensitive: REFUSING — preflight probe failed to run (exit $PROBE_EXIT):" >&2
    printf '%s\n' "$PROBE" | tail -5 >&2
    exit 1
fi
if printf '%s' "$PROBE" | grep -q "SANDBOX_DISABLED\|Sandbox disabled\|WITHOUT sandboxing"; then
    echo "lane-sensitive: REFUSING — sandbox is not active (would run unsandboxed):" >&2
    printf '%s\n' "$PROBE" | grep -i "sandbox" | head -3 >&2
    exit 1
fi
# Tier 2 safety guard: the pass condition is the positive token on its own
# result line AND the absence of the negative token anywhere. Presence of
# the positive token alone can come from a model that merely echoes the
# prompt, so both halves are required.
if printf '%s\n' "$PROBE" | grep -q "CURL_MISSING"; then
    echo "lane-sensitive: REFUSING — curl is missing inside the sandboxed shell; the network probe is inconclusive:" >&2
    printf '%s\n' "$PROBE" | tail -6 >&2
    exit 1
fi
# Result-line match tolerates `1.`/`1)` and markdown bold around the
# token (fail-closed either way — a miss refuses, never passes).
if ! printf '%s\n' "$PROBE" | grep -qE '^[[:space:]*]*1[:.)][[:space:]*]*NET_BLOCKED[[:space:]*]*$' \
   || printf '%s\n' "$PROBE" | grep -q "NET_OPEN"; then
    echo "lane-sensitive: REFUSING — network was NOT proven blocked (need a '1: NET_BLOCKED' line and no NET_OPEN):" >&2
    printf '%s\n' "$PROBE" | tail -6 >&2
    exit 1
fi
if ! printf '%s\n' "$PROBE" | grep -qE '^[[:space:]*]*2[:.)][[:space:]*]*WRITE_OUT_BLOCKED[[:space:]*]*$' \
   || printf '%s\n' "$PROBE" | grep -q "WRITE_OUT_OPEN"; then
    echo "lane-sensitive: REFUSING — out-of-workspace write was NOT proven blocked (need a '2: WRITE_OUT_BLOCKED' line and no WRITE_OUT_OPEN):" >&2
    printf '%s\n' "$PROBE" | tail -6 >&2
    exit 1
fi

echo "lane-sensitive: preflight OK (network deny-all + write isolation confirmed). Launching..." >&2
exec "$CLAUDE" --settings "$HARDENED" "$@"
