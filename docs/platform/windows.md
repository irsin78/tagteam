# Windows

Windows 11 is the platform the harness was developed and measured on. This page lists the problems seen there and what the
harness does about each. "Seen" means observed on a real install; items marked "unverified" are inferences. Commands are for
PowerShell or `cmd` unless a Git Bash line says otherwise.

Sections: Python (store alias, command name, console encoding) · Git Bash (argument conversion) · Codex (sandbox, `.git`,
messaging, hook trust) · agy (command permissions) · Credentials and push · Scheduled model check · Worktrees · Line
endings.

## Python

- **Store alias.** `python` can resolve to a stub under `WindowsApps` that opens the Microsoft Store instead of running;
  hooks then fail. `install.py --check` warns when it finds this. Install Python 3.9 or newer from python.org, or turn off
  the alias under Settings, Apps, Advanced app settings, App execution aliases.
- **Command name.** The Claude hooks and the Codex `commandWindows` entries call `python`, not `python3`. The command
  must work in the shell that starts the hook.
- **Console encoding.** A console in a legacy code page (cp949 in the tested setup) cannot print some characters.
  `run.py` and `models_collect.py` set their output to UTF-8 themselves; `route.py` and `role_guard.py` print ASCII. For
  scripts you write, set `PYTHONUTF8=1` or call `reconfigure(encoding="utf-8")` on the output.

## Git Bash

- **Argument conversion.** Git Bash rewrites arguments that start with `/` as paths, so `claude -p /model` or `schtasks
  /Query` receives a mangled argument. Prefix the command with `MSYS_NO_PATHCONV=1` (for example `MSYS_NO_PATHCONV=1
  schtasks /Query /TN <task name>`). The scripts in this harness call the CLIs directly from Python and are not affected.

## Codex

- **Read-only sandbox.** `codex exec -s read-only` failed with `CreateProcessWithLogonW failed: 1385`, so a read-only
  reviewer could not read files. Adding `-c windows.sandbox=unelevated` fixes it. `run.py` adds this argument by itself on
  Windows; add it yourself when you call `codex` by hand.
- **`.git` is read-only in `workspace-write`.** `git add` and `git commit` fail with `.git/index.lock: Permission
  denied`. Workers must not commit anyway, so the harness relies on it: the orchestrator commits from its own session.
- **Session messaging.** `codex queue` failed to start its embedded app server inside the sandbox (tested version
  0.169.0). The harness does not try to relay messages; on a Codex host use `.codex/agents` and `run.py`.
- **Hook trust.** Codex asks for trust again after a moved project or a new worktree path; see [manual section
  1.2](../manual.md#12-moving-a-project).

## agy (Google)

Measured with `agy` 1.5.9 in headless `-p` runs. How `run.py` uses these facts (modes, `--run-commands`, `--allow-command`,
git check): [manual section 7](../manual.md#7-the-launcher).

- **Commands are denied by default.** Every shell command is refused unless allowed.
- **`--settings '<json>'` applies for that run only.** `run.py` passes `{"permissions":{"allow":["command(*)"]}}`, or
  one `command(<cmd>)` per `--allow-command`. With `--mode accept-edits` the allowed commands run.
- **Exact match on Windows.** In PowerShell and `cmd` an allow rule must equal the command exactly; wildcards such as
  `command(git *)` do not match there. Only `command(*)` allows everything. For `--allow-command`, give the full command as
  the worker will write it.
- **`--mode plan` never runs commands**, even when they are allowed. A read-only review that must run tests or `git
  diff` therefore needs `run.py --read-only --run-commands` (`accept-edits`); `run.py` then only checks the work tree
  afterwards, which catches common accidents and is not a security boundary.

## Credentials and push

- **Wrong account prompt.** Git Credential Manager may show an account chooser on every push when several accounts
  exist. Pin the account per repository:

      git config credential.https://github.com.username <account name>

The chooser problem was seen during development; the pinning key is standard git configuration and was not retested on a
clean setup.
- **A stalled push.** A push waited for interactive credentials for more than 30 minutes once. Check for a hidden
  credential prompt before assuming the network is down.

## Scheduled model check

- `install.py <project> --schedule` registers a Task Scheduler task named `tagteam-models-<8 hex digits>`, weekly on
  Monday at 09:00, with "run as soon as possible after a missed start" and "only when the network is available". Check it
  with `schtasks /Query /TN <name>`; remove it with `--unschedule`.
- If registration through the XML file fails, the installer falls back to a plain weekly task and says that missed runs
  will not be re-run.
- The task reads **user-level** environment variables (confirmed), so `setx TAGTEAM_AA_API_KEY <key>` is enough; open a
  new terminal afterwards because `setx` does not change the current one.
- A `python.exe` console window appears briefly when the task runs. It does no harm; running the task under a different
  logon type to hide it is untested.
- The task runs the Python that was current at `--schedule` time (its absolute path is stored). Run `--schedule` again
  after upgrading or moving Python.

## Worktrees

A git worktree starts with submodules uninitialized (a worker's first command: `git submodule update --init <path>`, [manual
section 3.1](../manual.md#31-the-session-pool-and-automatic-turns)). The Claude Code desktop app also asks you to trust the
repository once before it creates worktree sessions.

## Line endings

The repository sets `eol=lf` in `.gitattributes` for all files. Python and Claude Code read LF without trouble; a Git
setting that rewrites to CRLF can make shell scripts fail on other platforms.
