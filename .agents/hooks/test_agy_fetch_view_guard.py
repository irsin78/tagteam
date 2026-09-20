#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


GUARD = Path(__file__).with_name("agy_fetch_view_guard.py")


class GuardTests(unittest.TestCase):
    def invoke(self, target: Path, artifact: Path, web: bool = True) -> dict[str, str]:
        payload = {
            "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(target)}},
            "artifactDirectoryPath": str(artifact),
        }
        env = os.environ.copy()
        env.pop("HARNESS_AGY_WEB", None)
        if web:
            env["HARNESS_AGY_WEB"] = "1"
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout)

    def invoke_url(self, url: str, allowed: str) -> dict[str, str]:
        payload = {"toolCall": {"name": "read_url_content", "args": {"Url": url}}}
        env = os.environ.copy()
        env["HARNESS_AGY_WEB"] = "1"
        env["HARNESS_AGY_URL_HOSTS"] = allowed
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), str(GUARD)], input=json.dumps(payload),
            text=True, capture_output=True, env=env, check=True,
        )
        return json.loads(result.stdout)

    def test_allows_current_conversation_url_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "conversation"
            target = artifact / ".system_generated" / "steps" / "2" / "content.md"
            target.parent.mkdir(parents=True)
            target.write_text("fetched", encoding="utf-8")
            self.assertEqual(self.invoke(target, artifact)["decision"], "allow")

    def test_denies_workspace_or_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "conversation"
            (artifact / ".system_generated" / "steps").mkdir(parents=True)
            target = root / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            self.assertEqual(self.invoke(target, artifact)["decision"], "deny")

    def test_does_not_affect_non_web_agy_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "ordinary.txt"
            target.write_text("ok", encoding="utf-8")
            self.assertEqual(self.invoke(target, root, web=False)["decision"], "allow")

    def test_missing_event_fields_fail_closed_in_web_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "workspace.txt"
            target.write_text("private", encoding="utf-8")
            env = os.environ.copy()
            env["HARNESS_AGY_WEB"] = "1"
            result = subprocess.run(
                [os.environ.get("PYTHON", "python3"), str(GUARD)],
                input=json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": str(target)}}}),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout)["decision"], "deny")

    def test_allows_only_caller_named_url_host(self) -> None:
        self.assertEqual(self.invoke_url("https://docs.example.com/page", "docs.example.com")["decision"], "allow")
        self.assertEqual(self.invoke_url("https://other.example/page", "docs.example.com")["decision"], "deny")
        self.assertEqual(
            self.invoke_url("https://docs.example.com,other.example/page", "docs.example.com,other.example")["decision"],
            "deny",
        )

    def test_local_url_is_always_denied(self) -> None:
        self.assertEqual(self.invoke_url("http://127.0.0.1:8080/x", "127.0.0.1")["decision"], "deny")
        self.assertEqual(self.invoke_url("http://localhost/x", "localhost")["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
