#!/usr/bin/env python3
"""Router: pick model, effort and launch method for a role.

Options: `route.py --help`. Launch kinds (session, subagent, run) and what each host does with them: skill
`cycle`, hosts.md. Grading and routing rules: docs/design.md, section 7.

A review needs the author's grade (--grade, or --author-model with --author-effort); on --host agy it is at least A.
An orchestrator in models.json "orchestrators" (agy's built-in model) is compared as its `compare_as` pair and takes no
effort. Tasks (models.local.json "tasks") layer a per-task preference over the role's rules.

Exit: 0 chosen, 2 usage or config error, 3 no independent reviewer (incomplete), 4 no model available.
Maker vs pool: a model's `maker` (default: its vendor) is who built it, the independence unit of a review; its `pool`
(default: its vendor) is the quota it draws on. exhausted.json keys are pool names. A model with `fallback_only` (agy's
Claude models) is chosen only when the pool of the vendor the role wants is exhausted (see Router.pick, standin). The one exception, like `candidate`: a user or task pin of the model itself (models.local.json
roles.<role>.model or tasks.<name>.model) is honored; its pool and maker checks still apply. A `vendor` pin never selects it.
Config dir: next to this script (TAGTEAM_CONFIG_DIR in tests). Install root for the agent definitions: three
folders above this script (TAGTEAM_ROOT in tests). State dir (exhausted.json): state.state_dir (TAGTEAM_STATE_DIR in tests).
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

try:
    import tomllib  # Python 3.11+; the harness also supports 3.9, which gets the small parser below
except ImportError:
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402  (same folder)

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
GRADES = {"C": 0, "B": 1, "A": 2, "S": 3}
ROLES = ["design", "implement", "review", "read", "translate"]
# Roles that go to the host vendor's models: another vendor only through exhaustion, a task or a role pin with `hosts`.
PINNED_ROLES = ("design", "implement", "read")
TS = "%Y-%m-%dT%H:%M:%SZ"
# Vendors whose plan is inferred from their CLI's available list when models.local.json names none (see Router.plan): a fresh
# list with an id of a min_plan=ultra model means ultra, one with a Pro-class id pro, any other free; no fresh list at all
# means free. Claude and Codex are judged by availability alone and count as pro when no plan is named.
INFER_FREE = ("google",)
# Plan levels, lowest first. models.local.json plans.<vendor> takes none|free|pro|ultra|max; `max` reads as ultra. A model's
# optional `min_plan` (free|pro|ultra, default free) is the lowest plan that offers it.
PLAN_LEVELS = {"none": 0, "free": 1, "pro": 2, "ultra": 3}
PLAN_ALIASES = {"max": "ultra"}
PRO_GRADE = "A"  # a model that reaches this grade at some effort is Pro-class
AVAILABLE_MAX_AGE = 14  # days: an `available` list checked longer ago (two missed weekly checks) or with no readable date is unknown
HOSTS = ["claude-desktop", "claude", "codex", "agy"]


class ConfigError(Exception):
    pass


def model_maker(model):
    return model.get("maker") or model["vendor"]


def model_pool(model):
    return model.get("pool") or model["vendor"]


def model_cli_id(mid, model):
    return model.get("cli_id") or mid


def min_plan(model):
    """The lowest plan that offers a model: free, pro or ultra (default free)."""
    return model.get("min_plan") or "free"


def pool_cooldowns(models):
    """{pool name: cooldown minutes}: every vendor (its own pool) and every `pool` a model names."""
    out = {v: cfg.get("cooldown_minutes", 60) for v, cfg in models["vendors"].items()}
    for m in models["models"].values():
        out.setdefault(model_pool(m), models["vendors"][m["vendor"]].get("cooldown_minutes", 60))
    return out


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
    return os.path.join(state.state_dir(), "exhausted.json")


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
    state.ensure_writable(os.path.dirname(path))  # StateError names the folder and the fix
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
    """A guess: the notice shows host=<value>, so a wrong guess is visible.

    Codex and agy started from Claude inherit CLAUDECODE, so the agy variable is checked first."""
    if os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "agy"
    if os.environ.get("CLAUDECODE") == "1":
        return "claude-desktop" if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop" else "claude"
    return "codex"


def host_family(host):
    """The vendor-side name of a host: claude-desktop is a Claude host like claude."""
    return "claude" if host == "claude-desktop" else host


def root_dir():
    return os.environ.get("TAGTEAM_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def toml_top_level(text):
    """The top-level `key = "string"` assignments of a TOML document (tomllib when available).

    Lines inside multi-line strings (developer_instructions may quote example settings) and everything
    after the first [table] header are ignored. Returns {} when the document cannot be read."""
    if tomllib is not None:
        try:
            return {k: v for k, v in tomllib.loads(text).items() if isinstance(v, str)}
        except (ValueError, TypeError):
            return {}
    out, closing = {}, None
    for line in text.splitlines():
        if closing:
            if closing in line:
                closing = None
            continue
        st = line.strip()
        if not st or st.startswith("#"):
            continue
        if st.startswith("["):
            break
        m = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.*)$", st)
        if not m:
            continue
        key, val = m.groups()
        multi = next((d for d in ('"""', "'''") if val.startswith(d)), None)
        if multi:
            if multi not in val[3:]:
                closing = multi
            continue
        sm = re.match(r'^"((?:[^"\\]|\\.)*)"|^\'([^\']*)\'', val)
        if sm:
            out[key] = sm.group(1) if sm.group(1) is not None else sm.group(2)
    return out


def read_definition(root, family, agent):
    """(model, effort) pinned by .claude/agents/<agent>.md (front matter) or .codex/agents/<agent>.toml, else None."""
    rel = {"claude": (".claude", "agents", agent + ".md"), "codex": (".codex", "agents", agent + ".toml")}.get(family)
    if not rel:
        return None
    try:
        with open(os.path.join(root, *rel), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if family == "claude":
        head = text.split("\n---", 1)[0] if text.startswith("---") else ""
        found = [re.search(p, head) for p in (r"(?m)^model:\s*(.+?)\s*$", r"(?m)^effort:\s*(.+?)\s*$")]
        if not all(found):
            return None
        return tuple(m.group(1).strip().strip("\"'") for m in found)
    top = toml_top_level(text)
    if not top.get("model") or not top.get("model_reasoning_effort"):
        return None
    return top["model"].strip(), top["model_reasoning_effort"].strip()


def load_task(local, name, models):
    """The validated `tasks.<name>` entry of models.local.json. ConfigError (exit 2) when missing or malformed."""
    tasks = local.get("tasks")
    tasks = tasks if isinstance(tasks, dict) else {}
    entry = tasks.get(name)
    if entry is None:
        raise ConfigError("unknown task %r (tasks in models.local.json: %s)" % (name, ", ".join(sorted(tasks)) or "none"))
    bad = lambda why: ConfigError("task %s: %s" % (name, why))  # noqa: E731
    if not isinstance(entry, dict):
        raise bad("not an object")
    if not isinstance(entry.get("about"), str) or not entry["about"].strip():
        raise bad("'about' is required")
    if entry.get("base") not in ROLES:
        raise bad("'base' must be one of %s" % ", ".join(ROLES))
    model, vendor = entry.get("model"), entry.get("vendor")
    if model is not None and model not in models["models"]:
        raise bad("unknown model: %s" % model)
    if vendor is not None and vendor not in models["vendors"]:
        raise bad("unknown vendor: %s" % vendor)
    if model and vendor and models["models"][model]["vendor"] != vendor:
        raise bad("model %s belongs to %s, not %s" % (model, models["models"][model]["vendor"], vendor))
    if entry.get("effort") is not None and entry["effort"] not in EFFORTS:
        raise bad("unknown effort: %s" % entry["effort"])
    if entry.get("grade") is not None and entry["grade"] not in GRADES:
        raise bad("unknown grade: %s" % entry["grade"])
    return entry


def cli_present(vendor, cfg):
    return bool(os.environ.get("TAGTEAM_CLI_" + vendor.upper()) or shutil.which(cfg["cli"]))


class Router:
    def __init__(self, models, local, exhausted, bench=None, root=None):
        self.m, self.local, self.exh, self.bench_data = models, local, exhausted, bench or {}
        self.root = root or root_dir()
        self.order = {mid: i for i, mid in enumerate(models["models"])}
        for name, cfg in (models.get("vendors") or {}).items():
            if isinstance(cfg, dict) and "max_effort" in cfg and cfg["max_effort"] not in EFFORTS:
                raise ConfigError("vendors.%s.max_effort must be one of %s" % (name, ", ".join(EFFORTS)))

    def bench(self, mid, metric, effort):
        """Artificial Analysis value at exactly this effort, else None (other efforts would skew comparisons)."""
        for b in self.bench_data.get(mid) or []:
            if isinstance(b, dict) and b.get("source") == "artificial_analysis" and b.get("metric") == metric                     and b.get("effort") == effort and num(b.get("value")) is not None:
                return b["value"]
        return None

    def pro_ids(self, vendor):
        """CLI ids that only a paid plan offers, from models.json: the vendor's built-in orchestrator ids and every effort of a
        registered model that reaches PRO_GRADE at some effort. A fallback_only model (another maker's, through this vendor's
        CLI) is no evidence of this vendor's plan. No model names are written here."""
        ids = {o for o, info in (self.m.get("orchestrators") or {}).items() if info.get("vendor") == vendor}
        arg = self.m["vendors"][vendor]["model_arg"]
        for mid, model in self.m["models"].items():
            grades = model.get("grades") or {}
            if model["vendor"] == vendor and not model.get("fallback_only") \
                    and any(GRADES.get(g, -1) >= GRADES[PRO_GRADE] for g in grades.values()):
                ids |= {arg.format(model=model_cli_id(mid, model), effort=e) for e in grades}
        return ids

    def available(self, vendor):
        """(ids, stale): the CLI ids the vendor offers, from a fresh `available` list; (None, False) for no list, and
        (None, True) for a list whose `checked` date is older than AVAILABLE_MAX_AGE days, in the future or unreadable. None means
        unknown: every model of the vendor counts as offered and nothing is inferred from the list. One rule for both."""
        entry = (self.local.get("available") or {}).get(vendor)
        if not entry or not isinstance(entry, dict):
            return None, False
        try:
            age = (now().date() - datetime.strptime(entry["checked"], "%Y-%m-%d").date()).days
        except (KeyError, TypeError, ValueError):
            return None, True
        if not 0 <= age <= AVAILABLE_MAX_AGE or not isinstance(entry.get("ids", []), list):
            return None, True
        return entry.get("ids", []), False

    def ultra_ids(self, vendor):
        """CLI ids of the vendor's models that need the ultra plan (min_plan "ultra"), every effort. Not a fallback_only model."""
        arg = self.m["vendors"][vendor]["model_arg"]
        return {arg.format(model=model_cli_id(mid, model), effort=e)
                for mid, model in self.m["models"].items()
                if model["vendor"] == vendor and not model.get("fallback_only") and min_plan(model) == "ultra"
                for e in model_efforts(model)}

    def named_plan(self, vendor):
        """The plan models.local.json names for this vendor (max -> ultra; an unknown word counts as pro), else None."""
        named = (self.local.get("plans") or {}).get(vendor)
        if not named or not isinstance(named, str):
            return None
        named = PLAN_ALIASES.get(named, named)
        return named if named in PLAN_LEVELS else "pro"

    def inferred_plan(self, vendor):
        """The plan of an INFER_FREE vendor with no named plan, from its fresh available list: an ultra-only id -> ultra, else a
        Pro-class id -> pro, else free (a fresh empty list included). No fresh list (missing, stale, unreadable) -> free."""
        ids = self.available(vendor)[0]
        if ids is None:
            return "free"
        if set(ids) & self.ultra_ids(vendor):
            return "ultra"
        return "pro" if set(ids) & self.pro_ids(vendor) else "free"

    def inferred_free(self, vendor):
        """True when models.local.json gives this vendor no plan, it is one of INFER_FREE and the inferred plan is free (its
        fresh available list holds no Pro-class id, or there is no fresh list)."""
        return vendor in INFER_FREE and self.named_plan(vendor) is None and self.inferred_plan(vendor) == "free"

    def plan(self, vendor):
        """none | free | pro | ultra: as models.local.json says (max reads as ultra); else, for an INFER_FREE vendor, inferred
        from its available list (see inferred_plan); else pro."""
        named = self.named_plan(vendor)
        if named:
            return named
        return self.inferred_plan(vendor) if vendor in INFER_FREE else "pro"

    def plan_below(self, mid):
        """True when the vendor's plan is lower than the model's min_plan."""
        return PLAN_LEVELS[self.plan(self.m["models"][mid]["vendor"])] < PLAN_LEVELS[min_plan(self.m["models"][mid])]

    def usable(self, mid, effort):
        model = self.m["models"][mid]
        v = model["vendor"]
        if self.plan(v) == "none" or self.plan_below(mid):
            return False
        enabled = ((self.local.get("models") or {}).get(mid) or {}).get("enabled")
        if enabled is not None:
            return bool(enabled)
        ids = self.available(v)[0]
        return True if ids is None else self.m["vendors"][v]["model_arg"].format(
            model=model_cli_id(mid, model), effort=effort) in ids

    def model_effort(self, mid, spec, grade):
        """Effort for a user-pinned model: the user effort (clamped), else its lowest effort at the grade within the vendor's
        max_effort (its highest allowed effort when none reaches the grade)."""
        model = self.m["models"][mid]
        if spec.get("user_effort"):
            return clamp_effort(model, spec["user_effort"])
        efforts = [e for e in model_efforts(model) if not self.too_high(model, e)] or model_efforts(model)[:1]
        for ok in (lambda g: g == grade, lambda g: GRADES[g] > GRADES[grade]):
            hit = [e for e in efforts if ok(model["grades"][e])]
            if hit:
                return hit[0], False
        return efforts[-1], False

    def spec(self, role, grade, task=None, host=None):
        spec = dict(self.m["roles"][role])
        lr = self.role_pin(role, host)[0]  # the role-level entry, filtered by host before the task's keys are merged
        for key in ("model", "vendor"):
            if lr.get(key):
                spec["user_" + key] = lr[key]
                spec["pin_origin"] = "role"  # who chose user_model/user_vendor: the role pin or (below) the task
        if lr.get("grade"):
            spec["grade"] = lr["grade"]
        if lr.get("effort"):
            spec["user_effort"] = lr["effort"]
        if task:  # the task's keys override the role's: a model or vendor choice replaces the role-level pins
            if task.get("model") or task.get("vendor"):
                for key in ("user_model", "user_vendor", "user_effort"):
                    spec.pop(key, None)
                spec["task_choice"] = True  # the task picks the vendor: the host vendor keeps no special place
                spec["pin_origin"] = "task"
            if task.get("model"):
                spec["user_model"] = task["model"]
                spec["user_vendor"] = self.m["models"][task["model"]]["vendor"]
            elif task.get("vendor"):
                spec["user_vendor"] = task["vendor"]
            if task.get("effort"):
                spec["user_effort"] = task["effort"]
            if task.get("grade"):
                spec["grade"] = task["grade"]
                if not task.get("effort"):  # the grade table chooses the effort, not a role-level pin
                    spec.pop("user_effort", None)
        if grade:
            spec["grade"] = grade
        for key, table in (("user_model", self.m["models"]), ("user_vendor", self.m["vendors"]), ("grade", GRADES)):
            if spec.get(key) and (not isinstance(spec[key], str) or spec[key] not in table):
                raise ConfigError("unknown %s: %s" % (key, spec[key]))
        if spec.get("user_effort") and spec["user_effort"] not in EFFORTS:
            raise ConfigError("unknown effort: %s" % spec["user_effort"])
        return spec

    def vendor_order(self, spec, host, author, reason, role=None):
        pinned = [spec["user_vendor"]] if spec.get("user_vendor") else []
        explicit = spec.get("vendors")
        rest = list(explicit or self.m["hosts"][host])
        for v in dict.fromkeys(pinned + rest):
            if self.available(v)[1]:
                reason.append("available_stale:" + v)
        if not explicit:
            # a pinned role keeps the host vendor in its place even on a free plan (unless a task chose the vendor);
            # it is still reported as inferred free when it is
            keep = self.host_vendor(host) if role in PINNED_ROLES and not spec.get("task_choice") else None
            free = [v for v in rest if self.plan(v) == "free" and v not in pinned]
            for v in free:
                if v != keep:
                    reason.append("plan_free_last:" + v)
                if self.inferred_free(v):
                    reason.append("plan_inferred_free:" + v)
            free = [v for v in free if v != keep]
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

    def too_high(self, m, effort, allow=None):
        """True when the model's vendor sets max_effort and `effort` is above it (the effort the user fixed, `allow`, is exempt)."""
        top = ((self.m.get("vendors") or {}).get(m["vendor"]) or {}).get("max_effort")
        return bool(top) and effort != allow and EFFORTS.index(effort) > EFFORTS.index(top)

    def cells(self, vendor, standin, bar, allow=None):
        """Every usable (model, effort, grade) this vendor may offer: the candidate filters of pick and upgrade, in one place.

        A vendor's optional max_effort drops its cells above that effort, except the effort `allow` (the user's own)."""
        for mid, m in self.m["models"].items():
            if m.get("candidate"):
                continue
            if standin:
                if not m.get("fallback_only") or vendor not in (m["vendor"], model_maker(m)):
                    continue
            elif m["vendor"] != vendor or m.get("fallback_only"):
                continue
            if model_pool(m) in self.exh or (bar and model_maker(m) == bar):
                continue
            for effort in model_efforts(m):
                if not self.too_high(m, effort, allow) and self.usable(mid, effort):
                    yield mid, effort, m["grades"][effort]

    def upgrade(self, base, grade, vendor, cap, native, standin, bar):
        """A (model, effort, False) of a strictly higher grade than `grade` that costs little more than `base`, else None.

        Needs grading.upgrade_cost_ratio > 0 and a known index and cost for base. The cell must be a candidate of the same
        filters as pick (and, native, within the orchestrator cap: its effort also when its model is unknown), have an index >= base's, a cost <= base's cost * ratio and
        <= caps[grade], both known. Best: highest index, then lowest cost, then models.json order. The grade stays the role's."""
        grading = self.m.get("grading") or {}
        ratio = num(grading.get("upgrade_cost_ratio"))
        base_index, base_cost = self.bench(base[0], "index", base[1]), self.bench(base[0], "cost_per_task", base[1])
        if not ratio or ratio <= 0 or base_index is None or base_cost is None:
            return None
        limit = (grading.get("caps") or {}).get(grade)
        best = None
        for mid, effort, g in self.cells(vendor, standin, bar):
            if GRADES[g] <= GRADES[grade] or (native and any(cap) and self.over_cap(mid, effort, *cap)):
                continue
            index, cost = self.bench(mid, "index", effort), self.bench(mid, "cost_per_task", effort)
            if index is None or cost is None or index < base_index or cost > base_cost * ratio \
                    or (limit is not None and cost > limit):
                continue
            key = (-index, cost, self.order[mid])
            if best is None or key < best[0]:
                best = (key, (mid, effort, False))
        return best[1] if best else None

    def pick(self, role, spec, vendor, grade, cap=(None, None), native=False, standin=False, bar=None):
        """(model, effort, clamped) of this vendor for the role grade, or None: pick_grade, then upgrade (not when the user
        fixed the effort). An upgraded cell has a higher grade than `grade`; _route reports it as `upgrade:<model>:<effort>`."""
        base = self.pick_grade(role, spec, vendor, grade, cap, native, standin, bar)
        if base and not spec.get("user_effort"):
            return self.upgrade(base, grade, vendor, cap, native, standin, bar) or base
        return base

    def launchable(self, role, spec, vendor, grade, cap, native, standin, bar, host):
        """pick, but an upgraded cell whose CLI is missing gives way to the exact-grade cell when that one can launch."""
        choice = self.pick(role, spec, vendor, grade, cap, native, standin, bar)
        if choice and self.cli_missing(role, choice[0], choice[1], host, cap):
            base = self.pick_grade(role, spec, vendor, grade, cap, native, standin, bar)
            if base and base[:2] != choice[:2] and not self.cli_missing(role, base[0], base[1], host, cap):
                return base
        return choice

    def pick_grade(self, role, spec, vendor, grade, cap=(None, None), native=False, standin=False, bar=None):
        """Best (model, effort, clamped) of this vendor whose grades[effort] is the role grade, or None.

        Normal: the vendor's models that are not fallback_only. standin=True (the vendor's pool is exhausted): only
        fallback_only models that run through this vendor's CLI or are made by it. Always skipped: a model whose pool is
        exhausted and one made by `bar` (the author's maker, for independence).

        Tiers: 0 cost known and within caps[grade] -> highest index; 1 no cost, index known -> highest index;
        2 cost above the cap -> lowest cost; 3 nothing known -> defaults[vendor][grade], else the first candidate
        (models.json order, lowest effort). A later tier is used only when the earlier ones are empty.
        """
        want = spec.get("user_effort")
        cands = [(mid, effort) for mid, effort, g in self.cells(vendor, standin, bar, want)
                 if g == grade and (not want or effort == want)]
        if native and cap[0] in self.m["models"]:
            within = [c for c in cands if not self.over_cap(c[0], c[1], *cap)]
            cands = within or cands
        if not cands:
            return None
        limit = (self.m.get("grading") or {}).get("caps", {}).get(grade)
        tiers = ([], [], [])
        for c in cands:
            idx, cost = self.bench(c[0], "index", c[1]), self.bench(c[0], "cost_per_task", c[1])
            order = self.order[c[0]]
            if cost is not None and limit is not None and cost <= limit:
                tiers[0].append(((idx is None, -(idx or 0), cost, order), c))
            elif cost is None and idx is not None:
                tiers[1].append(((-idx, order), c))
            elif cost is not None and limit is not None:
                tiers[2].append(((cost, -(idx or 0), order), c))
        for tier in tiers:
            if tier:
                return min(tier, key=lambda t: t[0])[1] + (False,)
        owners = [vendor] + ([self.m["models"][c[0]]["vendor"] for c in cands] if standin else [])
        wanted = [e for o in dict.fromkeys(owners) for e in (((self.m.get("defaults") or {}).get(o) or {}).get(grade) or [])]
        for entry in wanted:
            mid, _, effort = entry.rpartition(":")
            if (mid, effort) in cands:
                return mid, effort, False
        return cands[0] + (False,)  # nothing scored: every candidate is tier 3, in models.json order

    def over_cap(self, mid, effort, orch_model, orch_effort):
        """True when a native Claude session cannot take this model or effort.

        A model listed earlier in models.json than the orchestrator's (same vendor) counts as stronger."""
        model, om = self.m["models"][mid], self.m["models"].get(orch_model)
        if om and om["vendor"] == model["vendor"] and self.order[mid] < self.order[orch_model]:
            return True
        return orch_effort in EFFORTS and EFFORTS.index(effort) > EFFORTS.index(orch_effort)

    def definition(self, vendor, role):
        """(agent name, pinned (model, effort) or None) of the vendor host's definition for this role."""
        agent = self.m["roles"][role].get("agent")
        if not agent:
            return None, None
        return agent, read_definition(self.root, self.m["vendors"][vendor]["host"], agent)

    def run_launch(self, role, vendor, mid, effort, host):
        # the interpreter that runs route.py exists on this machine; a bare "python" is missing on a default macOS
        cmd = [sys.executable, ".claude/tagteam/run.py", "--vendor", vendor, "--model", mid,
               "--effort", effort, "--prompt-file", "<PROMPT_FILE>"]
        if self.m["roles"][role].get("read_only"):
            cmd.append("--read-only")
        launch = {"kind": "run", "command": cmd}
        if host == "codex":  # a sandboxed Codex session needs approval to run run.py, whatever the vendor
            launch["escalate"] = True
        return launch

    def make_launch(self, role, mid, effort, host, cap, reason):
        """How to start the pick on this host: session / subagent / run (see the module docstring)."""
        vendor = self.m["models"][mid]["vendor"]
        same = self.m["vendors"][vendor]["host"] == host_family(host)
        agent, pinned = self.definition(vendor, role)
        matches = bool(agent) and pinned == (mid, effort)  # an alias or a different pin never matches
        if same and host == "claude-desktop":
            if self.over_cap(mid, effort, *cap):
                reason.append("native_model_cap")
                return self.run_launch(role, vendor, mid, effort, host)
            launch = {"kind": "session", "model": mid, "effort": effort}
            if matches:
                launch["also"] = {"kind": "subagent", "host": "claude", "agent": agent}
            return launch
        if same and host == "claude" and matches:
            return {"kind": "subagent", "host": "claude", "agent": agent}
        if same and host == "codex" and role != "review":  # a subagent cannot be read-only (X11)
            if matches:
                return {"kind": "subagent", "host": "codex", "agent": agent, "fork_turns": "none"}
            return {"kind": "subagent", "host": "codex", "agent": "default", "model": mid,
                    "reasoning_effort": effort, "fork_turns": "none"}
        return self.run_launch(role, vendor, mid, effort, host)

    def cli_missing(self, role, mid, effort, host, cap):
        """The launch goes through run.py but the vendor CLI is missing."""
        vendor = self.m["models"][mid]["vendor"]
        launch = self.make_launch(role, mid, effort, host, cap, [])
        return launch["kind"] == "run" and not cli_present(vendor, self.m["vendors"][vendor])

    def stronger(self, mid, effort, orch_model, orch_effort):
        """(ask_user, reason): does the pick outrank (or cost more than) the orchestrator? None = unknown."""
        om = self.m["models"].get(orch_model)
        if not om or orch_effort not in EFFORTS:
            return None, "orchestrator model or effort unknown"
        model = self.m["models"][mid]
        if model["vendor"] == om["vendor"] and self.order[mid] < self.order[orch_model]:
            return True, "listed before the orchestrator's model (stronger)"
        mine, theirs = model["grades"].get(effort), om["grades"].get(orch_effort)
        if mine and theirs and GRADES[mine] > GRADES[theirs]:
            return True, "grade %s is above the orchestrator's %s" % (mine, theirs)
        cost, orch_cost = self.bench(mid, "cost_per_task", effort), self.bench(orch_model, "cost_per_task", orch_effort)
        if cost is not None and orch_cost is not None and cost > orch_cost:
            return True, "costs more per task than the orchestrator (bench)"
        return False, "not stronger or costlier than the orchestrator"

    def orchestrator(self, host, model, effort):
        """(model, effort, note): the pair the caps and ask_user compare with.

        A model in "orchestrators" is compared as its compare_as pair and takes no effort. CLAUDE_EFFORT stands in
        for a missing effort on Claude hosts only: an agy started from Claude inherits the variable."""
        entry = (self.m.get("orchestrators") or {}).get(model)
        if entry:
            if effort:
                raise ConfigError("orchestrator %s takes no effort: drop --orchestrator-effort" % model)
            base, _, base_effort = str(entry.get("compare_as")).rpartition(":")
            if base not in self.m["models"] or base_effort not in model_efforts(self.m["models"][base]):
                raise ConfigError("orchestrators.%s: compare_as must be <registered model>:<its effort>" % model)
            return base, base_effort, "Orchestrator compared as %s %s (assumed, not measured)." % (base, base_effort)
        if not effort and host_family(host) == "claude":
            effort = os.environ.get("CLAUDE_EFFORT")
        return model, effort, None

    def route(self, role, host, author=None, grade=None, orch_model=None, orch_effort=None, task=None, task_name=None):
        if role != "review":
            author = None  # the author's vendor is excluded only from a review; other roles keep the host order
        orch_model, orch_effort, assumed = self.orchestrator(host, orch_model, orch_effort)
        result, code = self.route_task(role, host, author, grade, orch_model, orch_effort, task, task_name)
        if assumed:
            result["notice"] += " " + assumed
        skipped = self.role_pin(role, host)[1]
        if skipped:
            result.setdefault("reason", []).append("pin_other_vendor:" + role)
            result["notice"] += " roles.%s pin (%s) not applied on host %s: add hosts to apply it." % (role, skipped, host)
        if code == 0 and role in PINNED_ROLES:
            # "host vendor model" is decided by the result's vendor (whose CLI runs it), not its maker: on agy an @agy
            # Claude stand-in (vendor google, maker claude) is a host vendor model, on a Claude host it is not
            hv = self.host_vendor(host)
            result["off_host_vendor"] = result["vendor"] != hv
            if result["off_host_vendor"]:
                cause = {"role": "roles.%s pin in models.local.json" % role, "task": "task %s" % task_name}.get(
                    result["off_cause"], result["off_cause"]) or "no usable %s model" % hv  # never an empty cause
                result["off_host_cause"] = cause
                result["notice"] += " Not the host vendor (%s): %s." % (hv, cause)
        result.pop("off_cause", None)
        return result, code

    def host_vendor(self, host):
        """The first vendor of models.json hosts.<host>: the vendor whose models design, implement and read default to."""
        return self.m["hosts"][host][0]

    def role_pin(self, role, host):
        """(entry, skipped): the models.local.json roles.<role> entry that applies on this host, and the pinned model or vendor
        when the whole entry is not applied because it names another vendor than the host's (else None).

        With `hosts` the entry applies on those hosts only (elsewhere it is ignored silently). Without it, a pinned role's entry
        that names a model or vendor of another vendor than the host vendor is not applied; an entry with only effort or grade,
        and any review or translate entry, applies everywhere. host None applies the entry as written."""
        roles = self.local.get("roles") or {}
        lr = (roles.get(role) if isinstance(roles, dict) else None) or {}
        if not isinstance(lr, dict):
            raise ConfigError("roles.%s in models.local.json is not an object" % role)
        if "hosts" in lr:
            hosts = lr["hosts"]
            if not isinstance(hosts, list) or any(h not in HOSTS for h in hosts):
                raise ConfigError("roles.%s.hosts must be a list of host names (%s): %r" % (role, ", ".join(HOSTS), hosts))
        if host is None:
            return lr, None
        if "hosts" in lr:
            return (lr if host in lr["hosts"] else {}), None
        if role in PINNED_ROLES and (lr.get("model") or lr.get("vendor")):
            # another vendor's entry: a recognized model of another vendor, or a recognized other vendor. Only the recognized
            # values decide; an entry kept here with an unknown value reaches spec()'s error (unless a task replaces it)
            mid, vendor = lr.get("model"), lr.get("vendor")
            mid = mid if isinstance(mid, str) and mid in self.m["models"] else None
            vendor = vendor if isinstance(vendor, str) and vendor in self.m["vendors"] else None
            vendors = {v for v in (self.m["models"][mid]["vendor"] if mid else None, vendor) if v}
            if vendors - {self.host_vendor(host)}:
                return {}, mid or vendor
        return lr, None

    def route_task(self, role, host, author, grade, orch_model, orch_effort, task, task_name):
        if not task:
            return self._route(role, host, author, self.spec(role, grade, host=host), orch_model, orch_effort)
        spec = self.spec(role, grade, task, host)
        result, code = self._route(role, host, author, spec, orch_model, orch_effort)
        if code == 0 and self.honors(task, result, self.review_floor(role, host)):
            result.update({"task": task_name, "task_applied": True})
            if role in PINNED_ROLES and result["vendor"] != self.host_vendor(host):
                # the task's cause, unless the base role's own route lands on the same vendor (then the base's cause holds,
                # e.g. a stand-in forced by the host vendor's exhaustion); a failed base route means the task found it
                try:  # a role entry the task replaced may be invalid on its own: then there is no base route
                    base, bcode = self._route(role, host, author, self.spec(role, grade, host=host), orch_model, orch_effort)
                except ConfigError:
                    base, bcode = {}, 2
                same = bcode == 0 and base["vendor"] == result["vendor"]
                result["off_cause"] = base["off_cause"] if same else "task"
            return result, code
        cause = self.task_cause(role, host, author, spec, task, (orch_model, orch_effort), result)
        base, code = self._route(role, host, author, self.spec(role, grade, host=host), orch_model, orch_effort)
        base.setdefault("reason", []).append("task_fallback:" + cause)
        base["notice"] += " task %s preference unusable (%s), routed as %s." % (task_name, cause, role)
        base.update({"task": task_name, "task_applied": False})
        return base, code

    @staticmethod
    def review_floor(role, host):
        """On agy a review is graded at least A: the agy executor may be a Flash-class model the orchestrator cannot see."""
        return role == "review" and host == "agy"

    @staticmethod
    def honors(task, result, floor=False):
        if task.get("model"):
            return result["model"] == task["model"]
        if task.get("vendor") and result["vendor"] != task["vendor"]:
            return False
        if task.get("effort") and result["effort"] != task["effort"] and "effort_clamped" not in result["reason"]:
            return False
        want = task.get("grade")
        if want and floor and GRADES[want] < GRADES["A"]:
            want = "A"  # the task's grade, raised like the route's
        return not want or result["grade"] == want

    def task_cause(self, role, host, author, spec, task, cap, result):
        """Why the task's preference was not used (design 2.3.6: exhausted, plan_none, unavailable, cli_missing, author_vendor)."""
        mid = task.get("model")
        vendor = self.m["models"][mid]["vendor"] if mid else task.get("vendor")
        if not vendor:
            return "unavailable:task"
        if self.plan(vendor) == "none":
            return "plan_none:" + vendor
        quota, maker = (model_pool(self.m["models"][mid]), model_maker(self.m["models"][mid])) if mid else (vendor, vendor)
        if quota in self.exh:
            return "exhausted:" + quota
        if role == "review" and maker == author:
            return "author_vendor"
        grade = spec["grade"]
        if mid:
            effort = self.model_effort(mid, spec, grade)[0]
            if not self.usable(mid, effort):
                return "unavailable:" + mid
            if self.review_floor(role, host) and GRADES[self.m["models"][mid]["grades"][effort]] < GRADES["A"]:
                return "agy_review_floor_A"
        else:
            hit = self.pick(role, spec, vendor, grade, cap, False, False, author)
            if hit is None:
                return "unavailable:" + vendor
            mid, effort = hit[0], hit[1]
        if self.cli_missing(role, mid, effort, host, cap):
            return "cli_missing:" + vendor
        return "unavailable:" + (task.get("model") or vendor)

    def _route(self, role, host, author, spec, orch_model, orch_effort):
        grade = spec["grade"]
        reason, notes = [], []
        floor = self.review_floor(role, host)
        if floor and GRADES[grade] < GRADES["A"]:
            reason.append("agy_review_floor_A")
            notes.append("agy host: review grade raised from %s to A" % grade)
            grade = spec["grade"] = "A"
        cap = (orch_model, orch_effort)
        choice = chooser = None  # chooser: what put the result off the host vendor (see Router.route)
        hv = self.host_vendor(host)
        um = spec.get("user_model")
        if um:
            vendor = self.m["models"][um]["vendor"]
            effort, clamped = self.model_effort(um, spec, grade)
            pinned = (self.m["models"][um].get("grades") or {}).get(effort)
            if not self.usable(um, effort):
                reason.append("override_unusable")
                if self.plan_below(um):
                    reason.append("plan_below:" + um)
            elif model_pool(self.m["models"][um]) in self.exh:
                reason.append("override_exhausted")
            elif role == "review" and model_maker(self.m["models"][um]) == author:
                reason.append("override_same_vendor_as_author")
            elif floor and GRADES[pinned] < GRADES["A"]:  # a pinned effort can land below the floor
                reason.append("override_below_floor")
                notes.append("user-pinned reviewer is grade %s, below the agy review floor A, substituted" % pinned)
            elif self.cli_missing(role, um, effort, host, cap):
                reason.append("cli_missing:" + vendor)
            else:
                choice = (um, effort, clamped)
                chooser = spec.get("pin_origin")  # the role pin or the task that named the model
                reason.append("user_override")
                if role == "review" and pinned != grade:
                    reason.append("override_grade_mismatch")
                    notes.append("user-pinned reviewer is grade %s, the author's grade is %s" % (pinned, grade))
        if choice is None:
            order = self.vendor_order(spec, host, author, reason, role)
            expected = order[0] if order else None
            hv_skip = None  # why the host vendor's own slot gave nothing, when it was tried
            for v in order:
                cfg = self.m["vendors"][v]
                standin = v in self.exh
                slot = v
                if v == hv:
                    hv_skip = "none"
                if standin:
                    if v == hv:
                        hv_skip = "exhausted"
                    reason.append("exhausted_skip:" + v)
                    until = parse_ts(self.exh[v]["until"]).strftime("%H:%M")
                    # the vendor's pool is exhausted: first a same-grade fallback_only model (another pool), else the next vendor
                    choice = self.launchable(role, spec, v, grade, cap, False, True, author, host)
                    if choice and self.cli_missing(role, choice[0], choice[1], host, cap):
                        sub = self.m["models"][choice[0]]["vendor"]
                        reason.append("cli_missing:" + sub)
                        notes.append("%s CLI missing, substituted" % sub)
                        choice = None
                    if choice is None:
                        notes.append("%s exhausted until %s UTC, substituted" % (v, until))
                        continue
                    notes.append("%s exhausted until %s UTC, substituted %s (%s quota)" % (
                        v, until, choice[0], model_pool(self.m["models"][choice[0]])))
                    reason.insert(0, "substituted:%s\u2192%s" % (expected, model_pool(self.m["models"][choice[0]])))
                    break
                if cfg["host"] != host_family(host) and not cli_present(v, cfg):
                    if v == hv:
                        hv_skip = "cli"
                    reason.append("cli_missing:" + v)
                    notes.append("%s CLI missing, substituted" % v)
                    continue
                choice = self.launchable(role, spec, v, grade, cap, cfg["host"] == "claude" and host == "claude-desktop", False,
                                         author, host)
                if choice and self.cli_missing(role, choice[0], choice[1], host, cap):
                    if v == hv:
                        hv_skip = "cli"
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
                return {"status": "incomplete" if incomplete else "none", "role": role, "host": host, "reason": reason,
                        "notice": "%s: %s." % (role, "no independent reviewer available, review incomplete"
                                               if incomplete else "no usable model found")}, 3 if incomplete else 4
            if not any(r.startswith("substituted") for r in reason):
                reason.insert(0, "default")
            if slot == spec.get("user_vendor") and slot != hv:
                chooser = spec.get("pin_origin")  # the pinned vendor's slot: the pin or task chose it
            elif slot == hv:  # only a stand-in of the exhausted host vendor leaves the host vendor from its own slot
                chooser = ("%s exhausted until %s UTC" % (hv, parse_ts(self.exh[hv]["until"]).strftime("%H:%M"))
                           if hv in self.exh else None)
            else:  # the host vendor was skipped: not in the order (plan none), exhausted, CLI missing, or nothing usable
                chooser = {"exhausted": "%s exhausted until %s UTC" % (
                    hv, parse_ts(self.exh[hv]["until"]).strftime("%H:%M")) if hv in self.exh else None,
                    "cli": "%s CLI missing" % hv}.get(hv_skip) or (
                        "%s plan free" % hv if self.plan(hv) == "free" else "no usable %s model" % hv)
        mid, effort, clamped = choice
        model = self.m["models"][mid]
        vendor = model["vendor"]
        if clamped:
            reason.append("effort_clamped")
        grade = model["grades"][effort]
        if "user_override" not in reason and GRADES[grade] > GRADES[spec["grade"]]:
            reason.append("upgrade:%s:%s" % (mid, effort))  # the result keeps the role's grade
            grade = spec["grade"]
        known = any(self.bench(mid, metric, effort) is not None for metric in ("index", "cost_per_task"))
        reason.append("bench:local" if known else "bench:none")
        launch = self.make_launch(role, mid, effort, host, cap, reason)
        extra = ""
        if launch["kind"] == "session":
            if not (orch_model in self.m["models"] and orch_effort in EFFORTS):
                extra = " Check orchestrator cap."
        elif "native_model_cap" in reason:
            extra = " Orchestrator cap exceeded, launching via run.py."
        ask, ask_reason = (False, "not a design role") if role != "design" else \
            self.stronger(mid, effort, orch_model, orch_effort)
        via = {"session": "session", "subagent": "subagent " + str(launch.get("agent")), "run": "run.py"}[launch["kind"]]
        notice = "%s -> %s (%s, %s, grade %s) via %s, host=%s." % (role, mid, effort, vendor, grade, via, host)
        if role == "review":
            notice += " Reviewer differs from author vendor %s." % author
        for n in notes:
            notice += " " + n + "."
        notice += extra
        if ask:
            notice += " Ask the user before launching: %s." % ask_reason
        elif ask is None:
            notice += " Pass --orchestrator-model/--orchestrator-effort so the router can compare."
        return {"status": "ok", "role": role, "host": host, "model": mid, "effort": effort, "vendor": vendor,
                "grade": grade, "maker": model_maker(model), "pool": model_pool(model), "launch": launch,
                "ask_user": ask, "ask_reason": ask_reason, "reason": reason, "notice": notice, "off_cause": chooser}, 0


def mark(args, models, path):
    v = args.mark_exhausted
    cooldowns = pool_cooldowns(models)
    if v not in cooldowns:
        raise ConfigError("unknown vendor or pool: %s (known: %s)" % (v, ", ".join(cooldowns)))
    if not args.evidence:
        raise ConfigError("--evidence is required with --mark-exhausted")
    t = now()
    until = parse_ts(args.until) if args.until else t + timedelta(minutes=cooldowns[v])
    vendors = read_exhausted(path)
    vendors[v] = {"since": t.strftime(TS), "until": until.strftime(TS), "evidence": args.evidence[:200], "source": "manual"}
    write_exhausted(path, vendors)
    print("%s exhausted until %s" % (v, vendors[v]["until"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pick model, effort and launch method for a role.")
    ap.add_argument("--role", choices=ROLES)
    ap.add_argument("--task", metavar="NAME", help="a task from models.local.json; its base is the role")
    ap.add_argument("--host", choices=HOSTS)
    ap.add_argument("--author-vendor", help="the author's maker (the company that built the model)")
    ap.add_argument("--author-model")
    ap.add_argument("--author-effort", choices=EFFORTS)
    ap.add_argument("--grade", choices=list(GRADES))
    ap.add_argument("--orchestrator-model")
    ap.add_argument("--orchestrator-effort", choices=EFFORTS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mark-exhausted", metavar="POOL", help="a quota name: a vendor or a model pool such as google-partner")
    ap.add_argument("--clear-exhausted", metavar="POOL")
    ap.add_argument("--evidence")
    ap.add_argument("--until")
    args = ap.parse_args(argv)
    try:
        if not (args.role or args.task or args.mark_exhausted or args.clear_exhausted):
            ap.error("one of --role, --task, --mark-exhausted, --clear-exhausted is required")
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
        local = load_json(os.path.join(cdir, "models.local.json"), False)
        task = load_task(local, args.task, models) if args.task else None
        role = args.role or task["base"]
        if task and role != task["base"]:
            raise ConfigError("--role %s does not match task %s (base %s)" % (role, args.task, task["base"]))
        author, grade = args.author_vendor, args.grade
        if args.author_effort and not args.author_model:
            raise ConfigError("--author-effort needs --author-model")
        if args.author_model:
            if args.author_model not in models["models"]:
                raise ConfigError("unknown model: %s" % args.author_model)
            amodel = models["models"][args.author_model]
            if author and author not in (model_maker(amodel), amodel["vendor"]):
                raise ConfigError("--author-vendor %s does not match --author-model %s" % (author, args.author_model))
            author = model_maker(amodel)  # independence is by maker: an agy-run Claude is a Claude author
            if not grade and args.author_effort:
                if args.author_effort not in amodel["grades"]:
                    raise ConfigError("%s has no effort %s" % (args.author_model, args.author_effort))
                grade = amodel["grades"][args.author_effort]
        if role == "review":
            if not author:
                ap.error("--author-vendor is required with --role review")
            if not grade:
                raise ConfigError("--role review needs the author's grade: --grade, or --author-model with --author-effort")
        if author and author not in models["vendors"]:
            raise ConfigError("unknown vendor: %s" % author)
        router = Router(models, local, read_exhausted(exhausted_path()), read_bench(cdir))
        result, code = router.route(role, args.host or default_host(), author, grade,
                                    args.orchestrator_model, args.orchestrator_effort, task, args.task)
    except (ConfigError, KeyError, ValueError, state.StateError) as exc:
        sys.stderr.write("route.py: %s: %s\n" % (type(exc).__name__, exc))
        return 2
    print(json.dumps(result, indent=1) if args.json else result["notice"])
    return code


if __name__ == "__main__":
    sys.exit(main())
