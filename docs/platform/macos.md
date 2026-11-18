# macOS

**Measured once, on one machine, before two fixes.** macOS 27.2.1 (arm64), Python 3.14.7 from Homebrew, Claude Code
2.1.326, codex-cli 0.169.0, agy 1.5.10, 2026-11-18, installed into a scratch git folder from the harness as it was before
the interpreter and launchd fixes. Those fixes are verified on Windows only: re-check them with the list below.

## Measured

| Item | Result |
|---|---|
| `python3 install.py <dir>` | works (18 files) |
| `--check` | printed `check: ok`, but did not prove the hook commands resolve; it now runs them (below) |
| Claude hook interpreter | **broken on a stock Mac**: macOS has `/usr/bin/python3` and no `python`; the old hook commands called `python`, exited 127, and Claude Code reads that as "no objection". With a `python` on PATH (here from `~/.zprofile`) a worker's `git commit` was denied; with PATH cut to `~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin` it **succeeded without a message** |
| Router `launch.command` | named `python`, same problem; it now uses the Python that runs the router |
| Codex hook entries | `python3` with an absolute guard path, as designed; a real Codex run was not made |
| `run.py` per vendor | `ACTUAL_MODEL ... (match)` for `claude`, `openai` and `google`; Codex needs no `windows.sandbox` argument here |
| Timeout | the process group is killed (`--timeout 5` against a child that sleeps: nothing left after 1 s) |
| `--schedule --dry-run`, `--schedule`, `--unschedule` | plist with Monday 09:00 `StartCalendarInterval` written to `~/Library/LaunchAgents`, loaded as `net.tagteam.<name>`, and booted out and removed again |
| launchd environment | **the collector found no CLI**: the job's PATH is `/usr/bin:/bin:/usr/sbin:/sbin` (`claude=missing google=missing openai=missing`, exit 3), `TAGTEAM_AA_API_KEY` is not passed, and the plist had no output file, so the failure left no trace. The job now gets its own PATH and `model-update.log` |
| State folder, worktrees | measured at the old `<repo>/.git/tagteam/` (shared by worktrees); the folder is now `<repo>/.tagteam/` and not re-measured |

Also seen once: a read-only `run.py` Claude worker in a worktree timed out after 120 s with no output; two reruns took 6 and
16 s. A similar silent timeout appeared on Windows inside the Codex sandbox; not understood.

## To re-check after the fixes

- [ ] The reduced-PATH worker above: `HARNESS_DELEGATE_RUN=1 claude -p ...` with PATH `~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`
      is now denied `git commit` (message `tagteam: workers may not run git commit or git push`).
- [ ] With your normal PATH `git status` is allowed and `git commit` denied, as before.
- [ ] `python3 install.py <dir> --check` on a PATH with no `python3` or `python` ends `FAILED` in `hook commands`.
- [ ] After `--schedule`, `launchctl kickstart -k gui/$(id -u)/net.tagteam.<name>` leaves `model-update.log` with
      `claude=ok google=ok openai=ok`, and `--schedule` printed each CLI's `--version ... ok` with the job PATH.
- [ ] `--unschedule` boots the agent out and removes the plist, `model-update.json`, `model-update.applied` and the log.

## Not measured

- A launchd job woken from sleep, or a missed Monday 09:00 (the plist has no catch-up).
- The desktop app's session tool name (`mcp__ccd_session__start_session`, the hook matcher): the run used the CLI. A real
  Codex session with trusted hooks. (`TAGTEAM_AA_API_KEY` is not passed to launchd on purpose: a plist is a plain file.)
