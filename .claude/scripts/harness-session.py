#!/usr/bin/env python
"""Explicit preflight/completion checks for hosts without active lifecycle hooks."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS))
from stop_gate import evaluate, evaluate_mission, update_mission, mission_marker  # noqa: E402


def check_codex_hooks(root, timeout=20):
    """Ask the installed engine about effective trust; never edit trust state."""
    root = Path(root).resolve()
    source = root / '.codex/hooks.json'
    definitions = json.loads(source.read_text(encoding='utf-8-sig'))['hooks']
    expected = {
        (re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower(), str(i), str(j))
        for event, entries in definitions.items()
        for i, entry in enumerate(entries) for j, _ in enumerate(entry['hooks'])
    }
    if not {'pre_tool_use', 'stop'} <= {key[0] for key in expected}:
        raise ValueError('PreToolUse and Stop definitions are required')
    executable = shutil.which('codex')
    if not executable:
        raise ValueError('Codex binary not found')
    process = subprocess.Popen(
        [executable, 'app-server', '--stdio'], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()

    def receive():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def request(identifier, method, params):
        process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        while True:
            line = messages.get(timeout=max(0, deadline - time.monotonic()))
            if line is None:
                raise ValueError('Codex app-server closed before responding')
            response = json.loads(line)
            if response.get('id') == identifier:
                if 'error' in response:
                    raise ValueError('Codex app-server does not support a successful ' + method)
                return response['result']

    try:
        request(1, 'initialize', {'clientInfo': {'name': 'harness_hook_check', 'version': '1'},
                                  'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        data = request(2, 'hooks/list', {'cwds': [str(root)]})['data']
        if len(data) != 1:
            raise ValueError('Codex returned an unexpected workspace count')
        if data[0].get('errors'):
            raise ValueError('Codex hook loading failed: ' + str(data[0]['errors']))
        for warning in data[0].get('warnings', []):
            print('Codex hook warning: ' + str(warning), file=sys.stderr)
        hooks = [hook for hook in data[0]['hooks']
                 if Path(hook['sourcePath']).resolve() == source]
        actual = {tuple(hook['key'].rsplit(':', 3)[1:]) for hook in hooks}
        if actual != expected or len(hooks) != len(expected):
            raise ValueError('configured hooks are missing from effective hooks/list (check features.hooks)')
        inactive = [hook['key'].rsplit(':', 3)[1] for hook in hooks
                    if hook.get('enabled') is not True
                    or hook.get('trustStatus') not in ('trusted', 'managed')]
        if inactive:
            raise ValueError('disabled, untrusted or modified hooks: ' + ', '.join(inactive))
        return len(hooks)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=2)
        process.stdout.close()


def update_budget(cwd, session, exhausted=None, clear=False):
    """Orchestrator-only availability bookkeeping; preserve mission fields."""
    marker = mission_marker(cwd, session)
    if marker is None or os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        raise ValueError('a current orchestrator session id is required')
    record = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if not isinstance(record, dict):
        raise ValueError('invalid session record')
    if clear:
        record.pop('exhausted', None)
        message = 'cleared'
    else:
        vendors = exhausted.split(',') if isinstance(exhausted, str) else []
        previous = record.get('exhausted', [])
        if (not vendors or not isinstance(previous, list)
                or any(not isinstance(v, str) or v not in ('openai', 'claude', 'google', 'local')
                       for v in vendors + previous)):
            raise ValueError('exhausted must be a comma-separated vendor list')
        record['exhausted'] = sorted(set(previous + vendors))
        message = 'exhausted:' + ','.join(record['exhausted'])
    if record:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(record, ensure_ascii=True), encoding='utf-8')
    else:
        marker.unlink(missing_ok=True)
    return 'HARNESS BUDGET: ' + message


def update_time(cwd, session, mode=None, tolerance_min=None, cost_at_tolerance=None,
                clear=False, reason=None, refocus_usd=None, slope=None):
    """Declare the user's waiting-time value in the selected orchestrator session."""
    from harness_records import load_time_policy, time_value
    marker = mission_marker(cwd, session)
    if marker is None or os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        raise ValueError('a current orchestrator session id is required')
    if clear:
        if any(value is not None for value in (mode, tolerance_min, cost_at_tolerance, refocus_usd, slope)):
            raise ValueError('--clear cannot be combined with time values')
    else:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('--reason is required for a time declaration')
        if mode is None and (tolerance_min is None or cost_at_tolerance is None):
            raise ValueError('time requires --mode or both --tolerance-min and --cost-at-tolerance')
    effective = time_value(load_time_policy(cwd), mode, tolerance_min, cost_at_tolerance, refocus_usd, slope)
    record = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if not isinstance(record, dict):
        raise ValueError('invalid session record')
    if clear:
        record.pop('time_cost', None)
    else:
        record['time_cost'] = dict(effective, reason=reason.strip())
    if record:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(record, ensure_ascii=True), encoding='utf-8')
    else:
        marker.unlink(missing_ok=True)
    source = 'bindings (cleared)' if clear else 'session'
    return (f"HARNESS TIME: {effective['mode']} T={effective['tolerance_min']:g} "
            f"k={effective['k']:g} source={source} refocus={effective['refocus_usd']:g} "
            f"slope={effective['slope_usd_per_tolerance']:g} switch={effective['switch_after_min']:g}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check-codex-hooks', help='check effective hook trust through the installed Codex engine')
    start = commands.add_parser('start')
    start.add_argument('--host', choices=('codex', 'claude'), required=True)
    finish = commands.add_parser('finish')
    finish.add_argument('--session', help='also check this orchestrator mission (without a continuation)')
    mission = commands.add_parser('mission', help='arm or release the current session mission guard')
    mission.add_argument('--session', required=True, help='id from HARNESS MISSION SESSION hook output')
    mission.add_argument('--state', required=True,
                         choices=('active', 'complete', 'paused', 'needs-input', 'switched'))
    mission.add_argument('--mission', help='project-relative mission folder containing spec.md')
    mission.add_argument('--reason', help='required for pause, decision/authorization wait, or changed request')
    budget = commands.add_parser('budget', help='record confirmed session vendor exhaustion')
    budget.add_argument('--session', required=True)
    action = budget.add_mutually_exclusive_group(required=True)
    action.add_argument('--exhausted', help='comma-separated vendors')
    action.add_argument('--clear', action='store_true')
    declaration = commands.add_parser('time', help='declare the session value of waiting time')
    declaration.add_argument('--session', required=True)
    declaration.add_argument('--mode', choices=('attended', 'background', 'unattended'))
    declaration.add_argument('--tolerance-min', type=float)
    declaration.add_argument('--cost-at-tolerance', type=float)
    declaration.add_argument('--refocus-usd', type=float)
    declaration.add_argument('--slope', type=float)
    declaration.add_argument('--clear', action='store_true')
    declaration.add_argument('--reason')
    outcome = commands.add_parser('outcome', help='record a diagnosed delegated run outcome')
    outcome.add_argument('--run', required=True)
    outcome.add_argument('--class', dest='failure_class', required=True)
    outcome.add_argument('--accepted', choices=('yes', 'no'), default='unknown')
    outcome.add_argument('--note', default='')
    direct = commands.add_parser('direct', help='record work handled directly by the orchestrator')
    direct.add_argument('phase', nargs='?', choices=('start', 'finish'))
    direct.add_argument('--task', required=True)
    direct.add_argument('--result', choices=('done', 'failed'))
    direct.add_argument('--elapsed-s')
    direct.add_argument('--assess')
    direct.add_argument('--model')
    for name in ('tokens', 'rework', 'interventions'):
        direct.add_argument('--' + name)
    direct.add_argument('--verify', choices=('passed', 'failed', 'none'))
    direct.add_argument('--note')
    args = parser.parse_args()
    if args.command in ('outcome', 'direct'):
        try:
            if os.environ.get('HARNESS_DELEGATE_RUN') == '1':
                raise ValueError('a current orchestrator session id is required')
            from harness_records import (FAILURE_CLASSES, nonnegative, parse_assessment, recorded_now,
                                         run_id, state_directory, task_slug, write_record)
            if args.command == 'outcome':
                args.run = run_id(args.run)
                if args.failure_class not in FAILURE_CLASSES:
                    raise ValueError('class must be ' + '|'.join(FAILURE_CLASSES))
            else:
                args.task = task_slug(args.task)
                if args.phase is None:
                    if args.result is None or args.elapsed_s is None:
                        raise ValueError('direct requires --result and --elapsed-s')
                    args.elapsed_s = nonnegative(args.elapsed_s)
                elif args.elapsed_s is not None:
                    raise ValueError('direct start/finish measures elapsed time; omit --elapsed-s')
                elif args.phase == 'finish' and args.result is None:
                    raise ValueError('direct finish requires --result')
                elif args.phase == 'start' and args.result is not None:
                    raise ValueError('direct start does not accept --result')
                phase_options = {'start': ('tokens', 'rework', 'interventions', 'verify', 'note'),
                                 'finish': ('model', 'assess')}
                for name in phase_options.get(args.phase, ()):
                    if getattr(args, name) is not None:
                        raise ValueError('direct ' + args.phase + ' does not accept --' + name)
                for name in ('tokens', 'rework', 'interventions'):
                    value = getattr(args, name)
                    setattr(args, name, nonnegative(str(0 if value is None else value)))
                if args.assess is not None:
                    args.assess = parse_assessment(args.assess)
                args.model = args.model or ''
                args.verify = args.verify or 'none'
                args.note = args.note or ''
            directory = state_directory()
            if args.command == 'outcome':
                if not (directory / ('report-' + args.run + '.txt')).is_file():
                    raise ValueError('no report exists for run ' + args.run)
                path = directory / ('outcome-' + args.run + '.json')
                record = dict(run=args.run, **{'class': args.failure_class}, accepted=args.accepted,
                              note=args.note, recorded=recorded_now())
            else:
                start_path = directory / ('direct-start-' + args.task + '.json')
                if args.phase == 'start':
                    if start_path.exists():
                        raise ValueError('direct start already exists for task ' + args.task)
                    record = dict(task=args.task, started=datetime.now(timezone.utc).isoformat(),
                                  model=args.model)
                    if args.assess is not None:
                        record['assessment'] = args.assess
                    write_record(start_path, record)
                    print(start_path)
                    return 0
                if args.phase == 'finish':
                    if not start_path.is_file():
                        raise ValueError('no direct start exists for task ' + args.task)
                    started = json.loads(start_path.read_text(encoding='utf-8'))
                    if not isinstance(started, dict) or started.get('task') != args.task:
                        raise ValueError('invalid direct start record')
                    start_time = datetime.fromisoformat(started['started'].replace('Z', '+00:00'))
                    if start_time.utcoffset() is None:
                        raise ValueError('direct start stamp requires a timezone')
                    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                    if elapsed < 0:
                        raise ValueError('direct start stamp is in the future')
                    args.elapsed_s = int(elapsed)
                    args.model = started.get('model', '')
                    args.assess = started.get('assessment')
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                path = directory / ('direct-' + stamp + '-' + secrets.token_hex(4) + '.json')
                record = dict(task=args.task, result=args.result, elapsed_s=args.elapsed_s,
                              model=args.model, tokens=args.tokens, rework=args.rework,
                              interventions=args.interventions, verify=args.verify,
                              note=args.note, recorded=recorded_now())
                if args.assess is not None:
                    record['assessment'] = args.assess
            write_record(path, record)
            if args.command == 'direct' and args.phase == 'finish':
                start_path.unlink()
            print(path)
        except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == 'budget':
        try:
            print(update_budget(os.getcwd(), args.session, args.exhausted, args.clear))
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == 'time':
        try:
            print(update_time(os.getcwd(), args.session, args.mode, args.tolerance_min,
                              args.cost_at_tolerance, args.clear, args.reason, args.refocus_usd, args.slope))
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == 'check-codex-hooks':
        try:
            count = check_codex_hooks(os.getcwd())
        except (OSError, ValueError, KeyError, TypeError, queue.Empty, subprocess.SubprocessError) as exc:
            print('HARNESS_DENIED: Codex hook trust is not confirmed: %s. Check features.hooks and /hooks; '
                  'hooks/list support is required. HARNESS_ALLOW_UNTRUSTED_HOOKS=1 is only for an '
                  'explicitly authorized unguarded run.' % (str(exc) or 'query timed out'), file=sys.stderr)
            return 4
        print('HOOKS_TRUSTED: %d configured handlers enabled and trusted by Codex' % count)
        return 0
    if args.command == 'mission':
        try:
            print(update_mission(os.getcwd(), args.session, args.state, args.mission, args.reason))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == 'start':
        # Same line the SessionStart hook prints: platform, host and role.
        return subprocess.run([sys.executable, str(HOOKS / 'session_preflight.py'), '--host', args.host],
                              input=json.dumps({'cwd': os.getcwd(), 'source': 'startup'}),
                              text=True).returncode
    code, message = evaluate(os.getcwd())
    if message:
        print(message, file=sys.stderr, end='')
    if code or Path('.claude/.stop-gate').exists():
        print('HARNESS INCOMPLETE: verification gate remains unsatisfied', file=sys.stderr)
        return 1
    if args.session:
        _, message = evaluate_mission(os.getcwd(), {'session_id': args.session, 'stop_hook_active': True})
        if message:
            print(message, file=sys.stderr, end='')
            if message.startswith(('MISSION_INCOMPLETE:', 'MISSION_UNKNOWN:')):
                return 1
    print('HARNESS GATE: clear (verify claimed files and task checks separately)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
