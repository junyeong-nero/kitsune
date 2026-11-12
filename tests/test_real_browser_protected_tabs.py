"""#395/#398 in a real browser: default read-only user tabs and explicit writable opt-out.

Opt-in via real_browser_harness.py. The startup tab is a user tab; only fixture setup
and explicitly writable scenarios opt out. All owned-tab requests share one client identity.
"""
from http.server import BaseHTTPRequestHandler
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import real_browser_harness as harness
from kitsune import playwright_tools, tools
from kitsune.task_modes import OWNER_HEADER, USER_TABS_HEADER


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
        auth = cls.bridge.auth
        owner = patch.object(cls.bridge, "auth", side_effect=lambda: {**auth(), OWNER_HEADER: "e2e-protected-tabs"})
        owner.start()
        cls.addClassCleanup(owner.stop)

    def protected(self, path, body):
        headers = {**self.bridge.auth(), "Content-Type": "application/json", USER_TABS_HEADER: "read-only"}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def writable(self, path, body):
        """Only explicit compatibility writes carry the authoritative writable permission."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json", USER_TABS_HEADER: "writable"}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def assert_read_only(self, result):
        self.assert_error(result, "user_tab_read_only", False)
        self.assertNotIn("confirmToken", result.get("state", {}))

    def tab_url(self, tab_id):
        return {t["id"]: t["url"] for t in self.call("/api/tabs")["tabs"]}[tab_id]

    def user_tab(self):
        tab = next(t for t in self.call("/api/tabs")["tabs"] if not t["owned"])
        # Explicit fixture setup does not grant ownership.
        self.assert_ok(self.writable("/api/navigate", {"url": self.page.origin + "/start", "tabId": tab["id"]}))
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
            self.assert_read_only(result)
            self.assertEqual(result["state"]["tabId"], tab)
            self.assertIn("browser_tabs", result["hint"])
        self.assert_read_only(self.protected("/api/back", {"tabId": tab}))
        # Read-only by default: a client that sends no header is protected too.
        self.assert_read_only(self.call("/api/navigate", {"url": self.page.origin + "/other", "tabId": tab}))
        batch = self.protected("/api/batch", {"commands": [
            {"action": "navigate", "url": self.page.origin + "/other", "tabId": tab}]})
        self.assert_read_only(batch["results"][0])
        self.assertNotIn("confirmToken", batch["results"][0]["state"])
        self.assertEqual(self.tab_url(tab), self.page.origin + "/start")

    def test_only_explicit_writable_navigation_runs_without_granting_ownership(self):
        tab = self.user_tab()
        body = {"url": self.page.origin + "/free", "tabId": tab}
        self.assert_read_only(self.protected("/api/navigate", {**body, "confirmToken": "forged"}))
        self.assertEqual(self.tab_url(tab), self.page.origin + "/start")
        self.assert_ok(self.writable("/api/navigate", body))
        self.assertEqual(self.tab_url(tab), self.page.origin + "/free")
        self.assertFalse(next(t for t in self.call("/api/tabs")["tabs"] if t["id"] == tab)["owned"])
        self.assert_read_only(self.call("/api/back", {"tabId": tab}))

    def core_tabs(self, user_tabs, action, **arguments):
        """Core browser_tabs as an MCP client with KITSUNE_USER_TABS=user_tabs runs it."""
        def request(path, body=None, timeout=15, start_bridge=True):
            headers = {**self.bridge.auth(), USER_TABS_HEADER: user_tabs}
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, _, text = harness._request(self.bridge.http_port, "GET" if body is None else "POST", path,
                                               headers, self.bridge.host,
                                               None if body is None else json.dumps(body), timeout=90)
            self.assertIn(status, (200, 403), text)
            return json.loads(text)
        with patch.object(tools, "client", SimpleNamespace(user_tabs=user_tabs)), \
                patch.object(tools, "_get", request), patch.object(tools, "_post", request):
            return playwright_tools.browser_tabs(action, **arguments)

    def test_core_select_keeps_current_truthful_and_list_explains_read_only(self):
        # #399: select never hides current; #416: an explicit list explains a read-only current tab;
        # #449: select makes a user tab current, read-only.
        user = self.user_tab()
        start = self.page.origin + "/start"
        with patch.object(playwright_tools, "_current_tab", None):
            index = next(t["index"] for t in self.core_tabs("read-only", "list")["tabs"] if t["url"] == start)
            self.assertFalse(any(t.get("current") for t in self.core_tabs("read-only", "list")["tabs"]))
            selected = self.assert_ok(self.core_tabs("read-only", "select", index=index))
            self.assertIs(selected["read_only"], True, selected)
            self.assertEqual((selected["tab"]["index"], selected["tab"].get("current")), (index, True), selected)
            self.assertEqual(playwright_tools._current_tab, user)

            # The kitsune-benchmark setup: a writable client selects the user's tab and waits for current.
            selected = self.assert_ok(self.core_tabs("writable", "select", index=index))
            self.assertNotIn("hint", selected)
            self.assertNotIn("read_only", selected)
            self.assertEqual((selected["tab"]["index"], selected["tab"].get("current")), (index, True), selected)
            self.assertNotIn("owned", selected["tab"])
            self.assertEqual(playwright_tools._current_tab, user)
            listed = self.core_tabs("read-only", "list")
            self.assertTrue(listed["tabs"][index].get("current"), listed)
            self.assertEqual(listed["hint"], f"Tab {index} is read-only for this client. Before writing, open your "
                                             f"own tab with browser_tabs action=new url={start} and work there.")

            opened = self.core_tabs("read-only", "new", url=self.page.origin + "/own")
            own = playwright_tools._current_tab
            self.assertNotEqual(own, user, opened)
            self.addCleanup(self.call, "/api/close-tab", {"tabId": own})
            self.assertNotIn("hint", opened)
            self.assertEqual({key: opened["tab"].get(key) for key in ("current", "owned")},
                             {"current": True, "owned": True})
            # Indices are positions: the new tab can shift the user's tab.
            index = next(t["index"] for t in self.core_tabs("read-only", "list")["tabs"] if t["url"] == start)
            self.assertIs(self.assert_ok(self.core_tabs("read-only", "select", index=index))["read_only"], True)
            self.assertEqual(playwright_tools._current_tab, user)
            selected = self.assert_ok(self.core_tabs("read-only", "select", index=opened["index"]))
            self.assertNotIn("hint", selected)
            self.assertEqual((selected["tab"]["index"], selected["tab"].get("current")), (opened["index"], True),
                             selected)
            self.assertNotIn("hint", self.core_tabs("read-only", "list"))
        user_tab = next(t for t in self.call("/api/tabs")["tabs"] if t["id"] == user)
        self.assertEqual((user_tab["url"], user_tab["active"], user_tab["owned"]), (start, True, False),
                         "selection neither changed the user's tab nor its view nor granted ownership")


if __name__ == "__main__":
    unittest.main()
