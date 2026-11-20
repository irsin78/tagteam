import io
import json
import os
import runpy
import shutil
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


def make_dir_link(link, target):
    """A directory link to target: a symlink, or on Windows (symlinks need a privilege) a junction. False when
    this system can make neither."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        return os.path.isdir(link)
    return False


def remove_dir_link(link):
    """Remove the link itself, never what it points at."""
    try:
        os.unlink(link)
    except OSError:
        try:
            os.rmdir(link)  # a junction
        except OSError:
            pass


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

    # ---- the same folder under two spellings (links, junctions): M7 U1 ------------------

    def make_link(self, link, target):
        if not make_dir_link(link, target):
            self.skipTest("this system cannot make a directory link or junction")
        self.addCleanup(remove_dir_link, link)  # before the temporary folder is removed

    def test_control_plane_is_found_through_a_link_to_the_project(self):
        # git prints the real folder as the top; the model may write the path through the link, or the cwd may be
        # the link (macOS: /var -> /private/var; elsewhere a symlink or junction to the project)
        real, link = os.path.join(self._tmp.name, "real-repo"), os.path.join(self._tmp.name, "link-repo")
        os.makedirs(os.path.join(real, "src"))
        os.makedirs(os.path.join(real, ".claude"))
        subprocess.run(["git", "-C", real, "init", "-q"], check=True)
        self.make_link(link, real)
        inner = os.path.join(real, "plain-looking")  # a link inside the project that points at the control plane
        self.make_link(inner, os.path.join(real, ".claude"))
        self.make_worker()
        denied = (  # (cwd, path)
            (real, os.path.join(link, "AGENTS.md")), (real, os.path.join(link, ".claude", "x")),
            (link, os.path.join(real, "CLAUDE.md")), (link, os.path.join(real, ".agents", "x")),
            (link, "AGENTS.md"), (link, ".codex/x"), (link, "src/../.claude/x"), (os.path.join(link, "src"), "../AGENTS.md"),
            (real, os.path.join(inner, "settings.json")), (real, "plain-looking/settings.json"),
            (link, os.path.join(link, "plain-looking", "settings.json")))
        for cwd, path in denied:
            r = self.tool("Edit", {"file_path": path}, cwd=cwd)
            self.assertEqual(r.returncode, 2, (cwd, path))
            self.assertIn("control plane", r.stderr)
            self.assert_codex_denied(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)),
                                     "control plane")
        for cwd, path in ((real, os.path.join(link, "src", "a.py")), (link, os.path.join(real, "src", "a.py")),
                          (link, "src/a.py"), (link, "src/AGENTS.md"), (os.path.join(link, "src"), "AGENTS.md"),
                          (real, os.path.join(link, "docs", "CLAUDE.md"))):  # not the root files: still allowed
            self.assertEqual(self.tool("Edit", {"file_path": path}, cwd=cwd).returncode, 0, (cwd, path))
            self.assert_allowed(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)))

    def test_both_spellings_of_a_folder_are_judged(self):
        # no operating-system links: realpath and the git top are faked so that one folder has two names. The
        # real spelling is what git prints; the payload uses the other one.
        real, link, elsewhere = (os.path.join(self._tmp.name, n) for n in ("real-name", "link-name", "elsewhere"))

        def fake_realpath(path):
            norm = os.path.normpath(path)
            if norm == link or norm.startswith(link + os.sep):
                return real + norm[len(link):]
            if norm == os.path.join(real, ".claude", "moved"):  # a link inside the project that leaves the control plane
                return os.path.join(elsewhere, "x")
            return norm

        with mock.patch.object(role_guard.state, "_git", return_value=real), \
                mock.patch.object(role_guard.os.path, "realpath", side_effect=fake_realpath):
            sub = os.path.join(link, "sub")
            for cwd, path in ((sub, "../AGENTS.md"), (sub, "../.claude/settings.json"), (sub, os.path.join(link, ".agents", "x")),
                              (link, "CLAUDE.md"), (link, os.path.join(real, ".codex", "x")), (real, os.path.join(link, "AGENTS.md")),
                              (sub, "../.Claude/x"), (real, os.path.join(real, ".claude", "moved"))):
                self.assertEqual(role_guard.control_plane_path([path], {"cwd": cwd}), path, (cwd, path))
                with self.assertRaises(role_guard.Denied):  # Claude's Edit, as a worker (a subagent)
                    role_guard.on_pre_tool({"cwd": cwd, "agent_id": "a", "tool_name": "Edit", "tool_input": {"file_path": path}}, "claude")
            for cwd, path in ((sub, "AGENTS.md"), (sub, "../src/a.py"), (link, os.path.join(link, "sub", "CLAUDE.md")),
                              (real, os.path.join(link, "docs", ".claude", "x")), (sub, "../../outside/AGENTS.md")):
                self.assertIsNone(role_guard.control_plane_path([path], {"cwd": cwd}), (cwd, path))

    def test_windows_prefix_is_removed_only_from_prefixed_drive_and_unc_paths(self):
        plain = role_guard.plain_windows_path
        bs = "\\"
        self.assertEqual(plain(bs * 2 + "?" + bs + "D:" + bs + "p" + bs + "AGENTS.md"), "D:" + bs + "p" + bs + "AGENTS.md")
        self.assertEqual(plain(bs * 2 + "." + bs + "d:/p/x"), "d:" + bs + "p" + bs + "x")
        self.assertEqual(plain(bs * 2 + "?" + bs + "unc" + bs + "srv" + bs + "share" + bs + "x"), bs * 2 + "srv" + bs + "share" + bs + "x")
        for other in ("D:" + bs + "p", "D:/p", "/var/p", "rel/p", "", bs * 2 + "srv" + bs + "share" + bs + "x",
                      bs * 2 + "?" + bs + "Volume{1234}" + bs + "x", bs * 2 + "?" + bs, bs * 2 + "?" + bs + "1:" + bs + "x"):
            self.assertEqual(plain(other), other, other)

    @unittest.skipUnless(os.name == "nt", "extended-length and device paths exist on Windows only")
    def test_windows_prefixed_drive_spellings_are_judged(self):
        proj = os.path.join(self._tmp.name, "proj")
        os.makedirs(os.path.join(proj, "sub"))
        os.makedirs(os.path.join(proj, "src"))
        subprocess.run(["git", "-C", proj, "init", "-q"], check=True)
        with open(os.path.join(proj, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("x")
        ext, dev = "\\\\?\\" + proj, "\\\\.\\" + proj  # \\?\D:\... and \\.\D:\...
        low = proj[0].lower() + proj[1:]
        self.make_worker()
        denied = (  # (cwd, path): an existing file, and files that do not exist yet under the control plane
            (proj, ext + "\\AGENTS.md"), (proj, ext + "\\.claude\\newfile"), (proj, ext + "\\.agents\\skills\\x\\SKILL.md"),
            (proj, ext + "\\sub\\..\\CLAUDE.md"), (proj, ext + "/.codex/x"),
            (proj, dev + "\\AGENTS.md"), (proj, dev + "\\.claude\\newfile"), (proj, low + "\\.claude\\newfile"),
            (proj, proj.replace("\\", "/") + "/.claude/newfile"), (ext, "AGENTS.md"), (ext, ".claude/newfile"),
            (dev, "AGENTS.md"), (os.path.join(proj, "sub"), ext + "\\.CLAUDE\\newfile"))
        for cwd, path in denied:
            r = self.tool("Edit", {"file_path": path}, cwd=cwd)
            self.assertEqual(r.returncode, 2, (cwd, path))
            self.assertIn("control plane", r.stderr)
            self.assert_codex_denied(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)),
                                     "control plane")
        for cwd, path in ((proj, ext + "\\src\\a.py"), (proj, ext + "\\sub\\AGENTS.md"), (proj, dev + "\\docs\\.claude\\x"),
                          (ext, "src/a.py"), (ext, "sub/CLAUDE.md"), (proj, "\\\\?\\Z:\\nothing\\.claude\\x")):  # not the control plane
            self.assertEqual(self.tool("Edit", {"file_path": path}, cwd=cwd).returncode, 0, (cwd, path))
            self.assert_allowed(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)))

    def test_stream_suffixes_are_cut_without_touching_the_drive_colon(self):
        cut = role_guard.without_streams
        self.assertEqual(cut("D:\\p\\AGENTS.md:review"), "D:\\p\\AGENTS.md")
        self.assertEqual(cut("D:\\p\\AGENTS.md::$DATA"), "D:\\p\\AGENTS.md")
        self.assertEqual(cut("D:\\p\\AGENTS.md:s:$DATA"), "D:\\p\\AGENTS.md")
        self.assertEqual(cut("D:\\p\\.claude::$INDEX_ALLOCATION\\x:s"), "D:\\p\\.claude\\x")
        self.assertEqual(cut("\\\\srv\\share\\repo\\CLAUDE.md::$DATA"), "\\\\srv\\share\\repo\\CLAUDE.md")
        self.assertEqual(cut("\\\\?\\D:\\p\\a:s"), "\\\\?\\D:\\p\\a")
        for other in ("D:\\p\\AGENTS.md", "D:", "D:\\", "D:rel\\x", "rel\\x", "\\\\srv\\share", ""):
            self.assertEqual(cut(other), other, other)

    @unittest.skipUnless(os.name == "nt", "NTFS streams exist on Windows only")
    def test_windows_stream_spellings_of_the_control_plane_are_judged(self):
        proj = os.path.join(self._tmp.name, "proj")
        os.makedirs(os.path.join(proj, "src"))
        os.makedirs(os.path.join(proj, ".claude"))
        subprocess.run(["git", "-C", proj, "init", "-q"], check=True)
        with open(os.path.join(proj, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("x")  # exists; CLAUDE.md and .codex do not
        self.make_worker()
        denied = ((proj, "AGENTS.md:review"), (proj, "AGENTS.md::$DATA"), (proj, "CLAUDE.md::$DATA"),
                  (proj, "CLAUDE.md:s:$DATA"), (proj, ".claude/newfile::$DATA"), (proj, ".claude::$INDEX_ALLOCATION/newfile"),
                  (proj, ".codex:s/x"), (proj, os.path.join(proj, "AGENTS.md") + "::$DATA"),
                  (proj, "\\\\?\\" + os.path.join(proj, "agents.md") + "::$DATA"), (proj, "\\\\?\\" + proj + "\\.agents\\x:s"),
                  (os.path.join(proj, "src"), "../AGENTS.md:s"))
        for cwd, path in denied:
            r = self.tool("Edit", {"file_path": path}, cwd=cwd)
            self.assertEqual(r.returncode, 2, (cwd, path))
            self.assertIn("control plane", r.stderr)
            self.assert_codex_denied(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)),
                                     "control plane")
        for cwd, path in ((proj, "src/a.py:s"), (proj, "src/a.py::$DATA"), (proj, "src/AGENTS.md::$DATA"), (proj, "docs/.claude::$DATA"),
                          (proj, os.path.join(proj, "src", "a.py")), (proj, ".claudeignore:s")):  # not the control plane
            self.assertEqual(self.tool("Edit", {"file_path": path}, cwd=cwd).returncode, 0, (cwd, path))
            self.assert_allowed(self.codex("apply_patch", cwd, agent_id="a1", tool_input=self.patch("*** Update File: " + path)))

    @unittest.skipUnless(os.name == "nt", "UNC and extended-length paths exist on Windows only")
    def test_unc_extended_paths_are_judged_against_a_plain_unc_project(self):
        # no real share: the git top is faked as an ordinary UNC path and realpath is the identity (a real one
        # would go to the network)
        top, bs = "\\\\server\\share\\repo", "\\"
        ext = bs * 2 + "?" + bs + "UNC" + bs + "server" + bs + "share" + bs + "repo"
        patch = lambda path: self.patch("*** Update File: " + path)  # noqa: E731
        with mock.patch.object(role_guard.state, "_git", return_value=top), \
                mock.patch.object(role_guard.os.path, "realpath", side_effect=os.path.normpath):
            denied = (ext + "\\AGENTS.md", ext + "\\.claude\\newfile", ext + "\\sub\\..\\.codex\\x", top + "\\CLAUDE.md")
            allowed = (ext + "\\src\\a.py", ext + "\\sub\\AGENTS.md", ext.replace("repo", "other") + "\\AGENTS.md",
                       ext.replace("share", "other") + "\\AGENTS.md")
            for cwd in (top, top + "\\sub", ext):
                for path in denied + allowed:
                    want = path in denied
                    claude = {"cwd": cwd, "agent_id": "a", "tool_name": "Edit", "tool_input": {"file_path": path}}
                    codex = {"cwd": cwd, "agent_id": "a", "tool_name": "apply_patch", "tool_input": patch(path)}
                    for payload, host in ((claude, "claude"), (codex, "codex")):
                        try:
                            role_guard.on_pre_tool(payload, host)
                            got = False
                        except role_guard.Denied:
                            got = True
                        self.assertEqual(got, want, (cwd, path, host))

    def test_a_path_the_system_cannot_resolve_is_still_judged_plainly(self):
        # realpath failing must not turn a deny into an allow: the plain comparison stays
        proj = os.path.join(self._tmp.name, "proj")
        for error in (OSError("boom"), ValueError("embedded null byte")):
            with mock.patch.object(role_guard.os.path, "realpath", side_effect=error):
                self.assertEqual(role_guard.spellings(os.path.join(proj, "a")), {os.path.normpath(os.path.join(proj, "a"))})
                self.assertEqual(role_guard.control_plane_path([".claude/x"], {"cwd": proj}), ".claude/x")
                self.assertIsNone(role_guard.control_plane_path(["src/a.py"], {"cwd": proj}))

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
                                 env=self.WORKER))  # an edit call without a Path has no path to judge

    # ---- edit tools (write_file, edit_file): M7 U1 --------------------------------------

    def edit(self, key, path, cid="c1", env=None, **changes):
        """A write_file / edit_file PreToolUse payload (fixture) with this Path, through the real hook process."""
        p = self.payload(key, cid, **changes)
        p["toolCall"]["args"]["Path"] = path
        return self.run_guard("PreToolUse", p, host="agy", env=env)

    def edit_roles(self):
        """Three ways to be a worker: (name, cid, env, label of the deny tail). Records are made where needed."""
        self.agy("pre_invocation", cid="mk", lastUserInput="HARNESS ROLE: DELEGATE\nx")  # marker on the first call
        self.agy("pre_invocation_subagent", cid="es")  # empty first input (an agy subagent)
        return (("marker", "mk", None), ("launcher variable", "lv", self.WORKER), ("empty input", "es", None))

    def test_worker_edit_tools_on_the_control_plane_are_denied(self):
        for name, cid, env in self.edit_roles():
            for key, path in (("pre_tool_write_file", self.work + "/.claude/x"), ("pre_tool_edit_file", self.work + "/AGENTS.md"),
                              ("pre_tool_edit_file", self.work + "/.agents/hooks.json"), ("pre_tool_write_file", self.work + "/CLAUDE.md"),
                              ("pre_tool_write_file", self.work + "/.codex/hooks.json")):
                reason = self.assert_deny(self.edit(key, path, cid=cid, env=env), "workers may not edit the control plane (%s)" % path)
                self.assertEqual("no user input" in reason, name == "empty input", (name, path))
            # the decision follows Path only: the other arguments of the measured calls change nothing
            args = {"Content": "x", "Overwrite": True, "Append": True, "ArtifactMetadata": {"a": 1}}
            p = self.payload("pre_tool_write_file", cid)
            p["toolCall"]["args"] = dict(args, Path=self.work + "/.claude/y")
            self.assert_deny(self.run_guard("PreToolUse", p, host="agy", env=env), "control plane")

    def test_worker_edit_path_spellings_on_the_control_plane(self):
        paths = [".claude/x", "./AGENTS.md", "src/../CLAUDE.md", self.work + "/.Claude/x", self.work + "/./.claude/../.claude/x",
                 self.work + "/src/../.CODEX/x"]  # relative paths are taken against cwd
        if os.name == "nt":  # a backslash is a separator only here
            paths += [self.work + "\\.agents\\x", self.work.replace("/", "\\") + "\\\\.CODEX/x"]
        for path in paths:
            self.assert_deny(self.edit("pre_tool_edit_file", path, env=self.WORKER), "control plane")

    def test_worker_other_edits_ask(self):
        for path in (self.work + "/src/a.py", "src/a.py", self.work + "/docs/AGENTS.md", self.work + "/.claudeignore",
                     self.work + "/claude/x", self.work + "/CLAUDE.md.bak", "../outside/AGENTS.md",
                     os.path.join(self._tmp.name, "elsewhere", ".claude", "x")):  # a folder outside this project
            for key in ("pre_tool_write_file", "pre_tool_edit_file"):
                self.assert_ask(self.edit(key, path, env=self.WORKER))

    def test_orchestrator_edits_ask_even_on_the_control_plane(self):
        self.agy("pre_invocation", cid="orch")
        for key in ("pre_tool_write_file", "pre_tool_edit_file"):
            for path in (self.work + "/.claude/x", self.work + "/AGENTS.md", self.work + "/src/a.py"):
                self.assert_ask(self.edit(key, path, cid="orch"))
        self.assertEqual(self.record("orch")["role"], "orchestrator")

    def test_unresolved_role_edits_of_the_control_plane_are_denied(self):
        os.makedirs(os.path.join(self.state, "sessions"))
        for cid, body in (("bad", "{not json"), ("lost", json.dumps({"role": "unresolved", "host": "agy", "ts": 1}))):
            with open(os.path.join(self.state, "sessions", cid + ".json"), "w", encoding="utf-8") as f:
                f.write(body)
            reason = self.assert_deny(self.edit("pre_tool_write_file", self.work + "/.codex/x", cid=cid), "control plane")
            self.assertIn("role unresolved", reason, cid)
            self.assert_ask(self.edit("pre_tool_write_file", self.work + "/src/a.py", cid=cid))
        self.assert_deny(self.edit("pre_tool_edit_file", self.work + "/AGENTS.md", cid="nobody", conversationId=""), "role unresolved")

    @unittest.skipUnless(os.name == "nt", "extended-length and device paths exist on Windows only")
    def test_worker_edit_with_a_windows_prefixed_path_is_denied(self):
        base = self.work.replace("/", "\\")
        for prefix in ("\\\\?\\", "\\\\.\\"):
            for rest in ("\\AGENTS.md", "\\.claude\\newfile", "\\.agents\\hooks.json", "\\.codex\\x"):
                for key in ("pre_tool_write_file", "pre_tool_edit_file"):
                    self.assert_deny(self.edit(key, prefix + base + rest, env=self.WORKER), "control plane")
            self.assert_ask(self.edit("pre_tool_write_file", prefix + base + "\\src\\a.py", env=self.WORKER))
        for rest in ("\\AGENTS.md:review", "\\AGENTS.md::$DATA", "\\CLAUDE.md::$DATA", "\\.claude::$INDEX_ALLOCATION\\newfile",
                     "\\.agents\\x:s", "\\.codex\\x::$DATA"):  # NTFS streams, files that do not exist yet
            for key in ("pre_tool_write_file", "pre_tool_edit_file"):
                self.assert_deny(self.edit(key, base + rest, env=self.WORKER), "control plane")
                self.assert_deny(self.edit(key, "\\\\?\\" + base + rest, env=self.WORKER), "control plane")
        for rest in ("\\src\\a.py:s", "\\src\\a.py::$DATA", "\\docs\\AGENTS.md::$DATA"):
            self.assert_ask(self.edit("pre_tool_write_file", base + rest, env=self.WORKER))
        self.agy("pre_invocation", cid="orch")
        self.assert_ask(self.edit("pre_tool_write_file", "\\\\?\\" + base + "\\AGENTS.md", cid="orch"))
        self.assert_ask(self.edit("pre_tool_write_file", base + "\\AGENTS.md::$DATA", cid="orch"))

    def test_edit_tools_are_not_edits_on_the_other_hosts(self):
        for tool in ("write_file", "edit_file"):
            for host in ("claude", "codex"):
                payload = {"session_id": "g", "agent_id": "a1", "cwd": self.work, "tool_name": tool,
                           "tool_input": {"Path": self.work + "/.claude/x", "file_path": ""}}
                r = self.run_guard("PreToolUse", payload, host=host)
                self.assertEqual((r.returncode, r.stdout), (0, ""), (tool, host))
        # the Claude and Codex tool names keep counting on every host, agy included (they did before)
        self.assertEqual(role_guard.edit_paths("write_file", {"Path": "a"}, "agy"), ["a"])
        self.assertEqual(role_guard.edit_paths("write_file", {"Path": "a"}, "claude"), [])
        self.assertEqual(role_guard.edit_paths("write_file", {"Path": "a"}), [])
        self.assertEqual(role_guard.edit_paths("Edit", {"file_path": "b"}, "agy"), ["b"])
        self.assertEqual(role_guard.edit_paths("Edit", {"file_path": "b"}, "codex"), ["b"])
        self.assertEqual(role_guard.edit_paths("Edit", {"file_path": "b"}), ["b"])

    def test_edit_calls_without_a_usable_path_are_not_judged(self):
        for path in (7, None, "", ["AGENTS.md"], {"a": 1}, True):
            for key in ("pre_tool_write_file", "pre_tool_edit_file"):
                p = self.payload(key)
                p["toolCall"]["args"]["Path"] = path
                self.assert_ask(self.run_guard("PreToolUse", p, host="agy", env=self.WORKER))
        p = self.payload("pre_tool_write_file")
        del p["toolCall"]["args"]["Path"]
        self.assert_ask(self.run_guard("PreToolUse", p, host="agy", env=self.WORKER))

    def test_edit_decision_is_never_allow_and_errors_ask(self):
        self.agy("pre_invocation", cid="w2", lastUserInput="HARNESS ROLE: DELEGATE\nx")
        raw = json.dumps(dict(self.payload("pre_tool_write_file", "w2"), toolCall={
            "name": "write_file", "id": "x", "args": {"Path": self.work + "/.claude/x", "Content": "x"}}))
        code, out, err = self.in_process("PreToolUse", raw)
        self.assertEqual((code, json.loads(out)["decision"]), (0, "deny"))
        # a guard bug while judging the path gives ask (M5 decision 1), never allow
        with mock.patch.object(role_guard, "control_plane_path", side_effect=RuntimeError("boom")):
            code, out, err = self.in_process("PreToolUse", raw)
        self.assertEqual((code, json.loads(out)), (0, {"decision": "ask"}))
        self.assertIn("RuntimeError: boom", err)
        # a payload the hook cannot read
        self.assert_ask(self.run_guard("PreToolUse", host="agy", raw="not json"))

    def test_edit_in_a_worktree_judges_both_trees(self):
        main, wt = os.path.join(self._tmp.name, "main"), os.path.join(self._tmp.name, "wt")
        os.makedirs(main)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", main]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "i"], check=True)
        subprocess.run(git + ["worktree", "add", "-q", "-b", "wt", wt], check=True)
        os.makedirs(os.path.join(wt, "sub"))
        # the guard of a worktree worker is the main tree's (an absolute path): INSTALL_ROOT is the main tree
        def judge(path):
            p = role_guard.agy_internal(self.payload("pre_tool_edit_file", cwd=os.path.join(wt, "sub")))
            p["tool_input"]["Path"] = path
            try:
                role_guard.on_pre_tool(p, "agy", worker=True)
            except role_guard.Denied as d:
                return str(d)
            return None
        with mock.patch.object(role_guard, "INSTALL_ROOT", main):
            for path in (os.path.join(wt, ".agents", "hooks.json"), os.path.join(main, ".claude", "x"),
                         os.path.join(wt, "AGENTS.md"), "../.claude/x"):  # the last one: from wt/sub up to wt
                self.assertIn("control plane", judge(path) or "", path)
            for path in (os.path.join(wt, "src", "a.py"), os.path.join(wt, "sub", "AGENTS.md"), "AGENTS.md",
                         os.path.join(main, "src", "a.py")):  # sub/AGENTS.md is not the root file
                self.assertIsNone(judge(path), path)

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


class UpdateNoticeTest(unittest.TestCase):
    """update_notice: a project folder with the installer's two records and a real git checkout to compare with."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = os.path.join(self._tmp.name, "proj")
        self.folder = os.path.join(self.project, ".claude", "tagteam")
        os.makedirs(self.folder)
        self.checkout = os.path.join(self.project, "tpl")  # a checkout inside the project, like a submodule
        os.makedirs(self.checkout)
        self.git("init", "-q")
        self.commits = [self.commit(n) for n in range(4)]

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.checkout, "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                            "commit.gpgsign=false"] + list(args), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def commit(self, n):
        with open(os.path.join(self.checkout, "f%d" % n), "w") as f:
            f.write(str(n))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c%d" % n)
        return self.git("rev-parse", "HEAD")

    def records(self, commit=None, checkout="tpl", describe="abc1234"):
        self.write("install.json", json.dumps({"template": {"commit": commit or self.commits[0], "tag": None,
                                                            "describe": describe, "dirty": False}}))
        self.write("install.local.json", json.dumps({"checkout": checkout}))

    def write(self, name, text):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def notice(self):
        return role_guard.update_notice(self.folder)

    def test_a_checkout_ahead_of_the_installed_commit_gets_one_line(self):
        self.records(self.commits[1])
        self.assertEqual(self.notice(), "TAGTEAM: tagteam update available (checkout is 2 commits ahead of installed "
                                        "abc1234); orchestrator: load skill tagteam-update.")
        self.records(self.commits[2])
        self.assertIn("is 1 commit ahead of installed", self.notice())

    def test_it_is_silent_when_the_checkout_is_not_ahead(self):
        self.records(self.commits[3])  # the same commit
        self.assertIsNone(self.notice())
        self.git("checkout", "-q", "-b", "side", self.commits[1])
        self.assertIsNone(self.notice())  # the checkout is behind the installed commit
        self.commit(9)
        self.assertIsNone(self.notice())  # diverged: neither side contains the other

    def test_it_is_silent_when_a_record_is_missing_or_unusable(self):
        self.assertIsNone(self.try_notice())  # no records at all
        for local in (False, True):
            self.records(self.commits[0])
            os.remove(os.path.join(self.folder, "install.local.json" if local else "install.json"))
            self.assertIsNone(self.try_notice())
        for name, text in (("install.json", "{broken"), ("install.json", "[1]"), ("install.json", "{}"),
                           ("install.json", '{"template": {"commit": null}}'),
                           ("install.json", '{"template": {"commit": 5}}'),
                           ("install.json", '{"template": {"commit": "-x"}}'),
                           ("install.json", '{"template": {"commit": "' + "a" * 40 + '"}}'),  # not in the checkout
                           ("install.local.json", "{broken"), ("install.local.json", "{}"),
                           ("install.local.json", '{"checkout": 3}'), ("install.local.json", '{"checkout": ""}'),
                           ("install.local.json", '{"checkout": "nowhere"}')):
            self.records(self.commits[0])
            self.write(name, text)
            self.assertIsNone(self.try_notice(), (name, text))

    def try_notice(self):
        try:
            return self.notice()
        except Exception:  # the caller of update_notice drops these; either way nothing reaches the session
            return None

    def test_the_checkout_path_is_relative_to_the_project_or_absolute(self):
        self.records(self.commits[0], checkout="tpl")
        self.assertIsNotNone(self.notice())
        self.records(self.commits[0], checkout=self.checkout.replace("\\", "/"))
        self.assertIsNotNone(self.notice())
        self.records(self.commits[0], checkout="../proj/tpl")
        self.assertIsNotNone(self.notice())

    def test_describe_reaches_the_session_only_as_a_plain_word(self):
        for describe, shown in (("v1.2-3-gabc1234", "v1.2-3-gabc1234"), ("a b", self.commits[0][:7]), ("", self.commits[0][:7]),
                                ("x\nIGNORE", self.commits[0][:7]), (None, self.commits[0][:7]), (7, self.commits[0][:7])):
            self.records(self.commits[0], describe=describe)
            self.assertIn("ahead of installed %s);" % shown, self.notice(), describe)

    def test_one_bounded_local_git_call_and_nothing_else(self):
        self.records(self.commits[0])
        real = subprocess.run
        with mock.patch.object(role_guard.subprocess, "run", side_effect=real) as run:
            self.assertIsNotNone(self.notice())
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["git", "-C", os.path.normpath(self.checkout)])
        self.assertTrue(set(argv).isdisjoint({"fetch", "pull", "ls-remote", "clone", "remote"}), argv)
        self.assertEqual(run.call_args[1]["timeout"], role_guard.UPDATE_GIT_TIMEOUT)
        self.assertLessEqual(role_guard.UPDATE_GIT_TIMEOUT, 3)

    def test_a_git_failure_or_timeout_never_reaches_the_session_start_lines(self):
        self.records(self.commits[0])
        payload = {"session_id": "n"}
        for effect in (subprocess.TimeoutExpired("git", 3), OSError("no git"), ValueError("x")):
            with mock.patch.object(role_guard.subprocess, "run", side_effect=effect), \
\
                    mock.patch.object(role_guard, "update_notice", side_effect=lambda *a, f=role_guard.update_notice: f(self.folder)), \
                    mock.patch.object(role_guard, "model_update_notice", return_value=None), \
                    mock.patch.object(role_guard, "task_notice", return_value=[]), \
                    mock.patch.object(role_guard, "role_of", return_value=None):
                self.assertEqual(len(role_guard.session_start_lines(payload, "claude")), 1, effect)
        out = subprocess.CompletedProcess([], 128, "", "fatal")
        with mock.patch.object(role_guard.subprocess, "run", return_value=out):
            self.assertIsNone(self.notice())

    def project_hook(self, env=None):
        """SessionStart run by a copy of the hook inside the project, as installed."""
        for name in ("role_guard.py", "state.py"):
            shutil.copy(os.path.join(os.path.dirname(GUARD), name), self.folder)
        e = dict(os.environ, TAGTEAM_STATE_DIR=os.path.join(self._tmp.name, "state"),
                 TAGTEAM_CONFIG_DIR=os.path.join(self._tmp.name, "cfg"))
        e.pop("HARNESS_DELEGATE_RUN", None)
        e.update(env or {})
        r = subprocess.run([sys.executable, os.path.join(self.folder, "role_guard.py"), "SessionStart"],
                           input=json.dumps({"session_id": "n"}), capture_output=True, text=True, encoding="utf-8",
                           env=e, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout.strip().splitlines()

    def test_the_installed_hook_prints_it_for_the_orchestrator_and_not_for_a_worker(self):
        self.records(self.commits[1])
        lines = self.project_hook()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("role=pending", lines[0])
        self.assertTrue(lines[1].startswith("TAGTEAM: tagteam update available (checkout is 2 commits ahead"), lines[1])
        self.assertEqual(self.project_hook({"HARNESS_DELEGATE_RUN": "1"}), ["TAGTEAM: role=worker host=claude"])
        self.records(self.commits[3])
        self.assertEqual(len(self.project_hook()), 1)  # up to date: silent


if __name__ == "__main__":
    unittest.main()
