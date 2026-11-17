#!/usr/bin/env python3
"""Collect the model lists the vendor CLIs offer and write a proposal file.

  models_collect.py [--project DIR] [--timeout 60] [--mark-applied]

No LLM, no model run, no edit of models.json or models.local.json. Writes
<state dir>/model-update.json (overwritten each run) and appends to collect.log.
--mark-applied records the current proposal's digest in <state dir>/model-update.applied.
Exit: 0 collected, 1 some vendor error or missing, 2 usage or config error, 3 every vendor failed.
Config dir: next to this script (TAGTEAM_CONFIG_DIR in tests).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import role_guard  # noqa: E402
import run  # noqa: E402  (resolve_cli, kill_tree; its CLI entry is behind a __main__ guard)

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SUFFIX = re.compile(r"^(.*)-(%s)$" % "|".join(EFFORTS))
LABEL_SUFFIX = re.compile(r"\s*\((?:%s)\)$" % "|".join(e.title() for e in EFFORTS), re.I)


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
        cli_ids = sorted({arg.format(model=mid, effort=e) for e in m["efforts"]})
        now = [c for c in cli_ids if c in ids and c not in had]
        gone = [c for c in cli_ids if c not in ids and c in had]
        for kind, found in (("now_available", now), ("no_longer_available", gone)):
            if found:
                changes.append({"kind": kind, "vendor": vendor, "model": mid, "cli_ids": found})
        if mid in info:
            dropped = [e for e in m["efforts"] if e not in info[mid]["efforts"]]
            if dropped:
                changes.append({"kind": "efforts_dropped", "vendor": vendor, "model": mid, "efforts": dropped})
        elif vendor == "openai" and not m.get("candidate") \
                and ((local.get("models") or {}).get(mid) or {}).get("enabled") is not False:
            changes.append({"kind": "missing_registered", "vendor": vendor, "model": mid})
    return changes


def digest(changes, cli):
    body = {"changes": changes, "cli": {v: c["status"] for v, c in sorted(cli.items())}}
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


def main(argv):
    ap = argparse.ArgumentParser(description="Collect vendor model lists into a proposal file.")
    ap.add_argument("--project")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--mark-applied", action="store_true")
    args = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
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
        cdir = os.environ.get("TAGTEAM_CONFIG_DIR") or here
        models = load(os.path.join(cdir, "models.json"), True)
        local = load(os.path.join(cdir, "models.local.json"), False)
        for key in ("vendors", "models"):
            if not isinstance(models.get(key), dict):
                raise ValueError("models.json: %r must be an object" % key)
        for mid, m in models["models"].items():
            if not (isinstance(m, dict) and m.get("vendor") in models["vendors"]
                    and isinstance(m.get("efforts"), list)):
                raise ValueError("models.json: model %s needs a known vendor and an efforts list" % mid)
        if any(not isinstance(v, dict) or "model_arg" not in v for v in models["vendors"].values()):
            raise ValueError("models.json: every vendor needs a model_arg")
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
    changes.sort(key=lambda c: (c["kind"], c["vendor"], c.get("model") or c.get("id")))
    write_json(proposal, {"schema": 1, "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "project": project.replace("\\", "/"), "cli": cli, "available": avail,
                          "changes": changes, "digest": digest(changes, cli)})
    targets = [v for v, c in cli.items() if c["status"] in ("ok", "error", "missing")]
    failed = [v for v in targets if cli[v]["status"] != "ok"]
    code = 3 if targets and len(failed) == len(targets) else (1 if failed else 0)
    summary = "exit=%d %s changes=%d" % (code, " ".join("%s=%s" % (v, c["status"]) for v, c in cli.items()), len(changes))
    log(state, summary)
    print(summary)
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
