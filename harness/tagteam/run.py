#!/usr/bin/env python3
"""Launcher: run one worker through another vendor's CLI, verify the model, report briefly.

  run.py --vendor claude|openai|google --model ID --effort E --prompt-file PATH
         [--cwd DIR] [--timeout SECONDS] [--read-only]

Exit: 0 ok / 1 CLI failed / 2 usage, config or pre-run check / 3 vendor exhausted /
4 actual model differs or cannot be confirmed / 5 timeout.
Records go to <state dir>/runs/<id>/. A usage-limit sign is written to
<state dir>/exhausted.json (route.py reads it). Environment (tests): TAGTEAM_STATE_DIR,
TAGTEAM_CONFIG_DIR, TAGTEAM_CLI_<VENDOR> (JSON array replacing the CLI).
Python 3.9+, standard library only. Accident check, not a security boundary.
"""
import argparse
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
import role_guard  # noqa: E402  (same folder; its CLI entry is behind a __main__ guard)

MARKER = "HARNESS ROLE: DELEGATE"
AGY_ARG_LIMIT = 30000  # characters of the serialized command; Windows stops near 32K
REPORT_CHARS = 4000
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
PRIORITY = [("exhausted", 3), ("timeout", 5), ("model_mismatch", 4), ("cli_failed", 1)]


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
    os.makedirs(state, exist_ok=True)
    write_text(tmp, json.dumps({"schema": 1, "vendors": vendors}, indent=2) + "\n")
    os.replace(tmp, path)


# ---- commands ---------------------------------------------------------------------------

def build_command(vendor, base, model_arg, effort, read_only, cwd, run_dir, prompt, timeout):
    """Argv for one vendor (design 5.3). The prompt is sent on stdin except for google."""
    if vendor == "claude":
        return base + ["-p", "--output-format", "json", "--model", model_arg, "--effort", effort,
                       "--permission-mode", "plan" if read_only else "auto", "--permission-prompts", "none"]
    if vendor == "openai":
        cmd = base + ["exec", "-m", model_arg, "-c", "model_reasoning_effort=" + effort,
                      "-s", "read-only" if read_only else "workspace-write", "-C", cwd,
                      "-o", os.path.join(run_dir, "last.txt")]
        if _is_windows():
            cmd += ["-c", "windows.sandbox=unelevated"]
        return cmd + ["-"]
    tail = ["--model", model_arg, "--effort", effort, "--mode", "plan" if read_only else "accept-edits",
            "--output-format", "json", "--log-file", os.path.join(run_dir, "agy.log"), "--print-timeout", "%ds" % timeout]
    cmd = base + ["-p", prompt] + tail
    if len(subprocess.list2cmdline(cmd)) > AGY_ARG_LIMIT:  # quoting can double the length
        hint = "%s\nRead and follow the task in %s" % (MARKER, os.path.join(run_dir, "prompt.txt"))
        cmd = base + ["-p", hint] + tail
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
    if args.effort not in info.get("efforts", []):
        return fail("effort %s is not allowed for %s (allowed: %s)" % (args.effort, args.model, ", ".join(info.get("efforts", []))))
    label = info.get("label")
    if args.vendor == "google" and not label:
        return fail("google model %s has no label in models.json" % args.model)
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

    state = role_guard.state_dir({"cwd": cwd})
    start = utc_now()
    run_id = "%s-%s-%s" % (start.astimezone().strftime("%Y%m%d-%H%M%S"), args.vendor, secrets.token_hex(2))
    run_dir = os.path.join(state, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    write_text(os.path.join(run_dir, "prompt.txt"), prompt)
    model_arg = vcfg.get("model_arg", "{model}").format(model=args.model, effort=args.effort)
    cmd = build_command(args.vendor, base, model_arg, args.effort, args.read_only, cwd, run_dir, prompt, args.timeout)
    env = dict(os.environ, HARNESS_DELEGATE_RUN="1")
    stdin_bytes = None if args.vendor == "google" else prompt.encode("utf-8")
    t0 = time.time()
    start_error = None
    try:
        code, timed_out = execute(cmd, cwd, env, stdin_bytes, run_dir, args.timeout)
    except (OSError, ValueError) as exc:
        code, timed_out, start_error = -1, False, "%s: %s" % (type(exc).__name__, exc)
        write_text(os.path.join(run_dir, "stderr.txt"), start_error + "\n")
    secs = int(time.time() - t0)
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
    status, exit_code = next(((s, c) for s, c in PRIORITY if s in flags), ("ok", 0))

    lines = ["RUN %s vendor=%s model=%s effort=%s status=%s exit=%s secs=%d"
             % (run_id, args.vendor, args.model, args.effort, status, code, secs)]
    if status == "ok" or status == "model_mismatch":
        lines.append("ACTUAL_MODEL: %s (%s)" % (found or "not found", "match" if matches else "MISMATCH, expected " + args.model))
    result = {"id": run_id, "vendor": args.vendor, "model": args.model, "effort": args.effort, "status": status,
              "exit_code": code, "secs": secs, "actual_model": found, "model_match": matches if found or status in ("ok", "model_mismatch") else None,
              "read_only": args.read_only, "run_dir": run_dir}
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
    lines.append("OUTPUT: " + os.path.join(run_dir, "last.txt"))
    lines.append("---")
    lines.append(last[:REPORT_CHARS] + ("\n[truncated, see OUTPUT]" if len(last) > REPORT_CHARS else ""))
    sys.stdout.write("\n".join(lines) + "\n")
    return exit_code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
