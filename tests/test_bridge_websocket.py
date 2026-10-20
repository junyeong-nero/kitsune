"""Extension WebSocket server over real sockets on an ephemeral 127.0.0.1 port."""
import asyncio
import json
import logging
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from kitsune import bridge

EXTENSION_ORIGIN = "moz-extension://0b5e3f9c-test"


class ExtensionWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name, value in (("extension_ws", None), ("extension_ws_generation", 0),
                            ("extension_info", {}), ("_ws_send_lock", None)):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pending = patch.dict(bridge.pending_commands, clear=True)
        pending.start()
        self.addCleanup(pending.stop)
        log = patch.object(bridge, "_log")
        self.log = log.start()
        self.addCleanup(log.stop)
        # websockets < 17 logs every rejected handshake as an error with a traceback.
        library_logs = patch.object(logging.getLogger("websockets.server"), "disabled", True)
        library_logs.start()
        self.addCleanup(library_logs.stop)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

        self.server = await bridge.serve_extension_ws(0)
        self.addAsyncCleanup(self.close_server)
        port = self.server.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{port}"

    async def close_server(self):
        self.server.close()
        await self.server.wait_closed()

    def connect(self, origin=EXTENSION_ORIGIN):
        return connect(self.uri, origin=origin, open_timeout=5)

    async def wait_until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached")

    def server_connections(self):
        return list(self.server.connections)

    async def test_uses_supported_asyncio_server_api(self):
        self.assertEqual(bridge.ws_serve.__module__, "websockets.asyncio.server")

    async def test_extension_connects_and_answers_commands(self):
        async with self.connect() as client:
            await client.send(json.dumps({"type": "hello", "version": "0.2.1", "build": "dev"}))
            await self.wait_until(lambda: bridge.extension_info)
            self.assertIs(bridge.extension_ws, self.server_connections()[0])
            self.assertEqual(bridge.extension_ws_generation, 1)
            self.assertEqual(bridge.extension_info, {"version": "0.2.1", "build": "dev"})

            async def answer():
                command = json.loads(await client.recv())
                self.assertEqual(command["action"], "getTabs")
                await client.send(json.dumps({"id": command["id"], "result": {"tabs": []}}))

            responder = asyncio.create_task(answer())
            result = await bridge._send_to_extension_raw("getTabs", timeout=5)
            await responder
            self.assertEqual(result, {"tabs": []})

        await self.wait_until(lambda: bridge.extension_ws is None)
        self.assertEqual(bridge.extension_info, {})

    async def test_reconnect_replaces_previous_connection(self):
        async with self.connect() as first:
            await self.wait_until(lambda: bridge.extension_ws is not None)
            first_server_side = bridge.extension_ws
            async with self.connect() as second:
                with self.assertRaises(ConnectionClosed) as closed:
                    await asyncio.wait_for(first.recv(), 5)
                self.assertEqual(closed.exception.rcvd.code, 1012)
                await self.wait_until(lambda: len(self.server_connections()) == 1)
                self.assertIsNot(bridge.extension_ws, first_server_side)
                self.assertIs(bridge.extension_ws, self.server_connections()[0])
                self.assertEqual(bridge.extension_ws_generation, 2)
                await second.ping()

        await self.wait_until(lambda: bridge.extension_ws is None)
        async with self.connect():
            await self.wait_until(lambda: bridge.extension_ws is not None)
            self.assertEqual(bridge.extension_ws_generation, 3)

    async def test_rejected_origins_do_not_replace_extension(self):
        async with self.connect() as extension:
            await self.wait_until(lambda: bridge.extension_ws is not None)
            active = bridge.extension_ws
            for origin in ("https://evil.example", "http://moz-extension.example", None):
                with self.subTest(origin=origin):
                    with self.assertRaises(InvalidStatus) as rejected:
                        async with self.connect(origin=origin):
                            pass
                    self.assertEqual(rejected.exception.response.status_code, 403)
                    self.log.assert_called_with(
                        f"⛔ Rejected WebSocket connection from origin {origin or '(none)'!r}")
            self.assertIs(bridge.extension_ws, active)
            self.assertEqual(bridge.extension_ws_generation, 1)
            await extension.ping()

    async def test_shutdown_closes_extension_and_fails_pending_commands(self):
        async with self.connect() as client:
            await self.wait_until(lambda: bridge.extension_ws is not None)
            command = asyncio.create_task(bridge._send_to_extension_raw("getTabs", timeout=5))
            json.loads(await client.recv())

            await self.close_server()
            self.assertEqual(await command, {"error": "Extension disconnected"})
            with self.assertRaises(ConnectionClosed) as closed:
                await asyncio.wait_for(client.recv(), 5)
            self.assertEqual(closed.exception.rcvd.code, 1001)
        self.assertIsNone(bridge.extension_ws)
        self.assertEqual(bridge.pending_commands, {})
        with self.assertRaises(OSError):
            async with self.connect():
                pass


if __name__ == "__main__":
    unittest.main()
