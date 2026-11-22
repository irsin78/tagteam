# Windows

Windows 11 is the platform the harness is developed on. This page lists the problems there and what the harness does about
each. Items marked "unverified" or "unconfirmed" are not established. Commands are for
PowerShell or `cmd` unless a Git Bash line says otherwise.

Sections: Python (store alias, hook shell, console encoding) · Git Bash (argument conversion) · Codex (sandbox, `.git`,
temporary folders, messaging, hook trust) · agy (modes, allow rules, hooks, waiting) · Credentials and push · Scheduled model
check · Worktrees · Line endings.

## Python

- **Store alias.** `python` or `python3` can resolve to a stub under `WindowsApps` that opens the Microsoft Store instead of
  running. Install Python 3.9 or newer from python.org, or turn off the alias under Settings, Apps, Advanced app settings,
  App execution aliases. The Claude hook commands survive a stub (they pick an interpreter by running it); the Codex
  entries do not, and `install.py --check` reports a Python that is not found.
- **Hook shell and command name.** Claude Code 2.1.327 runs hook commands in **Git Bash** on Windows (the shell
  reports `BASH_VERSION`, `bash` is `/usr/bin/bash`), so the commands in `.claude/settings.json` are `sh` code that tries
  `python3`, then `python`. A worker's `git commit` is denied and `git status` allowed through them. The Codex
  `commandWindows` entries call `python`. `--check` runs the Claude commands in that shell.
- **Console encoding.** A console in a legacy code page (for example cp949) cannot print some characters.
  `run.py` and `models_collect.py` set their output to UTF-8 themselves; `route.py` and `role_guard.py` print ASCII. For
  scripts you write, set `PYTHONUTF8=1` or call `reconfigure(encoding="utf-8")` on the output.

## Git Bash

- **Argument conversion.** Git Bash rewrites arguments that start with `/` as paths, so `claude -p /model` or `schtasks
  /Query` receives a mangled argument. Prefix the command with `MSYS_NO_PATHCONV=1` (for example `MSYS_NO_PATHCONV=1
  schtasks /Query /TN <task name>`). The scripts in this harness call the CLIs directly from Python and are not affected.

## Codex

- **`windows.sandbox`.** `run.py` passes `-c windows.sandbox=unelevated` itself (`codex.windows_sandbox` in
  `models.local.json`; `null` passes nothing); add it yourself when you call `codex` by hand. With `elevated`, every command
  of `codex exec` fails with `CreateProcessWithLogonW failed: 1385` on codex-cli 0.169.0 and works on 0.170.0 (one PC,
  `codex exec` only); `unelevated` works on both. `install.py --check --host codex` warns about `elevated` in your Codex
  config (your setting, tagteam does not change it); interactive Codex with `elevated` is unconfirmed. [host: codex-cli
  0.169.0 and 0.170.0 Windows, codex exec, measured 2026-11-18 / re-test after a Codex update]
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

Headless `-p` runs. [host: agy 1.5.9 and 1.5.10 Windows, measured 2026-11-18 / re-test after an agy update] How `run.py` uses these facts (modes, `--run-commands`, `--allow-command`, git check, worktree hooks):
[manual section 7](../manual.md#7-the-launcher). Hooks, recovery and the orchestrator role: [manual sections
1.3 and 3.4](../manual.md#13-agy-hooks), [hosts.md](../../harness/skills/cycle/hosts.md#agy-notes).

- **Modes.** The default mode (`toolPermission` `request-review`) runs commands such as `git status` and `echo` without any
  allow rule, but those commands cannot write a file (`PermissionError`), whatever rule or trusted folder is added.
  `--mode accept-edits` lets a command write, but only with a matching allow rule; with no rule a headless run auto-denies
  it (`a tool required the "command" permission that headless mode cannot prompt for`). `--mode plan` never runs commands.
  The file-writing tool works in the default mode (with a `write_file(*)` rule in the user's settings). `--mode auto` is
  unconfirmed.
- **`--settings '<json>'` applies for that run only and replaces the allow list**: it does not merge with your own, so your
  `write_file(*)` rule is gone during the run. `run.py` passes `command(*)` or one `command(<cmd>)` per `--allow-command`,
  with `--mode accept-edits`.
- **Exact match on Windows.** In PowerShell and `cmd` an allow rule must equal the command exactly; wildcards such as
  `command(git *)` do not match there. Only `command(*)` allows everything. Give `--allow-command` the full command as
  the worker will write it [host: agy 1.5.9 Windows, measured 2026-11-18 / re-test after an agy update]; newer versions are
  unconfirmed. The same limit means that in an interactive session an "always allow" rule is unlikely to cover the next `git commit` with other arguments
  (unconfirmed).
- **Hooks run through `cmd /c`**, from the folder `.agents`, so `.agents/hooks.json` holds `python` and an absolute, quoted
  guard path. A path with `%` is refused by the installer because `cmd` expands it even inside quotes. Hooks under another name,
  including a user's global ones, run next to the `tagteam` ones. One guard call (Python start plus the git calls for the
  state folder) takes 0.29 to 0.51 seconds against the 10 second hook timeout (Windows 11, `cmd /c`); another machine, with
  a slower disk or antivirus, is unconfirmed. A hook that exits 1 blocks a tool call, while a failing SessionStart or
  PreInvocation hook does not stop the session.
- **Ten second wait.** `run_command` waits about ten seconds and then moves the command to the background; setting
  `WaitMsBeforeAsync` to 60 seconds changes nothing. A child agy started through `run.py` takes about 30 seconds to start its
  session. `run.py --wait` therefore returns within nine seconds, and `--status` checks that the launcher is alive through
  the Windows API, since `os.kill(pid, 0)` is not a liveness check on Windows.
- **Models depend on the account.** Some accounts list only Gemini 3.8 Flash (low, medium, high) and three models of other
  companies, with no Argon, Hydrogen or Orchestrator model. Check `agy models` before choosing the Orchestrator model as the
  orchestrator.

## Credentials and push

- **Wrong account prompt.** Git Credential Manager may show an account chooser on every push when several accounts
  exist. Pin the account per repository (effectiveness on a clean setup: unconfirmed):

      git config credential.https://github.com.username <account name>

- **A stalled push** may be waiting for hidden interactive credentials ; check for that before
  blaming the network.

## Scheduled model check

- There is one task per machine, named `tagteam-models`, not one per project. Install, `--update` and `--schedule` register it
  when it is missing (`--schedule` registers it again): weekly on Monday at 09:00, with "run as soon as possible after a missed
  start" and "only when the network is available", running `<python> %USERPROFILE%\.tagteam\bin\models_collect.py --weekly`
  in `%USERPROFILE%\.tagteam\` (`TAGTEAM_HOME` moves that folder), with an execution limit of one hour. Check it with `schtasks /Query /TN tagteam-models`;
  `--unschedule` on the last registered project removes it, and so does policy `off`. The task of an older install,
  `tagteam-models-<8 hex digits>`, is removed when its project is updated; `--check` counts the ones left
  (`schtasks /Query /FO CSV`).
- If registration through the XML file fails, the installer falls back to a plain weekly task and says that missed runs
  will not be re-run.
- The task reads **user-level** environment variables (confirmed), so `setx TAGTEAM_AA_API_KEY <key>` is enough; open a
  new terminal afterwards because `setx` does not change the current one. The key is never stored by tagteam: the collector
  leaves it out of the environment of the CLIs and of each project's collector, and blanks it in everything it writes. The one
  weekly run fetches the Artificial Analysis data once for all projects.
- A `python.exe` console window appears briefly when the task runs (harmless; hiding it is unconfirmed). The task runs the
  Python that was current at `--schedule` time (its absolute path is stored): run `--schedule` again after upgrading or
  moving Python. The log is `%USERPROFILE%\.tagteam\weekly.log`, written by the collector itself.

## Worktrees

A git worktree starts with submodules uninitialized ([manual section 3.1](../manual.md#31-the-session-pool-and-automatic-turns)).
The Claude Code desktop app also asks you to trust the repository once before it creates worktree sessions.

## Line endings

This repository sets `eol=lf` in `.gitattributes` for all files; a Git setting that rewrites to CRLF can break shell scripts
on other platforms. A project you install into has no such file unless you add one: with `core.autocrlf=true` its clone is
checked out with CRLF while the installer writes LF. The installer does not rewrite a file that differs from its new
content only in line endings (it reports `kept`), so `--update` leaves nothing for git to show. If files already show as
` M` with no change in content, stage just those with `git add -u -- <paths>` (a bare `git add -u` also stages your other
edits to tracked files). `git add` may print `LF will be replaced by CRLF` for installed files; that is the same setting and
harmless.
