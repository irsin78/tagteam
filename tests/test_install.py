import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_gitignore_only_for_non_git(self):
        self.run_install()
        self.assertIn(".tagteam/", (self.dst / ".gitignore").read_text(encoding="utf-8"))
        self.run_install("--update")
        self.assertEqual((self.dst / ".gitignore").read_text(encoding="utf-8").count(".tagteam/"), 1)
        git_dst = self.dst.parent / "gitproj"
        (git_dst / ".git").mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            install.main([str(git_dst), "--source", str(self.src), "--yes"])
        self.assertFalse((git_dst / ".gitignore").exists())

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


if __name__ == "__main__":
    unittest.main()
