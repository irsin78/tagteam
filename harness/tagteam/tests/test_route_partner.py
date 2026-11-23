"""Maker vs pool: agy's Claude models are fallback-only workers; review independence is by maker (unit R1)."""
import itertools
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import route  # noqa: E402
from test_route import CLIS, FUTURE, PAST, RouteBase  # noqa: E402

E = {"since": PAST, "until": FUTURE, "evidence": "x", "source": "manual"}
POOLS = ["claude", "openai", "google", "google-partner"]
PARTNERS = ("claude-opus-5-5@agy", "claude-sonnet-5-5@agy")


def shipped():
    with open(os.path.join(os.path.dirname(HERE), "models.json"), encoding="utf-8") as f:
        return json.load(f)


class PartnerBase(RouteBase):
    def exh(self, *pools):
        self.exhausted(**{p: E for p in pools})

    def strip_partners(self, data):
        for mid in PARTNERS:
            del data["models"][mid]
        for by_grade in data["defaults"].values():
            for g, entries in by_grade.items():
                by_grade[g] = [e for e in entries if not e.startswith(PARTNERS)]


class RegistryTest(PartnerBase):
    def test_the_registration_matches_the_design(self):
        data = shipped()
        for mid, vendor_grades in (("claude-opus-5-5@agy", {"low": "B", "medium": "A", "high": "A"}),
                                   ("claude-sonnet-5-5@agy", {"low": "C", "medium": "C", "high": "B"})):
            m = data["models"][mid]
            self.assertEqual((m["vendor"], m["maker"], m["pool"], m["cli_id"], m["fallback_only"]),
                             ("google", "claude", "google-partner", mid.split("@")[0], True))
            self.assertEqual((m["aa_slug"], m["grades"]), (mid.split("@")[0], vendor_grades))
            # the same grades as the Claude Code entry of the same model at the shared efforts
            twin = data["models"][mid.split("@")[0]]["grades"]
            self.assertTrue(all(twin[e] == g for e, g in m["grades"].items()), mid)
        for gone in ("claude-opus-5-5", "claude-sonnet-5-5"):
            self.assertNotIn(gone, data["unregistered"]["google"])
        self.assertEqual(data["unregistered"]["google"]["gpt-oss-120b"], "AA index too low (High 3.8); agy offers medium only")

    def test_resolvers(self):
        m = shipped()["models"]
        self.assertEqual([route.model_maker(m[k]) for k in ("claude-opus-5-5", "gemini-4-neon", "claude-opus-5-5@agy")],
                         ["claude", "google", "claude"])
        self.assertEqual([route.model_pool(m[k]) for k in ("claude-opus-5-5", "gemini-4-neon", "claude-sonnet-5-5@agy")],
                         ["claude", "google", "google-partner"])
        self.assertEqual(route.model_cli_id("claude-opus-5-5@agy", m["claude-opus-5-5@agy"]), "claude-opus-5-5")
        self.assertEqual(route.model_cli_id("gemini-4-neon", m["gemini-4-neon"]), "gemini-4-neon")


class UnchangedTest(PartnerBase):
    """Acceptance 1: with no exhausted pool the route table is the one without the new entries."""

    def table(self):
        out = {}
        for host in route.HOSTS:
            for role in route.ROLES:
                for author in ((None,) if role != "review" else ("claude", "openai", "google")):
                    for grade in (None, "S", "B", "C"):
                        extra = ["--grade", grade] if grade else []
                        if author:
                            extra += ["--author-vendor", author] + ([] if grade else ["--grade", "A"])
                        for orch in ((), ("--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "low")):
                            code, r = self.go("--role", role, "--json", "--host", host, *extra, *orch)
                            out[(host, role, author, grade, orch)] = (code, r)
        return out

    def states(self):
        yield "plain", {}, None
        yield "google free", {"plans": {"google": "free"}}, None
        yield "google list with claude ids", {"available": {"google": {"ids": [
            "gemini-4-hydrogen-high", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-low", "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low", "claude-opus-5-5-high", "claude-sonnet-5-5-medium"]}}}, None
        yield "role pins", {"roles": {"implement": {"vendor": "google"}, "translate": {"effort": "medium"}}}, None
        yield "bench on the new ids", {}, {"claude-opus-5-5@agy": [self.b("index", 99, "high"), self.b("cost_per_task", 0.1, "high")],
                                          "claude-sonnet-5-5@agy": [self.b("index", 98, "medium")]}

    def test_every_cell_is_unchanged_without_exhaustion(self):
        for name, local, bench in self.states():
            with self.subTest(name):
                self.local(local)
                if bench:
                    self.bench(bench)
                with_entries = self.table()
                self.models_edit(self.strip_partners)
                without = self.table()
                shutil_copy_back(self.cfg)
                self.assertEqual(len(with_entries), len(without))
                for key in with_entries:
                    self.assertEqual(with_entries[key], without[key], key)
                    code, r = with_entries[key]
                    if code == 0:
                        self.assertNotIn("@agy", r["model"], key)

    def test_a_partner_pool_exhausted_alone_changes_nothing(self):
        base = self.table()
        self.exh("google-partner")
        self.assertEqual(self.table(), base)


def shutil_copy_back(cfg):
    import shutil
    shutil.copy(os.path.join(os.path.dirname(HERE), "models.json"), cfg)


class SubstituteTest(PartnerBase):
    def test_google_exhausted_translation_and_agy_implementation_go_to_the_partner(self):
        self.exh("google")
        r = self.assertPick(self.route("translate"), "claude-sonnet-5-5@agy", "high", "run")
        self.assertEqual((r["vendor"], r["maker"], r["pool"], r["grade"]), ("google", "claude", "google-partner", "B"))
        self.assertEqual(r["reason"][0], "substituted:google\u2192google-partner")
        self.assertIn("exhausted_skip:google", r["reason"])
        self.assertRegex(r["notice"], r"google exhausted until \d\d:\d\d UTC, substituted claude-sonnet-5-5@agy "
                                      r"\(google-partner quota\)\.")
        cmd = r["launch"]["command"]
        self.assertEqual((cmd[cmd.index("--vendor") + 1], cmd[cmd.index("--model") + 1]), ("google", "claude-sonnet-5-5@agy"))
        self.assertEqual(r["launch"].get("escalate"), None)
        r = self.assertPick(self.route("implement", host="agy"), "claude-sonnet-5-5@agy", "high", "run")
        self.assertEqual(r["grade"], "B")
        r = self.assertPick(self.route("design", host="agy"), "claude-opus-5-5@agy", "medium", "run")
        self.assertEqual(r["grade"], "A")
        r = self.assertPick(self.route("read", host="agy"), "claude-sonnet-5-5@agy", "low", "run")
        self.assertEqual(r["grade"], "C")

    def test_partner_pool_exhausted_too_goes_to_another_vendor(self):
        self.exh("google", "google-partner")
        r = self.route("translate")[1]
        self.assertEqual(r["vendor"], "openai")
        self.assertNotIn("@agy", r["model"])
        self.assertIn("google exhausted until", r["notice"])
        self.assertNotIn("(google-partner quota)", r["notice"])
        self.assertTrue(all(not c.endswith("google-partner") for c in r["reason"]), r["reason"])
        self.assertEqual(self.route("design", host="agy")[1]["vendor"], "openai")

    def test_no_partner_at_the_grade_goes_to_another_vendor(self):
        self.exh("google")
        r = self.route("design", "--grade", "S", host="agy")[1]
        self.assertEqual(r["vendor"], "openai")

    def test_claude_exhausted_design_implement_read_go_to_the_agy_claude(self):
        self.exh("claude")
        for host in ("claude-desktop", "claude"):
            with self.subTest(host):
                design = self.assertPick(self.route("design", host=host), "claude-opus-5-5@agy", "medium", "run")
                self.assertEqual((design["vendor"], design["maker"], design["pool"]), ("google", "claude", "google-partner"))
                self.assertNotIn("native_model_cap", design["reason"])
                self.assertNotIn("escalate", design["launch"])
                self.assertIn("claude exhausted until", design["notice"])
                self.assertIn("substituted claude-opus-5-5@agy (google-partner quota)", design["notice"])
                cmd = design["launch"]["command"]
                self.assertEqual((cmd[cmd.index("--vendor") + 1], cmd[cmd.index("--model") + 1], cmd[cmd.index("--effort") + 1]),
                                 ("google", "claude-opus-5-5@agy", "medium"))
                self.assertPick(self.route("implement", host=host), "claude-sonnet-5-5@agy", "high", "run")
                self.assertPick(self.route("read", host=host), "claude-sonnet-5-5@agy", "low", "run")
        # the native session cap does not apply to an agy run; ask_user is still judged against the orchestrator
        r = self.route("design", "--orchestrator-model", "claude-haiku-5-5", "--orchestrator-effort", "low")[1]
        self.assertEqual((r["model"], r["launch"]["kind"]), ("claude-opus-5-5@agy", "run"))
        self.assertNotIn("native_model_cap", r["reason"])
        self.assertIs(r["ask_user"], True)
        self.assertIn("grade A is above the orchestrator's C", r["ask_reason"])
        r = self.route("design", "--orchestrator-model", "claude-opus-5-5", "--orchestrator-effort", "high")[1]
        self.assertEqual(r["model"], "claude-opus-5-5@agy")
        self.assertIs(r["ask_user"], False)

    def test_claude_is_substituted_in_its_own_place_on_other_hosts(self):
        self.exh("claude")
        self.assertEqual(self.route("implement", host="codex")[1]["vendor"], "openai")  # openai comes first on codex
        self.assertEqual(self.route("implement", host="agy")[1]["model"], "gemini-4-hydrogen")
        # only openai exhausted: nothing is registered as a stand-in for it, so the next vendor's own model is used
        self.exh("openai")
        r = self.assertPick(self.route("implement", host="codex"), "claude-sonnet-5-5", "high", "run")
        self.assertEqual((r["vendor"], r["pool"]), ("claude", "claude"))
        self.assertIn("exhausted_skip:openai", r["reason"])
        self.assertEqual(r["reason"][0], "substituted:openai\u2192claude")
        self.assertIn("openai exhausted until", r["notice"])
        self.assertNotIn("quota)", r["notice"])
        self.exh("claude", "openai")
        r = self.assertPick(self.route("implement", host="codex"), "claude-sonnet-5-5@agy", "high", "run")  # claude's place comes first
        self.assertTrue(r["launch"]["escalate"])  # a sandboxed Codex needs approval for any run.py
        self.assertIn("exhausted_skip:openai", r["reason"])
        self.assertIn("exhausted_skip:claude", r["reason"])
        self.assertEqual(r["reason"][0], "substituted:openai\u2192google-partner")
        self.assertIn("openai exhausted until", r["notice"])
        self.assertIn("claude exhausted until", r["notice"])
        self.assertIn("substituted claude-sonnet-5-5@agy (google-partner quota)", r["notice"])
        self.exh("claude", "openai", "google-partner")
        self.assertPick(self.route("implement", host="codex"), "gemini-4-hydrogen", "medium")

    def test_claude_and_partner_exhausted_goes_to_another_vendor(self):
        self.exh("claude", "google-partner")
        self.assertEqual(self.route("design")[1]["vendor"], "openai")

    def test_claude_exhausted_and_no_google_plan_goes_to_another_vendor(self):
        self.exh("claude")
        self.local({"plans": {"google": "none"}})
        r = self.route("design")[1]
        self.assertEqual(r["vendor"], "openai")

    def test_the_agy_cli_must_be_present(self):
        self.exh("claude")
        env = dict(CLIS)
        del env["TAGTEAM_CLI_GOOGLE"]
        r = self.route("design", env=env)[1]
        self.assertEqual(r["vendor"], "openai")
        self.assertIn("cli_missing:google", r["reason"])

    def test_the_available_list_decides_with_the_cli_id(self):
        self.exh("google")
        self.local({"available": {"google": {"ids": ["gemini-4-hydrogen-high", "claude-opus-5-5-high"]}}})
        self.assertPick(self.route("design", host="agy"), "claude-opus-5-5@agy", "high", "run")
        self.assertEqual(self.route("implement", host="agy")[1]["vendor"], "openai")  # no Sonnet in the list
        self.local({"available": {"google": {"ids": ["gemini-4-hydrogen-high", "claude-opus-5-5@agy-high"]}}})
        self.assertEqual(self.route("design", host="agy")[1]["vendor"], "openai")  # the registry id is not a CLI id

    def test_enabled_false_removes_a_partner(self):
        self.exh("google")
        self.local({"models": {"claude-sonnet-5-5@agy": {"enabled": False}}})
        self.assertPick(self.route("implement", host="agy"), "claude-opus-5-5@agy", "low")

    def test_bench_orders_the_partner_candidates(self):
        self.exh("google")
        self.bench({"claude-opus-5-5@agy": [self.b("index", 70, "low"), self.b("cost_per_task", 1.0, "low")],
                    "claude-sonnet-5-5@agy": [self.b("index", 60, "medium"), self.b("cost_per_task", 0.5, "medium")]})
        self.assertPick(self.route("implement", host="agy"), "claude-opus-5-5@agy", "low")

    def test_a_vendor_pin_never_picks_a_partner_without_exhaustion(self):
        self.local({"roles": {"implement": {"vendor": "google"}}})
        self.assertEqual(self.route("implement")[1]["model"], "gemini-4-hydrogen")
        self.local({"tasks": {"g": {"about": "x", "base": "implement", "vendor": "google"}}})
        code, r = self.go("--task", "g", "--json")
        self.assertEqual((code, r["model"], r["task_applied"]), (0, "gemini-4-hydrogen", True))
        # every native Google model unusable: the partner is still not a candidate, the route moves on
        self.local({"tasks": {"g": {"about": "x", "base": "implement", "vendor": "google"}},
                    "models": {m: {"enabled": False} for m, v in shipped()["models"].items()
                               if v["vendor"] == "google" and not v.get("fallback_only")}})
        code, r = self.go("--task", "g", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("@agy", r["model"])
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:unavailable:google", r["reason"])
        self.assertEqual(self.route("implement", host="agy")[1]["vendor"], "openai")

    def test_a_task_cause_skips_the_authors_maker_like_the_route(self):
        # a registry where a cross-maker model is not fallback_only: the route skips it for a claude author, and so does the diagnosis
        def edit(d):
            d["models"]["claude-opus-5-5@agy"].pop("fallback_only")
            d["models"]["claude-sonnet-5-5@agy"].pop("fallback_only")
        self.models_edit(edit)
        self.local({"tasks": {"g": {"about": "x", "base": "review", "vendor": "google"}},
                    "models": {m: {"enabled": False} for m, v in shipped()["models"].items()
                               if v["vendor"] == "google" and not v.get("fallback_only") and "@" not in m}})
        env = dict(CLIS)
        del env["TAGTEAM_CLI_GOOGLE"]
        code, r = self.go("--task", "g", "--json", "--author-vendor", "claude", "--grade", "A", env=env)
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:unavailable:google", r["reason"])  # not cli_missing: the claude-made model is no candidate
        self.assertNotEqual(r["maker"], "claude")

    def test_only_a_pin_of_the_model_itself_selects_a_partner_without_exhaustion(self):
        self.local({"roles": {"implement": {"model": "claude-sonnet-5-5@agy"}}})
        r = self.assertPick(self.route("implement"), "claude-sonnet-5-5@agy", "high", "run")
        self.assertIn("user_override", r["reason"])
        self.local({"tasks": {"p": {"about": "x", "base": "implement", "model": "claude-sonnet-5-5@agy"}}})
        code, r = self.go("--task", "p", "--json")
        self.assertEqual((r["model"], r["task_applied"]), ("claude-sonnet-5-5@agy", True))

    def test_inference_ignores_the_partner_ids(self):
        models = shipped()
        local = {"available": {"google": {"ids": ["gemini-3.8-flash-low", "claude-opus-5-5-high"],
                                          "checked": route.now().strftime("%Y-%m-%d")}}}
        router = route.Router(models, local, {})
        self.assertTrue(router.inferred_free("google"))
        self.assertFalse(any("claude" in i for i in router.pro_ids("google")))


class IndependenceTest(PartnerBase):
    """Acceptance 4: a claude author is never reviewed by a model made by claude."""

    AUTHORS = (("--author-vendor", "claude"),
               ("--author-model", "claude-opus-5-5", "--author-effort", "high"),
               ("--author-model", "claude-opus-5-5@agy", "--author-effort", "high"),
               ("--author-model", "claude-sonnet-5-5@agy", "--author-effort", "medium", "--author-vendor", "claude"),
               ("--author-model", "claude-opus-5-5@agy", "--author-effort", "high", "--author-vendor", "google"))

    def test_no_host_and_no_exhaustion_combination_reviews_claude_with_claude(self):
        makers = {mid: route.model_maker(m) for mid, m in shipped()["models"].items()}
        for host in route.HOSTS:
            for size in range(len(POOLS) + 1):
                for combo in itertools.combinations(POOLS, size):
                    self.exh(*combo)
                    for author in self.AUTHORS:
                        grade = ["--grade", "A"] if "--author-effort" not in author else []
                        code, r = self.go("--role", "review", "--json", "--host", host, *author, *grade)
                        ctx = (host, combo, author, r)
                        self.assertIn(code, (0, 3), ctx)
                        if code == 0:
                            self.assertNotEqual(makers[r["model"]], "claude", ctx)
                            self.assertNotEqual(r["maker"], "claude", ctx)

    def test_the_agy_claude_can_review_a_non_claude_author(self):
        self.exh("claude")
        r = self.assertPick(self.route("review", "--author-vendor", "google"), "claude-opus-5-5@agy", "medium", "run")
        self.assertEqual((r["maker"], r["pool"]), ("claude", "google-partner"))
        self.assertIn("claude exhausted until", r["notice"])
        # an openai author may be reviewed by it too
        self.assertPick(self.route("review", "--author-vendor", "openai"), "claude-opus-5-5@agy", "medium")

    def test_claude_author_with_claude_and_google_exhausted_is_reviewed_by_openai_or_incomplete(self):
        self.exh("claude", "google")
        r = self.route("review", "--author-vendor", "claude")[1]
        self.assertEqual(r["vendor"], "openai")
        self.exh("claude", "google", "openai")
        code, r = self.route("review", "--author-vendor", "claude")
        self.assertEqual((code, r["status"]), (3, "incomplete"))

    def test_an_agy_claude_author_is_a_claude_author(self):
        code, r = self.route("review", "--author-model", "claude-opus-5-5@agy", "--author-effort", "high")
        self.assertEqual(code, 0)
        self.assertEqual(r["maker"], "openai")  # first non-claude vendor on the claude host
        self.assertIn("author_vendor_excluded:claude", r["reason"])
        self.assertEqual(r["grade"], "A")  # from the @agy model's grades

    def test_an_author_vendor_that_matches_neither_maker_nor_vendor_is_a_usage_error(self):
        code, _ = self.route("review", "--author-model", "claude-opus-5-5@agy", "--author-effort", "high", "--author-vendor", "openai")
        self.assertEqual(code, 2)
        code, _ = self.route("review", "--author-model", "claude-opus-5-5", "--author-effort", "high", "--author-vendor", "google")
        self.assertEqual(code, 2)

    def test_a_pinned_agy_claude_reviewer_is_refused_for_a_claude_author(self):
        self.local({"roles": {"review": {"model": "claude-opus-5-5@agy"}}})
        r = self.route("review", "--author-vendor", "claude")[1]
        self.assertIn("override_same_vendor_as_author", r["reason"])
        self.assertNotEqual(r["maker"], "claude")
        r = self.route("review", "--author-vendor", "google")[1]  # allowed for another maker
        self.assertEqual(r["model"], "claude-opus-5-5@agy")
        self.assertIn("user_override", r["reason"])

    def test_a_pinned_agy_claude_reviewer_in_an_exhausted_pool_is_refused(self):
        self.local({"roles": {"review": {"model": "claude-opus-5-5@agy"}}})
        self.exh("google-partner")
        r = self.route("review", "--author-vendor", "openai")[1]
        self.assertIn("override_exhausted", r["reason"])
        self.assertNotEqual(r["model"], "claude-opus-5-5@agy")

    def test_a_task_pinned_to_an_agy_claude_falls_back_with_the_maker_as_cause(self):
        self.local({"tasks": {"deep": {"about": "x", "base": "review", "model": "claude-opus-5-5@agy"}}})
        code, r = self.go("--task", "deep", "--json", "--author-vendor", "claude", "--grade", "A")
        self.assertEqual(code, 0)
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:author_vendor", r["reason"])
        self.assertNotEqual(r["maker"], "claude")
        self.exh("google-partner")
        code, r = self.go("--task", "deep", "--json", "--author-vendor", "openai", "--grade", "A")
        self.assertIn("task_fallback:exhausted:google-partner", r["reason"])


class ExhaustedKeysTest(PartnerBase):
    def read(self):
        with open(self.exh_path, encoding="utf-8") as f:
            return json.load(f)["vendors"]

    def test_mark_and_clear_accept_pool_names(self):
        for name in POOLS:
            self.assertEqual(self.go("--mark-exhausted", name, "--evidence", "limit")[0], 0, name)
        self.assertEqual(sorted(self.read()), sorted(POOLS))
        self.assertEqual(self.go("--mark-exhausted", "google-parter", "--evidence", "x")[0], 2)
        self.go("--clear-exhausted", "google-partner")
        self.assertNotIn("google-partner", self.read())
        self.assertIn("google", self.read())

    def test_the_cooldown_of_a_pool_comes_from_its_vendor(self):
        self.models_edit(lambda d: d["vendors"]["google"].update(cooldown_minutes=30))
        self.go("--mark-exhausted", "google-partner", "--evidence", "x")
        e = self.read()["google-partner"]
        since = route.parse_ts(e["since"])
        self.assertEqual((route.parse_ts(e["until"]) - since).total_seconds(), 30 * 60)

    def test_the_old_google_key_still_means_the_google_models(self):
        self.exhausted(google=E)  # a file written before pools existed
        self.assertEqual(self.route("translate")[1]["model"], "claude-sonnet-5-5@agy")
        self.assertEqual(self.go("--mark-exhausted", "google", "--evidence", "x", "--until", FUTURE)[0], 0)
        self.assertEqual(list(self.read()), ["google"])

    def test_a_marked_partner_pool_blocks_only_the_partner(self):
        self.go("--mark-exhausted", "google-partner", "--evidence", "x", "--until", FUTURE)
        self.assertPick(self.route("translate"), "gemini-4-hydrogen", "medium")
        self.local({"roles": {"translate": {"model": "claude-sonnet-5-5@agy"}}})
        self.assertIn("override_exhausted", self.route("translate")[1]["reason"])

    def test_the_result_names_maker_and_pool(self):
        r = self.route("implement")[1]
        self.assertEqual((r["maker"], r["pool"]), ("claude", "claude"))
        r = self.route("translate")[1]
        self.assertEqual((r["maker"], r["pool"]), ("google", "google"))
        r = self.route("review", "--author-vendor", "claude")[1]
        self.assertEqual((r["maker"], r["pool"]), ("openai", "openai"))


if __name__ == "__main__":
    unittest.main()
