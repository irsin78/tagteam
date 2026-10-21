#!/usr/bin/env python
"""Live trace for claude-run.sh: follow a `claude -p --output-format stream-json
--verbose` stream while it is being written and publish the final result.

    claude-trace.py STREAM TRACE RUN_JSON SENTINEL [--max-seconds N] [--poll S]

STREAM   the file claude's stdout is redirected to (one JSON event per line)
TRACE    human-readable log, appended one line per interesting event and
         flushed at once, so `tail` or a bounded read shows the run live
RUN_JSON the `type == "result"` event object, written atomically (temp file in
         the same directory, then os.replace); the launcher parses it exactly
         as it parsed the `--output-format json` envelope
SENTINEL a path the launcher creates after the claude process has exited

Lifecycle: poll STREAM every --poll seconds. The sentinel is latched the first
time it is seen (removing it later cannot un-latch it); once latched the loop
reads until a read returns nothing, parses the unterminated last line, and
exits. --max-seconds bounds the whole life: on expiry the converter drains
once more and exits 3, so a launcher that was SIGKILLed (no trap runs there)
never leaves it polling forever -- it may outlive such a launcher by at most
that budget. SIGTERM/SIGINT (POSIX) latch the same way and exit 143.

Trace formatting is isolated from the result: every event's trace text is
produced inside its own try/except and written best-effort (a failing trace
sink is counted, never raised), while the result write has its own error
path. Exit codes: 0 done (result or single-envelope copy written, or nothing
to write), 2 the result could not be written, 3 --max-seconds expired, 4 the
stream became unreadable after the sentinel (the drain may be incomplete), 143
signalled. Trace failures never change the exit code; the launcher re-extracts
the result from STREAM on any non-zero exit or missing RUN_JSON.

Envelope fallback: when no result event was seen and the WHOLE input parses
as one JSON value (the `--output-format json` envelope an older CLI or a test
stub prints), that text is copied to RUN_JSON unchanged. Nothing else is ever
promoted to a result.
"""
import argparse
import json
import os
import signal
import sys
import time

CUT = 200
# The one input field worth showing per tool call, in preference order.
DETAIL_KEYS = ("command", "file_path", "pattern", "url", "path", "query", "prompt", "skill")
# System subtypes that carry a human-readable status.
SUMMARY_SUBTYPES = {"post_turn_summary": "status_detail", "task_summary": "detail"}


def clip(value, limit=CUT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def stamp():
    return time.strftime("%H:%M:%SZ", time.gmtime())


class Trace:
    """Append-only sink. Every write is best-effort: an OSError is counted and
    swallowed, including for the diagnostic lines about earlier failures."""

    def __init__(self, path):
        self.path = path
        self.failures = 0

    def line(self, text):
        try:
            with open(self.path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write("%s %s\n" % (stamp(), text))
        except OSError:
            self.failures += 1


def describe(event):
    """Yield the trace lines for one parsed event. May raise on an unexpected
    shape; the caller turns that into a trace-error line."""
    kind = event.get("type")
    if kind == "assistant":
        for block in event["message"]["content"]:
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                yield "assistant: " + clip(block["text"])
            elif block_type == "tool_use":
                inputs = block.get("input") or {}
                key = next((k for k in DETAIL_KEYS if isinstance(inputs.get(k), str) and inputs[k].strip()), None)
                if key is None:
                    key = next((k for k, v in inputs.items() if isinstance(v, str) and v.strip()), None)
                detail = clip(inputs[key]) if key else "-"
                yield "tool_use %s: %s" % (block.get("name"), detail)
    elif kind == "user":
        content = event["message"]["content"]
        if not isinstance(content, list):
            return
        for block in content:
            if block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, list):
                texts = [b.get("text") for b in body if isinstance(b, dict) and isinstance(b.get("text"), str)]
                body = " ".join(texts) if texts else json.dumps(body, ensure_ascii=False)
            elif not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            label = "tool_result error" if block.get("is_error") else "tool_result"
            yield "%s: %s" % (label, clip(body) or "(empty)")
    elif kind == "system":
        field = SUMMARY_SUBTYPES.get(event.get("subtype"))
        if field and isinstance(event.get(field), str) and event[field].strip():
            yield "%s: %s" % ("summary" if field == "status_detail" else "task", clip(event[field]))
    elif kind == "result":
        yield "result: subtype=%s is_error=%s stop_reason=%s" % (
            event.get("subtype"), event.get("is_error"), event.get("stop_reason"))


def write_atomic(path, text):
    """Temp file next to the target, then os.replace. The temp file never
    survives this call: a failed write or replace removes it (the launcher
    also sweeps `<run json>.tmp*` after the converter is gone, for the case
    where the converter was killed between the two steps)."""
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class Converter:
    # Consecutive failed reads tolerated after the sentinel before giving up
    # (exit 4): a transient lock on Windows must not end the drain early, a
    # persistent failure must not pass as EOF.
    MAX_READ_FAILURES = 25

    def __init__(self, stream, trace, run_json, sentinel, poll):
        self.stream = stream
        self.trace = trace
        self.run_json = run_json
        self.sentinel = sentinel
        self.poll = poll
        self.offset = 0
        self.buffer = b""
        self.latched = False
        self.signalled = False
        self.result_seen = False
        self.result_written = False
        self.result_error = None

    # ---- input ----
    def read_new(self):
        """New bytes since the last read; b"" at EOF, None when the stream
        could not be read (a missing or locked file is NOT end of input)."""
        try:
            with open(self.stream, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return None
        self.offset += len(data)
        return data

    def feed(self, data):
        self.buffer += data
        while True:
            cut = self.buffer.find(b"\n")
            if cut < 0:
                return
            line, self.buffer = self.buffer[:cut], self.buffer[cut + 1:]
            self.handle_line(line)

    def flush_tail(self):
        if self.buffer.strip():
            self.handle_line(self.buffer)
        self.buffer = b""

    # ---- events ----
    def handle_line(self, raw):
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            self.trace.line("trace-skip: not JSON: " + clip(text, 80))
            return
        if isinstance(event, dict) and event.get("type") == "result":
            self.publish_result(event)
        try:
            for line in describe(event) if isinstance(event, dict) else ():
                self.trace.line(line)
        except Exception as exc:  # any shape surprise is a trace problem only
            self.trace.line("trace-error: %s: %s" % (type(exc).__name__, clip(str(exc), 120)))

    def publish_result(self, event):
        self.result_seen = True
        try:
            write_atomic(self.run_json, json.dumps(event, ensure_ascii=False) + "\n")
            self.result_written = True
            self.result_error = None
        except (OSError, TypeError, ValueError) as exc:
            self.result_error = "%s: %s" % (type(exc).__name__, exc)
            self.trace.line("trace: result write failed: " + clip(self.result_error, 120))

    def copy_envelope(self):
        """No result event: copy the input only when all of it is one JSON value."""
        try:
            with open(self.stream, "rb") as handle:
                whole = handle.read()
            text = whole.decode("utf-8", "replace").strip()
            if not text:
                return False
            json.loads(text)
        except (OSError, ValueError):
            return False
        try:
            write_atomic(self.run_json, text + "\n")
        except OSError as exc:
            self.result_error = "%s: %s" % (type(exc).__name__, exc)
            self.trace.line("trace: envelope copy failed: " + clip(self.result_error, 120))
            return False
        return True

    # ---- lifecycle ----
    def run(self, max_seconds):
        deadline = time.monotonic() + max_seconds
        expired = False
        read_failed = False
        read_failures = 0
        self.trace.line("trace: converter started, stream %s" % self.stream)
        while True:
            data = self.read_new()
            if data is None:
                # Before the sentinel the file may simply not exist yet; after
                # it a bounded retry, then a non-zero exit so the launcher
                # re-extracts instead of trusting a possibly stale result.
                # The sentinel and signals are checked here too, so failures
                # that began before the sentinel still end within the bound.
                read_failures += 1
                if not self.latched and (self.signalled or os.path.exists(self.sentinel)):
                    self.latched = True
                if self.latched and read_failures >= self.MAX_READ_FAILURES:
                    read_failed = True
                    break
                if not self.latched and time.monotonic() >= deadline:
                    expired = True
                    break
                time.sleep(self.poll)
                continue
            if read_failures:
                self.trace.line("trace: stream read recovered after %d failed reads" % read_failures)
                read_failures = 0
            if data:
                self.feed(data)
            if self.latched:
                if not data:
                    break
                continue
            if self.signalled or os.path.exists(self.sentinel):
                self.latched = True
                continue
            if time.monotonic() >= deadline:
                expired = True
                data = self.read_new()
                if data:
                    self.feed(data)
                break
            time.sleep(self.poll)
        self.flush_tail()
        outcome = "result written" if self.result_written else "no result event"
        if not self.result_seen and self.copy_envelope():
            outcome = "single JSON envelope copied"
        if self.result_error:
            code = 2
            outcome += "; result write failed"
        elif read_failed:
            code = 4
            outcome += "; stream unreadable after the sentinel (%d failed reads)" % read_failures
        elif expired:
            code = 3
            outcome += "; sentinel never appeared within %ss" % max_seconds
        elif self.signalled:
            code = 143
            outcome += "; signalled"
        else:
            code = 0
        self.trace.line("trace: converter finished (%s, exit %d%s)" % (
            outcome, code, ", %d trace writes failed" % self.trace.failures if self.trace.failures else ""))
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stream")
    parser.add_argument("trace")
    parser.add_argument("run_json")
    parser.add_argument("sentinel")
    parser.add_argument("--max-seconds", type=float, default=660.0)
    parser.add_argument("--poll", type=float, default=0.2)
    args = parser.parse_args(argv)
    converter = Converter(args.stream, Trace(args.trace), args.run_json, args.sentinel, max(args.poll, 0.01))

    def on_signal(signum, frame):
        converter.signalled = True

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, on_signal)
            except (ValueError, OSError):
                pass
    return converter.run(max(args.max_seconds, 0.0))


if __name__ == "__main__":
    sys.exit(main())
