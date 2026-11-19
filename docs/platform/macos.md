# macOS

[host: macOS 27.2.1 (arm64), Python 3.14.7 from Homebrew (Apple's 3.9.6 also works), Claude Code 2.1.326, codex-cli 0.169.0 and
0.170.0, agy 1.5.10, one machine, measured 2026-11-18 and 2026-11-19 / re-test after a host or macOS update]

## Behaviour

| Item | Result |
|---|---|
| `python3 install.py <dir>` | works |
| `--check` | runs each hook command as written, so a missing interpreter is caught (below) |
| Claude hook interpreter | macOS has `/usr/bin/python3` and no `python`, so the hook commands try `python3`, then `python`. With PATH cut to `~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin` a worker's `git commit` is denied (`tagteam: workers may not run git commit or git push`); with the normal PATH `git status` is allowed and `git commit` denied. On a PATH with neither interpreter `--check` ends `FAILED` in `hook commands` (static: `neither python3 nor python is on PATH`, exit 1) and PreToolUse blocks the tool |
| Router `launch.command` | uses the Python that runs the router |
| Codex hook entries | `python3` with an absolute guard path |
| `run.py` per vendor | `ACTUAL_MODEL ... (match)` for `claude`, `openai` and `google`; Codex needs no `windows.sandbox` argument here |
| Timeout | the process group is killed |
| `--schedule`, `--unschedule` | a plist with Monday 09:00 `StartCalendarInterval` in `~/Library/LaunchAgents`, loaded as `net.tagteam.<name>`; `--unschedule` boots it out and removes the plist, `model-update.json`, `model-update.applied` (when there is one) and the log; the collector's own `collect.log` stays |
| launchd environment | a job's own PATH is only `/usr/bin:/bin:/usr/sbin:/sbin`, so the job gets its own PATH (the CLI folders and the Python's folder) and writes `model-update.log`; `--schedule` prints each CLI's `--version ... ok` with that PATH. `launchctl kickstart -k gui/$(id -u)/net.tagteam.<name>` leaves `claude=ok google=ok openai=ok` in the log. `TAGTEAM_AA_API_KEY` is not passed on purpose: a plist is a plain file |
| State folder, worktrees | `<repo>/.tagteam/`, shared by linked worktrees |

A read-only `run.py` Claude worker in a worktree can time out after 120 s with no output (cause unknown); a similar silent
timeout appears on Windows inside the Codex sandbox.

## Codex on macOS

[host: codex-cli 0.170.0 macOS `codex exec`, project and hooks trusted, measured 2026-11-19 / re-test after a Codex update]

- The hooks run with the `python3` command line: in `read-only` a worker's `git commit --dry-run` is blocked by the
  PreToolUse hook (a real commit: unconfirmed), and the role record (host `codex`) is written to the main work tree's
  `.tagteam/sessions/` in `read-only`, in `workspace-write` and in a linked worktree.
- The Seatbelt `workspace-write` sandbox can write the state folder in the main work tree, but not `.git` (`Operation not
  permitted`), and a command in a linked worktree cannot write the main work tree's `.tagteam/`. So the orchestrator's
  commits need an escalated request, as on Windows; `run.py` inside the sandbox cannot start a nested CLI (`Not logged in`
  for claude, `Operation not permitted` for codex) and, from a worktree, stops with `state folder ... is not writable`.
- `/usr/bin/git` inside the sandbox prints `xcrun: couldn't create cache file ...` warnings on stderr and still exits 0
  with the right result: not a failure. Whether a Homebrew git avoids them is unconfirmed.
- The escalated path (a commit or `run.py` after the user's approval) is unconfirmed on macOS; it should follow the Windows result.

## Unconfirmed

- A launchd job woken from sleep, or a missed Monday 09:00 (the plist has no catch-up).
- The desktop app's session tool name (`mcp__ccd_session__start_session`, the hook matcher).
- An interactive Codex session on macOS (only `codex exec`).
