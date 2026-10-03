#!/usr/bin/env python
"""Maintainer-only installer regressions; no model calls or real trust changes.

Execute the installers' actual trust sections with only their config location
redirected to a disposable fixture. Do not rerun the complete installer from
its own host tests. Windows and POSIX sections are checked when shells exist.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_host_routes as host_tests

ROOT = Path(__file__).resolve().parents[2]


class InstallChecks(unittest.TestCase):
    def test_split_guides_copy_targets_and_anchor_links(self):
        docs = ROOT / 'docs'
        if not (docs / 'harness-install.md').read_text(encoding='utf-8').lstrip().startswith('# Harness installation'):
            self.skipTest('translated or customized guides; anchors are checked in the shipped English docs')
        guides = ('harness-install.md', 'harness-launchers.md', 'harness-manual.md')
        copy_table = (docs / 'harness-install.md').read_text(encoding='utf-8')
        for name in guides:
            self.assertIn('`docs/' + name + '`', copy_table)
        # Check cross-document and local links after moving installation/recipes.
        sources = [ROOT / 'README.md', *docs.rglob('*.md')]
        for source in sources:
            text = source.read_text(encoding='utf-8')
            for target, fragment in re.findall(
                    r'\]\(([^\s)]*harness-(?:manual|install|launchers)\.md)(?:#([^\s)]+))?\)', text):
                path = source.parent / target
                with self.subTest(source=source, target=target, fragment=fragment):
                    self.assertTrue(path.is_file(), 'Missing linked harness guide')
                    if fragment:
                        headings = re.findall(r'^#{1,6} (.+)$', path.read_text(encoding='utf-8'), re.M)
                        anchors = {re.sub(r'[^\w\- ]', '', re.sub(r'[`*_]', '', title).lower()).replace(' ', '-')
                                   for title in headings}
                        self.assertIn(fragment, anchors, 'Moved heading has an unresolved inbound link')
            if source.name in guides:
                headings = re.findall(r'^#{1,6} (.+)$', text, re.M)
                anchors = {re.sub(r'[^\w\- ]', '', re.sub(r'[`*_]', '', title).lower()).replace(' ', '-')
                           for title in headings}
                for fragment in re.findall(r'\]\(#([^\s)]+)\)', text):
                    self.assertIn(fragment, anchors, f'Unresolved local link in {source}')

    @unittest.skipUnless((ROOT / 'AGENTS.md.template').exists(),
                         'template default only; a project policy may allow git push')
    def test_push_default_requires_approval_and_retains_force_push_asks(self):
        permissions = json.loads((ROOT / '.claude/settings.json').read_text(encoding='utf-8'))['permissions']
        self.assertNotIn('Bash(git push:*)', permissions['allow'])
        for rule in ('Bash(git push --force:*)', 'Bash(git push --force-with-lease:*)', 'Bash(git push -f:*)'):
            self.assertIn(rule, permissions['ask'])

    def test_platform_guidance_references_installed_split_guides(self):
        spec = importlib.util.spec_from_file_location(
            'installed_preflight', ROOT / '.claude/hooks/session_preflight.py')
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        for label, (name, extra) in preflight.PLATFORM_NOTES.items():
            with self.subTest(platform=label):
                self.assertTrue((ROOT / name).is_file(), 'Missing platform reading target')
                note = preflight.platform_note(label, ROOT)
                self.assertNotIn('file not found', note)
                if extra:
                    for linked in re.findall(r'docs/[\w-]+\.md', extra):
                        self.assertTrue((ROOT / linked).is_file(), 'Missing supplemental reading target')
        windows = preflight.PLATFORM_NOTES['Windows'][0]
        self.assertEqual(windows, 'docs/harness-install.md')
        text = (ROOT / windows).read_text(encoding='utf-8')
        self.assertRegex(text, r'(?s)### Windows[^\n]*\n.*?\n### macOS\n')

    def test_codex_registration_tracks_installed_events(self):
        runners = []
        pwsh = shutil.which('pwsh') or shutil.which('powershell')
        if pwsh:
            source = (ROOT / 'check-windows-aliases.ps1').read_text(encoding='utf-8')
            body = source[source.index('# Codex hook trust'):source.index('# Launcher liveness')]
            config = "$codexConfig = Join-Path $codexHome 'config.toml'"
            self.assertIn(config, body)
            body = body.replace(config, "$codexConfig = Join-Path $PSScriptRoot 'fixture-config.toml'")
            script = '$warningCount = 0\nfunction codex {}\n' + body + '\nexit ([int]($warningCount -gt 0))\n'
            runners.append(('Windows', 'probe.ps1', [pwsh, '-NoProfile', '-File'], script))
        bash = host_tests.routes.find_bash()
        if bash:
            source = (ROOT / 'check-posix.sh').read_text(encoding='utf-8')
            body = source[source.index('# ---- 6b. Codex hook trust'):source.index('# ---- 7. optional launcher')]
            config = 'CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"'
            self.assertIn(config, body)
            body = body.replace(config, 'CODEX_CONFIG="$SCRIPT_DIR/fixture-config.toml"')
            # Explicit native root avoids Git Bash's /tmp alias for Windows TEMP.
            prefix = ('#!/usr/bin/env bash\nset -u\nSCRIPT_DIR="$1"\n'
                      'codex() { :; }\nWARNINGS=0\n'
                      'warn() { WARNINGS=$((WARNINGS + 1)); echo "WARN: $1"; }\n'
                      'note() { echo "INFO: $1"; }\n')
            runners.append(('POSIX', 'probe.sh', [bash], prefix + body + '\n[ "$WARNINGS" -eq 0 ]\n'))
        if not runners:
            self.skipTest('PowerShell or Bash is required for installer section tests')
        installed = json.loads((ROOT / '.codex/hooks.json').read_text(encoding='utf-8'))
        expected = ['session_start', 'pre_tool_use', 'user_prompt_submit', 'stop']
        extended = copy.deepcopy(installed)
        extended['hooks']['PostToolUse'] = copy.deepcopy(installed['hooks']['Stop'])
        cases = [('all registered', installed, expected, 0, None),
                 ('new event not registered', extended, expected, 1, 'post_tool_use'),
                 ('new event registered', extended, expected + ['post_tool_use'], 0, None),
                 ('empty hooks', {'hooks': {}}, expected, 1, None),
                 ('invalid JSON', '{', expected, 1, None),
                 # Codex quotes keys without backslashes as TOML basic strings.
                 ('all registered, basic-string keys', installed, expected, 0, None, '"')]
        cases += [('missing ' + event, installed, [e for e in expected if e != event], 1, event)
                  for event in expected]
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        for shell, name, command, script in runners:
            with tempfile.TemporaryDirectory(prefix='harness install-') as temp:
                folder = Path(temp).resolve()
                # Windows uses the actual executable directory without symlink privileges.
                shim = Path(sys.executable).parent
                if os.name != 'nt':
                    shim = folder / 'bin'
                    shim.mkdir()
                    (shim / "python").symlink_to(sys.executable)
                case_env = dict(env, PATH=shim.as_posix() + os.pathsep + env.get('PATH', ''))
                hooks = folder / '.codex/hooks.json'
                hooks.parent.mkdir()
                executable = folder / name
                executable.write_text(script, encoding='utf-8', newline='\n')
                for label, definition, registrations, exit_code, missing, *quote in cases:
                    quote = quote[0] if quote else "'"
                    with self.subTest(shell=shell, case=label):
                        hooks.write_text(definition if isinstance(definition, str) else json.dumps(definition), encoding='utf-8')
                        config = ''.join("[hooks.state.%s%s:%s:0:0%s]\ntrusted_hash = \"fixture-only\"\n"
                                         % (quote, hooks.as_posix(), event, quote) for event in registrations)
                        (folder / 'fixture-config.toml').write_text(config, encoding='utf-8')
                        args = command + [executable.as_posix()]
                        if shell == 'POSIX':
                            args.append(folder.as_posix())
                        result = subprocess.run(args, cwd=folder, env=case_env, capture_output=True,
                                                text=True, encoding='utf-8', timeout=20)
                        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                        if exit_code:
                            self.assertIn('WARN:', result.stdout)
                            self.assertNotIn('OK:', result.stdout)
                            if missing:
                                self.assertIn(missing, result.stdout)
                        else:
                            self.assertIn('registration entries found', result.stdout)
                            self.assertIn('presence only', result.stdout)

    def test_claude_connection_missing_and_equivalent_forms(self):
        installed = json.loads((ROOT / '.claude/settings.json').read_text(encoding='utf-8'))
        entry = installed['hooks']['UserPromptSubmit'][0]['hooks'][0]
        combined = {'type': 'command', 'command': '"C:/Python 3/python.exe" '
                    '"C:/a project/.claude/hooks/stop_gate.py" --new-prompt'}
        missing_flag = copy.deepcopy(entry)
        missing_flag['args'] = [arg for arg in missing_flag['args'] if arg != '--new-prompt']
        wrong_script = {'type': 'command', 'command': 'python stop_gate.py.bak --new-prompt'}
        cases = [('installed', entry, True), ('combined command', combined, True),
                 ('missing event', None, False), ('missing flag', missing_flag, False),
                 ('wrong script', wrong_script, False)]
        with tempfile.TemporaryDirectory() as temp, patch.object(host_tests, 'ROOT', Path(temp)):
            settings = Path(temp) / '.claude/settings.json'
            settings.parent.mkdir()
            for label, handler, passes in cases:
                with self.subTest(case=label):
                    fixture = copy.deepcopy(installed)
                    # Consumer customizations and unrelated handlers must survive.
                    fixture['permissions']['allow'].append('Read(project-notes/**)')
                    fixture['hooks']['UserPromptSubmit'] = [{'hooks': [
                        {'type': 'command', 'command': 'echo consumer hook'},
                        *([handler] if handler else [])]}]
                    if handler is None:
                        del fixture['hooks']['UserPromptSubmit']
                    settings.write_text(json.dumps(fixture), encoding='utf-8')
                    check = host_tests.HostRoutes('test_claude_mission_prompt_hook_is_wired')
                    if passes:
                        check.test_claude_mission_prompt_hook_is_wired()
                    else:
                        with self.assertRaisesRegex(AssertionError, 'Missing Claude UserPromptSubmit'):
                            check.test_claude_mission_prompt_hook_is_wired()


if __name__ == '__main__':
    unittest.main()
