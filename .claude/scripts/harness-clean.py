#!/usr/bin/env python
"""Explicit maintenance for this project's completed run records and old logs.

Preview is the default. --apply removes listed files, never directories.
No launcher invokes this tool. Active/unknown runs keep their logs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)

def old_files(folder, cutoff):
    if not folder.is_dir() or linked(folder):
        return []
    result = []
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if not linked(Path(base) / name)]
        for name in files:
            path = Path(base) / name
            if not linked(path) and path.is_file() and path.stat().st_mtime < cutoff:
                result.append(path)
    return result

def candidates(root, state_dir, cutoff):
    paths, active = [], False
    if state_dir.is_dir():
        if linked(state_dir):
            raise ValueError('linked state directory is unsupported')
        for path in state_dir.glob('state-*.json'):
            if linked(path):
                active = True
                continue
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
                finished = state.get('state') in ('done', 'aborted')
                if not finished:
                    active = True
                    continue
                updated = state.get('updated_epoch')
                if isinstance(updated, (int, float)) and updated < cutoff:
                    paths.append(path)
                    report = state_dir / ('report-' + path.name[len('state-'): -len('.json')] + '.txt')
                    if report.is_file() and not linked(report):
                        paths.append(report)
            except (OSError, ValueError, TypeError):
                active = True
    if not active:
        for name in ('codex', 'claude', 'agy', 'local', 'cmd'):
            paths.extend(old_files(root / '.claude' / (name + '-logs'), cutoff))
    return paths, active

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.days < 1 or os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        parser.error('positive retention and an orchestrator session are required')
    probe = subprocess.run([sys.executable, str(Path(__file__).with_name('workspace-snapshot.py')), '--root'],
                           capture_output=True, text=True, check=True)
    root = Path(probe.stdout.strip()).resolve()
    key = os.environ.get('HARNESS_TREE_KEY') or hashlib.sha1(
        os.path.normcase(os.path.realpath(root)).encode()).hexdigest()[:16]
    if not key or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in key):
        parser.error('invalid tree key')
    state_base = Path(os.environ.get('HARNESS_STATE_DIR') or Path.home() / '.claude/harness-runs')
    state_dir = state_base / key
    if state_base.exists() and linked(state_base):
        parser.error('linked state base is unsupported')
    paths, active = candidates(root, state_dir, time.time() - args.days * 86400)
    for path in paths:
        # Resolve and verify each final target before deletion. Only regular files.
        resolved = path.resolve()
        if not (resolved.is_relative_to(root) or resolved.is_relative_to(state_dir.resolve())):
            raise ValueError('maintenance target escaped its declared root')
        print(('REMOVE ' if args.apply else 'WOULD_REMOVE ') + str(path))
        if args.apply:
            path.unlink()
    if active:
        print('Logs retained: running or unreadable run records exist.')
    print(f'{len(paths)} files; ' + ('applied' if args.apply else 'preview only; pass --apply to remove'))
    return 0

if __name__ == '__main__':
    sys.exit(main())
