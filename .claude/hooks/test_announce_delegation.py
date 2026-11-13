#!/usr/bin/env python
"""Regression tests for announce_delegation.py, run as the host runs it (subprocess)."""
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, 'announce_delegation.py')
SCRIPTS = os.path.join(HERE, '..', 'scripts')
ANNOUNCE = ('band B (assessed difficulty 1, exposure 1) -> gpt-6.1-sol/medium via codex-run.sh: '
            'cheapest of 2 eligible in foreground; vendor differs from author (claude)')
MISSING = 'HARNESS DELEGATION: announce line not passed'
CODEX = 'bash .claude/scripts/codex-run.sh'


def encode(payload):
    if isinstance(payload, bytes):
        return payload
    return (payload if isinstance(payload, str) else json.dumps(payload)).encode('utf-8')


def call(command, tool='Bash', **extra):
    return dict(tool_name=tool, tool_input={'command': command}, **extra)


class AnnounceDelegation(unittest.TestCase):
    def notice(self, payload):
        result = subprocess.run([sys.executable, HOOK], input=encode(payload), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        output = result.stdout.decode('utf-8').strip()
        if not output:
            return None
        self.assertEqual(len(output.splitlines()), 1)
        data = json.loads(output)
        self.assertEqual(set(data), {'systemMessage'})
        return data['systemMessage']

    def tail(self, payload):
        text = self.notice(payload)
        self.assertIsNotNone(text, payload)
        return text.split(' | call: ', 1)[1]

    def test_launcher_calls_show_announce_line_and_literal_arguments(self):
        bash_cases = [
            (CODEX + ' -p x -m gpt-6.1-sol -e high', 'codex-run.sh -m gpt-6.1-sol -e high'),
            ('HARNESS_BAND=B HARNESS_ROLE=implement bash .claude/scripts/claude-run.sh -p x -m claude-opus-5-5',
             'claude-run.sh -m claude-opus-5-5 -e not given'),
            (CODEX + ' -p prompt.md', 'codex-run.sh -m not given -e not given'),
            ('bash "D:/a b/.claude/scripts/agy-run.sh" -p x', 'agy-run.sh -m not given -e not given'),
            (CODEX + ' -p x -m "$MODEL" -e "$EFFORT"', 'codex-run.sh -m $MODEL -e $EFFORT'),
            ('bash .claude/scripts/local-run.sh -i inputs.json -p q', 'local-run.sh'),
            ('cd /repo && ' + CODEX + ' -p x -m m1 -e low 2>&1 | tail -5', 'codex-run.sh -m m1 -e low'),
            (CODEX + ' -p x > out.log -m m2', 'codex-run.sh -m m2 -e not given'),
        ]
        powershell_cases = [
            ("& 'C:\\Program Files\\Git\\bin\\bash.exe' .claude/scripts/codex-run.sh -p x -e medium",
             'codex-run.sh -m not given -e medium'),
            ('& bash .claude\\scripts\\codex-run.sh -p x -m m1 -e low 2>&1', 'codex-run.sh -m m1 -e low'),
            ('bash "D:/a b/.claude/scripts/agy-run.sh" -p x', 'agy-run.sh -m not given -e not given'),
        ]
        for tool, cases in (('Bash', bash_cases), ('PowerShell', powershell_cases)):
            for command, tail in cases:
                with self.subTest(tool=tool, command=command):
                    self.assertEqual(self.notice(call(command, tool)), MISSING + ' | call: ' + tail)
        # The announce line's ->, (, /, ; and : survive unchanged in every form.
        bash_forms = [(f"HARNESS_ANNOUNCE='{ANNOUNCE}' ", True), (f'HARNESS_ANNOUNCE="{ANNOUNCE}" ', True),
                      (f"export HARNESS_ANNOUNCE='{ANNOUNCE}'; ", False),
                      (f"export HARNESS_ANNOUNCE='{ANNOUNCE}'\n", False)]
        for prefix, prefix_only in bash_forms:
            for command, tail in bash_cases:
                with self.subTest(prefix=prefix[:30], command=command):
                    # An assignment prefix reaches only its own simple command (here `cd`).
                    expected = MISSING if prefix_only and command.startswith('cd ') else 'HARNESS DELEGATION: ' + ANNOUNCE
                    self.assertEqual(self.notice(call(prefix + command)), expected + ' | call: ' + tail)
        powershell_forms = [f"$env:HARNESS_ANNOUNCE='{ANNOUNCE}'; ", f"$env:HARNESS_ANNOUNCE = '{ANNOUNCE}'; ",
                            f'$env:HARNESS_ANNOUNCE= "{ANNOUNCE}"; ', f"$env:HARNESS_ANNOUNCE ='{ANNOUNCE}'\n"]
        for prefix in powershell_forms:
            for command, tail in powershell_cases:
                with self.subTest(prefix=prefix[:30], command=command):
                    self.assertEqual(self.notice(call(prefix + command, 'PowerShell')),
                                     'HARNESS DELEGATION: ' + ANNOUNCE + ' | call: ' + tail)

    def test_only_the_first_launcher_call_is_announced(self):
        command = CODEX + ' -p a -m first; bash .claude/scripts/claude-run.sh -p b -m second'
        self.assertEqual(self.tail(call(command)), 'codex-run.sh -m first -e not given')

    def test_f2_1_heredoc_bodies_and_non_executing_shells_are_silent(self):
        silent = [
            "cat > prompt.md <<'EOF'\n" + CODEX + " -p x -m from-body\nEOF",
            'cat <<-EOF > prompt.md\n\t' + CODEX + ' -p x\n\tEOF\n',
            'cat <<EOF\n' + CODEX + ' -p x\n',  # unterminated: the rest is body
            'bash -n .claude/scripts/codex-run.sh',
            'sh -n .claude/scripts/codex-run.sh -p x',
            'bash -xn .claude/scripts/codex-run.sh',
            "bash -c 'true' .claude/scripts/codex-run.sh -p x",
            'bash -o pipefail .claude/scripts/codex-run.sh -p x',
        ]
        for command in silent:
            with self.subTest(command=command):
                self.assertIsNone(self.notice(call(command)))
        after_body = "cat > p.md <<'EOF'\n" + CODEX + ' -m from-body\nEOF\n' + CODEX + ' -p p.md -m after'
        self.assertEqual(self.tail(call(after_body)), 'codex-run.sh -m after -e not given')
        self.assertEqual(self.tail(call('bash -x .claude/scripts/codex-run.sh -p x -m traced')),
                         'codex-run.sh -m traced -e not given')

    def test_f2_2_options_follow_the_launchers_getopts(self):
        cases = [
            (CODEX + ' -p x -m old -e low -m actual -e high', 'codex-run.sh -m actual -e high'),
            (CODEX + ' -p -mfoo', 'codex-run.sh -m not given -e not given'),
            (CODEX + ' -p --help -m real', 'codex-run.sh -m real -e not given'),
            (CODEX + ' -p x -mglued -ehigh', 'codex-run.sh -m glued -e high'),
            (CODEX + ' -p x -bm model-b', 'codex-run.sh -m model-b -e not given'),
            (CODEX + ' -p x -m -e', 'codex-run.sh -m -e -e not given'),
            (CODEX + ' -p x extra -m ignored', 'codex-run.sh -m not given -e not given'),
            (CODEX + ' -p x -- -m ignored', 'codex-run.sh -m not given -e not given'),
            (CODEX + ' -p x --unknown -m m', 'codex-run.sh -m unparsed -e unparsed'),
            (CODEX + ' -p x -Z -m m', 'codex-run.sh -m unparsed -e unparsed'),
            (CODEX + ' -p x -m', 'codex-run.sh -m unparsed -e unparsed'),
            ('bash .claude/scripts/claude-run.sh -p x -a plan -m m', 'claude-run.sh -m m -e not given'),
            ('bash .claude/scripts/agy-run.sh -p x -x 3 -m m -e low', 'agy-run.sh -m m -e low'),
            ('bash .claude/scripts/agy-run.sh -p x -a plan -m m', 'agy-run.sh -m unparsed -e unparsed'),
        ]
        for command, tail in cases:
            with self.subTest(command=command):
                self.assertEqual(self.tail(call(command)), tail)
        silent = [CODEX + ' --wait 20261113T000000Z-1 -t 120', 'bash .claude/scripts/claude-run.sh --status R1',
                  'bash .claude/scripts/agy-run.sh --forget R1', 'bash .claude/scripts/agy-run.sh -h',
                  CODEX + ' --help', CODEX + ' -p x -h', CODEX + ' -p x --status R1',
                  'bash .claude/scripts/local-run.sh --help',
                  'bash .claude/scripts/local-run.sh -i inputs.json -p prompt.txt --help',
                  'bash .claude/scripts/local-run.sh -i inputs.json -h']
        for command in silent:
            with self.subTest(command=command):
                self.assertIsNone(self.notice(call(command)))

    def test_f2_2_option_table_matches_the_launchers(self):
        sys.path.insert(0, HERE)
        try:
            import announce_delegation as hook
        finally:
            sys.path.remove(HERE)
        for launcher, spec in hook.GETOPTS.items():
            with self.subTest(launcher=launcher):
                with open(os.path.join(SCRIPTS, launcher), encoding='utf-8') as source:
                    found = re.findall(r'^while getopts "([^"]+)" opt; do$', source.read(), re.M)
                self.assertEqual(found, [spec], f'update GETOPTS in announce_delegation.py for {launcher}')

    def test_f2_3_announce_only_where_it_reaches_the_launcher(self):
        cases = [
            ('HARNESS_ANNOUNCE=wrong true; ' + CODEX + ' -p x', MISSING),
            ("printf 'HARNESS_ANNOUNCE=wrong'; " + CODEX + ' -p x', MISSING),
            ('echo HARNESS_ANNOUNCE=wrong && ' + CODEX + ' -p x', MISSING),
            ('HARNESS_ANNOUNCE=unexported; ' + CODEX + ' -p x', MISSING),
            (CODEX + ' -p x HARNESS_ANNOUNCE=arg', MISSING),
            ('export HARNESS_ANNOUNCE=old; HARNESS_ANNOUNCE=new ' + CODEX + ' -p x', 'HARNESS DELEGATION: new'),
            ('export HARNESS_ANNOUNCE=first; export HARNESS_ANNOUNCE=second; ' + CODEX + ' -p x',
             'HARNESS DELEGATION: second'),
            (CODEX + ' -p x; export HARNESS_ANNOUNCE=late', MISSING),
            ('env HARNESS_ANNOUNCE=via-env ' + CODEX + ' -p x', 'HARNESS DELEGATION: via-env'),
        ]
        for command, head in cases:
            with self.subTest(command=command):
                self.assertEqual(self.notice(call(command)).split(' | call: ')[0], head)
        powershell = [
            ("Write-Output '$env:HARNESS_ANNOUNCE = wrong'; & bash .claude/scripts/codex-run.sh -p x", MISSING),
            ("$env:HARNESS_ANNOUNCE_X = 'other'; & bash .claude/scripts/codex-run.sh -p x", MISSING),
        ]
        for command, head in powershell:
            with self.subTest(command=command):
                self.assertEqual(self.notice(call(command, 'PowerShell')).split(' | call: ')[0], head)
        self.assertIsNone(self.notice(call("'HARNESS_ANNOUNCE=x' " + CODEX + ' -p x')))

    def test_f2_4_each_shell_keeps_its_own_quoting(self):
        cases = [
            ("$env:HARNESS_ANNOUNCE = 'author''s reason'; & bash .claude/scripts/codex-run.sh -p x",
             'PowerShell', "author's reason"),
            ('$env:HARNESS_ANNOUNCE = "say `"hi`" and ""bye"""; & bash .claude/scripts/codex-run.sh -p x',
             'PowerShell', 'say "hi" and "bye"'),
            ("$env:HARNESS_ANNOUNCE = 'C:\\path\\x'; & bash .claude/scripts/codex-run.sh -p x",
             'PowerShell', 'C:\\path\\x'),
            ("HARNESS_ANNOUNCE='author'\\''s reason' " + CODEX + ' -p x', 'Bash', "author's reason"),
            ('HARNESS_ANNOUNCE="say \\"hi\\" \\x" ' + CODEX + ' -p x', 'Bash', 'say "hi" \\x'),
            ("HARNESS_ANNOUNCE='a''b' " + CODEX + ' -p x', 'Bash', 'ab'),
        ]
        for command, tool, announce in cases:
            with self.subTest(tool=tool, command=command):
                self.assertEqual(self.notice(call(command, tool)).split(' | call: ')[0],
                                 'HARNESS DELEGATION: ' + announce)
        # POSIX backslash outside quotes is an escape, so this path is not the launcher.
        self.assertIsNone(self.notice(call('bash .claude\\scripts\\codex-run.sh -p x')))
        powershell_here_string = "$p = @'\n" + CODEX + " -p x\n'@\nSet-Content p.md $p"
        self.assertIsNone(self.notice(call(powershell_here_string, 'PowerShell')))

    def test_f2_5_closed_stdout_still_exits_zero(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            result = subprocess.run([sys.executable, HOOK], input=encode(call(CODEX + ' -p x -m m')),
                                    stdout=write_end, stderr=subprocess.PIPE, timeout=20)
        finally:
            os.close(write_end)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_silent_cases(self):
        silent = [
            call('git status'),
            call('cat .claude/scripts/codex-run.sh'),
            call('grep -n wait .claude/scripts/claude-run.sh'),
            call('bash codex-run.sh -p x'),
            call(CODEX + ' -p x', agent_id='worker-1'),
            dict(tool_name='Edit', tool_input={'file_path': '.claude/scripts/codex-run.sh'}),
            dict(tool_name='Bash', tool_input={}),
            dict(tool_name='Bash', tool_input={'command': ['bash', '.claude/scripts/codex-run.sh']}),
            '{not json',
            '',
            '[]',
            b'\xff\xfe\x00',
        ]
        for payload in silent:
            with self.subTest(payload=payload):
                self.assertIsNone(self.notice(payload))


if __name__ == '__main__':
    unittest.main()
