#!/usr/bin/env python
"""Host routing and explicit lifecycle regression tests, no model calls."""
import importlib.util
import json
import os
from pathlib import Path
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


class HostRoutes(unittest.TestCase):
    def setUp(self):
        self.data = routes.load_bindings(ROOT)

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
            route = routes.resolve(self.data, host, 'web')
            self.assertEqual(route['vendor'], 'claude')
            self.assertTrue(route['launcher'].endswith('claude-run.sh'))
            self.assertEqual(route['claude_role'], 'web')
            self.assertEqual(route['model'], self.data['bindings']['D']['claude']['model'])
            self.assertEqual(route['sandbox'], 'read-only')

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
        self.assertEqual(promoted['model'], self.data['bindings']['A']['claude']['model'])
        with self.assertRaises(IndexError):
            routes.resolve(self.data, 'codex', 'implement', 2)

    def test_openai_ultra_override_is_a_policy_error_on_both_hosts(self):
        data = routes.merge(self.data, {'bindings': {'B': {'openai': {'effort': 'ultra'}}}})
        for host in ('codex', 'claude'):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, 're-delegation'):
                routes.resolve(data, host, 'review_deep', author_vendors=['claude'])
        data = routes.merge(self.data, {'roles': {'implement': {'ladder': [
            {'vendor': 'openai', 'model': 'gpt-6-astra', 'effort': 'ultra'}]}}})
        with self.assertRaisesRegex(ValueError, 're-delegation'):
            routes.resolve(data, 'claude', 'implement')

    def test_openai_max_override_preserves_single_agent_route(self):
        data = routes.merge(self.data, {'bindings': {'B': {'openai': {'effort': 'max'}}}})
        result = routes.resolve(data, 'codex', 'review_deep', author_vendors=['claude'])
        self.assertTrue(result['available'])
        self.assertEqual(result['effort'], 'max')

    def test_task_tier_selects_efficient_binding_on_both_hosts(self):
        for host, vendor in [('codex', 'claude'), ('claude', 'openai')]:
            result = routes.resolve(self.data, host, 'implement', tier='C')
            self.assertEqual(result['model'], self.data['bindings']['C'][vendor]['model'])
            self.assertEqual(result['tier'], 'C')
            self.assertEqual(result['sandbox'], 'workspace-write')
            result = routes.resolve(self.data, host, 'write', tier='B')
            self.assertEqual(result['model'], self.data['bindings']['B'][vendor]['model'])

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
        merged = routes.merge(self.data, example)
        self.assertEqual(merged['specialties'], self.data['specialties'])
        self.assertEqual(merged['roles'], self.data['roles'])
        self.assertIn('endpoint', merged['vendors']['local'])
        self.assertNotIn('local_endpoint', example)

    def test_local_override_changes_only_selected_vendor(self):
        data = routes.merge(self.data, {'bindings': {'B': {'claude': {'model': 'test-claude'}}}})
        self.assertEqual(routes.resolve(data, 'codex', 'implement')['model'], 'test-claude')
        self.assertEqual(routes.resolve(data, 'claude', 'implement'),
                         routes.resolve(self.data, 'claude', 'implement'))

    def test_no_independent_advisory_candidate_is_unavailable(self):
        self.data['host_routes']['codex']['decide'] = self.data['host_routes']['claude']['decide']
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
        self.assertEqual(result['model'], self.data['roles']['implement']['ladder'][0]['model'])

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
        data = routes.merge(self.data, {'bindings': {'B': {'claude': {'model': 'project-reviewer'}}}})
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

    def test_both_templates_resolve_delegate_before_onboarding(self):
        for name in ('AGENTS.md.template', 'CLAUDE.md.template'):
            installed = ROOT / name.removesuffix('.template')
            source = installed if installed.exists() else ROOT / name
            if not source.exists():
                self.skipTest('both host entry instructions are not installed')
            text = source.read_text(encoding='utf-8')
            self.assertIn('HARNESS_DELEGATE_RUN=1', text)
            workflow = text.index('## Orchestrator workflow')
            entry = text[:workflow]
            self.assertIn('Skip the Orchestrator workflow and all its linked reading/setup', entry)
            self.assertIn('applicable project/security/verification rules', entry)
            self.assertLess(text.index('HARNESS_DELEGATE_RUN=1'), text.index('session-role.md'))
            self.assertLess(workflow, text.index('Read `.claude/rules/session-role.md`'))
            self.assertLess(workflow, text.index('python .claude/scripts/harness-route.py'))
            self.assertLess(text.index('session-role.md'), text.index('norm:delegation-route'))
            self.assertIn('harness-session.py finish', text)
        source = ROOT / ('AGENTS.md' if (ROOT / 'AGENTS.md').exists() else 'AGENTS.md.template')
        self.assertNotIn('- NEVER run `git commit`', source.read_text(encoding='utf-8'))

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
