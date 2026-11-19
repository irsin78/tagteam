"""tagteam state folder: where role records, exhaustion, run records and proposals live.

Where it is and why: docs/manual.md, section 2. In short: <main work tree>/.tagteam/ (a submodule uses the
superproject's work tree; a Codex sandbox cannot write inside .git, so it is not <git common dir>/tagteam/ any
more); <install root>/.tagteam/ outside git; TAGTEAM_STATE_DIR overrides everything (tests, manual use).
Standard library only. Every tool that needs the folder calls this module and nothing else.
"""
import errno
import filecmp
import os
import secrets
import shutil
import subprocess

NAME = ".tagteam"
OLD_NAME = "tagteam"
GIT_TIMEOUT = 5
_cache = {}


class StateError(Exception):
    """The message names the folder and says how to fix the problem."""


def _git(where, *args):
    """stdout (stripped) of `git -C where args`, or None on any failure or timeout."""
    try:
        out = subprocess.run(
            ["git", "-C", where] + list(args), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _common_dir(where):
    """Absolute git common dir of the repository at `where`, or None."""
    out = _git(where, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if out is None:  # git older than 2.31 has no --path-format
        out = _git(where, "rev-parse", "--git-common-dir")
    if out is None:
        return None
    return os.path.normpath(os.path.join(where, out))


def _top(cwd):
    """Outermost work tree: follow superprojects (nested submodules included). None outside git."""
    if _git(cwd, "rev-parse", "--git-dir") is None:
        return None
    top = cwd
    for _ in range(16):
        sup = _git(top, "rev-parse", "--show-superproject-working-tree")
        if not sup:
            break
        top = os.path.normpath(sup)
    return top


def _work_tree_of(top, common):
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    # A linked worktree of a submodule has no detectable superproject, but its common dir
    # is <super>/.git/modules/<name>: the superproject's work tree is what we want.
    # (of a superproject's linked worktree: <super>/.git/worktrees/<id>/modules/<name>; nested ones go deeper.)
    parts = os.path.normpath(common).split(os.sep)
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] == ".git" and (parts[i + 1] == "modules" or
                                   (parts[i + 1] == "worktrees" and i + 3 < len(parts) and parts[i + 3] == "modules")):
            root = os.sep.join(parts[:i]) or os.sep
            if os.path.isdir(os.path.join(root, ".git")):
                return root
            break
    # Git dir kept elsewhere (--separate-git-dir). Git records no link back to the main work tree, and
    # its worktree list may name the git dir itself; use the first entry only when it is a real checkout
    # (has its own .git), else the caller's own checkout. So in this layout the main checkout and each
    # linked worktree can end up with their own folder (known limitation).
    out = _git(top, "worktree", "list", "--porcelain")
    if out:
        record = out.split("\n\n")[0].split("\n")  # the first record is the main entry
        entry = record[0][9:] if record[0].startswith("worktree ") else ""
        if entry and "bare" not in record[1:] and os.path.lexists(os.path.join(entry, ".git")):
            return os.path.normpath(entry)
    tl = _git(top, "rev-parse", "--show-toplevel")
    return os.path.normpath(tl) if tl else None


def _resolve(cwd):
    """(top, root): the outermost work tree reached from cwd, and the main work tree that owns the state folder."""
    top = _top(cwd)
    if not top:
        return None, None
    common = _common_dir(top)
    return top, (_work_tree_of(top, common) if common else None)


def _override():
    return os.environ.get("TAGTEAM_STATE_DIR") or None


def state_dir(cwd=None):
    """Path of the state folder. Computes only; never creates anything."""
    override = _override()
    if override:
        return override
    cwd = os.path.abspath(cwd or os.getcwd())
    if cwd not in _cache:
        root = _resolve(cwd)[1]
        here = os.path.dirname(os.path.abspath(__file__))  # <root>/.claude/tagteam (or .../harness/tagteam)
        _cache[cwd] = os.path.join(root or os.path.dirname(os.path.dirname(here)), NAME)
    return _cache[cwd]


CREATE_TRIES = 3  # a taken name is retried this often; any other refusal is final at once


def create_exclusive(folder, prefix):
    """(fd, path) of a new file in folder, created exclusively under a short name of its own (prefix and 8 random hex
    digits, as long as mkstemp's: a folder path near the Windows MAX_PATH limit must still fit). A name that is taken
    (FileExistsError) is retried with another one, at most CREATE_TRIES times and then raised; every other OSError, a
    refusal included, is raised at once. tempfile.mkstemp is not used here: on affected Windows Pythons it retries a
    PermissionError up to TMP_MAX times (2147483647 on 3.12) while os.access says the folder is writable, which looks like
    a hang in a sandbox that denies writes os.access does not see."""
    for _ in range(CREATE_TRIES):
        path = os.path.join(folder, prefix + secrets.token_hex(4))
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no free file name in %d tries" % CREATE_TRIES)


def ensure_writable(path):
    """Create the folder and prove it can be written. Returns path; raises StateError."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = create_exclusive(path, ".write-test-")
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
            os.remove(probe)
    except OSError as exc:
        raise StateError(
            "state folder %s is not writable (%s: %s). Fix its permissions, or set TAGTEAM_STATE_DIR to a "
            "writable folder. Codex: run this command with escalated permissions."
            % (path, type(exc).__name__, exc.strerror or exc))
    return path


def legacy_dirs(cwd=None):
    """Old state folders (<git common dir>/tagteam) that still exist. Empty outside git."""
    cwd = os.path.abspath(cwd or os.getcwd())
    top, root = _resolve(cwd)
    if not top:
        return []
    found = []
    for where in (root, top, cwd):  # the owning work tree first: it also covers a submodule's linked worktree
        common = _common_dir(where) if where else None
        if not common:
            continue
        old = os.path.join(common, OLD_NAME)
        if os.path.isdir(old) and not any(os.path.realpath(old) == os.path.realpath(f) for f in found):
            found.append(old)
    return found


def _copy_file(src, dst):
    """Copy via a temporary sibling and publish with os.replace, so dst is never half-written."""
    tmp = "%s.copy-%d.tmp" % (dst, os.getpid())
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _copy_missing(src, dst, rel, report):
    """Copy what dst lacks; keep dst on conflicts. Returns the number of items copied."""
    copied = 0
    for name in sorted(os.listdir(src)):
        s, d, r = os.path.join(src, name), os.path.join(dst, name), (rel + "/" + name) if rel else name
        is_dir = os.path.isdir(s) and not os.path.islink(s)
        if not os.path.lexists(d):
            if is_dir:
                os.makedirs(d)
                copied += _copy_missing(s, d, r, report) + 1
            else:
                _copy_file(s, d)
                copied += 1
        elif is_dir and os.path.isdir(d):
            copied += _copy_missing(s, d, r, report)
        elif os.path.isfile(s) and os.path.isfile(d) and filecmp.cmp(s, d, shallow=False):
            continue  # already copied (an earlier run was interrupted)
        else:
            report.append("conflict: %s exists in both places; kept the new one" % r)
    return copied


def _moved_name(old):
    base = old + ".moved"
    n, name = 1, base
    while os.path.lexists(name):
        n += 1
        name = "%s-%d" % (base, n)
    return name


def migrate(cwd=None):
    """Move the old state folders into the new one, once. Returns report lines (empty = nothing to do).

    Never destroys data: only items missing in the new folder are copied (the new copy wins on a
    conflict and the conflict is reported), then the old folder is renamed to tagteam.moved
    (tagteam.moved-2, ... when that exists). Raises StateError when the new folder is not writable.
    Called by install.py only; hooks and launchers never move anything.
    """
    old_dirs = legacy_dirs(cwd)
    if not old_dirs:
        return []
    if _override():
        return ["TAGTEAM_STATE_DIR is set; old state folder%s not moved: %s" % ("s" if len(old_dirs) > 1 else "", ", ".join(old_dirs))]
    target = ensure_writable(state_dir(cwd))
    report = []
    for old in old_dirs:
        if os.path.realpath(old) == os.path.realpath(target):
            continue
        conflicts = []
        try:
            copied = _copy_missing(old, target, "", conflicts)
        except OSError as exc:  # the old folder is left as it is; running again finishes the copy
            raise StateError("could not copy %s to %s (%s: %s); nothing was removed, run again after fixing it"
                             % (old, target, type(exc).__name__, exc.strerror or exc))
        report.append("copied %d item(s) from %s to %s" % (copied, old, target))
        report.extend("%s (%s)" % (c, old) for c in conflicts)
        moved = _moved_name(old)
        try:
            os.rename(old, moved)
            report.append("renamed %s to %s" % (old, moved))
        except OSError as exc:
            report.append("WARNING: could not rename %s (%s); it will be copied again next time" % (old, exc.strerror or exc))
    return report
