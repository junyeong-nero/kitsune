"""Issue #86 in a real browser: reads of an empty-body HTTP error page.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. For
an error response with an empty body, Firefox shows its own error document, which no
content script can reach. browser_page_text must then report an empty page with the
HTTP status. Every other read must fail at once with a non-retryable error_page that
names the status, so an agent does not keep repeating an unreadable read.
"""
from http.server import BaseHTTPRequestHandler
import unittest

import real_browser_harness as harness

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


class _EmptyStatus(BaseHTTPRequestHandler):
    """/<status> answers with that status and an empty body."""

    def do_GET(self):
        self.send_response(int(self.path.strip("/")))
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, *args):
        pass


class RealBrowserErrorPageTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_EmptyStatus)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

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


if __name__ == "__main__":
    unittest.main()
