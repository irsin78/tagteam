#!/usr/bin/env python3
"""Tests for the web lane's provenance receipt."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("web_receipt.py")
_spec = importlib.util.spec_from_file_location("web_receipt", SCRIPT)
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

PAGE = (
    "The venv module supports creating lightweight virtual environments, each "
    "with their own independent set of Python packages.\n"
    "python -m venv /path/to/new/virtual/environment\n"
    "--system-site-packages Give the virtual environment access to the system "
    "site-packages directory.\n"
)


class ReceiptCase(unittest.TestCase):
    def check(self, response, page=PAGE, write_page=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            texts = root / "text"
            texts.mkdir()
            if write_page:
                (texts / "page-1.txt").write_text(page, encoding="utf-8")
            response_file = root / "response.txt"
            response_file.write_text(response, encoding="utf-8")
            return wr.check(response_file, texts)


class AcceptsHonestReceipts(ReceiptCase):
    def test_quotation_present_in_the_fetched_text(self):
        ok, note = self.check(
            'Summary.\n\nEVIDENCE:\n- "python -m venv /path/to/new/virtual/environment"\n'
            '\nSOURCES:\n- https://example.com/ : used\n')
        self.assertTrue(ok, note)
        self.assertIn("1/1", note)

    def test_whitespace_and_curly_quotes_do_not_matter(self):
        ok, note = self.check(
            'Summary.\n\nEVIDENCE:\n'
            '- "lightweight virtual   environments,\n  each with their own independent set"\n'
            '\nSOURCES:\n- x\n')
        self.assertTrue(ok, note)

    def test_several_quotations_all_present(self):
        ok, note = self.check(
            'Summary.\n\nEVIDENCE:\n'
            '- "python -m venv /path/to/new/virtual/environment"\n'
            '- "Give the virtual environment access to the system site-packages directory."\n'
            '\nSOURCES:\n- x\n')
        self.assertTrue(ok, note)
        self.assertIn("2/2", note)

    def test_markdown_decorated_headings(self):
        ok, _ = self.check(
            'Summary.\n\n**EVIDENCE:**\n- "python -m venv"\n\n**SOURCES:**\n- x\n')
        self.assertTrue(ok)


class RejectsUnsupportedReceipts(ReceiptCase):
    def test_invented_quotation(self):
        # The point of the check: a run steered into fabricating support must
        # not be reported DONE.
        ok, note = self.check(
            'Summary.\n\nEVIDENCE:\n'
            '- "The venv module was deprecated in 3.14 and removed entirely."\n'
            '\nSOURCES:\n- x\n')
        self.assertFalse(ok)
        self.assertIn("not in the fetched text", note)

    def test_one_invented_among_several(self):
        ok, note = self.check(
            'Summary.\n\nEVIDENCE:\n'
            '- "python -m venv /path/to/new/virtual/environment"\n'
            '- "and it also uploads your keys to a remote server"\n'
            '\nSOURCES:\n- x\n')
        self.assertFalse(ok)
        self.assertIn("1/2", note)

    def test_missing_blocks(self):
        for response, needle in (
                ("Just a summary.\n", "no EVIDENCE"),
                ('S.\n\nEVIDENCE:\n- "python -m venv"\n', "no SOURCES"),
                ("", "empty response")):
            ok, note = self.check(response)
            self.assertFalse(ok)
            self.assertIn(needle, note)

    def test_fetch_incomplete_is_a_failure(self):
        ok, note = self.check(
            'FETCH_INCOMPLETE: https://x/ - truncated\n\nEVIDENCE:\n'
            '- "python -m venv"\n\nSOURCES:\n- x\n')
        self.assertFalse(ok)
        self.assertIn("FETCH_INCOMPLETE", note)

    def test_empty_evidence_block(self):
        ok, note = self.check('S.\n\nEVIDENCE:\n\nSOURCES:\n- x\n')
        self.assertFalse(ok)
        self.assertIn("no usable quotations", note)

    def test_whitespace_only_quotation_is_not_support(self):
        # It normalizes to "", and "" is a substring of every corpus, so
        # without a length floor this passed as complete support.
        for evidence in ('- "        "', '- ""', '- "\t \t"', '- "  ,  "'):
            ok, note = self.check(f'Confident summary.\n\nEVIDENCE:\n{evidence}\n'
                                  f'\nSOURCES:\n- x\n')
            self.assertFalse(ok, f"{evidence} was accepted: {note}")
            self.assertIn("no usable quotations", note)

    def test_trivially_short_quotation_is_not_support(self):
        ok, note = self.check('S.\n\nEVIDENCE:\n- "venv"\n\nSOURCES:\n- x\n')
        self.assertFalse(ok)

    def test_no_fetched_text_to_check_against(self):
        ok, note = self.check(
            'S.\n\nEVIDENCE:\n- "python -m venv"\n\nSOURCES:\n- x\n',
            write_page=False)
        self.assertFalse(ok)
        self.assertIn("no fetched text", note)


if __name__ == "__main__":
    unittest.main(verbosity=1)
