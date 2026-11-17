#!/usr/bin/env python3
"""Router: pick model, effort and launch method for a role.

  route.py --role design|implement|review|read|translate [--host claude|codex]
           [--author-vendor V] [--grade S|A|B|C] [--orchestrator-model M]
           [--orchestrator-effort E] [--json]
  route.py --mark-exhausted VENDOR --evidence TEXT [--until ISO8601]
  route.py --clear-exhausted VENDOR

Exit: 0 chosen, 2 usage or config error, 3 no independent reviewer (incomplete),
4 no model available. Config dir: next to this script (TAGTEAM_CONFIG_DIR in tests).
State dir (exhausted.json): role_guard.state_dir (TAGTEAM_STATE_DIR in tests).
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
GRADES = {"C": 0, "B": 1, "A": 2, "S": 3}
ROLES = ["design", "implement", "review", "read", "translate"]
TS = "%Y-%m-%dT%H:%M:%SZ"


class ConfigError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(text):
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError("bad timestamp: %s" % text)
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def load_json(path, required):
    if not os.path.exists(path):
        if required:
            raise ConfigError("missing %s" % path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s is not a JSON object" % path)
    return data


def config_dir():
    return os.environ.get("TAGTEAM_CONFIG_DIR") or os.path.dirname(os.path.abspath(__file__))


def exhausted_path():
    if not os.environ.get("TAGTEAM_STATE_DIR"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import role_guard
        base = role_guard.state_dir({"cwd": os.getcwd()})
    else:
        base = os.environ["TAGTEAM_STATE_DIR"]
    return os.path.join(base, "exhausted.json")


def read_exhausted(path):
    """Active entries only (until in the future). Broken file = empty, with a warning."""
    try:
        data = load_json(path, False)
    except ConfigError as exc:
        sys.stderr.write("route.py warning: %s (treated as empty)\n" % exc)
        return {}
    vendors = data.get("vendors", {})
    if not isinstance(vendors, dict):
        sys.stderr.write("route.py warning: %s: 'vendors' is not an object (treated as empty)" % path + chr(10))
        return {}
    out = {}
    for v, e in vendors.items():
        try:
            if isinstance(e, dict) and parse_ts(e["until"]) > now():
                out[v] = e
        except (ConfigError, KeyError, TypeError, AttributeError):
            sys.stderr.write("route.py warning: ignoring bad exhausted entry for %s" % v + chr(10))
    return out


def write_exhausted(path, vendors):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "vendors": vendors}, f, indent=1)
    os.replace(tmp, path)


def read_bench(cdir):
    """bench.local.json -> {model id: [entries]}. Missing or broken file = empty."""
    try:
        with open(os.path.join(cdir, "bench.local.json"), encoding="utf-8") as f:
            models = json.load(f).get("models")
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: v for k, v in models.items() if isinstance(v, list)} if isinstance(models, dict) else {}


def num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def model_efforts(model):
    """Efforts a model offers: the keys of its grades table, in EFFORTS order."""
    return [e for e in EFFORTS if e in (model.get("grades") or {})]


def clamp_effort(model, want):
    efforts = model_efforts(model)
    if want in efforts:
        return want, False
    lower = [e for e in efforts if EFFORTS.index(e) < EFFORTS.index(want)]
    pool = lower or efforts
    return (max if lower else min)(pool, key=EFFORTS.index), True


def default_host():
    return "claude" if os.environ.get("CLAUDECODE") == "1" else "codex"


def cli_present(vendor, cfg):
    return bool(os.environ.get("TAGTEAM_CLI_" + vendor.upper()) or shutil.which(cfg["cli"]))


class Router:
    def __init__(self, models, local, exhausted, bench=None):
        self.m, self.local, self.exh, self.bench_data = models, local, exhausted, bench or {}
        self.order = {mid: i for i, mid in enumerate(models["models"])}

    def bench(self, mid, metric, effort):
        """Artificial Analysis value at exactly this effort, else None (other efforts would skew comparisons)."""
        for b in self.bench_data.get(mid) or []:
            if isinstance(b, dict) and b.get("source") == "artificial_analysis" and b.get("metric") == metric                     and b.get("effort") == effort and num(b.get("value")) is not None:
                return b["value"]
        return None

    def plan(self, vendor):
        return (self.local.get("plans") or {}).get(vendor, "pro")

    def usable(self, mid, effort):
        model = self.m["models"][mid]
        v = model["vendor"]
        if self.plan(v) == "none":
            return False
        enabled = ((self.local.get("models") or {}).get(mid) or {}).get("enabled")
        if enabled is not None:
            return bool(enabled)
        avail = (self.local.get("available") or {}).get(v)
        if avail:
            return self.m["vendors"][v]["model_arg"].format(model=mid, effort=effort) in avail.get("ids", [])
        return True

    def model_effort(self, mid, spec, grade):
        """Effort for a user-pinned model: the user effort (clamped), else its lowest effort at the grade."""
        model = self.m["models"][mid]
        if spec.get("user_effort"):
            return clamp_effort(model, spec["user_effort"])
        efforts = model_efforts(model)
        for ok in (lambda g: g == grade, lambda g: GRADES[g] > GRADES[grade]):
            hit = [e for e in efforts if ok(model["grades"][e])]
            if hit:
                return hit[0], False
        return efforts[-1], False

    def spec(self, role, grade):
        spec = dict(self.m["roles"][role])
        lr = (self.local.get("roles") or {}).get(role) or {}
        for key in ("model", "vendor", "grade"):
            if lr.get(key):
                spec["user_" + key] = lr[key]
                if key == "grade":
                    spec["grade"] = lr[key]
        if lr.get("effort"):
            spec["user_effort"] = lr["effort"]
        if grade:
            spec["grade"] = grade
        for key, table in (("user_model", self.m["models"]), ("user_vendor", self.m["vendors"]), ("grade", GRADES)):
            if spec.get(key) and spec[key] not in table:
                raise ConfigError("unknown %s: %s" % (key, spec[key]))
        if spec.get("user_effort") and spec["user_effort"] not in EFFORTS:
            raise ConfigError("unknown effort: %s" % spec["user_effort"])
        return spec

    def vendor_order(self, spec, host, author, reason):
        pinned = [spec["user_vendor"]] if spec.get("user_vendor") else []
        explicit = spec.get("vendors")
        rest = list(explicit or self.m["hosts"][host])
        if not explicit:
            free = [v for v in rest if self.plan(v) == "free" and v not in pinned]
            for v in free:
                reason.append("plan_free_last:" + v)
            rest = [v for v in rest if v not in free] + free
        order = []
        for v in pinned + rest:
            if v in order or self.plan(v) == "none":
                continue
            if author and v == author:
                reason.append("author_vendor_excluded:" + v)
                continue
            order.append(v)
        return order

    def pick(self, role, spec, vendor, grade, cap=(None, None), native=False):
        """Best (model, effort, clamped) of this vendor whose grades[effort] is the role grade, or None.

        Tiers: 0 cost known and within caps[grade] -> highest index; 1 no cost, index known -> highest index;
        2 cost above the cap -> lowest cost; 3 nothing known -> defaults[vendor][grade], else models.json order
        at the lowest effort. A later tier is used only when the earlier ones are empty.
        """
        want = spec.get("user_effort")
        cands = []
        for mid, m in self.m["models"].items():
            if m["vendor"] != vendor or m.get("candidate"):
                continue
            for effort in model_efforts(m):
                if m["grades"][effort] == grade and (not want or effort == want) and self.usable(mid, effort):
                    cands.append((mid, effort))
        if native and cap[0] in self.m["models"]:
            within = [c for c in cands if not self.over_cap(c[0], c[1], *cap)]
            cands = within or cands
        if not cands:
            return None
        limit = (self.m.get("grading") or {}).get("caps", {}).get(grade)
        tiers = ([], [], [], [])
        for c in cands:
            idx, cost = self.bench(c[0], "index", c[1]), self.bench(c[0], "cost_per_task", c[1])
            order = self.order[c[0]]
            if cost is not None and limit is not None and cost <= limit:
                tiers[0].append(((idx is None, -(idx or 0), cost, order), c))
            elif cost is None and idx is not None:
                tiers[1].append(((-idx, order), c))
            elif cost is not None and limit is not None:
                tiers[2].append(((cost, -(idx or 0), order), c))
            else:
                tiers[3].append(((order, EFFORTS.index(c[1])), c))
        for tier in tiers[:3]:
            if tier:
                return min(tier, key=lambda t: t[0])[1] + (False,)
        wanted = ((self.m.get("defaults") or {}).get(vendor) or {}).get(grade) or []
        for entry in wanted:
            mid, _, effort = entry.rpartition(":")
            if (mid, effort) in cands:
                return mid, effort, False
        return min(tiers[3], key=lambda t: t[0])[1] + (False,)

    def over_cap(self, mid, effort, orch_model, orch_effort):
        """True when a native Claude session cannot take this model or effort.

        A model listed earlier in models.json than the orchestrator's (same vendor) counts as stronger."""
        model, oe = self.m["models"][mid], orch_effort or os.environ.get("CLAUDE_EFFORT")
        om = self.m["models"].get(orch_model)
        if om and om["vendor"] == model["vendor"] and self.order[mid] < self.order[orch_model]:
            return True
        return oe in EFFORTS and EFFORTS.index(effort) > EFFORTS.index(oe)

    def needs_run_cli(self, mid, effort, host, cap):
        """The launch will go through run.py (not native) but the vendor CLI is missing."""
        v = self.m["models"][mid]["vendor"]
        cfg = self.m["vendors"][v]
        return cfg["host"] == host and host == "claude" and self.over_cap(mid, effort, *cap)             and not cli_present(v, cfg)

    def route(self, role, host, author=None, grade=None, orch_model=None, orch_effort=None):
        spec = self.spec(role, grade)
        grade = spec["grade"]
        reason, notes = [], []
        cap = (orch_model, orch_effort)
        choice = None
        um = spec.get("user_model")
        if um:
            vendor = self.m["models"][um]["vendor"]
            effort, clamped = self.model_effort(um, spec, grade)
            if not self.usable(um, effort):
                reason.append("override_unusable")
            elif vendor in self.exh:
                reason.append("override_exhausted")
            elif role == "review" and vendor == author:
                reason.append("override_same_vendor_as_author")
            elif self.needs_run_cli(um, effort, host, cap):
                reason.append("cli_missing:" + vendor)
            else:
                choice = (um, effort, clamped)
                reason.append("user_override")
                pinned = (self.m["models"][um].get("grades") or {}).get(effort)
                if role == "review" and pinned != grade:
                    reason.append("override_grade_mismatch")
                    notes.append("user-pinned reviewer is grade %s, the author's grade is %s" % (pinned, grade))
        if choice is None:
            order = self.vendor_order(spec, host, author, reason)
            expected = order[0] if order else None
            for v in order:
                cfg = self.m["vendors"][v]
                if v in self.exh:
                    reason.append("exhausted_skip:" + v)
                    until = parse_ts(self.exh[v]["until"]).strftime("%H:%M")
                    notes.append("%s exhausted until %s UTC, substituted" % (v, until))
                    continue
                if cfg["host"] != host and not cli_present(v, cfg):
                    reason.append("cli_missing:" + v)
                    notes.append("%s CLI missing, substituted" % v)
                    continue
                choice = self.pick(role, spec, v, grade, cap, cfg["host"] == host and host == "claude")
                if choice and self.needs_run_cli(choice[0], choice[1], host, cap):
                    reason.append("cli_missing:" + v)
                    notes.append("%s CLI missing, substituted" % v)
                    choice = None
                    continue
                if choice:
                    if v != expected:
                        reason.insert(0, "substituted:%s\u2192%s" % (expected, v))
                    break
            if choice is None:
                incomplete = role == "review"
                return {"status": "incomplete" if incomplete else "none", "role": role, "reason": reason,
                        "notice": "%s: %s." % (role, "no independent reviewer available, review incomplete"
                                               if incomplete else "no usable model found")}, 3 if incomplete else 4
            if not any(r.startswith("substituted") for r in reason):
                reason.insert(0, "default")
        mid, effort, clamped = choice
        model = self.m["models"][mid]
        vendor, cfg = model["vendor"], self.m["vendors"][model["vendor"]]
        if clamped:
            reason.append("effort_clamped")
        grade = model["grades"][effort]
        known = any(self.bench(mid, metric, effort) is not None for metric in ("index", "cost_per_task"))
        reason.append("bench:local" if known else "bench:none")
        native = cfg["host"] == host
        extra = ""
        if native and host == "claude":
            if self.over_cap(mid, effort, orch_model, orch_effort):
                native = False
                reason.append("native_model_cap")
                extra = " Orchestrator cap exceeded, launching via run.py."
            elif not (orch_model in self.m["models"] and (orch_effort or os.environ.get("CLAUDE_EFFORT")) in EFFORTS):
                extra = " Check orchestrator cap."
        if native:
            launch = {"kind": "native"}
        else:
            cmd = ["python", ".claude/tagteam/run.py", "--vendor", vendor, "--model", mid,
                   "--effort", effort, "--prompt-file", "<PROMPT_FILE>"]
            if self.m["roles"][role].get("read_only"):
                cmd.append("--read-only")
            launch = {"kind": "run", "command": cmd}
        notice = "%s -> %s (%s, %s, grade %s) via %s." % (
            role, mid, effort, vendor, grade, "native session" if native else "run.py")
        if role == "review":
            notice += " Reviewer differs from author vendor %s." % author
        for n in notes:
            notice += " " + n + "."
        notice += extra
        return {"status": "ok", "role": role, "model": mid, "effort": effort, "vendor": vendor,
                "grade": grade, "launch": launch, "reason": reason, "notice": notice}, 0


def mark(args, models, path):
    v = args.mark_exhausted
    if v not in models["vendors"]:
        raise ConfigError("unknown vendor: %s" % v)
    if not args.evidence:
        raise ConfigError("--evidence is required with --mark-exhausted")
    t = now()
    until = parse_ts(args.until) if args.until else t + timedelta(minutes=models["vendors"][v].get("cooldown_minutes", 60))
    vendors = read_exhausted(path)
    vendors[v] = {"since": t.strftime(TS), "until": until.strftime(TS), "evidence": args.evidence[:200], "source": "manual"}
    write_exhausted(path, vendors)
    print("%s exhausted until %s" % (v, vendors[v]["until"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pick model, effort and launch method for a role.")
    ap.add_argument("--role", choices=ROLES)
    ap.add_argument("--host", choices=["claude", "codex"])
    ap.add_argument("--author-vendor")
    ap.add_argument("--grade", choices=list(GRADES))
    ap.add_argument("--orchestrator-model")
    ap.add_argument("--orchestrator-effort", choices=EFFORTS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mark-exhausted", metavar="VENDOR")
    ap.add_argument("--clear-exhausted", metavar="VENDOR")
    ap.add_argument("--evidence")
    ap.add_argument("--until")
    args = ap.parse_args(argv)
    try:
        if not (args.role or args.mark_exhausted or args.clear_exhausted):
            ap.error("one of --role, --mark-exhausted, --clear-exhausted is required")
        cdir = config_dir()
        models = load_json(os.path.join(cdir, "models.json"), True)
        if args.mark_exhausted:
            mark(args, models, exhausted_path())
            return 0
        if args.clear_exhausted:
            path = exhausted_path()
            vendors = read_exhausted(path)
            print("cleared %s" % args.clear_exhausted if vendors.pop(args.clear_exhausted, None) else
                  "no active entry for %s" % args.clear_exhausted)
            write_exhausted(path, vendors)
            return 0
        if args.role == "review" and not args.author_vendor:
            ap.error("--author-vendor is required with --role review")
        if args.author_vendor and args.author_vendor not in models["vendors"]:
            raise ConfigError("unknown vendor: %s" % args.author_vendor)
        local = load_json(os.path.join(cdir, "models.local.json"), False)
        router = Router(models, local, read_exhausted(exhausted_path()), read_bench(cdir))
        result, code = router.route(args.role, args.host or default_host(), args.author_vendor, args.grade,
                                    args.orchestrator_model, args.orchestrator_effort)
    except (ConfigError, KeyError, ValueError) as exc:
        sys.stderr.write("route.py: %s: %s\n" % (type(exc).__name__, exc))
        return 2
    print(json.dumps(result, indent=1) if args.json else result["notice"])
    return code


if __name__ == "__main__":
    sys.exit(main())
