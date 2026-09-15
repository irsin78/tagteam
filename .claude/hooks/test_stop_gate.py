#!/usr/bin/env python
"""Regression test for stop_gate.py. Run: python test_stop_gate.py

Each case uses its own disposable marker sandbox; no repository gate marker is
read, written, or removed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_gate.py")
SESSION = os.path.join(os.path.dirname(HOOK), '..', 'scripts', 'harness-session.py')


def sandbox():
    """Return a temporary cwd with the gate's marker directory present."""
    directory = tempfile.mkdtemp()
    os.mkdir(os.path.join(directory, ".claude"))
    return directory


def write(directory, path, content):
    """Write a temporary verifier or marker, creating parent directories."""
    full_path = os.path.join(directory, path)
    parent = os.path.dirname(full_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(full_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def run(directory, stdin=None, env=None, args=()):
    """Run the hook with a normal payload, or supplied deliberately bad stdin."""
    if stdin is None:
        stdin = json.dumps({"cwd": directory, "stop_hook_active": False})
    return subprocess.run([sys.executable, HOOK] + list(args), input=stdin,
                          cwd=directory, capture_output=True, text=True, env=env)


def stripped_env():
    """An environment whose PATH cannot resolve bash by bare name.

    Reproduces the 2026-09-01 field failure: hook subprocesses ran with a
    PATH lacking Git Bash, so `subprocess.run(["bash", ...])` died with
    WinError 2 and the gate silently allowed every stop.
    """
    env = {k: v for k, v in os.environ.items()
           if k.upper() in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")}
    env["PATH"] = ""
    return env


def case(name, setup, check, stdin=None, env=None, args=()):
    """Run one isolated hook scenario and return whether its expectations hold."""
    directory = sandbox()
    try:
        setup(directory)
        result = run(directory, stdin, env, args)
        ok = check(directory, result)
        print("%s: %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            print("      exit=%d stdout=%r stderr=%r" %
                  (result.returncode, result.stdout, result.stderr))
        return ok
    finally:
        shutil.rmtree(directory)


def marker(directory, contents):
    """Create the temporary opt-in marker."""
    write(directory, os.path.join(".claude", ".stop-gate"), contents)


def mission_command(directory, *args, session='orchestrator-1', env=None):
    return subprocess.run([sys.executable, SESSION, 'mission', '--session', session, *args],
                          cwd=directory, capture_output=True, text=True,
                          env=mission_env() if env is None else env)


def mission_env():
    # Simulate the orchestrator ONLY in disposable fixture subprocesses, even
    # when a delegate invokes this test. Never change the running worker's role.
    env = dict(os.environ)
    env.pop('HARNESS_DELEGATE_RUN', None)
    return env


def arm(directory, session='orchestrator-1'):
    folder = 'docs/missions/five-items'
    write(directory, folder + '/spec.md', 'Confirmed: implement items 1 through 5.\n')
    write(directory, folder + '/state.md', '- [x] 1-4\n- [ ] 5 tree view\n')
    result = mission_command(directory, '--state', 'active', '--mission', folder, session=session)
    assert result.returncode == 0, result.stderr


def mission_tests():
    """Real CLI + hook payloads, including lifecycle and stale-state escape paths."""
    from stop_gate import mission_marker
    failures, count = 0, 0
    for host in ('claude', 'codex'):
        args = ('--host', host)
        payload = {'session_id': 'orchestrator-1', 'hook_event_name': 'Stop',
                   'stop_hook_active': False, 'last_assistant_message': 'I will now implement item 5.'}

        def invoke(d, **changes):
            return run(d, json.dumps(dict(payload, **changes)), env=mission_env(), args=args)

        def blocked(r):
            return ('MISSION_INCOMPLETE:' in (r.stderr + r.stdout)
                    and (r.returncode == 2 if host == 'claude'
                         else r.returncode == 0 and json.loads(r.stdout).get('decision') == 'block'))

        def allowed(r):
            return r.returncode == 0 and (not r.stdout or json.loads(r.stdout).get('decision') != 'block')

        def sequence(d):
            arm(d)
            assert blocked(invoke(d))
            # Model bookkeeping must not buy an unlimited retry loop.
            arm(d)
            second = invoke(d)
            assert allowed(second) and 'Recovery limit reached' in second.stdout
            result = subprocess.run([sys.executable, SESSION, 'finish', '--session', 'orchestrator-1'],
                                    cwd=d, capture_output=True, text=True, env=mission_env())
            assert result.returncode == 1 and 'MISSION_INCOMPLETE:' in result.stderr
            assert mission_marker(d, 'orchestrator-1').exists()

        def new_prompt(d, prompt):
            arm(d)
            arm(d, 'other-session')
            result = invoke(d, hook_event_name='UserPromptSubmit', prompt=prompt)
            assert result.returncode == 0 and 'HARNESS MISSION SESSION:' in result.stdout
            assert allowed(invoke(d))
            assert mission_marker(d, 'other-session').exists()
            assert not mission_marker(d, 'orchestrator-1').exists()

        def completed(d):
            arm(d)
            assert mission_command(d, '--state', 'complete').returncode == 0
            assert allowed(invoke(d))

        def paused(d, state):
            arm(d)
            assert mission_command(d, '--state', state).returncode != 0
            result = mission_command(d, '--state', state, '--reason', 'User decision: pause this work.')
            assert result.returncode == 0
            result = invoke(d)
            assert allowed(result) and 'User decision: pause this work.' in result.stdout

        def delegate(d):
            arm(d)
            env = dict(os.environ, HARNESS_DELEGATE_RUN='1')
            result = run(d, json.dumps(payload), env=env, args=args)
            assert allowed(result) and mission_marker(d, 'orchestrator-1').exists()
            result = run(d, json.dumps(dict(payload, hook_event_name='UserPromptSubmit')), env=env, args=args)
            assert not result.stdout and mission_marker(d, 'orchestrator-1').exists()
            assert mission_command(d, '--state', 'complete', env=env).returncode != 0

        def verifier(d):
            arm(d)
            mission_command(d, '--state', 'paused', '--reason', 'Waiting for authorization')
            write(d, 'fail.sh', 'exit 3\n')
            marker(d, 'fail.sh\n')
            result = invoke(d)
            assert 'verification failed (exit 3)' in result.stderr + result.stdout
            assert result.returncode == 2 if host == 'claude' else json.loads(result.stdout)['decision'] == 'block'
            write(d, 'fail.sh', 'exit 0\n')
            arm(d)
            assert blocked(invoke(d))  # Passing verifier does not complete the mission.
            assert not os.path.exists(os.path.join(d, '.claude', '.stop-gate'))

        def resumed(d):
            arm(d)
            assert blocked(invoke(d))
            invoke(d, hook_event_name='UserPromptSubmit', prompt='Continue the mission')
            arm(d)
            result = invoke(d, stop_hook_active=True)
            assert allowed(result) and 'MISSION_INCOMPLETE:' in result.stdout

        def malformed_prompt(d):
            # A prompt hook must NEVER fall into verification or reject the prompt,
            # even if stdin is malformed and a failing verifier is armed.
            write(d, 'fail.sh', 'exit 3\n')
            marker(d, 'fail.sh\n')
            result = run(d, 'not json', env=mission_env(), args=args + ('--new-prompt',))
            assert result.returncode == 0 and 'session id unavailable' in result.stdout
            assert os.path.exists(os.path.join(d, '.claude', '.stop-gate'))

        scenarios = [
            ('pending item rejects promise-only stop, bounded recovery, explicit finish', sequence),
            ('completed mission allows stop', completed),
            ('explicit pause', lambda d: paused(d, 'paused')),
            ('decision/authorization wait', lambda d: paused(d, 'needs-input')),
            ('changed request', lambda d: paused(d, 'switched')),
            ('new stop request releases only its session', lambda d: new_prompt(d, 'Stop now')),
            ('new unrelated request releases only its session', lambda d: new_prompt(d, 'Explain this term')),
            ('new status request releases; orchestrator decides to rearm', lambda d: new_prompt(d, 'What is the status?')),
            ('delegates cannot clear or inherit parent mission', delegate),
            ('independent verifier remains enforced', verifier),
            ('Codex-style synthetic prompt keeps host continuation cap', resumed),
            ('malformed new-prompt input never invokes verifier or rejects user', malformed_prompt),
            ('different session unaffected', lambda d: (arm(d, 'other-session'),
                                                       check(allowed(invoke(d))))),
            ('native subagent unaffected', lambda d: (arm(d), check(allowed(invoke(d, agent_id='worker'))))),
            ('no session id does not adopt stale marker', lambda d: (arm(d), check(allowed(invoke(d, session_id=None))))),
            ('malformed marker warns without trapping conversation', lambda d: (
                arm(d), mission_marker(d, 'orchestrator-1').write_text('{', encoding='utf-8'),
                check('MISSION_UNKNOWN:' in invoke(d).stdout))),
        ]
        for name, action in scenarios:
            count += 1
            directory = sandbox()
            try:
                action(directory)
                print('PASS: %s mission: %s' % (host, name))
            except Exception as exc:
                failures += 1
                print('FAIL: %s mission: %s: %r' % (host, name, exc))
            finally:
                shutil.rmtree(directory)
    return failures, count


def check(condition):
    assert condition


def bash_path_tests():
    """Exercise real Windows PATH lookup; fixture executables are never run."""
    if os.name != 'nt':
        print('SKIP: Windows Bash PATH lookup fixtures')
        return 0, 0
    import stop_gate
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for folder in ('Windows/System32', 'Microsoft/WindowsApps', 'Windows/Sysnative',
                       'Custom Git/bin', 'Other Git/bin', 'Fallback Git/bin'):
            executable = os.path.join(directory, folder, 'bash.exe')
            os.makedirs(os.path.dirname(executable))
            with open(executable, 'wb') as handle:
                handle.write(b'MZ selection fixture')
            paths.append(executable)
        blocked, first, second, fallback = paths[:3], paths[3], paths[4], paths[5]
        cases = [
            ('WSL entries yield to first custom Git on PATH', blocked + [first, second], (fallback,), first),
            ('custom Git PATH order is preserved', blocked + [second, first], (fallback,), second),
            ('normal first Bash is preserved', [first] + blocked, (fallback,), first),
            ('WSL-only PATH uses the fallback', blocked, (fallback,), fallback),
            ('WSL-only PATH without a fallback returns none', blocked, (), None),
        ]
        for name, entries, fallbacks, expected in cases:
            env = {'PATH': os.pathsep.join(os.path.dirname(p) for p in entries), 'PATHEXT': '.EXE'}
            with patch.dict(os.environ, env), patch.object(stop_gate, 'BASH_FALLBACKS', fallbacks):
                actual = stop_gate.find_bash()
            ok = (os.path.normcase(actual) if actual else actual) == (
                os.path.normcase(expected) if expected else expected)
            print('%s: %s' % ('PASS' if ok else 'FAIL', name))
            if not ok:
                failures += 1
                print('      expected=%r actual=%r' % (expected, actual))
    return failures, len(cases)


def main():
    marker_path = os.path.join(".claude", ".stop-gate")
    cases = [
        ("no marker", lambda d: None,
         lambda d, r: r.returncode == 0 and not r.stdout and not r.stderr, None),
        ("passing script clears marker",
         lambda d: (write(d, "pass.sh", "exit 0\n"), marker(d, "pass.sh\n")),
         lambda d, r: r.returncode == 0 and not r.stdout and not r.stderr
         and not os.path.exists(os.path.join(d, marker_path)), None),
        ("failing script blocks and retains marker",
         lambda d: (write(d, "fail.sh", "echo boom\nexit 3\n"), marker(d, "fail.sh\n")),
         lambda d, r: r.returncode == 2 and "verification failed (exit 3)" in r.stderr
         and "boom" in r.stderr and os.path.exists(os.path.join(d, marker_path)), None),
        ("failing output is capped to 40 lines",
         lambda d: (write(d, "many.sh", "for i in {1..60}; do echo line-$i; done\nexit 4\n"),
                    marker(d, "many.sh\n")),
         lambda d, r: r.returncode == 2 and "line-60" in r.stderr
         and "line-21" in r.stderr and "line-20" not in r.stderr and "line-1\n" not in r.stderr, None),
        ("missing script blocks",
         lambda d: marker(d, "missing-check.sh\n"),
         lambda d, r: r.returncode == 2 and "missing-check.sh" in r.stderr, None),
        ("bad stdin does not disable gate",
         lambda d: (write(d, "fail.sh", "exit 7\n"), marker(d, "fail.sh\n")),
         lambda d, r: r.returncode == 2 and "verification failed (exit 7)" in r.stderr,
         "not json"),
        ("leading blank marker line",
         lambda d: (write(d, "pass.sh", "exit 0\n"), marker(d, "\n\npass.sh\n")),
         lambda d, r: r.returncode == 0 and not os.path.exists(os.path.join(d, marker_path)), None),
        ("relative script path",
         lambda d: (write(d, os.path.join("checks", "pass.sh"), "exit 0\n"),
                    marker(d, "checks/pass.sh\n")),
         lambda d, r: r.returncode == 0 and not os.path.exists(os.path.join(d, marker_path)), None),
    ]
    failures = 0
    for name, setup, check, stdin in cases:
        if not case(name, setup, check, stdin):
            failures += 1

    # Codex host contract (measured 2026-09-04, Codex CLI 0.152.1/0.153.2):
    # exit 2 from a Stop hook is fail-OPEN there, so the gate asks for a
    # continuation with {"decision":"block"} instead -- and stops asking once
    # Codex reports stop_hook_active, since Codex has no runaway cap.
    codex = ("--host", "codex")
    failing = "echo boom" + os.linesep + "exit 3" + chr(10)
    codex_cases = [
        ("codex: failing script asks for continuation",
         lambda d: (write(d, "fail.sh", failing), marker(d, "fail.sh" + chr(10))),
         lambda d, r: r.returncode == 0 and '"decision": "block"' in r.stdout
         and "verification failed (exit 3)" in r.stdout
         and os.path.exists(os.path.join(d, marker_path)), None),
        ("codex: passing script clears marker silently",
         lambda d: (write(d, "pass.sh", "exit 0" + chr(10)), marker(d, "pass.sh" + chr(10))),
         lambda d, r: r.returncode == 0 and not r.stdout
         and not os.path.exists(os.path.join(d, marker_path)), None),
        ("codex: second continuation is not requested",
         lambda d: (write(d, "fail.sh", failing), marker(d, "fail.sh" + chr(10))),
         lambda d, r: r.returncode == 0 and not r.stdout
         and "verification failed (exit 3)" in r.stderr
         and os.path.exists(os.path.join(d, marker_path)),
         json.dumps({"stop_hook_active": True})),
        ("codex: missing script asks for continuation",
         lambda d: marker(d, "missing-check.sh" + chr(10)),
         lambda d, r: r.returncode == 0 and '"decision": "block"' in r.stdout
         and "missing-check.sh" in r.stdout, None),
    ]
    for name, setup, check, stdin in codex_cases:
        if not case(name, setup, check, stdin, None, codex):
            failures += 1
    cases.extend(codex_cases)

    # An explicit --host claude keeps the exit-2 contract, and so does an
    # unrecognized value: a typo must never switch the blocking mechanism.
    for label, args in (("--host claude", ("--host", "claude")),
                        ("unknown host", ("--host", "banana"))):
        name = "%s keeps exit 2" % label
        if not case(name,
                    lambda d: (write(d, "fail.sh", "exit 5" + chr(10)),
                               marker(d, "fail.sh" + chr(10))),
                    lambda d, r: r.returncode == 2 and not r.stdout
                    and "verification failed (exit 5)" in r.stderr,
                    None, None, args):
            failures += 1
        cases.append((name, None, None, None))

    # PATH-independent bash resolution (env=stripped): the gate must still
    # run the verifier via the fallback interpreter paths, not die on
    # bare-name lookup. Requires Git for Windows on the machine.
    if not case("empty PATH still runs verifier",
                lambda d: (write(d, "fail.sh", "echo boom\nexit 5\n"),
                           marker(d, "fail.sh\n")),
                lambda d, r: r.returncode == 2
                and "verification failed (exit 5)" in r.stderr,
                None, stripped_env()):
        failures += 1
    cases.append(("empty PATH still runs verifier", None, None, None))

    # A WindowsApps `bash.exe` (the WSL launcher stub) first on PATH must be
    # skipped, not executed: executing it yields exit 127 and a permanently
    # blocked turn end (observed 2026-09-02). The stub is simulated with a
    # non-executable file under a matching directory name; the gate must
    # fall through to the real Git-for-Windows bash.
    stub_root = tempfile.mkdtemp()
    stub_dir = os.path.join(stub_root, "Microsoft", "WindowsApps")
    os.makedirs(stub_dir)
    with open(os.path.join(stub_dir, "bash.exe"), "wb") as handle:
        handle.write(b"MZ stub")
    stub_env = stripped_env()
    stub_env["PATH"] = stub_dir
    stub_env["PATHEXT"] = ".EXE"
    try:
        if not case("WindowsApps bash stub is skipped",
                    lambda d: (write(d, "fail.sh", "echo boom\nexit 6\n"),
                               marker(d, "fail.sh\n")),
                    lambda d, r: r.returncode == 2
                    and "verification failed (exit 6)" in r.stderr,
                    None, stub_env):
            failures += 1
    finally:
        shutil.rmtree(stub_root)
    cases.append(("WindowsApps bash stub is skipped", None, None, None))

    # The WSL launcher `<SystemRoot>\System32\bash.exe` is a real file
    # outside WindowsApps and precedes Git on the default system PATH, so a
    # Windows shell resolves bare `bash` to it (observed 2026-09-10). The
    # gate must skip it by location, like the stub, and fall through to the
    # Git-for-Windows bash.
    wsl_root = tempfile.mkdtemp()
    wsl_dir = os.path.join(wsl_root, "Windows", "System32")
    os.makedirs(wsl_dir)
    with open(os.path.join(wsl_dir, "bash.exe"), "wb") as handle:
        handle.write(b"MZ launcher")
    wsl_env = stripped_env()
    wsl_env["PATH"] = wsl_dir
    wsl_env["PATHEXT"] = ".EXE"
    try:
        if not case("System32 WSL bash launcher is skipped",
                    lambda d: (write(d, "fail.sh", "echo boom\nexit 6\n"),
                               marker(d, "fail.sh\n")),
                    lambda d, r: r.returncode == 2
                    and "verification failed (exit 6)" in r.stderr,
                    None, wsl_env):
            failures += 1
    finally:
        shutil.rmtree(wsl_root)
    cases.append(("System32 WSL bash launcher is skipped", None, None, None))

    path_failures, path_count = bash_path_tests()
    mission_failures, mission_count = mission_tests()
    failures += path_failures + mission_failures
    print("\n%s / %d cases" % ("ALL PASS" if not failures else "%d FAILURES" % failures,
                                 len(cases) + path_count + mission_count))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
