"""#396: late native preflight after JS preparation; no real OS APIs are invoked."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge


class HandoffNativePreflightTests(unittest.TestCase):
    def test_native_click_drag_and_keyboard_refuse_after_preparation(self):
        async def run(kind):
            calls = []
            refusal = {"error": "taken back", "code": "user_tab_read_only", "retryable": False}
            async def send(action, params):
                calls.append((action, params))
                return refusal if action == "checkTabWrite" else {
                    "tabId": 5, "screenX": 10, "screenY": 20, "viewportX": 10, "viewportY": 20}
            token = bridge._handoff_identity.set({"sessionId": "internal-session"})
            try:
                with patch.object(bridge, "send_to_extension", side_effect=send), \
                        patch.object(bridge, "_mac_click_backend", return_value="quartz"), \
                        patch.object(bridge, "_mac_drag_unavailable", return_value=None), \
                        patch.object(bridge, "_mac_keyboard_unavailable", return_value=None), \
                        patch.object(bridge, "_focus_check_click") as click, \
                        patch.object(bridge, "_focus_check_drag") as drag:
                    params = {"tabId": 5, "selector": "#field", "to": "#end"}
                    if kind == "click":
                        result = await bridge.trusted_click(params)
                    elif kind == "drag":
                        result = await bridge.trusted_drag(params)
                    else:
                        result = await bridge._trusted_keys(params, ["x"], Mock())
                    self.assertEqual(result, refusal)
                    self.assertEqual(calls[-1], ("checkTabWrite", {"tabId": 5, "writeAction": "trustedClick"}))
                    self.assertEqual([a for a, _ in calls[:-1]], ["screenPoint"] * (2 if kind == "drag" else 1))
                    click.assert_not_called()
                    drag.assert_not_called()
            finally:
                bridge._handoff_identity.reset(token)
        for kind in ("click", "drag", "keyboard"):
            with self.subTest(kind=kind):
                asyncio.run(run(kind))

    def test_preflight_is_bound_and_fails_closed_without_authorization(self):
        async def run():
            token = bridge._handoff_identity.set({"sessionId": "internal-session"})
            try:
                with patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send:
                    self.assertEqual((await bridge._handoff_native_refusal({}, {}))["code"], "user_tab_read_only")
                    send.assert_not_called()
                    for result in (None, {}, {"ok": False}):
                        send.return_value = result
                        self.assertEqual((await bridge._handoff_native_refusal({"tabId": 3}, {"tabId": 5}))["code"],
                                         "user_tab_read_only")
                        self.assertEqual(send.call_args.args[1]["tabId"], 5)
                    send.return_value = {"ok": True}
                    self.assertIsNone(await bridge._handoff_native_refusal({}, {"tabId": 5}))
            finally:
                bridge._handoff_identity.reset(token)
        asyncio.run(run())
