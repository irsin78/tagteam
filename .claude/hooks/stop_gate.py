#!/usr/bin/env python
"""Opt-in verification gate and bounded, session-scoped mission continuation.

When .claude/.stop-gate names a verifier, Claude cannot finish until that
verifier passes. Separately, an armed mission gets one recovery at early Stop.
UserPromptSubmit releases the previous mission so new requests remain in control.
Without either marker this hook does not block.

LIVENESS: `python stop_gate.py --self-test` exercises real temporary markers
and scripts through the decision path, then prints SELFTEST_OK / SELFTEST_FAILED.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


MARKER_RELATIVE_PATH = os.path.join(".claude", ".stop-gate")
RUN_ID_PATTERN = r'[A-Za-z0-9TZ_-]+'
LIVE_RUN_STATES = ('starting', 'running')


def launcher_run_state(cwd, run_id):
    """Classified state of a launcher run (running/done/aborted/...), or None.

    The record lives outside the workspace (run-state.sh) and only the launcher
    writes it, but a recorded 'running' is not proof of life: a launcher killed
    without its traps leaves the word behind. So the hook asks that launcher's
    own `--status`, which applies the same PID-identity and starting-grace
    classification the busy guard uses (and persists `aborted` for dead runs).
    Unreadable record, missing launcher or no Git Bash all read as None, which
    the callers treat as 'not live' (fail closed).
    """
    import re
    if not isinstance(run_id, str) or not re.fullmatch(RUN_ID_PATTERN, run_id):
        return None
    scripts = Path(__file__).resolve().parent.parent / 'scripts'
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    # state_directory() keys the tree from the process cwd; the hook's tree is
    # the supplied cwd (the same one --status runs in), so resolve both there.
    previous = os.getcwd()
    try:
        os.chdir(cwd)
        from harness_records import state_directory
        record = json.loads((state_directory() / ('state-%s.json' % run_id)).read_text(encoding='utf-8'))
        tool = record.get('tool') if isinstance(record, dict) else None
    except (ImportError, OSError, ValueError, TypeError):
        return None
    finally:
        try:
            os.chdir(previous)
        except OSError:
            pass
    if tool not in ('codex', 'claude', 'agy'):
        return None
    launcher = scripts / ('%s-run.sh' % tool)
    bash = find_bash()
    if not bash or not launcher.is_file():
        return None
    try:
        # The launchers print UTF-8 (arrows and dashes in their notes); the
        # locale default would make the reader thread fail and hand back None.
        result = subprocess.run([bash, '--', str(launcher), '--status', run_id], cwd=cwd,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'^STATE: ([a-z]+)', result.stdout, re.MULTILINE)
    return match.group(1) if match else None


def mission_marker(cwd, session_id):
    """Select one session marker without scanning shared directories."""
    if not isinstance(session_id, str) or not session_id:
        return None
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return Path(cwd) / '.claude' / '.mission-open' / (key + '.json')


def update_mission(cwd, session_id, state, mission=None, reason=None, run=None):
    """Explicit orchestrator bookkeeping, not proof of consent or acceptance."""
    marker = mission_marker(cwd, session_id)
    if marker is None or os.environ.get('HARNESS_DELEGATE_RUN') == '1':
        raise ValueError('a current orchestrator session id is required')
    if state == 'complete':
        marker.unlink(missing_ok=True)
        return 'HARNESS MISSION: cleared; acceptance evidence belongs in the mission record.'
    previous = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('invalid mission record')
    if state == 'active':
        root = Path(cwd).resolve()
        target = (root / (mission or '')).resolve()
        if not mission or not target.is_relative_to(root) or not (target / 'spec.md').is_file():
            raise ValueError('mission must name a project folder containing the confirmed spec.md')
        previous.update(mission=target.relative_to(root).as_posix(), state=state, reason='')
    elif state in ('paused', 'needs-input', 'switched'):
        if not reason or not reason.strip() or not previous.get('mission'):
            raise ValueError('an armed mission and a concrete pause/wait/switch reason are required')
        previous.update(state=state, reason=reason.strip())
    elif state == 'waiting':
        # Waiting on a detached launcher run is not announcing: the Stop hook
        # lets the turn end while that run is still live, so the orchestrator
        # need not busy-wait. The run must exist and be live right now.
        if not previous.get('mission'):
            raise ValueError('waiting requires an armed mission')
        live = launcher_run_state(cwd, run)
        if live not in LIVE_RUN_STATES:
            raise ValueError('waiting requires --run <RUN_ID> of a launcher run that is still starting/running'
                             + (' (state: %s)' % live if live else ''))
        previous.update(state=state, run=run, reason='')
    else:
        raise ValueError('unknown mission state')
    # Re-arming within the same prompt must not reset the recovery allowance.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(previous, ensure_ascii=True), encoding='utf-8')
    return 'HARNESS MISSION: %s %s%s' % (
        state, previous['mission'], ' -- ' + reason if reason else '')


def mission_prompt(cwd, data):
    """Release only this session on a new prompt, without interpreting its text."""
    if os.environ.get('HARNESS_DELEGATE_RUN') == '1' or data.get('agent_id'):
        return ''
    session = data.get('session_id')
    marker = mission_marker(cwd, session)
    if marker is None:
        return 'HARNESS MISSION: session id unavailable; automatic continuation is inactive.'
    try:
        record = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    except ValueError:
        record = {}
    if not isinstance(record, dict):
        record = {}
    if record.get('exhausted'):
        marker.write_text(json.dumps({'exhausted': record['exhausted']}), encoding='utf-8')
    else:
        marker.unlink(missing_ok=True)
    return ('HARNESS MISSION SESSION: %s. Previous mission guard released for this prompt. '
            'If executing/continuing a confirmed multi-step mission, arm it with '
            'harness-session.py mission --session <this-id> --state active --mission <folder>. '
            'A status question does not cancel authorized work; honor pauses and new requests.' % session)


def evaluate_mission(cwd, data):
    """One recovery, then a visible incomplete notice; never an acceptance test."""
    if os.environ.get('HARNESS_DELEGATE_RUN') == '1' or data.get('agent_id'):
        return 0, ''
    marker = mission_marker(cwd, data.get('session_id'))
    if marker is None or not marker.is_file():
        return 0, ''
    try:
        record = json.loads(marker.read_text(encoding='utf-8'))
        if isinstance(record, dict) and set(record) == {'exhausted'}:
            return 0, ''
        state, mission = record['state'], record['mission']
        if not isinstance(mission, str) or not mission:
            raise ValueError('missing mission path')
        if state in ('paused', 'needs-input', 'switched') and record.get('reason'):
            return 0, 'HARNESS MISSION %s: %s\n' % (state, record['reason'])
        prefix = ''
        if state == 'waiting':
            live = launcher_run_state(cwd, record.get('run'))
            if live in LIVE_RUN_STATES:
                return 0, ('HARNESS MISSION waiting: run %s is still %s; re-arm with --state active '
                           'when its report arrives.\n' % (record.get('run'), live))
            # The run finished (or vanished): the wait is over and the mission is
            # active again, with the usual single recovery.
            prefix = 'run %s is %s, so the wait is over. ' % (record.get('run'), live or 'not recorded')
            state = 'active'
        if state != 'active':
            raise ValueError('invalid mission state or missing reason')
        message = ('MISSION_INCOMPLETE: ' + prefix + '%s is still active. Announcing the next step is not '
                   'performing it. Continue the confirmed work; clear the mission only after '
                   'acceptance, or record a real pause, missing decision/authorization/dependency, '
                   'or changed request with harness-session.py mission and explain it to the user.' % mission)
        if record.get('continued') or data.get('stop_hook_active'):
            return 0, message + ' Recovery limit reached; this is not completion.\n'
        record['continued'] = True
        marker.write_text(json.dumps(record, ensure_ascii=True), encoding='utf-8')
        return 2, message + '\n'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return 0, 'MISSION_UNKNOWN: cannot read/update this session marker (%s); check mission state.\n' % exc

# Hook subprocesses can run with a PATH that lacks Git Bash (observed
# 2026-09-01: CLI-session Stop hook died with WinError 2 on `bash` while
# python-only sibling hooks worked), so the interpreter is resolved
# explicitly instead of trusting bare-name lookup. macOS/Linux hook PATHs can
# be just as short (launchd, desktop apps), so POSIX gets the standard system
# and Homebrew locations; the gate fails closed when none exists.
if os.name == "nt":
    BASH_FALLBACKS = (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    )
else:
    BASH_FALLBACKS = (
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
        "/bin/bash",
        "/usr/bin/bash",
    )


def is_windowsapps_stub(path):
    """True only for the `...\\Microsoft\\WindowsApps\\` directory itself."""
    normalized = path.replace("/", "\\").lower()
    return "\\microsoft\\windowsapps\\" in normalized


def is_wsl_launcher(path):
    """True for `<SystemRoot>\\System32\\bash.exe`, the WSL launcher.

    Present on every machine with WSL enabled and ahead of Git on the
    default system PATH, so a Windows shell resolves bare `bash` to it
    (observed 2026-09-10: `execvpe(/bin/bash) failed` from the route tests
    and this gate's self-test under PowerShell while Git Bash passed). It
    is a real file outside WindowsApps, so the stub check does not catch it.
    """
    normalized = path.replace("/", "\\").lower()
    return (normalized.endswith("\\windows\\system32\\bash.exe")
            or normalized.endswith("\\windows\\sysnative\\bash.exe"))


def is_non_posix_bash(path):
    """A PATH hit that cannot run Windows-path scripts: stub or WSL launcher."""
    return is_windowsapps_stub(path) or is_wsl_launcher(path)


def find_bash():
    """Locate bash via PATH, then standard install paths (BASH_FALLBACKS).

    A PATH hit under `...\\Microsoft\\WindowsApps\\` is the WSL launcher
    stub, not a POSIX shell for Windows paths: it strips the backslashes
    from the script path and exits 127, which this gate would read as
    "verification failed" and block every turn end until the eight-block
    cap (observed 2026-09-02 on a machine where the stub was the ONLY
    `bash` on the hook PATH). The System32 WSL launcher fails the same way
    (see is_wsl_launcher). Skip both and check the remaining PATH entries
    before using the Git-for-Windows fallbacks.
    """
    found = shutil.which("bash")
    if found and not is_non_posix_bash(found):
        return found
    if found:
        # Resolve each entry explicitly so the rejected launcher cannot hide
        # a later, nonstandard Git install or be prepended again by Windows.
        for directory in os.get_exec_path():
            candidate = shutil.which(os.path.join(directory or os.curdir, "bash"))
            if candidate and not is_non_posix_bash(candidate):
                return candidate
    for candidate in BASH_FALLBACKS:
        if os.path.isfile(candidate):
            return candidate
    return None


def marker_script(marker, cwd):
    """Return the first non-empty marker line and its cwd-resolved path."""
    with open(marker, "r", encoding="utf-8") as handle:
        for line in handle:
            path = line.strip()
            if path:
                return path, path if os.path.isabs(path) else os.path.join(cwd, path)
    return "", ""


def evaluate(cwd):
    """Return (exit code, stderr text) for the marker and verifier at cwd."""
    marker = os.path.join(cwd, MARKER_RELATIVE_PATH)
    if not os.path.isfile(marker):
        return 0, ""

    named_path, script = marker_script(marker, cwd)
    if not script or not os.path.isfile(script):
        # ASCII-only message: an em dash gets backslash-escaped on cp949
        # consoles. NOTE the isfile check runs in WINDOWS Python — MSYS-style
        # paths like /tmp/x.sh are invisible to it; marker paths must be
        # repo-relative or Windows-style absolute.
        return 2, ("stop-gate: marker names a missing verify script '%s' -- fix "
                   "the path in .claude/.stop-gate (repo-relative or Windows-style "
                   "absolute; MSYS /tmp paths are not visible to this check) or "
                   "remove the marker deliberately.\n" % named_path)

    bash = find_bash()
    if not bash:
        # Fail CLOSED: a gate that cannot run its verifier must not allow a
        # silent pass — the reason tells the operator how to resolve it.
        return 2, ("stop-gate: no bash interpreter found (PATH and standard "
                   "install locations) -- install bash (Git Bash on Windows) "
                   "or remove .claude/.stop-gate deliberately.\n")

    # Ignore stop_hook_active: verifier success is the progress signal, and
    # Claude Code's built-in eight-block cap is the runaway-loop guard.
    try:
        result = subprocess.run([bash, "--", script], cwd=cwd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 2, ("stop-gate: could not launch the verify script (%s) -- "
                   "fix the environment or remove the marker deliberately.\n"
                   % exc)
    if result.returncode == 0:
        os.remove(marker)
        return 0, ""

    tail = result.stdout.splitlines()[-40:]
    message = ("stop-gate: verification failed (exit %d). Fix the failures and "
               "finish the turn again; the gate re-runs the script.\n" % result.returncode)
    if tail:
        message += "\n".join(tail) + "\n"
    return 2, message


def self_test():
    """Prove the gate is inert, self-clearing, and blocking when needed."""
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        os.mkdir(os.path.join(directory, ".claude"))
        marker = os.path.join(directory, MARKER_RELATIVE_PATH)

        code, _ = evaluate(directory)
        if code != 0:
            failures.append("no marker")

        passing = os.path.join(directory, "pass.sh")
        with open(passing, "w", encoding="utf-8") as handle:
            handle.write("exit 0\n")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("pass.sh\n")
        code, _ = evaluate(directory)
        if code != 0 or os.path.exists(marker):
            failures.append("passing script")

        failing = os.path.join(directory, "fail.sh")
        with open(failing, "w", encoding="utf-8") as handle:
            handle.write("exit 3\n")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("fail.sh\n")
        code, _ = evaluate(directory)
        if code != 2 or not os.path.exists(marker):
            failures.append("failing script")

    if failures:
        print("SELFTEST_FAILED: " + "; ".join(failures))
        return 1
    print("SELFTEST_OK: stop gate alive, 3 probes decided correctly")
    return 0


HOSTS = ("claude", "codex")


def host_from_argv(argv):
    """`--host codex` / `--host=codex`; anything else (absent or
    unrecognized) is the Claude contract, so a typo never silently changes
    the blocking mechanism."""
    for i, arg in enumerate(argv):
        value = None
        if arg == "--host" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--host="):
            value = arg.split("=", 1)[1]
        if value in HOSTS:
            return value
    return "claude"


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    host = host_from_argv(sys.argv[1:])
    data = {}
    try:
        loaded = json.load(sys.stdin)
        if isinstance(loaded, dict):
            data = loaded
        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
    except Exception:
        cwd = os.getcwd()
    if '--new-prompt' in sys.argv or data.get('hook_event_name') == 'UserPromptSubmit':
        try:
            message = mission_prompt(cwd, data)
        except OSError as exc:
            message = 'MISSION_UNKNOWN: could not release previous mission guard (%s).' % exc
        if message:
            print(message)
        sys.exit(0)  # Never reject, erase, or reinterpret a user's prompt.

    code, stderr = evaluate(cwd)
    if code == 0:
        code, stderr = evaluate_mission(cwd, data)
        if code == 0 and stderr:
            print(json.dumps({'systemMessage': stderr.strip()}))
            sys.exit(0)

    if host == "codex":
        # Measured on Codex CLI 0.152.1/0.153.2: exit 2 from a Stop hook is
        # reported as `hook: Stop Failed` and the turn ends anyway (fail
        # OPEN). Only `{"decision":"block"}` keeps Codex going, and it does
        # so by starting a continuation turn.
        if code != 0:
            if data.get("stop_hook_active"):
                # Already one continuation deep. Codex has no documented
                # runaway cap of its own, so stop asking and let the turn
                # end with the failure visible rather than loop on quota.
                if stderr:
                    sys.stderr.write(stderr)
                sys.exit(0)
            print(json.dumps({"decision": "block",
                              "reason": (stderr or "stop-gate: verification failed.").strip()}))
        sys.exit(0)

    if stderr:
        sys.stderr.write(stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
