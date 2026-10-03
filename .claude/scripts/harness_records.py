"""Validation and storage shared by recording commands and the router."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

DIMENSIONS = ('open', 'tangle', 'precedent', 'verifier', 'consequence')
ASSESSMENT_KEYS = DIMENSIONS + ('volume',)
FAILURE_CLASSES = ('none', 'infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning', 'defect')
TIME_MODES = ('attended', 'background', 'unattended')
ASSESSMENT_ERROR = ('assessment requires exactly open,tangle,precedent,verifier,consequence, each 0|1|2'
                    '; optional volume=0|1|2')


def parse_assessment(value):
    fields = value.split(',')
    assessment = {}
    for field in fields:
        match = re.fullmatch(r'(open|tangle|precedent|verifier|consequence|volume)=([012])', field)
        if not match or match[1] in assessment:
            raise ValueError(ASSESSMENT_ERROR)
        assessment[match[1]] = int(match[2])
    if not set(DIMENSIONS) <= set(assessment) <= set(ASSESSMENT_KEYS):
        raise ValueError(ASSESSMENT_ERROR)
    return {key: assessment[key] for key in ASSESSMENT_KEYS if key in assessment}


def parse_assessment_header(value):
    assessment = parse_assessment(value.replace(' ', ','))
    if value != ' '.join(f'{key}={level}' for key, level in assessment.items()):
        raise ValueError(ASSESSMENT_ERROR)
    return assessment


def time_number(value, positive=False):
    import math
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value < 0 or (positive and value == 0):
        raise ValueError('time value must be a finite ' + ('positive' if positive else 'non-negative') + ' number')
    return value


def time_value(policy, mode=None, tolerance_min=None, k=None, refocus_usd=None,
               slope_usd_per_tolerance=None, switch_after_min=None):
    """Normalize one mode; scalar settings retain the old convex curve unless a new cost is supplied."""
    mode = policy['default_mode'] if mode is None else mode
    if mode not in TIME_MODES:
        raise ValueError('time mode must be attended|background|unattended')
    time_number(policy['exponent'], True)
    values = policy['modes'][mode]
    legacy = not isinstance(values, dict)
    if legacy:
        values = dict(k=values, refocus_usd=0, slope_usd_per_tolerance=0)
    effective = dict(mode=mode,
                     tolerance_min=time_number(policy['tolerance_min'] if tolerance_min is None else tolerance_min, True),
                     switch_after_min=time_number(policy.get('switch_after_min', 3) if switch_after_min is None else switch_after_min))
    for key, override in (('k', k), ('refocus_usd', refocus_usd),
                          ('slope_usd_per_tolerance', slope_usd_per_tolerance)):
        effective[key] = time_number(values[key] if override is None else override)
    if legacy and refocus_usd in (None, 0) and slope_usd_per_tolerance in (None, 0):
        effective['legacy_convex'] = True
    return effective


def load_time_policy(root):
    """Read the same public/local time settings without requiring recording-only callers to load the router."""
    folder = Path(root) / '.claude'
    policy = json.loads((folder / 'model-bindings.json').read_text(encoding='utf-8'))['selection_policy']['time_cost']
    local = folder / 'model-bindings.local.json'
    if local.exists():
        override = json.loads(local.read_text(encoding='utf-8')).get('selection_policy', {}).get('time_cost', {})
        if not isinstance(override, dict) or not isinstance(override.get('modes', {}), dict):
            raise ValueError('time_cost and time_cost.modes must be objects')
        modes = dict(policy['modes'])
        for mode, values in override.get('modes', {}).items():
            modes[mode] = ({**modes[mode], **values}
                           if isinstance(modes.get(mode), dict) and isinstance(values, dict) else values)
        policy = {**policy, **override, 'modes': modes}
    return policy


def task_slug(value):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', value):
        raise ValueError('task must match [A-Za-z0-9._-]{1,64}')
    return value


def run_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('run id must match [A-Za-z0-9_-]+')
    return value


def nonnegative(value):
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('number must be a non-negative integer')
    return int(value)


def recorded_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def state_directory():
    # Use the launcher's root/canonicalization logic, including non-Git roots.
    spec = importlib.util.spec_from_file_location('workspace_snapshot', Path(__file__).with_name('workspace-snapshot.py'))
    workspace = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workspace)
    root, _ = workspace.context()
    key = os.environ.get('HARNESS_TREE_KEY') or hashlib.sha1(workspace.canonical(root).encode('utf-8')).hexdigest()[:16]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('bad HARNESS_TREE_KEY')
    base = os.environ.get('HARNESS_STATE_DIR') or Path(os.environ.get('HOME') or Path.home()) / '.claude/harness-runs'
    return Path(base) / key


def recent_reports(history_days):
    """Best-effort headers from this tree only; result text is never routing evidence."""
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=history_days)
        paths = sorted(state_directory().glob('report-*.txt'), reverse=True)
    except (ImportError, OSError, ValueError, TypeError, OverflowError):
        return
    for path in paths:
        try:
            ident = run_id(path.name[7:-4])
            stamp = datetime.strptime(ident.split('-', 1)[0], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            if not cutoff <= stamp <= now:
                continue
            fields = {}
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    if line.startswith(('FINAL_MESSAGE:', 'RESPONSE:')):
                        break
                    name, sep, value = line.rstrip('\r\n').partition(': ')
                    if sep and name not in fields:
                        fields[name] = value
            yield ident, fields, path
        except (OSError, ValueError, TypeError, OverflowError):
            continue


def path_directory(value):
    """Compare project-relative parent directories independently of the host OS."""
    from posixpath import dirname, normpath
    value = value.replace('\\', '/')
    normalized = normpath(value)
    if (not value or normalized in ('.', '..') or normalized.startswith(('/', '../'))
            or re.match(r'^[A-Za-z]:', normalized)):
        raise ValueError('paths must be project-relative files')
    return dirname(normalized).casefold()


def reasoning_failure(reports, paths):
    try:
        directories = {path_directory(value) for value in paths.split(',')}
    except (AttributeError, ValueError):
        return None
    for ident, fields, report in reports:
        try:
            outcome = json.loads(report.with_name('outcome-' + ident + '.json').read_text(encoding='utf-8'))
            if not isinstance(outcome, dict) or outcome.get('class') != 'reasoning':
                continue
            if outcome.get('run', ident) != ident:
                continue
            changed = fields.get('CHANGED', '')
            if changed in ('', 'none', 'unknown'):
                continue
            for value in changed.split(','):
                directory = path_directory(value.strip())
                if directory in directories:
                    return dict(run=ident, directory=directory or '.')
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            continue
    return None


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=True)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
