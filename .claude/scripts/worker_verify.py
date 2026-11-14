#!/usr/bin/env python
"""Validate Claude worker verification prefixes without executing commands."""
import argparse
import json
from pathlib import Path
import re


TOKEN = re.compile(r'[A-Za-z0-9._/\\:=+@,-]+')
VERIFY_PATH = re.compile(r'[A-Za-z0-9._/\\:-]+')
MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_.]*')
WRAPPERS = {'cmd', 'env', 'xargs', 'sudo', 'doas', 'nohup', 'timeout', 'time',
            'nice', 'busybox', 'wsl', 'npx', 'pnpx', 'bunx', 'uvx', 'pipx'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
INTERPRETERS = {'python', 'py', 'pypy', 'node', 'ruby', 'perl', 'php', 'lua', 'rscript'}


def allow_rules(entries, verify_path=None):
    """Return argv-safe Bash rules, or reject the entire list with its bad entry."""
    if not isinstance(entries, list):
        raise ValueError(f'{entries!r}: worker_verify_commands must be a list')
    rules = []
    for entry in entries:
        def reject(rule):
            raise ValueError(f'{entry!r}: {rule}')

        # Rule 1: a command and argument, separated by single ASCII spaces.
        if (not isinstance(entry, str) or len(entry.split(' ')) < 2
                or any(not token for token in entry.split(' '))
                or any(char.isspace() and char != ' ' for char in entry)):
            reject('rule 1: use at least two tokens separated by exactly one space')
        tokens = entry.split(' ')
        # Rule 2: neither shell syntax nor permission-rule syntax is accepted.
        if any(TOKEN.fullmatch(token) is None for token in tokens):
            reject('rule 2: tokens must match [A-Za-z0-9._/\\:=+@,-]+')
        # Rule 3: normalize executable paths and interpreter version suffixes.
        executable = re.split(r'[/\\]', tokens[0])[-1].lower()
        if executable.endswith('.exe'):
            executable = executable[:-4]
        executable = re.sub(r'[0-9.]+$', '', executable)
        # Rule 4: wrappers can turn a harmless-looking prefix into another command.
        if executable in WRAPPERS:
            reject('rule 4: wrappers and arbitrary runners are refused')
        argument = tokens[1]
        # Rule 5: shells must name a script, never inline code or stdin.
        if executable in SHELLS and (argument.startswith('-') or not argument.endswith('.sh')):
            reject('rule 5: shells require a .sh script path as token 2')
        if executable in {'pwsh', 'powershell'} and (argument != '-File' or len(tokens) < 3
                                                    or not tokens[2].endswith('.ps1')):
            reject('rule 5: PowerShell requires -File and a .ps1 script path')
        # Rule 6: code interpreters must name a script/module or the test subcommand.
        script = not argument.startswith('-') and re.search(r'\.[^.]+$', re.split(r'[/\\]', argument)[-1])
        module = (executable in {'python', 'py', 'pypy'} and argument == '-m'
                  and len(tokens) >= 3 and MODULE.fullmatch(tokens[2]) is not None)
        if executable in INTERPRETERS and not (script or module):
            reject('rule 6: interpreters require a script path with an extension or Python -m MODULE')
        if executable in {'deno', 'bun'} and argument != 'test':
            reject('rule 6: deno and bun require test as token 2')
        rules.append(f'Bash({entry}:*)')
    if isinstance(verify_path, str) and VERIFY_PATH.fullmatch(verify_path):
        rules.append(f'Bash(bash {verify_path})')
    return rules


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify')
    args = parser.parse_args(argv)
    try:
        path = Path('.claude/harness-config.json')
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            raise ValueError('harness-config.json must contain an object')
        rules = allow_rules(config.get('worker_verify_commands', []), args.verify)
    except (OSError, ValueError, RecursionError) as exc:
        print('ERR ' + ' '.join(str(exc).splitlines()))
        return 1
    for rule in rules:
        print(rule)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
