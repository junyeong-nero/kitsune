"""Issue #139: closing a tab the agent did not open needs the user's approval (user_tab + confirm token)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge


class FakeExtension:
    """Closes only owned tabs unless the bridge approved the close, like background.js closeTab."""

    def __init__(self, owned=(9,)):
        self.owned = set(owned)
        self.sent = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        if action == "describeTarget":
            return {"tabId": params.get("tabId"), "url": "https://user.example/"}
        if action == "closeWindow" and not params.get("userTabApproved"):
            return {"error": "Window 2 has tabs the agent did not open, so it was not closed", "code": "user_tab",
                    "retryable": False, "state": {"windowId": params["windowId"], "tabIds": [4]}}
        if action == "closeTab" and params["tabId"] not in self.owned and not params.get("userTabApproved"):
            return {"error": f"Tab {params['tabId']} was not opened by the agent, so it was not closed",
                    "code": "user_tab", "retryable": False, "hint": "extension hint",
                    "state": {"tabId": params["tabId"], "url": "https://user.example/"}}
        return {"ok": True}

    def closes(self):
        return [params for action, params in self.sent if action == "closeTab"]


class UserTabCloseTests(unittest.TestCase):
    ask = False

    def setUp(self):
        self.extension = FakeExtension()
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": self.ask,
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

    def close(self, body, action="closeTab"):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        if action == "closeTab":
            handler.handle_close_tab()
        else:
            handler._with_user_tab_approval(action)
        status, result = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        return result

    def refuse(self, body, action="closeTab"):
        result = self.close(body, action)
        self.assertEqual((result["code"], result["state"]["detail"], result["retryable"]), ("needs_user", "user_tab", False), result)
        self.assertEqual(result["state"]["expiresInSeconds"], bridge.CONFIRM_TOKEN_TTL)
        self.assertIn("ask the user", result["hint"])
        return result

    def test_owned_tab_closes_without_approval(self):
        self.assertEqual(self.close({"tabId": 9}), {"ok": True})
        self.assertEqual(self.extension.closes(), [{"tabId": 9}])

    def test_user_tab_closes_only_with_its_confirm_token(self):
        token = self.refuse({"tabId": 4, "force": True})["state"]["confirmToken"]
        self.assertEqual(self.extension.closes(), [{"tabId": 4, "force": True}])
        # The token is bound to this exact close: another tab or other options do not take it.
        self.refuse({"tabId": 5, "force": True, "confirmToken": token})
        self.refuse({"tabId": 4, "confirmToken": token})
        self.assertEqual(self.close({"tabId": 4, "force": True, "confirmToken": token}), {"ok": True})
        self.assertEqual(self.extension.closes()[-1], {"tabId": 4, "force": True, "userTabApproved": True})
        # It works once.
        again = self.refuse({"tabId": 4, "force": True, "confirmToken": token})
        self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertNotIn("userTabApproved", self.extension.closes()[-1])
        audit = [entry for entry in bridge._audit_log if entry["params"].get("confirmation")]
        self.assertEqual([(e["action"], e["params"]["reason"]) for e in audit], [("closeTab", "user_tab")])

    def test_window_with_user_tabs_closes_only_with_its_confirm_token(self):
        token = self.refuse({"windowId": 2}, "closeWindow")["state"]["confirmToken"]
        # A window token does not close a tab, nor a tab token a window.
        self.refuse({"tabId": 4, "confirmToken": token})
        self.assertEqual(self.close({"windowId": 2, "confirmToken": token}, "closeWindow"), {"ok": True})
        self.assertEqual(self.extension.sent[-1], ("closeWindow", {"windowId": 2, "userTabApproved": True}))

    def test_expired_token_is_rejected(self):
        token = self.refuse({"tabId": 4})["state"]["confirmToken"]
        bridge._confirm_tokens[token]["expires"] -= bridge.CONFIRM_TOKEN_TTL + 1
        self.assertEqual(self.refuse({"tabId": 4, "confirmToken": token})["state"]["tokenRejected"], "expired")

    def test_callers_cannot_approve_themselves(self):
        self.refuse({"tabId": 4, "userTabApproved": True})
        self.assertNotIn("userTabApproved", self.extension.closes()[-1])
        # Batch, workflow and parallel steps never approve, and get no token.
        result = asyncio.run(bridge.run_command({"action": "closeTab", "tabId": 4, "userTabApproved": True}))
        self.assertEqual(result["state"]["detail"], "user_tab")
        self.assertNotIn("confirmToken", result["state"])
        self.assertNotIn("userTabApproved", self.extension.closes()[-1])

    def test_refusal_is_a_failure_not_an_unknown_outcome(self):
        refusal = asyncio.run(bridge.send_to_extension("closeTab", {"tabId": 4}))
        self.assertEqual(bridge._result_status(refusal), "failed")
        self.assertEqual(bridge._unknown_outcomes, {})


class UserTabCloseAskModeTests(UserTabCloseTests):
    """Ask mode does not hold a close, and does not swallow the user_tab token."""
    ask = True


class TrustedClickRestoreTests(unittest.TestCase):
    def test_tab_shown_for_the_click_is_hidden_again(self):
        point = {"screenX": 10, "screenY": 20, "viewportX": 1, "viewportY": 2, "title": "t",
                 "tabId": 3, "previousTabId": 1}
        order = []
        send = AsyncMock(side_effect=lambda action, params, **kw: order.append(action) or
                         (point if action == "screenPoint" else {"ok": True, "restored": True}))
        with patch.object(bridge, "send_to_extension", send), \
                patch.object(bridge, "TRUSTED_CLICK_RESTORE_DELAY", 0), \
                patch.object(bridge, "_browser_not_at_point", return_value=None), \
                patch.object(bridge, "_native_click_screen", side_effect=lambda x, y, *options: order.append("click") or
                             {"ok": True, "backend": "test"}):
            result = asyncio.run(bridge.trusted_click({"selector": "#go", "tabId": 3}))
        self.assertIs(result["ok"], True)
        self.assertEqual(order, ["screenPoint", "click", "restoreActiveTab"])
        send.assert_awaited_with("restoreActiveTab", {"tabId": 1, "from": 3})

    def test_user_active_refusal_clicks_nothing(self):
        refusal = {"error": "The user is using tab 1; nothing was done", "code": "user_active", "retryable": True}
        with patch.object(bridge, "send_to_extension", AsyncMock(return_value=refusal)) as send, \
                patch.object(bridge, "_native_click_screen") as click:
            self.assertEqual(asyncio.run(bridge.trusted_click({"selector": "#go", "tabId": 3})), refusal)
        click.assert_not_called()
        send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
