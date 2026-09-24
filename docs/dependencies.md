# Dependencies

This page is the single list of what the harness needs on a machine, what
breaks without each item, and how the installation checks report it. The
[installation manual](harness-manual.md#installation) keeps the procedure
(settings merge, instruction files, trust registration); platform notes keep
operational details. When a requirement changes, update this page first.

Run the platform check after installing anything below:
`bash check-posix.sh` (macOS/Linux) or
`powershell -NoProfile -ExecutionPolicy Bypass -File check-windows-aliases.ps1`
(Windows). Exit 0 is healthy; exit 1 lists the warnings to fix.

## Summary

| Component | Needed when | Used by | Without it | Check |
|---|---|---|---|---|
| Python 3, callable as `python` | Always | All Claude hooks (`.claude/settings.json` invokes `python`), routing, snapshots, launcher helpers | Hooks do not run, so every prohibition (bypass flags, force push, delegate commits, control-plane writes) is **silently inert** | WARN `python was not found on PATH`, skipped hook self-tests |
| bash >= 4 | Any process launcher (`codex-run.sh`, `claude-run.sh`, `agy-run.sh`) | Launchers, `run-state.sh`, stop-gate verifiers | Launchers exit 2 `*_UNAVAILABLE`, so every delegation falls back | WARN `bash on PATH is 3.x` when no standard-location bash >= 4 exists either |
| GNU coreutils `timeout`, first on PATH as `timeout` | Codex or Claude launcher | Bounded foreground and detached runs | Launcher exits 4 `HARNESS_DENIED` before starting | WARN `GNU coreutils 'timeout' is not first on PATH` |
| Git | Git workspaces only | Snapshots, HEAD checks, worktree workers | Non-Git mode is used; worktree-isolated native workers are unavailable | OK/INFO line for `git` |
| Codex CLI (`codex`) | Codex is the host or a delegate | `codex-run.sh`, Codex hooks | Codex routes report `CODEX_UNAVAILABLE`; fallback applies | INFO `codex resolves to ...` |
| Claude Code CLI (`claude`) | Claude is the host or a delegate | `claude-run.sh` | Claude routes unavailable; fallback applies | Route output `available` |
| Antigravity CLI (`agy`) with a `write_file(*)` grant | Only if the agy route is selected | `agy-run.sh` | `AGY_UNAVAILABLE` | INFO about the grant |
| PowerShell 7 (non-Store) | Windows | Codex sandbox shell, Windows check | Codex sandbox cannot spawn processes | WARN `pwsh ...` |
| OpenAI-compatible local endpoint | Only if local reads are declared | `local-run.sh` | Local-read lane unavailable | None; declared in local bindings |

Minimum versions that have been measured are noted in the manual (for example
codex-cli >= 0.144 for the direct `codex exec` recipes). Update a CLI when a
flag or model is rejected, not on every run.

## Python

Every Claude hook is wired as `python <script>`. Codex hooks are generated with
an absolute interpreter path by `bash .claude/scripts/gen-codex-hooks.sh --force`
(see [Codex host](harness-manual.md#codex-host-hook-mirror-and-trust-registration)),
so Codex does not need the unversioned name, but Claude does.

Any Python 3 works for the hooks. Observed: the hook, routing and snapshot
suites pass on macOS system Python 3.9.6 and on Homebrew Python 3.14.

| Platform | Provide `python` |
|---|---|
| Windows | Install from python.org and use `python`/`py`. Never rely on `python3`: it is often a Microsoft Store alias. See [Windows-specific details](harness-manual.md#windows-specific-details) |
| macOS | Homebrew Python ships the unversioned name in `$(brew --prefix)/opt/python@3.X/libexec/bin`; put that directory on PATH, or link `python` to any Python 3 in a directory that is already on PATH |
| Linux | `sudo apt install python-is-python3` (Debian/Ubuntu) |

## bash >= 4 and GNU `timeout`

| Platform | bash | `timeout` |
|---|---|---|
| Windows | Git for Windows (Git Bash). The Stop gate also finds it in standard install paths when PATH does not | Included with Git Bash |
| macOS | `/bin/bash` is 3.2. `brew install bash`; no PATH change needed | `brew install coreutils` |
| Linux | Included | Included |

macOS notes (observed 2026-09-24, Homebrew coreutils 9.12 on arm64):

- Homebrew installs most coreutils with a `g` prefix (`gls`, `gsed`), but links
  `timeout`, which macOS lacks, unprefixed as `$(brew --prefix)/bin/timeout`.
  No `gnubin` PATH entry is needed for the harness. Adding `gnubin` replaces
  `ls`, `sed`, `date` and others with GNU versions for every program, which is
  wider than the harness requires. `check-posix.sh` still suggests the `gnubin`
  entry; either way satisfies its check, which only asks that `timeout` on PATH
  is GNU coreutils.
- PATH order does not matter for the launchers. A desktop app may put `/bin`
  first even when a login shell does not (observed: the Claude desktop app
  starts with the system paths and appends the shell's entries), so bare `bash`
  can be 3.2. When started under bash 3, `codex-run.sh`, `claude-run.sh` and
  `agy-run.sh` replace their own process once with `/opt/homebrew/bin/bash` or
  `/usr/local/bin/bash` (`exec`, about 1 ms; the run itself happens once).
  Without either, they still report `*_UNAVAILABLE`. `check-posix.sh` reports
  which bash the launchers will use.

## Host CLIs

- Codex: `npm install -g @openai/codex`, then `codex login`. Requires Node.js.
  Trust the project hooks with `/hooks` after generating them; the launcher
  refuses to run while any configured hook is untrusted or modified.
- Claude Code: install per the official instructions and log in.
- Antigravity (optional): install `agy`, log in, and grant only `write_file(*)`
  in `~/.gemini/antigravity-cli/settings.json`. See
  [the agy recipe](../.claude/skills/delegate-agy/SKILL.md).

## What is not required

- A Git repository: non-Git workspaces are supported (see
  [Git/non-Git workspaces](harness-manual.md#gitnon-git-workspaces)).
- `jq`, the `flock(1)` command, `setsid`: not used (locks use Python `fcntl`).
- WSL or a sandbox runtime: only for the optional isolation lane.
