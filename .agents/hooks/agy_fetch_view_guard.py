#!/usr/bin/env python3
"""Restrict agy web-mode file reads to its current URL-cache artifacts."""

from __future__ import annotations

import json
import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def emit(decision: str, reason: str = "") -> None:
    payload = {"decision": decision}
    if reason:
        payload["reason"] = reason
    print(json.dumps(payload))


def main() -> int:
    # This hook is installed globally because agy 1.2.7 does not activate
    # workspace hooks in headless runs. Leave unrelated/direct agy sessions
    # alone; the launcher sets the marker only for its constrained web lane.
    if os.environ.get("HARNESS_AGY_WEB") != "1":
        emit("allow")
        return 0

    try:
        event = json.load(sys.stdin)
        call = event.get("toolCall") or {}
        tool_name = call.get("name")
        args = call.get("args") or {}
        if tool_name == "read_url_content":
            url = args.get("Url", "").strip().strip('"')
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            allowed = {item for item in os.environ.get("HARNESS_AGY_URL_HOSTS", "").split(",") if item}
            if parsed.scheme not in ("http", "https") or not host or "," in host or host not in allowed:
                raise ValueError("URL host was not named by the caller")
            local = host == "localhost" or host.endswith(".localhost") or host.endswith(".local") \
                or host == "metadata.google.internal"
            try:
                address = ipaddress.ip_address(host)
                local = local or address.is_private or address.is_loopback or address.is_link_local \
                    or address.is_multicast or address.is_reserved or address.is_unspecified
            except ValueError:
                pass
            if local:
                raise ValueError("local and private URL hosts are not allowed")
            emit("allow")
            return 0
        if tool_name != "view_file":
            emit("allow")
            return 0
        target = Path(args.get("AbsolutePath", "").strip().strip('"')).resolve(strict=True)
        artifact = Path(event["artifactDirectoryPath"]).resolve(strict=True)
        cache_root = (artifact / ".system_generated" / "steps").resolve(strict=True)
        target.relative_to(cache_root)
        if target.name != "content.md" or not target.is_file():
            raise ValueError("not a generated URL content file")
    except Exception as error:
        emit("deny", f"agy web restriction: {error}")
        return 0

    emit("allow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
