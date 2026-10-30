"""Issue #199 in a real browser: KITSUNE_PROTECT_USER_TABS keeps navigation off the user's tabs,
by default (#326).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
browser's startup tab stands in for the user's starting tab: the agent did not open it.
It is also the tab the user views, which #326 holds for every write; calls that are not about
that hold send KITSUNE_HOLD_VIEWED_TAB=0's header (unheld).
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest
from unittest.mock import patch

import real_browser_harness as harness
from kitsune import playwright_tools, server, tools
from kitsune.task_modes import HOLD_VIEWED_TAB_HEADER, MODE_HEADER, PROTECT_USER_TABS_HEADER


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

    def unprotected(self, path, body, hold=True):
        """POST as a client with KITSUNE_PROTECT_USER_TABS=0 and, unless hold, KITSUNE_HOLD_VIEWED_TAB=0."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json", PROTECT_USER_TABS_HEADER: "0"}
        if not hold:
            headers[HOLD_VIEWED_TAB_HEADER] = "0"
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def unheld(self, path, body):
        return self.unprotected(path, body, hold=False)

    def tab_url(self, tab_id):
        return {t["id"]: t["url"] for t in self.call("/api/tabs")["tabs"]}[tab_id]

    def user_tab(self):
        tab = next(t for t in self.call("/api/tabs")["tabs"] if not t["owned"])
        # Start each test from a known page; protection and the viewed-tab hold are off for this call.
        self.assert_ok(self.unheld("/api/navigate", {"url": self.page.origin + "/start", "tabId": tab["id"]}))
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
        # On by default (#326): a client that sends no header is protected too.
        self.assert_error(self.call("/api/navigate", {"url": self.page.origin + "/other", "tabId": tab}), "user_tab",
                          False)
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
        # Without protection the user tab is still the one the user views (#326): user_viewing, not user_tab.
        free = {"url": self.page.origin + "/free", "tabId": tab}
        self.assert_error(self.unprotected("/api/navigate", free), "user_viewing", False)
        self.assertEqual(self.tab_url(tab), self.page.origin + "/approved")
        self.assert_ok(self.unheld("/api/navigate", free))
        self.assertEqual(self.tab_url(tab), self.page.origin + "/free")

    def restricted(self, path, body=None, mode="read"):
        """(HTTP status, reply) of a request from a read/draft client that protects user tabs."""
        headers = {**self.bridge.auth(), MODE_HEADER: mode, PROTECT_USER_TABS_HEADER: "1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, _, text = harness._request(self.bridge.http_port, "GET" if body is None else "POST", path,
                                           headers, self.bridge.host,
                                           None if body is None else json.dumps(body), timeout=90)
        return status, json.loads(text)

    def test_read_mode_opens_and_reads_its_own_tab_and_leaves_user_tabs_alone(self):
        # #270: read + KITSUNE_PROTECT_USER_TABS=1 can reach any page in a tab of its own.
        user = self.user_tab()
        get = lambda path: self.restricted(path)[1]
        post = lambda path, body, timeout=15: self.restricted(path, body)[1]
        with patch.object(tools, "_get", get), patch.object(tools, "_post", post), \
                patch.object(playwright_tools, "_current_tab", None):
            opened = tools.browser_tab_list(new_tab=self.page.origin + "/own")
            self.assertIsInstance(opened.get("tabId"), int, opened)
            own = opened["tabId"]
            self.addCleanup(self.call, "/api/close-tab", {"tabId": own})
            self.assertEqual(playwright_tools._current_tab, own)
            self.assertIn(own, [tab["id"] for tab in opened["tabs"]])
            self.assertEqual(opened["status"], "complete")
        status, navigated = self.restricted("/api/navigate", {"url": self.page.origin + "/next", "tabId": own})
        self.assertEqual(status, 200, navigated)
        self.assert_ok(navigated)
        self.assertEqual(self.tab_url(own), self.page.origin + "/next")
        status, page = self.restricted("/api/page-text", {"tabId": own})
        self.assertIn("/next", page.get("text", ""), page)
        self.assertEqual(next(t for t in self.call("/api/tabs")["tabs"] if t["id"] == user)["active"], True,
                         "the user's view did not change")

        status, refused = self.restricted("/api/navigate", {"url": self.page.origin + "/other", "tabId": user})
        self.assert_error(refused, "user_tab", False)
        self.assertIn("browser_tab_list new_tab", refused["hint"])
        self.assertNotIn("confirm_token", refused["hint"])
        self.assertNotIn("confirmToken", refused.get("state", {}), "read mode cannot use a token")
        for path in ("/api/switch-tab", "/api/close-tab"):
            status, result = self.restricted(path, {"tabId": own})
            self.assertEqual((status, result.get("code")), (403, "mode_restricted"), path)
        self.assertEqual(self.tab_url(user), self.page.origin + "/start")

    def test_draft_mode_opens_a_tab_without_a_confirmation(self):
        status, opened = self.restricted("/api/new-tab", {"url": self.page.origin + "/draft", "wait": True},
                                         mode="draft")
        self.assertEqual(status, 200, opened)
        self.assert_ok(opened)
        self.addCleanup(self.call, "/api/close-tab", {"tabId": opened["tabId"]})
        status, refused = self.restricted("/api/new-tab", {"url": "file:///etc/hosts"}, mode="draft")
        self.assertEqual((status, refused["code"]), (403, "mode_restricted"))


if __name__ == "__main__":
    unittest.main()
