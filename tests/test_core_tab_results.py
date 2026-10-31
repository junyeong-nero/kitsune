"""browser_tabs new, select and close return the tabs as list does (#224).

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

    def test_the_tab_the_user_may_be_viewing_is_not_marked_current(self):
        # #383: A is active and the agent did not open it, so writes there are held: "current" would read
        # as the agent's own tab. It is still the current tab, and select names its index.
        self.open.append(_tab(5, "C"))  # the user's background tab is marked as before
        self.assertEqual(playwright_tools.browser_tabs("select", index=0), {
            "ok": True, "index": 0, "tabs": [
                {"index": 0, "title": "A", "url": "https://a/", "active": True},
                {"index": 1, "title": "B", "url": "https://b/", "owned": True},
                {"index": 2, "title": "C", "url": "https://c/"}]})
        self.assertEqual(playwright_tools._current_tab, 3)
        self.assertTrue(playwright_tools.browser_tabs("select", index=2)["tabs"][2]["current"])
        # A client that lifted the hold (KITSUNE_HOLD_VIEWED_TAB=0) sees it marked.
        with patch.object(tools.client, "hold_viewed_tab", False):
            self.assertTrue(playwright_tools.browser_tabs("select", index=0)["tabs"][0]["current"])

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
