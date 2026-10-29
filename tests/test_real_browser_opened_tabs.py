"""Issues #87, #94, #223 and #304 in a real browser: a click reports the tab it opened.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
first class allows popups, as the benchmark profiles do; the default-prefs class keeps
the browser's own popup blocker (#94). A middle or Ctrl/Cmd click on a link (#223) never
opens a tab by itself, being synthetic, so the extension opens it in both. A button whose
script calls window.open (#304) opens its popup only with popups allowed, and otherwise
gets a popup_blocked warning.
"""
from http.server import BaseHTTPRequestHandler
import time
import unittest

import real_browser_harness as harness

PAGES = {
    "/ok": '<title>ok</title><a id="new" href="/ok2" target="_blank">new tab link</a>'
           '<button id="noop">noop</button>'
           '<a id="opener" href="/ok2" target="_blank" rel="opener">opener link</a>'
           '<a id="handled" href="/ok2" target="_blank" onclick="event.preventDefault()">handled link</a>'
           '<p><a id="plain" href="/ok2">plain link</a></p>'
           '<a id="cancelled" href="/ok2" onclick="event.preventDefault()" onauxclick="event.preventDefault()">'
           'cancelled link</a>'
           '<a id="scripted" href="/ok2" onauxclick="event.preventDefault(); window.open(\'/ok2?script\')"'
           ' onclick="if (event.ctrlKey || event.metaKey) { event.preventDefault(); window.open(\'/ok2?script\') }">'
           'scripted link</a>'
           '<button id="popup" onclick="window.open(\'/ok2?popup\', \'p\', \'width=400\')">popup</button>',
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
    opened_via = None  # the browser opens the link's tab itself
    script_tabs_open = True  # a page's window.open from a synthetic click

    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        cls.page_url = cls.page.origin
        super().setUpClass()

    def click_on_new_page(self, selector, expect=None, **extra):
        self.clicked_tab = self.open_tab(self.page_url + "/ok")
        body = {"tabId": self.clicked_tab, "selector": selector, **extra}
        if expect:
            body["expect"] = expect
        return self.call("/api/click", body)

    def tab_urls(self):
        return {t["id"]: t["url"] for t in self.call("/api/tabs")["tabs"]}

    def assert_opened_ok2(self, result):
        self.assertEqual(len(result["opened_tabs"]), 1, result)
        opened = result["opened_tabs"][0]
        self.assertEqual(opened["url"], self.page_url + "/ok2")
        self.assertEqual(opened.get("via"), self.opened_via, result)
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

    def test_middle_and_modifier_clicks_open_a_plain_link_in_one_new_tab(self):
        for extra in ({"modifiers": ["ControlOrMeta"]}, {"button": "middle"}):
            with self.subTest(extra):
                before = set(self.tab_urls())
                result = self.click_on_new_page("#plain", **extra)
                self.assertIs(result["ok"], True, result)
                self.assertNotIn("warning", result)
                self.assertEqual(len(result["opened_tabs"]), 1, result)
                opened = result["opened_tabs"][0]
                self.addCleanup(self.call, "/api/close-tab", {"tabId": opened["tabId"]})
                self.assertEqual((opened["url"], opened.get("via")), (self.page_url + "/ok2", "link_fallback"))
                time.sleep(1)  # a second tab would have opened by now
                urls = self.tab_urls()
                self.assertEqual(urls[self.clicked_tab], self.page_url + "/ok", "the clicked tab stays")
                self.assertEqual(set(urls) - before - {self.clicked_tab}, {opened["tabId"]}, "no duplicate tab")

    def test_cancelled_modifier_click_says_no_tab_opened(self):
        before = set(self.tab_urls())
        result = self.click_on_new_page("#cancelled", modifiers=["ControlOrMeta"])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["opened_tabs"], [], result)
        self.assertEqual(result["warning"]["code"], "no_tab_opened")
        self.assertEqual(result["warning"]["state"], {"url": self.page_url + "/ok2", "reason": "page_cancelled"})
        self.assertEqual(set(self.tab_urls()) - before, {self.clicked_tab})

    def test_page_opened_tab_is_reported_once(self):
        for extra in ({"modifiers": ["ControlOrMeta"]}, {"button": "middle"}):
            with self.subTest(extra):
                before = set(self.tab_urls())
                result = self.click_on_new_page("#scripted", **extra)
                self.assertIs(result["ok"], True, result)
                time.sleep(1)
                new = set(self.tab_urls()) - before - {self.clicked_tab}
                for tab_id in new:
                    self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
                if self.script_tabs_open:
                    self.assertEqual([t["url"] for t in result["opened_tabs"]], [self.page_url + "/ok2?script"])
                    self.assertNotIn("warning", result)
                    self.assertEqual(len(new), 1, "the page's tab is not duplicated")
                else:
                    self.assertEqual(result["opened_tabs"], [], result)
                    self.assertEqual(result["warning"]["code"], "no_tab_opened")
                    self.assertEqual(new, set())

    def test_script_popup_opens_or_warns(self):
        before = set(self.tab_urls())
        result = self.click_on_new_page("#popup")
        self.assertIs(result["ok"], True, result)
        time.sleep(1)
        new = set(self.tab_urls()) - before - {self.clicked_tab}
        for tab_id in new:
            self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
        if self.script_tabs_open:
            # The popup opens in its own window, whose tab has no openerTabId, so opened_tabs (#87)
            # does not list it; browser_tabs does.
            urls = self.tab_urls()
            self.assertNotIn("warning", result)
            self.assertEqual([urls[tab_id] for tab_id in new], [self.page_url + "/ok2?popup"])
        else:
            self.assertNotIn("opened_tabs", result)
            self.assertEqual(result["warning"]["code"], "popup_blocked", result)
            self.assertEqual(result["warning"]["state"],
                             {"url": self.page_url + "/ok2?popup", "urls": [self.page_url + "/ok2?popup"],
                              "reason": "script_popup"})
            self.assertIn("ask the user to click it", result["warning"]["hint"])
            self.assertEqual(new, set(), "Kitsune opens nothing in the popup's place")

    def test_no_op_click_opens_no_tab(self):
        result = self.click_on_new_page("#noop", {"url_changed": True, "timeout": 500})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        self.assertNotIn("opened_tabs", result)
        self.assertNotIn("opened tab", result["hint"])


class RealBrowserDefaultPrefsOpenedTabTests(RealBrowserOpenedTabTests):
    """#94: under the default popup blocker the extension opens the link's tab itself."""
    PROFILE_PREFS = {}
    opened_via = "link_fallback"
    script_tabs_open = False

    def ok2_tabs(self):
        return sum(t["url"] == self.page_url + "/ok2" for t in self.call("/api/tabs")["tabs"])

    def assert_no_tab_opened(self, result):
        self.assertNotIn("opened_tabs", result)
        self.assertEqual(self.ok2_tabs(), self.ok2_before, result)

    def click_on_new_page(self, selector, expect=None, **extra):
        self.ok2_before = self.ok2_tabs()
        return super().click_on_new_page(selector, expect, **extra)

    def test_rel_opener_link_reports_popup_blocked(self):
        result = self.click_on_new_page("#opener")
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["warning"]["code"], "popup_blocked", result)
        self.assertEqual(result["warning"]["state"]["url"], self.page_url + "/ok2")
        self.assert_no_tab_opened(result)

    def test_new_tab_expect_names_the_popup_blocker(self):
        result = self.click_on_new_page("#opener", {"new_tab": True, "timeout": 1000})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        self.assertIn("popup blocker", result["hint"])
        self.assert_no_tab_opened(result)

    def test_link_whose_click_the_page_cancelled_opens_no_tab(self):
        result = self.click_on_new_page("#handled")
        self.assertIs(result["ok"], True, result)
        self.assertNotIn("warning", result)
        self.assert_no_tab_opened(result)


if __name__ == "__main__":
    unittest.main()
