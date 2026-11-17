#!/usr/bin/env python3
"""tagteam role guard: the single role hook (Claude Code and Codex).

Usage: role_guard.py <SessionStart|UserPromptSubmit|PreToolUse> [--host claude|codex]
Reads the hook JSON payload on stdin. Deny = exit 2 with a one-line reason on stderr.
Any internal error fails open (exit 0 with a warning on stderr).
State lives in <git common dir>/tagteam/ (or <project root>/.tagteam/ outside git);
TAGTEAM_STATE_DIR overrides it (tests).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

MARKER = "HARNESS ROLE: DELEGATE"
START_SESSION = "mcp__ccd_session__start_session"
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "exec_command", "shell_command", "local_shell"}

# Accident check, not a security boundary: a worker can evade this with aliases,
# variables, scripts, or other tools. It only catches the plain command forms.
_ARG = r"""(?:"[^"]*"|'[^']*'|\S+)"""
GIT_WRITE = re.compile(
    r"\b(?i:git(?:\.exe)?)(?=\s)(?:\s+(?:-[Cc]\s+" + _ARG + r"|--[\w-]+(?:=" + _ARG + r")?|-\w+))*\s+(commit|push)\b"
)


def deny(reason):
    sys.stderr.write("tagteam: " + " ".join(reason.split()) + "\n")
    sys.exit(2)


def project_root(payload):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    if payload.get("cwd"):
        return payload["cwd"]
    here = os.path.dirname(os.path.abspath(__file__))  # <root>/.claude/tagteam
    return os.path.dirname(os.path.dirname(here))


def state_dir(payload):
    override = os.environ.get("TAGTEAM_STATE_DIR")
    if override:
        return override
    cwd = payload.get("cwd") or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=cwd, capture_output=True,
            text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return os.path.join(os.path.abspath(os.path.join(cwd, out.stdout.strip())), "tagteam")
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.join(project_root(payload), ".tagteam")


def session_file(payload):
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("session_id") or ""))
    if not sid:
        return None
    return os.path.join(state_dir(payload), "sessions", sid + ".json")


def read_role(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("role")
    except (OSError, ValueError, AttributeError):
        return None


def is_worker(payload):
    """Worker = recorded worker session, launcher env var, or a subagent (agent_id)."""
    if os.environ.get("HARNESS_DELEGATE_RUN") == "1" or payload.get("agent_id"):
        return True
    path = session_file(payload)
    return bool(path) and read_role(path) == "worker"


def shell_command(tool_input):
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    return str(cmd or "")


def on_session_start(payload, host):
    role = "worker" if is_worker(payload) else "orchestrator"
    print("TAGTEAM: role=%s host=%s" % (role, host))
    if role == "orchestrator":
        try:
            note = model_update_notice(payload)
        except Exception:  # fail open: a notice bug must not block a session
            note = None
        if note:
            print(note)


def model_update_notice(payload):
    """One line about a pending model-list proposal, or a stale-collection warning."""
    base = state_dir(payload)
    with open(os.path.join(base, "model-update.json"), encoding="utf-8") as f:
        prop = json.load(f)
    created = datetime.datetime.strptime(prop["created"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    days = (datetime.datetime.now(datetime.timezone.utc) - created).days
    if days > 14:
        return ("TAGTEAM: model list not collected for %d days; "
                "check the scheduled task (install.py --schedule)." % days)
    try:
        with open(os.path.join(base, "model-update.applied"), encoding="utf-8") as f:
            applied = json.load(f)["digest"]
    except FileNotFoundError:
        applied = None  # never applied
    except (OSError, ValueError, KeyError, TypeError):
        return None  # unreadable marker: stay silent
    changes = len(prop.get("changes") or [])
    errors = sum(1 for c in (prop.get("cli") or {}).values() if c.get("status") == "error")
    if prop.get("digest") == applied or not (changes or errors):
        return None
    return ("TAGTEAM: model update proposal (%d changes, %d CLI errors, collected %s); "
            "orchestrator: load skill model-update." % (changes, errors, prop["created"][:10]))


def on_prompt(payload, host):
    path = session_file(payload)
    if not path or os.path.exists(path):
        return  # record once on the first prompt; never change it afterwards
    prompt = payload.get("prompt") or ""
    marked = any(line.strip().startswith(MARKER) for line in prompt.splitlines())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"role": "worker" if marked else "orchestrator", "host": host,
                   "ts": int(time.time())}, f)


def on_pre_tool(payload, host):
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    worker = is_worker(payload)

    # [role] A worker session must not open further sessions. The orchestrator must pick
    # model and effort explicitly (fork cannot take a model, so only fresh sessions pass),
    # unless it starts a stored proposal by proposal_id alone (no model/effort/prompt).
    if tool == START_SESSION:
        if worker:
            deny("worker sessions may not open sessions")
        shaping = any(tool_input.get(k) for k in ("model", "effort", "prompt"))
        proposal_only = bool(tool_input.get("proposal_id")) and not shaping
        if not proposal_only and not (tool_input.get("model") and tool_input.get("effort")):
            deny("start_session needs an explicit model and effort")

    # [role] Workers may not commit or push; the orchestrator integrates.
    if tool in SHELL_TOOLS and worker and GIT_WRITE.search(shell_command(tool_input)):
        deny("workers may not run git commit or git push")

    # [role] Workers may not delegate again (subagents lose the Agent tool by definition).
    if tool in ("Agent", "spawn_agent") and worker:
        deny("worker sessions may not spawn subagents")


HANDLERS = {"SessionStart": on_session_start, "UserPromptSubmit": on_prompt, "PreToolUse": on_pre_tool}


def main(argv):
    host = "claude"
    rest = list(argv[1:])
    if "--host" in rest:
        i = rest.index("--host")
        if i + 1 < len(rest):
            host = rest[i + 1]
        del rest[i:i + 2]
    event = rest[0] if rest else ""
    if event not in HANDLERS:
        sys.stderr.write("tagteam: unknown event %r (ignored)\n" % event)
        return 0
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        raise ValueError("hook payload is not an object")
    HANDLERS[event](payload, host)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # fail open: a hook bug must not block all work
        sys.stderr.write("tagteam: role_guard warning (allowed): %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(0)
