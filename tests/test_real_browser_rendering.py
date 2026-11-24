"""navigate and a waiting new tab say when a loaded page still renders, in a real browser (issue #631).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Since #601 both return once the DOM is parsed, so a client-rendered page may show nothing yet or
a "Loading..." placeholder. The result says so with rendering, and the wait it names finds the content.
"""
from http.server import BaseHTTPRequestHandler
import unittest

import real_browser_harness as harness

PAGES = {
    # Like PipeCRM's detail pages: a placeholder until a client fetch returns.
    "/loading": """<!doctype html><title>Deal</title><nav>PipeCRM Deals Contacts</nav>
<div id="root"><div>Loading...</div></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Radiant Solar</h1><p>OPEN</p>' }, 2500)</script>""",
    # Like a React SPA: an empty root until its bundle renders.
    "/empty": """<!doctype html><title>Flights</title><div id="root"></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Flight search</h1>' }, 2500)</script>""",
    "/busy": """<!doctype html><title>Vault</title><h1>Vault</h1><section aria-busy="true">Items</section>""",
    "/plain": "<!doctype html><title>Plain</title><h1>Plain page</h1>",
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserRenderingTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_navigate_to_a_page_showing_loading_says_so_and_its_wait_finds_the_content(self):
        tab = self.open_tab(f"{self.site.origin}/plain")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/loading"}))
        self.assertEqual((result.get("rendering"), result.get("placeholder")), (True, "Loading..."), result)
        self.assertIn('text_gone="Loading..."', result["rendering_hint"])
        waited = self.call("/api/wait-for-text", {"tabId": tab, "text_gone": "Loading...", "timeout": 10000})
        self.assertIs(waited.get("ok"), True, waited)
        self.assertIn("Radiant Solar", self.call("/api/page-text", {"tabId": tab})["text"])

    def test_a_new_tab_on_an_empty_root_says_so(self):
        result = self.call("/api/new-tab", {"url": f"{self.site.origin}/empty", "wait": True})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result.get("tabId")})
        self.assertIs(result.get("rendering"), True, result)
        self.assertNotIn("placeholder", result)
        self.assertIn("browser_wait_for(text=...)", result["rendering_hint"])

    def test_an_aria_busy_section_says_so(self):
        tab = self.open_tab(f"{self.site.origin}/plain")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/busy"}))
        self.assertIs(result.get("rendering"), True, result)

    def test_a_rendered_page_and_a_blank_tab_add_nothing(self):
        tab = self.open_tab(f"{self.site.origin}/loading")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/plain"}))
        self.assertNotIn("rendering", result)
        blank = self.call("/api/new-tab", {"url": "about:blank", "wait": True})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": blank.get("tabId")})
        self.assertNotIn("rendering", blank)


if __name__ == "__main__":
    unittest.main()
