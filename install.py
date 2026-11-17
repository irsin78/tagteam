#!/usr/bin/env python3
"""tagteam installer: install / update / check. Standard library only (Python 3.9+).

    python install.py <target> [--update] [--check] [--source DIR] [--yes]
    python install.py <target> --schedule | --unschedule [--dry-run]
"""
import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import shlex
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


def is_git_repo(target):
    return (target / ".git").exists()


LOCAL_IGNORES = (".claude/tagteam/models.local.json", ".claude/tagteam/bench.local.json")
BENCH_REL = Path(".claude/tagteam/bench.local.json")


def ensure_gitignore(rep, target, git):
    """Machine-local files are always ignored; .tagteam/ (state) only where the project is not a git repo."""
    gi = target / ".gitignore"
    old = gi.read_text(encoding="utf-8") if gi.exists() else ""
    have = {ln.strip().rstrip("/") for ln in old.splitlines()}
    want = list(LOCAL_IGNORES) + ([] if git else [".tagteam/"])
    add = [w for w in want if w.rstrip("/") not in have]
    if not add:
        return
    sep = "" if old.endswith("\n") or not old else "\n"
    put(rep, target, Path(".gitignore"), (old + sep + "".join(w + "\n" for w in add)).encode("utf-8"))


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
    ensure_gitignore(rep, target, is_git_repo(target))
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


# ---- --schedule / --unschedule: weekly model-list check -------------------------------
# Opt-in. The task runs the installed collector (no LLM); it never edits models.json.

COLLECTOR_REL = ".claude/tagteam/models_collect.py"
PROPOSAL_FILES = ("model-update.json", "model-update.applied")
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


def installed_state_dir(target):
    """State dir via the installed role_guard (install.py has no state-dir logic of its own)."""
    guard = target / GUARD_REL
    try:
        spec = importlib.util.spec_from_file_location("tagteam_installed_guard", guard)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return Path(mod.state_dir({"cwd": str(target)}))
    except Exception:  # noqa: BLE001 - missing or old guard: nothing to clean
        return None


class CronReadError(Exception):
    pass


class Scheduler:
    def __init__(self, target, plat=None, runner=run_cmd, dry=False, python=None, home=None, uid=None):
        self.target, self.dry, self.runner = target, dry, runner
        self.plat = plat or {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
        self.name = task_name(target)
        self.argv = [python or sys.executable, str(target / COLLECTOR_REL), "--project", str(target)]
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

    def schedule(self):
        print(f"task {self.name} ({self.plat}): {subprocess.list2cmdline(self.argv)}")
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
        data = plistlib.dumps({"Label": "net.tagteam." + self.name, "ProgramArguments": self.argv,
                               "StartCalendarInterval": {"Weekday": 1, "Hour": 9, "Minute": 0},
                               "WorkingDirectory": str(self.target)})
        if self.dry:
            print(data.decode("utf-8"))
        else:
            if self.plist.exists():
                self.run(["launchctl", "bootout", f"{self._gui()}/net.tagteam.{self.name}"])
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            self.plist.write_bytes(data)
        return self.run(["launchctl", "bootstrap", self._gui(), str(self.plist)])[0]

    def _add_linux(self):
        line = "0 9 * * 1 %s # %s" % (shlex.join(self.argv).replace("%", "\\%"), self.name)  # % is special in crontab
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
    ap.add_argument("--check", action="store_true", help="check environment and hook smoke test")
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
    target, source = Path(a.target).resolve(), Path(a.source).resolve()
    sched = Scheduler(target, plat, runner, a.dry_run)
    if a.check:
        return check(target)
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
