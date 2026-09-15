#!/usr/bin/env python
"""Minimal SessionStart: platform guidance, no network, quota or file-tree probes."""
import json
import os
import platform
import sys
from pathlib import Path

PLATFORM_NOTES = {
    "Windows": ("docs/harness-manual.md", "only subsection starting with ### Windows, up to the next ### heading"),
    "macOS": ("docs/platform-notes-macos.md", None),
    "Linux": ("docs/platform-notes-linux.md", None),
    "Linux (WSL2)": ("docs/platform-notes-linux.md",
                     "plus docs/harness-manual.md WSL2 isolation-lane sections"),
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

def platform_line(label, note):
    return "HARNESS PLATFORM: %s -- before operational work read: %s" % (label, note)


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}
    cwd = data.get('cwd') or os.getcwd()
    label = detect_platform()
    note = platform_note(label, cwd)
    status = {'platform_label': label, 'platform_note': note,
              'hook_interpreter': sys.executable, 'hook_alive': True,
              'session_source': data.get('source'), 'diagnostics': 'not requested'}
    folder = Path(cwd) / '.claude'
    if folder.is_dir():
        try:
            (folder / '.preflight-status').write_text(json.dumps(status), encoding='utf-8')
        except OSError:
            print('HARNESS NOTICE: could not record session metadata', file=sys.stderr)
    print(platform_line(label, note))
    return 0


if __name__ == '__main__':
    sys.exit(main())
