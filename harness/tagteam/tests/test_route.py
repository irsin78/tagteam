import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TAGTEAM = os.path.dirname(HERE)
HARNESS = os.path.dirname(TAGTEAM)
sys.path.insert(0, TAGTEAM)
import route  # noqa: E402

FUTURE = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
CLIS = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}


class RouteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "cfg")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg)
        os.makedirs(self.state)
        shutil.copy(os.path.join(TAGTEAM, "models.json"), self.cfg)
        self.exh_path = os.path.join(self.state, "exhausted.json")
        # an install root with the shipped agent definitions (the router reads their pinned model and effort)
        self.root = os.path.join(self.tmp, "root")
        shutil.copytree(os.path.join(HARNESS, "claude", "agents"), os.path.join(self.root, ".claude", "agents"))
        shutil.copytree(os.path.join(HARNESS, "codex", "agents"), os.path.join(self.root, ".codex", "agents"))

    def local(self, data):
        with open(os.path.join(self.cfg, "models.local.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def bench(self, models):
        with open(os.path.join(self.cfg, "bench.local.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "models": models}, f)

    @staticmethod
    def b(metric, value, effort):
        return {"source": "artificial_analysis", "metric": metric, "value": value, "effort": effort}

    def exhausted(self, **vendors):
        with open(self.exh_path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "vendors": vendors}, f)

    def go(self, *argv, env=None, host="claude-desktop"):
        full_env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state, "PATH": self.tmp,
                    "TAGTEAM_ROOT": self.root}
        full_env.update(CLIS if env is None else env)
        args = list(argv)
        if ("--role" in args or "--task" in args) and "--host" not in args:
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
        if role == "review" and "--grade" not in extra and "--author-model" not in extra:
            extra += ("--grade", "A")  # a review needs the author's grade (tested separately)
        return self.go("--role", role, "--json", *extra, **kw)

    def edit_file(self, rel, old, new):
        path = os.path.join(self.root, rel)
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old, new, 1))

    def assertPick(self, res, model, effort, kind=None):
        code, r = res
        self.assertEqual(code, 0, r)
        self.assertEqual((r["model"], r["effort"]), (model, effort))
        if kind:
            self.assertEqual(r["launch"]["kind"], kind)
        return r

    def models_edit(self, fn):
        path = os.path.join(self.cfg, "models.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        fn(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


class RouteTest(RouteBase):
    def test_defaults_host_claude_desktop(self):
        r = self.assertPick(self.route("design"), "claude-fable-5-2", "low", "session")
        self.assertIn("Check orchestrator cap", r["notice"])
        self.assertIn("host=claude-desktop", r["notice"])
        self.assertEqual(r["grade"], "A")
        self.assertEqual(r["launch"], {"kind": "session", "model": "claude-fable-5-2", "effort": "low"})
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high", "session")
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.2-sol", "medium", "run")
        self.assertIn("--read-only", r["launch"]["command"])
        self.assertNotIn("escalate", r["launch"])
        self.assertIn("author_vendor_excluded:claude", r["reason"])
        self.assertIn("differs from author vendor claude", r["notice"])
        r = self.assertPick(self.route("read"), "claude-haiku-5-5", "high", "session")
        self.assertEqual((r["grade"], r["reason"][-1]), ("C", "bench:none"))
        r = self.assertPick(self.route("translate"), "gemini-4-hydrogen", "medium", "run")
        self.assertNotIn("--read-only", r["launch"]["command"])

    def test_run_launch_names_the_interpreter_that_runs_the_router(self):
        # a bare "python" does not exist on a default macOS; sys.executable exists wherever route.py ran
        for host in ("claude-desktop", "codex"):
            r = self.assertPick(self.route("translate", host=host), "gemini-4-hydrogen", "medium", "run")
            self.assertEqual(r["launch"]["command"][:2], [sys.executable, ".claude/tagteam/run.py"])

    def test_defaults_host_codex(self):
        r = self.assertPick(self.route("implement", host="codex"), "gpt-6.1-sol", "medium", "subagent")
        self.assertEqual(r["launch"], {"kind": "subagent", "host": "codex", "agent": "implementer", "fork_turns": "none"})
        r = self.assertPick(self.route("design", host="codex"), "gpt-6.2-sol", "medium", "subagent")
        self.assertEqual(r["launch"]["agent"], "designer")
        r = self.assertPick(self.route("review", "--author-vendor", "openai", host="codex"),
                            "claude-fable-5-2", "low", "run")
        self.assertEqual((r["vendor"], r["launch"]["escalate"]), ("claude", True))
        r = self.assertPick(self.route("read", host="codex"), "gpt-6.1-terra", "low", "subagent")
        self.assertEqual(r["launch"]["agent"], "reader")

    def test_user_override(self):
        self.local({"roles": {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}}})
        r = self.assertPick(self.route("implement"), "claude-opus-5-5", "xhigh")
        self.assertEqual(r["reason"], ["user_override", "bench:none"])
        self.assertEqual(r["grade"], "S")  # grade of the chosen pair, not of the role
        self.local({"roles": {"implement": {"model": "claude-opus-5-5"}}})
        self.assertPick(self.route("implement"), "claude-opus-5-5", "low")  # lowest effort at the grade
        self.local({"roles": {"implement": {"model": "claude-opus-5-5"}},
                    "models": {"claude-opus-5-5": {"enabled": False}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
        self.assertEqual(sorted(r["reason"]), ["bench:none", "default", "override_unusable"])

    def test_override_same_vendor_as_author(self):
        self.local({"roles": {"review": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.2-sol", "medium")
        self.assertIn("override_same_vendor_as_author", r["reason"])

    def test_review_override_grade_mismatch_is_flagged(self):
        self.local({"roles": {"review": {"model": "gpt-6.1-terra", "effort": "low"}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude", "--grade", "A"), "gpt-6.1-terra", "low")
        self.assertIn("override_grade_mismatch", r["reason"])
        self.assertIn("grade C", r["notice"])

    def test_review_needs_the_author_grade(self):
        base = ("--role", "review", "--author-vendor", "claude", "--host", "claude")
        errors = {
            "no grade": base,
            "model without effort": base + ("--author-model", "claude-sonnet-5-5"),
            "effort without model": ("--role", "review", "--author-effort", "high", "--author-vendor", "claude"),
            "unknown effort": ("--role", "review", "--json", "--author-model", "claude-sonnet-5-5", "--author-effort", "ultra"),
            "unknown model": ("--role", "review", "--author-model", "nope", "--author-effort", "low"),
            "vendor differs from the model's": ("--role", "review", "--author-model", "claude-sonnet-5-5",
                                                "--author-effort", "high", "--author-vendor", "openai"),
        }
        for name, argv in errors.items():
            with self.subTest(name):
                self.assertEqual(self.go(*argv)[0], 2)
        code, r = self.go(*base, "--grade", "B", "--json")
        self.assertEqual((code, r["status"]), (0, "ok"))
        # the grade comes from the author's model and effort; the author vendor from the model
        code, r = self.go("--role", "review", "--host", "claude", "--json", "--author-model", "claude-sonnet-5-5",
                          "--author-effort", "xhigh")
        self.assertEqual((code, r["grade"]), (0, "A"))
        self.assertEqual(r["vendor"], "openai")
        self.assertEqual(self.go("--role", "review", "--author-model", "claude-sonnet-5-5", "--author-effort", "low",
                                 "--author-vendor", "claude", "--grade", "A", "--host", "claude")[0], 0)  # --grade wins

    def test_review_needs_author_vendor(self):
        for argv in (("--role", "review", "--host", "claude"),
                     ("--role", "review", "--author-vendor", "nope", "--host", "claude"),
                     ("--host", "claude")):
            with self.subTest(argv):
                self.assertEqual(self.go(*argv)[0], 2)

    def test_plans(self):
        self.local({"plans": {"claude": "none"}})
        self.assertPick(self.route("implement"), "gpt-6.1-sol", "medium", "run")
        self.local({"plans": {"openai": "free"}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-hydrogen", "xhigh", "run")
        self.assertIn("plan_free_last:openai", r["reason"])
        self.local({"plans": {"google": "free"}})
        self.assertPick(self.route("translate"), "gemini-4-hydrogen", "medium", "run")

    def test_over_cap_by_model_order_and_effort(self):
        # every A pair is over the cap of Sonnet high: still picked, launched through run.py
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-sonnet-5-5",
                                       "--orchestrator-effort", "high"), "claude-fable-5-2", "low", "run")
        self.assertIn("native_model_cap", r["reason"])
        self.assertEqual(r["launch"]["command"][r["launch"]["command"].index("--vendor") + 1], "claude")
        # Opus high: Fable (earlier in models.json) is over the cap, so the Opus pair is taken natively
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-opus-5-5",
                                       "--orchestrator-effort", "high"), "claude-opus-5-5", "high", "session")
        self.assertNotIn("native_model_cap", r["reason"])
        self.assertNotIn("Check orchestrator cap", r["notice"])
        # a later model is not over the cap; Fable keeps its place
        self.assertPick(self.route("design", "--orchestrator-model", "claude-fable-5-2",
                                   "--orchestrator-effort", "xhigh"), "claude-fable-5-2", "low", "session")
        # effort above the orchestrator's is over the cap even for the same model
        r = self.assertPick(self.route("implement", "--orchestrator-model", "claude-sonnet-5-5",
                                       "--orchestrator-effort", "low"), "claude-sonnet-5-5", "high", "run")
        self.assertIn("native_model_cap", r["reason"])
        # within the cap beats the default order: Opus low fits an Opus low orchestrator
        self.assertPick(self.route("implement", "--orchestrator-model", "claude-opus-5-5",
                                   "--orchestrator-effort", "low"), "claude-opus-5-5", "low", "session")

    def test_available_and_enabled(self):
        self.local({"available": {"openai": {"ids": ["gpt-6.1-terra"]}}})
        self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-hydrogen", "xhigh")
        self.local({"available": {"openai": {"ids": ["gpt-6.1-terra"]}},
                    "models": {"gpt-6.1-sol": {"enabled": True}}})
        self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "xhigh")
        self.local({"available": {"google": {"ids": ["gemini-3.8-flash-low"]}}})
        self.assertPick(self.route("translate"), "gpt-6.1-sol", "medium")  # no B pair offered by google
        self.local({"available": {"google": {"ids": ["gemini-3.8-flash-medium"]}}})
        self.assertPick(self.route("translate"), "gemini-3.8-flash", "medium")  # defaults entries unusable: next one

    def test_exhausted_substitution(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-hydrogen", "xhigh")
        self.assertIn("exhausted_skip:openai", r["reason"])
        self.assertTrue(any(c.startswith("substituted:openai") for c in r["reason"]))
        self.assertIn("openai exhausted until", r["notice"])
        self.exhausted(openai={"since": PAST, "until": PAST, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.2-sol", "medium")
        self.assertEqual(r["reason"][0], "default")

    def test_incomplete_and_none(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        self.local({"plans": {"google": "none"}})
        code, r = self.route("review", "--author-vendor", "claude")
        self.assertEqual((code, r["status"]), (3, "incomplete"))
        self.assertIn("no independent reviewer", r["notice"])
        self.local({"plans": {"claude": "none", "openai": "none", "google": "none"}})
        code, r = self.route("implement")
        self.assertEqual((code, r["status"]), (4, "none"))

    def test_effort_override_limits_candidates(self):
        self.local({"roles": {"implement": {"effort": "max"}}})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")  # only B pair at max
        self.local({"roles": {"implement": {"effort": "xhigh"}}})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "xhigh")  # sonnet xhigh is A
        self.local({"roles": {"implement": {"effort": "low"}}})
        self.assertPick(self.route("implement"), "claude-opus-5-5", "low")  # Opus low is the only B pair at low
        self.local({"roles": {"design": {"effort": "max"}}})
        code, r = self.route("design")  # nothing is A at max
        self.assertEqual((code, r["status"]), (4, "none"))
        self.local({"roles": {"implement": {"effort": "bogus"}}})
        self.assertEqual(self.route("implement")[0], 2)

    def test_grade_letters(self):
        r = self.assertPick(self.route("design", "--grade", "S", host="codex"), "gpt-6.2-sol", "high", "subagent")
        self.assertEqual(r["grade"], "S")
        self.assertPick(self.route("design", "--grade", "S"), "claude-fable-5-2", "high")
        r = self.assertPick(self.route("review", "--author-vendor", "claude", "--grade", "C"), "gpt-6.1-terra", "low")
        self.assertEqual(r["grade"], "C")
        self.assertPick(self.route("implement", "--grade", "A"), "claude-fable-5-2", "low")
        self.assertEqual(self.go("--role", "design", "--grade", "D")[0], 2)
        self.local({"roles": {"read": {"grade": "B"}}})
        self.assertPick(self.route("read"), "claude-sonnet-5-5", "high")
        self.local({"roles": {"read": {"grade": "Z"}}})
        self.assertEqual(self.route("read")[0], 2)

    def test_cli_missing(self):
        env = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        r = self.assertPick(self.route("review", "--author-vendor", "claude", env=env), "gemini-4-hydrogen", "xhigh")
        self.assertIn("cli_missing:openai", r["reason"])
        self.assertPick(self.route("implement", host="codex", env=env), "gpt-6.1-sol", "medium", "subagent")  # no CLI needed

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
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")

    def test_malformed_exhausted_shapes_are_empty(self):
        for body in ('{"schema":1,"vendors":["bad"]}', '[1]', '{"vendors":{"openai":"bad"}}'):
            with self.subTest(body), open(self.exh_path, "w") as f:
                f.write(body)
            self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")

    def test_cap_to_run_checks_cli(self):
        env = {"TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        cap = ("--orchestrator-model", "claude-haiku-5-5")
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.2-sol", "medium", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.local({"roles": {"design": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.2-sol", "medium", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.assertNotIn("user_override", r["reason"])

    def test_pinned_remote_model_checks_cli(self):
        self.local({"roles": {"implement": {"model": "gpt-6.1-sol", "effort": "medium"}}})
        r = self.assertPick(self.route("implement"), "gpt-6.1-sol", "medium", "run")
        self.assertIn("user_override", r["reason"])
        env = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}  # no codex
        r = self.assertPick(self.route("implement", env=env), "claude-sonnet-5-5", "medium")  # the pinned effort still limits candidates
        self.assertIn("cli_missing:openai", r["reason"])
        self.assertNotIn("user_override", r["reason"])

    def test_candidate_skipped_by_default_but_selectable(self):
        self.models_edit(lambda d: d["models"]["claude-sonnet-5-5"].update(candidate=True))
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.local({"roles": {"implement": {"model": "claude-sonnet-5-5"}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")
        self.assertEqual(r["reason"], ["user_override", "bench:none"])

    # the tiers of pick(), with synthetic numbers (the B cost cap is 2)
    def test_tier0_best_index_within_cost_cap(self):
        self.bench({"claude-sonnet-5-5": [self.b("index", 40, "high"), self.b("cost_per_task", 1.0, "high"),
                                          self.b("index", 35, "medium"), self.b("cost_per_task", 0.5, "medium")],
                    "claude-haiku-5-5": [self.b("index", 99, "xhigh"), self.b("cost_per_task", 9.0, "xhigh"),
                                         self.b("index", 50, "max")]})  # over the cap / no cost: lower tiers
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
        self.assertIn("bench:local", r["reason"])

    def test_tier0_ties_go_to_lower_cost_then_order(self):
        self.bench({"claude-sonnet-5-5": [self.b("index", 40, "high"), self.b("cost_per_task", 1.5, "high")],
                    "claude-haiku-5-5": [self.b("index", 40, "max"), self.b("cost_per_task", 0.5, "max")]})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.bench({"claude-sonnet-5-5": [self.b("index", 40, "high"), self.b("cost_per_task", 0.5, "high")],
                    "claude-haiku-5-5": [self.b("index", 40, "max"), self.b("cost_per_task", 0.5, "max")]})
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")

    def test_tier0_beats_tier1(self):
        self.bench({"claude-sonnet-5-5": [self.b("index", 31, "medium"), self.b("cost_per_task", 1.0, "medium")],
                    "claude-haiku-5-5": [self.b("index", 45, "max")]})
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")

    def test_tier1_index_without_cost_beats_tier2(self):
        self.bench({"claude-sonnet-5-5": [self.b("index", 38, "high"), self.b("cost_per_task", 5.0, "high")],
                    "claude-haiku-5-5": [self.b("index", 32, "max")]})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.bench({"claude-sonnet-5-5": [self.b("index", 32, "high")], "claude-haiku-5-5": [self.b("index", 36, "xhigh")]})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "xhigh")

    def test_tier2_lowest_cost_when_all_over_cap(self):
        self.bench({"claude-sonnet-5-5": [self.b("index", 40, "high"), self.b("cost_per_task", 5.0, "high"),
                                          self.b("index", 33, "medium"), self.b("cost_per_task", 3.0, "medium")],
                    "claude-haiku-5-5": [self.b("index", 31, "max"), self.b("cost_per_task", 4.0, "max")]})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")
        self.assertIn("bench:local", r["reason"])

    def test_tier3_uses_defaults_then_order(self):
        # scores exist only for pairs of other grades or other efforts: the B pairs are unscored
        self.bench({"claude-sonnet-5-5": [self.b("index", 70, "max"), self.b("cost_per_task", 1.0, "low")]})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
        self.assertIn("bench:none", r["reason"])
        # defaults gone: first model in models.json order at its lowest effort of the grade
        self.models_edit(lambda d: d.pop("defaults"))
        self.assertPick(self.route("implement"), "claude-opus-5-5", "low")  # first model with a B pair
        self.assertPick(self.route("read"), "claude-sonnet-5-5", "low")
        # defaults list unusable (model disabled): the next entry, then the order rule
        self.models_edit(lambda d: d.update(defaults={"claude": {"B": ["claude-sonnet-5-5:high", "claude-haiku-5-5:max"]}}))
        self.local({"models": {"claude-sonnet-5-5": {"enabled": False}}})
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.local({"models": {m: {"enabled": False} for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")}})
        self.assertPick(self.route("implement"), "gpt-6.2-astra", "low")  # openai models.json order

    def test_bench_uses_only_the_actual_effort(self):
        # sonnet at low is C: a score there must not make it a B candidate
        self.bench({"claude-sonnet-5-5": [self.b("index", 99, "low"), self.b("cost_per_task", 0.01, "low")]})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
        self.assertIn("bench:none", r["reason"])

    def test_cost_cap_follows_grade(self):
        # read is grade C, cap 0.2
        self.bench({"claude-haiku-5-5": [self.b("index", 20, "high"), self.b("cost_per_task", 0.5, "high"),
                                         self.b("index", 10, "medium"), self.b("cost_per_task", 0.1, "medium")]})
        self.assertPick(self.route("read"), "claude-haiku-5-5", "medium")

    def test_broken_bench_file_is_empty(self):
        for text in ("{not json", "[1]", '{"models": 3}', '{"models": {"claude-haiku-5-5": 5}}'):
            with self.subTest(text):
                with open(os.path.join(self.cfg, "bench.local.json"), "w", encoding="utf-8") as f:
                    f.write(text)
                r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")
                self.assertIn("bench:none", r["reason"])

    def test_registry(self):
        with open(os.path.join(TAGTEAM, "models.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data["grading"]["floors"]), {"S", "A", "B"})
        self.assertEqual(set(data["grading"]["caps"]), set(route.GRADES))
        for mid, m in data["models"].items():
            self.assertIn(m["vendor"], data["vendors"], mid)
            self.assertNotIn("grade", m, mid)
            self.assertNotIn("efforts", m, mid)
            self.assertNotIn("effort_by_role", m, mid)
            self.assertTrue(m["grades"] and all(e in route.EFFORTS for e in m["grades"]), mid)
            self.assertTrue(all(g in route.GRADES for g in m["grades"].values()), mid)
            self.assertNotIn("candidate", m, mid)
            self.assertEqual(list(m["grades"]), route.model_efforts(m), mid)  # EFFORTS order
            if m["vendor"] == "google":
                self.assertTrue(m.get("label"), mid)
            self.assertNotIn("bench", m, mid)
        for v, cfg in data["vendors"].items():
            self.assertTrue({"cli", "host", "model_arg", "cooldown_minutes"} <= set(cfg), v)
        self.assertEqual(set(data["hosts"]), set(route.HOSTS))
        for host, order in data["hosts"].items():
            self.assertTrue(set(order) <= set(data["vendors"]), host)
        self.assertEqual({r: data["roles"][r].get("agent") for r in route.ROLES},
                         {"design": "designer", "implement": "implementer", "review": None, "read": "reader", "translate": None})
        for role in route.ROLES:
            self.assertIn(data["roles"][role]["grade"], route.GRADES)
            self.assertLessEqual(set(data["roles"][role]), {"grade", "read_only", "vendors", "agent"})
        # defaults point at registered pairs of exactly that grade
        for vendor, by_grade in data["defaults"].items():
            for grade, entries in by_grade.items():
                for entry in entries:
                    mid, _, effort = entry.rpartition(":")
                    self.assertEqual(data["models"][mid]["vendor"], vendor, entry)
                    self.assertEqual(data["models"][mid]["grades"][effort], grade, entry)
        for oid, o in data["orchestrators"].items():
            self.assertIn(o["vendor"], data["vendors"], oid)
            self.assertNotIn(oid, data["models"], oid)  # a separate block: no code that walks "models" sees it
            mid, _, effort = o["compare_as"].rpartition(":")
            self.assertIn(effort, data["models"][mid]["grades"], oid)
            self.assertEqual(data["models"][mid]["vendor"], o["vendor"], oid)
        for oid in ("gemini-pro-orchestrator-g1-pro", "gemini-pro-orchestrator-neon"):
            self.assertEqual(data["orchestrators"][oid]["compare_as"], "gemini-4-argon:high", oid)
        self.assertIn("gemini-4-carbon", data["models"])
        self.assertIn("gemini-4-hydrogen", data["models"])
        self.assertEqual(list(data["models"]).index("claude-fable-5-2"), 0)
        for mid in ("claude-fable-5-1", "claude-opus-5"):
            self.assertIn("superseded", data["unregistered"]["claude"][mid])

    def test_every_role_routes_on_both_hosts(self):
        for host in route.HOSTS:
            for role in route.ROLES:
                extra = ("--author-vendor", "claude") if role == "review" else ()
                code, r = self.route(role, *extra, host=host)
                self.assertEqual(code, 0, (host, role, r))


def kinds(r):
    return r["launch"]["kind"], r["launch"].get("agent"), r["launch"].get("escalate")


class LaunchTest(RouteBase):
    """Host x vendor x definition: how each pick is launched (design 2.3.3)."""

    def test_claude_desktop_session_adds_a_matching_subagent(self):
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high", "session")
        self.assertEqual(r["launch"]["also"], {"kind": "subagent", "host": "claude", "agent": "implementer"})
        r = self.assertPick(self.route("read"), "claude-haiku-5-5", "high", "session")
        self.assertEqual(r["launch"]["also"]["agent"], "reader")
        r = self.assertPick(self.route("design"), "claude-fable-5-2", "low", "session")  # designer pins opus: no match
        self.assertNotIn("also", r["launch"])
        self.edit_file(".claude/agents/implementer.md", "effort: high", "effort: low")
        self.assertNotIn("also", self.route("implement")[1]["launch"])
        os.remove(os.path.join(self.root, ".claude/agents/reader.md"))
        self.assertNotIn("also", self.route("read")[1]["launch"])

    def test_claude_desktop_other_vendor_is_run(self):
        self.exhausted(claude={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("implement"), "gpt-6.1-sol", "medium", "run")
        self.assertNotIn("escalate", r["launch"])

    def test_claude_cli_host(self):
        r = self.assertPick(self.route("implement", host="claude"), "claude-sonnet-5-5", "high", "subagent")
        self.assertEqual(r["launch"], {"kind": "subagent", "host": "claude", "agent": "implementer"})
        self.assertEqual(self.route("read", host="claude")[1]["launch"]["agent"], "reader")
        # the definition cannot set effort: a pick that differs from the pin goes through run.py
        self.assertPick(self.route("design", host="claude"), "claude-fable-5-2", "low", "run")
        self.edit_file(".claude/agents/implementer.md", "effort: high", "effort: low")
        self.assertPick(self.route("implement", host="claude"), "claude-sonnet-5-5", "high", "run")
        self.edit_file(".claude/agents/implementer.md", "effort: low", "effort: high")
        self.assertEqual(kinds(self.route("implement", host="claude")[1])[0], "subagent")
        # an alias cannot be compared with a model id: never a match
        self.edit_file(".claude/agents/implementer.md", "model: claude-sonnet-5-5", "model: sonnet")
        self.assertPick(self.route("implement", host="claude"), "claude-sonnet-5-5", "high", "run")
        os.remove(os.path.join(self.root, ".claude/agents/implementer.md"))
        self.assertPick(self.route("implement", host="claude"), "claude-sonnet-5-5", "high", "run")
        # other vendors, and roles without a definition: run, no escalate
        r = self.assertPick(self.route("review", "--author-vendor", "claude", host="claude"), "gpt-6.2-sol", "medium", "run")
        self.assertNotIn("escalate", r["launch"])
        self.assertEqual(kinds(self.route("translate", host="claude")[1]), ("run", None, None))

    def test_codex_host(self):
        # same vendor, pin equals the pick: the definition's name
        self.assertEqual(kinds(self.route("implement", host="codex")[1]), ("subagent", "implementer", None))
        # no matching definition (changed pin, missing file, role without one): agent default with arguments
        self.edit_file(".codex/agents/implementer.toml", 'model_reasoning_effort = "medium"', 'model_reasoning_effort = "low"')
        r = self.assertPick(self.route("implement", host="codex"), "gpt-6.1-sol", "medium", "subagent")
        self.assertEqual(r["launch"], {"kind": "subagent", "host": "codex", "agent": "default", "model": "gpt-6.1-sol",
                                       "reasoning_effort": "medium", "fork_turns": "none"})
        os.remove(os.path.join(self.root, ".codex/agents/designer.toml"))
        r = self.route("design", host="codex")[1]
        self.assertEqual((r["launch"]["agent"], r["launch"]["model"], r["launch"]["reasoning_effort"]),
                         ("default", "gpt-6.2-sol", "medium"))
        self.exhausted(google={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        self.local({"plans": {"google": "none"}})
        r = self.route("translate", host="codex")[1]  # no translate definition
        self.assertEqual((r["model"], r["launch"]["agent"]), ("gpt-6.1-sol", "default"))
        # a review is a read-only run, even with the host's own vendor (author claude -> openai reviewer)
        r = self.assertPick(self.route("review", "--author-vendor", "claude", host="codex"), "gpt-6.2-sol", "medium", "run")
        self.assertTrue(r["launch"]["escalate"])
        # every run on a Codex host asks for escalation, whatever the vendor
        self.local({"plans": {"openai": "none"}})
        r = self.assertPick(self.route("implement", host="codex"), "claude-sonnet-5-5", "high", "run")
        self.assertTrue(r["launch"]["escalate"])

    def test_a_definition_whose_instructions_quote_other_settings_does_not_match(self):
        text = ('name = "implementer"\ndeveloper_instructions = """\nmodel = "gpt-6.1-sol"\nmodel_reasoning_effort = "medium"\n"""\n'
                'model = "gpt-6.1-terra"\nmodel_reasoning_effort = "low"\n')
        with open(os.path.join(self.root, ".codex/agents/implementer.toml"), "w", encoding="utf-8") as f:
            f.write(text)
        r = self.assertPick(self.route("implement", host="codex"), "gpt-6.1-sol", "medium", "subagent")
        self.assertEqual(r["launch"]["agent"], "default")  # the definition really runs terra low

    def test_agy_host_runs_everything(self):
        r = self.assertPick(self.route("implement", host="agy"), "gemini-4-hydrogen", "medium", "run")
        self.assertNotIn("escalate", r["launch"])
        self.assertIn("host=agy", r["notice"])
        for role in ("design", "read", "translate"):
            launch = self.route(role, host="agy")[1]["launch"]
            self.assertEqual(launch["kind"], "run", role)
            self.assertNotIn("escalate", launch, role)
        self.exhausted(google={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        r = self.route("implement", host="agy")[1]
        self.assertEqual((r["vendor"], r["launch"]["kind"]), ("openai", "run"))

    def test_escalate_only_on_codex(self):
        for host in ("claude-desktop", "claude", "agy"):
            for role in route.ROLES:
                r = self.route(role, "--author-vendor", "openai", host=host)[1] if role == "review" else self.route(role, host=host)[1]
                if r["launch"]["kind"] == "run":
                    self.assertNotIn("escalate", r["launch"], (host, role))
        for role in route.ROLES:
            r = self.route(role, "--author-vendor", "claude", host="codex")[1] if role == "review" else self.route(role, host="codex")[1]
            if r["launch"]["kind"] == "run":
                self.assertTrue(r["launch"]["escalate"], role)

    def test_orchestrator_cap_applies_to_sessions_only(self):
        cap = ("--orchestrator-model", "claude-haiku-5-5", "--orchestrator-effort", "low")
        r = self.assertPick(self.route("implement", *cap), "claude-sonnet-5-5", "high", "run")
        self.assertIn("native_model_cap", r["reason"])
        for host in ("claude", "codex", "agy"):
            r = self.route("implement", *cap, host=host)[1]
            self.assertNotIn("native_model_cap", r["reason"], host)
            self.assertNotIn("Orchestrator cap", r["notice"], host)
        self.assertEqual(kinds(self.route("implement", *cap, host="claude")[1])[0], "subagent")

    def test_a_missing_cli_is_checked_only_for_run(self):
        env = {"TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}  # no claude CLI
        self.assertPick(self.route("implement", env=env), "claude-sonnet-5-5", "high", "session")
        self.assertPick(self.route("implement", host="claude", env=env), "claude-sonnet-5-5", "high", "subagent")
        self.edit_file(".claude/agents/implementer.md", "effort: high", "effort: low")
        r = self.assertPick(self.route("implement", host="claude", env=env), "gpt-6.1-sol", "medium", "run")
        self.assertIn("cli_missing:claude", r["reason"])

    def test_host_default_guess_and_notice(self):
        guesses = (({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, "claude-desktop"),
                   ({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude"),
                   ({"CLAUDECODE": "1"}, "claude"),
                   ({}, "codex"),
                   ({"ANTIGRAVITY_CONVERSATION_ID": "c-1"}, "agy"),
                   # an agy started from Claude inherits CLAUDECODE: the agy variable wins
                   ({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "ANTIGRAVITY_CONVERSATION_ID": "c-1"}, "agy"),
                   ({"ANTIGRAVITY_CONVERSATION_ID": ""}, "codex"))
        for env, want in guesses:
            with self.subTest(env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(route.default_host(), want)
        for host in route.HOSTS:
            self.assertEqual(self.route("read", host=host)[1]["host"], host)
        self.assertEqual(self.go("--role", "read", "--host", "nope")[0], 2)


class AgyHostTest(RouteBase):
    """agy as the orchestrator host (design m5 5.1, 5.2 and the review grade floor)."""
    ORCH = "gemini-pro-orchestrator-g1-pro"
    NEON = "gemini-pro-orchestrator-neon"

    def review(self, *extra, host="agy", env=None):
        return self.go("--role", "review", "--json", "--host", host, *extra, env=env)

    def test_review_never_goes_to_the_authors_vendor(self):
        r = self.assertPick(self.review("--author-vendor", "google", "--grade", "A"), "gpt-6.2-sol", "medium", "run")
        self.assertEqual(r["vendor"], "openai")
        self.assertIn("author_vendor_excluded:google", r["reason"])
        self.assertIn("--read-only", r["launch"]["command"])
        self.assertNotIn("escalate", r["launch"])

    def test_review_grade_floor_is_A_on_agy_only(self):
        for grade in ("C", "B"):
            with self.subTest(grade):
                code, r = self.review("--author-vendor", "google", "--grade", grade)
                self.assertEqual((code, r["grade"]), (0, "A"))
                self.assertIn("agy_review_floor_A", r["reason"])
                self.assertIn("review grade raised from %s to A" % grade, r["notice"])
                self.assertIn("host=agy", r["notice"])
        for grade in ("A", "S"):
            with self.subTest(grade):
                code, r = self.review("--author-vendor", "google", "--grade", grade)
                self.assertEqual((code, r["grade"]), (0, grade))
                self.assertNotIn("agy_review_floor_A", r["reason"])
                self.assertNotIn("raised", r["notice"])
        for host in ("claude-desktop", "claude", "codex"):
            with self.subTest(host):
                code, r = self.review("--author-vendor", "google", "--grade", "B", host=host)
                self.assertEqual((code, r["grade"]), (0, "B"))
                self.assertNotIn("agy_review_floor_A", r["reason"])
                self.assertNotIn("raised", r["notice"])

    def test_floor_also_applies_to_a_grade_taken_from_the_authors_model(self):
        code, r = self.review("--author-model", "gemini-3.8-flash", "--author-effort", "low")  # grade C
        self.assertEqual((code, r["grade"], r["vendor"]), (0, "A", "openai"))
        self.assertIn("agy_review_floor_A", r["reason"])

    def test_floor_raises_a_pinned_review_grade_and_a_review_task(self):
        self.local({"roles": {"review": {"grade": "C"}}, "tasks": {"r": {"about": "x", "base": "review"}}})
        code, r = self.go("--task", "r", "--json", "--host", "agy", "--author-vendor", "claude", "--grade", "C")
        self.assertEqual((code, r["grade"], r["task_applied"]), (0, "A", True))
        self.assertIn("agy_review_floor_A", r["reason"])

    def test_a_pinned_reviewer_below_the_floor_is_substituted(self):
        self.local({"roles": {"review": {"model": "gpt-6.1-terra", "effort": "low"}}})  # grade C
        code, r = self.review("--author-vendor", "google", "--grade", "C")
        self.assertEqual((code, r["grade"]), (0, "A"))
        self.assertNotEqual((r["model"], r["effort"]), ("gpt-6.1-terra", "low"))
        self.assertIn("override_below_floor", r["reason"])
        self.assertNotIn("user_override", r["reason"])
        self.assertIn("below the agy review floor A", r["notice"])
        # a pin that reaches A is kept; off agy the pin below A is kept too (it matches the author's grade C)
        self.local({"roles": {"review": {"model": "gpt-6.1-sol", "effort": "high"}}})  # grade A
        r = self.assertPick(self.review("--author-vendor", "google", "--grade", "C"), "gpt-6.1-sol", "high")
        self.assertIn("user_override", r["reason"])
        self.local({"roles": {"review": {"model": "gpt-6.1-terra", "effort": "low"}}})
        r = self.assertPick(self.review("--author-vendor", "google", "--grade", "C", host="claude"), "gpt-6.1-terra", "low")
        self.assertIn("user_override", r["reason"])
        # a pin without an effort takes its lowest effort at the raised grade, which is not below the floor
        self.local({"roles": {"review": {"model": "gpt-6.1-sol"}}})
        r = self.assertPick(self.review("--author-vendor", "google", "--grade", "C"), "gpt-6.1-sol", "high")
        self.assertEqual(r["grade"], "A")

    def test_a_task_vendor_preference_survives_the_floor(self):
        self.local({"tasks": {"r": {"about": "x", "base": "review", "vendor": "claude", "grade": "B"}}})
        code, r = self.go("--task", "r", "--json", "--host", "agy", "--author-vendor", "google", "--grade", "B")
        self.assertEqual((code, r["vendor"], r["grade"], r["task_applied"]), (0, "claude", "A", True))
        self.assertIn("agy_review_floor_A", r["reason"])
        self.assertFalse([x for x in r["reason"] if x.startswith("task_fallback")])
        # off agy the same task keeps its grade B
        r = self.go("--task", "r", "--json", "--host", "claude", "--author-vendor", "google", "--grade", "B")[1]
        self.assertEqual((r["vendor"], r["grade"], r["task_applied"]), ("claude", "B", True))
        # a task grade above the floor is still compared as written
        self.local({"tasks": {"r": {"about": "x", "base": "review", "vendor": "claude", "grade": "S"}}})
        r = self.go("--task", "r", "--json", "--host", "agy", "--author-vendor", "google", "--grade", "B")[1]
        self.assertEqual((r["grade"], r["task_applied"]), ("A", False))  # the route grade A is not the task's S

    def test_a_task_model_below_the_floor_falls_back_with_the_floor_as_cause(self):
        self.local({"tasks": {"r": {"about": "x", "base": "review", "model": "gpt-6.1-terra", "effort": "low"}}})
        code, r = self.go("--task", "r", "--json", "--host", "agy", "--author-vendor", "google", "--grade", "B")
        self.assertEqual((code, r["task_applied"], r["grade"]), (0, False, "A"))
        self.assertIn("task_fallback:agy_review_floor_A", r["reason"])

    def test_incomplete_is_still_exit_3_under_the_floor(self):
        self.local({"plans": {"openai": "none", "claude": "none"}})
        code, r = self.review("--author-vendor", "google", "--grade", "B")
        self.assertEqual((code, r["status"]), (3, "incomplete"))

    def test_other_roles_do_not_get_a_floor(self):
        r = self.assertPick(self.go("--role", "read", "--json", "--host", "agy"), "gemini-3.8-flash", "low", "run")
        self.assertEqual(r["grade"], "C")
        self.assertNotIn("agy_review_floor_A", r["reason"])

    def test_orchestrator_is_compared_as_argon_high(self):
        code, r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.ORCH)
        # the pick (Hydrogen xhigh) is listed before Argon in google's models: stronger than the assumed pair
        self.assertEqual((code, r["model"], r["effort"], r["ask_user"]), (0, "gemini-4-hydrogen", "xhigh", True))
        self.assertIn("listed before", r["ask_reason"])
        self.assertIn("compared as gemini-4-argon high", r["notice"])
        self.assertIn("Ask the user", r["notice"])
        self.assertNotIn("Pass --orchestrator-model", r["notice"])
        self.assertEqual(r["launch"]["kind"], "run")
        self.assertNotIn("escalate", r["launch"])

    def test_the_assumption_comes_from_compare_as(self):
        self.models_edit(lambda d: d["orchestrators"][self.ORCH].update(compare_as="gemini-4-hydrogen:xhigh"))
        code, r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.ORCH)
        self.assertEqual((code, r["ask_user"]), (0, False))
        self.assertIn("compared as gemini-4-hydrogen xhigh", r["notice"])

    def test_the_note_is_added_to_every_result_and_only_for_an_orchestrator(self):
        for role in ("implement", "read", "translate"):
            with self.subTest(role):
                r = self.go("--role", role, "--json", "--host", "agy", "--orchestrator-model", self.ORCH)[1]
                self.assertIn("compared as gemini-4-argon high", r["notice"])
                self.assertIs(r["ask_user"], False)
        r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", "gemini-4-argon")[1]
        self.assertNotIn("compared as", r["notice"])  # a normal model needs no note
        self.local({"plans": {"google": "none", "claude": "none", "openai": "none"}})
        code, r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.ORCH)
        self.assertEqual((code, r["status"]), (4, "none"))
        self.assertIn("compared as gemini-4-argon high", r["notice"])

    def test_an_orchestrator_takes_no_effort(self):
        for effort in ("high", "low"):
            with self.subTest(effort):
                code, out = self.go("--role", "design", "--host", "agy", "--orchestrator-model", self.ORCH,
                                    "--orchestrator-effort", effort)
                self.assertEqual((code, out), (2, ""))

    def test_a_bad_compare_as_is_a_config_error(self):
        for compare_as in ("nope:high", "gemini-4-argon:xhigh", "gemini-4-argon", None):
            with self.subTest(compare_as):
                self.models_edit(lambda d: d["orchestrators"][self.ORCH].update(compare_as=compare_as))
                self.assertEqual(self.go("--role", "design", "--host", "agy", "--orchestrator-model", self.ORCH)[0], 2)

    def test_orchestrator_on_a_claude_desktop_host_is_compared_too(self):
        # compared as Argon high: Fable low is another vendor and not above the effort, so it stays a native session
        r = self.assertPick(self.go("--role", "design", "--json", "--orchestrator-model", self.ORCH),
                            "claude-fable-5-2", "low", "session")
        self.assertNotIn("native_model_cap", r["reason"])
        self.assertIn("compared as gemini-4-argon high", r["notice"])

    def test_claude_effort_is_used_on_claude_hosts_only(self):
        env = dict(CLIS, CLAUDE_EFFORT="high")
        cap = ("--orchestrator-model", "claude-opus-5-5")
        # no --orchestrator-effort: the variable stands in on a Claude host (Fable is listed before Opus: ask)
        r = self.go("--role", "design", "--json", "--host", "claude", *cap, env=env)[1]
        self.assertIs(r["ask_user"], True)
        low = ("--role", "implement", "--json", "--orchestrator-model", "claude-sonnet-5-5")  # claude-desktop
        r = self.go(*low, env=dict(CLIS, CLAUDE_EFFORT="low"))[1]
        self.assertEqual(r["launch"]["kind"], "run")  # Sonnet high is above the cap low
        self.assertIn("native_model_cap", r["reason"])
        self.assertEqual(self.go(*low)[1]["launch"]["kind"], "session")  # without the variable: no cap known
        # ... and is ignored elsewhere: an agy or Codex started from Claude inherits it
        for host in ("agy", "codex"):
            with self.subTest(host):
                r = self.go("--role", "design", "--json", "--host", host, *cap, env=env)[1]
                self.assertIsNone(r["ask_user"])  # no ground to compare: null with the reason, never a silent False
                self.assertEqual(r["ask_reason"], "orchestrator model or effort unknown")
                self.assertIn("Pass --orchestrator-model/--orchestrator-effort", r["notice"])
        # an explicit effort always counts
        r = self.go("--role", "design", "--json", "--host", "agy", *cap, "--orchestrator-effort", "high", env=env)[1]
        self.assertIsNotNone(r["ask_user"])

    def test_agy_host_without_any_orchestrator_asks_for_it(self):
        r = self.go("--role", "design", "--json", "--host", "agy")[1]
        self.assertIsNone(r["ask_user"])

    def test_neon_is_an_orchestrator_like_the_other_built_in_one(self):
        # agy 1.5.12 lists two built-in Orchestrator models; an interactive session offers Neon. Same handling as g1-pro.
        for effort in (None, "high"):
            extra = ("--orchestrator-effort", effort) if effort else ()
            code, out = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.NEON, *extra)
            if effort:
                self.assertEqual((code, out), (2, ""))  # takes no effort, like g1-pro
            else:
                self.assertEqual(code, 0)
                self.assertIn("compared as gemini-4-argon high", out["notice"])  # compare_as is used
                self.assertEqual(out["ask_user"], self.go("--role", "design", "--json", "--host", "agy",
                                                         "--orchestrator-model", self.ORCH)[1]["ask_user"])
        self.models_edit(lambda d: d["orchestrators"][self.NEON].update(compare_as="gemini-4-hydrogen:xhigh"))
        r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.NEON)[1]
        self.assertIn("compared as gemini-4-hydrogen xhigh", r["notice"])  # edited per id: the two entries are independent
        r = self.go("--role", "design", "--json", "--host", "agy", "--orchestrator-model", self.ORCH)[1]
        self.assertIn("compared as gemini-4-argon high", r["notice"])

    def worker_launch(self, model):
        prompt = os.path.join(self.tmp, "prompt.txt")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("x")
        env = dict(os.environ, TAGTEAM_CONFIG_DIR=self.cfg, TAGTEAM_STATE_DIR=self.state)
        env.pop("HARNESS_DELEGATE_RUN", None)
        return subprocess.run([sys.executable, os.path.join(TAGTEAM, "run.py"), "--vendor", "google", "--model", model,
                               "--effort", "high", "--prompt-file", prompt], capture_output=True, text=True, env=env, timeout=60)

    def test_run_py_rejects_the_orchestrator_ids_as_a_worker_model(self):
        # the ids live in "orchestrators", not "models": a worker launch with one must fail before any CLI starts
        for model in (self.ORCH, self.NEON):
            with self.subTest(model):
                proc = self.worker_launch(model)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not registered", proc.stdout + proc.stderr)


class AskUserTest(RouteBase):
    def ask(self, *extra, role="design"):
        r = self.route(role, *extra)[1]
        return r["ask_user"], r["ask_reason"], r

    def test_unknown_orchestrator_is_null(self):
        ask, reason, r = self.ask()
        self.assertIsNone(ask)
        self.assertIn("--orchestrator-model/--orchestrator-effort", r["notice"])
        self.assertIsNone(self.ask("--orchestrator-model", "claude-opus-5-5")[0])  # effort missing
        self.assertIsNone(self.ask("--orchestrator-model", "mystery", "--orchestrator-effort", "high")[0])

    def test_stronger_model_or_grade_asks(self):
        # the pick (Fable) is listed before the orchestrator's Sonnet
        ask, reason, r = self.ask("--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "high")
        self.assertTrue(ask)
        self.assertIn("Ask the user", r["notice"])
        # another vendor, higher grade (Fable low is A, Terra low is C)
        self.assertTrue(self.ask("--orchestrator-model", "gpt-6.1-terra", "--orchestrator-effort", "low")[0])

    def test_not_stronger_does_not_ask(self):
        ask, reason, r = self.ask("--orchestrator-model", "claude-opus-5-5", "--orchestrator-effort", "high")
        self.assertEqual((r["model"], ask), ("claude-opus-5-5", False))  # the orchestrator's own pair
        self.assertNotIn("Ask the user", r["notice"])
        self.assertFalse(self.ask("--orchestrator-model", "claude-fable-5-2", "--orchestrator-effort", "xhigh")[0])  # S above A

    def test_cost_decides_at_equal_grade(self):
        cap = ("--orchestrator-model", "gpt-6.1-sol", "--orchestrator-effort", "high")  # A, like Fable low
        self.assertFalse(self.ask(*cap)[0])
        self.bench({"claude-fable-5-2": [self.b("cost_per_task", 3.0, "low")],
                    "gpt-6.1-sol": [self.b("cost_per_task", 2.0, "high")]})
        self.assertTrue(self.ask(*cap)[0])
        self.bench({"claude-fable-5-2": [self.b("cost_per_task", 1.0, "low")],
                    "gpt-6.1-sol": [self.b("cost_per_task", 2.0, "high")]})
        self.assertFalse(self.ask(*cap)[0])
        self.bench({"claude-fable-5-2": [self.b("cost_per_task", 3.0, "low")]})  # only one side known
        self.assertFalse(self.ask(*cap)[0])

    def test_other_roles_never_ask_and_every_host_answers(self):
        for role in ("implement", "read", "translate"):
            ask, reason, _ = self.ask("--orchestrator-model", "claude-haiku-5-5", "--orchestrator-effort", "low", role=role)
            self.assertIs(ask, False, role)
        for host in route.HOSTS:
            r = self.route("design", "--orchestrator-model", "claude-haiku-5-5", "--orchestrator-effort", "low", host=host)[1]
            self.assertIn(r["ask_user"], (True, False), host)
        self.assertIs(self.route("design", "--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "high",
                                 host="codex")[1]["ask_user"], True)


class DefinitionTest(unittest.TestCase):
    def test_read_definition_formats(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".claude", "agents"))
            os.makedirs(os.path.join(root, ".codex", "agents"))
            md = os.path.join(root, ".claude", "agents", "a.md")
            with open(md, "w", encoding="utf-8", newline="") as f:
                f.write('---\r\nname: a\r\ndescription: "model: nope"\r\nmodel: "claude-opus-5-5"\r\neffort: high\r\n---\r\nmodel: body\r\n')
            self.assertEqual(route.read_definition(root, "claude", "a"), ("claude-opus-5-5", "high"))
            with open(md, "w", encoding="utf-8") as f:
                f.write("---\nmodel: sonnet\n---\n")  # no effort: unusable
            self.assertIsNone(route.read_definition(root, "claude", "a"))
            with open(md, "w", encoding="utf-8") as f:
                f.write("no front matter\nmodel: x\neffort: y\n")
            self.assertIsNone(route.read_definition(root, "claude", "a"))
            toml = os.path.join(root, ".codex", "agents", "a.toml")
            with open(toml, "w", encoding="utf-8") as f:
                f.write('name = "a"\nmodel = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\ndeveloper_instructions = """\nmodel = "x"\n"""\n')
            self.assertEqual(route.read_definition(root, "codex", "a"), ("gpt-6.1-sol", "xhigh"))
            self.assertIsNone(route.read_definition(root, "codex", "missing"))
            self.assertIsNone(route.read_definition(root, "google", "a"))

    EXAMPLE_TOML = (
        'name = "a"\n'
        'developer_instructions = """\n'
        'Example settings, not real ones:\n'
        'model = "claude-haiku-5-5"\n'
        'model_reasoning_effort = "low"\n'
        '"""\n'
        "note = \'\'\'\nmodel = 'x'\n\'\'\'\n"
        'model = "gpt-6.1-sol"  # the real settings come after the strings\n'
        'model_reasoning_effort = "xhigh"\n'
        '[profile]\n'
        'model = "from-a-table"\n')

    def read_toml(self, text, no_tomllib):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".codex", "agents"))
            with open(os.path.join(root, ".codex", "agents", "a.toml"), "w", encoding="utf-8") as f:
                f.write(text)
            with mock.patch.object(route, "tomllib", None) if no_tomllib else contextlib.nullcontext():
                return route.read_definition(root, "codex", "a")

    def test_toml_definition_reads_only_top_level_settings(self):
        modes = (True, False) if route.tomllib is not None else (True,)  # the small parser always; tomllib where present
        for no_tomllib in modes:
            self.assertEqual(self.read_toml(self.EXAMPLE_TOML, no_tomllib), ("gpt-6.1-sol", "xhigh"), no_tomllib)
            # example lines inside a multi-line string are never the setting
            only_examples = self.EXAMPLE_TOML.split("model = \"gpt-6.1-sol\"")[0]
            self.assertIsNone(self.read_toml(only_examples, no_tomllib), no_tomllib)
            # settings after a table header belong to that table
            self.assertIsNone(self.read_toml('name = "a"\n[profile]\nmodel = "m"\nmodel_reasoning_effort = "low"\n', no_tomllib))
            self.assertEqual(self.read_toml("model = 'gpt-6.1-sol'\nmodel_reasoning_effort = 'low'\n", no_tomllib),
                             ("gpt-6.1-sol", "low"), no_tomllib)

    def test_shipped_definitions_pin_full_model_ids(self):
        with open(os.path.join(TAGTEAM, "models.json"), encoding="utf-8") as f:
            models = json.load(f)
        with tempfile.TemporaryDirectory() as root:
            shutil.copytree(os.path.join(HARNESS, "claude", "agents"), os.path.join(root, ".claude", "agents"))
            shutil.copytree(os.path.join(HARNESS, "codex", "agents"), os.path.join(root, ".codex", "agents"))
            for family in ("claude", "codex"):
                for name in ("designer", "implementer", "reader"):
                    model, effort = route.read_definition(root, family, name)
                    self.assertIn(model, models["models"], (family, name))  # a model id, not an alias
                    self.assertIn(effort, models["models"][model]["grades"], (family, name))
            self.assertEqual(route.read_definition(root, "codex", "designer"), ("gpt-6.2-sol", "medium"))


class TaskTest(RouteBase):
    def tasks(self, **tasks):
        local = {}
        try:
            with open(os.path.join(self.cfg, "models.local.json"), encoding="utf-8") as f:
                local = json.load(f)
        except OSError:
            pass
        local["tasks"] = {k: dict({"about": "about " + k, "base": "implement"}, **v) for k, v in tasks.items()}
        self.local(local)

    def task(self, name, *extra, **kw):
        return self.go("--task", name, "--json", *extra, **kw)

    def test_model_preference(self):
        self.tasks(vision={"model": "gpt-6.1-terra"})
        r = self.assertPick(self.task("vision"), "gpt-6.1-terra", "medium", "run")  # the grade table picks the effort
        self.assertEqual((r["task"], r["task_applied"], r["role"]), ("vision", True, "implement"))
        self.assertIn("user_override", r["reason"])
        self.assertEqual(self.go("--task", "vision", "--role", "implement", "--json")[1]["model"], "gpt-6.1-terra")
        self.tasks(vision={"model": "claude-opus-5-5", "effort": "xhigh"})
        self.assertPick(self.task("vision"), "claude-opus-5-5", "xhigh")
        self.tasks(vision={"model": "gpt-6.1-sol", "effort": "max"})  # clamped to the model's highest effort
        r = self.assertPick(self.task("vision"), "gpt-6.1-sol", "xhigh")
        self.assertIn("effort_clamped", r["reason"])
        self.assertTrue(r["task_applied"])

    def test_effort_vendor_and_grade_preferences(self):
        self.tasks(deep={"effort": "max"})
        r = self.assertPick(self.task("deep"), "claude-haiku-5-5", "max")
        self.assertTrue(r["task_applied"])
        self.tasks(html={"vendor": "google"})
        r = self.assertPick(self.task("html"), "gemini-4-hydrogen", "medium", "run")  # base grade B inside the vendor
        self.assertEqual((r["vendor"], r["task_applied"]), ("google", True))
        self.tasks(sharp={"grade": "A"})
        r = self.assertPick(self.task("sharp"), "claude-fable-5-2", "low")
        self.assertEqual((r["grade"], r["task_applied"]), ("A", True))
        self.tasks(sharp={"vendor": "openai", "grade": "S"})
        self.assertPick(self.task("sharp"), "gpt-6.2-sol", "high")

    def test_grade_only_task_ignores_the_role_effort_pin(self):
        self.local({"roles": {"implement": {"effort": "max"}},
                    "tasks": {"sharp": {"about": "x", "base": "implement", "grade": "A"}}})
        r = self.assertPick(self.task("sharp"), "claude-fable-5-2", "low")  # A at max does not exist; the table decides
        self.assertTrue(r["task_applied"])
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")  # without the task the pin stays
        # a pinned model and effort: the grade picks the effort of that model
        self.local({"roles": {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}},
                    "tasks": {"sharp": {"about": "x", "base": "implement", "grade": "A"}}})
        r = self.assertPick(self.task("sharp"), "claude-opus-5-5", "medium")
        self.assertEqual((r["grade"], r["task_applied"]), ("A", True))
        # an explicit task effort still wins over the role pin
        self.local({"roles": {"implement": {"effort": "max"}},
                    "tasks": {"sharp": {"about": "x", "base": "implement", "grade": "A", "effort": "high"}}})
        self.assertPick(self.task("sharp"), "claude-opus-5-5", "high")

    def test_task_layer_goes_over_role_pins(self):
        self.local({"roles": {"implement": {"model": "claude-opus-5-5", "effort": "xhigh"}},
                    "tasks": {"html": {"about": "x", "base": "implement", "vendor": "google"}}})
        self.assertPick(self.task("html"), "gemini-4-hydrogen", "medium")
        self.assertPick(self.route("implement"), "claude-opus-5-5", "xhigh")  # no task: unchanged

    def test_a_task_alone_equals_its_base_role(self):
        self.tasks(plain={})
        for host in route.HOSTS:
            base = self.route("implement", host=host)[1]
            task = self.task("plain", host=host)[1]
            self.assertTrue(task["task_applied"])
            for key in ("model", "effort", "vendor", "launch", "reason"):
                self.assertEqual(task[key], base[key], (host, key))

    def fallback(self, expect_cause, **task):
        self.tasks(t=task)
        base = self.route("implement")[1]
        code, r = self.task("t")
        self.assertEqual(code, 0, r)
        self.assertFalse(r["task_applied"])
        self.assertEqual(r["task"], "t")
        self.assertIn("task_fallback:" + expect_cause, r["reason"])
        self.assertIn("task t preference unusable (%s), routed as implement" % expect_cause, r["notice"])
        for key in ("model", "effort", "vendor", "launch"):
            self.assertEqual(r[key], base[key], key)
        return r

    def test_fallback_exhausted(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        self.fallback("exhausted:openai", model="gpt-6.1-terra")
        self.fallback("exhausted:openai", vendor="openai")

    def test_fallback_plan_none(self):
        self.local({"plans": {"google": "none"}})
        self.fallback("plan_none:google", vendor="google")
        self.fallback("plan_none:google", model="gemini-4-argon")

    def test_fallback_model_not_available_or_disabled(self):
        self.local({"available": {"openai": {"ids": ["gpt-6.1-sol"]}}})
        self.fallback("unavailable:gpt-6.1-terra", model="gpt-6.1-terra")
        self.local({"models": {"gpt-6.1-terra": {"enabled": False}}})
        self.fallback("unavailable:gpt-6.1-terra", model="gpt-6.1-terra")

    def test_fallback_vendor_without_a_usable_base_grade_model(self):
        self.local({"models": {m: {"enabled": False} for m in ("gemini-4-hydrogen", "gemini-4-argon", "gemini-3.8-flash")}})
        self.fallback("unavailable:google", vendor="google")

    def test_fallback_cli_missing(self):
        env = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        self.tasks(t={"model": "gpt-6.1-terra"})
        base = self.route("implement", env=env)[1]
        code, r = self.task("t", env=env)
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:cli_missing:openai", r["reason"])
        self.assertEqual(r["model"], base["model"])
        self.tasks(t={"vendor": "openai"})
        self.assertIn("task_fallback:cli_missing:openai", self.task("t", env=env)[1]["reason"])
        # on a Codex host the same vendor needs no CLI (a subagent)
        r = self.task("t", env=env, host="codex")[1]
        self.assertTrue(r["task_applied"])

    def test_fallback_review_author_vendor(self):
        self.tasks(r={"base": "review", "vendor": "claude"})
        code, r = self.task("r", "--author-vendor", "claude", "--grade", "A")
        self.assertEqual(code, 0)
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:author_vendor", r["reason"])
        self.assertEqual(r["vendor"], "openai")
        code, r = self.task("r", "--author-vendor", "openai", "--grade", "A")
        self.assertEqual((r["vendor"], r["task_applied"]), ("claude", True))
        self.tasks(r={"base": "review", "model": "claude-sonnet-5-5"})
        r = self.task("r", "--author-vendor", "claude", "--grade", "A")[1]
        self.assertIn("task_fallback:author_vendor", r["reason"])

    def test_review_task_still_needs_the_author_grade(self):
        self.tasks(r={"base": "review", "vendor": "openai"})
        self.assertEqual(self.task("r", "--author-vendor", "claude")[0], 2)

    def test_task_ask_user_follows_the_base_role(self):
        self.tasks(d={"base": "design", "model": "claude-fable-5-2", "effort": "high"})
        r = self.task("d", "--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "high")[1]
        self.assertEqual((r["model"], r["ask_user"]), ("claude-fable-5-2", True))
        self.tasks(i={})
        self.assertIs(self.task("i", "--orchestrator-model", "claude-haiku-5-5", "--orchestrator-effort", "low")[1]["ask_user"], False)

    def test_usage_errors(self):
        self.tasks(a={}, b={"base": "design"})
        code, _ = self.task("nope")
        self.assertEqual(code, 2)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state}), \
                contextlib.redirect_stderr(err):
            route.main(["--task", "nope"])
        self.assertIn("a, b", err.getvalue())
        self.assertEqual(self.go("--task", "a", "--role", "design", "--json")[0], 2)  # base is implement
        self.assertEqual(self.go("--task", "a", "--role", "implement", "--json")[0], 0)
        os.remove(os.path.join(self.cfg, "models.local.json"))
        self.assertEqual(self.task("a")[0], 2)  # no tasks at all
        bad = [{"about": "", "base": "implement"}, {"about": "x"}, {"about": "x", "base": "bogus"},
               {"about": "x", "base": "implement", "model": "nope"}, {"about": "x", "base": "implement", "vendor": "nope"},
               {"about": "x", "base": "implement", "model": "gpt-6.1-sol", "vendor": "claude"},
               {"about": "x", "base": "implement", "effort": "ultra"}, {"about": "x", "base": "implement", "grade": "Z"}, 5]
        for entry in bad:
            with self.subTest(entry):
                self.local({"tasks": {"bad": entry}})
                self.assertEqual(self.task("bad")[0], 2)
        self.local({"tasks": {"ok": {"about": "x", "base": "implement", "model": "gpt-6.1-sol", "vendor": "openai"}}})
        self.assertEqual(self.task("ok")[0], 0)

    def test_without_task_nothing_changes(self):
        self.tasks(html={"vendor": "google"})
        r = self.route("implement")[1]
        self.assertNotIn("task", r)
        self.assertNotIn("task_applied", r)
        self.assertEqual(r["model"], "claude-sonnet-5-5")


if __name__ == "__main__":
    unittest.main()
