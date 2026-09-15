"""Bounded local reads exercise the real HTTP/CLI path without cloud calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

SCRIPT=Path(__file__).with_name('local-read.py')
spec=importlib.util.spec_from_file_location('reader',SCRIPT)
reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)

class Handler(BaseHTTPRequestHandler):
    requests=[]
    result='summary only'
    finish_reason='stop'
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(body)
        if self.path.startswith('/redirect/'):
            self.send_response(307);self.send_header('Location','/v1/chat/completions');self.end_headers();return
        data=json.dumps({'model':'fixture','choices':[{'finish_reason':self.finish_reason,'message':{'content':self.result}}]}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args): pass

class LocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=HTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'.claude').mkdir()
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions':{'deny':['Read(./private/**)']}}))
        (self.root/'.claude/model-bindings.local.json').write_text(json.dumps({'vendors':{'local':{'endpoint':{
            'base_url':f'http://127.0.0.1:{self.server.server_port}/v1','model':'fixture'}}}}))
        (self.root/'source.txt').write_text('first\nkeep this\nlast\n',encoding='utf-8')
        (self.root/'prompt.txt').write_text('Summarize',encoding='utf-8')
        (self.root/'inputs.json').write_text(json.dumps([{'path':'source.txt','start':2,'end':2}]))
        Handler.requests.clear();Handler.result='summary only';Handler.finish_reason='stop'
    def run_reader(self,*extra):
        return subprocess.run([sys.executable,str(SCRIPT),'-i','inputs.json','-p','prompt.txt',*extra],
            cwd=self.root,capture_output=True,text=True,
            env={**os.environ,'HARNESS_DELEGATE_RUN':'0'})
    def test_nongit_read_range_and_no_raw_output(self):
        result=self.run_reader()
        self.assertEqual(result.returncode,0,result.stderr)
        body=Handler.requests[0]['messages'][-1]['content']
        self.assertIn('source.txt:2: keep this',body);self.assertNotIn('first',body)
        self.assertNotIn('keep this',result.stdout)
        self.assertIn('summary only',result.stdout);self.assertIn('CHANGED: not measured',result.stdout)
        self.assertFalse((self.root/'.git').exists())
    def test_literal_search(self):
        (self.root/'inputs.json').write_text(json.dumps([{'path':'source.txt','contains':'last'}]))
        self.assertEqual(self.run_reader().returncode,0)
        self.assertIn('source.txt:3: last',Handler.requests[-1]['messages'][-1]['content'])
    def test_sensitive_and_outside_inputs_never_sent(self):
        (self.root/'.env').write_text('credential')
        (self.root/'private').mkdir();(self.root/'private/key').write_text('credential')
        for path in ('.env','private/key','../outside'):
            (self.root/'inputs.json').write_text(json.dumps([{'path':path}]))
            self.assertEqual(self.run_reader().returncode,4)
        self.assertEqual(Handler.requests,[])
    def test_symlink_inputs(self):
        try: (self.root/'linked').symlink_to(self.root/'source.txt')
        except OSError: self.skipTest('symlink creation not permitted on this Windows account')
        (self.root/'inputs.json').write_text(json.dumps([{'path':'linked'}]))
        self.assertEqual(self.run_reader().returncode,4);self.assertEqual(Handler.requests,[])
    def test_nonregular_input_is_rejected(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO creation unavailable on Windows')
        os.mkfifo(self.root/'pipe')
        (self.root/'inputs.json').write_text(json.dumps([{'path':'pipe'}]))
        self.assertEqual(self.run_reader().returncode,4)
        self.assertEqual(Handler.requests,[])
    def test_commands_are_not_an_input_type(self):
        (self.root/'inputs.json').write_text(json.dumps([{'command':'echo hello > unexpected'}]))
        self.assertEqual(self.run_reader().returncode,4)
        self.assertEqual(self.run_reader('-c','source.txt').returncode,4)
        self.assertFalse((self.root/'unexpected').exists());self.assertEqual(Handler.requests,[])
    def test_invalid_log_path_checked_before_request(self):
        self.assertEqual(self.run_reader('-l','../outside').returncode,4)
        self.assertEqual(Handler.requests,[])
    def test_response_and_truncation_contract(self):
        Handler.result=''
        self.assertEqual(self.run_reader().returncode,1)
        Handler.result='partial';Handler.finish_reason='length'
        self.assertEqual(self.run_reader().returncode,1)
        Handler.finish_reason='stop';Handler.result='\n'.join(str(n) for n in range(300))
        result=self.run_reader('-n','3')
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.split('FINAL_MESSAGE:\n',1)[1].strip(),'0\n1\n2')
        self.assertIn('at most 3 short lines', Handler.requests[-1]['messages'][0]['content'])
        self.assertEqual(Handler.requests[-1]['max_tokens'], 8000)
        self.assertIn('STARTED: ', result.stdout)
    def test_timing_separates_input_work_from_endpoint_request(self):
        result=self.run_reader()
        self.assertEqual(result.returncode,0,result.stderr)
        line=next(line for line in result.stdout.splitlines() if line.startswith('TIMING: '))
        fields=dict(part.split('=',1) for part in line.split()[1:])
        phases=[int(fields[key]) for key in ('preflight_ms','request_ms','postflight_ms','verify_ms')]
        self.assertEqual(sum(phases),int(fields['total_ms']))
        self.assertTrue(all(value>=0 for value in phases))
        self.assertNotIn('cli_ms',fields)
        self.assertEqual(fields['attempts'],'1')
    def test_input_cap_is_visible(self):
        path=self.root/'.claude/model-bindings.local.json'
        data=json.loads(path.read_text());data['vendors']['local']['endpoint']['max_input_chars']=8
        path.write_text(json.dumps(data))
        result=self.run_reader()
        self.assertEqual(result.returncode,0)
        self.assertIn('INPUT_TRUNCATED: true',result.stdout)
    def test_redirect_is_not_followed(self):
        path=self.root/'.claude/model-bindings.local.json'
        data=json.loads(path.read_text());data['vendors']['local']['endpoint']['base_url']=f'http://127.0.0.1:{self.server.server_port}/redirect'
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_reader().returncode,2)
        self.assertEqual(len(Handler.requests),1)

if __name__=='__main__':unittest.main()
