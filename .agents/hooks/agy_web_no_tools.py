#!/usr/bin/env python3
"""Deny every tool call in the agy web lane.

The web lane's agent is declared with no tools, and the launcher fetches the
page itself, so a tool call in this lane should never happen. If one does, the
named agent was not applied and agy fell back to a default agent that still has
tools. Denying unconditionally makes that fallback harmless.

There is deliberately no parsing here. The predecessor inspected tool names,
URLs and file paths, and each of those inspections was a place to be wrong: it
read hosts with Python's parser while agy sends them through Node's, it let
unrecognized tool names through, and it misjudged numeric address spellings. A
guard with no logic cannot have a logic defect.

Installed globally because agy 1.2.7 does not activate workspace hooks in
headless runs. The marker keeps it inert for every other agy session.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    if os.environ.get("HARNESS_AGY_WEB") == "1":
        payload = {"decision": "deny",
                   "reason": "agy web lane uses no tools; the launcher fetches"}
    else:
        payload = {"decision": "allow"}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
