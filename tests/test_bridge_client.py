"""kitsune.bridge_client: the bridge connection the MCP server's tools use, without MCP code."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import ssl
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from kitsune import bridge_client, bridge_control
from kitsune.bridge_client import BridgeClient


def _run(code: str, env: dict | None = None) -> str:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("KITSUNE_")}
    return subprocess.run([sys.executable, "-c", code], env={**clean, **(env or {})},
                          capture_output=True, text=True, check=True).stdout


class BridgeClientTests(unittest.TestCase):
    def test_imports_no_mcp_code(self):
        loaded = _run("import json, sys, kitsune.bridge_client; "
                      "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'mcp')))")
        self.assertEqual(json.loads(loaded), [])

    def test_caps_have_no_default(self):
        # Requests without the caps header are not restricted to any group.
        with self.assertRaises(TypeError):
            BridgeClient()
        self.assertEqual(BridgeClient(caps={"tabs", "core"}).caps, "core,tabs")

    def test_mcp_server_client_takes_the_server_settings(self):
        code = ("import json; from kitsune import server; client = server._client; "
                "print(json.dumps([client.caps, client.mode, client.protect_user_tabs]))")
        for env, settings in (({}, ["core", "act", False]),
                              ({"KITSUNE_CAPS": "core,tabs", "KITSUNE_MODE": "draft",
                                "KITSUNE_PROTECT_USER_TABS": "1"}, ["core,tabs", "draft", True])):
            with self.subTest(env=env):
                self.assertEqual(json.loads(_run(code, env)), settings)

    def test_mcp_server_quiets_only_its_per_call_info_logs(self):
        # #298: one Rich-rendered INFO line each from the MCP server and httpx on every tool call.
        code = ("import json, logging; from unittest.mock import patch; from kitsune import server\n"
                "with patch.object(server._client, 'ensure_bridge', return_value=None), patch.object(server.mcp, 'run'):\n"
                "    server.main()\n"
                "print(json.dumps([logging.getLogger(name).getEffectiveLevel() for name in "
                "('mcp.server.lowlevel.server', 'httpx', 'mcp.server.fastmcp')]))")
        self.assertEqual(json.loads(_run(code)), [logging.WARNING, logging.WARNING, logging.INFO])


class BridgeTlsContextTests(unittest.TestCase):
    """#298: requests to the plain-HTTP bridge share one TLS context instead of building one each."""

    def setUp(self):
        body = json.dumps({"tabs": []}).encode()

        class Bridge(BaseHTTPRequestHandler):  # HTTP/1.0 and a JSON body, like the bridge
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        for patcher in (patch.object(bridge_client, "BRIDGE", url), patch.object(bridge_control, "BRIDGE", url),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": "test"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge_control.tls_context.cache_clear()
        self.addCleanup(bridge_control.tls_context.cache_clear)

    def test_requests_do_not_build_a_tls_context_per_call(self):
        client = BridgeClient(caps={"core"})
        with patch("ssl.create_default_context", wraps=ssl.create_default_context) as created:
            for _ in range(3):
                self.assertEqual(client.get("/api/tabs"), {"tabs": []})
            self.assertEqual(bridge_control.status(), {"tabs": []})
        # httpx's top-level functions built one per call (about 10 ms of CPU each); now one in all.
        self.assertEqual(created.call_count, 1)
        self.assertIsInstance(bridge_control.tls_context(), ssl.SSLContext)


if __name__ == "__main__":
    unittest.main()
