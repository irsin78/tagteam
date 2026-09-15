#!/usr/bin/env python
"""Exercise workspace evidence with real Git transitions and file contents."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('workspace-snapshot.py')


class WorkspaceSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'core.autocrlf', 'false')
        (self.root / 'tracked.bin').write_bytes(b'original\0data')
        (self.root / '.gitignore').write_text('.claude/claude-logs/\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def snapshot(self):
        return json.loads(subprocess.check_output([sys.executable, str(SCRIPT)], cwd=self.root))

    def test_excludes_only_owned_log_file_even_when_logs_share_project_directory(self):
        (self.root / 'run.txt').write_text('launcher log')
        (self.root / 'app.txt').write_text('pending project work')
        for name in ('run.txt', 'logs/../run.txt'):
            result = json.loads(subprocess.check_output(
                [sys.executable, str(SCRIPT), '--exclude', name], cwd=self.root))
            self.assertEqual(set(result), {'app.txt'})

    def test_same_status_and_length_still_reveal_binary_edit(self):
        file = self.root / 'tracked.bin'
        file.write_bytes(b'first\0edit')
        before = self.snapshot()
        status = self.git('status', '--porcelain')
        file.write_bytes(b'other\0edit')
        self.assertEqual(status, self.git('status', '--porcelain'))
        self.assertNotEqual(before['tracked.bin'], self.snapshot()['tracked.bin'])

    def test_deleted_untracked_and_reverted_dirty_files_remain_in_delta(self):
        file = self.root / 'ünïcode file.txt'
        file.write_text('existing', encoding='utf-8')
        (self.root / 'tracked.bin').write_bytes(b'dirty')
        before = self.snapshot()
        file.unlink()
        (self.root / 'tracked.bin').write_bytes(b'original\0data')
        after = self.snapshot()
        self.assertEqual(set(before), {'tracked.bin', 'ünïcode file.txt'})
        self.assertEqual(after, {})
        # Both sides of a transition participate in the actual CLI comparison.
        with tempfile.TemporaryDirectory() as evidence:
            paths = [Path(evidence) / name for name in ('before.json', 'after.json')]
            for path, value in zip(paths, (before, after)):
                path.write_text(json.dumps(value), encoding='utf-8')
            output = subprocess.check_output([sys.executable, str(SCRIPT), '--compare', *map(str, paths)],
                                             cwd=self.root, text=True, encoding='utf-8')
            self.assertEqual(set(output.splitlines()), {'tracked.bin', 'ünïcode file.txt'})

    def test_staged_only_change_with_identical_status_and_worktree(self):
        path = self.root / 'tracked.bin'
        path.write_bytes(b'staged one')
        self.git('add', 'tracked.bin')
        path.write_bytes(b'working copy')
        before, status = self.snapshot(), self.git('status', '--porcelain')
        path.write_bytes(b'staged two')
        self.git('add', 'tracked.bin')
        path.write_bytes(b'working copy')
        after = self.snapshot()
        self.assertEqual(status, self.git('status', '--porcelain'))
        self.assertEqual(before['tracked.bin'][:-1], after['tracked.bin'][:-1])
        self.assertNotEqual(before['tracked.bin'], after['tracked.bin'])
        # A status refresh does not invent an index-content change.
        self.assertEqual(after, self.snapshot())

    def test_rename_records_both_paths_and_excludes_launcher_logs(self):
        self.git('mv', 'tracked.bin', 'renamed file.bin')
        logs = self.root / '.claude/claude-logs'
        logs.mkdir(parents=True)
        (logs / 'run.txt').write_text('launcher output')
        result = self.snapshot()
        self.assertEqual(set(result), {'tracked.bin', 'renamed file.bin'})

    def test_broken_index_is_failure_not_non_git_fallback(self):
        (self.root / '.git/index').write_bytes(b'invalid index')
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')


class NonGitSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harness-nongit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.claude').mkdir()

    def call(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.root,
                              capture_output=True, text=True, encoding='utf-8')

    def snapshot(self, *args):
        result = self.call(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_creation_same_length_edit_and_deletion(self):
        file = self.root / 'ünïcode file.bin'
        self.assertEqual(self.snapshot(), {})
        file.write_bytes(b'first\0data')
        before = self.snapshot()
        file.write_bytes(b'other\0data')
        self.assertNotEqual(before[file.name], self.snapshot()[file.name])
        file.unlink()
        self.assertEqual(self.snapshot(), {})

    def test_scope_excludes_caches_but_not_arbitrary_log_directory_or_gitignore(self):
        for name in ('node_modules/pkg/a', '.claude/claude-logs/a', '.claude/scripts/a',
                     'logs/app.txt', 'ignored-by-git.txt'):
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('contents')
        (self.root / '.gitignore').write_text('ignored-by-git.txt\n')
        self.assertEqual(set(self.snapshot()),
                         {'.claude/scripts/a', 'logs/app.txt', 'ignored-by-git.txt', '.gitignore'})
        self.assertIn('logs/app.txt', self.snapshot('--exclude', 'logs/owned.txt'))

    def test_subdirectories_share_root_but_launch_requires_root(self):
        sub = self.root / 'src/nested'
        sub.mkdir(parents=True)
        result = self.call('--root', cwd=sub)
        self.assertEqual(Path(result.stdout.strip()), self.root)
        result = self.call('--require-root', cwd=sub)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('project root', result.stderr)

    def test_corrupt_git_marker_does_not_degrade_to_file_scan(self):
        (self.root / '.git').mkdir()
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Git workspace inspection failed', result.stderr)

    def test_nested_gitfile_and_directory_both_require_separate_workspace(self):
        nested = self.root / 'nested'
        nested.mkdir()
        marker = nested / '.git'
        for as_file in (True, False):
            if as_file:
                marker.write_text('gitdir: ../elsewhere\n')
            else:
                marker.mkdir()
            result = self.call()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('nested Git metadata', result.stderr)
            if as_file:
                marker.unlink()

    def test_missing_git_binary_only_allows_true_non_git(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('workspace_snapshot', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.Path, 'cwd', return_value=self.root), patch.object(
                module.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(module.context(), (self.root, 'files'))
            (self.root / '.git').mkdir()
            with self.assertRaisesRegex(ValueError, 'git is unavailable'):
                module.context()

    def test_inventory_failure_is_not_empty_success(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('workspace_snapshot', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.os, 'scandir', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(PermissionError):
                module.snapshot([], workspace=(self.root, 'files'))

    def test_compare_rejects_git_initialization_and_scope_changes(self):
        before = self.snapshot('--envelope')
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        after = self.snapshot('--envelope')
        with tempfile.TemporaryDirectory() as evidence:
            paths = [Path(evidence) / n for n in ('before.json', 'after.json')]
            for changed in (after, {**before, 'excluded_files': ['added exclusion']}):
                for path, value in zip(paths, (before, changed)):
                    path.write_text(json.dumps(value), encoding='utf-8')
                result = self.call('--compare', *map(str, paths))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('changed during the run', result.stderr)

    def test_directory_symlink_target_is_not_traversed(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'file.txt'
            target.write_text('before')
            try:
                os.symlink(outside, self.root / 'linked', target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks unavailable')
            before = self.snapshot()
            target.write_text('after')
            self.assertEqual(before, self.snapshot())
            self.assertEqual(set(before), {'linked'})


if __name__ == '__main__':
    unittest.main()
