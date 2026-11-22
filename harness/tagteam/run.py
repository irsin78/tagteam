#!/usr/bin/env python3
"""Launcher: run one worker through another vendor's CLI, verify the model, report briefly.

Options, exit codes, records and the git check's limits: `run.py --help` and docs/manual.md (section 7). Kept here:
- The git check is an accident check, not a security boundary.
- A run's records are <state dir>/runs/<id>/; started.json is written first (before anything touches the checkout) and report.txt last,
  so --status and --wait can tell a finished, a running and a dead run apart. A usage-limit sign goes to
  <state dir>/exhausted.json (route.py reads it), under the quota ("pool") of the model that ran: its models.json
  `pool`, else its vendor. A model reached through another vendor's CLI (id `<name>@agy`) runs as its `cli_id`.
- After a run: the git check, a control-plane check (.claude/, .codex/, .agents/, AGENTS.md, CLAUDE.md and the
  machine-local files git ignores, compared before and after) and, for Codex, a check that its hooks wrote a role record.
- Codex on Windows gets `-c windows.sandbox=<value>` from models.local.json codex.windows_sandbox
  [host: codex-cli 0.169.0 Windows, the elevated sandbox failed every command with CreateProcessWithLogonW 1385,
  measured 2026-11-17; codex-cli 0.170.0 ran `codex exec` commands under elevated on one PC (two runs, not
  interactive), measured 2026-11-18 / re-test after a Codex update]. The default stays unelevated.
- A CLI starts without the variables that name another host: CLAUDECODE and CLAUDE_CODE_ENTRYPOINT unless it is
  claude, ANTIGRAVITY_CONVERSATION_ID unless it is google. Workers get no extra writable folder (no --add-dir).
- Environment (tests): TAGTEAM_STATE_DIR, TAGTEAM_CONFIG_DIR, TAGTEAM_CLI_<VENDOR> (JSON array replacing the CLI).
Python 3.9+, standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import state as statelib  # noqa: E402  (same folder; `state` is a local name in main)
from role_guard import CONTROL_DIRS, CONTROL_FILES  # noqa: E402  (one list of what the control plane is)

MARKER = "HARNESS ROLE: DELEGATE"
AGY_ARG_LIMIT = 30000  # characters of the serialized command; Windows stops near 32K
REPORT_CHARS = 4000
HASH_MAX_PATHS = 2000  # git check: content hashes of listed files; beyond this the check is partial
HASH_MAX_BYTES = 50 * 1024 * 1024
CONTROL_HASH_CAP = 16 * 1024 * 1024  # a control-plane file over this is compared by size and time, not read
FLAGS = re.I | re.S
CLAUDE_EX = re.compile(
    r"(?:API Error: )?(?:HTTP )?429.*|(?:usage|rate)[ -]limit(?: exceeded| reached)?.*"
    r"|You['\u2019]ve hit your usage limit.*|Claude AI usage limit reached\|\d+.*"
    r"|\d+-hour limit reached.*|rate_limit_error.*", FLAGS)
OPENAI_EX = re.compile(
    "(?:⚠️ |ERROR: |stream error: )*(?:HTTP 429(?: Too Many Requests)?|429 Too Many Requests"
    r"|(?:usage|rate)[ -]limit(?: exceeded| reached)?[.!]?|You['\u2019]ve hit your usage limit.*"
    r"|usage limit (?:reached|exceeded).*|exceeded retry limit, last status: 429.*"
    r"|Quota exceeded\. Check your plan and billing details\..*)", re.I)
GOOGLE_JSON_EX = re.compile(r"quota|rate[ -]limit|\b429\b|RESOURCE_EXHAUSTED", re.I)
GOOGLE_LINE_EX = re.compile(
    r"(?:ERROR: )?(?:HTTP 429.*|429 Too Many Requests|(?:usage|rate)[ -]limit.*"
    r"|quota(?: exceeded| exhausted)?[.!]?)", re.I)
# codex: "... or try again at 4:14 AM." (or "try again at Nov 5th, 2026 4:14 AM."): a local time
TRY_AGAIN = re.compile(r"try again at (?:([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? )?"
                       r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
AGY_LABEL = re.compile(r'Propagating selected model override to backend: label="([^"]+)"')
CODEX_MODEL = re.compile(r"^model:\s*(\S+)", re.M)
PRIORITY = [("exhausted", 3), ("git_head_moved", 7), ("control_plane_changed", 10), ("hooks_not_run", 11),
            ("refused", 12), ("timeout", 5), ("model_mismatch", 4), ("wrote_files", 8), ("cli_failed", 1), ("no_output", 6)]
# Machine-local files git ignores, so `git status` never shows a change: kept by content hash (see control_path).
# Hook files are written by the installer and copy_agy_hooks only; a change in a run is the worker's or a stray process's.
# Setting files are also written by the host ("always allow") and the weekly model collector: a change only warns.
CONTROL_HOOK_FILES = (".agents/hooks.json", ".codex/hooks.json")
CONTROL_SETTING_FILES = (".claude/settings.local.json", ".claude/tagteam/models.local.json",
                         ".claude/tagteam/bench.local.json", ".claude/tagteam/install.local.json")
TRUST_WARNING = re.compile(r"^.*Untrusted project-local configuration is disabled.*$", re.M)
WINDOWS_SANDBOX = "unelevated"  # codex windows.sandbox default; models.local.json codex.windows_sandbox overrides it
# The values Codex accepts: codex-cli 0.169.0 rejects others at start with
# "unknown variant `x`, expected one of `elevated`, `unelevated`, `mxc` in `windows.sandbox`".
WINDOWS_SANDBOX_VALUES = ("elevated", "unelevated", "mxc")
# Variables that make a tool inside the worker take the host for another CLI: kept only for the vendor that owns them.
HOST_ENV = {"claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"), "google": ("ANTIGRAVITY_CONVERSATION_ID",)}
DEFAULT_TIMEOUT = 1800
# --wait ends before an agy command does: agy moves a run_command call to the background after about 10 s
# [host: agy 1.5.10 Windows and Linux, measured 2026-11-18 / re-test after an agy update]
WAIT_DEFAULT, WAIT_MAX = 8, 9
EXIT_NO_REPORT, EXIT_RUNNING = 6, 9  # --status / --wait: the run ended without a report / is still running
EXIT_UNKNOWN = 13  # --running: something could not be read or asked, so a run may be going
PID_GRACE = 120  # seconds past a run's timeout before a live pid is taken for a reused one
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a name under runs/: no separator, no ".."
PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE, ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER = 0x1000, 259, 5, 87


def windows_sandbox(config_dir):
    """codex windows.sandbox value: models.local.json codex.windows_sandbox, else the default; None = add nothing.

    A missing or unreadable models.local.json means the default. Any value other than null, "" or one of
    WINDOWS_SANDBOX_VALUES raises ValueError (a usage error, before the CLI starts)."""
    try:
        with open(os.path.join(config_dir, "models.local.json"), encoding="utf-8") as fh:
            local = json.load(fh)
    except (OSError, ValueError):
        return WINDOWS_SANDBOX
    codex = local.get("codex") if isinstance(local, dict) else None
    if not isinstance(codex, dict) or "windows_sandbox" not in codex:
        return WINDOWS_SANDBOX
    value = codex["windows_sandbox"]
    if value is None or value == "":
        return None
    if value not in WINDOWS_SANDBOX_VALUES:
        raise ValueError("models.local.json codex.windows_sandbox is %s; use one of %s, or null to pass nothing"
                         % (json.dumps(value), ", ".join('"%s"' % v for v in WINDOWS_SANDBOX_VALUES)))
    return value


def _is_windows():
    return os.name == "nt"


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fail(msg):
    sys.stderr.write("run.py: %s\n" % msg)
    return 2


def read_text(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_atomic(path, text):
    """Write through a temporary sibling and publish with os.replace, so a reader never sees half of it."""
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(3))
    write_text(tmp, text)
    os.replace(tmp, path)


def parse_json(text):
    try:
        data = json.loads(text.strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---- exhausted.json (interface shared with route.py) ----------------------------------

def load_exhausted(path):
    try:
        with open(path, encoding="utf-8") as fh:
            vendors = json.load(fh)["vendors"]
        if not isinstance(vendors, dict):
            raise ValueError("vendors is not an object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write("run.py: warning: broken exhausted.json treated as empty (%s)\n" % exc)
        return {}
    keep = {}
    for name, item in vendors.items():
        try:
            if parse_iso(item["until"]) > utc_now():
                keep[name] = item
        except (KeyError, TypeError, ValueError):
            continue
    return keep


def update_exhausted(state, vendor, entry):
    """entry None removes the vendor's item. Expired items are dropped on every write."""
    path = os.path.join(state, "exhausted.json")
    vendors = load_exhausted(path)
    if entry is None:
        if vendor not in vendors:
            return
        del vendors[vendor]
    else:
        vendors[vendor] = entry
    statelib.ensure_writable(state)
    write_atomic(path, json.dumps({"schema": 1, "vendors": vendors}, indent=2) + "\n")


# ---- commands ---------------------------------------------------------------------------

def agy_settings(allow_commands):
    """The per-run agy settings JSON: command(*) by default, else exactly the listed commands."""
    allow = ["command(%s)" % c for c in allow_commands] if allow_commands else ["command(*)"]
    return json.dumps({"permissions": {"allow": allow}}, separators=(",", ":"))


def pool_of(m):
    """The quota a model draws on: its own name when the model says nothing (the vendor's quota)."""
    return m.get("pool") or m["vendor"]


def cli_id_of(mid, m):
    """The name the vendor CLI knows the model by (claude-opus-5-5@agy is asked for as claude-opus-5-5)."""
    return m.get("cli_id") or mid


def agy_display(label):
    """What agy shows for a model of ours: the label without the (agy) note of a partner model."""
    return re.sub(r"\s*\(agy\)$", "", label)


def build_command(vendor, base, model_arg, effort, read_only, cwd, run_dir, prompt, timeout, allow_commands=None,
                  run_commands=False, sandbox=WINDOWS_SANDBOX):
    """Argv for one vendor (design 5.3). The prompt is sent on stdin except for google."""
    if vendor == "claude":
        return base + ["-p", "--output-format", "json", "--model", model_arg, "--effort", effort,
                       "--permission-mode", "plan" if read_only else "auto", "--permission-prompts", "none"]
    if vendor == "openai":
        cmd = base + ["exec", "-m", model_arg, "-c", "model_reasoning_effort=" + effort,
                      "-s", "read-only" if read_only else "workspace-write", "-C", cwd,
                      "-o", os.path.join(run_dir, "last.txt")]
        if _is_windows() and sandbox:
            cmd += ["-c", "windows.sandbox=" + sandbox]
        return cmd + ["-"]
    # plan mode never runs commands: a read-only run that must run them (run_commands) uses accept-edits
    mode = "plan" if read_only and not run_commands else "accept-edits"
    tail = ["--model", model_arg, "--effort", effort, "--mode", mode,
            "--output-format", "json", "--log-file", os.path.join(run_dir, "agy.log"), "--print-timeout", "%ds" % timeout,
            "--settings", agy_settings(allow_commands)]
    cmd = base + ["-p", prompt] + tail
    if len(subprocess.list2cmdline(cmd)) > AGY_ARG_LIMIT:  # quoting can double the length
        hint = "%s\nRead and follow the task in %s" % (MARKER, os.path.join(run_dir, "prompt.txt"))
        cmd = base + ["-p", hint] + tail
        if len(subprocess.list2cmdline(cmd)) > AGY_ARG_LIMIT:  # the allow list alone is too long
            raise ValueError("agy command line too long: use fewer or shorter --allow-command values")
    return cmd


def kill_tree(proc):
    try:
        if _is_windows():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def execute(argv, cwd, env, stdin_bytes, run_dir, timeout):
    """Run the child, output to files. Returns (exit code, timed_out)."""
    kwargs = {} if _is_windows() else {"start_new_session": True}
    with open(os.path.join(run_dir, "stdout.txt"), "wb") as out, open(os.path.join(run_dir, "stderr.txt"), "wb") as err:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL, **kwargs)

        def feed():
            try:
                proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            except OSError:
                pass
        if stdin_bytes is not None:
            threading.Thread(target=feed, daemon=True).start()
        try:
            return proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return -1, True


# ---- git before/after check -------------------------------------------------------------
# Catches common accidents; not a security boundary (ignored files, paths outside the
# repository, stashes and other refs are not seen).

def _under(path, dirs):
    return any(path == d or path.startswith(d + os.sep) for d in dirs)


def hash_file(path, room):
    """Return (digest, bytes read). None means over budget; 'dir' means skip (directory, submodule)."""
    try:
        if os.path.islink(path):
            return "link:" + hashlib.sha256(os.readlink(path).encode("utf-8", "replace")).hexdigest(), 0
        if os.path.isdir(path):
            return "dir", 0
        if not os.path.exists(path):
            return "missing", 0
        size = os.path.getsize(path)
        if size > room:
            return None, 0
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest(), size
    except OSError:
        return "unreadable", 0


def _parse_status(raw, root, skip):
    """`status --porcelain -z` output -> (entry lines, (path, full path) pairs, original paths of renames).
    Entries under the directories in `skip` are left out."""
    lines, paths, origs = [], [], []
    entries = raw.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        orig = None
        if "R" in entry[:2] or "C" in entry[:2]:
            orig = entries[i] if i < len(entries) else None  # the original path follows as its own entry
            i += 1
        full = os.path.join(root, entry[3:])
        # the path git reports decides, never what a link there points to (a control file swapped for a link into
        # the state folder must stay in the list); `root` and `skip` are real paths already
        if _under(os.path.normcase(os.path.normpath(full)), skip):
            continue
        lines.append(entry)
        paths.append((entry[3:], full))
        if orig:
            origs.append(orig)
    return lines, paths, origs


def git_snapshot(cwd, exclude=()):
    """Return (snapshot, problem). (None, None): not a git work tree, or no git: skip silently.
    (None, text): git failed. The snapshot has head, ref (symbolic HEAD, "" when detached), the
    `status --porcelain` lines, a sha256 per listed file and `partial` when the hash limits were hit.
    Directories in `exclude` (the launcher's own files) are left out when inside the work tree."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")

    def git(*a):
        return subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, timeout=30, env=env)

    def text(res):
        return res.stdout.decode("utf-8", errors="replace")
    try:
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            msg = inside.stderr.decode("utf-8", errors="replace").strip()
            if "not a git repository" in msg.lower():
                return None, None
            return None, "git rev-parse failed: " + msg[:120]
        if text(inside).strip() != "true":
            return None, None
        top = git("rev-parse", "--show-toplevel")
        head = git("rev-parse", "HEAD")  # fails in a repository without commits: treated as ""
        ref = git("symbolic-ref", "-q", "HEAD")  # fails when detached: treated as ""
        status = git("status", "--porcelain", "-z", "--untracked-files=all")
    except FileNotFoundError:
        return None, None
    except subprocess.TimeoutExpired:
        return None, "git timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    if top.returncode != 0 or status.returncode != 0:
        return None, "git status failed: " + status.stderr.decode("utf-8", errors="replace").strip()[:120]
    root = os.path.realpath(text(top).strip())
    skip = [os.path.normcase(os.path.realpath(d)) for d in exclude]
    lines, paths, origs = _parse_status(text(status), root, skip)
    hashes, used, partial = {}, 0, False
    for rel, full in sorted(paths):
        if len(hashes) >= HASH_MAX_PATHS:
            partial = True
            break
        digest, size = hash_file(full, HASH_MAX_BYTES - used)
        if digest is None:
            partial = True
        elif digest != "dir":
            hashes[rel] = digest
            used += size
    return {"head": text(head).strip() if head.returncode == 0 else "",
            "ref": text(ref).strip() if ref.returncode == 0 else "",
            "status": sorted(lines), "hashes": hashes, "partial": partial, "root": root,
            "control": control_digests(root, [p for p, _ in paths] + origs)}, None


def _where(snap):
    return "%s %s" % (snap["ref"].replace("refs/heads/", "") or "detached", snap["head"][:7] or "none")


def git_compare(before, after, read_only):
    """Return (flags, ERROR lines). No snapshot on either side means no check."""
    flags, lines = set(), []
    if not before or not after:
        return flags, lines
    if before["head"] != after["head"] or before["ref"] != after["ref"]:
        flags.add("git_head_moved")
        lines.append("ERROR: git HEAD moved during the run (%s -> %s); workers must not commit or switch branches"
                     % (_where(before), _where(after)))
    if read_only:
        changed = sorted(set(before["status"]) ^ set(after["status"]))
        shown = [" ".join(d.split(None, 1)) for d in changed]
        listed = {d[3:] for d in changed}
        shown += ["content of %s" % path for path in sorted(before["hashes"])
                  if path in after["hashes"] and before["hashes"][path] != after["hashes"][path] and path not in listed]
        if shown:
            flags.add("wrote_files")
            lines.append("ERROR: a read-only run changed the work tree: %s%s"
                         % (", ".join(shown[:5]), " (+%d more)" % (len(shown) - 5) if len(shown) > 5 else ""))
    return flags, lines


# ---- control-plane check (before and after the run) ----------------------------------------
# Only what differs between the two snapshots counts, so an edit made before the run is not a finding. It sees the
# paths `git status` lists plus the machine-local files above; a change made and undone inside the run is not seen.

def control_path(rel):
    """Is this repository-relative path in the control plane (the lists of role_guard, compared in lower case)?
    Python caches and the session worktrees of Claude Code's desktop app (.claude/worktrees/) are not."""
    parts = rel.replace("\\", "/").lower().split("/")
    if "__pycache__" in parts or parts[-1].endswith(".pyc"):
        return False
    if parts[0] == ".claude" and len(parts) > 1 and parts[1] == "worktrees":
        return False
    return parts[0] in CONTROL_DIRS or (len(parts) == 1 and parts[0] in CONTROL_FILES)


def control_digest(path):
    """sha256 of the whole file (read in chunks, outside the budget of the general check), 'missing', 'link:...',
    'unreadable'; a regular file over CONTROL_HASH_CAP is 'stat:<size>:<mtime_ns>' instead, so a huge file is not read
    on every run. Never a marker that differs from another only by being a marker."""
    try:
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode) and st.st_size > CONTROL_HASH_CAP:
            return "stat:%d:%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        pass
    return hash_file(path, float("inf"))[0]


def control_digests(root, paths):
    """{path: control_digest} for the control-plane paths among `paths` and for the machine-local files, plus
    '<name> (link)': 'plain', or 'link:<real path>' when a top-level control folder or file is a link or junction
    (its files can then look the same through the link). `root` is a real path."""
    rels = {p for p in paths if control_path(p)} | set(CONTROL_HOOK_FILES) | set(CONTROL_SETTING_FILES)
    digests = {rel: control_digest(os.path.join(root, rel)) for rel in sorted(rels)}
    for name in CONTROL_DIRS + ("AGENTS.md", "CLAUDE.md"):
        lexical = os.path.normcase(os.path.join(root, name))
        real = os.path.normcase(os.path.realpath(lexical))
        digests[name + " (link)"] = "plain" if real == lexical else "link:" + real
    return digests


def tree_control(top, exclude):
    """control_digests of another work tree (one `git status` in it); None when git cannot say."""
    try:
        res = subprocess.run(["git", "-C", top, "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, timeout=30, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    skip = [os.path.normcase(os.path.realpath(d)) for d in exclude]
    _, paths, origs = _parse_status(res.stdout.decode("utf-8", errors="replace"), top, skip)
    return control_digests(top, [p for p, _ in paths] + origs)


def linked_main_tree(snap):
    """The main work tree when the snapshot's work tree is a linked worktree of another checkout, else None."""
    main = own_main_work_tree(snap["root"]) if snap else None
    if not main or os.path.normcase(os.path.realpath(main)) == os.path.normcase(snap["root"]):
        return None
    return os.path.realpath(main)


def control_compare(before, after, main_before, main_after, read_only, wrote_files):
    """Return (flags, lines, record for result.json). Needs both snapshots of the run's own work tree.
    A write run that changed a control-plane path or a hook file is `control_plane_changed`. Setting files, hook files
    of a read-only run and the main checkout of a linked worktree only warn; a read-only run gets no new status
    (what it changed in `git status` is `wrote_files` already)."""
    if not before or not after:
        return set(), [], None

    def diff(a, b):  # a file that could not be read on either side proves nothing: it is not a change
        return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p) and "unreadable" not in (a.get(p), b.get(p)))

    def show(paths):
        return ", ".join(paths[:5]) + (" (+%d more)" % (len(paths) - 5) if len(paths) > 5 else "")
    changed = diff(before["control"], after["control"])
    hooks = [p for p in changed if p.lower() in CONTROL_HOOK_FILES]
    settings = [p for p in changed if p.lower() in CONTROL_SETTING_FILES]
    plain = [p for p in changed if p not in hooks and p not in settings]
    flags, lines = set(), []
    blind = sorted(p for p in set(before["control"]) | set(after["control"])
                   if "unreadable" in (before["control"].get(p), after["control"].get(p)))
    if blind:
        lines.append("WARNING: the control-plane check is incomplete: %s could not be read" % show(blind))
    capped = sorted(p for p, v in after["control"].items() if v.startswith("stat:") and before["control"].get(p) == v)
    if capped:
        lines.append("WARNING: the control-plane check for %s compared size and time only (file over %d MiB)"
                     % (show(capped), CONTROL_HASH_CAP >> 20))
    if not read_only and (plain or hooks):
        flags.add("control_plane_changed")
        lines.append("ERROR: the control plane changed during the run (%s); workers must not edit .claude/, .codex/, "
                     ".agents/, AGENTS.md or CLAUDE.md (this worker or another process in this checkout)"
                     % show(sorted(plain + hooks)))
    elif plain:
        lines.append("%s: a read-only run changed the control plane (%s)" % ("ERROR" if wrote_files else "WARNING", show(plain)))
    warn = settings + (hooks if read_only else [])
    if warn:
        lines.append('WARNING: %s changed during the run; the host (an "always allow" approval) or the model collector '
                     'may have written it, or the worker did; check it' % show(sorted(warn)))
    main_changed = diff(main_before, main_after) if main_before is not None and main_after is not None else []
    if main_changed:
        lines.append("WARNING: the main checkout's control plane changed during the run (%s); another process may have "
                     "done it" % show(main_changed))
    return flags, lines, {"changed": changed, "main_changed": main_changed}


# ---- Codex hooks: did they write a role record? ---------------------------------------------

def list_sessions(state):
    """(file names under <state>/sessions, None), or (None, why) when the folder cannot be read. No folder: none."""
    try:
        return set(os.listdir(os.path.join(state, "sessions"))), None
    except FileNotFoundError:
        return set(), None
    except OSError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def hooks_ran(state, before, since):
    """True when a role record of host codex with ts >= since appeared that `before` (file names) did not have, False
    when none did, None when that cannot be told (a folder or a new record that cannot be read)."""
    after = list_sessions(state)[0]
    if before is None or after is None:
        return None
    unreadable = False
    for name in sorted(n for n in after - before if n.endswith(".json")):
        rec = parse_json(read_text(os.path.join(state, "sessions", name)))
        if rec is None:
            unreadable = True
        elif rec.get("host") == "codex" and isinstance(rec.get("ts"), (int, float)) and rec["ts"] >= since:
            return True
    return None if unreadable else False


# ---- reading results --------------------------------------------------------------------

def local_now():
    return datetime.now().astimezone()


def retry_epoch(text, now=None):
    """The moment a usage-limit message says to try again (its local time), or None. A time of day that has
    already passed today means tomorrow."""
    m = TRY_AGAIN.search(text)
    if not m:
        return None
    now = now or local_now()
    month, day, year, hour, minute, ampm = m.groups()
    if not 1 <= int(hour) <= 12:
        return None
    hour = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
    try:
        if year:  # a date that has already passed is no usable time: the caller falls back to its cooldown
            number = datetime.strptime(month[:3].title(), "%b").month
            when = datetime(int(year), number, int(day), hour, int(minute)).astimezone()
            return when.timestamp() if when > now else None
        today = datetime(now.year, now.month, now.day, hour, int(minute))
    except ValueError:
        return None
    for fold in (0, 1):  # a wall-clock time that occurs twice (DST ends) may still lie ahead in its second pass
        when = today.replace(fold=fold).astimezone()
        if when > now:
            return when.timestamp()
    return (today + timedelta(days=1)).astimezone().timestamp()


def detect_exhausted(vendor, code, out, err, data, skip=frozenset()):
    """Return (evidence, until_epoch or None) when the output shows a usage limit, else None."""
    if vendor == "claude":
        if data and data.get("is_error") is True:
            result = str(data.get("result", "")).strip()
            if CLAUDE_EX.fullmatch(result):
                m = re.search(r"limit reached\|(\d+)", result, re.I)
                return result[:200], int(m.group(1)) if m else retry_epoch(result)
    elif vendor == "openai":
        if code != 0:
            for line in (out + "\n" + err).splitlines():
                if line.strip() not in skip and OPENAI_EX.fullmatch(line.strip()):
                    return line.strip()[:200], retry_epoch(line)
    else:
        if data and data.get("error") and GOOGLE_JSON_EX.search(str(data["error"])):
            return str(data["error"])[:200], None
        if code != 0:
            for line in err.splitlines():
                if line.strip() not in skip and GOOGLE_LINE_EX.fullmatch(line.strip()):
                    return line.strip()[:200], None
    return None


def agy_label_matches(found, label, effort):
    """agy shows "Gemini 4 Hydrogen (XHigh)": the model name exactly, the effort word in any case."""
    m = re.fullmatch(r"(.*) \(([^()]*)\)", found)
    return bool(m) and m.group(1) == label and m.group(2).lower() == effort.lower()


def actual_model(vendor, model, effort, label, data, out, err, run_dir):
    """Return (found model or None, matches). Not found counts as a mismatch."""
    if vendor == "claude":
        keys = list((data or {}).get("modelUsage") or {})
        return (model if model in keys else (keys[0] if keys else None)), model in keys
    if vendor == "openai":
        head = []  # codex prints its metadata header on stderr, before the echoed "user" prompt
        for line in err.splitlines():
            if line.strip() == "user":
                break
            head.append(line)
        m = CODEX_MODEL.search("\n".join(head))
        return (m.group(1) if m else None), bool(m) and m.group(1) == model
    found = AGY_LABEL.findall(read_text(os.path.join(run_dir, "agy.log")))
    return (found[-1] if found else None), bool(found) and agy_label_matches(found[-1], label, effort)


def final_text(vendor, data, out, run_dir):
    if vendor == "openai":
        return read_text(os.path.join(run_dir, "last.txt")) or out
    key = "result" if vendor == "claude" else "response"
    return str(data[key]) if data and key in data else out


# ---- agy hooks in a worktree ------------------------------------------------------------------

def git_out(cwd, *args):
    """stdout (stripped) of `git -C cwd args`, or None when git fails or is missing."""
    try:
        res = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.decode("utf-8", errors="replace").strip() if res.returncode == 0 else None


def own_main_work_tree(top):
    """The main work tree of the repository whose work tree is `top`, from its git common dir; None when that cannot
    be told. A plain repository or submodule gives itself, a linked worktree gives the checkout it was made from
    (a submodule's common dir is <super>/.git/modules/<name>; its work tree is the dir's core.worktree).
    state.py wants the outermost superproject instead; that is not what this needs."""
    out = git_out(top, "rev-parse", "--git-common-dir")
    if not out:
        return None
    common = os.path.normpath(os.path.join(top, out))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    configured = git_out(common, "config", "--get", "core.worktree")
    main = os.path.normpath(os.path.join(common, configured)) if configured else None
    return main if main and os.path.lexists(os.path.join(main, ".git")) else None


def copy_agy_hooks(cwd):
    """Give a linked worktree the "tagteam" hooks of its main checkout (agy reads .agents/hooks.json of the work tree
    it runs in). Returns report lines; [] when there is nothing to do (not a linked worktree: the main checkout, a
    plain submodule, outside git). Only the "tagteam" key is written, other keys stay. Nothing is written when git
    tracks the file, or when .agents or hooks.json is a link or otherwise resolves outside <top>/.agents/hooks.json
    (it could be a tracked file elsewhere). The guard path in the hooks is absolute (the installer writes it so), so
    the copy calls the main checkout's guard. Must run before the first git snapshot."""
    top = git_out(cwd, "rev-parse", "--show-toplevel")
    top = os.path.normpath(top) if top else None
    main = own_main_work_tree(top) if top else None
    if not main or os.path.normcase(os.path.realpath(main)) == os.path.normcase(os.path.realpath(top)):
        return []
    try:
        with open(os.path.join(main, ".agents", "hooks.json"), encoding="utf-8") as fh:
            ours = json.load(fh)["tagteam"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    rel, dst = ".agents/hooks.json", os.path.join(top, ".agents", "hooks.json")
    plain = os.path.join(os.path.realpath(top), ".agents", "hooks.json")
    if os.path.islink(dst) or os.path.islink(os.path.dirname(dst)) or os.path.normcase(os.path.realpath(dst)) != os.path.normcase(plain):
        return ["WARNING: agy hooks not copied (%s is a link or resolves elsewhere)" % rel]
    tracked = git_out(top, "ls-files", "--", rel)
    if tracked is None or tracked:
        return ["WARNING: agy hooks not copied (%s is %s)" % (rel, "tracked" if tracked else "not checkable: git failed")]
    try:
        doc = {}
        if os.path.exists(dst):
            with open(dst, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
        doc["tagteam"] = ours
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        write_atomic(dst, json.dumps(doc, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        return ["WARNING: agy hooks not copied (%s: %s)" % (rel, exc)]
    return ["HOOKS copied to " + dst]


# ---- --status and --wait ----------------------------------------------------------------------

def pid_state(pid):
    """True while a process with this pid exists, False when it does not, None when the system cannot say (an
    error that is neither). Nothing is sent to it. On Windows os.kill(pid, 0) is not a liveness check (0 is
    CTRL_C_EVENT and any other value terminates the process), so ctypes asks instead; a process that exited with
    code 259 (STILL_ACTIVE) looks alive."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not _is_windows():
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            return True  # it exists, but its exit code is not ours to read
        return False if error == ERROR_INVALID_PARAMETER else None  # no such process / anything else: unknown
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid):
    """pid_state() as a plain yes/no: only a process known to exist counts."""
    return pid_state(pid) is True


def run_alive(started):
    """Is the run.py named by started.json still running? A pid can be reused after run.py died, so a live pid
    older than the run's timeout plus PID_GRACE does not count. Not exact: a reused pid within that time does."""
    if not pid_alive(started.get("pid")):
        return False
    try:
        return utc_now() <= parse_iso(started["started"]) + timedelta(seconds=int(started["timeout"]) + PID_GRACE)
    except (KeyError, TypeError, ValueError):
        return True


def run_report(run_id, run_dir):
    """(text, exit code) for a run from its records: the stored report and the launcher's own exit code; or
    `running` (EXIT_RUNNING); or `ended without a report` (EXIT_NO_REPORT, run.py died before it could report)."""
    text = read_text(os.path.join(run_dir, "report.txt"))
    if text:
        result = parse_json(read_text(os.path.join(run_dir, "result.json"))) or {}
        code = result.get("run_exit")
        if not isinstance(code, int):  # a record without it: take it from the status word of the first line
            m = re.search(r" status=(\w+)", text.split("\n", 1)[0])
            code = dict(PRIORITY, ok=0).get(m.group(1) if m else "", 1)
        return text, code
    started = parse_json(read_text(os.path.join(run_dir, "started.json"))) or {}
    if run_alive(started):
        try:
            secs = max(0, int((utc_now() - parse_iso(started["started"])).total_seconds()))
        except (KeyError, TypeError, ValueError):
            secs = 0
        return "RUN %s running secs=%d run_dir=%s\n" % (run_id, secs, run_dir), EXIT_RUNNING
    return "RUN %s ended without a report\n" % run_id, EXIT_NO_REPORT


def _stat(path):
    """os.stat result, None when the path is not there; any other error propagates (the caller says `unknown`)."""
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def inspect_run(run_dir):
    """('running', started), ('unknown', reason) or None for a run that has finished or died. Fail closed: what
    cannot be read or asked is `unknown`, never taken for dead. Only a finished report or a pid known to be gone
    ends a run; elapsed time does not (setup and checks run outside the CLI timeout), so a reused pid blocks."""
    try:
        report = _stat(os.path.join(run_dir, "report.txt"))
        if report is not None and report.st_size > 0:
            return None
        try:
            with open(os.path.join(run_dir, "started.json"), "rb") as fh:
                raw = fh.read()
        except (FileNotFoundError, NotADirectoryError):
            return None  # never registered (the launcher registers first thing): nothing was started
    except OSError as exc:
        return "unknown", "cannot read the run's records (%s)" % exc.__class__.__name__
    started = parse_json(raw.decode("utf-8", errors="replace"))
    if started is None or not isinstance(started.get("pid"), int) or started["pid"] <= 0:
        return "unknown", "started.json has no usable pid"
    state = pid_state(started["pid"])
    if state is None:
        return "unknown", "cannot tell whether pid %d is alive" % started["pid"]
    return ("running", started) if state else None


def list_running():
    """--running: one line per run that is going or may be going. Exit EXIT_RUNNING when one is going, else
    EXIT_UNKNOWN when one could not be judged (or the runs folder could not be listed), else 0 and `no running runs`."""
    runs = os.path.join(statelib.state_dir(os.getcwd()), "runs")
    try:
        names = sorted(os.listdir(runs))
    except (FileNotFoundError, NotADirectoryError):
        names = []
    except OSError as exc:
        sys.stdout.write("RUNS unknown cannot list %s (%s)\n" % (runs, exc.__class__.__name__))
        return EXIT_UNKNOWN
    going = unknown = 0
    for name in names:
        run_dir = os.path.join(runs, name)
        if not RUN_ID.fullmatch(name) or not os.path.isdir(run_dir):
            continue
        found = inspect_run(run_dir)
        if found is None:
            continue
        kind, info = found
        if kind == "unknown":
            unknown += 1
            sys.stdout.write("RUN %s unknown %s run_dir=%s\n" % (name, info, run_dir))
            continue
        going += 1
        try:
            secs = max(0, int((utc_now() - parse_iso(info["started"])).total_seconds()))
        except (KeyError, TypeError, ValueError):
            secs = 0
        word = lambda key: re.sub(r"\s+", "_", str(info.get(key) or "?"))  # noqa: E731  (one token per field)
        sys.stdout.write("RUN %s running vendor=%s model=%s secs=%d pid=%d run_dir=%s\n"
                         % (name, word("vendor"), word("model"), secs, info["pid"], run_dir))
    if going or unknown:
        sys.stdout.write("if no run is active, check the listed runs/<id> (a reused pid or a damaged record blocks)\n")
        return EXIT_RUNNING if going else EXIT_UNKNOWN
    sys.stdout.write("no running runs\n")
    return 0


def wait_for(run_id, run_dir, secs):
    """run_report(), repeated every second until the run is not running or `secs` have passed."""
    deadline = time.monotonic() + secs
    while True:
        text, code = run_report(run_id, run_dir)
        left = deadline - time.monotonic()
        if code != EXIT_RUNNING or left <= 0:
            return text, code
        time.sleep(min(1.0, left))


def lookup(args):
    """--status ID / --wait ID [--secs N]."""
    run_id = args.status if args.status is not None else args.wait
    if not RUN_ID.fullmatch(run_id):
        return fail("invalid run id %r: give a name under <state dir>/runs/" % run_id)
    runs = os.path.join(statelib.state_dir(os.getcwd()), "runs")
    run_dir = os.path.join(runs, run_id)
    if not os.path.isdir(run_dir):
        return fail("no run %s under %s" % (run_id, runs))
    if args.wait is None:
        text, code = run_report(run_id, run_dir)
    else:
        secs = WAIT_DEFAULT if args.secs is None else args.secs
        if not 0 <= secs <= WAIT_MAX:
            return fail("--secs must be 0 to %d" % WAIT_MAX)
        text, code = wait_for(run_id, run_dir, secs)
    sys.stdout.write(text)
    return code


# ---- main ---------------------------------------------------------------------------------

RUN_ONLY = ("vendor", "model", "effort", "prompt_file", "cwd", "timeout", "read_only", "run_commands", "allow_command")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="run.py", description="Run one worker through a vendor CLI, or look up a run.")
    p.add_argument("--vendor", choices=["claude", "openai", "google"])
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--prompt-file")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=int, default=None, help="seconds, default %d" % DEFAULT_TIMEOUT)
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--run-commands", action="store_true",
                   help="google only: with --read-only, use accept-edits so the worker can run commands")
    p.add_argument("--allow-command", action="append", default=[], metavar="CMD",
                   help="google only, repeatable: exact command agy may run (default: any command)")
    look = p.add_mutually_exclusive_group()
    look.add_argument("--status", metavar="ID", help="print the report of run ID (a name under <state dir>/runs/), or "
                      "`running` (exit %d), or `ended without a report` (exit %d); no other option but --wait"
                      % (EXIT_RUNNING, EXIT_NO_REPORT))
    look.add_argument("--wait", metavar="ID", help="like --status, but wait for the report up to --secs seconds")
    look.add_argument("--running", action="store_true", help="list the runs of this state folder that are still "
                      "going, one per line (id, vendor, model, seconds, pid); exit %d if one is going, %d if something could "
                      "not be read or asked so a run may be going, else 0 and `no running runs`; reads files "
                      "only, no other option" % (EXIT_RUNNING, EXIT_UNKNOWN))
    p.add_argument("--secs", type=int, default=None,
                   help="with --wait: seconds to wait, default %d, at most %d" % (WAIT_DEFAULT, WAIT_MAX))
    args = p.parse_args(argv)
    given = [o for o in RUN_ONLY if getattr(args, o) != p.get_default(o)]
    if args.running:
        if given or args.secs is not None:
            p.error("--running takes no other option")
    elif args.status is None and args.wait is None:
        missing = [o for o in ("vendor", "model", "effort", "prompt_file") if getattr(args, o) is None]
        if missing:
            p.error("the following arguments are required: " + ", ".join("--" + o.replace("_", "-") for o in missing))
        if args.secs is not None:
            p.error("--secs applies to --wait only")
    elif given or (args.secs is not None and args.wait is None):
        p.error("--status takes a run id and nothing else; --wait takes a run id and --secs")
    if args.timeout is None:
        args.timeout = DEFAULT_TIMEOUT
    return args


def resolve_cli(vendor, cfg):
    override = os.environ.get("TAGTEAM_CLI_" + vendor.upper())
    if override:
        try:
            base = json.loads(override)
            if isinstance(base, list) and base and all(isinstance(x, str) for x in base):
                return base
        except ValueError:
            pass
        return None
    exe = shutil.which(cfg["vendors"][vendor]["cli"])
    return [exe] if exe else None


def main(argv):
    args = parse_args(argv)
    if os.environ.get("HARNESS_DELEGATE_RUN") == "1":
        return fail("refused: this process is already a launcher worker (no re-delegation)")
    if args.running:
        return list_running()
    if args.status is not None or args.wait is not None:
        return lookup(args)
    config_dir = os.environ.get("TAGTEAM_CONFIG_DIR") or HERE
    try:
        with open(os.path.join(config_dir, "models.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        vcfg = cfg["vendors"][args.vendor]
    except (OSError, ValueError, KeyError) as exc:
        return fail("cannot read models.json for vendor %s: %s" % (args.vendor, exc))
    info = cfg.get("models", {}).get(args.model)
    if not info or info.get("vendor") != args.vendor:
        return fail("model %s is not registered for vendor %s" % (args.model, args.vendor))
    allowed = [e for e in ("low", "medium", "high", "xhigh", "max") if e in (info.get("grades") or {})]
    if args.effort not in allowed:
        return fail("effort %s is not allowed for %s (allowed: %s)" % (args.effort, args.model, ", ".join(allowed)))
    if args.allow_command and args.vendor != "google":
        return fail("--allow-command applies to vendor google only")
    if args.run_commands and args.vendor != "google":
        return fail("--run-commands applies to vendor google only")
    if any(not c.strip() for c in args.allow_command):
        return fail("--allow-command must not be empty")
    label = info.get("label")
    if args.vendor == "google" and not label:
        return fail("google model %s has no label in models.json" % args.model)
    label = agy_display(label) if label else label
    pool = pool_of(info)
    try:
        sandbox = windows_sandbox(config_dir) if args.vendor == "openai" else None
    except ValueError as exc:
        return fail(str(exc))
    base = resolve_cli(args.vendor, cfg)
    if not base:
        return fail("CLI for %s not found (%s)" % (args.vendor, vcfg.get("cli")))
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        return fail("--cwd is not a directory: " + cwd)
    try:
        with open(args.prompt_file, "rb") as fh:
            prompt = fh.read().decode("utf-8-sig", errors="replace")
    except OSError as exc:
        return fail("cannot read prompt file: %s" % exc)
    if (prompt.splitlines() or [""])[0].strip() != MARKER:
        prompt = MARKER + "\n" + prompt

    state = statelib.state_dir(cwd)
    try:
        statelib.ensure_writable(state)
    except statelib.StateError as exc:
        return fail(str(exc))
    start = utc_now()
    run_id = "%s-%s-%s" % (start.astimezone().strftime("%Y%m%d-%H%M%S"), args.vendor, secrets.token_hex(2))
    run_dir = os.path.join(state, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    # the registration first: --running must see this launcher before it touches the checkout (hooks copy, CLI)
    write_atomic(os.path.join(run_dir, "started.json"), json.dumps(
        {"pid": os.getpid(), "started": iso(start), "vendor": args.vendor, "model": args.model, "effort": args.effort,
         "timeout": args.timeout}, indent=2) + "\n")
    write_text(os.path.join(run_dir, "prompt.txt"), prompt)
    model_arg = vcfg.get("model_arg", "{model}").format(model=cli_id_of(args.model, info), effort=args.effort)
    try:
        cmd = build_command(args.vendor, base, model_arg, args.effort, args.read_only, cwd, run_dir, prompt,
                            args.timeout, args.allow_command, args.run_commands, sandbox)
    except ValueError as exc:
        return fail(str(exc))
    env = dict(os.environ, HARNESS_DELEGATE_RUN="1")
    for owner, keys in HOST_ENV.items():
        if args.vendor != owner:
            for key in keys:
                env.pop(key, None)
    hook_lines = copy_agy_hooks(cwd) if args.vendor == "google" else []  # before the first git snapshot
    # the run id and log paths before the CLI starts, so a long run can be found and watched
    sys.stdout.write("RUN %s started run_dir=%s stdout=%s stderr=%s\n" % (
        run_id, run_dir, os.path.join(run_dir, "stdout.txt"), os.path.join(run_dir, "stderr.txt")))
    sys.stdout.flush()
    stdin_bytes = None if args.vendor == "google" else prompt.encode("utf-8")
    own_files = [run_dir, state]  # the launcher's records may lie inside the work tree
    git_before, problem = git_snapshot(cwd, own_files)  # the baseline of the control-plane check too (after the hook copy)
    main_top = linked_main_tree(git_before)
    plain = os.path.realpath(cwd)
    loose_before = {"control": control_digests(plain, [])} if git_before is None and problem is None else None  # no git
    main_before = tree_control(main_top, own_files) if main_top else None
    sessions_before = list_sessions(state)[0] if args.vendor == "openai" else None
    t0 = time.time()
    start_error = None
    try:
        code, timed_out = execute(cmd, cwd, env, stdin_bytes, run_dir, args.timeout)
    except (OSError, ValueError) as exc:
        code, timed_out, start_error = -1, False, "%s: %s" % (type(exc).__name__, exc)
        write_text(os.path.join(run_dir, "stderr.txt"), start_error + "\n")
    secs = int(time.time() - t0)
    git_after, problem_after = git_snapshot(cwd, own_files)
    problem = problem or problem_after
    if not problem and bool(git_before) != bool(git_after):
        problem = "the work tree changed between the two snapshots"
    git_flags, git_lines = git_compare(git_before, git_after, args.read_only)
    main_after = tree_control(main_top, own_files) if main_top else None
    loose = (loose_before, {"control": control_digests(plain, [])}) if loose_before else (git_before, git_after)
    control_flags, control_lines, control = control_compare(*loose, main_before, main_after,
                                                            args.read_only, "wrote_files" in git_flags)
    git_flags |= control_flags
    git_lines += control_lines
    if problem:
        git_check = "unavailable"
        git_lines.append("WARNING: git check unavailable (%s)" % problem)
    elif git_before and (git_before["partial"] or git_after["partial"]):
        git_check = "partial"
        git_lines.append("WARNING: git content check is partial (more than %d listed files or %d MB)"
                         % (HASH_MAX_PATHS, HASH_MAX_BYTES >> 20))
    else:
        git_check = "ok" if git_before else "skipped"
    write_text(os.path.join(run_dir, "cmd.json"), json.dumps(
        {"argv": cmd, "cwd": cwd, "started": iso(start), "ended": iso(utc_now())}, indent=2) + "\n")

    out = read_text(os.path.join(run_dir, "stdout.txt"))
    err = read_text(os.path.join(run_dir, "stderr.txt"))
    data = parse_json(out)
    last = final_text(args.vendor, data, out, run_dir)
    write_text(os.path.join(run_dir, "last.txt"), last)

    flags = set()
    skip = {ln.strip() for ln in (prompt + "\n" + last).splitlines()}  # echoed prompt and answer are not CLI errors
    hit = None if timed_out else detect_exhausted(args.vendor, code, out, err, data, skip)
    if hit:
        flags.add("exhausted")
    if timed_out:
        flags.add("timeout")
    elif code != 0:
        flags.add("cli_failed")
    found, matches = (None, True)
    check_lines = []
    if not flags:  # the model and the Codex hooks are checked only on a clean exit
        found, matches = actual_model(args.vendor, args.model, args.effort, label, data, out, err, run_dir)
        if not matches:
            flags.add("model_mismatch")
        elif not last.strip():  # a clean exit with an empty answer is a failed run, not a result
            flags.add("no_output")
        if args.vendor == "openai":
            top = main_top or (git_before["root"] if git_before else cwd)  # Codex trusts the main checkout
            ran = hooks_ran(state, sessions_before, int(t0))
            trust = TRUST_WARNING.search(err)
            if ran is False:
                none = ("no tagteam role record appeared for this Codex run: the project's hooks did not run (or could "
                        "not write the record)")
                flags.add("hooks_not_run")
                check_lines.append("ERROR: %s. Codex skips project hooks until the project is trusted: run codex once "
                                   "in %s, trust the project, then trust the hooks with /hooks; `install.py %s --check "
                                   "--host codex --live` confirms it. The worker ran without the role guard; review its "
                                   "changes." % (none, top, top))
                if trust:
                    check_lines.append("CODEX: " + trust.group(0).strip())
            elif ran is None:
                check_lines.append("WARNING: hook check unavailable (the sessions folder or a new role record could not be "
                                  "read)")
        head = last.lstrip("\ufeff \t\r\n").split("\n", 1)[0].rstrip()  # the first line of the answer
        if head.startswith("REFUSED:"):  # AGENTS.md: a worker that cannot do the task answers with this first line
            flags.add("refused")
            check_lines.append("ERROR: the worker refused the task: " + head)
        elif head.startswith("NEEDS_INPUT:"):  # a normal question, still ok; shown where the orchestrator will see it
            check_lines.append(head)
    flags |= git_flags
    status, exit_code = next(((s, c) for s, c in PRIORITY if s in flags), ("ok", 0))

    lines = ["RUN %s vendor=%s model=%s effort=%s status=%s exit=%s secs=%d"
             % (run_id, args.vendor, args.model, args.effort, status, code, secs)]
    if status == "ok" or status == "model_mismatch":
        verdict = "match"
        if not matches:
            verdict = "MISMATCH, expected " + args.model
            if args.vendor == "google" and "@" in args.model:  # the display name is an assumption: show both sides
                verdict += ' (agy should show "%s (%s)", effort in any case; it showed "%s")' % (
                    label, args.effort, found or "nothing")
        lines.append("ACTUAL_MODEL: %s (%s)" % (found or "not found", verdict))
    result = {"id": run_id, "vendor": args.vendor, "pool": pool, "model": args.model, "effort": args.effort, "status": status,
              "exit_code": code, "secs": secs, "actual_model": found, "model_match": matches if found or status in ("ok", "model_mismatch") else None,
              "read_only": args.read_only, "run_dir": run_dir}
    result["git_check"] = git_check
    if control is not None:
        result["control_plane"] = control
    result["run_exit"] = exit_code  # run.py's own exit code (exit_code above is the CLI's): --status repeats it
    if status == "exhausted":
        evidence, epoch = hit
        until = datetime.fromtimestamp(epoch, timezone.utc) if epoch else start + timedelta(minutes=int(vcfg.get("cooldown_minutes", 60)))
        update_exhausted(state, pool, {"since": iso(start), "until": iso(until), "evidence": evidence, "source": "run.py " + run_id})
        lines.append("EXHAUSTED: %s until %s (%s)" % (pool, iso(until), evidence))
        result["until"] = iso(until)
    elif status == "ok":
        update_exhausted(state, pool, None)
    write_text(os.path.join(run_dir, "result.json"), json.dumps(result, indent=2) + "\n")
    if start_error:
        lines.append("ERROR: could not start the CLI (%s)" % start_error)
    lines.extend(hook_lines + git_lines + check_lines)
    if status == "no_output":
        denied = [d.get("display_name") or d.get("action") for d in (data or {}).get("denied_actions") or []
                  if isinstance(d, dict)] if isinstance(data, dict) else []
        lines.append("ERROR: the CLI exited cleanly but returned no answer%s"
                     % (" (denied tools: %s)" % ", ".join(map(str, denied)) if denied else ""))
    lines.append("OUTPUT: " + os.path.join(run_dir, "last.txt"))
    lines.append("---")
    lines.append(last[:REPORT_CHARS] + ("\n[truncated, see OUTPUT]" if len(last) > REPORT_CHARS else ""))
    text = "\n".join(lines) + "\n"
    try:  # written last, so a report.txt that exists is a whole one
        write_atomic(os.path.join(run_dir, "report.txt"), text)
    except OSError as exc:
        sys.stderr.write("run.py: warning: could not write report.txt (%s); --status will not show this report\n" % exc)
    sys.stdout.write(text)
    return exit_code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
