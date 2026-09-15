"""The intentionally narrow guard contract; fixtures are data, never executed."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
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
            for path in ('.claude/settings.json','.claude/hooks/new.py','.codex/hooks.json','AGENTS.md'):
                data={'tool_name':'Write','tool_input':{'file_path':path}}
                self.assertIsNotNone(guard.evaluate_host(data,host,{'HARNESS_DELEGATE_RUN':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,
                    {'HARNESS_DELEGATE_RUN':'1','HARNESS_ALLOW_CONTROL_PLANE':'1'}))
                self.assertIsNone(guard.evaluate_host(data,host,{}))
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
        data={'agent_id':'worker','tool_name':'Write','tool_input':{'file_path':'.claude/settings.json'}}
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
    def ensure_template_dir(self):
        """The exemption needs <root>/template to exist; create it for the test when absent."""
        root=guard.PROJECT_ROOT
        folder=os.path.join(root,'template')
        if not os.path.isdir(folder):
            os.mkdir(folder)
            self.addCleanup(os.rmdir,folder)
        return root
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
