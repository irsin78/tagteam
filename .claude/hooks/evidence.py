#!/usr/bin/env python
"""Shared evidence paths and bounded, locked appends for worker metadata.

CLAUDE_PROJECT_DIR keeps worktree evidence at the initiating project root.
Git reconstruction is a fallback; non-Git projects use their current root.
Commands and permission requests are not recorded here.
"""
import errno
import os
import subprocess
import time

# Conservative UTF-8 byte budget per metadata record for bounded rotation reads.
MAX_LINE_BYTES = 2400

try:                    # POSIX
    import fcntl
except ImportError:     # pragma: no cover - Windows
    fcntl = None
try:                    # Windows
    import msvcrt
except ImportError:     # pragma: no cover - POSIX
    msvcrt = None

def git(cwd, *args):
    try:
        r = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def main_checkout(cwd):
    """The main working tree, even when `cwd` is a linked worktree.

    `git rev-parse --git-common-dir` answers the MAIN repository's .git
    from anywhere in the tree (a plain `.git` in the main checkout, an
    absolute path from a worktree). Its parent is the main checkout.

    Measured failure shapes, all falling back to `cwd`:
    a BARE repository's worktree (the common dir's parent holds no
    checkout), a SUBMODULE (`super/.git/modules/sub` -> the submodule,
    not the superproject), git < 2.31 (no `--path-format`, so the call
    errors out), and anything that is not a repository. The fallback is
    what this hook used to do; `project_root()` prefers the
    host's own `CLAUDE_PROJECT_DIR` precisely so these shapes are not
    the only answer.
    """
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return cwd
    root = os.path.dirname(os.path.normpath(common))
    return root if os.path.isdir(os.path.join(root, ".claude")) else cwd


def project_root(data=None):
    """The directory whose `.claude/` receives this hook's evidence."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(os.path.join(env, ".claude")):
        return env
    cwd = (data or {}).get("cwd") or os.getcwd()
    return main_checkout(cwd)


def evidence_dir(data=None):
    """`<project root>/.claude` when it exists, else None."""
    d = os.path.join(project_root(data), ".claude")
    return d if os.path.isdir(d) else None


def _lock(fd):
    """Take an exclusive lock on the file, or give up after ~2 seconds.

    Measured on Windows: 200 concurrent appends kept
    194-198 lines with a plain `open(path, "a")` and 168-179 with a raw
    `os.O_APPEND` write, because the CRT emulates append as seek + write.
    With this lock all 200 land, at 0.09 ms per uncontended append. A
    BLOCKING `msvcrt.locking` costs ~230 ms under that contention, so the
    spin is non-blocking on purpose. Failing to take the lock still
    writes: a possibly-lost line beats a lost denial record.
    """
    if fcntl is None and msvcrt is None:        # pragma: no cover
        return False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            # A filesystem that does not implement locking (some network
            # mounts) answers EINVAL/ENOTSUP — retrying that for two
            # seconds would put the delay on EVERY denial, in the
            # PreToolUse path. Only "someone else holds it" waits.
            if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
                             errno.EBADF):
                return False
            time.sleep(0.001)
    return False


def _unlock(fd):
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def append_line(path, line, max_lines, line_bytes=None):
    """Append one line under a file lock; rotate past 2x the cap.

    Parallel subagents are the default, so concurrent hook processes write
    this file at the same time; the read-modify-write this replaced kept
    3 of 40 concurrent denials. Rotation is far rarer
    than an append, so it can afford tmp + os.replace.
    """
    data = (line + "\n").encode("utf-8")
    # O_RDWR, not O_APPEND: the lock provides the exclusivity, and the
    # rotation below has to read and rewrite through this same descriptor.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        held = _lock(fd)
        os.lseek(fd, 0, os.SEEK_END)
        while data:                      # os.write is allowed to write short
            data = data[os.write(fd, data):]
        # Rotation happens INSIDE the lock, ON THIS DESCRIPTOR (push-range
        # review of r18). The first version rotated outside the lock via a
        # shared `.tmp` + os.replace, where two hooks past the threshold
        # could rename over each other -- and on Windows the replace fails
        # anyway while any handle is open, so rotation silently never ran
        # (measured: the log reached 441 lines with a 200 cap).
        _rotate(fd, max_lines, line_bytes)
    finally:
        if held:
            _unlock(fd)
        os.close(fd)


def _rotate(fd, max_lines, line_bytes=None):
    """Trim the open, locked log to `max_lines` once it holds twice that.

    The first version pre-checked the SIZE with a 64-byte-per-line lower
    bound, which meant short lines never rotated at all: 451 one-field
    entries stayed 451 lines under a 200 cap (measured —
    the line cap was not a cap). Counting lines is the fix, but reading
    the WHOLE file to count them is unbounded on first contact with a log
    this function has never trimmed: an r18-era file, a concatenation, a
    hand-edited one. So only the tail window that can hold 2x the cap is
    read (an entry is at most ~805 bytes: 500 of command, 200 of reason,
    metadata), and anything past it is dropped rather than parsed — the
    newest `max_lines` entries are all this file promises. A single line
    longer than the window leaves nothing to keep, which is the bounded
    outcome, not an error.
    """
    window = max_lines * 2 * (line_bytes or MAX_LINE_BYTES)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        start = max(0, size - window)
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        if start:
            cut = data.find(b"\n")          # the first line is a fragment
            data = data[cut + 1:] if cut >= 0 else b""
        lines = [l for l in data.splitlines() if l.strip()]
        if start == 0 and len(lines) <= max_lines * 2:
            return
        kept = b"\n".join(lines[-max_lines:]) + b"\n" if lines else b""
        os.lseek(fd, 0, os.SEEK_SET)
        while kept:
            kept = kept[os.write(fd, kept):]
        os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    except Exception:
        pass                    # a log that grows beats a log that is lost
