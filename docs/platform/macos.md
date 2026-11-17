# macOS

**Not yet verified.** Nothing in this harness has been run on macOS. This page is a
checklist of what to confirm, derived from how the code behaves. Report or fix what
fails; once an item is confirmed, replace its "to verify" with the observed result.

## Install

- [ ] To verify: `python3 install.py <project dir>` completes and prints created files.
      (The installer itself is plain Python; the examples elsewhere write `python`.)
- [ ] To verify: `python3 install.py <project dir> --check` ends with `check: ok`.
      The check runs the hook with `sys.executable`, so it does not prove that the
      hook commands below resolve.

## Hooks

- [ ] To verify: the Claude hook commands in `.claude/settings.json` call `python`.
      If only `python3` exists on the PATH Claude Code uses, the hooks fail to start.
      Either provide a `python` on PATH or change the command to `python3`.
- [ ] To verify: the Codex hook entries use `python3` on non-Windows systems
      (`command`), with the absolute guard path written by the installer.
- [ ] To verify: a worker session is denied `git commit`, and `git status` is allowed
      (the same cases `--check` runs, but through the real hook wiring).

## Launcher

- [ ] To verify: `run.py` for each vendor CLI you use reports `ACTUAL_MODEL ... (match)`
      with a short read-only prompt.
- [ ] To verify: on timeout the process group is killed (`run.py` starts the child in a
      new session and sends `SIGKILL` to the group). Check that no child process
      survives a run with `--timeout 5` against a slow command.
- [ ] To verify: Codex needs no `windows.sandbox` argument here; `run.py` adds it only
      on Windows. Confirm that `-s read-only` works without it.

## Scheduled model check

- [ ] To verify: `python3 install.py <project dir> --schedule --dry-run` prints a
      property list with a Monday 09:00 `StartCalendarInterval` and the collector
      command.
- [ ] To verify: after `--schedule`, the agent is loaded (`launchctl list` shows a
      label starting `net.tagteam.`) and the plist is under `~/Library/LaunchAgents`.
- [ ] To verify: a launchd job does not inherit your shell environment. Check whether
      `TAGTEAM_AA_API_KEY` and the CLIs (`claude`, `codex`, `agy`) are visible to it;
      a `launchctl setenv` entry or an absolute CLI path may be needed.
- [ ] To verify: the machine wakes for the job, or the missed run is simply skipped
      (the plist has no catch-up setting).
- [ ] To verify: `--unschedule` boots the agent out, removes the plist and deletes
      `model-update.json` and `model-update.applied`.

## Claude Code and the state folder

- [ ] To verify: the state folder is created under the git common directory
      (`<repo>/.git/tagteam/`) and shared by worktrees.
- [ ] To verify: the desktop app's session tool is named `mcp__ccd_session__start_session`
      there as well, so the hook matcher applies.
