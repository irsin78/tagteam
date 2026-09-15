"""A rejected Claude log path must not launch a worker or write outside the project."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'hooks'))
from stop_gate import find_bash

class LogPathTests(unittest.TestCase):
    def test_rejects_outside_paths_before_launch(self):
        bash=find_bash()
        if not bash:self.skipTest('Bash unavailable')
        script=Path(__file__).with_name('claude-run.sh').resolve().as_posix()
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);project=base/'project';(project/'.claude').mkdir(parents=True)
            (project/'prompt.txt').write_text('A fixture only')
            home=base/'home';home.mkdir()
            binpath=base/'bin';binpath.mkdir()
            stub=binpath/'claude'
            stub.write_text('#!/usr/bin/env bash\nprintf invoked > "$CALL_TRACE"\nexit 99\n',newline='\n')
            stub.chmod(0o755)
            trace=base/'called'
            env={**os.environ,'HOME':home.as_posix(),'CALL_TRACE':trace.as_posix(),
                 'HARNESS_DELEGATE_RUN':'0','PATH':str(binpath)+os.pathsep+os.environ['PATH']}
            for value in ('../outside','nested/../../outside',r'..\outside','/outside','C:/outside',''):
                with self.subTest(path=value):
                    result=subprocess.run([bash,script,'-p','prompt.txt','-l',value],
                        cwd=project,env=env,text=True,capture_output=True,timeout=30)
                    self.assertEqual(result.returncode,4,result.stdout+result.stderr)
                    self.assertIn('repo-relative',result.stderr)
                    self.assertFalse(trace.exists())
                    self.assertFalse((base/'outside').exists())

if __name__=='__main__':unittest.main()
