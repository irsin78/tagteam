#!/usr/bin/env python
"""Batch processing retains hash boundaries and the writer admission contract."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
from stop_gate import find_bash


class RuntimeBatch(unittest.TestCase):
    def test_detached_handoff_survives_competing_registration(self):
        setup = 'TOOL=claude; SANDBOX=workspace-write; source "$SCRIPTS/run-state.sh"; '
        reserved = self.shell(setup + 'RUN_ID=reserved; admit_run')
        self.assertEqual(reserved.returncode, 0, reserved.stderr)
        competitor = setup + '''
RUN_ID=competitor
eval "$(declare -f stale_run_check | sed '1s/stale_run_check/check_records/')"
stale_run_check() {
    touch "$HOME/locked"
    while [ ! -f "$HOME/release" ]; do sleep 0.05; done
    check_records
}
admit_run
'''
        with ThreadPoolExecutor(max_workers=2) as pool:
            other = pool.submit(self.shell, competitor)
            try:
                deadline = time.monotonic() + 10
                while not (self.personal / 'locked').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((self.personal / 'locked').exists())
                child = pool.submit(self.shell, setup + 'RUN_ID=reserved; admit_run', HARNESS_RUN_CHILD='1')
                time.sleep(1)
                self.assertFalse(child.done(), 'reserved handoff must wait for admission')
            finally:
                (self.personal / 'release').touch()
            rejected, accepted = other.result(), child.result()
        self.assertEqual(rejected.returncode, 5, rejected.stderr)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_admission_serializes_check_and_registration_but_allows_readers(self):
        setup = 'TOOL=claude; RUN_ID=first; SANDBOX=workspace-write; source "$SCRIPTS/run-state.sh"; '
        held = setup + '''
eval "$(declare -f stale_run_check | sed '1s/stale_run_check/check_records/')"
stale_run_check() {
    touch "$HOME/locked"
    while [ ! -f "$HOME/release" ]; do sleep 0.05; done
    check_records
}
admit_run
'''
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.shell, held)
            try:
                deadline = time.monotonic() + 10
                while not (self.personal / 'locked').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((self.personal / 'locked').exists())
                other = self.shell(setup + 'RUN_ID=second; admit_run')
                self.assertEqual(other.returncode, 5, other.stderr)
                reader = self.shell(setup + 'RUN_ID=reader; SANDBOX=read-only; admit_run')
                self.assertEqual(reader.returncode, 0, reader.stderr)
            finally:
                (self.personal / 'release').touch()
            result = first.result()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'states/sample/state-second.json').exists())
        handoff = self.shell(setup + 'admit_run')
        self.assertEqual(handoff.returncode, 0, handoff.stderr)

    def shell(self, code, **extra):
        env = dict(self.env, SCRIPTS=HERE.as_posix(), HARNESS_STATE_DIR=(self.root / 'states').as_posix(),
                   HARNESS_TREE_KEY='sample', **extra)
        return subprocess.run([find_bash(), '-c', code], cwd=self.root, env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runtime-batch-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".claude/rules").mkdir(parents=True)
        self.personal = self.root / "personal"
        (self.personal / ".codex").mkdir(parents=True)
        self.env = dict(os.environ, HOME=self.personal.as_posix())

    def hashes(self):
        result = subprocess.run([find_bash(), str(HERE / "control-plane-hash.sh")],
                                cwd=self.root, env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {path: digest for digest, path in (line.split("  ", 1) for line in result.stdout.splitlines())}

    def test_inventory_includes_rules_and_personal_settings_not_caches(self):
        target = self.root / ".claude/rules/ünïcode rule.md"
        target.write_bytes(b"sample\r\n")
        (self.root / ".claude/rules/ignored.pyc").write_bytes(b"cache")
        (self.root / ".claude/rules/__pycache__").mkdir()
        (self.root / ".claude/rules/__pycache__/ignored.txt").write_bytes(b"cache")
        (self.personal / ".codex/config.toml").write_bytes(b'model = "sample"\n')
        hashes = self.hashes()
        self.assertEqual(len(hashes), 2)
        self.assertEqual(hashes[".claude/rules/ünïcode rule.md"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertTrue(any(path.endswith("/personal/.codex/config.toml") for path in hashes))

    def test_only_hook_runtime_trust_is_ignored(self):
        config = self.personal / ".codex/config.toml"
        text = 'model = "sample"\n  [hooks.state."example"]\ntrusted_hash = "old"\n  [projects."example"]\ntrust_level = "untrusted"\n'
        config.write_bytes(text.encode())
        before = self.hashes()
        config.write_bytes(text.replace('"old"', '"new"').encode())
        self.assertEqual(before, self.hashes())
        config.write_bytes(text.replace('"untrusted"', '"trusted"').encode())
        self.assertNotEqual(before, self.hashes())

    def test_empty_codex_config_has_empty_digest(self):
        (self.personal / ".codex/config.toml").write_bytes(b"")
        self.assertEqual(list(self.hashes().values()), [hashlib.sha256(b"").hexdigest()])

    def control(self, mutation):
        (self.root / '.claude/rules/rule.md').write_text('original', encoding='utf-8')
        (self.personal / '.codex/config.toml').write_text('model="original"\n', encoding='utf-8')
        return self.shell('SCRIPT_DIR=$SCRIPTS; source "$SCRIPTS/launcher-common.sh"; '
                          'CP_BEFORE=before; CP_AFTER=after; CHANGED_FILE=changed; : > changed; '
                          'control_before; ' + mutation + '; control_after; '
                          'printf "OK=%s\\nHITS=%s\\nNOTICE=%s\\n" "$CP_EVIDENCE_OK" "$CP_HITS" "$CP_NOTICE"')

    def test_control_batch_detects_deletions_additions_and_global_notice(self):
        result = self.control('rm .claude/rules/rule.md; printf new > .claude/rules/new.md; '
                              'printf new > "$HOME/.codex/config.toml"; '
                              'printf "AGENTS.md\\n.claude/settings.local.json\\nsource.txt\\n" > changed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK=1', result.stdout)
        self.assertIn('HITS=.claude/rules/new.md,.claude/rules/rule.md,AGENTS.md', result.stdout)
        self.assertIn('NOTICE=.claude/settings.local.json,', result.stdout)
        self.assertIn('/.codex/config.toml', result.stdout)
        self.assertNotIn('source.txt', result.stdout)

    def test_control_batch_read_error_and_empty_producer_are_unknown(self):
        for mutation in ('printf broken > before', 'rm changed', "printf 'exit 0\\n' > empty.sh; CP_HASH=empty.sh"):
            with self.subTest(mutation=mutation):
                result = self.control(mutation)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('OK=0', result.stdout)

    def test_relocated_policies_are_hashed_and_deleted_paths_classified(self):
        folder=self.root/'docs/orchestration';folder.mkdir(parents=True)
        for name in ('delegation-matrix.md','retry-policy.md'):
            (folder/name).write_text('policy',encoding='utf-8')
        (folder/'project-notes.md').write_text('notes',encoding='utf-8')
        hashes=self.hashes()
        for name in ('delegation-matrix.md','retry-policy.md'):
            self.assertIn('docs/orchestration/'+name,hashes)
        self.assertNotIn('docs/orchestration/project-notes.md',hashes)
        result=self.control('printf revised > docs/orchestration/delegation-matrix.md; '
                            'rm docs/orchestration/retry-policy.md')
        self.assertIn('OK=1',result.stdout)
        self.assertIn('HITS=docs/orchestration/delegation-matrix.md,docs/orchestration/retry-policy.md',result.stdout)
        # Also classify an explicit changed path when there is no hash delta.
        result=self.control('printf "docs/orchestration/retry-policy.md\\n" > changed')
        self.assertIn('HITS=docs/orchestration/retry-policy.md',result.stdout)

    def test_saved_workspace_matches_old_envelope_and_reports_changes(self):
        (self.root / 'source.txt').write_text('before', encoding='utf-8')
        result = self.shell('SCRIPT_DIR=$SCRIPTS; PY=python; source "$SCRIPTS/workspace-evidence.sh"; '
                            'workspace_before "$HOME/before" --exclude-tree "$HOME"; '
                            'python "$WORKSPACE_SNAPSHOT" --envelope --exclude-tree "$HOME" > "$HOME/old"; '
                            'printf after > source.txt; workspace_after "$HOME/before" "$HOME/after" "$HOME/changed" --exclude-tree "$HOME"; '
                            'printf "OK=%s CHANGED=%s\\n" "$WORKSPACE_OK" "$CHANGED"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.personal / 'before').read_text()), json.loads((self.personal / 'old').read_text()))
        self.assertIn('OK=1 CHANGED=source.txt', result.stdout)

    def test_saved_workspace_scope_mismatch_is_unknown(self):
        result = self.shell('SCRIPT_DIR=$SCRIPTS; PY=python; source "$SCRIPTS/workspace-evidence.sh"; '
                            'workspace_before "$HOME/before" --exclude-tree "$HOME"; '
                            'workspace_after "$HOME/before" "$HOME/after" "$HOME/changed" --exclude-tree "$HOME" --exclude source.txt; '
                            'printf "OK=%s CHANGED=%s\\n" "$WORKSPACE_OK" "$CHANGED"')
        self.assertIn('OK=0 CHANGED=unknown', result.stdout)
        self.assertEqual((self.personal / 'changed').read_bytes(), b'')

    def test_context_matches_nongit_root_from_subdirectory(self):
        (self.root / 'nested').mkdir()
        result = self.shell('TOOL=codex; unset HARNESS_TREE_KEY; source "$SCRIPTS/run-state.sh"; '
                            'printf "%s\\n" "$RS_CWD" "$RS_TREE_KEY"; cd nested; '
                            'source "$SCRIPTS/run-state.sh"; printf "%s\\n" "$RS_CWD" "$RS_TREE_KEY"')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:2], lines[2:])
        self.assertEqual(lines[1], hashlib.sha1(os.path.normcase(os.path.realpath(self.root)).encode('utf-8')).hexdigest()[:16])

    def test_completed_status_and_wait_each_read_record_once(self):
        result = self.shell('TOOL=codex; source "$SCRIPTS/run-state.sh"; RUN_ID=finished; state_write done 3 expected; '
                            'eval "$(declare -f read_record | sed \'1s/read_record/original_read/\')"; '
                            'reads=0; read_record() { reads=$((reads + 1)); original_read "$@"; }; '
                            'cmd_status finished; printf "EXIT=%s READS=%s\\n" "$?" "$reads"; '
                            'reads=0; cmd_wait finished 2; printf "EXIT=%s READS=%s\\n" "$?" "$reads"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('EXIT=3 READS=1'), 2)

    def test_wait_budget_counts_time_spent_classifying(self):
        # Simulate a slow inspection. Old sleep-only accounting waits 2 more
        # seconds and inspects a second time; this should return after one.
        result = self.shell('TOOL=codex; source "$SCRIPTS/run-state.sh"; touch "$STATE_DIR/state-active.json"; '
                            'checks=0; classify_record() { checks=$((checks + 1)); sleep 2.1; RS_STATE=running; }; '
                            'cmd_wait active 2; printf "EXIT=%s CHECKS=%s\\n" "$?" "$checks"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EXIT=6 CHECKS=1', result.stdout)

    def test_baseline_diff_is_optional_and_keeps_tracked_dirty_contents(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'source.txt').write_text('original\n')
        for args in (['add', 'source.txt'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        (self.root / 'source.txt').write_text('dirty\n')
        result = self.shell('source "$SCRIPTS/launcher-common.sh"; HEAD=$(git rev-parse HEAD); LOG_DIR=.; TIMESTAMP=probe; '
                            'unset HARNESS_SAVE_BASELINE; save_git_baseline; test ! -e baseline-probe.diff || exit 9; '
                            'HARNESS_SAVE_BASELINE=1; save_git_baseline; cat baseline-probe.diff')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-original\n+dirty', result.stdout)

    def scan(self, records):
        bucket = self.root / "states/sample"
        bucket.mkdir(parents=True)
        for i, record in enumerate(records):
            (bucket / f"state-{i}.json").write_text(json.dumps(record), encoding="utf-8")
        counter = self.root / "python-calls.txt"
        wrapper = self.root / "count-python.sh"
        wrapper.write_text('#!/usr/bin/env bash\nprintf "call\\n" >> "$COUNT_FILE"\nexec "$REAL_PYTHON" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        env = dict(self.env, HARNESS_STATE_DIR=(self.root / "states").as_posix(), HARNESS_TREE_KEY="sample",
                   RUN_STATE=(HERE / "run-state.sh").as_posix(), COUNT_WRAPPER=wrapper.as_posix(),
                   COUNT_FILE=counter.as_posix(), REAL_PYTHON=sys.executable)
        result = subprocess.run([find_bash(), "-c",
            'TOOL=claude; source "$RUN_STATE"; RS_PY="$COUNT_WRAPPER"; SANDBOX=workspace-write; '
            'RUN_ID=self; stale_run_check; printf "CLEANED: %s\\n" "$STALE_CLEANED"'],
            cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8")
        return result, counter.read_text().splitlines()

    def test_terminal_history_is_read_once(self):
        records = [dict(run_id=f"finished-{i}", state="done", sandbox="workspace-write") for i in range(100)]
        records += [dict(run_id="old", state="aborted", status="launcher died before writing a final state")]
        records += [dict(run_id="advice", state="running", sandbox="read-only")]
        result, calls = self.scan(records)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEANED: old", result.stdout)
        self.assertEqual(len(calls), 1)

    def test_retry_and_verifier_time_partition(self):
        env = dict(self.env, COMMON=(HERE / "launcher-common.sh").as_posix())
        code = ('source "$COMMON"; timing_init 0.000; '
                'timing_clock() { TIMING_NOW_MS=$FAKE_NOW; }; '
                'FAKE_NOW=100; timing_enter cli; FAKE_NOW=400; timing_enter postflight; '
                'FAKE_NOW=600; timing_enter cli; FAKE_NOW=900; timing_enter postflight; '
                'FAKE_NOW=1000; timing_enter verify; FAKE_NOW=1400; timing_enter postflight; '
                'FAKE_NOW=1600; timing_report')
        result = subprocess.run([find_bash(), "-c", code], cwd=self.root, env=env,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ELAPSED: 1s", result.stdout)
        self.assertIn("preflight_ms=100 cli_ms=600 postflight_ms=500 verify_ms=400 total_ms=1600 attempts=2 resolution=ms", result.stdout)

    def test_clock_accepts_locale_decimal_separator(self):
        env = dict(self.env, COMMON=(HERE / "launcher-common.sh").as_posix())
        result = subprocess.run([find_bash(), "-c", 'source "$COMMON"; timing_init 12,125; printf "%s %s" "$TIMING_LAST_MS" "$TIMING_RESOLUTION"'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "12125 ms")

    def test_clock_accepts_bash_four_integer_seconds(self):
        env = dict(self.env, COMMON=(HERE / "launcher-common.sh").as_posix())
        result = subprocess.run([find_bash(), "-c", 'source "$COMMON"; timing_init 12; printf "%s %s" "$TIMING_LAST_MS" "$TIMING_RESOLUTION"'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "12000 seconds")

    def test_pending_writer_still_blocks(self):
        result, _ = self.scan([dict(run_id="pending", state="starting", sandbox="workspace-write",
                                    started_epoch=int(time.time()), launcher_pid=None, child_pid=None)])
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("HARNESS_BUSY: STALE_RUN pending", result.stderr)


if __name__ == "__main__":
    unittest.main()
