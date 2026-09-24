"""Bounded local reads exercise the real HTTP/CLI path without cloud calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unicodedata
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
    def test_parent_alias_cannot_bypass_read_policy(self):
        (self.root/'private').mkdir(); (self.root/'public').mkdir()
        (self.root/'private/note.txt').write_text('must not be sent', encoding='utf-8')
        policies = ['private/**', './private/**', (self.root/'private').as_posix()+'/**']
        for policy in policies:
            (self.root/'.claude/settings.json').write_text(json.dumps({'permissions': {'deny': [f'Read({policy})']}}))
            for name in ('private/note.txt', 'public/../private/note.txt'):
                with self.subTest(policy=policy, name=name):
                    (self.root/'inputs.json').write_text(json.dumps([{'path': name}]))
                    self.assertEqual(self.run_reader().returncode, 4)
        self.assertEqual(Handler.requests, [])
    def test_windows_name_aliases_cannot_bypass_exclusions(self):
        # r2 N3: Windows opens these spellings as the excluded file itself.
        (self.root/'.env').write_text('credential', encoding='utf-8')
        (self.root/'private').mkdir()
        (self.root/'private/note.txt').write_text('must not be sent', encoding='utf-8')
        (self.root/'secret.txt').write_text('must not be sent', encoding='utf-8')
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions': {'deny': [
            'Read(private/**)', 'Read(**/secret.txt)']}}))
        for name in ('private./note.txt', '.env ', '.env::$DATA', 'secret.txt::$DATA', 'secret.txt.',
                     'PRIVATE/Note.txt', '.ENV'):
            with self.subTest(name=name):
                (self.root/'inputs.json').write_text(json.dumps([{'path': name}]))
                self.assertEqual(self.run_reader().returncode, 4)
        self.assertEqual(Handler.requests, [])
        (self.root/'inputs.json').write_text(json.dumps([{'path': 'source.txt', 'start': 2, 'end': 2}]))
        self.assertEqual(self.run_reader().returncode, 0)
    def test_exact_spelling_blocks_case_aliases_before_volume_check(self):
        (self.root/'private').mkdir()
        (self.root/'private/note.txt').write_text('must not be sent', encoding='utf-8')
        (self.root/'.env').write_text('credential', encoding='utf-8')
        patterns=reader.read_patterns(self.root)
        with patch.object(reader, 'case_insensitive_volume', return_value=False) as detector:
            for name in ('PRIVATE/Note.txt', '.ENV'):
                with self.subTest(name=name):
                    with self.assertRaises(reader.InputError):
                        reader.readable_path(self.root, name, patterns)
        detector.assert_not_called()
        self.assertEqual(Handler.requests, [])
    def test_swap_that_does_not_fold_is_not_a_probe(self):
        # U+0131 swaps to I, which casefolds to i: not the same name anywhere.
        self.assertEqual(reader.swapped_case('\u0131x'), '\u0131X')
        self.assertEqual(reader.swapped_case('\u0131'), '\u0131')
    def test_one_negative_entry_cannot_outvote_proof_of_insensitivity(self):
        if not reader.case_insensitive_volume(self.root):
            self.skipTest('temporary volume is case-sensitive')
        probe=self.root/'case-probe';probe.mkdir()
        (probe/'Abc').write_text('x');(probe/'Def').write_text('x')
        # Whatever the listing order, the first probe reads as a clean negative.
        calls=[]
        def swap(name):
            calls.append(name)
            return 'zz-missing' if len(calls)==1 else name[0].swapcase()+name[1:]
        with patch.object(reader, 'swapped_case', side_effect=swap):
            self.assertTrue(reader.case_insensitive_volume(probe))
    def test_dangling_symlink_does_not_make_volume_look_sensitive(self):
        if not reader.case_insensitive_volume(self.root):
            self.skipTest('temporary volume is case-sensitive')
        probe=self.root/'case-probe';probe.mkdir()
        try: (probe/'Dangling').symlink_to(probe/'missing')
        except OSError: self.skipTest('symlink creation not permitted')
        self.assertEqual(os.listdir(probe), ['Dangling'])
        self.assertTrue(reader.case_insensitive_volume(probe))
    def test_policy_case_follows_volume(self):
        if not reader.case_insensitive_volume(self.root):
            self.skipTest('temporary volume is case-sensitive')
        (self.root/'private').mkdir();(self.root/'private/note.txt').write_text('must not be sent')
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions':{'deny':['Read(Private/**)']}}))
        (self.root/'inputs.json').write_text(json.dumps([{'path':'private/note.txt'}]))
        self.assertEqual(self.run_reader().returncode,4);self.assertEqual(Handler.requests,[])
    def test_case_insensitive_policy_keeps_raw_question_mark_match(self):
        if not reader.case_insensitive_volume(self.root):
            self.skipTest('temporary volume is case-sensitive')
        directory_name='größe'
        try: (self.root/directory_name).mkdir()
        except OSError: self.skipTest('filesystem refuses the Unicode directory name')
        entries=[name for name in os.listdir(self.root)
                 if unicodedata.normalize('NFC',name)==directory_name and (self.root/name).is_dir()]
        if len(entries) != 1:
            self.skipTest('filesystem refuses the Unicode directory name')
        stored_name=entries[0]
        (self.root/stored_name/'secret.txt').write_text('must not be sent', encoding='utf-8')
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions': {
            'deny': ['Read(gr??e/**)']}}))
        input_path=(Path(stored_name)/'secret.txt').as_posix()
        (self.root/'inputs.json').write_text(json.dumps([{'path': input_path}]))
        self.assertEqual(self.run_reader().returncode,4);self.assertEqual(Handler.requests,[])
    def test_core_exclusion_case_is_always_insensitive(self):
        if reader.case_insensitive_volume(self.root):
            self.skipTest('temporary volume is case-insensitive')
        (self.root/'.ENV').write_text('credential')
        (self.root/'inputs.json').write_text(json.dumps([{'path':'.ENV'}]))
        self.assertEqual(self.run_reader().returncode,4);self.assertEqual(Handler.requests,[])
    def assert_unicode_policy_blocked(self, directory_name, policy_name):
        try: (self.root/directory_name).mkdir()
        except OSError: self.skipTest('filesystem refuses to create the Unicode directory name')
        expected=unicodedata.normalize('NFC',directory_name)
        entries=[name for name in os.listdir(self.root)
                 if unicodedata.normalize('NFC',name)==expected and (self.root/name).is_dir()]
        self.assertEqual(len(entries),1)
        stored_name=entries[0]
        (self.root/stored_name/'note.txt').write_text('must not be sent',encoding='utf-8')
        policy=f'Read({policy_name}/**)'
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions':{'deny':[policy]}}))
        input_path=(Path(stored_name)/'note.txt').as_posix()
        (self.root/'inputs.json').write_text(json.dumps([{'path':input_path}]))
        self.assertEqual(self.run_reader().returncode,4);self.assertEqual(Handler.requests,[])
    def test_nfc_policy_blocks_nfd_directory(self):
        nfc=unicodedata.normalize('NFC','비밀')
        self.assert_unicode_policy_blocked(unicodedata.normalize('NFD',nfc),nfc)
    def test_nfd_policy_blocks_nfc_directory(self):
        nfc=unicodedata.normalize('NFC','비밀')
        self.assert_unicode_policy_blocked(nfc,unicodedata.normalize('NFD',nfc))
    def test_absolute_policy_resolves_symlinked_parent(self):
        (self.root/'actual/private').mkdir(parents=True)
        (self.root/'actual/private/note.txt').write_text('must not be sent')
        try: (self.root/'alias').symlink_to(self.root/'actual',target_is_directory=True)
        except OSError: self.skipTest('symlink creation not permitted')
        policy=(self.root/'alias/private').as_posix()+'/**'
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions':{'deny':[f'Read({policy})']}}))
        (self.root/'inputs.json').write_text(json.dumps([{'path':'actual/private/note.txt'}]))
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
