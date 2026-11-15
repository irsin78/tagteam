"""Gemini 4 Argon as a fallback reviewer and an explicit-only implementer."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("argon_routes", HERE / "harness-route.py")
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)

REVIEWS = ("plan_review", "review_gate", "review_deep")
OWN = {"claude": "claude", "codex": "openai"}
OTHER = {"claude": "openai", "codex": "claude"}


class ArgonRoutes(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((HERE.parent / "model-bindings.json").read_text(encoding="utf-8"))

    def test_default_reviewers_are_unchanged(self):
        for host in ("claude", "codex"):
            for role in REVIEWS:
                for latency in (None, "detached"):
                    with self.subTest(host=host, role=role, latency=latency):
                        r = routes.resolve(self.data, host, role, author_vendors=[OWN[host]],
                                           author_tier="B", latency=latency)
                        self.assertTrue(r["available"])
                        self.assertEqual(r["vendor"], OTHER[host])
                        self.assertNotIn("launcher_flags", r)

    def test_argon_reviews_when_the_other_vendor_is_exhausted(self):
        for host in ("claude", "codex"):
            for role in REVIEWS:
                with self.subTest(host=host, role=role):
                    r = routes.resolve(self.data, host, role, author_vendors=[OWN[host]], author_tier="B",
                                       budget="exhausted:" + OTHER[host])
                    self.assertEqual((r["worker_id"], r["available"]), ("argon-high", True))
                    self.assertEqual(r["launcher"], ".claude/scripts/agy-run.sh")
                    self.assertEqual(r["sandbox"], "read-only")
                    self.assertEqual(r["launcher_flags"], ["-r"])
                    self.assertTrue(r["needs_detached"])
                    self.assertIn("read-only plan mode (-r)", r["announce"])
                    self.assertIn("declared-exhausted", r["announce"])

    def test_requested_latency_is_not_widened(self):
        r = routes.resolve(self.data, "claude", "review_deep", author_vendors=["claude"], author_tier="B",
                           budget="exhausted:openai", latency="foreground")
        self.assertFalse(r["available"])
        self.assertEqual(r["vendor"], "openai")

    def test_google_author_excludes_argon(self):
        for host in ("claude", "codex"):
            r = routes.resolve(self.data, host, "review_deep", author_vendors=[OWN[host], "google"],
                               author_tier="B", budget="exhausted:" + OTHER[host])
            self.assertFalse(r["available"])
            self.assertNotEqual(r["vendor"], "google")

    def test_exhausted_google_leaves_no_review(self):
        r = routes.resolve(self.data, "claude", "review_gate", author_vendors=["claude"], author_tier="B",
                           budget="exhausted:openai,google")
        self.assertFalse(r["available"])

    def test_implementation_needs_an_explicit_choice(self):
        for host in ("claude", "codex"):
            for preference in ("same", "other"):
                self.data["vendor_precedence"]["implementation_default"] = preference
                for budget in ("normal", "exhausted:" + OWN[host], "exhausted:openai,claude"):
                    for latency in ("foreground", "detached"):
                        with self.subTest(host=host, preference=preference, budget=budget, latency=latency):
                            try:
                                r = routes.resolve(self.data, host, "implement", budget=budget, latency=latency)
                            except IndexError:
                                continue
                            self.assertNotEqual(r["worker_id"], "argon-high")
            self.data["vendor_precedence"]["implementation_default"] = "same"
            for options in ({"vendor": "google"}, {"worker": "argon-high"}):
                with self.subTest(host=host, options=options):
                    r = routes.resolve(self.data, host, "implement", **options)
                    self.assertEqual((r["worker_id"], r["available"]), ("argon-high", True))
                    self.assertTrue(r["needs_detached"])
                    self.assertNotIn("launcher_flags", r)
        with self.assertRaises(IndexError):
            routes.resolve(self.data, "claude", "implement", worker="argon-high", latency="foreground")
        # A Google-authored design does not make the explicit-only row the default lane.
        for host in ("claude", "codex"):
            r = routes.resolve(self.data, host, "implement", author_vendors=["google"])
            self.assertTrue(r["available"])
            self.assertNotEqual(r["vendor"], "google")

    def test_role_flags_are_validated(self):
        for flag in ("fallback", "explicit_only"):
            data = json.loads(json.dumps(self.data))
            row = next(w for w in data["workers"] if w.get("id") == "argon-high")
            row["roles"]["review_gate"] = {"priority": 5, flag: False}
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                routes.validate_bindings(data)


if __name__ == "__main__":
    unittest.main()
