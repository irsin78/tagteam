"""The intentionally narrow guard contract; fixtures are data, never executed."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch
import deny_dangerous as guard

def shell(argv, delegate=False, command_tail=None, **extra):
    command = shlex.join(argv) + (' && ' + shlex.join(command_tail) if command_tail else '')
    data = {'tool_name': 'Bash', 'tool_input': {'command': command}}
    if delegate:
        data['agent_id'] = 'worker'
    return guard.evaluate(dict(data, **extra))

class GuardTests(unittest.TestCase):
    def test_direct_protections(self):
        for argv in (['git','push','--force'], ['git','-C','.','reset','--hard'],
                     ['git','clean','-fd'], ['codex','exec','--sandbox','danger-full-access'],
                     ['codex','exec','--skip-git-repo-check'], ['claude','--dangerously-skip-permissions']):
            with self.subTest(argv=argv):
                self.assertIsNotNone(shell(argv))
    def test_delegate_grant_does_not_allow_commit(self):
        for host in ('claude','codex'):
            for verb in ('commit','push'):
                data={'tool_name':'Bash','tool_input':{'command':shlex.join(['git',verb])}}
                self.assertIsNotNone(guard.evaluate_host(data,host,
                    {'HARNESS_DELEGATE_RUN':'1','HARNESS_ALLOW_CONTROL_PLANE':'1'}))
    def test_writes_and_explicit_grant(self):
        for host in ('claude','codex'):
            for path in ('.claude/settings.json','.claude/harness-config.json',
                         '.claude/hooks/new.py','.codex/hooks.json','AGENTS.md'):
                data={'tool_name':'Write','tool_input':{'file_path':path}}
                self.assertIsNotNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,
                    {'HARNESS_DELEGATE_RUN':'1','HARNESS_ALLOW_CONTROL_PLANE':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,{}))
    def test_project_agent_definition_is_control_plane(self):
        for host in ('claude', 'codex'):
            data = {'tool_name': 'Write', 'tool_input': {'file_path': '.agents/agents/project-agent.md'}}
            self.assertIsNotNone(guard.evaluate_host(data, host, {'HARNESS_DELEGATE_RUN': '1'}))
            self.assertIsNone(guard.evaluate_host(data, host, {}))

    def test_patch_rename(self):
        data={'tool_name':'apply_patch','tool_input':{'command':'*** Update File: x\n*** Move to: .codex/hooks.json\n'}}
        self.assertIsNotNone(guard.evaluate_host(data,'codex',{'HARNESS_DELEGATE_RUN':'1'}))
    def test_relocated_orchestrator_policy_keeps_worker_protection(self):
        for name in ('delegation-matrix','retry-policy'):
            path=f'docs/orchestration/{name}.md'
            for host in ('claude','codex'):
                data={'tool_name':'Write','tool_input':{'file_path':path}}
                self.assertIsNotNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1','HARNESS_ALLOW_CONTROL_PLANE':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,{}))
            self.assertIsNotNone(shell(['tee',path],True))
        self.assertIsNone(shell(['tee','docs/orchestration/project-notes.md'],True))
    def test_normal_text_is_data(self):
        dangerous=shlex.join(['git','reset','--hard'])
        for argv in (['git','log','--grep',dangerous], ['LC_ALL=C','git','log','--grep',dangerous],
                     ['sed','-n','/'+dangerous+'/p','notes.md'], ['rg','HARNESS_ALLOW_CONTROL_PLANE=1','.claude']):
            with self.subTest(argv=argv):
                self.assertIsNone(shell(argv,delegate=True))
    def test_direct_sequence_and_environment(self):
        command=shlex.join(['git','status'])+';\n'+shlex.join(['git','commit'])
        self.assertIsNotNone(guard.evaluate({'agent_id':'worker','tool_name':'Bash','tool_input':{'command':command}}))
        self.assertIsNotNone(shell(['HARNESS_ALLOW_CONTROL_PLANE=1','bash','.claude/scripts/claude-run.sh'],True))
    def test_opaque_programs_are_outside_detection(self):
        danger=shlex.join(['git','reset','--hard'])
        for command in ("python -c "+shlex.quote("print("+repr(danger)+")"),
                        "cat <<'EOF'\n"+danger+"\nEOF", "echo $("+danger+")"):
            self.assertIsNone(guard.evaluate({'tool_name':'Bash','tool_input':{'command':command}}))
    def test_wire_denial(self):
        for path in ('.claude/settings.json', '.claude/harness-config.json'):
            with self.subTest(path=path):
                data={'agent_id':'worker','tool_name':'Write','tool_input':{'file_path':path}}
                result=subprocess.run([sys.executable,str(Path(guard.__file__)),'--host','codex'],
                    input=json.dumps(data),text=True,capture_output=True,env={**os.environ,'HARNESS_ALLOW_CONTROL_PLANE':'0'})
                self.assertEqual(result.returncode,0)
                self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'],'deny')
    def test_known_command_before_opaque_arguments(self):
        for command in ('git commit -m "$(cat <<EOF\nmessage\nEOF\n)"',
                        'git -C . push origin "$(echo main)"',
                        'git reset --hard "$(echo HEAD)"', 'git commit -m `date`',
                        'git commit <<EOF\nmessage\nEOF'):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.evaluate({'agent_id':'worker','tool_name':'Bash',
                                                    'tool_input':{'command':command}}))
    def test_opaque_prefix_does_not_invent_commands_or_arguments(self):
        for command in ('echo "git push $(date)"', "printf '%s' 'git commit <<EOF'",
                        'git log --grep "git commit $(date)"', 'git$(echo x) push',
                        'git commi$(echo t)', 'echo "$(date)"; git push'):
            with self.subTest(command=command):
                self.assertIsNone(guard.evaluate({'agent_id':'worker','tool_name':'Bash',
                                                 'tool_input':{'command':command}}))
    def ensure_template_dir(self, configured=True):
        """The exemption needs <root>/template to exist; create it for the test when absent."""
        root=guard.PROJECT_ROOT
        folder=os.path.join(root,'template')
        if not os.path.isdir(folder):
            os.mkdir(folder)
            self.addCleanup(os.rmdir,folder)
        if configured:
            configuration=patch.object(guard,'open',
                mock_open(read_data=json.dumps({'content_dirs':['template']})),create=True)
            configuration.start()
            self.addCleanup(configuration.stop)
        return root
    def assert_template_copy_denied(self, root):
        rel='template/.claude/settings.json'
        for path in (rel, os.path.join(root,rel)):
            for host in ('claude','codex'):
                data={'tool_name':'Write','tool_input':{'file_path':path},'cwd':root}
                self.assertIsNotNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
        for argv in (['tee',rel], ['cp','/tmp/new',rel]):
            self.assertIsNotNone(shell(argv,True,cwd=root))
        self.assertIsNotNone(guard.evaluate_host({'tool_name':'apply_patch','cwd':root,
            'tool_input':{'command':'*** Update File: '+rel+'\n'}},
            'codex',{'HARNESS_DELEGATE_RUN':'1'}))
    def test_default_content_dirs_deny_template_copy(self):
        root=self.ensure_template_dir(configured=False)
        with patch.object(guard,'open',mock_open(read_data='{"content_dirs":[]}'),create=True) as source:
            self.assert_template_copy_denied(root)
            source.assert_any_call(os.path.join(root,'.claude','harness-config.json'),encoding='utf-8-sig')
    @unittest.skipUnless(os.path.exists(os.path.join(guard.PROJECT_ROOT,'AGENTS.md.template')),
                         'template default only; an installed copy may configure content_dirs')
    def test_shipped_content_dirs_are_empty(self):
        self.assertEqual(guard.content_dirs(), ())
    def test_default_content_dirs_deny_other_payloads(self):
        root=self.ensure_template_dir(configured=False)
        rel='template/.claude/settings.json'
        paths=(rel, os.path.join(root,rel), rel.replace('/', '\\'),
               r'C:\project\template\.claude\settings.json',
               'C:/project/template/.claude/settings.json')
        with patch.object(guard,'open',mock_open(read_data='{"content_dirs":[]}'),create=True):
            for tool, key in (('Write','file_path'), ('Edit','file_path'), ('NotebookEdit','notebook_path')):
                for path in paths:
                    for host in ('claude','codex'):
                        with self.subTest(tool=tool,path=path,host=host):
                            data={'tool_name':tool,'tool_input':{key:path},'cwd':root}
                            self.assertIsNotNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
            for path in paths:
                with self.subTest(path=path):
                    self.assertIsNotNone(shell(['mv','/tmp/new',path],True,cwd=root))
                    self.assertIsNotNone(guard.evaluate({'agent_id':'worker','tool_name':'Bash','cwd':root,
                        'tool_input':{'command':'echo x > '+shlex.quote(path)}}))
            self.assertIsNotNone(guard.evaluate({'agent_id':'worker','tool_name':'Bash','cwd':root,
                'tool_input':{'command':'cd template && tee .claude/settings.json'}}))
    def test_bom_content_config_is_read_and_invalid_input_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=guard.PROJECT_ROOT) as root:
            configuration=Path(root,'.claude','harness-config.json')
            configuration.parent.mkdir()
            Path(root,'template').mkdir()
            with patch.object(guard,'PROJECT_ROOT',root):
                configuration.write_text('{"content_dirs":["template"]}',encoding='utf-8-sig')
                self.assertEqual(guard.content_dirs(), ('template',))
                self.assertIsNone(guard.evaluate_host({'tool_name':'Write','cwd':root,
                    'tool_input':{'file_path':'template/.claude/settings.json'}},
                    'codex',{'HARNESS_DELEGATE_RUN':'1'}))
                for invalid in ('{', '{"content_dirs":["template",".codex"]}'):
                    with self.subTest(configuration=invalid):
                        configuration.write_text(invalid,encoding='utf-8-sig')
                        self.assertEqual(guard.content_dirs(), ())
                        self.assert_template_copy_denied(root)
    def test_invalid_content_config_denies_template_copy(self):
        root=self.ensure_template_dir(configured=False)
        configurations=['{', '[' * (sys.getrecursionlimit() + 1),
                        'not JSON', 'null', '[]', '"template"', '1', '{}']
        configurations += [json.dumps({'content_dirs':value})
                           for value in (None, {}, 'template', 1, False)]
        invalid_names=(None, 1, False, [], {}, '', '.', '..', 'template..copy',
                       'nested/template', 'nested\\template', '.claude', '.CLAUDE',
                       '.codex', '.CODEX', '.agents', '.gemini', '.content',
                       'C:template', 'template\0', '.claude.', '.claude ', 'template.', 'template ')
        configurations += [json.dumps({'content_dirs':['template',name]}) for name in invalid_names]
        for configuration in configurations:
            with self.subTest(configuration=configuration), patch.object(guard,'open',
                    mock_open(read_data=configuration),create=True):
                self.assert_template_copy_denied(root)
    def test_unreadable_content_config_denies_template_copy(self):
        root=self.ensure_template_dir(configured=False)
        errors=(FileNotFoundError('missing'), PermissionError('unreadable'),
                IsADirectoryError('not a file'), UnicodeDecodeError('utf-8',b'\xff',0,1,'invalid'))
        for error in errors:
            with self.subTest(error=error), patch.object(guard,'open',side_effect=error,create=True):
                self.assert_template_copy_denied(root)
    def test_content_dirs_can_configure_other_root_names(self):
        root=self.ensure_template_dir(configured=False)
        with patch.object(guard,'open',
                mock_open(read_data='{"content_dirs":["template","docs"]}'),create=True):
            for name in ('template','docs'):
                data={'tool_name':'Write','cwd':root,
                      'tool_input':{'file_path':name+'/.claude/settings.json'}}
                self.assertIsNone(guard.evaluate_host(data,'codex',{'HARNESS_DELEGATE_RUN':'1'}))
    def test_content_folder_link_to_control_plane_is_not_exempt(self):
        with tempfile.TemporaryDirectory(dir=guard.PROJECT_ROOT) as root:
            live_folder=os.path.join(root,'.claude')
            os.mkdir(live_folder)
            folder=os.path.join(root,'template')
            try:
                if os.name == 'nt':
                    result=subprocess.run(['cmd.exe','/c','mklink','/J',folder,live_folder],
                                          text=True,capture_output=True)
                    if result.returncode:
                        self.skipTest(f'junction not available: {result.stderr or result.stdout}')
                else:
                    os.symlink(live_folder,folder,target_is_directory=True)
            except (OSError,NotImplementedError,AttributeError) as exc:
                self.skipTest(f'directory link not available: {exc}')
            try:
                with patch.object(guard,'PROJECT_ROOT',root), patch.object(guard,'open',
                        mock_open(read_data='{"content_dirs":["template"]}'),create=True):
                    self.assertTrue(os.path.samefile(folder,live_folder))
                    rel='.claude/hooks/x.py'
                    for path in (rel, os.path.join(root,rel)):
                        with self.subTest(path=path):
                            self.assertFalse(guard.template_path(path,root))
                            for host in ('claude','codex'):
                                self.assertIsNotNone(guard.evaluate_host({'tool_name':'Write','cwd':root,
                                    'tool_input':{'file_path':path}},host,{'HARNESS_DELEGATE_RUN':'1'}))
                            self.assertIsNotNone(shell(['tee',path],True,cwd=root))
            finally:
                os.rmdir(folder) if os.name == 'nt' else os.unlink(folder)
    def test_content_config_is_protected_with_content_exemption(self):
        root=self.ensure_template_dir()
        rel='.claude/harness-config.json'
        for path in (rel, os.path.join(root,rel)):
            for host in ('claude','codex'):
                self.assertIsNotNone(guard.evaluate_host({'tool_name':'Write','cwd':root,
                    'tool_input':{'file_path':path}},host,{'HARNESS_DELEGATE_RUN':'1'}))
        self.assertIsNotNone(shell(['tee',rel],True,cwd=root))
        self.assertIsNotNone(shell(['cp','/tmp/new',rel],True,cwd=root))
        self.assertIsNotNone(guard.evaluate_host({'tool_name':'apply_patch','cwd':root,
            'tool_input':{'command':'*** Update File: '+rel+'\n'}},
            'codex',{'HARNESS_DELEGATE_RUN':'1'}))
    def test_template_copy_is_content_not_control_plane(self):
        root=self.ensure_template_dir()
        for rel in ('template/.claude/hooks/deny_dangerous.py','template/.claude/settings.json',
                    'template/.codex/hooks.json','template/CLAUDE.md',
                    'template/docs/orchestration/delegation-matrix.md'):
            for path in (rel, os.path.join(root, rel)):
                for host in ('claude','codex'):
                    data={'tool_name':'Write','tool_input':{'file_path':path},'cwd':root}
                    self.assertIsNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
            self.assertIsNone(shell(['tee',rel],True,cwd=root))
            self.assertIsNone(shell(['cp','/tmp/new',rel],True,cwd=root))
            self.assertIsNotNone(shell(['tee',rel],True,cwd=root,agent_type='opus-architect'))
        patch='*** Update File: x\n*** Move to: template/.codex/hooks.json\n'
        self.assertIsNone(guard.evaluate_host({'tool_name':'apply_patch','tool_input':{'command':patch},'cwd':root},
                                              'codex',{'HARNESS_DELEGATE_RUN':'1'}))
        for rel in ('template/../.claude/hooks/x.py','template-x/.claude/hooks/x.py',
                    'docs/template/.claude/hooks/x.py','.claude/hooks/template/x.py'):
            with self.subTest(rel=rel):
                data={'tool_name':'Write','tool_input':{'file_path':rel},'cwd':root}
                self.assertIsNotNone(guard.evaluate_host(data,'claude',{'HARNESS_DELEGATE_RUN':'1'}))
                self.assertIsNotNone(shell(['tee',rel],True,cwd=root))
        data={'tool_name':'Write','tool_input':{'file_path':'template/.claude/hooks/x.py'},'cwd':os.path.join(root,'docs')}
        self.assertIsNotNone(guard.evaluate_host(data,'claude',{'HARNESS_DELEGATE_RUN':'1'}))
    def test_template_exemption_fails_closed(self):
        root=self.ensure_template_dir()
        live='.claude/hooks/x.py'
        # cd tracking: only a plain cd into an existing directory is followed.
        self.assertIsNone(shell(['cd','template'],True,cwd=root,command_tail=['tee',live]))
        for prefix in (['cd','template'],['cd','--','template']):
            self.assertIsNone(guard.evaluate({'agent_id':'w','tool_name':'Bash','cwd':root,
                'tool_input':{'command':shlex.join(prefix)+' && '+shlex.join(['tee',live])}}))
        for chain in ('cd template && cd -- .. && tee '+live, 'cd template && cd - && tee '+live,
                      'cd template && cd && tee '+live, 'cd template && cd ~ && tee '+live,
                      'cd no-such-dir && tee template/'+live, 'cd template && cd -P .. && tee '+live):
            with self.subTest(chain=chain):
                self.assertIsNotNone(guard.evaluate({'agent_id':'w','tool_name':'Bash','cwd':root,
                                                     'tool_input':{'command':chain}}))
        # a known control-plane cwd keeps protecting bare filenames, even after an unknown cd.
        for chain in ('cd .claude/hooks && tee deny_dangerous.py', 'cd .claude/hooks && cd - && tee deny_dangerous.py',
                      'cd .claude && cd hooks && tee x.py'):
            with self.subTest(chain=chain):
                self.assertIsNotNone(guard.evaluate({'agent_id':'w','tool_name':'Bash','cwd':root,
                                                     'tool_input':{'command':chain}}))
        # a cd inside a pipeline, in the background, or on a failure branch does not move the shell.
        for chain in ('cd template | tee '+live, 'cd template |& tee '+live, 'cd template & tee '+live,
                      'echo x | cd template && tee '+live, 'cd template || tee '+live,
                      'cd template | cat; tee '+live, 'cd template |\ntee '+live, 'cd template &\ntee '+live,
                      'cd template && echo x & tee '+live, 'cd template && echo x &\ntee '+live,
                      'cd template ;; tee '+live, 'cd template &&& tee '+live, 'cd template ;& tee '+live,
                      'cd template && cd . | cat && tee '+live, 'cd template || exit 1; tee '+live):
            with self.subTest(chain=chain):
                self.assertIsNotNone(guard.evaluate({'agent_id':'w','tool_name':'Bash','cwd':root,
                                                     'tool_input':{'command':chain}}))
        for chain in ('cd template; tee '+live, 'cd template\ntee '+live, 'ls | cat && cd template && tee '+live,
                      'cd template ;\n tee '+live, 'sleep 1 & cd template && tee '+live,
                      'cd template && tee '+live+' &', 'cd docs & cd template && tee '+live):
            with self.subTest(chain=chain):
                self.assertIsNone(guard.evaluate({'agent_id':'w','tool_name':'Bash','cwd':root,
                                                  'tool_input':{'command':chain}}))
        # unknown cwd: relative template paths are not exempt, absolute ones still are.
        self.assertFalse(guard.template_path('template/'+live, None))
        self.assertTrue(guard.template_path(os.path.join(root,'template',live), None))
        # missing template folder (a copied guard whose root has no template/): nothing is exempt.
        saved=guard.PROJECT_ROOT
        try:
            guard.PROJECT_ROOT=os.path.join(root,'template')
            self.assertFalse(guard.template_path(os.path.join(root,'template','template',live), None))
            self.assertIsNotNone(guard.evaluate_host({'tool_name':'Write','tool_input':{'file_path':live},
                'cwd':guard.PROJECT_ROOT},'claude',{'HARNESS_DELEGATE_RUN':'1'}))
        finally:
            guard.PROJECT_ROOT=saved
        # case: exempt only when the spelling names the same directory on disk.
        upper=os.path.join(root,'TEMPLATE')
        same=os.path.isdir(upper) and os.path.samefile(upper,os.path.join(root,'template'))
        data={'tool_name':'Write','tool_input':{'file_path':'TEMPLATE/'+live},'cwd':root}
        verdict=guard.evaluate_host(data,'claude',{'HARNESS_DELEGATE_RUN':'1'})
        self.assertIsNone(verdict) if same else self.assertIsNotNone(verdict)
        # links: a link inside template/ that points back at the project root is not exempt.
        link=os.path.join(root,'template','.guard-test-link')
        try:
            os.symlink(root,link,target_is_directory=True)
        except (OSError,NotImplementedError,AttributeError) as exc:
            self.skipTest(f'symlink not available: {exc}')
        try:
            data={'tool_name':'Write','tool_input':{'file_path':'template/.guard-test-link/'+live},'cwd':root}
            self.assertIsNotNone(guard.evaluate_host(data,'claude',{'HARNESS_DELEGATE_RUN':'1'}))
            self.assertIsNotNone(shell(['tee','template/.guard-test-link/'+live],True,cwd=root))
        finally:
            os.unlink(link)
    def test_direct_config_target_and_copy_source(self):
        self.assertIsNotNone(shell(['cp','/tmp/new','.claude/settings.json'],True))
        self.assertIsNone(shell(['cp','.claude/settings.json','/tmp/backup'],True))

if __name__ == '__main__':
    unittest.main()
