#!/usr/bin/env python3
"""Collect the model lists the vendor CLIs offer and write a proposal file.

  models_collect.py [--project DIR] [--timeout 60] [--mark-applied]
  models_collect.py --write-bench [--timeout 60]

No LLM, no model run, no edit of models.json or models.local.json. --write-bench rewrites
bench.local.json (next to models.json, machine-local, git-ignored) from Artificial Analysis for
every registered model and effort that has a value; entries of other sources are kept. Without a
key it writes nothing and exits 0; on an AA error it exits 1 and leaves the file alone. Writes
<state dir>/model-update.json (overwritten each run) and appends to collect.log.
--mark-applied records the current proposal's digest in <state dir>/model-update.applied.
With env TAGTEAM_AA_API_KEY set, also reads the Artificial Analysis /free model list (urllib,
max 10 pages) for vendors that have "aa_creator" in models.json: exact slug+effort matching,
bench_changed/bench_added (compared with bench.local.json; no file = no bench changes and
bench.local "missing") and AA-sourced new_model changes, and a "bench" block in the proposal. Without
the key bench.status is "skipped" and nothing is requested. The key is never written anywhere.
grade_changed: bench.local.json index values put against grading.floors give another grade than models.json
grades (needs the file, not the key); AA-sourced new_model changes carry proposed grades from the floors.
Data source: Artificial Analysis (https://artificialanalysis.ai/).
Exit: 0 collected, 1 some vendor error or missing (or key set and bench error), 2 usage or
config error, 3 every vendor CLI failed.
Config dir: next to this script (TAGTEAM_CONFIG_DIR in tests).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import role_guard  # noqa: E402
import run  # noqa: E402  (resolve_cli, kill_tree; its CLI entry is behind a __main__ guard)

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SUFFIX = re.compile(r"^(.*)-(%s)$" % "|".join(EFFORTS))
AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_SOURCE = "Artificial Analysis (https://artificialanalysis.ai/)"
AA_MAX_PAGES = 10
METRICS = ("index", "cost_per_task")
BENCH_FILE = "bench.local.json"
NAME_EFFORT = re.compile(r"\(([^)]*)\)\s*$")
LABEL_SUFFIX = re.compile(r"\s*\((?:%s)\)$" % "|".join(e.title() for e in EFFORTS), re.I)


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


def parse_codex(text):
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


def parse_agy(text):
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


LISTERS = {"openai": (["debug", "models"], parse_codex), "google": (["models"], parse_agy)}


def run_cli(cmd, timeout):
    """(returncode, stdout, stderr) or raises subprocess.TimeoutExpired after killing the tree."""
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        run.kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return proc.returncode, out, err


def collect_vendor(vendor, models, local, timeout):
    """-> (cli status dict, listing or None)."""
    if (local.get("plans") or {}).get(vendor) == "none":
        return {"status": "skipped"}, None
    if vendor not in LISTERS:
        return {"status": "unlisted"}, None
    cli = run.resolve_cli(vendor, models)
    if not cli:
        return {"status": "missing"}, None
    args, parser = LISTERS[vendor]
    try:
        code, out, err = run_cli(cli + args, timeout)
        if code != 0:
            return {"status": "error", "detail": (err.strip().splitlines() or ["exit %d" % code])[0][:200]}, None
        ids, info = parser(out)
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": "timeout after %ss" % timeout}, None
    except (OSError, ValueError) as exc:
        return {"status": "error", "detail": ("%s: %s" % (type(exc).__name__, exc))[:200]}, None
    if not ids:
        return {"status": "error", "detail": "no models parsed from output"}, None
    status = {"status": "ok"}
    try:
        status["version"] = run_cli(cli + ["--version"], 10)[1].strip().splitlines()[0][:100]
    except (subprocess.TimeoutExpired, OSError, IndexError):
        pass
    return status, {"ids": ids, "models": info}


def compare(vendor, listing, models, local):
    changes = []
    registered = {k: m for k, m in models["models"].items() if m["vendor"] == vendor}
    unreg = (models.get("unregistered") or {}).get(vendor) or {}
    had = set(((local.get("available") or {}).get(vendor) or {}).get("ids") or [])
    ids, info = set(listing["ids"]), listing["models"]
    for base, i in info.items():
        if base not in registered and base not in unreg:
            changes.append({"kind": "new_model", "vendor": vendor, "id": base, "label": i["label"],
                            "efforts": sorted(i["efforts"], key=EFFORTS.index)})
    for mid, m in registered.items():
        arg = models["vendors"][vendor]["model_arg"]
        cli_ids = sorted({arg.format(model=mid, effort=e) for e in efforts_of(m)})
        now = [c for c in cli_ids if c in ids and c not in had]
        gone = [c for c in cli_ids if c not in ids and c in had]
        for kind, found in (("now_available", now), ("no_longer_available", gone)):
            if found:
                changes.append({"kind": kind, "vendor": vendor, "model": mid, "cli_ids": found})
        if mid in info:
            dropped = [e for e in efforts_of(m) if e not in info[mid]["efforts"]]
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


def collect_bench(key, timeout):
    """-> (bench status dict, entries). The key never reaches the status."""
    if not key:
        return {"status": "skipped"}, []
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
        entries = aa_entries(rows)
    except urllib.error.HTTPError as exc:
        detail = "HTTP %d" % exc.code
        if exc.code == 429:
            wait = str((exc.headers.get("Retry-After") if exc.headers else "") or "").strip()
            detail += ", Retry-After %s" % (wait if wait.isdigit() else "?")
        return {"status": "error", "detail": detail}, []
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:  # URLError, timeouts, JSON, bad shapes
        return {"status": "error", "detail": ("%s: %s" % (type(exc).__name__, exc)).replace(key, "***")[:200]}, []
    return {"status": "ok", "index_version": version, "models_seen": len(rows), "remaining": remaining}, entries


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
        if m["vendor"] != vendor:
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
                changes.append({"kind": "bench_changed", "vendor": vendor, "model": mid, "effort": b["effort"],
                                "metric": b["metric"], "old": b.get("value"), "new": new, "index_version": version})
        for effort in efforts_of(m):
            for metric in METRICS:
                new = aa_value(by, models, mid, effort, metric)
                if new is not None and (effort, metric) not in have:
                    changes.append({"kind": "bench_added", "vendor": vendor, "model": mid, "effort": effort,
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
            if m["vendor"] != vendor:
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


def write_json(path, data):
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
    return models, local


def write_bench(cdir, timeout):
    """--write-bench: 0 written or no key, 1 AA error (file untouched), 2 config error."""
    try:
        models, _ = load_config(cdir)
    except (OSError, ValueError) as exc:
        sys.stderr.write("models_collect: %s\n" % exc)
        return 2
    key = os.environ.get("TAGTEAM_AA_API_KEY", "").strip()
    if not key:
        print("no TAGTEAM_AA_API_KEY set: bench.local.json not written")
        return 0
    bench, entries = collect_bench(key, timeout)
    if bench["status"] != "ok":
        sys.stderr.write("models_collect: AA error: %s; %s unchanged\n" % (bench.get("detail"), BENCH_FILE))
        return 1
    path = os.path.join(cdir, BENCH_FILE)
    old = (load_bench(cdir) or {}).get("models")
    if old is None and os.path.exists(path):
        sys.stderr.write("models_collect: %s exists but is unreadable or invalid; fix or delete it first\n" % BENCH_FILE)
        return 1
    data = build_bench(models, entries, bench["index_version"], utcnow().strftime("%Y-%m-%d"), old)
    write_json(path, data)
    print("wrote %s: %d models, index_version %s" % (BENCH_FILE, len(data["models"]), bench["index_version"]))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="Collect vendor model lists into a proposal file.")
    ap.add_argument("--project")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--mark-applied", action="store_true")
    ap.add_argument("--write-bench", action="store_true")
    args = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    cdir = os.environ.get("TAGTEAM_CONFIG_DIR") or here
    if args.write_bench:
        return write_bench(cdir, args.timeout)
    project = os.path.abspath(args.project or os.path.dirname(os.path.dirname(here)))
    state = role_guard.state_dir({"cwd": project})
    proposal = os.path.join(state, "model-update.json")
    if args.mark_applied:
        try:
            current = load(proposal, True)
        except (OSError, ValueError) as exc:
            sys.stderr.write("models_collect: no proposal to mark: %s\n" % exc)
            return 2
        write_json(os.path.join(state, "model-update.applied"), {
            "digest": current.get("digest"), "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        print("marked applied: %s" % current.get("digest"))
        return 0
    try:
        models, local = load_config(cdir)
    except (OSError, ValueError) as exc:
        sys.stderr.write("models_collect: %s\n" % exc)
        log(state, "config error: %s" % exc)
        return 2
    cli, avail, changes = {}, {}, []
    for vendor in sorted(models["vendors"]):
        cli[vendor], listing = collect_vendor(vendor, models, local, args.timeout)
        if listing:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            avail[vendor] = {"checked": today, "ids": listing["ids"]}
            changes += compare(vendor, listing, models, local)
    key = os.environ.get("TAGTEAM_AA_API_KEY", "").strip()
    bench, entries = collect_bench(key, args.timeout)
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
    if key and bench["status"] == "error":
        code = max(code, 1)
    summary = "exit=%d %s bench=%s changes=%d" % (
        code, " ".join("%s=%s" % (v, c["status"]) for v, c in cli.items()), bench["status"], len(changes))
    log(state, summary)
    print(summary)
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
