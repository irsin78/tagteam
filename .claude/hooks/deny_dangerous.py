#!/usr/bin/env python
"""Direct-command accident checks, not a shell interpreter or permission system.

Opaque: shell programs, substitutions, heredocs, scripts and encoded input.
Only complete direct-command tokens BEFORE an opaque suffix are inspected.
Host permissions and launcher argument checks remain the execution boundary.
See docs/runtime-boundary.md for the deliberately limited detection contract.
"""
import io
import json
import os
import re
import shlex
import sys

READ_ONLY_AGENTS = ('opus-architect',)
WRITE_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')
MUTATORS = {'rm', 'mv', 'cp', 'install', 'tee', 'touch', 'mkdir', 'truncate', 'ln'}
READ_GIT = {'status', 'diff', 'log', 'show', 'rev-parse', 'grep', 'blame',
            'ls-files', 'ls-tree', 'merge-base', 'rev-list', 'shortlog'}
CP_PATTERN = (
    r'(?:^|/)docs/orchestration/(?:delegation-matrix|retry-policy)\.md$|'
    r'(?:^|/)(?:\.claude/(?:hooks|scripts|rules|agents|skills|commands)(?:/|$)|'
    r'\.codex/hooks(?:/|$)|'
    r'\.agents/(?:agents(?:/|$)|hooks(?:\.json$|/|$))|'
    r'\.claude/(?:settings(?:\.local)?\.json|sandbox-sensitive\.json|'
    r'model-bindings(?:\.local)?\.json|\.stop-gate|\.preflight-status)$|'
    r'\.codex/(?:config\.toml|hooks\.json|AGENTS(?:\.override)?\.md)$|'
    r'\.gemini/antigravity-cli/settings\.json$|'
    r'\.gemini/config/(?:agents/agy-fetcher\.md|hooks(?:\.json|/agy_fetch_view_guard\.py))$|'
    r'(?:\.mcp\.json|CLAUDE\.md|AGENTS(?:\.override)?\.md|'
    r'check-windows-aliases\.ps1|check-posix\.sh)$)')


# Harness copies kept under these project-root folders (the published template
# submodule) are content, not this project's live control plane: the hooks that
# run here are always loaded from the outer .claude/, never from a copy.
TEMPLATE_DIRS = ('template',)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def clean_path(path):
    return os.path.normpath(path.replace('\\', '/')).replace('\\', '/')


def template_path(path, cwd):
    """True only when path resolves on disk inside a template folder at the project root.

    Fails closed: an unknown cwd for a relative path, a missing template folder,
    or a link/junction that leaves the folder all mean "not exempt".
    """
    if not os.path.isabs(path):
        if cwd is None:
            return False
        path = os.path.join(cwd, path)
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(PROJECT_ROOT)
        relative = os.path.relpath(real, root)
        parts = relative.split(os.sep)
        if not parts or parts[0] in ('', '.', '..'):
            return False
        for name in TEMPLATE_DIRS:
            folder = os.path.join(PROJECT_ROOT, name)
            spelled_folder = os.path.join(root, parts[0])
            if not (os.path.isdir(folder) and os.path.isdir(spelled_folder)
                    and os.path.samefile(spelled_folder, folder)):
                continue
            # Rebuild through the canonical folder spelling, then resolve the
            # suffix again so a symlink/junction inside template cannot escape.
            canonical = os.path.realpath(os.path.join(folder, *parts[1:]))
            folder_real = os.path.realpath(folder)
            if os.path.commonpath((canonical, folder_real)) == folder_real \
                    and canonical != folder_real:
                return True
    except (OSError, ValueError):
        pass
    return False


def track_cd(cwd, args):
    """Return (best-effort cwd, certain). Only a plain `cd <existing dir>` is certain."""
    if args[:1] == ['--']:
        args = args[1:]
    if len(args) != 1 or args[0][:1] in ('-', '~'):
        return cwd, False
    target = os.path.normpath(os.path.join(cwd, args[0]))
    return target, os.path.isdir(target)


def cp_path(path, cwd=None, certain=True):
    """Control-plane target? The template exemption needs a certain cwd; the
    pattern is matched against the raw path AND its best-effort resolution."""
    if certain and template_path(path, cwd):
        return False
    candidates = {clean_path(path)}
    if cwd is not None and not os.path.isabs(path):
        candidates.add(clean_path(os.path.join(cwd, path)))
    flags = re.I if sys.platform in ('win32', 'darwin') else 0
    return any(re.search(CP_PATTERN, c, flags) for c in candidates)


class CommandPrefix(io.StringIO):
    def read(self, size=-1):
        value = super().read(size)
        if not value:
            # Do not turn an unfinished word such as git$(...) into "git".
            raise ValueError('opaque suffix')
        return value


def direct_segments(command):
    """Direct command segments as (tokens, terminator); terminator is '' at the end."""
    opaque = min((command.index(t) for t in ('<<', '$(', chr(96)) if t in command), default=None)
    source = CommandPrefix(command[:opaque]) if opaque is not None else command
    result, current = [], []
    try:
        lex = shlex.shlex(source, posix=True, punctuation_chars=';&|<>\n')
        lex.whitespace, lex.whitespace_split = ' \t\r', True
        for token in lex:
            if token and all(c in ';&|\n' for c in token):
                if current:
                    result.append((current, token))
                current = []
            else:
                current.append(token)
        return result + ([(current, '')] if current else [])
    except ValueError:
        return result + ([(current, '')] if current else []) if opaque is not None else []


def direct_commands(command):
    return [tokens for tokens, _ in direct_segments(command)]


def separator_kind(terminator):
    """Classify a punctuation token: seq, and, or, bg, pipe, or other (unrecognised runs)."""
    token = terminator.replace('\n', '')
    if terminator.count(';') > 1:
        return 'other'
    if token in ('', ';'):
        return 'seq'
    if token in ('|', '|&'):
        return 'pipe'
    if token.strip(';') == '&':
        return 'bg'
    return {'&&': 'and', '||': 'or'}.get(token, 'other')


def invocation(tokens):
    args, grants = list(tokens), {}
    if args and args[0] == 'env':
        args.pop(0)
    while args and re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', args[0]):
        key, value = args.pop(0).split('=', 1)
        grants[key] = value
    if args and args[0] in ('command', 'exec'):
        args.pop(0)
    return grants, args


def executable(token):
    return token.replace('\\', '/').rsplit('/', 1)[-1].removesuffix('.exe').lower()


def git_args(args):
    args = list(args)
    while args and args[0].startswith('-'):
        opt = args.pop(0)
        if opt in ('-C', '-c', '--git-dir', '--work-tree', '--namespace') and args:
            args.pop(0)
    return (args[0], args[1:]) if args else ('', [])


def destructive_git(verb, args):
    if verb == 'reset':
        return any(a in ('--hard', '--merge') for a in args)
    if verb == 'restore':
        return '--staged' not in args or '--worktree' in args or '-W' in args
    if verb == 'clean':
        return '--force' in args or any(a.startswith('-') and not a.startswith('--') and 'f' in a for a in args)
    if verb in ('checkout', 'switch'):
        return '--' in args or any(a in ('-f', '--force', '--discard-changes') for a in args)
    return (verb == 'branch' and '-D' in args or
            verb == 'stash' and bool(args) and args[0] in ('drop', 'clear') or
            verb == 'worktree' and 'remove' in args and any(a in ('-f', '--force') for a in args))


def direct_reason(tokens, delegate, readonly, approved, cwd, certain=True):
    grants, argv = invocation(tokens)
    protected_env = ('HARNESS_DELEGATE_RUN', 'HARNESS_STATE_DIR', 'HARNESS_TREE_KEY',
                     'HARNESS_AGY_SETTINGS', 'HARNESS_RUN_ID', 'HARNESS_RUN_CHILD')
    if delegate and any(k.startswith('HARNESS_ALLOW_') or k in protected_env for k in grants):
        return 'harness rule: delegate cannot set orchestrator-only overrides'
    if not argv:
        return None
    name, args = executable(argv[0]), argv[1:]
    if delegate and name in ('export', 'unset', 'declare') and any(a.startswith('HARNESS_') for a in args):
        return 'harness rule: delegate cannot change harness identity or grants'
    if name in ('bash', 'sh') and args and executable(args[0]).endswith('-run.sh'):
        name, args = executable(args[0]), args[1:]
    if name in ('codex', 'claude', 'agy', 'codex-run.sh', 'claude-run.sh', 'agy-run.sh'):
        if any(a.startswith('--dangerously-') or a == '--yolo' for a in args):
            return 'harness rule: permission bypass is forbidden'
        full = any(a == 'danger-full-access' or a.endswith('=danger-full-access') or
                   a == '-sdanger-full-access' or a == '--add-dir' or a.startswith('--add-dir=') or
                   'sandbox_permissions=' in a or 'sandbox_workspace_write.' in a for a in args)
        if full and grants.get('HARNESS_ALLOW_FULL_ACCESS') != '1':
            return 'harness rule: expanded access requires an authorized launcher grant'
        if name == 'codex' and '--skip-git-repo-check' in args:
            return 'harness rule: non-Git repository option is supplied by the launcher only'
    if name == 'git':
        verb, params = git_args(args)
        if delegate and verb in ('commit', 'push'):
            return 'harness rule: commit/push belongs to the orchestrator'
        if readonly and verb not in READ_GIT:
            return 'harness rule: read-only worker cannot run a writing git command'
        if verb == 'push':
            if any(a.startswith('+') or a.startswith('--force') or
                   a.startswith('-') and not a.startswith('--') and 'f' in a for a in params):
                return 'harness rule: force push is forbidden'
            if any(a in ('-d', '--delete', '--mirror') or a.startswith(':') for a in params):
                if grants.get('HARNESS_ALLOW_REMOTE_DELETE') != '1':
                    return 'harness rule: remote deletion needs an authorized grant'
        if destructive_git(verb, params) and (delegate or grants.get('HARNESS_ALLOW_WORKTREE_RESET') != '1'):
            return 'harness rule: destructive worktree operation needs orchestrator authorization'
    if name == 'rm' and any(a.startswith('-') and ('r' in a or a == '--recursive') for a in args):
        if any('.git' in a.replace('\\', '/').split('/') for a in args):
            if delegate or grants.get('HARNESS_ALLOW_WORKTREE_RESET') != '1':
                return 'harness rule: deleting Git recovery data needs orchestrator authorization'
    if readonly and (name in MUTATORS or any(a in ('>', '>>') for a in args)):
        return 'harness rule: read-only worker cannot write files'
    if delegate and not approved:
        targets = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ('>', '>>')]
        if name in MUTATORS:
            paths = [a for a in args if not a.startswith('-')]
            targets += paths[-1:] if name in ('cp', 'install') else paths
        if any(cp_path(p, cwd, certain) for p in targets):
            return 'harness rule: delegate cannot modify harness configuration'
    return None


def evaluate(data, cp_approved=False):
    delegate = bool(data.get('agent_id'))
    readonly = data.get('agent_type') in READ_ONLY_AGENTS
    tool, inputs = data.get('tool_name'), data.get('tool_input') or {}
    if tool in WRITE_TOOLS:
        path = inputs.get('file_path') or inputs.get('notebook_path')
        if delegate and (readonly or not cp_approved and isinstance(path, str) and cp_path(path, data.get('cwd') or os.getcwd())):
            return 'harness rule: worker cannot write this path'
        return None
    if tool not in ('Bash', 'PowerShell', 'exec_command', 'shell_command'):
        return None
    command = inputs.get('command', inputs.get('cmd'))
    if not isinstance(command, str):
        return None
    cwd, certain = data.get('cwd') or os.getcwd(), True
    list_cwd, previous = cwd, 'seq'
    for tokens, terminator in direct_segments(command):
        reason = direct_reason(tokens, delegate, readonly, cp_approved, cwd, certain)
        if reason:
            return reason
        kind = separator_kind(terminator)
        _, argv = invocation(tokens)
        if argv and argv[0] == 'cd':
            if previous in ('seq', 'and', 'or', 'bg') and kind in ('seq', 'and', 'bg'):
                cwd, known = track_cd(cwd, argv[1:])
                certain = certain and known
            else:
                # Inside a pipeline the cd runs in a subshell; after `||` or an
                # unrecognised separator its effect is unknown: no exemption from here on.
                certain = False
        if kind == 'other':
            certain = False
        if kind == 'bg':
            # `&` backgrounds the whole and/or list: its cds never reached this shell.
            cwd = list_cwd
        if kind in ('seq', 'bg'):
            list_cwd = cwd
        previous = kind
    return None


def host_from_argv(argv):
    for i, value in enumerate(argv):
        if value.startswith('--host='):
            return value.split('=', 1)[1]
        if value == '--host' and i + 1 < len(argv):
            return argv[i + 1]
    return 'claude'


def evaluate_host(data, host, env=None):
    env = os.environ if env is None else env
    data = dict(data)
    if env.get('HARNESS_DELEGATE_RUN') == '1':
        data.setdefault('agent_id', 'launcher-run')
    approved = env.get('HARNESS_ALLOW_CONTROL_PLANE') == '1'
    inputs = data.get('tool_input') or {}
    if data.get('tool_name') == 'apply_patch':
        patch = inputs.get('command', inputs.get('input', ''))
        for path in re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', patch, re.M):
            reason = evaluate(dict(data, tool_name='Write', tool_input={'file_path': path}), approved)
            if reason:
                return reason
        return None
    cmd = inputs.get('command')
    if isinstance(cmd, list) and all(isinstance(t, str) for t in cmd):
        data['tool_input'] = dict(inputs, command=shlex.join(cmd))
    elif cmd is not None and not isinstance(cmd, str):
        return 'harness rule: unsupported command payload'
    return evaluate(data, approved)


def self_test():
    data = {'agent_id': 'probe', 'tool_name': 'Write',
            'tool_input': {'file_path': '.claude/settings.json'}}
    denied = evaluate(data)
    data['tool_input']['file_path'] = 'src/app.py'
    ok = bool(denied) and evaluate(data) is None
    print('SELFTEST_OK' if ok else 'SELFTEST_FAILED')
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv:
        return self_test()
    try:
        data = json.load(sys.stdin)
        reason = evaluate_host(data, host_from_argv(sys.argv[1:]))
    except (ValueError, TypeError, AttributeError):
        return 0
    if reason:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
              'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
