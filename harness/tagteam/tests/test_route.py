import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TAGTEAM = os.path.dirname(HERE)
sys.path.insert(0, TAGTEAM)
import route  # noqa: E402

FUTURE = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
CLIS = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "cfg")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg)
        os.makedirs(self.state)
        shutil.copy(os.path.join(TAGTEAM, "models.json"), self.cfg)
        self.exh_path = os.path.join(self.state, "exhausted.json")

    def local(self, data):
        with open(os.path.join(self.cfg, "models.local.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def exhausted(self, **vendors):
        with open(self.exh_path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "vendors": vendors}, f)

    def go(self, *argv, env=None, host="claude"):
        full_env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state, "PATH": self.tmp}
        full_env.update(CLIS if env is None else env)
        args = list(argv)
        if "--role" in args and "--host" not in args:
            args += ["--host", host]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, full_env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = route.main(args)
            except SystemExit as exc:
                code = exc.code
        text = out.getvalue()
        try:
            return code, json.loads(text)
        except ValueError:
            return code, text

    def route(self, role, *extra, **kw):
        return self.go("--role", role, "--json", *extra, **kw)

    def assertPick(self, res, model, effort, kind=None):
        code, r = res
        self.assertEqual(code, 0, r)
        self.assertEqual((r["model"], r["effort"]), (model, effort))
        if kind:
            self.assertEqual(r["launch"]["kind"], kind)
        return r

    # 1
    def test_defaults_host_claude(self):
        r = self.assertPick(self.route("design"), "claude-opus-5-5", "high", "native")
        self.assertIn("Check orchestrator cap", r["notice"])
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium", "native")
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "high", "run")
        self.assertIn("--read-only", r["launch"]["command"])
        self.assertIn("author_vendor_excluded:claude", r["reason"])
        self.assertIn("differs from author vendor claude", r["notice"])
        r = self.assertPick(self.route("read"), "claude-haiku-5-5", "low", "native")
        r = self.assertPick(self.route("translate"), "gemini-3.8-flash", "medium", "run")
        self.assertNotIn("--read-only", r["launch"]["command"])

    # 2
    def test_defaults_host_codex(self):
        self.assertPick(self.route("implement", host="codex"), "gpt-6.1-terra", "medium", "native")
        r = self.assertPick(self.route("review", "--author-vendor", "openai", host="codex"),
                            "claude-opus-5-5", "high", "run")
        self.assertEqual(r["vendor"], "claude")

    # 3
    def test_user_override(self):
        self.local({"roles": {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}}})
        r = self.assertPick(self.route("implement"), "claude-opus-5-5", "xhigh")
        self.assertEqual(r["reason"], ["user_override"])
        self.local({"roles": {"implement": {"model": "claude-opus-5-5"}},
                    "models": {"claude-opus-5-5": {"enabled": False}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")
        self.assertEqual(sorted(r["reason"]), ["default", "override_unusable"])

    # 4
    def test_override_same_vendor_as_author(self):
        self.local({"roles": {"review": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "high")
        self.assertIn("override_same_vendor_as_author", r["reason"])

    # 5
    def test_review_needs_author_vendor(self):
        self.assertEqual(self.go("--role", "review", "--host", "claude")[0], 2)
        self.assertEqual(self.go("--role", "review", "--author-vendor", "nope", "--host", "claude")[0], 2)
        self.assertEqual(self.go("--host", "claude")[0], 2)

    # 6
    def test_plans_and_orchestrator_cap(self):
        self.local({"plans": {"claude": "none"}})
        self.assertPick(self.route("implement"), "gpt-6.1-terra", "medium", "run")
        self.local({"plans": {"openai": "free"}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-argon", "high", "run")
        self.assertIn("plan_free_last:openai", r["reason"])
        self.local({"plans": {"google": "free"}})
        self.assertPick(self.route("translate"), "gemini-3.8-flash", "medium", "run")
        self.local({})
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-sonnet-5-5",
                                       "--orchestrator-effort", "high"), "claude-opus-5-5", "high", "run")
        self.assertIn("native_model_cap", r["reason"])
        self.assertEqual(r["launch"]["command"][r["launch"]["command"].index("--vendor") + 1], "claude")
        r = self.assertPick(self.route("implement", "--orchestrator-model", "claude-opus-5-5",
                                       "--orchestrator-effort", "low"), "claude-sonnet-5-5", "medium", "run")
        self.assertIn("native_model_cap", r["reason"])
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-fable-5-2",
                                       "--orchestrator-effort", "xhigh"), "claude-opus-5-5", "high", "native")
        self.assertNotIn("Check orchestrator cap", r["notice"])

    # 7
    def test_available_and_enabled(self):
        self.local({"available": {"openai": {"ids": ["gpt-6.1-terra"]}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-argon", "high")
        self.local({"available": {"openai": {"ids": ["gpt-6.1-terra"]}},
                    "models": {"gpt-6.1-sol": {"enabled": True}}})
        self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "high")
        self.local({"available": {"google": {"ids": ["gemini-3.8-flash-low"]}}})
        self.assertPick(self.route("translate"), "gpt-6.1-terra", "medium")  # flash-medium not offered; openai is next
        self.local({"available": {"google": {"ids": ["gemini-3.8-flash-medium"]}}})
        self.assertPick(self.route("translate"), "gemini-3.8-flash", "medium")

    # 8
    def test_exhausted_substitution(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-argon", "high")
        self.assertIn("exhausted_skip:openai", r["reason"])
        self.assertTrue(any(c.startswith("substituted:openai") for c in r["reason"]))
        self.assertIn("openai exhausted until", r["notice"])
        self.exhausted(openai={"since": PAST, "until": PAST, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "high")
        self.assertEqual(r["reason"][0], "default")

    # 9
    def test_incomplete_and_none(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        self.local({"plans": {"google": "none"}})
        code, r = self.route("review", "--author-vendor", "claude")
        self.assertEqual((code, r["status"]), (3, "incomplete"))
        self.assertIn("no independent reviewer", r["notice"])
        self.local({"plans": {"claude": "none", "openai": "none", "google": "none"}})
        code, r = self.route("implement")
        self.assertEqual((code, r["status"]), (4, "none"))

    # 10
    def test_effort_clamp_and_effort_by_role(self):
        self.local({"roles": {"implement": {"effort": "max"}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
        self.assertIn("effort_clamped", r["reason"])
        self.local({})
        self.assertPick(self.route("design", "--grade", "S", host="codex"), "gpt-6-astra", "medium", "native")
        self.assertPick(self.route("design", "--grade", "S"), "claude-fable-5-2", "high")

    # 11
    def test_cli_missing(self):
        env = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        r = self.assertPick(self.route("review", "--author-vendor", "claude", env=env), "gemini-4-argon", "high")
        self.assertIn("cli_missing:openai", r["reason"])
        self.assertPick(self.route("implement", host="codex", env=env), "gpt-6.1-terra", "medium", "native")

    # 12
    def test_mark_and_clear_exhausted(self):
        code, _ = self.go("--mark-exhausted", "openai", "--evidence", "usage limit", "--until", FUTURE)
        self.assertEqual(code, 0)
        with open(self.exh_path, encoding="utf-8") as f:
            data = json.load(f)
        e = data["vendors"]["openai"]
        self.assertEqual(data["schema"], 1)
        self.assertEqual((e["until"], e["evidence"], e["source"]), (FUTURE, "usage limit", "manual"))
        self.assertRegex(e["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.go("--mark-exhausted", "nope", "--evidence", "x")[0], 2)
        self.assertEqual(self.go("--mark-exhausted", "google")[0], 2)
        self.go("--mark-exhausted", "google", "--evidence", "x")
        self.go("--clear-exhausted", "openai")
        with open(self.exh_path, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)["vendors"]), ["google"])

    def test_broken_exhausted_file_is_empty(self):
        with open(self.exh_path, "w") as f:
            f.write("{nope")
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")

    def test_malformed_exhausted_shapes_are_empty(self):
        for body in ('{"schema":1,"vendors":["bad"]}', '[1]', '{"vendors":{"openai":"bad"}}'):
            with open(self.exh_path, "w") as f:
                f.write(body)
            self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")

    def test_cap_to_run_checks_cli(self):
        env = {"TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        cap = ("--orchestrator-model", "claude-sonnet-5-5")
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.1-sol", "high", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.local({"roles": {"design": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.1-sol", "high", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.assertNotIn("user_override", r["reason"])

    def test_candidate_skipped_by_default_but_selectable(self):
        path = os.path.join(self.cfg, "models.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["models"]["claude-sonnet-5-5"]["candidate"] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "medium")
        self.local({"roles": {"implement": {"model": "claude-sonnet-5-5"}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")
        self.assertEqual(r["reason"], ["user_override"])

    # 13
    def test_registry(self):
        with open(os.path.join(TAGTEAM, "models.json"), encoding="utf-8") as f:
            data = json.load(f)
        for mid, m in data["models"].items():
            self.assertIn(m["vendor"], data["vendors"], mid)
            self.assertIn(m["grade"], data["grades"], mid)
            self.assertTrue(m["efforts"] and all(e in route.EFFORTS for e in m["efforts"]), mid)
            self.assertNotIn("ultra", m["efforts"], mid)
            self.assertNotIn("ultra", json.dumps(m.get("effort_by_role", {})), mid)
            if m["vendor"] == "google":
                self.assertTrue(m.get("label"), mid)
            for b in m["bench"]:
                self.assertTrue({"source", "metric", "value", "date"} <= set(b), mid)
        for v, cfg in data["vendors"].items():
            self.assertTrue({"cli", "host", "model_arg", "cooldown_minutes"} <= set(cfg), v)
        for host, order in data["hosts"].items():
            self.assertTrue(set(order) <= set(data["vendors"]), host)
        for role in route.ROLES:
            self.assertIn(data["roles"][role]["grade"], data["grades"])
            self.assertIn(data["roles"][role]["effort"], route.EFFORTS)


if __name__ == "__main__":
    unittest.main()
