"""A CSS tag selector that names a role the snapshot printed, in a real browser (issue #628).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Snapshots print ARIA roles (`- article "…"`), and `article` in CSS matches <article> only. A query
or an action whose tag matches nothing, while elements carry it as their role, names the role selector.
"""
from http.server import BaseHTTPRequestHandler
import unittest

import real_browser_harness as harness

PAGES = {
    "/cards": """<!doctype html><title>Flights</title><main>
<div role="article" class="card">Flight from JFK to YYZ, $82</div>
<div role="article" class="card">Flight from EWR to YYZ, $95</div></main>""",
    "/tags": "<!doctype html><title>Posts</title><main><article>Post one</article></main>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path)
        if page is None:
            self.send_error(404)
            return
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserRoleSelectorTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_query_and_click_on_a_role_tag_name_the_role_selector(self):
        tab = self.open_tab(f"{self.site.origin}/cards")
        self.assertIn('article "Flight from JFK to YYZ, $82"', self.call("/api/snapshot", {"tabId": tab})["snapshot"])
        for selector in ("article", "main article"):
            with self.subTest(selector=selector):
                result = self.call("/api/query", {"tabId": tab, "selector": selector, "fields": ["text"]})
                self.assertEqual(result["items"], [])
                self.assertIn('2 elements have role="article"', result["hint"])
                self.assertTrue(result["hint"].endswith("use [role=article]."), "query takes CSS only")
        found = self.call("/api/query", {"tabId": tab, "selector": "[role=article]", "fields": ["text"]})
        self.assertEqual(len(found["items"]), 2)
        self.assertNotIn("hint", found)

        for selector in ("article", "article >> nth=0"):
            with self.subTest(selector=selector):
                clicked = self.call("/api/click", {"tabId": tab, "selector": selector})
                self.assert_error(clicked, "not_found", True)
                self.assertIn("[role=article] in CSS, or role=article.", clicked["hint"])
        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "role=article >> nth=0"}))

    def test_a_tag_selector_with_no_such_role_keeps_its_hint(self):
        tab = self.open_tab(f"{self.site.origin}/tags")
        self.assertNotIn("hint", self.call("/api/query", {"tabId": tab, "selector": "section"}))
        clicked = self.call("/api/click", {"tabId": tab, "selector": "section"})
        self.assert_error(clicked, "not_found", True)
        self.assertNotIn("role=", clicked["hint"])
        self.assertEqual(len(self.call("/api/query", {"tabId": tab, "selector": "article"})["items"]), 1)


if __name__ == "__main__":
    unittest.main()
