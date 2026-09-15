#!/usr/bin/env python
"""Resolve a host/role route; prints data only, never starts a model."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'hooks'))
from stop_gate import find_bash  # noqa: E402

REVIEW_ROLES = ('plan_review', 'review_gate', 'review_deep')


def merge(base, local):
    if isinstance(base, dict) and isinstance(local, dict):
        result = dict(base)
        for key, value in local.items():
            result[key] = merge(result[key], value) if key in result else value
        return result
    return local


def load_bindings(root):
    folder = Path(root) / '.claude'
    data = json.loads((folder / 'model-bindings.json').read_text(encoding='utf-8'))
    local = folder / 'model-bindings.local.json'
    if local.exists():
        data = merge(data, json.loads(local.read_text(encoding='utf-8')))
    return data


def budget_for(data, env):
    value = env.get('HARNESS_BUDGET', '').strip()
    if not value:
        # Public `budget` is the schema, local `budget` may replace it.
        value = data.get('budget')
        if not isinstance(value, str):
            value = ''
    if value in ('normal', 'tight') or value in (
            'exhausted:claude', 'exhausted:openai', 'exhausted:google', 'exhausted:local'):
        return value
    return 'normal'


def resolve(data, host, role, step=0, budget='normal', tier=None, author_vendors=None):
    if step < 0:
        raise ValueError('step must be non-negative')
    review_roles = REVIEW_ROLES
    separated_roles = ('implement', 'write', 'decide') + review_roles
    if role in review_roles and not author_vendors:
        raise ValueError('review requires --author-vendor for the actual artifact author(s)')
    if author_vendors and role not in separated_roles:
        raise ValueError('author-vendor is only for implementation, writing or review/advice')
    authors = set(author_vendors or [{'codex': 'openai', 'claude': 'claude'}[host]])
    if not authors <= {'openai', 'claude', 'google', 'local'}:
        raise ValueError('invalid author vendor')
    route = dict(data['host_routes'][host][role])
    separation_ok = True
    if role in separated_roles:
        # Host tables are configured candidates, not a permanent assignment.
        # Provenance refers to the artifact, including direct work/fallbacks.
        other = 'claude' if host == 'codex' else 'codex'
        candidates = [dict(data['host_routes'][source][role]) for source in (host, other)]
        eligible = [r for r in candidates if r['vendor'] not in authors]
        available = [r for r in eligible if budget != 'exhausted:' + r['vendor']]
        route = (available or eligible or candidates)[0]
        separation_ok = bool(eligible)
        route['author_vendors'] = sorted(authors)
        route['separation_satisfied'] = separation_ok
        if not separation_ok:
            route['reason'] = 'no configured route is independent of the artifact authors; do not claim cross-review'
    if role in ('decide', 'plan_review') + review_roles and route.get('sandbox') != 'read-only':
        raise ValueError('advice/review route must be read-only')
    if tier is not None:
        # Explicit capability choice; no benchmark arithmetic or automatic
        # demotion. Risk-specific requirements remain the caller's contract.
        allowed = {
            'implement': ('A', 'B', 'C'), 'write': ('A', 'B', 'C', 'D'),
            'explore': ('A', 'B', 'C', 'D'), 'decide': ('A', 'B'),
            'plan_review': ('A', 'B'), 'review_gate': ('A', 'B', 'C'),
            'review_deep': ('A', 'B'),
        }
        if tier not in allowed.get(role, ()) or step:
            raise ValueError('tier is incompatible with this role or ladder step')
        route.pop('ladder', None)
        route.pop('binding', None)
        cell = data['bindings'][tier][route['vendor']]
        route['tier'] = tier
    elif 'ladder' in route:
        cell = data['roles']['implement'][route.pop('ladder')][step]
        if 'binding' in cell:
            tier, vendor = cell['binding'].split('/')
            cell = data['bindings'][tier][vendor]
    else:
        if step:
            raise ValueError('step is only for implementation ladders')
        tier, vendor = route.pop('binding').split('/')
        cell = data['bindings'][tier][vendor]
    model, effort = cell['model'], cell.get('effort')
    vendor = cell.get('vendor', route['vendor'])
    if vendor != route['vendor']:
        raise ValueError('ladder vendor must match the launcher route')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', model):
        raise ValueError('invalid model token')
    if effort is not None and effort not in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ValueError('invalid effort')
    if vendor == 'openai' and effort == 'ultra':
        raise ValueError('Codex worker ultra enables re-delegation; select a single-agent effort')
    route.update(host=host, role=role, model=model, effort=effort, step=step, budget=budget)
    route['available'] = separation_ok and budget != 'exhausted:' + vendor
    if budget == 'tight' and step:
        route['available'] = False
        route['reason'] = 'tight budget disables optional ladder promotions'
    if not route['available']:
        route.setdefault('reason',
            'independent reviewer is exhausted; report required review as incomplete; do not use implementation fallback'
            if role in review_roles else 'selected vendor is declared exhausted; follow fallback policy')
    return route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('codex', 'claude'), required=True)
    parser.add_argument('--role', choices=('implement', 'decide', 'plan_review',
                        'review_gate', 'review_deep', 'explore', 'write', 'web'), required=True)
    parser.add_argument('--step', type=int, default=0)
    parser.add_argument('--tier', choices=('A', 'B', 'C', 'D'),
                        help='explicit task capability; cannot be combined with a ladder step')
    parser.add_argument('--author-vendor', action='append', choices=('openai', 'claude', 'google', 'local'),
                        help='actual author of the design (implement/write/advice) or review target; repeat for coauthors; required for reviews')
    args = parser.parse_args()
    if os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        print('HARNESS_DENIED: delegates execute their assignment, not another route', file=sys.stderr)
        return 4
    try:
        data = load_bindings(Path.cwd())
        route = resolve(data, args.host, args.role, args.step,
                        budget_for(data, os.environ), args.tier, args.author_vendor)
        route['shell'] = find_bash()
        if route['shell'] is None:
            native_review = (args.role in REVIEW_ROLES and route['available'] and
                             route['vendor'] == {'codex': 'openai', 'claude': 'claude'}[args.host])
            route['available'] = False
            route['reason'] = (route.get('reason', '') + '; ' if route.get('reason') else '') + (
                'no usable Bash for process launchers; use an available independent host-native reviewer of the selected vendor; otherwise report required review as incomplete; do not use implementation fallback'
                if native_review else
                'no usable Bash for process launchers; independent review unavailable; report required review as incomplete'
                if args.role in REVIEW_ROLES
                else 'no usable Bash for process launchers; follow native fallback')
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        print('HARNESS_DENIED: route configuration: ' + str(error), file=sys.stderr)
        return 4
    print(json.dumps(route, ensure_ascii=True, indent=2))
    return 0 if route['available'] else 2


if __name__ == '__main__':
    sys.exit(main())
