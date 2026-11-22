# Linux

[host: Ubuntu 26.04.1 LTS (aarch64), a server without the desktop app, Python 3.14.4, git 2.53.0, Claude Code 2.1.327,
codex-cli 0.170.0, agy 1.5.10, one machine, measured 2026-11-18 / re-test after an update] Behaviour that belongs to one CLI
version carries its own `[host: ...]` tag. What is unconfirmed is listed at the end.
Compare [macOS](macos.md).

## Python

- Ubuntu 26.04 has `/usr/bin/python3` and a `/usr/bin/python` link to it (the `python-is-python3` state), so on the system PATH
  `python` resolves too, which it does not on a stock macOS.
- The login shell's PATH has `~/.local/bin`, where the three CLIs live. Under `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin` only
  `python3` is found, and none of `claude`, `codex`, `agy`.

## Install and hooks

- `install.py <project dir>` and then `--check` ends with `check: ok` for `--host claude`, `codex` and `agy`. The `handler`,
  `hook commands` and `live` parts of `--check --host agy` are unconfirmed on Linux.
- The Claude hook commands in `.claude/settings.json` run in `sh` and choose the interpreter by running it. With the normal
  PATH and with a PATH holding only `python3`, `git` and `sh`, a worker's `git commit` is refused (exit 2, `workers may not
  run git commit or git push`). With **no Python** on PATH, PreToolUse exits 2 with `python3/python not found on PATH; tool
  calls are blocked ...` and SessionStart exits 0 with `the role guard is off in this session`. `--check` reports the
  missing interpreter as `FAILED` under `hook commands`. [host: Claude Code 2.1.327 Linux, measured 2026-11-18 / re-test
  after a Claude Code update]
- A Claude worker's `git status` runs and `git commit --allow-empty` is refused by the hook, so no commit is made. With
  no Python on PATH every tool call, `git status` included, is blocked (fail closed), again with no commit. Claude Code prints
  the whole hook command string in front of the refusal message, which only makes the worker's output longer. [host: Claude
  Code 2.1.327 Linux, measured 2026-11-18 / re-test after a Claude Code update]
- `.codex/hooks.json` has the four events, each `python3 "<project>/.claude/tagteam/role_guard.py" <Event> --host codex`,
  and `--check --host codex` is `ok`. `--live` for Codex needs the user's trust of the hooks; without it the result is `the
  tagteam hooks did not run; trust them in an interactive codex session`.
- A Claude Code session exports `CLAUDECODE=1` and other `CLAUDE_*` variables to the CLIs it starts, so an agy or Codex worker
  started from it can be taken for a Claude host. The router checks `ANTIGRAVITY_CONVERSATION_ID` first for that reason, and
  `run.py` removes the foreign host variables from its children.
- agy hook commands run through `sh -c` from the folder `.agents`; a failing PreToolUse hook blocks the tool. [host: agy 1.5.10
  Linux, measured 2026-11-18 / re-test after an agy update] The rest of agy's behaviour is in
  [hosts.md](../../harness/skills/cycle/hosts.md#agy-notes).

## Launcher

- `run.py` works for the three vendors, read-only with a short prompt, each reporting `ACTUAL_MODEL ... (match)` (for example
  `Gemini 3.8 Flash (Low) (match)`).
- On a timeout the whole process group is killed: a fake CLI that starts background `sleep` processes, run with
  `--timeout 5`, reports `status=timeout exit=-1 secs=5` and leaves no `sleep` behind.
- Codex runs with `-s read-only` and without the Windows-only `windows.sandbox` argument.

## Scheduled model check

- There is one cron line per machine, ending `# tagteam-models`, not one per project. `--schedule --dry-run` prints the job
  PATH (the CLI folder `~/.local/bin` followed by `/usr/bin:/bin:/usr/sbin:/sbin`), one `--version` check per CLI that it
  would run, and the crontab line as `  crontab  <line>`.
- After `--schedule`, `crontab -l` shows the existing lines plus
  `0 9 * * 1 cd ~/.tagteam && PATH=<job path> /usr/bin/python3 ~/.tagteam/bin/models_collect.py --weekly >> ~/.tagteam/weekly.log 2>&1 # tagteam-models`
  (with the real paths): the `PATH=` is what lets cron find the CLIs. The line runs the collector copy in `~/.tagteam/bin/`
  (`TAGTEAM_HOME` moves the folder); it refreshes the shared cache and writes each registered project's `model-update.json`.
  `--unschedule` on the last registered project (or policy `off`) removes only that line and `~/.tagteam/bin/`; a project's own
  older line, `# tagteam-models-<8 hex>`, is removed when that project is updated or unscheduled, and `--check` counts the ones left.
- Cron mails a job's output, and a server without a mail system would lose it, so the line redirects to
  `~/.tagteam/weekly.log`, and the folder is created when the job is registered. `TAGTEAM_AA_API_KEY` is not passed to the job;
  put it in the crontab if you want benchmark data, or run `models_collect.py --refresh` with the key by hand: every project
  reads the Artificial Analysis cache that run fills.
- Without `crontab` on PATH, `--schedule` prints `error: cannot read the crontab (crontab -l: ... No such file or directory:
  'crontab'); nothing was changed` and exits 1.
- Unconfirmed: a run of the machine line through real cron (the earlier per-project line was run with cron's minimal
  environment, `env -i HOME=... SHELL=/bin/sh sh -c`, and worked); cron does not repeat a missed run (a machine that is off on Monday 09:00 skips that week; the session-start
  line warns after 14 days).

## State folder

- After `git worktree add`, `route.py` and `run.py` run in the worktree write their records under the main checkout's
  `.tagteam/runs/`, and the worktree has no `.tagteam/`. `.tagteam/` is ignored through the project's `.gitignore`.
- Unconfirmed: a project inside a submodule uses the superproject's state folder.

## Not covered

- The session tool of the Claude Code desktop app (`mcp__ccd_session__start_session`), and whether Claude Code on Linux exposes
  it under a name the hook matches: the server has no desktop app. Without it the "model and effort" rule has no hook behind it.
- A Python that only the login shell's PATH provides, and the `--check` warning for it.
- Distributions other than Ubuntu 26.04 and CPUs other than aarch64.
