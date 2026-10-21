"""Issue #87 in a real browser: a target=_blank click reports the tab it opened.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
profile allows popups, as the benchmark profiles do; the default-prefs case is issue #94.
"""
from http.server import BaseHTTPRequestHandler
import unittest

import real_browser_harness as harness

PAGES = {
    "/ok": '<title>ok</title><a id="new" href="/ok2" target="_blank">new tab link</a>'
           '<button id="noop">noop</button>',
    "/ok2": "<title>ok2</title><h1>second</h1>",
}


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "not found").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserOpenedTabTests(harness.RealBrowserTestCase):
    # The popup blocker drops a synthetic click's new tab, so allow popups here.
    PROFILE_PREFS = harness.POPUPS_ALLOWED

    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        cls.page_url = cls.page.origin
        super().setUpClass()

    def click_on_new_page(self, selector, expect=None):
        body = {"tabId": self.open_tab(self.page_url + "/ok"), "selector": selector}
        if expect:
            body["expect"] = expect
        return self.call("/api/click", body)

    def assert_opened_ok2(self, result):
        self.assertEqual(len(result["opened_tabs"]), 1, result)
        opened = result["opened_tabs"][0]
        self.assertEqual(opened["url"], self.page_url + "/ok2")
        tabs = {t["id"]: t["url"] for t in self.call("/api/tabs")["tabs"]}
        self.assertEqual(tabs.get(opened["tabId"]), self.page_url + "/ok2")
        return opened

    def test_target_blank_click_reports_the_opened_tab_without_expect(self):
        result = self.click_on_new_page("#new")
        self.assertIs(result["ok"], True, result)
        self.assert_opened_ok2(result)

    def test_url_changed_failure_names_the_opened_tab(self):
        result = self.click_on_new_page("#new", {"url_changed": True, "timeout": 1000})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        opened = self.assert_opened_ok2(result)
        self.assertIn(f"opened tab {opened['tabId']}", result["hint"])
        self.assertIn(f"tab_id={opened['tabId']}", result["hint"])

    def test_new_tab_expect_is_met(self):
        result = self.click_on_new_page("#new", {"new_tab": True, "timeout": 3000})
        self.assertIs(result["ok"], True, result)
        self.assertIs(result["outcome"]["met"], True)
        self.assert_opened_ok2(result)

    def test_no_op_click_opens_no_tab(self):
        result = self.click_on_new_page("#noop", {"url_changed": True, "timeout": 500})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        self.assertNotIn("opened_tabs", result)
        self.assertNotIn("opened tab", result["hint"])


if __name__ == "__main__":
    unittest.main()
