# Windows

Windows 11 is the platform the harness was developed and measured on. This page lists the problems seen there and what the
harness does about each. "Seen" means observed on a real install; items marked "unverified" are inferences. Commands are for
PowerShell or `cmd` unless a Git Bash line says otherwise.

Sections: Python (store alias, hook shell, console encoding) · Git Bash (argument conversion) · Codex (sandbox, `.git`,
temporary folders, messaging, hook trust) · agy · Credentials and push · Scheduled model check · Worktrees · Line endings.

## Python

- **Store alias.** `python` or `python3` can resolve to a stub under `WindowsApps` that opens the Microsoft Store instead of
  running. Install Python 3.9 or newer from python.org, or turn off the alias under Settings, Apps, Advanced app settings,
  App execution aliases. The Claude hook commands survive a stub (they pick an interpreter by running it); the Codex
  entries do not, and `install.py --check` reports a Python that is not found.
- **Hook shell and command name.** Claude Code 2.1.327 runs hook commands in **Git Bash** on Windows (measured: the shell
  reports `BASH_VERSION`, `bash` is `/usr/bin/bash`), so the commands in `.claude/settings.json` are `sh` code that tries
  `python3`, then `python`. A real worker's `git commit` was denied and `git status` allowed through them. The Codex
  `commandWindows` entries call `python`. `--check` runs the Claude commands in that shell.
- **Console encoding.** A console in a legacy code page (cp949 in the tested setup) cannot print some characters.
  `run.py` and `models_collect.py` set their output to UTF-8 themselves; `route.py` and `role_guard.py` print ASCII. For
  scripts you write, set `PYTHONUTF8=1` or call `reconfigure(encoding="utf-8")` on the output.

## Git Bash

- **Argument conversion.** Git Bash rewrites arguments that start with `/` as paths, so `claude -p /model` or `schtasks
  /Query` receives a mangled argument. Prefix the command with `MSYS_NO_PATHCONV=1` (for example `MSYS_NO_PATHCONV=1
  schtasks /Query /TN <task name>`). The scripts in this harness call the CLIs directly from Python and are not affected.

## Codex

- **`windows.sandbox`.** With `elevated`, every command of `codex exec` failed with `CreateProcessWithLogonW failed:
  1385` (0.169.0); `unelevated` works. `install.py --check --host codex` warns about `elevated` in your Codex config (your
  setting, tagteam does not change it); interactive Codex with `elevated` is unconfirmed. `run.py` passes
  `-c windows.sandbox=unelevated` itself (`codex.windows_sandbox` in `models.local.json`; `null` passes nothing); add it
  yourself when you call `codex` by hand.
- **`.git` is read-only in `workspace-write`.** `git add`, `git commit`, branches and worktrees fail with `.git/index.lock:
  Permission denied`, and the nested `claude`/`codex` that `run.py` starts do not start. The orchestrator therefore asks for
  escalation for each ([manual section 3.3](../manual.md#33-codex-as-the-orchestrator)). Workers do not commit anyway.
- **Temporary folders.** Inside the sandbox `install.py --check` and the unit tests failed with `WinError 5` when they made a
  temporary folder; run them with escalation.
- **Session messaging.** `codex queue` failed to start its embedded app server inside the sandbox (0.169.0). Use
  `.codex/agents` and `run.py`; see [hosts.md](../../harness/skills/cycle/hosts.md#codex-notes).
- **Hook trust.** Codex skips new or changed hook entries silently until trusted in `/hooks`, and asks again after a moved
  project or a new worktree path; see [manual section 1.2](../manual.md#12-moving-a-project).

## agy (Google)

Measured with `agy` 1.5.9 in headless `-p` runs. How `run.py` uses these facts (modes, `--run-commands`, `--allow-command`,
git check): [manual section 7](../manual.md#7-the-launcher).

- **Commands are denied by default**, and `--mode plan` never runs commands even when allowed. `--settings '<json>'`
  applies for that run only; `run.py` passes `command(*)` or one `command(<cmd>)` per `--allow-command`, with `--mode
  accept-edits`.
- **Exact match on Windows.** In PowerShell and `cmd` an allow rule must equal the command exactly; wildcards such as
  `command(git *)` do not match there. Only `command(*)` allows everything. Give `--allow-command` the full command as
  the worker will write it.

## Credentials and push

- **Wrong account prompt.** Git Credential Manager may show an account chooser on every push when several accounts
  exist. Pin the account per repository (standard git configuration, not retested on a clean setup):

      git config credential.https://github.com.username <account name>

- **A stalled push** once waited over 30 minutes for hidden interactive credentials; check for that before blaming the network.

## Scheduled model check

- `install.py <project> --schedule` registers a Task Scheduler task named `tagteam-models-<8 hex digits>`, weekly on
  Monday at 09:00, with "run as soon as possible after a missed start" and "only when the network is available". Check it
  with `schtasks /Query /TN <name>`; remove it with `--unschedule`.
- If registration through the XML file fails, the installer falls back to a plain weekly task and says that missed runs
  will not be re-run.
- The task reads **user-level** environment variables (confirmed), so `setx TAGTEAM_AA_API_KEY <key>` is enough; open a
  new terminal afterwards because `setx` does not change the current one.
- A `python.exe` console window appears briefly when the task runs (harmless; hiding it is untested). The task runs the
  Python that was current at `--schedule` time (its absolute path is stored): run `--schedule` again after upgrading or
  moving Python.

## Worktrees

A git worktree starts with submodules uninitialized ([manual section 3.1](../manual.md#31-the-session-pool-and-automatic-turns)).
The Claude Code desktop app also asks you to trust the repository once before it creates worktree sessions.

## Line endings

The repository sets `eol=lf` in `.gitattributes` for all files; a Git setting that rewrites to CRLF can break shell scripts
on other platforms.
