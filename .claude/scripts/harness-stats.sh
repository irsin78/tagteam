#!/usr/bin/env bash
# Optional, read-only launcher report summary. Usage: harness-stats.sh [days].
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -n "$PY" ] || { echo 'harness-stats: Python is required' >&2; exit 1; }
"$PY" - "${1:-7}" "${HOME}/.claude/harness-runs" <<'PY_STATS'
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import statistics
import sys

sys.stdout.reconfigure(encoding='utf-8', newline='\n')
if not re.fullmatch(r'[0-9]+', sys.argv[1]):
    sys.exit('usage: harness-stats.sh [days]')
days = int(sys.argv[1])
cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')
records = [(p.parent.name, p, False) for p in Path(sys.argv[2]).glob('*/report-*.txt')]
records += [('checkout-local', p, True) for p in Path('.claude/local-logs').glob('run-*/report.txt')]
groups = defaultdict(list)
legacy_local = 0
for key, path, local in records:
    fields = {}
    try:
        # Only launcher headers, never quoted results or verifier output.
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                name, sep, value = line.rstrip('\r\n').partition(': ')
                if line.startswith('FINAL_MESSAGE:'):
                    break
                if sep and name not in fields:
                    if name == 'TIMING' and 'VERIFY' in fields:
                        continue
                    fields[name] = value
        stamp = fields.get('STARTED') if local else path.name[7:].split('-', 1)[0]
        if local and not stamp:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            legacy_local += 1
        if stamp and stamp >= cutoff:
            groups[key].append((fields, local))
    except OSError as error:
        print(f'harness-stats: cannot read {path}: {error}', file=sys.stderr)
        sys.exit(1)

print(f'harness runs in the last {days} days (per tree key; local reads in this checkout separately):')
print('modes are host-specific, not equivalent OS isolation; elapsed is launcher time only; legacy ELAPSED excludes some setup. TIMING phases cover only new reports; DONE is not independent acceptance.')
if legacy_local:
    print(f'INFO: {legacy_local} older local reports have no STARTED header; their date window uses file mtime.')
if not groups:
    print('no run reports in this window')
for key, rows in sorted(groups.items()):
    counts = Counter()
    elapsed, phases = [], []
    for fields, local in rows:
        status = fields.get('STATUS', '')
        result = next((s for s in ('DONE', 'FAILED', 'BLOCKED') if status.startswith(s)), 'other')
        counts[result] += 1
        host = 'local' if local else next((h for h in ('codex', 'claude', 'agy') if h + '_exit=' in status), 'local' if 'cmd_exit=' in status else 'unknown')
        counts[host] += 1
        mode = next((label for token, label in (
            ('sandbox=workspace-write', 'codex-workspace-write'), ('sandbox=read-only', 'codex-read-only'),
            ('sandbox=danger-full-access', 'codex-full-access'), ('mode=acceptEdits', 'claude-acceptEdits'),
            ('mode=plan', 'claude-plan')) if label.startswith(host + '-') and token in status), 'not-reported-or-unknown')
        counts[mode] += 1
        verify = fields.get('VERIFY', 'not requested' if local else '')
        if 'not requested' in verify:
            counts['verify-not-requested'] += 1
        else:
            counts['verify-attached'] += int('VERIFY' in fields)
            verdict = 'passed' if re.match(r'exit 0($|[^0-9])', verify) else 'failed' if re.match(r'exit [1-9][0-9]*($|[^0-9])', verify) else 'unknown'
            counts['verify-' + verdict] += 1
        token = re.match(r'[0-9][0-9,]*', fields.get('TOKENS', ''))
        if token:
            counts['tokens'] += int(token[0].replace(',', ''))
        duration = re.match(r'([0-9]+)s', fields.get('ELAPSED', ''))
        if duration:
            elapsed.append(int(duration[1]))
        timing = {k: int(v) for k, v in re.findall(r'(\w+)=([0-9]+)(?= |$)', fields.get('TIMING', ''))}
        keys = ('preflight_ms', 'request_ms' if local else 'cli_ms', 'postflight_ms', 'verify_ms')
        if all(k in timing for k in (*keys, 'total_ms')) and sum(timing[k] for k in keys) == timing['total_ms']:
            phases.append([timing[k] for k in (*keys, 'total_ms')])
    median = str(statistics.median_low(elapsed)) + 's' if elapsed else 'n/a'
    print(f"  {key}: runs={len(rows)} done={counts['DONE']} failed={counts['FAILED']} blocked={counts['BLOCKED']} status-other-or-missing={counts['other']} tokens={counts['tokens']} median-launcher-elapsed={median}")
    print('    hosts: ' + ' '.join(f'{h}={counts[h]}' for h in ('codex', 'claude', 'agy', 'local', 'unknown')))
    print('    modes: ' + ' '.join(f'{m}={counts[m]}' for m in ('codex-workspace-write', 'codex-read-only', 'codex-full-access', 'claude-acceptEdits', 'claude-plan', 'not-reported-or-unknown')))
    print('    ' + ' '.join(f'verify-{v}={counts["verify-" + v]}' for v in ('attached', 'passed', 'failed', 'not-requested', 'unknown')))
    if phases:
        labels = ('preflight', 'request' if key == 'checkout-local' else 'cli', 'postflight', 'verify', 'total')
        print(f'    timed-runs={len(phases)} mean-ms: ' + ' '.join(f'{label}={sum(p[i] for p in phases) / len(phases):.0f}' for i, label in enumerate(labels)))
PY_STATS
