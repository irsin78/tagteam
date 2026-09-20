#!/usr/bin/env python3
"""Check a web-lane response against the text the launcher actually fetched.

The predecessor grepped the response for the strings `EVIDENCE:` and
`SOURCES:`. That proved only that the worker emitted two words, so a run
steered by an injected page could invent support and still be reported DONE.

Because the launcher now does the fetching, it holds the source text and can
look every quotation up in it. That turns the receipt from a formatting check
into a provenance check.

Usage: web_receipt.py <response-file> <fetched-text-dir>
Prints one line for the report and exits non-zero when the receipt fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MIN_QUOTE_CHARS = 8
MAX_QUOTE_CHARS = 400
MIN_BARE_LINE_CHARS = 12
QUOTED = re.compile(r"[\"“”']([^\"“”']{%d,%d})[\"“”']"
                    % (MIN_QUOTE_CHARS, MAX_QUOTE_CHARS))
INCOMPLETE = re.compile(r"(?im)^\s*[*_`>#\-]*\s*FETCH_INCOMPLETE\b")
EVIDENCE = re.compile(r"(?im)^\s*[*_`>#\-]*\s*EVIDENCE\s*:")
SOURCES = re.compile(r"(?im)^\s*[*_`>#\-]*\s*SOURCES\s*:")


def normalize(text: str) -> str:
    """Compare on words, not layout.

    Extraction collapses markup to spaces, so a faithful quotation can differ
    from the page in whitespace and in the curly/straight quote it carries.
    Neither difference should fail an honest run.
    """
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def quotations(response: str) -> list[str]:
    block = EVIDENCE.split(response, maxsplit=1)[1]
    block = SOURCES.split(block, maxsplit=1)[0]
    found = [q.strip() for q in QUOTED.findall(block)]
    if found:
        return found
    # Some runs list evidence as bare bullets rather than quoted strings.
    return [line.strip(" -*\t•") for line in block.splitlines()
            if len(line.strip(" -*\t•")) >= MIN_BARE_LINE_CHARS]


def check(response_path: Path, text_dir: Path) -> tuple[bool, str]:
    if not response_path.is_file() or not response_path.read_text(
            encoding="utf-8", errors="replace").strip():
        return False, "incomplete (empty response)"
    response = response_path.read_text(encoding="utf-8", errors="replace")

    if INCOMPLETE.search(response):
        return False, "incomplete (worker reported FETCH_INCOMPLETE)"
    if not EVIDENCE.search(response):
        return False, "incomplete (no EVIDENCE block)"
    if not SOURCES.search(response):
        return False, "incomplete (no SOURCES block)"

    pages = sorted(text_dir.glob("page-*.txt")) if text_dir.is_dir() else []
    if not pages:
        return False, "incomplete (no fetched text to check quotations against)"
    corpus = normalize(" ".join(p.read_text(encoding="utf-8", errors="replace")
                                for p in pages))

    quotes = quotations(response)
    # Drop anything too short to carry meaning once normalized. Without this a
    # whitespace-only quotation normalizes to "" and `"" in corpus` is always
    # true, so an empty EVIDENCE entry passed as support.
    quotes = [q for q in quotes if len(normalize(q)) >= MIN_QUOTE_CHARS]
    if not quotes:
        return False, "incomplete (EVIDENCE block carries no usable quotations)"
    missing = [q for q in quotes if normalize(q) not in corpus]
    if missing:
        return False, (f"incomplete ({len(missing)}/{len(quotes)} quotations are "
                       f"not in the fetched text; first: {missing[0][:60]!r})")
    return True, (f"complete ({len(quotes)}/{len(quotes)} quotations found in the "
                  f"fetched text; provenance of quotes only, not of the summary)")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: web_receipt.py <response-file> <fetched-text-dir>\n")
        return 2
    ok, note = check(Path(argv[0]), Path(argv[1]))
    print(note)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
