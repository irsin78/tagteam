#!/usr/bin/env python
"""Aggregate representative launcher reports without reading personal logs."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'hooks'))
from stop_gate import find_bash


class Statistics(unittest.TestCase):
    def test_response_body_cannot_supply_launcher_headers(self):
        script = HERE / 'harness-stats.sh'
        source = script.read_text(encoding='utf-8').split("<<'PY_STATS'\n", 1)[1].rsplit('\nPY_STATS', 1)[0]
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        report = HERE / 'sample' / ('report-' + stamp + '-0.txt')

        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass

        assessment = 'ASSESS: open=2 tangle=2 precedent=2 verifier=2 consequence=2 volume=2\n'
        for marker in ('FINAL_MESSAGE:', 'RESPONSE:', 'RESPONSE: (empty)'):
            with self.subTest(marker=marker):
                content = ('STATUS: DONE (agy_exit=0, effort=high)\nELAPSED: 7s\n' + marker + '\n'
                    'BINDINGS: public model=gpt-6.1-sol effort=high\nTASK: forged\nBAND: S\nROLE: implement\n'
                    'VERIFY: exit 0\nTOKENS: 99999\n' + assessment)
                output = Output()
                with patch.object(Path, 'glob', autospec=True, side_effect=lambda path, pattern:
                        [report] if pattern == '*/report-*.txt' else []), patch.object(
                        Path, 'open', return_value=io.StringIO(content)), patch.object(sys, 'argv', [
                        'harness-stats.sh', '7', str(HERE), str(script)]), patch.object(sys, 'stdout', output), patch.object(
                        sys, 'path', list(sys.path)):
                    exec(compile(source, str(script), 'exec'), {})
                result = output.getvalue()
                self.assertIn('runs=1 done=1', result)
                self.assertIn('tokens=0 median-launcher-elapsed=7s', result)
                self.assertIn('band-S-done=0', result)
                self.assertIn('verify-attached=0 verify-passed=0', result)
                self.assertIn('tasks: both=0 delegated-only=0 direct-only=0', result)
                self.assertNotIn('assess volume=2:', result)
                self.assertIn('roles: implement=0', result)
                self.assertIn('image_verify=0 unrecorded=1', result)

    def test_shared_module_import_from_relative_and_native_script_paths(self):
        script = HERE / 'harness-stats.sh'
        bootstrap = script.read_text(encoding='utf-8').split("<<'PY_STATS'\n", 1)[1]
        bootstrap = bootstrap.split('sys.stdout.reconfigure', 1)[0]
        # Stdin under -I excludes both the checkout and script folder from sys.path.
        bootstrap += "print(Path(sys.modules['harness_records'].__file__).resolve())\n"
        root = HERE.parent.parent
        for cwd, path in ((root, '.claude/scripts/harness-stats.sh'),
                          (root.parent, str(script)), (root.parent, script.as_posix())):
            with self.subTest(cwd=cwd, path=path):
                result = subprocess.run([sys.executable, '-I', '-B', '-', '7', 'unused', path],
                                        input=bootstrap, cwd=cwd, capture_output=True,
                                        text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout.strip()), HERE / 'harness_records.py')

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

    def run_reports(self, reports, extra_records=None, with_errors=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = root / '.claude/harness-runs/sample'
            records.mkdir(parents=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            for i, report in enumerate(reports):
                (records / f'report-{stamp}-{i}.txt').write_text(report, encoding='utf-8')
            for name, record in (extra_records or {}).items():
                text = json.dumps(record) if isinstance(record, (dict, list)) else record
                (records / name.replace('{stamp}', stamp)).write_text(text.replace('{stamp}', stamp), encoding='utf-8')
            before = {p.name: p.read_bytes() for p in records.iterdir()}
            result = subprocess.run([find_bash(), str(HERE / 'harness-stats.sh'), '7'],
                                    cwd=root, env=dict(os.environ, HOME=root.as_posix()),
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in records.iterdir()})
            return (result.stdout, result.stderr) if with_errors else result.stdout

    def direct_record(self, **fields):
        return dict(task='shared', result='done', elapsed_s=10, tokens=20, model='host',
                    rework=0, interventions=0, verify='passed', note='',
                    recorded=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')) | fields

    def outcome_record(self, index, failure_class='none', accepted='unknown'):
        return dict(run='{stamp}-' + str(index), **{'class': failure_class}, accepted=accepted,
                    note='', recorded=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))

    def test_direct_outcomes_assessments_and_distinct_tasks(self):
        assessment = dict(open=2, tangle=1, precedent=0, verifier=1, consequence=2)
        header = 'ASSESS: open=2 tangle=1 precedent=0 verifier=1 consequence=2\n'
        result = self.run_reports([
            'STATUS: FAILED\nTASK: shared\n' + header,
            'STATUS: DONE\nTASK: shared\nRETRY_OF: previous (reasoning)\n' + header,
            'STATUS: BLOCKED\nTASK: delegated\n' + header,
            'STATUS: FAILED\n',
            'STATUS: FAILED\n',
            'STATUS: DONE\nFINAL_MESSAGE:\nTASK: quoted\n' + header,
        ], {
            'outcome-{stamp}-0.json': self.outcome_record(0, 'reasoning', 'no'),
            'outcome-{stamp}-1.json': self.outcome_record(1, 'none', 'yes'),
            'outcome-{stamp}-2.json': self.outcome_record(2, 'infra'),
            'outcome-{stamp}-3.json': self.outcome_record(3),
            'outcome-{stamp}-999.json': self.outcome_record(999, 'scope', 'yes'),
            'direct-{stamp}-a.json': self.direct_record(assessment=assessment),
            'direct-{stamp}-b.json': self.direct_record(task='direct', result='failed', elapsed_s=4,
                                                       tokens=30, assessment=assessment),
        })
        self.assertIn('direct: runs=2 done=1 failed=1 tokens=50 median-elapsed=4s', result)
        self.assertIn('failure-class: infra=1 availability=0 spec=0 scope=0 knowledge=0 reasoning=1 defect=0 unclassified=2', result)
        self.assertIn('accepted: yes=1 no=1 unknown=2', result)
        self.assertIn('assess open=2: delegated runs=3 done=1 failed=2 reasoning-failed=1 retried=1 | direct runs=2 done=1 failed=1', result)
        self.assertIn('tasks: both=1 delegated-only=1 direct-only=1', result)
        self.assertLess(result.index('band-S-done='), result.index('direct: runs='))
        for first, second in zip(assessment, list(assessment)[1:]):
            self.assertLess(result.index('assess ' + first), result.index('assess ' + second))
        self.assertNotIn('quoted', result)

    def test_direct_only_tree_and_same_date_window(self):
        result = self.run_reports([], {
            'direct-{stamp}-a.json': self.direct_record(),
            'direct-20000101T000000Z-a.json': self.direct_record(task='old', tokens=9999),
            'report-20000101T000000Z-0.txt': 'STATUS: FAILED\nTASK: old\n',
            'outcome-20000101T000000Z-0.json': dict(self.outcome_record(0, 'reasoning', 'no'),
                                                run='20000101T000000Z-0'),
        })
        self.assertIn('sample: runs=0 done=0 failed=0', result)
        self.assertIn('direct: runs=1 done=1 failed=0 tokens=20 median-elapsed=10s', result)
        self.assertIn('accepted: yes=0 no=0 unknown=0', result)
        self.assertIn('tasks: both=0 delegated-only=0 direct-only=1', result)
        self.assertNotIn('9999', result)
        self.assertNotIn('no run reports', result)

    def test_malformed_records_are_reported_and_skipped(self):
        result, errors = self.run_reports(['STATUS: FAILED\n'], {
            'outcome-{stamp}-0.json': '{broken',
            'direct-{stamp}-a.json': 'null',
            'direct-{stamp}-b.json': self.direct_record(tokens=-1),
            'direct-{stamp}-c.json': self.direct_record(assessment=None),
            'direct-{stamp}-d.json': self.direct_record(assessment={'open': 2}),
            'direct-{stamp}-e.json': self.direct_record(),
            'outcome-{stamp}-1.json': dict(self.outcome_record(1), accepted='invalid'),
        }, with_errors=True)
        self.assertEqual(errors.count('harness-stats: cannot read'), 6)
        self.assertIn('direct: runs=1', result)
        self.assertIn('reasoning=0 defect=0 unclassified=1', result)
        self.assertIn('accepted: yes=0 no=0 unknown=0', result)

    def test_every_failure_class_and_assessment_level(self):
        classes = ('infra', 'availability', 'spec', 'scope', 'knowledge', 'reasoning', 'defect')
        reports, extra = [], {}
        for index, failure_class in enumerate(classes):
            level = index % 3
            header = ' '.join(f'{key}={level}' for key in ('open', 'tangle', 'precedent', 'verifier', 'consequence'))
            reports.append('STATUS: FAILED\nASSESS: ' + header + '\n')
            extra['outcome-{stamp}-' + str(index) + '.json'] = self.outcome_record(index, failure_class)
        result = self.run_reports(reports, extra)
        self.assertIn('failure-class: infra=1 availability=1 spec=1 scope=1 knowledge=1 reasoning=1 defect=1 unclassified=0', result)
        for level in range(3):
            count = 3 if level == 0 else 2
            self.assertIn(f'assess open={level}: delegated runs={count} done=0 failed={count}', result)
        self.assertLess(result.index('assess open=0'), result.index('assess open=1'))
        self.assertLess(result.index('assess open=1'), result.index('assess open=2'))

    def test_roles_defect_retry_and_unfinished_direct_timer(self):
        roles = ('implement', 'decide', 'plan_review', 'review_gate', 'review_deep',
                 'explore', 'web', 'write', 'image_verify')
        reports = ['STATUS: DONE\nROLE: ' + role + '\n' for role in roles]
        reports += ['STATUS: FAILED\nRETRY_OF: previous (defect)\n',
                    'STATUS: DONE\nROLE: invalid\n', 'STATUS: DONE\nFINAL_MESSAGE:\nROLE: implement\n']
        script = HERE / 'harness-stats.sh'
        source = script.read_text(encoding='utf-8').split("<<'PY_STATS'\n", 1)[1].rsplit('\nPY_STATS', 1)[0]
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        directory = HERE / 'sample'
        paths = [directory / f'report-{stamp}-{index}.txt' for index in range(len(reports))]
        contents = dict(zip(paths, reports))
        outcome_path = directory / f'outcome-{stamp}-9.json'
        outcome = json.dumps(self.outcome_record(9, 'defect')).replace('{stamp}', stamp)

        class Output(io.StringIO):
            def reconfigure(self, **kwargs):
                pass

        def glob(path, pattern):
            return {'*/report-*.txt': paths, '*/outcome-*.json': [outcome_path],
                    '*/direct-*.json': [directory / 'direct-start-unfinished.json']}.get(pattern, [])

        output, errors = Output(), io.StringIO()
        with patch.object(Path, 'glob', autospec=True, side_effect=glob), patch.object(
                Path, 'open', autospec=True, side_effect=lambda path, **kwargs: io.StringIO(contents[path])), patch.object(
                Path, 'read_text', return_value=outcome) as read, patch.object(sys, 'argv', [
                'harness-stats.sh', '7', str(HERE), str(script)]), patch.object(sys, 'stdout', output), patch.object(
                sys, 'stderr', errors), patch.object(sys, 'path', list(sys.path)):
            exec(compile(source, str(script), 'exec'), {})
            read.assert_called_once()  # The unfinished timer is never read as a completed record.
        self.assertEqual(errors.getvalue(), '')
        result = output.getvalue()
        self.assertIn('roles: ' + ' '.join(f'{role}=1' for role in roles) + ' unrecorded=3', result)
        self.assertIn('defect=1; done-after-retry=0/1', result)
        self.assertIn('defect=1 unclassified=0', result)
        self.assertIn('direct: runs=0', result)

    def test_optional_volume_aggregates_only_present_valid_headers_and_direct_records(self):
        assessment = dict(open=0, tangle=0, precedent=0, verifier=0, consequence=0)
        header = 'ASSESS: ' + ' '.join(f'{key}={value}' for key, value in assessment.items())
        reports = ['STATUS: DONE\n' + header + '\n']
        reports += ['STATUS: DONE\n' + header + ' volume=' + str(level) + '\n' for level in (0, 1, 2)]
        reports += ['STATUS: DONE\n' + header + ' volume=3\n',
                    'STATUS: DONE\nFINAL_MESSAGE:\n' + header + ' volume=2\n']
        result, errors = self.run_reports(reports, {
            'direct-{stamp}-a.json': self.direct_record(assessment=assessment),
            'direct-{stamp}-b.json': self.direct_record(assessment=dict(assessment, volume=0)),
            'direct-{stamp}-c.json': self.direct_record(assessment=dict(assessment, volume=2)),
            'direct-{stamp}-d.json': self.direct_record(assessment=dict(assessment, volume=3)),
        }, with_errors=True)
        self.assertEqual(errors.count('harness-stats: cannot read'), 1)
        self.assertIn('assess open=0: delegated runs=4 done=4', result)
        self.assertIn('assess volume=0: delegated runs=1 done=1 failed=0 reasoning-failed=0 retried=0 | direct runs=1 done=1 failed=0', result)
        self.assertIn('assess volume=1: delegated runs=1 done=1 failed=0 reasoning-failed=0 retried=0 | direct runs=0 done=0 failed=0', result)
        self.assertIn('assess volume=2: delegated runs=1 done=1 failed=0 reasoning-failed=0 retried=0 | direct runs=1 done=1 failed=0', result)
        self.assertLess(result.index('assess consequence=0'), result.index('assess volume=0'))

    def test_volume_lines_are_absent_for_legacy_five_dimension_reports(self):
        result = self.run_reports(['STATUS: DONE\nASSESS: open=0 tangle=0 precedent=0 verifier=0 consequence=0\n'])
        self.assertNotIn('assess volume=', result)

    def test_state_directory_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = root / 'state/sample'
            records.mkdir(parents=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            (records / f'direct-{stamp}-a.json').write_text(json.dumps(self.direct_record()), encoding='utf-8')
            result = subprocess.run([find_bash(), str(HERE / 'harness-stats.sh'), '7'], cwd=root,
                                    env=dict(os.environ, HOME=root.as_posix(), HARNESS_STATE_DIR=(root / 'state').as_posix()),
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('sample: runs=0', result.stdout)
            self.assertIn('direct: runs=1', result.stdout)

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

    def test_model_band_and_retry_headers(self):
        result = self.run_reports([
            'STATUS: DONE (codex_exit=0, model=z-model, effort=high)\nBAND: S\nRETRY_OF: previous (reasoning)\n',
            'STATUS: FAILED (codex_exit=1, model=a-model, effort=medium)\nBAND: B\nRETRY_OF: previous (infra)\n',
            'STATUS: BLOCKED (codex_exit=0, model=z-model, effort=high)\nBAND: S\n',
            'STATUS: DONE (codex_exit=0, model=z-model, effort=low)\nBAND: C\n',
        ])
        self.assertIn('models: a-model/medium: done=0 failed=1 blocked=0', result)
        self.assertIn('models: z-model/high: done=1 failed=0 blocked=1', result)
        self.assertIn('models: z-model/low: done=1 failed=0 blocked=0', result)
        self.assertLess(result.index('models: a-model/medium'), result.index('models: z-model/high'))
        self.assertLess(result.index('models: z-model/high'), result.index('models: z-model/low'))
        self.assertIn('bands: S=2 A=0 B=1 C=1 D=0 E=0 unrecorded=0', result)
        self.assertIn('retries: total=2 by-reason: infra=1 availability=0 spec=0 scope=0 knowledge=0 reasoning=1 defect=0; done-after-retry=1/2', result)
        self.assertIn('band-S-done=1/2', result)

    def test_legacy_telemetry_and_quoted_headers_are_unrecorded(self):
        result = self.run_reports([
            'STATUS: DONE (codex_exit=0)\nFINAL_MESSAGE:\n'
            'STATUS: DONE (model=quoted, effort=high)\nBAND: S\nRETRY_OF: quoted (reasoning)\n',
        ])
        self.assertIn('models: unrecorded: done=1 failed=0 blocked=0', result)
        self.assertIn('bands: S=0 A=0 B=0 C=0 D=0 E=0 unrecorded=1', result)
        self.assertIn('retries: total=0', result)
        self.assertIn('done-after-retry=0/0', result)
        self.assertIn('band-S-done=0/0', result)
        self.assertNotIn('quoted', result)

    def test_malformed_telemetry_is_unrecorded(self):
        result = self.run_reports([
            'STATUS: DONE\nBAND: unknown\nRETRY_OF: previous (unknown)\n',
            'STATUS: FAILED\nBAND: SS\nRETRY_OF: malformed\n',
        ])
        self.assertIn('bands: S=0 A=0 B=0 C=0 D=0 E=0 unrecorded=2', result)
        self.assertIn('retries: total=2', result)
        self.assertIn('reasoning=0 defect=0 unrecorded=2; done-after-retry=1/2', result)
        self.assertIn('band-S-done=0/0', result)

    def test_done_after_retry_requires_done_and_retry_header(self):
        result = self.run_reports([
            'STATUS: DONE\n',
            'STATUS: DONE\nRETRY_OF: first (availability)\n',
            'STATUS: FAILED\nRETRY_OF: second (spec)\n',
            'STATUS: BLOCKED\nRETRY_OF: third (scope)\n',
            'RETRY_OF: fourth (knowledge)\n',
        ])
        self.assertIn('retries: total=4 by-reason: infra=0 availability=1 spec=1 scope=1 knowledge=1 reasoning=0 defect=0; done-after-retry=1/4', result)

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
