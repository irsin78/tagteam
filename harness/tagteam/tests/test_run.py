import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS))
import run  # noqa: E402

FAKE = os.path.join(TESTS, "fake_cli.py")
FIXTURES = os.path.join(TESTS, "fixtures")
MARKER = "HARNESS ROLE: DELEGATE"
VENDORS = {"claude": ("claude-sonnet-5-5", "low"), "openai": ("gpt-6.1-terra", "low"), "google": ("gemini-3.8-flash", "low")}


def pid_alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.record = os.path.join(self.tmp.name, "record.json")
        env = {"TAGTEAM_STATE_DIR": self.state, "TAGTEAM_CONFIG_DIR": FIXTURES, "FAKE_RECORD": self.record}
        for v in VENDORS:
            env["TAGTEAM_CLI_" + v.upper()] = json.dumps([sys.executable, FAKE, v])
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HARNESS_DELEGATE_RUN", None)

    def go(self, vendor, mode="ok", prompt="Do the thing.\n", extra=(), model=None, effort=None, cwd=None):
        pf = os.path.join(self.tmp.name, "prompt.md")
        with open(pf, "w", encoding="utf-8", newline="") as fh:
            fh.write(prompt)
        os.environ["FAKE_MODE"] = mode
        argv = ["--vendor", vendor, "--model", model or VENDORS[vendor][0], "--effort", effort or VENDORS[vendor][1],
                "--prompt-file", pf, "--cwd", cwd or self.tmp.name] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(argv)
        return code, out.getvalue(), err.getvalue()

    def recorded(self):
        with open(self.record, encoding="utf-8") as fh:
            return json.load(fh)

    def exhausted(self):
        with open(os.path.join(self.state, "exhausted.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def run_dirs(self):
        root = os.path.join(self.state, "runs")
        return [os.path.join(root, d) for d in os.listdir(root)] if os.path.isdir(root) else []


class StateFolderTests(RunTestBase):
    def test_unwritable_state_folder_is_a_usage_error_before_the_cli_starts(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        target = os.path.join(blocker, "state")
        os.environ["TAGTEAM_STATE_DIR"] = target
        code, out, err = self.go("claude")
        self.assertEqual(code, 2, out + err)
        self.assertIn(target, err)
        self.assertIn("TAGTEAM_STATE_DIR", err)
        self.assertIn("Codex: run this command with escalated permissions", err)
        self.assertFalse(os.path.exists(self.record))  # the CLI was never started

    def test_default_state_folder_is_in_the_main_work_tree_even_from_a_worktree(self):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        repo = os.path.join(self.tmp.name, "main")
        wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(repo)
        with mock.patch.dict(os.environ, env):
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"], ["worktree", "add", "-q", wt, "-b", "w"]):
                subprocess.run(["git", "-C", repo] + args, check=True, capture_output=True)
        os.environ["TAGTEAM_STATE_DIR"] = ""
        run.statelib._cache.clear()
        code, out, err = self.go("claude", cwd=wt)
        run.statelib._cache.clear()
        self.assertEqual(code, 0, out + err)
        runs = os.path.join(repo, ".tagteam", "runs")
        self.assertEqual(len(os.listdir(runs)), 1)
        self.assertFalse(os.path.exists(os.path.join(wt, ".tagteam")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "tagteam")))


class CommandTests(unittest.TestCase):
    def build(self, vendor, read_only, windows=False, prompt="p", model_arg="m"):
        with mock.patch.object(run, "_is_windows", return_value=windows):
            return run.build_command(vendor, ["cli"], model_arg, "low", read_only, "/w", "/r", prompt, 90)

    def test_claude(self):
        base = ["cli", "-p", "--output-format", "json", "--model", "m", "--effort", "low", "--permission-mode"]
        self.assertEqual(self.build("claude", True), base + ["plan", "--permission-prompts", "none"])
        self.assertEqual(self.build("claude", False), base + ["auto", "--permission-prompts", "none"])

    def test_openai(self):
        last = os.path.join("/r", "last.txt")
        for ro, sandbox in ((True, "read-only"), (False, "workspace-write")):
            want = ["cli", "exec", "-m", "m", "-c", "model_reasoning_effort=low", "-s", sandbox, "-C", "/w", "-o", last]
            self.assertEqual(self.build("openai", ro), want + ["-"])
            self.assertEqual(self.build("openai", ro, windows=True), want + ["-c", "windows.sandbox=unelevated", "-"])

    def test_google(self):
        tail = ["--model", "m", "--effort", "low", "--mode"]
        log = os.path.join("/r", "agy.log")
        settings = '{"permissions":{"allow":["command(*)"]}}'
        # (read_only, run_commands) -> mode: plan mode never runs commands, so --run-commands switches it off
        for ro, rc, mode in ((True, False, "plan"), (True, True, "accept-edits"), (False, False, "accept-edits")):
            want = ["cli", "-p", "prompt"] + tail + [mode, "--output-format", "json", "--log-file", log,
                                                     "--print-timeout", "90s", "--settings", settings]
            got = run.build_command("google", ["cli"], "m", "low", ro, "/w", "/r", "prompt", 90, None, rc)
            self.assertEqual(got, want, (ro, rc))

    def test_google_allow_commands_replace_wildcard(self):
        cmd = run.build_command("google", ["cli"], "m", "low", False, "/w", "/r", "p", 90, ["git log -1", 'echo "x"'])
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings, {"permissions": {"allow": ["command(git log -1)", 'command(echo "x")']}})

    def test_agy_settings_survive_the_file_hint_and_are_limited(self):
        cmd = self.build("google", True, prompt=MARKER + "\n" + "x" * 30001)
        self.assertIn("prompt.txt", cmd[2])
        self.assertIn("--settings", cmd)
        with self.assertRaises(ValueError):
            run.build_command("google", ["cli"], "m", "low", True, "/w", "/r", "p", 90, ["c" * 40] * 800)

    def test_agy_long_prompt_uses_file(self):
        cmd = self.build("google", True, prompt=MARKER + "\n" + "x" * 30001)
        lines = cmd[2].split("\n")
        self.assertEqual(lines[0], MARKER)
        self.assertEqual(len(lines), 2)
        self.assertIn(os.path.join("/r", "prompt.txt"), lines[1])

    def test_agy_quoted_prompt_measured_as_command_line(self):
        cmd = self.build("google", True, prompt=MARKER + "\n" + '"' * 17000)
        self.assertEqual(len(cmd[2].split("\n")), 2)
        self.assertIn("prompt.txt", cmd[2])

    def test_codex_model_read_from_stderr_header_only(self):
        pick = lambda out, err: run.actual_model("openai", "gpt-6.1-terra", "low", None, None, out, err, "/r")
        answer = "model: gpt-6.1-terra\n"
        self.assertEqual(pick(answer, "model: gpt-other-1\nuser\n" + answer), ("gpt-other-1", False))
        self.assertEqual(pick("", "provider: x\nuser\n" + answer), (None, False))
        tail = "user\n" + "\n".join("line %d" % i for i in range(200)) + "\n"
        self.assertEqual(pick("", answer + tail), ("gpt-6.1-terra", True))
        self.assertEqual(pick("", "x\n" * 70 + answer + "user\n"), ("gpt-6.1-terra", True))


class LauncherTests(RunTestBase):
    def test_worker_marker_and_env(self):
        for vendor in ("claude", "openai"):
            self.assertEqual(self.go(vendor, prompt="Task body\n")[0], 0)
            rec = self.recorded()
            self.assertEqual(rec["delegate_env"], "1")
            self.assertEqual(rec["stdin"], MARKER + "\nTask body\n")
        self.go("claude", prompt=MARKER + "\nTask body\n")
        self.assertEqual(self.recorded()["stdin"], MARKER + "\nTask body\n")
        self.go("google", prompt="Task body\n")
        self.assertEqual(self.recorded()["argv"][1], MARKER + "\nTask body\n")
        self.assertEqual(self.recorded()["delegate_env"], "1")

    def test_refuses_nested_launch(self):
        os.environ["HARNESS_DELEGATE_RUN"] = "1"
        code, _, err = self.go("claude")
        self.assertEqual(code, 2)
        self.assertIn("re-delegation", err)
        self.assertFalse(os.path.exists(self.record))

    def test_precheck_failures(self):
        self.assertEqual(self.go("claude", model="claude-unknown-9")[0], 2)
        self.assertEqual(self.go("claude", model="gpt-6.1-terra")[0], 2)
        self.assertEqual(self.go("claude", effort="ultra")[0], 2)
        with mock.patch.object(run.shutil, "which", return_value=None):
            os.environ.pop("TAGTEAM_CLI_CLAUDE")
            self.assertEqual(self.go("claude")[0], 2)
        self.assertFalse(os.path.exists(self.record))

    def test_success_report_and_files(self):
        for vendor in VENDORS:
            code, out, _ = self.go(vendor)
            self.assertEqual(code, 0, out)
            model = VENDORS[vendor][0]
            lines = out.splitlines()
            self.assertRegex(lines[0], r"^RUN \d{8}-\d{6}-%s-[0-9a-f]{4} vendor=%s model=%s effort=low status=ok exit=0 secs=\d+$"
                             % (vendor, vendor, model.replace(".", r"\.")))
            shown = "Gemini 3.8 Flash (Low)" if vendor == "google" else model
            self.assertEqual(lines[1], "ACTUAL_MODEL: %s (match)" % shown)
            self.assertTrue(lines[2].startswith("OUTPUT: "))
            self.assertEqual(lines[3:], ["---", "OK"])
        self.assertEqual(len(self.run_dirs()), 3)
        d = self.run_dirs()[0]
        for name in ("prompt.txt", "cmd.json", "stdout.txt", "stderr.txt", "last.txt", "result.json"):
            self.assertTrue(os.path.exists(os.path.join(d, name)), name)
        google = [p for p in self.run_dirs() if "-google-" in p][0]
        self.assertTrue(os.path.exists(os.path.join(google, "agy.log")))

    def test_model_mismatch_and_missing(self):
        for vendor in VENDORS:
            for mode in ("wrong_model", "no_model_field"):
                code, out, _ = self.go(vendor, mode)
                self.assertEqual(code, 4, (vendor, mode, out))
                self.assertIn("status=model_mismatch", out)

    def test_crash_is_cli_failed(self):
        code, out, _ = self.go("openai", "crash")
        self.assertEqual(code, 1)
        self.assertIn("status=cli_failed", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "exhausted.json")))

    def test_empty_answer_is_no_output(self):
        code, out, _ = self.go("google", "empty")
        self.assertEqual(code, 6)
        self.assertIn("status=no_output", out)
        self.assertIn("denied tools: Bash", out)

    def test_codex_echoed_prompt_line_is_not_exhaustion(self):
        code, out, _ = self.go("openai", "crash", prompt="Handle this case:\n429 Too Many Requests\nthanks\n")
        self.assertEqual(code, 1, out)
        self.assertIn("status=cli_failed", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "exhausted.json")))

    def test_process_start_error_is_cli_failed(self):
        with mock.patch.object(run.subprocess, "Popen", side_effect=OSError("cannot start")):
            code, out, _ = self.go("claude")
        self.assertEqual(code, 1)
        self.assertIn("status=cli_failed", out)
        self.assertIn("cannot start", out)

    def test_exhausted_recorded(self):
        for vendor in VENDORS:
            code, out, _ = self.go(vendor, "exhausted")
            self.assertEqual(code, 3, (vendor, out))
            self.assertIn("EXHAUSTED: %s until " % vendor, out)
            item = self.exhausted()["vendors"][vendor]
            self.assertEqual(set(item), {"since", "until", "evidence", "source"})
            self.assertTrue(item["since"].endswith("Z") and item["until"].endswith("Z"))
            self.assertTrue(item["source"].startswith("run.py "))
            self.assertLessEqual(len(item["evidence"]), 200)
            self.assertEqual((run.parse_iso(item["until"]) - run.parse_iso(item["since"])).total_seconds(), 3600)
        self.assertEqual(self.exhausted()["schema"], 1)

    def test_exhausted_epoch(self):
        code, _, _ = self.go("claude", "exhausted_epoch")
        self.assertEqual(code, 3)
        want = datetime.fromtimestamp(1900000000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.exhausted()["vendors"]["claude"]["until"], want)

    def test_quota_text_in_output_is_not_exhaustion(self):
        for vendor in VENDORS:
            code, out, _ = self.go(vendor, "quota_text_in_prompt", prompt="Handle the 429 usage limit case.\n")
            self.assertEqual(code, 0, (vendor, out))
            self.assertFalse(os.path.exists(os.path.join(self.state, "exhausted.json")), vendor)

    def test_success_clears_vendor_entry(self):
        self.go("openai", "exhausted")
        self.go("claude", "exhausted")
        self.assertEqual(self.go("openai", "ok")[0], 0)
        self.assertEqual(list(self.exhausted()["vendors"]), ["claude"])

    def test_expired_entries_dropped_on_write(self):
        os.makedirs(self.state)
        old = {"since": "2020-01-01T00:00:00Z", "until": "2020-01-01T01:00:00Z", "evidence": "x", "source": "manual"}
        with open(os.path.join(self.state, "exhausted.json"), "w") as fh:
            json.dump({"schema": 1, "vendors": {"google": old}}, fh)
        self.go("claude", "exhausted")
        self.assertEqual(list(self.exhausted()["vendors"]), ["claude"])

    def test_timeout_kills_child(self):
        t0 = time.time()
        code, out, _ = self.go("claude", "sleep", extra=["--timeout", "2"])
        self.assertEqual(code, 5)
        self.assertIn("status=timeout", out)
        self.assertLess(time.time() - t0, 30)
        pid = self.recorded()["pid"]
        for _ in range(20):
            if not pid_alive(pid):
                break
            time.sleep(0.25)
        self.assertFalse(pid_alive(pid))

    def test_agy_prompt_over_limit_passes_file_hint(self):
        self.assertEqual(self.go("google", prompt="x" * 31000)[0], 0)
        shown = self.recorded()["argv"][1]
        self.assertEqual(len(shown.split("\n")), 2)
        self.assertTrue(shown.startswith(MARKER + "\nRead and follow the task in "))
        self.assertIn("prompt.txt", shown)
        d = self.run_dirs()[0]
        with open(os.path.join(d, "prompt.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), MARKER + "\n" + "x" * 31000)

    def test_report_truncates_long_output(self):
        with mock.patch.object(run, "REPORT_CHARS", 1):
            code, out, _ = self.go("claude")
        self.assertEqual(code, 0)
        self.assertIn("[truncated, see OUTPUT]", out)

    def settings_passed(self):
        argv = self.recorded()["argv"]
        return json.loads(argv[argv.index("--settings") + 1])

    def test_google_gets_default_command_allow_list(self):
        for ro in ((), ("--read-only",)):
            self.assertEqual(self.go("google", extra=ro)[0], 0)
            self.assertEqual(self.settings_passed(), {"permissions": {"allow": ["command(*)"]}})

    def test_google_allow_command_is_exact_list(self):
        extra = ["--allow-command", "git diff", "--allow-command", "python -m unittest"]
        self.assertEqual(self.go("google", extra=extra)[0], 0)
        self.assertEqual(self.settings_passed(), {"permissions": {"allow": ["command(git diff)", "command(python -m unittest)"]}})

    def test_google_modes(self):
        for extra, mode in ((["--read-only"], "plan"), (["--read-only", "--run-commands"], "accept-edits"), ([], "accept-edits")):
            self.assertEqual(self.go("google", extra=extra)[0], 0)
            argv = self.recorded()["argv"]
            self.assertEqual(argv[argv.index("--mode") + 1], mode, extra)
            self.assertEqual(self.settings_passed(), {"permissions": {"allow": ["command(*)"]}})

    def test_other_vendors_get_no_settings_and_reject_google_only_options(self):
        for vendor in ("claude", "openai"):
            self.assertEqual(self.go(vendor)[0], 0)
            self.assertNotIn("--settings", self.recorded()["argv"])
            os.remove(self.record)
            for extra in (["--allow-command", "git diff"], ["--run-commands"]):
                code, _, err = self.go(vendor, extra=extra)
                self.assertEqual(code, 2)
                self.assertIn("google only", err)
                self.assertFalse(os.path.exists(self.record))
        self.assertEqual(self.go("google", extra=["--allow-command", " "])[0], 2)

    def test_priority_puts_head_moved_after_exhaustion_only(self):
        order = [s for s, _ in run.PRIORITY]
        self.assertEqual(order[0], "exhausted")
        self.assertEqual(order[1], "git_head_moved")
        self.assertEqual(dict(run.PRIORITY)["git_head_moved"], 7)
        self.assertEqual(dict(run.PRIORITY)["wrote_files"], 8)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitCheckTests(RunTestBase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp.name, "repo")  # the state folder stays outside the work tree
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.commit("base")

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-C", cwd or self.repo] + list(args), check=True, capture_output=True)

    def commit(self, msg):
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", msg)

    def last_result(self):
        paths = [os.path.join(d, "result.json") for d in self.run_dirs()]
        with open(max(paths, key=os.path.getmtime), encoding="utf-8") as fh:
            return json.load(fh)

    def head(self):
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def test_commit_is_git_head_moved_for_every_vendor(self):
        for vendor in VENDORS:
            before = self.head()
            code, out, _ = self.go(vendor, "commit", cwd=self.repo)
            self.assertEqual(code, 7, (vendor, out))
            self.assertIn("status=git_head_moved", out)
            self.assertRegex(out, r"ERROR: git HEAD moved during the run \(\S+ %s -> \S+ %s\)" % (before[:7], self.head()[:7]))

    def test_branch_switch_at_the_same_commit_counts_as_moved(self):
        a = {"head": "a" * 40, "ref": "refs/heads/main", "status": [], "hashes": {}, "partial": False}
        b = dict(a, ref="refs/heads/other")
        flags, lines = run.git_compare(a, b, False)
        self.assertEqual(flags, {"git_head_moved"})
        self.assertIn("main aaaaaaa -> other aaaaaaa", lines[0])
        self.assertEqual(run.git_compare(a, dict(a, ref=""), False)[0], {"git_head_moved"})  # detached

    def test_priority_of_head_moved(self):
        before = {"head": "a" * 40, "ref": "", "status": [], "hashes": {}, "partial": False}
        after = dict(before, head="b" * 40, status=["?? x"], hashes={"x": "1"})
        self.assertEqual(run.git_compare(before, after, False)[0], {"git_head_moved"})
        self.assertEqual(run.git_compare(before, after, True)[0], {"git_head_moved", "wrote_files"})
        pick = lambda flags: next(c for s, c in run.PRIORITY if s in flags)
        self.assertEqual(pick({"git_head_moved", "timeout", "cli_failed", "model_mismatch"}), 7)
        self.assertEqual(pick({"git_head_moved", "exhausted"}), 3)

    def test_read_only_write_is_wrote_files(self):
        for vendor in VENDORS:
            code, out, _ = self.go(vendor, "write_file", cwd=self.repo, extra=["--read-only"])
            self.assertEqual(code, 8, (vendor, out))
            self.assertIn("status=wrote_files", out)
            self.assertIn("ERROR: a read-only run changed the work tree: ?? fake_written.txt", out)
            os.remove(os.path.join(self.repo, "fake_written.txt"))

    def test_read_only_overwrite_of_an_already_untracked_file(self):
        self.write("fake_written.txt", "old")
        code, out, _ = self.go("claude", "write_file", cwd=self.repo, extra=["--read-only"])
        self.assertEqual(code, 8, out)
        self.assertIn("content of fake_written.txt", out)

    def test_read_only_overwrite_of_an_already_modified_tracked_file(self):
        self.write("fake_written.txt", "a")
        self.git("add", "fake_written.txt")
        self.commit("add")
        self.write("fake_written.txt", "b")  # dirty before the run: the status listing will not change
        code, out, _ = self.go("openai", "write_file", cwd=self.repo, extra=["--read-only"])
        self.assertEqual(code, 8, out)
        self.assertIn("content of fake_written.txt", out)
        self.assertEqual(self.go("openai", cwd=self.repo, extra=["--read-only"])[0], 0)  # unchanged dirty file passes

    def test_write_without_read_only_is_allowed(self):
        code, out, _ = self.go("google", "write_file", cwd=self.repo)
        self.assertEqual(code, 0, out)
        self.assertNotIn("ERROR:", out)

    def test_clean_read_only_run_passes(self):
        for vendor in VENDORS:
            code, out, _ = self.go(vendor, cwd=self.repo, extra=["--read-only"])
            self.assertEqual(code, 0, (vendor, out))
            self.assertEqual(self.last_result()["git_check"], "ok")

    def test_launcher_own_files_inside_the_work_tree_are_ignored(self):
        os.environ["TAGTEAM_STATE_DIR"] = os.path.join(self.repo, "state")
        for vendor in VENDORS:
            code, out, _ = self.go(vendor, cwd=self.repo, extra=["--read-only"])
            self.assertEqual(code, 0, (vendor, out))
            self.assertNotIn("ERROR:", out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "state", "runs")))

    def test_commit_in_a_repository_without_commits(self):
        fresh = os.path.join(self.tmp.name, "fresh")
        os.makedirs(fresh)
        self.git("init", "-q", cwd=fresh)
        code, out, _ = self.go("claude", "commit", cwd=fresh)
        self.assertEqual(code, 7, out)
        self.assertIn(" none -> ", out)

    def test_not_a_git_work_tree_skips_the_check_silently(self):
        plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(plain)
        for mode, extra in (("write_file", ["--read-only"]), ("commit", [])):
            code, out, _ = self.go("google", mode, cwd=plain, extra=extra)
            self.assertEqual(code, 0, (mode, out))
            self.assertNotIn("ERROR:", out)
            self.assertNotIn("WARNING:", out)
            self.assertEqual(self.last_result()["git_check"], "skipped")

    def test_git_missing_skips_the_check(self):
        with mock.patch.object(run.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(run.git_snapshot(self.repo), (None, None))

    def test_git_failure_warns_and_does_not_fail_the_run(self):
        with mock.patch.object(run, "git_snapshot", return_value=(None, "git timed out")):
            code, out, _ = self.go("claude", cwd=self.repo, extra=["--read-only"])
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: git check unavailable (git timed out)", out)
        self.assertEqual(self.last_result()["git_check"], "unavailable")
        with mock.patch.object(run.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            self.assertEqual(run.git_snapshot(self.repo), (None, "git timed out"))

    def test_hash_limits_make_the_check_partial(self):
        for i in range(3):
            self.write("f%d.txt" % i, "x")
        with mock.patch.object(run, "HASH_MAX_PATHS", 2):
            snap, problem = run.git_snapshot(self.repo)
        self.assertIsNone(problem)
        self.assertTrue(snap["partial"])
        self.assertEqual(len(snap["hashes"]), 2)
        with mock.patch.object(run, "HASH_MAX_BYTES", 0):
            code, out, _ = self.go("claude", cwd=self.repo, extra=["--read-only"])
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: git content check is partial", out)


if __name__ == "__main__":
    unittest.main()
