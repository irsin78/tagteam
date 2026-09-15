#!/usr/bin/env python
"""Explicit preflight/completion checks for hosts without active lifecycle hooks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS))
from stop_gate import evaluate, evaluate_mission, update_mission  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
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
