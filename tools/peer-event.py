#!/usr/bin/env python3
"""Append-only event log for peer-session missions, and a report derived from it (experimental).

Each session writes only its own file: <mission>/events/<session>.jsonl (one JSON object per
line). Lane worktrees therefore never conflict on merge. `report` merges every file under
<mission>/events/, sorts by timestamp and derives the send/receive table, per-lane and
per-phase timings (lane seconds, phase span, serial sum vs span) and cost sums.

Usage:
  python tools/peer-event.py log --mission docs/missions/<m> --session loop-fable --kind send \
      --msg DESIGN --phase 1 --lane a --peer lane-a [--head 7sha] [--note "..."]
  python tools/peer-event.py log --mission ... --session lane-a --kind cost --phase 1 \
      --run 20261012T081756Z-59707 --tokens 42751 [--usd 0.5]
  python tools/peer-event.py report --mission docs/missions/<m> [--json]

Kinds: send, recv, notice (idle notice), guard (mission guard re-armed), merge, eval,
verdict, stop, cost, note. Messages (--msg): DESIGN, ASK, DECIDE, DONE, PROBE, IDLE, or free text.
Standard library only.

Scope and limits (stated, not solved):
- One writer per file; the tool never edits an existing line. Corrections are new `correction`
  records carrying `--at`; they attach to the record with the same msg_id (else the same session,
  msg, phase and lane whose kind is the prefix of the note, e.g. `send: ...`). Unmatched corrections
  change nothing and are listed as applied counts only.
- Pairing requires an unambiguous send/recv ID and matching metadata. Missing observations,
  missing IDs, duplicate IDs and invalid reply references are informational report anomalies;
  a clean report is not proof of delivery. Logging never scans other writers' files.
- Timings use `at` (corrected) and fall back to `ts`; a DONE before its DESIGN is reported as a
  clock error, not as a negative duration. Costs sum `cost` records once per run id and keep the
  unmeasured count apart. The tool measures; it makes no efficiency claim.
- `--transport codex` records separately submitted native messages or an external deliverer;
  this tool supplies no transport (the Claude desktop transport is SendMessage). `--ref` is
  USER-ANSWER provenance; `--reply-to` references an earlier outbound ID. USER-ANSWER records
  are refused outside a trust directory so a peer cannot forge a user decision inside the mission.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Iterable, Optional

KINDS = ("send", "recv", "notice", "guard", "merge", "eval", "verdict", "stop", "cost", "note", "correction")
FIELDS = ("ts", "at", "session", "kind", "phase", "lane", "peer", "msg", "head", "run", "tokens", "usd", "note",
          "transport", "body", "msg_id", "reply_to", "files", "ref")
# `ts` is when the line was written; `at` (optional) is when the thing happened. A `correction`
# record re-states `at` for an earlier record of the same session (matched by msg_id, else by
# kind+msg+phase+lane) — the original line is never edited.


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> Optional[_dt.datetime]:
    """Accept 2026-10-12T08:16:22Z, with fractional seconds, or +00:00 offsets."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def events_dir(mission: str) -> str:
    return os.path.join(mission, "events")


def mission_key(mission: str) -> str:
    import hashlib
    return hashlib.sha256(os.path.abspath(mission).replace("\\", "/").lower().encode("utf-8")).hexdigest()[:16]


def default_trust_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "peer-relay")


def apply_corrections(records: list) -> list:
    """Return records with `at` filled (default ts) and corrections applied; corrections stay in the
    list (kind correction) so the report can show them."""
    for record in records:
        record.setdefault("at", record.get("ts"))
    for correction in [r for r in records if r.get("kind") == "correction"]:
        target = None
        for record in records:
            if record is correction or record.get("kind") == "correction" or record.get("session") != correction.get("session"):
                continue
            if correction.get("msg_id") and record.get("msg_id") == correction.get("msg_id"):
                target = record
            elif not correction.get("msg_id") and all(str(record.get(f)) == str(correction.get(f)) for f in ("msg", "phase", "lane")) \
                    and record.get("kind") == correction.get("note", "").split(":")[0]:
                target = record
        if target is not None:
            target["at"] = correction["at"]
            target["corrected"] = True
    return records


def safe_name(session: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in session) or "session"


def log_event(args: argparse.Namespace) -> int:
    record = {
        "ts": args.ts or utc_now(),
        "at": args.at,
        "session": args.session,
        "kind": args.kind,
        "phase": args.phase,
        "lane": args.lane,
        "peer": args.peer,
        "msg": args.msg,
        "head": args.head,
        "run": args.run,
        "tokens": args.tokens,
        "usd": args.usd,
        "note": args.note,
        "transport": args.transport,
        "body": args.body,
        "msg_id": args.msg_id,
        "reply_to": args.reply_to,
        "files": args.files,
        "ref": args.ref,
    }
    if parse_ts(record["ts"]) is None or (record["at"] is not None and parse_ts(record["at"]) is None):
        print("error: --ts/--at must be ISO 8601 (e.g. 2026-10-12T08:16:22Z)", file=sys.stderr)
        return 2
    if args.kind == "correction" and record["at"] is None:
        print("error: a correction needs --at (the corrected occurrence time)", file=sys.stderr)
        return 2
    if args.reply_to is not None and (not args.reply_to.strip() or args.reply_to == args.msg_id):
        print("error: --reply-to must be nonempty and different from --msg-id", file=sys.stderr)
        return 2
    if args.msg_id is not None and not args.msg_id.strip():
        print("error: --msg-id must be nonempty", file=sys.stderr)
        return 2
    if args.transport == "codex" and args.kind in ("send", "recv") and not args.msg_id:
        print("error: Codex send/recv needs --msg-id (receipts copy the outbound ID)", file=sys.stderr)
        return 2
    record = {key: value for key, value in record.items() if value is not None}
    trusted = args.kind == "send" and args.session == "orchestrator" and str(args.msg) == "USER-ANSWER"
    if trusted:
        if not args.trust_dir:
            print("error: a USER-ANSWER must be written to the trust directory: pass --trust-dir "
                  "(default root ~/.claude/peer-relay; a deliverer ignores USER-ANSWER inside the mission)", file=sys.stderr)
            return 2
        folder = os.path.join(args.trust_dir, mission_key(args.mission))
    else:
        folder = events_dir(args.mission)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name(args.session) + ".jsonl")
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
    print(line)
    return 0


def read_events(mission: str) -> tuple[list[dict], int]:
    folder = events_dir(mission)
    records: list[dict] = []
    skipped = 0
    if not os.path.isdir(folder):
        return records, skipped
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(folder, name), "rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    skipped += 1
                    continue
                if not isinstance(record, dict) or parse_ts(record.get("ts", "")) is None:
                    skipped += 1
                    continue
                record.setdefault("session", name[:-6])
                record["source_file"] = name
                record["source_line"] = line_number
                records.append(record)
    records = apply_corrections(records)
    records.sort(key=lambda item: (parse_ts(item.get("at") or item["ts"]), item.get("session", "")))
    return records, skipped


def seconds_between(start: Optional[dict], end: Optional[dict]) -> Optional[float]:
    if not start or not end:
        return None
    a, b = parse_ts(start.get("at") or start["ts"]), parse_ts(end.get("at") or end["ts"])
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)


def first(records: Iterable[dict], **match) -> Optional[dict]:
    for record in records:
        if all(str(record.get(key)) == str(value) for key, value in match.items()):
            return record
    return None


def last(records: Iterable[dict], **match) -> Optional[dict]:
    found = None
    for record in records:
        if all(str(record.get(key)) == str(value) for key, value in match.items()):
            found = record
    return found


def phases_of(records: list[dict]) -> list[str]:
    seen: list[str] = []
    for record in records:
        phase = record.get("phase")
        if phase is not None and str(phase) not in seen:
            seen.append(str(phase))
    return sorted(seen, key=lambda value: (len(value), value))


def lanes_of(records: list[dict], phase: str) -> list[str]:
    seen: list[str] = []
    for record in records:
        if str(record.get("phase")) == phase and record.get("lane") is not None:
            lane = str(record["lane"])
            if lane not in seen:
                seen.append(lane)
    return sorted(seen)


def fmt(value) -> str:
    return "n/a" if value is None else str(value)


def message_evidence(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep all observations; never pick a last-wins receipt for an ambiguous ID."""
    groups, unidentified, anomalies, pairs = {}, [], [], []

    def anomaly(identifier, kind, observations):
        anomalies.append({"msg_id": identifier, "type": kind,
                          "send_count": sum(r.get("kind") == "send" for r in observations),
                          "recv_count": sum(r.get("kind") == "recv" for r in observations),
                          "records": observations})

    for record in records:
        if record.get("kind") not in ("send", "recv"):
            continue  # corrections adjust times, not message counts
        identifier = record.get("msg_id")
        if not isinstance(identifier, str) or not identifier.strip():
            anomaly(None, "missing_id", [record])
            unidentified.append(record)
        else:
            groups.setdefault(identifier, []).append(record)
    outbound = {identifier for identifier, observations in groups.items()
                if any(r.get("kind") == "send" for r in observations)}
    for record in unidentified:
        if "reply_to" not in record:
            continue
        reference = record["reply_to"]
        if not isinstance(reference, str) or not reference.strip():
            anomaly(None, "invalid_reply_reference", [record])
        elif reference not in outbound:
            anomaly(None, "missing_reply_target", [record])
    for identifier, observations in sorted(groups.items()):
        sends = [r for r in observations if r.get("kind") == "send"]
        recvs = [r for r in observations if r.get("kind") == "recv"]
        if not sends:
            anomaly(identifier, "missing_send", observations)
        if len(sends) > 1:
            anomaly(identifier, "duplicate_send", observations)
        if len(recvs) > 1:
            anomaly(identifier, "duplicate_recv", observations)
        mismatch = len(sends) == 1 and any(
            any(recv.get(key) != sends[0].get(key) for key in ("msg", "phase", "reply_to"))
            for recv in recvs)
        if mismatch:
            anomaly(identifier, "metadata_mismatch", observations)
        for kind, selected in (
                ("invalid_reply_reference", [r for r in observations if "reply_to" in r and
                                            (not isinstance(r["reply_to"], str)
                                             or not r["reply_to"].strip() or r["reply_to"] == identifier)]),
                ("missing_reply_target", [r for r in observations if isinstance(r.get("reply_to"), str)
                                         and r["reply_to"].strip() and r["reply_to"] != identifier
                                         and r["reply_to"] not in outbound])):
            if selected:
                anomaly(identifier, kind, selected)
        if len(sends) != 1 or len(recvs) > 1 or mismatch:
            continue
        send, recv = sends[0], recvs[0] if recvs else None
        delta = seconds_between(send, recv) if recv else None
        pairs.append({"msg_id": identifier, "msg": send.get("msg"), "phase": send.get("phase"),
                      "sent_at": send.get("at"), "received_at": recv.get("at") if recv else None,
                      "seconds": None if delta is not None and delta < 0 else delta,
                      "problem": "missing (no recv)" if not recv else
                      ("reversed (recv before send)" if delta is not None and delta < 0 else None)})
    anomalies.sort(key=lambda a: (a["msg_id"] or "", a["type"],
                                 json.dumps(a["records"], sort_keys=True, ensure_ascii=False)))
    return pairs, anomalies


def build_report(records: list[dict]) -> dict:
    report = {"messages": [], "phases": [], "costs": []}
    for record in records:
        if record.get("kind") in ("send", "recv", "notice"):
            report["messages"].append(record)
    for phase in phases_of(records):
        row = {"phase": phase, "lanes": [], "span_seconds": None, "lane_sum_seconds": None,
               "saved_seconds": None}
        design_sends = [r for r in records if r.get("kind") == "send" and str(r.get("msg")) == "DESIGN"
                        and str(r.get("phase")) == phase]
        if not design_sends and not any(r.get("kind") in ("recv", "cost", "merge", "eval") and str(r.get("phase")) == phase
                                        for r in records):
            continue  # guard-only bookkeeping (e.g. phase 0) is not a measured phase
        # phase span ends at the last merge (parallel lanes) or, without merges, the last DONE receipt;
        # evaluation time is the designer's own work and is reported separately, not inside the span.
        ends = [r for r in records if str(r.get("phase")) == phase and r.get("kind") == "merge"]
        done_recvs = [r for r in records if r.get("kind") == "recv" and str(r.get("msg")) == "DONE"
                      and str(r.get("phase")) == phase]
        lane_total = 0.0
        lanes = lanes_of(records, phase) or [None]
        for lane in lanes:
            match = {"phase": phase}
            if lane is not None:
                match["lane"] = lane
            design = first(design_sends, **match)
            done = first(done_recvs, **match)
            seconds = seconds_between(design, done)
            reversed_clock = seconds is not None and seconds < 0
            if reversed_clock:
                seconds = None  # a DONE logged before its DESIGN is a clock/anchor error, not a measurement
            if seconds is not None:
                lane_total += seconds
            row["lanes"].append({"lane": lane, "design_sent": (design.get("at") or design["ts"]) if design else None,
                                 "done_received": (done.get("at") or done["ts"]) if done else None, "seconds": seconds,
                                 "reversed": reversed_clock,
                                 "skipped": bool(design and str(design.get("note", "")).startswith("skip"))})
        start = design_sends[0] if design_sends else None
        end = ends[-1] if ends else (done_recvs[-1] if done_recvs else None)
        row["span_seconds"] = seconds_between(start, end)
        if row["span_seconds"] is not None and row["span_seconds"] < 0:
            row["span_seconds"] = None
        measured = [lane for lane in row["lanes"] if lane["seconds"] is not None]
        row["lane_sum_seconds"] = round(lane_total, 1) if measured else None
        # "saved" only means something when more than one lane ran in parallel
        if row["span_seconds"] is not None and row["lane_sum_seconds"] is not None and len(measured) > 1:
            row["saved_seconds"] = round(row["lane_sum_seconds"] - row["span_seconds"], 1)
        # cost: one entry per run id (duplicates of the same run are counted once), unmeasured kept apart
        cost_records, seen_runs = [], set()
        for r in records:
            if r.get("kind") != "cost" or str(r.get("phase")) != phase:
                continue
            run_id = r.get("run")
            if run_id and run_id in seen_runs:
                continue
            if run_id:
                seen_runs.add(run_id)
            cost_records.append(r)
        measured_tokens = [int(r["tokens"]) for r in cost_records if r.get("tokens") is not None]
        row["tokens"] = sum(measured_tokens) if measured_tokens else None
        row["tokens_unmeasured_runs"] = sum(1 for r in cost_records if r.get("tokens") is None)
        measured_usd = [float(r["usd"]) for r in cost_records if r.get("usd") is not None]
        row["usd"] = round(sum(measured_usd), 4) if measured_usd else None
        row["usd_unmeasured_runs"] = sum(1 for r in cost_records if r.get("usd") is None)
        row["cost_runs"] = len(cost_records)
        report["phases"].append(row)
    report["costs"] = [r for r in records if r.get("kind") == "cost"]
    report["pairs"], report["message_anomalies"] = message_evidence(records)
    report["corrections"] = [r for r in records if r.get("kind") == "correction"]
    return report


def render_markdown(report: dict, skipped: int) -> str:
    lines = ["## Sent and received (derived from events/)", "",
             "| time (UTC) | session | kind | msg | phase | lane | peer | HEAD | note | msg_id | reply_to |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["messages"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["ts"], r.get("session", ""), r.get("kind", ""), r.get("msg", ""), fmt(r.get("phase")) if r.get("phase") is not None else "",
            r.get("lane", "") or "", r.get("peer", "") or "", r.get("head", "") or "", r.get("note", "") or "",
            r.get("msg_id", "") or "", r.get("reply_to", "") or ""))
    lines += ["", "## Phase timings", "",
              "| phase | lane | DESIGN sent | DONE received | lane s | phase span s | lane sum s | saved s | TOKENS | USD |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for row in report["phases"]:
        for idx, lane in enumerate(row["lanes"]):
            tokens_cell = fmt(row["tokens"]) + (" (+%d unmeasured)" % row["tokens_unmeasured_runs"] if row["tokens_unmeasured_runs"] else "")
            usd_cell = fmt(row["usd"]) + (" (+%d unmeasured)" % row["usd_unmeasured_runs"] if row["usd_unmeasured_runs"] else "")
            tail = (fmt(row["span_seconds"]), fmt(row["lane_sum_seconds"]),
                    fmt(row["saved_seconds"]) if len(row["lanes"]) > 1 else "n/a (1 lane)",
                    tokens_cell, usd_cell) if idx == 0 else ("", "", "", "", "")
            lane_cell = "skipped" if lane["skipped"] else ("reversed (clock error)" if lane.get("reversed") else fmt(lane["seconds"]))
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                row["phase"], lane["lane"] if lane["lane"] is not None else "-",
                fmt(lane["design_sent"]), fmt(lane["done_received"]), lane_cell, *tail))
    if report.get("pairs"):
        lines += ["", "## Message pairing (msg_id)", "", "| msg_id | msg | phase | sent | received | s | problem |", "|---|---|---|---|---|---|---|"]
        for pair in report["pairs"]:
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                pair["msg_id"], pair.get("msg", ""), fmt(pair.get("phase")) if pair.get("phase") is not None else "",
                fmt(pair["sent_at"]), fmt(pair["received_at"]), fmt(pair["seconds"]), pair["problem"] or ""))
    if report.get("message_anomalies"):
        lines += ["", "## Message anomalies (log observations, not proof of transport loss)", "",
                  "| msg_id | anomaly | sends | receives | evidence (session/kind/msg/phase/source) |",
                  "|---|---|---|---|---|"]
        for anomaly in report["message_anomalies"]:
            evidence = "; ".join("%s/%s/%s/%s/%s:%s" % (
                r.get("session", ""), r.get("kind", ""), r.get("msg", ""), r.get("phase", ""),
                r.get("source_file", ""), r.get("source_line", "")) for r in anomaly["records"])
            lines.append("| %s | %s | %s | %s | %s |" % (
                anomaly["msg_id"] or "(missing)", anomaly["type"], anomaly["send_count"],
                anomaly["recv_count"], evidence))
    if report.get("corrections"):
        lines += ["", "%d correction(s) applied (original lines untouched; timings use the corrected at)" % len(report["corrections"])]
    if skipped:
        lines += ["", "skipped: %d (lines that are not JSON or have no ts)" % skipped]
    return "\n".join(lines) + "\n"


def report_cmd(args: argparse.Namespace) -> int:
    records, skipped = read_events(args.mission)
    if not records:
        print("no events under %s" % events_dir(args.mission), file=sys.stderr)
        return 1
    report = build_report(records)
    if args.as_json:
        print(json.dumps({"report": report, "skipped": skipped}, ensure_ascii=False, indent=2))
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(render_markdown(report, skipped), end="")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp949; keep UTF-8 output alive
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="peer-event.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    log = commands.add_parser("log", help="append one event to <mission>/events/<session>.jsonl")
    log.add_argument("--mission", required=True)
    log.add_argument("--session", required=True)
    log.add_argument("--kind", required=True, choices=KINDS)
    log.add_argument("--phase")
    log.add_argument("--lane")
    log.add_argument("--peer")
    log.add_argument("--msg")
    log.add_argument("--head")
    log.add_argument("--run")
    log.add_argument("--tokens", type=int)
    log.add_argument("--usd", type=float)
    log.add_argument("--note")
    log.add_argument("--ts", help="override the timestamp (ISO 8601 UTC); default now")
    log.add_argument("--transport", choices=("claude", "codex"),
                     help="separately submitted transport (claude: SendMessage; codex: native message or external deliverer)")
    log.add_argument("--body", help="full message text; this logger does not submit messages")
    log.add_argument("--msg-id", dest="msg_id", help="unique outbound ID, copied exactly on receipt; required for Codex send/recv")
    log.add_argument("--reply-to", dest="reply_to", help="inbound message ID this new message replies to (not USER-ANSWER provenance)")
    log.add_argument("--files", help="comma-separated paths this turn changed (the responsible orchestrator commits only these)")
    log.add_argument("--ref", help="USER-ANSWER only: the user's words or message reference (provenance)")
    log.add_argument("--at", help="when it happened (ISO 8601 UTC) if different from now; required for corrections")
    log.add_argument("--trust-dir", dest="trust_dir", default=None,
                     help="USER-ANSWER only: trust directory root outside the workspace (a deliverer reads only there)")
    log.set_defaults(func=log_event)
    rep = commands.add_parser("report", help="derive tables from <mission>/events/*.jsonl")
    rep.add_argument("--mission", required=True)
    rep.add_argument("--json", action="store_true", dest="as_json")
    rep.set_defaults(func=report_cmd)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
