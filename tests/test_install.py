import contextlib
import copy
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

INSTALL = Path(__file__).resolve().parent.parent / "install.py"
REAL_STATE = INSTALL.parent / "harness/tagteam/state.py"
REAL_GUARD = INSTALL.parent / "harness/tagteam/role_guard.py"
REAL_PAYLOADS = INSTALL.parent / "harness/tagteam/tests/fixtures/codex_payloads.json"
REAL_SETTINGS = INSTALL.parent / "harness/claude/settings.json"
sys.path.insert(0, str(INSTALL.parent))
import install  # noqa: E402

REAL_PROBE = install.probe_cli  # the tests replace install.probe_cli with a fake; one test needs the real one

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
FAKE_COLLECT = '''import json, os, sys
cfg = os.path.dirname(os.path.abspath(__file__))
if "--write-available" in sys.argv:
    with open(os.path.join(cfg, "avail.log"), "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
    path =os.path.join(cfg, "models.local.json")
    data = json.load(open(path)) if os.path.exists(path) else {}
    data["available"] = {"openai": {"ids": ["m1"]}}
    json.dump(data, open(path, "w"))
    print("wrote available for openai to models.local.json")
    sys.exit(0)
with open(os.path.join(cfg, "calls.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "|" + os.environ.get("TAGTEAM_AA_API_KEY", "") + "\\n")
if os.environ.get("FAKE_COLLECT_FAIL"):
    sys.stderr.write("AA error: HTTP 500\\n")
    sys.exit(1)
open(os.path.join(cfg, "bench.local.json"), "w").write("{}")
'''
ALLOW_ALL = "import sys\nsys.exit(0)\n"

TEMPLATE_AGENTS = "# Agents\n\nintro v2\n\n## Project policy\n\ntemplate placeholder\n"


def hook(event, **extra):
    h = {"type": "command", "command": f"python3 .claude/tagteam/role_guard.py {event} --host codex", "timeout": 10,
         "statusMessage": "tagteam role guard", "commandWindows": f"python .claude/tagteam/role_guard.py {event} --host codex"}
    h.update(extra)
    return h


HOOKS_TEMPLATE = {"hooks": {
    "SessionStart": [{"hooks": [hook("SessionStart")]}],
    "PreToolUse": [{"matcher": ".*", "hooks": [hook("PreToolUse")]}],
}}


def build_source(root, guard=STUB):
    h = root / "harness"
    files = {
        "claude/settings.json": json.dumps({"hooks": {"PreToolUse": ["new"]}, "subagentPromptCacheTtl": "1h"}),
        "claude/agents/a.md": "agent",
        "skills/s1/SKILL.md": "skill",
        "tagteam/role_guard.py": guard,
        "tagteam/state.py": REAL_STATE.read_text(encoding="utf-8"),
        "tagteam/models.json": "{}",
        "tagteam/models_collect.py": FAKE_COLLECT,
        "tagteam/models.local.json.example": '{"plans": {}}',
        "codex/hooks.json": json.dumps(HOOKS_TEMPLATE),
        "codex/agents/b.toml": "x = 1",
        "tagteam/tests/fixtures/codex_payloads.json": REAL_PAYLOADS.read_text(encoding="utf-8"),  # read from the source by --check
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
        for k in ("TAGTEAM_AA_API_KEY", "TAGTEAM_CONFIG_DIR", "FAKE_COLLECT_FAIL", "TAGTEAM_STATE_DIR"):
            os.environ.pop(k, None)
        probe = mock.patch.object(install, "probe_cli", lambda argv, path: (0, ""))  # --schedule must not start a real CLI
        probe.start()
        self.addCleanup(probe.stop)
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

    def test_skills_installed_to_both_locations_with_the_same_content(self):
        (self.src / "harness/skills/s1/extra.md").write_text("helper", encoding="utf-8")
        self.run_install()
        for root in (".claude/skills", ".agents/skills"):
            self.assertEqual((self.dst / root / "s1/SKILL.md").read_text(encoding="utf-8"), "skill", root)
            self.assertEqual((self.dst / root / "s1/extra.md").read_text(encoding="utf-8"), "helper", root)
        self.assertEqual(install.skill_copy_errors(self.dst), [])
        (self.src / "harness/skills/s1/SKILL.md").write_text("skill v2", encoding="utf-8")
        self.run_install("--update")
        for root in (".claude/skills", ".agents/skills"):
            self.assertEqual((self.dst / root / "s1/SKILL.md").read_text(encoding="utf-8"), "skill v2", root)

    def git_init(self, path):
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)

    def write_old(self, rel, text):
        p = self.dst / ".git/tagteam" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_install_and_update_move_the_old_state_folder(self):
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        self.write_old("sessions/s1.json", "s1")
        code, out = self.run_install("--update")
        self.assertEqual(code, 0)
        self.assertEqual((self.dst / ".tagteam/exhausted.json").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.dst / ".tagteam/sessions/s1.json").read_text(encoding="utf-8"), "s1")
        self.assertFalse((self.dst / ".git/tagteam").exists())
        self.assertTrue((self.dst / ".git/tagteam.moved/exhausted.json").is_file())
        self.assertIn("state", out)
        self.assertIn("tagteam.moved", out)
        code, out = self.run_install("--update")  # once: nothing more to move
        self.assertNotIn("tagteam.moved", out)

    def test_conflicting_old_file_is_kept_aside_and_reported(self):
        self.git_init(self.dst)
        (self.dst / ".tagteam").mkdir()
        (self.dst / ".tagteam/exhausted.json").write_text("new", encoding="utf-8")
        self.write_old("exhausted.json", "old")
        code, out = self.run_install()
        self.assertEqual(code, 0)
        self.assertEqual((self.dst / ".tagteam/exhausted.json").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.dst / ".git/tagteam.moved/exhausted.json").read_text(encoding="utf-8"), "old")
        self.assertIn("conflict", out)

    def test_unwritable_new_state_folder_only_warns(self):
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        (self.dst / ".tagteam").write_text("a file in the way", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_install()
        self.assertEqual(code, 0)
        self.assertIn("could not move the old state folder", err.getvalue())
        self.assertEqual((self.dst / ".git/tagteam/exhausted.json").read_text(encoding="utf-8"), "old")

    def test_nothing_is_moved_outside_git_or_with_the_override(self):
        code, out = self.run_install()
        self.assertEqual(code, 0)
        self.assertNotIn("tagteam.moved", out)
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        os.environ["TAGTEAM_STATE_DIR"] = str(self.dst.parent / "elsewhere")
        self.run_install("--update")
        self.assertTrue((self.dst / ".git/tagteam/exhausted.json").is_file())

    def test_old_source_without_state_py_installs_without_migrating(self):
        (self.src / "harness/tagteam/state.py").unlink()
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        code, _ = self.run_install()
        self.assertEqual(code, 0)
        self.assertTrue((self.dst / ".git/tagteam/exhausted.json").is_file())

    # ---- U7: Codex hook drift, --repair-hooks, --check split -----------------------------------

    def hooks_path(self):
        return self.dst / ".codex/hooks.json"

    def hooks(self):
        return json.loads(self.hooks_path().read_text(encoding="utf-8"))

    def write_hooks(self, data):
        self.hooks_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def issues(self):
        expected = install.expected_codex_hooks(self.src, self.dst)
        current, err = install.load_codex_hooks(self.dst)
        self.assertIsNone(err)
        return install.diagnose_hooks(self.dst, expected, current)

    def kinds(self):
        return sorted((e, k) for e, k, _ in self.issues())

    def use_real_guard(self):
        self.src = build_source(self.src.parent / "src-real", guard=REAL_GUARD.read_text(encoding="utf-8"))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.main([str(self.dst), "--source", str(self.src), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_fresh_install_has_no_hook_drift(self):
        self.run_install()
        self.assertEqual(self.issues(), [])

    def test_drift_is_found_per_event_and_field(self):
        self.run_install()
        data = self.hooks()
        del data["hooks"]["SessionStart"]
        data["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
        data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 99
        self.write_hooks(data)
        found = self.issues()
        self.assertEqual(sorted((e, k) for e, k, _ in found), [("PreToolUse", "different"), ("PreToolUse", "different"),
                                                                ("SessionStart", "missing")])
        text = "\n".join(t for _, _, t in found)
        self.assertIn('matcher: have "Bash", want ".*"', text)
        self.assertIn("timeout: have 99, want 10", text)

    def test_wrong_guard_path_is_reported(self):
        self.run_install()
        data = self.hooks()
        h = data["hooks"]["SessionStart"][0]["hooks"][0]
        h["command"] = h["command"].replace(str(self.dst.resolve()).replace("\\", "/"), "C:/gone/elsewhere")
        self.write_hooks(data)
        kinds = self.kinds()
        self.assertIn(("SessionStart", "different"), kinds)
        self.assertIn(("SessionStart", "path"), kinds)
        # the file is simply not installed: same definition, missing script
        self.run_main("--repair-hooks", "--yes")  # --update keeps an existing hooks.json
        self.assertEqual(self.issues(), [])
        (self.dst / ".claude/tagteam/role_guard.py").unlink()
        self.assertEqual({k for _, k in self.kinds()}, {"path"})

    def test_other_entries_are_ignored_and_extra_tagteam_entries_only_noted(self):
        self.run_install()
        data = self.hooks()
        data["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": "python3 mine.py"})
        data["hooks"]["PreToolUse"].insert(0, {"hooks": [{"type": "command", "command": "echo mine", "timeout": 1}]})
        data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo stop"}]}]
        self.write_hooks(data)
        self.assertEqual(self.issues(), [])
        data["hooks"]["PreToolUse"].append(copy.deepcopy(data["hooks"]["PreToolUse"][1]))  # an exact second copy
        self.write_hooks(data)
        self.assertEqual(self.kinds(), [("PreToolUse", "extra")])
        data["hooks"]["PreToolUse"][-1]["matcher"] = "Bash"  # a second, different tagteam hook
        self.write_hooks(data)
        self.assertEqual(self.kinds(), [("PreToolUse", "stale")])

    def test_repair_previews_by_default_and_changes_nothing(self):
        self.run_install()
        data = self.hooks()
        del data["hooks"]["SessionStart"]
        self.write_hooks(data)
        before = self.hooks_path().read_bytes()
        code, out, _ = self.run_main("--repair-hooks")
        self.assertEqual(code, 0, out)
        self.assertIn("preview only", out)
        self.assertIn("SessionStart: appended", out)
        self.assertIn("+", out)
        self.assertEqual(self.hooks_path().read_bytes(), before)
        self.assertFalse((self.dst / ".codex/hooks.json.bak").exists())

    def test_repair_yes_keeps_other_entries_in_place_and_leaves_a_backup(self):
        self.run_install()
        data = self.hooks()
        foreign_group = {"hooks": [{"type": "command", "command": "echo mine", "timeout": 1}]}
        data["hooks"]["PreToolUse"].insert(0, foreign_group)  # tagteam's hook is now group 1
        data["hooks"]["PreToolUse"][1]["hooks"][0]["timeout"] = 99
        data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo stop"}]}]
        del data["hooks"]["SessionStart"]
        data["hooks"] = {"Stop": data["hooks"]["Stop"], "PreToolUse": data["hooks"]["PreToolUse"]}  # order matters to Codex
        self.write_hooks(data)
        original = self.hooks_path().read_bytes()
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("trust", out)
        self.assertIn("silently", out)
        self.assertEqual((self.dst / ".codex/hooks.json.bak").read_bytes(), original)
        new = self.hooks()
        self.assertEqual(list(new["hooks"]), ["Stop", "PreToolUse", "SessionStart"])  # the missing event goes last
        self.assertEqual(new["hooks"]["PreToolUse"][0], foreign_group)  # same index, same content
        self.assertEqual(new["hooks"]["Stop"], data["hooks"]["Stop"])
        self.assertEqual(new["hooks"]["PreToolUse"][1]["hooks"][0]["timeout"], 10)  # replaced in place (group 1)
        self.assertEqual(len(new["hooks"]["PreToolUse"]), 2)
        self.assertEqual(self.issues(), [])

    def test_repair_appends_a_missing_hook_at_the_end_of_an_existing_event(self):
        self.run_install()
        data = self.hooks()
        data["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "echo mine"}]}]  # no tagteam hook here
        self.write_hooks(data)
        self.run_main("--repair-hooks", "--yes")
        groups = self.hooks()["hooks"]["SessionStart"]
        self.assertEqual(groups[0]["hooks"][0]["command"], "echo mine")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1], {"hooks": [install.expected_codex_hooks(self.src, self.dst)["hooks"]["SessionStart"][0]["hooks"][0]]})

    def test_repair_is_idempotent_and_numbers_backups(self):
        self.run_install()
        data = self.hooks()
        data["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
        self.write_hooks(data)
        self.run_main("--repair-hooks", "--yes")
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(sorted(p.name for p in (self.dst / ".codex").glob("hooks.json*")), ["hooks.json", "hooks.json.bak"])
        data = self.hooks()
        data["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
        self.write_hooks(data)
        self.run_main("--repair-hooks", "--yes")
        self.assertTrue((self.dst / ".codex/hooks.json.bak-2").is_file())

    def test_repair_creates_a_missing_file_without_a_backup(self):
        self.run_install()
        self.hooks_path().unlink()
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issues(), [])
        self.assertFalse((self.dst / ".codex/hooks.json.bak").exists())

    def test_repair_refuses_a_file_it_cannot_understand(self):
        self.run_install()
        for body in ("{broken", '{"hooks": []}', "[1]"):
            self.hooks_path().write_text(body, encoding="utf-8")
            code, out, err = self.run_main("--repair-hooks", "--yes")
            self.assertEqual(code, 1, body)
            self.assertEqual(self.hooks_path().read_text(encoding="utf-8"), body)
            self.assertFalse((self.dst / ".codex/hooks.json.bak").exists())
        code, out, err = self.run_main("--check", "--host", "codex")
        self.assertEqual(code, 1)

    def test_repair_never_edits_a_group_that_shares_its_matcher_with_other_hooks(self):
        expected = install.expected_codex_hooks(self.src, self.dst)
        shared = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"},
                                               hook("PreToolUse", timeout=99)]}
        new, actions = install.plan_repair({"hooks": {"PreToolUse": [shared]}}, expected)
        self.assertEqual(new["hooks"]["PreToolUse"][0], shared)  # untouched
        self.assertEqual(new["hooks"]["PreToolUse"][1], expected["hooks"]["PreToolUse"][0])
        self.assertTrue(any("shares a matcher" in a for a in actions))

    def test_a_drifted_hook_sharing_a_matcher_is_repaired_once(self):
        self.use_real_guard()
        self.run_install()
        data = self.hooks()
        shared = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}, hook("PreToolUse", timeout=99)]}
        data["hooks"]["PreToolUse"] = [shared]
        self.write_hooks(data)
        self.assertEqual({k for _, k in self.kinds()}, {"different"})
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertEqual(code, 0, out)
        groups = self.hooks()["hooks"]["PreToolUse"]
        self.assertEqual(groups[0], shared)  # the user's group is untouched
        self.assertEqual(len(groups), 2)
        self.assertEqual(self.kinds(), [("PreToolUse", "stale")])  # the old entry is classified apart, not as drift
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertIn("nothing to do", out)
        self.assertEqual(len(self.hooks()["hooks"]["PreToolUse"]), 2)  # a second repair appends nothing
        self.assertEqual(sorted(p.name for p in (self.dst / ".codex").glob("hooks.json*")), ["hooks.json", "hooks.json.bak"])
        code, out, _ = self.codex_check()
        self.assertEqual(code, 0, out)  # a warning, not a failure
        self.assertIn("WARN", out)
        self.assertIn("differs from the template", out)

    def test_an_exact_entry_is_found_wherever_it_stands(self):
        self.run_install()
        data = self.hooks()
        data["hooks"]["PreToolUse"].insert(0, {"matcher": "Bash", "hooks": [hook("PreToolUse", timeout=1)]})  # stale first
        self.write_hooks(data)
        self.assertEqual(self.kinds(), [("PreToolUse", "stale")])  # the correct one (second) is recognized
        new, actions = install.plan_repair(data, install.expected_codex_hooks(self.src, self.dst))
        self.assertEqual(actions, [])
        self.assertEqual(new, data)

    def test_repair_refuses_structures_it_cannot_edit_without_writing(self):
        self.run_install()
        good = self.hooks()
        broken = []
        for mutate in (lambda d: d["hooks"].update(PreToolUse="oops"),
                       lambda d: d["hooks"].update(PreToolUse={"hooks": []}),
                       lambda d: d["hooks"].update(PreToolUse=["x"]),
                       lambda d: d["hooks"].update(PreToolUse=[{"matcher": "Bash"}]),
                       lambda d: d["hooks"].update(PreToolUse=[{"hooks": "x"}]),
                       lambda d: d["hooks"].update(PreToolUse=[{"hooks": ["x"]}]),
                       lambda d: d["hooks"].update(SessionStart=5)):
            data = copy.deepcopy(good)
            mutate(data)
            broken.append(data)
        for data in broken:
            self.write_hooks(data)
            before = self.hooks_path().read_bytes()
            code, out, err = self.run_main("--repair-hooks", "--yes")
            self.assertEqual(code, 1, data)
            self.assertIn("nothing was written", err)
            self.assertEqual(self.hooks_path().read_bytes(), before)
            self.assertFalse((self.dst / ".codex/hooks.json.bak").exists())
            self.assertEqual(self.run_main("--repair-hooks")[0], 1)  # the preview refuses too
        self.use_real_guard()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 1)
        self.assertIn("structure repair does not edit", out)
        # an odd event the template does not use is none of our business
        data = copy.deepcopy(good)
        data["hooks"]["Stop"] = "whatever"
        data["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
        self.write_hooks(data)
        code, out, _ = self.run_main("--repair-hooks", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hooks()["hooks"]["Stop"], "whatever")

    def test_install_gives_trust_guidance_and_warns_about_drift(self):
        code, out = self.run_install()
        self.assertIn("trust", out)
        self.assertNotIn("--repair-hooks", out)
        data = self.hooks()
        data["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
        self.write_hooks(data)
        code, out = self.run_install("--update")
        self.assertIn("kept (machine-local trust)", out)
        self.assertIn("--repair-hooks", out)
        self.run_main("--repair-hooks", "--yes")
        code, out = self.run_install("--update")
        self.assertNotIn("--repair-hooks", out)

    def test_flag_combinations(self):
        for argv in (["--repair-hooks", "--check"], ["--live"], ["--host", "codex"], ["--repair-hooks", "--update"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=argv):
                install.main([str(self.dst), *argv])

    def codex_check(self, *extra, **env):
        with mock.patch.dict(os.environ, env):
            return self.run_main("--check", "--host", "codex", *extra)

    def test_check_codex_reports_static_handler_and_live_parts(self):
        self.use_real_guard()
        self.run_install()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 0, out)
        self.assertIn("check host=codex", out)
        self.assertIn("static: ok", out)
        self.assertIn("handler (codex): ok", out)
        self.assertIn("handler (claude): unverified", out)
        self.assertIn("handler (agy): unverified", out)
        self.assertIn("live (codex): unverified (not requested", out)
        self.assertIn("git writes", out)  # the approval note
        self.assertIn("needs approval" if False else "need approval", out)

    def test_check_codex_fails_on_drift_with_the_fix(self):
        self.use_real_guard()
        self.run_install()
        data = self.hooks()
        del data["hooks"]["SessionStart"]
        self.write_hooks(data)
        code, out, _ = self.codex_check()
        self.assertEqual(code, 1)
        self.assertIn("ERROR    hooks.json missing: SessionStart", out)
        self.assertIn("--repair-hooks", out)
        self.assertIn("static: FAILED", out)
        self.hooks_path().unlink()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 1)
        self.assertIn("hooks.json is missing", out)

    def test_check_codex_needs_the_codex_files(self):
        self.use_real_guard()
        self.run_install()
        for f in (self.dst / ".codex/agents").glob("*.toml"):
            f.unlink()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 1)
        self.assertIn(".codex/agents has no agent definition", out)

    def test_check_codex_handler_fails_when_the_guard_denies_nothing(self):
        self.src = build_source(self.src.parent / "src-allow", guard=ALLOW_ALL)
        self.run_install()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 1)
        self.assertIn("handler (codex): FAILED", out)
        self.assertIn("codex 'subagent git commit'", out)
        self.assertIn("codex 'worker apply_patch on AGENTS.md'", out)

    def test_check_codex_handler_is_unverified_without_the_fixture(self):
        self.use_real_guard()
        self.run_install()
        (self.src / "harness/tagteam/tests/fixtures/codex_payloads.json").unlink()
        code, out, _ = self.codex_check()
        self.assertEqual(code, 0, out)
        self.assertIn("handler (codex): unverified (payload fixture not found", out)

    def test_check_without_host_runs_the_claude_handler_only(self):
        self.run_install()
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("handler (claude): ok", out)
        self.assertIn("handler (codex): unverified (run with --host codex)", out)
        self.assertIn("handler (agy): unverified", out)

    def test_check_agy_is_unverified_everywhere(self):
        self.run_install()
        code, out, _ = self.run_main("--check", "--host", "agy", "--live")
        self.assertEqual(code, 0, out)
        self.assertIn("handler (agy): unverified (adapter not installed)", out)
        self.assertIn("live (agy): unverified (adapter not installed", out)
        self.assertIn("no hook adapter", out)

    def fake_cli(self, name, writes=True, host="codex", role="worker", exit_code=0, ts_offset=0, session_name=None,
                 raw=None, print_id=True, record_host=None):
        """A stand-in CLI: records its argv and (like a trusted hook would) writes a role record."""
        script = self.dst.parent / (name + ".py")
        lines = ["import json, os, sys, time", "state = os.environ['TAGTEAM_STATE_DIR']",
                 "open(os.path.join(state, 'argv.txt'), 'w').write(' '.join(sys.argv[1:]))"]
        if host == "codex" and print_id:  # what codex exec --json prints first
            lines.append("print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-under-test'}))")
        if writes:
            lines += ["name = %r or 'thread-under-test.json'" % session_name,
                      "if %r is None and '--session-id' in sys.argv:" % session_name,
                      "    name = sys.argv[sys.argv.index('--session-id') + 1] + '.json'",
                      "os.makedirs(os.path.join(state, 'sessions'), exist_ok=True)",
                      "rec = %r if %r is not None else json.dumps({'role': %r, 'host': %r, 'ts': int(time.time()) + %d})"
                      % (raw, raw, role, record_host or host, ts_offset),
                      "open(os.path.join(state, 'sessions', name), 'w').write(rec)"]
        lines.append("sys.exit(%d)" % exit_code)
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json.dumps([sys.executable, str(script)])

    def live_env(self):
        state = self.dst.parent / "live-state"
        (state / "sessions").mkdir(parents=True, exist_ok=True)
        for f in (state / "sessions").glob("*"):
            f.unlink()
        return state, {"TAGTEAM_STATE_DIR": str(state)}

    def live_codex(self, **cli):
        state, env = self.live_env()
        return self.codex_check("--live", TAGTEAM_CLI_OPENAI=self.fake_cli("codexcli", **cli), **env) + (state,)

    def test_live_check_accepts_the_record_of_its_own_run(self):
        self.use_real_guard()
        self.run_install()
        code, out, _, state = self.live_codex()
        self.assertEqual(code, 0, out)
        self.assertIn("live (codex): ok", out)
        argv = (state / "argv.txt").read_text(encoding="utf-8")
        self.assertTrue(argv.startswith("exec --json -s read-only"), argv)
        self.assertIn("HARNESS ROLE: DELEGATE", argv)  # the marker that makes the record a worker's
        state, env = self.live_env()
        with mock.patch.dict(os.environ, {"TAGTEAM_CLI_CLAUDE": self.fake_cli("claudecli", host="claude"), **env}):
            code, out, _ = self.run_main("--check", "--live")
        self.assertEqual(code, 0, out)
        self.assertIn("live (claude): ok", out)
        self.assertIn("-p", (state / "argv.txt").read_text(encoding="utf-8"))
        self.assertIn("--session-id", (state / "argv.txt").read_text(encoding="utf-8"))

    def test_live_check_rejects_everything_that_is_not_this_run(self):
        self.use_real_guard()
        self.run_install()
        cases = {
            "no record (hooks did not run)": dict(writes=False),
            "record of an ordinary session": dict(role="orchestrator"),
            "record of another host": dict(record_host="claude"),
            "record from before the run": dict(ts_offset=-3600),
            "record that is not JSON": dict(raw="not json"),
            "record that is not an object": dict(raw="[1]"),
            "the CLI failed": dict(exit_code=1),
        }
        for label, cli in cases.items():
            code, out, _, _ = self.live_codex(**cli)
            self.assertEqual(code, 1, label)
            self.assertIn("live (codex): FAILED", out, label)
        self.assertIn("trust them", self.live_codex(writes=False)[1])
        self.assertIn("exited 1", self.live_codex(exit_code=1)[1])

    def test_live_check_needs_the_record_of_the_reported_session_not_any_codex_record(self):
        self.use_real_guard()
        self.run_install()
        # an unrelated worker session (valid, recent, same host) writes its record meanwhile; ours never does
        code, out, _, _ = self.live_codex(session_name="unrelated-concurrent-worker.json")
        self.assertEqual(code, 1, out)
        self.assertIn("live (codex): FAILED", out)
        self.assertNotIn("live (codex): ok", out)
        # the CLI never reports its session id: nothing can be tied to this run, however valid the record looks
        code, out, _, _ = self.live_codex(print_id=False)
        self.assertEqual(code, 0, out)
        self.assertIn("live (codex): unverified (the session id was not found", out)
        self.assertNotIn("live (codex): ok", out)
        # the id is read from the thread.started event only
        self.assertEqual(install.codex_thread_id('noise\n{"type": "turn.started"}\n{"type": "thread.started", "thread_id": "abc-1"}\n'), "abc-1")
        for text in ("", "not json", '{"type": "thread.started"}', '{"type": "thread.started", "thread_id": 5}',
                     '{"type": "turn.started", "thread_id": "x"}', '[1]'):
            self.assertIsNone(install.codex_thread_id(text), text)

    def test_live_check_ignores_records_that_existed_before(self):
        self.use_real_guard()
        self.run_install()
        state, env = self.live_env()
        (state / "sessions" / "live.json").write_text(
            json.dumps({"role": "worker", "host": "codex", "ts": int(time.time())}), encoding="utf-8")  # another session, just now
        code, out, _ = self.codex_check("--live", TAGTEAM_CLI_OPENAI=self.fake_cli("codexno", writes=False), **env)
        self.assertEqual(code, 1)
        self.assertIn("live (codex): FAILED", out)

    def test_live_claude_record_must_carry_the_session_id_it_was_given(self):
        self.run_install()
        state, env = self.live_env()
        with mock.patch.dict(os.environ, {"TAGTEAM_CLI_CLAUDE": self.fake_cli("claudecli", host="claude", session_name="other.json"), **env}):
            code, out, _ = self.run_main("--check", "--live")
        self.assertEqual(code, 1)
        self.assertIn("live (claude): FAILED", out)

    def test_live_is_off_unless_asked(self):
        self.run_install()
        called = []
        with mock.patch.object(install, "live_check", lambda *a: called.append(a) or ("ok", "")):
            self.run_main("--check", "--host", "codex")
        self.assertEqual(called, [])

    def test_windows_elevated_sandbox_warning(self):
        self.use_real_guard()
        self.run_install()
        home = self.dst.parent / "codex-home"
        home.mkdir()
        cfg = home / "config.toml"
        cases = (('[windows]\nsandbox = "elevated"\n', True), ('windows.sandbox = "elevated"\n', True),
                 ('[windows]\nsandbox = "unelevated"\n', False), ('[other]\nsandbox = "elevated"\n', False), ("", False))
        for text, warns in cases:
            cfg.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                with mock.patch.object(install, "is_windows", return_value=True):
                    code, out, _ = self.run_main("--check", "--host", "codex")
                    self.assertEqual(code, 0, out)
                    self.assertEqual("windows.sandbox is elevated" in out, warns, text)
                with mock.patch.object(install, "is_windows", return_value=False):
                    code, out, _ = self.run_main("--check", "--host", "codex")
                    self.assertNotIn("windows.sandbox is elevated", out)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home / "none")}):
            self.assertIsNone(install.codex_windows_sandbox())

    def test_check_state_folder_problems(self):
        self.run_install()
        self.git_init(self.dst)
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(self.dst / ".git" / "tagteam")}):
            code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("is inside .git", out)
        blocker = self.dst.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(blocker / "state")}):
            code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("not writable", out)
        (self.dst / ".gitignore").write_text("node_modules\n", encoding="utf-8")
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("does not ignore .tagteam/", out)

    def test_codex_handler_check_leaves_no_bytecode_even_when_the_parent_allows_it(self):
        self.use_real_guard()
        self.run_install()
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)  # restored by the setUp patch
        code, out, _ = self.codex_check()
        self.assertEqual(code, 0, out)
        self.assertIn("handler (codex): ok", out)
        self.assertEqual([t for t in self.tree(self.dst) if "__pycache__" in t], [])

    def test_static_checks_call_no_file_creating_operation(self):
        import builtins
        import pathlib
        import shutil as shutil_mod
        self.use_real_guard()
        self.run_install()
        real_open = builtins.open

        def guarded_open(file, mode="r", *a, **k):
            if set(mode) & set("wax+"):
                raise AssertionError(f"static check opened {file!r} for writing")
            return real_open(file, mode, *a, **k)

        def boom(name):
            def f(*a, **k):
                raise AssertionError(f"static check called {name}")
            return f
        patches = [mock.patch.object(builtins, "open", guarded_open)]
        for obj, attr in ((tempfile, "mkstemp"), (tempfile, "mkdtemp"), (tempfile, "NamedTemporaryFile"), (os, "makedirs"),
                          (os, "mkdir"), (shutil_mod, "copy2"), (shutil_mod, "copyfile"), (pathlib.Path, "mkdir"),
                          (pathlib.Path, "write_text"), (pathlib.Path, "write_bytes"), (pathlib.Path, "touch")):
            patches.append(mock.patch.object(obj, attr, boom(f"{obj.__name__}.{attr}")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        notes = []
        for host in ("claude", "codex", "agy"):
            with contextlib.redirect_stdout(io.StringIO()):
                errors, warnings, n = install.static_checks(self.dst, self.src, host)
            notes += n
            self.assertEqual([e for e in errors if "static check" in e], [], host)
        self.assertTrue(any("writability unverified" in n for n in notes), notes)

    def test_state_access_problem_reads_flags_only(self):
        blocker = self.dst.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        self.assertIn("is not a folder", install.state_access_problem(blocker / "state"))
        self.assertIsNone(install.state_access_problem(self.dst / "does" / "not" / "exist"))
        self.assertFalse((self.dst / "does").exists())
        with mock.patch.object(install.os, "access", return_value=False):
            self.assertIn("no write permission", install.state_access_problem(self.dst))

    def test_check_codex_creates_nothing(self):
        self.use_real_guard()
        self.run_install()
        before = self.tree(self.dst)
        code, out, _ = self.codex_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tree(self.dst), before)
        self.assertFalse((self.dst / ".tagteam").exists())

    def tree(self, root):
        return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if ".git" not in p.relative_to(root).parts)

    def test_install_and_check_leave_no_bytecode_in_the_project(self):
        self.src = build_source(self.src.parent / "src2", guard="import state\n" + STUB)  # a guard that imports state.py
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)  # restored by the setUp patch
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        self.run_install()
        self.assertEqual([t for t in self.tree(self.dst) if "__pycache__" in t], [])
        before = self.tree(self.dst)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.main([str(self.dst), "--check"]), 0, out.getvalue())
        self.assertEqual(self.tree(self.dst), before)

    def make_super(self):
        """A superproject with the target as its submodule `libs/sub` (local paths only)."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always")

        def git(cwd, *args):
            subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, env=env)
        base = self.dst.parent
        sub, sup = base / "subsrc", base / "sup"
        for d in (sub, sup):
            d.mkdir()
            git(d, "init", "-q")
            git(d, "commit", "-q", "--allow-empty", "-m", "i")
        git(sup, "submodule", "add", "-q", str(sub), "libs/sub")
        git(sup, "commit", "-q", "-m", "sm")
        return sup, sup / "libs" / "sub", git

    def test_state_ignore_rule_goes_to_the_checkout_that_holds_the_state_folder(self):
        sup, sub, git = self.make_super()
        wt = self.dst.parent / "subwt"
        git(sub, "worktree", "add", "-q", str(wt), "-b", "w")
        main = self.dst.parent / "main"
        main.mkdir()
        git(main, "init", "-q")
        git(main, "commit", "-q", "--allow-empty", "-m", "i")
        mainwt = self.dst.parent / "mainwt"
        git(main, "worktree", "add", "-q", str(mainwt), "-b", "w2")
        for target, root in ((sub, sup), (wt, sup), (mainwt, main)):
            code, out = self.run_install_into(target)
            self.assertEqual(code, 0, out)
            root_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertEqual(root_lines.count(".tagteam/"), 1, target)
            own = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn(".claude/tagteam/models.local.json", own)
            self.assertNotIn(".tagteam/", own, target)  # nothing is created in the target itself
            self.run_install_into(target)  # again: no duplicate rule
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8").splitlines().count(".tagteam/"), 1)

    def run_install_into(self, target):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.main([str(target), "--source", str(self.src), "--yes"])
        return code, out.getvalue()

    def test_check_warns_about_an_old_state_folder_and_stops_after_update(self):
        self.run_install()
        self.git_init(self.dst)
        self.write_old("exhausted.json", "old")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = install.main([str(self.dst), "--check"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("old state folder", out.getvalue())
        self.run_install("--update")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            install.main([str(self.dst), "--check"])
        self.assertNotIn("old state folder", out.getvalue())

    def test_check_fails_when_the_skill_copies_differ_or_one_is_missing(self):
        self.run_install()
        (self.dst / ".agents/skills/s1/SKILL.md").write_text("changed", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.main([str(self.dst), "--check"]), 1)
        self.assertIn("differ", out.getvalue())
        self.run_install("--update")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main([str(self.dst), "--check"]), 0)
        shutil.rmtree(self.dst / ".agents/skills")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.main([str(self.dst), "--check"]), 1)
        self.assertIn("missing", out.getvalue())

    def test_codex_hooks_absolute_path(self):
        self.run_install()
        raw = (self.dst / ".codex/hooks.json").read_text(encoding="utf-8")
        h = json.loads(raw)["hooks"]["PreToolUse"][0]["hooks"][0]
        guard = (self.dst / ".claude/tagteam/role_guard.py").resolve().as_posix()
        self.assertEqual(h["command"], f'python3 "{guard}" PreToolUse --host codex')
        self.assertEqual(h["commandWindows"], f'python "{guard}" PreToolUse --host codex')

    def test_update_preserves_local_state(self):
        self.run_install()
        (self.dst / "AGENTS.md").write_text(
            "# old head\n\n## Project policy\n\nMY POLICY\n", encoding="utf-8")
        (self.dst / ".claude/tagteam/models.local.json").write_text('{"mine": 1, "available": {"google": {"ids": ["g"]}}}', encoding="utf-8")
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
        self.assertEqual((self.dst / ".claude/tagteam/models.local.json").read_text(), '{"mine": 1, "available": {"google": {"ids": ["g"]}}}')
        self.assertEqual((self.dst / ".codex/hooks.json").read_text(), '{"trusted": true}')
        self.assertIn("kept", out)
        s = json.loads((self.dst / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(s["hooks"], {"PreToolUse": ["new"]})
        self.assertEqual(s["subagentPromptCacheTtl"], "1h")
        self.assertEqual(s["permissions"], {"allow": ["x"]})

    def test_existing_agents_md_without_policy_marker_is_kept_under_policy(self):
        (self.dst / "AGENTS.md").write_text("# My rules\n\nDo X.\nDo Y.\n", encoding="utf-8")
        code, out = self.run_install()
        self.assertEqual(code, 0)
        agents = (self.dst / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("intro v2", agents)
        self.assertNotIn("template placeholder", agents)
        self.assertEqual(agents.count("## Project policy"), 1)
        self.assertGreater(agents.index("# My rules"), agents.index("## Project policy"))
        self.assertIn("Do X.\nDo Y.\n", agents)
        self.assertIn("kept (moved under Project policy)", out)
        again = self.run_install("--update")[1]  # now the marker exists: no second move
        self.assertEqual((self.dst / "AGENTS.md").read_text(encoding="utf-8"), agents)
        self.assertNotIn("moved under Project policy", again)

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

    def test_gitignore_lists_local_files_and_the_state_folder_in_every_project(self):
        local = [".claude/tagteam/models.local.json", ".claude/tagteam/bench.local.json"]
        self.run_install()
        text = (self.dst / ".gitignore").read_text(encoding="utf-8")
        for line in local + [".tagteam/"]:
            self.assertIn(line, text.splitlines())
        self.run_install("--update")
        self.assertEqual((self.dst / ".gitignore").read_text(encoding="utf-8"), text)
        git_dst = self.dst.parent / "gitproj"  # a git project ignores .tagteam/ too (state is no longer inside .git)
        (git_dst / ".git").mkdir(parents=True)
        (git_dst / ".gitignore").write_text("node_modules\n.claude/tagteam/models.local.json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            install.main([str(git_dst), "--source", str(self.src), "--yes"])
        lines = (git_dst / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["node_modules", local[0], local[1], ".tagteam/"])

    def test_bench_written_once_when_key_set_and_file_missing(self):
        os.environ["TAGTEAM_AA_API_KEY"] = "k-123"
        code, out = self.run_install()
        self.assertEqual(code, 0)
        self.assertTrue(self.bench.is_file())
        self.assertEqual(self.calls.read_text().splitlines(), ["--write-bench --timeout 15|k-123"])
        self.assertNotIn("k-123", out)
        self.run_install("--update")  # file exists: not touched again
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_available_written_once(self):
        code, out = self.run_install()
        self.assertEqual(code, 0)
        avail_log = self.dst / ".claude/tagteam/avail.log"
        self.assertEqual(avail_log.read_text().splitlines(), ["--write-available --timeout 30"])
        local = json.loads((self.dst / ".claude/tagteam/models.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["available"], {"openai": {"ids": ["m1"]}})
        self.assertEqual(local["plans"], {})  # other keys kept
        self.assertIn("available model lists written", out)
        self.run_install("--update")  # lists exist: not asked again
        self.assertEqual(len(avail_log.read_text().splitlines()), 1)

    def test_available_retried_for_missing_vendor(self):
        self.run_install()
        avail_log = self.dst / ".claude/tagteam/avail.log"
        (self.src / "harness/tagteam/models.json").write_text(json.dumps(
            {"vendors": {"openai": {}, "claude": {}}}), encoding="utf-8")
        self.run_install("--update")  # claude has no list yet: asked again
        self.assertEqual(len(avail_log.read_text().splitlines()), 2)

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

    def use_real_hooks(self):
        self.use_real_guard()
        shutil.copy(REAL_SETTINGS, self.src / "harness/claude/settings.json")

    def test_check_runs_the_installed_hook_commands_as_written(self):
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("hook commands (claude): ok", out)
        # the wiring in the template's settings.json is what is tested: a guard that allows everything fails it
        (self.dst / ".claude/tagteam/role_guard.py").write_text(ALLOW_ALL, encoding="utf-8")
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("hook commands (claude): FAILED", out)
        self.assertIn("hook command 'PreToolUse worker git commit': expected exit 2, got 0", out)

    def test_check_reports_a_hook_command_whose_interpreter_is_not_on_path(self):
        # MAC4: an old settings.json calls a bare `python`. The shell cannot find it (127), the host reads that as
        # 'no objection', and the sys.executable smoke test never sees it.
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")
        path = self.dst / ".claude/settings.json"
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        for groups in data["hooks"].values():
            for g in groups:
                for h in g["hooks"]:
                    h["command"] = 'tagteam-no-such-python "${CLAUDE_PROJECT_DIR}/.claude/tagteam/role_guard.py" ' \
                                   + h["command"].rsplit(" ", 1)[1]
        path.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("hook commands (claude): FAILED", out)
        self.assertIn("hook command 'PreToolUse worker git commit': expected exit 2, got 127", out)
        self.assertIn("python3/python was not found", out)

    def edit_settings(self, fn):
        path = self.dst / ".claude/settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_check_fails_when_the_pretooluse_matcher_does_not_select_bash(self):
        # Claude Code calls the hook only for tools the matcher selects: a Read-only matcher guards nothing
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")
        self.edit_settings(lambda d: d["hooks"]["PreToolUse"][0].update(matcher="Read"))
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("hook commands (claude): FAILED", out)
        self.assertIn("no tagteam hook selects Bash (matchers: 'Read')", out)
        for matcher, hits in (("Bash", True), ("Edit|Bash", True), (None, True), ("*", True), ("", True), ("Bas", False),
                              ("Bash2", False), ("(", False)):
            self.assertEqual(install.matcher_hits(matcher, "Bash"), hits, matcher)
        self.edit_settings(lambda d: d["hooks"]["PreToolUse"][0].update(matcher="Edit|Bash"))
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_check_accepts_tagteam_hooks_split_over_groups_if_one_selects_bash(self):
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")

        def split(d):
            group = d["hooks"]["PreToolUse"][0]
            other = copy.deepcopy(group)
            group["matcher"], other["matcher"] = "Bash", "Edit|Write"
            d["hooks"]["PreToolUse"].append(other)
        self.edit_settings(split)
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("hook commands (claude): ok", out)
        self.assertEqual([m for m, _, _ in install.settings_guard_hooks(self.dst, "PreToolUse")], ["Bash", "Edit|Write"])
        # the Bash group is the one that counts: without it nothing guards shell commands
        self.edit_settings(lambda d: d["hooks"]["PreToolUse"][0].update(matcher="Read"))
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("no tagteam hook selects Bash (matchers: 'Read', 'Edit|Write')", out)

    def test_check_runs_the_hooks_in_the_project_wherever_it_is_started_from(self):
        # a hook with a relative path works only from the project folder, which is where Claude Code runs it
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")
        py = Path(sys.executable).as_posix()

        def relative(d):
            for event, groups in d["hooks"].items():
                for g in groups:
                    for h in g["hooks"]:
                        h["command"] = f'"{py}" .claude/tagteam/role_guard.py {event}'
        self.edit_settings(relative)
        elsewhere = self.dst.parent / "elsewhere"
        elsewhere.mkdir()
        here = os.getcwd()
        os.chdir(elsewhere)
        try:
            code, out, _ = self.run_main("--check")
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, out)
        self.assertIn("hook commands (claude): ok", out)

    def test_check_runs_each_hook_within_its_own_timeout(self):
        self.use_real_hooks()
        self.run_install()
        if not install.hook_shell():
            self.skipTest("no sh / Git Bash")
        self.edit_settings(lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(timeout=7))
        seen = []
        real = subprocess.run

        def spy(argv, *a, **kw):
            seen.append(kw.get("timeout"))
            if kw.get("timeout") == 7:
                raise subprocess.TimeoutExpired(argv, 7)  # the shim is slower than the hook may be
            return real(argv, *a, **kw)
        with mock.patch.object(install.subprocess, "run", spy):
            fails, _ = install.hook_command_test(self.dst)
        self.assertEqual(seen.count(7), 2)  # both PreToolUse runs
        self.assertEqual(seen.count(10), 2)  # SessionStart and UserPromptSubmit keep the template's 10 seconds
        self.assertEqual(len(fails), 2)
        self.assertIn("no answer within the hook's timeout of 7s", fails[0])
        self.edit_settings(lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].pop("timeout"))
        self.assertEqual(install.settings_guard_hooks(self.dst, "PreToolUse")[0][1], install.HOOK_TIMEOUT_DEFAULT)

    def test_check_fails_when_no_python_name_is_on_path(self):
        self.use_real_hooks()
        self.run_install()
        shell = install.hook_shell()
        if not shell:
            self.skipTest("no sh / Git Bash")
        empty = self.dst.parent / "empty-path"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty), "CLAUDE_CODE_GIT_BASH_PATH": shell}):
            code, out, _ = self.run_main("--check")
        self.assertEqual(code, 1, out)
        self.assertIn("neither python3 nor python is on PATH", out)
        self.assertIn("python3/python was not found", out)
        self.assertIn("hook commands (claude): FAILED", out)

    def test_check_names_the_python_the_codex_hooks_call(self):
        self.use_real_guard()
        self.run_install()
        empty = self.dst.parent / "empty-path"
        empty.mkdir()
        name = "python" if install.is_windows() else "python3"
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            code, out, _ = self.run_main("--check", "--host", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn(f"{name} is not on PATH: the Codex hooks call it", out)

    def test_check_does_not_stand_in_python_for_python3(self):
        self.run_install()
        only = self.dst.parent / "only-python"
        only.mkdir()
        fake = only / ("python.bat" if os.name == "nt" else "python")
        fake.write_text("x", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(only)}):
            self.assertEqual(install.interpreter_problems("claude")[0], [])  # one of the two names is enough for Claude
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                install.static_checks(self.dst, self.src, "claude")
        self.assertIn("found    python:", printed.getvalue())
        self.assertNotIn("found    python3", printed.getvalue())

    @unittest.skipIf(os.name == "nt", "the system PATH of an app started from the desktop is a macOS/Linux matter")
    def test_check_warns_when_python_is_only_on_the_shell_path(self):
        self.run_install()
        only = self.dst.parent / "shell-only"
        only.mkdir()
        fake = only / "python3"
        fake.write_text("x", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(only)}), mock.patch.object(install.shutil, "which", wraps=shutil.which) as w:
            real = shutil.which
            w.side_effect = lambda n, path=None: None if path == install.SYSTEM_PATH else real(n, path=path)
            errors, warnings = install.interpreter_problems("claude")
        self.assertEqual(errors, [])
        self.assertTrue(any("found through your shell PATH only" in x for x in warnings), warnings)

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


class HookCommandTests(unittest.TestCase):
    """The commands of harness/claude/settings.json, run as the host runs them (sh -c; Git Bash on Windows), with
    fake interpreters on a PATH that holds nothing else."""

    def setUp(self):
        self.shell = install.hook_shell()
        if not self.shell:
            self.skipTest("no sh / Git Bash")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.project = self.base / "my proj"  # a space: the guard path must stay one argument
        (self.project / ".claude").mkdir(parents=True)
        shutil.copy(REAL_SETTINGS, self.project / ".claude/settings.json")
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def fake(self, name, body):
        f = self.bin / name
        f.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        f.chmod(0o755)

    def probing(self, tag, code=3):
        """A fake interpreter: -c (the probe) succeeds; running the guard prints its tag, then one line per argument."""
        return f'if [ "$1" = "-c" ]; then exit 0; fi\necho "{tag}"\nfor a in "$@"; do echo "[$a]"; done\nexit {code}\n'

    def run_hook(self, event):
        (command,) = install.settings_guard_commands(self.project, event)
        env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_DELEGATE_RUN", "PATH")}
        env.update(PATH=str(self.bin), CLAUDE_PROJECT_DIR=str(self.project.as_posix() if os.name == "nt" else self.project))
        p = subprocess.run([self.shell, "-c", command], input="{}", capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def test_without_any_python_pretooluse_blocks_and_the_others_warn(self):
        code, out, err = self.run_hook("PreToolUse")
        self.assertEqual(code, 2)  # fail closed: 127 would read as 'no objection'
        self.assertIn("python3/python not found on PATH", err)
        for event in ("SessionStart", "UserPromptSubmit"):  # exit 2 here would stop every prompt, and the fix with it
            code, out, err = self.run_hook(event)
            self.assertEqual(code, 0, event)
            self.assertIn("role guard is off", out, event)

    def test_python3_only_python_only_and_both(self):
        self.fake("python3", self.probing("p3"))
        code, out, _ = self.run_hook("PreToolUse")
        self.assertEqual(code, 3)  # the guard's own exit code comes through
        # exactly two arguments: the guard path (with its space, in one piece) and the event
        guard = f"[{self.project.as_posix()}/.claude/tagteam/role_guard.py]"
        self.assertEqual(out.splitlines(), ["p3", guard, "[PreToolUse]"])
        self.fake("python", self.probing("p2"))
        self.assertEqual(self.run_hook("SessionStart")[1].splitlines(), ["p3", guard, "[SessionStart]"], "python3 comes first")
        (self.bin / "python3").unlink()
        self.assertEqual(self.run_hook("UserPromptSubmit")[1].splitlines(), ["p2", guard, "[UserPromptSubmit]"])

    def test_a_python3_that_cannot_run_is_skipped(self):
        # the Microsoft Store alias, or a pyenv shim without a version: present on PATH, unusable
        self.fake("python3", "exit 9\n")
        self.fake("python", self.probing("p2"))
        code, out, _ = self.run_hook("PreToolUse")
        self.assertEqual((code, out.splitlines()[0]), (3, "p2"))
        (self.bin / "python").unlink()
        code, _, err = self.run_hook("PreToolUse")
        self.assertEqual(code, 2)
        self.assertIn("not found on PATH", err)

    def test_a_denial_is_never_followed_by_the_other_interpreter(self):
        self.fake("python3", self.probing("p3", code=2))
        self.fake("python", self.probing("p2"))
        code, out, _ = self.run_hook("PreToolUse")
        self.assertEqual(code, 2)
        self.assertEqual(out.splitlines()[0], "p3")
        self.assertNotIn("p2", out)

    def test_all_three_commands_name_the_guard_and_their_event(self):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            (command,) = install.settings_guard_commands(self.project, event)
            self.assertTrue(command.endswith(f'role_guard.py" {event}'), command)
            self.assertEqual(command.count("role_guard.py"), 1, "the guard is named once: no 'python3 ... || python ...'")


SKILL = INSTALL.parent / "harness/skills/model-update/SKILL.md"
STATE_STUB = "import os\ndef state_dir(cwd=None):\n    return os.environ['TAGTEAM_STATE_DIR']\n"
CLI_FILES = ["/opt/cli/claude", "/home/u/.local/bin/codex"]
JOB_PATH = "/opt/cli:/home/u/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"  # CLI folders, the interpreter's (/usr/bin), system


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.proj = (base / "proj").resolve()
        (self.proj / ".claude/tagteam").mkdir(parents=True)
        (self.proj / ".claude/tagteam/models_collect.py").write_text("# collector\n", encoding="utf-8")
        (self.proj / ".claude/tagteam/state.py").write_text(STATE_STUB, encoding="utf-8")
        self.state = base / "state"
        self.state.mkdir()
        self.home = base / "home"
        self.calls = []
        self.fail = lambda argv: False
        self.crontab = ""
        self.probed = []
        self.probe_rc = 0

        def fake_probe(argv, path):  # never start a real CLI from a test
            self.probed.append((argv, path))
            return (self.probe_rc, "cli 1.0" if self.probe_rc == 0 else "env: node: No such file or directory")
        patcher = mock.patch.object(install, "probe_cli", fake_probe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, argv, input_text=None):
        self.calls.append((argv, input_text))
        if argv[:2] == ["crontab", "-l"]:
            return (0, self.crontab) if self.crontab else (1, "no crontab")
        return (1, "boom") if self.fail(argv) else (0, "")

    def sched(self, plat, dry=False):
        return install.Scheduler(self.proj, plat, self.runner, dry, python="/usr/bin/py", home=self.home, uid=501,
                                 clis=CLI_FILES)

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
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(self.state / "new")}):
            code, out = self.quiet(s.schedule)
        plist = self.home / f"Library/LaunchAgents/net.tagteam.{self.name()}.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["ProgramArguments"], self.cmd())
        self.assertEqual(data["StartCalendarInterval"], {"Weekday": 1, "Hour": 9, "Minute": 0})
        self.assertEqual(data["WorkingDirectory"], str(self.proj))
        # launchd hands a job /usr/bin:/bin:/usr/sbin:/sbin: the CLI folders and the interpreter's must be named
        self.assertEqual(data["EnvironmentVariables"], {"PATH": JOB_PATH})
        self.assertIn("PATH     " + JOB_PATH, out)
        # a failing run must leave a trace: the log is in the state folder, whose folder launchd does not create
        log = str(self.state / "new" / "model-update.log")
        self.assertEqual((data["StandardOutPath"], data["StandardErrorPath"]), (log, log))
        self.assertTrue((self.state / "new").is_dir())
        self.assertEqual(self.calls[-1][0], ["launchctl", "bootstrap", "gui/501", str(plist)])
        self.quiet(self.sched("macos").schedule)  # overwrite: boots out the old one first
        self.assertIn(["launchctl", "bootout", f"gui/501/net.tagteam.{self.name()}"], [c[0] for c in self.calls])
        self.calls.clear()
        self.assertEqual(self.quiet(self.sched("macos").unschedule)[0], 0)
        self.assertEqual(self.calls[0][0], ["launchctl", "bootout", f"gui/501/net.tagteam.{self.name()}"])
        self.assertFalse(plist.exists())

    def test_macos_dry_run_shows_path_and_log_but_creates_nothing(self):
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(self.state / "new")}):
            code, out = self.quiet(self.sched("macos", dry=True).schedule)
        self.assertEqual(code, 0)
        self.assertIn("EnvironmentVariables", out)
        self.assertIn(JOB_PATH, out)
        self.assertIn("StandardOutPath", out)
        self.assertEqual(self.probed, [], "a preview starts no CLI")
        for f in CLI_FILES:
            self.assertIn(f"[dry-run] would run: {f} --version", out)
        self.assertFalse((self.state / "new").exists())
        self.assertFalse((self.home / "Library").exists())

    def test_macos_without_a_computable_state_folder_has_no_log_paths(self):
        (self.proj / ".claude/tagteam/state.py").unlink()
        self.quiet(self.sched("macos").schedule)
        data = plistlib.loads(next((self.home / "Library/LaunchAgents").glob("*.plist")).read_bytes())
        self.assertNotIn("StandardOutPath", data)
        self.assertEqual(data["EnvironmentVariables"], {"PATH": JOB_PATH})

    def test_unschedule_deletes_the_job_log_too(self):
        for fn in ("model-update.log", "keep.txt"):
            (self.state / fn).write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAGTEAM_STATE_DIR": str(self.state)}):
            self.quiet(self.sched("macos").unschedule)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["keep.txt"])

    def make_cli(self, folder, name, first_line=None):
        folder.mkdir(exist_ok=True)
        f = folder / (name + (".bat" if os.name == "nt" else ""))
        f.write_text((first_line + "\n" if first_line else "") + "x\n", encoding="utf-8", newline="\n")
        f.chmod(0o755)
        return f

    def test_cli_paths_are_the_vendor_clis_found_on_path(self):
        a, b = self.state / "a", self.state / "b"
        fa, fb = self.make_cli(a, "claude"), self.make_cli(b, "codex")
        self.make_cli(b, "other")
        models = self.proj / ".claude/tagteam/models.json"
        models.write_text(json.dumps(
            {"vendors": {"claude": {"cli": "claude"}, "openai": {"cli": "codex"}, "google": {"cli": "agy"}}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(a), str(b)])}):
            self.assertEqual(install.cli_paths(self.proj), [shutil.which("claude"), shutil.which("codex")])
            models.unlink()  # no models.json: the three known names
            self.assertEqual(len(install.cli_paths(self.proj)), 2)
            s = install.Scheduler(self.proj, "macos", self.runner, True, python="/opt/py/python3")
            self.assertEqual(s.job_path(), f"{a}:{b}:/opt/py:/usr/bin:/bin:/usr/sbin:/sbin")

    def test_a_cli_script_that_needs_node_gets_the_folder_of_node(self):
        # npm CLI in ~/.npm-global/bin, node in /opt/homebrew/bin: '#!/usr/bin/env node' fails unless node is on PATH
        cli, node = self.state / "npm-global-bin", self.state / "brew-bin"
        script = self.make_cli(cli, "codex", "#!/usr/bin/env node")
        native = self.make_cli(cli, "claude")
        self.make_cli(node, "node")
        self.assertEqual((install.env_runtime(script), install.env_runtime(native)), ("node", None))
        with mock.patch.dict(os.environ, {"PATH": str(node)}):
            s = install.Scheduler(self.proj, "macos", self.runner, True, python="/usr/bin/py", clis=[str(script), str(native)])
            self.assertEqual(s.job_path(), f"{cli}:{node}:/usr/bin:/bin:/usr/sbin:/sbin")
        for first, want in (("#!/usr/bin/env -S node --no-warnings", "node"), ("#!/usr/bin/env FOO=1 python3", "python3"),
                            ("#!/bin/sh", None), ("#!/usr/bin/env", None), ("", None)):
            f = self.make_cli(cli, "t", first)
            self.assertEqual(install.env_runtime(f), want, first)
        self.assertIsNone(install.env_runtime(cli / "missing"))

    def test_the_version_check_runs_in_an_empty_folder_with_only_the_job_path(self):
        here = os.getcwd()
        code = "import os; print(os.getcwd() + '|' + os.environ.get('PATH', '') + '|' + str(sorted(os.listdir('.'))))"
        rc, line = REAL_PROBE([sys.executable, "-c", code], "/the/job/path")
        self.assertEqual(rc, 0, line)
        cwd, path, listing = line.split("|")
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(here))
        self.assertEqual((path, listing), ("/the/job/path", "[]"))
        self.assertEqual(REAL_PROBE([str(self.state / "no-such-cli"), "--version"], "/x")[0], 127)

    def test_schedule_starts_each_cli_with_the_job_path_and_warns_when_it_fails(self):
        code, out = self.quiet(self.sched("macos").schedule)
        self.assertEqual(code, 0)
        self.assertEqual([a for a, _ in self.probed], [[f, "--version"] for f in CLI_FILES])
        self.assertTrue(all(path == JOB_PATH for _, path in self.probed))
        self.assertIn("claude --version with that PATH: ok", out)
        self.probe_rc = 127
        code, out = self.quiet(self.sched("macos").schedule)
        self.assertEqual(code, 0, "a CLI that does not start is a warning, not a refusal to schedule")
        self.assertIn("WARN failed (127): env: node: No such file or directory", out)
        self.probed.clear()
        self.quiet(self.sched("windows").schedule)  # no PATH problem on Windows: nothing to probe
        self.assertEqual(self.probed, [])

    def test_linux_crontab_replaces_marked_line_only(self):
        mark = "# " + self.name()
        self.crontab = f"5 5 * * * other job\n0 9 * * 1 old {mark}\n"
        self.quiet(self.sched("linux").schedule)
        written = self.calls[-1][1]
        self.assertEqual(self.calls[-1][0], ["crontab", "-"])
        lines = written.splitlines()
        self.assertEqual(lines[0], "5 5 * * * other job")
        self.assertEqual(sum(ln.endswith(mark) for ln in lines), 1)
        self.assertTrue(lines[1].startswith(f"0 9 * * 1 PATH={JOB_PATH} /usr/bin/py "), lines[1])  # cron's PATH is minimal too
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

    def test_unschedule_deletes_proposal_files_from_the_new_state_folder(self):
        shutil.copy(REAL_STATE, self.proj / ".claude/tagteam/state.py")
        subprocess.run(["git", "-C", str(self.proj), "init", "-q"], check=True, capture_output=True)
        new = self.proj / ".tagteam"
        new.mkdir()
        old = self.proj / ".git/tagteam"
        old.mkdir()
        for folder in (new, old):
            for fn in ("model-update.json", "keep.txt"):
                (folder / fn).write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ):
            os.environ.pop("TAGTEAM_STATE_DIR", None)
            self.assertEqual(install.installed_state_dir(self.proj), self.proj / ".tagteam")
            code, out = self.quiet(self.sched("windows").unschedule)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["keep.txt"])
        self.assertIn(str(new / "model-update.json"), out)
        self.assertTrue((old / "model-update.json").exists())  # the old location is not this command's business

    def test_installed_state_dir_is_none_without_an_installed_state_py(self):
        (self.proj / ".claude/tagteam/state.py").unlink()
        self.assertIsNone(install.installed_state_dir(self.proj))

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
