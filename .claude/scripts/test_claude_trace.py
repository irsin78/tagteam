#!/usr/bin/env python
"""claude-trace.py: result publication isolated from trace formatting."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

SCRIPT = Path(__file__).with_name('claude-trace.py')
SPEC = importlib.util.spec_from_file_location('claude_trace', SCRIPT)
trace_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace_mod)

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "final text",
          "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01}
EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": ""}, {"type": "text", "text": "Starting  the\ntask."}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la", "description": "list"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "total 0\nx"}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.txt"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "1\thello"}], "is_error": True}]}},
    {"type": "system", "subtype": "post_turn_summary", "status_category": "completed", "status_detail": "did it"},
    {"type": "system", "subtype": "task_summary", "detail": "Reading a.txt"},
]


def lines(events):
    return "".join(json.dumps(e) + "\n" for e in events)


class ClaudeTrace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.stream = root / 'stream.jsonl'
        self.trace = root / 'trace.log'
        self.run_json = root / 'run.json'
        self.sentinel = root / 'done'

    def run_converter(self, max_seconds=5.0, poll=0.05):
        return trace_mod.main([str(self.stream), str(self.trace), str(self.run_json), str(self.sentinel),
                               '--max-seconds', str(max_seconds), '--poll', str(poll)])

    def trace_text(self):
        return self.trace.read_text(encoding='utf-8') if self.trace.exists() else ''

    def test_result_event_published_and_tools_traced(self):
        self.stream.write_text(lines(EVENTS + [RESULT, {"type": "system", "subtype": "task_summary", "detail": None}]))
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        text = self.trace_text()
        self.assertIn('assistant: Starting the task.', text)
        self.assertIn('tool_use Bash: ls -la', text)
        self.assertIn('tool_result: total 0 x', text)
        self.assertIn('tool_use Read: a.txt', text)
        self.assertIn('tool_result error: 1 hello', text)
        self.assertIn('summary: did it', text)
        self.assertIn('task: Reading a.txt', text)
        self.assertIn('result: subtype=success is_error=False stop_reason=None', text)
        self.assertIn('converter finished (result written, exit 0)', text)
        self.assertNotIn('init', text.split('converter started')[1].split('assistant:')[0])

    def test_unterminated_last_line_and_last_result_wins(self):
        first = dict(RESULT, result='first')
        body = lines(EVENTS[:2] + [first]) + json.dumps(RESULT)  # no trailing newline
        self.stream.write_text(body)
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8'))['result'], 'final text')

    def test_malformed_events_and_non_json_lines_do_not_block_the_result(self):
        body = ('not json at all\n'
                + json.dumps({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": 7}]}}) + '\n'
                + json.dumps({"type": "assistant", "message": "broken"}) + '\n'
                + json.dumps({"type": "user", "message": {"content": "plain string"}}) + '\n'
                + '[1, 2, 3]\n'
                + lines([RESULT]))
        self.stream.write_text(body)
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        text = self.trace_text()
        self.assertIn('trace-skip: not JSON: not json at all', text)
        self.assertIn('tool_result: 7', text)
        self.assertIn('trace-error: TypeError', text)

    def test_failing_trace_sink_still_publishes_result(self):
        # A directory cannot be opened for append on any platform.
        self.trace.mkdir()
        self.stream.write_text(lines(EVENTS + [RESULT]))
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        self.assertTrue(self.trace.is_dir())

    def test_single_envelope_is_copied_unchanged(self):
        envelope = '{"result":"stub final message","usage":{"input_tokens":10,"output_tokens":5}}\n'
        self.stream.write_text(envelope)
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 0)
        self.assertEqual(self.run_json.read_text(encoding='utf-8'), envelope)
        self.assertIn('single JSON envelope copied', self.trace_text())

    def test_garbage_or_truncated_stream_publishes_nothing(self):
        for body in ('not json\n', lines(EVENTS[:3]), ''):
            with self.subTest(body=body[:12]):
                self.stream.write_text(body)
                if self.run_json.exists():
                    self.run_json.unlink()
                self.sentinel.touch()
                self.assertEqual(self.run_converter(), 0)
                self.assertFalse(self.run_json.exists())

    def test_result_write_failure_exits_2(self):
        self.run_json.mkdir()  # os.replace onto a directory fails
        self.stream.write_text(lines([RESULT]))
        self.sentinel.touch()
        self.assertEqual(self.run_converter(), 2)
        self.assertIn('result write failed', self.trace_text())

    def test_max_seconds_without_sentinel_exits_3_after_draining(self):
        self.stream.write_text(lines(EVENTS[:3] + [RESULT]))
        started = time.monotonic()
        self.assertEqual(self.run_converter(max_seconds=0.3), 3)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        self.assertIn('sentinel never appeared', self.trace_text())

    def test_follows_split_writes_and_drains_after_the_sentinel(self):
        self.stream.write_bytes(b'')
        result_line = json.dumps(RESULT).encode()

        def producer():
            with open(self.stream, 'ab') as handle:
                handle.write(lines(EVENTS[:2]).encode()); handle.flush()
                time.sleep(0.3)
                handle.write(result_line[:20]); handle.flush()
                time.sleep(0.3)
                handle.write(result_line[20:] + b'\n'); handle.flush()
            # The launcher creates the sentinel only after the producer exited.
            self.sentinel.touch()

        thread = threading.Thread(target=producer)
        thread.start()
        code = self.run_converter(max_seconds=10)
        thread.join()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        self.assertIn('assistant: Starting the task.', self.trace_text())

    def test_sentinel_latch_survives_its_removal(self):
        self.stream.write_text(lines([RESULT]))
        self.sentinel.touch()
        original = trace_mod.Converter.read_new
        state = {'reads': 0}

        def read_once(conv):
            state['reads'] += 1
            if state['reads'] == 2 and self.sentinel.exists():
                self.sentinel.unlink()  # removed after the first sighting
            return original(conv)

        trace_mod.Converter.read_new = read_once
        try:
            started = time.monotonic()
            self.assertEqual(self.run_converter(max_seconds=5), 0)
        finally:
            trace_mod.Converter.read_new = original
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)

    def test_persistent_read_failure_after_the_sentinel_exits_4(self):
        # A result was already published; the final drain then cannot read the
        # stream at all. The converter must not pass that off as EOF/exit 0.
        self.stream.write_text(lines([dict(RESULT, result='first')]))
        self.sentinel.touch()
        original = trace_mod.Converter.read_new
        state = {'reads': 0}

        def failing_after_first(conv):
            state['reads'] += 1
            if state['reads'] == 1:
                return original(conv)
            return None

        trace_mod.Converter.read_new = failing_after_first
        try:
            code = self.run_converter(max_seconds=10, poll=0.01)
        finally:
            trace_mod.Converter.read_new = original
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8'))['result'], 'first')
        self.assertIn('stream unreadable after the sentinel', self.trace_text())

    def test_transient_read_failure_still_delivers_the_last_result(self):
        self.stream.write_text(lines([dict(RESULT, result='first')]))
        self.sentinel.touch()
        original = trace_mod.Converter.read_new
        state = {'reads': 0}

        def flaky(conv):
            state['reads'] += 1
            if state['reads'] == 2:
                with open(self.stream, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(RESULT) + '\n')
                return None  # the second read fails once; the data is there
            return original(conv)

        trace_mod.Converter.read_new = flaky
        try:
            code = self.run_converter(max_seconds=10, poll=0.01)
        finally:
            trace_mod.Converter.read_new = original
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8'))['result'], 'final text')
        self.assertIn('stream read recovered after 1 failed reads', self.trace_text())

    def test_read_failures_that_begin_before_the_sentinel_still_end_bounded(self):
        # Every read fails from the start; the sentinel appears during the
        # failures. The converter must latch it and give up with exit 4 within
        # MAX_READ_FAILURES polls, not run on to --max-seconds (exit 3). The
        # stream has several lines, so the single-envelope copy does not apply.
        self.stream.write_text(lines(EVENTS[:2] + [RESULT]))
        original = trace_mod.Converter.read_new
        state = {'reads': 0}

        def always_failing(conv):
            state['reads'] += 1
            if state['reads'] == 3:
                self.sentinel.touch()
            return None

        trace_mod.Converter.read_new = always_failing
        try:
            started = time.monotonic()
            code = self.run_converter(max_seconds=30, poll=0.01)
        finally:
            trace_mod.Converter.read_new = original
        self.assertEqual(code, 4)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertLessEqual(state['reads'], trace_mod.Converter.MAX_READ_FAILURES + 3)
        self.assertFalse(self.run_json.exists())

    def test_expiry_with_a_failing_last_read_still_finishes(self):
        # The first read succeeds (so the loop reaches the deadline through the
        # normal path); every later read, including the one drain read after
        # expiry, fails. The result read first must survive and exit must be 3.
        self.stream.write_text(lines(EVENTS[:2] + [RESULT]))
        original = trace_mod.Converter.read_new
        state = {'reads': 0}

        def fail_after_first(conv):
            state['reads'] += 1
            return original(conv) if state['reads'] == 1 else None

        trace_mod.Converter.read_new = fail_after_first
        try:
            code = self.run_converter(max_seconds=0.2, poll=0.01)
        finally:
            trace_mod.Converter.read_new = original
        self.assertEqual(code, 3)
        self.assertGreater(state['reads'], 1)
        self.assertEqual(json.loads(self.run_json.read_text(encoding='utf-8')), RESULT)
        self.assertIn('converter finished (result written; sentinel never appeared', self.trace_text())

    def test_write_atomic_leaves_no_temp_file_on_failure(self):
        self.run_json.mkdir()
        with self.assertRaises(OSError):
            trace_mod.write_atomic(str(self.run_json), 'x')
        leftovers = [p.name for p in self.run_json.parent.iterdir() if p.name.startswith('run.json.tmp')]
        self.assertEqual(leftovers, [])

    def test_clip_collapses_whitespace_and_cuts(self):
        self.assertEqual(trace_mod.clip('a  b\n\tc'), 'a b c')
        self.assertEqual(len(trace_mod.clip('x' * 500)), 203)
        self.assertTrue(trace_mod.clip('x' * 500).endswith('...'))


if __name__ == '__main__':
    unittest.main()
