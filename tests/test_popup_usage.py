"""Minimal popup usage: one outer MCP call, authenticated reporting, connection-local state."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from websockets.asyncio.client import connect

from kitsune import server
from test_bridge_security import Bridge, _request


class MCPUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_failed_and_gateway_calls_report_once(self):
        async def run(name, arguments):
            if name == "gateway":
                return await server.mcp.call_tool("inner", arguments)
            return {"error": "failed"} if name == "failed" else {"ok": True}

        with patch.object(server.mcp, "_call_tool", side_effect=run), patch.object(server._client, "post") as report:
            for name in ("direct", "failed", "gateway"):
                report.reset_mock()
                await server.mcp.call_tool(name, {"url": "https://private.test", "secret": "never sent"})
                report.assert_called_once_with("/api/mcp-activity", {}, timeout=0.25, start_bridge=False)
            self.assertFalse(server._mcp_call_active.get())

    async def test_concurrent_calls_are_not_mistaken_for_nested_calls(self):
        with patch.object(server.mcp, "_call_tool", new=AsyncMock(return_value={"ok": True})), \
                patch.object(server._client, "post") as report:
            await asyncio.gather(*(server.mcp.call_tool("direct", {}) for _ in range(8)))
            self.assertEqual(report.call_count, 8)

    async def test_reporting_failure_does_not_change_tool_result_or_exception(self):
        with patch.object(server._client, "post", side_effect=OSError("offline")):
            with patch.object(server.mcp, "_call_tool", new=AsyncMock(return_value={"ok": True})):
                self.assertEqual(await server.mcp.call_tool("direct", {}), {"ok": True})
            with patch.object(server.mcp, "_call_tool", new=AsyncMock(side_effect=ValueError("tool failure"))):
                with self.assertRaisesRegex(ValueError, "tool failure"):
                    await server.mcp.call_tool("direct", {})
            self.assertFalse(server._mcp_call_active.get())


class PopupUsageWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.bridge = Bridge(self.home.name)
        await asyncio.to_thread(self.bridge.start)
        self.addAsyncCleanup(asyncio.to_thread, self.bridge.stop)

    async def report(self, headers=None):
        return await asyncio.to_thread(_request, self.bridge.http_port, "POST", "/api/mcp-activity",
                                       self.bridge.auth() if headers is None else headers,
                                       self.bridge.host, "{}")

    def socket(self):
        return connect(f"ws://127.0.0.1:{self.bridge.ws_port}", origin="moz-extension://popup-test")

    async def hello(self, socket):
        await socket.send(json.dumps({"type": "hello", "version": "test", "build": "dev", "popupUsage": True}))
        return json.loads(await asyncio.wait_for(socket.recv(), 5))

    async def test_authenticated_usage_is_aggregated_and_reset_on_reconnect(self):
        # No browser connection: reports do not accumulate for a later connection.
        self.assertEqual((await self.report())[0], 200)
        async with self.socket() as socket:
            self.assertEqual(await self.hello(socket), {"type": "usage", "calls": 0, "lastActivity": None})
            self.assertEqual((await self.report({}))[0], 401)
            for count, mode in enumerate(("read", "draft", "act"), 1):
                headers = {**self.bridge.auth(), "X-Kitsune-Mode": mode, "X-Kitsune-Caps": "core",
                           "X-Kitsune-Client": f"client-{count}"}
                self.assertEqual((await self.report(headers))[0], 200)
                usage = json.loads(await asyncio.wait_for(socket.recv(), 5))
                self.assertEqual(set(usage), {"type", "calls", "lastActivity"})
                self.assertEqual(usage["calls"], count)
                self.assertIsInstance(usage["lastActivity"], int)
            async with self.socket() as replacement:
                self.assertEqual(await self.hello(replacement), {"type": "usage", "calls": 0, "lastActivity": None})
                self.assertEqual((await self.report())[0], 200)
                self.assertEqual(json.loads(await asyncio.wait_for(replacement.recv(), 5))["calls"], 1)

    async def test_old_extension_does_not_receive_unknown_usage_messages(self):
        async with self.socket() as socket:
            await socket.send(json.dumps({"type": "hello", "version": "old", "build": "dev"}))
            self.assertEqual((await self.report())[0], 200)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(socket.recv(), 0.1)


if __name__ == "__main__":
    unittest.main()
