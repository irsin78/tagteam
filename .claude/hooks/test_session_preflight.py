"""Startup is local, minimal metadata; installed platform instructions survive."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import session_preflight as hook

class StartupTests(unittest.TestCase):
    def test_platforms(self):
        for system,version,label in [('Windows','','Windows'),('Darwin','','macOS'),
                                     ('Linux','microsoft','Linux (WSL2)'),('Linux','','Linux')]:
            self.assertEqual(hook.detect_platform(system,version),label)
    def test_startup_never_probes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'.claude').mkdir(); (root/'docs').mkdir()
            (root/'docs/harness-manual.md').write_text('manual')
            out=io.StringIO()
            with patch('sys.stdin',io.StringIO(json.dumps({'cwd':temp,'source':'startup'}))), \
                 patch('urllib.request.urlopen',side_effect=AssertionError('network at startup')), \
                 patch('subprocess.run',side_effect=AssertionError('process at startup')), \
                 contextlib.redirect_stdout(out):
                self.assertEqual(hook.main(),0)
            data=json.loads((root/'.claude/.preflight-status').read_text())
            self.assertEqual(data['diagnostics'],'not requested')
            self.assertTrue(data['hook_alive'])
            self.assertEqual(len(out.getvalue().splitlines()),1)

if __name__ == '__main__':
    unittest.main()
