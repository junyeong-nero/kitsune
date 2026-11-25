"""#633: tab.query names a field no row has, and gives truncation advice only for a limit it chose.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import os
from unittest.mock import patch
import warnings

import kitsune
from kitsune import bridge_client, bridge_control
import real_browser_harness as harness

PAGE = ('<!doctype html><title>Query warnings</title>'
        '<button class="p-2" title="Close" aria-label="Close dialog"></button>'
        + "".join(f'<a href="/item/{i}">Item {i}</a>' for i in range(60)))


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserQueryWarningsTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        # Point the library at this class's bridge, as KITSUNE_HTTP_PORT and KITSUNE_TOKEN would.
        url = f"http://127.0.0.1:{self.bridge.http_port}"
        token = self.bridge.auth()["Authorization"].removeprefix("Bearer ")
        for patcher in (patch.object(bridge_client, "BRIDGE", url), patch.object(bridge_control, "BRIDGE", url),
                        patch.object(bridge_client, "HTTP_PORT", self.bridge.http_port),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": token})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = kitsune.connect()
        self.tab = self.browser.new_tab(self.site.origin + "/")
        self.addCleanup(self.tab.close)

    def query(self, *args, **kwargs):
        """tab.query's rows and the messages of the warnings it gave."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows = self.tab.query(*args, **kwargs)
        return rows, [str(w.message) for w in caught]

    def test_a_field_no_row_has_is_named_and_outer_html_points_to_html(self):
        rows, said = self.query("button.p-2", fields=["text", "title", "aria-label", "outerHTML"], limit=1)
        self.assertEqual(rows, [{"tag": "button", "text": "", "title": "Close", "aria-label": "Close dialog"}])
        self.assertEqual(said, ["Query fields outerHTML: no such field or attribute on any row; use html for "
                                "the inner markup or tab.html(target) for one element's outerHTML"])
        rows, said = self.query("a", fields=["herf", "href"], limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(said, ["Query fields herf: no such field or attribute on any row"])
        # A built-in or an attribute some row has is not unknown, and the MCP result carries the names.
        rows, said = self.query("a", fields=["html", "href"], limit=1)
        self.assertEqual(rows, [{"tag": "a", "html": "Item 0", "href": self.site.origin + "/item/0"}])
        self.assertEqual(said, [])
        reply = self.call("/api/query", {"tabId": self.tab.id, "selector": "button, a", "limit": 3,
                                         "fields": ["title", "innerHTML", "bounds"]})
        self.assertEqual(reply["unknownFields"], ["innerHTML"])
        self.assertNotIn("unknownFields", self.call("/api/query", {"tabId": self.tab.id, "selector": "a"}))

    def test_truncation_advice_only_at_the_default_limit_or_the_ceiling(self):
        rows, said = self.query("a", limit=1)
        self.assertEqual([row["text"] for row in rows], ["Item 0"])
        self.assertEqual(said, [], "an explicit small limit took the rows it asked for")
        rows, said = self.query("a", fields=["text"])
        self.assertEqual(len(rows), 50)
        self.assertEqual(said, ["Query results were truncated; narrow the selector or increase limit"])
        reply = self.call("/api/query", {"tabId": self.tab.id, "selector": "a", "limit": 1})
        self.assertEqual((reply["count"], reply["total"], reply["truncated"]), (1, 60, True))
