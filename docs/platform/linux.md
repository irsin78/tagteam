# Linux

**Not yet verified.** Nothing in this harness has been run on Linux. This page is a checklist of what to confirm, derived
from how the code behaves and from what [macOS](macos.md) showed. Once an item is confirmed, replace its "to verify" with
the observed result.

## Install and hooks

- [ ] To verify: `python3 install.py <project dir>` and then `--check` end with `check: ok`.
- [ ] To verify: the Claude hook commands in `.claude/settings.json` run in `sh`, try `python3` and then `python`, and
      work with only one of them present. Many distributions ship only `python3`; on a machine with neither, PreToolUse
      must block and say so (`--check` reports `FAILED` under `hook commands`).
- [ ] To verify: the Codex hook entries use `python3` (`command`) with the absolute guard path written by the installer.
- [ ] To verify: a worker session is denied `git commit` and allowed `git status` through the real hook wiring.
- [ ] To verify: a Python found only through your login shell's PATH (not `/usr/bin:/bin:/usr/sbin:/sbin`) is also found
      when Claude Code starts from the desktop; `--check` warns about this case.

## Launcher

- [ ] To verify: `run.py` for each installed vendor CLI reports `ACTUAL_MODEL ... (match)` with a short read-only prompt.
- [ ] To verify: on timeout the whole process group is killed (`SIGKILL` to the group of a child started in a new
      session): run with `--timeout 5` against a slow command and check that nothing survives.
- [ ] To verify: Codex `-s read-only` works without the Windows-only `windows.sandbox` argument (also in a container).

## Scheduled model check

- [ ] To verify: `python3 install.py <project dir> --schedule --dry-run` prints a crontab line
      `0 9 * * 1 PATH=<job path> <command> # tagteam-models-<8 hex digits>` and the `--version` checks it would run.
- [ ] To verify: after `--schedule`, `crontab -l` shows the line and keeps your other entries; `--unschedule` removes only
      that line and deletes `model-update.json`, `model-update.applied` and `model-update.log`.
- [ ] To verify: cron's minimal environment is covered by that `PATH=`: the CLIs (`claude`, `codex`, `agy`) start with it
      (`--schedule` runs `--version` for each). On macOS the same problem was real (launchd). `TAGTEAM_AA_API_KEY` is not
      passed; set it in the crontab if you want benchmark data.
- [ ] To verify: cron does not catch up missed runs (a machine that is off on Monday 09:00 skips that week; the
      session-start line warns after 14 days) and the job has no log file. On systems without cron, `--schedule` fails
      with a clear message and changes nothing.

## State folder

- [ ] To verify: the state folder is `<main work tree>/.tagteam/`, shared by worktrees and git-ignored, and a project inside
      a submodule uses the superproject's.
- [ ] To verify: Claude Code on Linux exposes the session tool under a name matched by the hook
      (`mcp__ccd_session__start_session` is the desktop app's name); if not, the "model and effort" rule has no hook behind it.
