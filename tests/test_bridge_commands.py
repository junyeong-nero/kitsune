"""Regression coverage for shared forwarding and batch sequence results."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge


class BridgeForwardingTests(unittest.TestCase):
    def handler(self, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        return handler

    def test_forward_preserves_default_timeout_and_audit(self):
        body = {"selector": "#target", "tabId": 7}
        handler = self.handler(body)
        result = {"ok": True}
        with patch.object(bridge, "_check_policy", return_value=None) as policy, \
             patch.object(bridge, "_invalidate_cache_for_action") as invalidate, \
             patch.object(bridge, "_audit") as audit, \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value=result) as send:
            handler._forward("getBounds")
            policy.assert_called_once_with("getBounds", body)
            invalidate.assert_called_once_with("getBounds")
            send.assert_awaited_once_with("getBounds", body, timeout=15)
            self.assertEqual(handler.run_async.call_args.kwargs["timeout"], 20)
            self.assertEqual(audit.call_args.args[:4], ("getBounds", body, True, None))
        handler.send_json.assert_called_once_with(200, result)

    def test_forward_denied_policy_has_no_side_effects(self):
        handler = self.handler({"value": "hello"})
        error = {"error": "Read-only mode is active"}
        with patch.object(bridge, "_check_policy", return_value=error), \
             patch.object(bridge, "_invalidate_cache_for_action") as invalidate, \
             patch.object(bridge, "_audit") as audit, \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send:
            handler._forward("fill")
            invalidate.assert_not_called()
            audit.assert_not_called()
            send.assert_not_called()
        handler.run_async.assert_not_called()
        handler.send_json.assert_called_once_with(403, error)

    def test_forward_reports_transport_and_timeout_errors(self):
        for action, body, error in [
            ("query", {}, RuntimeError("disconnected")),
            ("waitForUrl", {"timeout": "invalid"}, TypeError()),
        ]:
            with self.subTest(action=action):
                handler = self.handler(body)
                with patch.object(bridge, "_check_policy", return_value=None), \
                     patch.object(bridge, "_invalidate_cache_for_action"), \
                     patch.object(bridge, "_audit") as audit, \
                     patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                                  side_effect=error) as send:
                    handler._forward(action, default_timeout=25)
                    result = handler.send_json.call_args.args[1]
                    self.assertIn("error", result)
                    self.assertEqual(audit.call_args.args[:4], (action, body, False, result["error"]))
                    if action == "query":
                        send.assert_awaited_once_with(action, body, timeout=25)
                        self.assertEqual(handler.run_async.call_args.kwargs["timeout"], 30)
                        self.assertEqual(result, {"error": "disconnected"})
                    else:
                        send.assert_not_called()


class BatchSequenceTests(unittest.TestCase):
    def test_sequence_substitutes_all_prior_results_in_order(self):
        commands = [
            {"action": "newTab", "url": "about:blank"},
            {"action": "pageInfo", "tabId": "${$0.tabId}"},
            {"action": "click", "tabId": "${$0.tabId}", "selector": "${$1.selector}"},
        ]
        results = [{"tabId": 7}, {"selector": "#submit"}, {"ok": True}]
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_invalidate_cache_for_action"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          side_effect=results) as send:
            result = asyncio.run(bridge.run_command({"action": "sequence", "do": commands}))
        self.assertEqual(result, {"results": results})
        self.assertEqual([call.args for call in send.await_args_list], [
            ("newTab", {"url": "about:blank"}),
            ("getPageInfo", {"tabId": "7"}),
            ("click", {"tabId": "7", "selector": "#submit"}),
        ])

    def test_empty_sequence(self):
        self.assertEqual(asyncio.run(bridge.run_command({"action": "sequence"})), {"results": []})



class SignedBuildTransportTests(unittest.TestCase):
    """The signed extension has no executeJS; the bridge must not rely on it internally."""

    def test_hello_reports_extension_build_until_disconnect(self):
        seen = []

        class FakeSocket:
            request = Mock(headers={"Origin": "moz-extension://abc"})

            def __aiter__(self):
                return self.messages()

            async def messages(self):
                yield '{"type": "hello", "version": "0.2.0", "build": "signed"}'
                seen.append(dict(bridge.extension_info))

        with patch.object(bridge, "_log"):
            asyncio.run(bridge.ws_handler(FakeSocket()))
        self.assertEqual(seen, [{"version": "0.2.0", "build": "signed"}])
        self.assertEqual(bridge.extension_info, {})

    def test_status_includes_extension_build(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.send_json = Mock()
        with patch.object(bridge, "extension_info", {"version": "0.2.0", "build": "signed"}):
            handler.handle_status()
        data = handler.send_json.call_args.args[1]
        self.assertEqual((data["extension_version"], data["extension_build"]), ("0.2.0", "signed"))

    def test_sync_barrier_fails_fast_on_signed_build(self):
        reply = {"error": "needs developer build", "devBuildRequired": True}
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=reply) as send:
            result = asyncio.run(bridge._sync_barrier_async([1, 2], "true", timeout=30000))
        self.assertEqual(result["ok"], False)
        self.assertTrue(result["devBuildRequired"])
        send.assert_awaited_once()

    def test_internal_operations_use_named_actions(self):
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"screenX": 10, "screenY": 20, "viewportX": 1, "viewportY": 2,
                                        "devicePixelRatio": 2, "title": "t"}) as send, \
                patch.object(bridge, "_native_click_screen", return_value={"ok": True}):
            asyncio.run(bridge.trusted_click({"selector": "#go", "tabId": 3}))
        send.assert_awaited_once_with("screenPoint", {"selector": "#go", "x": None, "y": None, "tabId": 3})

        handler = BridgeForwardingTests.handler(None, {"tabId": 5, "force": True})
        handler._send_extension_action = Mock()
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value={"ok": True}) as send:
            handler.handle_close_tab()
        send.assert_awaited_once_with("neuterUnload", {"tabId": 5}, timeout=8)


if __name__ == "__main__":
    unittest.main()
