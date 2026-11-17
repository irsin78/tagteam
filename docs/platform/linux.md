# Linux

**Not yet verified.** Nothing in this harness has been run on Linux. This page is a
checklist of what to confirm, derived from how the code behaves. Once an item is
confirmed, replace its "to verify" with the observed result.

## Install

- [ ] To verify: `python3 install.py <project dir>` and then `--check` end with
      `check: ok`. (The examples elsewhere write `python`.)

## Hooks

- [ ] To verify: the Claude hook commands in `.claude/settings.json` call `python`.
      Many distributions ship only `python3`; install a `python` alias or change the
      command to `python3`.
- [ ] To verify: the Codex hook entries use `python3` (`command`) with the absolute
      guard path written by the installer.
- [ ] To verify: a worker session is denied `git commit` and allowed `git status`
      through the real hook wiring.

## Launcher

- [ ] To verify: `run.py` for each installed vendor CLI reports `ACTUAL_MODEL ...
      (match)` with a short read-only prompt.
- [ ] To verify: on timeout the whole process group is killed (`SIGKILL` to the
      group of a child started in a new session). Run with `--timeout 5` against a
      slow command and check that nothing survives.
- [ ] To verify: Codex `-s read-only` works without the Windows-only
      `windows.sandbox` argument, including inside a container or a restricted
      user namespace.

## Scheduled model check

- [ ] To verify: `python3 install.py <project dir> --schedule --dry-run` prints a
      crontab line `0 9 * * 1 <command> # tagteam-models-<8 hex digits>`.
- [ ] To verify: after `--schedule`, `crontab -l` shows the line and keeps your other
      entries; `--unschedule` removes only that line and deletes `model-update.json`
      and `model-update.applied`.
- [ ] To verify: cron has a minimal environment. Check that `TAGTEAM_AA_API_KEY` and
      the CLIs (`claude`, `codex`, `agy`) are visible to the job; set the variable in
      the crontab or use absolute CLI paths if not.
- [ ] To verify: cron does not catch up missed runs, so a machine that is off on
      Monday 09:00 skips that week; the session-start line warns after 14 days
      without collection.
- [ ] To verify: on systems without cron, `--schedule` fails with a clear message and
      changes nothing.

## State folder

- [ ] To verify: the state folder is created under the git common directory
      (`<repo>/.git/tagteam/`), shared by worktrees, and `.tagteam/` is used only
      outside git.
- [ ] To verify: Claude Code on Linux exposes the session tool under a name matched by
      the hook (`mcp__ccd_session__start_session` is the desktop app's name); if not,
      the "model and effort" rule has no hook behind it there.
