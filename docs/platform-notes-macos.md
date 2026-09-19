# Platform notes — macOS (zsh)

Read this when working on macOS. It stays outside `.claude/rules/` so
other platforms do not need to load macOS operational details.

Windows-side deltas live in docs/harness-manual.md (installation
preflight, the codex sandbox/Store-alias failure mode, and the direct
`codex exec` recipe appendix).

## macOS (zsh)

### Codex hooks: Python command discovery

macOS may provide `python3` without an unversioned `python` command. A hook
configured as `python .claude/hooks/...` then fails with command-not-found
(exit 127), even if the same configuration works on Windows.

During installation, run from the target project root:

```sh
bash .claude/scripts/gen-codex-hooks.sh --force
```

The generator verifies Python 3 (`python3`, then `python`) and writes absolute
interpreter and script paths. This also avoids relying on the desktop app's
PATH. Review and trust the generated definitions with `/hooks`. Regenerate and
retrust after moving the project or changing the interpreter installation path.
This generator updates Codex hooks only; Claude settings and other scripts
that invoke `python` still require that command or their own configuration.

### 1. Filename normalization (NFC/NFD) — the biggest trap

macOS filesystems return filenames in **NFD**. Windows has no equivalent
issue. Hardcoding Korean/accented literals in source silently returns zero
matches.

Incident (recorded case, kept for persuasive value — the rule stands on
its own): the same three-syllable Hangul string against a SQLite `LIKE` search returned
**0 rows as NFC and 966 rows as NFD** when stored paths were NFD.

**Rule**: store the filesystem's original string as-is; **normalize to NFC
only at comparison/decision time**. When a path value is embedded in an
outbound field (e.g. a description field), state explicitly which form it
carries and require the reader to normalize to NFC before comparing.
Violating this produces phantom entries.

### 2. Background execution

- **No `setsid`.** Use `nohup <cmd> > log 2>&1 &` + `disown` only.
- Redirecting Python's stdout to a file gets **block-buffered**. Set
  `PYTHONUNBUFFERED=1` if you need to tail progress logs.
- **Watching completion with `pgrep -f "<module-name>"` matches the
  watcher shell's own command line and never resolves.** Use PID-based
  checks (`kill -0 <pid>`) instead.

### 3. Scheduling: launchd, not cron

- Put the plist under `~/Library/LaunchAgents/`, register with
  `launchctl bootstrap gui/$(id -u) <plist>`, unregister with
  `launchctl bootout gui/$(id -u)/<label>`.
- Validate syntax with `plutil -lint <plist>`.
- `StartCalendarInterval` uses the **system's local time**.
- **launchd does not read shell profiles, so there is no `PATH`.**
  Reference `/bin/bash` and any interpreter by **absolute (or otherwise
  fully-qualified) path**, never relying on `PATH` resolution. Before
  registering, **run the script under a clean environment with `env -i`**
  to confirm it actually works without inherited PATH.

### 4. Default shell is zsh

Commands written assuming bash can break: `grep -rn "x" src/
--include=*.py` fails with `no matches found` due to zsh glob expansion,
and a trailing `|| echo "none"` fallback may not fire. Quote globs
(`--include='*.py'`), and don't rely solely on `||` fallbacks to detect
failure.

### 5. Locking

- **No `flock(1)` command.** Don't build single-instance locks out of
  shell. Python's `fcntl.flock` works normally and the kernel releases it
  automatically on process exit, so no stale lock is left behind.

### 6. codex sandbox — read-only file access varies by machine

macOS read-only advisory codex can **read repository files directly and
cite `file:line` evidence.** On Windows this capability depends on setup:
older codex + Store-pwsh machines failed every child-process spawn
(`CreateProcessAsUserW failed: 5`), while codex >= 0.150 with a
normally-ACL'd pwsh spawns and reads fine even in read-only.

Review inputs follow `.claude/rules/verification-tiering.md` on every platform:
inline focused evidence or provide bounded file/ranges where reads work. If
access fails, supply the missing evidence or report the limitation; do not
accept a review of unseen material.

### 7. Sandboxing

The harness manual describes `sandbox.*` as out of this template's scope because
it's unsupported on native Windows. **It is supported on macOS/Linux/
WSL2.** Recorded here as an open option on macOS — this file does not
prescribe turning any of it on.

### 8. SMB/NAS mounts

- SMB shares mount under `/Volumes/<share>`. **`df`'s usage figure is for
  the whole volume the share lives on, not the share itself** — using `df`
  for capacity decisions is wrong. Incident (recorded case): `df` reported
  4.9 TiB for a share whose actual capacity was 778.8 GiB.
- Mounting via Finder can connect over **AFP**. Designs that assume SMB
  (including NFC/NFD analysis) need to be remounted over SMB explicitly.
- `os.path.exists`/`isfile`/`isdir` **swallow exceptions**, so they can't
  distinguish a dropped mount from a genuinely missing file. Use
  `os.stat` directly for error handling.
