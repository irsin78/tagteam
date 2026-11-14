#!/usr/bin/env python
"""Verification prefix grammar and config CLI regressions; no commands run."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from worker_verify import allow_rules, main


class WorkerVerify(unittest.TestCase):
    def test_accepted_commands_produce_exact_rules(self):
        entries = ['cargo test', '.venv/Scripts/python.exe -m pytest', 'bash scripts/check.sh',
                   'pwsh -File tools/check.ps1', 'python tools/check.py', 'bun test',
                   '/usr/bin/python3.12 tools/check.py', 'PYTHON3.EXE -m unittest',
                   'node tools/check.js', 'deno test', 'powershell -File check.ps1']
        self.assertEqual(allow_rules(entries), [f'Bash({entry}:*)' for entry in entries])

    def test_refused_commands_name_entry_and_rule(self):
        cases = [(5, 1), ('cargo', 1), ('cargo  test', 1), (' cargo test', 1),
                 ('cargo test ', 1), ('cargo\ttest', 1), ('cargo\ntest', 1),
                 ('cargo test; rm', 2), ('python *', 2), ('cargo "test"', 2),
                 ('env cargo test', 4), ('npx jest', 4),
                 ('/usr/bin/env cargo test', 4), ('C:\\Windows\\cmd.exe /c', 4),
                 ('bash -s', 5), ('Bash -s', 5), ('bash scripts/check', 5), ('pwsh -Command x', 5),
                 ('pwsh -File', 5), ('pwsh -File check', 5),
                 ('python --version', 6), ('python -m', 6), ('python -c x', 6), ('PYTHON.EXE -c x', 6),
                 ('python -', 6), ('/usr/bin/python3.12 -c x', 6),
                 ('C:\\Python312\\python.exe tool', 6), ('python tool', 6),
                 ('python -m 3bad', 6), ('node -e code', 6), ('deno run', 6), ('bun run', 6)]
        for entry, rule in cases:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError) as error:
                    allow_rules([entry])
                self.assertIn(repr(entry), str(error.exception))
                self.assertIn(f'rule {rule}:', str(error.exception))

    def test_all_wrapper_names_are_refused(self):
        for name in ('cmd', 'env', 'xargs', 'sudo', 'doas', 'nohup', 'timeout', 'time',
                     'nice', 'busybox', 'wsl', 'npx', 'pnpx', 'bunx', 'uvx', 'pipx'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'rule 4:'):
                allow_rules([name + ' cargo'])

    def test_verify_script_path_is_exact_or_omitted(self):
        self.assertEqual(allow_rules([], 'verify.sh'), ['Bash(bash verify.sh)'])
        self.assertEqual(allow_rules([], 'my verify.sh'), [])
        self.assertEqual(allow_rules([], 'verify.sh;other'), [])
        self.assertEqual(allow_rules([], r'C:\tools\verify.sh'), [r'Bash(bash C:\tools\verify.sh)'])

    def test_config_cli_empty_missing_valid_and_invalid(self):
        for config in ({}, {'worker_verify_commands': []}, {'worker_verify_commands': ['cargo test']}):
            output = io.StringIO()
            with patch.object(Path, 'read_text', return_value=json.dumps(config)), contextlib.redirect_stdout(output):
                self.assertEqual(main([]), 0)
            self.assertEqual(output.getvalue(), 'Bash(cargo test:*)\n' if config.get('worker_verify_commands') else '')
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--verify', 'verify.sh']), 0)
        self.assertEqual(output.getvalue(), 'Bash(bash verify.sh)\n')
        for content in ('{', '[]', '{"worker_verify_commands":"cargo test"}',
                        '{"worker_verify_commands":["cargo test","python"]}'):
            with self.subTest(content=content), patch.object(Path, 'read_text', return_value=content), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertNotEqual(main([]), 0)
            self.assertTrue(output.getvalue().startswith('ERR '))
            self.assertEqual(len(output.getvalue().splitlines()), 1)
        with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 1)
        self.assertEqual(output.getvalue(), 'ERR unreadable\n')


if __name__ == '__main__':
    unittest.main()
