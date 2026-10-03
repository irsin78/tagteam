#!/usr/bin/env python
"""Minimal SessionStart: platform guidance plus host/role, no network, quota or file-tree probes."""
import json
import os
import platform
import sys
from pathlib import Path

PLATFORM_NOTES = {
    "Windows": ("docs/harness-install.md", "only subsection starting with ### Windows, up to the next ### heading"),
    "macOS": ("docs/platform-notes-macos.md", None),
    "Linux": ("docs/platform-notes-linux.md", None),
    "Linux (WSL2)": ("docs/platform-notes-linux.md",
                     "plus docs/harness-launchers.md WSL2 isolation-lane sections"),
}

def detect_platform(system=None, proc_version=None):
    """Map platform.system() (+ /proc/version on Linux) to a note label."""
    if system is None:
        system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "macOS"
    if system == "Linux":
        if proc_version is None:
            try:
                with open("/proc/version", "r", encoding="utf-8",
                          errors="replace") as f:
                    proc_version = f.read()
            except Exception:
                proc_version = ""
        if "microsoft" in proc_version.lower():
            return "Linux (WSL2)"
        return "Linux"
    return system or "unknown"

def platform_note(label, cwd):
    """The file to read for this platform; flags a missing copy."""
    path, extra = PLATFORM_NOTES.get(label, ("docs/harness-manual.md", None))
    note = path + (" (%s)" % extra if extra else "")
    if not os.path.isfile(os.path.join(cwd, path)):
        note += " (file not found -- copy it from the template: README copy-targets table)"
    return note

def host_from_argv(argv):
    """`--host codex` / `--host=codex`; absent or unknown means claude."""
    for i, value in enumerate(argv):
        if value.startswith("--host="):
            host = value.split("=", 1)[1]
        elif value == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        else:
            continue
        return host if host in ("claude", "codex") else "claude"
    return "claude"

def session_role(data, env=None):
    """DELEGATE for a launcher child or native worker; otherwise ORCHESTRATOR."""
    env = os.environ if env is None else env
    if env.get("HARNESS_DELEGATE_RUN") == "1" or data.get("agent_id"):
        return "DELEGATE"
    return "ORCHESTRATOR"

def platform_line(label, note, host="claude", role="ORCHESTRATOR"):
    return "HARNESS PLATFORM: %s -- host: %s (%s) -- before operational work read: %s" % (
        label, host, role, note)


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}
    cwd = data.get('cwd') or os.getcwd()
    label = detect_platform()
    note = platform_note(label, cwd)
    host = host_from_argv(sys.argv[1:])
    role = session_role(data)
    status = {'platform_label': label, 'platform_note': note, 'host': host, 'role': role,
              'hook_interpreter': sys.executable, 'hook_alive': True,
              'session_source': data.get('source'), 'diagnostics': 'not requested'}
    folder = Path(cwd) / '.claude'
    if folder.is_dir():
        try:
            (folder / '.preflight-status').write_text(json.dumps(status), encoding='utf-8')
        except OSError:
            print('HARNESS NOTICE: could not record session metadata', file=sys.stderr)
    print(platform_line(label, note, host, role))
    return 0


if __name__ == '__main__':
    sys.exit(main())
