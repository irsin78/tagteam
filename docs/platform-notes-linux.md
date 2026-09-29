# Platform notes — Linux (Ubuntu) — PARTLY VERIFIED

Evidence covers the WSL2 isolation lane and one native Ubuntu 24.04 LTS
**aarch64** Oracle Cloud VM (kernel 6.17-oracle, systemd, bash 5.2), checked via
`tools/linux-probe.sh`. "Inferred, unverified" means neither lane measured it.
Keep this header, the manual support table and Linux install section aligned.
Sandboxing failed on the native host (§5).

SessionStart points here on Linux/WSL2; this stays outside `.claude/rules/`.

## Linux (Ubuntu)

### 1. `python` name

Hooks are invoked as `python`; Ubuntu ships only `python3`, so install it with
`sudo apt install python-is-python3`. Otherwise every prohibition is silently
inert, and `check-posix.sh` reports `WARN: python was not found on PATH.`, skips
three hook self-tests and the agy grant check, and exits 1 (both lanes verified).

### 2. Login-shell PATH

The CLIs install into `~/.local/bin`, which the profile adds to PATH. A LOGIN
shell has it; an empty environment does not. Measured natively:
`env -i bash -lc` yields a PATH containing `~/.local/bin`, while
`env -i bash -c` yields only the system directories. So the split is
profile-read vs not, not interactive vs not — systemd units and cron get the
short PATH, and must reference interpreters and `claude` by absolute path.
This mirrors the macOS launchd rule. (observed, native aarch64)

**nvm-managed `node` is on the interactive PATH but NOT on the login PATH**,
because nvm is sourced from `.bashrc`, not
`.profile`. Anything that shells out to `node` from a login shell or a unit
needs an absolute path too. (observed, native aarch64)

### 3. Headless servers

Headless servers (`DISPLAY` empty, no `xdg-open`) need each CLI's URL/device-login
flow. These login flows remain unmeasured on the native host.

### 4. Installing the CLIs

Install Claude Code with `curl -fsSL https://claude.ai/install.sh | bash`, then
run `/login`. (observed, WSL2 lane)

Install Codex through npm (which needs Node), and install Antigravity by its
official install document. (inferred, unverified)

The three CLI binaries at least START on **aarch64** — `claude`, `codex` and
`agy` were all present under the user's `~/.local/bin` on the measured ARM host,
and `codex-cli 0.153.2` and `Claude Code 2.1.260` printed their versions. That
is a launch, not an end-to-end run: no delegation was executed there. (observed,
native aarch64; the install commands themselves were not re-run)

### 5. Sandboxing

On the native host, bubblewrap 0.9.0 failed every unprivileged run:

    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

Without a new network namespace, it failed earlier:

    bwrap: setting up uid map: Permission denied

The host set `kernel.apparmor_restrict_unprivileged_userns` to `1`, blocking
unprivileged USER namespaces and uid mapping; the loopback error was secondary.
Ubuntu documents this as a 24.04 default, but only one host was measured, without
an A/B sysctl check. Generalisation is likely but unproven. Check your host with
`sysctl kernel.apparmor_restrict_unprivileged_userns`.

Consequence: any sandboxing that relies on unprivileged bwrap — the codex
Linux sandbox included — does not work on a stock Ubuntu 24.04 host until an
administrator changes that. Two ways out, both deliberate acts by the machine's
owner, neither done here: relax the sysctl
(`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, persisted in
`/etc/sysctl.d/`), or ship an AppArmor profile that grants `userns create` to
the binary. Relaxing the sysctl weakens a system-wide hardening measure, so it
is a decision, not a fix to apply silently.

Treat Linux codex sandboxing as UNPROVEN until a host is shown to run bwrap
unprivileged. (observed, native aarch64, Ubuntu 24.04 — one host)

The lane required a non-root user (root fails with `apply-seccomp: write
/proc/self/uid_map`), `socat`, and `python-is-python3`. (observed, WSL2 lane)
`socat` is NOT installed by default on a stock Ubuntu server image — install
it before using the isolation lane. (observed, native aarch64)

`lane-sensitive.sh` proves network deny-all and the out-of-workspace write
block before starting, and needs `curl` and `HOME`. Out-of-workspace READS
remain allowed by default; egress deny-all is the actual exfiltration block.
(observed, WSL2 lane)

### 6. WSL2-only

Clones under `/mnt/<drive>` need `git config --global --add safe.directory
<path>`; `check-posix.sh` prints the command. (observed, WSL2 lane)

Fetch back from Windows with `//wsl.localhost/<distro>/home/<user>/<clone>`
using FORWARD slashes. See the manual's WSL2 sections for details.
(observed, WSL2 lane)

### 7. Filenames

Linux filesystems store what is written: an NFC name and its NFD spelling
stay two distinct files. The macOS NFD trap does not apply, but data that
crossed a Mac can still carry NFD; normalize at comparison time.
(observed, native aarch64)

### 8. codex sandbox

Linux read-only codex (landlock/seccomp) can most likely read repository files,
as on macOS. Use the shared review-input contract in verification-tiering.md.
(inferred, unverified)

### 9. Tooling present by default

bash 5, GNU coreutils `timeout`, and `flock` — which macOS lacks — are all
present out of the box, so the launchers' bash >= 4 requirement and the
timeout wrapper work with no setup. (observed, WSL2 lane AND native aarch64:
bash 5.2.21, timeout 9.4, flock present.)

Absent on the measured server image: `socat`, `xdg-open`, and the unversioned
`python`. One image is not every image — check rather than assume. (observed,
native aarch64)

Native verification after `sudo apt install python-is-python3`:
`bash check-posix.sh --launchers` found `python` 3.12.3, passed all three hook
self-tests and reported `ALL PASS / 64 cases`. Codex hook trust still required
the interactive per-clone step (§ security-boundary). Without an agy `write_file`
grant, `agy-run.sh` reports `AGY_UNAVAILABLE`.
