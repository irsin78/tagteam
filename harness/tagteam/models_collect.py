#!/usr/bin/env python3
"""Collect the model lists the vendor CLIs offer and write a proposal file.

  models_collect.py [--project DIR] [--timeout 60] [--refresh] [--mark-applied]
  models_collect.py --write-bench [--timeout 60] [--refresh]
  models_collect.py --write-available [--timeout 60] [--refresh]
  models_collect.py --from-cache [--project DIR]
  models_collect.py --weekly [--timeout 60]

No LLM and no model run; it never edits models.json or models.local.json. What each mode reads and writes, the change
kinds and the apply step: docs/manual.md, section 8. The Artificial Analysis key (TAGTEAM_AA_API_KEY) is never written
anywhere. Ids in models.json "orchestrators" are known to the collector and never reported as new models.
Data source: Artificial Analysis (https://artificialanalysis.ai/).

Network results (the vendor CLI lists and the Artificial Analysis rows) live in one cache per machine, in
~/.tagteam/cache/ (TAGTEAM_HOME replaces ~/.tagteam). A cache younger than CACHE_MAX_AGE_DAYS is used without any
network call; an older or missing one is refreshed under ~/.tagteam/refresh.lock. --refresh refreshes regardless of age.
--from-cache never touches the network. --weekly is the machine-wide weekly job (run from ~/.tagteam/bin/, beside a copy
of state.py, so it imports nothing from a project): it refreshes every cache, then runs each registered project's own
collector with --from-cache and logs the results to ~/.tagteam/weekly.log.
Exit: 0 collected, 1 some vendor error or missing (or bench error, or a weekly project failed), 2 usage or
config error, 3 every vendor CLI failed (--weekly: every refresh failed).
Config dir: next to this script (TAGTEAM_CONFIG_DIR in tests).
"""
# The installer reads this line to decide whether a copy in ~/.tagteam/bin/ is older than the project's collector.
COLLECTOR_SCHEMA = 2

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state as statelib  # noqa: E402  (`state` is a local name in main; the only module copied beside the weekly copy)

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SUFFIX = re.compile(r"^(.*)-(%s)$" % "|".join(EFFORTS))
AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_SOURCE = "Artificial Analysis (https://artificialanalysis.ai/)"
AA_MAX_PAGES = 10
METRICS = ("index", "cost_per_task")
BENCH_FILE = "bench.local.json"
NAME_EFFORT = re.compile(r"\(([^)]*)\)\s*$")
LABEL_SUFFIX = re.compile(r"\s*\((?:%s)\)$" % "|".join(e.title() for e in EFFORTS), re.I)
CACHE_SCHEMA = 1
CACHE_MAX_AGE_DAYS = 6
LOCK_NAME = "refresh.lock"
REGISTRY_LOCK = "registry.lock"  # the installer takes the same lock around every change of projects.json
ATTEMPTS_FILE = "attempts.json"
KEY_VAR = "TAGTEAM_AA_API_KEY"
LOCK_WAIT_SECONDS = 5 * 60      # how long to wait for another refresh before reading the cache as it is
LOCK_POLL_SECONDS = 5
PROJECT_TIMEOUT = 120           # --weekly: seconds one project's --from-cache run may take
ISO = "%Y-%m-%dT%H:%M:%SZ"
# CLI executable per vendor when no models.json is at hand (the weekly copy in ~/.tagteam/bin/ has none).
DEFAULT_CLI = {"claude": "claude", "openai": "codex", "google": "agy"}


def maker(m):
    """The company that made the model: a model reached through another vendor's CLI names it."""
    return m.get("maker") or m["vendor"]


def cli_id(mid, m):
    """The name the vendor CLI knows the model by (an id like claude-opus-5-5@agy has its own)."""
    return m.get("cli_id") or mid


def efforts_of(model):
    """Efforts a registered model offers: the keys of its grades table, in EFFORTS order."""
    keys = list(model.get("grades") or {})
    return sorted(keys, key=lambda e: EFFORTS.index(e) if e in EFFORTS else len(EFFORTS))


def grade_for(index, floors):
    """Grade of an AA index value under grading.floors (S, A, B; below B is C)."""
    for g in ("S", "A", "B"):
        if index >= floors[g]:
            return g
    return "C"


def parse_codex(text, models=None):
    """slug -> {"efforts", "label"} for models with visibility list; ultra is never offered."""
    out = {}
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list)             or not all(isinstance(m, dict) for m in data["models"]):
        raise ValueError("unexpected codex payload: need an object with a models list of objects")
    for m in data["models"]:
        if m.get("visibility") == "list" and m.get("slug"):
            levels = [x.get("effort") for x in m.get("supported_reasoning_levels") or []]
            out[m["slug"]] = {"efforts": [e for e in levels if e in EFFORTS], "label": m.get("display_name", "")}
    return list(out), out


def parse_agy(text, models=None):
    ids, models = [], {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        cid, label = (p.strip() for p in line.split("\t", 1))
        ids.append(cid)
        hit = SUFFIX.match(cid)
        base, effort = (hit.group(1), hit.group(2)) if hit else (cid, None)
        entry = models.setdefault(base, {"efforts": [], "label": LABEL_SUFFIX.sub("", label) if hit else label})
        if effort and effort not in entry["efforts"]:
            entry["efforts"].append(effort)
    return ids, models


CLAUDE_SPECIAL = {"best", "default", "opusplan"}


def claude_family(mid, model):
    """Model family as /model names it: `family` in models.json, else the second part of the id (claude-opus-5-5 -> opus)."""
    return model.get("family") or (mid.split("-")[1] if mid.count("-") >= 1 else mid)


def parse_claude_families(text):
    """`claude -p /model --output-format json` lists the aliases this account may use: the model families, sorted."""
    data = json.loads(text)
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, str):
        raise ValueError("unexpected /model output: no text result")
    hit = re.search(r"Available:\s*(.+)", result)
    if not hit:
        raise ValueError("unexpected /model output: no 'Available:' list")
    families = set()
    for token in hit.group(1).split(","):
        token = re.sub(r"\[1m\]$", "", token.strip().rstrip(".").lower())
        if re.fullmatch(r"[a-z]+", token) and token not in CLAUDE_SPECIAL:
            families.add(token)
    if not families:
        raise ValueError("unexpected /model output: no model family listed")
    return sorted(families)


def claude_ids(families, models):
    """A registered Claude model counts as offered when its family is listed; Claude Code lists families, not versions."""
    return [mid for mid, m in models["models"].items()
            if m.get("vendor") == "claude" and claude_family(mid, m) in set(families)]


def parse_claude(text, models):
    return claude_ids(parse_claude_families(text), models), {}


# How each vendor CLI is asked for its models. The cached listing is the vendor's own answer and does not depend on a
# project: ids and per-model info for codex and agy, the list of families for claude (a project maps families to ids).
LISTERS = {"openai": ["debug", "models"], "google": ["models"], "claude": ["-p", "/model", "--output-format", "json"]}


def raw_listing(vendor, text):
    if vendor == "claude":
        return {"families": parse_claude_families(text)}
    ids, info = (parse_codex if vendor == "openai" else parse_agy)(text)
    return {"ids": ids, "models": info}


def listing_for(vendor, raw, models):
    """Cached raw listing -> {"ids", "models"} for one project. Raises ValueError when no id is left."""
    if vendor == "claude":
        ids, info = claude_ids(raw["families"], models), {}
    else:
        ids, info = raw["ids"], raw["models"]
    if not ids:
        raise ValueError("no models parsed from output")
    return {"ids": ids, "models": info}


def resolve_cli(vendor):
    """Command prefix of the vendor CLI, or None. The machine's default executable name is looked up on PATH; no project
    file picks it, because the result goes into a cache every project reads. TAGTEAM_CLI_<VENDOR> (a JSON list) overrides."""
    override = os.environ.get("TAGTEAM_CLI_" + vendor.upper())
    if override:
        try:
            base = json.loads(override)
            if isinstance(base, list) and base and all(isinstance(x, str) for x in base):
                return base
        except ValueError:
            pass
        return None
    exe = shutil.which(DEFAULT_CLI[vendor]) if vendor in DEFAULT_CLI else None
    return [exe] if exe else None


def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_cli(cmd, timeout, cwd=None, env=None):
    """(returncode, stdout, stderr) or raises subprocess.TimeoutExpired after killing the tree."""
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", cwd=cwd, env=env, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return proc.returncode, out, err


def child_env(**extra):
    """The environment of a child process: ours without the Artificial Analysis key, which no child needs."""
    env = {k: v for k, v in os.environ.items() if k != KEY_VAR}
    env.update(extra)
    return env


def fetch_vendor(vendor, timeout, home):
    """Ask one vendor CLI for its models. -> (status dict, {"version", "listing"} or None). Machine-level: it reads no
    project file, runs in the machine folder `home` and writes no cache."""
    if vendor not in LISTERS:
        return {"status": "unlisted"}, None
    cli = resolve_cli(vendor)
    if not cli:
        return {"status": "missing"}, None
    args = LISTERS[vendor]
    try:
        os.makedirs(home, exist_ok=True)
        cwd = home
    except OSError:
        cwd = tempfile.gettempdir()
    # The claude child is marked as a worker so that a user-level copy of the role guard records nothing as an
    # orchestrator. /model itself calls no model.
    env = child_env(HARNESS_DELEGATE_RUN="1") if vendor == "claude" else child_env()
    try:
        code, out, err = run_cli(cli + args, timeout, cwd=cwd, env=env)
        if code != 0:
            return {"status": "error", "detail": (err.strip().splitlines() or ["exit %d" % code])[0][:200]}, None
        listing = raw_listing(vendor, out)
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": "timeout after %ss" % timeout}, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"status": "error", "detail": ("%s: %s" % (type(exc).__name__, exc))[:200]}, None
    if not (listing.get("ids") or listing.get("families")):
        return {"status": "error", "detail": "no models parsed from output"}, None
    status = {"status": "ok"}
    try:
        status["version"] = run_cli(cli + ["--version"], 10, cwd=cwd, env=env)[1].strip().splitlines()[0][:100]
    except (subprocess.TimeoutExpired, OSError, IndexError):
        pass
    return status, {"version": status.get("version"), "listing": listing}


def compare(vendor, listing, models, local):
    changes = []
    registered = {k: m for k, m in models["models"].items() if m["vendor"] == vendor}
    by_cli = {cli_id(k, m): k for k, m in registered.items()}  # what the CLI lists -> the registered id
    unreg = (models.get("unregistered") or {}).get(vendor) or {}
    orchestrators = models.get("orchestrators") or {}
    had = set(((local.get("available") or {}).get(vendor) or {}).get("ids") or [])
    ids, info = set(listing["ids"]), listing["models"]
    for base, i in info.items():
        if base not in by_cli and base not in unreg and base not in orchestrators:
            changes.append({"kind": "new_model", "vendor": vendor, "id": base, "label": i["label"],
                            "efforts": sorted(i["efforts"], key=EFFORTS.index)})
    for mid, m in registered.items():
        arg = models["vendors"][vendor]["model_arg"]
        cid = cli_id(mid, m)
        cli_ids = sorted({arg.format(model=cid, effort=e) for e in efforts_of(m)})
        now = [c for c in cli_ids if c in ids and c not in had]
        gone = [c for c in cli_ids if c not in ids and c in had]
        for kind, found in (("now_available", now), ("no_longer_available", gone)):
            if found:
                changes.append({"kind": kind, "vendor": vendor, "model": mid, "cli_ids": found})
        if cid in info:
            dropped = [e for e in efforts_of(m) if e not in info[cid]["efforts"]]
            if dropped:
                changes.append({"kind": "efforts_dropped", "vendor": vendor, "model": mid, "efforts": dropped})
        elif vendor == "openai" and not m.get("candidate") \
                and ((local.get("models") or {}).get(mid) or {}).get("enabled") is not False:
            changes.append({"kind": "missing_registered", "vendor": vendor, "model": mid})
    return changes


def grade_changes(models, stored):
    """grade_changed: index in bench.local.json ("models" mapping) puts a registered (model, effort)
    into another grade than models.json grades says. No bench file = no changes."""
    if not stored:
        return []
    floors = (models.get("grading") or {}).get("floors")
    if not floors:
        return []
    changes = []
    for mid, m in models["models"].items():
        for effort in efforts_of(m):
            for b in stored.get(mid) or []:
                if isinstance(b, dict) and b.get("source") == "artificial_analysis" and b.get("metric") == "index"                         and b.get("effort") == effort and aa_num(b.get("value")) is not None:
                    new = grade_for(b["value"], floors)
                    if new != m["grades"][effort]:
                        changes.append({"kind": "grade_changed", "vendor": m["vendor"], "model": mid, "effort": effort,
                                        "old": m["grades"][effort], "new": new, "index": b["value"]})
                    break
    return changes


def aa_fetch_page(page, key, timeout):
    """-> (parsed JSON, response headers with lowercase names). Raises urllib/OS/ValueError errors."""
    req = urllib.request.Request("%s?page=%d" % (AA_URL, page),
                                 headers={"x-api-key": key, "User-Agent": "tagteam-collect"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data, {k.lower(): v for k, v in resp.headers.items()}


def aa_entries(rows):
    """AA rows -> [{creator, base, effort, name, release, index, cost}]; rows without a usable effort are dropped."""
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("slug"):
            raise ValueError("unexpected AA entry")
        hit = NAME_EFFORT.search(row.get("name") or "")
        effort = hit.group(1).split(",")[0].strip().lower() if hit else None
        if effort not in EFFORTS:
            continue
        slug = row["slug"]
        base = slug[:-len(effort) - 1] if slug.endswith("-" + effort) else slug
        ev = row.get("evaluations") or {}
        cost = ((row.get("artificial_analysis_intelligence_index_cost") or {}).get("cost_per_task") or {})
        out.append({"creator": (row.get("model_creator") or {}).get("name"), "base": base, "effort": effort,
                    "name": row.get("name"), "release": row.get("release_date"),
                    "index": ev.get("artificial_analysis_intelligence_index"), "cost": cost.get("total_cost")})
    return out


def fetch_aa(key, timeout):
    """-> (status dict, {"rows", "index_version", "remaining"} or None). The key never reaches the status."""
    rows, version, remaining = [], None, None
    try:
        for page in range(1, AA_MAX_PAGES + 1):
            data, headers = aa_fetch_page(page, key, timeout)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("unexpected AA payload")
            rows += data["data"]
            version = data.get("intelligence_index_version", version)
            remaining = headers.get("x-ratelimit-remaining", remaining)
            if not (data.get("pagination") or {}).get("has_more"):
                break
        else:
            raise ValueError("still has_more after %d pages" % AA_MAX_PAGES)
        aa_entries(rows)  # shape check: a payload that cannot be read must not reach the cache
    except urllib.error.HTTPError as exc:
        detail = "HTTP %d" % exc.code
        if exc.code == 429:
            wait = str((exc.headers.get("Retry-After") if exc.headers else "") or "").strip()
            detail += ", Retry-After %s" % (wait if wait.isdigit() else "?")
        return {"status": "error", "detail": detail}, None
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:  # URLError, timeouts, JSON, bad shapes
        return {"status": "error", "detail": ("%s: %s" % (type(exc).__name__, exc)).replace(key, "***")[:200]}, None
    return {"status": "ok", "index_version": version, "models_seen": len(rows), "remaining": remaining}, \
        {"rows": rows, "index_version": version, "remaining": remaining}


def bench_from_entry(entry, cached):
    """AA cache entry -> (bench status dict, entries). `cached` adds when the data was fetched."""
    try:
        entries = aa_entries(entry["rows"])
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "error", "detail": "invalid AA cache"}, []
    status = {"status": "ok", "index_version": entry["index_version"], "models_seen": len(entry["rows"]),
              "remaining": entry.get("remaining")}
    if cached:
        status["cached_at"] = entry["fetched"]
    return status, entries


def utcnow():
    return datetime.now(timezone.utc)


def aa_slug(models, mid):
    return models["models"][mid].get("aa_slug") or mid.replace(".", "-")


def aa_num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def aa_value(by, models, mid, effort, metric):
    e = by.get((aa_slug(models, mid), effort))
    return aa_num(e and e["index" if metric == "index" else "cost"])


def compare_bench(vendor, entries, models, version, now, stored=None):
    """stored: bench.local.json "models" mapping, or None when the file is missing (no bench changes)."""
    creator = models["vendors"][vendor].get("aa_creator")
    if not creator:
        return []
    mine = [e for e in entries if e["creator"] == creator]
    by = {(e["base"], e["effort"]): e for e in mine}
    changes, known = [], set()
    for mid, m in models["models"].items():
        if maker(m) != vendor:  # AA lists a model under its maker, also when another vendor's CLI serves it
            continue
        known.add(aa_slug(models, mid))
        if stored is None:
            continue
        have = set()
        for b in stored.get(mid) or []:
            if not isinstance(b, dict) or b.get("source") != "artificial_analysis"                     or b.get("metric") not in METRICS or b.get("effort") not in efforts_of(m):
                continue
            have.add((b["effort"], b["metric"]))
            new = aa_value(by, models, mid, b["effort"], b["metric"])
            if new is not None and new != b.get("value"):
                changes.append({"kind": "bench_changed", "vendor": m["vendor"], "model": mid, "effort": b["effort"],
                                "metric": b["metric"], "old": b.get("value"), "new": new, "index_version": version})
        for effort in efforts_of(m):
            for metric in METRICS:
                new = aa_value(by, models, mid, effort, metric)
                if new is not None and (effort, metric) not in have:
                    changes.append({"kind": "bench_added", "vendor": m["vendor"], "model": mid, "effort": effort,
                                    "metric": metric, "new": new, "index_version": version})
    known |= {u.replace(".", "-") for u in (models.get("unregistered") or {}).get(vendor) or {}}
    cutoff = (now - timedelta(days=models.get("aa_new_model_days", 120))).strftime("%Y-%m-%d")
    floors = (models.get("grading") or {}).get("floors") or {"S": 54, "A": 48, "B": 30}
    groups = {}
    for e in mine:
        if e["base"] not in known:
            groups.setdefault(e["base"], []).append(e)
    for base, group in groups.items():
        aa = {e["effort"]: {"index": e["index"], "cost_per_task": e["cost"]} for e in group if e["index"] is not None}
        release = max([e["release"] for e in group if isinstance(e["release"], str)] or [""])
        if aa and cutoff <= release <= now.strftime("%Y-%m-%d"):
            label = re.sub(r"\s*\([^)]*\)\s*$", "", group[0]["name"] or base)
            changes.append({"kind": "new_model", "vendor": vendor, "id": base, "label": label,
                            "efforts": sorted(aa, key=EFFORTS.index), "source": "artificial_analysis",
                            "aa": aa, "release_date": release,
                            "grades": {e: grade_for(aa[e]["index"], floors) for e in sorted(aa, key=EFFORTS.index)}})
    return changes


def merge_new_models(changes):
    """One new_model per vendor and base slug: the CLI entry wins and gains the AA fields."""
    out, seen = [], {}
    for c in sorted(changes, key=lambda c: c.get("source") == "artificial_analysis"):
        key = (c["vendor"], c["id"].replace(".", "-")) if c["kind"] == "new_model" else None
        if key and key in seen and c.get("source") == "artificial_analysis":
            seen[key].update({"aa": c["aa"], "release_date": c["release_date"], "grades": c["grades"]})
            continue
        out.append(c)
        if key:
            seen.setdefault(key, c)
    return out


def load_bench(cdir):
    """bench.local.json as a dict, or None when it is missing or unreadable."""
    try:
        data = load(os.path.join(cdir, BENCH_FILE), False)
    except (OSError, ValueError):
        return None
    if not isinstance(data.get("models"), dict):
        return None
    data["models"] = {k: v for k, v in data["models"].items() if isinstance(v, list)}
    return data


def build_bench(models, entries, version, today, old):
    """New bench.local.json content: fresh AA values for registered models and their efforts,
    plus every old entry whose source is not artificial_analysis."""
    out = {mid: [b for b in (old or {}).get(mid) or [] if isinstance(b, dict) and b.get("source") != "artificial_analysis"]
           for mid in (old or {})}
    for vendor, cfg in models["vendors"].items():
        creator = cfg.get("aa_creator")
        if not creator:
            continue
        by = {(e["base"], e["effort"]): e for e in entries if e["creator"] == creator}
        for mid, m in models["models"].items():
            if maker(m) != vendor:
                continue
            for effort in efforts_of(m):
                for metric in METRICS:
                    value = aa_value(by, models, mid, effort, metric)
                    if value is not None:
                        out.setdefault(mid, []).append({"source": "artificial_analysis", "metric": metric,
                                                        "value": value, "effort": effort, "date": today,
                                                        "version": version})
    return {"schema": 1, "source": AA_SOURCE, "fetched": today, "index_version": version,
            "models": {k: v for k, v in out.items() if v}}


def digest(changes, cli, bench=None):
    bench = bench or {}
    body = {"changes": changes, "cli": {v: c["status"] for v, c in sorted(cli.items())},
            "bench": {"status": bench.get("status"), "index_version": bench.get("index_version")}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def scrub(obj):
    """obj with the Artificial Analysis key (TAGTEAM_AA_API_KEY of this process) replaced by *** in every string.
    Everything this script writes or prints passes through it: a key must never reach a cache, proposal or log."""
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        return obj
    if isinstance(obj, str):
        return obj.replace(key, "***")
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {scrub(k): scrub(v) for k, v in obj.items()}
    return obj


def write_json(path, data):
    data = scrub(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load(path, required):
    if not os.path.exists(path):
        if required:
            raise ValueError("missing %s" % path)
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    return data


def log(state, text):
    text = scrub(text)
    try:
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "collect.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), text))
    except OSError:
        pass


def load_config(cdir):
    models = load(os.path.join(cdir, "models.json"), True)
    local = load(os.path.join(cdir, "models.local.json"), False)
    for key in ("vendors", "models"):
        if not isinstance(models.get(key), dict):
            raise ValueError("models.json: %r must be an object" % key)
    for mid, m in models["models"].items():
        if not (isinstance(m, dict) and m.get("vendor") in models["vendors"]
                and isinstance(m.get("grades"), dict)):
            raise ValueError("models.json: model %s needs a known vendor and a grades table" % mid)
    if any(not isinstance(v, dict) or "model_arg" not in v for v in models["vendors"].values()):
        raise ValueError("models.json: every vendor needs a model_arg")
    orchestrators = models.get("orchestrators", {})
    if not isinstance(orchestrators, dict):
        raise ValueError("models.json: 'orchestrators' must be an object")
    for oid, o in orchestrators.items():
        if not (isinstance(o, dict) and o.get("vendor") in models["vendors"] and isinstance(o.get("compare_as"), str)):
            raise ValueError("models.json: orchestrator %s needs a known vendor and a compare_as string" % oid)
    return models, local


def warn(text):
    sys.stderr.write("models_collect: %s\n" % scrub(str(text)))


def tagteam_home():
    """The machine-wide folder: TAGTEAM_HOME, else ~/.tagteam, as an absolute path (children run in other folders)."""
    return os.path.abspath(os.environ.get("TAGTEAM_HOME") or str(Path.home() / ".tagteam"))


def parse_iso(text):
    try:
        return datetime.strptime(text, ISO).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(when=None):
    return (when or utcnow()).strftime(ISO)


def cache_path(home, item):
    return os.path.join(home, "cache", "aa.json" if item == "aa" else "cli-%s.json" % item)


def read_cache(home, item):
    """The cache entry of one item ("aa" or a vendor), or None when it is missing or unusable."""
    try:
        entry = load(cache_path(home, item), False)
    except (OSError, ValueError):
        return None
    if entry.get("schema") != CACHE_SCHEMA or parse_iso(entry.get("fetched")) is None:
        return None
    if item == "aa":
        ok = isinstance(entry.get("rows"), list) and "index_version" in entry
    else:
        listing = entry.get("listing")
        ok = isinstance(listing, dict) and (
            (isinstance(listing.get("families"), list)) if item == "claude"
            else (isinstance(listing.get("ids"), list) and isinstance(listing.get("models"), dict)))
    return entry if ok else None


def write_cache(home, item, entry):
    write_json(cache_path(home, item), dict(entry, schema=CACHE_SCHEMA))


def is_fresh(entry):
    age = utcnow() - parse_iso(entry["fetched"])
    return -timedelta(days=1) <= age <= timedelta(days=CACHE_MAX_AGE_DAYS)  # a far-future stamp is not fresh either


def cache_line(home, items):
    parts = []
    for item in items:
        entry = read_cache(home, item)
        days = None if entry is None else max(0, (utcnow() - parse_iso(entry["fetched"])).days)
        parts.append("%s %s" % (item, "none" if days is None else "%dd" % days))
    return "cache: " + ", ".join(parts)


def try_os_lock(fd):
    """Take an advisory lock on the open file without waiting. True when taken, False when another holder has it."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def os_unlock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the file drops the lock anyway


@contextlib.contextmanager
def machine_lock(home, name, wait=None):
    """Hold an OS-level advisory lock on the file <home>/<name> (fcntl.flock, msvcrt.locking on byte 0). Polls every
    LOCK_POLL_SECONDS for `wait` seconds (default LOCK_WAIT_SECONDS). Yields True (held), False (still taken after the
    wait) or None (the file cannot be locked at all; the caller goes on without). The OS drops the lock when the process
    ends, however it ends, so there is no stale lock to detect; the file itself is never deleted. The installer uses the
    same file names (refresh.lock for the caches, registry.lock for projects.json) and the same kind of lock."""
    wait = LOCK_WAIT_SECONDS if wait is None else wait
    fd, held = None, False
    try:
        os.makedirs(home, exist_ok=True)
        fd = os.open(os.path.join(home, name), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        deadline = time.monotonic() + wait
        while True:
            if try_os_lock(fd):
                held = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(LOCK_POLL_SECONDS)
    except OSError:
        held = None
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                os_unlock(fd)
            os.close(fd)


def read_attempts(home):
    """{item: {"at", "ts", "status", "detail"?}}: the last fetch attempt of every item, failed or not."""
    try:
        data = load(os.path.join(home, "cache", ATTEMPTS_FILE), False).get("attempts")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_attempt(home, attempts, item, status):
    entry = {"at": iso(), "ts": time.time(), "status": status.get("status")}
    if status.get("detail"):
        entry["detail"] = status["detail"]
    attempts[item] = entry
    try:
        write_json(os.path.join(home, "cache", ATTEMPTS_FILE), {"schema": CACHE_SCHEMA, "attempts": attempts})
    except OSError as exc:
        warn("cannot record the %s attempt: %s" % (item, exc))


def refresh_item(home, item, key, timeout):
    """Fetch one item and store it when the fetch worked. -> (status dict, new cache entry or None).
    A cache that cannot be written is a failed refresh: the status says so and nothing is used."""
    if item == "aa":
        status, body = fetch_aa(key, timeout)
    else:
        status, body = fetch_vendor(item, timeout, home)
    status = scrub(status)
    if body is None:
        return status, None
    entry = scrub(dict(body, schema=CACHE_SCHEMA, fetched=iso()))
    try:
        write_cache(home, item, entry)
    except OSError as exc:
        return {"status": "error", "detail": scrub("cannot write the %s cache: %s" % (item, exc))[:200]}, None
    return status, entry


def refresh_caches(home, items, key, timeout, force, since):
    """Bring the caches of `items` up to date. -> ({item: (status, entry)} for what this call fetched or what failed, busy).

    An item is stale when it has no usable cache, the cache is older than CACHE_MAX_AGE_DAYS, or `force` is set and
    the cache file was last written before `since` (time.time() at the start of this run: a refresh another process
    finished meanwhile counts). Nothing stale means no lock and no network. Stale items are fetched under
    refresh.lock, after checking again, so two runs that start together fetch once.
    Every attempt is recorded in cache/attempts.json, failed ones too: a stale item whose last attempt is newer than
    `since` is not fetched again, it is reported with the failure the other run saw (the old cache stays on disk).
    busy: the lock stayed taken for LOCK_WAIT_SECONDS and nothing was fetched."""
    def needs(item):
        entry = read_cache(home, item)
        if entry is None:
            return True
        if force:
            try:
                if os.path.getmtime(cache_path(home, item)) < since:
                    return True
            except OSError:
                return True
        return not is_fresh(entry)

    def decide(attempts):
        """{item: None (up to date) | status dict of a failed attempt made since we started | "fetch"}"""
        out = {}
        for item in items:
            if not needs(item):
                out[item] = None
                continue
            a = attempts.get(item)
            seen = isinstance(a, dict) and isinstance(a.get("ts"), (int, float)) and a["ts"] >= since \
                and a.get("status") != "ok"
            out[item] = {k: a[k] for k in ("status", "detail") if k in a} if seen else "fetch"
        return out

    def reused(plan):
        return {i: (d, None) for i, d in plan.items() if isinstance(d, dict)}
    plan = decide(read_attempts(home))
    if "fetch" not in plan.values():
        return reused(plan), False
    with machine_lock(home, LOCK_NAME) as held:
        if held is False:
            return {}, True
        attempts = read_attempts(home)
        plan = decide(attempts)
        results = reused(plan)
        for item in items:
            if plan[item] == "fetch":
                results[item] = refresh_item(home, item, key, timeout)
                record_attempt(home, attempts, item, results[item][0])
    return results, False


def vendor_status(vendor, models, local, home, results, busy):
    """-> (cli status dict, cache entry or None, listing for this project or None). Reads the cache (or this run's fetch)."""
    if (local.get("plans") or {}).get(vendor) == "none":
        return {"status": "skipped"}, None, None
    if vendor not in LISTERS:
        return {"status": "unlisted"}, None, None
    if vendor in results:
        status, entry = results[vendor]
        if status["status"] != "ok":
            return status, None, None
    else:
        entry = read_cache(home, vendor)
        if entry is None:
            return {"status": "error", "detail": "no cache (refresh lock busy)" if busy else "no cache"}, None, None
        status = {"status": "ok"}
        if entry.get("version"):
            status["version"] = entry["version"]
    try:
        listing = listing_for(vendor, entry["listing"], models)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"status": "error", "detail": ("%s: %s" % (type(exc).__name__, exc))[:200]}, None, None
    return status, entry, listing


def bench_status(home, results, busy, key):
    """-> (bench status dict, entries) from this run's fetch or the cache. Without a cache the status is skipped."""
    if "aa" in results:
        status, entry = results["aa"]
        if status["status"] != "ok":
            return status, []
        return bench_from_entry(entry, False)
    entry = read_cache(home, "aa")
    if entry is None:
        if key and busy:
            return {"status": "error", "detail": "no cache (refresh lock busy)"}, []
        return {"status": "skipped"}, []
    return bench_from_entry(entry, True)


def refresh_vendors(models, local):
    return [v for v in sorted(models["vendors"]) if v in LISTERS and (local.get("plans") or {}).get(v) != "none"]


def write_bench(cdir, timeout, home, force, since):
    """--write-bench: 0 written or no data, 1 AA error (file untouched), 2 config error."""
    try:
        models, _ = load_config(cdir)
    except (OSError, ValueError) as exc:
        warn(exc)
        return 2
    key = os.environ.get("TAGTEAM_AA_API_KEY", "").strip()
    results, busy = refresh_caches(home, ["aa"] if key else [], key, timeout, force, since)
    print(cache_line(home, ["aa"]))
    bench, entries = bench_status(home, results, busy, key)
    if bench["status"] == "skipped":
        print("no TAGTEAM_AA_API_KEY set and no usable Artificial Analysis cache: bench.local.json not written")
        return 0
    if bench["status"] != "ok":
        warn("AA error: %s; %s unchanged" % (bench.get("detail"), BENCH_FILE))
        return 1
    path = os.path.join(cdir, BENCH_FILE)
    old = (load_bench(cdir) or {}).get("models")
    if old is None and os.path.exists(path):
        warn("%s exists but is unreadable or invalid; fix or delete it first" % BENCH_FILE)
        return 1
    data = build_bench(models, entries, bench["index_version"], utcnow().strftime("%Y-%m-%d"), old)
    write_json(path, data)
    print("wrote %s: %d models, index_version %s" % (BENCH_FILE, len(data["models"]), bench["index_version"]))
    return 0


def write_available(cdir, timeout, home, force, since):
    """--write-available: 0 written (or nothing to write), 2 config error."""
    try:
        models, local = load_config(cdir)
    except (OSError, ValueError) as exc:
        warn(exc)
        return 2
    vendors = refresh_vendors(models, local)
    results, busy = refresh_caches(home, vendors, "", timeout, force, since)
    got = {}
    for vendor in sorted(models["vendors"]):
        status, entry, listing = vendor_status(vendor, models, local, home, results, busy)
        print("%s: %s" % (vendor, status["status"]))
        if listing:
            got[vendor] = {"checked": entry["fetched"][:10], "ids": listing["ids"]}
    print(cache_line(home, vendors))
    if got:
        try:  # reload: keep edits made while the CLIs were answering
            local = load(os.path.join(cdir, "models.local.json"), False)
        except (OSError, ValueError) as exc:
            warn("models.local.json changed and is unreadable: %s" % exc)
            return 2
        local.setdefault("available", {}).update(got)
        write_json(os.path.join(cdir, "models.local.json"), local)
        print("wrote available for %s to models.local.json" % ", ".join(sorted(got)))
    return 0


def norm_path(path):
    return os.path.normcase(os.path.abspath(path))


def read_projects(home):
    """The registered projects: a list of entries, or None when projects.json cannot be read."""
    path = os.path.join(home, "projects.json")
    if not os.path.exists(path):
        return []
    try:
        data = load(path, True)
    except (OSError, ValueError):
        return None
    projects = data.get("projects")
    if not isinstance(projects, list):
        return None
    return [p for p in projects if isinstance(p, dict) and isinstance(p.get("path"), str)]


def save_project_results(home, last, removed):
    """Write each project's last result and drop the removed ones. projects.json is read again under registry.lock,
    the lock the installer holds while it adds or removes a project, so no change of either side is lost."""
    path = os.path.join(home, "projects.json")
    with machine_lock(home, REGISTRY_LOCK) as held:
        if held is False:
            warn("projects.json not updated: %s is held by another process" % REGISTRY_LOCK)
            return False
        try:
            data = load(path, True)
            projects = data["projects"]
            if not isinstance(projects, list):
                raise ValueError("projects is not a list")
        except (OSError, ValueError, KeyError) as exc:
            warn("projects.json not updated: %s" % exc)
            return False
        kept = []
        for entry in projects:
            key = norm_path(entry["path"]) if isinstance(entry, dict) and isinstance(entry.get("path"), str) else None
            if key in removed:
                continue
            if key in last:
                entry = dict(entry, last=last[key])
            kept.append(entry)
        try:
            write_json(path, dict(data, projects=kept))
        except OSError as exc:
            warn("projects.json not updated: %s" % exc)
            return False
    return True


def weekly_log(home, text):
    text = scrub(text)
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "weekly.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (iso(datetime.now(timezone.utc)), text))
    except OSError:
        pass


def run_project(proj, script, home):
    """Run one project's own collector on the caches. -> (exit code or -1, one line of what it said). The child gets
    no AA key (it reads the caches only) and the absolute machine folder (it runs in the project folder)."""
    try:
        code, out, err = run_cli([sys.executable, script, "--from-cache", "--project", proj], PROJECT_TIMEOUT,
                                 cwd=proj, env=child_env(TAGTEAM_HOME=home))
    except subprocess.TimeoutExpired:
        return -1, "timeout after %ss" % PROJECT_TIMEOUT
    except OSError as exc:
        return -1, ("%s: %s" % (type(exc).__name__, exc))[:200]
    lines = [x for x in (out.strip().splitlines() or err.strip().splitlines()) if x.strip()]
    return code, scrub(re.sub(r"^exit=-?\d+ ", "", lines[-1] if lines else "")[:200])


def weekly(args, home, since):
    """--weekly: refresh every cache, then let each registered project write its proposal from the caches.
    Exit 0 all well, 1 a refresh or a project failed, 3 every refresh failed."""
    key = os.environ.get("TAGTEAM_AA_API_KEY", "").strip()
    items = sorted(LISTERS) + (["aa"] if key else [])
    results, busy = refresh_caches(home, items, key, args.timeout, True, since)
    outcome = {}
    for item in items:
        outcome[item] = results[item][0]["status"] if item in results else ("error" if busy else "ok")
    failed = [i for i in items if outcome[i] != "ok"]
    shown = dict(outcome, **({} if key else {"aa": "skipped"}))
    order = sorted(LISTERS) + ["aa"]
    why = "".join("; %s: %s" % (i, results[i][0]["detail"]) for i in order if i in results and results[i][0].get("detail"))
    weekly_log(home, "refresh %s%s%s" % (" ".join("%s=%s" % (i, shown[i]) for i in order),
                                         " (refresh lock busy)" if busy else "", why))
    code = 3 if len(failed) == len(items) else (1 if failed else 0)
    projects = read_projects(home)
    last, removed, project_failed = {}, set(), 0
    if projects is None:
        weekly_log(home, "projects.json unreadable: no project processed")
        warn("projects.json is unreadable; no project processed")
        code = max(code, 1)
        projects = []
    for entry in projects:
        proj = entry["path"]
        script = os.path.join(proj, ".claude", "tagteam", "models_collect.py")
        reason = "folder missing" if not os.path.isdir(proj) else (
            "collector missing" if not os.path.isfile(script) else None)
        if reason:
            removed.add(norm_path(proj))
            weekly_log(home, "project %s removed from the list: %s" % (proj, reason))
            continue
        result, note = run_project(proj, script, home)
        last[norm_path(proj)] = {"at": iso(datetime.now(timezone.utc)), "exit": result}
        if result != 0:
            project_failed += 1
            code = max(code, 1)
        weekly_log(home, "project %s exit=%d %s" % (proj, result, note))
    if (last or removed) and not save_project_results(home, last, removed):
        code = max(code, 1)
    summary = "weekly: exit=%d refresh %s projects=%d failed=%d removed=%d" % (
        code, " ".join("%s=%s" % (i, shown[i]) for i in order), len(last), project_failed, len(removed))
    if not stdout_is(os.path.join(home, "weekly.log")):  # launchd and cron send stdout to weekly.log already
        weekly_log(home, summary)  # the Windows task keeps no stdout at all
    print(summary)
    return code


def stdout_is(path):
    """True when our stdout is that very file: a regular file with the same device and inode (a Windows pipe or console
    can report 0 for both, so only a regular file counts)."""
    try:
        out, there = os.fstat(sys.stdout.fileno()), os.stat(path)
        return stat.S_ISREG(out.st_mode) and out.st_ino != 0 and os.path.samestat(out, there)
    except (OSError, ValueError, AttributeError):  # no stdout, a stream without a descriptor, or no log yet
        return False


def main(argv):
    ap = argparse.ArgumentParser(description="Collect vendor model lists into a proposal file.")
    ap.add_argument("--project")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--refresh", action="store_true", help="fetch again even when the cache is fresh")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--mark-applied", action="store_true")
    modes.add_argument("--write-bench", action="store_true")
    modes.add_argument("--write-available", action="store_true")
    modes.add_argument("--from-cache", action="store_true", help="write the proposal from the cache, no network")
    modes.add_argument("--weekly", action="store_true", help="the machine-wide weekly job")
    args = ap.parse_args(argv)
    if args.refresh and args.from_cache:
        ap.error("--refresh cannot be combined with --from-cache")
    since = time.time()
    home = tagteam_home()
    if args.weekly:
        return weekly(args, home, since)
    here = os.path.dirname(os.path.abspath(__file__))
    cdir = os.environ.get("TAGTEAM_CONFIG_DIR") or here
    if args.write_bench:
        return write_bench(cdir, args.timeout, home, args.refresh, since)
    if args.write_available:
        return write_available(cdir, args.timeout, home, args.refresh, since)
    project = os.path.abspath(args.project or os.path.dirname(os.path.dirname(here)))
    state = statelib.state_dir(project)
    try:
        statelib.ensure_writable(state)
    except statelib.StateError as exc:
        warn(exc)
        return 2
    proposal = os.path.join(state, "model-update.json")
    if args.mark_applied:
        try:
            current = load(proposal, True)
        except (OSError, ValueError) as exc:
            warn("no proposal to mark: %s" % exc)
            return 2
        write_json(os.path.join(state, "model-update.applied"), {
            "digest": current.get("digest"), "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        print("marked applied: %s" % current.get("digest"))
        return 0
    try:
        models, local = load_config(cdir)
    except (OSError, ValueError) as exc:
        warn(exc)
        log(state, "config error: %s" % exc)
        return 2
    key = "" if args.from_cache else os.environ.get("TAGTEAM_AA_API_KEY", "").strip()
    vendors = refresh_vendors(models, local)
    results, busy = ({}, False) if args.from_cache else refresh_caches(
        home, vendors + (["aa"] if key else []), key, args.timeout, args.refresh, since)
    cli, avail, changes = {}, {}, []
    for vendor in sorted(models["vendors"]):
        cli[vendor], entry, listing = vendor_status(vendor, models, local, home, results, busy)
        if listing:
            avail[vendor] = {"checked": entry["fetched"][:10], "ids": listing["ids"]}
            changes += compare(vendor, listing, models, local)
    bench, entries = bench_status(home, results, busy, key)
    stored = load_bench(cdir)
    stored = stored["models"] if stored else None
    if bench["status"] == "ok":  # judge grades on the fresh values the apply step will write
        fresh = build_bench(models, entries, bench["index_version"], "", None)["models"]
        changes += grade_changes(models, fresh)
    else:
        changes += grade_changes(models, stored)
    if bench["status"] == "ok":
        now = utcnow()
        if stored is None:
            bench["local"] = "missing"
            bench["hint"] = "run models_collect.py --write-bench to create %s" % BENCH_FILE
        for vendor in sorted(models["vendors"]):
            changes += compare_bench(vendor, entries, models, bench["index_version"], now, stored)
        changes = merge_new_models(changes)
    changes.sort(key=lambda c: (c["kind"], c["vendor"], c.get("model") or c.get("id"),
                                c.get("metric") or "", c.get("effort") or ""))
    write_json(proposal, {"schema": 1, "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "project": project.replace("\\", "/"), "cli": cli, "available": avail,
                          "changes": changes, "bench": dict(bench, source=AA_SOURCE),
                          "digest": digest(changes, cli, bench)})
    targets = [v for v, c in cli.items() if c["status"] in ("ok", "error", "missing")]
    failed = [v for v in targets if cli[v]["status"] != "ok"]
    code = 3 if targets and len(failed) == len(targets) else (1 if failed else 0)
    if bench["status"] == "error":
        code = max(code, 1)
    summary = "exit=%d %s bench=%s changes=%d" % (
        code, " ".join("%s=%s" % (v, c["status"]) for v, c in cli.items()), bench["status"], len(changes))
    log(state, summary)
    print(cache_line(home, vendors + ["aa"]))
    print(summary)
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
