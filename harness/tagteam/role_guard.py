#!/usr/bin/env python3
"""tagteam role guard: the single role hook (Claude Code and Codex).

Usage: role_guard.py <SessionStart|UserPromptSubmit|PreToolUse|SubagentStart> [--host claude|codex]
Reads the hook JSON payload on stdin. A deny is written in the host's format:
  claude: exit 2 with a one-line reason on stderr;
  codex:  exit 0 with {"hookSpecificOutput": {..., "permissionDecision": "deny"}} on stdout
          (Codex CLI 0.169.0 ignores exit 2 from PreToolUse hooks), the reason also on stderr.
Any internal error fails open (exit 0 with a warning on stderr, nothing on stdout).
State lives in <main work tree>/.tagteam/ (see state.py); TAGTEAM_STATE_DIR overrides it (tests).
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import state  # noqa: E402  (same folder)

INSTALL_ROOT = os.path.dirname(os.path.dirname(HERE))  # <project>/.claude/tagteam -> <project>

MARKER = "HARNESS ROLE: DELEGATE"
START_SESSION = "mcp__ccd_session__start_session"
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "exec_command", "shell_command", "local_shell"}
# Codex hook payloads join namespace and name without a separator (collaboration + spawn_agent);
# the other two spellings are kept for other Codex versions.
SPAWN_TOOLS = {"collaborationspawn_agent", "spawn_agent", "collaboration.spawn_agent"}
DELEGATE_TOOLS = {"Agent", START_SESSION} | SPAWN_TOOLS
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
PATCH_TOOLS = {"apply_patch"}
PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): *(.+?)\s*$", re.M)
CONTROL_DIRS = (".claude", ".codex", ".agents")
CONTROL_FILES = ("agents.md", "claude.md")
AGENT_NAME = re.compile(r"""^\s*name\s*=\s*["']([^"']+)["']""", re.M)

# Accident check, not a security boundary: a worker can evade this with aliases,
# variables, scripts, or other tools. It only catches the plain command forms.
_ARG = r"""(?:"[^"]*"|'[^']*'|\S+)"""
GIT_WRITE = re.compile(
    r"\b(?i:git(?:\.exe)?)(?=\s)(?:\s+(?:-[Cc]\s+" + _ARG + r"|--[\w-]+(?:=" + _ARG + r")?|-\w+))*\s+(commit|push)\b"
)


class Denied(Exception):
    """Raised by a rule; main() writes it in the host's deny format."""


def deny(reason):
    raise Denied(" ".join(reason.split()))


def emit_deny(reason, host):
    """Write a deny for this host and return the exit code."""
    line = "tagteam: " + reason
    sys.stderr.write(line + "\n")
    if host == "codex":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": line}}) + "\n")
        return 0
    return 2


def session_file(payload):
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("session_id") or ""))
    if not sid:
        return None
    return os.path.join(state.state_dir(payload.get("cwd")), "sessions", sid + ".json")


def read_role(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("role")
    except (OSError, ValueError, AttributeError):
        return None


def role_of(payload):
    """"worker", "orchestrator", or None while the first prompt has not decided it yet.

    Worker = launcher env var, a subagent (agent_id: Codex sends the root session_id with it),
    or a recorded worker session."""
    if os.environ.get("HARNESS_DELEGATE_RUN") == "1" or payload.get("agent_id"):
        return "worker"
    path = session_file(payload)
    return read_role(path) if path else None


def is_worker(payload):
    return role_of(payload) == "worker"


def shell_command(tool_input):
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    return str(cmd or "")


def on_session_start(payload, host):
    role = role_of(payload)
    if role is None:  # a new session: its first prompt sets the role
        print("TAGTEAM: role=pending host=%s (set by the first prompt: worker if it carries %s)" % (host, MARKER))
    else:
        print("TAGTEAM: role=%s host=%s" % (role, host))
    if role != "worker":  # notices for the orchestrator; a pending session may become one
        try:
            note = model_update_notice(payload)
        except Exception:  # fail open: a notice bug must not block a session
            note = None
        if note:
            print(note)
        try:
            lines = task_notice()
        except Exception:  # fail open
            lines = []
        for line in lines:
            print(line)


def model_update_notice(payload):
    """One line about a pending model-list proposal, or a stale-collection warning."""
    base = state.state_dir(payload.get("cwd"))
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


TASK_LINES = 10


def task_notice():
    """One line per task in models.local.json `tasks`, 10 lines at most (the last one "and N more"); [] when unreadable."""
    cdir = os.environ.get("TAGTEAM_CONFIG_DIR") or os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(cdir, "models.local.json"), encoding="utf-8") as f:
            tasks = json.load(f).get("tasks")
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(tasks, dict):
        return []
    lines = []
    for name, spec in tasks.items():
        about = spec.get("about") if isinstance(spec, dict) else None
        if isinstance(about, str) and about.strip():
            lines.append("TAGTEAM: task %s - %s (route.py --task %s)" % (" ".join(str(name).split()), " ".join(about.split()), " ".join(str(name).split())))
    if len(lines) > TASK_LINES:  # the whole notice stays within TASK_LINES lines
        extra = len(lines) - (TASK_LINES - 1)
        lines = lines[:TASK_LINES - 1] + ["TAGTEAM: and %d more" % extra]
    return lines


def on_prompt(payload, host):
    if payload.get("agent_id"):
        return  # a subagent's prompt must not write the root session's record (Codex shares session_id)
    path = session_file(payload)
    if not path or os.path.exists(path):
        return  # record once on the first prompt; never change it afterwards
    prompt = payload.get("prompt") or ""
    marked = any(line.strip().startswith(MARKER) for line in prompt.splitlines())
    state.ensure_writable(os.path.dirname(path))  # StateError: fail open with a message that says how to fix it
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
    if tool in DELEGATE_TOOLS and worker:
        deny("worker sessions may not spawn subagents")

    # [role] Codex spawn: a custom agent runs on its definition's model and effort and silently
    # ignores spawn arguments; any other agent_type takes them and needs both.
    if tool in SPAWN_TOOLS:
        agent_type = tool_input.get("agent_type")
        given = [k for k in ("model", "reasoning_effort") if tool_input.get(k)]
        if agent_type and agent_type in custom_agents(payload):
            if given:
                deny("custom agent %s fixes model and effort in its definition; spawn arguments (%s) are ignored "
                     "- omit them or use agent_type default" % (agent_type, ", ".join(given)))
        elif len(given) < 2:
            deny("spawn_agent without a custom agent_type needs explicit model and reasoning_effort")

    # [role] Workers may not edit the control plane (plain edit tools only; shell writes are not seen).
    if worker:
        hit = control_plane_path(edit_paths(tool, tool_input), payload)
        if hit:
            deny("workers may not edit the control plane (%s)" % hit)


def custom_agents(payload):
    """Names of the project's Codex custom agents (.codex/agents/*.toml) in cwd and the install root."""
    names = set()
    for root in {payload.get("cwd") or os.getcwd(), INSTALL_ROOT}:
        folder = os.path.join(root, ".codex", "agents")
        try:
            files = [f for f in os.listdir(folder) if f.endswith(".toml")]
        except OSError:
            continue
        for f in files:
            try:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    m = AGENT_NAME.search(fh.read())
            except (OSError, ValueError):
                continue
            names.add(m.group(1) if m else f[:-len(".toml")])
    return names


def edit_paths(tool, tool_input):
    if tool in EDIT_TOOLS:
        path = tool_input.get(EDIT_TOOLS[tool])
        return [path] if isinstance(path, str) and path else []
    if tool in PATCH_TOOLS:
        text = tool_input.get("command", tool_input.get("patch", tool_input.get("input", "")))
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        return PATCH_PATH.findall(str(text or ""))
    return []


def control_plane_path(paths, payload):
    """The first path inside the control plane of the project root (git top of cwd, or cwd) or the install root."""
    if not paths:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    roots = {os.path.normpath(state._git(cwd, "rev-parse", "--show-toplevel") or cwd), os.path.normpath(INSTALL_ROOT)}
    for path in paths:
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        for root in roots:
            try:
                rel = os.path.relpath(full, root)
            except ValueError:  # another drive (Windows)
                continue
            parts = rel.replace("\\", "/").lower().split("/")
            if parts[0] == ".." or parts[0] == ".":
                continue
            if parts[0] in CONTROL_DIRS or (len(parts) == 1 and parts[0] in CONTROL_FILES):
                return path
    return None


def on_subagent_start(payload, host):
    """Record which model a subagent really runs on (agents.jsonl), so the orchestrator can check it."""
    rec = {"ts": int(time.time()), "host": host}
    for key in ("session_id", "agent_id", "agent_type", "model"):
        rec[key] = payload.get(key)
    folder = state.ensure_writable(state.state_dir(payload.get("cwd")))
    with open(os.path.join(folder, "agents.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


HANDLERS = {"SessionStart": on_session_start, "UserPromptSubmit": on_prompt, "PreToolUse": on_pre_tool,
            "SubagentStart": on_subagent_start}


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
    try:
        HANDLERS[event](payload, host)
    except Denied as d:
        return emit_deny(str(d), host)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # fail open: a hook bug must not block all work
        sys.stderr.write("tagteam: role_guard warning (allowed): %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(0)
