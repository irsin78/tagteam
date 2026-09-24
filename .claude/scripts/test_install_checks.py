#!/usr/bin/env python
"""Maintainer-only installer regressions; no model calls or real trust changes.

Execute the installers' actual trust sections with only their config location
redirected to a disposable fixture. Do not rerun the complete installer from
its own host tests. Windows and POSIX sections are checked when shells exist.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_host_routes as host_tests

ROOT = Path(__file__).resolve().parents[2]


class InstallChecks(unittest.TestCase):
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
