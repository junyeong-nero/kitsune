"""browser_tabs action tab lists (#224), a truthful current marker (#399), explicit-list permission guidance (#416)
and select of any tab, read-only where this client cannot write it (#449).

new reports the new tab's index (the index every other action takes), not the bridge's tabId.
new and select return the tab they made current and the tab count, not the whole list (#488).
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
        if endpoint == "/api/request-tab":
            # An approved handoff: the inventory lists the tab as owned by this client from now on.
            next(tab for tab in self.open if tab["id"] == data["tabId"])["owned"] = True
            return {"ok": True, "tabId": data["tabId"], "owned": True}
        if endpoint == "/api/tab-write-check":
            # The bridge admits writes on owned tabs, and on any tab for a writable client.
            target = next(tab for tab in self.open if tab["id"] == data["tabId"])
            if not target.get("owned") and tools.client.user_tabs != "writable":
                return {"error": "read only", "code": "user_tab_read_only", "retryable": False,
                        "state": {"tabId": target["id"], "index": self.open.index(target),
                                  "action": data["writeAction"]}}
        return {"ok": True}

    def test_new_returns_its_index_and_tab_and_the_tab_count(self):
        self.assertEqual(playwright_tools.browser_tabs("new", url="https://d/"), {
            "ok": True, "url": "https://d/", "status": "complete", "index": 2,
            "tab": {"index": 2, "title": "D", "url": "https://d/", "current": True, "owned": True},
            "tab_count": 3})
        self.assertEqual(playwright_tools._current_tab, 11)

    def test_new_with_a_url_carries_the_page_text(self):
        # #448: like browser_navigate's (#401), the extension's pageText snapshot of a small page is in the result.
        bridge = self.post.side_effect
        self.post.side_effect = lambda endpoint, data, timeout=15: (
            {**bridge(endpoint, data), "snapshot": '- text "Open 13:00–18:00"'} if endpoint == "/api/new-tab"
            else bridge(endpoint, data))
        result = playwright_tools.browser_tabs("new", url="https://d/")
        self.assertEqual((result["snapshot"], result["index"]), ('- text "Open 13:00–18:00"', 2))
        endpoint, body = self.post.call_args.args
        self.assertEqual((endpoint, body["pageText"]), ("/api/new-tab", True))

    def test_select_returns_the_selected_tab_and_the_tab_count(self):
        self.assertEqual(playwright_tools.browser_tabs("select", index=1), {
            "ok": True, "index": 1,
            "tab": {"index": 1, "title": "B", "url": "https://b/", "current": True, "owned": True},
            "tab_count": 2})

    def test_new_and_select_leave_out_the_tab_list(self):
        # #488: the list was most of each result's characters; action=list still returns it.
        for action, arguments in (("new", {"url": "https://d/"}), ("select", {"index": 0}), ("new", {})):
            with self.subTest(action=action, arguments=arguments):
                result = playwright_tools.browser_tabs(action, **arguments)
                listed = playwright_tools.browser_tabs("list")["tabs"]
                self.assertNotIn("tabs", result)
                self.assertEqual(result["tab_count"], len(listed))
                self.assertEqual(result["tab"], listed[result["index"]])
                self.assertTrue(result["tab"]["current"])

    def test_select_on_a_handed_off_tab_lists_it_current_without_hint(self):
        # #399: once a handoff is approved, select makes the user's tab current and no longer read-only.
        self.assertTrue(playwright_tools.browser_tabs("select", index=0)["read_only"])
        playwright_tools._current_tab = 8
        self.assertEqual(playwright_tools.browser_tabs("request", index=0, reason="Edit page"),
                         {"ok": True, "tabId": 3, "owned": True})
        self.assertEqual(playwright_tools._current_tab, 8, "a request does not select")
        self.assertEqual(playwright_tools.browser_tabs("select", index=0), {
            "ok": True, "index": 0,
            "tab": {"index": 0, "title": "A", "url": "https://a/", "current": True, "active": True, "owned": True},
            "tab_count": 2})
        self.assertEqual(playwright_tools._current_tab, 3)
        self.assertEqual([call.args[0] for call in self.post.call_args_list], ["/api/request-tab"])
        self.assertNotIn("hint", playwright_tools.browser_tabs("list"))

    def test_writable_select_on_a_user_tab_makes_it_current(self):
        # #399: the kitsune-benchmark setup selects the user's starting tab and waits for current.
        self.assertTrue(playwright_tools.browser_tabs("select", index=0)["read_only"])
        with patch.object(tools.client, "user_tabs", "writable"):
            for current in (None, 8):
                with self.subTest(current=current):
                    playwright_tools._current_tab = current
                    self.assertEqual(playwright_tools.browser_tabs("select", index=0), {
                        "ok": True, "index": 0,
                        "tab": {"index": 0, "title": "A", "url": "https://a/", "current": True, "active": True},
                        "tab_count": 2})
                    self.assertEqual(playwright_tools._current_tab, 3)

    def test_select_makes_a_user_or_other_client_tab_current_read_only(self):
        # #449: every tab is readable, so select makes any tab current; it never shows the tab nor asks
        # the bridge, and a tab this client cannot write says so in select's own hint (#416: the list's
        # hint stays the list's).
        for ownership in ({}, {"owned": False}):  # a user tab, another client's tab
            for active in (False, True):
                with self.subTest(ownership=ownership, active=active):
                    self.open[0] = _tab(3, "A", **({"active": True} if active else {}), **ownership)
                    for current in (None, 8):
                        playwright_tools._current_tab = current
                        self.post.reset_mock()
                        self.assertEqual(playwright_tools.browser_tabs("select", index=0), {
                            "ok": True, "index": 0, "read_only": True,
                            "hint": "Reads work on tab 0, but this client cannot write it. To write, open your own "
                                    "tab with browser_tabs action=new url=https://a/, or request this one with "
                                    "browser_tabs action=request.",
                            "tab": {"index": 0, "title": "A", "url": "https://a/", "current": True,
                                    **({"active": True} if active else {})},
                            "tab_count": 2})
                        self.assertEqual(playwright_tools._current_tab, 3)
                        self.post.assert_not_called()
                    self.assertTrue(playwright_tools.browser_tabs("list")["hint"].startswith(
                        "Tab 0 is read-only for this client."))

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
                self.assertTrue((result["tab"] if action == "select" else result["tabs"][0])["current"])

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

    def test_select_of_an_unknown_tab_keeps_current(self):
        for current in (None, 3, 8):
            with self.subTest(current=current):
                playwright_tools._current_tab = current
                self.assertEqual(playwright_tools.browser_tabs("select", index=2)["code"], "not_found")
                self.assertEqual(playwright_tools._current_tab, current)
        self.post.assert_not_called()

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

    def test_close_by_tab_id_closes_that_tab_not_the_current_one(self):
        # #481: open A and B (B current), close tab_id=A: A closes, B stays open and current.
        self.open = [_tab(3, "User", active=True), _tab(20, "A", owned=True), _tab(21, "B", owned=True)]
        playwright_tools._current_tab = 21
        result = playwright_tools.browser_tabs("close", tab_id=20)
        self.assertEqual(self.post.call_args.args[0:2], ("/api/close-tab", {"tabId": 20}))
        self.assertEqual([tab["title"] for tab in result["tabs"]], ["User", "B"])
        self.assertEqual(playwright_tools._current_tab, 21)

        # An unknown tab_id, or one with index, closes nothing.
        self.post.reset_mock()
        self.assertEqual(playwright_tools.browser_tabs("close", tab_id=20)["code"], "not_found")
        self.assertEqual(playwright_tools.browser_tabs("close", tab_id=21, index=1)["code"], "invalid_argument")
        self.post.assert_not_called()

    def test_tabless_close_after_the_current_tab_closed_never_targets_the_users_tab(self):
        # #481: once the current tab is closed, a close naming no tab is refused, not sent to the active tab.
        self.open = [_tab(3, "User", active=True), _tab(20, "A", owned=True)]
        playwright_tools._current_tab = 20
        playwright_tools.browser_tabs("close")
        self.assertIsNone(playwright_tools._current_tab)
        self.post.reset_mock()
        for close in (lambda: playwright_tools.browser_tabs("close"), playwright_tools.browser_close):
            result = close()
            self.assertEqual((result["code"], result["retryable"], result["state"]),
                             ("no_current_tab", False, {"tool": "browser_tabs", "action": "close"}))
        self.post.assert_not_called()
        self.assertEqual([tab["id"] for tab in self.open], [3])

    def test_tab_id_is_refused_where_it_is_not_supported(self):
        # #481: an ignored tab_id would act on another tab than the one named.
        playwright_tools._current_tab = 8
        for action in ("list", "new", "select"):
            with self.subTest(action=action):
                result = playwright_tools.browser_tabs(action, index=0, tab_id=3)
                self.assertEqual((result["code"], result["retryable"]), ("invalid_argument", False))
                self.assertIn(f"action={action}", result["error"])
        self.post.assert_not_called()
        self.assertEqual(playwright_tools._current_tab, 8)

    def test_results_match_list(self):
        for action, arguments in (("new", {"url": "https://d/"}), ("select", {"index": 1}), ("close", {"index": 1})):
            with self.subTest(action=action):
                result = playwright_tools.browser_tabs(action, **arguments)
                listed = playwright_tools.browser_tabs("list")["tabs"]
                if action == "close":
                    self.assertEqual(result["tabs"], listed)
                else:
                    self.assertEqual((result["tab"], result["tab_count"]), (listed[result["index"]], len(listed)))
                self.assertNotIn("tabId", result)

    def test_a_failed_action_has_no_tab_list(self):
        self.post.side_effect = None
        self.post.return_value = {"error": "user_tab", "code": "user_tab"}
        self.assertEqual(playwright_tools.browser_tabs("close", index=0), {"error": "user_tab", "code": "user_tab"})
        self.post.return_value = {"error": "confirmation_required", "code": "confirmation_required"}
        self.assertNotIn("tab", playwright_tools.browser_tabs("new", url="https://d/"))

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
        self.assertEqual(result["tab"], {
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
