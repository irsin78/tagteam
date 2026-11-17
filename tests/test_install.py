import contextlib
import hashlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

INSTALL = Path(__file__).resolve().parent.parent / "install.py"
sys.path.insert(0, str(INSTALL.parent))
import install  # noqa: E402

STUB = '''import json, os, sys
event = sys.argv[1]
d = json.load(sys.stdin)
state = os.path.join(os.environ["TAGTEAM_STATE_DIR"], d["session_id"] + ".json")
if event == "UserPromptSubmit":
    if not os.path.exists(state):
        worker = any(l.startswith("HARNESS ROLE: DELEGATE") for l in d.get("prompt", "").splitlines())
        open(state, "w").write("worker" if worker else "orchestrator")
    sys.exit(0)
if event == "PreToolUse":
    role = open(state).read() if os.path.exists(state) else "orchestrator"
    ti = d.get("tool_input", {})
    if d["tool_name"].endswith("start_session") and not ti.get("model") and not ti.get("effort"):
        sys.stderr.write("deny"); sys.exit(2)
    if role == "worker" and "git commit" in ti.get("command", ""):
        sys.stderr.write("deny"); sys.exit(2)
sys.exit(0)
'''
FAKE_COLLECT = '''import os, sys
cfg = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(cfg, "calls.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "|" + os.environ.get("TAGTEAM_AA_API_KEY", "") + "\\n")
if os.environ.get("FAKE_COLLECT_FAIL"):
    sys.stderr.write("AA error: HTTP 500\\n")
    sys.exit(1)
open(os.path.join(cfg, "bench.local.json"), "w").write("{}")
'''
ALLOW_ALL = "import sys\nsys.exit(0)\n"

TEMPLATE_AGENTS = "# Agents\n\nintro v2\n\n## Project policy\n\ntemplate placeholder\n"


def build_source(root, guard=STUB):
    h = root / "harness"
    files = {
        "claude/settings.json": json.dumps({"hooks": {"PreToolUse": ["new"]}, "subagentPromptCacheTtl": "1h"}),
        "claude/agents/a.md": "agent",
        "claude/skills/s1/SKILL.md": "skill",
        "tagteam/role_guard.py": guard,
        "tagteam/models.json": "{}",
        "tagteam/models_collect.py": FAKE_COLLECT,
        "tagteam/models.local.json.example": '{"plans": {}}',
        "codex/hooks.json": json.dumps({"hooks": {"PreToolUse": [{"command": "python3 .claude/tagteam/role_guard.py PreToolUse --host codex", "commandWindows": "python .claude/tagteam/role_guard.py PreToolUse --host codex"}]}}),
        "codex/agents/b.toml": "x = 1",
    }
    for rel, text in files.items():
        p = h / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "AGENTS.md.template").write_text(TEMPLATE_AGENTS, encoding="utf-8")
    (root / "CLAUDE.md.template").write_text("@AGENTS.md\n", encoding="utf-8")
    return root


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.src = build_source(base / "src")
        self.dst = base / "proj"
        self.dst.mkdir()
        env = mock.patch.dict(os.environ)  # no real key, no network: the collector here is a fake
        env.start()
        self.addCleanup(env.stop)
        for k in ("TAGTEAM_AA_API_KEY", "TAGTEAM_CONFIG_DIR", "FAKE_COLLECT_FAIL"):
            os.environ.pop(k, None)
        self.calls = self.dst / ".claude/tagteam/calls.log"
        self.bench = self.dst / ".claude/tagteam/bench.local.json"

    def run_install(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.main([str(self.dst), "--source", str(self.src), "--yes", *extra])
        return code, out.getvalue()

    def test_fresh_install_file_set(self):
        code, out = self.run_install()
        self.assertEqual(code, 0)
        for rel in [".claude/settings.json", ".claude/agents/a.md", ".claude/skills/s1/SKILL.md",
                    ".claude/tagteam/role_guard.py", ".claude/tagteam/models.json",
                    ".codex/hooks.json", ".codex/agents/b.toml", "AGENTS.md", "CLAUDE.md",
                    ".claude/tagteam/models.local.json"]:
            self.assertTrue((self.dst / rel).is_file(), rel)
        self.assertIn("created", out)
        self.assertFalse((self.dst / ".claude/tagteam/models.local.json.example").exists())

    def test_codex_hooks_absolute_path(self):
        self.run_install()
        raw = (self.dst / ".codex/hooks.json").read_text(encoding="utf-8")
        h = json.loads(raw)["hooks"]["PreToolUse"][0]
        guard = (self.dst / ".claude/tagteam/role_guard.py").resolve().as_posix()
        self.assertEqual(h["command"], f'python3 "{guard}" PreToolUse --host codex')
        self.assertEqual(h["commandWindows"], f'python "{guard}" PreToolUse --host codex')

    def test_update_preserves_local_state(self):
        self.run_install()
        (self.dst / "AGENTS.md").write_text(
            "# old head\n\n## Project policy\n\nMY POLICY\n", encoding="utf-8")
        (self.dst / ".claude/tagteam/models.local.json").write_text('{"mine": 1}', encoding="utf-8")
        (self.dst / ".codex/hooks.json").write_text('{"trusted": true}', encoding="utf-8")
        (self.dst / ".claude/settings.json").write_text(json.dumps(
            {"hooks": {"old": 1}, "permissions": {"allow": ["x"]}, "subagentPromptCacheTtl": "5m"}),
            encoding="utf-8")
        code, out = self.run_install("--update")
        self.assertEqual(code, 0)
        agents = (self.dst / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("intro v2", agents)
        self.assertIn("MY POLICY", agents)
        self.assertNotIn("template placeholder", agents)
        self.assertNotIn("old head", agents)
        self.assertEqual((self.dst / ".claude/tagteam/models.local.json").read_text(), '{"mine": 1}')
        self.assertEqual((self.dst / ".codex/hooks.json").read_text(), '{"trusted": true}')
        self.assertIn("kept", out)
        s = json.loads((self.dst / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(s["hooks"], {"PreToolUse": ["new"]})
        self.assertEqual(s["subagentPromptCacheTtl"], "1h")
        self.assertEqual(s["permissions"], {"allow": ["x"]})

    def test_claude_md_handling(self):
        (self.dst / "CLAUDE.md").write_text("# mine", encoding="utf-8")
        self.run_install()
        text = (self.dst / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# mine"))
        self.assertIn("@AGENTS.md", text.splitlines())
        self.run_install("--update")
        self.assertEqual((self.dst / "CLAUDE.md").read_text(encoding="utf-8"), text)

    def test_claude_md_with_import_untouched(self):
        (self.dst / "CLAUDE.md").write_text("@AGENTS.md\nextra\n", encoding="utf-8")
        self.run_install()
        self.assertEqual((self.dst / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\nextra\n")

    def test_gitignore_local_files_always_state_dir_only_for_non_git(self):
        local = [".claude/tagteam/models.local.json", ".claude/tagteam/bench.local.json"]
        self.run_install()
        text = (self.dst / ".gitignore").read_text(encoding="utf-8")
        for line in local + [".tagteam/"]:
            self.assertIn(line, text.splitlines())
        self.run_install("--update")
        self.assertEqual((self.dst / ".gitignore").read_text(encoding="utf-8"), text)
        git_dst = self.dst.parent / "gitproj"
        (git_dst / ".git").mkdir(parents=True)
        (git_dst / ".gitignore").write_text("node_modules\n.claude/tagteam/models.local.json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            install.main([str(git_dst), "--source", str(self.src), "--yes"])
        lines = (git_dst / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["node_modules", local[0], local[1]])

    def test_bench_written_once_when_key_set_and_file_missing(self):
        os.environ["TAGTEAM_AA_API_KEY"] = "k-123"
        code, out = self.run_install()
        self.assertEqual(code, 0)
        self.assertTrue(self.bench.is_file())
        self.assertEqual(self.calls.read_text().splitlines(), ["--write-bench --timeout 15|k-123"])
        self.assertNotIn("k-123", out)
        self.run_install("--update")  # file exists: not touched again
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_bench_not_written_without_key(self):
        self.run_install()
        self.assertFalse(self.bench.exists() or self.calls.exists())

    def test_bench_failure_only_warns(self):
        os.environ["TAGTEAM_AA_API_KEY"] = "k-123"
        os.environ["FAKE_COLLECT_FAIL"] = "1"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_install()
        self.assertEqual(code, 0)
        self.assertFalse(self.bench.exists())
        self.assertIn("warning", err.getvalue())
        self.assertIn("HTTP 500", err.getvalue())

    def test_check_and_dry_run_do_not_fetch_or_need_the_file(self):
        self.run_install()
        os.environ["TAGTEAM_AA_API_KEY"] = "k-123"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main([str(self.dst), "--check"]), 0)
            install.main([str(self.dst), "--source", str(self.src), "--schedule", "--dry-run"])
        self.assertFalse(self.bench.exists() or self.calls.exists())

    def test_check_passes_with_stub(self):
        self.run_install()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = install.main([str(self.dst), "--check"])
        self.assertEqual(code, 0, out.getvalue())

    def test_check_fails_when_guard_allows_everything(self):
        self.run_install()
        (self.dst / ".claude/tagteam/role_guard.py").write_text(ALLOW_ALL, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = install.main([str(self.dst), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out.getvalue())

    def test_check_fails_when_guard_missing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main([str(self.dst), "--check"]), 1)

    def test_cli_runs_as_script(self):
        p = subprocess.run([sys.executable, str(INSTALL), str(self.dst), "--source", str(self.src),
                            "--yes"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr)


SKILL = INSTALL.parent / "harness/claude/skills/model-update/SKILL.md"
GUARD_WITH_STATE = "import os\ndef state_dir(payload):\n    return os.environ['TAGTEAM_STATE_DIR']\n"


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.proj = (base / "proj").resolve()
        (self.proj / ".claude/tagteam").mkdir(parents=True)
        (self.proj / ".claude/tagteam/models_collect.py").write_text("# collector\n", encoding="utf-8")
        (self.proj / ".claude/tagteam/role_guard.py").write_text(GUARD_WITH_STATE, encoding="utf-8")
        self.state = base / "state"
        self.state.mkdir()
        self.home = base / "home"
        self.calls = []
        self.fail = lambda argv: False
        self.crontab = ""

    def runner(self, argv, input_text=None):
        self.calls.append((argv, input_text))
        if argv[:2] == ["crontab", "-l"]:
            return (0, self.crontab) if self.crontab else (1, "no crontab")
        return (1, "boom") if self.fail(argv) else (0, "")

    def sched(self, plat, dry=False):
        return install.Scheduler(self.proj, plat, self.runner, dry, python="/usr/bin/py", home=self.home, uid=501)

    def name(self):
        return "tagteam-models-" + hashlib.sha1(str(self.proj).encode("utf-8")).hexdigest()[:8]

    def quiet(self, fn):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = fn()
        return code, out.getvalue()

    def cmd(self):
        return ["/usr/bin/py", str(self.proj / ".claude/tagteam/models_collect.py"), "--project", str(self.proj)]

    def test_windows_schedule_xml_and_argv(self):
        captured = {}

        def runner(argv, input_text=None):
            self.calls.append(argv)
            if "/XML" in argv:
                data = Path(argv[argv.index("/XML") + 1]).read_bytes()
                captured["raw"] = data
            return 0, ""
        s = install.Scheduler(self.proj, "windows", runner, False, python="C:/Py/python.exe")
        code, _ = self.quiet(s.schedule)
        self.assertEqual(code, 0)
        name = self.name()
        self.assertEqual(self.calls[0][:4], ["schtasks", "/Create", "/TN", name])
        self.assertEqual(self.calls[0][4], "/XML")
        self.assertEqual(self.calls[0][-1], "/F")
        raw = captured["raw"]
        self.assertTrue(raw.startswith(b"\xff\xfe"))
        xml = raw[2:].decode("utf-16-le")
        for want in ("<Monday />", "<LogonType>InteractiveToken</LogonType>", "<StartWhenAvailable>true</StartWhenAvailable>",
                     "<ExecutionTimeLimit>PT10M</ExecutionTimeLimit>", "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>",
                     '<Command>"C:/Py/python.exe"</Command>', "models_collect.py", "--project", f"<WorkingDirectory>{self.proj}</WorkingDirectory>"):
            self.assertIn(want, xml)
        self.assertFalse(Path(self.calls[0][5]).exists(), "temp xml removed")

    def test_task_name_hash_per_project(self):
        self.assertEqual(install.task_name(self.proj), self.name())
        self.assertNotEqual(install.task_name(self.proj), install.task_name(self.proj / "other"))
        self.assertRegex(self.name(), r"^tagteam-models-[0-9a-f]{8}$")

    def test_windows_xml_failure_falls_back_to_weekly(self):
        self.fail = lambda argv: "/XML" in argv
        code, out = self.quiet(self.sched("windows").schedule)
        self.assertEqual(code, 0)
        fallback = self.calls[1][0]
        self.assertEqual(fallback[:2], ["schtasks", "/Create"])
        for tok in ("/SC", "WEEKLY", "/D", "MON", "/ST", "09:00", "/TR", "/F"):
            self.assertIn(tok, fallback)
        self.assertIn("will not be re-run", out)

    def test_dry_run_runs_nothing_and_shows_xml(self):
        code, out = self.quiet(self.sched("windows", dry=True).schedule)
        self.assertEqual((code, self.calls), (0, []))
        self.assertIn("InteractiveToken", out)
        self.assertIn("schtasks /Create /TN", out)

    def test_macos_schedule_and_unschedule(self):
        s = self.sched("macos")
        self.quiet(s.schedule)
        plist = self.home / f"Library/LaunchAgents/net.tagteam.{self.name()}.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["ProgramArguments"], self.cmd())
        self.assertEqual(data["StartCalendarInterval"], {"Weekday": 1, "Hour": 9, "Minute": 0})
        self.assertEqual(data["WorkingDirectory"], str(self.proj))
        self.assertEqual(self.calls[-1][0], ["launchctl", "bootstrap", "gui/501", str(plist)])
        self.quiet(self.sched("macos").schedule)  # overwrite: boots out the old one first
        self.assertIn(["launchctl", "bootout", f"gui/501/net.tagteam.{self.name()}"], [c[0] for c in self.calls])
        self.calls.clear()
        self.assertEqual(self.quiet(self.sched("macos").unschedule)[0], 0)
        self.assertEqual(self.calls[0][0], ["launchctl", "bootout", f"gui/501/net.tagteam.{self.name()}"])
        self.assertFalse(plist.exists())

    def test_linux_crontab_replaces_marked_line_only(self):
        mark = "# " + self.name()
        self.crontab = f"5 5 * * * other job\n0 9 * * 1 old {mark}\n"
        self.quiet(self.sched("linux").schedule)
        written = self.calls[-1][1]
        self.assertEqual(self.calls[-1][0], ["crontab", "-"])
        lines = written.splitlines()
        self.assertEqual(lines[0], "5 5 * * * other job")
        self.assertEqual(sum(ln.endswith(mark) for ln in lines), 1)
        self.assertTrue(lines[1].startswith("0 9 * * 1 /usr/bin/py "))
        self.assertIn("--project", lines[1])
        self.crontab = written
        self.calls.clear()
        self.quiet(self.sched("linux").unschedule)
        self.assertEqual(self.calls[-1][1], "5 5 * * * other job\n")

    def test_crontab_read_error_never_writes(self):
        reads = []

        def runner(argv, input_text=None):
            self.calls.append((argv, input_text))
            if argv == ["crontab", "-l"]:
                reads.append(1)
                return 1, "crontab: error while loading shared libraries"
            return 0, ""
        for op in ("schedule", "unschedule"):
            self.calls.clear()
            s = install.Scheduler(self.proj, "linux", runner, False, python="/usr/bin/py")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = getattr(s, op)()
            self.assertEqual(code, 1, op)
            self.assertIn("cannot read the crontab", err.getvalue())
            self.assertEqual([c for c in self.calls if c[0] == ["crontab", "-"]], [], op)
        self.assertEqual(len(reads), 2, "one read per operation")

    def test_crontab_read_once_and_percent_escaped(self):
        odd = self.proj / "100%done"
        s = install.Scheduler(odd, "linux", self.runner, False, python="/usr/bin/py")
        self.quiet(s.schedule)
        self.assertEqual(sum(c[0] == ["crontab", "-l"] for c in self.calls), 1)
        line = self.calls[-1][1].splitlines()[-1]
        self.assertIn("100\\%done", line)
        self.assertNotIn("100%done", line)

    def test_unschedule_when_not_registered_is_ok(self):
        for plat in ("windows", "macos", "linux"):
            self.fail = lambda argv: True
            code, out = self.quiet(self.sched(plat).unschedule)
            self.assertEqual(code, 0, plat)
            self.assertIn("not registered", out)

    def test_unschedule_deletes_proposal_files_and_uses_delete_argv(self):
        for fn in ("model-update.json", "model-update.applied", "keep.txt"):
            (self.state / fn).write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(self.state)}):
            code, _ = self.quiet(self.sched("windows").unschedule)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0][0], ["schtasks", "/Delete", "/TN", self.name(), "/F"])
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["keep.txt"])

    def test_main_flags(self):
        calls = []
        runner = lambda argv, input_text=None: calls.append(argv) or (0, "")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.main([str(self.proj), "--schedule", "--dry-run"], runner, "linux"), 0)
            self.assertEqual(install.main([str(self.proj), "--schedule"], runner, "windows"), 0)
            self.assertEqual(install.main([str(self.proj), "--unschedule", "--yes"], runner, "windows"), 0)
        self.assertIn("[dry-run] crontab -", out.getvalue())
        self.assertEqual([c[:2] for c in calls if c[0] == "schtasks"], [["schtasks", "/Create"], ["schtasks", "/Delete"]])
        self.assertFalse((self.proj / "AGENTS.md").exists(), "schedule-only must not reinstall")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            install.main([str(self.proj), "--dry-run"])

    def test_schedule_without_installed_collector_fails_without_registering(self):
        (self.proj / ".claude/tagteam/models_collect.py").unlink()
        calls = []
        runner = lambda argv, input_text=None: calls.append(argv) or (0, "")
        src = self._tmp.name + "/nosrc"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = install.main([str(self.proj), "--schedule", "--source", src], runner, "windows")
        self.assertEqual((code, calls), (1, []))

    def test_skill_file(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 80)
        self.assertTrue(text.startswith("---\nname: model-update\ndescription:"))
        self.assertIn("Do not use it in a worker", text)
        for kw in ("worker", "model-update.json", "route.py", "Artificial Analysis", "FrontierSWE", "candidate",
                   "probe", "--check", "[fix:", "Commit", "--mark-applied"):
            self.assertIn(kw, text, kw)


if __name__ == "__main__":
    unittest.main()
