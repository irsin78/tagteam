"""Host vendor: design, implement and read go to the host vendor's models; another vendor only through exhaustion, a task or
a role pin that names its hosts, and the result then says why (off_host_vendor, off_host_cause, the notice's last sentence)."""
import os
import re
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import route  # noqa: E402
from test_route import CLIS, FUTURE, RouteBase  # noqa: E402

PINNED = ("design", "implement", "read")
HOST_VENDOR = {"claude-desktop": "claude", "claude": "claude", "codex": "openai", "agy": "google"}
UNTIL = datetime.strptime(FUTURE, "%Y-%m-%dT%H:%M:%SZ").strftime("%H:%M")
CAUSE = re.compile(r"^(roles\.(design|implement|read) pin in models\.local\.json|task \S+|(claude|openai|google) exhausted until "
                   r"\d\d:\d\d UTC|(claude|openai|google) plan free|(claude|openai|google) CLI missing|"
                   r"no usable (claude|openai|google) model)$")
KINDS = {"pin": r"^roles\.", "task": r"^task ", "exhausted": r" exhausted until ", "plan": r" plan free$",
         "cli": r" CLI missing$", "none": r"^no usable "}
EXH = {"since": "2020-01-01T00:00:00Z", "until": FUTURE, "evidence": "x", "source": "manual"}
SOL = "gpt-6.2-sol"


class HostVendorBase(RouteBase):
    def assertOnHost(self, r):
        self.assertFalse(r["off_host_vendor"], r)
        self.assertNotIn("off_host_cause", r)
        self.assertNotIn("Not the host vendor", r["notice"])

    def assertOffHost(self, r, host_vendor, cause):
        self.assertTrue(r["off_host_vendor"], r)
        self.assertEqual(r["off_host_cause"], cause)
        self.assertTrue(r["notice"].endswith(" Not the host vendor (%s): %s." % (host_vendor, cause)), r["notice"])


class AuthorOnlyForReviewTest(HostVendorBase):
    """Acceptance 1: an author vendor changes nothing outside a review."""

    def test_author_vendor_does_not_move_pinned_roles(self):
        for role in PINNED:
            for author in (("--author-vendor", "claude", "--grade", "B"),
                           ("--author-model", "claude-opus-5-5", "--author-effort", "medium"),
                           ("--author-vendor", "claude", "--author-model", "claude-opus-5-5", "--author-effort", "low")):
                with self.subTest(role=role, author=author):
                    plain = self.route(role, "--grade", "B", host="claude")[1]
                    code, r = self.route(role, *author, host="claude")
                    self.assertEqual(code, 0, r)
                    self.assertEqual(r["vendor"], "claude")
                    self.assertEqual((r["model"], r["effort"], r["grade"]), (plain["model"], plain["effort"], "B"))
                    self.assertFalse([x for x in r["reason"] if x.startswith("author_vendor_excluded")], r["reason"])
                    self.assertOnHost(r)

    def test_grade_from_the_author_model_still_applies(self):
        code, r = self.route("implement", "--author-model", "claude-opus-5-5", "--author-effort", "high", host="claude")
        self.assertEqual((code, r["vendor"], r["grade"]), (0, "claude", "A"))

    def test_review_still_excludes_the_author(self):
        r = self.route("review", "--author-vendor", "claude", "--grade", "B", host="claude")[1]
        self.assertNotEqual(r["vendor"], "claude")
        self.assertIn("author_vendor_excluded:claude", r["reason"])
        self.assertNotIn("off_host_vendor", r)  # review and translate carry neither key
        self.assertNotIn("Not the host vendor", r["notice"])
        r = self.route("translate", host="claude")[1]
        self.assertNotIn("off_host_vendor", r)

    def test_a_task_preference_ignores_the_author(self):
        self.local({"tasks": {"t": {"about": "x", "base": "implement", "vendor": "claude"}}})
        code, r = self.go("--task", "t", "--json", "--host", "claude", "--author-vendor", "claude", "--grade", "B")
        self.assertEqual((code, r["vendor"], r["task_applied"]), (0, "claude", True))


class PinScopeTest(HostVendorBase):
    """Acceptance 2 and 3: a pin of another vendor's model applies only on the hosts it names."""

    def test_other_vendor_pin_without_hosts(self):
        self.local({"roles": {"implement": {"model": SOL}}})
        for host in ("claude", "claude-desktop"):
            with self.subTest(host):
                r = self.route("implement", host=host)[1]
                self.assertEqual(r["vendor"], "claude")
                self.assertIn("pin_other_vendor:implement", r["reason"])
                self.assertNotIn("user_override", r["reason"])
                self.assertIn(" roles.implement pin (%s) not applied on host %s: add hosts to apply it." % (SOL, host),
                              r["notice"])
                self.assertOnHost(r)
        r = self.assertPick(self.route("implement", host="codex"), SOL, "low")
        self.assertIn("user_override", r["reason"])
        self.assertNotIn("pin_other_vendor:implement", r["reason"])
        self.assertNotIn("not applied", r["notice"])
        self.assertOnHost(r)

    def test_other_vendor_pin_with_hosts(self):
        self.local({"roles": {"implement": {"model": SOL, "hosts": ["claude"]}}})
        r = self.assertPick(self.route("implement", host="claude"), SOL, "low")
        self.assertOffHost(r, "claude", "roles.implement pin in models.local.json")
        r = self.route("implement", host="claude-desktop")[1]
        self.assertEqual(r["vendor"], "claude")
        self.assertNotIn("pin_other_vendor:implement", r["reason"])  # a host outside the list: ignored silently
        self.assertNotIn("not applied", r["notice"])
        self.assertOnHost(r)

    def test_the_whole_entry_is_not_applied(self):
        plain = self.route("implement", host="claude")[1]
        self.local({"roles": {"implement": {"model": SOL, "effort": "high", "grade": "A"}}})
        r = self.route("implement", host="claude")[1]
        # neither the effort nor the grade of the entry: the same pick as without it (an effort leak would give Sonnet high)
        self.assertEqual((r["model"], r["effort"], r["grade"]), (plain["model"], plain["effort"], plain["grade"]))

    def test_an_explicit_other_vendor_drops_a_host_vendor_model_pin(self):
        # review finding 1: the model is the host vendor's, the vendor is not: the whole entry is another vendor's
        self.local({"roles": {"implement": {"model": "claude-opus-5-5", "vendor": "openai"}},
                    "models": {"claude-opus-5-5": {"enabled": False}}})
        r = self.route("implement", host="claude")[1]
        self.assertEqual(r["vendor"], "claude")
        self.assertIn("pin_other_vendor:implement", r["reason"])
        self.assertIn("roles.implement pin (claude-opus-5-5) not applied on host claude", r["notice"])
        self.assertOnHost(r)

    def test_validation_is_no_earlier_than_before(self):
        # review finding 2: an unknown role model replaced by a task, or scoped to another host, does not block the route
        self.local({"roles": {"implement": {"model": "gpt-6-sol"}},
                    "tasks": {"t": {"about": "x", "base": "implement", "model": SOL}}})
        code, r = self.go("--task", "t", "--json", "--host", "claude")
        self.assertEqual((code, r["model"], r["task_applied"]), (0, SOL, True))
        self.local({"roles": {"implement": {"model": "gpt-6-sol", "hosts": ["codex"]}}})
        self.assertEqual(self.route("implement", host="claude")[0], 0)
        # where it errored before, it still does
        self.assertEqual(self.route("implement", host="codex")[0], 2)
        self.local({"roles": {"implement": {"model": "gpt-6-sol"}}})
        self.assertEqual(self.route("implement", host="claude")[0], 2)
        self.local({"roles": {"implement": {"vendor": "nope"}}})
        self.assertEqual(self.route("implement", host="claude")[0], 2)

    def test_vendor_only_pin(self):
        self.local({"roles": {"implement": {"vendor": "openai"}}})
        r = self.route("implement", host="claude")[1]
        self.assertEqual(r["vendor"], "claude")
        self.assertIn("pin_other_vendor:implement", r["reason"])
        self.assertIn(" roles.implement pin (openai) not applied on host claude: add hosts to apply it.", r["notice"])
        self.local({"roles": {"implement": {"vendor": "openai", "hosts": ["claude"]}}})
        r = self.route("implement", host="claude")[1]
        self.assertEqual(r["vendor"], "openai")
        self.assertEqual(r["reason"][0], "default")  # a vendor pin goes through the vendor order, not user_override
        self.assertOffHost(r, "claude", "roles.implement pin in models.local.json")

    def test_same_vendor_pin_applies_everywhere_it_matches(self):
        self.local({"roles": {"implement": {"model": "claude-sonnet-5-5"}}})
        r = self.assertPick(self.route("implement", host="claude"), "claude-sonnet-5-5", "high")
        self.assertIn("user_override", r["reason"])
        r = self.route("implement", host="codex")[1]
        self.assertEqual(r["vendor"], "openai")
        self.assertIn("pin_other_vendor:implement", r["reason"])

    def test_effort_only_pin_applies_on_every_host(self):
        self.local({"roles": {"implement": {"effort": "high"}}})
        for host in route.HOSTS:
            with self.subTest(host):
                r = self.route("implement", host=host)[1]
                self.assertEqual(r["effort"], "high")
                self.assertFalse([x for x in r["reason"] if x.startswith("pin_other_vendor")], r["reason"])

    def test_review_and_translate_pins_apply_on_every_host(self):
        self.local({"roles": {"review": {"model": SOL}, "translate": {"model": "claude-sonnet-5-5"}}})
        for host in route.HOSTS:
            with self.subTest(host):
                r = self.route("review", "--author-vendor", "google", host=host)[1]
                self.assertEqual(r["model"], SOL)
                self.assertNotIn("not applied", r["notice"])
                self.assertEqual(self.route("translate", host=host)[1]["model"], "claude-sonnet-5-5")

    def test_bad_hosts_is_a_config_error(self):
        for hosts in (["claude", "vscode"], "claude", {"claude": 1}, None):
            with self.subTest(hosts):
                self.local({"roles": {"implement": {"model": SOL, "hosts": hosts}}})
                self.assertEqual(self.route("implement", host="codex")[0], 2)

    def test_empty_hosts_applies_nowhere(self):
        self.local({"roles": {"implement": {"model": SOL, "hosts": []}}})
        r = self.route("implement", host="codex")[1]
        self.assertNotIn("user_override", r["reason"])
        self.assertNotIn("pin_other_vendor:implement", r["reason"])

    def test_a_task_still_routes_by_itself_over_a_not_applied_pin(self):
        self.local({"roles": {"implement": {"model": SOL}},
                    "tasks": {"t": {"about": "x", "base": "implement", "effort": "high"}}})
        code, r = self.go("--task", "t", "--json", "--host", "claude")
        self.assertEqual((code, r["vendor"], r["effort"], r["task_applied"]), (0, "claude", "high", True))
        self.assertIn("pin_other_vendor:implement", r["reason"])
        self.assertOnHost(r)


class OffHostCauseTest(HostVendorBase):
    """Acceptance 4: an allowed departure keeps its route and names its cause."""

    def test_exhausted_host_vendor(self):
        self.exhausted(claude=EXH)
        r = self.route("implement", host="claude")[1]
        self.assertTrue(r["reason"][0].startswith("substituted:claude"), r["reason"])
        self.assertNotEqual(r["vendor"], "claude")
        self.assertOffHost(r, "claude", "claude exhausted until %s UTC" % UNTIL)

    def test_a_stand_in_is_judged_by_its_vendor(self):
        # the @agy Claude stand-in runs through agy: vendor google, maker claude. On a Claude host it is another vendor...
        self.exhausted(claude=EXH)
        r = self.assertPick(self.route("implement", host="claude"), "claude-opus-5-5@agy", "medium")
        self.assertEqual((r["vendor"], r["maker"]), ("google", "claude"))
        self.assertOffHost(r, "claude", "claude exhausted until %s UTC" % UNTIL)
        # ...and on agy it is the host vendor's model
        self.exhausted(google=EXH)
        r = self.route("implement", host="agy")[1]
        self.assertEqual((r["vendor"], r["maker"]), ("google", "claude"))
        self.assertOnHost(r)

    def test_a_malformed_role_model_does_not_block_a_task(self):
        # second review, finding 1: a list as the role model is replaced by the task, as before; unreplaced it is a ConfigError
        self.local({"roles": {"implement": {"model": ["retired"]}},
                    "tasks": {"t": {"about": "x", "base": "implement", "model": SOL}}})
        code, r = self.go("--task", "t", "--json", "--host", "claude")
        self.assertEqual((code, r["model"], r["task_applied"]), (0, SOL, True))
        self.assertEqual(self.route("implement", host="claude")[0], 2)
        self.local({"roles": {"implement": "x"}})
        self.assertEqual(self.route("implement", host="claude")[0], 2)

    def test_a_recognized_value_scopes_the_entry_despite_an_unknown_companion(self):
        # second review, finding 2: the registered model (or vendor) is another vendor's: dropped, the unknown value unreported
        for entry, label in (({"model": SOL, "vendor": "retired"}, SOL), ({"model": "retired", "vendor": "openai"}, "openai")):
            with self.subTest(entry):
                self.local({"roles": {"implement": entry}})
                code, r = self.route("implement", host="claude")
                self.assertEqual((code, r["vendor"]), (0, "claude"))
                self.assertIn("roles.implement pin (%s) not applied on host claude" % label, r["notice"])
                self.assertEqual(self.route("implement", host="codex")[0], 2)  # where it applies, the old error

    def test_an_effort_only_task_that_leaves_the_host_vendor(self):
        # second review, finding 3: the base role picks google medium; the task's effort high sends it to openai
        self.local({"plans": {"google": "pro"}, "tasks": {"t": {"about": "x", "base": "implement", "effort": "high"}}})
        code, r = self.go("--task", "t", "--json", "--host", "agy")
        self.assertEqual((code, r["vendor"], r["task_applied"]), (0, "openai", True))
        self.assertOffHost(r, "google", "task t")

    def test_a_task_that_displaces_a_usable_host_vendor(self):
        # second review, finding 4: the base role can use openai; the task's google slot leads to the claude stand-in
        self.local({"plans": {"openai": "free", "google": "pro"},
                    "tasks": {"t": {"about": "x", "base": "implement", "vendor": "google"}},
                    "models": {m: {"enabled": False} for m in ("gemini-4-neon", "gemini-4-hydrogen", "gemini-4-argon")}})
        self.exhausted(claude=EXH)
        self.assertEqual(self.route("implement", host="codex")[1]["vendor"], "openai")
        code, r = self.go("--task", "t", "--json", "--host", "codex")
        self.assertEqual((code, r["task_applied"]), (0, True))
        self.assertOffHost(r, "openai", "task t")
        self.assertNotIn("off_cause", r)

    def test_a_vendor_pin_that_selects_a_stand_in(self):
        # review finding 3: the scoped pin chose claude, whose exhaustion gave the stand-in: the pin is the cause
        self.local({"roles": {"implement": {"vendor": "claude", "hosts": ["codex"]}}})
        self.exhausted(claude=EXH)
        r = self.assertPick(self.route("implement", host="codex"), "claude-opus-5-5@agy", "medium")
        self.assertOffHost(r, "openai", "roles.implement pin in models.local.json")

    def test_a_task_gets_no_credit_for_the_host_vendor_stand_in(self):
        # review finding 4: the task's google slot had nothing; claude's exhaustion branch picked the stand-in
        self.local({"tasks": {"t": {"about": "x", "base": "implement", "vendor": "google"}},
                    "models": {m: {"enabled": False} for m in ("gemini-4-neon", "gemini-4-hydrogen", "gemini-4-argon")}})
        self.exhausted(claude=EXH)
        code, r = self.go("--task", "t", "--json", "--host", "claude")
        self.assertEqual((code, r["model"]), (0, "claude-opus-5-5@agy"))
        self.assertOffHost(r, "claude", "claude exhausted until %s UTC" % UNTIL)

    def test_plan_none_is_not_reported_as_exhausted(self):
        # review finding 5: plan none removes the host vendor before its exhaustion matters
        self.local({"plans": {"claude": "none"}})
        self.exhausted(claude=EXH)
        r = self.route("implement", host="claude")[1]
        self.assertOffHost(r, "claude", "no usable claude model")

    def test_task_to_another_vendor(self):
        self.local({"tasks": {"deep": {"about": "x", "base": "implement", "vendor": "openai"}}})
        code, r = self.go("--task", "deep", "--json", "--host", "claude")
        self.assertEqual((code, r["vendor"], r["task_applied"]), (0, "openai", True))
        self.assertOffHost(r, "claude", "task deep")
        self.local({"tasks": {"deep": {"about": "x", "base": "design", "model": SOL}}})
        r = self.go("--task", "deep", "--json", "--host", "agy")[1]
        self.assertOffHost(r, "google", "task deep")

    def test_host_vendor_cli_missing(self):
        env = dict(CLIS)
        del env["TAGTEAM_CLI_GOOGLE"]  # agy launches its own models through run.py
        r = self.route("implement", host="agy", env=env)[1]
        self.assertNotEqual(r["vendor"], "google")
        self.assertOffHost(r, "google", "google CLI missing")

    def test_host_vendor_plan_none(self):
        self.local({"plans": {"claude": "none"}})
        r = self.route("implement", host="claude")[1]
        self.assertOffHost(r, "claude", "no usable claude model")

    def test_no_usable_host_vendor_model(self):
        self.local({"models": {m: {"enabled": False} for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")}})
        r = self.route("implement", host="claude")[1]
        self.assertOffHost(r, "claude", "no usable claude model")


class FreeHostVendorTest(HostVendorBase):
    """Decision B: a pinned role tries the host vendor first even on a free plan."""

    FLASH = {"google": {"ids": ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"]}}

    def test_free_host_vendor_with_a_usable_model(self):
        self.local({"plans": {"google": "free"}})
        r = self.route("implement", host="agy")[1]
        self.assertEqual(r["vendor"], "google")
        self.assertNotIn("plan_free_last:google", r["reason"])
        self.assertOnHost(r)

    def test_free_host_vendor_without_a_usable_model(self):
        self.local({"plans": {"google": "free"}, "available": self.FLASH})
        r = self.route("implement", host="agy")[1]
        self.assertNotEqual(r["vendor"], "google")
        self.assertOffHost(r, "google", "google plan free")

    def test_order(self):
        self.local({"plans": {"google": "free", "openai": "free"}})
        router = route.Router(self.models(), {"plans": {"google": "free", "openai": "free"}}, {})
        reason = []
        self.assertEqual(router.vendor_order({}, "agy", None, reason, "implement"), ["google", "claude", "openai"])
        self.assertEqual(reason, ["plan_free_last:openai"])  # a free vendor that is not the host's still goes last
        self.assertEqual(router.vendor_order({}, "agy", None, [], "review"), ["claude", "google", "openai"])
        self.assertEqual(router.vendor_order({}, "claude", None, [], "implement"), ["claude", "openai", "google"])
        self.assertEqual(router.vendor_order({"task_choice": True}, "agy", None, [], "implement"),
                         ["claude", "google", "openai"])  # a task that names the vendor: today's order

    def test_a_task_without_a_vendor_keeps_the_free_host_vendor_first(self):
        self.local({"plans": {"google": "free"}, "tasks": {"t": {"about": "x", "base": "implement", "effort": "medium"}}})
        code, r = self.go("--task", "t", "--json", "--host", "agy")
        self.assertEqual((code, r["vendor"], r["effort"], r["task_applied"]), (0, "google", "medium", True))
        self.assertNotIn("plan_free_last:google", r["reason"])
        self.assertOnHost(r)

    def test_review_with_a_free_vendor_is_unchanged(self):
        self.local({"plans": {"google": "free"}})
        r = self.route("review", "--author-vendor", "openai", host="agy")[1]
        self.assertEqual(r["vendor"], "claude")
        self.assertIn("plan_free_last:google", r["reason"])

    def models(self):
        import json
        with open(os.path.join(self.cfg, "models.json"), encoding="utf-8") as f:
            return json.load(f)


class UnchangedNoticeTest(HostVendorBase):
    """Acceptance 5: a host vendor result has the notice of the router before this change, byte for byte."""

    FROZEN = {
        ("claude", "design"): "design -> claude-fable-5-2 (low, claude, grade A) via run.py, host=claude. "
                              "Pass --orchestrator-model/--orchestrator-effort so the router can compare.",
        ("claude", "implement"): "implement -> claude-opus-5-5 (medium, claude, grade B) via subagent implementer, host=claude.",
        ("claude-desktop", "implement"): "implement -> claude-opus-5-5 (medium, claude, grade B) via session, "
                                         "host=claude-desktop. Check orchestrator cap.",
        ("codex", "read"): "read -> gpt-6.1-terra (low, openai, grade C) via subagent reader, host=codex.",
        ("agy", "implement"): "implement -> gemini-4-hydrogen (medium, google, grade B) via run.py, host=agy.",
    }

    def test_frozen_notices(self):
        for (host, role), want in self.FROZEN.items():
            with self.subTest(host=host, role=role):
                r = self.route(role, host=host)[1]
                self.assertEqual(r["notice"], want)
                self.assertOnHost(r)


class InvariantTest(HostVendorBase):
    """off_host_vendor is true exactly when the result's vendor is not the host vendor, and then the cause is a known one."""

    def configs(self):
        no_cli = {v: {k: c for k, c in CLIS.items() if k != "TAGTEAM_CLI_" + v.upper()} for v in ("claude", "openai", "google")}
        yield "default", {}, {}, None
        yield "google free", {"plans": {"google": "free"}}, {}, None
        yield "google inferred free", None, {}, None
        yield "google free, flash only", {"plans": {"google": "free"}, "available": FreeHostVendorTest.FLASH}, {}, None
        for v in ("claude", "openai", "google"):
            yield v + " exhausted", {}, {v: EXH}, None
            yield v + " none", {"plans": {v: "none"}}, {}, None
            yield v + " cli missing", {}, {}, no_cli[v]
        pins = {r: {"vendor": "openai", "hosts": list(route.HOSTS)} for r in PINNED}
        yield "pins with hosts", {"roles": pins}, {}, None
        yield "pins without hosts", {"roles": {r: {"model": SOL} for r in PINNED}}, {}, None
        tasks = {r + "-t": {"about": "x", "base": r, "vendor": "google"} for r in PINNED}
        yield "tasks", {"tasks": tasks}, {}, None

    def test_sweep(self):
        seen = set()
        for name, local, exh, env in self.configs():
            if local is None:
                self.local({}, stamp=False, plan_default=False)
            else:
                self.local(local)
            self.exhausted(**exh)
            for host in route.HOSTS:
                for role in PINNED:
                    argv = ("--task", role + "-t", "--json", "--host", host) if name == "tasks" else \
                        ("--role", role, "--json", "--host", host)
                    code, r = self.go(*argv, env=env)
                    with self.subTest(config=name, host=host, role=role):
                        if code != 0:
                            self.assertNotIn("off_host_vendor", r)
                            continue
                        off = r["vendor"] != HOST_VENDOR[host]
                        self.assertEqual(r["off_host_vendor"], off, r)
                        if off:
                            self.assertRegex(r["off_host_cause"], CAUSE)
                            self.assertTrue(r["notice"].endswith(
                                " Not the host vendor (%s): %s." % (HOST_VENDOR[host], r["off_host_cause"])), r["notice"])
                            seen.add(next(k for k, p in KINDS.items() if re.search(p, r["off_host_cause"])))
                        else:
                            self.assertNotIn("off_host_cause", r)
                            self.assertNotIn("Not the host vendor", r["notice"])
        self.assertEqual(seen, set(KINDS))  # every kind of cause occurred at least once


if __name__ == "__main__":
    unittest.main()
