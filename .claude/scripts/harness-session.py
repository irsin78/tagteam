#!/usr/bin/env python
"""Explicit preflight/completion checks for hosts without active lifecycle hooks."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS))
from stop_gate import evaluate, evaluate_mission, update_mission  # noqa: E402


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
    args = parser.parse_args()
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
        role = 'delegate' if os.environ.get('HARNESS_DELEGATE_RUN') == '1' else 'orchestrator'
        print('HARNESS SESSION: host=%s role=%s' % (args.host, role), flush=True)
        return subprocess.run([sys.executable, str(HOOKS / 'session_preflight.py')],
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
