#!/usr/bin/env python
"""Resolve a host/role route; prints data only, never starts a model."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import shutil

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


VENDORS = ('claude', 'openai', 'google', 'local')
HOSTS = ('claude', 'codex')
ROLES = ('implement', 'decide', 'plan_review', 'review_gate', 'review_deep',
         'explore', 'web', 'write', 'image_verify')
SEPARATED_ROLES = ('implement', 'write', 'decide') + REVIEW_ROLES
REQUIRES = ('local_endpoint', 'agy_grant', 'agent_file', 'binary')


def role_config(worker, role):
    value = worker['roles'].get(role)
    return value if isinstance(value, dict) and 'priority' in value else {'priority': value}


def priority(worker, role, host):
    value = role_config(worker, role)['priority']
    return value.get(host) if isinstance(value, dict) else value


def validate_requires(value):
    if not isinstance(value, dict) or set(value) - set(REQUIRES):
        raise ValueError('unknown or invalid requires')
    for key, setting in value.items():
        if key == 'local_endpoint':
            if setting is not True:
                raise ValueError('local_endpoint requires true')
        elif not isinstance(setting, str) or not setting:
            raise ValueError('requires ' + key + ' must be a nonempty string')


def validate_bindings(data):
    if not isinstance(data, dict) or data.get('schema_version') != 2 or not isinstance(data.get('workers'), list):
        raise ValueError('schema_version 2 and workers array required')
    ids, cells, priorities = set(), set(), set()
    for w in data['workers']:
        if not isinstance(w, dict):
            raise ValueError('worker must be an object')
        ident = w.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9-]+', ident) or ident in ids:
            raise ValueError('invalid or duplicate worker id')
        ids.add(ident)
        if w.get('vendor') not in VENDORS or w.get('tier') not in ('A', 'B', 'C', 'D'):
            raise ValueError('unknown vendor or tier')
        if w.get('status') not in ('active', 'optional', 'conditional', 'unverified'):
            raise ValueError('unknown worker status')
        if not isinstance(w.get('model'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', w['model']):
            raise ValueError('model must be a token')
        if w.get('effort') not in (None, 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise ValueError('effort is invalid')
        if w.get('native') is not True and (not isinstance(w.get('launcher'), str) or not w['launcher']):
            raise ValueError('launcher or native required')
        if 'tier_cell' in w and type(w['tier_cell']) is not bool:
            raise ValueError('tier_cell must be boolean')
        if w.get('tier_cell'):
            cell = (w['tier'], w['vendor'])
            if cell in cells:
                raise ValueError('duplicate tier_cell')
            cells.add(cell)
        validate_requires(w.get('requires', {}))
        if not isinstance(w.get('roles'), dict) or set(w['roles']) - set(ROLES):
            raise ValueError('unknown or invalid worker roles')
        for role in w['roles']:
            cfg = role_config(w, role)
            if set(cfg) - {'priority', 'hosts', 'requires'}:
                raise ValueError('unknown role configuration')
            prio = cfg['priority']
            if isinstance(prio, dict):
                if not prio or set(prio) - set(HOSTS):
                    raise ValueError('invalid priority host map')
                values = prio.values()
            else:
                values = [prio]
            if any(type(v) is not int or v <= 0 for v in values):
                raise ValueError('priority must be a positive integer')
            if 'hosts' in cfg and (not isinstance(cfg['hosts'], list) or any(h not in HOSTS for h in cfg['hosts'])):
                raise ValueError('invalid hosts')
            validate_requires(cfg.get('requires', {}))
            for host in HOSTS:
                rank = priority(w, role, host)
                if rank is None or host not in cfg.get('hosts', HOSTS):
                    continue
                key = (host, w['vendor'], role, rank)
                if key in priorities:
                    raise ValueError('duplicate vendor/role/priority')
                priorities.add(key)
    return data


def merge_bindings(base, local):
    if not isinstance(base, dict) or not isinstance(local, dict):
        raise ValueError('bindings must be an object')
    data = merge(base, local)
    if not isinstance(data.get('workers'), list) or any(not isinstance(w, dict) for w in data['workers']):
        raise ValueError('workers must be an array of objects')
    workers = list(data['workers'])
    additions = data.pop('workers_local', [])
    if not isinstance(additions, list):
        raise ValueError('workers_local must be an array')
    for override in additions:
        if not isinstance(override, dict) or 'id' not in override:
            raise ValueError('workers_local entry requires id')
        matches = [i for i, w in enumerate(workers) if w.get('id') == override['id']]
        if matches:
            workers[matches[0]] = merge(workers[matches[0]], override)
        else:
            workers.append(override)
    data['workers'] = workers
    return validate_bindings(data)


def load_bindings(root):
    folder = Path(root) / '.claude'
    data = json.loads((folder / 'model-bindings.json').read_text(encoding='utf-8'))
    local = folder / 'model-bindings.local.json'
    override = json.loads(local.read_text(encoding='utf-8')) if local.exists() else {}
    return merge_bindings(data, override)


def session_budget(env):
    """Reserved for the session availability record (U4)."""
    return None


def budget_for(data, env):
    value = env.get('HARNESS_BUDGET', '').strip() or session_budget(env) or data.get('budget')
    if isinstance(value, str):
        if value in ('normal', 'tight'):
            return value
        if value.startswith('exhausted:') and all(v in VENDORS for v in value[10:].split(',')):
            return value
    return 'normal'


def unmet_requires(requirements, data, root, env):
    for key, value in requirements.items():
        if key == 'local_endpoint':
            endpoint = data.get('vendors', {}).get('local', {}).get('endpoint')
            if not isinstance(endpoint, dict) or not endpoint.get('base_url') or not endpoint.get('model'):
                return 'requires local_endpoint'
        elif key == 'agent_file':
            if not (Path(root) / value).is_file():
                return 'requires agent_file: ' + value
        elif key == 'binary':
            if not shutil.which(value, path=env.get('PATH')):
                return 'requires binary: ' + value
        elif key == 'agy_grant':
            path = Path(env.get('HARNESS_AGY_SETTINGS') or Path.home() / '.gemini/antigravity-cli/settings.json')
            try:
                settings = json.loads(path.read_text(encoding='utf-8'))
                grants = settings.get('permissions', {}).get('allow', [])
                allowed = isinstance(grants, list) and any(isinstance(g, str) and g.startswith(value + '(') for g in grants)
            except (OSError, ValueError, AttributeError):
                allowed = False
            if not allowed:
                return 'requires agy_grant: ' + value
    return None


def exclusion(worker, role, host, explicit, data, root, env):
    cfg = role_config(worker, role)
    if role in worker['roles'] and (priority(worker, role, host) is None or host not in cfg.get('hosts', HOSTS)):
        return 'host is not eligible'
    if not explicit and worker['status'] in ('conditional', 'unverified'):
        return 'status ' + worker['status'] + ' requires explicit --worker'
    return (unmet_requires(worker.get('requires', {}), data, root, env)
            or unmet_requires(cfg.get('requires', {}), data, root, env))


def launcher_default(data, vendor, role):
    # Use the usual opposite orchestrator for per-host priorities. The public
    # implementation order within each vendor is identical on both hosts.
    host = 'claude' if vendor == 'openai' else 'codex'
    validate_bindings(data)
    candidates = [w for w in data['workers'] if w['vendor'] == vendor and w['status'] == 'active'
                  and role in w['roles'] and priority(w, role, host) is not None
                  and host in role_config(w, role).get('hosts', HOSTS)]
    if not candidates:
        raise ValueError('no active launcher default')
    return min(candidates, key=lambda w: priority(w, role, host))


def resolve(data, host, role, step=0, budget='normal', tier=None, author_vendors=None,
            worker=None, root=None, env=None):
    validate_bindings(data)
    root = Path.cwd() if root is None else Path(root)
    env = os.environ if env is None else env
    if host not in HOSTS or role not in ROLES:
        raise ValueError('invalid host or role')
    if step < 0:
        raise ValueError('step must be non-negative')
    if role in REVIEW_ROLES and not author_vendors:
        raise ValueError('review requires --author-vendor for the actual artifact author(s)')
    if author_vendors and role not in SEPARATED_ROLES:
        raise ValueError('author-vendor is only for implementation, writing or review/advice')
    authors = set(author_vendors or [{'codex': 'openai', 'claude': 'claude'}[host]])
    if not authors <= set(VENDORS):
        raise ValueError('invalid author vendor')
    allowed = {'implement': 'ABC', 'write': 'ABCD', 'explore': 'ABCD', 'web': 'CD',
               'decide': 'AB', 'plan_review': 'AB', 'review_gate': 'ABC', 'review_deep': 'AB'}
    if tier is not None and (tier not in allowed.get(role, '') or step):
        raise ValueError('tier is incompatible with this role or ladder step')
    if worker is not None and not any(w['id'] == worker for w in data['workers']):
        raise ValueError('unknown worker id: ' + worker)
    candidates = [w for w in data['workers'] if role in w['roles'] and (worker is None or w['id'] == worker)]
    candidates.sort(key=lambda w: priority(w, role, host) or float('inf'))
    skipped, eligible, diagnostic = [], [], []
    for w in candidates:
        reason = exclusion(w, role, host, worker is not None, data, root, env)
        if not reason:
            diagnostic.append(w)
            if role in SEPARATED_ROLES and w['vendor'] in authors:
                reason = 'vendor is an artifact author'
        if reason:
            skipped.append({'id': w['id'], 'reason': reason})
        else:
            eligible.append(w)
    separation_ok = bool(eligible) if role in SEPARATED_ROLES else True
    exhausted = set(budget[10:].split(',')) if budget.startswith('exhausted:') else set()
    preferred_tier = tier or 'D'
    if not separation_ok and diagnostic:
        selected = diagnostic[0]
    else:
        if tier and role in SEPARATED_ROLES and eligible:
            vendor = eligible[0]['vendor']
            cell = next((w for w in data['workers'] if w.get('tier_cell') and w['tier'] == tier and w['vendor'] == vendor), None)
            if cell is None:
                raise ValueError('no tier cell for ' + tier + '/' + vendor)
            reason = exclusion(cell, role, host, worker is not None, data, root, env)
            if reason:
                raise ValueError('tier cell unavailable: ' + reason)
            eligible = [cell]
        # Stable priority ordering retains the requested reader tier within each
        # availability group; exhaustion may select a reader from another tier.
        eligible.sort(key=lambda w: (w['vendor'] in exhausted,
                      w['tier'] != preferred_tier if role in ('explore', 'web') else False))
        selected = eligible[step]
    vendor = selected['vendor']
    if vendor == 'openai' and selected.get('effort') == 'ultra':
        raise ValueError('Codex worker ultra enables re-delegation; select a single-agent effort')
    route = dict(vendor=vendor, launcher=selected.get('launcher'),
                 sandbox='workspace-write' if role in ('implement', 'write') else 'read-only',
                 model=selected['model'], effort=selected.get('effort'), tier=selected['tier'],
                 host=host, role=role, step=step, budget=budget,
                 worker_id=selected['id'], skipped=skipped,
                 tier_fallback=role in ('explore', 'web') and selected['tier'] != preferred_tier,
                 available=separation_ok and vendor not in exhausted)
    if role == 'web' and vendor == 'claude':
        route['claude_role'] = 'web'
    if role in SEPARATED_ROLES:
        route.update(author_vendors=sorted(authors), separation_satisfied=separation_ok)
    if not separation_ok:
        route['reason'] = 'no configured route is independent of the artifact authors; do not claim cross-review'
    elif budget == 'tight' and step:
        route['available'] = False
        route['reason'] = 'tight budget disables optional ladder promotions'
    elif vendor in exhausted:
        route['reason'] = ('independent reviewer is exhausted; report required review as incomplete; do not use implementation fallback'
                           if role in REVIEW_ROLES else 'selected vendor is declared exhausted; follow fallback policy')
    return route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('codex', 'claude'))
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--worker')
    parser.add_argument('--launcher-default', action='store_true')
    parser.add_argument('--vendor', choices=VENDORS)
    parser.add_argument('--step', type=int, default=0)
    parser.add_argument('--tier', choices=('A', 'B', 'C', 'D'),
                        help='explicit task capability; cannot be combined with a ladder step')
    parser.add_argument('--author-vendor', action='append', choices=('openai', 'claude', 'google', 'local'),
                        help='actual author of the design (implement/write/advice) or review target; repeat for coauthors; required for reviews')
    args = parser.parse_args()
    if args.launcher_default:
        try:
            if args.vendor is None:
                raise ValueError('--vendor required for launcher default')
            data = load_bindings(Path.cwd())
            cell = launcher_default(data, args.vendor, args.role)
            source = 'public+local' if Path('.claude/model-bindings.local.json').exists() else 'public'
            print(source, cell['model'], cell.get('effort') or 'unspecified')
            return 0
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            print('ERR', type(error).__name__, str(error).replace('\n', ' ')[:60])
            return 2
    if args.host is None:
        parser.error('--host required for routing')
    if os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        print('HARNESS_DENIED: delegates execute their assignment, not another route', file=sys.stderr)
        return 4
    try:
        data = load_bindings(Path.cwd())
        route = resolve(data, args.host, args.role, args.step,
                        budget_for(data, os.environ), args.tier, args.author_vendor, args.worker)
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
