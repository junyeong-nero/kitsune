"""Action error contract: stable codes, retry safety, and no blind repeats of writes."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from kitsune import bridge, server


class SilentSocket:
    """An extension socket that accepts commands and never replies."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class BridgeTransportErrorTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("extension_ws", None), ("_ws_send_lock", None)):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pending = patch.dict(bridge.pending_commands, clear=True)
        pending.start()
        self.addCleanup(pending.stop)

    def test_not_connected_is_retryable_because_nothing_was_sent(self):
        for action in ("submitForm", "getSnapshot"):
            with self.subTest(action=action):
                result = asyncio.run(bridge._send_to_extension_raw(action, {}))
                self.assertTrue(result["error"].startswith("Kitsune extension not connected"))
                self.assertEqual(result["code"], "extension_disconnected")
                self.assertIs(result["retryable"], True)
                self.assertEqual(result["state"], {"connected": False})
                self.assertIn("browser_status", result["hint"])

    def test_timeout_after_send_is_retryable_only_for_reads(self):
        for action, retryable in (("submitForm", False), ("click", False), ("keypress", False),
                                  ("uploadCommit", False), ("getSnapshot", True), ("getTabs", True)):
            with self.subTest(action=action), patch.object(bridge, "extension_ws", SilentSocket()):
                result = asyncio.run(bridge._send_to_extension_raw(action, {}, timeout=0.01))
                self.assertEqual(result["error"], "Command timed out after 0.01s")
                self.assertEqual(result["code"], "timeout")
                self.assertIs(result["retryable"], retryable)
                self.assertEqual(result["state"], {"timeoutSeconds": 0.01})
                self.assertEqual(len(bridge.extension_ws.sent), 1)


class RetryActionTests(unittest.TestCase):
    def run_retry(self, replies, **params):
        command = {"action": "retry", "command": {"action": "submitForm", "selector": "#pay"},
                   "maxAttempts": 3, "backoffMs": 0, **params}
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_invalidate_cache_for_action"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          side_effect=replies) as send:
            return asyncio.run(bridge.run_command(command)), send

    def test_unknown_outcome_is_never_repeated(self):
        timeout = bridge._action_error("Command timed out after 30s", "timeout",
                                       retryable=False, hint="check", timeoutSeconds=30)
        result, send = self.run_retry([timeout, {"ok": True}])
        self.assertEqual(result, {"ok": False, "attempts": 1, "result": timeout, "reason": "not retryable"})
        send.assert_awaited_once_with("submitForm", {"selector": "#pay"})

    def test_not_retryable_wins_over_on_errors(self):
        timeout = {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}
        result, send = self.run_retry([timeout], onErrors=["timed out"])
        self.assertEqual(result["reason"], "not retryable")
        send.assert_awaited_once()

    def test_failures_before_dispatch_are_retried(self):
        missing = {"error": "Element not found", "code": "not_found", "retryable": True, "hint": "snapshot"}
        result, send = self.run_retry([missing, {"ok": True}])
        self.assertEqual(result, {"ok": True, "attempts": 2, "result": {"ok": True}})
        self.assertEqual(send.await_count, 2)

    def test_unclassified_errors_keep_previous_retry_behavior(self):
        result, send = self.run_retry([{"error": "boom"}] * 3)
        self.assertEqual(result, {"ok": False, "attempts": 3, "result": {"error": "boom"}})
        self.assertEqual(send.await_count, 3)


class ServerTransportErrorTests(unittest.TestCase):
    def test_unreachable_bridge_is_retryable(self):
        with patch.object(server.httpx, "request", side_effect=httpx.ConnectError("refused")), \
             patch.object(server, "_ensure_bridge", return_value=None):
            result = server._request("POST", "/api/click", json={}, timeout=1)
        self.assertTrue(result["error"].startswith("Kitsune bridge is not reachable"))
        self.assertEqual((result["code"], result["retryable"]), ("bridge_unavailable", True))

        with patch.object(server.httpx, "request", side_effect=httpx.ConnectError("refused")), \
             patch.object(server, "_ensure_bridge", return_value="Cannot start Kitsune bridge: denied"):
            result = server._request("POST", "/api/click", json={}, timeout=1)
        self.assertEqual(result["error"], "Cannot start Kitsune bridge: denied")
        self.assertEqual(result["code"], "bridge_unavailable")

    def test_timeout_is_retryable_only_for_get(self):
        for method, retryable in (("POST", False), ("GET", True)):
            with self.subTest(method=method), \
                 patch.object(server.httpx, "request", side_effect=httpx.ReadTimeout("read timed out")):
                result = server._request(method, "/api/submit-form", timeout=1)
            self.assertEqual(result["error"], "read timed out")
            self.assertEqual((result["code"], result["retryable"]), ("timeout", retryable))

    def test_bridge_error_fields_pass_through_unchanged(self):
        error = {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
                 "hint": "snapshot", "state": {"reason": "other_document", "ref": "s1e0", "tabId": 7}}
        with patch.object(server.httpx, "request", return_value=Mock(json=Mock(return_value=error))):
            self.assertEqual(server.browser_click(ref="s1e0", tab_id=7), error)


if __name__ == "__main__":
    unittest.main()
