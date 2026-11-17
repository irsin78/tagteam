#!/usr/bin/env python3
"""Launcher: run one worker through another vendor's CLI, verify the model, report briefly.

  run.py --vendor claude|openai|google --model ID --effort E --prompt-file PATH
         [--cwd DIR] [--timeout SECONDS] [--read-only] [--run-commands] [--allow-command CMD ...]

Exit: 0 ok / 1 CLI failed / 2 usage, config or pre-run check / 3 vendor exhausted /
4 actual model differs or cannot be confirmed / 5 timeout / 6 no answer /
7 git HEAD or branch moved during the run / 8 a read-only run changed the work tree.
Google (agy) runs always get --settings with a per-run command allow list: command(*), or
exactly command(CMD) for each --allow-command (google only). Google --read-only uses --mode
plan, which never runs commands; --read-only --run-commands (google only) uses accept-edits
so a reviewer can run tests and git diff, and read-only is then enforced only by the git check.
The git check (every vendor; HEAD, branch, `git status` listing and content hashes of the
listed files, before and after) catches common accidents. It is not a security boundary:
ignored files, paths outside the repository, stashes and other refs are not seen, and it
sees every change in the checkout during the run, other writers' included.
Records go to <state dir>/runs/<id>/. The first line, printed before the CLI starts, is
`RUN <id> started run_dir=<dir> stdout=<file> stderr=<file>`; the report follows when the CLI ends.
An unwritable state folder is a pre-run error (exit 2; inside a Codex sandbox run.py needs escalated
permissions). A usage-limit sign is written to <state dir>/exhausted.json (route.py reads it).
Codex on Windows gets `-c windows.sandbox=<value>` from models.local.json codex.windows_sandbox
(default "unelevated"; null or "" adds nothing) [host: codex-cli 0.169.0 Windows, the elevated
sandbox failed every command with CreateProcessWithLogonW 1385, measured 2026-11-17 / re-test after
a Codex update]. Non-Claude CLIs start without CLAUDECODE and CLAUDE_CODE_ENTRYPOINT, so a tool inside
the worker does not take the host for Claude Code. Workers get no extra writable folder (no --add-dir):
they do not write the state folder. Environment (tests): TAGTEAM_STATE_DIR,
TAGTEAM_CONFIG_DIR, TAGTEAM_CLI_<VENDOR> (JSON array replacing the CLI).
Python 3.9+, standard library only. Accident check, not a security boundary.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import state as statelib  # noqa: E402  (same folder; `state` is a local name in main)

MARKER = "HARNESS ROLE: DELEGATE"
AGY_ARG_LIMIT = 30000  # characters of the serialized command; Windows stops near 32K
REPORT_CHARS = 4000
HASH_MAX_PATHS = 2000  # git check: content hashes of listed files; beyond this the check is partial
HASH_MAX_BYTES = 50 * 1024 * 1024
FLAGS = re.I | re.S
CLAUDE_EX = re.compile(
    r"(?:API Error: )?(?:HTTP )?429.*|(?:usage|rate)[ -]limit(?: exceeded| reached)?.*"
    r"|You've hit your usage limit.*|Claude AI usage limit reached\|\d+.*"
    r"|\d+-hour limit reached.*|rate_limit_error.*", FLAGS)
OPENAI_EX = re.compile(
    "(?:⚠️ |ERROR: |stream error: )*(?:HTTP 429(?: Too Many Requests)?|429 Too Many Requests"
    r"|(?:usage|rate)[ -]limit(?: exceeded| reached)?[.!]?|You've hit your usage limit.*"
    r"|usage limit (?:reached|exceeded).*|exceeded retry limit, last status: 429.*"
    r"|Quota exceeded\. Check your plan and billing details\..*)", re.I)
GOOGLE_JSON_EX = re.compile(r"quota|rate[ -]limit|\b429\b|RESOURCE_EXHAUSTED", re.I)
GOOGLE_LINE_EX = re.compile(
    r"(?:ERROR: )?(?:HTTP 429.*|429 Too Many Requests|(?:usage|rate)[ -]limit.*"
    r"|quota(?: exceeded| exhausted)?[.!]?)", re.I)
AGY_LABEL = re.compile(r'Propagating selected model override to backend: label="([^"]+)"')
CODEX_MODEL = re.compile(r"^model:\s*(\S+)", re.M)
PRIORITY = [("exhausted", 3), ("git_head_moved", 7), ("timeout", 5), ("model_mismatch", 4), ("wrote_files", 8),
            ("cli_failed", 1), ("no_output", 6)]
WINDOWS_SANDBOX = "unelevated"  # codex windows.sandbox default; models.local.json codex.windows_sandbox overrides it
# The values Codex accepts: codex-cli 0.169.0 rejects others at start with
# "unknown variant `x`, expected one of `elevated`, `unelevated`, `mxc` in `windows.sandbox`".
WINDOWS_SANDBOX_VALUES = ("elevated", "unelevated", "mxc")
CLAUDE_HOST_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")  # removed for non-Claude CLIs (host detection)


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
    tmp = "%s.%s.tmp" % (path, secrets.token_hex(3))
    statelib.ensure_writable(state)
    write_text(tmp, json.dumps({"schema": 1, "vendors": vendors}, indent=2) + "\n")
    os.replace(tmp, path)


# ---- commands ---------------------------------------------------------------------------

def agy_settings(allow_commands):
    """The per-run agy settings JSON: command(*) by default, else exactly the listed commands."""
    allow = ["command(%s)" % c for c in allow_commands] if allow_commands else ["command(*)"]
    return json.dumps({"permissions": {"allow": allow}}, separators=(",", ":"))


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
    lines, paths = [], []
    entries = text(status).split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if "R" in entry[:2] or "C" in entry[:2]:
            i += 1  # the original path follows as its own entry
        full = os.path.join(root, entry[3:])
        if _under(os.path.normcase(os.path.realpath(full)), skip):
            continue
        lines.append(entry)
        paths.append((entry[3:], full))
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
            "status": sorted(lines), "hashes": hashes, "partial": partial}, None


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


# ---- reading results --------------------------------------------------------------------

def detect_exhausted(vendor, code, out, err, data, skip=frozenset()):
    """Return (evidence, until_epoch or None) when the output shows a usage limit, else None."""
    if vendor == "claude":
        if data and data.get("is_error") is True:
            result = str(data.get("result", "")).strip()
            if CLAUDE_EX.fullmatch(result):
                m = re.search(r"limit reached\|(\d+)", result, re.I)
                return result[:200], int(m.group(1)) if m else None
    elif vendor == "openai":
        if code != 0:
            for line in (out + "\n" + err).splitlines():
                if line.strip() not in skip and OPENAI_EX.fullmatch(line.strip()):
                    return line.strip()[:200], None
    else:
        if data and data.get("error") and GOOGLE_JSON_EX.search(str(data["error"])):
            return str(data["error"])[:200], None
        if code != 0:
            for line in err.splitlines():
                if line.strip() not in skip and GOOGLE_LINE_EX.fullmatch(line.strip()):
                    return line.strip()[:200], None
    return None


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
    want = "%s (%s)" % (label, effort.capitalize())
    return (found[-1] if found else None), bool(found) and found[-1] == want


def final_text(vendor, data, out, run_dir):
    if vendor == "openai":
        return read_text(os.path.join(run_dir, "last.txt")) or out
    key = "result" if vendor == "claude" else "response"
    return str(data[key]) if data and key in data else out


# ---- main ---------------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(prog="run.py", description="Run one worker through a vendor CLI.")
    p.add_argument("--vendor", required=True, choices=["claude", "openai", "google"])
    p.add_argument("--model", required=True)
    p.add_argument("--effort", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--run-commands", action="store_true",
                   help="google only: with --read-only, use accept-edits so the worker can run commands")
    p.add_argument("--allow-command", action="append", default=[], metavar="CMD",
                   help="google only, repeatable: exact command agy may run (default: any command)")
    return p.parse_args(argv)


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
    write_text(os.path.join(run_dir, "prompt.txt"), prompt)
    model_arg = vcfg.get("model_arg", "{model}").format(model=args.model, effort=args.effort)
    try:
        cmd = build_command(args.vendor, base, model_arg, args.effort, args.read_only, cwd, run_dir, prompt,
                            args.timeout, args.allow_command, args.run_commands, sandbox)
    except ValueError as exc:
        return fail(str(exc))
    env = dict(os.environ, HARNESS_DELEGATE_RUN="1")
    if args.vendor != "claude":
        for key in CLAUDE_HOST_ENV:
            env.pop(key, None)
    # the run id and log paths before the CLI starts, so a long run can be found and watched
    sys.stdout.write("RUN %s started run_dir=%s stdout=%s stderr=%s\n" % (
        run_id, run_dir, os.path.join(run_dir, "stdout.txt"), os.path.join(run_dir, "stderr.txt")))
    sys.stdout.flush()
    stdin_bytes = None if args.vendor == "google" else prompt.encode("utf-8")
    own_files = [run_dir, state]  # the launcher's records may lie inside the work tree
    git_before, problem = git_snapshot(cwd, own_files)
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
    if not flags:  # the model is checked only on a clean exit
        found, matches = actual_model(args.vendor, args.model, args.effort, label, data, out, err, run_dir)
        if not matches:
            flags.add("model_mismatch")
        elif not last.strip():  # a clean exit with an empty answer is a failed run, not a result
            flags.add("no_output")
    flags |= git_flags
    status, exit_code = next(((s, c) for s, c in PRIORITY if s in flags), ("ok", 0))

    lines = ["RUN %s vendor=%s model=%s effort=%s status=%s exit=%s secs=%d"
             % (run_id, args.vendor, args.model, args.effort, status, code, secs)]
    if status == "ok" or status == "model_mismatch":
        lines.append("ACTUAL_MODEL: %s (%s)" % (found or "not found", "match" if matches else "MISMATCH, expected " + args.model))
    result = {"id": run_id, "vendor": args.vendor, "model": args.model, "effort": args.effort, "status": status,
              "exit_code": code, "secs": secs, "actual_model": found, "model_match": matches if found or status in ("ok", "model_mismatch") else None,
              "read_only": args.read_only, "run_dir": run_dir}
    result["git_check"] = git_check
    if status == "exhausted":
        evidence, epoch = hit
        until = datetime.fromtimestamp(epoch, timezone.utc) if epoch else start + timedelta(minutes=int(vcfg.get("cooldown_minutes", 60)))
        update_exhausted(state, args.vendor, {"since": iso(start), "until": iso(until), "evidence": evidence, "source": "run.py " + run_id})
        lines.append("EXHAUSTED: %s until %s (%s)" % (args.vendor, iso(until), evidence))
        result["until"] = iso(until)
    elif status == "ok":
        update_exhausted(state, args.vendor, None)
    write_text(os.path.join(run_dir, "result.json"), json.dumps(result, indent=2) + "\n")
    if start_error:
        lines.append("ERROR: could not start the CLI (%s)" % start_error)
    lines.extend(git_lines)
    if status == "no_output":
        denied = [d.get("display_name") or d.get("action") for d in (data or {}).get("denied_actions") or []
                  if isinstance(d, dict)] if isinstance(data, dict) else []
        lines.append("ERROR: the CLI exited cleanly but returned no answer%s"
                     % (" (denied tools: %s)" % ", ".join(map(str, denied)) if denied else ""))
    lines.append("OUTPUT: " + os.path.join(run_dir, "last.txt"))
    lines.append("---")
    lines.append(last[:REPORT_CHARS] + ("\n[truncated, see OUTPUT]" if len(last) > REPORT_CHARS else ""))
    sys.stdout.write("\n".join(lines) + "\n")
    return exit_code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
