"""Upgrade rule: after the grade is chosen, a higher-grade cell of the same vendor at about the same cost replaces it
(grading.upgrade_cost_ratio, committed 1.5). Synthetic numbers; the implement role is grade B with a cost cap of 2."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import route  # noqa: E402
from test_route import RouteBase  # noqa: E402

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


class UpgradeTest(RouteBase):
    def setUp(self):
        super().setUp()
        self.anchor = [self.b("index", 35, "high"), self.b("cost_per_task", 1.0, "high")]  # sonnet high: grade B

    def set_bench(self, anchor=None, opus_index=40, opus_cost=1.4, extra=None):
        models = {SONNET: anchor if anchor is not None else self.anchor}
        opus = []
        if opus_index is not None:
            opus.append(self.b("index", opus_index, "medium"))
        if opus_cost is not None:
            opus.append(self.b("cost_per_task", opus_cost, "medium"))
        models[OPUS] = opus  # opus medium: grade A, one grade above the role
        models.update(extra or {})
        self.bench(models)

    def test_committed_ratio(self):
        self.assertEqual(route.load_json(os.path.join(self.cfg, "models.json"), True)["grading"]["upgrade_cost_ratio"], 1.5)

    def test_upgrades_to_higher_grade_within_ratio(self):
        self.set_bench()
        r = self.assertPick(self.route("implement"), OPUS, "medium")
        self.assertEqual(r["grade"], "B")  # the role's grade, not the cell's
        self.assertIn("upgrade:%s:medium" % OPUS, r["reason"])
        self.assertIn("grade B", r["notice"])

    def test_no_upgrade_without_ratio(self):
        self.set_bench()
        self.models_edit(lambda d: d["grading"].pop("upgrade_cost_ratio"))
        r = self.assertPick(self.route("implement"), SONNET, "high")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])
        self.models_edit(lambda d: d["grading"].update(upgrade_cost_ratio=0))
        self.assertPick(self.route("implement"), SONNET, "high")

    def test_no_upgrade_index_below_anchor(self):
        self.set_bench(opus_index=34)
        self.assertPick(self.route("implement"), SONNET, "high")
        self.set_bench(opus_index=35)  # equal index is enough
        self.assertPick(self.route("implement"), OPUS, "medium")

    def test_no_upgrade_cost_ratio_exceeded(self):
        self.set_bench(opus_cost=1.6)
        self.assertPick(self.route("implement"), SONNET, "high")
        self.set_bench(opus_cost=1.5)  # exactly the ratio is allowed
        self.assertPick(self.route("implement"), OPUS, "medium")

    def test_no_upgrade_over_role_cap(self):
        self.set_bench(anchor=[self.b("index", 35, "high"), self.b("cost_per_task", 1.8, "high")], opus_cost=2.5)
        self.assertPick(self.route("implement"), SONNET, "high")  # 2.5 is within 1.8 * 1.5 but above the B cap 2
        self.set_bench(anchor=[self.b("index", 35, "high"), self.b("cost_per_task", 1.8, "high")], opus_cost=2.0)
        self.assertPick(self.route("implement"), OPUS, "medium")

    def test_no_upgrade_unknown_cell_values(self):
        self.set_bench(opus_cost=None)
        self.assertPick(self.route("implement"), SONNET, "high")
        self.set_bench(opus_index=None)
        self.assertPick(self.route("implement"), SONNET, "high")

    def test_no_upgrade_unknown_anchor_values(self):
        self.set_bench(anchor=[self.b("index", 35, "high")])  # no cost: the anchor itself is a tier 1 pick
        self.assertPick(self.route("implement"), SONNET, "high")
        self.set_bench(anchor=[self.b("cost_per_task", 1.0, "high")])
        self.assertPick(self.route("implement"), SONNET, "high")
        self.set_bench(anchor=[])
        self.assertPick(self.route("implement"), SONNET, "high")

    def test_no_upgrade_when_user_gives_effort(self):
        self.set_bench()
        self.local({"roles": {"implement": {"effort": "high"}}})
        r = self.assertPick(self.route("implement"), SONNET, "high")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])

    def test_no_upgrade_over_cap_on_native(self):
        self.set_bench()
        # an orchestrator on Sonnet high: Opus is earlier in models.json (stronger), so a native session cannot take it
        r = self.assertPick(self.route("implement", "--orchestrator-model", SONNET, "--orchestrator-effort", "high"),
                            SONNET, "high", "session")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])
        # an orchestrator on Opus high takes it
        r = self.assertPick(self.route("implement", "--orchestrator-model", OPUS, "--orchestrator-effort", "high"),
                            OPUS, "medium", "session")
        self.assertIn("upgrade:%s:medium" % OPUS, r["reason"])
        # not native (host claude): the cap does not filter upgrades
        self.assertPick(self.route("implement", "--orchestrator-model", SONNET, "--orchestrator-effort", "high",
                                   host="claude"), OPUS, "medium")

    def test_no_downgrade_and_only_strictly_higher_grade(self):
        # haiku max is grade B like the anchor, with a better index at the same cost: tier 0 picks it, no upgrade happens
        self.set_bench(opus_index=None, extra={"claude-haiku-5-5": [self.b("index", 50, "max"), self.b("cost_per_task", 1.0, "max")]})
        r = self.assertPick(self.route("implement"), "claude-haiku-5-5", "max")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])

    def test_best_candidate_highest_index_then_lowest_cost(self):
        extra = {SONNET: self.anchor + [self.b("index", 41, "xhigh"), self.b("cost_per_task", 1.2, "xhigh")]}  # sonnet xhigh: A
        self.set_bench(opus_index=41, opus_cost=1.4, extra=extra)
        self.assertPick(self.route("implement"), SONNET, "xhigh")  # same index, lower cost
        self.set_bench(opus_index=42, opus_cost=1.4, extra=extra)
        self.assertPick(self.route("implement"), OPUS, "medium")  # higher index wins over cost

    def test_user_model_override_is_not_an_upgrade(self):
        self.set_bench()
        self.local({"roles": {"implement": {"model": OPUS}}})
        r = self.assertPick(self.route("implement"), OPUS, "low")  # the pin's lowest effort at the role grade
        self.assertIn("user_override", r["reason"])
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])

    def test_review_takes_an_equal_or_stronger_cell(self):
        # a grade A review by claude's pool: the S cell (fable high) costs about the same as the A anchor
        self.bench({"claude-opus-5-5": [self.b("index", 50, "medium"), self.b("cost_per_task", 3.0, "medium")],
                    "claude-fable-5-2": [self.b("index", 62, "high"), self.b("cost_per_task", 4.0, "high")]})
        r = self.assertPick(self.route("review", "--author-vendor", "openai"), "claude-fable-5-2", "high")
        self.assertEqual(r["grade"], "A")
        self.assertIn("upgrade:claude-fable-5-2:high", r["reason"])


    def test_native_effort_cap_holds_with_an_unknown_orchestrator_model(self):
        extra = {SONNET: self.anchor + [self.b("index", 41, "xhigh"), self.b("cost_per_task", 1.2, "xhigh")]}  # sonnet xhigh: A
        self.set_bench(opus_index=None, extra=extra)
        r = self.assertPick(self.route("implement", "--orchestrator-model", "gpt-6.1-sol", "--orchestrator-effort", "high"),
                            SONNET, "high")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])

    def test_upgrade_without_its_cli_gives_way_to_the_launchable_anchor(self):
        self.set_bench()
        env = {"TAGTEAM_CLI_OPENAI": "[]", "TAGTEAM_CLI_GOOGLE": "[]"}  # no claude CLI: opus medium would need run.py
        r = self.assertPick(self.route("implement", host="claude", env=env), SONNET, "high", "subagent")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])
        self.assertPick(self.route("implement", host="claude"), OPUS, "medium", "run")  # with the CLI it upgrades


if __name__ == "__main__":
    unittest.main()
