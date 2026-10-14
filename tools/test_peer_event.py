import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("peer_event", os.path.join(HERE, "peer-event.py"))
peer_event = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer_event)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = peer_event.main(argv)
    return code, out.getvalue(), err.getvalue()


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mission = os.path.join(self.tmp.name, "m")

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_appends_one_json_line_per_call_in_own_session_file(self):
        code, out, _ = run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "send",
                            "--msg", "DESIGN", "--phase", "1", "--lane", "a", "--peer", "lane-a",
                            "--ts", "2026-10-12T08:16:22Z"])
        self.assertEqual(code, 0)
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "note", "--note", "x"])
        path = os.path.join(self.mission, "events", "loop-fable.jsonl")
        with open(path, "rb") as stream:
            lines = stream.read().split(b"\n")
        self.assertEqual(len([line for line in lines if line]), 2)
        self.assertNotIn(b"\r", b"".join(lines))
        first = json.loads(lines[0])
        self.assertEqual(first["ts"], "2026-10-12T08:16:22Z")
        self.assertEqual(first["msg"], "DESIGN")
        self.assertNotIn("head", first, "unset optional fields are omitted")
        self.assertEqual(json.loads(out.strip()), first)

    def test_sessions_write_separate_files_and_names_are_sanitised(self):
        run(["log", "--mission", self.mission, "--session", "lane-a", "--kind", "recv", "--msg", "DESIGN"])
        run(["log", "--mission", self.mission, "--session", "loop fable/x", "--kind", "note"])
        names = sorted(os.listdir(os.path.join(self.mission, "events")))
        self.assertEqual(names, ["lane-a.jsonl", "loop-fable-x.jsonl"])

    def test_default_timestamp_is_utc_z_and_bad_ts_is_usage_error(self):
        code, out, _ = run(["log", "--mission", self.mission, "--session", "s", "--kind", "guard"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ts"].endswith("Z"))
        code, _, err = run(["log", "--mission", self.mission, "--session", "s", "--kind", "guard", "--ts", "yesterday"])
        self.assertEqual(code, 2)
        self.assertIn("ISO 8601", err)

    def test_unknown_kind_and_missing_args_are_usage_errors(self):
        self.assertEqual(run(["log", "--mission", self.mission, "--session", "s", "--kind", "bogus"])[0], 2)
        self.assertEqual(run(["log", "--mission", self.mission])[0], 2)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mission = os.path.join(self.tmp.name, "m")
        events = [
            ("loop-fable", dict(kind="send", msg="DESIGN", phase="1", lane="a", peer="lane-a", ts="2026-10-12T08:16:22Z")),
            ("loop-fable", dict(kind="send", msg="DESIGN", phase="1", lane="b", peer="lane-b", ts="2026-10-12T08:16:22Z")),
            ("lane-a", dict(kind="recv", msg="DESIGN", phase="1", lane="a", ts="2026-10-12T08:16:40Z")),
            ("lane-a", dict(kind="cost", phase="1", lane="a", run="r1", tokens="42751", ts="2026-10-12T08:20:00Z")),
            ("loop-fable", dict(kind="recv", msg="DONE", phase="1", lane="a", peer="lane-a", ts="2026-10-12T08:21:30Z")),
            ("loop-fable", dict(kind="merge", phase="1", lane="a", head="7008a39", ts="2026-10-12T08:21:50Z")),
            ("loop-fable", dict(kind="recv", msg="DONE", phase="1", lane="b", peer="lane-b", ts="2026-10-12T08:22:30Z")),
            ("loop-fable", dict(kind="merge", phase="1", lane="b", head="5ced5e3", ts="2026-10-12T08:22:48Z")),
            ("loop-fable", dict(kind="cost", phase="1", run="r2", tokens="266698", usd="0.5559", ts="2026-10-12T08:26:00Z")),
            ("loop-fable", dict(kind="send", msg="DESIGN", phase="2", lane="b", peer="lane-b", ts="2026-10-12T08:29:53Z")),
            ("loop-fable", dict(kind="send", msg="DESIGN", phase="2", lane="a", peer="lane-a", note="skip: no work", ts="2026-10-12T08:29:53Z")),
            ("loop-fable", dict(kind="recv", msg="DONE", phase="2", lane="b", peer="lane-b", ts="2026-10-12T08:36:58Z")),
        ]
        for session, fields in events:
            argv = ["log", "--mission", self.mission, "--session", session]
            for key, value in fields.items():
                argv += ["--" + key, value]
            self.assertEqual(run(argv)[0], 0, argv)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self):
        code, out, _ = run(["report", "--mission", self.mission, "--json"])
        self.assertEqual(code, 0)
        return json.loads(out)["report"]

    def test_parallel_phase_lane_seconds_span_and_saving(self):
        phase1 = self.report()["phases"][0]
        lanes = {lane["lane"]: lane for lane in phase1["lanes"]}
        self.assertEqual(lanes["a"]["seconds"], 308.0)
        self.assertEqual(lanes["b"]["seconds"], 368.0)
        self.assertEqual(phase1["lane_sum_seconds"], 676.0)
        self.assertEqual(phase1["span_seconds"], 386.0, "first DESIGN send to last merge")
        self.assertEqual(phase1["saved_seconds"], 290.0)

    def test_cost_sums_and_unmeasured_usd_are_counted(self):
        phase1 = self.report()["phases"][0]
        self.assertEqual(phase1["tokens"], 42751 + 266698)
        self.assertEqual(phase1["usd"], 0.5559)
        self.assertEqual(phase1["usd_unmeasured_runs"], 1)

    def test_sequential_phase_without_merge_uses_last_done_and_marks_skipped_lane(self):
        phase2 = self.report()["phases"][1]
        lanes = {lane["lane"]: lane for lane in phase2["lanes"]}
        self.assertEqual(lanes["b"]["seconds"], 425.0)
        self.assertTrue(lanes["a"]["skipped"])
        self.assertIsNone(lanes["a"]["seconds"])
        self.assertEqual(phase2["span_seconds"], 425.0)
        self.assertIsNone(phase2["tokens"])

    def test_markdown_report_lists_messages_and_marks_unmeasured(self):
        code, out, _ = run(["report", "--mission", self.mission])
        self.assertEqual(code, 0)
        self.assertIn("| 2026-10-12T08:16:22Z | loop-fable | send | DESIGN | 1 | a | lane-a |", out)
        self.assertIn("| skipped |", out)
        self.assertIn("| n/a |", out)

    def test_broken_lines_are_skipped_and_counted(self):
        path = os.path.join(self.mission, "events", "lane-b.jsonl")
        with open(path, "ab") as stream:
            stream.write(b"not json\n{\"kind\": \"note\"}\n\xff\xfe\n")
        code, out, _ = run(["report", "--mission", self.mission, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["skipped"], 3)

    def test_reversed_clock_is_flagged_not_negative(self):
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "send", "--msg", "DESIGN",
             "--phase", "3", "--lane", "a", "--ts", "2026-10-13T09:00:00Z"])
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "recv", "--msg", "DONE",
             "--phase", "3", "--lane", "a", "--ts", "2026-10-13T08:59:00Z"])
        phase3 = [row for row in self.report()["phases"] if row["phase"] == "3"][0]
        self.assertIsNone(phase3["lanes"][0]["seconds"])
        self.assertTrue(phase3["lanes"][0]["reversed"])
        self.assertIsNone(phase3["span_seconds"])
        _, out, _ = run(["report", "--mission", self.mission])
        self.assertIn("reversed (clock error)", out)

    def test_eval_events_do_not_extend_the_phase_span(self):
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "eval", "--phase", "2",
             "--ts", "2026-10-13T09:30:00Z"])
        phase2 = [row for row in self.report()["phases"] if row["phase"] == "2"][0]
        self.assertEqual(phase2["span_seconds"], 425.0, "span ends at the last DONE/merge, not at evaluation")

    def test_unmeasured_tokens_are_counted_not_zeroed_and_runs_are_deduplicated(self):
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "cost", "--phase", "2",
             "--run", "r9", "--note", "session tokens unmeasured"])
        run(["log", "--mission", self.mission, "--session", "lane-b", "--kind", "cost", "--phase", "1",
             "--run", "r1", "--tokens", "42751"])  # same run as the seeded r1: counted once
        phases = {row["phase"]: row for row in self.report()["phases"]}
        self.assertIsNone(phases["2"]["tokens"])
        self.assertEqual(phases["2"]["tokens_unmeasured_runs"], 1)
        self.assertEqual(phases["1"]["tokens"], 42751 + 266698)
        self.assertEqual(phases["1"]["cost_runs"], 2)
        _, out, _ = run(["report", "--mission", self.mission])
        self.assertIn("| n/a (+1 unmeasured) |", out)

    def test_saved_seconds_only_for_parallel_phases(self):
        phases = {row["phase"]: row for row in self.report()["phases"]}
        self.assertEqual(phases["1"]["saved_seconds"], 290.0)
        self.assertIsNone(phases["2"]["saved_seconds"], "one measured lane has no serial baseline")

    def test_correction_changes_the_measurement_without_editing_the_original_line(self):
        # the original send was logged late; a correction restates when it happened
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "send", "--msg", "DESIGN",
             "--phase", "3", "--lane", "a", "--msg-id", "m3", "--ts", "2026-10-13T09:10:00Z"])
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "recv", "--msg", "DONE",
             "--phase", "3", "--lane", "a", "--ts", "2026-10-13T09:12:00Z"])
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "correction", "--msg-id", "m3",
             "--at", "2026-10-13T09:09:00Z", "--note", "send: logged 60s late"])
        phase3 = [row for row in self.report()["phases"] if row["phase"] == "3"][0]
        self.assertEqual(phase3["lanes"][0]["seconds"], 180.0, "measured from the corrected occurrence time")
        text = open(os.path.join(self.mission, "events", "loop-fable.jsonl"), encoding="utf-8").read()
        self.assertIn('"ts": "2026-10-13T09:10:00Z"', text, "the original line is untouched")
        self.assertEqual(run(["log", "--mission", self.mission, "--session", "s", "--kind", "correction"])[0], 2,
                         "a correction without --at is a usage error")

    def test_pairs_by_msg_id_flag_missing_and_reversed_receipts(self):
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "send", "--msg", "DESIGN",
             "--phase", "4", "--msg-id", "p1", "--ts", "2026-10-13T10:00:00Z"])
        run(["log", "--mission", self.mission, "--session", "loop-opus", "--kind", "recv", "--msg", "DESIGN",
             "--phase", "4", "--msg-id", "p1", "--ts", "2026-10-13T09:59:30Z"])
        run(["log", "--mission", self.mission, "--session", "loop-opus", "--kind", "send", "--msg", "DONE",
             "--phase", "4", "--msg-id", "p2", "--ts", "2026-10-13T10:05:00Z"])
        pairs = {pair["msg_id"]: pair for pair in self.report()["pairs"]}
        self.assertIn("reversed", pairs["p1"]["problem"])
        self.assertIn("missing", pairs["p2"]["problem"])
        _, out, _ = run(["report", "--mission", self.mission])
        self.assertIn("## Message pairing (msg_id)", out)

    def test_guard_only_phase_is_not_a_measured_phase(self):
        run(["log", "--mission", self.mission, "--session", "loop-fable", "--kind", "guard", "--phase", "0",
             "--note", "active registered"])
        phases = [row["phase"] for row in self.report()["phases"]]
        self.assertEqual(phases, ["1", "2"])

    def test_report_without_events_exits_1(self):
        empty = os.path.join(self.tmp.name, "none")
        code, _, err = run(["report", "--mission", empty])
        self.assertEqual(code, 1)
        self.assertIn("no events", err)


if __name__ == "__main__":
    unittest.main()
