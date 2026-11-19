import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS))
sys.path.insert(0, TESTS)
import fake_cli  # noqa: E402
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


def last_result(test):
    """result.json of the newest run of a RunTestBase test."""
    paths = [os.path.join(d, "result.json") for d in test.run_dirs()]
    with open(max(paths, key=os.path.getmtime), encoding="utf-8") as fh:
        return json.load(fh)


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
        self.assertEqual(out, "")  # no "started" line for a run that never started

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
            for sandbox, tail in ((None, ["-"]), ("elevated", ["-c", "windows.sandbox=elevated", "-"])):
                with mock.patch.object(run, "_is_windows", return_value=True):
                    got = run.build_command("openai", ["cli"], "m", "low", ro, "/w", "/r", "p", 90, sandbox=sandbox)
                self.assertEqual(got, want + tail)
                with mock.patch.object(run, "_is_windows", return_value=False):  # a Windows-only setting
                    got = run.build_command("openai", ["cli"], "m", "low", ro, "/w", "/r", "p", 90, sandbox=sandbox)
                self.assertEqual(got, want + ["-"])
            self.assertNotIn("--add-dir", self.build("openai", ro, windows=True))  # workers do not write the state folder

    def test_windows_sandbox_setting(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run.windows_sandbox(d), "unelevated")  # no models.local.json
            path = os.path.join(d, "models.local.json")
            for body, want in (("{broken", "unelevated"), ("[1]", "unelevated"), ('{"plans": {}}', "unelevated"),
                               ('{"codex": {}}', "unelevated"), ('{"codex": {"windows_sandbox": "elevated"}}', "elevated"),
                               ('{"codex": {"windows_sandbox": "unelevated"}}', "unelevated"),
                               ('{"codex": {"windows_sandbox": "mxc"}}', "mxc"),
                               ('{"codex": {"windows_sandbox": null}}', None), ('{"codex": {"windows_sandbox": ""}}', None)):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                self.assertEqual(run.windows_sandbox(d), want, body)
            # values Codex would reject at start: true, numbers, unknown words, other case, lists, padding
            for bad in ("true", "1", "123", '"123"', '"bogus"', '"true"', '"Elevated"', '" unelevated"',
                        '["unelevated"]', '{"v": 1}', '"a b"', '"x\\"; y"'):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('{"codex": {"windows_sandbox": %s}}' % bad)
                with self.assertRaises(ValueError, msg=bad) as ctx:
                    run.windows_sandbox(d)
                self.assertIn('"elevated", "unelevated", "mxc"', str(ctx.exception))

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

    def test_claude_host_env_removed_for_other_vendors(self):
        os.environ.update(CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="claude-desktop")
        for vendor in ("openai", "google"):
            self.assertEqual(self.go(vendor)[0], 0, vendor)
            rec = self.recorded()
            self.assertEqual((rec["claudecode"], rec["entrypoint"], rec["delegate_env"]), (None, None, "1"), vendor)
        self.assertEqual(self.go("claude")[0], 0)
        rec = self.recorded()
        self.assertEqual((rec["claudecode"], rec["entrypoint"]), ("1", "claude-desktop"))
        self.assertEqual(os.environ["CLAUDECODE"], "1")  # the launcher's own environment is unchanged

    def test_antigravity_id_removed_for_other_vendors(self):
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "cid-1"
        for vendor, want in (("claude", None), ("openai", None), ("google", "cid-1")):
            self.assertEqual(self.go(vendor)[0], 0, vendor)
            self.assertEqual(self.recorded()["antigravity"], want, vendor)
        self.assertEqual(os.environ["ANTIGRAVITY_CONVERSATION_ID"], "cid-1")  # the launcher's own environment is unchanged

    def test_started_json_exists_before_the_cli_starts_and_report_txt_holds_the_report(self):
        for vendor in ("openai", "google"):
            code, out, _ = self.go(vendor, extra=["--timeout", "77"])
            self.assertEqual(code, 0, out)
            run_dir = os.path.join(self.state, "runs", out.split()[1])
            started = json.loads(self.recorded()["started"])  # what the CLI saw when it started
            self.assertEqual(started, {"pid": os.getpid(), "started": started["started"], "vendor": vendor,
                                       "model": VENDORS[vendor][0], "effort": "low", "timeout": 77})
            self.assertRegex(started["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            with open(os.path.join(run_dir, "report.txt"), encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), out.split("\n", 1)[1])  # the report is everything after the started line
            with open(os.path.join(run_dir, "result.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["run_exit"], 0)
            self.assertEqual([n for n in os.listdir(run_dir) if n.endswith(".tmp")], [])

    def config_with_local(self, body):
        cfg = os.path.join(self.tmp.name, "cfg")
        os.makedirs(cfg, exist_ok=True)
        shutil.copy(os.path.join(FIXTURES, "models.json"), cfg)
        with open(os.path.join(cfg, "models.local.json"), "w", encoding="utf-8") as fh:
            fh.write(body)
        os.environ["TAGTEAM_CONFIG_DIR"] = cfg

    def test_windows_sandbox_from_models_local(self):
        self.config_with_local('{"codex": {"windows_sandbox": null}}')
        with mock.patch.object(run, "_is_windows", return_value=True):
            self.assertEqual(self.go("openai")[0], 0)
            self.assertFalse(any(a.startswith("windows.sandbox") for a in self.recorded()["argv"]))
            self.config_with_local('{"codex": {"windows_sandbox": "elevated"}}')
            self.assertEqual(self.go("openai")[0], 0)
            self.assertIn("windows.sandbox=elevated", self.recorded()["argv"])
            for bad in ("5", "true", '"bogus"'):
                if os.path.exists(self.record):
                    os.remove(self.record)
                self.config_with_local('{"codex": {"windows_sandbox": %s}}' % bad)
                code, out, err = self.go("openai")
                self.assertEqual((code, out), (2, ""), bad)
                self.assertIn("codex.windows_sandbox is %s; use one of" % bad, err)
                self.assertFalse(os.path.exists(self.record), bad)  # refused before the CLI starts
            self.assertEqual(self.go("claude")[0], 0)  # other vendors ignore the setting

    def test_run_id_is_printed_before_the_cli_starts(self):
        pf = os.path.join(self.tmp.name, "prompt.md")
        with open(pf, "w", encoding="utf-8") as fh:
            fh.write("Do it.\n")
        out_file = os.path.join(self.tmp.name, "launcher-stdout.txt")
        env = dict(os.environ, FAKE_MODE="ok", FAKE_PEEK=out_file)
        with open(out_file, "w", encoding="utf-8") as out:
            subprocess.run([sys.executable, os.path.join(os.path.dirname(TESTS), "run.py"), "--vendor", "openai",
                            "--model", VENDORS["openai"][0], "--effort", "low", "--prompt-file", pf,
                            "--cwd", self.tmp.name], stdout=out, stderr=subprocess.PIPE, env=env, timeout=120, check=True)
        seen = self.recorded()["peek"]  # what the launcher had printed when the CLI started
        self.assertRegex(seen, r"^RUN \S+-openai-[0-9a-f]{4} started run_dir=\S.* stdout=\S.*stdout\.txt stderr=\S.*stderr\.txt\n$")
        with open(out_file, encoding="utf-8") as fh:
            final = fh.read().splitlines()
        self.assertEqual(final[0], seen.rstrip("\n"))
        self.assertIn("status=ok", final[1])

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
            run_id = lines[0].split()[1]
            run_dir = os.path.join(self.state, "runs", run_id)
            self.assertEqual(lines[0], "RUN %s started run_dir=%s stdout=%s stderr=%s" % (
                run_id, run_dir, os.path.join(run_dir, "stdout.txt"), os.path.join(run_dir, "stderr.txt")))
            self.assertRegex(lines[1], r"^RUN \d{8}-\d{6}-%s-[0-9a-f]{4} vendor=%s model=%s effort=low status=ok exit=0 secs=\d+$"
                             % (vendor, vendor, model.replace(".", r"\.")))
            self.assertEqual(lines[1].split()[1], run_id)
            shown = "Gemini 3.8 Flash (Low)" if vendor == "google" else model
            self.assertEqual(lines[2], "ACTUAL_MODEL: %s (match)" % shown)
            self.assertTrue(lines[3].startswith("OUTPUT: "))
            self.assertEqual(lines[4:], ["---", "OK"])
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
class GitRepoBase(RunTestBase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp.name, "repo")  # the state folder stays outside the work tree
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.commit("base")

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-C", cwd or self.repo] + list(args), check=True, capture_output=True)

    def commit(self, msg, cwd=None):
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", msg, cwd=cwd)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitCheckTests(GitRepoBase):
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



class StatusTests(RunTestBase):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = run.main(list(argv))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def sleeper(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def make_run(self, run_id, pid=None, ago=5, timeout=1800, started=True):
        run_dir = os.path.join(self.state, "runs", run_id)
        os.makedirs(run_dir)
        if started:
            doc = {"pid": pid, "started": run.iso(run.utc_now() - timedelta(seconds=ago)), "vendor": "claude",
                   "model": "m", "effort": "low", "timeout": timeout}
            run.write_atomic(os.path.join(run_dir, "started.json"), json.dumps(doc))
        return run_dir

    def test_finished_run_repeats_its_report_and_exit_code(self):
        for vendor, mode, want in (("claude", "ok", 0), ("openai", "crash", 1), ("claude", "exhausted", 3),
                                   ("google", "wrong_model", 4)):
            with self.subTest(mode):
                code, out, _ = self.go(vendor, mode)
                self.assertEqual(code, want)
                first, report = out.split("\n", 1)
                run_id = first.split()[1]
                self.assertEqual(self.cli("--status", run_id), (want, report, ""))
                self.assertEqual(self.cli("--wait", run_id), (want, report, ""))

    def test_old_record_without_run_exit_takes_the_code_from_the_status_word(self):
        _, out, _ = self.go("openai", "crash")
        run_id = out.split()[1]
        path = os.path.join(self.state, "runs", run_id, "result.json")
        with open(path, encoding="utf-8") as fh:
            result = json.load(fh)
        del result["run_exit"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        self.assertEqual(self.cli("--status", run_id)[0], 1)

    def test_running(self):
        child = self.sleeper()
        run_dir = self.make_run("r-running", pid=child.pid, ago=7)
        code, out, _ = self.cli("--status", "r-running")
        self.assertEqual(code, 9)
        secs = int(re.fullmatch(r"RUN r-running running secs=(\d+) run_dir=%s\n" % re.escape(run_dir), out).group(1))
        self.assertIn(secs, (7, 8, 9))
        self.assertIsNone(child.poll())  # looking at a process does not end it

    def test_ended_without_a_report(self):
        child = self.sleeper()
        child.kill()
        child.wait()
        self.make_run("r-dead", pid=child.pid)
        self.make_run("r-nostart", started=False)
        for run_id in ("r-dead", "r-nostart"):
            self.assertEqual(self.cli("--status", run_id), (6, "RUN %s ended without a report\n" % run_id, ""))

    def test_a_live_pid_older_than_the_timeout_is_a_reused_pid(self):
        child = self.sleeper()
        self.make_run("r-old", pid=child.pid, ago=3000, timeout=1800)
        self.make_run("r-late", pid=child.pid, ago=100, timeout=1)  # inside the grace period: still running
        self.assertEqual(self.cli("--status", "r-old")[0], 6)
        self.assertEqual(self.cli("--status", "r-late")[0], 9)

    def test_wait_returns_when_the_report_appears_and_when_the_run_has_ended(self):
        child = self.sleeper()
        run_dir = self.make_run("r-wait", pid=child.pid)
        report = "RUN r-wait vendor=claude model=m effort=low status=ok exit=0 secs=1\n---\nOK\n"
        timer = threading.Timer(0.3, run.write_atomic, (os.path.join(run_dir, "report.txt"), report))
        timer.start()
        self.addCleanup(timer.cancel)
        t0 = time.monotonic()
        self.assertEqual(self.cli("--wait", "r-wait", "--secs", "9"), (0, report, ""))
        self.assertLess(time.monotonic() - t0, 4)
        dead = self.sleeper()
        dead.kill()
        dead.wait()
        self.make_run("r-gone", pid=dead.pid)
        t0 = time.monotonic()
        self.assertEqual(self.cli("--wait", "r-gone", "--secs", "9")[0], 6)
        self.assertLess(time.monotonic() - t0, 2)

    def test_wait_gives_up_after_secs(self):
        child = self.sleeper()
        self.make_run("r-slow", pid=child.pid)
        t0 = time.monotonic()
        code, out, _ = self.cli("--wait", "r-slow", "--secs", "1")
        self.assertEqual((code, out.startswith("RUN r-slow running secs=")), (9, True))
        self.assertTrue(0.9 <= time.monotonic() - t0 < 4)
        t0 = time.monotonic()
        self.assertEqual(self.cli("--wait", "r-slow", "--secs", "0")[0], 9)  # one look
        self.assertLess(time.monotonic() - t0, 0.9)

    def test_wait_default_and_limit(self):
        self.make_run("r-x", pid=None)
        with mock.patch.object(run, "wait_for", return_value=("x\n", 9)) as waited:
            self.assertEqual(self.cli("--wait", "r-x")[0], 9)
            self.assertEqual(waited.call_args[0][2], 8)
            self.assertEqual(self.cli("--wait", "r-x", "--secs", "9")[0], 9)
            self.assertEqual(waited.call_args[0][2], 9)
            for bad in ("10", "-1"):
                code, _, err = self.cli("--wait", "r-x", "--secs", bad)
                self.assertEqual(code, 2, bad)
                self.assertIn("--secs must be 0 to 9", err)
            self.assertEqual(waited.call_count, 2)

    def test_run_id_must_be_a_name_under_runs(self):
        outside = os.path.join(self.state, "outside")
        os.makedirs(os.path.join(self.state, "runs"))
        os.makedirs(outside)
        run.write_atomic(os.path.join(outside, "report.txt"), "RUN outside vendor=x status=ok\n")
        for bad in ("..", "../outside", "..\\outside", "a/b", "", ".", ".hidden", "x y", outside):
            for flag in ("--status", "--wait"):
                code, out, err = self.cli(flag, bad)
                self.assertEqual((code, out), (2, ""), (flag, bad))
                self.assertIn("invalid run id", err)
        code, out, err = self.cli("--status", "20261118-000000-claude-ffff")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no run 20261118-000000-claude-ffff under", err)

    def test_usage_errors(self):
        self.make_run("r-u", pid=None)
        full = ["--vendor", "claude", "--model", "m", "--effort", "low", "--prompt-file", "p"]
        for argv in (["--status", "r-u", "--vendor", "claude"], ["--status", "r-u", "--read-only"],
                     ["--status", "r-u", "--secs", "3"], ["--wait", "r-u", "--timeout", "5"],
                     ["--wait", "r-u", "--cwd", "."], ["--status", "r-u", "--wait", "r-u"],
                     ["--secs", "3"] + full, [], ["--vendor", "claude"], full[:-2]):
            with self.subTest(argv):
                code, out, _ = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.record))  # no CLI was started


class PidAliveTests(unittest.TestCase):
    def test_a_live_child_is_alive_and_stays_alive_a_dead_one_is_not(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        for _ in range(3):
            self.assertTrue(run.pid_alive(proc.pid))
            self.assertIsNone(proc.poll())
        proc.kill()
        proc.wait()
        self.assertFalse(run.pid_alive(proc.pid))

    def test_values_that_are_not_pids(self):
        for bad in (None, 0, -5, "12", 1.5, [], {}):
            self.assertFalse(run.pid_alive(bad), bad)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_never_calls_os_kill(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        with mock.patch.object(run.os, "kill", side_effect=AssertionError("os.kill must not be used on Windows")):
            self.assertTrue(run.pid_alive(proc.pid))
            self.assertTrue(run.pid_alive(os.getpid()))
        self.assertIsNone(proc.poll())

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_posix_sends_signal_zero(self):
        for effect, want in ((None, True), (PermissionError(), True), (ProcessLookupError(), False)):
            with mock.patch.object(run.os, "kill", side_effect=effect) as kill:
                self.assertEqual(run.pid_alive(4242), want)
                kill.assert_called_once_with(4242, 0)


class HookCopyTests(GitRepoBase):
    TAGTEAM = {"PreToolUse": [{"matcher": "run_command",
                               "hooks": [{"type": "command", "command": "python '/main/role_guard.py' PreToolUse"}]}]}

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.tmp.name, "wt")
        self.git("worktree", "add", "-q", self.wt, "-b", "side")
        self.put(self.repo, {"tagteam": self.TAGTEAM, "mine": 1})

    @staticmethod
    def put(root, doc):
        os.makedirs(os.path.join(root, ".agents"), exist_ok=True)
        with open(os.path.join(root, ".agents", "hooks.json"), "w", encoding="utf-8") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))

    @staticmethod
    def hooks(root):
        path = os.path.join(root, ".agents", "hooks.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def copied_to(self, out):
        lines = [ln for ln in out.splitlines() if ln.startswith("HOOKS copied to ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0][len("HOOKS copied to "):]

    def test_worktree_gets_only_the_tagteam_key_before_the_git_snapshot(self):
        self.put(self.wt, {"other": 2})
        code, out, _ = self.go("google", cwd=self.wt, extra=["--read-only"])
        self.assertEqual(code, 0, out)  # a copy made after the first snapshot would be a read-only run's write (exit 8)
        self.assertTrue(os.path.samefile(self.copied_to(out), os.path.join(self.wt, ".agents", "hooks.json")))
        self.assertEqual(self.hooks(self.wt), {"other": 2, "tagteam": self.TAGTEAM})
        self.assertEqual(self.hooks(self.repo), {"tagteam": self.TAGTEAM, "mine": 1})
        self.assertLess(out.index("HOOKS copied"), out.index("OUTPUT:"))

    def test_the_copy_exists_when_the_first_snapshot_is_taken(self):
        seen = []
        path = os.path.join(self.wt, ".agents", "hooks.json")

        def snapshot(cwd, exclude=()):
            seen.append(os.path.exists(path))
            return None, None
        with mock.patch.object(run, "git_snapshot", side_effect=snapshot):
            self.assertEqual(self.go("google", cwd=self.wt)[0], 0)
        self.assertEqual(seen, [True, True])
        self.assertEqual(self.hooks(self.wt), {"tagteam": self.TAGTEAM})  # no file before: created

    def test_a_tracked_hooks_file_is_left_alone(self):
        self.put(self.wt, {"theirs": 3})
        self.git("add", ".agents/hooks.json", cwd=self.wt)
        self.commit("hooks", cwd=self.wt)
        code, out, _ = self.go("google", cwd=self.wt, extra=["--read-only"])
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: agy hooks not copied (.agents/hooks.json is tracked)", out)
        self.assertNotIn("HOOKS copied", out)
        self.assertEqual(self.hooks(self.wt), {"theirs": 3})

    def test_a_hooks_file_that_is_not_a_json_object_is_left_alone(self):
        for body in ("{broken", "[1]"):
            with self.subTest(body):
                self.put(self.wt, body)
                code, out, _ = self.go("google", cwd=self.wt)
                self.assertEqual(code, 0, out)
                self.assertRegex(out, r"WARNING: agy hooks not copied \(\.agents/hooks\.json: ")
                with open(os.path.join(self.wt, ".agents", "hooks.json"), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), body)

    def test_nothing_is_copied_when_the_conditions_do_not_hold(self):
        sub = os.path.join(self.repo, "deep", "er")
        os.makedirs(sub)
        plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(plain)
        wt_hooks = os.path.join(self.wt, ".agents", "hooks.json")
        cases = (("vendor claude", "claude", self.wt), ("vendor openai", "openai", self.wt),
                 ("main checkout", "google", self.repo), ("folder of the main checkout", "google", sub),
                 ("outside git", "google", plain))
        for name, vendor, cwd in cases:
            with self.subTest(name):
                code, out, _ = self.go(vendor, cwd=cwd)
                self.assertEqual(code, 0, out)
                self.assertNotIn("HOOKS", out)
                self.assertFalse(os.path.exists(wt_hooks))
        for name, doc in (("no tagteam key", {"mine": 1}), ("not JSON", "{broken"), ("no file", None)):
            with self.subTest(name):
                if doc is None:
                    os.remove(os.path.join(self.repo, ".agents", "hooks.json"))
                else:
                    self.put(self.repo, doc)
                code, out, _ = self.go("google", cwd=self.wt)
                self.assertEqual(code, 0, out)
                self.assertNotIn("HOOKS", out)
                self.assertNotIn("WARNING", out)
                self.assertFalse(os.path.exists(wt_hooks))

    def link(self, target, link, directory):
        """A symbolic link, or on Windows without the privilege a junction (directories only); else skip the test."""
        try:
            os.symlink(target, link, target_is_directory=directory)
            return
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt" and directory and subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                                                            capture_output=True).returncode == 0:
            return
        self.skipTest("cannot create links here")

    def test_a_link_to_a_tracked_file_is_not_written_through(self):
        os.makedirs(os.path.join(self.wt, "config"))
        tracked = os.path.join(self.wt, "config", "hooks.json")
        with open(tracked, "w", encoding="utf-8") as fh:
            fh.write('{"theirs": 3}')
        self.git("add", "config/hooks.json", cwd=self.wt)
        self.commit("config", cwd=self.wt)
        self.link(os.path.join(self.wt, "config"), os.path.join(self.wt, ".agents"), True)  # .agents -> tracked config/
        code, out, _ = self.go("google", cwd=self.wt)
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: agy hooks not copied (.agents/hooks.json is a link or resolves elsewhere)", out)
        self.assertNotIn("HOOKS copied", out)
        self.assertEqual(self.hooks(self.wt), {"theirs": 3})

    def test_a_hooks_file_that_is_a_link_is_not_written_through(self):
        other = os.path.join(self.wt, "other.json")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write('{"theirs": 4}')
        self.git("add", "other.json", cwd=self.wt)
        self.commit("other", cwd=self.wt)
        os.makedirs(os.path.join(self.wt, ".agents"))
        self.link(other, os.path.join(self.wt, ".agents", "hooks.json"), False)
        code, out, _ = self.go("google", cwd=self.wt)
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: agy hooks not copied (.agents/hooks.json is a link or resolves elsewhere)", out)
        with open(other, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"theirs": 4})

    def test_a_file_the_system_calls_a_link_is_not_written_even_when_no_link_can_be_made(self):
        dst = os.path.realpath(os.path.join(self.wt, ".agents", "hooks.json"))  # git names the work tree by its real path
        real_islink = os.path.islink
        with mock.patch.object(run.os.path, "islink", side_effect=lambda p: real_islink(p) or os.path.realpath(p) == dst):
            code, out, _ = self.go("google", cwd=self.wt)
        self.assertEqual(code, 0, out)
        self.assertIn("is a link or resolves elsewhere", out)
        self.assertFalse(os.path.exists(dst))

    def test_the_copied_hooks_are_not_a_control_plane_change_but_a_worker_edit_of_them_is(self):
        code, out, _ = self.go("google", cwd=self.wt)  # a write run: the baseline is taken after the copy
        self.assertEqual(code, 0, out)
        self.copied_to(out)
        self.assertEqual(last_result(self)["control_plane"], {"changed": [], "main_changed": []})
        os.environ["FAKE_WRITE"] = json.dumps({".agents/hooks.json": "{}"})
        code, out, _ = self.go("google", cwd=self.wt)
        self.assertEqual(code, 10, out)
        self.assertIn("the control plane changed during the run (.agents/hooks.json)", out)

    def make_submodule(self):
        """The repository gets a submodule libs/sub; both the superproject and the submodule have their own hooks."""
        sub = os.path.join(self.tmp.name, "sub-origin")
        os.makedirs(sub)
        self.git("init", "-q", cwd=sub)
        self.commit("sub", cwd=sub)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "libs/sub")
        self.commit("add submodule")
        inner = os.path.join(self.repo, "libs", "sub")
        self.put(inner, {"tagteam": {"SessionStart": [{"type": "command", "command": "sub-guard"}]}})
        return inner

    def test_a_plain_submodule_is_not_a_linked_worktree(self):
        inner = self.make_submodule()
        before = self.hooks(inner)
        for cwd in (inner, os.path.join(inner, "x")):
            os.makedirs(cwd, exist_ok=True)
            code, out, _ = self.go("google", cwd=cwd)
            self.assertEqual(code, 0, out)
            self.assertNotIn("HOOKS", out)
            self.assertNotIn("WARNING", out)
        self.assertEqual(self.hooks(inner), before)  # the superproject's hooks were not copied into it

    def test_a_worktree_of_a_submodule_gets_the_hooks_of_the_submodule_checkout(self):
        inner = self.make_submodule()
        wt = os.path.join(self.tmp.name, "subwt")
        self.git("worktree", "add", "-q", wt, "-b", "w", cwd=inner)
        code, out, _ = self.go("google", cwd=wt)
        self.assertEqual(code, 0, out)
        self.copied_to(out)
        self.assertEqual(self.hooks(wt), self.hooks(inner))  # not the superproject's {"tagteam": TAGTEAM, ...}
        self.assertNotEqual(self.hooks(wt)["tagteam"], self.TAGTEAM)


class ControlPlaneTests(GitRepoBase):
    """The control-plane check: what a run changed in .claude/, .codex/, .agents/, AGENTS.md, CLAUDE.md."""
    IGNORED = run.CONTROL_HOOK_FILES + run.CONTROL_SETTING_FILES  # machine-local files, ignored by git

    def setUp(self):
        super().setUp()
        self.put("AGENTS.md", "a\n")
        self.git("add", "AGENTS.md")
        self.commit("agents")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
            fh.write("\n".join(self.IGNORED) + "\n")
        self.reset()

    def put(self, rel, text, root=None):
        path = os.path.join(root or self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def reset(self):
        self.git("checkout", "--", "AGENTS.md")
        self.git("clean", "-fdq")
        for rel in self.IGNORED:
            self.put(rel, "{}")

    def writes(self, files, vendor="claude", mode="ok", cwd=None, extra=()):
        os.environ["FAKE_WRITE"] = json.dumps(files)
        try:
            return self.go(vendor, mode, cwd=cwd or self.repo, extra=extra)
        finally:
            os.environ.pop("FAKE_WRITE", None)

    def test_the_lists_are_role_guards_and_lower_case(self):
        import role_guard
        self.assertIs(run.CONTROL_DIRS, role_guard.CONTROL_DIRS)
        self.assertIs(run.CONTROL_FILES, role_guard.CONTROL_FILES)
        for name in self.IGNORED:
            self.assertEqual(name, name.lower())
            self.assertTrue(run.control_path(name), name)

    def test_which_paths_are_the_control_plane(self):
        for rel in (".claude/x", ".Claude/x", ".codex/a/b", ".agents/hooks.json", "AGENTS.md", "Agents.md", "claude.MD",
                    ".claude/tagteam/role_guard.py", ".claude\\x"):
            self.assertTrue(run.control_path(rel), rel)
        for rel in ("src/a.py", "docs/AGENTS.md", "x/.claude/y", "notes.md", ".claudex/y", "AGENTS.md.bak",
                    ".claude/tagteam/__pycache__/role_guard.cpython-39.pyc", ".codex/z.pyc", ".claude/worktrees/s1/a.txt",
                    ".Claude/Worktrees/s1", ".claude/worktrees"):
            self.assertFalse(run.control_path(rel), rel)

    def test_a_write_run_that_changes_the_control_plane_ends_with_exit_10(self):
        cases = (("untracked file", {".claude/x.txt": "1"}), ("tracked file", {"AGENTS.md": "b\n"}),
                 ("ignored hook file", {".agents/hooks.json": '{"a": 1}'}), ("codex hook file", {".codex/hooks.json": "[]"}),
                 ("new folder", {".codex/a/b.txt": "1"}), ("several", {".claude/x.txt": "1", ".agents/hooks.json": "[]"}))
        for name, files in cases:
            with self.subTest(name):
                code, out, _ = self.writes(files)
                self.assertEqual(code, 10, out)
                self.assertIn("status=control_plane_changed", out)
                self.assertRegex(out, r"ERROR: the control plane changed during the run \(.*%s" % re.escape(sorted(files)[0]))
                self.assertEqual(last_result(self)["control_plane"], {"changed": sorted(files), "main_changed": []})
                self.assertEqual(last_result(self)["run_exit"], 10)
                self.reset()

    def test_every_vendor_is_checked(self):
        for vendor in VENDORS:
            code, out, _ = self.writes({".claude/x.txt": "1"}, vendor=vendor)
            self.assertEqual(code, 10, (vendor, out))
            self.reset()

    def test_changes_made_before_the_run_are_not_findings_but_a_further_change_is(self):
        self.put("AGENTS.md", "dirty\n")
        self.put(".claude/old.txt", "1")
        self.put(".agents/hooks.json", '{"mine": 1}')
        code, out, _ = self.go("claude", cwd=self.repo)
        self.assertEqual(code, 0, out)
        self.assertNotIn("ERROR", out)
        self.assertEqual(last_result(self)["control_plane"]["changed"], [])
        code, out, _ = self.writes({"AGENTS.md": "dirtier\n"})
        self.assertEqual(code, 10, out)
        self.assertEqual(last_result(self)["control_plane"]["changed"], ["AGENTS.md"])

    def test_a_read_only_run_gets_no_new_exit_code(self):
        code, out, _ = self.writes({".codex/hooks.json": "[]", ".claude/settings.local.json": '{"x": 1}'}, extra=["--read-only"])
        self.assertEqual(code, 0, out)  # ignored machine-local files never make wrote_files
        self.assertIn("status=ok", out)
        self.assertRegex(out, r"WARNING: \.claude/settings\.local\.json, \.codex/hooks\.json changed during the run")
        self.assertNotIn("ERROR", out)
        code, out, _ = self.writes({".claude/x.txt": "1"}, extra=["--read-only"])
        self.assertEqual(code, 8, out)  # what `git status` shows is wrote_files, as before
        self.assertIn("status=wrote_files", out)
        self.assertIn("ERROR: a read-only run changed the control plane (.claude/x.txt)", out)
        self.assertNotIn("control_plane_changed", out)

    def test_setting_files_only_warn(self):
        files = {".claude/settings.local.json": '{"permissions": {}}', ".claude/tagteam/models.local.json": '{"a": 1}'}
        code, out, _ = self.writes(files)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"WARNING: \.claude/settings\.local\.json, \.claude/tagteam/models\.local\.json changed during "
                              r"the run; the host \(an \"always allow\" approval\) or the model collector")
        self.assertNotIn("ERROR", out)
        self.assertEqual(last_result(self)["control_plane"]["changed"], sorted(files))

    def test_python_caches_and_session_worktrees_are_not_findings(self):
        files = {".claude/tagteam/__pycache__/role_guard.cpython-39.pyc": "x", ".claude/worktrees/s1/a.txt": "1",
                 ".codex/z.pyc": "x"}
        code, out, _ = self.writes(files)
        self.assertEqual(code, 0, out)
        self.assertEqual(last_result(self)["control_plane"], {"changed": [], "main_changed": []})

    def test_the_names_are_compared_in_lower_case(self):
        code, out, _ = self.writes({"Agents.md": "x\n"})  # a new file here, AGENTS.md itself on a case-insensitive disk
        self.assertEqual(code, 10, out)
        self.assertIn("agents.md", out.lower())

    def test_the_original_path_of_a_rename_is_kept(self):
        self.git("mv", "AGENTS.md", "notes.md")  # the status entry names notes.md; AGENTS.md is the control file
        self.assertEqual(run.git_snapshot(self.repo)[0]["control"].get("AGENTS.md"), "missing")

    def test_the_main_checkout_of_a_linked_worktree_only_warns(self):
        wt = os.path.join(self.tmp.name, "wt")
        self.git("worktree", "add", "-q", wt, "-b", "side")
        other = os.path.join(self.repo, ".claude", "x.txt")
        code, out, _ = self.writes({other: "1", os.path.join(self.repo, ".agents", "hooks.json"): "[]"}, cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"WARNING: the main checkout's control plane changed during the run "
                              r"\(\.agents/hooks\.json, \.claude/x\.txt\); another process may have done it")
        self.assertEqual(last_result(self)["control_plane"],
                         {"changed": [], "main_changed": [".agents/hooks.json", ".claude/x.txt"]})
        code, out, _ = self.writes({".claude/y.txt": "1"}, cwd=wt)  # the worktree's own control plane is checked
        self.assertEqual(code, 10, out)
        self.assertEqual(last_result(self)["control_plane"], {"changed": [".claude/y.txt"], "main_changed": []})

    def test_a_control_file_swapped_for_a_link_into_the_state_folder_is_seen(self):
        state = os.path.join(self.repo, ".tagteam")  # the state folder is inside the work tree, so it is left out
        os.environ["TAGTEAM_STATE_DIR"] = state
        self.state = state  # last_result() reads the folder the run really used
        os.makedirs(state)
        target = os.path.join(state, "replacement.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("evil\n")
        probe = os.path.join(self.tmp.name, "probe")
        try:
            os.symlink(target, probe)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create links here")
        os.environ["FAKE_LINK"] = json.dumps({"AGENTS.md": target})
        code, out, _ = self.go("claude", cwd=self.repo)
        self.assertEqual(code, 10, out)
        self.assertIn("status=control_plane_changed", out)
        self.assertIn("AGENTS.md", last_result(self)["control_plane"]["changed"])  # macOS also lists "AGENTS.md (link)"
        self.assertNotIn(".tagteam", next(ln for ln in out.splitlines() if ln.startswith("ERROR:")))  # only the swap

    def test_exclusions_follow_the_path_git_reports_not_its_resolved_target(self):  # needs no link: realpath is faked
        root = os.path.realpath(self.repo)
        skip = [os.path.normcase(os.path.join(root, ".tagteam"))]
        real = os.path.realpath
        into_state = lambda p: os.path.join(root, ".tagteam", "replacement.md") if p.endswith("AGENTS.md") else real(p)
        with mock.patch.object(run.os.path, "realpath", side_effect=into_state):
            lines, paths, _ = run._parse_status(" T AGENTS.md\0?? .tagteam/runs/x/stdout.txt\0", root, skip)
        self.assertEqual(lines, [" T AGENTS.md"])  # the swapped control file stays, the state folder's own file goes

    def test_the_content_hash_of_control_files_has_no_budget(self):
        with mock.patch.object(run, "HASH_MAX_BYTES", 10):  # the general check is capped; this one must not be
            big = "x" * 100
            self.put(".codex/hooks.json", big)
            code, out, _ = self.writes({".codex/hooks.json": big + "y"})
        self.assertEqual(code, 10, out)
        self.assertEqual(last_result(self)["control_plane"]["changed"], [".codex/hooks.json"])

    def test_a_budget_shift_cannot_turn_an_untouched_file_into_a_change(self):
        self.put("AGENTS.md", "dirty " * 20)  # modified before the run and left alone
        self.put(".claude/settings.local.json", "{" + " " * 200 + "}")
        with mock.patch.object(run, "HASH_MAX_BYTES", 300):
            code, out, _ = self.writes({".claude/settings.local.json": "{}"})
        self.assertEqual(code, 0, out)  # only the setting file changed: a warning
        self.assertEqual(last_result(self)["control_plane"]["changed"], [".claude/settings.local.json"])

    def test_a_file_that_could_not_be_read_is_never_a_proven_change_and_makes_the_check_incomplete(self):
        def snap(digest):
            return {"control": {".codex/hooks.json": digest, "AGENTS.md": "d"}}
        for before, after in (("a", "unreadable"), ("unreadable", "a"), ("unreadable", "unreadable")):
            flags, lines, record = run.control_compare(snap(before), snap(after), None, None, False, False)
            self.assertEqual((flags, record["changed"]), (set(), []), (before, after))
            self.assertEqual(lines, ["WARNING: the control-plane check is incomplete: .codex/hooks.json could not be read"])
        flags, lines, _ = run.control_compare(snap("a"), snap("b"), None, None, False, False)
        self.assertEqual(flags, {"control_plane_changed"})

    def test_a_control_folder_replaced_by_a_link_to_an_identical_one_is_seen(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere", ".codex")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "hooks.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")  # the same content as the ignored .codex/hooks.json: no file digest differs
        try:
            fake_cli.replace_by_link(os.path.join(self.tmp.name, "probe"), elsewhere)
        except OSError:
            self.skipTest("cannot create links or junctions here")
        os.environ["FAKE_LINK"] = json.dumps({os.path.join(self.repo, ".codex"): elsewhere})
        code, out, _ = self.go("claude", cwd=self.repo)
        self.assertEqual(code, 10, out)
        self.assertIn("status=control_plane_changed", out)
        # a junction (Windows) shows only the link key; a symlink (macOS, Linux) also lists the folder git reports as changed
        self.assertIn(".codex (link)", last_result(self)["control_plane"]["changed"])
        self.assertNotIn(".tagteam", next(ln for ln in out.splitlines() if ln.startswith("ERROR:")))

    def test_a_file_over_the_cap_is_compared_by_size_and_time(self):
        self.put(".codex/hooks.json", "x" * 100)
        with mock.patch.object(run, "CONTROL_HASH_CAP", 50):
            code, out, _ = self.go("claude", cwd=self.repo)  # equal: a warning, never a change
            self.assertEqual(code, 0, out)
            self.assertIn("WARNING: the control-plane check for .codex/hooks.json compared size and time only", out)
            code, out, _ = self.writes({".codex/hooks.json": "y" * 101})
            self.assertEqual(code, 10, out)  # a different size is a change
        snap = run.control_digests(os.path.realpath(self.repo), [])
        self.assertRegex(snap[".codex/hooks.json"], r"^[0-9a-f]{64}$")  # under the real cap it is read
        flags, lines, _ = run.control_compare({"control": {"AGENTS.md": "stat:5:1"}}, {"control": {"AGENTS.md": "ab"}},
                                              None, None, False, False)
        self.assertEqual(flags, {"control_plane_changed"})  # leaving the cap changes the file, so it is a change

    def test_outside_a_work_tree_only_the_machine_local_files_are_checked(self):
        plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(os.path.join(plain, ".agents"))
        with open(os.path.join(plain, ".agents", "hooks.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        code, out, _ = self.writes({".claude/x.txt": "1", ".claude/settings.local.json": "{}"}, cwd=plain)
        self.assertEqual(code, 0, out)  # no git status to see .claude/x.txt; a setting file only warns
        self.assertIn("WARNING: .claude/settings.local.json changed during the run", out)
        self.assertNotIn("ERROR", out)
        code, out, _ = self.writes({".agents/hooks.json": "[]"}, cwd=plain)
        self.assertEqual(code, 10, out)
        self.assertEqual(last_result(self)["control_plane"], {"changed": [".agents/hooks.json"], "main_changed": []})
        self.assertEqual(last_result(self)["git_check"], "skipped")
        code, out, _ = self.writes({".agents/hooks.json": "{}"}, cwd=plain, extra=["--read-only"])
        self.assertEqual(code, 0, out)  # a read-only run only warns
        self.assertIn("WARNING: .agents/hooks.json changed during the run", out)

    def test_priority_of_the_new_statuses(self):
        order = [s for s, _ in run.PRIORITY]
        self.assertEqual(order[:4], ["exhausted", "git_head_moved", "control_plane_changed", "hooks_not_run"])
        self.assertLess(order.index("hooks_not_run"), order.index("timeout"))  # refused (12) sits between them
        self.assertEqual((dict(run.PRIORITY)["control_plane_changed"], dict(run.PRIORITY)["hooks_not_run"]), (10, 11))
        code, out, _ = self.writes({".claude/x.txt": "1"}, mode="commit")
        self.assertEqual(code, 7, out)  # a commit outweighs a control-plane change
        self.assertIn("ERROR: the control plane changed during the run", out)  # whose line is still printed


class CodexHookTests(GitRepoBase):
    """A Codex worker whose hooks did not run left no role record: hooks_not_run."""
    UNTRUSTED = "warning: Untrusted project-local configuration is disabled. Trust this project to load its hooks.\n"

    def codex(self, session="none", mode="ok", cwd=None, extra=(), stderr=""):
        os.environ["FAKE_SESSION"] = session
        os.environ["FAKE_STDERR"] = stderr
        return self.go("openai", mode, cwd=cwd or self.repo, extra=extra)

    def during(self, host, ts=None, body=None):
        """Another process writes a record while the run is going (before the CLI starts, after the baseline)."""
        real = run.execute

        def execute(*args, **kwargs):
            folder = os.path.join(self.state, "sessions")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "other.json"), "w", encoding="utf-8") as fh:
                fh.write(body if body is not None else json.dumps({"role": "worker", "host": host, "ts": ts or int(time.time())}))
            return real(*args, **kwargs)
        return mock.patch.object(run, "execute", side_effect=execute)

    def test_a_record_of_the_run_passes(self):
        for extra in ((), ("--read-only",)):
            code, out, _ = self.codex("codex", extra=extra)
            self.assertEqual(code, 0, out)
            self.assertIn("status=ok", out)

    def test_no_record_is_hooks_not_run_even_for_a_read_only_review(self):
        for extra in ((), ("--read-only",)):
            code, out, _ = self.codex(extra=extra)
            self.assertEqual(code, 11, out)
            self.assertIn("status=hooks_not_run exit=0", out)
            top = os.path.realpath(self.repo)
            self.assertIn("ERROR: no tagteam role record appeared for this Codex run: the project's hooks did not run "
                          "(or could not write the record). Codex skips project hooks until the project is trusted: "
                          "run codex once in %s, trust the project, then trust the hooks with /hooks; "
                          "`install.py %s --check --host codex --live` confirms it. The worker ran without the role "
                          "guard; review its changes." % (top, top), out)
            self.assertNotIn("CODEX:", out)
            self.assertEqual(last_result(self)["run_exit"], 11)

    def test_records_that_are_not_this_runs(self):
        folder = os.path.join(self.state, "sessions")
        os.makedirs(folder)
        with open(os.path.join(folder, "old.json"), "w", encoding="utf-8") as fh:  # there before the run
            json.dump({"role": "worker", "host": "codex", "ts": int(time.time())}, fh)
        self.assertEqual(self.codex()[0], 11)
        with self.during("claude"):  # another host
            self.assertEqual(self.codex()[0], 11)
        with self.during("codex", ts=1):  # made before the run started
            self.assertEqual(self.codex()[0], 11)

    def test_a_record_made_by_another_process_counts(self):  # a false negative that is accepted (design 4.2)
        with self.during("codex"):
            code, out, _ = self.codex()
        self.assertEqual(code, 0, out)

    def test_only_a_clean_exit_is_judged(self):
        code, out, _ = self.codex(mode="crash")
        self.assertEqual(code, 1, out)
        self.assertIn("status=cli_failed", out)
        self.assertNotIn("hooks_not_run", out)
        self.assertNotIn("no tagteam role record", out)
        code, out, _ = self.codex(mode="wrong_model")  # judged, and 11 outranks model_mismatch (4)
        self.assertEqual(code, 11, out)

    def test_a_folder_or_a_record_that_cannot_be_read_only_warns(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "sessions"), "w", encoding="utf-8") as fh:  # a file where the folder belongs
            fh.write("x")
        code, out, _ = self.codex()
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: hook check unavailable", out)
        os.remove(os.path.join(self.state, "sessions"))
        with self.during("codex", body="{broken"):
            code, out, _ = self.codex()
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: hook check unavailable", out)

    def test_the_trust_warning_of_codex_is_carried_over_when_the_check_fails(self):
        code, out, _ = self.codex(stderr=self.UNTRUSTED)
        self.assertEqual(code, 11, out)
        self.assertIn("CODEX: " + self.UNTRUSTED.strip(), out)
        code, out, _ = self.codex("codex", stderr=self.UNTRUSTED)
        self.assertEqual(code, 0, out)
        self.assertNotIn("CODEX:", out)

    def test_other_vendors_are_not_judged(self):
        for vendor in ("claude", "google"):
            code, out, _ = self.go(vendor, cwd=self.repo)
            self.assertEqual(code, 0, (vendor, out))
            self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))

    def test_in_a_linked_worktree_it_is_judged_like_the_main_checkout(self):  # I20: Codex runs the main checkout's hooks
        wt = os.path.join(self.tmp.name, "wt")
        self.git("worktree", "add", "-q", wt, "-b", "side")
        code, out, _ = self.codex(cwd=wt, stderr=self.UNTRUSTED)
        self.assertEqual(code, 11, out)
        self.assertIn("status=hooks_not_run", out)
        main = os.path.realpath(self.repo)
        self.assertIn("run codex once in %s," % main, out)  # the project to trust is the main checkout
        self.assertIn("CODEX: " + self.UNTRUSTED.strip(), out)
        code, out, _ = self.codex("codex", cwd=wt)  # a record of the run: ok
        self.assertEqual(code, 0, out)
        self.assertIn("status=ok", out)
        self.assertNotIn("WARNING: no tagteam role record", out)

    def test_a_control_plane_change_outranks_it_and_both_lines_are_printed(self):
        os.environ["FAKE_WRITE"] = json.dumps({".claude/x.txt": "1"})
        code, out, _ = self.codex()
        self.assertEqual(code, 10, out)
        self.assertIn("status=control_plane_changed", out)
        self.assertIn("ERROR: the control plane changed during the run", out)
        self.assertIn("ERROR: no tagteam role record appeared", out)
        code, out, _ = self.codex(mode="commit")
        self.assertEqual(code, 7, out)


class RefusedTests(GitRepoBase):
    """A worker whose answer starts with REFUSED: is not an ok run."""
    def say(self, text, vendor="claude", mode="ok", extra=(), cwd=None):
        os.environ["FAKE_ANSWER"] = text
        return self.go(vendor, mode, cwd=cwd or self.repo, extra=extra)

    def test_a_refusal_on_the_first_line_is_status_refused_for_every_vendor_and_read_only(self):
        for vendor in VENDORS:
            for extra in ((), ("--read-only",)):
                code, out, _ = self.say("REFUSED: every tool was blocked\nmore text", vendor, extra=extra)
                self.assertEqual(code, 12, (vendor, extra, out))
                self.assertIn("status=refused", out)
                self.assertIn("ERROR: the worker refused the task: REFUSED: every tool was blocked", out)
                self.assertEqual(last_result(self)["run_exit"], 12)
                self.assertNotIn("ACTUAL_MODEL", out.split("OUTPUT:")[0].split("ERROR:")[0])  # only ok/model_mismatch print it

    def test_leading_space_and_a_byte_order_mark_are_ignored_but_the_case_is_not(self):
        for text in ("\ufeffREFUSED: x", "  \n\t REFUSED: x"):
            self.assertEqual(self.say(text)[0], 12, repr(text))
        for text in ("Refused: x", "refused: x", "REFUSED x", "NOT REFUSED: x"):
            code, out, _ = self.say(text)
            self.assertEqual(code, 0, (text, out))
            self.assertNotIn("ERROR", out)

    def test_a_refusal_after_the_first_line_is_not_a_refusal(self):
        code, out, _ = self.say("Done with the part I could do.\nREFUSED: the rest")
        self.assertEqual(code, 0, out)
        self.assertIn("status=ok", out)

    def test_needs_input_stays_ok_and_is_repeated_in_the_header(self):
        code, out, _ = self.say("NEEDS_INPUT: which file?\ndetails")
        self.assertEqual(code, 0, out)
        self.assertIn("status=ok", out)
        header = out.split("OUTPUT:")[0].splitlines()
        self.assertIn("NEEDS_INPUT: which file?", header)
        self.assertNotIn("ERROR", out)

    def test_a_plain_answer_is_untouched(self):
        code, out, _ = self.say("DONE")
        self.assertEqual(code, 0, out)
        self.assertNotIn("REFUSED", out)

    def test_only_a_clean_exit_is_judged(self):
        code, out, _ = self.say("REFUSED: x", mode="crash")
        self.assertEqual(code, 1, out)
        self.assertIn("status=cli_failed", out)

    def test_precedence(self):
        order = [s for s, _ in run.PRIORITY]
        self.assertEqual(order.index("refused"), order.index("hooks_not_run") + 1)
        self.assertEqual(order.index("timeout"), order.index("refused") + 1)
        self.assertEqual(dict(run.PRIORITY)["refused"], 12)
        code, out, _ = self.say("REFUSED: x", mode="commit")
        self.assertEqual(code, 7, out)  # a commit outweighs the refusal, whose line is still printed
        self.assertIn("ERROR: the worker refused the task", out)
        os.environ["FAKE_WRITE"] = json.dumps({".claude/x.txt": "1"})
        self.assertEqual(self.say("REFUSED: x")[0], 10)
        os.environ.pop("FAKE_WRITE")
        os.environ["FAKE_SESSION"] = "none"
        self.assertEqual(self.say("REFUSED: x", "openai")[0], 11)
        os.environ.pop("FAKE_SESSION")
        os.environ["FAKE_WRITE"] = json.dumps({"fake_written.txt": "x"})
        code, out, _ = self.say("REFUSED: x", extra=["--read-only"])
        self.assertEqual(code, 12, out)  # refused (12) before wrote_files (8)
        self.assertIn("ERROR: a read-only run changed the work tree", out)


if __name__ == "__main__":
    unittest.main()
