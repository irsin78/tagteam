#!/usr/bin/env python
"""Compare Git working files or a non-Git project's file inventory.

This is before/after evidence, not a sandbox or a backup. See the manual's
Git/non-Git workspace section for the scope and exclusions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


# Non-Git scans do not interpret .gitignore. Keep exclusions narrow and
# explicit; project artifacts (including build outputs) are otherwise scanned.
CACHE_DIRS = {'.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
              '.mypy_cache', '.ruff_cache', '.cache'}
RUNTIME_DIRS = {'.claude/' + name + '-logs' for name in ('codex', 'claude', 'agy', 'local')}
RUNTIME_FILES = {'.claude/.probe-cache'}


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


def context():
    cwd = Path.cwd().resolve()
    ancestors = (cwd, *cwd.parents)
    git_marked = any(os.path.lexists(p / '.git') or
                     ((p / 'HEAD').is_file() and (p / 'objects').is_dir() and
                      (p / 'refs').is_dir()) for p in ancestors)
    try:
        probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'],
                               capture_output=True, env={**os.environ, 'LC_ALL': 'C'})
    except FileNotFoundError:
        if git_marked or os.environ.get('GIT_DIR'):
            raise ValueError('Git workspace found but git is unavailable')
    else:
        if probe.returncode == 0:
            return Path(os.fsdecode(probe.stdout).strip()), 'git'
        detail = probe.stderr.decode('utf-8', 'replace').strip()
        if git_marked or os.environ.get('GIT_DIR') or 'not a git repository' not in detail.lower():
            raise ValueError('Git workspace inspection failed: ' + detail)
    root = next((p for p in ancestors if (p / '.claude').is_dir() or
                 (p / 'AGENTS.md').is_file() or (p / 'CLAUDE.md').is_file()), cwd)
    return root, 'files'


def file_record(path, status):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [status, 'missing']
    mode = info.st_mode
    if stat.S_ISLNK(mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        # Junctions and directory symlinks are identities, never scan targets.
        digest = 'symlink:' + os.readlink(path)
    elif stat.S_ISREG(mode):
        with path.open('rb') as stream:
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
            digest = hasher.hexdigest()
    else:
        digest = 'nonregular:' + str(stat.S_IFMT(mode))
    return [status, mode, digest]


def snapshot(exclude, exclude_tree=(), workspace=None):
    root, kind = workspace or context()
    result = {}
    excluded = {Path(os.path.abspath(name)) for name in exclude}
    trees = [Path(os.path.abspath(name)) for name in exclude_tree]

    def omit(path):
        return path in excluded or any(path == p or p in path.parents for p in trees)

    if kind == 'files':
        def visit(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    name = path.relative_to(root).as_posix()
                    if omit(path) or name in RUNTIME_FILES:
                        continue
                    if entry.name == '.git':
                        # Includes worktree/submodule gitfiles and symlinks.
                        raise ValueError('nested Git metadata in non-Git scan: ' + name)
                    info = entry.stat(follow_symlinks=False)
                    linked = stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    if stat.S_ISDIR(info.st_mode) and not linked:
                        if entry.name not in CACHE_DIRS and name not in RUNTIME_DIRS:
                            visit(path)
                    else:
                        result[name] = file_record(path, 'file')
        visit(root)
        return result
    rows = iter(subprocess.check_output([
        'git', 'status', '--porcelain=v1', '-z', '--untracked-files=all'
    ], cwd=root).split(b'\0'))
    for row in rows:
        if not row:
            continue
        status = row[:2].decode('ascii')
        name = os.fsdecode(row[3:])
        if 'R' in status or 'C' in status:
            origin = os.fsdecode(next(rows))
            result[origin] = [status, 'destination:' + name]
        path = root / name
        if omit(path):
            continue
        result[name] = file_record(path, status)
    if result:
        # One metadata read, no second content scan. Status/worktree bytes alone
        # miss a changed staged blob while the same path stays MM or AM.
        index = {}
        for row in subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root).split(b'\0'):
            if row:
                identity, name = row.split(b'\t', 1)
                index.setdefault(os.fsdecode(name), []).append(identity.decode('ascii'))
        for name, record in result.items():
            record.append(index.get(name, []))
    return result


def changed_files(before, after):
    if before.get('format') == 1 or after.get('format') == 1:
        if {k: v for k, v in before.items() if k != 'files'} != {k: v for k, v in after.items() if k != 'files'}:
            raise ValueError('workspace root, mode or exclusions changed during the run')
        before, after = before['files'], after['files']
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def describe(data):
    return 'WORKSPACE: ' + json.dumps({k: v for k, v in data.items() if k != 'files'}, ensure_ascii=True, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exclude', action='append', default=[],
                        help='exact launcher-owned output file to omit; repeat as needed')
    parser.add_argument('--exclude-tree', action='append', default=[],
                        help='launcher-owned private run directory only')
    parser.add_argument('--root', action='store_true', help='print the project root')
    parser.add_argument('--context', action='store_true', help='launcher root, canonical path, tree key and mode; no file scan')
    parser.add_argument('--require-root', action='store_true', help='refuse execution outside the project root')
    parser.add_argument('--envelope', action='store_true', help='include comparison scope metadata')
    parser.add_argument('--describe', metavar='SNAPSHOT')
    parser.add_argument('--compare', nargs=2, metavar=('BEFORE', 'AFTER'))
    parser.add_argument('--save', metavar='SNAPSHOT', help='save an envelope and print its mode and description')
    parser.add_argument('--since', metavar='BEFORE', help='with --save, print changed paths instead')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    if args.compare:
        before, after = [json.loads(Path(p).read_text(encoding='utf-8'))
                         for p in args.compare]
        for name in changed_files(before, after):
            print(name)
    elif args.describe:
        data = json.loads(Path(args.describe).read_text(encoding='utf-8'))
        print(describe(data))
    else:
        if args.since and not args.save:
            parser.error('--since requires --save')
        if args.context:
            # Printed before context() so launchers can distinguish a broken
            # workspace from a Python alias that never executed this script.
            print('HARNESS_CONTEXT_V1', flush=True)
        root, kind = context()
        if args.context:
            cwd = canonical(root)
            print(str(root))
            print(cwd)
            print(hashlib.sha1(cwd.encode('utf-8')).hexdigest()[:16])
            print(kind)
            return
        if args.root:
            print(str(root))
            return
        if args.require_root and canonical(root) != canonical(Path.cwd()):
            raise ValueError('run the launcher from the project root: ' + str(root))
        data = snapshot(args.exclude, args.exclude_tree, (root, kind))
        if args.envelope or args.save:
            data = dict(format=1, root=canonical(root), mode=kind,
                        excluded_files=sorted(os.path.normcase(os.path.abspath(p)) for p in args.exclude),
                        excluded_trees=sorted(os.path.normcase(os.path.abspath(p)) for p in args.exclude_tree),
                        defaults=sorted(CACHE_DIRS | RUNTIME_DIRS | RUNTIME_FILES) if kind == 'files' else ['Git ignored files'],
                        files=data)
        encoded = json.dumps(data, ensure_ascii=True, sort_keys=True)
        if args.save:
            Path(args.save).write_text(encoded + '\n', encoding='utf-8')
            if args.since:
                before = json.loads(Path(args.since).read_text(encoding='utf-8'))
                for name in changed_files(before, data):
                    print(name)
            else:
                print(kind)
                print(describe(data))
        else:
            print(encoded)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('workspace snapshot failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
