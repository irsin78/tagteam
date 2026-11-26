"""Effort ceiling: vendors.<vendor>.max_effort drops that vendor's cells above it from the candidates and from the upgrade
targets, unless the user fixed that effort. Synthetic numbers on the shipped models.json (openai carries max_effort high)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import models_collect as mc  # noqa: E402
import route  # noqa: E402
from test_route import RouteBase  # noqa: E402

SOL, ASTRA = "gpt-6.2-sol", "gpt-6.2-astra"
SHIPPED = os.path.join(os.path.dirname(HERE), "models.json")


def shipped():
    with open(SHIPPED, encoding="utf-8") as f:
        return json.load(f)


class ShippedTest(unittest.TestCase):
    def test_openai_is_capped_at_high_and_the_others_are_not(self):
        vendors = shipped()["vendors"]
        self.assertEqual(vendors["openai"]["max_effort"], "high")
        self.assertNotIn("max_effort", vendors["claude"])
        self.assertNotIn("max_effort", vendors["google"])

    def test_every_default_entry_has_the_grade_of_its_list(self):
        data = shipped()
        for vendor, by_grade in data["defaults"].items():
            for grade, entries in by_grade.items():
                for entry in entries:
                    mid, _, effort = entry.rpartition(":")
                    with self.subTest(entry=entry, listed=grade):
                        self.assertEqual(data["models"][mid]["grades"][effort], grade)
                        self.assertIn(data["models"][mid]["vendor"], (vendor, "google"))

    def test_no_default_entry_is_above_its_vendors_max_effort(self):
        data = shipped()
        for vendor, by_grade in data["defaults"].items():
            top = data["vendors"][vendor].get("max_effort")
            for entries in by_grade.values():
                for entry in entries:
                    if top:
                        self.assertLessEqual(route.EFFORTS.index(entry.rpartition(":")[2]), route.EFFORTS.index(top), entry)

    def test_orchestrators_compare_as_a_registered_cell(self):
        data = shipped()
        for oid, o in data["orchestrators"].items():
            mid, _, effort = o["compare_as"].rpartition(":")
            self.assertIn(effort, data["models"][mid]["grades"], oid)


class CeilingTest(RouteBase):
    def cap(self, value="high"):
        self.models_edit(lambda d: d["vendors"]["openai"].update(max_effort=value) if value
                         else d["vendors"]["openai"].pop("max_effort", None))

    def sol_bench(self, extra=None):
        # the anchor: sol low is grade B; sol medium and high are A; sol xhigh is A too; astra high and xhigh are S
        models = {SOL: [self.b("index", 35, "low"), self.b("cost_per_task", 1.0, "low")]}
        models.update(extra or {})
        self.bench(models)

    def test_a_cell_above_the_limit_is_not_a_candidate(self):
        self.bench({ASTRA: [self.b("index", 58, "high"), self.b("cost_per_task", 3.0, "high"),
                            self.b("index", 59, "xhigh"), self.b("cost_per_task", 4.0, "xhigh")]})
        args = ("--author-vendor", "claude", "--grade", "S")
        self.assertPick(self.route("review", *args), ASTRA, "high")  # xhigh scores higher but is above the limit
        self.cap(None)
        self.assertPick(self.route("review", *args), ASTRA, "xhigh")

    def test_a_cell_above_the_limit_is_not_an_upgrade_target(self):
        self.sol_bench({SOL: [self.b("index", 35, "low"), self.b("cost_per_task", 1.0, "low"),
                              self.b("index", 40, "medium"), self.b("cost_per_task", 1.3, "medium"),
                              self.b("index", 50, "xhigh"), self.b("cost_per_task", 1.4, "xhigh")]})
        r = self.assertPick(self.route("implement", host="codex"), SOL, "medium")  # the best cell within the limit
        self.assertIn("upgrade:%s:medium" % SOL, r["reason"])
        self.cap(None)
        r = self.assertPick(self.route("implement", host="codex"), SOL, "xhigh")
        self.assertIn("upgrade:%s:xhigh" % SOL, r["reason"])

    def test_no_upgrade_when_every_better_cell_is_above_the_limit(self):
        self.sol_bench({SOL: [self.b("index", 35, "low"), self.b("cost_per_task", 1.0, "low"),
                              self.b("index", 50, "xhigh"), self.b("cost_per_task", 1.4, "xhigh")]})
        r = self.assertPick(self.route("implement", host="codex"), SOL, "low")
        self.assertFalse([x for x in r["reason"] if x.startswith("upgrade")])

    def test_the_limit_is_the_vendors_own(self):
        self.bench({"claude-fable-5-2": [self.b("index", 70, "max"), self.b("cost_per_task", 5.0, "max"),
                                          self.b("index", 60, "high"), self.b("cost_per_task", 4.0, "high")]})
        self.assertPick(self.route("design", "--grade", "S"), "claude-fable-5-2", "max")  # claude has no max_effort

    def test_a_user_effort_above_the_limit_is_honoured(self):
        self.local({"roles": {"design": {"vendor": "openai", "effort": "xhigh", "hosts": ["claude-desktop"]}}})
        self.assertPick(self.route("design"), SOL, "xhigh")
        self.local({"roles": {"design": {"vendor": "openai", "effort": "max", "hosts": ["claude-desktop"]}}})
        self.assertPick(self.route("design"), SOL, "max")
        self.local({"tasks": {"deep": {"about": "x", "base": "design", "vendor": "openai", "effort": "xhigh"}}})
        r = self.assertPick(self.go("--task", "deep", "--json"), SOL, "xhigh")
        self.assertTrue(r["task_applied"])

    def test_a_user_effort_does_not_lift_the_limit_for_other_efforts(self):
        # without a user effort the same role stays within the limit
        self.local({"roles": {"design": {"vendor": "openai", "hosts": ["claude-desktop"]}}})
        r = self.route("design")[1]
        self.assertEqual(r["vendor"], "openai")
        self.assertLessEqual(route.EFFORTS.index(r["effort"]), route.EFFORTS.index("high"))

    def test_no_max_effort_leaves_every_cell(self):
        self.cap(None)
        self.bench({ASTRA: [self.b("index", 59, "max"), self.b("cost_per_task", 4.0, "max")]})
        self.assertPick(self.route("review", "--author-vendor", "claude", "--grade", "S"), ASTRA, "max")

    def test_a_bad_max_effort_is_a_config_error(self):
        for bad in ("ultra", "", 3):
            with self.subTest(bad):
                self.models_edit(lambda d: d["vendors"]["openai"].update(max_effort=bad))
                self.assertEqual(self.route("implement")[0], 2)


class CollectConfigTest(RouteBase):
    def test_load_config_rejects_an_unknown_effort_word(self):
        self.assertEqual(mc.load_config(self.cfg)[0]["vendors"]["openai"]["max_effort"], "high")
        self.models_edit(lambda d: d["vendors"]["openai"].update(max_effort="ultra"))
        with self.assertRaises(ValueError):
            mc.load_config(self.cfg)

    def test_pinned_model_without_effort_stays_within_the_ceiling(self):
        self.local({"roles": {"design": {"model": "gpt-6.1-terra"}}})  # terra reaches A nowhere; its top allowed effort is high
        code, r = self.route("design", host="codex")
        self.assertEqual((code, r["model"], r["effort"]), (0, "gpt-6.1-terra", "high"))
        self.local({"roles": {"design": {"model": "gpt-6.1-terra", "effort": "xhigh"}}})  # an explicit effort is the user's
        code, r = self.route("design", host="codex")
        self.assertEqual((code, r["effort"]), (0, "xhigh"))

    def test_null_max_effort_is_rejected(self):
        self.models_edit(lambda d: d["vendors"]["openai"].update(max_effort=None))
        code, _ = self.route("implement", host="codex")
        self.assertEqual(code, 2)
        with self.assertRaises(ValueError):
            mc.load_config(self.cfg)


if __name__ == "__main__":
    unittest.main()
