"""Issue #326: viewing counts as using.

The extension refuses writes and navigations on the tab shown in the browser window the user last
focused (user_viewing, extension side in tests/shared_browser.cjs). The bridge turns a direct
call's refusal into a confirm step like user_tab's, and a client can lift the hold with
KITSUNE_HOLD_VIEWED_TAB=0.
"""
import asyncio
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, bridge_client, errors, playwright_tools, server, task_modes, tools

VIEWED_TAB = 4
HELD_ACTIONS = {"click", "fill", "navigate", "goBack", "scroll", "executeJS"}


class FakeExtension:
    """Holds the shown tab VIEWED_TAB like background.js viewedTabError; tab 9 is the agent's."""

    def __init__(self):
        self.sent = []
        self.target = {}

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        tab_id = params.get("tabId") or VIEWED_TAB
        if action == "describeTarget":
            return {"tabId": tab_id, "url": "https://user.example/", "target": self.target}
        held = action in HELD_ACTIONS or (action == "screenPoint" and params.get("click"))
        if (held and tab_id == VIEWED_TAB and params.get("holdViewedTab") is not False
                and not params.get("viewedTabApproved") and not params.get("userTabApproved")):
            return {"error": f"The user is viewing tab {tab_id}; nothing was done", "code": "user_viewing",
                    "retryable": False, "hint": "extension hint",
                    "state": {"tabId": tab_id, "url": "https://user.example/", "windowId": 1,
                              "reason": "viewing", "browserFocused": False}}
        if action == "screenPoint":
            return {"screenX": 10, "screenY": 20, "viewportX": 1, "viewportY": 2, "title": "t"}
        return {"ok": True}

    def last(self, action):
        return [params for sent, params in self.sent if sent == action][-1]


class ViewedTabBridgeTests(unittest.TestCase):
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

    def call(self, path, body, headers=None, status=200):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = dict(headers or {})
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler.authorized = Mock(return_value=True)
        handler.do_POST()
        sent_status, result = handler.send_json.call_args.args
        self.assertEqual(sent_status, status, result)
        return result

    def refuse(self, path, body, headers=None):
        result = self.call(path, body, headers)
        self.assertEqual((result["code"], result["retryable"]), ("user_viewing", False), result)
        return result

    def test_direct_write_gets_a_token_that_approves_it_once(self):
        body = {"selector": "#go", "tabId": VIEWED_TAB}
        refused = self.refuse("/api/click", body)
        token = refused["state"]["confirmToken"]
        self.assertEqual(refused["state"]["expiresInSeconds"], bridge.CONFIRM_TOKEN_TTL)
        self.assertIn(f"tab_id={VIEWED_TAB}", refused["hint"])
        self.assertIn("browser_tabs", refused["hint"])
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))

        self.assertEqual(self.call("/api/click", {**body, "confirmToken": token}), {"ok": True})
        self.assertIs(self.extension.last("click")["viewedTabApproved"], True)
        self.assertNotIn("confirmToken", self.extension.last("click"))
        again = self.refuse("/api/click", {**body, "confirmToken": token})
        self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))
        self.assertTrue(any(entry.get("params", {}).get("reason") == "user_viewing"
                            for entry in bridge._audit_log), "the accepted approval is audited")

    def test_token_binds_the_call_and_the_refused_tab(self):
        refused = self.refuse("/api/navigate", {"url": "https://a.example/"})  # no tabId: the shown tab
        token = refused["state"]["confirmToken"]
        self.assertEqual(refused["state"]["tabId"], VIEWED_TAB)
        # Another URL, or the call without tab_id, does not carry it.
        self.refuse("/api/navigate", {"url": "https://b.example/", "tabId": VIEWED_TAB, "confirmToken": token})
        self.refuse("/api/navigate", {"url": "https://a.example/", "confirmToken": token})
        approved = {"url": "https://a.example/", "tabId": VIEWED_TAB, "confirmToken": token}
        self.assertEqual(self.call("/api/navigate", approved), {"ok": True})
        self.assertIs(self.extension.last("navigate")["viewedTabApproved"], True)

    def test_callers_cannot_release_the_hold_themselves(self):
        for fields in ({"viewedTabApproved": True}, {"holdViewedTab": False}):
            with self.subTest(fields=fields):
                self.refuse("/api/click", {"selector": "#go", "tabId": VIEWED_TAB, **fields})
                sent = self.extension.last("click")
                self.assertNotIn("viewedTabApproved", sent)
                self.assertNotIn("holdViewedTab", sent)

    def test_batch_steps_are_refused_without_a_token(self):
        result = self.call("/api/batch", {"commands": [
            {"action": "click", "selector": "#go", "tabId": VIEWED_TAB, "viewedTabApproved": True}]})
        step = result["results"][0]
        self.assertEqual(step["code"], "user_viewing")
        self.assertNotIn("confirmToken", step["state"])
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))

    def test_client_setting_lifts_the_hold(self):
        headers = {task_modes.HOLD_VIEWED_TAB_HEADER: "0"}
        self.assertEqual(self.call("/api/click", {"selector": "#go", "tabId": VIEWED_TAB}, headers), {"ok": True})
        self.assertIs(self.extension.last("click")["holdViewedTab"], False)
        self.assertEqual(self.call("/api/batch", {"commands": [
            {"action": "fill", "selector": "#q", "value": "x", "tabId": VIEWED_TAB}]}, headers)["results"][0],
            {"ok": True})
        # Without the header the bridge adds nothing.
        self.call("/api/click", {"selector": "#go", "tabId": 9})
        self.assertNotIn("holdViewedTab", self.extension.last("click"))

    def test_restricted_modes_get_no_token(self):
        self.extension.target = {"sensitive": False}  # draft fills only inspected fields
        result = self.refuse("/api/fill", {"selector": "#q", "value": "x", "tabId": VIEWED_TAB},
                             {task_modes.MODE_HEADER: "draft"})
        self.assertEqual(result["hint"], bridge.RESTRICTED_VIEWED_TAB_HINT)
        self.assertNotIn("confirmToken", result["state"])
        self.assertEqual(bridge._confirm_tokens, {})

    def test_ask_before_act_approval_covers_the_hold(self):
        # One approval of a held submit: confirmation_required's token also passes the viewed tab.
        bridge._policy["ask"] = True
        self.extension.target = {"submits": True, "text": "Pay", "tag": "button"}
        body = {"selector": "#pay", "tabId": VIEWED_TAB}
        held = self.call("/api/click", body, status=403)
        self.assertEqual(held["code"], "confirmation_required")
        self.assertEqual(self.call("/api/click", {**body, "confirmToken": held["state"]["confirmToken"]}),
                         {"ok": True})
        self.assertIs(self.extension.last("click")["viewedTabApproved"], True)

    def test_a_refusal_is_not_an_unknown_outcome(self):
        # retryable: false, but nothing ran: ask mode must not hold the repeat as unknown_outcome.
        bridge._policy["ask"] = True
        body = {"selector": "#go", "tabId": VIEWED_TAB}
        token = self.refuse("/api/click", body)["state"]["confirmToken"]
        self.assertEqual(bridge._unknown_outcomes, {})
        self.assertEqual(self.call("/api/click", {**body, "confirmToken": token}), {"ok": True})

    def test_trusted_click_screen_point_is_approved_by_the_token(self):
        # No OS input: the focus check and the click are mocked, and the real ones would fail the test.
        clicked = Mock(return_value=(False, {"backend": "test"}))
        with patch.object(bridge, "_focus_check_click", clicked), \
                patch.object(bridge, "_mac_click_backend", return_value=None), \
                patch.object(bridge, "_native_click_screen", side_effect=AssertionError("OS click")), \
                patch.object(bridge, "_focus_browser_window", side_effect=AssertionError("OS focus")):
            body = {"selector": "#go", "tabId": VIEWED_TAB}
            token = self.refuse("/api/trusted-click", body)["state"]["confirmToken"]
            clicked.assert_not_called()
            self.assertEqual(self.call("/api/trusted-click", {**body, "confirmToken": token})["ok"], True)
        self.assertIs(self.extension.last("screenPoint")["viewedTabApproved"], True)
        clicked.assert_called_once()


class ViewedTabClientTests(unittest.TestCase):
    def test_raise_for_asks_for_confirmation(self):
        refused = {"error": "The user is viewing tab 4; nothing was done", "code": "user_viewing",
                   "retryable": False, "state": {"tabId": 4, "confirmToken": "tok", "expiresInSeconds": 120}}
        with self.assertRaises(errors.ConfirmationRequired) as raised:
            errors.raise_for(refused)
        self.assertEqual(raised.exception.token, "tok")
        # read and draft get no token: a plain error.
        with self.assertRaises(errors.KitsuneError) as plain:
            errors.raise_for({**refused, "state": {"tabId": 4}})
        self.assertNotIsInstance(plain.exception, errors.ConfirmationRequired)

    def test_mcp_sends_the_setting_only_when_off(self):
        self.assertIs(server.HOLD_VIEWED_TAB, True, "on unless KITSUNE_HOLD_VIEWED_TAB=0")
        for hold in (True, False):
            with patch.object(server._client, "hold_viewed_tab", hold), \
                    patch.object(bridge_client.httpx, "request") as request:
                request.return_value.status_code = 200
                request.return_value.json.return_value = {"ok": True}
                tools._post("/api/click", {"selector": "#go", "tabId": 4})
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(headers.get(task_modes.HOLD_VIEWED_TAB_HEADER), None if hold else "0")

    def test_held_tools_take_a_confirm_token(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_scroll(tab_id=4, confirm_token="t")
            tools.browser_hover(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_focus(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_blur(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_reload_tab(tab_id=4, confirm_token="t")
            tools.browser_back(tab_id=4, confirm_token="t")
            tools.browser_forward(tab_id=4, confirm_token="t")
            playwright_tools.browser_hover(target="e1", confirm_token="t")
            playwright_tools.browser_navigate_back(tab_id=4, confirm_token="t")
        self.assertEqual([call.args[1].get("confirmToken") for call in post.call_args_list], ["t"] * 9)
        for name in ("browser_hover", "browser_navigate_back"):
            self.assertNotIn("confirm_token", playwright_tools.SCHEMAS[name]["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
