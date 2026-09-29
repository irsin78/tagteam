#!/usr/bin/env python
"""Host routing and explicit lifecycle regression tests, no model calls."""
import importlib.util
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
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('harness_route', HERE / 'harness-route.py')
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)
session_spec = importlib.util.spec_from_file_location('harness_session', HERE / 'harness-session.py')
session = importlib.util.module_from_spec(session_spec)
session_spec.loader.exec_module(session)


class HostRoutes(unittest.TestCase):
    def setUp(self):
        # Public bindings only: a developer's model-bindings.local.json (declared local
        # endpoint, budget) must not change these expectations.
        public = json.loads((ROOT / '.claude/model-bindings.json').read_text(encoding='utf-8'))
        self.data = routes.merge_bindings(public, {})

    def cell(self, tier, vendor):
        return next(w for w in self.data['workers'] if w.get('tier_cell') and w['tier'] == tier and w['vendor'] == vendor)

    def override(self, ident, **fields):
        return routes.merge_bindings(self.data, {'workers_local': [dict(id=ident, **fields)]})

    def with_test_reader(self):
        return self.override('test-reader', vendor='google', model='test-model',
                             effort='medium', tier='C', status='optional',
                             launcher='.claude/scripts/agy-run.sh',
                             roles={'explore': {'priority': 1, 'requires': {
                                 'agy_grant': 'write_file',
                                 'agent_file': '.agents/agents/project-agent.md'}}})

    def test_tier_prefers_nonexhausted_vendor(self):
        for role, tier, exhausted, fallback, normal in (
                ('review_deep', 'A', 'claude', 'astra-high', 'opus55-xhigh'),
                ('implement', 'B', 'openai', 'opus55-high', 'astra-medium')):
            for budget, expected in [('exhausted:' + exhausted, fallback), ('normal', normal)]:
                with self.subTest(role=role, budget=budget):
                    result = routes.resolve(self.data, 'claude', role, tier=tier,
                                            author_vendors=['google'], budget=budget)
                    self.assertEqual(result['worker_id'], expected)
                    self.assertTrue(result['available'])

    def test_worker_cannot_combine_with_tier(self):
        with self.assertRaisesRegex(ValueError, '--worker cannot be combined with --tier'):
            routes.resolve(self.data, 'claude', 'implement', worker='luna56-max', tier='B')

    def test_optional_worker_needs_requires(self):
        with self.assertRaisesRegex(ValueError, 'optional worker needs requires'):
            self.override('sol-high', status='optional', requires={})

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

    def test_v1_baseline_all_92_cases(self):
        fixture = HERE / 'test_fixtures/route-baseline-v1.json'
        if not fixture.exists():
            self.skipTest('Development baseline fixture is not installed')
        cases = json.loads(fixture.read_text(encoding='utf-8'))
        self.assertEqual(len(cases), 92)
        exceptions = {'claude/write/budget=exhausted:openai',
                      'codex/write/budget=exhausted:claude', 'codex/implement/step2'}
        seen = set()
        with tempfile.TemporaryDirectory() as temp:
            for case in cases:
                with self.subTest(case=case['case']):
                    host, role, *options = case['case'].split('/')
                    kwargs = dict(root=temp, env={'HARNESS_AGY_SETTINGS': str(Path(temp) / 'absent.json')})
                    if role in routes.REVIEW_ROLES:
                        kwargs['author_vendors'] = [{'claude': 'claude', 'codex': 'openai'}[host]]
                    for opt in options:
                        if opt.startswith('step'): kwargs['step'] = int(opt[4:])
                        elif opt.startswith('tier'): kwargs['tier'] = opt[4:]
                        elif opt.startswith('budget='): kwargs['budget'] = opt[7:]
                        elif opt.startswith('author='): kwargs['author_vendors'] = opt[7:].split(',')
                    if case['case'] in exceptions:
                        seen.add(case['case'])
                    if case['case'] == 'codex/implement/step2':
                        self.assertIn('error', case)
                        with self.assertRaises((ValueError, IndexError)):
                            routes.resolve(self.data, host, role, **kwargs)
                        continue
                    result = routes.resolve(self.data, host, role, **kwargs)
                    expected = dict(case)
                    if case['case'] in exceptions:
                        expected.update(vendor='google', launcher='.claude/scripts/agy-run.sh',
                                        model='gemini-3.8-flash', effort='medium',
                                        sandbox='workspace-write', available=True, separation_satisfied=True)
                        self.assertEqual(result['worker_id'], 'flash-medium')
                    for field in ('vendor', 'launcher', 'sandbox', 'model', 'effort', 'available', 'separation_satisfied'):
                        self.assertEqual(result.get(field), expected.get(field), field)
        self.assertEqual(seen, exceptions)

    def test_requires_and_reader_tiers(self):
        with tempfile.TemporaryDirectory() as temp:
            reader_data = self.with_test_reader()
            root = Path(temp)
            settings = root / 'agy.json'
            agent = root / '.agents/agents/project-agent.md'
            env = {'HARNESS_AGY_SETTINGS': str(settings), 'PATH': ''}
            def explore(data=None, **kw):
                return routes.resolve(data or reader_data, 'codex', 'explore', root=root, env=env, **kw)
            public = explore(self.data, tier='C')
            self.assertEqual(public['worker_id'], 'haiku45')
            self.assertTrue(public['tier_fallback'])
            missing = explore(tier='C')
            self.assertEqual(missing['worker_id'], 'haiku45')
            self.assertTrue(missing['tier_fallback'])
            self.assertTrue(any('agy_grant' in x['reason'] for x in missing['skipped']))
            reader = next(w for w in reader_data['workers'] if w['id'] == 'test-reader')
            grant = reader['roles']['explore']['requires']['agy_grant']
            settings.write_text(json.dumps({'permissions': {'allow': [grant + '(*)']}}))
            self.assertTrue(any('agent_file' in x['reason'] for x in explore(tier='C')['skipped']))
            agent.parent.mkdir(parents=True)
            agent.write_text('reader')
            self.assertEqual(explore(tier='C')['worker_id'], 'test-reader')
            self.assertFalse(explore(tier='C')['tier_fallback'])
            self.assertEqual(explore()['worker_id'], 'haiku45')
            fallback = explore(tier='C', budget='exhausted:google')
            self.assertEqual(fallback['worker_id'], 'haiku45')
            self.assertTrue(fallback['tier_fallback'])
            self.assertEqual(explore(budget='exhausted:claude')['worker_id'], 'test-reader')
            settings.write_text(json.dumps({'permissions': {'allow': ['write_file_other(*)']}}))
            self.assertEqual(explore(tier='C')['worker_id'], 'haiku45')
            with self.assertRaises(IndexError):
                routes.resolve(self.data, 'codex', 'explore', step=1, root=root, env=env)
            data = routes.merge_bindings(self.data, {'vendors': {'local': {'endpoint': {'base_url': 'http://localhost', 'model': 'auto'}}}})
            self.assertEqual(routes.resolve(data, 'codex', 'explore', step=1, root=root, env=env)['worker_id'], 'local-reader')
            data = self.override('haiku45', requires={'binary': 'test-reader'})
            with patch.object(routes.shutil, 'which', return_value=None):
                with self.assertRaises(IndexError): explore(data)
            with patch.object(routes.shutil, 'which', return_value='/bin/test-reader'):
                self.assertEqual(explore(data)['worker_id'], 'haiku45')

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
            base = self.cell('D', 'claude')
            added = dict(base, id='other-reader', tier_cell=False, roles={'explore': 3})
            override = {'workers': [base], 'workers_local': [added]}
            (folder / 'model-bindings.local.json').write_text(json.dumps(override))
            self.assertEqual([w['id'] for w in routes.load_bindings(temp)['workers']], ['haiku45', 'other-reader'])

    def test_explicit_worker_and_host_priorities(self):
        with self.assertRaisesRegex(ValueError, 'unknown worker'):
            routes.resolve(self.data, 'claude', 'implement', worker='absent')
        implicit = routes.resolve(self.data, 'claude', 'implement')
        self.assertIn({'id': 'luna56-max', 'reason': 'status conditional requires explicit --worker'}, implicit['skipped'])
        self.assertEqual(routes.resolve(self.data, 'claude', 'implement', worker='luna56-max')['worker_id'], 'luna56-max')
        data = routes.merge_bindings(self.data, {})
        data['workers'] = [dict(w, roles={'implement': {'claude': 1}}) if w['id'] == 'sol-high' else w for w in data['workers']]
        self.assertEqual(routes.resolve(data, 'claude', 'implement')['worker_id'], 'sol-high')
        result = routes.resolve(data, 'codex', 'implement', author_vendors=['claude'])
        self.assertEqual(result['worker_id'], 'astra-medium')
        self.assertIn({'id': 'sol-high', 'reason': 'host is not eligible'}, result['skipped'])
        data['workers'] = [dict(w, roles={'implement': {'priority': 1, 'hosts': ['codex']}}) if w['id'] == 'sol-high' else w for w in data['workers']]
        self.assertEqual(routes.resolve(data, 'claude', 'implement')['worker_id'], 'astra-medium')

    def test_multiple_exhausted_vendors_and_diagnostic(self):
        budget = 'exhausted:openai,claude'
        self.assertEqual(routes.budget_for(self.data, {'HARNESS_BUDGET': budget}), budget)
        result = routes.resolve(self.data, 'claude', 'write', budget=budget)
        self.assertEqual(result['worker_id'], 'flash-medium')
        self.assertTrue(result['available'])
        result = routes.resolve(self.data, 'claude', 'write', budget=budget + ',google')
        self.assertEqual(result['worker_id'], 'astra-low')
        self.assertFalse(result['available'])
        for host, ident in [('claude', 'opus55-high'), ('codex', 'astra-medium')]:
            result = routes.resolve(self.data, host, 'review_deep', step=99,
                                    author_vendors=['openai', 'claude'], budget=budget)
            self.assertEqual(result['worker_id'], ident)
            self.assertFalse(result['separation_satisfied'])
            self.assertFalse(result['available'])
            self.assertIn('no configured route is independent', result['reason'])

    def test_schema_rejects_invalid_workers(self):
        for invalid in (None, [], {'schema_version': 2, 'workers': {}},
                        {'schema_version': 2, 'workers': [None]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                routes.merge_bindings(invalid, {})
        for fields in ({'vendor': 'unknown'}, {'tier': 'E'}, {'status': 'unknown'},
                       {'requires': {'network': True}}, {'roles': {'unknown': 1}},
                       {'roles': {'implement': 0}}, {'roles': {'implement': {'other': 1}}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.override('sol-high', **fields)
        for worker in (dict(self.cell('B', 'openai'), id='duplicate-cell'),
                       dict(self.cell('B', 'openai'), id='duplicate-priority', tier_cell=False),
                       dict(self.cell('B', 'openai'))):
            with self.assertRaises(ValueError):
                routes.merge_bindings(self.data, {'workers': self.data['workers'] + [worker]})

    def test_launcher_default_cli_works_for_delegates(self):
        env = dict(os.environ, HARNESS_DELEGATE_RUN='1')
        command = [sys.executable, str(HERE / 'harness-route.py'), '--launcher-default']
        for vendor, role, expected in [('openai', 'implement', 'gpt-5.6-sol high'),
                                       ('openai', 'image_verify', 'gpt-5.6-terra medium'),
                                       ('claude', 'implement', 'claude-opus-5-5 high')]:
            result = subprocess.run(command + ['--vendor', vendor, '--role', role], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            source, _, rest = result.stdout.strip().partition(' ')
            self.assertIn(source, ('public', 'public+local'))
            self.assertEqual(rest, expected)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / '.claude'
            folder.mkdir()
            data = self.override('opus55-high', effort=None)
            (folder / 'model-bindings.json').write_text(json.dumps(data), encoding='utf-8')
            result = subprocess.run(command + ['--vendor', 'claude', '--role', 'implement'], cwd=temp, env=env, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), 'public claude-opus-5-5 unspecified')
            (folder / 'model-bindings.json').write_text('{')
            result = subprocess.run(command + ['--vendor', 'claude', '--role', 'implement'], cwd=temp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stdout.startswith('ERR JSONDecodeError'))

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
                self.assertEqual(route['model'], self.cell('D', 'claude')['model'])
                self.assertEqual(route['sandbox'], 'read-only')
                self.assertEqual(route['worker_id'], 'haiku45')
                self.assertFalse(route['tier_fallback'])

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

    def test_claude_promotion_uses_claude_binding(self):
        promoted = routes.resolve(self.data, 'codex', 'implement', 1)
        self.assertEqual(promoted['model'], self.cell('A', 'claude')['model'])
        with self.assertRaises(IndexError):
            routes.resolve(self.data, 'codex', 'implement', 2)

    def test_openai_ultra_override_is_a_policy_error_on_both_hosts(self):
        data = self.override('astra-medium', effort='ultra')
        for host in ('codex', 'claude'):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, 're-delegation'):
                routes.resolve(data, host, 'review_deep', author_vendors=['claude'])
        data = self.override('sol-high', model='gpt-6-astra', effort='ultra')
        with self.assertRaisesRegex(ValueError, 're-delegation'):
            routes.resolve(data, 'claude', 'implement')

    def test_openai_max_override_preserves_single_agent_route(self):
        data = self.override('astra-medium', effort='max')
        result = routes.resolve(data, 'codex', 'review_deep', author_vendors=['claude'])
        self.assertTrue(result['available'])
        self.assertEqual(result['effort'], 'max')

    def test_task_tier_selects_efficient_binding_on_both_hosts(self):
        for host, vendor in [('codex', 'claude'), ('claude', 'openai')]:
            result = routes.resolve(self.data, host, 'implement', tier='C')
            self.assertEqual(result['model'], self.cell('C', vendor)['model'])
            self.assertEqual(result['tier'], 'C')
            self.assertEqual(result['sandbox'], 'workspace-write')
            result = routes.resolve(self.data, host, 'write', tier='B')
            self.assertEqual(result['model'], self.cell('B', vendor)['model'])

    def test_tier_override_preserves_review_floor_and_exhaustion(self):
        for role in ('decide', 'plan_review', 'review_deep'):
            with self.assertRaises(ValueError):
                routes.resolve(self.data, 'codex', role, tier='C', author_vendors=['openai'])
        with self.assertRaises(ValueError):
            routes.resolve(self.data, 'codex', 'implement', step=1, tier='C')
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
        data = self.override('opus55-high', model='test-claude')
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
        self.assertFalse(routes.resolve(self.data, 'codex', 'implement', 1, 'tight')['available'])
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
        self.assertEqual(result['model'], next(w for w in self.data['workers'] if w['id'] == 'sol-high')['model'])

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
        result = routes.resolve(data, 'claude', 'review_deep', author_vendors=['google'])
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
                route = routes.resolve(self.data, 'claude', 'write', budget='exhausted:openai', root=root, env={})
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


if __name__ == '__main__':
    unittest.main()
