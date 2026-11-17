import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TAGTEAM = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, TAGTEAM)
import models_collect as mc  # noqa: E402

STUB = """import sys, time
fixture, code, delay, err = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
if delay:
    time.sleep(delay)
if err:
    sys.stderr.write(err + "\\n")
if fixture != "-":
    sys.stdout.write(open(fixture, encoding="utf-8").read())
sys.exit(code)
"""


def digest_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg, self.state = os.path.join(self.tmp, "cfg"), os.path.join(self.tmp, "state")
        os.makedirs(self.cfg)
        os.makedirs(self.state)
        shutil.copy(os.path.join(TAGTEAM, "models.json"), self.cfg)
        self.stub = os.path.join(self.tmp, "stub.py")
        with open(self.stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        self.models = json.loads(read(os.path.join(self.cfg, "models.json")))
        self.proposal = os.path.join(self.state, "model-update.json")

    def cli(self, fixture="-", code=0, delay=0, err=""):
        return json.dumps([sys.executable, self.stub, fixture, str(code), str(delay), err])

    def local(self, data):
        with open(os.path.join(self.cfg, "models.local.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def collect(self, openai=None, google=None, extra=()):
        env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state,
               "TAGTEAM_CLI_OPENAI": openai or self.cli(os.path.join(FIX, "codex_models.json")),
               "TAGTEAM_CLI_GOOGLE": google or self.cli(os.path.join(FIX, "agy_models.txt"))}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = mc.main(["--project", self.tmp, *extra])
        return code, (json.loads(read(self.proposal)) if os.path.exists(self.proposal) else None)

    def kinds(self, prop):
        return sorted({c["kind"] for c in prop["changes"]})

    # 1
    def test_parsers(self):
        ids, info = mc.parse_codex(read(os.path.join(FIX, "codex_models.json")))
        self.assertNotIn("gpt-reserve", ids)
        self.assertNotIn("codex-auto-review", ids)
        self.assertIn("gpt-6.1-sol", ids)
        self.assertTrue(all("ultra" not in i["efforts"] for i in info.values()))
        self.assertIn("max", info["gpt-6.1-sol"]["efforts"])
        ids, info = mc.parse_agy(read(os.path.join(FIX, "agy_models.txt")))
        self.assertEqual(len(ids), 6)  # banner line ignored
        self.assertEqual(sorted(info["gemini-3.8-flash"]["efforts"]), ["high", "low", "medium"])
        self.assertEqual(info["gemini-3.8-flash"]["label"], "Gemini 3.8 Flash")
        self.assertEqual(info["gpt-oss-120b"]["efforts"], ["medium"])
        self.assertEqual(info["claude-sonnet-4-6"]["efforts"], [])

    # 2
    def test_real_registry_against_real_lists(self):
        code, prop = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(self.kinds(prop), ["now_available"])  # nothing known on this machine yet
        for c in prop["changes"]:
            self.assertNotIn(c["kind"], ("missing_registered", "efforts_dropped", "new_model"))

    def test_each_change_kind(self):
        models = copy.deepcopy(self.models)
        ids, info = mc.parse_codex(read(os.path.join(FIX, "codex_models.json")))
        listing = {"ids": ids, "models": info}
        del models["models"]["gpt-6.1-terra"]
        models["models"]["gpt-9-gone"] = {"vendor": "openai", "grade": "B", "efforts": ["low"]}
        models["models"]["gpt-9-cand"] = {"vendor": "openai", "grade": "B", "efforts": ["low"], "candidate": True}
        models["models"]["gpt-9-off"] = {"vendor": "openai", "grade": "B", "efforts": ["low"]}
        models["models"]["gpt-6-luna"]["efforts"] = ["low", "ultra"]
        local = {"models": {"gpt-9-off": {"enabled": False}},
                 "available": {"openai": {"ids": ["gpt-6.1-sol", "gpt-9-gone"]}}}
        by = {(c["kind"], c.get("model") or c.get("id")): c for c in mc.compare("openai", listing, models, local)}
        self.assertIn(("new_model", "gpt-6.1-terra"), by)
        self.assertEqual(by[("new_model", "gpt-6.1-terra")]["efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertNotIn(("new_model", "gpt-6-sol"), by)  # unregistered on purpose
        self.assertIn(("now_available", "gpt-6-luna"), by)
        self.assertNotIn(("now_available", "gpt-6.1-sol"), by)
        self.assertIn(("no_longer_available", "gpt-9-gone"), by)
        self.assertEqual(by[("efforts_dropped", "gpt-6-luna")]["efforts"], ["ultra"])
        self.assertIn(("missing_registered", "gpt-9-gone"), by)
        self.assertNotIn(("missing_registered", "gpt-9-cand"), by)
        self.assertNotIn(("missing_registered", "gpt-9-off"), by)
        gids, ginfo = mc.parse_agy(read(os.path.join(FIX, "agy_models.txt")))
        gby = {(c["kind"], c.get("model") or c.get("id")) for c in mc.compare(
            "google", {"ids": gids, "models": ginfo}, self.models,
            {"available": {"google": {"ids": ["gemini-4-argon-high"]}}})}
        self.assertIn(("no_longer_available", "gemini-4-argon"), gby)
        self.assertNotIn(("missing_registered", "gemini-4-argon"), gby)  # free-tier lists omit it

    def test_same_proposal_same_digest_and_applied_marker(self):
        _, first = self.collect()
        time.sleep(1.1)
        code, second = self.collect()
        self.assertEqual(first["digest"], second["digest"])
        self.assertNotEqual(first["created"], second["created"])
        self.assertEqual(self.collect(extra=["--mark-applied"])[0], 0)
        applied = json.loads(read(os.path.join(self.state, "model-update.applied")))
        self.assertEqual(applied["digest"], second["digest"])
        self.assertIn("applied_at", applied)
        self.assertEqual(self.collect()[1]["digest"], applied["digest"])

    # 3
    def test_missing_error_skipped_and_all_failed(self):
        self.local({"available": {"openai": {"ids": ["gpt-6.1-sol"]}}})
        code, prop = self.collect(openai="[]")
        self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["google"]["status"]), (1, "missing", "ok"))
        err = self.cli(os.path.join(FIX, "agy_error.txt"), code=1, err="not logged in")
        code, prop = self.collect(openai=self.cli(code=2, err="boom\nsecond line"))
        self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["openai"]["detail"]), (1, "error", "boom"))
        self.assertNotIn("openai", prop["available"])
        self.assertFalse([c for c in prop["changes"] if c["vendor"] == "openai"])
        code, prop = self.collect(openai=self.cli(code=2, err="x"), google=err)
        self.assertEqual(code, 3)
        self.assertEqual(prop["cli"]["claude"]["status"], "unlisted")
        self.assertEqual(prop["available"], {})
        self.local({"plans": {"openai": "none"}})
        code, prop = self.collect(openai=self.cli(code=2, err="would fail if run"))
        self.assertEqual((code, prop["cli"]["openai"]["status"]), (0, "skipped"))

    def test_empty_output_is_error(self):
        code, prop = self.collect(google=self.cli("-"))
        self.assertEqual((code, prop["cli"]["google"]["status"]), (1, "error"))

    # 4
    def test_timeout_is_error(self):
        started = time.time()
        code, prop = self.collect(openai=self.cli(delay=30), extra=["--timeout", "1"])
        self.assertLess(time.time() - started, 20)
        self.assertEqual((code, prop["cli"]["openai"]["status"]), (1, "error"))
        self.assertIn("timeout", prop["cli"]["openai"]["detail"])

    # 5
    def test_schema_and_config_files_untouched(self):
        self.local({"plans": {"google": "free"}})
        before = {n: digest_of(os.path.join(self.cfg, n))
                  for n in ("models.json", "models.local.json")}
        _, prop = self.collect()
        for key in ("schema", "created", "project", "cli", "available", "changes", "digest"):
            self.assertIn(key, prop)
        self.assertRegex(prop["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(sorted(prop["available"]), ["google", "openai"])
        self.assertEqual(prop["available"]["google"]["checked"], prop["created"][:10])
        self.assertEqual(len(prop["digest"]), 16)
        after = {n: digest_of(os.path.join(self.cfg, n)) for n in before}
        self.assertEqual(before, after)
        self.assertIn("exit=0", read(os.path.join(self.state, "collect.log")))

    # 6
    def test_mark_applied_needs_proposal(self):
        self.assertEqual(self.collect(extra=["--mark-applied"])[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "model-update.applied")))

    def test_malformed_codex_payload_is_error_and_others_continue(self):
        for body in ("[]", '{"models": 3}', '{"models": [1]}'):
            path = os.path.join(self.tmp, "bad.json")
            with open(path, "w") as f:
                f.write(body)
            code, prop = self.collect(openai=self.cli(path))
            self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["google"]["status"]),
                             (1, "error", "ok"), body)
            self.assertIn("codex payload", prop["cli"]["openai"]["detail"])

    def test_config_missing_required_keys_is_exit_2(self):
        for body in ("{}", '{"vendors": {}, "models": []}',
                     '{"vendors": {"openai": {"model_arg": "{model}"}}, "models": {"m": {"vendor": "x", "efforts": []}}}'):
            with open(os.path.join(self.cfg, "models.json"), "w") as f:
                f.write(body)
            self.assertEqual(self.collect()[0], 2, body)

    def test_broken_config_is_exit_2(self):
        with open(os.path.join(self.cfg, "models.json"), "w") as f:
            f.write("{nope")
        self.assertEqual(self.collect()[0], 2)


if __name__ == "__main__":
    unittest.main()
