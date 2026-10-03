#!/usr/bin/env bash
# Optional, read-only launcher report summary. Usage: harness-stats.sh [days].
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -n "$PY" ] || { echo 'harness-stats: Python is required' >&2; exit 1; }
"$PY" - "${1:-7}" "${HARNESS_STATE_DIR:-${HOME}/.claude/harness-runs}" "${BASH_SOURCE[0]}" <<'PY_STATS'
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import statistics
import sys

sys.path.insert(0, str(Path(sys.argv[3]).resolve().parent))
from harness_records import (ASSESSMENT_KEYS, DIMENSIONS, FAILURE_CLASSES,
                             parse_assessment_header, run_id, task_slug)

sys.stdout.reconfigure(encoding='utf-8', newline='\n')
if not re.fullmatch(r'[0-9]+', sys.argv[1]):
    sys.exit('usage: harness-stats.sh [days]')
days = int(sys.argv[1])
cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')
records = [(p.parent.name, p, False) for p in Path(sys.argv[2]).glob('*/report-*.txt')]
records += [('checkout-local', p, True) for p in Path('.claude/local-logs').glob('run-*/report.txt')]
groups = defaultdict(list)
directs = defaultdict(list)
outcomes = {}
for kind in ('direct', 'outcome'):
    for path in Path(sys.argv[2]).glob('*/' + kind + '-*.json'):
        stamp = path.name[len(kind) + 1:].split('-', 1)[0]
        if stamp < cutoff:
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                raise ValueError('record must be an object')
            for name in ('note', 'recorded'):
                if name in record and not isinstance(record[name], str):
                    raise ValueError(name + ' must be a string')
            if kind == 'outcome':
                run_id(record['run'])
                record.setdefault('accepted', 'unknown')
                if (path.name != 'outcome-' + record['run'] + '.json'
                        or record['class'] not in FAILURE_CLASSES
                        or record['accepted'] not in ('yes', 'no', 'unknown')):
                    raise ValueError('invalid outcome fields')
                outcomes[path.parent.name, record['run']] = record
            else:
                task_slug(record['task'])
                if record['result'] not in ('done', 'failed'):
                    raise ValueError('invalid direct result')
                for name in ('elapsed_s', 'tokens', 'rework', 'interventions'):
                    if name != 'elapsed_s':
                        record.setdefault(name, 0)
                    if type(record[name]) is not int or record[name] < 0:
                        raise ValueError(name + ' must be a non-negative integer')
                if record.get('verify', 'none') not in ('passed', 'failed', 'none') or not isinstance(record.get('model', ''), str):
                    raise ValueError('invalid direct fields')
                assessment = record.get('assessment')
                if 'assessment' in record and (not isinstance(assessment, dict)
                        or not set(DIMENSIONS) <= set(assessment) <= set(ASSESSMENT_KEYS)
                        or any(type(v) is not int or v not in (0, 1, 2) for v in assessment.values())):
                    raise ValueError('invalid assessment')
                directs[path.parent.name].append(record)
                groups[path.parent.name]  # Direct-only trees still have a summary.
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f'harness-stats: cannot read {path}: {error}', file=sys.stderr)
legacy_local = 0
for key, path, local in records:
    fields = {}
    try:
        # Only launcher headers, never quoted results or verifier output.
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                name, sep, value = line.rstrip('\r\n').partition(': ')
                if line.startswith(('FINAL_MESSAGE:', 'RESPONSE:')):
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
            if not local:
                fields['_RUN_ID'] = path.name[7:-4]
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
    models = defaultdict(Counter)
    bands, retries = Counter(), Counter()
    retry_reasons = ('infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning')
    elapsed, phases = [], []
    for fields, local in rows:
        status = fields.get('STATUS', '')
        result = next((s for s in ('DONE', 'FAILED', 'BLOCKED') if status.startswith(s)), 'other')
        counts[result] += 1
        settings = dict(re.findall(r'(?:\(|,\s*)(model|effort)=([^,()\s]+)', status))
        model = settings['model'] + '/' + settings.get('effort', 'unrecorded') if 'model' in settings else 'unrecorded'
        models[model][result] += 1
        band = fields.get('BAND', 'unrecorded')
        bands[band if band in ('S', 'A', 'B', 'C', 'D', 'E') else 'unrecorded'] += 1
        counts['band-S-done'] += int(band == 'S' and result == 'DONE')
        if 'RETRY_OF' in fields:
            retry = re.fullmatch(r'[^\s()]+ \(([^()]+)\)', fields['RETRY_OF'])
            reason = retry[1] if retry and retry[1] in retry_reasons else 'unrecorded'
            retries[reason] += 1
            counts['retry-total'] += 1
            counts['retry-done'] += int(result == 'DONE')
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
    for model, results in sorted(models.items()):
        print(f"    models: {model}: done={results['DONE']} failed={results['FAILED']} blocked={results['BLOCKED']}")
    print('    bands: ' + ' '.join(f'{band}={bands[band]}' for band in ('S', 'A', 'B', 'C', 'D', 'E', 'unrecorded')))
    reasons = ' '.join(f'{reason}={retries[reason]}' for reason in retry_reasons)
    if retries['unrecorded']:
        reasons += f" unrecorded={retries['unrecorded']}"
    print(f"    retries: total={counts['retry-total']} by-reason: {reasons}; done-after-retry={counts['retry-done']}/{counts['retry-total']}")
    print(f"    band-S-done={counts['band-S-done']}/{bands['S']}")
    direct_rows = directs[key]
    direct_counts = Counter(record['result'] for record in direct_rows)
    direct_elapsed = [record['elapsed_s'] for record in direct_rows]
    median_direct = str(statistics.median_low(direct_elapsed)) + 's' if direct_elapsed else 'n/a'
    print(f"    direct: runs={len(direct_rows)} done={direct_counts['done']} failed={direct_counts['failed']} tokens={sum(r['tokens'] for r in direct_rows)} median-elapsed={median_direct}")
    failures, accepted = Counter(), Counter()
    delegated_assess, direct_assess = defaultdict(Counter), defaultdict(Counter)
    delegated_tasks, direct_tasks = set(), {record['task'] for record in direct_rows}
    for fields, local in rows:
        if local:
            continue
        status = fields.get('STATUS', '')
        done = status.startswith('DONE')
        failed = status.startswith(('FAILED', 'BLOCKED'))
        outcome = outcomes.get((key, fields['_RUN_ID']))
        failure_class = outcome['class'] if outcome else 'none'
        if failed:
            failures[failure_class if failure_class != 'none' else 'unclassified'] += 1
        if outcome:
            accepted[outcome['accepted']] += 1
        task = fields.get('TASK', '')
        if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', task):
            delegated_tasks.add(task)
        try:
            assessment = parse_assessment_header(fields.get('ASSESS', ''))
        except ValueError:
            continue
        for name, level in assessment.items():
            bucket = delegated_assess[name, level]
            bucket['runs'] += 1
            bucket['done'] += int(done)
            bucket['failed'] += int(failed)
            bucket['reasoning-failed'] += int(failure_class == 'reasoning')
            bucket['retried'] += int('RETRY_OF' in fields)
    for record in direct_rows:
        for name, level in record.get('assessment', {}).items():
            bucket = direct_assess[name, level]
            bucket['runs'] += 1
            bucket[record['result']] += 1
    print('    failure-class: ' + ' '.join(f'{name}={failures[name]}' for name in (*retry_reasons, 'unclassified')))
    print('    accepted: ' + ' '.join(f'{name}={accepted[name]}' for name in ('yes', 'no', 'unknown')))
    for name in ASSESSMENT_KEYS:
        for level in (0, 1, 2):
            delegated, direct = delegated_assess[name, level], direct_assess[name, level]
            if delegated['runs'] or direct['runs']:
                print(f"    assess {name}={level}: delegated runs={delegated['runs']} done={delegated['done']} failed={delegated['failed']} reasoning-failed={delegated['reasoning-failed']} retried={delegated['retried']} | direct runs={direct['runs']} done={direct['done']} failed={direct['failed']}")
    print(f'    tasks: both={len(delegated_tasks & direct_tasks)} delegated-only={len(delegated_tasks - direct_tasks)} direct-only={len(direct_tasks - delegated_tasks)}')
PY_STATS
