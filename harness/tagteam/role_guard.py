#!/usr/bin/env python3
"""tagteam role guard: the single role hook (Claude Code, Codex, agy).

Usage: role_guard.py <SessionStart|UserPromptSubmit|PreToolUse|SubagentStart|PreInvocation> [--host claude|codex|agy]
The hook JSON payload is read from stdin. Rules and the output format of each host: the template's docs/design.md.
State: <main work tree>/.tagteam/ (see state.py); TAGTEAM_STATE_DIR overrides it (tests).
Codex CLI 0.169.0 ignores exit 2 from PreToolUse hooks, so a Codex deny is JSON on stdout.
[host: agy 1.5.10 blocks a PreToolUse hook that prints nothing; every agy path prints one JSON object, exit 0, measured 2026-11-18 / re-test after an agy update]
"""
import datetime
import json
import ntpath
import os
import re
import subprocess
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
# agy tool names count only on host agy; of the delegation tools only invoke_subagent is measured
# (the other names come from agy's changelog).
AGY_SHELL_TOOLS = {"run_command"}
AGY_DELEGATE_TOOLS = {"invoke_subagent", "define_subagent", "run_workflow", "swarm"}
# agy edit tools and the argument that holds the absolute path [measured: agy 1.5.11, Windows, headless;
# the other arguments (Overwrite, Append, ArtifactMetadata, Content, Target, Replacement) are not judged]
AGY_EDIT_TOOLS = {"write_file": "Path", "edit_file": "Path"}
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
    r"\b(?i:git(?:\.exe)?)[\"']?(?=\s)(?:\s+(?:-[Cc]\s+" + _ARG + r"|--[\w-]+(?:=" + _ARG + r")?|-\w+))*\s+(commit|push)\b"
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


UNRESOLVED = "unresolved"  # a role that is not known: protected calls get a worker's rules


def load_record(path):
    """(record, status): "none" when there is no file, "ok" for a record with a known role, "unresolved" for
    a record that says the first role decision was lost, "bad" when the file exists but cannot be read, is
    empty, corrupt or names no known role."""
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (FileNotFoundError, NotADirectoryError):  # no such file, or the folder above it is not a folder
        return {}, "none"
    except (OSError, ValueError):
        return {}, "bad"
    role = rec.get("role") if isinstance(rec, dict) else None
    if role in ("worker", "orchestrator"):
        return rec, "ok"
    return (rec, "unresolved") if role == UNRESOLVED else ({}, "bad")


def unresolved_why(status, path):
    """Why a record leaves the role unresolved, and how to get out of it."""
    if status == "unresolved":
        return ("the first role decision of this conversation was lost; check the state folder, then remove %s "
                "to decide again" % path)
    return "the role record %s is empty, unreadable or corrupt; check the state folder or remove that file" % path


def create_record(path, role, host, **extra):
    """Publish a session's role record without ever replacing an existing one: True when this call created
    it, False when a record is there already or another process is publishing one (the caller then reads
    the winner). Only a complete record ever appears under the final name: the content goes to a temporary
    file first and is linked in (os.link fails when the name exists). Without hard links a lock folder
    serialises the writers and the temporary file is renamed in. StateError when the folder is unwritable."""
    rec = dict({"role": role, "host": host, "ts": int(time.time())}, **extra)
    folder = state.ensure_writable(os.path.dirname(path))
    fd, tmp = state.create_exclusive(folder, ".role-")  # not mkstemp: on affected Windows Pythons it retries a refusal up to TMP_MAX times, which looks like a hang
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:  # this file system has no hard links
            return _publish_locked(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


LOCK_STALE_SECONDS = 30


def _publish_locked(tmp, path):
    lock = path + ".lock"
    for attempt in (0, 1):
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                stale = time.time() - os.path.getmtime(lock) > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if attempt or not stale:
                return False  # someone is publishing right now; its record decides
            try:
                os.rmdir(lock)  # left behind by a process that died
            except OSError:
                return False
    try:
        if os.path.exists(path):
            return False
        os.replace(tmp, path)  # complete content, and nothing is replaced: we hold the lock
        return True
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


COMMAND_PREFIX = re.compile(r"^/[\w-]+[ \t]+")


def strip_command_prefix(line):
    """Drop a leading slash command ("/plan "): some hosts put it in front of the prompt."""
    return COMMAND_PREFIX.sub("", line, count=1)


def has_marker(prompt):
    """True when one line of the prompt starts with the worker marker (after a slash-command prefix)."""
    return any(strip_command_prefix(line.strip()).startswith(MARKER) for line in prompt.splitlines())


def role_of(payload):
    """"worker", "orchestrator", "unresolved" (a record that cannot be trusted), or None while the first prompt
    has not decided it yet.

    Worker = launcher env var, a subagent (agent_id: Codex sends the root session_id with it),
    or a recorded worker session."""
    if os.environ.get("HARNESS_DELEGATE_RUN") == "1" or payload.get("agent_id"):
        return "worker"
    path = session_file(payload)
    if not path:
        return None
    rec, status = load_record(path)
    if status == "ok":
        return rec["role"]
    return UNRESOLVED if status in ("bad", "unresolved") else None


def is_worker(payload):
    return role_of(payload) in ("worker", UNRESOLVED)


def shell_command(tool_input):
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    return str(cmd or "")


def session_start_lines(payload, host):
    """The lines a new session sees: its role, then notices for the orchestrator."""
    role = role_of(payload)
    if role is None:  # a new session: its first prompt sets the role
        how = ("the first model call: worker if it carries %s or has no user input" % MARKER if host == "agy"
               else "the first prompt: worker if it carries %s" % MARKER)
        lines = ["TAGTEAM: role=pending host=%s (set by %s)" % (host, how)]
    else:
        lines = ["TAGTEAM: role=%s host=%s" % (role, host)]
    if role != "worker":  # notices for the orchestrator; a pending session may become one
        try:
            note = model_update_notice(payload)
        except Exception:  # fail open: a notice bug must not block a session
            note = None
        if note:
            lines.append(note)
        try:
            note = update_notice()
        except Exception:  # fail open
            note = None
        if note:
            lines.append(note)
        try:
            lines.extend(task_notice())
        except Exception:  # fail open
            pass
    return lines


def on_session_start(payload, host):
    for line in session_start_lines(payload, host):
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


UPDATE_GIT_TIMEOUT = 3  # seconds for the one git call of update_notice
COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")
DESCRIBE = re.compile(r"[A-Za-z0-9._+-]{1,60}")  # install.json is a tracked file: only a plain word reaches the session


def update_notice(folder=HERE):
    """One line when the checkout recorded at install time has commits the installed template lacks. Local files
    and one git call, no network. `folder` holds install.json (the installed commit) and install.local.json (the
    checkout, relative to the project or absolute); the installer writes both. Silent (None) when either is missing
    or unreadable, the commit is unknown to the checkout, the checkout is not ahead (same commit, behind or
    diverged) or git fails or takes longer than UPDATE_GIT_TIMEOUT; update_notice's caller drops any exception."""
    def read(name):
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            return json.load(f)
    template = read("install.json")["template"]
    commit, checkout = template["commit"], read("install.local.json")["checkout"]
    if not (isinstance(commit, str) and COMMIT_ID.fullmatch(commit) and isinstance(checkout, str) and checkout):
        return None
    checkout = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(folder))), checkout))
    if not os.path.isdir(checkout):
        return None
    out = subprocess.run(["git", "-C", checkout, "rev-list", "--left-right", "--count", commit + "...HEAD", "--"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=UPDATE_GIT_TIMEOUT)
    behind, ahead = (out.stdout.split() + ["", ""])[:2] if out.returncode == 0 else ("", "")
    if behind != "0" or not ahead.isdigit() or int(ahead) == 0:  # only commits the installed one lacks, none the other way
        return None
    describe = template.get("describe")
    describe = describe if isinstance(describe, str) and DESCRIBE.fullmatch(describe) else commit[:7]
    return ("TAGTEAM: tagteam update available (checkout is %s commit%s ahead of installed %s); "
            "orchestrator: load skill tagteam-update." % (ahead, "" if ahead == "1" else "s", describe))


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
    create_record(path, "worker" if has_marker(payload.get("prompt") or "") else "orchestrator", host)


def on_pre_tool(payload, host, worker=None, why=None):
    """Apply the rules; `worker` overrides the recorded role (agy decides it itself). `why` says that the
    role is unresolved and is added to the reasons of the worker rules."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    shell_tools = SHELL_TOOLS | AGY_SHELL_TOOLS if host == "agy" else SHELL_TOOLS
    delegate_tools = DELEGATE_TOOLS | AGY_DELEGATE_TOOLS if host == "agy" else DELEGATE_TOOLS
    if worker is None:
        role = role_of(payload)
        worker = role in ("worker", UNRESOLVED)
        if role == UNRESOLVED:
            try:
                path = session_file(payload)
                why = unresolved_why(load_record(path)[1], path)
            except Exception:
                why = "the role record cannot be read"

    def block(reason):
        deny(reason + (" (role unresolved: %s)" % why if why else ""))

    # [role] A worker session must not open further sessions. The orchestrator must pick
    # model and effort explicitly (fork cannot take a model, so only fresh sessions pass),
    # unless it starts a stored proposal by proposal_id alone (no model/effort/prompt).
    if tool == START_SESSION:
        if worker:
            block("worker sessions may not open sessions")
        shaping = any(tool_input.get(k) for k in ("model", "effort", "prompt"))
        proposal_only = bool(tool_input.get("proposal_id")) and not shaping
        if not proposal_only and not (tool_input.get("model") and tool_input.get("effort")):
            deny("start_session needs an explicit model and effort")

    # [role] Workers may not commit or push; the orchestrator integrates.
    if tool in shell_tools and worker and GIT_WRITE.search(shell_command(tool_input)):
        block("workers may not run git commit or git push")

    # [role] Workers may not delegate again (subagents lose the Agent tool by definition).
    if tool in delegate_tools and worker:
        block("worker sessions may not spawn subagents")

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
        hit = control_plane_path(edit_paths(tool, tool_input, host), payload)
        if hit:
            block("workers may not edit the control plane (%s)" % hit)


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


def edit_paths(tool, tool_input, host=None):
    """The paths an edit tool call writes. The Claude and Codex tool names count on every host; the agy names
    (write_file, edit_file) only on host agy."""
    keys = dict(EDIT_TOOLS, **AGY_EDIT_TOOLS) if host == "agy" else EDIT_TOOLS
    if tool in keys:
        path = tool_input.get(keys[tool])
        return [path] if isinstance(path, str) and path else []
    if tool in PATCH_TOOLS:
        text = tool_input.get("command", tool_input.get("patch", tool_input.get("input", "")))
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        return PATCH_PATH.findall(str(text or ""))
    return []


def plain_windows_path(path):
    r"""A Windows extended-length or device path without its prefix: \\?\D:\x and \\.\D:\x -> D:\x,
    \\?\UNC\srv\share\x -> \\srv\share\x. Any other path comes back unchanged. relpath takes the prefixed
    and the plain spelling of one folder for two drives, and realpath keeps a prefix it was given.
    The prefix also hides a name that ends in a dot or a space, which Windows would drop from a plain path
    (\\?\D:\repo\AGENTS.md. is a different file); after stripping, such a path is judged as AGENTS.md. That
    is a wider judgment by design: nobody names files like that in normal work."""
    p = path.replace("/", "\\")
    if p[:4] in ("\\\\?\\", "\\\\.\\"):
        rest = p[4:]
        if rest[:4].upper() == "UNC\\":
            return "\\\\" + rest[4:]
        if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
            return rest
    return path


def without_streams(path):
    """A Windows path without NTFS stream suffixes: name:stream, name:stream:$TYPE and name::$DATA are all
    the file `name` (and `.claude::$INDEX_ALLOCATION` is the folder `.claude`). Only the part after the drive
    or share is cut, so the colon of `D:` is not taken for a stream."""
    drive, tail = ntpath.splitdrive(path)
    return drive + "\\".join(part.split(":", 1)[0] for part in tail.split("\\"))


def spellings(path):
    """The ways one path can be written: normalised, with links, junctions and short names resolved
    (realpath does not need the file to exist: it resolves the part that does), and on Windows also without
    an extended-length or device prefix (plain_windows_path) and without stream suffixes (without_streams).
    A path the system cannot resolve keeps its normalised form only, so a strange path is still judged."""
    forms = {os.path.normpath(path)}
    try:
        forms.add(os.path.normpath(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    if os.name == "nt":  # on other systems such names are ordinary file names
        for form in list(forms):
            other = without_streams(plain_windows_path(form))
            if other != form:
                forms |= spellings(other)
    return forms


def control_plane_path(paths, payload):
    """The first path inside the control plane of the project root (git top of cwd, or cwd) or the install root.

    Every path and every root is compared in both spellings (spellings()): a project opened through a link
    (macOS /var -> /private/var) has a git top and a payload cwd that name one folder differently. Any pair
    that lands in the control plane counts, so this only ever finds more than a plain comparison."""
    if not paths:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    roots = spellings(state._git(cwd, "rev-parse", "--show-toplevel") or cwd) | spellings(INSTALL_ROOT)
    for path in paths:
        for full in spellings(path if os.path.isabs(path) else os.path.join(cwd, path)):
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


# ---- agy ---------------------------------------------------------------------------------
# agy sends its own payload shape and wants one JSON object on stdout for every event (exit 0).
# The payload is converted to the internal shape so the rules above stay in one place.

AGY_EMPTY_INPUT = "empty-input"  # record basis: the first model call carried no user input
AGY_MODEL_LOG = "agy-models.jsonl"


_quiet = None


def quiet_stderr():
    """stderr cannot be used (closed pipe): swap in the null device, so that neither this process nor the
    interpreter's flush at exit fails on it (that would end the process with exit code 120)."""
    global _quiet
    try:
        _quiet = open(os.devnull, "w")
        sys.stderr = _quiet
    except Exception:
        pass


def note(line):
    """A diagnostic line on stderr, best effort: a closed or broken stderr must not change the decision."""
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        quiet_stderr()


def settle_stderr():
    try:
        sys.stderr.flush()
    except Exception:
        quiet_stderr()


def warn(message):
    note("tagteam: role_guard warning (allowed): %s" % message)


def agy_internal(p):
    """agy payload -> the shape the rules read (session_id, cwd, prompt, tool_name, tool_input, model, invocation)."""
    call = p.get("toolCall") if isinstance(p.get("toolCall"), dict) else {}
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    tool_input = dict(args)
    if "CommandLine" in args:
        tool_input["command"] = args["CommandLine"]
    spaces = p.get("workspacePaths")
    cwd = p.get("cwd") or (spaces[0] if isinstance(spaces, list) and spaces else None)
    prompt = p.get("lastUserInput")
    number = p.get("invocationNum")
    return {"session_id": p.get("conversationId"), "cwd": cwd if isinstance(cwd, str) else None,
            "prompt": prompt if isinstance(prompt, str) else "", "tool_name": call.get("name") or "",
            "tool_input": tool_input, "model": p.get("modelName"),
            "invocation": number if isinstance(number, int) and not isinstance(number, bool) else None}


def agy_role(payload, first_call_lost=False):
    """(role, basis, unsure): the role of this conversation, recorded when none is recorded yet.

    A recorded role is final. Otherwise the first model call or tool call decides: no user input at all is a
    worker (an agy subagent starts with an empty input), else a marker line makes a worker. The record is
    published atomically; a hook that loses the race takes the winner's role. The launcher variable makes a
    worker whatever the record says (the record is still written, as on the other hosts).

    `first_call_lost` says that this is not the conversation's first model call although nothing is
    recorded: the first decision (maybe a worker) was lost, so an ordinary input must not become an
    orchestrator now. The record then says "unresolved"; a worker input is still recorded as a worker.

    `unsure` is None when the role rests on a record this call read or wrote. Otherwise it says why the role
    is unresolved (no conversation id, unusable state folder, empty or corrupt record, lost first decision,
    record not writable): the caller gives protected calls a worker's rules."""
    launched = os.environ.get("HARNESS_DELEGATE_RUN") == "1"
    path, status, unsure, rec = None, "none", None, {}
    try:
        path = session_file(payload)
        if path:
            rec, status = load_record(path)
        else:
            unsure = "the hook payload has no conversation id"
    except Exception as exc:  # e.g. an unusable state folder
        warn("%s: %s" % (type(exc).__name__, exc))
        path, unsure = None, "the state folder cannot be used (%s); check it" % exc
    if status == "ok":
        return ("worker", None, None) if launched else (rec["role"], rec.get("basis"), None)
    if status in ("bad", "unresolved"):  # never overwritten: it may hold the first role
        unsure = unresolved_why(status, path)
    prompt = payload.get("prompt") or ""
    if launched:  # the launcher variable comes first, so that is what gets recorded
        role, extra = "worker", {}
    elif not prompt.strip():
        role, extra = "worker", {"basis": AGY_EMPTY_INPUT}
    elif has_marker(prompt):
        role, extra = "worker", {}
    elif first_call_lost and status == "none":
        role, extra = UNRESOLVED, {}
        unsure = unresolved_why("unresolved", path) if path else unsure
    else:
        role, extra = "orchestrator", {}
    if status == "none" and path:
        try:
            created = create_record(path, role, "agy", **extra)
        except (state.StateError, OSError) as exc:
            warn("%s" % exc)
            unsure = "the role record could not be written (%s: %s); check the state folder" % (type(exc).__name__, exc)
        else:
            if not created:  # another hook recorded first: its role stands
                rec, status = load_record(path)
                if status == "ok":
                    return ("worker", None, None) if launched else (rec["role"], rec.get("basis"), None)
                unsure = unresolved_why("bad" if status == "none" else status, path)
    return role, extra.get("basis"), (None if launched else unsure)


def agy_log_model(payload):
    """Append {ts, cid, model} to agy-models.jsonl when this conversation's model differs from its last record."""
    cid, model = payload.get("session_id"), payload.get("model")
    if not (isinstance(cid, str) and cid and isinstance(model, str) and model):
        return
    folder = state.state_dir(payload.get("cwd"))
    path = os.path.join(folder, AGY_MODEL_LOG)
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("cid") == cid:
                    last = rec.get("model")
    except OSError:
        pass
    if last == model:
        return
    state.ensure_writable(folder)
    line = json.dumps({"ts": int(time.time()), "cid": cid, "model": model}) + "\n"
    with open(path, "ab+") as f:  # a write cut short earlier leaves no newline: end that line first
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode("utf-8"))


def agy_session_start(payload):
    lines = session_start_lines(payload, "agy")  # the role is decided by the first model call, not here
    if not lines:
        return {}
    return {"injectSteps": [{"systemMessage": {"systemMessage": "\n".join(lines)}}]}


def agy_pre_invocation(payload):
    number = payload.get("invocation")
    agy_role(payload, first_call_lost=(number is not None and number != 0))
    try:
        agy_log_model(payload)
    except Exception as exc:  # the log is for people; it must not break the role record
        warn("%s: %s" % (type(exc).__name__, exc))
    return {}


def agy_pre_tool(payload):
    role, basis, unsure = agy_role(payload)
    try:
        # unsure of the role: protected calls get a worker's rules, everything else is left to the user's rules
        on_pre_tool(payload, "agy", worker=(role == "worker" or unsure is not None),
                    why=" ".join(unsure.split()) if unsure else None)
    except Denied as d:
        reason = "tagteam: " + str(d)
        if basis == AGY_EMPTY_INPUT and not unsure:
            reason += " (role recorded as worker: the first model call had no user input)"
        note(reason)
        return {"decision": "deny", "reason": reason}
    return {"decision": "ask"}  # no opinion: the user's permission rules decide


AGY_HANDLERS = {"SessionStart": agy_session_start, "PreInvocation": agy_pre_invocation, "PreToolUse": agy_pre_tool}
_agy_printed = False


def agy_fallback(event):
    return {"decision": "ask"} if event == "PreToolUse" else {}


def agy_print(out):
    global _agy_printed
    sys.stdout.write(json.dumps(out) + "\n")
    _agy_printed = True
    sys.stdout.flush()


def main_agy(event):
    """Every path prints one JSON object and returns 0: agy blocks a PreToolUse hook that prints nothing."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")  # not the console code page
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not an object")
        handler = AGY_HANDLERS.get(event)
        if handler is None:
            note("tagteam: unknown event %r (ignored)" % event)
            out = {}
        else:
            out = handler(agy_internal(payload))
    except Exception as exc:  # ask hands the call to the user's permission rules; silence would block it
        warn("%s: %s" % (type(exc).__name__, exc))
        out = agy_fallback(event)
    agy_print(out)
    settle_stderr()
    return 0


def parse_argv(argv):
    """(host, event, rest) from the command line. Never raises: main() needs both before anything can fail."""
    host = "claude"
    rest = list(argv[1:])
    if "--host" in rest:
        i = rest.index("--host")
        if i + 1 < len(rest):
            host = rest[i + 1]
        del rest[i:i + 2]
    return host, (rest[0] if rest else ""), rest


def main(argv):
    host, event, _ = parse_argv(argv)
    if host == "agy":
        return main_agy(event)
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
    _host, _event, _ = parse_argv(sys.argv)  # before anything that can fail
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # fail open: a hook bug must not block all work
        warn("%s: %s" % (type(exc).__name__, exc))
        if _host == "agy" and not _agy_printed:
            try:
                agy_print(agy_fallback(_event))
            except Exception:
                pass
        if _host == "agy":
            settle_stderr()
        sys.exit(0)
