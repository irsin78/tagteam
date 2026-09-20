#!/usr/bin/env python3
"""Tests for the agy web lane's deny-all tool guard."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().with_name("agy_web_no_tools.py")


def run(payload, web_marker):
    env = dict(os.environ)
    if web_marker:
        env["HARNESS_AGY_WEB"] = "1"
    else:
        env.pop("HARNESS_AGY_WEB", None)
    result = subprocess.run([sys.executable, str(GUARD)],
                            input=payload, text=True, capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["decision"]


class WebModeDeniesEverything(unittest.TestCase):
    """No tool call is legitimate in this lane, whatever it is called."""

    def test_denies_the_tools_the_old_agent_had(self):
        for name in ("view_file", "read_url_content"):
            payload = json.dumps({"toolCall": {"name": name, "args": {}}})
            self.assertEqual(run(payload, True), "deny")

    def test_denies_names_the_old_guard_let_through(self):
        # The predecessor allowed any name it did not recognize, so a renamed
        # or newly added tool silently escaped it.
        for name in ("view_file_outline", "run_command", "write_file",
                     "search_web", "spawn_agent", ""):
            payload = json.dumps({"toolCall": {"name": name, "args": {}}})
            self.assertEqual(run(payload, True), "deny")

    def test_denies_unparseable_and_empty_events(self):
        for payload in ("", "{}", "not json at all", '{"tool_call": {}}',
                        '[{"unexpected": "shape"}]'):
            self.assertEqual(run(payload, True), "deny")

    def test_denies_regardless_of_arguments(self):
        payload = json.dumps({"toolCall": {"name": "view_file", "args": {
            "AbsolutePath": "/etc/passwd"}}})
        self.assertEqual(run(payload, True), "deny")


class OtherSessionsUnaffected(unittest.TestCase):
    def test_allows_when_the_web_marker_is_absent(self):
        payload = json.dumps({"toolCall": {"name": "write_file", "args": {}}})
        self.assertEqual(run(payload, False), "allow")

    def test_allows_empty_input_without_the_marker(self):
        self.assertEqual(run("", False), "allow")


if __name__ == "__main__":
    unittest.main(verbosity=1)
