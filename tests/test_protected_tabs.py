"""Issue #199: a session can protect user tabs from server-mediated navigation (KITSUNE_PROTECT_USER_TABS).

Ownership, permission to close and permission to navigate are separate: an owned helper tab
closes and navigates freely, a protected user tab navigates only with the user's approval, and
without protection navigation is unchanged.
"""
import asyncio
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, server, task_modes

ACTIVE_TAB = 4


class FakeExtension:
    """Refuses protected navigation of tabs it does not own, like background.js handleCommand."""

    def __init__(self, owned=(9,)):
        self.owned = set(owned)
        self.sent = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        tab_id = params.get("tabId") or ACTIVE_TAB
        if (action in {"navigate", "goBack", "goForward"} and params.get("protectUserTabs")
                and tab_id not in self.owned and not params.get("userTabApproved")):
            return {"error": f"Tab {tab_id} was not opened by the agent, so it was not navigated",
                    "code": "user_tab", "retryable": False, "hint": "extension hint",
                    "state": {"tabId": tab_id, "url": "https://user.example/"}}
        if action == "closeTab" and tab_id not in self.owned and not params.get("userTabApproved"):
            return {"error": f"Tab {tab_id} was not opened by the agent, so it was not closed",
                    "code": "user_tab", "retryable": False, "state": {"tabId": tab_id}}
        return {"ok": True}

    def last(self, action):
        return [params for sent, params in self.sent if sent == action][-1]


class ProtectedTabTests(unittest.TestCase):
    def setUp(self):
        self.extension = FakeExtension()
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": False,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
            patch.dict(bridge._unknown_outcomes, clear=True),
            patch.object(bridge, "_audit_log", []),
            patch.object(bridge, "_global_command_lock", None),
            patch.object(bridge, "_tab_command_locks", {}),
            patch.object(bridge, "_scheduler_gate", None),
            patch.object(bridge, "_send_to_extension_raw", new=lambda *a, **k: self.extension(*a, **k)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, path, body, protect=True):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = {task_modes.PROTECT_USER_TABS_HEADER: "1"} if protect else {}
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler.authorized = Mock(return_value=True)
        handler.do_POST()
        status, result = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        return result

    def refuse(self, path, body):
        result = self.call(path, body)
        self.assertEqual((result["code"], result["retryable"]), ("user_tab", False), result)
        self.assertIn("browser_tabs", result["hint"])
        self.assertIn("tab_id", result["hint"])
        return result

    # (a) Owned helper tabs: navigation and cleanup are unchanged under protection.
    def test_owned_helper_tab_navigates_and_closes(self):
        self.assertEqual(self.call("/api/navigate", {"url": "https://a.example/", "tabId": 9}), {"ok": True})
        self.assertIs(self.extension.last("navigate")["protectUserTabs"], True)
        self.assertEqual(self.call("/api/back", {"tabId": 9}), {"ok": True})
        self.assertEqual(self.call("/api/close-tab", {"tabId": 9}), {"ok": True})

    # (b) Protected initial tabs: navigate, back and forward are refused, nothing is redirected.
    def test_protected_user_tab_refuses_navigation(self):
        result = self.refuse("/api/navigate", {"url": "https://a.example/", "tabId": 4})
        self.assertEqual(result["state"]["tabId"], 4)
        self.assertEqual(result["state"]["expiresInSeconds"], bridge.CONFIRM_TOKEN_TTL)
        self.refuse("/api/back", {"tabId": 4})
        self.refuse("/api/forward", {"tabId": 4})
        # Without tab_id the active (user) tab is the target; it is refused, not swapped for another tab.
        self.assertEqual(self.refuse("/api/navigate", {"url": "https://a.example/"})["state"]["tabId"], ACTIVE_TAB)
        self.assertEqual([p.get("tabId") for a, p in self.extension.sent if a == "navigate"], [4, None])

    def test_batch_steps_are_refused_without_a_token(self):
        result = self.call("/api/batch", {"commands": [
            {"action": "navigate", "url": "https://a.example/", "tabId": 4, "userTabApproved": True}]})
        step = result["results"][0]
        self.assertEqual(step["code"], "user_tab")
        self.assertNotIn("confirmToken", step["state"])
        self.assertNotIn("userTabApproved", self.extension.last("navigate"))
        self.assertIs(self.extension.last("navigate")["protectUserTabs"], True)

    def test_limits_clicks_are_not_guarded(self):
        self.call("/api/click", {"selector": "a", "tabId": 4})
        self.assertNotIn("protectUserTabs", self.extension.last("click"))

    # (c) Explicitly authorized navigation.
    def test_confirm_token_authorizes_one_navigation(self):
        body = {"url": "https://a.example/", "tabId": 4}
        token = self.refuse("/api/navigate", body)["state"]["confirmToken"]
        # Bound to this exact navigation.
        self.refuse("/api/navigate", {"url": "https://b.example/", "tabId": 4, "confirmToken": token})
        self.refuse("/api/navigate", {"url": "https://a.example/", "tabId": 5, "confirmToken": token})
        token = self.refuse("/api/navigate", body)["state"]["confirmToken"]
        self.assertEqual(self.call("/api/navigate", {**body, "confirmToken": token}), {"ok": True})
        self.assertIs(self.extension.last("navigate")["userTabApproved"], True)
        again = self.refuse("/api/navigate", {**body, "confirmToken": token})
        self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertNotIn("userTabApproved", self.extension.last("navigate"))

    def test_token_for_the_active_tab_binds_its_tab_id(self):
        result = self.refuse("/api/navigate", {"url": "https://a.example/"})
        token = result["state"]["confirmToken"]
        self.assertIn(f"tab_id={ACTIVE_TAB}", result["hint"])
        body = {"url": "https://a.example/", "tabId": ACTIVE_TAB, "confirmToken": token}
        self.assertEqual(self.call("/api/navigate", body), {"ok": True})

    def test_callers_cannot_approve_themselves(self):
        self.refuse("/api/navigate", {"url": "https://a.example/", "tabId": 4, "userTabApproved": True})
        self.assertNotIn("userTabApproved", self.extension.last("navigate"))

    def test_without_protection_user_tab_navigation_is_unchanged(self):
        body = {"url": "https://a.example/", "tabId": 4, "protectUserTabs": True}
        self.assertEqual(self.call("/api/navigate", body, protect=False), {"ok": True})
        self.assertNotIn("protectUserTabs", self.extension.last("navigate"))
        self.assertEqual(self.call("/api/back", {"tabId": 4}, protect=False), {"ok": True})


class ServerProtectUserTabsTests(unittest.TestCase):
    def test_mcp_forwards_protection_on_every_bridge_request(self):
        for protect in (True, False):
            with patch.object(server, "PROTECT_USER_TABS", protect), \
                    patch.object(server.httpx, "request") as request:
                request.return_value.status_code = 200
                request.return_value.json.return_value = {"ok": True}
                server._post("/api/navigate", {"url": "https://example.com"})
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(headers.get(task_modes.PROTECT_USER_TABS_HEADER), "1" if protect else None)


if __name__ == "__main__":
    unittest.main()
