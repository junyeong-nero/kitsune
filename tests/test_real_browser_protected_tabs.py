"""Issue #199 in a real browser: KITSUNE_PROTECT_USER_TABS keeps navigation off the user's tabs.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
browser's startup tab stands in for the user's starting tab: the agent did not open it.
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest

import real_browser_harness as harness
from kitsune.task_modes import PROTECT_USER_TABS_HEADER


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<title>{self.path}</title><h1>{self.path}</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserProtectedTabTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def protected(self, path, body):
        headers = {**self.bridge.auth(), "Content-Type": "application/json", PROTECT_USER_TABS_HEADER: "1"}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def tab_url(self, tab_id):
        return {t["id"]: t["url"] for t in self.call("/api/tabs")["tabs"]}[tab_id]

    def user_tab(self):
        tab = next(t for t in self.call("/api/tabs")["tabs"] if not t["owned"])
        # Start each test from a known page; protection is off for this call.
        self.assert_ok(self.call("/api/navigate", {"url": self.page.origin + "/start", "tabId": tab["id"]}))
        return tab["id"]

    def test_owned_helper_tab_navigates_and_closes(self):
        helper = self.open_tab(self.page.origin + "/helper")
        self.assert_ok(self.protected("/api/navigate", {"url": self.page.origin + "/next", "tabId": helper}))
        self.assertEqual(self.tab_url(helper), self.page.origin + "/next")
        self.assert_ok(self.protected("/api/close-tab", {"tabId": helper}))

    def test_protected_starting_tab_is_not_navigated(self):
        tab = self.user_tab()
        for body in ({"url": self.page.origin + "/other", "tabId": tab}, {"url": self.page.origin + "/other"}):
            result = self.protected("/api/navigate", body)
            self.assert_error(result, "user_tab", False)
            self.assertEqual(result["state"]["tabId"], tab)
            self.assertIn("browser_tabs", result["hint"])
        self.assert_error(self.protected("/api/back", {"tabId": tab}), "user_tab", False)
        batch = self.protected("/api/batch", {"commands": [
            {"action": "navigate", "url": self.page.origin + "/other", "tabId": tab}]})
        self.assertEqual(batch["results"][0]["code"], "user_tab", batch)
        self.assertNotIn("confirmToken", batch["results"][0]["state"])
        self.assertEqual(self.tab_url(tab), self.page.origin + "/start")

    def test_approved_and_unprotected_navigation_runs(self):
        tab = self.user_tab()
        body = {"url": self.page.origin + "/approved", "tabId": tab}
        token = self.protected("/api/navigate", body)["state"]["confirmToken"]
        self.assert_ok(self.protected("/api/navigate", {**body, "confirmToken": token}))
        self.assertEqual(self.tab_url(tab), self.page.origin + "/approved")
        self.assert_ok(self.call("/api/navigate", {"url": self.page.origin + "/free", "tabId": tab}))
        self.assertEqual(self.tab_url(tab), self.page.origin + "/free")


if __name__ == "__main__":
    unittest.main()
