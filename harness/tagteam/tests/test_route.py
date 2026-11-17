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

    def bench(self, models):
        with open(os.path.join(self.cfg, "bench.local.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "models": models}, f)

    @staticmethod
    def b(metric, value, effort):
        return {"source": "artificial_analysis", "metric": metric, "value": value, "effort": effort}

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

    def models_edit(self, fn):
        path = os.path.join(self.cfg, "models.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        fn(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    # 1
    def test_defaults_host_claude(self):
        r = self.assertPick(self.route("design"), "claude-fable-5-2", "low", "native")
        self.assertIn("Check orchestrator cap", r["notice"])
        self.assertEqual(r["grade"], "A")
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high", "native")
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "xhigh", "run")
        self.assertIn("--read-only", r["launch"]["command"])
        self.assertIn("author_vendor_excluded:claude", r["reason"])
        self.assertIn("differs from author vendor claude", r["notice"])
        r = self.assertPick(self.route("read"), "claude-haiku-5-5", "high", "native")
        self.assertEqual((r["grade"], r["reason"][-1]), ("C", "bench:none"))
        r = self.assertPick(self.route("translate"), "gemini-4-hydrogen", "medium", "run")
        self.assertNotIn("--read-only", r["launch"]["command"])

    # 2
    def test_defaults_host_codex(self):
        self.assertPick(self.route("implement", host="codex"), "gpt-6.1-sol", "medium", "native")
        self.assertPick(self.route("design", host="codex"), "gpt-6.1-sol", "xhigh", "native")
        r = self.assertPick(self.route("review", "--author-vendor", "openai", host="codex"),
                            "claude-fable-5-2", "low", "run")
        self.assertEqual(r["vendor"], "claude")
        self.assertPick(self.route("read", host="codex"), "gpt-6.1-terra", "low", "native")

    # 3
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

    # 4
    def test_override_same_vendor_as_author(self):
        self.local({"roles": {"review": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "xhigh")
        self.assertIn("override_same_vendor_as_author", r["reason"])

    def test_review_override_grade_mismatch_is_flagged(self):
        self.local({"roles": {"review": {"model": "gpt-6.1-terra", "effort": "low"}}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude", "--grade", "A"), "gpt-6.1-terra", "low")
        self.assertIn("override_grade_mismatch", r["reason"])
        self.assertIn("grade C", r["notice"])

    # 5
    def test_review_needs_author_vendor(self):
        self.assertEqual(self.go("--role", "review", "--host", "claude")[0], 2)
        self.assertEqual(self.go("--role", "review", "--author-vendor", "nope", "--host", "claude")[0], 2)
        self.assertEqual(self.go("--host", "claude")[0], 2)

    # 6
    def test_plans(self):
        self.local({"plans": {"claude": "none"}})
        self.assertPick(self.route("implement"), "gpt-6.1-sol", "medium", "run")
        self.local({"plans": {"openai": "free"}})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-hydrogen", "xhigh", "run")
        self.assertIn("plan_free_last:openai", r["reason"])
        self.local({"plans": {"google": "free"}})
        self.assertPick(self.route("translate"), "gemini-4-hydrogen", "medium", "run")

    # 6b
    def test_over_cap_by_model_order_and_effort(self):
        # every A pair is over the cap of Sonnet high: still picked, launched through run.py
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-sonnet-5-5",
                                       "--orchestrator-effort", "high"), "claude-fable-5-2", "low", "run")
        self.assertIn("native_model_cap", r["reason"])
        self.assertEqual(r["launch"]["command"][r["launch"]["command"].index("--vendor") + 1], "claude")
        # Opus high: Fable (earlier in models.json) is over the cap, so the Opus pair is taken natively
        r = self.assertPick(self.route("design", "--orchestrator-model", "claude-opus-5-5",
                                       "--orchestrator-effort", "high"), "claude-opus-5-5", "high", "native")
        self.assertNotIn("native_model_cap", r["reason"])
        self.assertNotIn("Check orchestrator cap", r["notice"])
        # a later model is not over the cap; Fable keeps its place
        self.assertPick(self.route("design", "--orchestrator-model", "claude-fable-5-2",
                                   "--orchestrator-effort", "xhigh"), "claude-fable-5-2", "low", "native")
        # effort above the orchestrator's is over the cap even for the same model
        r = self.assertPick(self.route("implement", "--orchestrator-model", "claude-sonnet-5-5",
                                       "--orchestrator-effort", "low"), "claude-sonnet-5-5", "high", "run")
        self.assertIn("native_model_cap", r["reason"])
        # within the cap beats the default order: Opus low fits an Opus low orchestrator
        self.assertPick(self.route("implement", "--orchestrator-model", "claude-opus-5-5",
                                   "--orchestrator-effort", "low"), "claude-opus-5-5", "low", "native")

    # 7
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

    # 8
    def test_exhausted_substitution(self):
        self.exhausted(openai={"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gemini-4-hydrogen", "xhigh")
        self.assertIn("exhausted_skip:openai", r["reason"])
        self.assertTrue(any(c.startswith("substituted:openai") for c in r["reason"]))
        self.assertIn("openai exhausted until", r["notice"])
        self.exhausted(openai={"since": PAST, "until": PAST, "evidence": "x", "source": "manual"})
        r = self.assertPick(self.route("review", "--author-vendor", "claude"), "gpt-6.1-sol", "xhigh")
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

    # 10b
    def test_grade_letters(self):
        r = self.assertPick(self.route("design", "--grade", "S", host="codex"), "gpt-6-astra", "max", "native")
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

    # 11
    def test_cli_missing(self):
        env = {"TAGTEAM_CLI_CLAUDE": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        r = self.assertPick(self.route("review", "--author-vendor", "claude", env=env), "gemini-4-hydrogen", "xhigh")
        self.assertIn("cli_missing:openai", r["reason"])
        self.assertPick(self.route("implement", host="codex", env=env), "gpt-6.1-sol", "medium", "native")

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
        self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")

    def test_malformed_exhausted_shapes_are_empty(self):
        for body in ('{"schema":1,"vendors":["bad"]}', '[1]', '{"vendors":{"openai":"bad"}}'):
            with open(self.exh_path, "w") as f:
                f.write(body)
            self.assertPick(self.route("implement"), "claude-sonnet-5-5", "high")

    def test_cap_to_run_checks_cli(self):
        env = {"TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}
        cap = ("--orchestrator-model", "claude-haiku-5-5")
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.1-sol", "xhigh", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.local({"roles": {"design": {"model": "claude-opus-5-5"}}})
        r = self.assertPick(self.route("design", *cap, env=env), "gpt-6.1-sol", "xhigh", "run")
        self.assertIn("cli_missing:claude", r["reason"])
        self.assertNotIn("user_override", r["reason"])

    def test_candidate_skipped_by_default_but_selectable(self):
        self.models_edit(lambda d: d["models"]["claude-sonnet-5-5"].update(candidate=True))
        self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.local({"roles": {"implement": {"model": "claude-sonnet-5-5"}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5", "medium")
        self.assertEqual(r["reason"], ["user_override", "bench:none"])

    # 13: the four tiers (synthetic numbers; the B cost cap is 2)
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
        self.assertPick(self.route("implement"), "gpt-6-astra", "low")  # openai models.json order

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
        for host, order in data["hosts"].items():
            self.assertTrue(set(order) <= set(data["vendors"]), host)
        for role in route.ROLES:
            self.assertIn(data["roles"][role]["grade"], route.GRADES)
            self.assertLessEqual(set(data["roles"][role]), {"grade", "read_only", "vendors"})
        # defaults point at registered pairs of exactly that grade
        for vendor, by_grade in data["defaults"].items():
            for grade, entries in by_grade.items():
                for entry in entries:
                    mid, _, effort = entry.rpartition(":")
                    self.assertEqual(data["models"][mid]["vendor"], vendor, entry)
                    self.assertEqual(data["models"][mid]["grades"][effort], grade, entry)
        self.assertIn("gemini-4-carbon", data["models"])
        self.assertIn("gemini-4-hydrogen", data["models"])
        self.assertEqual(list(data["models"]).index("claude-fable-5-2"), 0)
        for mid in ("claude-fable-5-1", "claude-opus-5"):
            self.assertIn("superseded", data["unregistered"]["claude"][mid])

    def test_every_role_routes_on_both_hosts(self):
        for host in ("claude", "codex"):
            for role in route.ROLES:
                extra = ("--author-vendor", "claude") if role == "review" else ()
                code, r = self.route(role, *extra, host=host)
                self.assertEqual(code, 0, (host, role, r))


if __name__ == "__main__":
    unittest.main()
