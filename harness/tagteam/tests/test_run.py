import contextlib
import io
import json
import os
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

    def go(self, vendor, mode="ok", prompt="Do the thing.\n", extra=(), model=None, effort=None):
        pf = os.path.join(self.tmp.name, "prompt.md")
        with open(pf, "w", encoding="utf-8", newline="") as fh:
            fh.write(prompt)
        os.environ["FAKE_MODE"] = mode
        argv = ["--vendor", vendor, "--model", model or VENDORS[vendor][0], "--effort", effort or VENDORS[vendor][1],
                "--prompt-file", pf, "--cwd", self.tmp.name] + list(extra)
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
        for ro, mode in ((True, "plan"), (False, "accept-edits")):
            want = ["cli", "-p", "prompt"] + tail + [mode, "--output-format", "json", "--log-file", log, "--print-timeout", "90s"]
            self.assertEqual(self.build("google", ro, prompt="prompt"), want)

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


if __name__ == "__main__":
    unittest.main()
