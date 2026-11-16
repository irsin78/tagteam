#!/usr/bin/env python3
"""tagteam installer: install / update / check. Standard library only (Python 3.9+).

    python install.py <target> [--update] [--check] [--source DIR] [--yes]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
                if s == state:
                    print(f"  {state:8} {p}")


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


def copy_tree(rep, src_dir, target, rel_dir, pattern="*"):
    """Copy files matching pattern (recursively) from src_dir to target/rel_dir."""
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob(pattern)):
        if f.is_file() and ".local." not in f.name and "__pycache__" not in f.parts:
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
        if i >= 0:
            head = text[:j] if j >= 0 else text.rstrip("\n") + "\n\n"
            text = head + old[i:]
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
            ans = input(f"Plan tier for {v} ({'/'.join(TIERS)}) [none]: ").strip().lower() or "none"
            if ans in TIERS:
                plans[v] = ans
                break
    return plans


def is_git_repo(target):
    return (target / ".git").exists()


def ensure_gitignore(rep, target):
    gi = target / ".gitignore"
    old = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if any(ln.strip().rstrip("/") == ".tagteam" for ln in old.splitlines()):
        return
    sep = "" if old.endswith("\n") or not old else "\n"
    put(rep, target, Path(".gitignore"), (old + sep + ".tagteam/\n").encode("utf-8"))


def install(target, source, interactive):
    rep = Report()
    h = source / "harness"
    if (h / "claude" / "settings.json").is_file():
        merge_settings(rep, h / "claude" / "settings.json", target)
    copy_tree(rep, h / "claude" / "agents", target, ".claude/agents", "*.md")
    copy_tree(rep, h / "claude" / "skills", target, ".claude/skills")
    copy_tree(rep, h / "tagteam", target, ".claude/tagteam")
    hooks = h / "codex" / "hooks.json"
    if hooks.is_file():
        existed = (target / ".codex/hooks.json").exists()
        data = localize_hooks(hooks, target / ".claude" / "tagteam" / "role_guard.py")
        put(rep, target, Path(".codex/hooks.json"), data, keep_existing=True)
        if existed:
            print("note: .codex/hooks.json already exists and was kept (machine-local trust).")
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
    if not is_git_repo(target):
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


def check(target):
    errors, warnings = [], []
    for tool in ("python", "git", "claude", "codex", "agy"):
        path = shutil.which(tool) or (shutil.which("python3") if tool == "python" else None)
        if not path:
            warnings.append(f"{tool}: missing on PATH")
            continue
        print(f"  found    {tool}: {path}")
        if os.name == "nt" and "windowsapps" in path.lower():
            warnings.append(f"{tool}: resolves to a WindowsApps alias stub ({path})")
    errors += smoke_test(target)
    for w in warnings:
        print(f"  WARN     {w}")
    for e in errors:
        print(f"  ERROR    {e}")
    print("check: " + ("FAILED" if errors else "ok"))
    return 1 if errors else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install, update or check tagteam in a project.")
    ap.add_argument("target")
    ap.add_argument("--update", action="store_true", help="re-apply files, preserving local content")
    ap.add_argument("--check", action="store_true", help="check environment and hook smoke test")
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    a = ap.parse_args(argv)
    target, source = Path(a.target).resolve(), Path(a.source).resolve()
    if a.check:
        return check(target)
    if not (source / "harness").is_dir():
        print(f"error: no harness/ directory under source {source}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    interactive = sys.stdin.isatty() and not a.yes
    rep = install(target, source, interactive)
    print(("Updated" if a.update else "Installed") + f" tagteam in {target}")
    rep.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
