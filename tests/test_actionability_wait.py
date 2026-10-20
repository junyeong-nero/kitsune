"""Transport timeouts and tool surface for the opt-in actionability wait (issue #66)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge, server


class BridgeActionWaitTests(unittest.TestCase):
    def send(self, route, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_audit"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True}) as send:
            route(handler)()
        return send, handler

    def test_immediate_mode_keeps_default_timeouts(self):
        for route, action, expected in [
            (lambda h: h.handle_click, "click", 35),
            (lambda h: h.handle_type, "type", 35),
            (lambda h: lambda: h._forward("checkBox"), "checkBox", 15),
        ]:
            with self.subTest(action=action):
                # A timeout without wait is ignored, as before.
                body = {"selector": "#go", "timeout": 60000}
                send, handler = self.send(route, body)
                send.assert_awaited_once_with(action, body, timeout=expected)
                self.assertEqual(handler.run_async.call_args.kwargs["timeout"], expected + 5)

    def test_wait_extends_transport_timeout_by_the_wait_bound(self):
        for route, action, body, expected in [
            (lambda h: h.handle_click, "click", {"selector": "#go", "wait": True}, 45),
            (lambda h: h.handle_hover, "hover", {"selector": "#go", "wait": True, "timeout": 30000}, 65),
            (lambda h: h.handle_fill, "fill", {"selector": "#go", "wait": True, "timeout": 2000}, 37),
            (lambda h: lambda: h._forward("drag"), "drag",
             {"from": "#a", "to": "#b", "wait": True, "timeout": 20000}, 35),
            (lambda h: lambda: h._forward("keypress"), "keypress",
             {"selector": "#q", "key": "Enter", "wait": True}, 25),
        ]:
            with self.subTest(action=action):
                send, handler = self.send(route, body)
                send.assert_awaited_once_with(action, body, timeout=expected)
                self.assertEqual(handler.run_async.call_args.kwargs["timeout"], expected + 5)

    def test_wait_does_not_extend_other_actions(self):
        send, _ = self.send(lambda h: lambda: h._forward("submitForm"),
                            {"selector": "#f", "wait": True, "timeout": 60000})
        self.assertEqual(send.await_args.kwargs, {"timeout": 15})

    def test_batch_action_wait_extends_timeout(self):
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True}) as send:
            for cmd in [
                {"action": "click", "selector": "#go", "wait": True, "timeout": 20000},
                {"action": "selectOption", "selector": "#c", "value": "x", "wait": True},
                {"action": "click", "selector": "#go"},
                {"action": "click", "selector": "#go", "timeout": 20000},
            ]:
                asyncio.run(bridge.run_command(cmd))
        self.assertEqual([c.kwargs for c in send.await_args_list],
                         [{"timeout": 50}, {"timeout": 40}, {}, {}])


class ToolActionWaitTests(unittest.TestCase):
    def test_tools_are_immediate_by_default(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_click("#go")
            post.assert_called_with("/api/click", {"selector": "#go"})
            server.browser_check(ref="s1e2")
            post.assert_called_with("/api/check", {"selector": "", "checked": True, "ref": "s1e2"})

    def test_tools_forward_wait_and_extend_transport_timeout(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            for call, endpoint, body in [
                (lambda: server.browser_click("#go", wait=True), "click", {"selector": "#go"}),
                (lambda: server.browser_hover(ref="s1e2", wait=True), "hover",
                 {"selector": "", "ref": "s1e2"}),
                (lambda: server.browser_double_click("#go", wait=True), "double-click", {"selector": "#go"}),
                (lambda: server.browser_type("#q", "hi", "fill", wait=True), "fill",
                 {"selector": "#q", "value": "hi"}),
                (lambda: server.browser_type("#q", "hi", wait=True), "type",
                 {"selector": "#q", "text": "hi", "clear": False}),
                (lambda: server.browser_select_option("#c", "x", wait=True), "select-option",
                 {"selector": "#c", "value": "x", "byText": False}),
                (lambda: server.browser_check("#a", wait=True), "check", {"selector": "#a", "checked": True}),
                (lambda: server.browser_keypress("Enter", "#q", wait=True), "keypress",
                 {"key": "Enter", "selector": "#q"}),
                (lambda: server.browser_drag("#a", "#b", wait=True), "drag",
                 {"from": "#a", "to": "#b", "toRef": None}),
            ]:
                with self.subTest(endpoint=endpoint):
                    call()
                    post.assert_called_with(f"/api/{endpoint}", {**body, "wait": True, "timeout": 10000},
                                            timeout=25)
            server.browser_click("#go", tab_id=3, wait=True, timeout=30000)
            post.assert_called_with("/api/click", {"selector": "#go", "tabId": 3, "wait": True,
                                                   "timeout": 30000}, timeout=45)
            post.reset_mock()
            self.assertIn("error", server.browser_click("#go", wait=True, timeout=0))
            post.assert_not_called()

    def test_batch_action_wait_extends_timeout(self):
        with patch.object(server, "_post", return_value={"results": []}) as post:
            server.browser_batch([{"action": "click", "selector": "#go"}])
            self.assertEqual(post.call_args.kwargs["timeout"], 15)
            server.browser_batch([{"action": "click", "selector": "#go", "timeout": 60000}])
            self.assertEqual(post.call_args.kwargs["timeout"], 15)
            server.browser_batch([{"action": "checkBox", "selector": "#a", "wait": True, "timeout": 30000},
                                  {"action": "hover", "selector": "#go", "wait": True}])
            self.assertEqual(post.call_args.kwargs["timeout"], 45)

    def test_server_and_bridge_cover_the_same_actions(self):
        self.assertEqual(set(server.ACTIONABILITY_WAIT_ACTIONS), bridge.ACTIONABILITY_WAIT_ACTIONS)


if __name__ == "__main__":
    unittest.main()
