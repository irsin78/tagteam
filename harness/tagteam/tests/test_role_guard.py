import json
import os
import subprocess
import sys
import tempfile
import unittest

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "role_guard.py")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
START = "mcp__ccd_session__start_session"
SPAWN = "collaborationspawn_agent"


def pending(host="claude"):
    return "TAGTEAM: role=pending host=%s (set by the first prompt: worker if it carries HARNESS ROLE: DELEGATE)" % host


def codex_payloads():
    with open(os.path.join(FIXTURES, "codex_payloads.json"), encoding="utf-8") as f:
        return json.load(f)


class RoleGuardTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
