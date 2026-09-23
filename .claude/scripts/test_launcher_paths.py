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
    def test_sensitive_lane_requires_runtime_policy_denials(self):
        bash = find_bash()
        if not bash: self.skipTest('Bash unavailable')
        script = Path(__file__).with_name('lane-sensitive.sh')
        if not script.exists(): self.skipTest('Optional sensitive lane is not installed')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / 'project'
            (project / '.claude').mkdir(parents=True)
            (project / '.claude/sandbox-sensitive.json').write_text('{}')
            home = base / 'home'; home.mkdir()
            binpath = base / 'bin'; binpath.mkdir()
            engine = base / 'engine.py'
            engine.write_text('''import json, os, sys
if '-p' not in sys.argv:
    print('INTERACTIVE_LAUNCH_ADMITTED'); sys.exit(0)
commands = [line[3:] for line in sys.stdin.read().splitlines() if line.startswith(('1. ', '2. '))]
case = os.environ['PROBE_CASE']
if case == 'startup':
    print('fixture startup error', file=sys.stderr); sys.exit(2)
def emit(value): print(json.dumps(value), flush=True)
if case == 'claims':
    emit({'type':'result','subtype':'success','result':'1: NET_BLOCKED\\n2: WRITE_OUT_BLOCKED'}); sys.exit(0)
network = 'HTTP/1.1 403 Forbidden\\r\\nX-Proxy-Error: blocked-by-allowlist\\r\\n'
if case in ('6', '28', '60'): network = 'curl: (' + case + ') transport failed'
if case == 'generic403': network = 'HTTP/1.1 403 Forbidden\\r\\n'
if case == 'open': network = 'HTTP/1.1 200 OK\\r\\n'
for i, command in enumerate(commands):
    args = {'command': command}
    if case == 'bypass': args['dangerouslyDisableSandbox'] = True
    emit({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash','id':str(i),'input':args}]}})
    emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':str(i),'content':network if i == 0 else 'bash: target: Permission denied','is_error':True}]}})
emit({'type':'result','subtype':'success','is_error':False,'result':'done'})
''', encoding='utf-8')
            for tool in ('python3', 'claude'):
                stub = binpath / tool
                stub.write_text('#!/usr/bin/env bash\nexec "$FIXTURE_PY" ' +
                                ('"$FIXTURE_ENGINE" ' if tool == 'claude' else '') + '"$@"\n', newline='\n')
                stub.chmod(0o755)
            env = {**os.environ, 'HOME': home.as_posix(), 'FIXTURE_PY': sys.executable,
                   'FIXTURE_ENGINE': engine.as_posix(), 'PATH': str(binpath) + os.pathsep + os.environ['PATH']}
            for case in ('policy', '6', '28', '60', 'generic403', 'open', 'claims', 'bypass', 'startup'):
                with self.subTest(case=case):
                    result = subprocess.run([bash, str(script.resolve())], cwd=project,
                                            env={**env, 'PROBE_CASE': case}, text=True,
                                            encoding='utf-8', capture_output=True, timeout=15)
                    if case == 'policy':
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('INTERACTIVE_LAUNCH_ADMITTED', result.stdout)
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn('fixture startup error' if case == 'startup' else 'inconclusive probe', result.stderr)
                        self.assertNotIn('INTERACTIVE_LAUNCH_ADMITTED', result.stdout)

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
