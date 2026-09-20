#!/usr/bin/env python3
"""Fetch caller-named URLs for the agy web lane.

The launcher owns fetching so the summarizing worker needs no tools at all.
Every decision here is deterministic: page text can never steer which host is
contacted, because the model is not in this loop.

Two modes:
  --check <url>...              validate only; exit 0 when every URL is allowed
  --fetch --out <dir> <url>...  validate, fetch, extract text, write files

A manifest is printed as JSON on stdout in both modes.
"""

from __future__ import annotations

import argparse
import gzip
import html
import ipaddress
import json
import re
import socket
import sys
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (HTTPDefaultErrorHandler, HTTPErrorProcessor,
                            HTTPHandler, HTTPSHandler, OpenerDirector, Request)

MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 30
USER_AGENT = "claude-harness-web-lane/1 (+deterministic launcher fetch)"

# A host we accept: dot-separated LDH labels whose last label is alphabetic.
# Requiring an alphabetic TLD rejects every numeric spelling of an address
# (2130706433, 127.1, 0x7f.0.0.1) without relying on ip_address() succeeding.
HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$"
)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
BLOCKED_NAMES = {"localhost", "metadata.google.internal"}


class Rejected(Exception):
    """The URL is not allowed by policy. Never retried through another lane."""


class Unreachable(Exception):
    """The URL is allowed but could not be retrieved. An availability failure."""


def _opener() -> OpenerDirector:
    """An opener that follows nothing and speaks only HTTP(S).

    Two deliberate omissions. No redirect handler: urlopen's default opener
    follows 3xx itself, which made this module's manual "validate every hop"
    loop dead code — a 302 from a validated page to 127.0.0.1 was fetched and
    returned under the original URL's name. Without one, a 3xx reaches
    HTTPDefaultErrorHandler and raises, so fetch() takes each hop explicitly.
    And no FTP or file handler: the default opener installs both, and a
    Location header could otherwise reach them.
    """
    director = OpenerDirector()
    for handler in (HTTPHandler(), HTTPSHandler(),
                    HTTPDefaultErrorHandler(), HTTPErrorProcessor()):
        director.add_handler(handler)
    return director


OPENER = _opener()


def validate(raw: str) -> str:
    """Return the normalized URL, or raise Rejected.

    Deliberately narrower than any URL parser. agy is a Node process and Node
    follows WHATWG rules, which disagree with Python's urlsplit on backslashes
    and on some userinfo spellings. Rather than emulate another language's
    parser, refuse every shape the two could read differently.
    """
    if not isinstance(raw, str) or not raw:
        raise Rejected("empty URL")
    if len(raw) > 2048:
        raise Rejected("URL longer than 2048 characters")
    try:
        raw.encode("ascii")
    except UnicodeEncodeError:
        raise Rejected("URL contains non-ASCII characters") from None
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in raw):
        raise Rejected("URL contains whitespace or control characters")
    if "\\" in raw:
        raise Rejected("URL contains a backslash")

    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise Rejected(f"scheme {parts.scheme or '(none)'} is not http/https")
    authority = parts.netloc
    if "@" in authority:
        raise Rejected("URL carries userinfo (@ in the authority)")
    if "%" in authority:
        raise Rejected("URL percent-encodes its authority")
    try:
        port = parts.port
    except ValueError:
        raise Rejected("URL has an unparseable port") from None
    if port is not None and port not in (80, 443):
        raise Rejected(f"port {port} is not 80/443")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise Rejected("URL has no host")
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
        raise Rejected(f"host {host} is a local name")
    if not HOST_RE.match(host):
        raise Rejected(
            f"host {host} is not a dotted name with an alphabetic top-level label"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                       parts.query, ""))


def resolve_is_public(host: str) -> None:
    """Raise Rejected when the name resolves anywhere non-public.

    This closes the gap between a public-looking name and a private address.
    It cannot close a rebind between this lookup and the connection; that
    residual is recorded in docs/runtime-boundary.md.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as error:
        raise Rejected(f"host {host} does not resolve ({error.strerror})") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved
                or address.is_unspecified):
            raise Rejected(f"host {host} resolves to non-public {address}")


def decode_body(raw: bytes, encoding: str, max_bytes: int = MAX_BYTES) -> tuple[bytes, bool]:
    """Decompress per Content-Encoding, stopping at max_bytes.

    Two things are deliberate. The decompressed output is never trimmed to
    Content-Length: that header describes the COMPRESSED body, and trimming
    decompressed bytes to it is exactly the agy 1.2.7 defect that silently
    loses ~85% of a gzip page. And decompression is bounded, because the wire
    size is not: a few hundred KB of gzip expands to hundreds of MB, so a cap
    applied only to the compressed read is no cap at all.

    Returns (body, truncated).
    """
    encoding = (encoding or "").lower().strip()
    if encoding in ("gzip", "x-gzip"):
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        stream = zlib.decompressobj()
    else:
        return raw[:max_bytes], len(raw) > max_bytes
    try:
        body = stream.decompress(raw, max_bytes + 1)
    except zlib.error:
        if encoding != "deflate":
            raise Unreachable("response body could not be decompressed") from None
        try:
            body = zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw, max_bytes + 1)
        except zlib.error:
            raise Unreachable("response body could not be decompressed") from None
    return body[:max_bytes], len(body) > max_bytes


_DROP = re.compile(r"(?is)<(script|style|head|noscript|template|svg)\b.*?</\1\s*>")
_BREAKS = re.compile(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6]|/section|/article)\b[^>]*>")
_TAG = re.compile(r"(?s)<[^>]+>")


def extract_text(body: bytes, content_type: str) -> str:
    text = body.decode("utf-8", errors="replace")
    if "html" not in (content_type or "").lower() and "<" not in text[:4096]:
        return _tidy(text)
    text = _DROP.sub(" ", text)
    text = _BREAKS.sub("\n", text)
    text = _TAG.sub(" ", text)
    return _tidy(html.unescape(text))


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch(url: str, max_bytes: int = MAX_BYTES, timeout: int = TIMEOUT_S) -> dict:
    """Retrieve the URL, validating every redirect hop before taking it."""
    current = validate(url)
    hops = [current]
    for _ in range(MAX_REDIRECTS + 1):
        resolve_is_public(urlsplit(current).hostname or "")
        request = Request(current, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with OPENER.open(request, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                # Belt and braces: if any handler ever follows a redirect for
                # us, the body would come from a host we never validated.
                landed = getattr(response, "url", current)
                if landed and validate(landed) != current:
                    raise Rejected(
                        f"response came from {landed}, not the validated {current}")
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(max_bytes + 1)
                encoding = response.headers.get("Content-Encoding", "")
        except HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                location = error.headers.get("Location")
                error.close()
                if not location:
                    raise Unreachable(f"HTTP {error.code} without a Location") from None
                current = validate(_absolute(current, location))
                hops.append(current)
                continue
            error.close()
            raise Unreachable(f"HTTP {error.code} {error.reason}") from None
        except URLError as error:
            raise Unreachable(f"could not fetch: {error.reason}") from None

        body, truncated = decode_body(raw, encoding, max_bytes)
        text = extract_text(body, content_type)
        return {
            "url": url,
            "final_url": current,
            "redirects": hops[1:],
            "status": status,
            "content_type": content_type.split(";")[0].strip(),
            "bytes": len(body),
            "chars": len(text),
            "truncated": truncated,
            "text": text,
        }
    raise Rejected(f"more than {MAX_REDIRECTS} redirects")


def _absolute(base: str, location: str) -> str:
    if re.match(r"(?i)^https?://", location):
        return location
    parts = urlsplit(base)
    if location.startswith("//"):
        return f"{parts.scheme}:{location}"
    if location.startswith("/"):
        return urlunsplit((parts.scheme, parts.netloc, location, "", ""))
    base_dir = parts.path.rsplit("/", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, f"{base_dir}/{location}", "", ""))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--timeout", type=int, default=TIMEOUT_S)
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args(argv)
    if args.fetch and not args.out:
        parser.error("--fetch requires --out")

    pages, errors = [], []
    policy_refusal = False
    for url in args.urls:
        try:
            if args.fetch:
                page = fetch(url, args.max_bytes, args.timeout)
            else:
                page = {"url": url, "final_url": validate(url), "status": None}
                resolve_is_public(urlsplit(page["final_url"]).hostname or "")
        except Rejected as error:
            policy_refusal = True
            errors.append({"url": url, "reason": str(error), "kind": "policy"})
            continue
        except Unreachable as error:
            errors.append({"url": url, "reason": str(error), "kind": "unreachable"})
            continue
        if args.fetch:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"page-{len(pages) + 1}.txt"
            path.write_text(page.pop("text"), encoding="utf-8")
            page["path"] = str(path)
        pages.append(page)

    json.dump({"pages": pages, "errors": errors}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    if policy_refusal:
        return 4          # policy: never retried through another lane
    if errors or not pages:
        return 1          # allowed but not retrievable
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
