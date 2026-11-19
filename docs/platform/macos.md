# macOS

**Measured once, on one machine, before two fixes.** macOS 27.2.1 (arm64), Python 3.14.7 from Homebrew, Claude Code
2.1.326, codex-cli 0.169.0, agy 1.5.10, 2026-11-18, installed into a scratch git folder from the harness as it was before
the interpreter and launchd fixes. The fixes were re-checked on the same Mac later that day (the list below, all five
passed; Apple's Python 3.9.6 on the reduced PATH compiles the installed scripts and judges the same way) and a real Codex run
was made on 2026-11-19 (codex-cli 0.170.0, section "Codex on macOS").

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

## Re-checked after the fixes (2026-11-18, same Mac)

- [x] The reduced-PATH worker above: `HARNESS_DELEGATE_RUN=1 claude -p ...` with PATH `~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`
      is now denied `git commit` (message `tagteam: workers may not run git commit or git push`).
- [x] With your normal PATH `git status` is allowed and `git commit` denied, as before.
- [x] `python3 install.py <dir> --check` on a PATH with no `python3` or `python` ends `FAILED` in `hook commands`
      (static: `neither python3 nor python is on PATH`, exit 1).
- [x] After `--schedule`, `launchctl kickstart -k gui/$(id -u)/net.tagteam.<name>` leaves `model-update.log` with
      `claude=ok google=ok openai=ok`, and `--schedule` printed each CLI's `--version ... ok` with the job PATH.
- [x] `--unschedule` boots the agent out and removes the plist, `model-update.json` and the log (`model-update.applied`
      when there is one). The collector's own `collect.log` stays.

## Codex on macOS

Measured on 2026-11-19 with codex-cli 0.170.0 after the project and its hooks were trusted in interactive Codex, using
`codex exec` (not an interactive session).

- The hooks run with the `python3` command line: in `read-only` a worker's `git commit --dry-run` was blocked by the
  PreToolUse hook (a real commit was not tried), and the role record (host `codex`) was written to the main work tree's
  `.tagteam/sessions/` in `read-only`, in `workspace-write` and in a linked worktree.
- The Seatbelt `workspace-write` sandbox can write the state folder in the main work tree, but not `.git` (`Operation not
  permitted`), and a command in a linked worktree cannot write the main work tree's `.tagteam/`. So the orchestrator's
  commits need an escalated request, as on Windows; `run.py` inside the sandbox cannot start a nested CLI (`Not logged in`
  for claude, `Operation not permitted` for codex) and, from a worktree, stops with `state folder ... is not writable`.
- `/usr/bin/git` inside the sandbox prints `xcrun: couldn't create cache file ...` warnings on stderr and still exits 0
  with the right result: not a failure. Whether a Homebrew git avoids them is unconfirmed.
- The escalated path (a commit or `run.py` after the user's approval) was not measured on macOS; it follows the Windows
  result.

## Not measured

- A launchd job woken from sleep, or a missed Monday 09:00 (the plist has no catch-up).
- The desktop app's session tool name (`mcp__ccd_session__start_session`, the hook matcher): the run used the CLI.
  (`TAGTEAM_AA_API_KEY` is not passed to launchd on purpose: a plist is a plain file.)
- An interactive Codex session on macOS (only `codex exec` was run).
