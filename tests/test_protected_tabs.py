"""The navigation hold is gone (#398): the bridge sends navigations on as they are, with no hold fields.

The extension admits them by tab permissions (user_tab_read_only, see test_user_tabs_read_only); the
bridge no longer turns a navigation refusal into a user_tab approval. Closing a user tab keeps its
user_tab approval for a v0.7.1 extension (test_user_tabs).
"""
import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, task_modes

ACTIVE_TAB = 4


class ExtensionSocket:
    """The extension's WebSocket, as the bridge writes to it: records each command and runs it.
    describeTarget reports the active tab for commands without one."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        command = json.loads(message)
        self.sent.append(command)
        reply = ({"tabId": command.get("tabId") or ACTIVE_TAB, "url": "https://user.example/"}
                 if command["action"] == "describeTarget" else {"ok": True})
        bridge.pending_commands[command["id"]]["future"].set_result(reply)

    def last(self, action):
        return [command for command in self.sent if command["action"] == action][-1]


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.extension = ExtensionSocket()
        patches = [
            patch.object(bridge, "extension_ws", self.extension),
            patch.object(bridge, "_ws_send_lock", None),
            patch.dict(bridge.pending_commands, clear=True),
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": False,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
            patch.dict(bridge._unknown_outcomes, clear=True),
            patch.object(bridge, "_audit_log", []),
            patch.object(bridge, "_global_command_lock", None),
            patch.object(bridge, "_tab_command_locks", {}),
            patch.object(bridge, "_scheduler_gate", None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, path, body, user_tabs=None):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = {} if user_tabs is None else {task_modes.USER_TABS_HEADER: user_tabs}
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler.authorized = Mock(return_value=True)
        handler.do_POST()
        return handler.send_json.call_args.args

    def test_navigations_reach_the_extension_without_hold_fields(self):
        for user_tabs in (None, "read-only", "writable"):
            for path, action, body in (("/api/navigate", "navigate", {"url": "https://a.example/"}),
                                       ("/api/back", "goBack", {}), ("/api/forward", "goForward", {})):
                with self.subTest(user_tabs=user_tabs, action=action):
                    sent = {**body, "tabId": ACTIVE_TAB, "protectUserTabs": True, "holdViewedTab": True,
                            "viewedTabApproved": True, "userTabApproved": True}
                    self.assertEqual(self.call(path, sent, user_tabs), (200, {"ok": True}))
                    received = self.extension.last(action)
                    self.assertEqual(received["tabId"], ACTIVE_TAB)
                    for field in ("protectUserTabs", "holdViewedTab", "viewedTabApproved", "userTabApproved",
                                  "confirmToken"):
                        self.assertNotIn(field, received)
                    self.assertIs(received.get("userTabsWritable"), True if user_tabs == "writable" else None)
        self.assertEqual(bridge._confirm_tokens, {}, "no navigation approval is offered")

    def test_an_ask_before_act_approval_pins_the_navigation_to_the_approved_tab(self):
        # Without tab_id the hold resolves the active tab; the approved repeat goes to that tab only.
        bridge._policy["ask"] = True
        for body in ({"url": "https://shop.example/checkout", "tabId": ACTIVE_TAB},
                     {"url": "https://shop.example/checkout"}):
            with self.subTest(body=body):
                status, held = self.call("/api/navigate", body)
                self.assertEqual((status, held["code"], held["state"]["detail"], held["state"]["tabId"]),
                                 (403, "needs_user", "confirmation_required", ACTIVE_TAB), held)
                status, result = self.call("/api/navigate", {**body, "confirmToken": held["state"]["confirmToken"]})
                self.assertEqual((status, result), (200, {"ok": True}))
                sent = self.extension.last("navigate")
                self.assertEqual(sent["tabId"], ACTIVE_TAB)
                self.assertNotIn("userTabApproved", sent)


if __name__ == "__main__":
    unittest.main()
