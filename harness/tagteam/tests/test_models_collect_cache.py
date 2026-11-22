"""The machine-wide cache of models_collect.py: freshness, the refresh lock, --from-cache, --weekly."""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TAGTEAM = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, TAGTEAM)
import models_collect as mc  # noqa: E402

KEY = "secret-key-123"
VENDORS = ("claude", "google", "openai")
FIXTURES = {"claude": "claude_model.json", "google": "agy_models.txt", "openai": "codex_models.json"}
# A fake vendor CLI: counts every call (--version too) in a file, says whether it saw the AA key, prints a fixture.
# It also echoes the key, as a careless wrapper might.
STUB = """import os, sys
fixture, count, fail = sys.argv[1], sys.argv[2], sys.argv[3] == "fail"
with open(count, "a") as f:  # every invocation counts, --version included
    f.write("key=%s\\n" % bool(os.environ.get("TAGTEAM_AA_API_KEY")))
if sys.argv[-1] == "--version":
    print("1.0 " + os.environ.get("TAGTEAM_AA_API_KEY", "") + " secret-key-123")
    sys.exit(0)
if fail:
    sys.stderr.write("down secret-key-123\\n")
    sys.exit(1)
sys.stdout.write(open(fixture, encoding="utf-8").read())
"""


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class FakeAA:
    def __init__(self, fail=None, echo=None):
        self.calls, self.fail, self.echo = [], fail, echo  # echo: text a careless server puts in a row and a header

    def __call__(self, page, key, timeout):
        self.calls.append(page)
        if self.fail:
            raise self.fail
        with open(os.path.join(FIX, "aa_page%d.json" % page), encoding="utf-8") as f:
            data = json.load(f)
        if self.echo:
            data["data"][0]["debug"] = "token=" + self.echo
        return data, {"x-ratelimit-remaining": "90" + (" " + self.echo if self.echo else "")}


class CacheBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cfg = os.path.join(self.tmp, "cfg")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg)
        shutil.copy(os.path.join(TAGTEAM, "models.json"), self.cfg)
        self.models = json.loads(read(os.path.join(self.cfg, "models.json")))
        self.stub = os.path.join(self.tmp, "stub.py")
        write(self.stub, STUB)
        self.counts = {v: os.path.join(self.tmp, "count-%s" % v) for v in VENDORS}
        self.failing = set()
        home = mock.patch.dict(os.environ, {"TAGTEAM_HOME": self.home})
        home.start()
        self.addCleanup(home.stop)
        self.aa = FakeAA()

    def cli_env(self):
        return {"TAGTEAM_CLI_" + v.upper(): json.dumps([sys.executable, self.stub, os.path.join(FIX, FIXTURES[v]),
                                                         self.counts[v], "fail" if v in self.failing else "ok"])
                for v in VENDORS}

    def calls(self, vendor):
        path = self.counts[vendor]
        return len(read(path).splitlines()) if os.path.exists(path) else 0

    def per(self, n):
        """Invocations after n successful fetches of every vendor: the listing and the --version call."""
        return {v: 2 * n for v in VENDORS}

    def all_calls(self):
        return {v: self.calls(v) for v in VENDORS}

    def run_main(self, *argv, key="", extra_env=None):
        """mc.main in this process with the fake CLIs and the fake AA; -> (exit code, printed text)."""
        env = dict(self.cli_env(), TAGTEAM_CONFIG_DIR=self.cfg, TAGTEAM_STATE_DIR=self.state, TAGTEAM_AA_API_KEY=key,
                   **(extra_env or {}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def proposal(self):
        return json.loads(read(os.path.join(self.state, "model-update.json")))

    def age(self, item, days):
        path = mc.cache_path(self.home, item)
        data = json.loads(read(path))
        data["fetched"] = mc.iso(mc.utcnow() - timedelta(days=days))
        write(path, json.dumps(data))

    def cache_file(self, item):
        return mc.cache_path(self.home, item)


class FreshnessTest(CacheBase):
    def test_fresh_cache_means_no_network_for_all_three_manual_commands(self):
        code, text = self.run_main(key=KEY)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.all_calls(), self.per(1))
        self.assertEqual(self.aa.calls, [1, 2])
        self.assertIn("cache: claude 0d, google 0d, openai 0d, aa 0d", text)
        self.run_main(key=KEY)
        self.run_main("--write-bench", key=KEY)
        self.run_main("--write-available", key=KEY)
        self.assertEqual(self.all_calls(), self.per(1))
        self.assertEqual(self.aa.calls, [1, 2])

    def test_cache_older_than_six_days_is_fetched_once_more(self):
        self.run_main(key=KEY)
        for item in VENDORS + ("aa",):
            self.age(item, 5)
        self.run_main(key=KEY)
        self.assertEqual((self.all_calls(), self.aa.calls), (self.per(1), [1, 2]))
        for item in VENDORS + ("aa",):
            self.age(item, 7)
        code, text = self.run_main(key=KEY)
        self.assertEqual(self.all_calls(), self.per(2))
        self.assertEqual(self.aa.calls, [1, 2, 1, 2])
        self.assertIn("aa 0d", text)

    def test_only_the_stale_item_is_fetched(self):
        self.run_main(key=KEY)
        self.age("google", 8)
        self.age("aa", 3)
        _, text = self.run_main(key=KEY)
        self.assertEqual(self.all_calls(), {"claude": 2, "google": 4, "openai": 2})
        self.assertEqual(self.aa.calls, [1, 2])
        self.assertIn("aa 3d", text)

    def test_refresh_flag_fetches_even_when_fresh(self):
        self.run_main(key=KEY)
        self.run_main("--refresh", key=KEY)
        self.assertEqual(self.all_calls(), self.per(2))
        self.assertEqual(self.aa.calls, [1, 2, 1, 2])
        self.run_main("--write-available", "--refresh")
        self.assertEqual(self.all_calls(), self.per(3))
        self.run_main("--write-bench", "--refresh", key=KEY)
        self.assertEqual(self.aa.calls, [1, 2, 1, 2, 1, 2])

    def test_a_future_stamp_is_not_fresh(self):
        self.run_main()
        self.age("claude", -3)
        self.run_main()
        self.assertEqual(self.calls("claude"), 4)

    def test_a_failed_fetch_keeps_the_previous_cache(self):
        self.run_main(key=KEY)
        for item in VENDORS + ("aa",):
            self.age(item, 8)
        before = {i: read(self.cache_file(i)) for i in VENDORS + ("aa",)}
        self.failing = {"openai"}
        self.aa.fail = TimeoutError("timed out")
        code, _ = self.run_main(key=KEY)
        self.assertEqual(code, 1)
        prop = self.proposal()
        self.assertEqual((prop["cli"]["openai"]["status"], prop["bench"]["status"]), ("error", "error"))
        self.assertEqual(prop["cli"]["claude"]["status"], "ok")
        self.assertEqual(read(self.cache_file("openai")), before["openai"])
        self.assertEqual(read(self.cache_file("aa")), before["aa"])
        self.assertNotEqual(read(self.cache_file("claude")), before["claude"])  # the others were refreshed

    def test_plan_none_vendor_is_not_fetched(self):
        write(os.path.join(self.cfg, "models.local.json"), json.dumps({"plans": {"openai": "none"}}))
        code, text = self.run_main()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.calls("openai"), 0)
        self.assertEqual(self.proposal()["cli"]["openai"]["status"], "skipped")
        self.assertFalse(os.path.exists(self.cache_file("openai")))
        self.assertIn("cache: claude 0d, google 0d, aa none\n", text)

    def test_cache_files_have_the_documented_shape_and_never_hold_the_key(self):
        self.run_main(key=KEY)
        for vendor in VENDORS:
            data = json.loads(read(self.cache_file(vendor)))
            self.assertEqual((data["schema"], data["version"]), (1, "1.0  ***"))  # the echoed key is redacted
            self.assertRegex(data["fetched"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertIn("listing", data)
        self.assertEqual(json.loads(read(self.cache_file("claude")))["listing"],
                         {"families": ["fable", "haiku", "opus", "sonnet"]})
        aa = json.loads(read(self.cache_file("aa")))
        self.assertEqual((aa["schema"], aa["index_version"], aa["remaining"]), (1, 5, "90"))
        self.assertTrue(aa["rows"])
        for root, _, names in os.walk(self.home):
            for name in names:
                self.assertNotIn(KEY, read(os.path.join(root, name)))
        self.assertFalse([n for n in os.listdir(os.path.join(self.home, "cache")) if ".tmp" in n])

    def test_the_claude_listing_is_project_independent(self):
        raw = {"families": ["opus", "sonnet"]}
        mine = mc.listing_for("claude", raw, self.models)["ids"]
        self.assertIn("claude-opus-5-5", mine)
        self.assertFalse([i for i in mine if "fable" in i or "haiku" in i])
        other = copy.deepcopy(self.models)
        other["models"] = {k: v for k, v in other["models"].items() if "opus" not in k}
        self.assertNotIn("claude-opus-5-5", mc.listing_for("claude", raw, other)["ids"])
        with self.assertRaises(ValueError):
            mc.listing_for("claude", {"families": ["nothing"]}, self.models)

    def test_a_broken_cache_file_counts_as_missing(self):
        self.run_main()
        write(self.cache_file("google"), "{nope")
        self.run_main()
        self.assertEqual(self.calls("google"), 4)
        write(self.cache_file("openai"), json.dumps({"schema": 9, "fetched": mc.iso(), "listing": {}}))
        self.run_main()
        self.assertEqual(self.calls("openai"), 4)


class AAKeyTest(CacheBase):
    def test_a_run_without_key_keeps_aa_json_and_uses_it(self):
        self.run_main(key=KEY)
        before = read(self.cache_file("aa"))
        code, text = self.run_main()
        self.assertEqual(code, 0, text)
        self.assertEqual(read(self.cache_file("aa")), before)
        self.assertEqual(self.aa.calls, [1, 2])
        bench = self.proposal()["bench"]
        self.assertEqual(bench["status"], "ok")
        self.assertEqual(bench["cached_at"], json.loads(before)["fetched"])
        self.age("aa", 30)  # stale, and no key to refresh it: still kept and used
        before = read(self.cache_file("aa"))
        self.run_main("--refresh")
        self.assertEqual(read(self.cache_file("aa")), before)
        self.assertEqual(self.aa.calls, [1, 2])

    def test_no_key_and_no_cache_is_skipped_and_writes_no_aa_json(self):
        code, _ = self.run_main()
        self.assertEqual((code, self.proposal()["bench"]["status"]), (0, "skipped"))
        self.assertFalse(os.path.exists(self.cache_file("aa")))
        self.assertIn("aa none", _)
        self.assertEqual(self.aa.calls, [])

    def test_write_bench_uses_the_cache_without_a_key(self):
        self.run_main(key=KEY)
        code, text = self.run_main("--write-bench")
        self.assertEqual(code, 0, text)
        self.assertIn("wrote bench.local.json", text)
        self.assertEqual(self.aa.calls, [1, 2])
        self.assertIn("cache: aa 0d", text)


class LockTest(CacheBase):
    def setUp(self):
        super().setUp()
        for name, value in (("LOCK_POLL_SECONDS", 0.02), ("LOCK_WAIT_SECONDS", 0.4)):
            patcher = mock.patch.object(mc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetched = []
        self.lock = os.path.join(self.home, "refresh.lock")

    def fake_fetch(self, delay=0.0):
        def fetch(vendor, timeout, home):
            self.fetched.append(vendor)
            time.sleep(delay)
            listing = {"families": ["opus"]} if vendor == "claude" else {"ids": ["a"], "models": {}}
            return {"status": "ok", "version": "1"}, {"version": "1", "listing": listing}
        return mock.patch.object(mc, "fetch_vendor", fetch)

    def refresh(self, force=False, since=None, items=VENDORS, key=""):
        return mc.refresh_caches(self.home, list(items), key, 5, force, since if since is not None else time.time())

    def two_at_once(self, **kw):
        out = []
        threads = [threading.Thread(target=lambda: out.append(self.refresh(**kw))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        return out

    def test_two_refreshes_that_start_together_fetch_once(self):
        with self.fake_fetch(0.3), mock.patch.object(mc, "LOCK_WAIT_SECONDS", 20):
            out = self.two_at_once()
        self.assertEqual(sorted(self.fetched), list(VENDORS))
        self.assertEqual(sorted(len(results) for results, busy in out), [0, 3])
        self.assertFalse(any(busy for _, busy in out))

    def test_two_forced_refreshes_also_fetch_once(self):
        since = time.time()
        with self.fake_fetch(0.3), mock.patch.object(mc, "LOCK_WAIT_SECONDS", 20):
            self.two_at_once(force=True, since=since)
        self.assertEqual(sorted(self.fetched), list(VENDORS))

    def test_two_refreshes_that_both_fail_attempt_each_call_once(self):
        """A failed attempt is recorded: the second run reports it instead of calling the vendor or AA again."""
        self.failing = {"openai"}
        self.aa.fail = mc.urllib.error.HTTPError("u", 429, "x", {"Retry-After": "3600"}, None)
        out = []
        env = dict(self.cli_env(), TAGTEAM_AA_API_KEY=KEY)
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa), \
                mock.patch.object(mc, "LOCK_WAIT_SECONDS", 20):
            since = time.time()
            threads = [threading.Thread(target=lambda: out.append(
                mc.refresh_caches(self.home, list(VENDORS) + ["aa"], KEY, 5, False, since))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(self.aa.calls, [1])  # one request, not one per run
        self.assertEqual(self.all_calls(), {"claude": 2, "google": 2, "openai": 1})  # the failing CLI: one listing call
        self.assertEqual(len(out), 2)
        for results, busy in out:
            self.assertFalse(busy)
            self.assertEqual((results["openai"][0]["status"], results["aa"][0]["status"]), ("error", "error"))
            self.assertIn("Retry-After 3600", results["aa"][0]["detail"])
            self.assertEqual(results["openai"][0]["detail"], "down ***")
        self.assertEqual(sorted(len(r) for r, _ in out), [2, 4])
        attempts = mc.read_attempts(self.home)
        self.assertEqual(sorted(attempts), ["aa", "claude", "google", "openai"])
        self.assertEqual(attempts["aa"]["status"], "error")
        # a run that starts later tries again
        self.aa.fail = None
        self.failing = set()
        env = dict(self.cli_env(), TAGTEAM_AA_API_KEY=KEY)
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa):
            results, _ = mc.refresh_caches(self.home, ["openai", "aa"], KEY, 5, False, time.time() + 1)
        self.assertEqual((results["openai"][0]["status"], results["aa"][0]["status"]), ("ok", "ok"))

    def test_a_forced_refresh_after_an_older_cache_fetches(self):
        with self.fake_fetch():
            self.refresh()
            self.assertEqual(len(self.fetched), 3)
            self.refresh(force=True, since=time.time() + 5)
        self.assertEqual(len(self.fetched), 6)

    def test_a_lock_held_by_someone_else_means_busy_and_nothing_fetched(self):
        with mc.machine_lock(self.home, "refresh.lock") as held, self.fake_fetch():
            self.assertTrue(held)
            long_ago = time.time() - 3600  # however old the file looks, a live holder keeps it
            os.utime(self.lock, (long_ago, long_ago))
            results, busy = self.refresh()
        self.assertEqual((results, busy, self.fetched), ({}, True, []))

    def test_busy_run_reads_the_stale_cache_or_reports_none(self):
        with mc.machine_lock(self.home, "refresh.lock"):
            code, text = self.run_main()
        self.assertEqual(code, 3, text)
        self.assertEqual(self.proposal()["cli"]["claude"], {"status": "error", "detail": "no cache (refresh lock busy)"})
        self.assertEqual(self.all_calls(), self.per(0))
        self.run_main()
        for item in VENDORS:
            self.age(item, 9)
        with mc.machine_lock(self.home, "refresh.lock"):
            code, text = self.run_main()
        self.assertEqual(code, 0, text)  # the old lists are used as they are
        self.assertIn("claude 9d", text)
        self.assertEqual(self.all_calls(), self.per(1))

    def test_a_lock_file_left_by_a_dead_process_blocks_nobody(self):
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        write(self.lock, json.dumps({"pid": done.pid, "at": "2000-01-01T00:00:00Z"}))
        with self.fake_fetch():
            results, busy = self.refresh()
        self.assertEqual((len(results), busy), (3, False))

    def test_the_os_drops_the_lock_when_its_holder_is_killed(self):
        code = ("import sys, time; sys.path.insert(0, %r); import models_collect as mc\n"
                "with mc.machine_lock(sys.argv[1], 'refresh.lock') as held:\n"
                "    print(held, flush=True); time.sleep(60)\n" % TAGTEAM)
        child = subprocess.Popen([sys.executable, "-c", code, self.home], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "True")
        with mc.machine_lock(self.home, "refresh.lock", wait=0.1) as held:
            self.assertFalse(held)  # a live process in another interpreter holds it
        child.kill()
        child.wait()
        with mc.machine_lock(self.home, "refresh.lock", wait=2) as held:
            self.assertTrue(held)  # killed: no cleanup ran, and none is needed

    def test_the_lock_file_is_never_deleted_and_names_are_independent(self):
        with mc.machine_lock(self.home, "refresh.lock") as a, mc.machine_lock(self.home, "registry.lock") as b:
            self.assertEqual((a, b), (True, True))
        self.assertTrue(os.path.exists(self.lock))
        self.assertTrue(os.path.exists(os.path.join(self.home, "registry.lock")))
        with self.assertRaises(RuntimeError):
            with mc.machine_lock(self.home, "refresh.lock"):
                raise RuntimeError("boom")
        with mc.machine_lock(self.home, "refresh.lock", wait=0.1) as held:
            self.assertTrue(held)  # released after the error

    def test_an_unwritable_machine_folder_is_a_failed_refresh(self):
        blocker = os.path.join(self.tmp, "blocker")
        write(blocker, "x")
        with mock.patch.dict(os.environ, {"TAGTEAM_HOME": os.path.join(blocker, "home")}):
            code, text = self.run_main()
        self.assertEqual(code, 3, text)
        for vendor in VENDORS:
            self.assertEqual(self.proposal()["cli"][vendor]["status"], "error")
            self.assertIn("cannot write the %s cache" % vendor, self.proposal()["cli"][vendor]["detail"])

    def test_a_cache_that_cannot_be_written_is_not_ok(self):
        self.run_main()
        for item in VENDORS:
            self.age(item, 9)
        real = mc.write_cache

        def write_cache(home, item, entry):
            if item == "google":
                raise OSError("disk full")
            return real(home, item, entry)
        with mock.patch.object(mc, "write_cache", write_cache), mock.patch.dict(os.environ, self.cli_env()):
            results, _ = mc.refresh_caches(self.home, list(VENDORS), "", 5, False, time.time())
        self.assertEqual(results["claude"][0]["status"], "ok")
        self.assertEqual(results["google"][0]["status"], "error")
        self.assertIn("cannot write the google cache: disk full", results["google"][0]["detail"])
        self.assertIsNone(results["google"][1])


class KeyAndScopeTest(CacheBase):
    def everything_written(self):
        blob = ""
        for folder in (self.home, self.state):
            for root, _, names in os.walk(folder):
                for name in names:
                    blob += read(os.path.join(root, name))
        return blob

    def test_the_key_never_reaches_a_cache_proposal_log_or_output(self):
        self.aa.echo = KEY  # a row field and the rate-limit header both carry it
        self.failing = {"openai"}  # and a failing CLI echoes it on stderr
        code, text = self.run_main(key=KEY)
        self.assertEqual(code, 1, text)
        blob = self.everything_written() + text
        self.assertNotIn(KEY, blob)
        aa = json.loads(read(self.cache_file("aa")))
        self.assertEqual(aa["rows"][0]["debug"], "token=***")
        self.assertEqual(aa["remaining"], "90 ***")
        self.assertEqual(json.loads(read(self.cache_file("claude")))["version"], "1.0  ***")  # --version echoed it
        self.assertEqual(self.proposal()["cli"]["openai"]["detail"], "down ***")
        self.assertIn("***", read(os.path.join(self.state, "collect.log")) + blob)

    def test_no_cli_child_sees_the_key(self):
        self.run_main(key=KEY)
        for vendor in VENDORS:
            lines = read(self.counts[vendor]).splitlines()
            self.assertEqual(lines, ["key=False", "key=False"], vendor)  # the listing and the --version call

    def test_the_key_is_redacted_from_every_stream(self):
        with mock.patch.dict(os.environ, {"TAGTEAM_AA_API_KEY": KEY}), contextlib.redirect_stderr(io.StringIO()) as err:
            mc.warn("rejected key " + KEY)
        self.assertEqual(err.getvalue(), "models_collect: rejected key ***\n")
        path = os.path.join(self.tmp, "x.json")
        with mock.patch.dict(os.environ, {"TAGTEAM_AA_API_KEY": KEY}):
            mc.write_json(path, {"a": ["x " + KEY], KEY: 1})
        self.assertNotIn(KEY, read(path))

    def test_the_cli_name_comes_from_the_machine_not_from_a_project(self):
        self.models["vendors"]["openai"]["cli"] = "project-wrapper"  # what a project's models.json could say
        write(os.path.join(self.cfg, "models.json"), json.dumps(self.models))
        seen = []

        def fake(cmd, timeout, cwd=None, env=None):
            seen.append((cmd[0], cwd))
            return 0, read(os.path.join(FIX, "codex_models.json")), ""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TAGTEAM_CLI_")}
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(mc, "run_cli", side_effect=fake), \
                mock.patch.object(mc.shutil, "which", side_effect=lambda name: "/bin/" + name):
            status, _ = mc.fetch_vendor("openai", 5, self.home)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(seen, [("/bin/codex", self.home), ("/bin/codex", self.home)])  # listing, --version
        self.assertEqual(mc.fetch_vendor.__code__.co_varnames[:3], ("vendor", "timeout", "home"))  # no models.json in

    def test_the_machine_folder_is_made_absolute(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        with mock.patch.dict(os.environ, {"TAGTEAM_HOME": "rel-home"}):
            self.assertEqual(mc.tagteam_home(), os.path.join(os.getcwd(), "rel-home"))


class FromCacheTest(CacheBase):
    def test_from_cache_writes_the_same_proposal_without_any_network(self):
        self.run_main(key=KEY)
        online = self.proposal()
        os.remove(os.path.join(self.state, "model-update.json"))
        self.failing = set(VENDORS)  # any CLI call would fail
        self.aa.fail = AssertionError("network")
        before = (self.all_calls(), list(self.aa.calls))
        with mock.patch.object(mc, "fetch_vendor", side_effect=AssertionError("network")):
            code, text = self.run_main("--from-cache", key=KEY)
        self.assertEqual(code, 0, text)
        self.assertEqual((self.all_calls(), list(self.aa.calls)), before)
        cached = self.proposal()
        self.assertEqual(cached["bench"].pop("cached_at"), json.loads(read(self.cache_file("aa")))["fetched"])
        for prop in (online, cached):
            prop.pop("created")
        self.assertEqual(cached["digest"], online["digest"])
        self.assertEqual(cached, online)

    def test_from_cache_with_no_cache_reports_no_cache(self):
        code, text = self.run_main("--from-cache")
        self.assertEqual(code, 3, text)
        prop = self.proposal()
        for vendor in VENDORS:
            self.assertEqual(prop["cli"][vendor], {"status": "error", "detail": "no cache"})
        self.assertEqual(prop["bench"]["status"], "skipped")
        self.assertEqual(self.all_calls(), self.per(0))
        self.assertFalse(os.path.exists(self.home) and os.listdir(self.home))

    def test_from_cache_with_a_missing_vendor_cache_is_a_partial_failure(self):
        self.run_main()
        os.remove(self.cache_file("google"))
        code, _ = self.run_main("--from-cache")
        self.assertEqual(code, 1)
        prop = self.proposal()
        self.assertEqual(prop["cli"]["google"]["detail"], "no cache")
        self.assertEqual(prop["cli"]["claude"]["status"], "ok")

    def test_from_cache_uses_stale_caches(self):
        self.run_main(key=KEY)
        for item in VENDORS + ("aa",):
            self.age(item, 40)
        code, _ = self.run_main("--from-cache")
        self.assertEqual(code, 0)
        self.assertEqual(self.proposal()["bench"]["status"], "ok")
        self.assertEqual(self.all_calls(), self.per(1))

    def test_usage_errors(self):
        for argv in (["--from-cache", "--refresh"], ["--weekly", "--from-cache"], ["--weekly", "--write-bench"]):
            with self.subTest(argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    mc.main(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_mark_applied_and_digest_still_work(self):
        self.run_main()
        digest = self.proposal()["digest"]
        self.assertEqual(self.run_main("--mark-applied")[0], 0)
        self.assertEqual(json.loads(read(os.path.join(self.state, "model-update.applied")))["digest"], digest)


class WeeklyBase(CacheBase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.home, "bin")
        for name in ("models_collect.py", "state.py"):  # exactly what the installer puts beside the weekly job
            os.makedirs(self.bin, exist_ok=True)
            shutil.copy(os.path.join(TAGTEAM, name), self.bin)
        self.projects = [self.make_project("one"), self.make_project("two")]
        self.register(*self.projects)
        self.weekly_env = dict(self.cli_env(), TAGTEAM_AA_API_KEY="")

    def make_project(self, name):
        proj = os.path.join(self.tmp, name)
        folder = os.path.join(proj, ".claude", "tagteam")
        os.makedirs(folder)
        for fname in ("models_collect.py", "state.py", "models.json"):  # no run.py, no role_guard.py
            shutil.copy(os.path.join(TAGTEAM, fname), folder)
        return proj

    def register(self, *paths):
        write(os.path.join(self.home, "projects.json"), json.dumps({"schema": 1, "projects": [
            {"path": p.replace("\\", "/"), "added": "2026-11-23T00:00:00Z"} for p in paths]}))

    def registered(self):
        return json.loads(read(os.path.join(self.home, "projects.json")))["projects"]

    def log(self):
        path = os.path.join(self.home, "weekly.log")
        return read(path) if os.path.exists(path) else ""

    def proposal_of(self, proj):
        return json.loads(read(os.path.join(proj, ".tagteam", "model-update.json")))

    def weekly_process(self, env=None):
        """The real thing: the copy in bin/ run by a fresh Python, in a folder that holds nothing else."""
        full = dict(os.environ, **(env or self.weekly_env))
        for name in ("TAGTEAM_STATE_DIR", "TAGTEAM_CONFIG_DIR"):
            full.pop(name, None)
        return subprocess.run([sys.executable, os.path.join(self.bin, "models_collect.py"), "--weekly"], cwd=self.bin,
                              env=full, capture_output=True, text=True, timeout=120)

class WeeklyTest(WeeklyBase):
    def test_the_bin_copy_runs_without_any_project_file(self):
        self.assertEqual(sorted(os.listdir(self.bin)), ["models_collect.py", "state.py"])
        done = self.weekly_process()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.all_calls(), self.per(1))  # one listing per CLI, not per project
        for proj in self.projects:
            prop = self.proposal_of(proj)
            self.assertEqual(sorted(prop["available"]), list(VENDORS))
            self.assertEqual(prop["project"], proj.replace("\\", "/"))
        self.assertTrue(all(p["last"]["exit"] == 0 for p in self.registered()))
        log = self.log()
        self.assertIn("refresh claude=ok google=ok openai=ok", log)
        self.assertEqual(len(re.findall(r"project \S+ exit=0", log)), 2)
        self.assertIn("weekly: exit=0", done.stdout)
        with mc.machine_lock(self.home, "refresh.lock", wait=0.1) as held:
            self.assertTrue(held)  # released when the run ended

    def test_a_run_refreshes_even_a_fresh_cache(self):
        self.assertEqual(self.weekly_process().returncode, 0)
        self.assertEqual(self.weekly_process().returncode, 0)
        self.assertEqual(self.all_calls(), self.per(2))

    def test_one_run_one_aa_collection_and_both_projects_get_proposals(self):
        env = dict(self.cli_env(), TAGTEAM_AA_API_KEY=KEY, TAGTEAM_STATE_DIR="", TAGTEAM_CONFIG_DIR="")
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa), \
                contextlib.redirect_stdout(out):
            code = mc.main(["--weekly"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.aa.calls, [1, 2])
        self.assertEqual(self.all_calls(), self.per(1))
        for proj in self.projects:
            bench = self.proposal_of(proj)["bench"]
            self.assertEqual(bench["status"], "ok")
            self.assertIn("cached_at", bench)
        self.assertIn("aa=ok", self.log())
        self.assertNotIn(KEY, self.log() + out.getvalue() + read(self.cache_file("aa")))

    def test_without_a_key_weekly_leaves_the_aa_cache_alone(self):
        self.run_main(key=KEY)
        before = read(self.cache_file("aa"))
        self.assertEqual(self.weekly_process().returncode, 0)
        self.assertEqual(read(self.cache_file("aa")), before)
        self.assertEqual(self.proposal_of(self.projects[0])["bench"]["status"], "ok")
        self.assertIn("aa=skipped", self.log())

    def test_a_deleted_project_leaves_the_list_and_is_logged(self):
        no_collector = self.make_project("three")
        os.remove(os.path.join(no_collector, ".claude", "tagteam", "models_collect.py"))
        gone = os.path.join(self.tmp, "gone")
        self.register(self.projects[0], gone, no_collector, self.projects[1])
        shutil.rmtree(self.projects[1])
        done = self.weekly_process()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([p["path"] for p in self.registered()], [self.projects[0].replace("\\", "/")])
        log = self.log()
        for path, reason in ((gone, "folder missing"), (no_collector, "collector missing"),
                             (self.projects[1], "folder missing")):
            self.assertIn("project %s removed from the list: %s" % (path, reason), log)
        self.assertIn("removed=3", done.stdout)

    def test_exit_codes(self):
        self.failing = {"openai"}
        done = self.weekly_process(dict(self.cli_env(), TAGTEAM_AA_API_KEY=""))
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("openai=error", self.log())
        self.assertFalse(os.path.exists(self.cache_file("openai")))
        self.failing = set(VENDORS)
        done = self.weekly_process(dict(self.cli_env(), TAGTEAM_AA_API_KEY=""))
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertTrue(all(p["last"]["exit"] != 0 for p in self.registered()))  # their caches are partial or empty

    def test_a_failing_project_makes_the_run_exit_1(self):
        os.remove(os.path.join(self.projects[0], ".claude", "tagteam", "models.json"))
        done = self.weekly_process()
        self.assertEqual(done.returncode, 1, done.stdout)
        exits = {os.path.basename(p["path"]): p["last"]["exit"] for p in self.registered()}
        self.assertEqual(exits["two"], 0)
        self.assertEqual(exits["one"], 2)
        self.assertRegex(self.log(), r"project \S*one exit=2 ")

    def test_a_project_that_hangs_is_cut_off(self):
        hang = os.path.join(self.projects[0], ".claude", "tagteam", "models_collect.py")
        write(hang, "import time\ntime.sleep(60)\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.weekly_env, TAGTEAM_STATE_DIR="")), \
                mock.patch.object(mc, "PROJECT_TIMEOUT", 1), contextlib.redirect_stdout(out):
            code = mc.main(["--weekly"])
        self.assertEqual(code, 1)
        self.assertIn("timeout after 1s", self.log())
        self.assertEqual({os.path.basename(p["path"]): p["last"]["exit"] for p in self.registered()},
                         {"one": -1, "two": 0})

    def test_unreadable_registry_is_reported_and_left_alone(self):
        path = os.path.join(self.home, "projects.json")
        write(path, "{nope")
        done = self.weekly_process()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(read(path), "{nope")
        self.assertIn("projects.json unreadable", self.log())
        self.assertEqual(self.all_calls(), self.per(1))  # the caches were still refreshed

    def test_no_registry_still_refreshes(self):
        os.remove(os.path.join(self.home, "projects.json"))
        self.assertEqual(self.weekly_process().returncode, 0)
        self.assertEqual(self.all_calls(), self.per(1))

    def test_results_are_merged_into_the_file_as_it_is_then(self):
        a, b, c = self.projects[0], self.projects[1], os.path.join(self.tmp, "added-later")
        self.register(a, b, c)  # c was registered while the run was going
        mc.save_project_results(self.home, {mc.norm_path(a): {"at": "2026-11-23T09:00:00Z", "exit": 0}},
                                {mc.norm_path(b)})
        got = {os.path.basename(p["path"]): p for p in self.registered()}
        self.assertEqual(sorted(got), ["added-later", "one"])
        self.assertEqual(got["one"]["last"], {"at": "2026-11-23T09:00:00Z", "exit": 0})
        self.assertNotIn("last", got["added-later"])
        self.assertEqual(json.loads(read(os.path.join(self.home, "projects.json")))["schema"], 1)


class WeeklyScopeTest(WeeklyBase):
    """The weekly job's children: what they are given and what of their output is kept."""
    def echo_child(self, proj, body):
        write(os.path.join(proj, ".claude", "tagteam", "models_collect.py"), body)

    def run_weekly_here(self, key=""):
        env = dict(self.cli_env(), TAGTEAM_AA_API_KEY=key, TAGTEAM_STATE_DIR="", TAGTEAM_CONFIG_DIR="")
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "aa_fetch_page", self.aa), \
                contextlib.redirect_stdout(out):
            code = mc.main(["--weekly"])
        return code, out.getvalue()

    def test_a_project_child_gets_no_key_and_its_output_is_redacted_in_the_log(self):
        self.echo_child(self.projects[0], "import os\nprint('seen')\n"
                        "print('HASKEY=%s HOME=%s leaked secret-key-123' % (bool(os.environ.get('TAGTEAM_AA_API_KEY')),"
                        " os.environ.get('TAGTEAM_HOME')))\n")
        code, out = self.run_weekly_here(key=KEY)
        self.assertEqual(code, 0, out)
        log = self.log()
        self.assertNotIn(KEY, log + out + read(self.cache_file("aa")))
        self.assertIn("HASKEY=False HOME=%s leaked ***" % self.home, log)

    def test_a_relative_machine_folder_reaches_the_children_as_an_absolute_one(self):
        self.echo_child(self.projects[0], "import os\nprint('HOME=' + os.environ['TAGTEAM_HOME'])\n")
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        with mock.patch.dict(os.environ, {"TAGTEAM_HOME": "home"}):
            code, out = self.run_weekly_here()
        self.assertEqual(code, 0, out)
        self.assertIn("HOME=" + os.path.join(os.getcwd(), "home"), self.log())

    def test_a_cache_that_could_not_be_refreshed_makes_the_run_exit_1(self):
        self.run_weekly_here()
        real = mc.write_cache

        def write_cache(home, item, entry):
            if item == "google":
                raise OSError("disk full")
            return real(home, item, entry)
        with mock.patch.object(mc, "write_cache", write_cache):
            code, out = self.run_weekly_here()
        self.assertEqual(code, 1, out)
        self.assertIn("google=error", out)
        self.assertIn("google: cannot write the google cache: disk full", self.log())
        self.assertTrue(all(p["last"]["exit"] == 0 for p in self.registered()))  # the children read the old cache

    def test_weekly_and_the_installer_exclude_each_other_on_registry_lock(self):
        before = read(os.path.join(self.home, "projects.json"))
        err = io.StringIO()
        with mock.patch.object(mc, "LOCK_WAIT_SECONDS", 0.2), mock.patch.object(mc, "LOCK_POLL_SECONDS", 0.02), \
                contextlib.redirect_stderr(err):
            with mc.machine_lock(self.home, "registry.lock") as held:  # the installer in the middle of a change
                self.assertTrue(held)
                saved = mc.save_project_results(self.home, {mc.norm_path(self.projects[0]): {"at": "x", "exit": 0}}, set())
            self.assertFalse(saved)
            self.assertIn("registry.lock", err.getvalue())
            self.assertEqual(read(os.path.join(self.home, "projects.json")), before)
            self.assertTrue(mc.save_project_results(self.home, {mc.norm_path(self.projects[0]): {"at": "x", "exit": 0}},
                                                    set()))
        self.assertEqual(self.registered()[0]["last"], {"at": "x", "exit": 0})


class VersionLineTest(unittest.TestCase):
    def test_the_installer_can_read_the_collector_schema(self):
        with open(os.path.join(TAGTEAM, "models_collect.py"), encoding="utf-8") as f:
            hits = re.findall(r"^COLLECTOR_SCHEMA = (\d+)$", f.read(), re.M)
        self.assertEqual(hits, ["2"])
        self.assertEqual(mc.COLLECTOR_SCHEMA, 2)

    def test_the_collector_imports_only_the_standard_library_and_state(self):
        with open(os.path.join(TAGTEAM, "models_collect.py"), encoding="utf-8") as f:
            text = f.read()
        mine = set(re.findall(r"^(?:import|from) (\w+)", text, re.M)) | set(re.findall(r"^\s+import (\w+)", text, re.M))
        project_modules = {n[:-3] for n in os.listdir(TAGTEAM) if n.endswith(".py")} - {"models_collect"}
        self.assertEqual(mine & project_modules, {"state"})


if __name__ == "__main__":
    unittest.main()
