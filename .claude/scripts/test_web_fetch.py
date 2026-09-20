#!/usr/bin/env python3
"""Offline tests for the web lane's launcher-side fetcher.

No network: these cover the decisions that keep the lane safe and correct.
"""

import gzip
import http.server
import importlib.util
import socketserver
import threading
import unittest
import zlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("web_fetch.py")
_spec = importlib.util.spec_from_file_location("web_fetch", SCRIPT)
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)


class ValidateRejects(unittest.TestCase):
    """Each case is a shape that reached the network under the old hook."""

    def assert_rejected(self, url, needle=None):
        with self.assertRaises(wf.Rejected, msg=f"{url} should be rejected") as ctx:
            wf.validate(url)
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_backslash_authority(self):
        # Python read the host as docs.example.com, Node as evil.com; the
        # disagreement was an outbound channel, so refuse the shape outright.
        self.assert_rejected("https://evil.com\\@docs.example.com/leak", "backslash")

    def test_userinfo(self):
        self.assert_rejected("https://docs.example.com@evil.com/x", "userinfo")

    def test_percent_encoded_authority(self):
        self.assert_rejected("https://docs.example.com%2f@evil.com/x")

    def test_numeric_host_spellings(self):
        for host in ("2130706433", "127.1", "0x7f.0.0.1", "127.0.0.1",
                     "192.168.1.5", "10.0.0.1", "169.254.169.254", "100.64.0.1"):
            self.assert_rejected(f"http://{host}/admin")

    def test_ipv6_literal(self):
        self.assert_rejected("http://[::1]/x")

    def test_local_names(self):
        for host in ("localhost", "app.localhost", "printer.local",
                     "metadata.google.internal", "svc.internal", "box.home.arpa"):
            self.assert_rejected(f"http://{host}/x")

    def test_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x",
                    "gopher://example.com/", "data:text/plain,hi"):
            self.assert_rejected(url)

    def test_odd_characters(self):
        self.assert_rejected("https://exa mple.com/x", "whitespace")
        self.assert_rejected("https://exam\tple.com/x", "whitespace")
        self.assert_rejected("https://例.jp/x", "non-ASCII")

    def test_non_standard_port(self):
        self.assert_rejected("https://docs.example.com:8443/x", "port")

    def test_empty_and_overlong(self):
        self.assert_rejected("", "empty")
        self.assert_rejected("https://" + "a" * 3000 + ".com/", "2048")


class ValidateAccepts(unittest.TestCase):
    def test_ordinary_urls(self):
        for url in ("https://docs.python.org/3/library/json.html",
                    "http://example.com",
                    "https://www.rfc-editor.org/rfc/rfc6265.html?x=1",
                    "https://xn--80ak6aa92e.com/page"):
            self.assertTrue(wf.validate(url).startswith(("http://", "https://")))

    def test_fragment_is_dropped_and_path_defaulted(self):
        self.assertEqual(wf.validate("https://example.com#frag"),
                         "https://example.com/")

    def test_explicit_default_ports_allowed(self):
        wf.validate("http://example.com:80/x")
        wf.validate("https://example.com:443/x")


class DecodeBody(unittest.TestCase):
    def test_gzip_and_deflate_roundtrip(self):
        payload = b"<html><body>" + b"x" * 50000 + b"</body></html>"
        self.assertEqual(wf.decode_body(gzip.compress(payload), "gzip"), (payload, False))
        self.assertEqual(wf.decode_body(zlib.compress(payload), "deflate"), (payload, False))
        self.assertEqual(wf.decode_body(payload, ""), (payload, False))

    def test_raw_deflate_without_zlib_header(self):
        payload = b"raw deflate body " * 100
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(payload) + compressor.flush()
        self.assertEqual(wf.decode_body(raw, "deflate"), (payload, False))

    def test_decompressed_output_is_not_trimmed_to_compressed_length(self):
        # The agy 1.2.7 defect this lane exists to avoid: a gzip body trimmed
        # to Content-Length (which describes the COMPRESSED bytes) loses most
        # of the page. Decoding must return the full decompressed body.
        payload = b"A" * 100_000
        compressed = gzip.compress(payload)
        self.assertLess(len(compressed), len(payload) // 10)
        body, truncated = wf.decode_body(compressed, "gzip")
        self.assertEqual(len(body), len(payload))
        self.assertFalse(truncated)

    def test_decompression_is_bounded(self):
        # A compression bomb is small on the wire, so a cap applied to the
        # compressed read is no cap at all. Decompression itself must stop.
        bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))
        self.assertLess(len(bomb), 200_000)
        body, truncated = wf.decode_body(bomb, "gzip", max_bytes=1024)
        self.assertEqual(len(body), 1024)
        self.assertTrue(truncated)

    def test_decompression_never_materializes_more_than_the_cap(self):
        # Slicing the output afterwards is not a bound: the whole expansion
        # already happened in memory. Assert the limit reaches zlib itself.
        limits = []
        real_factory = wf.zlib.decompressobj

        class Recording:
            def __init__(self, inner):
                self._inner = inner

            def decompress(self, data, max_length=None):
                limits.append(max_length)
                if max_length is None:
                    return self._inner.decompress(data)
                return self._inner.decompress(data, max_length)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        wf.zlib.decompressobj = lambda *a, **k: Recording(real_factory(*a, **k))
        try:
            wf.decode_body(gzip.compress(b"\0" * (8 * 1024 * 1024)), "gzip",
                           max_bytes=2048)
        finally:
            wf.zlib.decompressobj = real_factory
        self.assertTrue(limits, "decompressobj was never used")
        for limit in limits:
            self.assertIsNotNone(limit, "decompression ran without a length limit")
            self.assertLessEqual(limit, 2048 + 1)

    def test_identity_body_is_also_capped(self):
        body, truncated = wf.decode_body(b"y" * 5000, "", max_bytes=1000)
        self.assertEqual(len(body), 1000)
        self.assertTrue(truncated)

    def test_undecompressable_body_is_unreachable_not_rejected(self):
        with self.assertRaises(wf.Unreachable):
            wf.decode_body(b"not gzip at all", "gzip")


class ExtractText(unittest.TestCase):
    def test_drops_script_style_and_tags(self):
        body = (b"<html><head><title>t</title></head><body>"
                b"<script>steal()</script><style>a{}</style>"
                b"<h1>Title</h1><p>Hello &amp; welcome</p>"
                b"</body></html>")
        text = wf.extract_text(body, "text/html")
        self.assertIn("Title", text)
        self.assertIn("Hello & welcome", text)
        self.assertNotIn("steal()", text)
        self.assertNotIn("a{}", text)
        self.assertNotIn("<", text)

    def test_block_ends_become_newlines(self):
        text = wf.extract_text(b"<p>one</p><p>two</p>", "text/html")
        self.assertEqual(text.split("\n")[:2], ["one", "two"])

    def test_plain_text_passes_through(self):
        self.assertEqual(wf.extract_text(b"just  text\r\nhere", "text/plain"),
                         "just text\nhere")


class RedirectResolution(unittest.TestCase):
    def test_absolute_forms(self):
        base = "https://example.com/a/b/page.html"
        self.assertEqual(wf._absolute(base, "https://other.com/x"),
                         "https://other.com/x")
        self.assertEqual(wf._absolute(base, "//other.com/x"),
                         "https://other.com/x")
        self.assertEqual(wf._absolute(base, "/root"),
                         "https://example.com/root")
        self.assertEqual(wf._absolute(base, "next.html"),
                         "https://example.com/a/b/next.html")

    def test_redirect_target_shape_is_revalidated(self):
        # A hop is only taken after validate(); a redirect to a rejected shape
        # must raise rather than be followed.
        with self.assertRaises(wf.Rejected):
            wf.validate(wf._absolute("https://example.com/a", "//127.0.0.1/x"))


def _serve(handler_cls):
    server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RedirectsAreActuallyFollowedByUs(unittest.TestCase):
    """Socket-level, because the shape test above passes either way.

    urlopen's default opener follows 3xx internally. With it, fetch()'s manual
    loop never ran and a 302 to a loopback address was retrieved and returned
    labelled with the original URL. These tests fail if that regresses.
    """

    def setUp(self):
        self.hits = []
        hits = self.hits

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                hits.append(inner.path)
                inner.send_response(200)
                inner.send_header("Content-Type", "text/html")
                inner.end_headers()
                inner.wfile.write(b"<html><body>INTERNAL ONLY</body></html>")

            def log_message(inner, *a):
                pass

        self.target = _serve(Target)
        target_port = self.target.server_address[1]

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                inner.send_response(302)
                inner.send_header(
                    "Location", f"http://127.0.0.1:{target_port}/internal")
                inner.end_headers()

            def log_message(inner, *a):
                pass

        self.redirector = _serve(Redirector)
        self.addCleanup(self.target.shutdown)
        self.addCleanup(self.redirector.shutdown)
        self.url = f"http://127.0.0.1:{self.redirector.server_address[1]}/public"

    def test_redirect_to_loopback_is_refused_and_never_requested(self):
        # Only the hostname policy is relaxed for the first hop; the redirect
        # target must still be rejected, and the target server must see nothing.
        real_validate = wf.validate
        wf.validate = lambda u: u if u == self.url else real_validate(u)
        try:
            with self.assertRaises(wf.Rejected):
                wf.fetch(self.url)
        finally:
            wf.validate = real_validate
        self.assertEqual(self.hits, [], "the redirect target was contacted")

    def test_every_hop_reaches_the_public_address_check(self):
        real_validate, real_resolve = wf.validate, wf.resolve_is_public
        seen = []
        wf.validate = lambda u: u
        wf.resolve_is_public = lambda host: seen.append(host)
        try:
            result = wf.fetch(self.url)
        finally:
            wf.validate, wf.resolve_is_public = real_validate, real_resolve
        self.assertEqual(len(seen), 2, f"checked hosts: {seen}")
        self.assertEqual(len(result["redirects"]), 1)
        self.assertIn("/internal", result["final_url"])
        self.assertIn("INTERNAL ONLY", result["text"])

    def test_body_from_an_unvalidated_final_url_is_refused(self):
        # Defense in depth for the opener: if any handler ever resolves a
        # redirect for us, the response's url no longer matches what was
        # validated, and the body must not be accepted.
        class Faked:
            status = 200
            headers = {"Content-Type": "text/html", "Content-Encoding": ""}
            # A shape validate() accepts, so the comparison is what fires.
            url = "https://other.example.com/internal"

            def read(self, n):
                return b"<html>INTERNAL</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real_open, real_resolve = wf.OPENER.open, wf.resolve_is_public
        wf.OPENER.open = lambda *a, **k: Faked()
        wf.resolve_is_public = lambda host: None
        try:
            with self.assertRaises(wf.Rejected) as ctx:
                wf.fetch("https://docs.example.com/page")
        finally:
            wf.OPENER.open, wf.resolve_is_public = real_open, real_resolve
        self.assertIn("not the validated", str(ctx.exception))

    def test_redirect_chain_is_bounded(self):
        class Loop(http.server.BaseHTTPRequestHandler):
            def do_GET(inner):
                inner.send_response(302)
                inner.send_header("Location", inner.path + "x")
                inner.end_headers()

            def log_message(inner, *a):
                pass

        looper = _serve(Loop)
        self.addCleanup(looper.shutdown)
        url = f"http://127.0.0.1:{looper.server_address[1]}/a"
        real_validate, real_resolve = wf.validate, wf.resolve_is_public
        wf.validate = lambda u: u
        wf.resolve_is_public = lambda host: None
        try:
            with self.assertRaises(wf.Rejected) as ctx:
                wf.fetch(url)
        finally:
            wf.validate, wf.resolve_is_public = real_validate, real_resolve
        self.assertIn("redirects", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
