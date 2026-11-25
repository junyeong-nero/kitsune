"""A button's snapshot line: its name and its value, in a real browser (issues #649, #656).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
A <button value="x">Named</button> is named by its text, the name role= selectors match;
an <input type=submit value="Go"> by its value. A button's value is shown only when it says
something its name does not (#656); a text field keeps its empty value.
"""
from http.server import BaseHTTPRequestHandler
import re
import unittest

import real_browser_harness as harness

PAGES = {
    "/buttons": """<!doctype html><title>Buttons</title>
<button>Search</button><button value="x">Named</button><input type=submit value="Go">
<label>Title <input name=title></label><input type=button value="Same"><button value="Same">Same</button>""",
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
        self.assertEqual(button_lines(snapshot), [
            '- button "Search" [ref]',
            '- button "Named" [ref] [value="x"]',
            '- button "Go" [ref]',
            '- button "Same" [ref]',
            '- button "Same" [ref]',
        ], snapshot)
        self.assertRegex(snapshot, r'(?m)^- textbox "Title" \[ref=\w+\] \[value=""\]$')

        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": 'role=button[name="Named"]'}))


if __name__ == "__main__":
    unittest.main()
