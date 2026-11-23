"""Plan gating: a model's min_plan against the vendor's plan (named in models.local.json, or inferred for google)."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import route  # noqa: E402
from test_route import RouteBase  # noqa: E402

CARBON = "gemini-4-carbon"
CARBON_IDS = ["gemini-4-carbon-low", "gemini-4-carbon-medium", "gemini-4-carbon-high"]
PRO_IDS = ["gemini-4-hydrogen-low", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-high", "gemini-4-hydrogen-xhigh",
           "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
           "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"]
FLASH_IDS = ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"]
EXH = {"since": "2020-01-01T00:00:00Z", "evidence": "x", "source": "manual"}

# Frozen from the router before plan gating (tagteam template 038353d: route.py and models.json), fresh Pro-class google list,
# every host, role, author, grade (default, S, B) and orchestrator cap (none, Sonnet low): `host/role/author/grade/cap model:effort:launch`.
# Regenerated for the grading floors S 57.85 / A 49.05 / B 32.5 and B cap 2.5 (b-class-opus): the only changed cells are the claude
# B picks, which moved from claude-sonnet-5-5 high to claude-opus-5-5 medium (Opus medium, cost 2.45, is now grade B and fits the cap);
# before that (floors-ceiling, S 57.85 / A 46.5 / B 32.5) the S picks of openai moved from gpt-6.2-sol high to gpt-6.2-astra high;
# plan gating is unchanged.
FROZEN_PRO_LIST = """
claude-desktop/design/-/-/- claude-fable-5-2:low:session
claude-desktop/design/-/-/cap claude-fable-5-2:low:run
claude-desktop/design/-/S/- claude-fable-5-2:high:session
claude-desktop/design/-/S/cap claude-fable-5-2:high:run
claude-desktop/design/-/B/- claude-opus-5-5:medium:session
claude-desktop/design/-/B/cap claude-opus-5-5:medium:run
claude-desktop/implement/-/-/- claude-opus-5-5:medium:session
claude-desktop/implement/-/-/cap claude-opus-5-5:medium:run
claude-desktop/implement/-/S/- claude-fable-5-2:high:session
claude-desktop/implement/-/S/cap claude-fable-5-2:high:run
claude-desktop/implement/-/B/- claude-opus-5-5:medium:session
claude-desktop/implement/-/B/cap claude-opus-5-5:medium:run
claude-desktop/review/claude/-/- gpt-6.2-sol:medium:run
claude-desktop/review/claude/-/cap gpt-6.2-sol:medium:run
claude-desktop/review/claude/S/- gpt-6.2-astra:high:run
claude-desktop/review/claude/S/cap gpt-6.2-astra:high:run
claude-desktop/review/claude/B/- gpt-6.2-sol:low:run
claude-desktop/review/claude/B/cap gpt-6.2-sol:low:run
claude-desktop/review/openai/-/- claude-fable-5-2:low:session
claude-desktop/review/openai/-/cap claude-fable-5-2:low:run
claude-desktop/review/openai/S/- claude-fable-5-2:high:session
claude-desktop/review/openai/S/cap claude-fable-5-2:high:run
claude-desktop/review/openai/B/- claude-opus-5-5:medium:session
claude-desktop/review/openai/B/cap claude-opus-5-5:medium:run
claude-desktop/review/google/-/- claude-fable-5-2:low:session
claude-desktop/review/google/-/cap claude-fable-5-2:low:run
claude-desktop/review/google/S/- claude-fable-5-2:high:session
claude-desktop/review/google/S/cap claude-fable-5-2:high:run
claude-desktop/review/google/B/- claude-opus-5-5:medium:session
claude-desktop/review/google/B/cap claude-opus-5-5:medium:run
claude-desktop/read/-/-/- claude-haiku-5-5:high:session
claude-desktop/read/-/-/cap claude-sonnet-5-5:low:session
claude-desktop/read/-/S/- claude-fable-5-2:high:session
claude-desktop/read/-/S/cap claude-fable-5-2:high:run
claude-desktop/read/-/B/- claude-opus-5-5:medium:session
claude-desktop/read/-/B/cap claude-opus-5-5:medium:run
claude-desktop/translate/-/-/- gemini-4-hydrogen:medium:run
claude-desktop/translate/-/-/cap gemini-4-hydrogen:medium:run
claude-desktop/translate/-/S/- gpt-6.2-astra:high:run
claude-desktop/translate/-/S/cap gpt-6.2-astra:high:run
claude-desktop/translate/-/B/- gemini-4-hydrogen:medium:run
claude-desktop/translate/-/B/cap gemini-4-hydrogen:medium:run
claude/design/-/-/- claude-fable-5-2:low:run
claude/design/-/-/cap claude-fable-5-2:low:run
claude/design/-/S/- claude-fable-5-2:high:run
claude/design/-/S/cap claude-fable-5-2:high:run
claude/design/-/B/- claude-opus-5-5:medium:run
claude/design/-/B/cap claude-opus-5-5:medium:run
claude/implement/-/-/- claude-opus-5-5:medium:subagent
claude/implement/-/-/cap claude-opus-5-5:medium:subagent
claude/implement/-/S/- claude-fable-5-2:high:run
claude/implement/-/S/cap claude-fable-5-2:high:run
claude/implement/-/B/- claude-opus-5-5:medium:subagent
claude/implement/-/B/cap claude-opus-5-5:medium:subagent
claude/review/claude/-/- gpt-6.2-sol:medium:run
claude/review/claude/-/cap gpt-6.2-sol:medium:run
claude/review/claude/S/- gpt-6.2-astra:high:run
claude/review/claude/S/cap gpt-6.2-astra:high:run
claude/review/claude/B/- gpt-6.2-sol:low:run
claude/review/claude/B/cap gpt-6.2-sol:low:run
claude/review/openai/-/- claude-fable-5-2:low:run
claude/review/openai/-/cap claude-fable-5-2:low:run
claude/review/openai/S/- claude-fable-5-2:high:run
claude/review/openai/S/cap claude-fable-5-2:high:run
claude/review/openai/B/- claude-opus-5-5:medium:run
claude/review/openai/B/cap claude-opus-5-5:medium:run
claude/review/google/-/- claude-fable-5-2:low:run
claude/review/google/-/cap claude-fable-5-2:low:run
claude/review/google/S/- claude-fable-5-2:high:run
claude/review/google/S/cap claude-fable-5-2:high:run
claude/review/google/B/- claude-opus-5-5:medium:run
claude/review/google/B/cap claude-opus-5-5:medium:run
claude/read/-/-/- claude-haiku-5-5:high:subagent
claude/read/-/-/cap claude-haiku-5-5:high:subagent
claude/read/-/S/- claude-fable-5-2:high:run
claude/read/-/S/cap claude-fable-5-2:high:run
claude/read/-/B/- claude-opus-5-5:medium:run
claude/read/-/B/cap claude-opus-5-5:medium:run
claude/translate/-/-/- gemini-4-hydrogen:medium:run
claude/translate/-/-/cap gemini-4-hydrogen:medium:run
claude/translate/-/S/- gpt-6.2-astra:high:run
claude/translate/-/S/cap gpt-6.2-astra:high:run
claude/translate/-/B/- gemini-4-hydrogen:medium:run
claude/translate/-/B/cap gemini-4-hydrogen:medium:run
codex/design/-/-/- gpt-6.2-sol:medium:subagent
codex/design/-/-/cap gpt-6.2-sol:medium:subagent
codex/design/-/S/- gpt-6.2-astra:high:subagent
codex/design/-/S/cap gpt-6.2-astra:high:subagent
codex/design/-/B/- gpt-6.2-sol:low:subagent
codex/design/-/B/cap gpt-6.2-sol:low:subagent
codex/implement/-/-/- gpt-6.2-sol:low:subagent
codex/implement/-/-/cap gpt-6.2-sol:low:subagent
codex/implement/-/S/- gpt-6.2-astra:high:subagent
codex/implement/-/S/cap gpt-6.2-astra:high:subagent
codex/implement/-/B/- gpt-6.2-sol:low:subagent
codex/implement/-/B/cap gpt-6.2-sol:low:subagent
codex/review/claude/-/- gpt-6.2-sol:medium:run
codex/review/claude/-/cap gpt-6.2-sol:medium:run
codex/review/claude/S/- gpt-6.2-astra:high:run
codex/review/claude/S/cap gpt-6.2-astra:high:run
codex/review/claude/B/- gpt-6.2-sol:low:run
codex/review/claude/B/cap gpt-6.2-sol:low:run
codex/review/openai/-/- claude-fable-5-2:low:run
codex/review/openai/-/cap claude-fable-5-2:low:run
codex/review/openai/S/- claude-fable-5-2:high:run
codex/review/openai/S/cap claude-fable-5-2:high:run
codex/review/openai/B/- claude-opus-5-5:medium:run
codex/review/openai/B/cap claude-opus-5-5:medium:run
codex/review/google/-/- gpt-6.2-sol:medium:run
codex/review/google/-/cap gpt-6.2-sol:medium:run
codex/review/google/S/- gpt-6.2-astra:high:run
codex/review/google/S/cap gpt-6.2-astra:high:run
codex/review/google/B/- gpt-6.2-sol:low:run
codex/review/google/B/cap gpt-6.2-sol:low:run
codex/read/-/-/- gpt-6.1-terra:low:subagent
codex/read/-/-/cap gpt-6.1-terra:low:subagent
codex/read/-/S/- gpt-6.2-astra:high:subagent
codex/read/-/S/cap gpt-6.2-astra:high:subagent
codex/read/-/B/- gpt-6.2-sol:low:subagent
codex/read/-/B/cap gpt-6.2-sol:low:subagent
codex/translate/-/-/- gemini-4-hydrogen:medium:run
codex/translate/-/-/cap gemini-4-hydrogen:medium:run
codex/translate/-/S/- gpt-6.2-astra:high:subagent
codex/translate/-/S/cap gpt-6.2-astra:high:subagent
codex/translate/-/B/- gemini-4-hydrogen:medium:run
codex/translate/-/B/cap gemini-4-hydrogen:medium:run
agy/design/-/-/- gemini-4-hydrogen:xhigh:run
agy/design/-/-/cap gemini-4-hydrogen:xhigh:run
agy/design/-/S/- gpt-6.2-astra:high:run
agy/design/-/S/cap gpt-6.2-astra:high:run
agy/design/-/B/- gemini-4-hydrogen:medium:run
agy/design/-/B/cap gemini-4-hydrogen:medium:run
agy/implement/-/-/- gemini-4-hydrogen:medium:run
agy/implement/-/-/cap gemini-4-hydrogen:medium:run
agy/implement/-/S/- gpt-6.2-astra:high:run
agy/implement/-/S/cap gpt-6.2-astra:high:run
agy/implement/-/B/- gemini-4-hydrogen:medium:run
agy/implement/-/B/cap gemini-4-hydrogen:medium:run
agy/review/claude/-/- gemini-4-hydrogen:xhigh:run
agy/review/claude/-/cap gemini-4-hydrogen:xhigh:run
agy/review/claude/S/- gpt-6.2-astra:high:run
agy/review/claude/S/cap gpt-6.2-astra:high:run
agy/review/claude/B/- gemini-4-hydrogen:xhigh:run
agy/review/claude/B/cap gemini-4-hydrogen:xhigh:run
agy/review/openai/-/- gemini-4-hydrogen:xhigh:run
agy/review/openai/-/cap gemini-4-hydrogen:xhigh:run
agy/review/openai/S/- claude-fable-5-2:high:run
agy/review/openai/S/cap claude-fable-5-2:high:run
agy/review/openai/B/- gemini-4-hydrogen:xhigh:run
agy/review/openai/B/cap gemini-4-hydrogen:xhigh:run
agy/review/google/-/- gpt-6.2-sol:medium:run
agy/review/google/-/cap gpt-6.2-sol:medium:run
agy/review/google/S/- gpt-6.2-astra:high:run
agy/review/google/S/cap gpt-6.2-astra:high:run
agy/review/google/B/- gpt-6.2-sol:medium:run
agy/review/google/B/cap gpt-6.2-sol:medium:run
agy/read/-/-/- gemini-3.8-flash:low:run
agy/read/-/-/cap gemini-3.8-flash:low:run
agy/read/-/S/- gpt-6.2-astra:high:run
agy/read/-/S/cap gpt-6.2-astra:high:run
agy/read/-/B/- gemini-4-hydrogen:medium:run
agy/read/-/B/cap gemini-4-hydrogen:medium:run
agy/translate/-/-/- gemini-4-hydrogen:medium:run
agy/translate/-/-/cap gemini-4-hydrogen:medium:run
agy/translate/-/S/- gpt-6.2-astra:high:run
agy/translate/-/S/cap gpt-6.2-astra:high:run
agy/translate/-/B/- gemini-4-hydrogen:medium:run
agy/translate/-/B/cap gemini-4-hydrogen:medium:run
"""

# differences from FROZEN_PRO_LIST with openai's pool exhausted
OPENAI_EXHAUSTED_DIFF = """
claude-desktop/review/claude/-/- gemini-4-hydrogen:xhigh:run
claude-desktop/review/claude/-/cap gemini-4-hydrogen:xhigh:run
claude-desktop/review/claude/S/- exit3
claude-desktop/review/claude/S/cap exit3
claude-desktop/review/claude/B/- gemini-4-hydrogen:medium:run
claude-desktop/review/claude/B/cap gemini-4-hydrogen:medium:run
claude-desktop/translate/-/S/- claude-fable-5-2:high:session
claude-desktop/translate/-/S/cap claude-fable-5-2:high:run
claude/review/claude/-/- gemini-4-hydrogen:xhigh:run
claude/review/claude/-/cap gemini-4-hydrogen:xhigh:run
claude/review/claude/S/- exit3
claude/review/claude/S/cap exit3
claude/review/claude/B/- gemini-4-hydrogen:medium:run
claude/review/claude/B/cap gemini-4-hydrogen:medium:run
claude/translate/-/S/- claude-fable-5-2:high:run
claude/translate/-/S/cap claude-fable-5-2:high:run
codex/design/-/-/- claude-fable-5-2:low:run
codex/design/-/-/cap claude-fable-5-2:low:run
codex/design/-/S/- claude-fable-5-2:high:run
codex/design/-/S/cap claude-fable-5-2:high:run
codex/design/-/B/- claude-opus-5-5:medium:run
codex/design/-/B/cap claude-opus-5-5:medium:run
codex/implement/-/-/- claude-opus-5-5:medium:run
codex/implement/-/-/cap claude-opus-5-5:medium:run
codex/implement/-/S/- claude-fable-5-2:high:run
codex/implement/-/S/cap claude-fable-5-2:high:run
codex/implement/-/B/- claude-opus-5-5:medium:run
codex/implement/-/B/cap claude-opus-5-5:medium:run
codex/review/claude/-/- gemini-4-hydrogen:xhigh:run
codex/review/claude/-/cap gemini-4-hydrogen:xhigh:run
codex/review/claude/S/- exit3
codex/review/claude/S/cap exit3
codex/review/claude/B/- gemini-4-hydrogen:medium:run
codex/review/claude/B/cap gemini-4-hydrogen:medium:run
codex/review/google/-/- claude-fable-5-2:low:run
codex/review/google/-/cap claude-fable-5-2:low:run
codex/review/google/S/- claude-fable-5-2:high:run
codex/review/google/S/cap claude-fable-5-2:high:run
codex/review/google/B/- claude-opus-5-5:medium:run
codex/review/google/B/cap claude-opus-5-5:medium:run
codex/read/-/-/- claude-haiku-5-5:high:run
codex/read/-/-/cap claude-haiku-5-5:high:run
codex/read/-/S/- claude-fable-5-2:high:run
codex/read/-/S/cap claude-fable-5-2:high:run
codex/read/-/B/- claude-opus-5-5:medium:run
codex/read/-/B/cap claude-opus-5-5:medium:run
codex/translate/-/S/- claude-fable-5-2:high:run
codex/translate/-/S/cap claude-fable-5-2:high:run
agy/design/-/S/- claude-fable-5-2:high:run
agy/design/-/S/cap claude-fable-5-2:high:run
agy/implement/-/S/- claude-fable-5-2:high:run
agy/implement/-/S/cap claude-fable-5-2:high:run
agy/review/claude/S/- exit3
agy/review/claude/S/cap exit3
agy/review/google/-/- claude-fable-5-2:low:run
agy/review/google/-/cap claude-fable-5-2:low:run
agy/review/google/S/- claude-fable-5-2:high:run
agy/review/google/S/cap claude-fable-5-2:high:run
agy/review/google/B/- claude-fable-5-2:low:run
agy/review/google/B/cap claude-fable-5-2:low:run
agy/read/-/S/- claude-fable-5-2:high:run
agy/read/-/S/cap claude-fable-5-2:high:run
agy/translate/-/S/- claude-fable-5-2:high:run
agy/translate/-/S/cap claude-fable-5-2:high:run
"""

# differences from FROZEN_PRO_LIST when google is ultra (Carbon offered)
ULTRA_DIFF_TEXT = """
claude-desktop/translate/-/S/- gemini-4-carbon:high:run
claude-desktop/translate/-/S/cap gemini-4-carbon:high:run
claude/translate/-/S/- gemini-4-carbon:high:run
claude/translate/-/S/cap gemini-4-carbon:high:run
codex/translate/-/S/- gemini-4-carbon:high:run
codex/translate/-/S/cap gemini-4-carbon:high:run
agy/design/-/S/- gemini-4-carbon:high:run
agy/design/-/S/cap gemini-4-carbon:high:run
agy/implement/-/S/- gemini-4-carbon:high:run
agy/implement/-/S/cap gemini-4-carbon:high:run
agy/review/claude/S/- gemini-4-carbon:high:run
agy/review/claude/S/cap gemini-4-carbon:high:run
agy/review/openai/S/- gemini-4-carbon:high:run
agy/review/openai/S/cap gemini-4-carbon:high:run
agy/read/-/S/- gemini-4-carbon:high:run
agy/read/-/S/cap gemini-4-carbon:high:run
agy/translate/-/S/- gemini-4-carbon:high:run
agy/translate/-/S/cap gemini-4-carbon:high:run
"""

# differences from FROZEN_PRO_LIST when google is named max with no list (Carbon and Neon offered; Neon heads defaults.google.A and S)
ULTRA_NAMED_DIFF_TEXT = """
claude-desktop/translate/-/S/- gemini-4-neon:high:run
claude-desktop/translate/-/S/cap gemini-4-neon:high:run
claude/translate/-/S/- gemini-4-neon:high:run
claude/translate/-/S/cap gemini-4-neon:high:run
codex/translate/-/S/- gemini-4-neon:high:run
codex/translate/-/S/cap gemini-4-neon:high:run
agy/design/-/-/- gemini-4-neon:medium:run
agy/design/-/-/cap gemini-4-neon:medium:run
agy/design/-/S/- gemini-4-neon:high:run
agy/design/-/S/cap gemini-4-neon:high:run
agy/implement/-/S/- gemini-4-neon:high:run
agy/implement/-/S/cap gemini-4-neon:high:run
agy/review/claude/-/- gemini-4-neon:medium:run
agy/review/claude/-/cap gemini-4-neon:medium:run
agy/review/claude/S/- gemini-4-neon:high:run
agy/review/claude/S/cap gemini-4-neon:high:run
agy/review/claude/B/- gemini-4-neon:medium:run
agy/review/claude/B/cap gemini-4-neon:medium:run
agy/review/openai/-/- gemini-4-neon:medium:run
agy/review/openai/-/cap gemini-4-neon:medium:run
agy/review/openai/S/- gemini-4-neon:high:run
agy/review/openai/S/cap gemini-4-neon:high:run
agy/review/openai/B/- gemini-4-neon:medium:run
agy/review/openai/B/cap gemini-4-neon:medium:run
agy/read/-/S/- gemini-4-neon:high:run
agy/read/-/S/cap gemini-4-neon:high:run
agy/translate/-/S/- gemini-4-neon:high:run
agy/translate/-/S/cap gemini-4-neon:high:run
"""


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PlanBase(RouteBase):
    def models(self):
        with open(os.path.join(os.path.dirname(HERE), "models.json"), encoding="utf-8") as f:
            return json.load(f)

    def table(self, hosts=route.HOSTS):
        """The routes of every host, role, author, grade and orchestrator cell: {cell: (exit code, result)}."""
        out = {}
        for host in hosts:
            for role in route.ROLES:
                for author in ((None,) if role != "review" else ("claude", "openai", "google")):
                    for grade in (None, "S", "A", "B", "C"):
                        extra = ["--grade", grade] if grade else []
                        if author:
                            extra += ["--author-vendor", author] + ([] if grade else ["--grade", "A"])
                        for orch in ((), ("--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "low")):
                            out[(host, role, author, grade, orch)] = self.go("--role", role, "--json", "--host", host, *extra, *orch)
        return out

    def picked(self, table):
        return {r["model"] for code, r in table.values() if code == 0}


class RegistryTest(PlanBase):
    def test_only_carbon_needs_ultra(self):
        with open(os.path.join(os.path.dirname(HERE), "models.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual({mid: m["min_plan"] for mid, m in data["models"].items() if "min_plan" in m}, {CARBON: "ultra"})
        self.assertEqual(route.min_plan(data["models"][CARBON]), "ultra")
        self.assertEqual(route.min_plan(data["models"]["gemini-4-hydrogen"]), "free")

    def test_plan_levels_are_ordered(self):
        lv = route.PLAN_LEVELS
        self.assertTrue(lv["none"] < lv["free"] < lv["pro"] < lv["ultra"])


class PlanNamedTest(PlanBase):
    def router(self, local):
        return route.Router(self.models(), local, {})

    def test_named_plans_and_max_reads_as_ultra(self):
        for named, want in (("none", "none"), ("free", "free"), ("pro", "pro"), ("ultra", "ultra"), ("max", "ultra")):
            self.assertEqual(self.router({"plans": {"google": named, "claude": named}}).plan("google"), want)
            self.assertEqual(self.router({"plans": {"claude": named}}).plan("claude"), want)

    def test_unnamed_plans(self):
        r = self.router({})
        self.assertEqual(r.plan("claude"), "pro")
        self.assertEqual(r.plan("openai"), "pro")
        self.assertEqual(r.plan("google"), "free")  # no fresh list: free, for google only

    def test_plan_below_follows_the_levels(self):
        for named, below in (("free", True), ("pro", True), ("ultra", False), ("max", False)):
            r = self.router({"plans": {"google": named}})
            self.assertIs(r.plan_below(CARBON), below, named)
            self.assertIs(r.usable(CARBON, "high"), not below, named)
            self.assertFalse(r.plan_below("gemini-4-hydrogen"), named)
        self.assertFalse(self.router({"plans": {"google": "none"}}).usable("gemini-4-hydrogen", "high"))

    def test_acceptance_3_a_named_ultra_or_max_offers_carbon_without_a_list(self):
        for named in ("ultra", "max"):
            with self.subTest(named):
                self.local({"plans": {"google": named}})
                code, r = self.go("--role", "design", "--grade", "S", "--json", "--host", "agy")
                # no bench: defaults.google.S puts gemini-4-neon:high before Carbon
                self.assertEqual((code, r["model"], r["effort"]), (0, "gemini-4-neon", "high"))
                self.assertNotIn("plan_free_last:google", r["reason"])
                self.bench({"gemini-4-neon": [self.b("index", 59, "high"), self.b("cost_per_task", 2.0, "high")],
                            CARBON: [self.b("index", 60, "high"), self.b("cost_per_task", 3.0, "high")]})
                code, r = self.go("--role", "design", "--grade", "S", "--json", "--host", "agy")
                self.assertEqual((code, r["model"], r["effort"]), (0, CARBON, "high"))
                os.remove(os.path.join(self.cfg, "bench.local.json"))
        for named in ("pro", "free"):
            with self.subTest(named):
                self.local({"plans": {"google": named}})
                self.assertNotIn(CARBON, self.picked(self.table(("agy", "codex"))))


class InferenceTest(PlanBase):
    def router(self, ids, checked=None, plans=None):
        entry = {"ids": ids}
        if checked is not False:
            entry["checked"] = checked or today()
        local = {"available": {"google": entry}}
        if plans:
            local["plans"] = plans
        return route.Router(self.models(), local, {})

    def test_a_list_with_an_ultra_only_id_is_ultra(self):
        for ids in (CARBON_IDS, PRO_IDS + CARBON_IDS[:1], FLASH_IDS + ["gemini-4-carbon-high"]):
            self.assertEqual(self.router(ids).plan("google"), "ultra", ids)

    def test_a_list_with_a_pro_class_id_and_no_ultra_id_is_pro(self):
        self.assertEqual(self.router(PRO_IDS).plan("google"), "pro")
        self.assertEqual(self.router(FLASH_IDS + ["gemini-pro-orchestrator-neon"]).plan("google"), "pro")

    def test_a_list_with_neither_is_free(self):
        self.assertEqual(self.router(FLASH_IDS).plan("google"), "free")

    def test_no_fresh_list_is_free(self):
        for kw in ({"checked": "2020-01-01"}, {"checked": False}, {"checked": "nope"}):
            self.assertEqual(self.router(PRO_IDS + CARBON_IDS, **kw).plan("google"), "free", kw)
        self.assertEqual(route.Router(self.models(), {}, {}).plan("google"), "free")

    def test_a_fresh_empty_list_is_free(self):
        self.assertEqual(self.router([]).plan("google"), "free")
        self.assertEqual(self.router([], plans={"google": "pro"}).plan("google"), "pro")  # a named plan still wins

    def test_a_named_plan_wins_over_the_list(self):
        self.assertEqual(self.router(CARBON_IDS, plans={"google": "pro"}).plan("google"), "pro")
        self.assertEqual(self.router(FLASH_IDS, plans={"google": "max"}).plan("google"), "ultra")

    def test_a_partner_id_is_no_evidence(self):
        self.assertEqual(self.router(FLASH_IDS + ["claude-opus-5-5-high", "claude-sonnet-5-5-medium"]).plan("google"), "free")

    def test_other_vendors_are_not_inferred(self):
        r = route.Router(self.models(),
                         {"available": {"claude": {"ids": ["claude-haiku-5-5"], "checked": today()},
                                        "openai": {"ids": ["gpt-6-luna"], "checked": today()}}}, {})
        self.assertEqual((r.plan("claude"), r.plan("openai")), ("pro", "pro"))


class CarbonNeverPickedTest(PlanBase):
    """Acceptance 1: no list or a stale list, no named plan: Carbon is not picked on any host, role or exhaustion set."""

    def states(self):
        yield "no file content", {}
        yield "stale list with carbon ids", {"available": {"google": {"checked": "2020-01-01", "ids": PRO_IDS + CARBON_IDS}}}
        yield "undated list with carbon ids", {"available": {"google": {"ids": PRO_IDS + CARBON_IDS}}}
        yield "other vendors listed", {"available": {"claude": {"ids": ["claude-fable-5-2"], "checked": today()}}}
        yield "pro plan", {"plans": {"google": "pro"}}
        yield "pro plan and a carbon list", {"plans": {"google": "pro"}, "available": {"google": {"ids": PRO_IDS + CARBON_IDS}}}

    def exhaustion_sets(self):
        yield ()
        for pool in ("claude", "openai", "google", "google-partner"):
            yield (pool,)
        yield ("claude", "openai")
        yield ("openai", "google")

    def test_never_picked(self):
        for name, local in self.states():
            for pools in self.exhaustion_sets():
                with self.subTest(name, exhausted=pools):
                    self.local(local, stamp=False, plan_default=False)
                    self.exhausted(**{p: dict(EXH, until="2999-01-01T00:00:00Z") for p in pools})
                    self.assertNotIn(CARBON, self.picked(self.table()))

    def test_a_role_pin_of_carbon_falls_back(self):
        self.local({"roles": {"design": {"model": CARBON, "effort": "high"}}}, plan_default=False)
        code, r = self.go("--role", "design", "--json", "--host", "agy", "--grade", "S")
        self.assertEqual(code, 0)
        self.assertNotEqual(r["model"], CARBON)
        self.assertIn("override_unusable", r["reason"])
        self.assertIn("plan_below:" + CARBON, r["reason"])
        self.assertNotIn("user_override", r["reason"])
        self.local({"plans": {"google": "pro"}, "roles": {"design": {"model": CARBON, "effort": "high"}}})
        self.assertIn("override_unusable", self.go("--role", "design", "--json", "--host", "agy")[1]["reason"])
        self.local({"plans": {"google": "ultra"}, "roles": {"design": {"model": CARBON, "effort": "high"}}})
        code, r = self.go("--role", "design", "--json", "--host", "agy")
        self.assertEqual(r["model"], CARBON)
        self.assertIn("user_override", r["reason"])

    def test_a_task_pin_of_carbon_falls_back_as_unavailable(self):
        task = {"tasks": {"deep": {"about": "x", "base": "design", "model": CARBON, "effort": "high"}}}
        self.local(task, plan_default=False)
        code, r = self.go("--task", "deep", "--json", "--host", "agy")
        self.assertEqual(code, 0)
        self.assertFalse(r["task_applied"])
        self.assertIn("task_fallback:unavailable:" + CARBON, r["reason"])
        self.assertNotEqual(r["model"], CARBON)
        self.local(dict(task, plans={"google": "max"}))
        code, r = self.go("--task", "deep", "--json", "--host", "agy")
        self.assertEqual((r["model"], r["task_applied"]), (CARBON, True))


class AcceptanceTwoTest(PlanBase):
    def test_a_list_with_carbon_ids_infers_ultra_and_carbon_can_be_picked(self):
        self.local({"available": {"google": {"ids": PRO_IDS + CARBON_IDS}}})
        code, r = self.go("--role", "design", "--grade", "S", "--json", "--host", "agy")
        self.assertEqual((code, r["model"], r["effort"]), (0, CARBON, "high"))
        self.assertNotIn("plan_free_last:google", r["reason"])
        self.assertIn(CARBON, self.picked(self.table()))

    def test_carbon_stays_unpicked_when_the_list_does_not_offer_it_at_that_effort(self):
        self.local({"available": {"google": {"ids": PRO_IDS + ["gemini-4-carbon-low"]}}})
        self.bench({CARBON: [self.b("index", 54.2, "low"), self.b("cost_per_task", 3.0, "low")]})
        code, r = self.go("--role", "design", "--grade", "A", "--json", "--host", "agy")
        self.assertEqual((code, r["model"], r["effort"]), (0, CARBON, "low"))  # Carbon low is grade A
        code, r = self.go("--role", "design", "--grade", "S", "--json", "--host", "agy")
        self.assertNotEqual(r["model"], CARBON)  # the list offers no S effort of Carbon


class TableUnchangedTest(PlanBase):
    """Acceptance 4: the route table is the pre-change router's, cell for cell (FROZEN_*), wherever the plan is known to be
    pro (fresh Pro-class list, with or without a named pro) or ultra (named max, or a fresh list with Carbon ids)."""

    @staticmethod
    def parse(text):
        return dict(line.rsplit(" ", 1) for line in text.strip().splitlines())

    def grid(self):
        out = {}
        for host in route.HOSTS:
            for role in route.ROLES:
                for author in ((None,) if role != "review" else ("claude", "openai", "google")):
                    for grade in (None, "S", "B"):
                        for cap in (False, True):
                            extra = ["--grade", grade] if grade else []
                            if author:
                                extra += ["--author-vendor", author] + ([] if grade else ["--grade", "A"])
                            if cap:
                                extra += ["--orchestrator-model", "claude-sonnet-5-5", "--orchestrator-effort", "low"]
                            code, r = self.go("--role", role, "--json", "--host", host, *extra)
                            out["%s/%s/%s/%s/%s" % (host, role, author or "-", grade or "-", "cap" if cap else "-")] = \
                                "exit%d" % code if code else "%s:%s:%s" % (r["model"], r["effort"], r["launch"]["kind"])
        return out

    def check(self, local, want, exhausted=()):
        self.local(local, stamp=False, plan_default=False)
        self.exhausted(**{p: dict(EXH, until="2999-01-01T00:00:00Z") for p in exhausted})
        got = self.grid()
        self.assertEqual(len(got), len(want))
        self.assertEqual([(k, got[k]) for k in want if got[k] != want[k]], [])

    def pro(self):
        return self.parse(FROZEN_PRO_LIST)

    def ultra(self):
        return dict(self.pro(), **self.parse(ULTRA_DIFF_TEXT))

    def ultra_named(self):
        """Named ultra with no list: neon is offered too and heads defaults.google.A and S, so Carbon is chosen only by the bench."""
        return dict(self.pro(), **self.parse(ULTRA_NAMED_DIFF_TEXT))

    def test_frozen_tables_are_sound(self):
        self.assertEqual(len(self.pro()), 168)
        self.assertNotIn(CARBON, "".join(self.pro().values()))
        self.assertTrue(all(v.startswith(CARBON) for v in self.parse(ULTRA_DIFF_TEXT).values()))

    def test_fresh_pro_class_list_without_carbon(self):
        self.check({"available": {"google": {"checked": today(), "ids": PRO_IDS}}}, self.pro())

    def test_named_pro_with_the_same_list(self):
        self.check({"plans": {"google": "pro"}, "available": {"google": {"checked": today(), "ids": PRO_IDS}}}, self.pro())

    def test_fresh_pro_class_list_with_openai_exhausted(self):
        self.check({"available": {"google": {"checked": today(), "ids": PRO_IDS}}},
                   dict(self.pro(), **self.parse(OPENAI_EXHAUSTED_DIFF)), exhausted=("openai",))

    def test_named_max_without_a_list_offers_carbon(self):
        self.check({"plans": {"google": "max"}}, self.ultra_named())

    def test_fresh_list_with_carbon_is_ultra(self):
        self.check({"available": {"google": {"checked": today(), "ids": PRO_IDS + CARBON_IDS}}}, self.ultra())

    def test_a_pro_class_list_shows_no_plan_reasons(self):
        """google keeps its place: no `plan_free_last` or `plan_inferred_free` for a Pro-class list."""
        self.local({"available": {"google": {"checked": today(), "ids": PRO_IDS}}}, stamp=False, plan_default=False)
        for host in route.HOSTS:
            r = self.go("--role", "implement", "--json", "--host", host)[1]
            self.assertFalse([x for x in r["reason"] if x.startswith(("plan_free_last", "plan_inferred_free"))], r)

    def test_no_list_and_no_plan_differs_only_by_google_going_last_and_no_carbon(self):
        """Spec 4: unknown google plan is free. Against the pro table only cells that used google or Carbon may change."""
        self.local({}, stamp=False, plan_default=False)
        got, pro = self.grid(), self.pro()
        moved = [k for k in pro if got[k] != pro[k]]
        self.assertTrue(moved)
        for k in moved:
            self.assertTrue("gemini" in pro[k] or "gemini" in got[k], k)
        self.assertNotIn(CARBON, "".join(got.values()))


if __name__ == "__main__":
    unittest.main()
