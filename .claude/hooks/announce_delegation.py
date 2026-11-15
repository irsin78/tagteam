#!/usr/bin/env python
"""Show the user which worker an orchestrator launcher call starts. Never blocks.

Claude PreToolUse hook for Bash/PowerShell. When the orchestrator (no
`agent_id`) runs one of the harness launchers under `.claude/scripts/`, print
one JSON line `{"systemMessage": ...}` that the host shows as a notice:

    HARNESS DELEGATION: <router announce line> | call: <launcher> -m <M> -e <E>

The announce line is the router's own text (`launch_env.HARNESS_ANNOUNCE`),
read only where it reaches that launcher call: an assignment prefix of the same
simple command, or an earlier `export HARNESS_ANNOUNCE=...` / PowerShell
`$env:HARNESS_ANNOUNCE = ...` statement in the same command string. `-m`/`-e`
are read the way the launcher's getopts reads them (last value wins); `not
given` when absent, `unparsed` when an unknown option makes positions uncertain.
Nothing is guessed from bindings or defaults.

This is a notice, not a guard and not a shell interpreter. When unsure it says
less (`not given`, `unparsed`, or nothing): read-back and help calls, `bash -n`,
`bash -c`, heredoc bodies and quoted strings print nothing. Only the first
launcher call of a command is announced. Exports inside subshells, functions or
conditionals are read as if they applied. Any error ends in exit 0, no output.
The sentence announcement in docs/orchestration/delegation-matrix.md still
applies; this line is its floor.
"""
import json
import os
import re
import sys

# Options that take an argument, copied from each launcher's getopts string
# (codex-run.sh, claude-run.sh, agy-run.sh `while getopts "..."` lines);
# test_announce_delegation.py fails when a launcher's string changes.
GETOPTS = {
    'codex-run.sh': ':p:m:e:s:v:l:r:i:o:t:bP',
    'claude-run.sh': ':p:m:e:a:s:v:l:t:bP',
    'agy-run.sh': ':p:e:x:l:t:bm:rc',
}
LAUNCHERS = tuple(GETOPTS) + ('local-run.sh',)
READBACK = frozenset(('--status', '--wait', '--forget'))  # run-state.sh launcher_readback
HELP = frozenset(('-h', '--help'))
SHELLS = frozenset(('bash', 'sh'))
SKIP_HEADS = frozenset(('then', 'do', 'else', 'exec', 'time', 'nohup', '!', '{', '('))
ANNOUNCE = 'HARNESS_ANNOUNCE'
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*$')
REDIRECT = re.compile(r'^\d*(?:[<>]{1,2}|&>{1,2}|[<>]&)(.*)$')
SEP = object()


class Word(str):
    """A token with the length of its unquoted leading part (for assignments)."""
    def __new__(cls, text, plain):
        word = super().__new__(cls, text)
        word.plain = plain
        return word


def tokenize(command, powershell):
    """Words and SEP markers. Quotes group and are removed.

    Bash: POSIX quoting (backslash escapes outside quotes; inside double quotes
    only before " \\ $ `), heredoc bodies skipped. PowerShell: '' inside single
    quotes is ', backtick escapes inside double quotes, backslash literal,
    here-strings skipped.
    """
    tokens, word, quoted, plain = [], [], False, None
    pending = []  # heredoc delimiters waiting for the next newline
    i, n = 0, len(command)

    def flush():
        nonlocal word, quoted, plain
        if word or quoted:
            tokens.append(Word(''.join(word), len(word) if plain is None else plain))
        word, quoted, plain = [], False, None

    def mark_quote():
        nonlocal plain, quoted
        if plain is None:
            plain = len(word)
        quoted = True

    while i < n:
        char = command[i]
        if powershell and char == '@' and not word and command[i + 1:i + 2] in ("'", '"') \
                and command[i + 2:i + 3] in ('\n', '\r'):
            close = '\n' + command[i + 1] + '@'
            end = command.find(close, i)
            i = n if end < 0 else end + len(close)
            mark_quote()
            continue
        if char == "'":
            mark_quote()
            i += 1
            while i < n:
                if command[i] == "'":
                    if powershell and command[i + 1:i + 2] == "'":
                        word.append("'")
                        i += 2
                        continue
                    break
                word.append(command[i])
                i += 1
            i += 1
            continue
        if char == '"':
            mark_quote()
            i += 1
            while i < n:
                if command[i] == '"':
                    if powershell and command[i + 1:i + 2] == '"':
                        # PowerShell "" inside a double-quoted string is a literal quote.
                        word.append('"')
                        i += 2
                        continue
                    break
                if powershell and command[i] == '`' and i + 1 < n:
                    i += 1
                elif not powershell and command[i] == '\\' and command[i + 1:i + 2] in ('"', '\\', '$', '`'):
                    i += 1
                word.append(command[i])
                i += 1
            i += 1
            continue
        if char == '\\' and not powershell:
            if command[i + 1:i + 2] == '\n':
                i += 2
                continue
            mark_quote()
            if i + 1 < n:
                word.append(command[i + 1])
            i += 2
            continue
        if char == '`' and powershell and i + 1 < n:
            mark_quote()
            word.append(command[i + 1])
            i += 2
            continue
        if not powershell and char == '<' and command[i:i + 2] == '<<' and command[i:i + 3] != '<<<':
            flush()
            i += 2
            strip_tabs = command[i:i + 1] == '-'
            i += 1 if strip_tabs else 0
            while i < n and command[i] in ' \t':
                i += 1
            delimiter = []
            while i < n and command[i] not in ' \t\n;&|<>':
                if command[i] in '\'"\\':
                    i += 1
                    continue
                delimiter.append(command[i])
                i += 1
            pending.append((''.join(delimiter), strip_tabs))
            continue
        if char == '&' and ((word and word[-1] in '<>') or command[i + 1:i + 2] == '>'):
            word.append(char)
            i += 1
            continue
        if char in ' \t\r':
            flush()
            i += 1
            continue
        if char in ';&|\n':
            flush()
            if command[i:i + 2] in ('&&', '||'):
                i += 2
            else:
                i += 1
            tokens.append(SEP)
            if char == '\n' and pending:
                i = skip_heredocs(command, i, pending)
                pending = []
            continue
        word.append(char)
        i += 1
    flush()
    return tokens


def skip_heredocs(command, i, pending):
    """Skip heredoc bodies starting at i; an unterminated one skips to the end."""
    for delimiter, strip_tabs in pending:
        while True:
            if i >= len(command):
                return len(command)
            end = command.find('\n', i)
            line = command[i:] if end < 0 else command[i:end]
            i = len(command) if end < 0 else end + 1
            if (line.lstrip('\t') if strip_tabs else line).rstrip('\r') == delimiter:
                break
    return i


def segments(tokens):
    current = []
    for token in tokens:
        if token is SEP:
            yield current
            current = []
        else:
            current.append(token)
    yield current


def assignment(word):
    """(name, value) when the word is an unquoted NAME=... assignment."""
    equals = word.find('=')
    if equals <= 0 or equals >= word.plain or not NAME.match(word[:equals]):
        return None
    return word[:equals], word[equals + 1:]


def powershell_env(words):
    """Value of `$env:HARNESS_ANNOUNCE = value` as a statement, else None."""
    if not words or not words[0].lower().startswith('$env:' + ANNOUNCE.lower()):
        return None
    rest = words[0][len('$env:' + ANNOUNCE):]
    tail = list(words[1:])
    if not rest:
        if not tail or not tail[0].startswith('='):
            return None
        rest, tail = tail[0], tail[1:]
    if not rest.startswith('='):
        return None
    value = rest[1:]
    if not value and tail:
        value = tail[0]
    return value or None


def base(token):
    return re.split(r'[\\/]', token)[-1]


def launcher_of(token):
    path = token.replace('\\', '/')
    name = base(path)
    if name in LAUNCHERS and '/.claude/scripts/' in '/' + path:
        return name
    return None


def without_redirects(words):
    args, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        match = REDIRECT.match(word) if word.plain >= len(word) else None
        if match:
            skip = not match.group(1)
            continue
        args.append(word)
    return args


def find_call(tokens, powershell):
    """(launcher, args, announce) of the first executed launcher, or None."""
    exported = None
    for words in segments(tokens):
        position = 0
        while position < len(words) and words[position] in SKIP_HEADS:
            position += 1
        words = words[position:]
        if not words:
            continue
        if powershell:
            value = powershell_env(words)
            if value is not None:
                exported = value
                continue
        elif words[0] == 'export':
            for word in words[1:]:
                pair = assignment(word)
                if pair and pair[0] == ANNOUNCE:
                    exported = pair[1]
            continue
        prefix, position = None, 0
        while position < len(words):
            if words[position] == 'env' and not powershell:
                position += 1
                continue
            pair = None if powershell else assignment(words[position])
            if not pair:
                break
            if pair[0] == ANNOUNCE:
                prefix = pair[1]
            position += 1
        if position >= len(words):
            continue
        name = launcher_of(words[position])
        if name is None and base(words[position]).lower().removesuffix('.exe') in SHELLS:
            position += 1
            executes = True
            while position < len(words) and words[position][:1] in ('-', '+') and words[position] != '-':
                option = words[position]
                position += 1
                if option == '--':
                    break
                if option.startswith('--'):
                    continue
                if set(option[1:]) & set('nco'):
                    # -n: syntax check only; -c/-o take an argument, so the
                    # launcher would not be the script bash runs.
                    executes = False
                    break
            if executes and position < len(words):
                name = launcher_of(words[position])
        if name is not None:
            return name, without_redirects(words[position + 1:]), prefix if prefix else exported
    return None


def options(launcher, args):
    """(model, effort) as getopts reads them, or None for read-back/help calls."""
    if args and args[0] in READBACK:
        return None
    spec = GETOPTS[launcher]
    takes = {spec[i] for i in range(len(spec) - 1) if spec[i + 1] == ':' and spec[i] != ':'}
    values = {'m': 'not given', 'e': 'not given'}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--' or not arg.startswith('-') or arg == '-':
            break
        if arg in HELP or arg in READBACK:
            return None
        if arg.startswith('--'):
            return 'unparsed', 'unparsed'
        letters = arg[1:]
        for offset, letter in enumerate(letters):
            if letter in takes:
                value = letters[offset + 1:]
                if not value:
                    index += 1
                    if index >= len(args):
                        return 'unparsed', 'unparsed'
                    value = args[index]
                if letter in values:
                    values[letter] = value
                break
            if letter not in spec or letter == ':':
                return 'unparsed', 'unparsed'
        index += 1
    return values['m'], values['e']


def message(data):
    tool = data.get('tool_name')
    if tool not in ('Bash', 'PowerShell') or data.get('agent_id'):
        return None
    command = (data.get('tool_input') or {}).get('command')
    if not isinstance(command, str):
        return None
    found = find_call(tokenize(command, tool == 'PowerShell'), tool == 'PowerShell')
    if found is None:
        return None
    launcher, args, announce = found
    text = 'HARNESS DELEGATION: ' + (announce if announce else 'announce line not passed')
    if launcher == 'local-run.sh':
        # local-read.py is argparse: -h/--help anywhere prints help and starts no work.
        if any(arg in HELP for arg in args):
            return None
        return text + ' | call: local-run.sh'
    parsed = options(launcher, args)
    if parsed is None:
        return None
    return f'{text} | call: {launcher} -m {parsed[0]} -e {parsed[1]}'


def main():
    try:
        raw = sys.stdin.buffer.read().decode('utf-8', errors='replace')
        data = json.loads(raw)
        text = message(data) if isinstance(data, dict) else None
        if text:
            sys.stdout.write(json.dumps({'systemMessage': text}) + '\n')
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    main()
    # A closed stdout must not turn into a nonzero exit at interpreter shutdown.
    os._exit(0)
