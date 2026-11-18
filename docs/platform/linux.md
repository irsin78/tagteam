# Linux

Measured once, on one machine: a server without the desktop app. Ubuntu 26.04.1 LTS (aarch64), Python 3.14.4, git 2.53.0,
Claude Code 2.1.327, codex-cli 0.170.0, agy 1.5.10, 2026-11-18, in a fresh project (an empty git repository with one
commit). The orchestrating session was Claude Code CLI; Claude workers ran on a small model with only the tools the check needed.
"Observed" below means seen on that machine. What the run could not show is listed at the end as unconfirmed. Behaviour that
belongs to one CLI version carries a `[host: ...]` tag, to be re-tested after an update. Compare [macOS](macos.md).

## Python

- Ubuntu 26.04 has `/usr/bin/python3` and a `/usr/bin/python` link to it (the `python-is-python3` state), so on the system PATH
  `python` resolves too, which it does not on a stock macOS. The case of no Python at all was reproduced by cutting PATH down.
- The login shell's PATH had `~/.local/bin`, where the three CLIs live. Under `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin` only
  `python3` was found, and none of `claude`, `codex`, `agy`. No Python that exists only in the login shell was available to test.

## Install and hooks

- Observed: the test suites passed, `install.py <project dir>` and then `--check` ended with `check: ok` for `--host claude`,
  `codex` and `agy`. That `agy` check was the static one from before the agy hook checks existed; the later parts (`handler`,
  `hook commands`, `live`) were not run on Linux (unconfirmed).
- Observed: the Claude hook commands in `.claude/settings.json` run in `sh` and choose the interpreter by running it.
  With the normal PATH and with a PATH holding only `python3`, `git` and `sh`, a worker's `git commit` was refused (exit 2,
  `workers may not run git commit or git push`). With **no Python** on PATH, PreToolUse exited 2 with `python3/python not
  found on PATH; tool calls are blocked ...` and SessionStart exited 0 with `the role guard is off in this session`.
  `--check` reports the missing interpreter as `FAILED` under `hook commands`. [host: Claude Code 2.1.327 Linux, measured
  2026-11-18 / re-test after a Claude Code update]
- Observed with a real Claude worker: `git status` ran, `git commit --allow-empty` was refused by the hook, and no commit was
  made. With no Python on PATH every tool call, `git status` included, was blocked (fail closed), again with no commit. The
  macOS defect where the old hook let the call through without Python did not occur. Claude Code prints the whole hook command
  string in front of the refusal message, which only makes the worker's output longer. [host: Claude Code 2.1.327 Linux,
  measured 2026-11-18 / re-test after a Claude Code update]
- Observed, static only: `.codex/hooks.json` has the four events, each `python3 "<project>/.claude/tagteam/role_guard.py"
  <Event> --host codex`, and `--check --host codex` is `ok`. `--live` for Codex needs the user's trust of the hooks, so it was
  not part of the run (tried once without trust: `the tagteam hooks did not run; trust them in an interactive codex session`).
- Observed: a Claude Code session exports `CLAUDECODE=1` and other `CLAUDE_*` variables to the CLIs it starts, so an agy or
  Codex worker started from it can be taken for a Claude host. The router checks `ANTIGRAVITY_CONVERSATION_ID` first for that
  reason, and `run.py` removes the foreign host variables from its children.
- agy hook commands run through `sh -c` from the folder `.agents`; a failing PreToolUse hook blocks the tool. [host: agy 1.5.10
  Linux, measured 2026-11-18 / re-test after an agy update] The rest of agy's behaviour is in
  [hosts.md](../../harness/skills/cycle/hosts.md#agy-notes).

## Launcher

- Observed: `run.py` for the three vendors at once, read-only with a short prompt: the Claude model answered in 5 seconds,
  the Codex model in 3, the Gemini model in 8 (`Gemini 3.8 Flash (Low) (match)`), each reporting `ACTUAL_MODEL ... (match)`.
- Observed: on a timeout the whole process group is killed. A fake CLI that started a background `sleep` and then another one,
  run with `--timeout 5`, reported `status=timeout exit=-1 secs=5`, and a second later no `sleep` process was left.
- Observed: Codex runs with `-s read-only` and without the Windows-only `windows.sandbox` argument (it is absent from the
  recorded command line).

## Scheduled model check

- Observed: `--schedule --dry-run` printed the task name `tagteam-models-<8 hex digits>`, the job PATH (the CLI folder
  `~/.local/bin` followed by `/usr/bin:/bin:/usr/sbin:/sbin`) and one `--version` check per CLI that it would run. It showed
  the crontab line only as `[dry-run] crontab -`; the installer has since been changed to print the line itself, as
  `  crontab  <line>` (covered by unit tests, as macOS shows its plist).
- Observed: after `--schedule`, `crontab -l` showed the existing line plus
  `0 9 * * 1 PATH=<job path> /usr/bin/python3 <project>/.claude/tagteam/models_collect.py --project <project> # tagteam-models-<8 hex>`.
  Run through `env -i HOME=... SHELL=/bin/sh sh -c`, which is cron's minimal environment, that command ended `exit=0 claude=ok
  google=ok openai=ok bench=skipped changes=3` in 5 seconds and wrote `model-update.json`: the `PATH=` in the line is what lets
  cron find the CLIs. `--unschedule` removed only that line, deleted `model-update.json`, and the crontab matched the backup.
- Observed: without `crontab` on PATH, `--schedule` printed `error: cannot read the crontab (crontab -l: ... No such file or
  directory: 'crontab'); nothing was changed` and exited 1.
- The measurement also showed that the crontab line sent its output nowhere: cron mails it, and on a server without a mail
  system a failing run (for example `claude=missing`) disappeared, so only a stale `model-update.json` showed that something
  was wrong. The installer has since been changed to end the line with `>> <state folder>/model-update.log 2>&1` and to create
  the folder when it registers the job (covered by unit tests; a run through real cron after the change is unconfirmed).
  `TAGTEAM_AA_API_KEY` is not passed to the job; put it in the crontab if you want benchmark data.
- Unconfirmed: cron does not repeat a missed run (a machine that is off on Monday 09:00 skips that week; the session-start
  line warns after 14 days).

## State folder

- Observed: after `git worktree add`, `route.py` and `run.py` run in the worktree wrote their records under the main checkout's
  `.tagteam/runs/`, and the worktree had no `.tagteam/`. `.tagteam/` was ignored through the project's `.gitignore`.
- Unconfirmed: a project inside a submodule uses the superproject's state folder.

## Not covered

- The session tool of the Claude Code desktop app (`mcp__ccd_session__start_session`), and whether Claude Code on Linux exposes
  it under a name the hook matches: the server has no desktop app. Without it the "model and effort" rule has no hook behind it.
- A Python that only the login shell's PATH provides: none existed on the machine, so the `--check` warning for it was not
  exercised.
- Distributions other than Ubuntu 26.04 and CPUs other than aarch64.
