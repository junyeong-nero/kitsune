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

    def test_batch_http_deadline_covers_the_sum_of_waits(self):
        def batch(commands):
            handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
            handler.read_body = Mock(return_value={"commands": commands})
            handler.send_json = Mock()
            handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
            with patch.object(bridge, "_check_policy", return_value=None), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                              return_value={"ok": True}):
                handler.handle_batch()
            return handler.run_async.call_args.kwargs["timeout"]

        # The batch stops at its deadline; the request allows 15s more for cancelled steps to settle.
        self.assertEqual(batch([{"action": "click", "selector": "#go"}]), 300 + 15)
        # 20 sequential 30s waits outlast the old fixed 300s deadline.
        self.assertEqual(batch([{"action": "click", "selector": f"#b{i}", "wait": True,
                                 "timeout": 30000} for i in range(20)]), 615 + 15)

    def test_retry_http_deadline_covers_every_attempt(self):
        def retry(body):
            handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
            handler.read_body = Mock(return_value=body)
            handler.send_json = Mock()
            handler.run_async = Mock(return_value={"ok": True})
            handler.handle_retry()
            handler.run_async.call_args.args[0].close()
            return handler.run_async.call_args.kwargs["timeout"]

        self.assertEqual(retry({"command": {"action": "click", "selector": "#go"}}), 35)
        # Five 10s waits plus 0.5+1+2+4+8s of backoff outlast the old fixed 35s.
        self.assertEqual(retry({"command": {"action": "click", "selector": "#go", "wait": True},
                                "maxAttempts": 5, "backoffMs": 500}), 80.5)


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
            self.assertEqual(post.call_args.kwargs["timeout"], 55)

    def test_wait_and_expect_combine(self):
        # #66 waits before dispatch and #67 checks the outcome after it; both bound the request.
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_click("#go", wait=True, timeout=10000, expect={"text": "Saved", "timeout": 3000})
            post.assert_called_with("/api/click", {"selector": "#go", "wait": True, "timeout": 10000,
                                                   "expect": {"text": "Saved", "timeout": 3000}},
                                    timeout=15 + 10 + 3 + 45)
            server.browser_batch([{"action": "click", "selector": "#go", "wait": True,
                                   "expect": {"text": "Saved", "timeout": 3000}}])
            self.assertEqual(post.call_args.kwargs["timeout"], 15 + 10 + 3 + 5)

    def test_batch_timeout_covers_the_sum_of_sequential_waits(self):
        with patch.object(server, "_post", return_value={"results": []}) as post:
            # Three 10s waits run one after another; the max of them (25s) cut the batch off.
            clicks = [{"action": "click", "selector": f"#b{i}", "wait": True} for i in range(3)]
            server.browser_batch(clicks)
            self.assertEqual(post.call_args.kwargs["timeout"], 45)
            # The same applies to the older waitForElement/URL/title and newTab waits.
            server.browser_batch([
                {"action": "waitForElement", "selector": "#a", "timeout": 20000},
                {"action": "waitForUrl", "pattern": "done", "timeout": 20000},
                {"action": "newTab", "url": "https://example.com", "wait": True},
                {"action": "sleep", "ms": 5000},
                {"action": "pageText"},
            ])
            self.assertEqual(post.call_args.kwargs["timeout"], 75)
            # Parallel sequences run concurrently: the slowest sequence counts, plus the
            # waits around the parallel command.
            server.browser_batch([
                {"action": "parallel", "sequences": [clicks, clicks[:1]]},
                {"action": "waitForTitle", "pattern": "x", "timeout": 10000},
            ])
            self.assertEqual(post.call_args.kwargs["timeout"], 55)
            server.browser_parallel([clicks * 20, clicks[:1]])
            self.assertEqual(post.call_args.kwargs["timeout"], 615)
            server.browser_parallel([clicks])
            self.assertEqual(post.call_args.kwargs["timeout"], 300)

    def test_batch_timeout_counts_control_flow_bodies(self):
        click = {"action": "click", "selector": "#go", "wait": True}
        with patch.object(server, "_post", return_value={"results": []}) as post:
            for cmd, wait_s in [
                # Three attempts of a 10s wait, plus 0.5+1+2s of backoff.
                ({"action": "retry", "command": click}, 33.5),
                ({"action": "retry", "command": click, "maxAttempts": 1, "backoffMs": 0}, 10),
                ({"action": "retry", "command": click, "maxAttempts": "x", "backoffMs": -1}, 30),
                ({"action": "if", "condition": "1", "then": [click],
                  "else": [{"action": "waitForElement", "selector": "#a", "timeout": 20000}]}, 20),
                ({"action": "while", "condition": "1", "maxIterations": 4,
                  "do": [{**click, "timeout": 5000}]}, 20),
                ({"action": "while", "condition": "1", "do": [{"action": "sleep", "ms": 500}]}, 10),
                ({"action": "try", "do": [click], "catch": [{"action": "sleep", "ms": 2000}]}, 12),
                ({"action": "retry", "command": {"action": "click", "selector": "#go"}}, 3.5),
            ]:
                with self.subTest(action=cmd["action"], wait_s=wait_s):
                    server.browser_batch([cmd])
                    self.assertEqual(post.call_args.kwargs["timeout"], wait_s + 15)

    def test_retry_timeout_covers_every_attempt(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_retry({"action": "click", "selector": "#go"})
            self.assertEqual(post.call_args.kwargs["timeout"], 60)
            server.browser_retry({"action": "click", "selector": "#go", "wait": True}, max_attempts=5)
            post.assert_called_with("/api/retry", {
                "command": {"action": "click", "selector": "#go", "wait": True},
                "maxAttempts": 5, "backoffMs": 500,
            }, timeout=80.5)


if __name__ == "__main__":
    unittest.main()
