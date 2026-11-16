import json
import os
import subprocess
import sys
import tempfile
import unittest

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "role_guard.py")
START = "mcp__ccd_session__start_session"


class RoleGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = self._tmp.name

    def run_guard(self, event, payload=None, host=None, env=None, raw=None):
        e = dict(os.environ, TAGTEAM_STATE_DIR=self.state)
        e.pop("HARNESS_DELEGATE_RUN", None)
        e.update(env or {})
        cmd = [sys.executable, GUARD, event] + (["--host", host] if host else [])
        data = raw if raw is not None else json.dumps(payload or {})
        return subprocess.run(cmd, input=data, capture_output=True, text=True, env=e, timeout=30)

    def prompt(self, text, sid="s1"):
        return self.run_guard("UserPromptSubmit", {"session_id": sid, "prompt": text})

    def tool(self, name, tool_input, sid="s1", **extra):
        p = {"session_id": sid, "tool_name": name, "tool_input": tool_input}
        p.update(extra)
        return self.run_guard("PreToolUse", p)

    def role_line(self, sid="s1"):
        return self.run_guard("SessionStart", {"session_id": sid}).stdout.strip()

    def make_worker(self, sid="s1"):
        self.prompt("Background (from the session that started this one): x\n\nHARNESS ROLE: DELEGATE\ndo it", sid)

    def test_marker_on_later_line_is_worker(self):
        self.make_worker()
        self.assertEqual(self.role_line(), "TAGTEAM: role=worker host=claude")

    def test_no_marker_is_orchestrator(self):
        self.prompt("hello there")
        self.assertEqual(self.role_line(), "TAGTEAM: role=orchestrator host=claude")

    def test_later_marker_does_not_change_orchestrator(self):
        self.prompt("hello there")
        self.prompt("HARNESS ROLE: DELEGATE\nnow")
        self.assertIn("role=orchestrator", self.role_line())

    def test_worker_denied_commit_and_push(self):
        self.make_worker()
        for cmd in ("git commit -m x", "git push origin main", "git -C x commit -m y",
                    'git -C "a b" push', "GIT.EXE commit -m x", r"C:\Git\bin\git.exe push",
                    "cd r && git -c user.name=a commit"):
            r = self.tool("Bash", {"command": cmd})
            self.assertEqual(r.returncode, 2, cmd)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_worker_allowed_git_status(self):
        self.make_worker()
        self.assertEqual(self.tool("Bash", {"command": "git status"}).returncode, 0)
        self.assertEqual(self.tool("PowerShell", {"command": "git log --oneline"}).returncode, 0)
        self.assertEqual(self.tool("PowerShell", {"command": "git.exe status"}).returncode, 0)
        self.assertEqual(self.tool("Bash", {"command": "echo digit commit"}).returncode, 0)

    def test_orchestrator_may_commit(self):
        self.prompt("hello")
        self.assertEqual(self.tool("Bash", {"command": "git commit -m x"}).returncode, 0)

    def test_worker_denied_start_session_and_agent(self):
        self.make_worker()
        self.assertEqual(self.tool(START, {"model": "m", "effort": "high"}).returncode, 2)
        self.assertEqual(self.tool("Agent", {"prompt": "x"}).returncode, 2)

    def test_orchestrator_start_session_rules(self):
        self.prompt("hello")
        self.assertEqual(self.tool(START, {}).returncode, 2)
        self.assertEqual(self.tool(START, {"model": "m"}).returncode, 2)
        self.assertEqual(self.tool(START, {"effort": "high"}).returncode, 2)
        self.assertEqual(self.tool(START, {"model": "m", "effort": "high"}).returncode, 0)
        self.assertEqual(self.tool(START, {"proposal_id": "p1"}).returncode, 0)
        self.assertEqual(self.tool(START, {"proposal_id": "p1", "model": "m"}).returncode, 2)
        self.assertEqual(self.tool(START, {"proposal_id": "p1", "prompt": "x"}).returncode, 2)
        self.assertEqual(self.tool("Agent", {"prompt": "x"}).returncode, 0)

    def test_env_var_makes_worker(self):
        r = self.run_guard("PreToolUse", {"session_id": "e", "tool_name": "Bash",
                                          "tool_input": {"command": "git push"}},
                           env={"HARNESS_DELEGATE_RUN": "1"})
        self.assertEqual(r.returncode, 2)

    def test_agent_id_makes_worker(self):
        r = self.tool("Bash", {"command": "git commit -m x"}, sid="none", agent_id="a1")
        self.assertEqual(r.returncode, 2)

    def test_malformed_stdin_fails_open(self):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            r = self.run_guard(event, raw="{not json")
            self.assertEqual(r.returncode, 0, event)
            self.assertIn("warning", r.stderr)
        self.assertEqual(self.run_guard("PreToolUse", raw="[1]").returncode, 0)

    def test_session_start_prints_role_line(self):
        r = self.run_guard("SessionStart", {"session_id": "z"}, host="codex")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "TAGTEAM: role=orchestrator host=codex")

    def test_codex_shell_tool_with_list_command(self):
        self.make_worker()
        r = self.run_guard("PreToolUse", {"session_id": "s1", "tool_name": "shell",
                                          "tool_input": {"command": ["bash", "-lc", "git commit -m x"]}},
                           host="codex")
        self.assertEqual(r.returncode, 2)
        r = self.run_guard("PreToolUse", {"session_id": "s1", "tool_name": "exec_command",
                                          "tool_input": {"cmd": "git status"}}, host="codex")
        self.assertEqual(r.returncode, 0)

    def test_record_written_under_state_dir(self):
        self.prompt("hello")
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "s1.json")))


if __name__ == "__main__":
    unittest.main()
