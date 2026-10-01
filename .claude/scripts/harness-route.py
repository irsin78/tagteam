#!/usr/bin/env python
"""Resolve a host/role route; prints data only, never starts a model."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'hooks'))
from stop_gate import find_bash, mission_marker  # noqa: E402

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
    if not isinstance(data, dict) or data.get('schema_version') != 3 or not isinstance(data.get('workers'), list):
        raise ValueError('schema_version 3 and workers array required')
    bands = data.get('bands', {})
    if not isinstance(bands, dict) or bands.get('order') != list('EDCBAS') or not isinstance(data.get('latency'), dict):
        raise ValueError('bands and latency required')
    for band in 'EDCBA':
        if not isinstance(bands.get(band, {}).get('min_index'), (int, float)):
            raise ValueError('band min_index required')
    if not isinstance(bands.get('S', {}).get('lanes'), dict):
        raise ValueError('S lanes required')
    for name in ('interactive', 'foreground', 'detached'):
        if name not in data['latency']:
            raise ValueError('latency class required')
    ids, cells, priorities = set(), set(), set()
    for w in data['workers']:
        if not isinstance(w, dict):
            raise ValueError('worker must be an object')
        ident = w.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9-]+', ident) or ident in ids:
            raise ValueError('invalid or duplicate worker id')
        ids.add(ident)
        if w.get('vendor') not in VENDORS or w.get('tier') not in bands['order']:
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
        metrics = w.get('metrics')
        if w.get('scored') is not False:
            if (not isinstance(metrics, dict) or type(metrics.get('index')) not in (int, float)
                    or not math.isfinite(metrics['index']) or metrics['index'] < 0):
                raise ValueError('metrics with current index or scored:false required')
            for key in ('cost', 'ttft_s', 'tps'):
                value = metrics.get(key)
                if key not in metrics or (value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)):
                    raise ValueError('invalid metric: ' + key)
            if w['tier'] != 'S':
                expected = next((b for b in 'ABCDE' if metrics['index'] >= bands[b]['min_index']), None)
                if w['tier'] != expected:
                    raise ValueError('index does not match tier: ' + ident)
        if w['tier'] == 'S' and (w.get('tier_fixed') is not True or
                w['model'] != bands['S']['lanes'].get(w['vendor'])):
            raise ValueError('S row must match fixed lane model')
        if 'trust' in w and w['trust'] != 'low':
            raise ValueError('trust must be "low" when present: ' + ident)
        if w['status'] in ('active', 'unverified'):
            probe = w.get('probe') or {}
            if not isinstance(probe, dict) or not all(isinstance(probe.get(k), str) and probe[k] for k in ('date', 'result', 'cli')):
                raise ValueError('probe required: ' + ident)
        validate_requires(w.get('requires', {}))
        if not isinstance(w.get('roles'), dict) or set(w['roles']) - set(ROLES):
            raise ValueError('unknown or invalid worker roles')
        if w['status'] == 'optional' and not w.get('requires') and not w['roles']:
            raise ValueError('optional worker needs requires')
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
            if w['status'] == 'optional' and not w.get('requires') and not cfg.get('requires'):
                raise ValueError('optional worker needs requires')
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


def load_bindings(root=None):
    folder = Path(Path.cwd() if root is None else root) / '.claude'
    data = json.loads((folder / 'model-bindings.json').read_text(encoding='utf-8'))
    local = folder / 'model-bindings.local.json'
    override = json.loads(local.read_text(encoding='utf-8')) if local.exists() else {}
    return merge_bindings(data, override)


def session_budget(env):
    """Read only the session selected by orchestrator-set HARNESS_SESSION_ID."""
    marker = mission_marker(Path.cwd(), env.get('HARNESS_SESSION_ID'))
    if marker is None:
        return None
    try:
        record = json.loads(marker.read_text(encoding='utf-8'))
        exhausted = record.get('exhausted')
        if (isinstance(exhausted, list) and exhausted
                and all(isinstance(v, str) and v in VENDORS for v in exhausted)):
            return 'exhausted:' + ','.join(sorted(set(exhausted)))
    except (OSError, ValueError, AttributeError):
        pass
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
                if (isinstance(grants, list) and env.get('HARNESS_ALLOW_AGY_COMMAND') != '1'
                        and any(isinstance(g, str) and g.strip().startswith('command(') for g in grants)):
                    return 'requires agy_grant: command grant present'
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


def metric(worker, key):
    value = (worker.get('metrics') or {}).get(key)
    return float('inf') if value is None else value


def within_latency(worker, data, latency):
    if latency == 'detached':
        return True
    return (worker.get('effort') not in ('max', 'ultra') and
            metric(worker, 'ttft_s') <= data['latency'][latency])


def within_band(worker, data, floor, latency):
    if floor == 'S':
        lane = data['bands']['S']
        effort = lane['detached_effort' if latency == 'detached' else 'foreground_effort']
        return worker['model'] == lane['lanes'].get(worker['vendor']) and worker.get('effort') == effort
    # S is a deliberate model choice, never an automatic index promotion.
    return worker['tier'] != 'S' and data['bands']['order'].index(worker['tier']) >= data['bands']['order'].index(floor)


def cost_order(rows, data, exhausted):
    if not rows:
        return []
    c0 = min(metric(w, 'cost') for w in rows)
    ceiling = c0 * (1 + data['selection_policy']['cost_tie_pct'] / 100)
    # trust 'low' (a user call on a new model) ranks after every other row in the pool.
    return sorted(rows, key=lambda w: (w['vendor'] in exhausted, w.get('trust') == 'low',
                  w.get('metrics', {}).get('provisional', True),
                  0 if metric(w, 'cost') <= ceiling else metric(w, 'cost'),
                  metric(w, 'ttft_s')))


def launcher_default(data, vendor, role):
    host = 'claude' if vendor == 'openai' else 'codex'
    scoped = dict(data, workers=[w for w in data['workers'] if w['vendor'] == vendor])
    authors = [{'claude': 'claude', 'codex': 'openai'}[host]] if role in SEPARATED_ROLES else None
    # Foreground first. A bindings file whose only rows are detached-only (max/ultra)
    # still names its row, so the launcher's effort guards refuse it instead of a
    # silent builtin fallback.
    try:
        route = resolve(scoped, host, role, latency='foreground', author_vendors=authors)
    except IndexError:
        route = resolve(scoped, host, role, latency='detached', author_vendors=authors, allow_ultra=True)
    if not route['available']:
        raise ValueError('no active launcher default')
    return next(w for w in scoped['workers'] if w['id'] == route['worker_id'])


def resolve(data, host, role, budget='normal', tier=None, author_vendors=None,
            worker=None, root=None, env=None, latency=None, retry_from=None,
            retry_reason=None, attempt=0, allow_ultra=False):
    validate_bindings(data)
    root = Path.cwd() if root is None else Path(root)
    env = os.environ if env is None else env
    if host not in HOSTS or role not in ROLES:
        raise ValueError('invalid host or role')
    if role in REVIEW_ROLES and not author_vendors:
        raise ValueError('review requires --author-vendor for the actual artifact author(s)')
    if author_vendors and role not in SEPARATED_ROLES:
        raise ValueError('author-vendor is only for implementation, writing or review/advice')
    authors = set(author_vendors or [{'codex': 'openai', 'claude': 'claude'}[host]])
    if not authors <= set(VENDORS):
        raise ValueError('invalid author vendor')
    if worker is not None and tier is not None:
        raise ValueError('--worker cannot be combined with --tier')
    floor = tier or data['roles'][role]['band_floor']
    latency = latency or data['roles'][role].get('latency_default', 'foreground')
    if floor not in data['bands']['order'] or latency not in ('interactive', 'foreground', 'detached'):
        raise ValueError('invalid band or latency')
    if attempt < 0:
        raise ValueError('attempt must be non-negative')
    if attempt >= data['selection_policy']['attempt_cap']:  # cap counts the first attempt
        raise ValueError('attempt cap reached; orchestrator decides (retry-policy §4c)')
    if bool(retry_from) != bool(retry_reason) or (attempt and not retry_from):
        raise ValueError('retry requires --retry-from and --retry-reason')
    if retry_from and (worker or attempt < 1):
        raise ValueError('retry requires positive --attempt and cannot combine with --worker')
    if retry_reason and retry_reason not in ('infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning'):
        raise ValueError('invalid retry reason')
    by_id = {w['id']: w for w in data['workers']}
    for ident in (worker, retry_from):
        if ident is not None and ident not in by_id:
            raise ValueError('unknown worker id: ' + ident)
    exhausted = set(budget[10:].split(',')) if budget.startswith('exhausted:') else set()
    previous = by_id.get(retry_from)
    if previous and (role not in previous['roles'] or
            exclusion(previous, role, host, True, data, root, env) or
            (role in SEPARATED_ROLES and previous['vendor'] in authors)):
        raise ValueError('retry worker is not eligible for role/host/authors')
    keep = previous and retry_reason != 'reasoning' and not (
        retry_reason == 'availability' and previous['vendor'] in exhausted)
    explicit = worker or (retry_from if keep else None)
    candidates = [w for w in data['workers'] if role in w['roles'] and
                  (explicit is None or w['id'] == explicit)]
    skipped, eligible, diagnostic = [], [], []
    for w in candidates:
        reason = exclusion(w, role, host, explicit is not None, data, root, env)
        if not reason and explicit is None and w.get('scored') is False:
            reason = 'unscored requires explicit --worker'
        if not reason:
            diagnostic.append(w)
            if role in SEPARATED_ROLES and w['vendor'] in authors:
                reason = 'vendor is an artifact author'
        if reason:
            skipped.append({'id': w['id'], 'reason': reason})
        else:
            eligible.append(w)
    separation_ok = bool(eligible) if role in SEPARATED_ROLES else True
    pool = eligible if separation_ok else diagnostic
    needs_detached = False
    if previous and retry_reason == 'reasoning':
        pool = [w for w in pool if w['vendor'] == previous['vendor']]
        if previous['tier'] == 'S':
            # A failed S row is never repeated (retry-policy 5): the detached effort is
            # the only remaining step; past that the orchestrator decides (retry-policy 4).
            if previous.get('effort') == data['bands']['S']['foreground_effort'] and latency != 'detached':
                latency = 'detached'
                needs_detached = True
            else:
                raise ValueError('S row already failed at its detached effort; orchestrator decides (retry-policy 4)')
            floor = 'S'
        elif attempt >= 2 or tier == 'S':
            floor = 'S'
        else:
            index = (previous.get('metrics') or {}).get('index')
            promotion = [w for w in pool if w['tier'] != 'S' and index is not None and
                         metric(w, 'index') >= index + data['selection_policy']['promotion_delta']]
            fast = [w for w in promotion if within_latency(w, data, latency)]
            if fast or promotion:
                pool = fast or promotion
                needs_detached = not bool(fast)
            else:
                floor = 'S'
    promoting = previous and retry_reason == 'reasoning' and floor != 'S'
    if not allow_ultra and explicit and pool and pool[0]['vendor'] == 'openai' and pool[0].get('effort') == 'ultra':
        raise ValueError('Codex worker ultra enables re-delegation; select a single-agent effort')
    if explicit:
        chosen = [w for w in pool if within_latency(w, data, latency) or keep]
    elif promoting:
        chosen = pool
    else:
        chosen = []
        for w in pool:
            reason = None
            if not within_band(w, data, floor, latency):
                reason = 'S only' if w['tier'] == 'S' and floor != 'S' else 'below band or different S effort'
            elif not within_latency(w, data, latency):
                reason = 'latency ' + latency
            if reason:
                skipped.append({'id': w['id'], 'reason': reason})
            else:
                chosen.append(w)
    floor_met = bool(chosen)
    fallback = False
    if not chosen and not explicit and floor != 'S':
        chosen = [w for w in pool if w['tier'] != 'S' and w.get('scored') is not False and
                  within_latency(w, data, latency)]
        fallback = True
    ordered = cost_order(chosen, data, exhausted)
    if fallback:
        ordered.sort(key=lambda w: (w['vendor'] in exhausted, w.get('trust') == 'low', -metric(w, 'index')))
    if not ordered:
        raise IndexError('no eligible worker meets latency and hard constraints')
    selected = ordered[0]
    if explicit:
        floor_met = within_band(selected, data, floor, latency) and selected.get('scored') is not False
    vendor = selected['vendor']
    if not allow_ultra and vendor == 'openai' and selected.get('effort') == 'ultra':
        raise ValueError('Codex worker ultra enables re-delegation; select a single-agent effort')
    metrics = selected.get('metrics', {})
    limit = data['latency'][latency]
    label = latency if limit is None else f'{latency}<={limit}s'
    if needs_detached:
        label = f'detached (no {latency} row gains the promotion delta; run with -b --wait)'
    reason = (f'band {floor} floor, {label}: cheapest of {len(ordered)} eligible = '
              f"{selected['id']} ({metrics.get('index')}, ${metrics.get('cost')}, {metrics.get('ttft_s')}s)")
    if fallback:
        reason = f"no row meets band {floor} in {latency}; best available is {selected['id']}"
    if keep or retry_reason == 'availability':
        reason = f'retry keeps settings; fix {retry_reason}; ' + reason
    elif previous:
        reason = 'reasoning retry; ' + reason
    c0 = min(metric(w, 'cost') for w in ordered)
    ties = [w['id'] for w in ordered if metric(w, 'cost') <= c0 * (1 + data['selection_policy']['cost_tie_pct'] / 100)]
    reason += '; ties: ' + (', '.join(ties) if len(ties) > 1 else 'none')
    reason += '; skipped: ' + (', '.join(f"{w['id']} ({w['reason']})" for w in skipped) or 'none')
    route = dict(vendor=vendor, launcher=selected.get('launcher'),
                 sandbox='workspace-write' if role in ('implement', 'write') else 'read-only',
                 model=selected['model'], effort=selected.get('effort'), tier=selected['tier'],
                 host=host, role=role, budget=budget, band=floor, latency_class=latency,
                 floor_met=floor_met, reason=reason, needs_detached=needs_detached,
                 eligible=[w['id'] for w in ordered], worker_id=selected['id'], skipped=skipped,
                 tier_fallback=not floor_met, available=separation_ok and vendor not in exhausted)
    if role == 'web' and vendor == 'claude':
        route['claude_role'] = 'web'
    if role in SEPARATED_ROLES:
        route.update(author_vendors=sorted(authors), separation_satisfied=separation_ok)
    if not separation_ok:
        route['reason'] += '; no configured route is independent of the artifact authors; do not claim cross-review'
    elif budget == 'tight' and retry_reason == 'reasoning':
        route['available'] = False
        route['reason'] += '; tight budget disables optional reasoning promotions'
    elif vendor in exhausted:
        route['reason'] += ('; independent reviewer is exhausted; report required review as incomplete; do not use implementation fallback'
                            if role in REVIEW_ROLES else '; selected vendor is declared exhausted; follow fallback policy')
    return route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=('codex', 'claude'))
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--worker')
    parser.add_argument('--launcher-default', action='store_true')
    parser.add_argument('--vendor', choices=VENDORS)
    parser.add_argument('--tier', choices=tuple('SABCDE'), help='capability band floor')
    parser.add_argument('--latency', choices=('interactive', 'foreground', 'detached'))
    parser.add_argument('--retry-from')
    parser.add_argument('--retry-reason', choices=('infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning'))
    parser.add_argument('--attempt', type=int, default=0)
    parser.add_argument('--author-vendor', action='append', choices=('openai', 'claude', 'google', 'local'),
                        help='actual author of the design (implement/write/advice) or review target; repeat for coauthors; required for reviews')
    args = parser.parse_args()
    if args.launcher_default:
        try:
            if args.vendor is None:
                raise ValueError('--vendor required for launcher default')
            data = load_bindings()
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
        data = load_bindings()
        route = resolve(data, args.host, args.role, budget_for(data, os.environ),
                        args.tier, args.author_vendor, args.worker, latency=args.latency,
                        retry_from=args.retry_from, retry_reason=args.retry_reason, attempt=args.attempt)
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
