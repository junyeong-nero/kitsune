"""browser_tabs action tab lists (#224) and explicit-list permission guidance (#416).

new reports the new tab's index (the index every other action takes), not the bridge's tabId.
"""
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools


def _tab(tab_id, title, **flags):
    return {"id": tab_id, "title": title, "url": f"https://{title.lower()}/", **flags}


class TabResultTests(unittest.TestCase):
    def setUp(self):
        playwright_tools._current_tab = None
        playwright_tools._clicked.clear()
        playwright_tools._requests.clear()
        self.open = [_tab(3, "A", active=True), _tab(8, "B", owned=True)]
        permission = patch.object(tools.client, "user_tabs", "read-only")
        permission.start()
        self.addCleanup(permission.stop)
        post = patch.object(tools, "_post", side_effect=self.bridge)
        get = patch.object(tools, "_get", side_effect=lambda endpoint: {"tabs": list(self.open)})
        self.post, self.get = post.start(), get.start()
        self.addCleanup(post.stop)
        self.addCleanup(get.stop)

    def bridge(self, endpoint, data, timeout=15):
        if endpoint == "/api/new-tab":
            self.open.append(_tab(11, "D", owned=True))
            return {"ok": True, "tabId": 11, "url": "https://d/", "status": "complete"}
        if endpoint == "/api/close-tab":
            self.open = [tab for tab in self.open if tab["id"] != data["tabId"]]
        return {"ok": True}

    def test_new_returns_its_index_and_the_tabs(self):
        self.assertEqual(playwright_tools.browser_tabs("new", url="https://d/"), {
            "ok": True, "url": "https://d/", "status": "complete", "index": 2, "tabs": [
                {"index": 0, "title": "A", "url": "https://a/", "active": True},
                {"index": 1, "title": "B", "url": "https://b/", "owned": True},
                {"index": 2, "title": "D", "url": "https://d/", "current": True, "owned": True}]})
        self.assertEqual(playwright_tools._current_tab, 11)

    def test_select_returns_the_tabs_with_the_current_marker(self):
        self.assertEqual(playwright_tools.browser_tabs("select", index=1), {
            "ok": True, "index": 1, "tabs": [
                {"index": 0, "title": "A", "url": "https://a/", "active": True},
                {"index": 1, "title": "B", "url": "https://b/", "current": True, "owned": True}]})

    def test_current_is_truthful_for_user_and_other_client_tabs(self):
        # Ownership can change after selection; listing never hides the agent's current tab.
        playwright_tools._current_tab = 3
        self.assertTrue(playwright_tools.browser_tabs("list")["tabs"][0]["current"])
        self.open[0]["owned"] = True
        self.assertTrue(playwright_tools.browser_tabs("list")["tabs"][0]["current"])
        self.open[0]["owned"] = False
        self.assertTrue(playwright_tools.browser_tabs("list")["tabs"][0]["current"])

    def test_list_hint_for_current_user_or_foreign_tab_including_background(self):
        # The inventory represents both user and foreign tabs as not owned by this client.
        for ownership in ({}, {"owned": False}):
            for active in (False, True):
                with self.subTest(ownership=ownership, active=active):
                    self.open[0] = _tab(3, "A", active=active, **ownership)
                    playwright_tools._current_tab = 3
                    self.get.reset_mock()
                    result = playwright_tools.browser_tabs("list")
                    self.assertEqual(result["hint"],
                        "Tab 0 is read-only for this client. Before writing, open your own tab "
                        "with browser_tabs action=new url=https://a/ and work there.")
                    self.assertTrue(result["tabs"][0]["current"])
                    self.assertEqual(bool(result["tabs"][0].get("active")), active)
                    self.assertNotIn("owned", result["tabs"][0])
                    self.assertNotIn("code", result)
                    self.get.assert_called_once_with("/api/tabs")
        self.post.assert_not_called()

    def test_list_hint_tracks_current_index_url_and_ownership_changes(self):
        playwright_tools._current_tab = 3
        self.open.reverse()
        self.open[1]["url"] = "https://a/path?q=one&next=two#part"
        result = playwright_tools.browser_tabs("list")
        self.assertIn("Tab 1 is read-only", result["hint"])
        self.assertIn("url=https://a/path?q=one&next=two#part", result["hint"])
        # An authorized handoff is listed as owned; taking it back removes that grant.
        self.open[1]["owned"] = True
        self.assertNotIn("hint", playwright_tools.browser_tabs("list"))
        self.open[1]["owned"] = False
        self.assertEqual(playwright_tools.browser_tabs("list"), result)

    def test_owned_current_has_no_hint_even_with_active_user_tab(self):
        playwright_tools._current_tab = 8
        result = playwright_tools.browser_tabs("list")
        self.assertNotIn("hint", result)
        self.assertTrue(result["tabs"][1]["current"])

    def test_writable_current_user_tab_has_no_list_or_select_hint(self):
        playwright_tools._current_tab = 3
        with patch.object(tools.client, "user_tabs", "writable"):
            for action, arguments in (("list", {}), ("select", {"index": 0})):
                result = playwright_tools.browser_tabs(action, **arguments)
                self.assertNotIn("hint", result)
                self.assertTrue(result["tabs"][0]["current"])

    def test_no_current_or_stale_current_has_no_hint(self):
        for current in (None, 99):
            with self.subTest(current=current):
                playwright_tools._current_tab = current
                result = playwright_tools.browser_tabs("list")
                self.assertNotIn("hint", result)
                self.assertFalse(any(tab.get("current") for tab in result["tabs"]))
                self.assertIsNone(playwright_tools._current_tab)

    def test_failed_list_preserves_error_without_permission_hint(self):
        playwright_tools._current_tab = 3
        failure = {"error": "bridge down", "hint": "Reconnect the bridge."}
        self.get.side_effect = None
        self.get.return_value = failure
        self.assertEqual(playwright_tools.browser_tabs("list"), failure)
        self.assertEqual(playwright_tools._current_tab, 3)
        self.get.assert_called_once_with("/api/tabs")
        self.post.assert_not_called()

    def test_denied_select_keeps_current_and_existing_refusal_hint(self):
        refusal = {"error": "read only", "code": "user_tab_read_only", "retryable": False,
                   "state": {"tabId": 3, "index": 0, "action": "switchTab"},
                   "hint": "Open a tab of your own with browser_tabs action=new and this URL, or request this tab "
                           "with browser_tabs action=request; reads must not scroll or focus it."}
        self.post.side_effect = None
        self.post.return_value = refusal
        for current in (None, 3, 8):
            with self.subTest(current=current):
                playwright_tools._current_tab = current
                self.get.reset_mock()
                self.post.reset_mock()
                self.assertEqual(playwright_tools.browser_tabs("select", index=0), refusal)
                self.assertEqual(playwright_tools._current_tab, current)
                self.get.assert_called_once_with("/api/tabs")
                self.post.assert_called_once_with("/api/tab-write-check", {"tabId": 3, "writeAction": "switchTab"})

    def test_close_other_tab_does_not_hint_about_current_user_tab(self):
        playwright_tools._current_tab = 3
        result = playwright_tools.browser_tabs("close", index=1)
        self.assertEqual(result, {"ok": True, "tabs": [
            {"index": 0, "title": "A", "url": "https://a/", "active": True, "current": True}]})
        self.assertIn("hint", playwright_tools.browser_tabs("list"))

    def test_request_preserves_its_result_without_list_hint(self):
        playwright_tools._current_tab = 3
        for response in ({"ok": True, "tabId": 3, "owned": True},
                         {"error": "Approval required", "code": "needs_user", "hint": "Ask the user."}):
            with self.subTest(response=response):
                self.post.reset_mock()
                self.post.side_effect = None
                self.post.return_value = response
                self.assertEqual(playwright_tools.browser_tabs("request", tab_id=3, reason="Edit page"), response)
                self.post.assert_called_once()
                self.get.assert_not_called()
                self.assertEqual(playwright_tools._current_tab, 3)

    def test_close_returns_the_remaining_tabs(self):
        playwright_tools.browser_tabs("select", index=1)
        self.assertEqual(playwright_tools.browser_tabs("close", index=0), {
            "ok": True, "tabs": [{"index": 0, "title": "B", "url": "https://b/", "current": True, "owned": True}]})
        self.assertEqual(playwright_tools.browser_close(), {"ok": True, "tabs": []})
        self.assertIsNone(playwright_tools._current_tab)

    def test_results_match_list(self):
        for action, arguments in (("new", {"url": "https://d/"}), ("select", {"index": 0}), ("close", {"index": 1})):
            with self.subTest(action=action):
                result = playwright_tools.browser_tabs(action, **arguments)
                self.assertEqual(result["tabs"], playwright_tools.browser_tabs("list")["tabs"])
                self.assertNotIn("tabId", result)

    def test_a_failed_action_has_no_tab_list(self):
        self.post.side_effect = None
        self.post.return_value = {"error": "user_tab", "code": "user_tab"}
        self.assertEqual(playwright_tools.browser_tabs("close", index=0), {"error": "user_tab", "code": "user_tab"})
        self.post.return_value = {"error": "confirmation_required", "code": "confirmation_required"}
        self.assertNotIn("tabs", playwright_tools.browser_tabs("new", url="https://d/"))

    def test_a_timed_out_new_tab_is_current_and_listed(self):
        self.post.side_effect = None
        self.open.append(_tab(11, "D", owned=True))
        self.post.return_value = {"ok": False, "timedOut": True, "tabId": 11, "url": "https://d/"}
        result = playwright_tools.browser_tabs("new", url="https://d/")
        self.assertEqual((result["ok"], result["timedOut"], result["index"]), (False, True, 2))
        self.assertNotIn("tabId", result)

    def test_a_stalled_new_tab_keeps_its_index_and_recovery_guidance(self):
        self.post.side_effect = None
        self.open.append({"id": 11, "title": "example.com", "url": "about:blank", "owned": True})
        self.post.return_value = {
            "ok": False, "timedOut": True, "tabId": 11, "url": "about:blank", "status": "complete",
            "code": "new_tab_stalled", "retryable": False,
            "hint": "The tab was created; save work and restart if the browser was updated.",
        }
        result = playwright_tools.browser_tabs("new", url="https://example.com")
        self.assertEqual(result["code"], "new_tab_stalled")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["hint"], self.post.return_value["hint"])
        self.assertEqual(result["index"], 2)
        self.assertEqual(playwright_tools._current_tab, 11)
        self.assertEqual(result["tabs"][2], {
            "index": 2, "title": "example.com", "url": "about:blank", "owned": True, "current": True,
        })
        self.assertNotIn("tabId", result)
        self.post.assert_called_once()

    def test_a_failed_listing_keeps_the_action_result(self):
        playwright_tools.browser_tabs("select", index=1)
        self.get.side_effect = None
        self.get.return_value = {"error": "bridge down"}
        self.assertEqual(playwright_tools.browser_tabs("new", url="https://d/"),
                         {"ok": True, "url": "https://d/", "status": "complete"})
        self.assertEqual(playwright_tools._current_tab, 11)


if __name__ == "__main__":
    unittest.main()
