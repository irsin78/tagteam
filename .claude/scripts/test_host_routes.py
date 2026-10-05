#!/usr/bin/env python
"""Host routing and explicit lifecycle regression tests, no model calls."""
import importlib.util
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_records as records
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('harness_route', HERE / 'harness-route.py')
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)
session_spec = importlib.util.spec_from_file_location('harness_session', HERE / 'harness-session.py')
session = importlib.util.module_from_spec(session_spec)
session_spec.loader.exec_module(session)


class HostRoutes(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'AGENTS.md.template').exists(), 'template source only')
    def test_direct_delegation_instructions_point_to_score_and_scout(self):
        instructions = (ROOT / 'AGENTS.md.template').read_text(encoding='utf-8')
        paragraph = ('Before choosing direct implementation or delegation, read only "Direct work or '
                     'delegation" in `docs/orchestration/delegation-matrix.md`; when delegating, also '
                     'its assignment, author-separation and announcement sections. Direct work or '
                     'delegation is decided by the cost score in the delegation matrix, with the '
                     'scout brief first (Thin orchestration). Read the fallback '
                     'section when a vendor is unavailable or exhausted (no probe needed), and '
                     '`docs/orchestration/retry-policy.md` when a run fails.')
        self.assertIn(paragraph, ' '.join(instructions.split()))
        for path in [ROOT / 'AGENTS.md.template', *sorted((ROOT / '.claude/rules').glob('*.md'))]:
            with self.subTest(path=path):
                self.assertNotIn('Interacting new behaviors take the cross-vendor route even inside one file.',
                                 ' '.join(path.read_text(encoding='utf-8').split()))

    def test_local_example_keeps_public_time_defaults_and_points_to_session_declaration(self):
        example = json.loads((ROOT / '.claude/model-bindings.local.json.example').read_text(encoding='utf-8'))
        merged = routes.merge_bindings(self.data, example)
        self.assertEqual(merged['selection_policy']['time_cost'], self.data['selection_policy']['time_cost'])
        self.assertEqual(example['selection_policy']['time_cost']['default_mode'], 'attended')
        self.assertIn('harness-session.py time', example['time_cost_comment'])
        self.assertIn('normal way to change', example['time_cost_comment'])

    def test_cli_assessment_sets_start_floor_and_records_ratings(self):
        assessment = 'consequence=1,verifier=1,precedent=0,tangle=2,open=1'
        for recent, band, ident in [(False, 'A', 'opus55-high'), (True, 'S', 'fable-high')]:
            stream = io.StringIO()
            extra = ['--recent-failure'] if recent else []
            with patch.object(routes, 'load_bindings', return_value=self.data), patch.object(
                    routes, 'find_bash', return_value='bash'), patch.object(
                    routes, 'budget_for', return_value='normal'), patch.object(sys, 'argv', [
                    'harness-route.py', '--host', 'codex', '--role', 'implement', '--assess', assessment,
                    *extra]), contextlib.redirect_stdout(stream):
                self.assertEqual(routes.main(), 0)
            output = json.loads(stream.getvalue())
            self.assertEqual(output['assessment'], dict(open=1, tangle=2, precedent=0, verifier=1, consequence=1))
            self.assertEqual((output['band'], output['worker_id']), (band, ident))
            self.assertEqual(output['assessment_floor']['recent_failure'], recent)
            self.assertTrue(output['plan_first'])

    def test_invalid_assessment_uses_router_argument_error(self):
        for value in ('', 'open=1', 'open=1,tangle=2,precedent=0,verifier=1,consequence=3',
                      'open=1,tangle=2,precedent=0,verifier=1,open=1',
                      'open=1,tangle=2,precedent=0,verifier=1,consequence=1,extra=0'):
            with self.subTest(value=value), patch.object(sys, 'argv', [
                    'harness-route.py', '--host', 'codex', '--role', 'implement', '--assess', value
                    ]), patch.object(routes, 'resolve') as select, contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    routes.main()
                self.assertNotEqual(raised.exception.code, 0)
                self.assertIn('assessment requires exactly open,tangle,precedent,verifier,consequence, each 0|1|2',
                              errors.getvalue())
                select.assert_not_called()

    def test_legacy_commands_work_without_recording_module(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'path', list(sys.path)):
            scripts = Path(folder) / '.claude/scripts'
            scripts.mkdir(parents=True)
            for name in ('harness-route.py', 'harness-session.py'):
                shutil.copyfile(HERE / name, scripts / name)
            self.assertFalse((scripts / 'harness_records.py').exists())
            # Prevent another checkout or this process's module cache masking the missing file.
            with patch.dict(sys.modules, {'harness_records': None}):
                loaded = {}
                for name in ('harness-route', 'harness-session'):
                    spec = importlib.util.spec_from_file_location(name, scripts / (name + '.py'))
                    loaded[name] = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(loaded[name])
                route = loaded['harness-route']
                for extra in (['--host', 'codex'], ['--launcher-default', '--vendor', 'openai']):
                    stream = io.StringIO()
                    with patch.object(route, 'load_bindings', return_value=self.data), patch.object(
                            route, 'budget_for', return_value='normal'), patch.object(
                            route, 'find_bash', return_value='bash'), patch.object(sys, 'argv', [
                            'harness-route.py', '--role', 'implement', *extra]), contextlib.redirect_stdout(stream):
                        self.assertEqual(route.main(), 0)
                    if '--launcher-default' in extra:
                        self.assertTrue(stream.getvalue().startswith('public'))
                    else:
                        self.assertTrue(json.loads(stream.getvalue())['available'])
                legacy_session = loaded['harness-session']
                stream = io.StringIO()
                with patch.object(legacy_session, 'check_codex_hooks', return_value=4), patch.object(
                        sys, 'argv', ['harness-session.py', 'check-codex-hooks']), contextlib.redirect_stdout(stream):
                    self.assertEqual(legacy_session.main(), 0)
                self.assertIn('HOOKS_TRUSTED: 4 configured handlers', stream.getvalue())

    def setUp(self):
        # Public bindings only: a developer's model-bindings.local.json (declared local
        # endpoint, budget) must not change these expectations.
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        self.data = routes.merge_bindings(public, {})
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('HARNESS_DELEGATE_RUN', None)

    def cell(self, tier, vendor):
        return next(w for w in self.data['workers'] if w.get('tier_cell') and w['tier'] == tier and w['vendor'] == vendor)

    def override(self, ident, **fields):
        return routes.merge_bindings(self.data, {'workers_local': [dict(id=ident, **fields)]})

    def sol61_unverified(self):
        # Pre-rollout state (2026-09-30): GPT-6.1 Sol listed but not runnable on the account.
        rows = [dict(id=w['id'], status='unverified') for w in self.data['workers'] if w['model'] == 'gpt-6.1-sol']
        return routes.merge_bindings(self.data, {'workers_local': rows})

    def with_test_reader(self):
        return self.override('test-reader', vendor='google', model='test-model',
                             effort='medium', tier='C', status='optional',
                             metrics=dict(index=40, cost=.5, ttft_s=2, tps=None, provisional=True),
                             launcher='.claude/scripts/agy-run.sh',
                             roles={'explore': {'priority': 1, 'requires': {
                                 'agy_grant': 'write_file',
                                 'agent_file': '.agents/agents/project-agent.md'}}})

    def test_band_floor_and_s_lane_are_distinct(self):
        for band in ('A', 'B', 'C'):
            result = routes.resolve(self.data, 'codex', 'implement', tier=band)
            rows = [w for w in self.data['workers'] if w['id'] in result['eligible']]
            self.assertTrue(all(w['tier'] != 'S' for w in rows))
            self.assertTrue(all(self.data['bands']['order'].index(w['tier']) >=
                                self.data['bands']['order'].index(band) for w in rows))
        for host, model in [('codex', 'claude-fable-5-1'), ('claude', 'gpt-6-astra')]:
            for latency, effort in [('foreground', 'high'), ('detached', 'xhigh')]:
                result = routes.resolve(self.data, host, 'implement', tier='S', latency=latency)
                self.assertEqual((result['model'], result['effort']), (model, effort))
                self.assertTrue(result['floor_met'])

    def test_latency_and_cost_tie(self):
        data = self.override('opus55-medium', tier='C', tier_cell=False,
                             metrics={'cost': .55, 'ttft_s': 14.3})
        for latency in ('foreground', 'interactive'):
            result = routes.resolve(data, 'codex', 'implement', tier='C', latency=latency)
            self.assertEqual(result['worker_id'], 'sonnet55-medium')
            self.assertEqual('opus55-medium' in result['eligible'], latency == 'foreground')
        result = routes.resolve(data, 'codex', 'implement', tier='C')
        self.assertIn('ties: sonnet55-medium, opus55-medium', result['reason'])
        result = routes.resolve(self.data, 'claude', 'implement', latency='detached')
        self.assertIn('sol61-max', result['eligible'])
        self.assertTrue(result['floor_met'])

    def test_unverified_and_unscored_never_enter_fallback(self):
        active = routes.resolve(self.data, 'claude', 'implement')
        self.assertEqual(active['worker_id'], 'sol61-medium')
        self.assertTrue(active['floor_met'])
        result = routes.resolve(self.sol61_unverified(), 'claude', 'implement')
        self.assertEqual(result['worker_id'], 'luna6-high')
        self.assertFalse(result['floor_met'])
        self.assertIn('no row meets band B in foreground', result['reason'])
        self.assertFalse(any(ident.startswith('sol61-') for ident in result['eligible']))
        unscored = routes.merge_bindings(self.sol61_unverified(), {'workers_local': [
            {'id': 'sol61-medium', 'status': 'active', 'scored': False}]})
        self.assertNotIn('sol61-medium', routes.resolve(unscored, 'claude', 'implement')['eligible'])
        explicit = routes.resolve(unscored, 'claude', 'implement', worker='sol61-medium')
        self.assertEqual(explicit['worker_id'], 'sol61-medium')
        self.assertFalse(explicit['floor_met'])

    def test_reasoning_retries_promote_then_choose_s(self):
        for previous, attempt, expected in [('opus55-medium', 1, 'opus55-high'),
                                             ('opus55-high', 2, 'fable-high')]:
            result = routes.resolve(self.data, 'codex', 'implement', retry_from=previous,
                                    retry_reason='reasoning', attempt=attempt)
            self.assertEqual(result['worker_id'], expected)
        detached = routes.resolve(self.data, 'claude', 'implement', retry_from='sol61-medium',
                                  retry_reason='reasoning', attempt=1)
        self.assertEqual(detached['worker_id'], 'sol61-max')
        self.assertTrue(detached['needs_detached'])
        flat = routes.resolve(self.data, 'claude', 'implement', retry_from='sol61-xhigh',
                              retry_reason='reasoning', attempt=1)
        self.assertEqual(flat['worker_id'], 'sol61-max')
        self.assertEqual(flat['band'], 'A')
        with self.assertRaisesRegex(ValueError, 'attempt cap reached'):
            routes.resolve(self.data, 'codex', 'implement', retry_from='opus55-high',
                           retry_reason='reasoning', attempt=3)

    def test_nonreasoning_retries_keep_settings_and_availability_reselects(self):
        for reason in ('infra', 'availability', 'spec', 'scope', 'knowledge', 'defect'):
            result = routes.resolve(self.data, 'codex', 'implement', retry_from='opus55-high',
                                    retry_reason=reason, attempt=1)
            self.assertEqual(result['worker_id'], 'opus55-high')
            self.assertIn('retry keeps settings; fix ' + reason, result['reason'])
        result = routes.resolve(self.data, 'codex', 'implement', retry_from='sol61-high',
                                author_vendors=['google'], retry_reason='availability',
                                attempt=1, budget='exhausted:openai', tier='C')
        self.assertEqual(result['vendor'], 'claude')
        self.assertTrue(result['available'])

    def test_v3_schema_rejects_missing_evidence_and_invalid_metrics(self):
        for fields in ({'metrics': None}, {'probe': None},
                       {'metrics': {'index': None}}, {'metrics': {'index': -1}},
                       {'metrics': {'index': float('nan')}}, {'metrics': {'cost': -1}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.override('sol61-high', **fields)
        with self.assertRaisesRegex(ValueError, 'fixed lane'):
            self.override('astra-high', model='gpt-6.1-sol')
        self.assertIsNotNone(self.override('sol61-high', tier='D', tier_cell=False))
        for key in ('bands', 'latency'):
            invalid = dict(self.data)
            invalid.pop(key)
            with self.assertRaises(ValueError): routes.validate_bindings(invalid)

    def test_s_lane_does_not_substitute_an_ordinary_model(self):
        with self.assertRaisesRegex(IndexError, 'no eligible worker'):
            routes.resolve(self.data, 'codex', 'implement', tier='S', latency='interactive')

    def test_provisional_precedes_cost_and_unscored_promotion_is_excluded(self):
        data = self.override('sol61-medium', status='active', probe={'result': 'OK'})
        ordinary = routes.resolve(data, 'claude', 'implement')
        self.assertEqual(ordinary['worker_id'], 'sol61-medium')
        self.assertTrue(ordinary['floor_met'])
        data = routes.merge_bindings(data, {'workers_local': [
            {'id': 'sol61-high', 'status': 'active', 'probe': {'result': 'OK'},
             'metrics': {'provisional': False, 'secondary_pass1': .7}}]})
        result = routes.resolve(data, 'claude', 'implement')
        self.assertEqual(result['worker_id'], 'sol61-high')
        unscored = self.override('sol61-max', scored=False)
        result = routes.resolve(unscored, 'claude', 'implement', retry_from='sol61-high',
                                retry_reason='reasoning', attempt=1)
        self.assertEqual(result['worker_id'], 'astra-high')
        self.assertNotIn('sol61-max', result['eligible'])

    def test_retry_argument_validation_and_removed_step(self):
        for options in (dict(retry_from='opus55-high'), dict(attempt=1),
                        dict(retry_from='opus55-high', retry_reason='reasoning'),
                        dict(retry_from='sol61-high', retry_reason='reasoning', attempt=1)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                routes.resolve(self.data, 'codex', 'implement', **options)
        result = subprocess.run([sys.executable, str(HERE / 'harness-route.py'),
                                 '--host', 'codex', '--role', 'implement', '--step', '1'],
                                cwd=ROOT.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments: --step', result.stderr)

    def test_tier_prefers_nonexhausted_vendor(self):
        for budget, expected in [('normal', 'astra-high'), ('exhausted:openai', 'fable-high')]:
            result = routes.resolve(self.data, 'claude', 'implement', tier='S',
                                    author_vendors=['google'], budget=budget)
            self.assertEqual(result['worker_id'], expected)
            self.assertTrue(result['available'])

    def test_worker_cannot_combine_with_tier(self):
        with self.assertRaisesRegex(ValueError, '--worker cannot be combined with --tier'):
            routes.resolve(self.data, 'claude', 'implement', worker='sol61-high', tier='B')

    def test_optional_worker_needs_requires(self):
        with self.assertRaisesRegex(ValueError, 'optional worker needs requires'):
            self.override('sol61-high', status='optional', requires={})

    def test_agy_command_grant_requires_authorization(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / 'settings.json'
            env = {'HARNESS_AGY_SETTINGS': str(settings)}
            data = self.with_test_reader()
            reader = next(w for w in data['workers'] if w['id'] == 'test-reader')
            requires = {'agy_grant': reader['roles']['explore']['requires']['agy_grant']}
            settings.write_text(json.dumps({'permissions': {'allow': [requires['agy_grant'] + '(*)', 'command(*)']}}))
            self.assertEqual(routes.unmet_requires(requires, self.data, temp, env),
                             'requires agy_grant: command grant present')
            env['HARNESS_ALLOW_AGY_COMMAND'] = '1'
            self.assertIsNone(routes.unmet_requires(requires, self.data, temp, env))

    def test_requires_and_reader_tiers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / 'agy.json'
            agent = root / '.agents/agents/project-agent.md'
            env = {'HARNESS_AGY_SETTINGS': str(settings), 'PATH': ''}
            data = self.with_test_reader()
            def explore(**kw):
                return routes.resolve(data, 'codex', 'explore', root=root, env=env, **kw)
            missing = explore(tier='C')
            self.assertEqual(missing['worker_id'], 'haiku45')
            self.assertFalse(missing['floor_met'])
            self.assertTrue(any('agy_grant' in x['reason'] for x in missing['skipped']))
            settings.write_text(json.dumps({'permissions': {'allow': ['write_file(*)']}}))
            self.assertTrue(any('agent_file' in x['reason'] for x in explore(tier='C')['skipped']))
            agent.parent.mkdir(parents=True)
            agent.write_text('reader')
            self.assertEqual(explore(tier='C')['worker_id'], 'test-reader')
            data = routes.merge_bindings(data, {'vendors': {'local': {'endpoint': {'base_url': 'http://localhost', 'model': 'auto'}}}})
            self.assertEqual(explore(worker='local-reader', latency='detached')['worker_id'], 'local-reader')
            settings.write_text(json.dumps({'permissions': {'allow': ['write_file_other(*)']}}))
            data = routes.merge_bindings(data, {'workers_local': [{'id': 'haiku45', 'requires': {'binary': 'test-reader'}}]})
            with patch.object(routes.shutil, 'which', return_value=None):
                with self.assertRaises(IndexError): explore()
            with patch.object(routes.shutil, 'which', return_value='/bin/test-reader'):
                self.assertEqual(explore()['worker_id'], 'haiku45')

    def test_workers_local_recursive_merge_and_array_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / '.claude'
            folder.mkdir()
            (folder / 'model-bindings.json').write_text(json.dumps(self.with_test_reader()), encoding='utf-8')
            override = {'workers_local': [{'id': 'test-reader', 'roles': {'explore': {'priority': 7}}}]}
            (folder / 'model-bindings.local.json').write_text(json.dumps(override))
            merged = routes.load_bindings(temp)
            reader = next(w for w in merged['workers'] if w['id'] == 'test-reader')
            self.assertEqual(reader['roles']['explore']['priority'], 7)
            self.assertEqual(reader['roles']['explore']['requires']['agy_grant'], 'write_file')
            base = self.cell('E', 'claude')
            added = dict(base, id='other-reader', tier_cell=False, roles={'explore': 3})
            override = {'workers': [base], 'workers_local': [added]}
            (folder / 'model-bindings.local.json').write_text(json.dumps(override))
            self.assertEqual([w['id'] for w in routes.load_bindings(temp)['workers']], ['haiku45', 'other-reader'])

    def test_explicit_worker_and_host_priorities(self):
        with self.assertRaisesRegex(ValueError, 'unknown worker'):
            routes.resolve(self.data, 'claude', 'implement', worker='absent')
        pending = self.sol61_unverified()
        implicit = routes.resolve(pending, 'claude', 'implement')
        self.assertIn({'id': 'sol61-high', 'reason': 'status unverified requires explicit --worker'}, implicit['skipped'])
        self.assertEqual(routes.resolve(pending, 'claude', 'implement', worker='sol61-high')['worker_id'], 'sol61-high')
        data = routes.merge_bindings(self.data, {'workers_local': [dict(
            id='sol61-high', roles={'implement': {'priority': 150, 'hosts': ['claude']}})]})
        result = routes.resolve(data, 'codex', 'implement', author_vendors=['claude'])
        self.assertEqual(result['worker_id'], 'sol61-medium')
        self.assertIn({'id': 'sol61-high', 'reason': 'host is not eligible'}, result['skipped'])
        data = self.override('sol61-high', status='conditional')
        self.assertEqual(routes.resolve(data, 'claude', 'implement')['worker_id'], 'sol61-medium')

    def test_multiple_exhausted_vendors_and_diagnostic(self):
        budget = 'exhausted:openai,claude'
        self.assertEqual(routes.budget_for(self.data, {'HARNESS_BUDGET': budget}), budget)
        result = routes.resolve(self.data, 'claude', 'write', budget=budget, latency='detached')
        self.assertEqual(result['worker_id'], 'flash-medium')
        self.assertTrue(result['available'])
        result = routes.resolve(self.data, 'claude', 'write', budget=budget + ',google', latency='detached')
        self.assertFalse(result['available'])
        for host in ('claude', 'codex'):
            result = routes.resolve(self.data, host, 'review_deep', author_vendors=['openai', 'claude'], budget=budget)
            self.assertFalse(result['separation_satisfied'])
            self.assertFalse(result['available'])
            self.assertIn('no configured route is independent', result['reason'])

    def test_schema_rejects_invalid_workers(self):
        for invalid in (None, [], {'schema_version': 3, 'workers': {}},
                        {'schema_version': 3, 'workers': [None]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                routes.merge_bindings(invalid, {})
        for fields in ({'vendor': 'unknown'}, {'tier': 'Z'}, {'status': 'unknown'},
                       {'requires': {'network': True}}, {'roles': {'unknown': 1}},
                       {'roles': {'implement': 0}}, {'roles': {'implement': {'other': 1}}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.override('sol61-high', **fields)
        for worker in (dict(self.cell('B', 'openai'), id='duplicate-cell'),
                       dict(self.cell('B', 'openai'), id='duplicate-priority', tier_cell=False),
                       dict(self.cell('B', 'openai'))):
            with self.assertRaises(ValueError):
                routes.merge_bindings(self.data, {'workers': self.data['workers'] + [worker]})

    def test_launcher_default_cli_works_for_delegates(self):
        env = dict(os.environ, HARNESS_DELEGATE_RUN='1')
        command = [sys.executable, str(HERE / 'harness-route.py'), '--launcher-default']
        for vendor, role, expected in [('openai', 'implement', 'gpt-6.1-sol medium'),
                                       ('openai', 'image_verify', 'gpt-6.1-sol medium'),
                                       ('claude', 'implement', 'claude-opus-5-5 medium')]:
            # Launchers run from the project root; bindings resolve from that cwd.
            result = subprocess.run(command + ['--vendor', vendor, '--role', role], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            source, _, rest = result.stdout.strip().partition(' ')
            self.assertIn(source, ('public', 'public+local'))
            self.assertEqual(rest, expected)
        with patch.object(routes, '__file__', str(HERE / 'harness-route.py')):
            self.assertEqual(routes.load_bindings()['schema_version'], 3)

    def test_codex_hook_trust_uses_effective_engine_state(self):
        # Native hooks/list response shape; no model, credentials or trust writes.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / '.codex/hooks.json'
            source.parent.mkdir()
            source.write_text(json.dumps({'hooks': {event: [{'hooks': [{'type': 'command', 'command': 'echo ok'}]}]
                                                    for event in ('PreToolUse', 'Stop', 'UserPromptSubmit')}}))
            config = root / 'config.toml'
            config.write_text('# trust state must remain untouched\n')
            stub = root / 'engine.py'
            stub.write_text('''import json, sys
from pathlib import Path
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        response = {'id': request['id'], 'result': {}}
    elif request.get('method') == 'hooks/list':
        response = json.loads(Path(sys.argv[1]).read_text())
    else:
        continue
    print(json.dumps(response), flush=True)
''')
            hooks = [{'key': str(source) + ':' + event + ':0:0', 'sourcePath': str(source),
                      'enabled': True, 'trustStatus': 'trusted'}
                     for event in ('pre_tool_use', 'stop', 'user_prompt_submit')]
            native = subprocess.Popen
            response_file = root / 'response.json'
            def spawn(*args, **kwargs):
                return native([sys.executable, str(stub), str(response_file)], **kwargs)
            cases = ('trusted', 'warning', 'disabled', 'modified', 'untrusted', 'missing', 'feature-off', 'unsupported', 'malformed', 'load-error')
            for case in cases:
                with self.subTest(case=case):
                    listed = json.loads(json.dumps(hooks))
                    if case == 'disabled': listed[-1]['enabled'] = False
                    if case in ('modified', 'untrusted'): listed[-1]['trustStatus'] = case
                    if case == 'missing': listed.pop()
                    if case == 'feature-off': listed.clear()
                    response = {'id': 2, 'result': {'data': [{'hooks': listed, 'warnings': [], 'errors': []}]}}
                    if case == 'warning': response['result']['data'][0]['warnings'] = ['unrelated user hook warning']
                    if case == 'load-error': response['result']['data'][0]['errors'] = ['cannot read hooks source']
                    if case == 'unsupported': response = {'id': 2, 'error': {'code': -32601}}
                    if case == 'malformed': response = {'id': 2, 'result': {}}
                    response_file.write_text(json.dumps(response))
                    with patch.object(session.shutil, 'which', return_value='codex'), patch.object(session.subprocess, 'Popen', side_effect=spawn):
                        if case in ('trusted', 'warning'):
                            self.assertEqual(session.check_codex_hooks(root), 3)
                        else:
                            with self.assertRaises((ValueError, KeyError)):
                                session.check_codex_hooks(root)
            self.assertEqual(config.read_text(), '# trust state must remain untouched\n')

    def test_generated_hooks_execute_in_paths_with_shell_metacharacters(self):
        bash = routes.find_bash()
        if not bash: self.skipTest('Bash required')
        with tempfile.TemporaryDirectory() as temp:
            for name in ('plain', 'has space', 'project&demo', "quote'and$dollar`tick"):
                with self.subTest(name=name):
                    root = Path(temp) / name
                    hook_dir = root / '.claude/hooks'
                    hook_dir.mkdir(parents=True)
                    shutil.copyfile(ROOT / '.claude/hooks/session_preflight.py', hook_dir / 'session_preflight.py')
                    output = root / 'hooks.json'
                    result = subprocess.run([bash, str(HERE / 'gen-codex-hooks.sh'), '--python', sys.executable,
                                             '--out', 'hooks.json'], cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    hook = json.loads(output.read_text())['hooks']['SessionStart'][0]['hooks'][0]
                    commands = [[bash, '-c', hook['command']]]
                    if os.name == 'nt':
                        commands.append([shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command', hook['commandWindows']])
                    for command in commands:
                        result = subprocess.run(command, cwd=root, input=json.dumps({'cwd': str(root)}),
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('HARNESS PLATFORM:', result.stdout)

    def test_claude_only_install_skips_codex_generator_checks(self):
        with tempfile.TemporaryDirectory() as temp, patch(__name__ + '.ROOT', Path(temp)):
            for check in (self.test_sessionstart_is_wired_and_generator_keeps_all_events,
                          self.test_generator_uses_nongit_harness_root_from_subdirectory):
                with self.assertRaises(unittest.SkipTest):
                    check()

    def test_selected_codex_install_requires_its_generator(self):
        if not (ROOT / '.codex/hooks.json').exists():
            self.skipTest('Codex hooks are not installed on this project')
        with tempfile.TemporaryDirectory() as temp, patch(__name__ + '.HERE', Path(temp)):
            with self.assertRaisesRegex(AssertionError, 'No such file|cannot open'):
                self.test_generator_uses_nongit_harness_root_from_subdirectory()

    def test_sensitive_lane_covers_home_read_denials(self):
        lane = ROOT / '.claude/sandbox-sensitive.json'
        if not lane.exists():
            self.skipTest('Optional sensitive lane is not installed')
        settings = json.loads((ROOT / '.claude/settings.json').read_text(encoding='utf-8'))
        protected = json.loads(lane.read_text(encoding='utf-8'))['sandbox']['filesystem']['denyRead']
        for rule in settings['permissions']['deny']:
            if rule.startswith('Read(~/'):
                target = rule[5:-1].removesuffix('/**')
                self.assertTrue(any(target == root or target.startswith(root.rstrip('/') + '/')
                                    for root in protected), rule)

    def test_claude_mission_prompt_hook_is_wired(self):
        settings = json.loads((ROOT / '.claude/settings.json').read_text(encoding='utf-8'))
        wired = False
        for entry in settings.get('hooks', {}).get('UserPromptSubmit', []):
            for handler in entry.get('hooks', []):
                if handler.get('type') != 'command':
                    continue
                # Accept command+args and a combined command, including quoted
                # Windows interpreter paths. This checks wiring, not execution.
                tokens = shlex.split(handler.get('command', ''), posix=False)
                tokens += handler.get('args', [])
                tokens = [token.strip('\"\'').replace('\\', '/') for token in tokens]
                wired |= ('--new-prompt' in tokens
                          and any(token.rsplit('/', 1)[-1] == 'stop_gate.py' for token in tokens))
        self.assertTrue(wired, 'Missing Claude UserPromptSubmit -> stop_gate.py --new-prompt; '
                              'merge that hook into .claude/settings.json.')

    def test_orchestrator_policies_are_installed_at_their_referenced_paths(self):
        for name in ('delegation-matrix.md', 'retry-policy.md'):
            path = ROOT / 'docs' / 'orchestration' / name
            self.assertTrue(path.is_file(), f'Missing required orchestration policy: {path}; '
                                           'copy it with the updated host entry instructions.')

    def test_required_operation_guides_are_installed(self):
        for name in ('harness-install.md', 'harness-launchers.md', 'harness-manual.md'):
            path = ROOT / 'docs' / name
            self.assertTrue(path.is_file(), f'Missing required harness guide: {path}; '
                                           'copy all three guides from the installation copy table.')

    def test_exhausted_review_reports_incomplete_without_implementation_fallback(self):
        for role in ('plan_review', 'review_gate', 'review_deep'):
            route = routes.resolve(self.data, 'codex', role,
                                   budget='exhausted:claude', author_vendors=['openai'])
            self.assertFalse(route['available'])
            self.assertIn('required review as incomplete', route['reason'])
            self.assertIn('do not use implementation fallback', route['reason'])
        route = routes.resolve(self.data, 'codex', 'implement', budget='exhausted:claude')
        self.assertIn('follow fallback policy', route['reason'])

    def test_missing_bash_preserves_independence_reason_and_exit_contract(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        output = StringIO()
        with patch.object(routes, 'find_bash', return_value=None), patch.object(sys, 'argv', [
                'harness-route.py', '--host', 'codex', '--role', 'review_deep',
                '--author-vendor', 'openai', '--author-vendor', 'claude']), redirect_stdout(output):
            self.assertEqual(routes.main(), 2)
        route = json.loads(output.getvalue())
        self.assertFalse(route['available'])
        self.assertIn('no configured route is independent', route['reason'])
        self.assertIn('required review as incomplete', route['reason'])

    def test_missing_bash_can_suggest_independent_native_reviewer_but_not_exhausted_one(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        for budget, native in [('normal', True), ('exhausted:claude', False)]:
            output = StringIO()
            with patch.object(routes, 'find_bash', return_value=None), patch.object(
                    routes, 'budget_for', return_value=budget), patch.object(sys, 'argv', [
                    'harness-route.py', '--host', 'claude', '--role', 'review_deep',
                    '--author-vendor', 'openai']), redirect_stdout(output):
                self.assertEqual(routes.main(), 2)
            route = json.loads(output.getvalue())
            self.assertFalse(route['available'])  # Process-launcher contract unchanged.
            self.assertEqual('host-native reviewer' in route['reason'], native)
            self.assertIn('required review as incomplete', route['reason'])

    def test_review_refuses_cross_lane_s_before_missing_bash_fallback(self):
        for host, previous, author in [('codex', 'fable-xhigh', 'openai'),
                                        ('claude', 'astra-xhigh', 'claude')]:
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(routes, 'load_bindings', return_value=self.data), patch.object(
                    routes, 'budget_for', return_value='normal'), patch.object(
                    routes, 'find_bash', return_value=None) as shell, patch.object(sys, 'argv', [
                    'harness-route.py', '--host', host, '--role', 'review_deep', '--author-vendor', author,
                    '--retry-from', previous, '--retry-reason', 'reasoning', '--attempt', '2'
                    ]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(routes.main(), 4)
            self.assertEqual(output.getvalue(), '')
            self.assertIn('S row already failed at its detached effort; orchestrator decides (retry-policy 4)',
                          errors.getvalue())
            shell.assert_not_called()

    def test_human_hosts_delegate_to_opposite_vendor(self):
        for host, vendor, launcher in [('codex', 'claude', 'claude-run.sh'),
                                      ('claude', 'openai', 'codex-run.sh')]:
            route = routes.resolve(self.data, host, 'implement')
            self.assertEqual(route['vendor'], vendor)
            self.assertTrue(route['launcher'].endswith(launcher))
            self.assertEqual(route['sandbox'], 'workspace-write')

    def test_web_uses_haiku_on_both_hosts(self):
        for host in ('codex', 'claude'):
            for tier in (None, 'D'):
                route = routes.resolve(self.data, host, 'web', tier=tier)
                self.assertEqual(route['vendor'], 'claude')
                self.assertTrue(route['launcher'].endswith('claude-run.sh'))
                self.assertEqual(route['claude_role'], 'web')
                self.assertEqual(route['model'], self.cell('E', 'claude')['model'])
                self.assertEqual(route['sandbox'], 'read-only')
                self.assertEqual(route['worker_id'], 'haiku45')
                self.assertEqual(route['tier_fallback'], tier == 'D')

    def test_advisory_is_other_vendor_and_readonly(self):
        for host, vendor in [('codex', 'claude'), ('claude', 'openai')]:
            for role in ('decide', 'plan_review'):
                route = routes.resolve(self.data, host, role, author_vendors=[{'codex': 'openai', 'claude': 'claude'}[host]])
                self.assertEqual((route['vendor'], route['sandbox']), (vendor, 'read-only'))

    def test_tier_two_has_two_independent_vendors(self):
        for host in ('codex', 'claude'):
            designer = {'codex': 'openai', 'claude': 'claude'}[host]
            implementer = routes.resolve(self.data, host, 'implement')['vendor']
            gate = routes.resolve(self.data, host, 'review_gate', author_vendors=[designer])
            deep = routes.resolve(self.data, host, 'review_deep', author_vendors=[implementer])
            self.assertNotEqual(gate['vendor'], deep['vendor'])
            for route in (gate, deep):
                self.assertEqual(route['sandbox'], 'read-only')

    def test_openai_ultra_override_is_a_policy_error_on_both_hosts(self):
        data = self.override('sol61-high', effort='ultra')
        for host in ('codex', 'claude'):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, 're-delegation'):
                routes.resolve(data, host, 'review_deep', author_vendors=['claude'],
                               worker='sol61-high', latency='detached')

    def test_openai_max_override_preserves_single_agent_route(self):
        result = routes.resolve(self.data, 'claude', 'implement', worker='sol61-max', latency='detached')
        self.assertTrue(result['available'])
        self.assertEqual(result['effort'], 'max')
        result = routes.resolve(self.data, 'claude', 'implement')
        self.assertNotIn('sol61-max', result['eligible'])

    def test_task_tier_selects_efficient_binding_on_both_hosts(self):
        for host, ident, tier in [('codex', 'sonnet55-medium', 'C'), ('claude', 'sol61-medium', 'B')]:
            result = routes.resolve(self.data, host, 'implement', tier='C')
            self.assertEqual(result['worker_id'], ident)
            self.assertEqual(result['band'], 'C')
            self.assertEqual(result['tier'], tier)
            self.assertEqual(result['sandbox'], 'workspace-write')

    def test_tier_override_preserves_review_floor_and_exhaustion(self):
        for role in ('decide', 'plan_review', 'review_deep'):
            result = routes.resolve(self.data, 'codex', role, author_vendors=['openai'])
            self.assertEqual(result['band'], 'B')
            self.assertTrue(result['floor_met'])
        result = routes.resolve(self.data, 'codex', 'implement', budget='exhausted:claude', tier='C')
        self.assertFalse(result['available'])

    def test_local_example_preserves_defaults_and_uses_supported_endpoint(self):
        example = json.loads((ROOT / '.claude/model-bindings.local.json.example').read_text(encoding='utf-8'))
        merged = routes.merge_bindings(self.data, example)
        self.assertEqual(merged['specialties'], self.data['specialties'])
        self.assertEqual(merged['roles'], self.data['roles'])
        self.assertIn('endpoint', merged['vendors']['local'])
        self.assertNotIn('local_endpoint', example)

    def test_local_override_changes_only_selected_vendor(self):
        data = self.override('opus55-medium', model='test-claude')
        self.assertEqual(routes.resolve(data, 'codex', 'implement')['model'], 'test-claude')
        self.assertEqual(routes.resolve(data, 'claude', 'implement'),
                         routes.resolve(self.data, 'claude', 'implement'))

    def test_no_independent_advisory_candidate_is_unavailable(self):
        self.data['workers'] = [w for w in self.data['workers'] if w['vendor'] != 'claude']
        result = routes.resolve(self.data, 'codex', 'decide')
        self.assertFalse(result['available'])
        self.assertFalse(result['separation_satisfied'])

    def test_exhaustion_and_tight_do_not_silently_change_route(self):
        blocked = routes.resolve(self.data, 'codex', 'implement', budget='exhausted:claude')
        self.assertFalse(blocked['available'])
        self.assertEqual(blocked['vendor'], 'claude')
        self.assertFalse(routes.resolve(self.data, 'codex', 'implement', budget='tight', retry_from='opus55-medium', retry_reason='reasoning', attempt=1)['available'])
        self.assertTrue(routes.resolve(self.data, 'codex', 'review_deep', budget='tight', author_vendors=['claude'])['available'])

    def test_review_follows_actual_implementer_after_direct_work_or_fallback(self):
        for host in ('codex', 'claude'):
            for author, reviewer in [('openai', 'claude'), ('claude', 'openai')]:
                for role in ('review_gate', 'review_deep'):
                    result = routes.resolve(self.data, host, role, author_vendors=[author])
                    self.assertTrue(result['available'])
                    self.assertEqual(result['vendor'], reviewer)
                    self.assertEqual(result['host'], host)
                    self.assertEqual(result['sandbox'], 'read-only')

    def test_implementation_separates_from_actual_designer_without_switching_host(self):
        result = routes.resolve(self.data, 'codex', 'implement', author_vendors=['claude'])
        self.assertEqual((result['host'], result['vendor']), ('codex', 'openai'))
        self.assertEqual(result['model'], next(w for w in self.data['workers'] if w['id'] == 'sol61-medium')['model'])

    def test_review_never_infers_authorship_from_starting_host(self):
        for role in ('plan_review', 'review_gate', 'review_deep'):
            with self.assertRaisesRegex(ValueError, 'actual artifact'):
                routes.resolve(self.data, 'codex', role)
        result = routes.resolve(self.data, 'codex', 'review_deep', author_vendors=['openai', 'claude'])
        self.assertFalse(result['available'])
        self.assertFalse(result['separation_satisfied'])
        result = routes.resolve(self.data, 'codex', 'review_deep', author_vendors=['openai'], budget='exhausted:claude')
        self.assertFalse(result['available'])
        self.assertEqual(result['vendor'], 'claude')

    def test_specialty_author_and_local_review_override(self):
        data = self.override('opus55-high', model='project-reviewer')
        result = routes.resolve(data, 'claude', 'review_deep', author_vendors=['openai'])
        self.assertTrue(result['available'])
        self.assertEqual(result['model'], 'project-reviewer')

    def test_cli_requires_actual_review_author(self):
        env = dict(os.environ, HARNESS_BUDGET='normal')
        env.pop('HARNESS_DELEGATE_RUN', None)
        command = [sys.executable, str(HERE / 'harness-route.py'), '--host', 'codex', '--role', 'review_deep']
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 4)
        result = subprocess.run(command + ['--author-vendor', 'openai'], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['vendor'], 'claude')

    def test_budget_declaration_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / '.claude'
            folder.mkdir()
            (folder / '.preflight-status').write_text(json.dumps(
                {'budget_probed': True, 'budget': 'exhausted:openai'}))
            # Startup metadata is not a live quota declaration.
            self.assertEqual(routes.budget_for(self.data, {}), 'normal')
            data = routes.merge(self.data, {'budget': 'normal'})
            self.assertEqual(routes.budget_for(data, {}), 'normal')
            self.assertEqual(routes.budget_for(data, {'HARNESS_BUDGET': 'tight'}), 'tight')
            self.assertEqual(routes.budget_for(data, {'HARNESS_BUDGET': 'bad'}), 'normal')

    def test_session_budget_record_is_read_only_for_the_named_session(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '.claude').mkdir()
            previous = os.getcwd()
            os.chdir(root)
            try:
                # No session id: no record is consulted.
                self.assertIsNone(routes.session_budget({}))
                message = session.update_budget(root, 'sess-1', exhausted='openai')
                self.assertEqual(message, 'HARNESS BUDGET: exhausted:openai')
                session.update_budget(root, 'sess-1', exhausted='google,openai')
                self.assertEqual(routes.session_budget({'HARNESS_SESSION_ID': 'sess-1'}), 'exhausted:google,openai')
                self.assertIsNone(routes.session_budget({'HARNESS_SESSION_ID': 'sess-2'}))
                # Precedence: HARNESS_BUDGET > session record > local budget > normal.
                data = routes.merge(self.data, {'budget': 'tight'})
                self.assertEqual(routes.budget_for(data, {'HARNESS_SESSION_ID': 'sess-1'}), 'exhausted:google,openai')
                self.assertEqual(routes.budget_for(data, {'HARNESS_SESSION_ID': 'sess-1', 'HARNESS_BUDGET': 'normal'}), 'normal')
                self.assertEqual(routes.budget_for(data, {'HARNESS_SESSION_ID': 'sess-2'}), 'tight')
                # The recorded exhaustion ranks that vendor last and marks the route unavailable.
                route = routes.resolve(self.data, 'claude', 'implement', budget='exhausted:google,openai', root=root, env={})
                self.assertEqual((route['vendor'], route['available']), ('openai', False))
                route = routes.resolve(self.data, 'claude', 'write', budget='exhausted:openai', root=root, env={}, latency='detached')
                self.assertEqual((route['vendor'], route['available']), ('google', True))
                self.assertEqual(session.update_budget(root, 'sess-1', clear=True), 'HARNESS BUDGET: cleared')
                self.assertIsNone(routes.session_budget({'HARNESS_SESSION_ID': 'sess-1'}))
                with self.assertRaises(ValueError):
                    session.update_budget(root, 'sess-1', exhausted='vendorx')
            finally:
                os.chdir(previous)

    def test_budget_command_refuses_delegates_and_router_reads_the_record(self):
        with tempfile.TemporaryDirectory() as root:
            shutil.copytree(ROOT / '.claude', Path(root) / '.claude',
                            ignore=shutil.ignore_patterns('*-logs', '__pycache__', '.mission-open', 'worktrees'))
            command = [sys.executable, str(HERE / 'harness-session.py'), 'budget', '--session', 'sess-9']
            env = dict(os.environ)
            env.pop('HARNESS_DELEGATE_RUN', None)
            denied = subprocess.run(command + ['--exhausted', 'openai'], cwd=root,
                                    env=dict(env, HARNESS_DELEGATE_RUN='1'), capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('orchestrator session id', denied.stderr)
            recorded = subprocess.run(command + ['--exhausted', 'openai'], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertIn('HARNESS BUDGET: exhausted:openai', recorded.stdout)
            route = subprocess.run([sys.executable, str(HERE / 'harness-route.py'), '--host', 'claude', '--role', 'implement'],
                                   cwd=root, env=dict(env, HARNESS_SESSION_ID='sess-9'), capture_output=True, text=True)
            self.assertEqual(route.returncode, 2, route.stderr)
            data = json.loads(route.stdout)
            self.assertEqual((data['vendor'], data['available'], data['budget']), ('openai', False, 'exhausted:openai'))
            unrelated = subprocess.run([sys.executable, str(HERE / 'harness-route.py'), '--host', 'claude', '--role', 'implement'],
                                       cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
            self.assertTrue(json.loads(unrelated.stdout)['available'])

    def test_delegate_cannot_resolve_another_worker(self):
        env = dict(os.environ, HARNESS_DELEGATE_RUN='1')
        result = subprocess.run([sys.executable, str(HERE / 'harness-route.py'),
                                 '--host', 'codex', '--role', 'implement'],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 4)
        self.assertIn('delegates execute', result.stderr)

    def test_cli_locates_real_shell_without_global_path_change(self):
        env = dict(os.environ, HARNESS_BUDGET='normal')
        env.pop('HARNESS_DELEGATE_RUN', None)
        result = subprocess.run([sys.executable, str(HERE / 'harness-route.py'),
                                 '--host', 'codex', '--role', 'implement'],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        shell = json.loads(result.stdout)['shell']
        self.assertTrue(Path(shell).is_file())
        self.assertNotIn('windowsapps', shell.lower())

    def test_single_entry_resolves_delegate_before_onboarding(self):
        # AGENTS.md is the one instruction file for both hosts; CLAUDE.md only imports it.
        installed = ROOT / 'AGENTS.md'
        source = installed if installed.exists() else ROOT / 'AGENTS.md.template'
        if not source.exists():
            self.skipTest('the entry instructions are not installed')
        text = source.read_text(encoding='utf-8')
        self.assertIn('HARNESS_DELEGATE_RUN=1', text)
        workflow = text.index('## Orchestrator workflow')
        entry = text[:workflow]
        self.assertIn('Skip the Orchestrator workflow and all its linked reading/setup', entry)
        self.assertIn('applicable project/security/verification rules', entry)
        self.assertLess(text.index('HARNESS_DELEGATE_RUN=1'), text.index('session-role.md'))
        self.assertLess(workflow, text.index('Read `.claude/rules/session-role.md`'))
        self.assertLess(workflow, text.index('python .claude/scripts/harness-route.py'))
        self.assertLess(text.index('Read `.claude/rules/session-role.md`'), text.index('delegation-matrix.md'))
        self.assertIn('harness-session.py finish', text)
        self.assertNotIn('- NEVER run `git commit`', text)
        # The host comes from the SessionStart line, never from a hardcoded name.
        for hardcoded in ('host `claude`', 'host `codex`', '--host claude', '--host codex'):
            self.assertNotIn(hardcoded, text)
        self.assertIn('--host <host>', text)
        stub = ROOT / 'CLAUDE.md'
        stub = stub if stub.exists() else ROOT / 'CLAUDE.md.template'
        self.assertTrue(stub.exists(), 'CLAUDE.md stub is required next to AGENTS.md (a parent CLAUDE.md disables the fallback)')
        self.assertRegex(stub.read_text(encoding='utf-8'), r'(?m)^@AGENTS\.md[ \t]*$')

    def test_explicit_finish_keeps_failure_and_clears_pass(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / '.claude'
            folder.mkdir()
            marker = folder / '.stop-gate'
            verify = Path(root) / 'verify.sh'
            marker.write_text('verify.sh\n')
            verify.write_text('exit 3\n')
            command = [sys.executable, str(HERE / 'harness-session.py'), 'finish']
            first = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(first.returncode, 1)
            self.assertTrue(marker.exists())
            verify.write_text('exit 0\n')
            second = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0)
            self.assertFalse(marker.exists())

    def test_sessionstart_is_wired_and_generator_keeps_all_events(self):
        if not (ROOT / '.codex/hooks.json').exists():
            self.skipTest('Codex hooks are not installed on this project')
        hooks = json.loads((ROOT / '.codex/hooks.json').read_text())['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'PreToolUse', 'UserPromptSubmit', 'Stop'})
        self.assertIn('session_preflight.py', hooks['SessionStart'][0]['hooks'][0]['command'])
        self.assertIn('stop_gate.py', hooks['UserPromptSubmit'][0]['hooks'][0]['command'])
        self.assertIn('--new-prompt', hooks['UserPromptSubmit'][0]['hooks'][0]['command'])
        # The generator runs in a scratch repo, never changes trusted definitions.
        sys.path.insert(0, str(ROOT / '.claude/hooks'))
        from stop_gate import find_bash
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(['git', 'init', '-q', root], check=True)
            output = Path(root) / 'generated.json'
            result = subprocess.run([find_bash(), str(HERE / 'gen-codex-hooks.sh'),
                                     '--out', str(output), '--python', sys.executable],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = json.loads(output.read_text())['hooks']
            self.assertEqual(set(generated), set(hooks))
            self.assertIn('--new-prompt', generated['UserPromptSubmit'][0]['hooks'][0]['command'])
            for event in hooks:
                self.assertEqual(generated[event][0]['hooks'][0]['timeout'],
                                 hooks[event][0]['hooks'][0]['timeout'])

    @unittest.skipIf(os.name == 'nt', 'POSIX PATH fixtures use executable symlinks')
    def test_generator_detects_working_python3_before_writing(self):
        import shutil
        bash = routes.find_bash()
        if not bash:
            self.skipTest('Bash is required')
        with tempfile.TemporaryDirectory(prefix='python discovery-') as temp:
            root = Path(temp)
            (root / '.claude').mkdir()
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            for tool in ('dirname', 'sed', 'git'):
                (bin_dir / tool).symlink_to(shutil.which(tool))
            env = dict(os.environ, PATH=str(bin_dir))
            output = root / 'hooks.json'
            command = [bash, str(HERE / 'gen-codex-hooks.sh'), '--out', str(output), '--force']
            for available in ('python3', 'python'):
                with self.subTest(available=available):
                    candidate = bin_dir / available
                    candidate.symlink_to(sys.executable)
                    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    hooks = json.loads(output.read_text())['hooks']
                    executable = subprocess.check_output(
                        [str(candidate), '-c', 'import sys; print(sys.executable)'], text=True).strip()
                    self.assertIn(executable, hooks['Stop'][0]['hooks'][0]['command'])
                    candidate.unlink()
                    # A non-working python3 (e.g. a Store alias) must not hide python.
                    if available == 'python3':
                        candidate.write_text('#!/bin/sh\nexit 1\n')
                        candidate.chmod(0o755)
            before = output.read_bytes()
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('no working Python 3', result.stderr)
            self.assertEqual(output.read_bytes(), before)
            result = subprocess.run(command + ['--python', str(bin_dir / 'python3')],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), before)

    def test_generator_uses_nongit_harness_root_from_subdirectory(self):
        if not (ROOT / '.codex/hooks.json').exists():
            self.skipTest('Codex hooks are not installed on this project')
        sys.path.insert(0, str(ROOT / '.claude/hooks'))
        from stop_gate import find_bash
        with tempfile.TemporaryDirectory(prefix='harness space-') as root:
            project = Path(root)
            (project / '.claude').mkdir()
            (project / 'nested').mkdir()
            output = project / 'generated.json'
            command = [find_bash(), str(HERE / 'gen-codex-hooks.sh'), '--out', str(output), '--python', sys.executable]
            result = subprocess.run(command, cwd=project / 'nested', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((project / '.git').exists())
            hooks = json.loads(output.read_text())['hooks']
            self.assertEqual(set(hooks), {'SessionStart', 'PreToolUse', 'UserPromptSubmit', 'Stop'})
            prompt_hook = hooks['UserPromptSubmit'][0]['hooks'][0]
            self.assertIn('--new-prompt', prompt_hook['commandWindows'])
            for entries in hooks.values():
                hook = entries[0]['hooks'][0]
                self.assertIn(project.as_posix() + '/.claude/hooks/', hook['command'])
                self.assertIn('commandWindows', hook)
            before = output.read_bytes()
            self.assertEqual(subprocess.run(command, cwd=project, capture_output=True).returncode, 2)
            self.assertEqual(output.read_bytes(), before)


class ReviewFollowUps(unittest.TestCase):
    """Review r1 notes: a failed S row is never repeated; null metrics never crash."""

    def setUp(self):
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        self.data = routes.merge_bindings(public, {})

    def test_failed_s_row_steps_to_detached_effort_then_cross_lane(self):
        first = routes.resolve(self.data, 'codex', 'implement', retry_from='fable-high',
                               retry_reason='reasoning', attempt=1)
        self.assertEqual(first['worker_id'], 'fable-xhigh')
        self.assertTrue(first['needs_detached'])
        second = routes.resolve(self.data, 'codex', 'implement', retry_from='fable-xhigh',
                                retry_reason='reasoning', attempt=2)
        self.assertEqual(second['worker_id'], 'astra-high')
        self.assertTrue(second['cross_lane_s'])

    def test_null_metrics_override_is_rejected_not_crashed(self):
        with self.assertRaises(ValueError):
            routes.merge_bindings(self.data, {'workers_local': [{'id': 'opus55-medium', 'metrics': None}]})

    def test_explore_floor_is_met_by_the_reader(self):
        route = routes.resolve(self.data, 'codex', 'explore')
        self.assertTrue(route['floor_met'])

    def test_launcher_default_names_a_detached_only_row_instead_of_falling_back(self):
        # A bindings file whose only implement row is max/ultra must still name that row
        # so the launcher's effort guards refuse it (no silent builtin fallback).
        rows = [w for w in self.data['workers'] if not (w['vendor'] == 'openai' and 'implement' in w['roles'])]
        rows.append({'id': 'only-max', 'vendor': 'openai', 'model': 'gpt-6.1-sol', 'effort': 'max', 'tier': 'B',
                     'metrics': {'index': 48, 'cost': 1.05, 'ttft_s': 186, 'tps': 76, 'provisional': True},
                     'probe': {'date': '2026-09-30', 'result': 'OK', 'cli': 'stub'},
                     'status': 'active', 'roles': {'implement': 1}, 'launcher': '.claude/scripts/codex-run.sh'})
        data = routes.validate_bindings(dict(self.data, workers=rows))
        self.assertEqual(routes.launcher_default(data, 'openai', 'implement')['id'], 'only-max')
        rows[-1] = dict(rows[-1], effort='ultra')
        data = routes.validate_bindings(dict(self.data, workers=rows))
        self.assertEqual(routes.launcher_default(data, 'openai', 'implement')['id'], 'only-max')
        with self.assertRaisesRegex(ValueError, 'ultra'):
            routes.resolve(data, 'claude', 'implement', worker='only-max', latency='detached')

    def test_promotion_needing_detached_says_so_in_reason(self):
        route = routes.resolve(self.data, 'claude', 'implement', retry_from='sol61-medium',
                               retry_reason='reasoning', attempt=1)
        self.assertEqual(route['worker_id'], 'sol61-max')
        self.assertTrue(route['needs_detached'])
        self.assertIn('detached', route['reason'])
        self.assertNotIn('foreground<=', route['reason'])

    def test_low_trust_rows_rank_last_in_band_and_fallback(self):
        # A low-trust row is chosen only when nothing else qualifies, even if it is cheaper.
        base = next(w for w in self.data['workers'] if w['id'] == 'opus55-high')
        cheap = dict(base, id='low-trust-writer', tier_cell=False, trust='low', status='active',
                     metrics=dict(index=53, cost=0.01, ttft_s=1, tps=100, provisional=False),
                     roles={'write': 200})
        data = routes.merge_bindings(self.data, {'workers_local': [cheap]})
        result = routes.resolve(data, 'codex', 'write', tier='A', author_vendors=['openai'])
        self.assertNotEqual(result['worker_id'], 'low-trust-writer')
        only = routes.merge_bindings(data, {'workers_local': [
            dict(id=w['id'], status='conditional') for w in data['workers']
            if 'write' in w['roles'] and w['id'] != 'low-trust-writer' and w['vendor'] != 'openai']})
        result = routes.resolve(only, 'codex', 'write', tier='A', author_vendors=['openai'])
        self.assertEqual(result['worker_id'], 'low-trust-writer')
        with self.assertRaisesRegex(ValueError, 'trust must be'):
            routes.merge_bindings(data, {'workers_local': [dict(id='low-trust-writer', trust='high')]})


class AssessmentRoutes(unittest.TestCase):
    def setUp(self):
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        self.data = routes.merge_bindings(public, {})

    def assess(self, **ratings):
        return dict(dict(open=0, tangle=0, precedent=0, verifier=0, consequence=0), **ratings)

    def test_catalog_contains_exactly_the_18_supported_rows(self):
        expected = {
            'S': {'fable-high', 'fable-xhigh', 'astra-high', 'astra-xhigh'},
            'A': {'opus55-xhigh', 'opus55-high', 'sol61-max'},
            'B': {'opus55-medium', 'sol61-xhigh', 'sol61-high', 'sol61-medium'},
            'C': {'sonnet55-medium', 'flash-medium'},
            'D': {'luna6-high', 'flash-low', 'local-reader'},
            'E': {'haiku45', 'luna6-low'},
        }
        self.assertEqual(len(self.data['workers']), 18)
        for band, ids in expected.items():
            self.assertEqual({w['id'] for w in self.data['workers'] if w['tier'] == band}, ids)
        # Only Gemini Flash is a last resort (cross-check 2026-10-06, user decision).
        self.assertEqual({w['id'] for w in self.data['workers'] if w.get('trust') == 'low'}, {'flash-medium', 'flash-low'})
        self.assertNotIn('trust_note', self.data['selection_policy'])
        self.assertNotIn('promotion_delta', self.data['selection_policy'])
        self.assertEqual(routes.launcher_default(self.data, 'openai', 'image_verify')['id'], 'sol61-medium')
        self.assertEqual(routes.launcher_default(self.data, 'claude', 'image_verify')['id'], 'opus55-high')

    def test_rescaled_indices_load_and_leave_choices_and_promotions_unchanged(self):
        data = json.loads(json.dumps(self.data))
        for row in data['workers']:
            if row.get('scored') is not False:
                row['metrics']['index'] /= 100
        data = routes.merge_bindings(data, {})
        for host, previous, expected in [('claude', 'sol61-medium', 'sol61-max'),
                                          ('codex', 'opus55-medium', 'opus55-high')]:
            self.assertEqual(routes.resolve(data, host, 'implement')['worker_id'], previous)
            promoted = routes.resolve(data, host, 'implement', retry_from=previous,
                                      retry_reason='reasoning', attempt=1)
            self.assertEqual(promoted['worker_id'], expected)
            self.assertTrue(promoted['floor_met'])

    def test_judgement_roles_use_high_rows_on_both_hosts(self):
        for host, author, expected in [('claude', 'claude', 'sol61-high'),
                                        ('codex', 'openai', 'opus55-high')]:
            for role in ('decide', 'plan_review', 'review_deep'):
                with self.subTest(host=host, role=role):
                    route = routes.resolve(self.data, host, role, author_vendors=[author])
                    self.assertEqual((route['worker_id'], route['effort'], route['band']), (expected, 'high', 'B'))
                    self.assertTrue(route['floor_met'])

    def test_all_assessment_mapping_cells_and_both_sources_of_each_maximum(self):
        cases = [(0, 0, 'C'), (0, 1, 'B'), (1, 0, 'B'), (1, 1, 'B'),
                 (2, 0, 'A'), (0, 2, 'A'), (2, 1, 'A'), (1, 2, 'A'), (2, 2, 'S')]
        for difficulty, exposure, band in cases:
            for d_key in ('tangle', 'precedent'):
                for e_key in ('verifier', 'consequence'):
                    with self.subTest(d=difficulty, e=exposure, d_key=d_key, e_key=e_key):
                        route = routes.resolve(self.data, 'codex', 'implement',
                                               assess=self.assess(**{d_key: difficulty, e_key: exposure}))
                        self.assertEqual(route['band'], band)
                        self.assertEqual(route['assessment_floor'], dict(difficulty=difficulty, exposure=exposure,
                                         computed=band, applied=band, recent_failure=False))
                        self.assertTrue(route['reason'].startswith(f'assessed difficulty {difficulty}'))
                        if band == 'S':
                            self.assertEqual(route['worker_id'], 'fable-high')
                        else:
                            self.assertNotIn(route['worker_id'], ('fable-high', 'fable-xhigh'))
        route = routes.resolve(self.data, 'codex', 'implement', assess=self.assess(tangle=2, verifier=1))
        self.assertTrue(route['reason'].startswith('assessed difficulty 2 (tangle), exposure 1 -> band A;'))

    def test_assessment_applies_each_role_minimum(self):
        for role, band in [('implement', 'C'), ('write', 'C'), ('decide', 'B'), ('plan_review', 'B'),
                           ('review_gate', 'C'), ('review_deep', 'B'), ('explore', 'C'), ('web', 'C'),
                           ('image_verify', 'C')]:
            with self.subTest(role=role):
                route = routes.resolve(self.data, 'codex', role, assess=self.assess(),
                                       author_vendors=['openai'] if role in routes.REVIEW_ROLES else None)
                self.assertEqual(route['band'], band)
                self.assertEqual(route['assessment_floor']['computed'], 'C')
                self.assertEqual(route['assessment_floor']['applied'], band)
        data = routes.merge_bindings(self.data, {'roles': {'decide': {'band_floor': 'A'}}})
        self.assertEqual(routes.resolve(data, 'codex', 'decide', assess=self.assess())['band'], 'A')

    def test_recent_failure_raises_exactly_one_band_and_saturates_at_s(self):
        for ratings, computed, raised in [({}, 'C', 'B'), ({'tangle': 1}, 'B', 'A'),
                                          ({'precedent': 2}, 'A', 'S'),
                                          ({'tangle': 2, 'consequence': 2}, 'S', 'S')]:
            with self.subTest(computed=computed):
                route = routes.resolve(self.data, 'codex', 'implement', assess=self.assess(**ratings),
                                       recent_failure=True)
                self.assertEqual(route['band'], raised)
                self.assertEqual(route['assessment_floor']['computed'], raised)
                self.assertTrue(route['assessment_floor']['recent_failure'])

    def test_tier_worker_and_retry_override_the_assessed_start_floor(self):
        hardest = self.assess(open=2, tangle=2, consequence=2)
        for tier, expected in [('C', 'sonnet55-medium'), ('B', 'opus55-medium'), ('S', 'fable-high')]:
            plain = routes.resolve(self.data, 'codex', 'implement', tier=tier)
            route = routes.resolve(self.data, 'codex', 'implement', tier=tier, assess=hardest, recent_failure=True)
            self.assertEqual((route['band'], route['worker_id']), (tier, expected))
            self.assertEqual(route, plain)
        for options in (dict(worker='opus55-medium'), dict(retry_from='opus55-medium',
                        retry_reason='reasoning', attempt=1), dict(retry_from='opus55-medium',
                        retry_reason='infra', attempt=1)):
            plain = routes.resolve(self.data, 'codex', 'implement', **options)
            assessed = routes.resolve(self.data, 'codex', 'implement', assess=hardest, recent_failure=True, **options)
            self.assertEqual(assessed, plain)

    def test_tier_override_disables_assessment_and_plan_advice_with_normal_budget(self):
        for tier, assessment, expected in [('B', self.assess(tangle=2, consequence=2), 'opus55-medium'),
                                            ('A', self.assess(), 'opus55-high')]:
            with self.subTest(tier=tier):
                plain = routes.resolve(self.data, 'codex', 'implement', tier=tier)
                route = routes.resolve(self.data, 'codex', 'implement', tier=tier, assess=assessment)
                self.assertEqual(route['worker_id'], expected)
                self.assertEqual(route, plain)

    def test_overrides_do_not_consult_the_assessment_policy(self):
        data = routes.merge_bindings(self.data, {'selection_policy': {'assessment': {'mapping': []}}})
        for options in (dict(tier='B'), dict(worker='opus55-medium'),
                        dict(retry_from='opus55-medium', retry_reason='reasoning', attempt=1)):
            plain = routes.resolve(data, 'codex', 'implement', **options)
            self.assertEqual(routes.resolve(data, 'codex', 'implement', assess=self.assess(), **options), plain)

    def test_without_assessment_the_complete_fixed_route_is_identical(self):
        fixture = json.loads((HERE / 'test_fixtures/route-baseline-v1.json').read_text(encoding='utf-8'))
        for options in ({}, {'recent_failure': True}):
            result = routes.resolve(fixture['bindings'], 'codex', 'implement', root=ROOT, env={}, **options)
            self.assertEqual(result, fixture['route'])

    def test_local_assessment_mapping_and_minimum_override_are_used(self):
        data = routes.merge_bindings(self.data, {'selection_policy': {'assessment': {
            'mapping': [['B', 'B', 'A'], ['B', 'B', 'A'], ['A', 'A', 'S']],
            'minimum_by_role': {'implement': 'A'}}}})
        route = routes.resolve(data, 'codex', 'implement', assess=self.assess())
        self.assertEqual(route['assessment_floor'], dict(difficulty=0, exposure=0, computed='B',
                         applied='A', recent_failure=False))
        self.assertEqual(route['worker_id'], 'opus55-high')

    def test_no_volume_keeps_cheapest_settings_for_nonzero_assessments(self):
        for host, expected in [('claude', 'sol61-medium'), ('codex', 'opus55-medium')]:
            for budget in ('normal', 'tight'):
                with self.subTest(host=host, budget=budget):
                    route = routes.resolve(self.data, host, 'implement', budget=budget,
                                           assess=self.assess(tangle=1))
                    self.assertEqual(route['worker_id'], expected)
                    self.assertNotIn('scores', route)
                    self.assertNotIn('value rule', route['reason'])

    def test_assessed_a_prefers_a_detached_row_and_keeps_foreground_rows_on_both_hosts(self):
        assessment = self.assess(open=1, tangle=2, precedent=1, verifier=1, consequence=1)
        for host, expected, detached in [('claude', 'sol61-max', True), ('codex', 'opus55-high', False)]:
            with self.subTest(host=host):
                route = routes.resolve(self.data, host, 'implement', assess=assessment)
                self.assertEqual((route['worker_id'], route['band'], route['tier']), (expected, 'A', 'A'))
                self.assertEqual(route['needs_detached'], detached)
                self.assertEqual(route['latency_class'], 'foreground')
                self.assertTrue(route['floor_met'])
                self.assertFalse(route['tier_fallback'])
                self.assertTrue(route['available'])
                self.assertTrue(route['plan_first'])
                self.assertNotIn(expected, [w['id'] for w in route['skipped']])
                if detached:
                    self.assertIn('assessed band A is only available detached', route['reason'])
                    self.assertIn('run with -b --wait', route['reason'])
                else:
                    self.assertIn('foreground<=60s', route['reason'])

    def test_assessed_detached_preference_does_not_change_role_defaults_or_tier_overrides(self):
        data = routes.merge_bindings(self.data, {'roles': {'implement': {'band_floor': 'A'}}})
        for options in ({}, {'tier': 'A'}, {'tier': 'A', 'assess': self.assess(tangle=2, verifier=1)}):
            with self.subTest(options=options):
                route = routes.resolve(data, 'claude', 'implement', **options)
                self.assertEqual(route['worker_id'], 'sol61-medium')
                self.assertFalse(route['needs_detached'])
                self.assertFalse(route['floor_met'])
                self.assertNotIn('assessment_floor', route)

    def test_assessed_floor_chooses_the_cheapest_detached_row_and_keeps_s_lane_efforts(self):
        data = routes.merge_bindings(self.data, {'workers_local': [
            {'id': 'opus55-high', 'metrics': {'ttft_s': 90}},
            {'id': 'opus55-xhigh', 'metrics': {'cost': .5}}]})
        route = routes.resolve(data, 'codex', 'implement', assess=self.assess(tangle=2, verifier=1))
        self.assertEqual(route['worker_id'], 'opus55-xhigh')
        self.assertTrue(route['needs_detached'])
        self.assertTrue(route['floor_met'])
        for host, foreground, detached in [('claude', 'astra-high', 'astra-xhigh'),
                                           ('codex', 'fable-high', 'fable-xhigh')]:
            route = routes.resolve(self.data, host, 'implement', latency='interactive',
                                   assess=self.assess(tangle=2, verifier=2))
            self.assertEqual(route['worker_id'], foreground)
            self.assertTrue(route['floor_met'])
            self.assertTrue(route['needs_detached'])
            data = routes.merge_bindings(self.data, {'workers_local': [{'id': foreground, 'status': 'conditional'}]})
            route = routes.resolve(data, host, 'implement', assess=self.assess(tangle=2, verifier=2))
            self.assertEqual(route['worker_id'], detached)
            self.assertTrue(route['floor_met'])
            self.assertTrue(route['needs_detached'])

    def test_assessed_floor_falls_back_only_when_no_eligible_row_meets_it_anywhere(self):
        for host, vendor, expected in [('claude', 'openai', 'sol61-medium'), ('codex', 'claude', 'opus55-medium')]:
            data = routes.merge_bindings(self.data, {'workers_local': [
                {'id': w['id'], 'status': 'conditional'} for w in self.data['workers']
                if w['vendor'] == vendor and w['tier'] == 'A']})
            route = routes.resolve(data, host, 'implement', assess=self.assess(tangle=2, verifier=1))
            self.assertEqual(route['worker_id'], expected)
            self.assertEqual(route['band'], 'A')
            self.assertFalse(route['floor_met'])
            self.assertTrue(route['tier_fallback'])
            self.assertFalse(route['needs_detached'])

    def test_decide_first_and_plan_first_are_advisory(self):
        open_task = routes.resolve(self.data, 'codex', 'implement', assess=self.assess(open=2))
        self.assertEqual((open_task['shape'], open_task['band']), ('decide-first', 'C'))
        self.assertNotIn('plan_first', open_task)
        for ratings in ({'tangle': 2}, {'tangle': 2, 'verifier': 2}):
            implement = routes.resolve(self.data, 'codex', 'implement', assess=self.assess(**ratings))
            self.assertTrue(implement['plan_first'])
            self.assertNotIn('shape', implement)
            write = routes.resolve(self.data, 'codex', 'write', assess=self.assess(**ratings))
            self.assertNotIn('plan_first', write)
        for assess in (None, self.assess(open=1, tangle=1)):
            route = routes.resolve(self.data, 'codex', 'implement', assess=assess)
            self.assertNotIn('shape', route)
            self.assertNotIn('plan_first', route)

    def test_fallback_uses_band_then_cost_even_when_indices_disagree(self):
        data = routes.merge_bindings(self.data, {'workers_local': [
            {'id': 'sol61-medium', 'metrics': {'index': 1}},
            {'id': 'sol61-high', 'metrics': {'index': 9999}},
            {'id': 'luna6-high', 'metrics': {'index': 99999}},
            {'id': 'sol61-max', 'status': 'conditional'}]})
        route = routes.resolve(data, 'claude', 'implement', tier='A')
        self.assertEqual(route['worker_id'], 'sol61-medium')
        self.assertFalse(route['floor_met'])
        data = routes.merge_bindings(data, {'workers_local': [{'id': 'sol61-medium', 'trust': 'low'}]})
        self.assertEqual(routes.resolve(data, 'claude', 'implement', tier='A')['worker_id'], 'sol61-high')


class ScoredRoutes(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((HERE / 'test_fixtures/route-score-v1.json').read_text(encoding='utf-8'))
        empty_history = patch.object(records, 'state_directory', return_value=HERE / 'unused-score-history')
        empty_history.start()
        self.addCleanup(empty_history.stop)

    def assess(self, **values):
        return dict(dict(open=0, tangle=1, precedent=0, verifier=0, consequence=0, volume=0), **values)

    def route(self, data=None, role='implement', **options):
        return routes.resolve(self.data if data is None else data, 'claude', role,
                              env={}, assess=options.pop('assess', self.assess()), **options)

    def scores(self, route):
        return {score['option']: score for score in route['scores']}

    def test_timeout_minimum_rounding_and_launch_environment(self):
        for volume in (None, 0, 1, 2):
            for minutes, expected in ((0, 570), (3, 570), (3.001, 571), (4.125, 672)):
                with self.subTest(volume=volume, minutes=minutes), patch.object(
                        routes, 'worker_estimate', return_value=(1, minutes, 'seed')):
                    data = json.loads(json.dumps(self.data))
                    for seed in data['selection_policy']['estimates']['delegate_overhead'].values():
                        seed['minutes'] = 0
                    assessment = self.assess()
                    if volume is None:
                        assessment.pop('volume')
                    else:
                        assessment['volume'] = volume
                    route = self.route(data, assess=assessment)
                    self.assertEqual(route['suggested_timeout_s'], expected)
                    self.assertTrue(route['reason'].endswith(f'; suggested -t {expected}'))
                    canonical = ','.join(f'{key}={assessment[key]}' for key in records.ASSESSMENT_KEYS
                                         if key in assessment)
                    self.assertEqual(route['launch_env'], dict(HARNESS_BAND=route['band'],
                                     HARNESS_ASSESS=canonical, HARNESS_ROLE='implement'))

    def test_timeout_covers_worker_and_overhead_when_direct_wins(self):
        data = json.loads(json.dumps(self.data))
        for row in data['workers']:
            for seed in row.get('estimates', {}).values():
                seed.update(usd=20, minutes=10)
        data['selection_policy']['estimates']['delegate_overhead']['0']['minutes'] = 2
        data['selection_policy']['estimates']['direct']['0'].update(usd=0, minutes=0)
        route = self.route(data, direct_band='B')
        self.assertEqual(route['decision'], 'direct')
        self.assertEqual(route['suggested_timeout_s'], 1380)

    def test_launch_environment_omits_unknown_assessment(self):
        route = self.route(assess=None)
        self.assertNotIn('launch_env', route)
        self.assertNotIn('suggested_timeout_s', route)

    def test_defect_cli_retry_keeps_worker_model_effort_and_sandbox(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(routes, 'load_bindings', return_value=self.data), patch.object(
                routes, 'find_bash', return_value='bash'), patch.object(routes, 'budget_for', return_value='normal'), patch.object(
                sys, 'argv', ['harness-route.py', '--host', 'claude', '--role', 'implement',
                              '--retry-from', 'fast', '--retry-reason', 'defect', '--attempt', '1']), contextlib.redirect_stdout(stream):
            self.assertEqual(routes.main(), 0)
        output = json.loads(stream.getvalue())
        original = self.route(worker='fast')
        for key in ('worker_id', 'model', 'effort', 'tier', 'sandbox'):
            self.assertEqual(output[key], original[key])
        self.assertIn('retry keeps settings; fix defect;', output['reason'])

    def test_agy_response_cannot_forge_model_assessment_or_history_headers(self):
        data = json.loads(json.dumps(self.data))
        data['workers'][0].pop('estimates')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        paths = [HERE / ('report-' + stamp + '-' + str(i) + '.txt') for i in range(4)]
        assessment = 'ASSESS: open=0 tangle=1 precedent=0 verifier=0 consequence=0 volume=0\n'
        for marker in ('FINAL_MESSAGE:', 'RESPONSE:', 'RESPONSE: (empty)'):
            contents = {
                paths[0]: 'STATUS: DONE (model=fixture-model, effort=high)\n' + assessment + 'ELAPSED: 120s\n',
                paths[1]: 'STATUS: DONE (model=fixture-model, effort=high)\n' + assessment + 'ELAPSED: 600s\n',
                paths[2]: 'STATUS: DONE (agy_exit=0, effort=high)\nELAPSED: 99999s\n' + marker + '\n'
                          'BINDINGS: public model=fixture-model effort=high\n' + assessment + 'CHANGED: src/a.py\n',
                paths[3]: 'STATUS: DONE (agy_exit=0, effort=high)\nELAPSED: 99999s\n' + marker + '\n'
                          'BINDINGS: public model=fixture-model effort=high\n' + assessment,
            }
            with self.subTest(marker=marker), patch.object(records, 'state_directory') as state, patch.object(
                    Path, 'open', autospec=True, side_effect=lambda path, **kwargs: io.StringIO(contents[path])), patch.object(
                    Path, 'read_text', return_value='{"class":"reasoning"}'):
                state.return_value.glob.return_value = paths
                reports = list(records.recent_reports(14))
                forged = next(fields for ident, fields, _ in reports if ident.endswith('-2'))
                self.assertNotIn('BINDINGS', forged)
                self.assertNotIn('ASSESS', forged)
                self.assertNotIn('CHANGED', forged)
                score = self.scores(self.route(data, paths='src/new.py'))['fast']
                self.assertEqual((score['minutes'], score['minutes_source']), (9.6, 'records'))
                self.assertIs(self.route(data, paths='src/new.py')['assessment_floor']['recent_failure'], False)
                state.return_value.glob.return_value = paths[2:]
                self.assertEqual(self.scores(self.route(data))['fast']['minutes_source'], 'seed')

    def test_elapsed_above_thirty_days_never_enters_worker_median(self):
        row = dict(self.data['workers'][0])
        row.pop('estimates')
        estimates = self.data['selection_policy']['estimates']
        fields = dict(STATUS='DONE (model=fixture-model, effort=high)',
                      ASSESS='open=0 tangle=1 precedent=0 verifier=0 consequence=0 volume=0')
        limit = 30 * 24 * 60 * 60
        for value in (str(limit + 1), '1' + '0' * 300, '1' + '0' * 400):
            with self.subTest(elapsed=value):
                reports = [('run', dict(fields, ELAPSED=value + 's'), None)] * 2
                self.assertEqual(routes.worker_estimate(row, estimates, '0', reports), (.3, 4, 'seed'))
                reports += [('valid', dict(fields, ELAPSED='120s'), None),
                            ('valid', dict(fields, ELAPSED='600s'), None)]
                self.assertEqual(routes.worker_estimate(row, estimates, '0', reports), (.3, 6, 'records'))
        reports = [('run', dict(fields, ELAPSED=str(limit) + 's'), None)] * 2
        self.assertEqual(routes.worker_estimate(row, estimates, '0', reports), (.3, limit / 60, 'records'))

    def test_unrepresentable_scores_use_normal_router_error_and_emit_no_json(self):
        assessment = 'open=0,tangle=1,precedent=0,verifier=0,consequence=0,volume=1'
        for options in (['--time-tolerance-min', '1e-308'], ['--time-refocus', '1e308', '--time-slope', '1e308']):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(options=options), patch.dict(os.environ, {}, clear=True), patch.object(
                    routes, 'load_bindings', return_value=self.data), patch.object(
                    routes, 'budget_for', return_value='normal'), patch.object(sys, 'argv', [
                    'harness-route.py', '--host', 'claude', '--role', 'implement', '--assess', assessment,
                    *options]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(routes.main(), 4)
            self.assertEqual(output.getvalue(), '')
            self.assertIn('HARNESS_DENIED: route configuration:', errors.getvalue())
        # Both a non-finite waiting cost and overflow in the final addition are rejected.
        for usd, minutes, k in ((0, 2, 1e308), (1e308, 1, 1e308)):
            with self.subTest(usd=usd, minutes=minutes, k=k), self.assertRaises(ValueError):
                routes.score_option('test', usd, minutes, 'seed', dict(tolerance_min=1, k=k), 2)
        json.dumps(self.route(time_cost=1e307)['scores'], allow_nan=False)

    def test_invalid_time_cli_values_are_usage_errors_even_without_scoring(self):
        for option, values in (('--time-tolerance-min', ('0', '-1', 'inf', 'nan', '-inf')),
                               ('--time-cost', ('-1', 'inf', 'nan', '-inf')),
                               ('--time-refocus', ('-1', 'inf', 'nan', '-inf')),
                               ('--time-slope', ('-1', 'inf', 'nan', '-inf'))):
            for value in values:
                with self.subTest(option=option, value=value), patch.object(sys, 'argv', [
                        'harness-route.py', '--host', 'claude', '--role', 'implement', option + '=' + value
                        ]), patch.object(routes, 'load_bindings') as load, contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as raised:
                        routes.main()
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn('must be a finite', errors.getvalue())
                    load.assert_not_called()

    def test_scored_row_with_unknown_cost_is_excluded_even_with_seed_estimates(self):
        data = routes.merge_bindings(self.data, {'workers_local': [{'id': 'fast', 'metrics': {'cost': None}}]})
        route = self.route(data)
        self.assertEqual(set(self.scores(route)), {'slow'})
        self.assertEqual(route['worker_id'], 'slow')
        self.assertIn(dict(id='fast', reason='missing metrics.cost for score'), route['skipped'])
        json.dumps(route, allow_nan=False)
        data['workers'][0].pop('estimates')
        self.assertEqual(set(self.scores(self.route(data))), {'slow'})
        data['workers'][1]['metrics']['cost'] = None
        with self.assertRaises(IndexError):
            self.route(data)

    def test_invalid_paths_are_usage_errors_before_routing(self):
        for value in ('', 'src/a.py,', ',src/a.py', 'src/a.py,,src/b.py',
                      'src/a.py,/abs/x', 'src/a.py,../x', 'src/a.py,src/../../x',
                      'src/a.py,C:\\abs\\x', 'src/a.py,\\\\host\\share\\x'):
            with self.subTest(paths=value), patch.object(sys, 'argv', [
                    'harness-route.py', '--host', 'claude', '--role', 'implement', '--paths', value
                    ]), patch.object(routes, 'load_bindings') as load, contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    routes.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('paths must be project-relative files', errors.getvalue())
                load.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(routes, 'load_bindings', return_value=self.data), patch.object(
                routes, 'find_bash', return_value='bash'), patch.object(sys, 'argv', [
                'harness-route.py', '--host', 'claude', '--role', 'implement', '--paths',
                'src/a.py,src/../other/b.py,src\\nested\\c.py']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(routes.main(), 0)

    def test_score_settings_are_validated_when_loading_public_and_local_bindings(self):
        cases = [
            (('time_cost',), None),
            (('time_cost', 'tolerance_min'), 0),
            (('time_cost', 'tolerance_min'), '30'),
            (('time_cost', 'tolerance_min'), float('inf')),
            (('time_cost', 'exponent'), 0),
            (('time_cost', 'exponent'), True),
            (('time_cost', 'switch_after_min'), -1),
            (('time_cost', 'switch_after_min'), float('nan')),
            (('time_cost', 'switch_after_min'), float('inf')),
            (('time_cost', 'switch_after_min'), True),
            (('time_cost', 'switch_after_min'), '3'),
            (('time_cost', 'default_mode'), 'invalid'),
            (('time_cost', 'default_mode'), []),
            (('time_cost', 'modes'), []),
            (('time_cost', 'modes', 'attended'), -1),
            (('time_cost', 'modes', 'background'), float('nan')),
            (('estimates',), None),
            (('estimates', 'history_days'), 'fourteen'),
            (('estimates', 'history_days'), -1),
            (('estimates', 'history_days'), 1.5),
            (('estimates', 'history_days'), True),
            (('estimates', 'history_days'), float('inf')),
            (('estimates', 'history_days'), 10 ** 20),
            (('estimates', 'direct'), []),
            (('estimates', 'direct', '0'), None),
            (('estimates', 'direct', '0', 'usd'), -1),
            (('estimates', 'direct', '0', 'minutes'), float('nan')),
            (('estimates', 'delegate_overhead', '2', 'minutes'), '20'),
            (('estimates', 'worker'), []),
            (('estimates', 'worker', 'task_units'), None),
            (('estimates', 'worker', 'task_units', '1'), -1),
            (('estimates', 'worker', 'base_minutes', '2'), float('inf')),
            (('estimates', 'worker', 'reference_ttft_s'), 0),
            (('estimates', 'worker', 'speed_clamp'), [3, .5]),
            (('estimates', 'worker', 'speed_clamp'), [1]),
            (('estimates', 'worker', 'speed_clamp'), [True, 3]),
            (('estimates', 'worker', 'min_records'), 0),
            (('estimates', 'worker', 'min_records'), 1.5),
        ]
        for key in ('k', 'refocus_usd', 'slope_usd_per_tolerance'):
            cases += [(('time_cost', 'modes', mode, key), value)
                      for mode in records.TIME_MODES
                      for value in (-1, float('inf'), float('nan'), True, '1', None, 2 ** 2048)]
        for keys, value in cases:
            override = value
            for key in reversed(keys):
                override = {key: override}
            override = {'selection_policy': override}
            for source in ('public', 'local'):
                public = routes.merge(self.data, override) if source == 'public' else self.data
                local = override if source == 'local' else {}
                with self.subTest(keys=keys, value=value, source=source), patch.object(
                        Path, 'exists', return_value=source == 'local'), patch.object(
                        Path, 'read_text', autospec=True, side_effect=lambda path, **kwargs:
                        json.dumps(local if path.name == 'model-bindings.local.json' else public)):
                    with self.assertRaisesRegex(ValueError, keys[0]):
                        routes.load_bindings(ROOT)
        for values in ({'0': {'usd': -1, 'minutes': 1}}, {'0': {'usd': 1, 'minutes': '1'}},
                       {'3': {'usd': 1, 'minutes': 1}}, []):
            with self.subTest(row_estimates=values), self.assertRaisesRegex(ValueError, 'estimates'):
                routes.merge_bindings(self.data, {'workers_local': [{'id': 'fast', 'estimates': values}]})
        # Zero waiting value, history window and estimates are valid.
        valid = routes.merge_bindings(self.data, {'selection_policy': {
            'time_cost': {'modes': {'attended': 0}}, 'estimates': {'history_days': 0,
            'direct': {'0': {'usd': 0, 'minutes': 0}}}}})
        self.assertEqual(valid['selection_policy']['estimates']['history_days'], 0)

    def test_fixed_three_mode_scores_change_worker_and_direct_decisions(self):
        for mode, decision, winner, worker, totals in [
                ('attended', 'direct', 'fast', 'fast', {'direct': 8.97, 'fast': 8.95, 'slow': 9.25}),
                ('background', 'direct', 'fast', 'fast', {'fast': 4.1, 'direct': 4.12, 'slow': 4.4}),
                ('unattended', 'delegate', 'slow', 'slow', {'slow': 3.15, 'fast': 3.27, 'direct': 3.85})]:
            with self.subTest(mode=mode):
                route = self.route(direct_band='A', time_mode=mode)
                self.assertEqual((route['decision'], route['worker_id']), (decision, worker))
                self.assertEqual(route['scores'][0]['option'], winner)
                self.assertEqual({s['option']: s['total'] for s in route['scores']}, totals)
                self.assertEqual(route['time_cost'], dict(records.time_value(self.data['selection_policy']['time_cost'], mode), source='cli'))
                self.assertIn('score: ' + ('direct' if decision == 'direct' else winner), route['reason'])
                self.assertIn('time ' + mode + ' T=30 k=', route['reason'])
                self.assertTrue(route['floor_met'])
                self.assertTrue(route['separation_satisfied'])
                self.assertEqual(route['vendor'], 'openai')
                self.assertEqual(route['model'], 'fixture-model')
                self.assertEqual([s['total'] for s in route['scores']], sorted(s['total'] for s in route['scores']))

    def test_waiting_threshold_and_switched_away_lines(self):
        policy = self.data['selection_policy']['time_cost']
        for mode, cases in (
                ('attended', ((0, 0), (2, 5 * (2 / 30) ** 2), (3, .05), (4, 5.05), (30, 6.35), (60, 7.85))),
                ('background', ((0, 0), (2, .1), (3, .15), (4, .2), (30, 1.5), (60, 3))),
                ('unattended', ((0, 0), (2, 1 / 60), (3, .025), (4, 1 / 30), (30, .25), (60, .5)))):
            for minutes, expected in cases:
                with self.subTest(mode=mode, minutes=minutes):
                    score = routes.score_option('test', 2, minutes, 'seed', records.time_value(policy, mode), 2)
                    self.assertAlmostEqual(score['time_cost'], expected)
                    self.assertAlmostEqual(score['total'], 2 + expected)
        # The switch threshold is configurable and accepts zero.
        for threshold in (0, 10):
            time = records.time_value(dict(policy, switch_after_min=threshold))
            self.assertAlmostEqual(routes.score_option('test', 0, threshold, 'seed', time, 2)['time_cost'],
                                   5 * (threshold / 30) ** 2)
            self.assertAlmostEqual(routes.score_option('test', 0, threshold + 1, 'seed', time, 2)['time_cost'], 5.05)
        for mode in ('background', 'unattended'):
            time = records.time_value(policy, mode, k=99, refocus_usd=2, slope_usd_per_tolerance=3)
            self.assertEqual(routes.score_option('test', 0, 60, 'seed', time, 2)['time_cost'], 8)

    def test_legacy_numeric_modes_keep_pure_convex_cost(self):
        local = routes.merge_bindings(self.data, {'selection_policy': {'time_cost': {
            'modes': {'attended': 5, 'background': 1.5, 'unattended': .25}}}})
        local['selection_policy']['time_cost'].pop('switch_after_min')
        for mode, k in (('attended', 5), ('background', 1.5), ('unattended', .25)):
            time = routes.time_cost_for(local, {}, mode=mode)
            self.assertEqual((time['k'], time['refocus_usd'], time['slope_usd_per_tolerance']), (k, 0, 0))
            for minutes in (2, 3, 4, 60):
                with self.subTest(mode=mode, minutes=minutes):
                    self.assertAlmostEqual(routes.score_option('test', 0, minutes, 'seed', time, 2)['time_cost'],
                                           k * (minutes / 30) ** 2)

    def test_public_volume_two_seed_slower_cheaper_worker_wins_after_switch(self):
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        public['workers'] = [row for row in public['workers'] if row['id'] == 'sol61-max']
        options = dict(assess=self.assess(tangle=2, verifier=1, volume=2), direct_band='A')
        route = self.route(public, **options)
        scores = self.scores(route)
        self.assertEqual((route['decision'], route['worker_id']), ('delegate', 'sol61-max'))
        self.assertEqual(scores['direct']['minutes'], 30.4)
        self.assertEqual(scores['direct']['usd'], 13.6)
        self.assertGreater(scores['sol61-max']['minutes'], scores['direct']['minutes'])
        self.assertLess(scores['sol61-max']['usd'], scores['direct']['usd'])
        self.assertLess(scores['sol61-max']['total'], scores['direct']['total'])
        self.assertEqual((scores['sol61-max']['total'], scores['direct']['total']), (15.82, 19.97))
        legacy = routes.merge_bindings(public, {'selection_policy': {'time_cost': {'modes': {'attended': 5}}}})
        old = self.route(legacy, **options)
        self.assertEqual(old['decision'], 'direct')
        self.assertGreater(self.scores(old)['sol61-max']['total'], self.scores(old)['direct']['total'])
        self.assertEqual((self.scores(old)['sol61-max']['total'], self.scores(old)['direct']['total']), (22.11, 18.73))

    def test_direct_only_implement_write_and_band_at_least_the_floor(self):
        for role in routes.ROLES:
            for band in ('E', 'C', 'B', 'A', 'S'):
                options = {'author_vendors': ['claude']} if role in routes.REVIEW_ROLES else {}
                route = self.route(role=role, direct_band=band, **options)
                expected = role in ('implement', 'write') and band in ('B', 'A', 'S')
                self.assertEqual('direct' in self.scores(route), expected, (role, band))
        self.assertNotIn('direct', self.scores(self.route()))
        self.assertNotIn('direct', self.scores(self.route(direct_band='B', assess=self.assess(tangle=2))))

    def test_score_chooses_detached_only_and_high_ttft_rows_without_latency_filtering(self):
        route = self.route(time_mode='unattended')
        self.assertEqual(route['worker_id'], 'slow')
        self.assertEqual(route['eligible'], ['slow', 'fast'])
        self.assertTrue(route['needs_detached'])
        self.assertEqual(route['latency_class'], 'foreground')
        self.assertIn('run with -b --wait', route['reason'])
        self.assertNotIn('slow', [item['id'] for item in route['skipped']])
        data = routes.merge_bindings(self.data, {'workers_local': [{'id': 'slow', 'effort': 'xhigh'}]})
        self.assertTrue(self.route(data, time_mode='unattended')['needs_detached'])

    def test_availability_then_trust_precede_even_a_lower_score(self):
        low_trust = routes.merge_bindings(self.data, {'workers_local': [{'id': 'slow', 'trust': 'low'}]})
        route = self.route(low_trust, time_mode='unattended')
        self.assertEqual(route['worker_id'], 'fast')
        self.assertEqual(route['scores'][0]['option'], 'slow')
        self.assertIn('ahead of slow $3.15', route['reason'])
        data = routes.merge_bindings(self.data, {'workers_local': [{'id': 'fast', 'vendor': 'google', 'trust': 'low'}]})
        route = self.route(data, time_mode='unattended', budget='exhausted:openai')
        self.assertEqual(route['worker_id'], 'fast')
        self.assertTrue(route['available'])
        exhausted = self.route(time_mode='unattended', budget='exhausted:openai')
        self.assertFalse(exhausted['available'])

    def test_one_percent_tie_uses_minutes_and_output_stays_sorted_by_total(self):
        data = routes.merge_bindings(self.data, {'workers_local': [
            {'id': 'fast', 'estimates': {'0': {'usd': .12, 'minutes': 1}}},
            {'id': 'slow', 'estimates': {'0': {'usd': .10, 'minutes': 100}}}]})
        route = self.route(data, time_cost=0, time_refocus=0, time_slope=0)
        self.assertEqual(route['worker_id'], 'fast')
        self.assertEqual([s['option'] for s in route['scores']], ['slow', 'fast'])
        data = routes.merge_bindings(data, {'workers_local': [{'id': 'fast', 'estimates': {'0': {'usd': .14}}}]})
        self.assertEqual(self.route(data, time_cost=0, time_refocus=0, time_slope=0)['worker_id'], 'slow')

    def test_best_worker_ties_are_ranked_independently_of_the_direct_option(self):
        data = routes.merge_bindings(self.data, {
            'selection_policy': {'estimates': {'direct': {'0': {'usd': 100, 'minutes': 1}}}},
            'workers_local': [
                {'id': 'fast', 'estimates': {'0': {'usd': 97.7, 'minutes': 100}}},
                {'id': 'slow', 'estimates': {'0': {'usd': 98.3, 'minutes': 1}}}]})
        route = self.route(data, time_cost=0, time_refocus=0, time_slope=0, direct_band='A')
        self.assertEqual(route['worker_id'], 'slow')
        self.assertEqual(route['decision'], 'direct')

    def test_worker_formula_units_sqrt_clamp_missing_ttft_and_row_estimates(self):
        estimates = self.data['selection_policy']['estimates']
        row = dict(self.data['workers'][0])
        row.pop('estimates')
        for volume, units, base in [('0', 1, 4), ('1', 2.5, 15), ('2', 4, 30)]:
            for ttft, factor in [(None, 1), (0, .5), (58, 1), (232, 2), (5800, 3)]:
                with self.subTest(volume=volume, ttft=ttft):
                    row['metrics'] = dict(row['metrics'], ttft_s=ttft)
                    usd, minutes, source = routes.worker_estimate(row, estimates, volume, [])
                    self.assertAlmostEqual(usd, .3 * units)
                    self.assertAlmostEqual(minutes, base * factor)
                    self.assertEqual(source, 'seed')
        usd, minutes, source = routes.worker_estimate(self.data['workers'][0], estimates, '0', [])
        self.assertEqual((usd, minutes, source), (.3, 16.4, 'seed'))

    def test_no_volume_tier_worker_and_retries_ignore_all_scoring_inputs(self):
        assessment = self.assess(volume=2, tangle=2, verifier=2)
        for options in ({'tier': 'B'}, {'worker': 'fast'},
                        {'retry_from': 'fast', 'retry_reason': 'infra', 'attempt': 1}):
            plain = routes.resolve(self.data, 'claude', 'implement', env={}, **options)
            scored = self.route(assess=assessment, direct_band='S', time_mode='unattended',
                                paths='src/a.py', **options)
            self.assertEqual(scored, plain)
        no_volume = self.assess()
        no_volume.pop('volume')
        self.assertEqual(self.route(assess=no_volume, direct_band='A', time_mode='unattended')['worker_id'], 'fast')
        self.assertNotIn('scores', self.route(assess=no_volume))
        self.assertNotIn('value rule', self.route(assess=no_volume)['reason'])
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        options = dict(retry_from='sol61-medium', retry_reason='reasoning', attempt=1)
        plain = routes.resolve(public, 'claude', 'implement', env={}, **options)
        self.assertEqual(routes.resolve(public, 'claude', 'implement', env={}, assess=assessment,
            paths='src/a.py', direct_band='S', time_mode='unattended', **options), plain)

    def test_scored_path_keeps_band_author_status_requirements_and_ultra_guards(self):
        changes = [{'id': 'slow', 'effort': 'ultra'}, {'id': 'fast', 'tier': 'D'}]
        data = routes.merge_bindings(self.data, {'workers_local': changes})
        route = self.route(data)
        self.assertNotIn('slow', self.scores(route))
        self.assertFalse(route['floor_met'])
        with self.assertRaisesRegex(ValueError, 'Codex worker ultra'):
            self.route(data, worker='slow', latency='detached')
        for fields in ({'status': 'conditional'}, {'status': 'unverified'},
                       {'requires': {'binary': 'nonexistent-score-test-binary'}},
                       {'vendor': 'claude'}, {'roles': {'implement': {'priority': 2, 'hosts': ['codex']}}}):
            with self.subTest(fields=fields):
                data = routes.merge_bindings(self.data, {'workers_local': [dict(id='slow', **fields)]})
                self.assertEqual(set(self.scores(self.route(data))), {'fast'})

    def test_s_rows_only_scored_for_s_floor_and_both_efforts_are_candidates(self):
        rows = [dict(self.data['workers'][0], id='top-' + effort, model='top-openai',
                     effort=effort, tier='S', tier_fixed=True, roles={'implement': 20 + i},
                     estimates={'0': {'usd': 1 - i * .5, 'minutes': 2}})
                for i, effort in enumerate(('high', 'xhigh'))]
        data = routes.merge_bindings(self.data, {'workers_local': rows})
        self.assertEqual(set(self.scores(self.route(data))), {'fast', 'slow'})
        route = self.route(data, assess=self.assess(tangle=2, verifier=2))
        self.assertEqual(set(self.scores(route)), {'top-high', 'top-xhigh'})
        self.assertEqual(route['worker_id'], 'top-xhigh')

    def test_time_value_precedence_and_partial_cli_overrides(self):
        policy = self.data['selection_policy']['time_cost']
        self.assertEqual(self.route()['time_cost'], dict(records.time_value(policy), source='bindings'))
        local = routes.merge_bindings(self.data, {'selection_policy': {'time_cost': {
            'default_mode': 'background', 'tolerance_min': 45, 'switch_after_min': 4,
            'modes': {'background': {'k': 2, 'refocus_usd': 1, 'slope_usd_per_tolerance': 3}}}}})
        local_policy = local['selection_policy']['time_cost']
        self.assertEqual(self.route(local)['time_cost'], dict(records.time_value(local_policy), source='bindings'))
        session_time = dict(mode='unattended', tolerance_min=90, k=.1, refocus_usd=2,
                            slope_usd_per_tolerance=.5, switch_after_min=6)
        declaration = dict(time_cost=session_time)
        with patch.object(Path, 'read_text', return_value=json.dumps(declaration)):
            env = {'HARNESS_SESSION_ID': 'session-1'}
            self.assertEqual(routes.time_cost_for(local, env), dict(session_time, source='session'))
            self.assertEqual(routes.time_cost_for(local, env, mode='attended'),
                             dict(mode='attended', tolerance_min=90, k=5, refocus_usd=5,
                                  slope_usd_per_tolerance=1.5, switch_after_min=6, source='cli'))
            for options, changes in ((dict(tolerance_min=10, cost=0), dict(tolerance_min=10, k=0)),
                                     (dict(refocus=0), dict(refocus_usd=0)),
                                     (dict(slope=3), dict(slope_usd_per_tolerance=3))):
                self.assertEqual(routes.time_cost_for(local, env, **options), dict(session_time, **changes, source='cli'))
            self.assertEqual(routes.time_cost_for(local, env, mode='background', cost=7, refocus=8, slope=9),
                             dict(mode='background', tolerance_min=90, k=7, refocus_usd=8,
                                  slope_usd_per_tolerance=9, switch_after_min=6, source='cli'))
            self.assertEqual(routes.time_cost_for(local, {})['source'], 'bindings')
        invalid = [None, [], {}, {'mode': 'bad', 'tolerance_min': 30, 'k': 5},
                            {'mode': 'attended', 'tolerance_min': 0, 'k': 5},
                            {'mode': 'attended', 'tolerance_min': 30, 'k': -1},
                            {'mode': 'attended', 'tolerance_min': 30, 'k': 2 ** 2048},
                            {'mode': 'attended', 'tolerance_min': '30', 'k': 5},
                            {'mode': 'attended', 'tolerance_min': 30, 'k': float('nan')}]
        invalid += [dict(session_time, **{key: value})
                    for key in ('refocus_usd', 'slope_usd_per_tolerance', 'switch_after_min')
                    for value in (-1, float('inf'), float('nan'), True, '1', None, 2 ** 2048)]
        for declaration in invalid:
            with self.subTest(declaration=declaration), patch.object(Path, 'read_text', return_value=json.dumps({'time_cost': declaration})):
                self.assertEqual(routes.time_cost_for(local, {'HARNESS_SESSION_ID': 'session-1'})['source'], 'bindings')
        with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')):
            self.assertEqual(routes.time_cost_for(local, {'HARNESS_SESSION_ID': 'session-1'})['source'], 'bindings')
        for text in ('{broken', '[' * 2000 + '0' + ']' * 2000):
            with patch.object(Path, 'read_text', return_value=text):
                self.assertEqual(routes.time_cost_for(local, {'HARNESS_SESSION_ID': 'session-1'})['source'], 'bindings')
                self.assertIsNone(routes.session_budget({'HARNESS_SESSION_ID': 'session-1'}))
        for options in ({'time_tolerance_min': 0}, {'time_cost': -1}, {'time_cost': float('inf')},
                        {'time_refocus': -1}, {'time_refocus': float('nan')},
                        {'time_slope': -1}, {'time_slope': float('inf')}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.route(**options)

    def test_cli_accepts_optional_volume_and_new_flags(self):
        for volume in (None, 0, 1, 2):
            assessment = self.assess(volume=volume)
            if volume is None:
                assessment.pop('volume')
            command = ['harness-route.py', '--host', 'claude', '--role', 'implement', '--assess',
                       ','.join(f'{key}={value}' for key, value in reversed(list(assessment.items()))),
                       '--direct-band', 'A', '--time-mode', 'unattended', '--time-tolerance-min', '45', '--time-cost', '0',
                       '--time-refocus', '2', '--time-slope', '3',
                       '--paths', 'src/a.py,src/b.py']
            stream = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch.object(routes, 'load_bindings', return_value=self.data), patch.object(
                    routes, 'find_bash', return_value='bash'), patch.object(sys, 'argv', command), contextlib.redirect_stdout(stream):
                self.assertEqual(routes.main(), 0)
            output = json.loads(stream.getvalue())
            self.assertEqual(output['assessment'], assessment)
            self.assertEqual('scores' in output, volume is not None)
            if volume is not None:
                self.assertEqual(output['time_cost'], dict(mode='unattended', tolerance_min=45, k=0,
                                 refocus_usd=2, slope_usd_per_tolerance=3, switch_after_min=3, source='cli'))
        for value in ('3', '-1', '01', 'x', ''):
            with patch.object(sys, 'argv', ['harness-route.py', '--host', 'claude', '--role', 'implement',
                    '--assess', 'open=0,tangle=0,precedent=0,verifier=0,consequence=0,volume=' + value]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    routes.main()
                self.assertEqual(raised.exception.code, 2)

    def write_report(self, directory, index, *, age=0, effort='high', model='fixture-model',
                     volume=0, elapsed='120s', status='DONE', changed='src/a.py', extra=''):
        stamp = (datetime.now(timezone.utc) - timedelta(days=age)).strftime('%Y%m%dT%H%M%SZ')
        ident = stamp + '-' + str(index)
        assessment = 'open=0 tangle=1 precedent=0 verifier=0 consequence=0 volume=' + str(volume)
        report = directory / ('report-' + ident + '.txt')
        report.write_text(f'STATUS: {status} (model={model}, effort={effort})\nASSESS: {assessment}\n'
                          f'ELAPSED: {elapsed}\nCHANGED: {changed}\n' + extra, encoding='utf-8')
        return ident, report

    def test_history_headers_drive_minutes_and_failure_floor_without_result_text(self):
        data = json.loads(json.dumps(self.data))
        data['workers'][0].pop('estimates')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        paths = [HERE / ('report-' + stamp + '-' + str(i) + '.txt') for i in range(4)]
        header = 'ASSESS: open=0 tangle=0 precedent=0 verifier=0 consequence=0 volume=0\n'
        contents = {
            paths[0]: 'STATUS: DONE (model=fixture-model, effort=high)\n' + header + 'ELAPSED: 120s\n',
            paths[1]: 'STATUS: DONE (model=fixture-model, effort=high)\n' + header + 'ELAPSED: 600s\n',
            paths[2]: 'STATUS: FAILED\nCHANGED: Src\\Nested\\old.py\n',
            paths[3]: 'STATUS: DONE\nFINAL_MESSAGE:\nSTATUS: DONE (model=fixture-model, effort=high)\n'
                      + header + 'ELAPSED: 9999s\nCHANGED: other/quoted.py\n',
        }
        with patch.object(records, 'state_directory') as state, patch.object(
                Path, 'open', autospec=True, side_effect=lambda path, **kwargs: io.StringIO(contents[path])) as opened, patch.object(
                Path, 'read_text', autospec=True, return_value='{"class":"reasoning"}') as outcome:
            state.return_value.glob.return_value = paths
            route = self.route(data, assess=self.assess(tangle=0), paths='src/nested/new.py')
            self.assertEqual(self.scores(route)['fast']['minutes'], 9.6)
            self.assertEqual(self.scores(route)['fast']['minutes_source'], 'records')
            self.assertEqual(route['band'], 'B')
            self.assertEqual(route['assessment_floor']['recent_failure'], dict(run=stamp + '-2', directory='src/nested'))
            self.assertIs(self.route(data, paths='other/new.py')['assessment_floor']['recent_failure'], False)
            outcome.return_value = '[' * 2000 + '0' + ']' * 2000
            self.assertIs(self.route(data, paths='src/nested/new.py')['assessment_floor']['recent_failure'], False)
            state.return_value.glob.return_value = [paths[0], paths[2], paths[3]]
            self.assertEqual(self.scores(self.route(data))['fast']['minutes_source'], 'seed')
            opened.side_effect = PermissionError('unreadable report')
            route = self.route(data, assess=self.assess(tangle=0), paths='src/nested/new.py')
            self.assertEqual(route['band'], 'C')
            self.assertIs(route['assessment_floor']['recent_failure'], False)

    def test_enough_matching_done_records_replace_formula_with_median_minutes(self):
        data = json.loads(json.dumps(self.data))
        data['workers'][0].pop('estimates')
        with tempfile.TemporaryDirectory() as folder, patch.object(records, 'state_directory', return_value=Path(folder)):
            directory = Path(folder)
            self.write_report(directory, 0, elapsed='120s')
            score = self.scores(self.route(data))['fast']
            self.assertEqual((score['minutes'], score['minutes_source']), (7.6, 'seed'))
            self.write_report(directory, 1, elapsed='600s')
            self.write_report(directory, 2, elapsed='9999s', effort='max')
            self.write_report(directory, 3, elapsed='9999s', model='other-model')
            self.write_report(directory, 4, elapsed='9999s', volume=1)
            self.write_report(directory, 5, elapsed='9999s', status='FAILED')
            self.write_report(directory, 6, elapsed='9999s', age=15)
            self.write_report(directory, 7, elapsed='malformed')
            self.write_report(directory, 8, volume=3)
            score = self.scores(self.route(data))['fast']
            self.assertEqual((score['usd'], score['minutes'], score['minutes_source']), (3.1, 9.6, 'records'))
            # Per-row estimates take precedence over the formula and its history replacement.
            self.assertEqual(self.scores(self.route())['fast']['minutes_source'], 'seed')
            _, report = self.write_report(directory, 9, elapsed='unknown')
            report.write_text('STATUS: DONE\nFINAL_MESSAGE:\nSTATUS: DONE (model=fixture-model, effort=high)\n'
                              'ASSESS: open=0 tangle=1 precedent=0 verifier=0 consequence=0 volume=0\nELAPSED: 9999s\n', encoding='utf-8')
            self.assertEqual(self.scores(self.route(data))['fast']['minutes'], 9.6)

    def test_history_reasoning_failure_matches_normalized_parent_directory_once(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(records, 'state_directory', return_value=Path(folder)):
            directory = Path(folder)
            ident, _ = self.write_report(directory, 0, status='FAILED', changed='other/a.py,Src\\Nested\\old.py')
            outcome = directory / ('outcome-' + ident + '.json')
            outcome.write_text(json.dumps({'run': ident, 'class': 'reasoning'}), encoding='utf-8')
            route = self.route(assess=self.assess(tangle=0), paths='src/nested/new.py,other2/x.py')
            self.assertEqual(route['band'], 'B')
            self.assertEqual(route['assessment_floor']['recent_failure'], dict(run=ident, directory='src/nested'))
            self.assertIn('run ' + ident + ', directory src/nested', route['reason'])
            manual = self.route(assess=self.assess(tangle=0), paths='src/nested/new.py', recent_failure=True)
            self.assertEqual(manual['band'], 'B')
            self.assertIs(manual['assessment_floor']['recent_failure'], True)
            no_volume = self.assess(tangle=0)
            no_volume.pop('volume')
            self.assertEqual(self.route(assess=no_volume, paths='src/nested/new.py')['band'], 'B')
            for failure_class in ('none', 'infra', 'availability', 'spec', 'scope', 'knowledge'):
                outcome.write_text(json.dumps({'class': failure_class}), encoding='utf-8')
                self.assertIs(self.route(assess=self.assess(tangle=0), paths='src/nested/new.py')['assessment_floor']['recent_failure'], False)
            outcome.write_text(json.dumps({'class': 'reasoning'}), encoding='utf-8')
            self.assertIs(self.route(assess=self.assess(tangle=0), paths='src/elsewhere.py')['assessment_floor']['recent_failure'], False)

    def test_old_malformed_body_only_and_unreadable_history_never_raise(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(records, 'state_directory', return_value=Path(folder)):
            directory = Path(folder)
            ident, _ = self.write_report(directory, 0, age=15)
            (directory / ('outcome-' + ident + '.json')).write_text('{"class":"reasoning"}', encoding='utf-8')
            ident, report = self.write_report(directory, 1)
            outcome = directory / ('outcome-' + ident + '.json')
            for text in ('null', '{broken', '[]'):
                outcome.write_text(text, encoding='utf-8')
                self.assertIs(self.route(paths='src/a.py')['assessment_floor']['recent_failure'], False)
            outcome.write_text('{"class":"reasoning"}', encoding='utf-8')
            report.write_text('STATUS: FAILED\nFINAL_MESSAGE:\nCHANGED: src/a.py\n', encoding='utf-8')
            (directory / 'report-not-a-date.txt').write_text('CHANGED: src/a.py', encoding='utf-8')
            (directory / 'report-20269999T999999Z-x.txt').mkdir()
            self.assertIs(self.route(paths='src/a.py')['assessment_floor']['recent_failure'], False)
            with patch.object(Path, 'open', side_effect=PermissionError('report unreadable')):
                self.assertIs(self.route(paths='src/a.py')['assessment_floor']['recent_failure'], False)
            with patch.object(Path, 'read_text', side_effect=PermissionError('outcome unreadable')):
                self.assertIs(self.route(paths='src/a.py')['assessment_floor']['recent_failure'], False)
        for error in (PermissionError('state unreadable'), ValueError('invalid tree key')):
            with patch.object(records, 'state_directory', side_effect=error):
                self.assertIs(self.route(paths='src/a.py')['assessment_floor']['recent_failure'], False)


class RelativeRetries(unittest.TestCase):
    def setUp(self):
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        self.data = routes.merge_bindings(public, {})

    def retry(self, data, host, previous, attempt=1, role='implement', **options):
        return routes.resolve(data, host, role, retry_from=previous,
                              retry_reason='reasoning', attempt=attempt, **options)

    def test_each_relative_band_step_on_both_hosts(self):
        cases = [
            ('claude', 'luna6-low', None, 'D', 'luna6-high', False),
            ('claude', 'luna6-high', None, 'C', 'sol61-medium', False),
            ('claude', 'sol61-medium', 'C', 'B', 'sol61-high', False),
            ('claude', 'sol61-medium', None, 'A', 'sol61-max', True),
            ('claude', 'sol61-max', None, 'S', 'astra-high', False),
            ('codex', 'opus55-medium', 'E', 'D', 'sonnet55-medium', False),
            ('codex', 'opus55-medium', 'D', 'C', 'sonnet55-medium', False),
            ('codex', 'sonnet55-medium', None, 'B', 'opus55-medium', False),
            ('codex', 'opus55-medium', None, 'A', 'opus55-high', False),
            ('codex', 'opus55-high', None, 'S', 'fable-high', False),
        ]
        for host, previous, previous_band, floor, expected, detached in cases:
            data = self.data if previous_band is None else routes.merge_bindings(self.data, {
                'workers_local': [{'id': previous, 'tier': previous_band, 'tier_cell': False}]})
            for attempt in (1, 2):
                with self.subTest(host=host, previous=previous, previous_band=previous_band, attempt=attempt):
                    route = self.retry(data, host, previous, attempt=attempt)
                    self.assertEqual((route['band'], route['worker_id'], route['needs_detached']),
                                     (floor, expected, detached))
                    self.assertTrue(route['floor_met'])
                    self.assertEqual(route['vendor'], next(w['vendor'] for w in data['workers'] if w['id'] == previous))

    def test_cheapest_fast_promotion_wins_then_cheapest_detached_when_all_are_slow(self):
        cases = [('codex', 'opus55-medium', 'opus55-high', 'opus55-xhigh'),
                 ('claude', 'sol61-medium', 'sol61-high', 'sol61-max')]
        for host, previous, fast, slow in cases:
            data = routes.merge_bindings(self.data, {'workers_local': [
                {'id': fast, 'tier': 'A', 'tier_cell': False, 'metrics': {'cost': 2, 'ttft_s': 20}},
                {'id': slow, 'metrics': {'cost': .5}}]})
            self.assertEqual(self.retry(data, host, previous)['worker_id'], fast)
            data = routes.merge_bindings(data, {'workers_local': [{'id': fast, 'metrics': {'ttft_s': 90}}]})
            route = self.retry(data, host, previous)
            self.assertEqual(route['worker_id'], slow)
            self.assertTrue(route['needs_detached'])
            self.assertIn('run with -b --wait', route['reason'])

    def test_no_higher_ordinary_row_steps_to_same_lane_s(self):
        for host, previous, vendor, expected in [('claude', 'sol61-medium', 'openai', 'astra-high'),
                                                  ('codex', 'opus55-medium', 'claude', 'fable-high')]:
            data = routes.merge_bindings(self.data, {'workers_local': [
                {'id': w['id'], 'status': 'conditional'} for w in self.data['workers']
                if w['vendor'] == vendor and w['tier'] == 'A']})
            route = self.retry(data, host, previous)
            self.assertEqual((route['band'], route['worker_id']), ('S', expected))

    def test_a_to_s_uses_foreground_effort_and_marks_detached_for_a_slow_class(self):
        for host, previous, expected in [('claude', 'sol61-max', 'astra-high'),
                                          ('codex', 'opus55-high', 'fable-high')]:
            for latency, detached in [('interactive', True), ('foreground', False), ('detached', False)]:
                route = self.retry(self.data, host, previous, latency=latency)
                self.assertEqual(route['worker_id'], expected)
                self.assertEqual(route['needs_detached'], detached)

    def test_s_foreground_to_detached_then_one_cross_lane_on_both_hosts(self):
        for host, first, detached, cross in [('claude', 'astra-high', 'astra-xhigh', 'fable-high'),
                                            ('codex', 'fable-high', 'fable-xhigh', 'astra-high')]:
            for role in ('implement', 'write'):
                for latency in ('foreground', 'detached'):
                    second = self.retry(self.data, host, first, role=role, latency=latency)
                    self.assertEqual((second['worker_id'], second['latency_class']), (detached, 'detached'))
                    self.assertTrue(second['needs_detached'])
                    for attempt in (2, 3):
                        route = self.retry(self.data, host, detached, role=role, attempt=attempt, latency=latency)
                        self.assertEqual((route['worker_id'], route['effort'], route['band']), (cross, 'high', 'S'))
                        self.assertTrue(route['cross_lane_s'])
                        self.assertFalse(route['separation_satisfied'])
                        self.assertTrue(route['available'])
                        self.assertTrue(route['floor_met'])
                        self.assertIn(route['vendor'], route['author_vendors'])
                        self.assertNotIn(cross, [w['id'] for w in route['skipped']])
                        self.assertIn('author separation is waived for this single attempt', route['reason'])
                        self.assertIn('reviewed by a different vendor than the implementer', route['reason'])

    def test_retry_from_cross_lane_s_is_refused_for_implementation_and_writing(self):
        for host, cross, author in [('claude', 'fable-high', 'claude'), ('codex', 'astra-high', 'openai')]:
            for role in ('implement', 'write'):
                for attempt in (2, 4):
                    with self.subTest(host=host, role=role, attempt=attempt), self.assertRaisesRegex(
                            ValueError, 'cross-lane S already failed; orchestrator decides \\(retry-policy 4\\)'):
                        routes.resolve(self.data, host, role, retry_from=cross, retry_reason='reasoning',
                                       attempt=attempt, author_vendors=[author])

    def test_cross_lane_s_never_serves_advice_or_review_roles(self):
        for host, foreground, detached, author in [('codex', 'fable-high', 'fable-xhigh', 'openai'),
                                                   ('claude', 'astra-high', 'astra-xhigh', 'claude')]:
            for role in ('decide', 'plan_review', 'review_gate', 'review_deep'):
                for data, previous in [(self.data, detached),
                                       (dict(self.data, workers=[w for w in self.data['workers']
                                                                if w['id'] != detached]), foreground)]:
                    with self.subTest(host=host, role=role, previous=previous), self.assertRaisesRegex(
                            ValueError, r'^S row already failed at its detached effort; orchestrator decides \(retry-policy 4\)$'):
                        self.retry(data, host, previous, role=role, author_vendors=[author], attempt=2)

    def test_google_author_sequence_stops_before_cross_lane_and_never_cycles_back(self):
        options = dict(author_vendors=['google'], latency='detached')
        first = routes.resolve(self.data, 'claude', 'implement', tier='S', **options)
        self.assertEqual(first['worker_id'], 'astra-xhigh')
        self.assertTrue(first['separation_satisfied'])
        self.assertNotIn('cross_lane_s', first)
        with self.assertRaisesRegex(ValueError, 'S row already failed at its detached effort'):
            self.retry(self.data, 'claude', first['worker_id'], **options)
        # An independent lane remains available through explicit routing under the orchestrator's cap.
        manual = routes.resolve(self.data, 'claude', 'implement', worker='fable-high', **options)
        self.assertTrue(manual['separation_satisfied'])
        self.assertNotIn('cross_lane_s', manual)
        second = self.retry(self.data, 'claude', manual['worker_id'], attempt=2, **options)
        self.assertEqual(second['worker_id'], 'fable-xhigh')
        self.assertTrue(second['separation_satisfied'])
        self.assertNotIn('cross_lane_s', second)
        with self.assertRaisesRegex(ValueError, 'S row already failed at its detached effort'):
            self.retry(self.data, 'claude', second['worker_id'], attempt=3, **options)
        for host, previous in [('claude', 'astra-xhigh'), ('codex', 'fable-xhigh')]:
            for role in ('implement', 'write'):
                with self.subTest(host=host, role=role), self.assertRaisesRegex(
                        ValueError, 'S row already failed at its detached effort'):
                    self.retry(self.data, host, previous, role=role, **options)

    def test_cross_lane_failure_refusal_applies_only_to_reasoning_retries(self):
        for host, cross, author in [('claude', 'fable-high', 'claude'), ('codex', 'astra-high', 'openai')]:
            for role in routes.SEPARATED_ROLES:
                for reason in ('infra', 'availability', 'spec', 'scope', 'knowledge'):
                    with self.subTest(host=host, role=role, reason=reason), self.assertRaisesRegex(
                            ValueError, r'^retry worker is not eligible for role/host/authors$'):
                        routes.resolve(self.data, host, role, retry_from=cross, retry_reason=reason,
                                       attempt=1, author_vendors=[author])

    def test_cap_allows_only_cross_lane_s_as_the_extra_attempt(self):
        for host, ordinary, foreground, detached in [('claude', 'sol61-medium', 'astra-high', 'astra-xhigh'),
                                                     ('codex', 'opus55-medium', 'fable-high', 'fable-xhigh')]:
            for previous, attempt in [(ordinary, 3), (foreground, 3), (detached, 4)]:
                with self.subTest(host=host, previous=previous, attempt=attempt), self.assertRaisesRegex(
                        ValueError, 'attempt cap reached'):
                    self.retry(self.data, host, previous, attempt=attempt)
            self.assertTrue(self.retry(self.data, host, detached, attempt=3)['cross_lane_s'])
            with self.assertRaisesRegex(ValueError, 'attempt cap reached'):
                routes.resolve(self.data, host, 'implement', retry_from=detached, retry_reason='infra', attempt=3)

    def test_cross_lane_disabled_restores_detached_s_refusal(self):
        data = routes.merge_bindings(self.data, {'selection_policy': {'cross_lane_s': False}})
        for host, foreground, detached in [('claude', 'astra-high', 'astra-xhigh'),
                                           ('codex', 'fable-high', 'fable-xhigh')]:
            self.assertEqual(self.retry(data, host, foreground)['worker_id'], detached)
            with self.assertRaisesRegex(ValueError, 'S row already failed at its detached effort'):
                self.retry(data, host, detached, attempt=2)

    def test_s_with_no_further_effort_crosses_without_repeating_itself(self):
        for host, foreground, detached, expected in [('claude', 'astra-high', 'astra-xhigh', 'fable-high'),
                                                     ('codex', 'fable-high', 'fable-xhigh', 'astra-high')]:
            data = dict(self.data, workers=[w for w in self.data['workers'] if w['id'] != detached])
            route = self.retry(data, host, foreground)
            self.assertEqual(route['worker_id'], expected)
            self.assertTrue(route['cross_lane_s'])
            # A lane can also configure the same effort for foreground and detached.
            data = routes.merge_bindings(self.data, {'bands': {'S': {'detached_effort': 'high'}}})
            self.assertEqual(self.retry(data, host, foreground)['worker_id'], expected)

    def test_cross_lane_keeps_host_requirements_exhaustion_and_tight_budget(self):
        for host, previous, cross, vendor in [('claude', 'astra-xhigh', 'fable-high', 'claude'),
                                               ('codex', 'fable-xhigh', 'astra-high', 'openai')]:
            for budget in ('tight', 'exhausted:' + vendor):
                route = self.retry(self.data, host, previous, attempt=3, budget=budget)
                self.assertEqual(route['worker_id'], cross)
                self.assertTrue(route['cross_lane_s'])
                self.assertFalse(route['available'])
            data = routes.merge_bindings(self.data, {'workers_local': [{'id': cross, 'requires': {'binary': 'missing-test-cli'}}]})
            with patch.object(routes.shutil, 'which', return_value=None), self.assertRaisesRegex(IndexError, 'no eligible worker'):
                self.retry(data, host, previous, attempt=3)


if __name__ == '__main__':
    unittest.main()
