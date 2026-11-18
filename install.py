#!/usr/bin/env python3
"""tagteam installer: install / update / check. Standard library only (Python 3.9+).

    python install.py <target> [--update] [--source DIR] [--yes]
    python install.py <target> --check [--host claude|codex|agy] [--live] [--source DIR]
    python install.py <target> --repair-hooks [--yes]      (preview unless --yes)
    python install.py <target> --schedule | --unschedule [--dry-run]
"""
import argparse
import copy
import difflib
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

POLICY_MARK = "## Project policy"
HOOK_KEYS = ("hooks", "subagentPromptCacheTtl")
VENDORS = ("claude", "openai", "google")
TIERS = ("none", "free", "pro", "max")


class Report:
    def __init__(self):
        self.items = []

    def add(self, state, path):
        self.items.append((state, str(path).replace("\\", "/")))

    def print(self):
        for state in ("created", "updated", "kept"):
            for s, p in self.items:
                if s.split(" ")[0] == state:
                    print(f"  {s:8} {p}")


def put(rep, target, rel, data, *, keep_existing=False, note=""):
    """Write bytes to target/rel and record created/updated/kept."""
    dst = target / rel
    if dst.exists():
        if keep_existing or dst.read_bytes() == data:
            rep.add("kept" + note, rel)
            return
        state = "updated"
    else:
        state = "created"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    rep.add(state, rel)


GUARD_REL = ".claude/tagteam/role_guard.py"
STATE_REL = ".claude/tagteam/state.py"


def localize_hooks(src, guard):
    """Codex runs hooks in the session cwd: point at the absolute, quoted guard path."""
    quoted = '"' + str(guard.resolve()).replace("\\", "/") + '"'

    def walk(v):
        if isinstance(v, str):
            return v.replace('"' + GUARD_REL + '"', GUARD_REL).replace(GUARD_REL, quoted)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    obj = walk(json.loads(src.read_text(encoding="utf-8")))
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


HOOKS_REL = Path(".codex/hooks.json")
GUARD_NAME = "role_guard.py"
TRUST_NOTE = ("Codex trusts each hook by its definition. Open this project in an interactive codex session and trust the "
              "tagteam hooks; until then a new or changed hook is skipped silently, with no warning.")


def expected_codex_hooks(source, target):
    """The template's hooks.json with the guard path made absolute, as install writes it. None without a template."""
    src = source / "harness" / "codex" / "hooks.json"
    if not src.is_file():
        return None
    return json.loads(localize_hooks(src, target / GUARD_REL).decode("utf-8"))


def is_tagteam_hook(hook):
    return isinstance(hook, dict) and any(GUARD_NAME in str(hook.get(k) or "") for k in ("command", "commandWindows"))


def tagteam_slots(groups):
    """[(group index, hook index, group, hook)] of the tagteam hooks in one event's group list (others are ignored)."""
    out = []
    for gi, group in enumerate(groups if isinstance(groups, list) else []):
        hooks = group.get("hooks") if isinstance(group, dict) else None
        for hi, hook in enumerate(hooks if isinstance(hooks, list) else []):
            if is_tagteam_hook(hook):
                out.append((gi, hi, group, hook))
    return out


def hook_definition(group, hook):
    """What Codex's trust is bound to: the matcher and every field of the hook."""
    d = dict(hook)
    d["matcher"] = group.get("matcher")
    return d


def definition_diff(have, want):
    return ["%s: have %s, want %s" % (k, json.dumps(have.get(k, "(absent)")), json.dumps(want.get(k, "(absent)")))
            for k in sorted(set(have) | set(want)) if have.get(k, "(absent)") != want.get(k, "(absent)")]


def missing_guard_paths(target, hook):
    out = []
    for key in ("command", "commandWindows"):
        for m in re.finditer(r'"([^"]*role_guard\.py)"|(\S*role_guard\.py)', str(hook.get(key) or "")):
            path = m.group(1) or m.group(2)
            full = Path(path) if os.path.isabs(path) else target / path
            if not full.is_file() and path not in out:
                out.append(path)
    return out


def pair_hooks(want, have):
    """Match the template's tagteam hooks to the existing ones: an identical definition first, then the
    remaining ones in order. -> ({want index: (have index or None, exact)}, [unmatched existing slots])."""
    used, pairs = set(), {}
    for wi, (_, _, egroup, ehook) in enumerate(want):
        for si, (_, _, hgroup, hhook) in enumerate(have):
            if si not in used and hook_definition(hgroup, hhook) == hook_definition(egroup, ehook):
                used.add(si)
                pairs[wi] = (si, True)
                break
    for wi in range(len(want)):
        if wi not in pairs:
            free = next((si for si in range(len(have)) if si not in used), None)
            if free is not None:
                used.add(free)
            pairs[wi] = (free, False)
    return pairs, [have[si] for si in range(len(have)) if si not in used]


def hook_shape_error(current, expected):
    """Why the events the template uses cannot be edited safely (None when they can). Other events are not looked at."""
    hooks = current.get("hooks") if isinstance(current, dict) else None
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        return "'hooks' is not an object"
    for event in expected["hooks"]:
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            return f"hooks.{event} is not a list"
        for gi, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                return f"hooks.{event}[{gi}] is not a group object with a 'hooks' list"
            for hi, hook in enumerate(group["hooks"]):
                if not isinstance(hook, dict):
                    return f"hooks.{event}[{gi}].hooks[{hi}] is not an object"
    return None


def diagnose_hooks(target, expected, current):
    """Drift of the tagteam entries of .codex/hooks.json: [(event, kind, text)]. kind = missing | different |
    path (these three are drift) | stale (a tagteam hook that differs from the template, kept next to a correct
    one) | extra (a duplicate or an unused event). Other entries are ignored."""
    issues = []
    have_events = current.get("hooks") if isinstance(current, dict) else None
    have_events = have_events if isinstance(have_events, dict) else {}
    for event, egroups in expected["hooks"].items():
        want, have = tagteam_slots(egroups), tagteam_slots(have_events.get(event))
        pairs, left = pair_hooks(want, have)
        for wi, (_, _, egroup, ehook) in enumerate(want):
            si, exact = pairs[wi]
            if si is None:
                issues.append((event, "missing", "%s: no tagteam hook" % event))
                continue
            _, _, hgroup, hhook = have[si]
            if not exact:
                for line in definition_diff(hook_definition(hgroup, hhook), hook_definition(egroup, ehook)):
                    issues.append((event, "different", "%s: %s" % (event, line)))
            for path in missing_guard_paths(target, hhook):
                issues.append((event, "path", "%s: the guard script does not exist: %s" % (event, path)))
        wanted = [hook_definition(g, h) for _, _, g, h in want]
        for gi, hi, hgroup, hhook in left:
            if hook_definition(hgroup, hhook) in wanted:
                issues.append((event, "extra", "%s: duplicate tagteam hook (group %d, hook %d), left as it is" % (event, gi, hi)))
            else:
                issues.append((event, "stale", "%s: tagteam hook (group %d, hook %d) differs from the template and was left as it is "
                               "(it shares a group with other hooks, or a correct one exists)" % (event, gi, hi)))
    for event, groups in have_events.items():
        if event not in expected["hooks"]:
            for gi, hi, _, _ in tagteam_slots(groups):
                issues.append((event, "extra", "%s: tagteam hook for an event the template does not use (group %d, hook %d), left as it is" % (event, gi, hi)))
    return issues


def load_codex_hooks(target):
    """(current hooks.json object or None when absent, error text or None)."""
    path = target / HOOKS_REL
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{HOOKS_REL.as_posix()} cannot be read: {exc}"
    if not isinstance(data, dict) or ("hooks" in data and not isinstance(data["hooks"], dict)):
        return None, f"{HOOKS_REL.as_posix()} has an unexpected shape (no hooks object)"
    return data, None


def plan_repair(current, expected):
    """(new hooks.json object, action lines). Other entries keep their place: Codex's trust key holds the
    group and hook index, so a tagteam hook is replaced in place, a missing one is appended at the event's end.
    ValueError when an event the template uses has a structure that cannot be edited safely."""
    problem = hook_shape_error(current, expected)
    if problem:
        raise ValueError(problem)
    new = copy.deepcopy(current) if current else {}
    hooks = new.setdefault("hooks", {})
    actions = []
    for event, egroups in expected["hooks"].items():
        want, have = tagteam_slots(egroups), tagteam_slots(hooks.get(event))
        pairs, _ = pair_hooks(want, have)
        for wi, (_, _, egroup, ehook) in enumerate(want):
            si, exact = pairs[wi]
            if exact:
                continue
            if si is not None:
                gi, hi, hgroup, _ = have[si]
                if egroup.get("matcher") == hgroup.get("matcher") or all(is_tagteam_hook(h) for h in hgroup["hooks"]):
                    hgroup["hooks"][hi] = copy.deepcopy(ehook)
                    if "matcher" in egroup:
                        hgroup["matcher"] = egroup["matcher"]
                    else:
                        hgroup.pop("matcher", None)
                    actions.append("%s: replaced the tagteam hook in place (group %d, hook %d)" % (event, gi, hi))
                    continue
                actions.append("%s: group %d shares a matcher with other hooks; its tagteam hook stays as it is and a correct "
                               "group is appended" % (event, gi))
            group = {k: copy.deepcopy(v) for k, v in egroup.items() if k != "hooks"}
            group["hooks"] = [copy.deepcopy(ehook)]
            hooks.setdefault(event, []).append(group)
            actions.append("%s: appended the tagteam hook at the end of the event" % event)
    return new, actions


def backup_path(path):
    bak = path.with_name(path.name + ".bak")
    n = 1
    while bak.exists():
        n += 1
        bak = path.with_name(f"{path.name}.bak-{n}")
    return bak


def repair_hooks(target, source, apply):
    """Bring the tagteam entries of .codex/hooks.json back to the template. Preview unless apply."""
    expected = expected_codex_hooks(source, target)
    if expected is None:
        print(f"error: {source}/harness/codex/hooks.json not found; nothing to repair against", file=sys.stderr)
        return 1
    current, err = load_codex_hooks(target)
    if err:
        print("error: " + err + "; fix or remove it first", file=sys.stderr)
        return 1
    try:
        new, actions = plan_repair(current, expected)
    except (ValueError, AttributeError, TypeError, KeyError) as exc:
        print(f"error: {HOOKS_REL.as_posix()} has a structure repair does not edit ({exc}); nothing was written. "
              "Fix that part by hand", file=sys.stderr)
        return 1
    if not actions:
        print(f"{HOOKS_REL.as_posix()}: the tagteam hooks already match the template; nothing to do")
        return 0
    for line in actions:
        print("  " + line)
    dump = lambda obj: json.dumps(obj, indent=2, ensure_ascii=False) + "\n"  # noqa: E731
    for line in difflib.unified_diff(dump(current or {}).splitlines(), dump(new).splitlines(),
                                     "hooks.json (now)", "hooks.json (repaired)", lineterm=""):
        print("  " + line)
    if not apply:
        print("preview only: nothing was written. Run again with --yes to apply.")
        return 0
    path = target / HOOKS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        bak = backup_path(path)
        shutil.copy2(path, bak)
        print(f"  backup   {bak.relative_to(target).as_posix()}")
    path.write_text(dump(new), encoding="utf-8")
    print(f"  updated  {HOOKS_REL.as_posix()}")
    print(TRUST_NOTE)
    return 0


def copy_tree(rep, src_dir, target, rel_dir, pattern="*"):
    """Copy files matching pattern (recursively) from src_dir to target/rel_dir."""
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob(pattern)):
        if (f.is_file() and ".local." not in f.name and "__pycache__" not in f.parts
                and "tests" not in f.relative_to(src_dir).parts):
            rel = Path(rel_dir) / f.relative_to(src_dir)
            put(rep, target, rel, f.read_bytes(), keep_existing=f.name.endswith(".local.json"))


def merge_settings(rep, src, target):
    rel = Path(".claude/settings.json")
    new = json.loads(src.read_text(encoding="utf-8"))
    dst = target / rel
    if not dst.exists():
        put(rep, target, rel, src.read_bytes())
        return
    cur = json.loads(dst.read_text(encoding="utf-8"))
    for k in HOOK_KEYS:
        if k in new:
            cur[k] = new[k]
    put(rep, target, rel, (json.dumps(cur, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def merge_agents_md(rep, src, target):
    rel = Path("AGENTS.md")
    text = src.read_text(encoding="utf-8")
    dst = target / rel
    if dst.exists():
        old = dst.read_text(encoding="utf-8")
        i, j = old.find(POLICY_MARK), text.find(POLICY_MARK)
        head = text[:j] if j >= 0 else text.rstrip("\n") + "\n\n"
        if i >= 0:
            text = head + old[i:]
        elif old.strip():
            # No marker: the file is the project's own. Keep all of it under a new policy section.
            text = head + POLICY_MARK + "\n\n" + old.strip("\n") + "\n"
            dst.write_bytes(text.encode("utf-8"))
            rep.add("kept (moved under Project policy)", rel)
            return
    put(rep, target, rel, text.encode("utf-8"))


def ensure_claude_md(rep, src, target):
    dst = target / "CLAUDE.md"
    if not dst.exists():
        put(rep, target, Path("CLAUDE.md"), src.read_bytes())
        return
    old = dst.read_text(encoding="utf-8")
    if not any(ln.strip() == "@AGENTS.md" for ln in old.splitlines()):
        sep = "" if old.endswith("\n") or not old else "\n"
        put(rep, target, Path("CLAUDE.md"), (old + sep + "@AGENTS.md\n").encode("utf-8"))
    else:
        rep.add("kept", "CLAUDE.md")


def ask_tiers():
    plans = {}
    for v in VENDORS:
        while True:
            ans = input(f"Plan tier for {v} ({'/'.join(TIERS)}) [pro]: ").strip().lower()
            if not ans:  # Enter keeps the default (an unset vendor counts as pro)
                break
            if ans in TIERS:
                plans[v] = ans
                break
    return plans


LOCAL_IGNORES = (".claude/tagteam/models.local.json", ".claude/tagteam/bench.local.json")
STATE_IGNORE = ".tagteam/"
BENCH_REL = Path(".claude/tagteam/bench.local.json")
SKILL_ROOTS = (".claude/skills", ".agents/skills")  # Claude Code reads the first, Codex the second


def add_ignores(rep, root, want):
    """Append the missing lines of `want` to root/.gitignore."""
    gi = root / ".gitignore"
    old = gi.read_text(encoding="utf-8") if gi.exists() else ""
    have = {ln.strip().rstrip("/") for ln in old.splitlines()}
    add = [w for w in want if w.rstrip("/") not in have]
    if not add:
        return
    sep = "" if old.endswith("\n") or not old else "\n"
    data = (old + sep + "".join(w + "\n" for w in add)).encode("utf-8")
    put(rep, root, Path(".gitignore"), data)


def state_checkout(target):
    """The checkout that holds the state folder: <main work tree> (a submodule's superproject, a linked
    worktree's main checkout) or the target itself. Only a folder named .tagteam counts (not an override)."""
    state = installed_state_dir(target)
    if state is not None and state.name == ".tagteam" and state.parent.is_dir():
        return state.parent
    return target


def ensure_gitignore(rep, target):
    """Machine-local files are ignored in the target. The state folder (.tagteam/) is ignored in the checkout
    that contains it, which is another one for a submodule or a linked worktree: the target's own .gitignore
    gets no .tagteam/ rule then, because nothing is created there."""
    root = state_checkout(target)
    if root == target:
        add_ignores(rep, target, LOCAL_IGNORES + (STATE_IGNORE,))
        return
    add_ignores(rep, target, LOCAL_IGNORES)
    add_ignores(rep, root, (STATE_IGNORE,))


def ensure_bench(target):
    """With an AA key and no bench.local.json yet, write it once. Never fails the install."""
    collector = target / COLLECTOR_REL
    if (target / BENCH_REL).exists() or not os.environ.get("TAGTEAM_AA_API_KEY", "").strip() \
            or not collector.is_file():
        return
    env = {k: v for k, v in os.environ.items() if k != "TAGTEAM_CONFIG_DIR"}
    try:
        p = subprocess.run([sys.executable, str(collector), "--write-bench", "--timeout", "15"],
                           capture_output=True, text=True, env=env, timeout=90)
        ok, text = p.returncode == 0, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        ok, text = False, str(exc)
    if ok:
        print("  bench    " + BENCH_REL.as_posix() + " written from Artificial Analysis")
    else:
        print("warning: could not write %s (%s); run models_collect.py --write-bench later"
              % (BENCH_REL.as_posix(), text.splitlines()[-1][:200] if text else "no output"), file=sys.stderr)


def ensure_available(target):
    """With no "available" lists in models.local.json yet, ask the vendor CLIs once, so routes skip
    models this machine's plans do not offer. Never fails the install."""
    collector, local = target / COLLECTOR_REL, target / ".claude/tagteam/models.local.json"
    try:
        data = json.loads(local.read_text(encoding="utf-8")) if local.is_file() else {}
        vendors = json.loads((target / ".claude/tagteam/models.json").read_text(encoding="utf-8")).get("vendors") or {}
        have = set(data.get("available") or {})
        plans = data.get("plans") or {}
    except (OSError, ValueError, AttributeError):
        return
    want = {v for v in vendors if plans.get(v) != "none"}
    if not collector.is_file() or (have and want <= have):  # retry while some vendor has no list yet
        return
    env = {k: v for k, v in os.environ.items() if k != "TAGTEAM_CONFIG_DIR"}
    try:
        p = subprocess.run([sys.executable, str(collector), "--write-available", "--timeout", "30"],
                           capture_output=True, text=True, env=env, timeout=150)
        ok, text = p.returncode == 0, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        ok, text = False, str(exc)
    if ok and "wrote available" in text:
        print("  models   available model lists written to models.local.json")
    else:
        print("warning: could not record available models (%s); run models_collect.py --write-available later"
              % (text.splitlines()[-1][:200] if text else "no output"), file=sys.stderr)


def install(target, source, interactive):
    rep = Report()
    h = source / "harness"
    if (h / "claude" / "settings.json").is_file():
        merge_settings(rep, h / "claude" / "settings.json", target)
    copy_tree(rep, h / "claude" / "agents", target, ".claude/agents", "*.md")
    for skills_root in SKILL_ROOTS:
        copy_tree(rep, h / "skills", target, skills_root)
    copy_tree(rep, h / "tagteam", target, ".claude/tagteam")
    hooks = h / "codex" / "hooks.json"
    if hooks.is_file():
        existed = (target / ".codex/hooks.json").exists()
        data = localize_hooks(hooks, target / ".claude" / "tagteam" / "role_guard.py")
        put(rep, target, Path(".codex/hooks.json"), data, keep_existing=True)
        if existed:
            print("note: .codex/hooks.json already exists and was kept (machine-local trust).")
            current, err = load_codex_hooks(target)
            expected = json.loads(data.decode("utf-8"))
            err = err or (hook_shape_error(current, expected) if current is not None else None)
            drift = [i for i in diagnose_hooks(target, expected, current)
                     if i[1] not in ("extra", "stale")] if current is not None and not err else []
            if err or drift:
                print("warning: the tagteam hooks in .codex/hooks.json differ from the template (%s); "
                      "preview the fix with: python install.py %s --repair-hooks" % (err or f"{len(drift)} difference(s)", target))
        else:
            print("note: " + TRUST_NOTE)
    copy_tree(rep, h / "codex" / "agents", target, ".codex/agents", "*.toml")
    if (source / "AGENTS.md.template").is_file():
        merge_agents_md(rep, source / "AGENTS.md.template", target)
    if (source / "CLAUDE.md.template").is_file():
        ensure_claude_md(rep, source / "CLAUDE.md.template", target)
    local = Path(".claude/tagteam/models.local.json")
    if not (target / local).exists():
        example = next((p for p in (h / "tagteam" / "models.local.json.example",
                                    source / "models.local.json.example") if p.is_file()), None)
        if interactive:
            data = (json.dumps({"plans": ask_tiers()}, indent=2) + "\n").encode("utf-8")
            put(rep, target, local, data)
        elif example:
            put(rep, target, local, example.read_bytes())
    ensure_gitignore(rep, target)
    return rep


# ---- --check -------------------------------------------------------------

def smoke_test(target):
    """Feed fake payloads to the installed role_guard. Returns a list of failures."""
    guard = target / ".claude" / "tagteam" / "role_guard.py"
    if not guard.is_file():
        return [f"role_guard.py missing: {guard}"]
    fails = []
    with tempfile.TemporaryDirectory() as state:
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_DELEGATE_RUN"}
        env["TAGTEAM_STATE_DIR"] = state
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # the installed guard imports state.py; leave no __pycache__

        def run(event, payload):
            try:
                p = subprocess.run([sys.executable, str(guard), event, "--host", "claude"],
                                   input=json.dumps(payload), capture_output=True, text=True,
                                   env=env, timeout=30)
                return p.returncode
            except Exception as e:  # noqa: BLE001
                return f"error: {e}"

        run("UserPromptSubmit", {"session_id": "smoke-orch", "prompt": "hello"})
        run("UserPromptSubmit", {"session_id": "smoke-worker",
                                 "prompt": "HARNESS ROLE: DELEGATE\ndo work"})
        cases = [
            ("orchestrator start_session without model", {
                "session_id": "smoke-orch", "tool_name": "mcp__ccd_session__start_session",
                "tool_input": {"prompt": "x"}}, 2),
            ("worker git commit", {
                "session_id": "smoke-worker", "tool_name": "Bash",
                "tool_input": {"command": "git commit -m x"}}, 2),
            ("git status", {
                "session_id": "smoke-worker", "tool_name": "Bash",
                "tool_input": {"command": "git status"}}, 0),
        ]
        for name, payload, want in cases:
            got = run("PreToolUse", payload)
            if got != want:
                fails.append(f"smoke '{name}': expected exit {want}, got {got}")
    return fails


def skill_copy_errors(target):
    """The two installed skill folders must hold the same files with the same content."""
    trees = {}
    for root in SKILL_ROOTS:
        base = target / root
        trees[root] = ({f.relative_to(base).as_posix(): f.read_bytes() for f in base.rglob("*") if f.is_file()}
                       if base.is_dir() else None)
    if all(t is None for t in trees.values()):
        return []
    first, second = SKILL_ROOTS
    if trees[first] is None or trees[second] is None:
        missing = first if trees[first] is None else second
        return [f"skills: {missing} is missing; run install.py --update"]
    diff = sorted(f for f in set(trees[first]) | set(trees[second]) if trees[first].get(f) != trees[second].get(f))
    return [f"skills: {first} and {second} differ in {', '.join(diff[:5])}; run install.py --update"] if diff else []


PAYLOADS_REL = Path("harness/tagteam/tests/fixtures/codex_payloads.json")
APPROVAL_NOTE = "Codex orchestrator: git writes (add, commit, branch, worktree) and run.py need approval; ask for escalated permissions."
CHECK_HOSTS = ("claude", "codex", "agy")
LIVE_PROMPT = "Reply with the single word ok. Do not use any tool."


def is_windows():
    return sys.platform == "win32"


def codex_windows_sandbox():
    """The user's Codex `windows.sandbox` setting (from $CODEX_HOME or ~/.codex/config.toml), or None."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        text = Path(home, "config.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[] ")
            continue
        m = re.match(r'^(windows\.)?sandbox\s*=\s*["\']([^"\']*)["\']', line)
        if m and ((section == "windows" and not m.group(1)) or (section is None and m.group(1))):
            return m.group(2)
    return None


def state_access_problem(path):
    """Error text if the state folder (or, when it does not exist yet, its nearest existing parent) cannot take
    files. Looks at existence and access flags only: the static check creates nothing."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.is_dir():
        return f"{probe} is not a folder"
    if not os.access(probe, os.W_OK | os.X_OK):
        return f"no write permission on {probe}"
    return None


SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # what an app started from the desktop, or launchd, hands its children


def interpreter_problems(host):
    """(errors, warnings) about the interpreter name the host's hooks call: python3 or python (claude), python3 on
    macOS/Linux and python on Windows (codex). A bare name that is missing makes the hook die with 127, which the
    host reads as 'no objection': the role guard would be off without a word."""
    errors, warnings = [], []
    have = [n for n in ("python3", "python") if shutil.which(n)]
    if host == "claude" and not have:
        errors.append("neither python3 nor python is on PATH: the Claude hooks cannot run the role guard "
                      "(PreToolUse then blocks every tool call until Python is installed)")
    if host == "codex":
        name = "python" if is_windows() else "python3"
        if name not in have:
            errors.append(f"{name} is not on PATH: the Codex hooks call it and would silently not run")
    if have and not is_windows():
        if not any(shutil.which(n, path=SYSTEM_PATH) for n in have):
            warnings.append(f"{have[0]} is found through your shell PATH only; an app started from the desktop (or launchd) "
                            f"gets {SYSTEM_PATH} and may not find it (unverified)")
    return errors, warnings


def hook_shell():
    """The shell Claude Code runs a hook command with: sh on macOS and Linux; Git Bash on Windows (measured with
    Claude Code 2.1.327: BASH_VERSION set, /usr/bin/bash). None when none can be found."""
    if not is_windows():
        return "/bin/sh" if os.path.exists("/bin/sh") else shutil.which("sh")
    cands = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    git = shutil.which("git")
    if git:
        for root in (Path(git).resolve().parent.parent, Path(git).resolve().parent.parent.parent):
            cands += [str(root / "bin" / "bash.exe"), str(root / "usr" / "bin" / "bash.exe")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


HOOK_TIMEOUT_DEFAULT = 60  # seconds a command hook may take when settings.json names no timeout (assumed, not measured)


def settings_guard_hooks(target, event):
    """[(matcher, timeout seconds, command)] of the tagteam hooks of one event in the installed .claude/settings.json."""
    try:
        groups = json.loads((target / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"][event]
        out = []
        for g in groups:
            for h in g["hooks"]:
                if GUARD_NAME in str(h.get("command", "")):
                    t = h.get("timeout")
                    ok = isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0
                    out.append((g.get("matcher"), t if ok else HOOK_TIMEOUT_DEFAULT, h["command"]))
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def settings_guard_commands(target, event):
    """The tagteam hook commands of one event in the installed .claude/settings.json."""
    return [command for _, _, command in settings_guard_hooks(target, event)]


def matcher_hits(matcher, tool):
    """Whether Claude Code would call a hook with this matcher for this tool name (a regex over the whole name)."""
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def hook_command_test(target):
    """(failures, unverified reason): run the installed settings.json hook commands as written, in the shell Claude
    Code uses, with this PATH (so the interpreter the command picks is the one that gets tested) and within the
    hook's own timeout (a hook that overruns it is not enforced). A PreToolUse hook whose matcher does not select
    the tested tool is a failure: Claude Code would never call it for that tool."""
    pre = settings_guard_hooks(target, "PreToolUse")
    if not pre:
        return [], "no tagteam PreToolUse command in .claude/settings.json"
    shell = hook_shell()
    if not shell:
        return [], "no sh (Git Bash on Windows) found to run the hook commands"
    fails = []
    with tempfile.TemporaryDirectory() as state:
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_DELEGATE_RUN"}
        env.update(TAGTEAM_STATE_DIR=state, PYTHONDONTWRITEBYTECODE="1", CLAUDE_PROJECT_DIR=str(target))

        def run(name, command, timeout, payload, want, worker=False, expect_out=None):
            e = dict(env, HARNESS_DELEGATE_RUN="1") if worker else env
            try:
                p = subprocess.run([shell, "-c", command], input=json.dumps(payload), capture_output=True, text=True,
                                   env=e, timeout=timeout, cwd=str(target))  # Claude Code runs a hook in the project
            except subprocess.TimeoutExpired:
                fails.append(f"hook command '{name}': no answer within the hook's timeout of {timeout}s "
                             "(Claude Code would drop the hook and not block)")
                return
            except Exception as exc:  # noqa: BLE001
                fails.append(f"hook command '{name}': {type(exc).__name__}: {exc}")
                return
            text = (p.stdout + p.stderr).strip()
            if p.returncode != want or (expect_out and expect_out not in text):
                hint = " (python3/python was not found by the command: the guard did not run)" \
                    if "not found on PATH" in text or p.returncode == 127 else ""
                fails.append(f"hook command '{name}': expected exit {want}, got {p.returncode}{hint}. Output: {text[:100]!r}")

        # the tagteam hooks may sit in several groups (Bash in one, Edit|Write in another): test those that select Bash
        chosen = [(m, t, c) for m, t, c in pre if matcher_hits(m, "Bash")]
        if not chosen:
            fails.append("hook command 'PreToolUse': no tagteam hook selects Bash (matchers: "
                         + ", ".join(repr(m) for m, _, _ in pre) + "), so Claude Code would not call the guard for shell commands")
        for _, timeout, command in chosen:
            base = {"session_id": "hook-smoke", "tool_name": "Bash", "cwd": str(target)}
            run("PreToolUse worker git commit", command, timeout, dict(base, tool_input={"command": "git commit -m x"}), 2,
                worker=True)
            run("PreToolUse git status", command, timeout, dict(base, tool_input={"command": "git status"}), 0, worker=True)
        for event, marker in (("SessionStart", "TAGTEAM:"), ("UserPromptSubmit", None)):
            for _, timeout, command in settings_guard_hooks(target, event):
                run(event, command, timeout,
                    {"session_id": "hook-smoke", "hook_event_name": event, "prompt": "hello", "cwd": str(target)},
                    0, expect_out=marker)
    return fails, None


def static_checks(target, source, host):
    """(errors, warnings, notes) that need no hook run. Prints the tool lines as it goes."""
    errors, warnings, notes = [], [], []
    for tool in ("python3", "python", "git", "claude", "codex", "agy"):
        path = shutil.which(tool)  # the name a hook calls: python is not a stand-in for python3 (a default macOS has no python)
        if not path:
            if tool not in ("python3", "python"):
                warnings.append(f"{tool}: missing on PATH")
            continue
        print(f"  found    {tool}: {path}")
        if os.name == "nt" and "windowsapps" in path.lower():
            warnings.append(f"{tool}: resolves to a WindowsApps alias stub ({path})")
    bad, shaky = interpreter_problems(host)
    errors += bad
    warnings += shaky
    for rel in (GUARD_REL, STATE_REL):
        if not (target / rel).is_file():
            errors.append(f"{rel} is missing; run install.py")
    errors += skill_copy_errors(target)
    if host == "claude":
        if not any((target / ".claude/agents").glob("*.md")):
            errors.append(".claude/agents has no agent definition; run install.py --update")
        try:
            settings = (target / ".claude/settings.json").read_text(encoding="utf-8")
            if not isinstance(json.loads(settings).get("hooks"), dict):
                errors.append(".claude/settings.json has no hooks")
            elif GUARD_NAME not in settings:
                warnings.append(f".claude/settings.json does not mention {GUARD_NAME}: the role hooks are not wired")
        except (OSError, ValueError, AttributeError):
            errors.append(".claude/settings.json is missing or unreadable; run install.py --update")
    if host == "codex":
        if not any((target / ".codex/agents").glob("*.toml")):
            errors.append(".codex/agents has no agent definition; run install.py --update")
        expected = expected_codex_hooks(source, target)
        current, err = load_codex_hooks(target)
        if err:
            errors.append(err)
        elif current is None:
            errors.append(f"{HOOKS_REL.as_posix()} is missing; run install.py {target} (Codex then has no role hooks)")
        elif expected is None:
            notes.append(f"hooks.json drift: unverified (no template under {source})")
        elif hook_shape_error(current, expected):
            errors.append(f"hooks.json has a structure repair does not edit ({hook_shape_error(current, expected)}); fix it by hand")
        else:
            issues = diagnose_hooks(target, expected, current)
            for event, kind, text in issues:
                if kind in ("extra", "stale"):
                    warnings.append(text)
                else:
                    errors.append(f"hooks.json {kind}: {text}")
            if any(k not in ("extra", "stale") for _, k, _ in issues):
                errors.append(f"fix with: python install.py {target} --repair-hooks (preview) / --yes, then trust the hooks in codex")
        notes.append(APPROVAL_NOTE)
        if is_windows() and (codex_windows_sandbox() or "").lower() == "elevated":
            warnings.append("Windows: your Codex setting windows.sandbox is elevated; commands failed with CreateProcessWithLogonW 1385 "
                            "under codex exec (codex-cli 0.169.0). Interactive behaviour is unverified; unelevated is the "
                            "alternative (your global setting, not changed by tagteam)")
    if host == "agy":
        notes.append("agy: no hook adapter yet (planned for a later milestone); roles are not enforced there")
    # the state folder
    mod = load_installed_state(target)
    try:
        state = Path(mod.state_dir(str(target))) if mod else None
    except Exception:  # noqa: BLE001 - an old state.py
        state = None
    if state is not None:
        print(f"  state    {state}")
        if ".git" in state.parts:
            errors.append(f"state folder {state} is inside .git (Codex sandboxes cannot write there); run install.py --update")
        bad = state_access_problem(state)
        if bad:
            errors.append(f"state folder {state} is not writable ({bad})")
        else:
            notes.append("state folder: writability unverified (only existence and access flags were read; the first hook or run writes it)")
        root = state_checkout(target)
        gi = root / ".gitignore"
        if state.name == ".tagteam" and (root / ".git").exists() and \
                (not gi.is_file() or ".tagteam" not in {ln.strip().rstrip("/") for ln in gi.read_text(encoding="utf-8").splitlines()}):
            warnings.append(f"{gi} does not ignore .tagteam/; run install.py --update")
        try:
            old = list(mod.legacy_dirs(str(target)))
        except Exception:  # noqa: BLE001
            old = []
        for path in old:
            warnings.append(f"old state folder {path} still exists; run install.py --update to move it to .tagteam/")
    else:
        warnings.append("state folder: not computed (no usable installed state.py)")
    return errors, warnings, notes


def handler_codex(target, source):
    """(failures, unverified reason) from fake Codex payloads (the template's fixture) through the installed guard."""
    guard = target / GUARD_REL
    fixture = source / PAYLOADS_REL
    if not guard.is_file():
        return [], "role_guard.py is missing"
    try:
        fx = json.loads(fixture.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], f"payload fixture not found under the source ({fixture})"
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        proj, state = os.path.join(tmp, "proj"), os.path.join(tmp, "state")
        os.makedirs(os.path.join(proj, ".codex", "agents"))
        with open(os.path.join(proj, ".codex", "agents", "reader.toml"), "w", encoding="utf-8") as f:
            f.write('name = "reader"\nmodel = "m"\nmodel_reasoning_effort = "low"\n')
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_DELEGATE_RUN"}
        env["TAGTEAM_STATE_DIR"] = state
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # the guard imports state.py; leave no __pycache__ in the project

        def case(key, deny, name, drop=(), **changes):
            try:
                payload = copy.deepcopy(fx[key])
                payload["cwd"] = proj
                tool_input = changes.pop("tool_input", None)
                if tool_input is not None:
                    payload["tool_input"] = dict(payload["tool_input"], **tool_input)
                for k in drop:
                    payload["tool_input"].pop(k, None)
                payload.update(changes)
                p = subprocess.run([sys.executable, str(guard), payload["hook_event_name"], "--host", "codex"],
                                   input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
            except Exception as exc:  # noqa: BLE001
                fails.append(f"codex '{name}': {type(exc).__name__}: {exc}")
                return
            out = p.stdout.strip()
            try:
                denied = json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny" if out else False
            except (ValueError, KeyError, TypeError):
                denied = False
            if p.returncode != 0 or denied != deny or (not deny and out):
                fails.append(f"codex '{name}': expected {'a deny' if deny else 'no output'}, got exit {p.returncode}, "
                             f"stdout {out[:80]!r}")

        patch = "*** Begin Patch\n*** Update File: AGENTS.md\n@@\n-a\n+b\n*** End Patch"
        try:
            case("spawn_default_with_args", True, "worker spawn", agent_id="smoke-agent")
            case("spawn_default_with_args", True, "orchestrator default spawn without model and effort",
                 drop=("model", "reasoning_effort"))
            case("spawn_custom_with_args", True, "custom definition spawn with model and effort")
            case("subagent_shell", True, "subagent git commit")
            case("apply_patch", True, "worker apply_patch on AGENTS.md", agent_id="smoke-agent", tool_input={"command": patch})
            case("root_shell", False, "git status", tool_input={"command": "git status"})
            case("spawn_custom", False, "orchestrator custom spawn without arguments")
        except KeyError as exc:
            return [], f"payload fixture lacks {exc}"
    return fails, None


def cli_argv(host):
    """The host's CLI as an argv list: TAGTEAM_CLI_<VENDOR> (a JSON list, as run.py reads it) or the PATH entry."""
    key, exe = ("CLAUDE", "claude") if host == "claude" else ("OPENAI", "codex")
    override = os.environ.get("TAGTEAM_CLI_" + key)
    if override:
        try:
            argv = json.loads(override)
            if isinstance(argv, list) and all(isinstance(x, str) for x in argv):
                return argv
        except ValueError:
            pass
    found = shutil.which(exe)
    return [found] if found else None


def codex_thread_id(stdout):
    """The thread (= session) id from the first {"type": "thread.started", "thread_id": ...} line of codex exec --json."""
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str) \
                and event["thread_id"].strip():
            return event["thread_id"].strip()
    return None


def live_check(target, host):
    """('ok'|'FAILED'|'unverified', text): run the real CLI once, as a marked worker session, and require that it
    succeeded and that the role record of THAT session exists (a worker, this host, written after the start).
    The session is named by the id we passed (claude --session-id) or the one codex exec --json reports in its
    thread.started event; without an id the result is unverified, never ok."""
    if host == "agy":
        return "unverified", "adapter not installed (planned for a later milestone)"
    base = cli_argv(host)
    if not base:
        return "unverified", f"the {host} CLI was not found"
    mod = load_installed_state(target)
    try:
        state = Path(mod.state_dir(str(target))) if mod else None
    except Exception:  # noqa: BLE001
        state = None
    if state is None:
        return "unverified", "state folder not computed"
    sessions = state / "sessions"
    sid = str(uuid.uuid4())
    prompt = "HARNESS ROLE: DELEGATE\n" + LIVE_PROMPT  # the marker makes the record say worker, unlike an ordinary session
    argv = base + (["-p", prompt, "--session-id", sid] if host == "claude"
                   else ["exec", "--json", "-s", "read-only", "--skip-git-repo-check", prompt])
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_DELEGATE_RUN"}
    if host != "claude":
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    started = time.time()
    try:
        p = subprocess.run(argv, cwd=str(target), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           env=env, timeout=240)
    except (OSError, subprocess.SubprocessError) as exc:
        return "FAILED", f"could not run {host}: {exc}"
    tail = ((p.stderr or p.stdout or "").strip().splitlines() or [""])[-1][:160]
    if p.returncode != 0:
        return "FAILED", f"{host} exited {p.returncode}. Last output: {tail}"
    if host != "claude":
        sid = codex_thread_id(p.stdout)
        if not sid:
            return "unverified", ("the session id was not found in the codex exec --json output (thread.started event), "
                                  "so no role record can be tied to this run")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", sid) + ".json"  # role_guard names the record after the session id
    try:
        rec = json.loads((sessions / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = None
    ts = rec.get("ts") if isinstance(rec, dict) else None
    if isinstance(rec, dict) and rec.get("role") == "worker" and rec.get("host") == host \
            and isinstance(ts, (int, float)) and started - 2 <= ts <= time.time() + 2:
        return "ok", f"role record {name} written in {sessions}"
    hint = "the tagteam hooks did not run; trust them in an interactive codex session (install.py --repair-hooks)" \
        if host == "codex" else "the tagteam hooks did not run"
    return "FAILED", f"{host} succeeded but no valid role record of this run appeared in {sessions}: {hint}. Last output: {tail}"


def check(target, source=None, host=None, live=False):
    source = source or Path(__file__).resolve().parent
    shown = host or "claude"
    print(f"check host={shown}" + ("" if host else " (no --host: claude handler check only)"))
    print("static:")
    errors, warnings, notes = static_checks(target, source, shown)
    for n in notes:
        print(f"  note     {n}")
    for w in warnings:
        print(f"  WARN     {w}")
    for e in errors:
        print(f"  ERROR    {e}")
    print("static: " + ("FAILED" if errors else "ok"))
    failed = bool(errors)
    # hook handler: fake payloads of the host's own format through the installed role_guard
    for name in CHECK_HOSTS:
        if name != shown:
            print(f"handler ({name}): unverified (run with --host {name})")
            continue
        if name == "agy":
            print("handler (agy): unverified (adapter not installed)")
        elif name == "claude":
            fails = smoke_test(target)
            for f in fails:
                print(f"  ERROR    {f}")
            failed |= bool(fails)
            print("handler (claude): " + ("FAILED" if fails else "ok"))
            cmd_fails, why = hook_command_test(target)
            for f in cmd_fails:
                print(f"  ERROR    {f}")
            failed |= bool(cmd_fails)
            print("hook commands (claude): " + (f"unverified ({why})" if why else "FAILED" if cmd_fails else "ok"))
        else:
            fails, why = handler_codex(target, source)
            for f in fails:
                print(f"  ERROR    {f}")
            failed |= bool(fails)
            print("handler (codex): " + (f"unverified ({why})" if why else "FAILED" if fails else "ok"))
    if live:
        status, text = live_check(target, shown)
        failed |= status == "FAILED"
        print(f"live ({shown}): {status} ({text})")
    else:
        print(f"live ({shown}): unverified (not requested; --live runs the real {shown} CLI once and costs tokens)")
    print("check: " + ("FAILED" if failed else "ok"))
    return 1 if failed else 0


# ---- --schedule / --unschedule: weekly model-list check -------------------------------
# Opt-in. The task runs the installed collector (no LLM); it never edits models.json.

COLLECTOR_REL = ".claude/tagteam/models_collect.py"
PROPOSAL_FILES = ("model-update.json", "model-update.applied", "model-update.log")  # the log: macOS job output
MARK_FALLBACK = "missed runs will not be re-run"


def run_cmd(argv, input_text=None):
    """Default command runner: returns (exit code, combined output)."""
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def task_name(target):
    return "tagteam-models-" + hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:8]


def task_xml(argv, root):
    from xml.sax.saxutils import escape
    cmd, args = escape('"' + argv[0] + '"'), escape(subprocess.list2cmdline(argv[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>tagteam: weekly model list check</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>2026-01-05T09:00:00</StartBoundary><Enabled>true</Enabled>
    <ScheduleByWeek><DaysOfWeek><Monday /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
  </CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{cmd}</Command><Arguments>{args}</Arguments>
    <WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def load_installed_state(target):
    """The installed state.py as a module (install.py has no state-folder logic of its own), or None.
    Loaded without writing bytecode, so the project tree gets no __pycache__ from install or --check."""
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("tagteam_installed_state", target / STATE_REL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 - missing or old install: no state module
        return None
    finally:
        sys.dont_write_bytecode = keep


def installed_state_dir(target):
    mod = load_installed_state(target)
    try:
        return Path(mod.state_dir(str(target))) if mod else None
    except Exception:  # noqa: BLE001 - nothing to clean
        return None


def migrate_state(target):
    """Move an old <git dir>/tagteam state folder to .tagteam/ (install and --update only). Never fails the install."""
    mod = load_installed_state(target)
    if mod is None or not hasattr(mod, "migrate"):
        return
    try:
        lines = mod.migrate(str(target))
    except Exception as exc:  # noqa: BLE001 - StateError: the old folder stays where it is
        print(f"warning: could not move the old state folder ({exc})", file=sys.stderr)
        return
    for line in lines:
        print("  state    " + line)


class CronReadError(Exception):
    pass


def env_runtime(path):
    """x of a '#!/usr/bin/env x' first line (an npm CLI needs node on PATH), else None."""
    try:
        with open(path, "rb") as f:
            first = f.readline(256).decode("utf-8", "replace").strip()
    except OSError:
        return None
    parts = first[2:].split() if first.startswith("#!") else []
    if len(parts) < 2 or os.path.basename(parts[0]) != "env":
        return None
    return next((t for t in parts[1:] if not t.startswith("-") and "=" not in t), None)


def cli_paths(target):
    """The vendor CLIs (claude, codex, agy) as found on PATH now. A launchd or cron job does not get the shell's
    PATH, so the job is given their folders explicitly."""
    names = ["claude", "codex", "agy"]
    try:
        vendors = json.loads((target / ".claude/tagteam/models.json").read_text(encoding="utf-8"))["vendors"]
        names = [v["cli"] for v in vendors.values() if isinstance(v, dict) and v.get("cli")] or names
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    return [p for p in (shutil.which(n) for n in names) if p]


def probe_cli(argv, path):
    """(exit code, first output line) of argv run with only this PATH (and HOME), as a launchd job would, in an
    empty temporary folder (a CLI wrapper may write next to its working directory)."""
    env = {"PATH": path, "HOME": os.environ.get("HOME", "")}
    try:
        with tempfile.TemporaryDirectory() as cwd:
            p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, ((p.stdout or p.stderr).strip().splitlines() or [""])[0][:120]


class Scheduler:
    def __init__(self, target, plat=None, runner=run_cmd, dry=False, python=None, home=None, uid=None, clis=None, probe=None):
        self.target, self.dry, self.runner = target, dry, runner
        self.plat = plat or {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
        self.name = task_name(target)
        self.argv = [python or sys.executable, str(target / COLLECTOR_REL), "--project", str(target)]
        self.clis = clis  # CLI executables; None: look them up when the job is registered
        self.probe = probe or probe_cli  # looked up late: a test replaces probe_cli
        self.uid = uid
        self.plist = Path(home or Path.home()) / "Library/LaunchAgents" / f"net.tagteam.{self.name}.plist"

    def run(self, argv, input_text=None):
        print("  " + ("[dry-run] " if self.dry else "$ ") + subprocess.list2cmdline(argv))
        return (0, "") if self.dry else self.runner(argv, input_text)

    def _gui(self):
        return "gui/%d" % (self.uid if self.uid is not None else os.getuid())

    def _cron(self):
        """Read the user crontab once: (all lines, lines without our marked line)."""
        rc, text = self.runner(["crontab", "-l"])
        if rc != 0:
            if "no crontab" not in text.lower():
                raise CronReadError("cannot read the crontab (crontab -l: %s); nothing was changed" % text.strip()[:200])
            text = ""
        mark = "# " + self.name
        lines = text.splitlines()
        return lines, [ln for ln in lines if not ln.rstrip().endswith(mark)]

    def cli_files(self):
        return self.clis if self.clis is not None else cli_paths(self.target)

    def job_path(self):
        """PATH of a macOS/Linux job: the CLI folders, the folders of the runtime a CLI script asks for with
        '#!/usr/bin/env x' (node), the interpreter's folder, then the system folders. Short and reproducible; the
        shell's whole PATH is not copied."""
        files = self.cli_files()
        runtimes = [shutil.which(r) for r in dict.fromkeys(env_runtime(f) for f in files) if r]
        dirs = [os.path.dirname(f) for f in files] + [os.path.dirname(r) for r in runtimes if r]             + [os.path.dirname(self.argv[0])] + SYSTEM_PATH.split(":")
        return ":".join(dict.fromkeys(d for d in dirs if d))

    def log_path(self):
        state = installed_state_dir(self.target)
        return state / "model-update.log" if state else None

    def schedule(self):
        print(f"task {self.name} ({self.plat}): {subprocess.list2cmdline(self.argv)}")
        if self.plat in ("macos", "linux"):
            path = self.job_path()
            print("  PATH     " + path + (" (linux: cron, unverified)" if self.plat == "linux" else ""))
            for f in self.cli_files():  # the job's PATH must be enough to start each CLI, not only to find it
                if self.dry:  # a preview starts nothing
                    print(f"  cli      [dry-run] would run: {f} --version with that PATH")
                    continue
                rc, line = self.probe([f, "--version"], path)
                print(f"  cli      {os.path.basename(f)} --version with that PATH: " + ("ok" if rc == 0 else f"WARN failed ({rc}): {line}"))
        try:
            rc = getattr(self, "_add_" + self.plat)()
        except CronReadError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        print(("would schedule" if self.dry else "scheduled") if rc == 0 else "scheduling FAILED", self.name)
        return int(rc != 0)

    def _add_windows(self):
        xml = task_xml(self.argv, self.target)
        if self.dry:
            print(xml)
        tmp = Path(tempfile.gettempdir()) / f"{self.name}.xml"
        try:
            if not self.dry:
                tmp.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
            rc, msg = self.run(["schtasks", "/Create", "/TN", self.name, "/XML", str(tmp), "/F"])
        finally:
            if not self.dry and tmp.exists():
                tmp.unlink()
        if rc != 0:
            print(f"note: /XML registration failed ({msg.strip()[:200]}); using /SC WEEKLY, {MARK_FALLBACK}")
            rc, _ = self.run(["schtasks", "/Create", "/TN", self.name, "/SC", "WEEKLY", "/D", "MON", "/ST", "09:00",
                              "/TR", subprocess.list2cmdline(self.argv), "/F"])
        return rc

    def _add_macos(self):
        job = {"Label": "net.tagteam." + self.name, "ProgramArguments": self.argv,
               "StartCalendarInterval": {"Weekday": 1, "Hour": 9, "Minute": 0},
               "WorkingDirectory": str(self.target),
               "EnvironmentVariables": {"PATH": self.job_path()}}  # launchd's own PATH is /usr/bin:/bin:/usr/sbin:/sbin
        log = self.log_path()
        if log:  # without these a failing run leaves no trace anywhere
            job["StandardOutPath"] = job["StandardErrorPath"] = str(log)
        data = plistlib.dumps(job)
        if self.dry:
            print(data.decode("utf-8"))
        else:
            if log:
                log.parent.mkdir(parents=True, exist_ok=True)  # launchd does not create the folder of its log
            if self.plist.exists():
                self.run(["launchctl", "bootout", f"{self._gui()}/net.tagteam.{self.name}"])
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            self.plist.write_bytes(data)
        return self.run(["launchctl", "bootstrap", self._gui(), str(self.plist)])[0]

    def _add_linux(self):
        # cron's PATH is as short as launchd's; % is special in crontab
        line = "0 9 * * 1 %s # %s" % (("PATH=%s %s" % (shlex.quote(self.job_path()), shlex.join(self.argv))).replace("%", "\\%"),
                                      self.name)
        return self.run(["crontab", "-"], "\n".join(self._cron()[1] + [line]) + "\n")[0]

    def unschedule(self):
        try:
            rc = getattr(self, "_del_" + self.plat)()
        except CronReadError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        print(("removed " if rc == 0 else "not registered (nothing to remove): ") + self.name)
        state = installed_state_dir(self.target)
        for fn in PROPOSAL_FILES if state else ():
            if (state / fn).exists():
                print(("  [dry-run] would delete " if self.dry else "  deleted ") + str(state / fn))
                if not self.dry:
                    (state / fn).unlink()
        return 0

    def _del_windows(self):
        return self.run(["schtasks", "/Delete", "/TN", self.name, "/F"])[0]

    def _del_macos(self):
        rc = self.run(["launchctl", "bootout", f"{self._gui()}/net.tagteam.{self.name}"])[0]
        if self.plist.exists():
            if not self.dry:
                self.plist.unlink()
            rc = 0
        return rc

    def _del_linux(self):
        old, keep = self._cron()
        if len(keep) == len(old):
            return 1
        return self.run(["crontab", "-"], "\n".join(keep) + "\n" if keep else "")[0]


def main(argv=None, runner=run_cmd, plat=None):
    ap = argparse.ArgumentParser(description="Install, update or check tagteam in a project.")
    ap.add_argument("target")
    ap.add_argument("--update", action="store_true", help="re-apply files, preserving local content")
    ap.add_argument("--check", action="store_true", help="check environment, files, state folder and hook handling")
    ap.add_argument("--host", choices=CHECK_HOSTS, help="with --check: the host to check (default: claude)")
    ap.add_argument("--live", action="store_true", help="with --check: also run the real CLI once (costs tokens)")
    ap.add_argument("--repair-hooks", action="store_true",
                    help="bring the tagteam entries of .codex/hooks.json back to the template (preview; --yes writes)")
    ap.add_argument("--schedule", action="store_true", help="register the weekly model-list check (installs first if needed)")
    ap.add_argument("--unschedule", action="store_true", help="remove the weekly check and its proposal files")
    ap.add_argument("--dry-run", action="store_true", help="with --schedule/--unschedule: print, change nothing")
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    a = ap.parse_args(argv)
    if a.dry_run and not (a.schedule or a.unschedule):
        ap.error("--dry-run needs --schedule or --unschedule")
    if a.schedule and a.unschedule:
        ap.error("--schedule and --unschedule exclude each other")
    if (a.host or a.live) and not a.check:
        ap.error("--host and --live need --check")
    if a.repair_hooks and (a.check or a.schedule or a.unschedule or a.update):
        ap.error("--repair-hooks stands alone (add --yes to write)")
    target, source = Path(a.target).resolve(), Path(a.source).resolve()
    sched = Scheduler(target, plat, runner, a.dry_run)
    if a.check:
        return check(target, source, a.host, a.live)
    if a.repair_hooks:
        return repair_hooks(target, source, a.yes)
    if a.unschedule:
        return sched.unschedule()
    if not (source / "harness").is_dir():
        print(f"error: no harness/ directory under source {source}", file=sys.stderr)
        return 1
    interactive = sys.stdin.isatty() and not a.yes
    if not (a.schedule and not a.update and (target / COLLECTOR_REL).is_file()) and not a.dry_run:
        target.mkdir(parents=True, exist_ok=True)
        rep = install(target, source, interactive)
        print(("Updated" if a.update else "Installed") + f" tagteam in {target}")
        rep.print()
        migrate_state(target)
        ensure_bench(target)
        ensure_available(target)
    if not a.schedule and interactive and not a.update:
        a.schedule = input("Register a weekly model-list check (scheduled task, no LLM)? [y/N]: ").strip().lower() == "y"
    if a.schedule:
        if not a.dry_run and not (target / COLLECTOR_REL).is_file():
            print(f"error: {COLLECTOR_REL} is not installed; cannot schedule", file=sys.stderr)
            return 1
        return sched.schedule()
    return 0


if __name__ == "__main__":
    sys.exit(main())
