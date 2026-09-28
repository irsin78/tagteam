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
            self.assertEqual((data['host'],data['role']),('claude','ORCHESTRATOR'))
            lines=out.getvalue().splitlines()
            self.assertEqual(len(lines),1)
            self.assertTrue(lines[0].startswith('HARNESS PLATFORM: '))
            self.assertIn(' -- host: claude (ORCHESTRATOR) -- before operational work read: ',lines[0])
    def test_host_argument(self):
        self.assertEqual(hook.host_from_argv([]),'claude')
        self.assertEqual(hook.host_from_argv(['--host','codex']),'codex')
        self.assertEqual(hook.host_from_argv(['--host=codex']),'codex')
        self.assertEqual(hook.host_from_argv(['--host','gemini']),'claude')
        self.assertEqual(hook.host_from_argv(['--host']),'claude')
    def test_role_from_marker_or_agent(self):
        self.assertEqual(hook.session_role({},{}),'ORCHESTRATOR')
        self.assertEqual(hook.session_role({},{'HARNESS_DELEGATE_RUN':'1'}),'DELEGATE')
        self.assertEqual(hook.session_role({'agent_id':'w1'},{}),'DELEGATE')
        self.assertEqual(hook.session_role({},{'HARNESS_DELEGATE_RUN':'0'}),'ORCHESTRATOR')
    def test_codex_delegate_line(self):
        with tempfile.TemporaryDirectory() as temp:
            out=io.StringIO()
            with patch('sys.stdin',io.StringIO(json.dumps({'cwd':temp}))),                  patch('sys.argv',['session_preflight.py','--host','codex']),                  patch.dict('os.environ',{'HARNESS_DELEGATE_RUN':'1'}),                  contextlib.redirect_stdout(out):
                self.assertEqual(hook.main(),0)
            self.assertIn(' -- host: codex (DELEGATE) -- ',out.getvalue())

if __name__ == '__main__':
    unittest.main()
