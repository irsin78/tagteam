"""Maintenance never runs implicitly and preserves active runs and linked files."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT=Path(__file__).with_name('harness-clean.py')
spec=importlib.util.spec_from_file_location('maintenance',SCRIPT)
clean=importlib.util.module_from_spec(spec);spec.loader.exec_module(clean)

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'.claude/codex-logs').mkdir(parents=True)
        self.states=self.root/'states';self.states.mkdir()
        self.old=time.time()-30*86400
    def record(self,name,state):
        path=self.states/('state-'+name+'.json')
        path.write_text(json.dumps({'state':state,'updated_epoch':self.old}))
        return path
    def log(self):
        path=self.root/'.claude/codex-logs/old.log';path.write_text('old evidence')
        os.utime(path,(self.old,self.old));return path
    def test_completed_records_and_old_logs_are_candidates(self):
        state=self.record('done','done');log=self.log()
        paths,active=clean.candidates(self.root,self.states,time.time()-14*86400)
        self.assertFalse(active);self.assertIn(state,paths);self.assertIn(log,paths)
        self.assertTrue(log.exists());self.assertTrue(state.exists())
    def test_active_or_unreadable_records_retain_logs(self):
        log=self.log();state=self.record('active','running')
        for value in (state.read_text(),'broken JSON'):
            state.write_text(value)
            paths,active=clean.candidates(self.root,self.states,time.time()-14*86400)
            self.assertTrue(active);self.assertNotIn(log,paths)
    def test_preview_then_explicit_apply(self):
        log=self.log()
        env={**os.environ,'HARNESS_STATE_DIR':str(self.states),'HARNESS_DELEGATE_RUN':'0'}
        preview=subprocess.run([sys.executable,str(SCRIPT)],cwd=self.root,env=env,text=True,capture_output=True)
        self.assertEqual(preview.returncode,0,preview.stderr);self.assertTrue(log.exists())
        self.assertIn('WOULD_REMOVE',preview.stdout)
        applied=subprocess.run([sys.executable,str(SCRIPT),'--apply'],cwd=self.root,env=env,text=True,capture_output=True)
        self.assertEqual(applied.returncode,0,applied.stderr);self.assertFalse(log.exists())
        self.assertTrue(log.parent.is_dir())
    def test_links_are_not_removed(self):
        target=self.root/'outside';target.write_text('keep')
        link=self.root/'.claude/codex-logs/link'
        try:link.symlink_to(target)
        except OSError:self.skipTest('symlink creation unavailable')
        paths,_=clean.candidates(self.root,self.states,time.time()+10)
        self.assertNotIn(link,paths);self.assertTrue(target.exists())

if __name__=='__main__':unittest.main()
