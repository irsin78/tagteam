#!/usr/bin/env python
"""Aggregate representative launcher reports without reading personal logs."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'hooks'))
from stop_gate import find_bash


class Statistics(unittest.TestCase):
    def test_current_local_reports_and_legacy_date_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            for name, started in (('current', f'STARTED: {stamp}\n'), ('legacy', ''), ('old', 'STARTED: 20000101T000000Z\n')):
                run = root / '.claude/local-logs' / ('run-' + name)
                run.mkdir(parents=True)
                (run / 'report.txt').write_text('STATUS: DONE\n' + started + 'ELAPSED: 3s\n'
                    'TIMING: preflight_ms=10 request_ms=2000 postflight_ms=990 verify_ms=0 total_ms=3000\n'
                    'FINAL_MESSAGE:\nTIMING: total_ms=99999\n', encoding='utf-8')
            result = subprocess.run([find_bash(), str(HERE / 'harness-stats.sh'), '7'],
                                    cwd=root, env=dict(os.environ, HOME=root.as_posix()),
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('checkout-local: runs=2 done=2', result.stdout)
            self.assertIn('local=2 unknown=0', result.stdout)
            self.assertIn('mean-ms: preflight=10 request=2000 postflight=990 verify=0 total=3000', result.stdout)
            self.assertIn('verify-not-requested=2 verify-unknown=0', result.stdout)
            self.assertIn('1 older local reports', result.stdout)
            self.assertNotIn('99999', result.stdout)

    def run_reports(self, reports):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = root / '.claude/harness-runs/sample'
            records.mkdir(parents=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            for i, report in enumerate(reports):
                (records / f'report-{stamp}-{i}.txt').write_text(report, encoding='utf-8')
            before = {p.name: p.read_bytes() for p in records.iterdir()}
            result = subprocess.run([find_bash(), str(HERE / 'harness-stats.sh'), '7'],
                                    cwd=root, env=dict(os.environ, HOME=root.as_posix()),
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in records.iterdir()})
            return result.stdout

    def test_hosts_and_modes_account_for_every_report(self):
        result = self.run_reports([
            'STATUS: DONE (codex_exit=0, sandbox=workspace-write)\n',
            'STATUS: DONE (claude_exit=0, mode=plan)\n',
            'STATUS: DONE (agy_exit=0, agy_status=success)\n',
            'STATUS: DONE (elapsed=5s, finish=stop, cmd_exit=0)\n',
            'STATUS: FAILED (unknown legacy format)\n',
        ])
        self.assertIn('runs=5 done=4 failed=1', result)
        self.assertIn('hosts: codex=1 claude=1 agy=1 local=1 unknown=1', result)
        self.assertIn('codex-workspace-write=1', result)
        self.assertIn('claude-plan=1 not-reported-or-unknown=3', result)

    def test_verifier_attachment_is_not_success_and_body_is_not_header(self):
        result = self.run_reports([
            'STATUS: DONE (claude_exit=0)\nVERIFY: exit 0\nELAPSED: 7s\n',
            'STATUS: FAILED (codex_exit=0)\nVERIFY: exit 7\n',
            'STATUS: DONE (agy_exit=0)\nVERIFY: not requested\n',
            'STATUS: DONE (cmd_exit=0)\nFINAL_MESSAGE:\nVERIFY: exit 0\nTOKENS: 999\n',
            'VERIFY: unavailable\n',
        ])
        self.assertIn('verify-attached=3 verify-passed=1 verify-failed=1 verify-not-requested=1 verify-unknown=2', result)
        self.assertIn('status-other-or-missing=1 tokens=0 median-launcher-elapsed=7s', result)

    def test_unknown_header_is_not_replaced_by_verifier_output(self):
        result = self.run_reports([
            'STATUS: DONE (claude_exit=0)\nTOKENS: unknown\nELAPSED: unknown\n'
            'VERIFY: exit 0\nTOKENS: 12345\nELAPSED: 777s\nFINAL_MESSAGE:\n',
        ])
        self.assertIn('tokens=0 median-launcher-elapsed=n/a', result)
        self.assertIn('verify-passed=1', result)

    def test_phase_means_use_complete_new_reports_only(self):
        result = self.run_reports([
            'STATUS: DONE (claude_exit=0)\nTIMING: preflight_ms=100 cli_ms=600 postflight_ms=500 verify_ms=400 total_ms=1600 attempts=2 resolution=ms\nVERIFY: not requested\n',
            'STATUS: DONE (codex_exit=0)\nTIMING: preflight_ms=300 cli_ms=800 postflight_ms=500 verify_ms=0 total_ms=1600 attempts=1 resolution=ms\n',
            'STATUS: DONE (codex_exit=0)\nELAPSED: 200s\n',
            'STATUS: DONE (claude_exit=0)\nTIMING: total_ms=999\n',
            'STATUS: DONE (claude_exit=0)\nVERIFY: exit 0\nTIMING: preflight_ms=9 cli_ms=9 postflight_ms=9 verify_ms=9 total_ms=36\n',
        ])
        self.assertIn('timed-runs=2 mean-ms: preflight=200 cli=700 postflight=500 verify=200 total=1600', result)

    def test_other_modes_are_distinct(self):
        result = self.run_reports([
            'STATUS: DONE (codex_exit=0, sandbox=read-only)\n',
            'STATUS: DONE (codex_exit=0, sandbox=danger-full-access)\n',
            'STATUS: DONE (claude_exit=0, mode=acceptEdits)\n',
            'STATUS: DONE (claude_exit=0, mode=unknown)\n',
        ])
        self.assertIn('codex-read-only=1 codex-full-access=1 claude-acceptEdits=1 claude-plan=0 not-reported-or-unknown=1', result)


if __name__ == '__main__':
    unittest.main()
