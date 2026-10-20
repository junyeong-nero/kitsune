"""Action error contract: stable codes, retry safety, and no blind repeats of writes."""
import asyncio
import json
import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

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


class McpErrorResultTests(unittest.TestCase):
    """Failures reach MCP clients as isError results through FastMCP serialization."""

    def setUp(self):
        # The lowlevel server logs every request at INFO.
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    def call(self, name, arguments, reply=None, mcp=server.mcp, **request):
        if reply is not None:
            request["return_value"] = Mock(json=Mock(return_value=reply))

        async def run():
            async with create_connected_server_and_client_session(mcp._mcp_server) as session:
                return await session.call_tool(name, arguments)

        with patch.object(server.httpx, "request", **request):
            return asyncio.run(run())

    def assertMcpResult(self, result, payload, is_error):
        self.assertIs(result.isError, is_error)
        self.assertEqual(len(result.content), 1)
        self.assertEqual(json.loads(result.content[0].text), payload)

    def registered(self, *functions):
        mcp = FastMCP("test")
        for function in functions:
            mcp.tool()(server._mcp_errors(function))
        return mcp

    def test_stale_and_missing_refs_are_errors_with_full_details(self):
        stale = {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
                 "hint": "A newer snapshot replaced this ref; use refs from the latest browser_snapshot.",
                 "state": {"reason": "snapshot_replaced", "ref": "s1e0", "tabId": 7,
                           "url": "https://example.com/é", "status": "complete"}}
        missing = {"error": "Element not found", "code": "not_found", "retryable": True,
                   "hint": "No element matches; take a new browser_snapshot."}
        for name, arguments, reply in (("browser_click", {"ref": "s1e0", "tab_id": 7}, stale),
                                       ("browser_type", {"ref": "s1e0", "text": "hi"}, stale),
                                       ("browser_snapshot", {"ref": "s1e0"}, stale),
                                       ("browser_click", {"selector": "#gone"}, missing)):
            with self.subTest(name=name, code=reply["code"]):
                self.assertMcpResult(self.call(name, arguments, reply), reply, True)

    def test_extension_disconnected_is_an_error(self):
        with patch.object(bridge, "extension_ws", None):
            disconnected = asyncio.run(bridge._send_to_extension_raw("click", {}))
        self.assertEqual(disconnected["code"], "extension_disconnected")
        self.assertMcpResult(self.call("browser_click", {"ref": "s1e0"}, disconnected), disconnected, True)

    def test_bridge_unavailable_is_an_error(self):
        with patch.object(server, "_ensure_bridge", return_value="Cannot start Kitsune bridge: denied"):
            result = self.call("browser_snapshot", {}, side_effect=httpx.ConnectError("refused"))
        self.assertMcpResult(result, server._bridge_unavailable("Cannot start Kitsune bridge: denied"), True)

    def test_server_validation_and_ok_false_are_errors(self):
        self.assertMcpResult(self.call("browser_tabs", {"action": "close"}),
                             {"error": "tab_id is required for action=close"}, True)
        timed_out = {"ok": False, "timedOut": True, "tabId": 9, "url": "https://example.com/"}
        self.assertMcpResult(self.call("browser_tabs", {"action": "new", "wait": True}, timed_out),
                             timed_out, True)

    def test_successes_and_warnings_are_not_errors(self):
        warned = {"ok": True, "tag": "input", "warning": {"code": "hidden_or_covered",
                  "state": {"reason": "not_rendered"}, "hint": "prefer its label"}}
        self.assertMcpResult(self.call("browser_click", {"ref": "s1e0"}, warned), warned, False)
        # The click happened; only the expected navigation was not observed.
        clicked = {"ok": True, "clicked": True, "navigation": {"ok": False, "timedOut": True}}
        self.assertMcpResult(self.call("browser_click_and_wait_navigation", {"ref": "s1e0"}, clicked,
                                       mcp=self.registered(server.browser_click_and_wait_navigation)),
                             clicked, False)
        self.assertMcpResult(self.call("browser_status", {}, {"status": "running", "extension_connected": False}),
                             {"status": "running", "extension_connected": False}, False)

    def test_containers_report_per_command_failures_without_failing_the_call(self):
        mcp = self.registered(server.browser_batch, server.browser_parallel, server.browser_retry)
        partial = {"results": [{"ok": True, "snapshot": "- button \"Pay\" [ref=s2e0]"},
                               {"error": "stale ref, call browser_snapshot again", "code": "stale_ref",
                                "retryable": True}]}
        self.assertMcpResult(self.call("browser_batch", {"commands": [{"action": "click"}] * 2}, partial,
                                       mcp=mcp), partial, False)
        sequences = {"results": [[partial["results"]]]}
        self.assertMcpResult(self.call("browser_parallel", {"sequences": [[{"action": "click"}] * 2]},
                                       sequences, mcp=mcp), sequences, False)
        unavailable = server._bridge_unavailable("Kitsune bridge is not reachable")
        self.assertMcpResult(self.call("browser_batch", {"commands": []}, unavailable, mcp=mcp), unavailable, True)
        exhausted = {"ok": False, "attempts": 1, "reason": "not retryable",
                     "result": {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}}
        self.assertMcpResult(self.call("browser_retry", {"command": {"action": "submitForm"}}, exhausted,
                                       mcp=mcp), exhausted, True)

    def test_python_callers_still_get_plain_results(self):
        error = {"error": "Element not found", "code": "not_found", "retryable": True}
        with patch.object(server.httpx, "request", return_value=Mock(json=Mock(return_value=error))):
            self.assertEqual(server.browser_click(selector="#gone"), error)


if __name__ == "__main__":
    unittest.main()
