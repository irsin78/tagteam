import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TAGTEAM = os.path.dirname(HERE)
sys.path.insert(0, TAGTEAM)
import state  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always",
}


def real(path):
    return os.path.realpath(path)


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class StateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = real(self._tmp.name)
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TAGTEAM_STATE_DIR", None)
        state._cache.clear()
        self.addCleanup(state._cache.clear)

    def git(self, cwd, *args):
        out = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, "git %s: %s" % (" ".join(args), out.stderr))
        return out.stdout

    def repo(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        self.git(path, "init", "-q")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "init")
        return path

    def sd(self, cwd):
        state._cache.clear()
        return real(state.state_dir(cwd))

    def tt(self, root):
        return real(os.path.join(root, ".tagteam"))


class StateDirTest(StateTestBase):
    @staticmethod
    def install_root_state():
        # installed as <project>/.claude/tagteam/state.py: two folders above the module's folder
        here = os.path.dirname(os.path.abspath(state.__file__))
        return os.path.join(os.path.dirname(os.path.dirname(here)), ".tagteam")

    def test_normal_repo_and_subfolder_use_the_work_tree(self):
        repo = self.repo("repo")
        sub = os.path.join(repo, "a", "b")
        os.makedirs(sub)
        self.assertEqual(self.sd(repo), self.tt(repo))
        self.assertEqual(self.sd(sub), self.tt(repo))

    def test_computing_creates_nothing(self):
        repo = self.repo("repo")
        state.state_dir(repo)
        self.assertFalse(os.path.exists(os.path.join(repo, ".tagteam")))

    def test_linked_worktree_uses_the_main_work_tree(self):
        repo = self.repo("repo")
        wt = os.path.join(self.tmp, "wt")
        self.git(repo, "worktree", "add", "-q", wt, "-b", "side")
        self.assertEqual(self.sd(wt), self.tt(repo))

    def make_super(self):
        sub = self.repo("sub")
        sup = self.repo("sup")
        self.git(sup, "submodule", "add", "-q", sub, "libs/sub")
        self.git(sup, "commit", "-q", "-m", "add submodule")
        return sup, os.path.join(sup, "libs", "sub")

    def test_submodule_uses_the_superproject_work_tree(self):
        sup, sub = self.make_super()
        self.assertEqual(self.sd(sub), self.tt(sup))
        self.assertEqual(self.sd(sup), self.tt(sup))
        deep = os.path.join(sub, "x")
        os.makedirs(deep)
        self.assertEqual(self.sd(deep), self.tt(sup))
        self.assertFalse(os.path.exists(os.path.join(sub, ".tagteam")))

    def test_worktree_of_a_submodule_uses_the_superproject_work_tree(self):
        sup, sub = self.make_super()
        outside = os.path.join(self.tmp, "sub-wt")
        inside = os.path.join(sup, "inner-wt")
        self.git(sub, "worktree", "add", "-q", outside, "-b", "w1")
        self.git(sub, "worktree", "add", "-q", inside, "-b", "w2")
        self.assertEqual(self.sd(outside), self.tt(sup))
        self.assertEqual(self.sd(inside), self.tt(sup))

    def test_submodules_of_a_superproject_worktree_use_the_main_work_tree(self):
        deep = self.repo("deep")
        sub = self.repo("sub")
        self.git(sub, "submodule", "add", "-q", deep, "inner")
        self.git(sub, "commit", "-q", "-m", "inner")
        sup = self.repo("sup")
        self.git(sup, "submodule", "add", "-q", sub, "libs/sub")
        self.git(sup, "commit", "-q", "-m", "sm")
        suplw = os.path.join(self.tmp, "suplw")
        self.git(sup, "worktree", "add", "-q", suplw, "-b", "lw")
        self.git(suplw, "submodule", "update", "-q", "--init", "--recursive")
        direct, nested = os.path.join(suplw, "libs", "sub"), os.path.join(suplw, "libs", "sub", "inner")
        # the module folders live under <sup>/.git/worktrees/<id>/modules/...
        common = self.git(direct, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        self.assertIn("worktrees", common.replace("\\", "/"))
        self.assertEqual(self.sd(direct), self.tt(sup))
        self.assertEqual(self.sd(nested), self.tt(sup))
        # linked worktrees of those submodules have no detectable superproject: the common dir is the only clue
        w_direct = os.path.join(self.tmp, "w-direct")
        w_nested = os.path.join(self.tmp, "w-nested")
        self.git(direct, "worktree", "add", "-q", w_direct, "-b", "wd")
        self.git(nested, "worktree", "add", "-q", w_nested, "-b", "wn")
        self.assertEqual(self.sd(w_direct), self.tt(sup))
        self.assertEqual(self.sd(w_nested), self.tt(sup))
        # and their old folders (module dir and the superproject's) are all found
        old_sup = os.path.join(sup, ".git", "tagteam")
        nested_common = self.git(nested, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        old_nested = os.path.join(real(nested_common), "tagteam")
        write(os.path.join(old_sup, "a.json"), "1")
        write(os.path.join(old_nested, "b.json"), "2")
        self.assertEqual(sorted(real(p) for p in state.legacy_dirs(w_nested)), sorted([real(old_sup), real(old_nested)]))
        state.migrate(w_nested)
        self.assertEqual(read(os.path.join(sup, ".tagteam", "a.json")), "1")
        self.assertEqual(read(os.path.join(sup, ".tagteam", "b.json")), "2")

    def test_worktree_of_the_superproject_still_uses_the_main_work_tree(self):
        sup, sub = self.make_super()
        wt = os.path.join(self.tmp, "sup-wt")
        self.git(sup, "worktree", "add", "-q", wt, "-b", "w")
        self.assertEqual(self.sd(wt), self.tt(sup))

    def test_separate_git_dir_uses_the_work_tree(self):
        repo = os.path.join(self.tmp, "sep")
        gitdir = os.path.join(self.tmp, "elsewhere.git")
        os.makedirs(repo)
        self.git(repo, "init", "-q", "--separate-git-dir", gitdir)
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        # git records no link from the git dir back to its work tree, and its worktree list names the
        # git dir itself: the state folder must still be inside a checkout, never in the git dir
        self.assertEqual(self.sd(repo), self.tt(repo))
        wt = os.path.join(self.tmp, "sep-wt")
        self.git(repo, "worktree", "add", "-q", wt, "-b", "side")
        self.assertEqual(self.sd(repo), self.tt(repo))
        self.assertEqual(self.sd(wt), self.tt(wt))  # known limitation: a linked worktree has its own folder
        os.makedirs(os.path.join(repo, "x"))
        self.assertEqual(self.sd(os.path.join(repo, "x")), self.tt(repo))

    def test_outside_git_uses_the_install_root(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        expected = self.install_root_state()
        self.assertEqual(self.sd(plain), real(expected))

    def test_git_failure_falls_back_to_the_install_root(self):
        repo = self.repo("repo")
        expected = self.install_root_state()
        with mock.patch.object(state.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(real(state.state_dir(repo)), real(expected))
        state._cache.clear()
        with mock.patch.object(state.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertEqual(real(state.state_dir(repo)), real(expected))

    def test_override_wins_and_is_not_cached(self):
        repo = self.repo("repo")
        custom = os.path.join(self.tmp, "custom")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": custom}):
            self.assertEqual(state.state_dir(repo), custom)
        self.assertEqual(self.sd(repo), self.tt(repo))

    def test_empty_override_is_ignored(self):
        repo = self.repo("repo")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": ""}):
            self.assertEqual(self.sd(repo), self.tt(repo))

    def test_result_is_cached_per_process(self):
        repo = self.repo("repo")
        first = state.state_dir(repo)
        with mock.patch.object(state.subprocess, "run", side_effect=AssertionError("git called again")):
            self.assertEqual(state.state_dir(repo), first)


class EnsureWritableTest(StateTestBase):
    def test_creates_and_leaves_no_probe_file(self):
        target = os.path.join(self.tmp, "a", "b")
        self.assertEqual(state.ensure_writable(target), target)
        self.assertEqual(os.listdir(target), [])

    def test_blocked_path_message_names_folder_and_fixes(self):
        blocker = os.path.join(self.tmp, "blocker")
        write(blocker)
        target = os.path.join(blocker, ".tagteam")  # a file where a folder is needed
        with self.assertRaises(state.StateError) as cm:
            state.ensure_writable(target)
        msg = str(cm.exception)
        self.assertIn(target, msg)
        self.assertIn("TAGTEAM_STATE_DIR", msg)
        self.assertIn("Codex: run this command with escalated permissions", msg)

    def test_probe_creation_failure_is_reported(self):
        # a stand-in for a read-only folder that also works on Windows (a read-only directory attribute
        # does not stop file creation there)
        folder = os.path.join(self.tmp, "no-probe")
        with mock.patch.object(state.tempfile, "mkstemp", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(state.StateError) as cm:
                state.ensure_writable(folder)
        self.assertIn(folder, str(cm.exception))
        self.assertIn("Permission denied", str(cm.exception))

    def test_probe_is_unique_and_only_it_is_removed(self):
        folder = os.path.join(self.tmp, "keep")
        old_name = ".write-test-%d" % os.getpid()  # the former predictable name must not be touched
        write(os.path.join(folder, old_name), "mine")
        write(os.path.join(folder, "other.json"), "x")
        state.ensure_writable(folder)
        self.assertEqual(sorted(os.listdir(folder)), sorted([old_name, "other.json"]))
        self.assertEqual(read(os.path.join(folder, old_name)), "mine")

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions and a non-root user")
    def test_read_only_folder(self):
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        with self.assertRaises(state.StateError) as cm:
            state.ensure_writable(ro)
        self.assertIn(ro, str(cm.exception))


class MigrateTest(StateTestBase):
    def test_nothing_to_do_outside_git_and_without_old_folders(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(state.legacy_dirs(plain), [])
        self.assertEqual(state.migrate(plain), [])
        repo = self.repo("repo")
        self.assertEqual(state.legacy_dirs(repo), [])
        self.assertEqual(state.migrate(repo), [])
        self.assertFalse(os.path.exists(os.path.join(repo, ".tagteam")))

    def test_moves_old_folder_without_destroying_it(self):
        repo = self.repo("repo")
        old = os.path.join(repo, ".git", "tagteam")
        write(os.path.join(old, "exhausted.json"), "{old}")
        write(os.path.join(old, "sessions", "s1.json"), "{s1}")
        self.assertEqual([real(p) for p in state.legacy_dirs(repo)], [real(old)])
        report = state.migrate(repo)
        new = os.path.join(repo, ".tagteam")
        self.assertEqual(read(os.path.join(new, "exhausted.json")), "{old}")
        self.assertEqual(read(os.path.join(new, "sessions", "s1.json")), "{s1}")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(read(os.path.join(repo, ".git", "tagteam.moved", "exhausted.json")), "{old}")
        self.assertTrue(any("tagteam.moved" in line for line in report), report)
        self.assertEqual(state.legacy_dirs(repo), [])
        self.assertEqual(state.migrate(repo), [])  # once

    def test_conflict_keeps_the_new_file_and_reports_it(self):
        repo = self.repo("repo")
        old = os.path.join(repo, ".git", "tagteam")
        write(os.path.join(old, "exhausted.json"), "old")
        write(os.path.join(old, "sessions", "s1.json"), "old-s1")
        write(os.path.join(old, "sessions", "s2.json"), "old-s2")
        new = os.path.join(repo, ".tagteam")
        write(os.path.join(new, "exhausted.json"), "new")
        write(os.path.join(new, "sessions", "s1.json"), "new-s1")
        report = state.migrate(repo)
        self.assertEqual(read(os.path.join(new, "exhausted.json")), "new")
        self.assertEqual(read(os.path.join(new, "sessions", "s1.json")), "new-s1")
        self.assertEqual(read(os.path.join(new, "sessions", "s2.json")), "old-s2")
        conflicts = [line for line in report if line.startswith("conflict")]
        self.assertEqual(len(conflicts), 2, report)
        self.assertTrue(any("exhausted.json" in c for c in conflicts))
        self.assertTrue(any("sessions/s1.json" in c for c in conflicts))
        # the old copy of the conflicting file is preserved
        self.assertEqual(read(os.path.join(repo, ".git", "tagteam.moved", "exhausted.json")), "old")

    def test_moved_name_gets_a_number_when_taken(self):
        repo = self.repo("repo")
        git = os.path.join(repo, ".git")
        write(os.path.join(git, "tagteam", "a.json"), "1")
        state.migrate(repo)
        write(os.path.join(git, "tagteam", "b.json"), "2")
        state.migrate(repo)
        write(os.path.join(git, "tagteam", "c.json"), "3")
        state.migrate(repo)
        self.assertTrue(os.path.isdir(os.path.join(git, "tagteam.moved")))
        self.assertTrue(os.path.isdir(os.path.join(git, "tagteam.moved-2")))
        self.assertTrue(os.path.isdir(os.path.join(git, "tagteam.moved-3")))
        self.assertEqual([n for n in sorted(os.listdir(os.path.join(repo, ".tagteam")))], ["a.json", "b.json", "c.json"])

    def test_submodule_moves_both_old_locations(self):
        sub = self.repo("sub")
        sup = self.repo("sup")
        self.git(sup, "submodule", "add", "-q", sub, "libs/sub")
        self.git(sup, "commit", "-q", "-m", "sm")
        subdir = os.path.join(sup, "libs", "sub")
        old_sup = os.path.join(sup, ".git", "tagteam")
        old_mod = os.path.join(sup, ".git", "modules", "libs", "sub", "tagteam")
        write(os.path.join(old_sup, "runs", "r1", "result.json"), "r1")
        write(os.path.join(old_sup, "exhausted.json"), "from-super")
        write(os.path.join(old_mod, "sessions", "s9.json"), "s9")
        write(os.path.join(old_mod, "exhausted.json"), "from-module")
        found = sorted(real(p) for p in state.legacy_dirs(subdir))
        self.assertEqual(found, sorted([real(old_sup), real(old_mod)]))
        report = state.migrate(subdir)
        new = os.path.join(sup, ".tagteam")
        self.assertEqual(read(os.path.join(new, "runs", "r1", "result.json")), "r1")
        self.assertEqual(read(os.path.join(new, "sessions", "s9.json")), "s9")
        self.assertEqual(read(os.path.join(new, "exhausted.json")), "from-super")  # first location wins
        self.assertTrue(any(line.startswith("conflict") and "exhausted.json" in line for line in report), report)
        self.assertEqual(read(os.path.join(old_mod + ".moved", "exhausted.json")), "from-module")
        self.assertEqual(read(os.path.join(old_sup + ".moved", "exhausted.json")), "from-super")
        self.assertEqual(state.legacy_dirs(subdir), [])
        self.assertFalse(os.path.exists(os.path.join(subdir, ".tagteam")))

    def test_worktree_of_a_submodule_finds_the_module_folder(self):
        sub = self.repo("sub")
        sup = self.repo("sup")
        self.git(sup, "submodule", "add", "-q", sub, "libs/sub")
        self.git(sup, "commit", "-q", "-m", "sm")
        wt = os.path.join(self.tmp, "subwt")
        self.git(os.path.join(sup, "libs", "sub"), "worktree", "add", "-q", wt, "-b", "w")
        old_mod = os.path.join(sup, ".git", "modules", "libs", "sub", "tagteam")
        write(os.path.join(old_mod, "exhausted.json"), "m")
        state.migrate(wt)
        self.assertEqual(read(os.path.join(sup, ".tagteam", "exhausted.json")), "m")

    def test_worktree_of_a_submodule_finds_the_superproject_old_folder_too(self):
        sub = self.repo("sub")
        sup = self.repo("sup")
        self.git(sup, "submodule", "add", "-q", sub, "libs/sub")
        self.git(sup, "commit", "-q", "-m", "sm")
        wt = os.path.join(self.tmp, "subwt")
        self.git(os.path.join(sup, "libs", "sub"), "worktree", "add", "-q", wt, "-b", "w")
        old_sup = os.path.join(sup, ".git", "tagteam")
        old_mod = os.path.join(sup, ".git", "modules", "libs", "sub", "tagteam")
        write(os.path.join(old_sup, "runs", "r1", "result.json"), "r1")
        write(os.path.join(old_mod, "sessions", "s9.json"), "s9")
        self.assertEqual(sorted(real(p) for p in state.legacy_dirs(wt)), sorted([real(old_sup), real(old_mod)]))
        state.migrate(wt)
        new = os.path.join(sup, ".tagteam")
        self.assertEqual(read(os.path.join(new, "runs", "r1", "result.json")), "r1")
        self.assertEqual(read(os.path.join(new, "sessions", "s9.json")), "s9")
        self.assertEqual(state.legacy_dirs(wt), [])

    def test_separate_git_dir_with_a_worktree_finds_the_old_folder(self):
        repo = os.path.join(self.tmp, "sep")
        gitdir = os.path.join(self.tmp, "elsewhere.git")
        os.makedirs(repo)
        self.git(repo, "init", "-q", "--separate-git-dir", gitdir)
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        wt = os.path.join(self.tmp, "sep-wt")
        self.git(repo, "worktree", "add", "-q", wt, "-b", "side")
        write(os.path.join(gitdir, "tagteam", "a.json"), "1")
        state.migrate(wt)
        self.assertEqual(read(os.path.join(wt, ".tagteam", "a.json")), "1")  # inside the checkout it ran from
        self.assertFalse(os.path.exists(os.path.join(gitdir, ".tagteam")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".tagteam")))

    def test_failed_copy_leaves_no_partial_file_and_a_retry_completes(self):
        repo = self.repo("repo")
        old = os.path.join(repo, ".git", "tagteam")
        write(os.path.join(old, "a.json"), "AAAA")
        write(os.path.join(old, "sessions", "b.json"), "BBBB")
        write(os.path.join(old, "sessions", "c.json"), "CCCC")
        real_copy = shutil.copy2
        calls = []

        def flaky(src, dst, **kw):
            calls.append(src)
            if len(calls) == 2:
                with open(dst, "w", encoding="utf-8") as f:  # a half-written destination
                    f.write("BB")
                raise OSError(28, "No space left on device")
            return real_copy(src, dst, **kw)

        with mock.patch.object(state.shutil, "copy2", flaky):
            with self.assertRaises(state.StateError) as cm:
                state.migrate(repo)
        self.assertIn("nothing was removed", str(cm.exception))
        new = os.path.join(repo, ".tagteam")
        self.assertTrue(os.path.isdir(old))  # the old folder is untouched
        self.assertEqual(read(os.path.join(old, "sessions", "b.json")), "BBBB")
        leftovers = [n for _, _, names in os.walk(new) for n in names if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertFalse(os.path.exists(os.path.join(new, "sessions", "b.json")))  # no truncated copy
        report = state.migrate(repo)  # retry
        self.assertEqual(read(os.path.join(new, "a.json")), "AAAA")
        self.assertEqual(read(os.path.join(new, "sessions", "b.json")), "BBBB")
        self.assertEqual(read(os.path.join(new, "sessions", "c.json")), "CCCC")
        self.assertFalse([line for line in report if line.startswith("conflict")], report)
        self.assertEqual(read(os.path.join(repo, ".git", "tagteam.moved", "sessions", "b.json")), "BBBB")

    def test_override_set_moves_nothing(self):
        repo = self.repo("repo")
        old = os.path.join(repo, ".git", "tagteam")
        write(os.path.join(old, "a.json"), "1")
        custom = os.path.join(self.tmp, "custom")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": custom}):
            report = state.migrate(repo)
        self.assertEqual(len(report), 1)
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(custom))

    def test_unwritable_new_folder_raises_and_keeps_the_old_one(self):
        repo = self.repo("repo")
        old = os.path.join(repo, ".git", "tagteam")
        write(os.path.join(old, "a.json"), "1")
        write(os.path.join(repo, ".tagteam"), "a file in the way")
        with self.assertRaises(state.StateError):
            state.migrate(repo)
        self.assertEqual(read(os.path.join(old, "a.json")), "1")


class ToolMessageTest(StateTestBase):
    """The tools that write to the state folder report an unwritable folder as exit 2 with the fix."""

    def blocked(self):
        blocker = os.path.join(self.tmp, "blocker")
        write(blocker)
        return os.path.join(blocker, "state")

    def run_tool(self, script, *args, state_dir=None, extra_env=None):
        env = dict(os.environ, TAGTEAM_STATE_DIR=state_dir, PYTHONIOENCODING="utf-8")
        env.update(extra_env or {})
        return subprocess.run([sys.executable, os.path.join(TAGTEAM, script)] + list(args), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env, timeout=60)

    def test_route_mark_and_clear_exhausted(self):
        target = self.blocked()
        for args in (("--mark-exhausted", "openai", "--evidence", "limit"), ("--clear-exhausted", "openai")):
            r = self.run_tool("route.py", *args, state_dir=target)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(target, r.stderr)
            self.assertIn("escalated permissions", r.stderr)

    def test_route_reading_works_without_a_state_folder(self):
        r = self.run_tool("route.py", "--role", "read", "--json", state_dir=os.path.join(self.tmp, "nothing"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nothing")))
        json.loads(r.stdout)

    def test_models_collect(self):
        target = self.blocked()
        r = self.run_tool("models_collect.py", "--project", self.tmp, state_dir=target)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(target, r.stderr)
        self.assertIn("escalated permissions", r.stderr)


if __name__ == "__main__":
    unittest.main()
