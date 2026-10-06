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
import statistics

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'hooks'))
from stop_gate import find_bash, mission_marker  # noqa: E402

REVIEW_ROLES = ('plan_review', 'review_gate', 'review_deep')
# Launchers accept -t above this only with -b (the parent's Bash tool kills a
# foreground call at 600 s); kept in sync with the launchers' foreground cap.
FOREGROUND_TIMEOUT_CAP_S = 570
# retry-policy.md section 2. Only `reasoning` promotes; `deadline` keeps the
# worker and runs it detached with a longer budget; `refusal` keeps the band
# and excludes the refusing vendor for this route only.
RETRY_REASONS = ('infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning', 'defect',
                 'deadline', 'refusal')


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


def finite_number(value, name, positive=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value < 0 or (positive and value == 0):
        raise ValueError(name + ' must be a finite ' + ('positive' if positive else 'non-negative') + ' number')
    return value


def validate_volume_estimates(value, name, complete=True):
    if (not isinstance(value, dict) or set(value) - set('012')
            or (complete and set(value) != set('012'))):
        raise ValueError(name + ' requires volume keys 0|1|2')
    for volume, estimate in value.items():
        label = name + '.' + volume
        if not isinstance(estimate, dict) or not {'usd', 'minutes'} <= set(estimate):
            raise ValueError(label + ' requires usd and minutes')
        for key in ('usd', 'minutes'):
            finite_number(estimate[key], label + '.' + key)


def validate_score_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError('selection_policy must be an object')
    if 'time_cost' in policy:
        time = policy['time_cost']
        if not isinstance(time, dict):
            raise ValueError('time_cost must be an object')
        finite_number(time.get('tolerance_min'), 'time_cost.tolerance_min', True)
        finite_number(time.get('exponent'), 'time_cost.exponent', True)
        finite_number(time.get('switch_after_min', 3), 'time_cost.switch_after_min')
        modes = time.get('modes')
        if (not isinstance(modes, dict) or set(modes) != {'attended', 'background', 'unattended'}
                or not isinstance(time.get('default_mode'), str) or time['default_mode'] not in modes):
            raise ValueError('time_cost requires attended|background|unattended modes and a valid default_mode')
        for mode, value in modes.items():
            if isinstance(value, dict):
                for key in ('k', 'refocus_usd', 'slope_usd_per_tolerance'):
                    finite_number(value.get(key), 'time_cost.modes.' + mode + '.' + key)
            else:
                finite_number(value, 'time_cost.modes.' + mode)
    if 'estimates' in policy:
        estimates = policy['estimates']
        if not isinstance(estimates, dict):
            raise ValueError('estimates must be an object')
        for name in ('direct', 'delegate_overhead'):
            validate_volume_estimates(estimates.get(name), 'estimates.' + name)
        days = estimates.get('history_days')
        if type(days) is not int or not 0 <= days <= 999999999:
            raise ValueError('estimates.history_days must be a non-negative integer within the timedelta range')
        worker = estimates.get('worker')
        if not isinstance(worker, dict):
            raise ValueError('estimates.worker must be an object')
        for name in ('task_units', 'base_minutes'):
            values = worker.get(name)
            if not isinstance(values, dict) or set(values) != set('012'):
                raise ValueError('estimates.worker.' + name + ' requires volume keys 0|1|2')
            for volume, value in values.items():
                finite_number(value, 'estimates.worker.' + name + '.' + volume)
        finite_number(worker.get('reference_ttft_s'), 'estimates.worker.reference_ttft_s', True)
        clamp = worker.get('speed_clamp')
        if not isinstance(clamp, list) or len(clamp) != 2:
            raise ValueError('estimates.worker.speed_clamp requires two ordered non-negative numbers')
        for value in clamp:
            finite_number(value, 'estimates.worker.speed_clamp')
        if clamp[0] > clamp[1]:
            raise ValueError('estimates.worker.speed_clamp must be ordered')
        if type(worker.get('min_records')) is not int or worker['min_records'] <= 0:
            raise ValueError('estimates.worker.min_records must be a positive integer')


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
    validate_score_policy(data.get('selection_policy'))
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
        if 'estimates' in w:
            validate_volume_estimates(w['estimates'], 'worker ' + ident + ' estimates', complete=False)
        if w.get('scored') is not False:
            if (not isinstance(metrics, dict) or type(metrics.get('index')) not in (int, float)
                    or not math.isfinite(metrics['index']) or metrics['index'] < 0):
                raise ValueError('metrics with current index or scored:false required')
            for key in ('cost', 'ttft_s', 'tps'):
                value = metrics.get(key)
                if key not in metrics or (value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)):
                    raise ValueError('invalid metric: ' + key)
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
    validate_bindings(data)
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
    except (OSError, ValueError, AttributeError, RecursionError):
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


def time_cost_for(data, env, root=None, mode=None, tolerance_min=None, cost=None,
                  refocus=None, slope=None):
    from harness_records import time_number, time_value
    policy = data['selection_policy']['time_cost']
    effective = time_value(policy)
    source = 'bindings'
    marker = mission_marker(Path.cwd() if root is None else root, env.get('HARNESS_SESSION_ID'))
    if marker is not None:
        try:
            declaration = json.loads(marker.read_text(encoding='utf-8'))['time_cost']
            if not isinstance(declaration, dict) or any(declaration.get(key) is None for key in ('mode', 'tolerance_min', 'k')):
                raise ValueError('invalid session time declaration')
            for key in ('refocus_usd', 'slope_usd_per_tolerance', 'switch_after_min'):
                if key in declaration:
                    time_number(declaration[key])
            effective = time_value(policy, declaration['mode'], declaration['tolerance_min'], declaration['k'],
                                   declaration.get('refocus_usd'), declaration.get('slope_usd_per_tolerance'),
                                   declaration.get('switch_after_min'))
            source = 'session'
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            pass
    if any(value is not None for value in (mode, tolerance_min, cost, refocus, slope)):
        # A new mode selects its bindings parameters; other CLI fields overlay the session.
        effective = time_value(policy, mode if mode is not None else effective['mode'],
                               tolerance_min if tolerance_min is not None else effective['tolerance_min'],
                               cost if cost is not None else (None if mode is not None else effective['k']),
                               refocus if refocus is not None else (None if mode is not None else effective['refocus_usd']),
                               slope if slope is not None else (None if mode is not None else effective['slope_usd_per_tolerance']),
                               effective['switch_after_min'])
        source = 'cli'
    return dict(effective, source=source)


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
    # S is selected only by an S floor, never as a higher ordinary band.
    return worker['tier'] != 'S' and data['bands']['order'].index(worker['tier']) >= data['bands']['order'].index(floor)


def assessment_floor(data, role, assess, recent_failure):
    policy = data['selection_policy']['assessment']
    required = set(policy['dimensions']) - {'volume'}
    if (not isinstance(assess, dict) or not required <= set(assess) <= required | {'volume'} or
            any(type(v) is not int or v not in (0, 1, 2) for v in assess.values())):
        raise ValueError('assessment requires exactly open,tangle,precedent,verifier,consequence, each 0|1|2; optional volume=0|1|2')
    difficulty = max(assess[key] for key in policy['difficulty'])
    exposure = max(assess[key] for key in policy['exposure'])
    computed = policy['mapping'][difficulty][exposure]
    sources = ', '.join(key for key in policy['difficulty'] if assess[key] == difficulty)
    detail = f' ({sources})' if difficulty else ''
    reason = f'assessed difficulty {difficulty}{detail}, exposure {exposure} -> band {computed}; '
    order = data['bands']['order']
    if recent_failure:
        raised = order[min(order.index(computed) + 1, len(order) - 1)]
        origin = (f" (run {recent_failure['run']}, directory {recent_failure['directory']})"
                  if isinstance(recent_failure, dict) else '')
        reason += f'recent reasoning failure{origin} raises {computed} -> {raised}; '
        computed = raised
    minimum = policy['minimum_by_role'].get(role, 'band_floor')
    if minimum == 'band_floor':
        minimum = data['roles'][role]['band_floor']
    applied = max((computed, minimum), key=order.index)
    if applied != computed:
        reason += f'role minimum {minimum} raises {computed} -> {applied}; '
    return dict(difficulty=difficulty, exposure=exposure, computed=computed,
                applied=applied, recent_failure=recent_failure or False), reason


def worker_estimate(row, estimates, volume, reports):
    from harness_records import parse_assessment_header, time_number
    if volume in row.get('estimates', {}):
        seed = row['estimates'][volume]
        return time_number(seed['usd']), time_number(seed['minutes']), 'seed'
    policy = estimates['worker']
    usd = metric(row, 'cost') * policy['task_units'][volume]
    elapsed = []
    for _, fields, _ in reports:
        try:
            status = fields.get('STATUS', '')
            if not re.match(r'^DONE(?:\s|$)', status):
                continue
            settings = dict(re.findall(r'(?:\(|,\s*)(model|effort)=([^,()\s]+)', status))
            # Only launcher-written STATUS/BINDINGS headers identify settings.
            # AGY writes no model or BINDINGS, so its reports cannot match a row.
            for key, value in re.findall(r'\b(model|effort)=([^,()\s]+)', fields.get('BINDINGS', '')):
                settings.setdefault(key, value)
            if settings.get('model') != row['model'] or settings.get('effort') != (row.get('effort') or 'unspecified'):
                continue
            if parse_assessment_header(fields.get('ASSESS', '')).get('volume') != int(volume):
                continue
            duration = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)s', fields.get('ELAPSED', ''))
            if duration:
                seconds = time_number(float(duration[1]))
                if seconds <= 30 * 24 * 60 * 60:
                    elapsed.append(seconds / 60)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    if len(elapsed) >= max(1, policy['min_records']):
        return usd, statistics.median(elapsed), 'records'
    ttft = (row.get('metrics') or {}).get('ttft_s')
    factor = 1 if ttft is None else math.sqrt(ttft / policy['reference_ttft_s'])
    lower, upper = policy['speed_clamp']
    return usd, policy['base_minutes'][volume] * min(upper, max(lower, factor)), 'seed'


def score_option(option, usd, minutes, minutes_source, time_cost, exponent):
    finite_number(usd, 'score usd')
    finite_number(minutes, 'score minutes')
    try:
        if (time_cost.get('legacy_convex') or 'mode' not in time_cost
                or (time_cost['mode'] == 'attended' and minutes <= time_cost['switch_after_min'])):
            waiting = 0 if time_cost['k'] == 0 else time_cost['k'] * (minutes / time_cost['tolerance_min']) ** exponent
        else:
            elapsed = minutes - time_cost['switch_after_min'] if time_cost['mode'] == 'attended' else minutes
            slope = time_cost['slope_usd_per_tolerance']
            waiting = time_cost['refocus_usd'] + (0 if slope == 0 else slope * (elapsed / time_cost['tolerance_min']))
    except OverflowError as error:
        raise ValueError('score exceeds finite numeric range: ' + option) from error
    finite_number(waiting, 'score time cost')
    finite_number(usd + waiting, 'score total')
    return dict(option=option, usd=usd, minutes=minutes, minutes_source=minutes_source,
                time_cost=waiting, total=usd + waiting)


def score_order(scores, priorities):
    """Availability and trust come first; within each group a 1% tie favors time."""
    ordered = []
    for priority_group in sorted({priorities[s['option']] for s in scores}):
        remaining = sorted((s for s in scores if priorities[s['option']] == priority_group),
                           key=lambda s: (s['total'], s['minutes'], s['option']))
        while remaining:
            ceiling = remaining[0]['total'] * 1.01
            tied = [s for s in remaining if s['total'] <= ceiling]
            winner = min(tied, key=lambda s: (s['minutes'], s['total'], s['option']))
            ordered.append(winner)
            remaining.remove(winner)
    return ordered


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
            retry_reason=None, attempt=0, allow_ultra=False, assess=None,
            recent_failure=False, direct_band=None, time_mode=None,
            time_tolerance_min=None, time_cost=None, paths=None, time_refocus=None,
            time_slope=None):
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
    assessment, assessment_reason = None, ''
    reports = []
    if assess is not None and tier is None and worker is None and retry_from is None:
        if paths or (isinstance(assess, dict) and 'volume' in assess):
            from harness_records import recent_reports, reasoning_failure
            reports = list(recent_reports(data['selection_policy']['estimates']['history_days']))
            if paths and not recent_failure:
                recent_failure = reasoning_failure(reports, paths)
        assessment, assessment_reason = assessment_floor(data, role, assess, recent_failure)
        floor = assessment['applied']
    scoring = assessment is not None and 'volume' in assess
    if direct_band is not None and direct_band not in data['bands']['order']:
        raise ValueError('invalid direct band')
    latency = latency or data['roles'][role].get('latency_default', 'foreground')
    if floor not in data['bands']['order'] or latency not in ('interactive', 'foreground', 'detached'):
        raise ValueError('invalid band or latency')
    if attempt < 0:
        raise ValueError('attempt must be non-negative')
    if bool(retry_from) != bool(retry_reason) or (attempt and not retry_from):
        raise ValueError('retry requires --retry-from and --retry-reason')
    if retry_from and (worker or attempt < 1):
        raise ValueError('retry requires positive --attempt and cannot combine with --worker')
    if retry_reason and retry_reason not in RETRY_REASONS:
        raise ValueError('invalid retry reason')
    by_id = {w['id']: w for w in data['workers']}
    for ident in (worker, retry_from):
        if ident is not None and ident not in by_id:
            raise ValueError('unknown worker id: ' + ident)
    exhausted = set(budget[10:].split(',')) if budget.startswith('exhausted:') else set()
    previous = by_id.get(retry_from)
    if (previous and retry_reason == 'reasoning' and previous['tier'] == 'S'
            and role in ('implement', 'write') and previous['vendor'] in authors):
        raise ValueError('cross-lane S already failed; orchestrator decides (retry-policy 4)')
    if previous and (role not in previous['roles'] or
            exclusion(previous, role, host, True, data, root, env) or
            (role in SEPARATED_ROLES and previous['vendor'] in authors)):
        raise ValueError('retry worker is not eligible for role/host/authors')
    keep = previous and retry_reason not in ('reasoning', 'refusal') and not (
        retry_reason == 'availability' and previous['vendor'] in exhausted)
    refused_vendor = None
    if previous and retry_reason == 'refusal':
        # A safety refusal is vendor- and task-specific, not exhaustion: keep the
        # band (never below the role floor), drop that vendor from this route
        # only, keep author separation.
        refused_vendor = previous['vendor']
        order = data['bands']['order']
        if not tier and order.index(previous['tier']) > order.index(floor):
            floor = previous['tier']
    explicit = worker or (retry_from if keep else None)
    candidates = [w for w in data['workers'] if role in w['roles'] and
                  (explicit is None or w['id'] == explicit)]
    skipped, eligible, diagnostic = [], [], []
    for w in candidates:
        reason = exclusion(w, role, host, explicit is not None, data, root, env)
        if not reason and refused_vendor and w['vendor'] == refused_vendor:
            reason = 'vendor refused this task (retry-policy refusal)'
        if not reason and explicit is None and w.get('scored') is False:
            reason = 'unscored requires explicit --worker'
        if not reason and scoring and (w.get('metrics') or {}).get('cost') is None:
            reason = 'missing metrics.cost for score'
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
    cross_lane_s = False
    s_retry_effort = None
    if previous and retry_reason == 'reasoning':
        same_lane = [w for w in pool if w['vendor'] == previous['vendor']]
        if previous['tier'] == 'S':
            other_vendor = next((v for v in data['bands']['S']['lanes'] if v != previous['vendor']), None)
            further = [w for w in same_lane if w.get('effort') != previous.get('effort') and
                       within_band(w, data, 'S', 'detached')]
            if previous.get('effort') == data['bands']['S']['foreground_effort'] and further:
                pool = further
                latency = 'detached'
                needs_detached = True
                s_retry_effort = 'detached'
            elif (data['selection_policy'].get('cross_lane_s', False) and role in ('implement', 'write')
                  and other_vendor in authors):
                # Only implementation/writing may waive separation for this final reasoning attempt.
                pool = [w for w in diagnostic if w['vendor'] == other_vendor]
                cross_lane_s = True
                separation_ok = False
                s_retry_effort = 'foreground'
            else:
                raise ValueError('S row already failed at its detached effort; orchestrator decides (retry-policy 4)')
            floor = 'S'
        else:
            pool = same_lane
            order = data['bands']['order']
            floor = order[order.index(previous['tier']) + 1]
            if tier is not None:
                floor = max((floor, tier), key=order.index)
            promotion = [w for w in pool if within_band(w, data, floor, latency)]
            if not promotion:
                floor = 'S'
            s_retry_effort = 'foreground'
        effort_class = s_retry_effort if floor == 'S' else latency
        promotion = [w for w in pool if within_band(w, data, floor, effort_class)]
        fast = [w for w in promotion if within_latency(w, data, latency)]
        pool = fast or promotion
        needs_detached = needs_detached or not bool(fast)
    # The cap counts the first attempt. Only the single cross-lane S route gets an extra slot.
    cap = data['selection_policy']['attempt_cap']
    if attempt > cap or (attempt == cap and not cross_lane_s):
        raise ValueError('attempt cap reached; orchestrator decides (retry-policy §4c)')
    promoting = previous and retry_reason == 'reasoning'
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
            band_met = within_band(w, data, floor, latency)
            if scoring and floor == 'S':
                band_met = any(within_band(w, data, floor, lane) for lane in ('foreground', 'detached'))
            if not band_met:
                reason = 'S only' if w['tier'] == 'S' and floor != 'S' else 'below band or different S effort'
            elif scoring and not allow_ultra and w['vendor'] == 'openai' and w.get('effort') == 'ultra':
                reason = 'Codex worker ultra enables re-delegation'
            elif not scoring and not within_latency(w, data, latency):
                reason = 'latency ' + latency
            if reason:
                skipped.append({'id': w['id'], 'reason': reason})
            else:
                chosen.append(w)
    assessed_detached = False
    if not chosen and assessment is not None:
        chosen = [w for w in pool if (within_band(w, data, floor, latency) or
                                     within_band(w, data, floor, 'detached')) and
                  (allow_ultra or w['vendor'] != 'openai' or w.get('effort') != 'ultra')]
        if chosen:
            assessed_detached = needs_detached = True
    floor_met = bool(chosen)
    score_rows = list(chosen)
    fallback = False
    if not chosen and not explicit and not promoting and floor != 'S':
        chosen = [w for w in pool if w['tier'] != 'S' and w.get('scored') is not False and
                  within_latency(w, data, latency)]
        fallback = True
    ordered = cost_order(chosen, data, exhausted)
    if fallback:
        ordered.sort(key=lambda w: (w['vendor'] in exhausted, w.get('trust') == 'low',
                     -data['bands']['order'].index(w['tier']), metric(w, 'cost'), metric(w, 'ttft_s')))
    if not ordered:
        raise IndexError('no eligible worker meets latency and hard constraints')
    if cross_lane_s or assessed_detached:
        eligible_ids = {w['id'] for w in ordered}
        skipped = [item for item in skipped if item['id'] not in eligible_ids]
    cheapest = selected = ordered[0]
    scores, ranked_scores, effective_time = [], [], None
    if scoring:
        from harness_records import time_number
        effective_time = time_cost_for(data, env, root, time_mode, time_tolerance_min, time_cost,
                                       time_refocus, time_slope)
        exponent = time_number(data['selection_policy']['time_cost']['exponent'], True)
        estimates = data['selection_policy']['estimates']
        volume = str(assess['volume'])
        overhead = estimates['delegate_overhead'][volume]
        priorities = {}
        for row in score_rows:
            usd, minutes, source = worker_estimate(row, estimates, volume, reports)
            scores.append(score_option(row['id'], overhead['usd'] + usd,
                                       overhead['minutes'] + minutes, source, effective_time, exponent))
            priorities[row['id']] = (row['vendor'] in exhausted, row.get('trust') == 'low')
        if (direct_band is not None and role in ('implement', 'write')
                and data['bands']['order'].index(direct_band) >= data['bands']['order'].index(floor)):
            direct = estimates['direct'][volume]
            scores.append(score_option('direct', direct['usd'], direct['minutes'], 'seed', effective_time, exponent))
            priorities['direct'] = ({'codex': 'openai', 'claude': 'claude'}[host] in exhausted, False)
        ranked_scores = score_order(scores, priorities)
        worker_scores = [score for score in ranked_scores if score['option'] != 'direct']
        if ranked_scores and ranked_scores[0]['option'] == 'direct':
            worker_scores = score_order(worker_scores, priorities)
        worker_order = [score['option'] for score in worker_scores]
        if worker_order:
            ordered = [by_id[ident] for ident in worker_order]
            selected = ordered[0]
            needs_detached = not within_latency(selected, data, latency)
    if explicit:
        floor_met = within_band(selected, data, floor, latency) and selected.get('scored') is not False
    vendor = selected['vendor']
    if not allow_ultra and vendor == 'openai' and selected.get('effort') == 'ultra':
        raise ValueError('Codex worker ultra enables re-delegation; select a single-agent effort')
    metrics = (selected if scoring else cheapest).get('metrics', {})
    limit = data['latency'][latency]
    label = latency if limit is None else f'{latency}<={limit}s'
    if scoring and needs_detached:
        label = f'detached (scored row exceeds {latency} latency; run with -b --wait)'
    elif assessed_detached:
        label = f'detached (assessed band {floor} is only available detached; run with -b --wait)'
    elif needs_detached:
        label = ('detached (S effort step; run with -b --wait)' if s_retry_effort == 'detached' else
                 f'detached (no {latency} row meets band {floor} promotion; run with -b --wait)')
    choice = 'best worker score' if scoring else 'cheapest'
    named = selected if scoring else cheapest
    reason = (f'band {floor} floor, {label}: {choice} of {len(ordered)} eligible = '
              f"{named['id']} ({metrics.get('index')}, ${metrics.get('cost')}, {metrics.get('ttft_s')}s)")
    if fallback:
        reason = f"no row meets band {floor} in {latency}; best available is {selected['id']}"
    if retry_reason == 'deadline':
        needs_detached = True
        reason = ('deadline retry keeps the worker; rerun detached (-b --wait) with a longer -t; '
                  + reason)
    elif retry_reason == 'refusal':
        reason = f'refusal retry: vendor {refused_vendor} refused this task; same band, other vendor; ' + reason
    elif keep or retry_reason == 'availability':
        reason = f'retry keeps settings; fix {retry_reason}; ' + reason
    elif previous:
        reason = 'reasoning retry; ' + reason
    reason = assessment_reason + reason
    if ranked_scores:
        winner = ranked_scores[0]
        reason += (f"; score: {winner['option']} ${winner['total']:.2f} "
                   f"(usd {winner['usd']:.2f} + time {winner['time_cost']:.2f})")
        if len(ranked_scores) > 1:
            runner = ranked_scores[1]
            relation = '<' if winner['total'] < runner['total'] else 'ahead of'
            reason += f" {relation} {runner['option']} ${runner['total']:.2f}"
        reason += (f"; time {effective_time['mode']} T={effective_time['tolerance_min']:g} "
                   f"k={effective_time['k']:g}")
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
                 tier_fallback=not floor_met, available=(separation_ok or cross_lane_s) and vendor not in exhausted)
    if scoring:
        route['decision'] = 'direct' if ranked_scores and ranked_scores[0]['option'] == 'direct' else 'delegate'
        route['time_cost'] = {key: round(value, 2) if type(value) in (int, float) else value
                              for key, value in effective_time.items()}
        route['scores'] = [{key: round(value, 2) if type(value) in (int, float) else value
                            for key, value in score.items()}
                           for score in sorted(scores, key=lambda s: (s['total'], s['minutes'], s['option']))]
    if assessment is not None:
        route['assessment_floor'] = assessment
        if assess['open'] == 2:
            route['shape'] = 'decide-first'
        if role == 'implement' and floor in ('A', 'S'):
            route['plan_first'] = True
    if role == 'web' and vendor == 'claude':
        route['claude_role'] = 'web'
    if role in SEPARATED_ROLES:
        route.update(author_vendors=sorted(authors), separation_satisfied=separation_ok)
    if cross_lane_s:
        route['cross_lane_s'] = True
        route['separation_satisfied'] = False
        route['reason'] += ('; cross-lane S: author separation is waived for this single attempt; '
                            'the result must be reviewed by a different vendor than the implementer')
    elif not separation_ok:
        route['reason'] += '; no configured route is independent of the artifact authors; do not claim cross-review'
    if (separation_ok or cross_lane_s) and budget == 'tight' and retry_reason == 'reasoning':
        route['available'] = False
        route['reason'] += '; tight budget disables optional reasoning promotions'
    elif (separation_ok or cross_lane_s) and vendor in exhausted:
        route['reason'] += ('; independent reviewer is exhausted; report required review as incomplete; do not use implementation fallback'
                            if role in REVIEW_ROLES else '; selected vendor is declared exhausted; follow fallback policy')
    if assessment is not None:
        from harness_records import ASSESSMENT_KEYS
        route['launch_env'] = dict(HARNESS_BAND=floor, HARNESS_ROLE=role,
                                  HARNESS_ASSESS=','.join(
                                      f'{key}={assess[key]}' for key in ASSESSMENT_KEYS if key in assess))
        estimates = data['selection_policy'].get('estimates')
        # Without volume, use the small-task seed; timeout always covers the
        # selected worker, even when the cost comparison favors direct work.
        # Estimates are optional in bindings: without them there is no suggestion.
        if estimates:
            volume = str(assess.get('volume', 0))
            _, minutes, _ = worker_estimate(selected, estimates, volume, reports)
            minutes += estimates['delegate_overhead'][volume]['minutes']
            route['suggested_timeout_s'] = max(570, math.ceil(minutes * 1.5 * 60) + 300)
            route['reason'] += f"; suggested -t {route['suggested_timeout_s']}"
            # The launchers accept -t above 570 only detached (-b): the parent's
            # Bash tool kills a foreground call at 600 s. A suggestion past the
            # cap therefore makes the route detached, whatever the TTFT class said.
            if route['suggested_timeout_s'] > FOREGROUND_TIMEOUT_CAP_S:
                route['needs_detached'] = True
                route['reason'] += f' (exceeds the {FOREGROUND_TIMEOUT_CAP_S} s foreground cap; run with -b --wait)'
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
    parser.add_argument('--retry-reason', choices=RETRY_REASONS)
    parser.add_argument('--attempt', type=int, default=0)
    parser.add_argument('--assess', help='five required 0|1|2 ratings and optional volume=0|1|2')
    parser.add_argument('--recent-failure', action='store_true', help='recent reasoning failure in the same area; raises assessed band once')
    parser.add_argument('--paths', help='comma-separated project-relative affected paths for recent failure history')
    parser.add_argument('--direct-band', choices=tuple('SABCDE'), help='orchestrator capability band; offer direct implement/write')
    parser.add_argument('--time-mode', choices=('attended', 'background', 'unattended'))
    parser.add_argument('--time-tolerance-min', type=float)
    parser.add_argument('--time-cost', type=float, help='convex waiting-cost coefficient k in USD')
    parser.add_argument('--time-refocus', type=float, help='fixed USD cost on return after switching away')
    parser.add_argument('--time-slope', type=float, help='USD per tolerance interval after switching away')
    parser.add_argument('--author-vendor', action='append', choices=('openai', 'claude', 'google', 'local'),
                        help='actual author of the design (implement/write/advice) or review target; repeat for coauthors; required for reviews')
    args = parser.parse_args()
    if args.assess is not None:
        try:
            from harness_records import parse_assessment
            args.assess = parse_assessment(args.assess)
        except (ImportError, ValueError) as error:
            parser.error(str(error))
    if args.paths is not None:
        try:
            from harness_records import path_directory
            for value in args.paths.split(','):
                path_directory(value)
        except (ImportError, ValueError) as error:
            parser.error(str(error))
    try:
        if args.time_tolerance_min is not None:
            finite_number(args.time_tolerance_min, 'time tolerance', True)
        if args.time_cost is not None:
            finite_number(args.time_cost, 'time cost')
        if args.time_refocus is not None:
            finite_number(args.time_refocus, 'time refocus')
        if args.time_slope is not None:
            finite_number(args.time_slope, 'time slope')
    except ValueError as error:
        parser.error(str(error))
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
                        retry_from=args.retry_from, retry_reason=args.retry_reason, attempt=args.attempt,
                        assess=args.assess, recent_failure=args.recent_failure, paths=args.paths,
                        direct_band=args.direct_band, time_mode=args.time_mode,
                        time_tolerance_min=args.time_tolerance_min, time_cost=args.time_cost,
                        time_refocus=args.time_refocus, time_slope=args.time_slope)
        route['shell'] = find_bash()
        if route['shell'] is None:
            native_review = (args.role in REVIEW_ROLES and route['available'] and route['separation_satisfied'] and
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
    if args.assess is not None:
        route['assessment'] = args.assess
    print(json.dumps(route, ensure_ascii=True, indent=2))
    return 0 if route['available'] else 2


if __name__ == '__main__':
    sys.exit(main())
