"""Issue #86 in a real browser: reads of an empty-body HTTP error page.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. For
an error response with an empty body, Firefox shows its own error document, which no
content script can reach. browser_page_text must then report an empty page with the
HTTP status. Every other read must fail at once with a non-retryable error_page that
names the status, so an agent does not keep repeating an unreadable read.

Firefox's JSON and PDF viewers are such documents too, with HTTP 200 (#95): right after a
load, reload or wake a read may report a retryable navigating, but it must settle on the
same final error_page, and waking such a tab must not wait for its timeout.
"""
from http.server import BaseHTTPRequestHandler
import time
import unittest

import real_browser_harness as harness
from test_real_browser import CONTROL_SCRIPT, ExtensionControl

# Every page-scoped read tool, as (MCP tool, bridge endpoint, body without tabId).
READS = [
    ("browser_page_info", "/api/page-info", {}),
    ("browser_snapshot", "/api/snapshot", {}),
    ("browser_snapshot text", "/api/snapshot", {"text": True}),
    ("browser_dom", "/api/dom", {}),
    ("browser_forms", "/api/forms", {}),
    ("browser_find", "/api/find", {"query": "anything"}),
    ("browser_markdown", "/api/markdown", {}),
    ("browser_query", "/api/query", {"selector": "body"}),
    ("browser_html", "/api/html", {}),
    ("browser_links", "/api/links", {"internalOnly": False}),
    ("browser_images", "/api/images", {}),
    ("browser_meta", "/api/meta", {}),
    ("browser_structured_data", "/api/structured-data", {}),
    ("browser_bounds", "/api/bounds", {"selector": "body"}),
    ("browser_computed_style", "/api/computed-style", {"selector": "body"}),
    ("browser_readability", "/api/readability", {}),
    ("browser_iframes", "/api/iframes", {}),
    ("browser_explain_selector", "/api/explain-selector", {"selector": "body"}),
    ("browser_full_page_metrics", "/api/full-page-metrics", {}),
    ("browser_console_logs", "/api/console-logs", {}),
]


PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
# Documents Firefox renders in its own viewers, which no content script can reach.
VIEWER_DOCUMENTS = {"/doc.json": ("application/json", b'{"a": 1}'), "/doc.pdf": ("application/pdf", PDF)}
# The extension's RELOAD_SETTLE_MS plus its status wait, with a margin.
SETTLE_SECONDS = 8


class _Pages(BaseHTTPRequestHandler):
    """/<status> answers with that status and an empty body; VIEWER_DOCUMENTS with HTTP 200;
    anything else (a viewer's favicon) with an empty 404."""

    def do_GET(self):
        content_type, body = VIEWER_DOCUMENTS.get(self.path, ("text/html", b""))
        status = self.path.strip("/")
        self.send_response(200 if self.path in VIEWER_DOCUMENTS else int(status) if status.isdigit() else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserErrorPageTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        return {"test-control.js": CONTROL_SCRIPT % cls.control.port}

    def assert_reads_of_empty_error_page(self, status):
        url = f"{self.site.origin}/{status}"
        tab_id = self.open_tab(url)

        text = self.call("/api/page-text", {"tabId": tab_id})
        self.assertEqual({k: text.get(k) for k in ("text", "empty", "httpStatus", "tabId", "url")},
                         {"text": "", "empty": True, "httpStatus": status, "tabId": tab_id, "url": url}, text)
        self.assertNotIn("error", text)
        for tool, path, body in READS:
            with self.subTest(tool=tool):
                result = self.call(path, {**body, "tabId": tab_id})
                self.assert_error(result, "error_page", False)
                self.assertEqual(result["state"]["httpStatus"], status, result)
                self.assertIn(f"HTTP {status}", result["error"])

    def test_empty_404_page(self):
        self.assert_reads_of_empty_error_page(404)

    def test_empty_500_page(self):
        self.assert_reads_of_empty_error_page(500)

    def assert_viewer_error_page(self, tab_id, url):
        """Reads settle on a final error_page with HTTP 200; before that only navigating."""
        deadline = time.monotonic() + SETTLE_SECONDS
        while True:
            result = self.call("/api/page-info", {"tabId": tab_id})
            if result.get("code") != "navigating":
                break
            self.assertIs(result["retryable"], True, result)
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(0.1)
        self.assert_error(result, "error_page", False)
        self.assertEqual((result["state"]["url"], result["state"]["httpStatus"]), (url, 200), result)
        # A final error_page stays final.
        self.assert_error(self.call("/api/page-text", {"tabId": tab_id}), "error_page", False)

    def test_json_and_pdf_viewers_are_final_error_pages_after_load_reload_and_wake(self):
        for path in VIEWER_DOCUMENTS:
            with self.subTest(document=path):
                url = self.site.origin + path
                tab_id = self.open_tab(url)
                self.assert_viewer_error_page(tab_id, url)

                self.assert_ok(self.call("/api/reload-tab", {"tabId": tab_id}))
                self.assert_viewer_error_page(tab_id, url)

                self.open_tab(f"{self.site.origin}/404")  # discard needs an inactive tab
                self.assertEqual(self.control.call("discard", tabId=tab_id), {"discarded": True})
                started = time.monotonic()
                woken = self.call("/api/wake-tab", {"tabId": tab_id})
                self.assertLess(time.monotonic() - started, 5, "an unscriptable page ends the wake early")
                self.assertEqual({k: woken.get(k) for k in ("ok", "woken", "ready", "unscriptable")},
                                 {"ok": True, "woken": True, "ready": False, "unscriptable": True}, woken)
                self.assertNotIn("timedOut", woken)
                self.assertEqual((woken["state"]["url"], woken["state"]["httpStatus"]), (url, 200), woken)
                self.assert_viewer_error_page(tab_id, url)


if __name__ == "__main__":
    unittest.main()
