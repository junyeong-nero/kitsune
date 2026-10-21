"""Connection diagnosis: each connection failure has its own code and hint (issue #34).

Nothing here contacts a user bridge: bridge state is patched, the extension WebSocket and
isolated bridges listen on ephemeral ports, and HTTP calls go to ports these tests own.
"""
import asyncio
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import socket
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, mock_open, patch

import httpx
from websockets.asyncio.client import connect

from kitsune import bridge, server
import kitsune.bridge_control as control
from test_bridge_security import SYNTHETIC_TOKEN, Bridge
from test_bridge_websocket import EXTENSION_ORIGIN

SECRET = "diagnosis-secret-token-" + "y" * 24


def _ensure_bridge_with(status, port_in_use=False):
    """Run _ensure_bridge without spawning: status() is called before and while waiting."""
    with patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": "0"}), \
            patch.object(control, "status", side_effect=status), \
            patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
            patch("builtins.open", mock_open()), patch.object(server.subprocess, "Popen"), \
            patch.object(server.time, "monotonic", side_effect=[0, 0, 16]), patch.object(server.time, "sleep"), \
            patch.object(control, "port_in_use", side_effect=port_in_use) as probe:
        return server._ensure_bridge(), probe


class BridgeDiagnosisTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("extension_ws", None), ("extension_ws_generation", 0),
                            ("extension_info", {}), ("WS_PORT", bridge.EXTENSION_WS_PORT)):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def connected(self, **info):
        bridge.extension_ws = object()
        bridge.extension_ws_generation = 1
        bridge.extension_info = {"version": bridge.BRIDGE_VERSION, "build": "dev", **info}

    def test_no_extension_says_how_to_connect_it(self):
        diagnosis = bridge._connection_diagnosis()
        self.assertEqual(diagnosis["code"], "extension_disconnected")
        self.assertIn("about:addons", diagnosis["hint"])
        self.assertIn("browser_status", diagnosis["hint"])

    def test_bridge_on_another_ws_port_than_the_extension_is_a_port_conflict(self):
        bridge.WS_PORT = bridge.EXTENSION_WS_PORT + 1
        diagnosis = bridge._connection_diagnosis()
        self.assertEqual(diagnosis["code"], "port_conflict")
        self.assertEqual(diagnosis["state"], {"port": bridge.WS_PORT, "reason": "extension_ws_port"})
        self.assertIn("KITSUNE_WS_PORT", diagnosis["hint"])
        result = asyncio.run(bridge._send_to_extension_raw("click", {}))
        self.assertEqual((result["code"], result["retryable"]), ("port_conflict", True))
        self.assertEqual(result["state"], {"connected": False, "port": bridge.WS_PORT, "reason": "extension_ws_port"})
        # An extension that connected once uses this port; it is only disconnected now.
        bridge.extension_ws_generation = 1
        self.assertEqual(bridge._connection_diagnosis()["code"], "extension_disconnected")

    def test_extension_of_another_version_is_a_mismatch(self):
        for version in ("0.0.1", None):
            with self.subTest(version=version):
                self.connected(version=version)
                diagnosis = bridge._connection_diagnosis()
                self.assertEqual(diagnosis["code"], "extension_version_mismatch")
                self.assertEqual(diagnosis["state"],
                                 {"extensionVersion": version, "bridgeVersion": bridge.BRIDGE_VERSION})
                self.assertIn("about:debugging", diagnosis["hint"])

    def test_browser_state_from_the_extension(self):
        for browser, code in (({"windows": 0, "allSites": True}, "browser_window_unavailable"),
                              ({"windows": 2, "allSites": False}, "host_permission_denied"),
                              ({"windows": 2, "allSites": True}, "connected"),
                              ({"windows": None, "allSites": None}, "connected")):
            with self.subTest(browser=browser):
                self.connected(browser=browser)
                self.assertEqual(bridge._connection_diagnosis()["code"], code)
        self.connected()
        self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"}, "before the extension reports")

    def test_status_reports_the_diagnosis(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.send_json = Mock()
        handler.handle_status()
        self.assertEqual(handler.send_json.call_args.args[1]["diagnosis"]["code"], "extension_disconnected")
        self.connected(browser={"windows": 0, "allSites": True})
        handler.handle_status()
        self.assertEqual(handler.send_json.call_args.args[1]["diagnosis"],
                         {"code": "browser_window_unavailable", "hint": bridge._BROWSER_WINDOW_HINT,
                          "state": {"windows": 0}})


class ExtensionStateTests(unittest.IsolatedAsyncioTestCase):
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
        log.start()
        self.addCleanup(log.stop)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.server = await bridge.serve_extension_ws(0)
        self.addAsyncCleanup(self.close_server)
        self.uri = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def wait_until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached")

    async def unknown_action(self, version):
        async with connect(self.uri, origin=EXTENSION_ORIGIN, open_timeout=5) as client:
            await client.send(json.dumps({"type": "hello", "version": version, "build": "dev"}))
            await client.send(json.dumps({"type": "state", "windows": 0, "allSites": True}))
            await self.wait_until(lambda: "browser" in bridge.extension_info)
            self.assertEqual(bridge.extension_info["browser"], {"windows": 0, "allSites": True})

            async def answer():
                command = json.loads(await client.recv())
                await client.send(json.dumps({"id": command["id"],
                                              "result": {"error": f"Unknown action: {command['action']}"}}))

            responder = asyncio.create_task(answer())
            result = await bridge._send_to_extension_raw("getSnapshot", timeout=5)
            await responder
            return result

    async def test_state_message_and_unknown_action_of_an_older_extension(self):
        result = await self.unknown_action("0.0.1")
        self.assertEqual(result["error"], "Unknown action: getSnapshot", "error text is unchanged")
        self.assertEqual((result["code"], result["retryable"]), ("extension_version_mismatch", True))
        self.assertEqual(result["state"], {"extensionVersion": "0.0.1", "bridgeVersion": bridge.BRIDGE_VERSION})
        await self.wait_until(lambda: bridge.extension_ws is None)
        self.assertEqual(bridge.extension_info, {}, "browser state is forgotten with the connection")

    async def test_unknown_action_of_a_matching_extension_is_unchanged(self):
        self.assertEqual(await self.unknown_action(bridge.BRIDGE_VERSION), {"error": "Unknown action: getSnapshot"})


class ServerDiagnosisTests(unittest.TestCase):
    def test_rejected_token_is_a_token_mismatch_without_the_token(self):
        rejected = httpx.Response(401, json={"error": "Missing or invalid token (see token)"},
                                  request=httpx.Request("GET", server.BRIDGE))
        for env, source in (({"KITSUNE_TOKEN": SECRET}, "KITSUNE_TOKEN"), ({}, server.TOKEN_FILE)):
            with self.subTest(source=source), patch.dict(os.environ, env), \
                    patch.object(server.httpx, "request", return_value=rejected):
                if not env:
                    os.environ.pop("KITSUNE_TOKEN", None)
                result = server._request("GET", "/api/status")
                self.assertEqual((result["code"], result["retryable"]), ("token_mismatch", True))
                self.assertEqual(result["state"], {"port": server.HTTP_PORT, "tokenSource": source})
                self.assertIn("KITSUNE_TOKEN", result["hint"])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_reply_that_is_not_json_is_a_port_conflict(self):
        page = httpx.Response(200, text="<html>other app</html>", request=httpx.Request("GET", server.BRIDGE))
        with patch.object(server.httpx, "request", return_value=page):
            result = server._request("POST", "/api/click", json={})
        self.assertEqual(result["code"], "port_conflict")
        self.assertEqual(result["state"], {"port": server.HTTP_PORT, "reason": "not_kitsune"})
        self.assertIn(f"lsof -nP -iTCP:{server.HTTP_PORT}", result["hint"])

    def test_start_failures_are_classified(self):
        def http_error(code):
            request = httpx.Request("GET", server.BRIDGE)
            return httpx.HTTPStatusError("error", request=request, response=httpx.Response(code, request=request))

        for status, code in ((http_error(401), "token_mismatch"), (http_error(404), "port_conflict"),
                             (ValueError("Bridge returned invalid status"), "port_conflict")):
            with self.subTest(code=code):
                error, _ = _ensure_bridge_with([status])
                self.assertEqual(error["code"], code)

        # Nothing answers after the start: a port held by another program explains it.
        error, probe = _ensure_bridge_with([None, None, None], port_in_use=[False, True])
        self.assertEqual(error["code"], "port_conflict")
        self.assertEqual(error["state"], {"port": server.WS_PORT, "reason": "in_use"})
        self.assertIn("KITSUNE_WS_PORT", error["hint"])
        self.assertEqual(probe.call_count, 2)
        error, _ = _ensure_bridge_with([None, None, None], port_in_use=[False, False])
        self.assertEqual(error["code"], "bridge_unavailable")
        self.assertIn("kitsune-bridge start", error["hint"])

    def test_browser_status_always_has_a_diagnosis(self):
        unavailable = server._bridge_unavailable("Kitsune bridge is not reachable")
        with patch.object(server, "_get", return_value=dict(unavailable)):
            result = server.browser_status()
        self.assertEqual(result["diagnosis"], {"code": "bridge_unavailable", "hint": unavailable["hint"]})
        running = {"status": "running", "diagnosis": {"code": "connected"}}
        with patch.object(server, "_get", return_value=dict(running)):
            self.assertEqual(server.browser_status(), running)


class PortProbeTests(unittest.TestCase):
    def test_port_in_use_binds_without_connecting(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(control.port_in_use(port))
        self.assertFalse(control.port_in_use(port))


class _NotKitsune(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html>another program</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _FailingBridge(BaseHTTPRequestHandler):
    """Fails like the bridge's own BaseHTTPRequestHandler: an HTML error page, not JSON."""
    def do_POST(self):
        self.send_error(500)

    def log_message(self, *args):
        pass


class IsolatedBridgeDiagnosisTests(unittest.TestCase):
    """The MCP server against real HTTP listeners on ports these tests own."""

    def setUp(self):
        logger = logging.getLogger("httpx")  # logs every request at INFO
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    def against(self, port):
        return patch.object(server, "BRIDGE", f"http://127.0.0.1:{port}"), patch.object(server, "HTTP_PORT", port)

    def test_isolated_bridge_reports_token_mismatch_and_its_extension_port(self):
        with tempfile.TemporaryDirectory() as home:
            daemon = Bridge(home).start()
            self.addCleanup(daemon.stop)
            # Its ephemeral WebSocket port is not the extension's, and no extension ever connected.
            self.assertEqual(daemon.status()["diagnosis"]["state"],
                             {"port": daemon.ws_port, "reason": "extension_ws_port"})
            bridge_url, http_port = self.against(daemon.http_port)
            with bridge_url, http_port, patch.dict(os.environ, {"KITSUNE_TOKEN": SECRET}), \
                    patch.object(server, "_ensure_bridge") as ensure:
                result = server.browser_status()
                ensure.assert_not_called()
            self.assertEqual(result["diagnosis"]["code"], "token_mismatch")
            self.assertEqual(result["state"], {"port": daemon.http_port, "tokenSource": "KITSUNE_TOKEN"})
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(result))
            with bridge_url, http_port, patch.dict(os.environ, {"KITSUNE_TOKEN": SYNTHETIC_TOKEN}):
                self.assertEqual(server.browser_status()["diagnosis"]["code"], "port_conflict")

    def test_another_program_on_the_http_port_is_a_port_conflict(self):
        other = HTTPServer(("127.0.0.1", 0), _NotKitsune)
        Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        port = other.server_address[1]
        bridge_url, http_port = self.against(port)
        with bridge_url, http_port, patch.object(server, "_ensure_bridge") as ensure:
            result = server.browser_status()
            ensure.assert_not_called()
        self.assertEqual(result["diagnosis"]["code"], "port_conflict")
        self.assertEqual(result["state"], {"port": port, "reason": "not_kitsune"})

    def test_bridge_error_without_json_keeps_its_http_status(self):
        failing = HTTPServer(("127.0.0.1", 0), _FailingBridge)
        Thread(target=failing.serve_forever, daemon=True).start()
        self.addCleanup(failing.server_close)
        self.addCleanup(failing.shutdown)
        bridge_url, http_port = self.against(failing.server_address[1])
        with bridge_url, http_port, patch.object(server, "_ensure_bridge") as ensure:
            result = server._request("POST", "/api/click", json={})
            ensure.assert_not_called()
        self.assertEqual(result["error"], "Kitsune bridge returned HTTP 500 without a JSON body")
        self.assertEqual(result["state"]["httpStatus"], 500)
        self.assertNotIn("code", result)


if __name__ == "__main__":
    unittest.main()
