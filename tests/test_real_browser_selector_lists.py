"""A comma selector list with a Playwright item (#635), in a real browser.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/playwright_selectors.cjs checks the same rule on a mocked DOM; here Firefox's own CSS
parser rejects the list, and commas inside quotes, brackets and :is() stay part of one item.
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest

import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>mail</title>
<button id="rcmbtn_delete">Delete</button>
<button id="archive" title="Archive, then close">Archive, then close</button>
<script>
  document.addEventListener('click', event => {
    const list = JSON.parse(document.documentElement.dataset.clicked || '[]');
    document.documentElement.dataset.clicked = JSON.stringify([...list, event.target.id]);
  }, true);
</script>"""
CLICKED = "document.documentElement.dataset.clicked || '[]'"
RESET = "document.documentElement.dataset.clicked = '[]'"


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserSelectorListTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def clicked(self, tab):
        return json.loads(self.call("/api/js", {"tabId": tab, "code": CLICKED})["result"])

    def test_a_list_with_a_playwright_item_names_it(self):
        tab = self.open_tab(self.page.origin + "/")
        selector = "a.delete, #rcmbtn_delete, text=Delete"
        for path in ("/api/click", "/api/snapshot"):
            with self.subTest(path):
                result = self.call(path, {"tabId": tab, "selector": selector})
                self.assert_error(result, "unsupported_selector", False)
                self.assertEqual(result["error"], "Unsupported selector: a comma selector list is CSS only; "
                                                  "`text=Delete` is a Playwright form")
                self.assertEqual(result["hint"], "Try each alternative in turn, or use "
                                                 "`:is(a.delete, #rcmbtn_delete)` plus a separate `text=Delete`.")
                self.assertEqual(result["state"]["selector"], selector)
        self.assertEqual(self.clicked(tab), [])
        # The hint's alternatives each click the button.
        for alternative in (":is(a.delete, #rcmbtn_delete)", "text=Delete"):
            self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": alternative, "includeSnapshot": False}))
        self.assertEqual(self.clicked(tab), ["rcmbtn_delete", "rcmbtn_delete"])

    def test_commas_inside_quotes_brackets_and_groups_are_one_item(self):
        tab = self.open_tab(self.page.origin + "/")
        for selector in ('text="Archive, then close"', 'role=button[name="Archive, then close"]',
                         'text=Archive, then close', '[title="Archive, then close"]', ':is(#missing, #archive)',
                         '#missing, button:has-text("Archive, then")'):
            with self.subTest(selector):
                self.call("/api/js", {"tabId": tab, "code": RESET})
                self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": selector, "includeSnapshot": False}))
                self.assertEqual(self.clicked(tab), ["archive"])


if __name__ == "__main__":
    unittest.main()
