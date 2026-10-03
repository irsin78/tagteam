#!/usr/bin/env python
"""Run recording commands and validation, without worker calls."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_records as records

spec = importlib.util.spec_from_file_location('harness_session', HERE / 'harness-session.py')
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)
from stop_gate import find_bash
ASSESS = 'consequence=1,verifier=1,precedent=0,tangle=2,open=1'
EXPECTED = dict(open=1, tangle=2, precedent=0, verifier=1, consequence=1)


class Validation(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('HARNESS_DELEGATE_RUN', None)

    def assert_delegate_refused(self, args):
        with patch.dict(os.environ, HARNESS_DELEGATE_RUN='1'), patch.object(
                sys, 'argv', ['harness-session.py', *args]), patch.object(
                records, 'state_directory') as directory, patch.object(
                records, 'write_record') as write, contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as raised:
                session.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('a current orchestrator session id is required', errors.getvalue())
            directory.assert_not_called()
            write.assert_not_called()

    def test_outcome_refuses_delegates_before_reading_or_writing_records(self):
        self.assert_delegate_refused(['outcome', '--run', '20261002T010000Z-42', '--class', 'reasoning'])

    def test_direct_refuses_delegates_before_reading_or_writing_records(self):
        self.assert_delegate_refused(['direct', '--task', 'task', '--result', 'done', '--elapsed-s', '120'])

    def test_cli_errors_preserve_expected_value_messages(self):
        base = ['direct', '--task', 'valid', '--result', 'done', '--elapsed-s', '0']
        cases = [
            (['outcome', '--run', '../x', '--class', 'none'], 'run id must match [A-Za-z0-9_-]+'),
            (base + ['--task', 'has space'], 'task must match [A-Za-z0-9._-]{1,64}'),
            (base + ['--assess', 'open=1'],
             'assessment requires exactly open,tangle,precedent,verifier,consequence, each 0|1|2'),
        ]
        cases += [(base + [option, '-1'], 'number must be a non-negative integer')
                  for option in ('--elapsed-s', '--tokens', '--rework', '--interventions')]
        for args, expected in cases:
            with self.subTest(args=args), patch.object(sys, 'argv', ['harness-session.py', *args]), patch.object(
                    records, 'state_directory') as directory, contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    session.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected, errors.getvalue())
                self.assertNotIn('invalid run_id value', errors.getvalue())
                directory.assert_not_called()

    def test_state_directory_prefers_home_and_explicit_override(self):
        bash_home = HERE / 'bash-home'
        windows_home = HERE / 'windows-home'
        with patch.dict(os.environ), patch.object(Path, 'home', return_value=windows_home):
            os.environ.pop('HARNESS_STATE_DIR', None)
            os.environ['HARNESS_TREE_KEY'] = 'sample-tree'
            os.environ['HOME'] = str(bash_home)
            os.environ['USERPROFILE'] = str(windows_home)
            self.assertEqual(records.state_directory(), bash_home / '.claude/harness-runs/sample-tree')
            os.environ['HARNESS_STATE_DIR'] = str(HERE / 'explicit-state')
            self.assertEqual(records.state_directory(), HERE / 'explicit-state/sample-tree')
            os.environ.pop('HARNESS_STATE_DIR')
            os.environ.pop('HOME')
            self.assertEqual(records.state_directory(), windows_home / '.claude/harness-runs/sample-tree')

    def test_assessment_order_and_exact_keys(self):
        self.assertEqual(records.parse_assessment(ASSESS), EXPECTED)
        self.assertEqual(list(records.parse_assessment(ASSESS)), list(records.DIMENSIONS))
        for value in ('', ASSESS + ',', ASSESS + ',extra=0', ASSESS.replace('open=1', 'tangle=1'),
                      ASSESS.replace('open=1', 'open=3'), ASSESS.replace('open=1', 'open=01'),
                      ASSESS.replace('open=1', 'open=-1'), ASSESS.replace('open=1', ' open=1')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.parse_assessment(value)

    def test_optional_volume_preserves_required_dimensions_and_canonical_order(self):
        for level in (0, 1, 2):
            assessment = records.parse_assessment('volume=' + str(level) + ',' + ASSESS)
            self.assertEqual(assessment, dict(EXPECTED, volume=level))
            self.assertEqual(list(assessment), list(records.ASSESSMENT_KEYS))
            header = ' '.join(f'{key}={value}' for key, value in assessment.items())
            self.assertEqual(records.parse_assessment_header(header), assessment)
        for value in (ASSESS + ',volume=3', ASSESS + ',volume=-1', ASSESS + ',volume=01',
                      ASSESS + ',volume=', ASSESS + ',volume=0,volume=1',
                      'volume=0,' + ASSESS.replace(',open=1', '')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.parse_assessment(value)

    def test_time_numbers_reject_negative_nonfinite_boolean_and_zero_tolerance(self):
        self.assertEqual(records.time_number(0), 0)
        self.assertEqual(records.time_number(.5, True), .5)
        for value in (-1, float('nan'), float('inf'), True, '1', 2 ** 2048):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.time_number(value)
        with self.assertRaises(ValueError):
            records.time_number(0, True)

    def test_time_value_validates_new_parameters_and_exponent(self):
        policy = json.loads((HERE / 'test_fixtures/route-score-v1.json').read_text(encoding='utf-8'))['selection_policy']['time_cost']
        for key in ('refocus_usd', 'slope_usd_per_tolerance', 'switch_after_min'):
            for value in (-1, float('nan'), float('inf'), True, '1', 2 ** 2048):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    records.time_value(policy, **{key: value})
        for value in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(exponent=value), self.assertRaises(ValueError):
                records.time_value(dict(policy, exponent=value))
        self.assertEqual(records.time_value(policy, refocus_usd=0, slope_usd_per_tolerance=0, switch_after_min=0),
                         dict(mode='attended', tolerance_min=30, k=5, refocus_usd=0,
                              slope_usd_per_tolerance=0, switch_after_min=0))

    def test_load_time_policy_merges_partial_mode_objects_like_router(self):
        public = json.loads((HERE / 'test_fixtures/route-score-v1.json').read_text(encoding='utf-8'))
        local = {'selection_policy': {'time_cost': {'modes': {'attended': {'refocus_usd': 2}, 'background': 4}}}}
        with patch.object(Path, 'exists', return_value=True), patch.object(
                Path, 'read_text', autospec=True, side_effect=lambda path, **kwargs:
                json.dumps(local if path.name == 'model-bindings.local.json' else public)):
            policy = records.load_time_policy(HERE)
        self.assertEqual(policy['modes']['attended'], dict(k=5, refocus_usd=2, slope_usd_per_tolerance=1.5))
        self.assertEqual(policy['modes']['background'], 4)
        self.assertEqual(policy['modes']['unattended'], public['selection_policy']['time_cost']['modes']['unattended'])
        for invalid in (None, [], {'modes': None}, {'modes': []}):
            local = {'selection_policy': {'time_cost': invalid}}
            with self.subTest(override=invalid), patch.object(Path, 'exists', return_value=True), patch.object(
                    Path, 'read_text', autospec=True, side_effect=lambda path, **kwargs:
                    json.dumps(local if path.name == 'model-bindings.local.json' else public)), self.assertRaises(ValueError):
                records.load_time_policy(HERE)

    def test_session_cli_serializes_new_overrides_and_preserves_mission_fields(self):
        policy = json.loads((HERE / 'test_fixtures/route-score-v1.json').read_text(encoding='utf-8'))['selection_policy']['time_cost']
        original = dict(mission='docs/missions/task', state='active', exhausted=['google'])
        command = ['harness-session.py', 'time', '--session', 'session-1', '--mode', 'attended',
                   '--tolerance-min', '45', '--cost-at-tolerance', '2', '--refocus-usd', '3',
                   '--slope', '.5', '--reason', 'user switched activity']
        with patch.object(session, 'mission_marker') as marker, patch.object(records, 'load_time_policy', return_value=policy), patch.object(
                sys, 'argv', command), contextlib.redirect_stdout(io.StringIO()):
            marker.return_value.read_text.return_value = json.dumps(original)
            self.assertEqual(session.main(), 0)
            record = json.loads(marker.return_value.write_text.call_args.args[0])
        self.assertEqual(record, dict(original, time_cost=dict(mode='attended', tolerance_min=45, k=2,
                         refocus_usd=3, slope_usd_per_tolerance=.5, switch_after_min=3, reason='user switched activity')))

    def test_slug_run_and_integer_validation(self):
        self.assertEqual(records.task_slug('A.0_-'), 'A.0_-')
        self.assertEqual(records.task_slug('x' * 64), 'x' * 64)
        for value in ('', 'x' * 65, '../task', 'has space', 'task\n', 'é'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.task_slug(value)
        for value in ('', '../run', 'run.txt', 'run\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.run_id(value)
        self.assertEqual(records.nonnegative('0'), 0)
        for value in ('-1', '+1', '1.5', ' 1', '1\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.nonnegative(value)


class Recording(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('HARNESS_DELEGATE_RUN', None)

    def invoke(self, directory, *args):
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['harness-session.py', *args]), patch.object(
                records, 'state_directory', return_value=directory), contextlib.redirect_stdout(stream):
            self.assertEqual(session.main(), 0)
        return Path(stream.getvalue().strip())

    def test_outcome_replaces_record_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            report = directory / 'report-20261001T010000Z-42.txt'
            report.write_text('STATUS: FAILED\n', encoding='utf-8')
            path = self.invoke(directory, 'outcome', '--run', '20261001T010000Z-42',
                               '--class', 'reasoning', '--accepted', 'no', '--note', 'diagnosed')
            data = json.loads(path.read_text())
            self.assertEqual(set(data), {'run', 'class', 'accepted', 'note', 'recorded'})
            self.assertEqual((data['class'], data['accepted'], data['note']), ('reasoning', 'no', 'diagnosed'))
            self.assertRegex(data['recorded'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
            self.assertEqual(path, self.invoke(directory, 'outcome', '--run', data['run'], '--class', 'none'))
            self.assertEqual(json.loads(path.read_text())['accepted'], 'unknown')
            self.assertEqual(report.read_text(), 'STATUS: FAILED\n')

    def test_direct_fields_defaults_and_unique_records(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'tree'
            first = self.invoke(directory, 'direct', '--task', 'phase-1', '--result', 'done',
                                '--elapsed-s', '12', '--assess', ASSESS, '--model', 'host model',
                                '--tokens', '123', '--rework', '2', '--interventions', '1',
                                '--verify', 'passed', '--note', 'verified')
            data = json.loads(first.read_text())
            self.assertEqual(data['assessment'], EXPECTED)
            self.assertEqual((data['task'], data['result'], data['elapsed_s']), ('phase-1', 'done', 12))
            self.assertEqual((data['model'], data['tokens'], data['rework'], data['interventions']),
                             ('host model', 123, 2, 1))
            self.assertEqual((data['verify'], data['note']), ('passed', 'verified'))
            self.assertRegex(first.name, r'^direct-\d{8}T\d{6}Z-[a-f0-9]+\.json$')
            second = self.invoke(directory, 'direct', '--task', 'phase-1', '--result', 'failed', '--elapsed-s', '0')
            self.assertNotEqual(first, second)
            data = json.loads(second.read_text())
            self.assertNotIn('assessment', data)
            self.assertEqual((data['tokens'], data['rework'], data['interventions'], data['verify']), (0, 0, 0, 'none'))

    def test_direct_cli_optional_volume_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            base = ['direct', '--task', 'volume', '--result', 'done', '--elapsed-s', '12', '--assess']
            for level in (0, 1, 2):
                path = self.invoke(directory, *base, 'volume=' + str(level) + ',' + ASSESS)
                self.assertEqual(json.loads(path.read_text())['assessment'], dict(EXPECTED, volume=level))
            before = sorted(directory.iterdir())
            for value in ('3', '-1', '01', 'x', ''):
                with patch.object(sys, 'argv', ['harness-session.py', *base, ASSESS + ',volume=' + value]), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        session.main()
                    self.assertEqual(raised.exception.code, 2)
                    self.assertEqual(sorted(directory.iterdir()), before)

    def test_commands_refuse_bad_input_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            commands = [
                ['outcome', '--run', 'missing', '--class', 'infra'],
                ['outcome', '--run', '../escape', '--class', 'infra'],
                ['outcome', '--run', 'valid', '--class', 'other'],
                ['outcome', '--run', 'valid', '--class', 'none', '--accepted', 'unknown'],
            ]
            base = ['direct', '--task', 'valid', '--result', 'done', '--elapsed-s', '0']
            commands += [base + ['--task', 'invalid task'], base + ['--assess', 'open=1'],
                         base + ['--result', 'blocked'], base + ['--verify', 'unknown']]
            commands += [base + [name, '-1'] for name in ('--elapsed-s', '--tokens', '--rework', '--interventions')]
            for args in commands:
                with self.subTest(args=args), patch.object(sys, 'argv', ['harness-session.py', *args]), patch.object(
                        records, 'state_directory', return_value=directory), contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as raised:
                        session.main()
                    self.assertNotEqual(raised.exception.code, 0)
                    if args[0:3] == ['outcome', '--run', 'missing']:
                        self.assertIn('no report exists for run missing', errors.getvalue())
                    self.assertEqual(list(directory.iterdir()), [])

    def test_state_directory_matches_launcher_context_and_overrides(self):
        bash = find_bash()
        if not bash:
            self.skipTest('Bash is required for launcher state-directory comparison')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HARNESS_TREE_KEY', None)
            os.environ.pop('HARNESS_STATE_DIR', None)
            os.environ['HOME'] = (Path(folder) / 'bash-home').as_posix()
            os.environ['USERPROFILE'] = str(Path(folder) / 'windows-home')
            # Git Bash rewrites HOME into its own mount form (/tmp/...); compare native paths.
            launcher = subprocess.run([bash, '-c', 'TOOL=codex; . "$1"; if command -v cygpath >/dev/null 2>&1; '
                                       'then cygpath -m "$STATE_DIR"; else printf "%s\\n" "$STATE_DIR"; fi',
                                       'run-state-test', (HERE / 'run-state.sh').as_posix()],
                                      capture_output=True, text=True, encoding='utf-8', check=True)
            self.assertEqual(os.path.normcase(os.path.realpath(records.state_directory())),
                             os.path.normcase(os.path.realpath(launcher.stdout.strip())))
            os.environ['HARNESS_STATE_DIR'] = folder
            os.environ['HARNESS_TREE_KEY'] = 'sample-tree'
            self.assertEqual(records.state_directory(), Path(folder) / 'sample-tree')
            os.environ['HARNESS_TREE_KEY'] = '../escape'
            with self.assertRaisesRegex(ValueError, 'bad HARNESS_TREE_KEY'):
                records.state_directory()

    def test_non_git_subdirectories_share_tree(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            # context() uses Path.cwd(); patch it without changing the process cwd.
            root = Path(folder)
            (root / '.claude').mkdir()
            nested = root / 'src/nested'
            nested.mkdir(parents=True)
            for name in ('HARNESS_TREE_KEY', 'HARNESS_STATE_DIR'):
                os.environ.pop(name, None)
            with patch.object(Path, 'cwd', return_value=nested), patch('subprocess.run', return_value=
                    subprocess.CompletedProcess([], 128, b'', b'fatal: not a git repository')):
                key = hashlib.sha1(os.path.normcase(os.path.realpath(root)).encode('utf-8')).hexdigest()[:16]
                self.assertEqual(records.state_directory().name, key)


class SessionTime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / '.claude').mkdir()
        self.bindings = json.loads((HERE / 'test_fixtures/route-score-v1.json').read_text(encoding='utf-8'))
        (self.root / '.claude/model-bindings.json').write_text(json.dumps(self.bindings), encoding='utf-8')
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('HARNESS_DELEGATE_RUN', None)

    def invoke(self, *args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['harness-session.py', 'time', '--session', 'test-session', *args]), patch.object(
                session.os, 'getcwd', return_value=str(self.root)), contextlib.redirect_stdout(output):
            self.assertEqual(session.main(), 0)
        return output.getvalue()

    def test_time_cli_mode_and_explicit_numbers_print_and_preserve_other_fields(self):
        marker = session.mission_marker(self.root, 'test-session')
        marker.parent.mkdir()
        original = dict(mission='docs/missions/task', state='active', exhausted=['google'])
        marker.write_text(json.dumps(original), encoding='utf-8')
        for mode, cost in [('attended', 5), ('background', 0), ('unattended', 0)]:
            message = self.invoke('--mode', mode, '--reason', 'user changed activity')
            record = json.loads(marker.read_text())
            self.assertEqual({key: record[key] for key in original}, original)
            self.assertEqual(record['time_cost'], dict(records.time_value(self.bindings['selection_policy']['time_cost'], mode),
                                                       reason='user changed activity'))
            self.assertIn(f'HARNESS TIME: {mode} T=30 k={cost:g} source=session', message)
        message = self.invoke('--mode', 'background', '--tolerance-min', '60', '--cost-at-tolerance', '0', '--reason', 'overnight')
        self.assertIn('background T=60 k=0', message)
        self.assertEqual(json.loads(marker.read_text())['time_cost']['k'], 0)
        message = self.invoke('--tolerance-min', '10.5', '--cost-at-tolerance', '2.5', '--reason', 'explicit value')
        self.assertIn('attended T=10.5 k=2.5', message)
        session.update_budget(self.root, 'test-session', exhausted='openai')
        self.assertEqual(json.loads(marker.read_text())['time_cost']['k'], 2.5)

    def test_clear_restores_merged_defaults_preserves_other_fields_and_removes_empty_marker(self):
        local = self.root / '.claude/model-bindings.local.json'
        local.write_text(json.dumps({'selection_policy': {'time_cost': {
            'default_mode': 'background', 'tolerance_min': 45, 'modes': {'background': 2}}}}), encoding='utf-8')
        session.update_budget(self.root, 'test-session', exhausted='google')
        self.invoke('--mode', 'unattended', '--reason', 'away')
        self.assertIn('background T=45 k=2 source=bindings (cleared)', self.invoke('--clear'))
        marker = session.mission_marker(self.root, 'test-session')
        self.assertEqual(json.loads(marker.read_text()), {'exhausted': ['google']})
        session.update_budget(self.root, 'test-session', clear=True)
        self.assertFalse(marker.exists())
        self.invoke('--mode', 'attended', '--reason', 'returned')
        self.invoke('--clear')
        self.assertFalse(marker.exists())

    def test_time_cli_refuses_invalid_declarations_without_mutation(self):
        self.invoke('--mode', 'attended', '--reason', 'waiting')
        marker = session.mission_marker(self.root, 'test-session')
        before = marker.read_bytes()
        cases = [[], ['--mode', 'attended'], ['--mode', 'invalid', '--reason', 'x'],
                 ['--mode', 'attended', '--reason', ' '], ['--reason', 'x'],
                 ['--tolerance-min', '10', '--reason', 'x'], ['--cost-at-tolerance', '0', '--reason', 'x'],
                 ['--clear', '--mode', 'attended'], ['--clear', '--tolerance-min', '1'],
                 ['--clear', '--cost-at-tolerance', '0'], ['--clear', '--refocus-usd', '0'],
                 ['--clear', '--slope', '0'], ['--refocus-usd', '1', '--reason', 'x'], ['--slope', '1', '--reason', 'x']]
        cases += [['--mode', 'attended', option, value, '--reason', 'x']
                  for option, values in [('--tolerance-min', ('0', '-1', 'nan', 'inf', 'bad')),
                                         ('--cost-at-tolerance', ('-1', 'nan', 'inf', 'bad')),
                                         ('--refocus-usd', ('-1', 'nan', 'inf', 'bad')),
                                         ('--slope', ('-1', 'nan', 'inf', 'bad'))]
                  for value in values]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke(*args)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(marker.read_bytes(), before)
        with patch.dict(os.environ, HARNESS_DELEGATE_RUN='1'), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit):
                self.invoke('--mode', 'unattended', '--reason', 'not authorized')
            self.assertIn('orchestrator session id', errors.getvalue())
            self.assertEqual(marker.read_bytes(), before)

    def test_router_reads_only_named_session_cli_then_session_then_local_then_public(self):
        route_spec = importlib.util.spec_from_file_location('time_test_route', HERE / 'harness-route.py')
        routes = importlib.util.module_from_spec(route_spec)
        route_spec.loader.exec_module(routes)
        data = routes.load_bindings(self.root)
        self.assertEqual(routes.time_cost_for(data, {}, self.root)['k'], 5)
        (self.root / '.claude/model-bindings.local.json').write_text(json.dumps({'selection_policy': {
            'time_cost': {'default_mode': 'background', 'modes': {'background': 2}}}}), encoding='utf-8')
        data = routes.load_bindings(self.root)
        self.assertEqual(routes.time_cost_for(data, {}, self.root)['k'], 2)
        self.invoke('--mode', 'unattended', '--tolerance-min', '60', '--reason', 'away')
        env = {'HARNESS_SESSION_ID': 'test-session'}
        self.assertEqual(routes.time_cost_for(data, env, self.root), dict(mode='unattended', tolerance_min=60, k=0,
                         refocus_usd=0, slope_usd_per_tolerance=.25, switch_after_min=3, source='session'))
        self.assertEqual(routes.time_cost_for(data, env, self.root, cost=7)['k'], 7)
        self.assertEqual(routes.time_cost_for(data, {'HARNESS_SESSION_ID': 'another'}, self.root)['k'], 2)
        self.invoke('--clear')
        self.assertEqual(routes.time_cost_for(data, env, self.root)['source'], 'bindings')

    def test_new_overrides_use_merged_mode_parameters_and_survive_router_read(self):
        local = self.root / '.claude/model-bindings.local.json'
        local.write_text(json.dumps({'selection_policy': {'time_cost': {
            'switch_after_min': 7, 'modes': {'attended': {'refocus_usd': 4, 'slope_usd_per_tolerance': 2}}}}}),
                         encoding='utf-8')
        self.invoke('--mode', 'attended', '--reason', 'waiting')
        marker = session.mission_marker(self.root, 'test-session')
        effective = dict(mode='attended', tolerance_min=30, k=5, refocus_usd=4,
                         slope_usd_per_tolerance=2, switch_after_min=7)
        self.assertEqual(json.loads(marker.read_text())['time_cost'], dict(effective, reason='waiting'))
        message = self.invoke('--mode', 'attended', '--tolerance-min', '60', '--cost-at-tolerance', '3',
                              '--refocus-usd', '0', '--slope', '.5', '--reason', 'custom')
        expected = dict(effective, tolerance_min=60, k=3, refocus_usd=0, slope_usd_per_tolerance=.5)
        self.assertEqual(json.loads(marker.read_text())['time_cost'], dict(expected, reason='custom'))
        self.assertIn('refocus=0 slope=0.5 switch=7', message)
        route_spec = importlib.util.spec_from_file_location('override_time_route', HERE / 'harness-route.py')
        routes = importlib.util.module_from_spec(route_spec)
        route_spec.loader.exec_module(routes)
        data = routes.load_bindings(self.root)
        env = {'HARNESS_SESSION_ID': 'test-session'}
        self.assertEqual(routes.time_cost_for(data, env, self.root), dict(expected, source='session'))
        self.assertEqual(routes.time_cost_for(data, env, self.root, refocus=1), dict(expected, refocus_usd=1, source='cli'))
        self.assertEqual(routes.time_cost_for(data, env, self.root, slope=0), dict(expected, slope_usd_per_tolerance=0, source='cli'))

    def test_legacy_local_mode_session_keeps_convex_semantics(self):
        local = self.root / '.claude/model-bindings.local.json'
        local.write_text(json.dumps({'selection_policy': {'time_cost': {'modes': {'attended': 2}}}}), encoding='utf-8')
        self.invoke('--mode', 'attended', '--reason', 'legacy')
        marker = session.mission_marker(self.root, 'test-session')
        time = json.loads(marker.read_text())['time_cost']
        self.assertEqual((time['k'], time['refocus_usd'], time['slope_usd_per_tolerance']), (2, 0, 0))
        self.assertTrue(time['legacy_convex'])


if __name__ == '__main__':
    unittest.main()
