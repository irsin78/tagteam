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

if __name__ == '__main__':
    unittest.main()
