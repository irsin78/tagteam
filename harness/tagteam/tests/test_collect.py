import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
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
        self.home = os.path.join(self.tmp, "home")  # the machine folder; never the real ~/.tagteam
        home = mock.patch.dict(os.environ, {"TAGTEAM_HOME": self.home})
        home.start()
        self.addCleanup(home.stop)
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

    def fresh_cache(self):
        """Each run below asks the fake CLIs and AA itself; the cache is tested in test_models_collect_cache.py."""
        shutil.rmtree(os.path.join(self.home, "cache"), True)

    def collect(self, openai=None, google=None, extra=(), key="", fetch=None, claude=None):
        self.fresh_cache()
        env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state, "TAGTEAM_AA_API_KEY": key,
               "TAGTEAM_CLI_CLAUDE": claude or self.cli(os.path.join(FIX, "claude_model.json")),
               "TAGTEAM_CLI_OPENAI": openai or self.cli(os.path.join(FIX, "codex_models.json")),
               "TAGTEAM_CLI_GOOGLE": google or self.cli(os.path.join(FIX, "agy_models.txt"))}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", fetch or self.no_fetch), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.main(["--project", self.tmp, *extra])
        self.printed = out.getvalue() + err.getvalue()
        return code, (json.loads(read(self.proposal)) if os.path.exists(self.proposal) else None)

    def no_fetch(self, page, key, timeout):
        raise AssertionError("AA must not be requested here")

    def kinds(self, prop):
        return sorted({c["kind"] for c in prop["changes"]})

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
        models["models"]["gpt-9-gone"] = {"vendor": "openai", "grades": {"low": "B"}}
        models["models"]["gpt-9-cand"] = {"vendor": "openai", "grades": {"low": "B"}, "candidate": True}
        models["models"]["gpt-9-off"] = {"vendor": "openai", "grades": {"low": "B"}}
        models["models"]["gpt-6-luna"]["grades"] = {"low": "C", "ultra": "C"}
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

    ORCH = "gemini-pro-orchestrator-g1-pro"

    def agy_listing(self, *extra_lines):
        lines = read(os.path.join(FIX, "agy_models.txt")).rstrip("\n").split("\n") + list(extra_lines)
        return mc.parse_agy("\n".join(lines))

    def test_an_orchestrator_id_is_never_a_new_model(self):
        ids, info = self.agy_listing(self.ORCH + "\tOrchestrator (Argon)")
        self.assertIn(self.ORCH, ids)
        listing = {"ids": ids, "models": info}
        kinds = [(c["kind"], c.get("id") or c.get("model")) for c in mc.compare("google", listing, self.models, {})]
        self.assertNotIn(("new_model", self.ORCH), kinds)
        del self.models["orchestrators"]  # control: without the block the same id is reported
        kinds = [(c["kind"], c.get("id") or c.get("model")) for c in mc.compare("google", listing, self.models, {})]
        self.assertIn(("new_model", self.ORCH), kinds)

    def test_a_full_run_lists_the_orchestrator_as_available_but_proposes_nothing_for_it(self):
        path = os.path.join(self.tmp, "agy_orch.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(read(os.path.join(FIX, "agy_models.txt")).rstrip("\n") + "\n" + self.ORCH + "\tOrchestrator (Argon)\n")
        code, prop = self.collect(google=self.cli(path))
        self.assertEqual(code, 0)
        self.assertIn(self.ORCH, prop["available"]["google"]["ids"])
        self.assertFalse([c for c in prop["changes"] if self.ORCH in (c.get("id"), c.get("model"))])

    def test_orchestrators_block_is_checked_for_shape_only(self):
        bad = {"not an object": [1], "entry not an object": {"o": 5}, "unknown vendor": {"o": {"vendor": "nope", "compare_as": "a:high"}},
               "no compare_as": {"o": {"vendor": "google"}}, "compare_as not a string": {"o": {"vendor": "google", "compare_as": 5}}}
        for name, block in bad.items():
            with self.subTest(name):
                self.models["orchestrators"] = block
                with open(os.path.join(self.cfg, "models.json"), "w", encoding="utf-8") as f:
                    json.dump(self.models, f)
                self.assertEqual(self.collect()[0], 2)
        # the reference itself is route.py's to check: an unknown compare_as target still loads here
        self.models["orchestrators"] = {"o": {"vendor": "google", "compare_as": "nothing:high"}}
        with open(os.path.join(self.cfg, "models.json"), "w", encoding="utf-8") as f:
            json.dump(self.models, f)
        self.assertEqual(self.collect()[0], 0)
        del self.models["orchestrators"]  # the block is optional
        with open(os.path.join(self.cfg, "models.json"), "w", encoding="utf-8") as f:
            json.dump(self.models, f)
        self.assertEqual(self.collect()[0], 0)

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

    def test_missing_error_skipped_and_all_failed(self):
        self.local({"available": {"openai": {"ids": ["gpt-6.1-sol"]}}})
        code, prop = self.collect(openai="[]")
        self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["google"]["status"]), (1, "missing", "ok"))
        err = self.cli(os.path.join(FIX, "agy_error.txt"), code=1, err="not logged in")
        code, prop = self.collect(openai=self.cli(code=2, err="boom\nsecond line"))
        self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["openai"]["detail"]), (1, "error", "boom"))
        self.assertNotIn("openai", prop["available"])
        self.assertFalse([c for c in prop["changes"] if c["vendor"] == "openai"])
        code, prop = self.collect(openai=self.cli(code=2, err="x"), google=err, claude=self.cli(code=1, err="y"))
        self.assertEqual(code, 3)
        self.assertEqual(prop["cli"]["claude"]["status"], "error")
        self.assertEqual(prop["available"], {})
        self.local({"plans": {"openai": "none"}})
        code, prop = self.collect(openai=self.cli(code=2, err="would fail if run"))
        self.assertEqual((code, prop["cli"]["openai"]["status"]), (0, "skipped"))

    def test_claude_model_families(self):
        code, prop = self.collect()
        self.assertEqual((code, prop["cli"]["claude"]["status"]), (0, "ok"))
        self.assertEqual(sorted(prop["available"]["claude"]["ids"]),
                         sorted(m for m, v in self.models["models"].items() if v["vendor"] == "claude"))
        self.assertFalse([c for c in prop["changes"] if c["vendor"] == "claude" and c["kind"] == "new_model"])
        # a plan without fable: fable models are not offered
        text = read(os.path.join(FIX, "claude_model.json")).replace(" fable,", "").replace(" fable[1m],", "")
        path = os.path.join(self.tmp, "claude_nofable.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.local({"available": {"claude": {"ids": ["claude-fable-5-2"]}}})
        _, prop = self.collect(claude=self.cli(path))
        self.assertNotIn("claude-fable-5-2", prop["available"]["claude"]["ids"])
        self.assertIn("claude-opus-5-5", prop["available"]["claude"]["ids"])
        gone = [c for c in prop["changes"] if c["kind"] == "no_longer_available" and c["vendor"] == "claude"]
        self.assertEqual([c["model"] for c in gone], ["claude-fable-5-2"])

    def test_write_available_updates_local_only(self):
        self.local({"plans": {"claude": "max"}, "available": {"google": {"ids": ["old"]}}})
        before = os.listdir(self.state)
        self.collect(google=self.cli(code=1, err="down"), extra=["--write-available"])
        local = json.loads(read(os.path.join(self.cfg, "models.local.json")))
        self.assertEqual(local["plans"], {"claude": "max"})
        self.assertEqual(local["available"]["google"], {"ids": ["old"]})  # failed vendor keeps its old list
        self.assertIn("claude-opus-5-5", local["available"]["claude"]["ids"])
        self.assertIn("gpt-6.1-sol", local["available"]["openai"]["ids"])
        self.assertEqual(os.listdir(self.state), before)  # no proposal written

    def test_claude_aliases_case_and_variants(self):
        models = {"models": {"claude-opus-5-5": {"vendor": "claude"}, "claude-fable-5-2": {"vendor": "claude"},
                             "claude-x-1": {"vendor": "claude", "family": "fable"}}}
        ids, _ = mc.parse_claude(json.dumps({"result": "Available: Opus, fable[1m], default."}), models)
        self.assertEqual(sorted(ids), ["claude-fable-5-2", "claude-opus-5-5", "claude-x-1"])

    def test_claude_lister_runs_outside_the_project(self):
        calls = []

        def fake(cmd, timeout, cwd=None, env=None):
            calls.append({"cmd": cmd, "cwd": cwd, "env": env})
            return 0, read(os.path.join(FIX, "claude_model.json")), ""
        with mock.patch.object(mc, "run_cli", side_effect=fake),                 mock.patch.object(mc, "resolve_cli", return_value=["claude"]):
            with mock.patch.dict(os.environ, {"TAGTEAM_AA_API_KEY": "secret-key-123"}):
                status, entry = mc.fetch_vendor("claude", 5, self.home)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(entry["listing"]["families"], ["fable", "haiku", "opus", "sonnet"])
        listing_call = calls[0]
        self.assertIn("/model", listing_call["cmd"])
        self.assertEqual(listing_call["cwd"], self.home)  # the machine folder, not the project
        self.assertEqual(listing_call["env"]["HARNESS_DELEGATE_RUN"], "1")
        for call in calls:  # the listing and the --version call alike
            self.assertNotIn("TAGTEAM_AA_API_KEY", call["env"])
            self.assertEqual(call["cwd"], self.home)

    def test_claude_model_unexpected_output_is_error(self):
        path = os.path.join(self.tmp, "claude_odd.json")
        for text in ('{"result": "no list here"}', "not json", '{"result": "Available: best, default"}',
                     '{"result": {"message": "unavailable"}}', '[1, 2]'):
            with self.subTest(text):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                code, prop = self.collect(claude=self.cli(path))
                self.assertEqual((code, prop["cli"]["claude"]["status"]), (1, "error"))
                self.assertNotIn("claude", prop["available"])

    def test_empty_output_is_error(self):
        code, prop = self.collect(google=self.cli("-"))
        self.assertEqual((code, prop["cli"]["google"]["status"]), (1, "error"))

    def test_timeout_is_error(self):
        started = time.time()
        code, prop = self.collect(openai=self.cli(delay=30), extra=["--timeout", "1"])
        self.assertLess(time.time() - started, 20)
        self.assertEqual((code, prop["cli"]["openai"]["status"]), (1, "error"))
        self.assertIn("timeout", prop["cli"]["openai"]["detail"])

    def test_schema_and_config_files_untouched(self):
        self.local({"plans": {"google": "free"}})
        before = {n: digest_of(os.path.join(self.cfg, n))
                  for n in ("models.json", "models.local.json")}
        _, prop = self.collect()
        for key in ("schema", "created", "project", "cli", "available", "changes", "digest"):
            self.assertIn(key, prop)
        self.assertRegex(prop["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(sorted(prop["available"]), ["claude", "google", "openai"])
        self.assertEqual(prop["available"]["google"]["checked"], prop["created"][:10])
        self.assertEqual(len(prop["digest"]), 16)
        after = {n: digest_of(os.path.join(self.cfg, n)) for n in before}
        self.assertEqual(before, after)
        self.assertIn("exit=0", read(os.path.join(self.state, "collect.log")))

    def test_unwritable_state_folder_is_a_usage_error_with_the_fix(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        target = os.path.join(blocker, "state")
        env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": target}
        for extra in ([], ["--mark-applied"]):
            err = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
                code = mc.main(["--project", self.tmp, *extra])
            self.assertEqual(code, 2)
            self.assertIn(target, err.getvalue())
            self.assertIn("escalated permissions", err.getvalue())

    def test_state_folder_comes_from_the_project_work_tree(self):
        repo = os.path.join(self.tmp, "proj")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True, capture_output=True)
        err = io.StringIO()
        mc.statelib._cache.clear()
        with mock.patch.dict(os.environ, {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": ""}),                 contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = mc.main(["--project", repo, "--mark-applied"])  # no proposal: exit 2, but the folder is made
        mc.statelib._cache.clear()
        self.assertEqual(code, 2)
        self.assertTrue(os.path.isdir(os.path.join(repo, ".tagteam")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "tagteam")))

    def test_mark_applied_needs_proposal(self):
        self.assertEqual(self.collect(extra=["--mark-applied"])[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "model-update.applied")))

    def test_malformed_codex_payload_is_error_and_others_continue(self):
        for body in ("[]", '{"models": 3}', '{"models": [1]}'):
            with self.subTest(body):
                path = os.path.join(self.tmp, "bad.json")
                with open(path, "w") as f:
                    f.write(body)
                code, prop = self.collect(openai=self.cli(path))
                self.assertEqual((code, prop["cli"]["openai"]["status"], prop["cli"]["google"]["status"]),
                                 (1, "error", "ok"))
                self.assertIn("codex payload", prop["cli"]["openai"]["detail"])

    def test_config_missing_required_keys_is_exit_2(self):
        for body in ("{}", '{"vendors": {}, "models": []}',
                     '{"vendors": {"openai": {"model_arg": "{model}"}}, "models": {"m": {"vendor": "x", "grades": {}}}}', '{"vendors": {"openai": {"model_arg": "{model}"}}, "models": {"m": {"vendor": "openai", "efforts": ["low"]}}}'):
            with self.subTest(body):
                with open(os.path.join(self.cfg, "models.json"), "w") as f:
                    f.write(body)
                self.assertEqual(self.collect()[0], 2)

    def test_broken_config_is_exit_2(self):
        with open(os.path.join(self.cfg, "models.json"), "w") as f:
            f.write("{nope")
        self.assertEqual(self.collect()[0], 2)


AA_KEY = "secret-key-123"


def aa_fixture(n):
    with open(os.path.join(FIX, "aa_page%d.json" % n), encoding="utf-8") as f:
        return json.load(f)


class FakeAA:
    """Stands in for mc.aa_fetch_page: serves pages, records calls."""
    def __init__(self, pages=None, headers=None, fail=None):
        self.pages = pages or [aa_fixture(1), aa_fixture(2)]
        self.headers = headers or {"x-ratelimit-remaining": "96"}
        self.fail, self.calls = fail, []

    def __call__(self, page, key, timeout):
        self.calls.append((page, key, timeout))
        if self.fail:
            raise self.fail
        return copy.deepcopy(self.pages[page - 1]), dict(self.headers)


class BenchTest(CollectTest):
    def setUp(self):
        super().setUp()
        clock = mock.patch.object(mc, "utcnow", return_value=mc.datetime(2099, 1, 10, tzinfo=mc.timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.write_local_bench({
            "claude-fable-5-2": [self.entry("index", 60, "high"), self.entry("cost_per_task", 9.5, "high"),
                                 {"source": "frontierswe", "metric": "pass1", "value": 0.699, "date": "2026-11-10"}],
            "claude-sonnet-5-5": [self.entry("index", 47, "high"), self.entry("cost_per_task", 0.88, "high")],
            "gpt-6.1-terra": [self.entry("index", 45, "xhigh"), self.entry("cost_per_task", 0.19, "xhigh")]})

    @staticmethod
    def entry(metric, value, effort):
        return {"source": "artificial_analysis", "metric": metric, "value": value, "effort": effort,
                "date": "2026-10-01", "version": 5}

    def write_local_bench(self, models):
        with open(os.path.join(self.cfg, "bench.local.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "models": models}, f)

    def local_bench(self):
        return json.loads(read(os.path.join(self.cfg, "bench.local.json")))

    def write_bench(self, **kw):
        """Run --write-bench with a fake AA; -> (exit code, printed text)."""
        key = kw.pop("key", AA_KEY)
        self.fresh_cache()
        out, err = io.StringIO(), io.StringIO()
        env = {"TAGTEAM_CONFIG_DIR": self.cfg, "TAGTEAM_STATE_DIR": self.state, "TAGTEAM_AA_API_KEY": key}
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa(**kw)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.main(["--write-bench"])
        return code, out.getvalue() + err.getvalue()

    def aa(self, **kw):
        self.fake = FakeAA(**kw)
        return self.fake

    def with_meta(self):
        self.models["vendors"]["meta"] = {"cli": "x", "host": None, "model_arg": "{model}", "aa_creator": "Meta"}
        self.models["models"]["muse-wonder"] = {"vendor": "meta", "grades": {"max": "B"}}
        self.write_models()
        stored = self.local_bench()["models"]
        stored["muse-wonder"] = [self.entry("index", 10, "max")]
        self.write_local_bench(stored)

    def write_models(self):
        with open(os.path.join(self.cfg, "models.json"), "w", encoding="utf-8") as f:
            json.dump(self.models, f)

    def run_aa(self, **kw):
        return self.collect(key=AA_KEY, fetch=self.aa(**kw))

    def by(self, prop, kind):
        return [c for c in prop["changes"] if c["kind"] == kind]

    def test_no_key_is_skipped_without_request(self):
        code, prop = self.collect()
        self.assertEqual((code, prop["bench"]["status"]), (0, "skipped"))
        self.assertEqual(prop["bench"]["source"], mc.AA_SOURCE)
        self.assertFalse(self.by(prop, "bench_changed"))

    def test_models_json_has_aa_keys(self):
        self.assertEqual({v: c["aa_creator"] for v, c in self.models["vendors"].items()},
                         {"claude": "Anthropic", "openai": "OpenAI", "google": "Google"})
        self.assertEqual(self.models["aa_new_model_days"], 120)

    def test_pages_and_bench_block(self):
        code, prop = self.run_aa()
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.fake.calls], [1, 2])
        self.assertEqual(self.fake.calls[0][1], AA_KEY)
        self.assertEqual(prop["bench"], {"status": "ok", "index_version": 5, "models_seen": 16, "remaining": "96",
                                         "source": mc.AA_SOURCE})
        self.assertFalse(any("bench" in m for m in self.models["models"].values()))

    def test_bench_changed_exact_match_and_null_ignored(self):
        _, prop = self.run_aa()
        got = {(c["model"], c["effort"], c["metric"]): (c["old"], c["new"]) for c in self.by(prop, "bench_changed")}
        self.assertEqual(got, {
            ("claude-fable-5-2", "high", "index"): (60, 61.3),
            ("claude-fable-5-2", "high", "cost_per_task"): (9.5, 9.1234),
            ("claude-sonnet-5-5", "high", "cost_per_task"): (0.88, 0.9),  # its index is null: ignored
            ("gpt-6.1-terra", "xhigh", "index"): (45, 46.5)})
        self.assertTrue(all(c["index_version"] == 5 for c in self.by(prop, "bench_changed")))

    def test_effort_parsing(self):
        by = {(e["base"], e["effort"]) for e in mc.aa_entries(aa_fixture(1)["data"])}
        self.assertIn(("claude-fable-5-2", "high"), by)    # (High, Default Fallback), suffix stripped
        self.assertIn(("claude-fable-5-2", "max"), by)     # (Max, ...), no suffix
        self.assertIn(("claude-nova-6", "low"), by)
        self.assertFalse([b for b in by if "non-reasoning" in b[0]])  # (Non-reasoning) has no effort
        self.assertEqual(mc.aa_entries([{"name": "X (High)", "slug": "x-max"}])[0]["base"], "x-max")
        self.assertEqual(mc.aa_entries([{"name": "X", "slug": "x-high"}]), [])

    def test_aa_new_model_rules(self):
        _, prop = self.run_aa()
        news = {c["id"]: c for c in self.by(prop, "new_model") if c.get("source") == "artificial_analysis"}
        self.assertEqual(sorted(news), ["claude-nova-6"])  # old, null-index, creator-mismatch, unregistered: none
        nova = news["claude-nova-6"]
        self.assertEqual(nova["aa"], {"low": {"index": 50.0, "cost_per_task": None},
                                      "max": {"index": 70.0, "cost_per_task": 5.0}})
        self.assertEqual((nova["efforts"], nova["label"], nova["release_date"], nova["vendor"]),
                         (["low", "max"], "Claude Nova 6", "2099-01-01", "claude"))
        self.assertEqual(nova["grades"], {"low": "A", "max": "S"})  # index 50 and 70 against floors 48 and 54

    def test_grade_for_uses_floors(self):
        floors = {"S": 54, "A": 48, "B": 30}
        got = [mc.grade_for(v, floors) for v in (54, 53.9, 48, 47.9, 30, 29.9, 0)]
        self.assertEqual(got, ["S", "A", "A", "B", "B", "C", "C"])

    def test_grade_changed_from_local_bench(self):
        # synthetic values: fable high 40 falls to B (table says S); terra xhigh 49 rises to A (table says B);
        # sonnet high 35 stays B; haiku has no score
        self.write_local_bench({
            "claude-fable-5-2": [self.entry("index", 40, "high")],
            "claude-sonnet-5-5": [self.entry("index", 35, "high")],
            "gpt-6.1-terra": [self.entry("index", 49, "xhigh"), self.entry("cost_per_task", 0.2, "xhigh")]})
        _, prop = self.collect()
        got = {(c["model"], c["effort"]): c for c in self.by(prop, "grade_changed")}
        self.assertEqual(sorted(got), [("claude-fable-5-2", "high"), ("gpt-6.1-terra", "xhigh")])
        self.assertEqual({k: got[("claude-fable-5-2", "high")][k] for k in ("kind", "vendor", "old", "new", "index")},
                         {"kind": "grade_changed", "vendor": "claude", "old": "S", "new": "B", "index": 40})
        self.assertEqual((got[("gpt-6.1-terra", "xhigh")]["old"], got[("gpt-6.1-terra", "xhigh")]["new"]), ("B", "A"))
        os.remove(os.path.join(self.cfg, "bench.local.json"))
        _, prop = self.collect()
        self.assertFalse(self.by(prop, "grade_changed"))  # no bench file, no grade changes

    def test_grade_changed_uses_fresh_aa_values(self):
        # stored terra xhigh 45 matches the table (B); the fresh AA value 49 moves it to A
        self.write_local_bench({"gpt-6.1-terra": [self.entry("index", 45, "xhigh")]})
        pages = [aa_fixture(1), aa_fixture(2)]
        for page in pages:
            for row in page["data"]:
                if row["slug"] == "gpt-6-1-terra-xhigh":
                    row["evaluations"]["artificial_analysis_intelligence_index"] = 49.0
        _, prop = self.run_aa(pages=pages)
        got = [(c["model"], c["effort"], c["old"], c["new"], c["index"]) for c in self.by(prop, "grade_changed")]
        self.assertIn(("gpt-6.1-terra", "xhigh", "B", "A", 49.0), got)

    def test_non_list_bench_entry_does_not_crash(self):
        with open(os.path.join(self.cfg, "bench.local.json"), "w", encoding="utf-8") as f:
            json.dump({"models": {"claude-opus-5-5": 5, "gpt-6.1-terra": [self.entry("index", 49, "xhigh")]}}, f)
        code, prop = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual([c["model"] for c in self.by(prop, "grade_changed")], ["gpt-6.1-terra"])

    def test_grade_changed_ignores_efforts_the_model_lacks(self):
        self.write_local_bench({"gpt-6.1-terra": [self.entry("index", 80, "max")]})
        _, prop = self.collect()
        self.assertFalse(self.by(prop, "grade_changed"))

    def test_new_model_days_window(self):
        e = {"creator": "Anthropic", "base": "claude-x", "effort": "max", "name": "X (Max)", "index": 1.0, "cost": 1.0}
        now = mc.datetime(2026, 11, 17, tzinfo=mc.timezone.utc)
        models = copy.deepcopy(self.models)

        def ids(rel):
            return [c["id"] for c in mc.compare_bench("claude", [dict(e, release=rel)], models, 5, now)]
        self.assertEqual(ids("2026-07-20"), ["claude-x"])
        self.assertEqual(ids("2026-07-19"), [])
        self.assertEqual(ids("2026-11-17"), ["claude-x"])
        self.assertEqual(ids("2026-11-18"), [])  # future dates are bad data, not recent
        models["aa_new_model_days"] = 10
        self.assertEqual(ids("2026-07-20"), [])

    def test_prefix_never_matches(self):
        self.with_meta()
        _, prop = self.run_aa()
        self.assertFalse([c for c in self.by(prop, "bench_changed") if c["model"] == "muse-wonder"])
        self.assertIn("muse-wonder-full", [c["id"] for c in self.by(prop, "new_model")])
        # claude-sonnet-5 (old) must not feed claude-sonnet-5-5
        sonnet = [c for c in self.by(prop, "bench_changed") if c["model"] == "claude-sonnet-5-5"]
        self.assertEqual([c["metric"] for c in sonnet], ["cost_per_task"])

    def test_vendor_without_creator_is_skipped(self):
        del self.models["vendors"]["claude"]["aa_creator"]
        self.write_models()
        _, prop = self.run_aa()
        self.assertFalse([c for c in prop["changes"] if c["vendor"] == "claude"
                          and c["kind"] in ("bench_changed", "bench_added", "new_model")])

    def test_aa_slug_override(self):
        e = {"creator": "Anthropic", "base": "other-slug", "effort": "high", "name": "X (High)",
             "index": 70.0, "cost": 1.0, "release": "2020-01-01"}
        models = copy.deepcopy(self.models)
        now = mc.datetime(2026, 11, 17, tzinfo=mc.timezone.utc)
        self.assertEqual(mc.compare_bench("claude", [e], models, 5, now), [])
        models["models"]["claude-fable-5-2"]["aa_slug"] = "other-slug"
        stored = {"claude-fable-5-2": [self.entry("index", 60, "high"), self.entry("cost_per_task", 0.5, "high")]}
        got = mc.compare_bench("claude", [e], models, 5, now, stored)
        self.assertEqual([(c["model"], c["new"]) for c in got], [("claude-fable-5-2", 70.0), ("claude-fable-5-2", 1.0)])

    def test_cli_new_model_merges_with_aa(self):
        del self.models["models"]["gpt-6.1-terra"]
        self.write_models()
        _, prop = self.run_aa()
        terra = [c for c in self.by(prop, "new_model") if c["vendor"] == "openai"]
        self.assertEqual(len(terra), 1)
        self.assertEqual(terra[0]["id"], "gpt-6.1-terra")
        self.assertIn("label", terra[0])
        self.assertEqual(terra[0]["aa"], {"xhigh": {"index": 46.5, "cost_per_task": 0.19}})
        self.assertNotIn("source", terra[0])
        self.assertNotIn("gpt-6-sol", [c["id"] for c in self.by(prop, "new_model")])  # unregistered

    def test_applying_values_leaves_zero_bench_changed(self):
        _, prop = self.run_aa()
        self.assertTrue(self.by(prop, "bench_changed"))
        stored = self.local_bench()["models"]
        for c in self.by(prop, "bench_changed"):
            for b in stored[c["model"]]:
                if (b["source"], b["metric"], b.get("effort")) == ("artificial_analysis", c["metric"], c["effort"]):
                    b["value"] = c["new"]
        for c in self.by(prop, "bench_added"):
            stored.setdefault(c["model"], []).append(self.entry(c["metric"], c["new"], c["effort"]))
        self.write_local_bench(stored)
        again = self.run_aa()[1]
        self.assertFalse(self.by(again, "bench_changed") + self.by(again, "bench_added"))

    def test_bench_added_for_effort_without_stored_value(self):
        _, prop = self.run_aa()
        added = {(c["model"], c["effort"], c["metric"]) for c in self.by(prop, "bench_added")}
        self.assertIn(("claude-fable-5-2", "max", "index"), added)
        self.assertNotIn(("claude-fable-5-2", "high", "index"), added)  # stored
        self.assertTrue(all("old" not in c for c in self.by(prop, "bench_added")))

    def test_missing_local_file_means_no_bench_changes(self):
        os.remove(os.path.join(self.cfg, "bench.local.json"))
        code, prop = self.run_aa()
        self.assertEqual(code, 0)
        self.assertEqual(prop["bench"]["local"], "missing")
        self.assertIn("--write-bench", prop["bench"]["hint"])
        self.assertFalse(self.by(prop, "bench_changed") + self.by(prop, "bench_added"))
        self.assertTrue([c for c in self.by(prop, "new_model") if c.get("source") == "artificial_analysis"])

    def test_write_bench_writes_all_registered_efforts(self):
        os.remove(os.path.join(self.cfg, "bench.local.json"))
        code, text = self.write_bench()
        self.assertEqual(code, 0, text)
        data = self.local_bench()
        self.assertEqual((data["schema"], data["source"], data["index_version"]), (1, mc.AA_SOURCE, 5))
        self.assertRegex(data["fetched"], r"^\d{4}-\d\d-\d\d$")
        got = {(m, b["effort"], b["metric"]): b["value"] for m, bs in data["models"].items() for b in bs}
        self.assertEqual(got[("claude-fable-5-2", "high", "index")], 61.3)
        self.assertEqual(got[("claude-fable-5-2", "high", "cost_per_task")], 9.1234)
        self.assertIn(("claude-fable-5-2", "max", "index"), got)
        self.assertNotIn(("claude-sonnet-5-5", "high", "index"), got)  # null at AA
        self.assertNotIn("claude-nova-6", data["models"])  # not registered
        self.assertTrue(all(b["version"] == 5 and b["source"] == "artificial_analysis"
                            for bs in data["models"].values() for b in bs))
        self.assertEqual([c[0] for c in self.fake.calls], [1, 2])
        self.assertFalse(os.path.exists(self.proposal))
        self.assertFalse([f for f in os.listdir(self.cfg) if ".tmp" in f])
        again = self.run_aa()[1]  # a fresh file leaves nothing to propose
        self.assertFalse(self.by(again, "bench_changed") + self.by(again, "bench_added"))

    def test_build_bench_covers_new_google_models(self):
        entries = [{"creator": "Google", "base": "gemini-4-carbon", "effort": "high", "name": "x", "release": "2026-11-01",
                    "index": 61.0, "cost": 3.0},
                   {"creator": "Google", "base": "gemini-4-hydrogen", "effort": "xhigh", "name": "x", "release": "2026-11-01",
                    "index": 50.0, "cost": 1.0}]
        out = mc.build_bench(self.models, entries, 5, "2099-01-10", {})["models"]
        self.assertEqual({(b["effort"], b["metric"], b["value"]) for b in out["gemini-4-carbon"]},
                         {("high", "index", 61.0), ("high", "cost_per_task", 3.0)})
        self.assertIn(("xhigh", "index", 50.0), {(b["effort"], b["metric"], b["value"]) for b in out["gemini-4-hydrogen"]})

    def test_write_bench_keeps_other_sources_and_replaces_aa(self):
        self.write_bench()
        mine = self.local_bench()["models"]["claude-fable-5-2"]
        self.assertIn({"source": "frontierswe", "metric": "pass1", "value": 0.699, "date": "2026-11-10"}, mine)
        high = [b for b in mine if b.get("effort") == "high" and b["metric"] == "index"]
        self.assertEqual([b["value"] for b in high], [61.3])

    def test_write_bench_without_key_writes_nothing(self):
        os.remove(os.path.join(self.cfg, "bench.local.json"))
        code, text = self.write_bench(key="")
        self.assertEqual(code, 0)
        self.assertIn("TAGTEAM_AA_API_KEY", text)
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "bench.local.json")))
        self.assertEqual(self.fake.calls, [])

    def test_write_bench_aa_error_keeps_file(self):
        before = read(os.path.join(self.cfg, "bench.local.json"))
        code, text = self.write_bench(fail=mc.urllib.error.HTTPError("u", 500, "x", {}, None))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", text)
        self.assertNotIn(AA_KEY, text)
        self.assertEqual(read(os.path.join(self.cfg, "bench.local.json")), before)

    def test_write_bench_refuses_broken_existing_file(self):
        path = os.path.join(self.cfg, "bench.local.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"models": {"x": [{"source": "frontierswe"')
        code, text = self.write_bench()
        self.assertEqual(code, 1)
        self.assertIn("unreadable or invalid", text)
        self.assertEqual(read(path), '{"models": {"x": [{"source": "frontierswe"')

    def test_digest_ignores_remaining_and_models_seen(self):
        _, a = self.run_aa(headers={"x-ratelimit-remaining": "90"})
        pages = [aa_fixture(1), aa_fixture(2)]
        pages[1]["data"].append(dict(pages[1]["data"][-1], slug="extra", name="Extra"))
        _, b = self.run_aa(pages=pages, headers={"x-ratelimit-remaining": "10"})
        self.assertNotEqual((a["bench"]["remaining"], a["bench"]["models_seen"]),
                            (b["bench"]["remaining"], b["bench"]["models_seen"]))
        self.assertEqual(a["digest"], b["digest"])
        pages[0]["intelligence_index_version"] = pages[1]["intelligence_index_version"] = 6
        _, c = self.run_aa(pages=pages)
        self.assertNotEqual(a["digest"], c["digest"])
        self.assertNotEqual(a["digest"], self.collect()[1]["digest"])  # skipped vs ok

    def test_page_cap_is_error(self):
        code, prop = self.run_aa(pages=[aa_fixture(1)] * 10)
        self.assertEqual((code, prop["bench"]["status"]), (1, "error"))
        self.assertIn("has_more", prop["bench"]["detail"])
        self.assertEqual(len(self.fake.calls), 10)
        self.assertFalse(self.by(prop, "bench_changed"))

    def test_http_errors_are_error_and_exit_1(self):
        for exc, text in ((mc.urllib.error.HTTPError("u", 500, "x", {}, None), "HTTP 500"),
                          (mc.urllib.error.HTTPError("u", 429, "x", {"Retry-After": "3600"}, None), "Retry-After 3600"),
                          (TimeoutError("timed out"), "timed out"),
                          (ValueError("bad json"), "bad json")):
            code, prop = self.run_aa(fail=exc)
            self.assertEqual((code, prop["bench"]["status"]), (1, "error"))
            self.assertIn(text, prop["bench"]["detail"])
        code, prop = self.run_aa(pages=[{"data": 3}])
        self.assertEqual((code, prop["bench"]["status"]), (1, "error"))

    def test_cli_all_failed_still_3_with_bench_error(self):
        err = self.cli(os.path.join(FIX, "agy_error.txt"), code=1, err="not logged in")
        code, _ = self.collect(openai=self.cli(code=2, err="x"), google=err, claude=self.cli(code=1, err="y"),
                               key=AA_KEY, fetch=self.aa(fail=ValueError("x")))
        self.assertEqual(code, 3)

    def test_key_never_appears_anywhere(self):
        for kw in ({}, {"fail": ValueError("rejected key " + AA_KEY)}):
            self.run_aa(**kw)
            blob = read(self.proposal) + read(os.path.join(self.state, "collect.log")) + self.printed
            self.assertNotIn(AA_KEY, blob)
        self.assertIn("bench=error", read(os.path.join(self.state, "collect.log")))

        self.run_aa(fail=mc.urllib.error.HTTPError("u", 429, "x", {"Retry-After": AA_KEY}, None))
        self.assertNotIn(AA_KEY, read(self.proposal))
        self.assertIn("Retry-After ?", read(self.proposal))

    def test_malformed_nested_aa_data_is_error(self):
        row = aa_fixture(1)["data"][0]
        for bad in ({"name": 5}, {"evaluations": [1]}, {"model_creator": [1]},
                    {"artificial_analysis_intelligence_index_cost": {"cost_per_task": [1]}}):
            page = {"data": [dict(row, **bad)], "pagination": {"has_more": False}}
            code, prop = self.run_aa(pages=[page])
            self.assertEqual((code, prop["bench"]["status"]), (1, "error"), bad)
        code, prop = self.run_aa(pages=[{"data": [], "pagination": [1]}])
        self.assertEqual((code, prop["bench"]["status"]), (1, "error"))

    def test_merge_keeps_colliding_cli_entries(self):
        a = {"kind": "new_model", "vendor": "openai", "id": "gpt-9.1"}
        b = {"kind": "new_model", "vendor": "openai", "id": "gpt-9-1"}
        aa = {"kind": "new_model", "vendor": "openai", "id": "gpt-9-1", "source": "artificial_analysis",
              "aa": {"high": {}}, "release_date": "2099-01-01", "grades": {"high": "B"}}
        out = mc.merge_new_models([a, b, aa])
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["aa"], out[0]["grades"]), ({"high": {}}, {"high": "B"}))


for _name in [n for n in dir(CollectTest) if n.startswith("test_")]:
    setattr(BenchTest, _name, None)  # run only the bench tests here; the base tests already ran


if __name__ == "__main__":
    unittest.main()
