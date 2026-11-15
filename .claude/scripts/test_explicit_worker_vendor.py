"""Explicit worker selection must not weaken independent review."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("explicit_routes", HERE / "harness-route.py")
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)

class ExplicitWorkerVendor(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((HERE.parent / "model-bindings.json").read_text(encoding="utf-8"))

    def test_explicit_implementation_vendor_overrides_automatic_preference(self):
        for preference in ("same", "other"):
            self.data["vendor_precedence"]["implementation_default"] = preference
            for host in ("codex", "claude"):
                author = "openai" if host == "codex" else "claude"
                for role in ("implement", "write"):
                    for worker, vendor in (("sol61-high", "openai"), ("opus55-medium", "claude"),
                                           ("terra61-medium", "openai"), ("terra61-xhigh", "openai")):
                        with self.subTest(preference=preference, host=host, role=role, worker=worker):
                            r = routes.resolve(self.data, host, role, worker=worker, latency="detached")
                            self.assertTrue(r["available"])
                            self.assertEqual(r["worker_id"], worker)
                            self.assertEqual(r.get("same_vendor", False), vendor == author)
                            self.assertEqual(r["separation_satisfied"], vendor != author)

    def test_reviews_and_advice_keep_all_author_separation(self):
        for host in ("codex", "claude"):
            for role in ("plan_review", "review_gate", "review_deep", "decide"):
                for worker, vendor in (("sol61-high", "openai"), ("opus55-medium", "claude")):
                    for authors in ([vendor], ["openai", "claude"]):
                        with self.subTest(host=host, role=role, worker=worker, authors=authors):
                            r = routes.resolve(self.data, host, role, worker=worker,
                                               author_vendors=authors, latency="detached")
                            self.assertFalse(r["available"])
                            self.assertFalse(r["separation_satisfied"])
                    other = "claude" if vendor == "openai" else "openai"
                    r = routes.resolve(self.data, host, role, worker=worker,
                                       author_vendors=[other], latency="detached")
                    self.assertTrue(r["available"])
                    self.assertTrue(r["separation_satisfied"])

    def test_explicit_selection_retains_constraints(self):
        for kwargs in ({"worker": "missing"}, {"worker": "haiku55"},
                       {"worker": "sol61-high", "vendor": "openai"},
                       {"worker": "sol61-high", "tier": "B"},
                       {"worker": "terra61-medium", "latency": "interactive"}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, IndexError)):
                routes.resolve(self.data, "codex", "implement", **kwargs)
        for worker, vendor in (("sol61-high", "openai"), ("opus55-medium", "claude")):
            r = routes.resolve(self.data, "codex", "implement", worker=worker,
                               author_vendors=["openai", "claude"], latency="detached")
            self.assertTrue(r["available"])
            self.assertTrue(r["same_vendor"])
            self.assertFalse(r["separation_satisfied"])
            r = routes.resolve(self.data, "codex", "implement", worker=worker,
                               budget="exhausted:" + vendor, latency="detached")
            self.assertFalse(r["available"])
        row = next(w for w in self.data["workers"] if w["id"] == "sol61-high")
        row["effort"] = "ultra"
        with self.assertRaises((ValueError, IndexError)):
            routes.resolve(self.data, "codex", "implement", worker="sol61-high", latency="detached")
        with self.assertRaises((ValueError, IndexError)):
            routes.resolve(self.data, "codex", "explore", worker="sol61-high", latency="detached")

if __name__ == "__main__":
    unittest.main()
