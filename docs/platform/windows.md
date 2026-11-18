# Windows

Windows 11 is the platform the harness was developed and measured on. This page lists the problems seen there and what the
harness does about each. "Seen" means observed on a real install; items marked "unverified" are inferences. Commands are for
PowerShell or `cmd` unless a Git Bash line says otherwise.

Sections: Python (store alias, hook shell, console encoding) · Git Bash (argument conversion) · Codex (sandbox, `.git`,
temporary folders, messaging, hook trust) · agy (modes, allow rules, hooks, waiting) · Credentials and push · Scheduled model
check · Worktrees · Line endings.

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

Measured with `agy` in headless `-p` runs. [host: agy 1.5.10 Windows, measured 2026-11-18, the exact-match rule with 1.5.9 /
re-test after an agy update] How `run.py` uses these facts (modes, `--run-commands`, `--allow-command`, git check, worktree hooks):
[manual section 7](../manual.md#7-the-launcher). Hooks, recovery and the orchestrator role: [manual sections
1.3 and 3.4](../manual.md#13-agy-hooks), [hosts.md](../../harness/skills/cycle/hosts.md#agy-notes).

- **Modes.** The default mode (`toolPermission` `request-review`) ran commands such as `git status` and `echo` without any
  allow rule, but those commands could not write a file (`PermissionError`), whatever rule or trusted folder was added.
  `--mode accept-edits` lets a command write, but only with a matching allow rule; with no rule a headless run auto-denies
  it (`a tool required the "command" permission that headless mode cannot prompt for`). `--mode plan` never runs commands.
  The file-writing tool worked in the default mode (with a `write_file(*)` rule in the user's settings). `--mode auto` was
  not available to the test account.
- **`--settings '<json>'` applies for that run only and replaces the allow list**: it does not merge with your own, so your
  `write_file(*)` rule was gone during the run. `run.py` passes `command(*)` or one `command(<cmd>)` per `--allow-command`,
  with `--mode accept-edits`.
- **Exact match on Windows.** In PowerShell and `cmd` an allow rule must equal the command exactly; wildcards such as
  `command(git *)` do not match there. Only `command(*)` allows everything. Give `--allow-command` the full command as
  the worker will write it. (Measured with 1.5.9; not measured again with 1.5.10.) The same limit means that in an
  interactive session an "always allow" rule is unlikely to cover the next `git commit` with other arguments
  (inference; unconfirmed).
- **Hooks run through `cmd /c`**, from the folder `.agents`, so `.agents/hooks.json` holds `python` and an absolute, quoted
  guard path. A path with `%` is refused by the installer because `cmd` expands it even inside quotes. Hooks under another name,
  including a user's global ones, run next to the `tagteam` ones (a global hook and a project hook were seen running together).
  One guard call (Python start plus the git calls for the state folder) took 0.29 to 0.51 seconds against the 10 second hook
  timeout (Windows 11, `cmd /c`); another machine, with a slower disk or antivirus, is unmeasured. A hook that exits 1 blocks a tool call, while a failing SessionStart or PreInvocation hook does not
  stop the session.
- **Ten second wait.** `run_command` waits about ten seconds and then moves the command to the background; setting
  `WaitMsBeforeAsync` to 60 seconds changed nothing. A child agy started through `run.py` took about 30 seconds to start its
  session. `run.py --wait` therefore returns within nine seconds, and `--status` checks that the launcher is alive through
  the Windows API, since `os.kill(pid, 0)` is not a liveness check on Windows.
- **Models depend on the account.** On the Windows test account `agy models` listed Gemini 3.8 Flash (low, medium, high) and
  three models of other companies, and no Argon, Hydrogen or Orchestrator model, although another account had them. Check
  `agy models` before choosing the Orchestrator model as the orchestrator.

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
