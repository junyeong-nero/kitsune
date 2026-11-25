"""A button's snapshot line: its name and its value, in a real browser (issue #649).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
A <button value="x">Named</button> is named by its text, the name role= selectors match;
an <input type=submit value="Go"> by its value.
"""
from http.server import BaseHTTPRequestHandler
import re
import unittest

import real_browser_harness as harness

PAGES = {
    "/buttons": """<!doctype html><title>Buttons</title>
<button>Search</button><button value="x">Named</button><input type=submit value="Go">""",
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


def button_lines(snapshot):
    return [re.sub(r"\[ref=\w+\]", "[ref]", line.strip()) for line in snapshot.splitlines()
            if line.strip().startswith("- button")]


class RealBrowserButtonNameTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_a_button_is_named_by_its_text_and_a_submit_input_by_its_value(self):
        tab = self.open_tab(f"{self.site.origin}/buttons")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        lines = button_lines(snapshot)
        self.assertIn('- button "Named" [ref] [value="x"]', lines, snapshot)
        self.assertTrue(any(line.startswith('- button "Go" [ref]') for line in lines), snapshot)
        self.assertNotIn('button "x"', snapshot)

        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": 'role=button[name="Named"]'}))


if __name__ == "__main__":
    unittest.main()
