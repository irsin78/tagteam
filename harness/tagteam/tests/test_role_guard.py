import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "role_guard.py")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
sys.path.insert(0, os.path.dirname(GUARD))
import role_guard  # noqa: E402  (in-process checks of the agy entry points)
START = "mcp__ccd_session__start_session"
SPAWN = "collaborationspawn_agent"


def pending(host="claude"):
    return "TAGTEAM: role=pending host=%s (set by the first prompt: worker if it carries HARNESS ROLE: DELEGATE)" % host


def codex_payloads():
    with open(os.path.join(FIXTURES, "codex_payloads.json"), encoding="utf-8") as f:
        return json.load(f)


class GuardCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state)
        self.cfg = os.path.join(self._tmp.name, "cfg")  # no models.local.json unless a test writes one
        os.makedirs(self.cfg)

    def run_guard(self, event, payload=None, host=None, env=None, raw=None):
        e = dict(os.environ, TAGTEAM_STATE_DIR=self.state, TAGTEAM_CONFIG_DIR=self.cfg)
        e.pop("HARNESS_DELEGATE_RUN", None)
        e.update(env or {})
        cmd = [sys.executable, GUARD, event] + (["--host", host] if host else [])
        data = raw if raw is not None else json.dumps(payload or {})
        return subprocess.run(cmd, input=data, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=e, timeout=30)


class RoleGuardTest(GuardCase):
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
        self.assertEqual(r.stdout.strip(), pending("codex"))  # no prompt yet: the role is not decided

    def test_codex_shell_tool_with_list_command(self):
        self.make_worker()
        r = self.run_guard("PreToolUse", {"session_id": "s1", "tool_name": "shell",
                                          "tool_input": {"command": ["bash", "-lc", "git commit -m x"]}},
                           host="codex")
        self.assert_codex_denied(r, "git commit")
        r = self.run_guard("PreToolUse", {"session_id": "s1", "tool_name": "exec_command",
                                          "tool_input": {"cmd": "git status"}}, host="codex")
        self.assert_allowed(r)

    # ---- deny format per host ----------------------------------------------------------

    def assert_codex_denied(self, r, needle=""):
        """Codex ignores exit 2: the deny is one JSON object on stdout with exit 0 (X13)."""
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        out = json.loads(lines[0])["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertTrue(out["permissionDecisionReason"].startswith("tagteam: "))
        self.assertIn(needle, out["permissionDecisionReason"])
        self.assertEqual(r.stderr.strip(), out["permissionDecisionReason"])

    def assert_allowed(self, r):
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)

    def test_deny_format_per_host(self):
        payload = {"session_id": "d", "agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "git push"}}
        claude = self.run_guard("PreToolUse", payload)
        self.assertEqual((claude.returncode, claude.stdout), (2, ""))
        self.assertEqual(claude.stderr, "tagteam: workers may not run git commit or git push\n")
        self.assert_codex_denied(self.run_guard("PreToolUse", payload, host="codex"), "git commit or git push")

    def test_codex_internal_error_fails_open_without_stdout(self):
        for event in ("PreToolUse", "SubagentStart"):
            r = self.run_guard(event, raw="{not json", host="codex")
            self.assertEqual((r.returncode, r.stdout), (0, ""), event)
            self.assertIn("warning (allowed)", r.stderr)

    # ---- Codex payloads (real shapes, tests/fixtures/codex_payloads.json) ---------------

    def project(self, agents=("reader",)):
        """A non-git project folder with Codex custom agent definitions (one folder per agent set)."""
        proj = os.path.join(self._tmp.name, "proj-" + "-".join(agents))
        os.makedirs(os.path.join(proj, ".codex", "agents"), exist_ok=True)
        for name in agents:
            with open(os.path.join(proj, ".codex", "agents", name + ".toml"), "w", encoding="utf-8") as f:
                f.write('name = "%s"\nmodel = "m"\nmodel_reasoning_effort = "low"\n' % name)
        return proj

    def codex(self, key, proj=None, env=None, **changes):
        p = codex_payloads()[key]
        p["cwd"] = proj or self.project()
        tool_input = changes.pop("tool_input", None)
        if tool_input is not None:
            p["tool_input"] = dict(p["tool_input"], **tool_input)
        drop = changes.pop("drop", ())
        p.update(changes)
        for k in drop:
            p.get("tool_input", {}).pop(k, None)
        return self.run_guard(p["hook_event_name"], p, host="codex", env=env)

    def test_fixture_is_real_codex_shape(self):
        fx = codex_payloads()
        self.assertEqual(fx["spawn_custom"]["tool_name"], SPAWN)
        self.assertEqual(fx["apply_patch"]["tool_name"], "apply_patch")
        for key in ("subagent_shell", "subagent_start"):
            self.assertTrue(fx[key]["agent_id"] and fx[key]["agent_type"], key)
        self.assertNotIn("agent_id", fx["root_shell"])
        self.assertTrue(fx["spawn_custom"]["tool_input"]["message"].startswith("gAAAA"))  # encrypted by Codex

    def test_codex_subagent_git_commit_denied(self):
        self.assert_codex_denied(self.codex("subagent_shell"), "git commit")
        self.assert_allowed(self.codex("subagent_shell", tool_input={"command": "git status"}))

    def test_codex_root_session_roles(self):
        self.assert_allowed(self.codex("root_shell"))  # the orchestrator may commit
        self.assert_codex_denied(self.codex("root_shell", env={"HARNESS_DELEGATE_RUN": "1"}), "git commit")

    def test_codex_spawn_rules_for_the_orchestrator(self):
        self.assert_allowed(self.codex("spawn_custom"))  # custom agent: its definition fixes model and effort
        self.assert_codex_denied(self.codex("spawn_custom_with_args"), "custom agent reader fixes model and effort")
        self.assert_codex_denied(self.codex("spawn_custom", tool_input={"reasoning_effort": "high"}), "reasoning_effort")
        self.assert_allowed(self.codex("spawn_default_with_args"))  # built-in type honours the arguments
        for drop in (("model",), ("reasoning_effort",), ("model", "reasoning_effort")):
            self.assert_codex_denied(self.codex("spawn_default_with_args", drop=drop), "needs explicit model")
        self.assert_allowed(self.codex("spawn_default_with_args", drop=("agent_type",)))
        self.assert_codex_denied(self.codex("spawn_default_with_args", drop=("agent_type", "model")))
        # an agent_type that is not a definition of this project is a built-in type
        self.assert_codex_denied(self.codex("spawn_custom", proj=self.project(agents=())), "needs explicit model")

    def test_codex_custom_agent_name_comes_from_the_definition(self):
        proj = self.project(agents=())
        with open(os.path.join(proj, ".codex", "agents", "file-name.toml"), "w", encoding="utf-8") as f:
            f.write("description = 'x'\nname = 'reader'\n")
        self.assert_allowed(self.codex("spawn_custom", proj=proj))

    def test_codex_worker_may_not_spawn_but_may_follow_up(self):
        self.assert_codex_denied(self.codex("spawn_default_with_args", agent_id="a1"), "may not spawn")
        self.assert_codex_denied(self.codex("spawn_custom", env={"HARNESS_DELEGATE_RUN": "1"}), "may not spawn")
        self.assert_allowed(self.codex("followup_task"))
        self.assert_allowed(self.codex("followup_task", agent_id="a1"))

    def test_other_spawn_spellings(self):
        for name in ("spawn_agent", "collaboration.spawn_agent"):
            self.assert_codex_denied(self.codex("spawn_default_with_args", tool_name=name, drop=("model",)))

    def patch(self, *lines):
        return {"command": "*** Begin Patch\n" + "\n".join(lines) + "\n*** End Patch"}

    def test_codex_worker_apply_patch_on_control_plane_denied(self):
        proj = self.project()
        self.assert_allowed(self.codex("apply_patch", proj, agent_id="a1"))  # real payload: Add File x11-edit.txt
        for line in ("*** Update File: AGENTS.md", "*** Delete File: claude.md",
                     "*** Add File: .agents/skills/cycle/SKILL.md", "*** Update File: .codex\\hooks.json",
                     "*** Update File: %s" % os.path.join(proj, ".claude", "settings.json")):
            self.assert_codex_denied(self.codex("apply_patch", proj, agent_id="a1", tool_input=self.patch(line)),
                                     "control plane")
        moved = self.patch("*** Update File: notes.md", "*** Move to: .claude/notes.md", "@@", "-a", "+b")
        self.assert_codex_denied(self.codex("apply_patch", proj, agent_id="a1", tool_input=moved), ".claude/notes.md")
        for line in ("*** Update File: docs/AGENTS.md", "*** Add File: src/claude/x.py",
                     "*** Update File: .claudeignore", "*** Add File: ../other/AGENTS.md"):
            self.assert_allowed(self.codex("apply_patch", proj, agent_id="a1", tool_input=self.patch(line)))
        # the orchestrator edits the control plane
        self.assert_allowed(self.codex("apply_patch", proj, tool_input=self.patch("*** Update File: AGENTS.md")))

    def test_control_plane_is_relative_to_the_git_top(self):
        repo = os.path.join(self._tmp.name, "repo")
        sub = os.path.join(repo, "sub")
        os.makedirs(sub)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        p = {"session_id": "w", "agent_id": "a1", "cwd": sub, "tool_name": "apply_patch"}
        p["tool_input"] = self.patch("*** Update File: AGENTS.md")  # sub/AGENTS.md: not the root file
        self.assert_allowed(self.run_guard("PreToolUse", p, host="codex"))
        p["tool_input"] = self.patch("*** Update File: ../AGENTS.md")
        self.assert_codex_denied(self.run_guard("PreToolUse", p, host="codex"), "control plane")

    def test_claude_worker_edit_tools_on_control_plane_denied(self):
        proj = self.project()
        self.make_worker()
        for tool, key, path in (("Edit", "file_path", "CLAUDE.md"), ("Write", "file_path", ".claude/settings.json"),
                                ("MultiEdit", "file_path", os.path.join(proj, "AGENTS.md")),
                                ("NotebookEdit", "notebook_path", ".agents/x.ipynb")):
            r = self.tool(tool, {key: path}, cwd=proj)
            self.assertEqual(r.returncode, 2, (tool, path))
            self.assertIn("control plane", r.stderr)
        self.assertEqual(self.tool("Write", {"file_path": "src/a.py"}, cwd=proj).returncode, 0)
        self.prompt("hello", sid="o")
        self.assertEqual(self.tool("Edit", {"file_path": "AGENTS.md"}, sid="o", cwd=proj).returncode, 0)

    def test_codex_prompt_with_agent_id_records_nothing(self):
        r = self.codex("user_prompt_submit", agent_id="a1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))
        self.codex("user_prompt_submit")
        sid = codex_payloads()["user_prompt_submit"]["session_id"]
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", sid + ".json")))

    def test_codex_session_start_pending_then_recorded(self):
        r = self.codex("session_start")
        self.assertEqual(r.stdout.strip().splitlines()[0], pending("codex"))
        self.codex("user_prompt_submit")  # same session id as session_start
        self.assertEqual(self.codex("session_start").stdout.strip(), "TAGTEAM: role=orchestrator host=codex")

    def test_codex_subagent_start_records_the_model(self):
        r = self.codex("subagent_start")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        with open(os.path.join(self.state, "agents.jsonl"), encoding="utf-8") as f:
            recs = [json.loads(line) for line in f]
        fx = codex_payloads()["subagent_start"]
        self.assertEqual(len(recs), 1)
        self.assertEqual({k: recs[0][k] for k in ("session_id", "agent_id", "agent_type", "model", "host")},
                         {"session_id": fx["session_id"], "agent_id": fx["agent_id"], "agent_type": "default",
                          "model": fx["model"], "host": "codex"})
        self.codex("subagent_start")
        with open(os.path.join(self.state, "agents.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)  # appended

    def test_codex_subagent_start_fails_open_when_state_unwritable(self):
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        r = self.codex("subagent_start", env={"TAGTEAM_STATE_DIR": os.path.join(blocker, "s")})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("allowed", r.stderr)

    def test_record_written_under_state_dir(self):
        self.prompt("hello")
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "s1.json")))

    def test_record_goes_to_the_work_tree_state_folder_by_default(self):
        repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(repo)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid")
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, env=env)
        r = self.run_guard("UserPromptSubmit", {"session_id": "g1", "prompt": "hi", "cwd": repo},
                           env={"TAGTEAM_STATE_DIR": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(repo, ".tagteam", "sessions", "g1.json")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "tagteam")))
        r = self.run_guard("SessionStart", {"session_id": "g1", "cwd": repo}, env={"TAGTEAM_STATE_DIR": ""})
        self.assertEqual(r.stdout.strip(), "TAGTEAM: role=orchestrator host=claude")

    def test_unwritable_state_folder_fails_open_with_the_fix(self):
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        target = os.path.join(blocker, "state")
        r = self.run_guard("UserPromptSubmit", {"session_id": "u1", "prompt": "hi"}, env={"TAGTEAM_STATE_DIR": target})
        self.assertEqual(r.returncode, 0)
        self.assertIn("allowed", r.stderr)
        self.assertIn(target, r.stderr)
        self.assertIn("TAGTEAM_STATE_DIR", r.stderr)

    # task notice (spec 3.9): one line per task in models.local.json
    def tasks(self, tasks=None, raw=None):
        with open(os.path.join(self.cfg, "models.local.json"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps({"tasks": tasks}))

    def test_task_notice_lines(self):
        self.tasks({"html": {"about": "single-file HTML pages", "base": "implement", "vendor": "google"},
                    "vision": {"about": "read  images\nand screenshots", "base": "read"}})
        lines = self.start_lines()
        self.assertEqual(lines, [pending(),
                                 "TAGTEAM: task html - single-file HTML pages (route.py --task html)",
                                 "TAGTEAM: task vision - read images and screenshots (route.py --task vision)"])

    def test_task_notice_only_for_the_orchestrator(self):
        self.tasks({"html": {"about": "pages", "base": "implement"}})
        self.make_worker()
        self.assertEqual(self.start_lines(payload={"session_id": "s1"}), ["TAGTEAM: role=worker host=claude"])
        r = self.run_guard("SessionStart", {"session_id": "s1"}, env={"HARNESS_DELEGATE_RUN": "1"})
        self.assertEqual(r.stdout.strip(), "TAGTEAM: role=worker host=claude")

    def test_task_notice_silent_when_file_missing_broken_or_malformed(self):
        self.assertEqual(len(self.start_lines()), 1)  # no models.local.json
        for raw in ("{broken", "[1]", "{}", '{"tasks": []}', '{"tasks": "x"}', '{"tasks": {"a": 1, "b": {}, "c": {"about": ""}}}'):
            self.tasks(raw=raw)
            r = self.run_guard("SessionStart", {"session_id": "n"})
            self.assertEqual((r.returncode, len(r.stdout.strip().splitlines())), (0, 1), raw)
            self.assertEqual(r.stderr, "", raw)

    def test_task_notice_skips_entries_without_about(self):
        self.tasks({"bad": {"base": "read"}, "good": {"about": "ok", "base": "read"}})
        self.assertEqual(self.start_lines()[1:], ["TAGTEAM: task good - ok (route.py --task good)"])

    def test_task_notice_is_capped_at_ten_lines(self):
        self.tasks({"t%d" % i: {"about": "task %d" % i, "base": "read"} for i in range(10)})
        lines = self.start_lines()[1:]
        self.assertEqual(len(lines), 10)  # exactly ten: all shown
        self.assertEqual(lines[-1], "TAGTEAM: task t9 - task 9 (route.py --task t9)")
        self.tasks({"t%d" % i: {"about": "task %d" % i, "base": "read"} for i in range(14)})
        lines = self.start_lines()[1:]
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[8], "TAGTEAM: task t8 - task 8 (route.py --task t8)")
        self.assertEqual(lines[9], "TAGTEAM: and 5 more")

    def test_task_notice_comes_after_the_model_update_notice(self):
        self.proposal()
        self.tasks({"html": {"about": "pages", "base": "implement"}})
        lines = self.start_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("model update proposal", lines[1])
        self.assertTrue(lines[2].startswith("TAGTEAM: task html"))

    # model-update notice (P4)
    def proposal(self, created=None, digest="d1", changes=1, errors=0, applied=None):
        import datetime
        created = created or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cli = {"openai": {"status": "error" if errors else "ok"}}
        with open(os.path.join(self.state, "model-update.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "created": created, "cli": cli, "digest": digest,
                       "changes": [{"kind": "new_model"}] * changes}, f)
        if applied:
            with open(os.path.join(self.state, "model-update.applied"), "w", encoding="utf-8") as f:
                json.dump({"digest": applied}, f)

    def start_lines(self, env=None, payload=None):
        r = self.run_guard("SessionStart", payload or {"session_id": "n"}, env=env)
        self.assertEqual(r.returncode, 0)
        return r.stdout.strip().splitlines()

    def test_notice_for_pending_proposal(self):
        self.proposal(changes=2, errors=1)
        lines = self.start_lines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1], r"^TAGTEAM: model update proposal \(2 changes, 1 CLI errors, collected \d{4}-\d\d-\d\d\); "
                                   r"orchestrator: load skill model-update\.$")

    def test_no_notice_when_applied_worker_missing_or_broken(self):
        self.assertEqual(len(self.start_lines()), 1)  # no proposal file
        self.proposal(applied="d1")
        self.assertEqual(len(self.start_lines()), 1)
        self.proposal(changes=0, errors=0)
        self.assertEqual(len(self.start_lines()), 1)  # nothing to report
        self.proposal()
        self.assertEqual(len(self.start_lines(env={"HARNESS_DELEGATE_RUN": "1"})), 1)
        self.assertEqual(len(self.start_lines(payload={"session_id": "n", "agent_id": "a"})), 1)
        with open(os.path.join(self.state, "model-update.json"), "w") as f:
            f.write("{broken")
        r = self.run_guard("SessionStart", {"session_id": "n"})
        self.assertEqual((r.returncode, len(r.stdout.strip().splitlines())), (0, 1))

    def test_applied_marker_states(self):
        marker = os.path.join(self.state, "model-update.applied")
        self.proposal()
        self.assertEqual(len(self.start_lines()), 2)  # no marker: not applied
        for body in ("{broken", "[1]", "{}"):
            with open(marker, "w") as f:
                f.write(body)
            self.assertEqual(len(self.start_lines()), 1, body)  # unreadable marker: silent
        os.remove(marker)
        os.mkdir(marker)  # exists but cannot be read as a file
        self.assertEqual(len(self.start_lines()), 1)

    def test_stale_collection_warning(self):
        self.proposal(created="2020-01-01T00:00:00Z")
        lines = self.start_lines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1], r"^TAGTEAM: model list not collected for \d+ days; check the scheduled task")

    def test_slash_command_prefix_is_ignored_before_the_marker(self):
        self.prompt("/plan HARNESS ROLE: DELEGATE\nwork", sid="p1")
        self.assertEqual(self.role_line("p1"), "TAGTEAM: role=worker host=claude")
        self.prompt("/plan hello\nthe marker HARNESS ROLE: DELEGATE is only mentioned here", sid="p2")
        self.assertEqual(self.role_line("p2"), "TAGTEAM: role=orchestrator host=claude")
        self.prompt("see /plan HARNESS ROLE: DELEGATE", sid="p3")  # not at the start of the line
        self.assertEqual(self.role_line("p3"), "TAGTEAM: role=orchestrator host=claude")

    def test_empty_or_corrupt_record_is_unresolved_and_never_an_orchestrator(self):
        sessions = os.path.join(self.state, "sessions")
        os.makedirs(sessions)
        bodies = {"empty": "", "broken": "{half", "unknown": '{"role": "boss"}', "lost": '{"role": "unresolved"}'}
        for sid, body in bodies.items():
            path = os.path.join(sessions, sid + ".json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            r = self.tool("Bash", {"command": "git commit -m x"}, sid=sid)
            self.assertEqual(r.returncode, 2, sid)
            self.assertIn("role unresolved", r.stderr)
            self.assertEqual(self.tool("Agent", {"prompt": "x"}, sid=sid).returncode, 2, sid)
            self.assertEqual(self.tool("Bash", {"command": "git status"}, sid=sid).returncode, 0, sid)
            self.assertEqual(self.role_line(sid), "TAGTEAM: role=unresolved host=claude", sid)
            self.assert_codex_denied(self.run_guard("PreToolUse", {"session_id": sid, "tool_name": "shell",
                                                                    "tool_input": {"command": "git push"}}, host="codex"),
                                     "role unresolved")
            self.prompt("hello", sid=sid)  # a later prompt does not turn it into an orchestrator
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), body, sid)

    def test_agy_tool_names_count_only_on_host_agy(self):
        for tool in ("run_command", "invoke_subagent", "define_subagent", "run_workflow", "swarm"):
            payload = {"session_id": "g", "agent_id": "a1", "tool_name": tool,
                       "tool_input": {"command": "git commit -m x", "Prompt": "p"}}
            for host in ("claude", "codex"):
                r = self.run_guard("PreToolUse", payload, host=host)
                self.assertEqual((r.returncode, r.stdout), (0, ""), (tool, host))
            r = self.run_guard("PreToolUse", {"conversationId": "g", "lastUserInput": "HARNESS ROLE: DELEGATE",
                                              "toolCall": {"name": tool, "args": {"CommandLine": "git commit -m x"}}},
                               host="agy")
            self.assertEqual(json.loads(r.stdout)["decision"], "deny", tool)

    def test_quoted_git_executable_paths_are_caught(self):
        self.make_worker()
        denied = (r'"C:\Program Files\Git\cmd\git.exe" commit -m x', r'"C:\Program Files\Git\cmd\git.exe" push',
                  r'& "C:\Program Files\Git\cmd\git.exe" -C x commit', r"'/usr/bin/git' push origin main",
                  '"git" commit -m x', r'cd r && "C:\Program Files\Git\cmd\GIT.EXE" -c a=b push')
        for cmd in denied:
            self.assertEqual(self.tool("PowerShell", {"command": cmd}).returncode, 2, cmd)
            self.assertEqual(self.tool("Bash", {"command": cmd}).returncode, 2, cmd)
            self.assert_codex_denied(self.run_guard("PreToolUse", {"session_id": "s1", "tool_name": "shell",
                                                                    "tool_input": {"command": cmd}}, host="codex"))
        for cmd in (r'"C:\Program Files\Git\cmd\git.exe" status', r'"C:\Program Files\Git\cmd\git.exe" log -1',
                    'echo "digit" commit', r'"C:\git\tool.exe" commit'):
            self.assertEqual(self.tool("PowerShell", {"command": cmd}).returncode, 0, cmd)

    def test_command_prefix_and_marker_functions(self):
        self.assertEqual(role_guard.strip_command_prefix("/plan  x y"), "x y")
        self.assertEqual(role_guard.strip_command_prefix("/café-cmd x"), "x")
        self.assertEqual(role_guard.strip_command_prefix("/plan"), "/plan")
        self.assertEqual(role_guard.strip_command_prefix("plan /x y"), "plan /x y")
        self.assertTrue(role_guard.has_marker("a\n  /plan HARNESS ROLE: DELEGATE\nb"))
        self.assertFalse(role_guard.has_marker("a HARNESS ROLE: DELEGATE"))
        self.assertFalse(role_guard.has_marker(""))

    def test_argv_is_parsed_without_raising(self):
        self.assertEqual(role_guard.parse_argv(["g", "PreToolUse", "--host", "agy"])[:2], ("agy", "PreToolUse"))
        self.assertEqual(role_guard.parse_argv(["g", "--host", "agy", "SessionStart"])[:2], ("agy", "SessionStart"))
        self.assertEqual(role_guard.parse_argv(["g", "--host"])[:2], ("claude", ""))
        self.assertEqual(role_guard.parse_argv(["g"])[:2], ("claude", ""))



def fill(obj, **values):
    """Replace <name> placeholders in every string of a fixture payload."""
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("<%s>" % k, v)
        return obj
    if isinstance(obj, list):
        return [fill(o, **values) for o in obj]
    if isinstance(obj, dict):
        return {k: fill(v, **values) for k, v in obj.items()}
    return obj


class AgyGuardTest(GuardCase):
    """agy adapter: payloads in tests/fixtures/agy_payloads.json (_source says measured or reconstructed)."""

    ORCH = "gemini-pro-orchestrator-g1-pro"
    EXEC = "gemini-flash-worker-agy"

    def setUp(self):
        super().setUp()
        self.work = os.path.join(self._tmp.name, "work").replace("\\", "/")
        os.makedirs(self.work)
        with open(os.path.join(FIXTURES, "agy_payloads.json"), encoding="utf-8") as f:
            self.fx = json.load(f)
        self.addCleanup(lambda: role_guard._quiet and role_guard._quiet.close())

    # ---- helpers -----------------------------------------------------------------------

    def payload(self, key, cid="c1", command=None, **changes):
        p = fill(self.fx[key]["payload"], cid=cid, workspace=self.work, home=self._tmp.name.replace("\\", "/"))
        if command is not None:
            p["toolCall"]["args"]["CommandLine"] = command
        p.update(changes)
        return p

    def agy(self, key, cid="c1", env=None, command=None, event=None, **changes):
        p = self.payload(key, cid, command, **changes)
        return self.run_guard(event or p["hookEventName"], p, host="agy", env=env)

    def out(self, r):
        """The one JSON object on stdout (exit 0, nothing else)."""
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        obj = json.loads(lines[0])
        self.assertIsInstance(obj, dict)
        return obj

    def assert_ask(self, r):
        self.assertEqual(self.out(r), {"decision": "ask"}, r.stderr)

    def assert_deny(self, r, needle=""):
        obj = self.out(r)
        self.assertEqual(obj["decision"], "deny")
        self.assertEqual(set(obj), {"decision", "reason"})
        self.assertTrue(obj["reason"].startswith("tagteam: "), obj)
        self.assertIn(needle, obj["reason"])
        # the reason is on stderr as well (compared on its ASCII start: OS messages in it follow the locale)
        self.assertTrue(any(line.startswith(obj["reason"][:60]) for line in r.stderr.splitlines()), r.stderr)
        return obj["reason"]

    def record(self, cid="c1"):
        try:
            with open(os.path.join(self.state, "sessions", cid + ".json"), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def model_log(self):
        try:
            with open(os.path.join(self.state, "agy-models.jsonl"), encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    WORKER = {"HARNESS_DELEGATE_RUN": "1"}

    # ---- fixtures ----------------------------------------------------------------------

    def test_every_fixture_says_where_it_comes_from(self):
        entries = {k: v for k, v in self.fx.items() if not k.startswith("_")}
        self.assertGreaterEqual(len(entries), 8)
        for key, entry in entries.items():
            self.assertRegex(entry["_source"], r"^(measured|reconstructed from)", key)
            event = "SessionStart" if key == "session_start" else "PreInvocation" if key.startswith("pre_invocation") else "PreToolUse"
            self.assertEqual(entry["payload"]["hookEventName"], event, key)
        for key in ("session_start", "pre_invocation", "pre_tool_run_command"):
            self.assertTrue(self.fx[key]["_source"].startswith("measured"), key)
        self.assertEqual(self.payload("pre_invocation_subagent")["lastUserInput"], "")

    # ---- input conversion --------------------------------------------------------------

    def test_internal_shape(self):
        internal = role_guard.agy_internal(self.payload("pre_tool_run_command", command="git log"))
        self.assertEqual(internal["session_id"], "c1")
        self.assertEqual(internal["cwd"], self.work)
        self.assertEqual(internal["prompt"], "Reply with exactly: OK")
        self.assertEqual((internal["tool_name"], internal["tool_input"]["command"]), ("run_command", "git log"))
        self.assertEqual(internal["tool_input"]["WaitMsBeforeAsync"], 5000)
        self.assertEqual(internal["model"], "gemini-3.8-flash-low")
        no_cwd = self.payload("pre_invocation")
        del no_cwd["cwd"]
        self.assertEqual(role_guard.agy_internal(no_cwd)["cwd"], self.work)  # workspacePaths[0]
        for bad in ({"toolCall": 5}, {"toolCall": {"args": []}}, {"lastUserInput": 3}, {"workspacePaths": "x", "cwd": 1}):
            role_guard.agy_internal(bad)  # odd shapes do not raise

    def test_state_folder_comes_from_the_payload_not_from_claude_project_dir(self):
        repo = os.path.join(self._tmp.name, "repo")
        other = os.path.join(self._tmp.name, "other")
        for d in (repo, other):
            os.makedirs(d)
            subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        p = self.payload("pre_invocation", cid="g1")
        p["cwd"] = ""
        p["workspacePaths"] = [repo.replace("\\", "/")]
        r = self.run_guard("PreInvocation", p, host="agy", env={"TAGTEAM_STATE_DIR": "", "CLAUDE_PROJECT_DIR": other})
        self.assertEqual(self.out(r), {})
        self.assertTrue(os.path.exists(os.path.join(repo, ".tagteam", "sessions", "g1.json")))
        self.assertFalse(os.path.exists(os.path.join(other, ".tagteam")))

    # ---- SessionStart ------------------------------------------------------------------

    def test_session_start_injects_one_system_message_and_decides_nothing(self):
        obj = self.out(self.agy("session_start"))
        self.assertEqual(list(obj), ["injectSteps"])
        steps = obj["injectSteps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["systemMessage"]["systemMessage"],
                         "TAGTEAM: role=pending host=agy (set by the first model call: worker if it carries "
                         "HARNESS ROLE: DELEGATE or has no user input)")
        self.assertIsNone(self.record())  # the role is decided by the first model call, not here
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions")))

    def test_session_start_with_a_marker_or_empty_input_still_records_nothing(self):
        for text in ("HARNESS ROLE: DELEGATE\nx", ""):
            r = self.agy("session_start", lastUserInput=text)
            self.assertIn("role=pending", self.out(r)["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertIsNone(self.record())

    def test_session_start_shows_the_recorded_role(self):
        self.agy("pre_invocation")
        msg = self.out(self.agy("session_start"))["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertEqual(msg, "TAGTEAM: role=orchestrator host=agy")

    def test_session_start_of_a_launched_worker(self):
        msg = self.out(self.agy("session_start", env=self.WORKER))["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertEqual(msg, "TAGTEAM: role=worker host=agy")

    def test_session_start_collects_the_notices_in_one_message(self):
        with open(os.path.join(self.cfg, "models.local.json"), "w", encoding="utf-8") as f:
            json.dump({"tasks": {"html": {"about": "pages", "base": "implement"}}}, f)
        import datetime
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(self.state, "model-update.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "created": created, "cli": {}, "digest": "d", "changes": [{"kind": "new_model"}]}, f)
        msg = self.out(self.agy("session_start"))["injectSteps"][0]["systemMessage"]["systemMessage"].split("\n")
        self.assertEqual(len(msg), 3)
        self.assertIn("role=pending host=agy", msg[0])
        self.assertIn("model update proposal", msg[1])
        self.assertEqual(msg[2], "TAGTEAM: task html - pages (route.py --task html)")
        # a worker gets no notices
        msg = self.out(self.agy("session_start", env=self.WORKER))["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertEqual(msg, "TAGTEAM: role=worker host=agy")

    # ---- PreInvocation: the role is decided here ----------------------------------------

    def test_first_pre_invocation_records_an_orchestrator(self):
        self.assertEqual(self.out(self.agy("pre_invocation")), {})
        rec = self.record()
        self.assertEqual((rec["role"], rec["host"]), ("orchestrator", "agy"))
        self.assertNotIn("basis", rec)

    def test_marker_input_records_a_worker_and_it_never_changes(self):
        self.assertEqual(self.out(self.agy("pre_invocation", lastUserInput="x\nHARNESS ROLE: DELEGATE\ny")), {})
        self.assertEqual(self.record()["role"], "worker")
        self.agy("pre_invocation", lastUserInput="hello")  # later inputs never change the record
        self.assertEqual(self.record()["role"], "worker")
        self.agy("pre_invocation", cid="c2")
        self.agy("pre_invocation", cid="c2", lastUserInput="HARNESS ROLE: DELEGATE")
        self.assertEqual(self.record("c2")["role"], "orchestrator")

    def test_slash_command_prefix_before_the_marker_makes_a_worker(self):
        self.agy("pre_invocation_plan_prefix")  # "/plan HARNESS ROLE: DELEGATE\n..." (G9)
        self.assertEqual(self.record()["role"], "worker")
        self.agy("pre_invocation", cid="c2", lastUserInput="/plan write a design")
        self.assertEqual(self.record("c2")["role"], "orchestrator")

    def test_empty_first_input_is_a_worker(self):  # R1: an agy subagent starts with an empty input
        self.assertEqual(self.out(self.agy("pre_invocation_subagent")), {})
        rec = self.record()
        self.assertEqual((rec["role"], rec["basis"]), ("worker", "empty-input"))
        for blank in ("", "  \n\t"):
            self.agy("pre_invocation", cid="b" + str(len(blank)), lastUserInput=blank)
            self.assertEqual(self.record("b" + str(len(blank)))["role"], "worker")

    def test_resumed_conversation_without_session_start_is_decided_by_pre_invocation(self):
        # F7: --conversation sends PreInvocation only; there was no earlier record
        self.agy("pre_invocation", cid="old", lastUserInput="reply OK")
        self.assertEqual(self.record("old")["role"], "orchestrator")
        self.assertEqual(self.out(self.agy("pre_tool_run_command", cid="old", command="git commit -m x")), {"decision": "ask"})

    def test_launched_worker_is_a_worker_whatever_the_record_says(self):
        self.agy("pre_invocation", lastUserInput="hello")  # recorded as orchestrator
        self.assert_deny(self.agy("pre_tool_run_command", env=self.WORKER, command="git commit -m x"), "git commit")
        r = self.agy("pre_tool_run_command", env=self.WORKER, command="git status")
        self.assert_ask(r)
        # a launched session with plain input is recorded as the worker it is enforced as
        self.agy("pre_invocation", cid="plain", env=self.WORKER, lastUserInput="just do it")
        self.assertEqual(self.record("plain")["role"], "worker")
        self.agy("pre_tool_run_command", cid="plain2", env=self.WORKER, command="git status")
        self.assertEqual(self.record("plain2")["role"], "worker")
        # recorded like the other hosts do, and the empty-input note is not claimed for it
        self.agy("pre_invocation_subagent", cid="e", env=self.WORKER)
        reason = self.assert_deny(self.agy("pre_tool_run_command", cid="e", env=self.WORKER, command="git push"))
        self.assertNotIn("no user input", reason)

    # ---- PreToolUse --------------------------------------------------------------------

    def test_worker_git_commit_and_push_denied_status_asks(self):
        self.agy("pre_invocation", lastUserInput="HARNESS ROLE: DELEGATE\nx")
        for cmd in ("git commit -m x", "git push origin main", "git -C x commit -m y", 'git -C "a b" push',
                    "GIT.EXE commit -m x", r"C:\Git\bin\git.exe push", "cd r && git -c user.name=a commit"):
            self.assert_deny(self.agy("pre_tool_run_command", command=cmd), "git commit or git push")
        for cmd in ("git status", "git log --oneline", "echo digit commit", "python -m unittest"):
            self.assert_ask(self.agy("pre_tool_run_command", command=cmd))

    def test_orchestrator_commit_asks(self):
        self.agy("pre_invocation")
        self.assert_ask(self.agy("pre_tool_run_command", command="git commit -m x"))
        self.assert_ask(self.agy("pre_tool_run_command", command="git push"))

    def test_worker_delegation_tools_denied_orchestrator_asks(self):
        for tool in ("invoke_subagent", "define_subagent", "run_workflow", "swarm"):
            self.assert_deny(self.agy("pre_tool_invoke_subagent", env=self.WORKER, toolCall={
                "name": tool, "id": "x", "args": {"Prompt": "p"}}), "may not spawn subagents")
            self.assert_ask(self.agy("pre_tool_invoke_subagent", cid="orch", toolCall={
                "name": tool, "id": "x", "args": {"Prompt": "p"}}))
        # the measured invoke_subagent payload (reconstructed G5), parent is a worker through its record
        self.agy("pre_invocation", cid="w", lastUserInput="HARNESS ROLE: DELEGATE\nx")
        self.assert_deny(self.agy("pre_tool_invoke_subagent", cid="w"), "may not spawn subagents")

    def test_tools_the_guard_has_no_opinion_on_ask(self):
        self.assert_ask(self.agy("pre_tool_exit_plan_mode"))
        self.assert_ask(self.agy("pre_tool_exit_plan_mode", env=self.WORKER))
        self.assert_ask(self.agy("pre_tool_run_command", command=""))
        self.assert_ask(self.agy("pre_tool_run_command", toolCall={"name": "write_file", "args": {"TargetFile": "AGENTS.md"}},
                                 env=self.WORKER))  # edit tools are not matched: control plane edits are not checked on agy

    def test_pre_tool_use_without_a_record_decides_and_records(self):
        # PreInvocation hook failed or never ran: the subagent's first sign is a PreToolUse with an empty input
        reason = self.assert_deny(self.agy("pre_tool_subagent_commit"), "git commit")
        self.assertIn("role recorded as worker: the first model call had no user input", reason)
        rec = self.record()
        self.assertEqual((rec["role"], rec["basis"]), ("worker", "empty-input"))
        # the later denies of that conversation say it too (the role came from the record)
        self.assertIn("no user input", self.assert_deny(self.agy("pre_tool_subagent_commit")))
        # a normal first input decides an orchestrator in the same way
        self.assert_ask(self.agy("pre_tool_run_command", cid="o", command="git commit -m x"))
        self.assertEqual(self.record("o")["role"], "orchestrator")
        self.assert_deny(self.agy("pre_tool_run_command", cid="m", lastUserInput="HARNESS ROLE: DELEGATE", command="git push"))
        self.assertEqual(self.record("m")["role"], "worker")

    def test_unwritable_state_folder_still_applies_the_decided_role(self):
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        env = {"TAGTEAM_STATE_DIR": os.path.join(blocker, "s")}
        r = self.agy("pre_tool_subagent_commit", env=env)
        self.assert_deny(r, "git commit")  # the warning goes to stderr as well
        self.assertIn("not writable", r.stderr)
        self.assertIn("role unresolved", json.loads(r.stdout)["reason"])
        # an orchestrator's protected call is held too: the first role cannot be kept, so it is not trusted
        r = self.agy("pre_tool_run_command", env=env, command="git commit -m x")
        self.assert_deny(r, "git commit")
        self.assertIn("could not be written", json.loads(r.stdout)["reason"])
        self.assert_ask(self.agy("pre_tool_run_command", env=env, command="git status"))  # not protected
        self.assertEqual(self.out(self.agy("pre_invocation", env=env)), {})
        self.assertEqual(self.out(self.agy("pre_invocation", env=env, modelName="m2")), {})

    def unwritable(self):
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        return {"TAGTEAM_STATE_DIR": os.path.join(blocker, "s")}

    def test_failed_record_write_cannot_be_bypassed_by_a_later_event_with_other_input(self):
        env = self.unwritable()
        # the worker marker is seen by PreInvocation, but its record cannot be written ...
        self.assertEqual(self.out(self.agy("pre_invocation", env=env, lastUserInput="HARNESS ROLE: DELEGATE\nx")), {})
        # ... and the next event of the same conversation carries an ordinary input (an orchestrator by that input)
        for cmd in ("git commit -m x", "git push"):
            reason = self.assert_deny(self.agy("pre_tool_run_command", env=env, command=cmd, lastUserInput="plain"),
                                      "git commit or git push")
            self.assertIn("role unresolved", reason)
        self.assert_deny(self.agy("pre_tool_invoke_subagent", env=env, lastUserInput="plain"), "may not spawn subagents")
        self.assert_ask(self.agy("pre_tool_run_command", env=env, command="git status", lastUserInput="plain"))
        self.assert_ask(self.agy("pre_tool_exit_plan_mode", env=env, lastUserInput="plain"))

    def test_unreadable_or_corrupt_record_holds_protected_calls(self):
        sessions = os.path.join(self.state, "sessions")
        os.makedirs(sessions)
        def text(body):
            def make(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body)
            return make
        cases = {"corrupt": text("{broken"), "unknown role": text('{"role": "boss"}'), "not an object": text("[1]"),
                 "unreadable": os.mkdir}  # a folder where the file should be
        for name, make in cases.items():
            cid = name.replace(" ", "-")
            path = os.path.join(sessions, cid + ".json")
            make(path)
            self.agy("pre_invocation", cid=cid, lastUserInput="HARNESS ROLE: DELEGATE\nx")  # would have decided a worker
            reason = self.assert_deny(self.agy("pre_tool_run_command", cid=cid, command="git commit -m x",
                                               lastUserInput="plain"), "git commit")
            self.assertIn("unreadable or corrupt", reason, name)
            self.assert_ask(self.agy("pre_tool_run_command", cid=cid, command="git log", lastUserInput="plain"))
            if os.path.isfile(path):
                with open(path) as f:
                    self.assertNotEqual(f.read(), "", name)  # never rewritten
        with open(os.path.join(sessions, "corrupt.json")) as f:
            self.assertEqual(f.read(), "{broken")

    def test_payload_without_a_conversation_id_holds_protected_calls(self):
        p = self.payload("pre_tool_run_command", command="git commit -m x")
        del p["conversationId"]
        reason = self.assert_deny(self.run_guard("PreToolUse", p, host="agy"), "git commit")
        self.assertIn("no conversation id", reason)
        p["toolCall"]["args"]["CommandLine"] = "git status"
        self.assert_ask(self.run_guard("PreToolUse", p, host="agy"))

    def test_a_launched_worker_is_not_unsure(self):
        env = dict(self.unwritable(), **self.WORKER)
        reason = self.assert_deny(self.agy("pre_tool_run_command", env=env, command="git push"), "git commit or git push")
        self.assertNotIn("role unresolved", reason)

    def test_corrupt_record_does_not_change_the_decision(self):
        os.makedirs(os.path.join(self.state, "sessions"))
        with open(os.path.join(self.state, "sessions", "c1.json"), "w") as f:
            f.write("{broken")
        self.assert_deny(self.agy("pre_tool_subagent_commit"), "git commit")  # decided from the empty input
        with open(os.path.join(self.state, "sessions", "c1.json")) as f:
            self.assertEqual(f.read(), "{broken")  # an existing file is never rewritten

    # ---- every path prints one JSON object, exit 0 --------------------------------------

    def test_broken_input_answers_ask_for_pre_tool_use_and_empty_otherwise(self):
        for raw in ("{not json", "[1]", "null", '"x"', "", "  \n"):
            for event in ("PreToolUse", "SessionStart", "PreInvocation", "Stop"):
                r = self.run_guard(event, raw=raw, host="agy")
                obj = self.out(r)
                if event == "PreToolUse":
                    self.assertEqual(obj, {"decision": "ask"}, raw)
                elif event == "SessionStart" and not raw.strip():
                    self.assertEqual(list(obj), ["injectSteps"])  # an empty payload is a payload with no fields
                else:
                    self.assertEqual(obj, {}, (event, raw))
                if raw.strip():
                    self.assertTrue("warning" in r.stderr or "unknown event" in r.stderr, (event, raw))

    def test_non_utf8_input_does_not_break_the_output(self):
        e = dict(os.environ, TAGTEAM_STATE_DIR=self.state, TAGTEAM_CONFIG_DIR=self.cfg)
        e.pop("HARNESS_DELEGATE_RUN", None)
        data = json.dumps(self.payload("pre_tool_run_command")).encode("utf-8").replace(b"git status", b"git status \xff\xfe")
        r = subprocess.run([sys.executable, GUARD, "PreToolUse", "--host", "agy"], input=data, capture_output=True, env=e, timeout=30)
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"decision": "ask"}))

    def test_multibyte_input_is_read_as_utf8(self):
        self.agy("pre_invocation", lastUserInput="café über\nHARNESS ROLE: DELEGATE\nñ")
        self.assertEqual(self.record()["role"], "worker")
        self.agy("pre_invocation", cid="c2", lastUserInput="café über only")
        self.assertEqual(self.record("c2")["role"], "orchestrator")

    def test_unknown_event_prints_an_empty_object(self):
        r = self.run_guard("Stop", self.payload("pre_invocation"), host="agy")
        self.assertEqual(self.out(r), {})
        self.assertIn("unknown event", r.stderr)
        self.assertEqual(self.out(self.run_guard("", {}, host="agy")), {})

    def in_process(self, event, raw, handler=None, stderr=None):
        """main_agy in this process with stdin/stdout replaced: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), stderr or io.StringIO()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(raw.encode("utf-8")))
        env = {"TAGTEAM_STATE_DIR": self.state, "TAGTEAM_CONFIG_DIR": self.cfg}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            os.environ.pop("HARNESS_DELEGATE_RUN", None)
            if handler:
                with mock.patch.dict(role_guard.AGY_HANDLERS, {event: handler}):
                    code = role_guard.main_agy(event)
            else:
                code = role_guard.main_agy(event)
        return code, out.getvalue(), err.getvalue()

    def test_handler_exception_answers_ask_or_empty(self):
        def boom(payload):
            raise RuntimeError("boom")
        raw = json.dumps(self.payload("pre_tool_run_command"))
        for event, want in (("PreToolUse", {"decision": "ask"}), ("PreInvocation", {}), ("SessionStart", {})):
            code, out, err = self.in_process(event, raw, boom)
            self.assertEqual((code, json.loads(out)), (0, want), event)
            self.assertEqual(len(out.splitlines()), 1)
            self.assertIn("RuntimeError: boom", err)

    class BrokenStderr:
        def write(self, text):
            raise OSError("stderr is closed")

        def getvalue(self):
            return ""

    def test_failing_stderr_keeps_the_decision_and_exit_code(self):
        self.agy("pre_invocation", lastUserInput="HARNESS ROLE: DELEGATE\nx")
        raw = json.dumps(self.payload("pre_tool_run_command", command="git commit -m x"))
        code, out, _ = self.in_process("PreToolUse", raw, stderr=self.BrokenStderr())
        obj = json.loads(out)
        self.assertEqual((code, obj["decision"]), (0, "deny"))  # the deny is not lost with its diagnostic line
        self.assertEqual(len(out.splitlines()), 1)

        def boom(payload):
            raise RuntimeError("boom")
        code, out, _ = self.in_process("PreToolUse", raw, boom, stderr=self.BrokenStderr())
        self.assertEqual((code, json.loads(out)), (0, {"decision": "ask"}))
        code, out, _ = self.in_process("Stop", raw, stderr=self.BrokenStderr())  # unknown event note
        self.assertEqual((code, json.loads(out)), (0, {}))
        # and no stderr at all
        with mock.patch.object(sys, "stderr", None):
            role_guard.note("x")
            role_guard.warn("y")

    def test_state_error_in_a_handler_answers_ask(self):
        def unwritable(payload):
            raise role_guard.state.StateError("state folder x is not writable")
        code, out, err = self.in_process("PreToolUse", json.dumps(self.payload("pre_tool_run_command")), unwritable)
        self.assertEqual((code, json.loads(out)), (0, {"decision": "ask"}))
        self.assertIn("not writable", err)

    def test_outer_backstop_prints_the_fallback_once(self):
        # the output step itself fails once (before anything is written): the __main__ guard prints the fallback
        real = role_guard.json.dumps
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("cannot encode")
            return real(*a, **k)

        out, err = io.StringIO(), io.StringIO()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(self.payload("pre_tool_run_command")).encode()))
        env = {"TAGTEAM_STATE_DIR": self.state, "TAGTEAM_CONFIG_DIR": self.cfg}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err), mock.patch.object(role_guard.json, "dumps", flaky), \
                mock.patch.object(sys, "argv", [GUARD, "PreToolUse", "--host", "agy"]):
            os.environ.pop("HARNESS_DELEGATE_RUN", None)
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(GUARD, run_name="__main__")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"decision": "ask"})
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn("cannot encode", err.getvalue())

    # ---- model log (mitigation C) ------------------------------------------------------

    def test_model_changes_are_logged_once_per_change(self):
        self.agy("pre_invocation", modelName=self.ORCH)
        self.agy("pre_invocation", modelName=self.ORCH)  # same model: nothing new
        self.agy("pre_invocation_executor", modelName=self.EXEC)  # G8: the plan hands over to the execution side
        self.agy("pre_invocation_executor", modelName=self.EXEC)
        self.agy("pre_invocation", modelName=self.ORCH)  # a change back is a change
        log = self.model_log()
        self.assertEqual([(r["cid"], r["model"]) for r in log], [("c1", self.ORCH), ("c1", self.EXEC), ("c1", self.ORCH)])
        for rec in log:
            self.assertEqual(set(rec), {"ts", "cid", "model"})
            self.assertIsInstance(rec["ts"], int)

    def test_model_log_is_kept_per_conversation_and_ignores_missing_names(self):
        self.agy("pre_invocation", cid="a", modelName="m1")
        self.agy("pre_invocation", cid="b", modelName="m1")  # another conversation: its own first record
        self.agy("pre_invocation", cid="a", modelName="m1")
        p = self.payload("pre_invocation", cid="c")
        del p["modelName"]
        self.run_guard("PreInvocation", p, host="agy")
        self.assertEqual([(r["cid"], r["model"]) for r in self.model_log()], [("a", "m1"), ("b", "m1")])

    def test_model_log_only_on_pre_invocation_and_survives_a_broken_line(self):
        self.agy("pre_tool_run_command", modelName="m1")
        self.agy("session_start", modelName="m1")
        self.assertEqual(self.model_log(), [])
        with open(os.path.join(self.state, "agy-models.jsonl"), "w", encoding="utf-8") as f:
            f.write("not json\n" + json.dumps({"ts": 1, "cid": "c1", "model": "m1"}) + "\n[1]\n")
        self.agy("pre_invocation", modelName="m1")
        self.agy("pre_invocation", modelName="m2")
        with open(os.path.join(self.state, "agy-models.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 4)

    def test_model_log_failure_keeps_the_role_record_and_output(self):
        os.makedirs(os.path.join(self.state, "agy-models.jsonl"))  # a directory where the log file should be
        r = self.agy("pre_invocation", modelName="m1")
        self.assertEqual(self.out(r), {})
        self.assertIn("warning", r.stderr)
        self.assertEqual(self.record()["role"], "orchestrator")

    def test_an_unterminated_log_line_does_not_swallow_the_next_record(self):
        path = os.path.join(self.state, "agy-models.jsonl")
        with open(path, "w", encoding="utf-8") as f:  # the last write was cut short: no newline at the end
            f.write(json.dumps({"ts": 1, "cid": "c1", "model": "m1"}))
        self.agy("pre_invocation", modelName="m2")
        self.agy("pre_invocation", modelName="m3")
        self.assertEqual([(r["cid"], r["model"]) for r in self.model_log()], [("c1", "m1"), ("c1", "m2"), ("c1", "m3")])
        with open(path, "w", encoding="utf-8") as f:  # a fragment of a record
            f.write('{"ts": 1, "cid": "c1", "mo')
        self.agy("pre_invocation", modelName="m4")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], '{"ts": 1, "cid": "c1", "mo')
        self.assertEqual(json.loads(lines[1])["model"], "m4")
        self.agy("pre_invocation", modelName="m5")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 3)  # a terminated file gets no blank line

    # ---- role record creation (atomic, never replaced) ----------------------------------

    def test_create_record_publishes_once_and_leaves_no_temporary_file(self):
        path = os.path.join(self.state, "sessions", "x.json")
        self.assertTrue(role_guard.create_record(path, "worker", "agy"))
        self.assertFalse(role_guard.create_record(path, "orchestrator", "agy"))
        rec, status = role_guard.load_record(path)
        self.assertEqual((rec["role"], status), ("worker", "ok"))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["x.json"])

    def test_create_record_without_hard_links_still_refuses_to_replace(self):
        path = os.path.join(self.state, "sessions", "y.json")
        with mock.patch.object(os, "link", side_effect=OSError("no hard links")):
            self.assertTrue(role_guard.create_record(path, "worker", "agy"))
            self.assertFalse(role_guard.create_record(path, "orchestrator", "agy"))
        self.assertEqual(role_guard.load_record(path)[0]["role"], "worker")
        self.assertEqual(os.listdir(os.path.dirname(path)), ["y.json"])

    def test_losing_the_creation_race_enforces_the_winners_record(self):
        # both hooks read "no record"; the other one publishes between this hook's read and its write
        path = os.path.join(self.state, "sessions", "c1.json")
        real = role_guard.load_record
        calls = []

        def racing(p):
            result = real(p)
            if not calls:
                calls.append(1)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"role": "orchestrator", "host": "agy", "ts": 1}, f)
            return result

        internal = role_guard.agy_internal(self.payload("pre_invocation", lastUserInput="HARNESS ROLE: DELEGATE\nx"))
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": self.state}), \
                mock.patch.object(role_guard, "load_record", racing):
            os.environ.pop("HARNESS_DELEGATE_RUN", None)
            role, basis, unsure = role_guard.agy_role(internal)
        self.assertEqual((role, basis, unsure), ("orchestrator", None, None))  # the winner's role, not this input's
        self.assertEqual(real(path)[0], {"role": "orchestrator", "host": "agy", "ts": 1})  # not replaced

    def test_losing_the_race_to_an_unreadable_record_holds_protected_calls(self):
        real = role_guard.load_record
        calls = []

        def racing(p):
            result = real(p)
            if not calls:
                calls.append(1)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write("{half")
            return result

        internal = role_guard.agy_internal(self.payload("pre_invocation"))
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": self.state}), \
                mock.patch.object(role_guard, "load_record", racing):
            os.environ.pop("HARNESS_DELEGATE_RUN", None)
            role, basis, unsure = role_guard.agy_role(internal)
        self.assertIn("unreadable or corrupt", unsure)

    def test_without_hard_links_only_a_complete_record_is_ever_visible(self):
        path = os.path.join(self.state, "sessions", "z.json")
        real_replace = os.replace
        seen = []

        def checking_replace(src, dst):
            with open(src, encoding="utf-8") as f:
                seen.append((os.path.exists(dst), json.load(f)["role"]))  # complete, not yet visible
            return real_replace(src, dst)

        with mock.patch.object(os, "link", side_effect=OSError("no hard links")), \
                mock.patch.object(os, "replace", checking_replace):
            self.assertTrue(role_guard.create_record(path, "worker", "agy"))
        self.assertEqual(seen, [(False, "worker")])
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["z.json"])  # temporary file and lock are gone

    def test_without_hard_links_a_lock_folder_serialises_the_writers(self):
        path = os.path.join(self.state, "sessions", "l.json")
        os.makedirs(path + ".lock")  # another process is publishing right now
        with mock.patch.object(os, "link", side_effect=OSError("no hard links")):
            self.assertFalse(role_guard.create_record(path, "worker", "agy"))
            self.assertFalse(os.path.exists(path))
            old = os.path.getmtime(path + ".lock") - 3600  # a lock left behind by a process that died
            os.utime(path + ".lock", (old, old))
            self.assertTrue(role_guard.create_record(path, "worker", "agy"))
        self.assertEqual(role_guard.load_record(path)[1], "ok")
        self.assertFalse(os.path.exists(path + ".lock"))

    def test_a_record_published_by_a_hook_that_won_is_what_the_loser_enforces_without_hard_links(self):
        # the loser sees the lock held: no record yet, so it must hold protected calls, not trust its own input
        path = os.path.join(self.state, "sessions", "c1.json")
        os.makedirs(path + ".lock")
        internal = role_guard.agy_internal(self.payload("pre_invocation"))
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": self.state}), \
                mock.patch.object(os, "link", side_effect=OSError("no hard links")):
            os.environ.pop("HARNESS_DELEGATE_RUN", None)
            role, basis, unsure = role_guard.agy_role(internal)
        self.assertIn("role record", unsure)
        self.assertFalse(os.path.exists(path))

    # ---- the first decision is lost: nothing promotes the conversation to an orchestrator ----

    def test_a_lost_first_decision_is_not_turned_into_an_orchestrator(self):
        real = role_guard.create_record
        calls = []

        def flaky(*a, **k):  # the first write fails; the storage has recovered by the second
            if not calls:
                calls.append(1)
                raise role_guard.state.StateError("state folder x is not writable")
            return real(*a, **k)

        marker = self.payload("pre_invocation", lastUserInput="HARNESS ROLE: DELEGATE\nx")  # invocationNum 0
        later = self.payload("pre_invocation_executor", lastUserInput="an ordinary request")  # invocationNum 1
        with mock.patch.object(role_guard, "create_record", flaky):
            self.assertEqual(json.loads(self.in_process("PreInvocation", json.dumps(marker))[1]), {})
            self.assertIsNone(self.record())  # the worker decision was lost with the failed write
            self.assertEqual(json.loads(self.in_process("PreInvocation", json.dumps(later))[1]), {})
        self.assertEqual(self.record()["role"], "unresolved")  # the second write succeeded: not an orchestrator
        path = os.path.join(self.state, "sessions", "c1.json")
        for tool, args in (("run_command", {"CommandLine": "git commit -m x"}), ("invoke_subagent", {"Prompt": "p"})):
            raw = json.dumps(self.payload("pre_tool_run_command", lastUserInput="an ordinary request",
                                          toolCall={"name": tool, "args": args}))
            out = json.loads(self.in_process("PreToolUse", raw)[1])
            self.assertEqual(out["decision"], "deny", tool)
            self.assertIn("role unresolved", out["reason"])
            self.assertIn("check the state folder", out["reason"])
            self.assertIn("remove ", out["reason"])
            self.assertIn("c1.json", out["reason"])
        raw = json.dumps(self.payload("pre_tool_run_command", lastUserInput="an ordinary request", command="git status"))
        self.assertEqual(json.loads(self.in_process("PreToolUse", raw)[1]), {"decision": "ask"})
        self.assertEqual(role_guard.load_record(path)[1], "unresolved")  # never rewritten

    def test_first_decision_rules_by_invocation_number(self):
        cases = (("c0", 0, "plain input", "orchestrator"), ("cn", None, "plain input", "orchestrator"),
                 ("c3", 3, "plain input", "unresolved"), ("c3m", 3, "HARNESS ROLE: DELEGATE\nx", "worker"),
                 ("c3e", 3, "", "worker"), ("c0m", 0, "HARNESS ROLE: DELEGATE\nx", "worker"))
        for cid, number, text, want in cases:
            p = self.payload("pre_invocation", cid=cid, lastUserInput=text)
            if number is None:
                del p["invocationNum"]
            else:
                p["invocationNum"] = number
            self.assertEqual(self.out(self.run_guard("PreInvocation", p, host="agy")), {})
            self.assertEqual(self.record(cid)["role"], want, cid)

    def test_pre_tool_use_without_any_record_still_decides_from_its_own_input(self):
        # known limit: when the PreInvocation hook itself never ran or failed, nothing says the first decision
        # was lost, so the PreToolUse input decides (design 3.3)
        self.assert_ask(self.agy("pre_tool_run_command", command="git commit -m x", lastUserInput="ordinary"))
        self.assertEqual(self.record()["role"], "orchestrator")

    def test_closed_stderr_pipe_keeps_exit_code_zero(self):
        env = dict(os.environ, TAGTEAM_STATE_DIR=self.state, TAGTEAM_CONFIG_DIR=self.cfg, HARNESS_DELEGATE_RUN="1")
        denied = self.payload("pre_tool_run_command", command="git push")
        for event, payload, want in (("PreToolUse", denied, "deny"), ("Stop", denied, None), ("PreInvocation", denied, None)):
            read_end, write_end = os.pipe()
            os.close(read_end)  # nobody reads this stderr any more
            try:
                r = subprocess.run([sys.executable, GUARD, event, "--host", "agy"], input=json.dumps(payload),
                                   stdout=subprocess.PIPE, stderr=write_end, text=True, encoding="utf-8", env=env, timeout=30)
            finally:
                os.close(write_end)
            self.assertEqual(r.returncode, 0, event)
            out = json.loads(r.stdout)
            self.assertEqual(out.get("decision"), want, event)
            self.assertEqual(len(r.stdout.splitlines()), 1)

    def test_the_other_hosts_never_replace_a_record_either(self):
        for text in ("hello", "HARNESS ROLE: DELEGATE"):
            self.run_guard("UserPromptSubmit", {"session_id": "r1", "prompt": text})
        self.assertEqual(self.run_guard("SessionStart", {"session_id": "r1"}).stdout.strip(),
                         "TAGTEAM: role=orchestrator host=claude")


if __name__ == "__main__":
    unittest.main()
