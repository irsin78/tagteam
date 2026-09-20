#!/usr/bin/env bash
# Hash the same control-plane inventory in one Python process.
# The HOME argument is converted to a native path by MSYS; the prefixed
# second argument retains its display spelling for existing report consumers.
PY=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -n "$PY" ] || { echo "control-plane-hash: python unavailable" >&2; exit 2; }
MSYS2_ARG_CONV_EXCL="_" "$PY" - "${HOME:-}" "_${HOME:-}" "$@" <<'PY'
import hashlib
import os
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", newline="\n")
def scan_error(error):
    raise error

def inventory():
    paths = {}
    for directory in (".claude/hooks", ".claude/scripts", ".claude/rules", ".claude/agents",
                      ".claude/skills", ".claude/commands", ".codex/hooks",
                      ".agents/agents", ".agents/hooks"):
        base = Path(directory)
        if not base.is_dir() or base.is_symlink():
            continue
        for current, folders, files in os.walk(base, onerror=scan_error):
            folders[:] = [name for name in folders if name != "__pycache__"]
            for name in files:
                path = Path(current) / name
                if not name.endswith(".pyc") and not path.is_symlink() and path.is_file():
                    paths[path.as_posix()] = path
    for name in (
        "docs/orchestration/delegation-matrix.md", "docs/orchestration/retry-policy.md",
        ".claude/settings.json", ".claude/settings.local.json", ".claude/sandbox-sensitive.json",
        ".claude/model-bindings.json", ".claude/model-bindings.local.json",
        ".claude/.stop-gate", ".claude/.preflight-status", ".mcp.json",
        ".agents/hooks.json",
        ".codex/hooks.json", ".codex/config.toml", ".codex/AGENTS.md", ".codex/AGENTS.override.md",
        "CLAUDE.md", "AGENTS.md", "AGENTS.override.md", "check-windows-aliases.ps1", "check-posix.sh",
    ):
        if Path(name).is_file():
            paths[name] = Path(name)
    if sys.argv[1]:
        for name in (".claude/settings.json", ".claude/CLAUDE.md", ".gemini/antigravity-cli/settings.json",
                     ".gemini/config/agents/agy-fetcher.md",
                     ".gemini/config/hooks.json", ".gemini/config/hooks/agy_fetch_view_guard.py",
                     ".codex/config.toml", ".codex/AGENTS.md", ".codex/AGENTS.override.md"):
            path = Path(sys.argv[1]) / name
            if path.is_file():
                paths[sys.argv[2][1:].rstrip("/") + "/" + name] = path
    return paths

# Only runtime hook trust records are excluded. A following table, even
# indented, ends that exclusion; project trust and all other settings remain.
def content(path, label):
    data = path.read_bytes()
    if label.endswith(".codex/config.toml"):
        if os.name == "nt":
            data = data.replace(b"\r\n", b"\n")
        keep = []
        skip = False
        lines = data.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        for line in lines:
            if re.match(rb"^[ \t]*\[hooks\.state(\.|\])", line):
                skip = True
            else:
                if re.match(rb"^[ \t]*\[", line):
                    skip = False
                if not skip:
                    keep.append(line + b"\n")
        return b"".join(keep)
    return data

try:
    paths = inventory()
    rows = []
    for label, path in paths.items():
        digest = hashlib.sha256(content(path, label)).hexdigest()
        # Match GNU checksum escaping, including unusual POSIX filenames.
        if "\\" in label or "\n" in label:
            digest = "\\" + digest
            label = label.replace("\\", "\\\\").replace("\n", "\\n")
        rows.append((label, digest))
    output = ''.join(f"{digest}  {label}\n" for label, digest in sorted(rows))
    if len(sys.argv) == 3:
        print(output, end='')
    else:
        # Hash, persist and classify in the same process. A failed read or
        # malformed snapshot is unknown evidence, never an empty change set.
        option, before, after, changed, control_re, notice_re = sys.argv[3:]
        if option != '--compare' or not rows:
            raise ValueError('expected --compare and a nonempty control plane')
        Path(after).write_text(output, encoding='utf-8')

        def records(text):
            result = {}
            for line in text.splitlines():
                match = re.fullmatch(r'(\\?)([0-9a-f]{64})  (.+)', line)
                if not match:
                    raise ValueError('malformed control-plane snapshot')
                escaped, digest, label = match.groups()
                if escaped:
                    label = re.sub(r'\\([\\n])', lambda m: '\n' if m[1] == 'n' else '\\', label)
                result[label] = digest
            if not result:
                raise ValueError('empty control-plane snapshot')
            return result

        old = records(Path(before).read_text(encoding='utf-8'))
        new = records(output)
        names = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
        names.update(name for name in Path(changed).read_text(encoding='utf-8').splitlines()
                     if re.search(control_re, name) or re.search(notice_re, name))
        notices = {name for name in names if re.search(notice_re, name) or
                   name == sys.argv[2][1:].rstrip('/') + '/.codex/config.toml'}
        for key, values in (('hits', names - notices), ('notice', notices)):
            # Keep unusual filenames on one report line, as in checksum output.
            print(key + '\t' + ','.join(name.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r')
                                       for name in sorted(values)))
except (OSError, ValueError) as error:
    print(f"control-plane-hash: {error}", file=sys.stderr)
    sys.exit(2)
PY
