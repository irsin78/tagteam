# Platform notes — Linux (Ubuntu) — PARTLY VERIFIED

Originally derived from WSL2 isolation-lane measurements on 2026-09-01/02.
A native measurement now exists: Ubuntu 24.04 LTS on **aarch64** (Oracle
Cloud VM, kernel 6.17-oracle, systemd, bash 5.2), 2026-09-04, from a fresh
clone via `tools/linux-probe.sh`. Items below carry their evidence grade;
"inferred, unverified" now means neither lane has measured it. The harness manual
support table, Linux install section and this header move together. (observed, native aarch64)

One item was REFUTED on that host and is called out in §5.

It deliberately lives outside `.claude/rules/`: those files cost context on
every turn, while the SessionStart hook points here on Linux and WSL2.
(observed, WSL2 lane)

## Linux (Ubuntu)

### 1. `python` name

Hooks are invoked as `python`; Ubuntu ships only `python3`, so install it with
`sudo apt install python-is-python3`. Otherwise every prohibition is silently
inert, and `check-posix.sh` reports WARN. (observed, WSL2 lane AND native
aarch64: a fresh clone gave `WARN: python was not found on PATH.` plus three
skipped hook self-tests, the agy grant unchecked, and exit 1 — the cascade
this note predicts, all at once.)

### 2. Login-shell PATH

The CLIs install into `~/.local/bin`, which the profile adds to PATH. A LOGIN
shell has it; an empty environment does not. Measured natively:
`env -i bash -lc` yields a PATH containing `~/.local/bin`, while
`env -i bash -c` yields only the system directories. So the split is
profile-read vs not, not interactive vs not — systemd units and cron get the
short PATH, and must reference interpreters and `claude` by absolute path.
This mirrors the macOS launchd rule. (observed, native aarch64)

A second trap on top of it: **nvm-managed `node` is on the interactive PATH
but NOT on the login PATH**, because nvm is sourced from `.bashrc`, not
`.profile`. Anything that shells out to `node` from a login shell or a unit
needs an absolute path too. (observed, native aarch64)

### 3. Headless servers

Servers without a browser need each CLI's URL/device-login flow. Confirmed
headless natively: `DISPLAY` empty and no `xdg-open`, so nothing can open a
browser on the user's behalf. The login flows themselves are still unmeasured.
(observed, native aarch64; login flows inferred)

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

**REFUTED, and the cause is pinned.** bubblewrap 0.9.0 was installed, but
every unprivileged run failed. The first symptom was misleading:

    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

That reads like a network-namespace problem. It is not — a run with no new
network namespace fails earlier and more plainly:

    bwrap: setting up uid map: Permission denied

On the measured host `kernel.apparmor_restrict_unprivileged_userns` was set to
`1`, which restricts unprivileged USER namespaces outright — so bwrap cannot map
uids at all and the loopback error was a downstream symptom. Ubuntu documents
that setting as a 24.04 default, but this measurement observed ONE host and did
not A/B the sysctl, so treat "every 24.04 host behaves this way" as likely, not
proven. Check your own host with
`sysctl kernel.apparmor_restrict_unprivileged_userns` before assuming either
way.

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

**The harness itself was then verified there.** After
`sudo apt install python-is-python3`, `bash check-posix.sh --launchers` gave:
`python` resolving and running (3.12.3), all three hook self-tests PASSING —
so the guards are alive on Linux/aarch64, not merely installed — and the
launcher regression suite `ALL PASS / 64 cases`. One warning remained, the
Codex hook trust, which is an interactive per-clone step (§ security-boundary).
The box also reported no agy `write_file` grant, so `agy-run.sh` there would
report `AGY_UNAVAILABLE` — the documented fallback, behaving as designed.
(observed, native aarch64)
