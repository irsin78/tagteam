"""Native-worker records are metadata, never proof of successful implementation."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import verify_delegation as hook

class MetadataTests(unittest.TestCase):
    def test_shared_log_rotation_retains_recent_entries(self):
        from evidence import append_line
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metadata.jsonl'
            for i in range(11):
                append_line(path, json.dumps({'item': i}), 5)
            self.assertEqual([json.loads(line)['item'] for line in path.read_text().splitlines()], list(range(6, 11)))

    def test_shared_log_keeps_concurrent_appends(self):
        from concurrent.futures import ThreadPoolExecutor
        from evidence import append_line
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'metadata.jsonl'
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda i: append_line(path, json.dumps({'item': i}), 200), range(100)))
            self.assertEqual(sorted(json.loads(line)['item'] for line in path.read_text().splitlines()), list(range(100)))


    def test_no_repository_or_memory_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'.claude').mkdir()
            payload={'cwd':temp,'agent_id':'worker','agent_type':'implementer','session_id':'s'}
            with patch.dict(os.environ,{'CLAUDE_PROJECT_DIR':temp}), \
                 patch('sys.stdin',io.StringIO(json.dumps(payload))), \
                 patch('subprocess.run',side_effect=AssertionError('unnecessary repository scan')):
                self.assertEqual(hook.main(),0)
            entry=json.loads((Path(temp)/'.claude/.delegation-log.jsonl').read_text())
            self.assertEqual(entry['cwd'],temp)
            self.assertEqual(entry['verification'],'parent-required')
            self.assertNotIn('memory_hashes',entry)
            self.assertNotIn('status',entry)
            self.assertEqual(entry['host'],'claude')

    def test_codex_subagent_stop_records_host_and_agent(self):
        # Keys observed from codex-cli 0.168.0 SubagentStop (2026-11-13); no stop_reason.
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'.claude').mkdir()
            payload={'agent_id':'019a-child','agent_type':'harness-scout','agent_transcript_path':'t.jsonl',
                     'last_assistant_message':'done','stop_hook_active':False,'cwd':temp,
                     'session_id':'s','turn_id':'t1','hook_event_name':'SubagentStop'}
            log=Path(temp)/'.claude/.delegation-log.jsonl'
            for argv,host in ((['--host','codex'],'codex'),(['--host=codex'],'codex'),([],'claude'),
                              (['--host','other'],'claude')):
                with self.subTest(argv=argv), patch.dict(os.environ,{'CLAUDE_PROJECT_DIR':temp}), \
                     patch('sys.stdin',io.StringIO(json.dumps(payload))):
                    self.assertEqual(hook.main(argv),0)
                    entry=json.loads(log.read_text().splitlines()[-1])
                    self.assertEqual(entry['host'],host)
                    self.assertEqual((entry['agent_id'],entry['agent_type']),('019a-child','harness-scout'))
                    self.assertIsNone(entry['stop_reason'])
                    self.assertEqual(entry['verification'],'parent-required')

if __name__ == '__main__':
    unittest.main()
