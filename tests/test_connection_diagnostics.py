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
from pathlib import Path
import socket
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import Mock, mock_open, patch

import httpx
from websockets.asyncio.client import connect

from kitsune import bridge, server
import kitsune.bridge_control as control
from kitsune.version import extension_manifest_hash, extension_source_hash
from test_bridge_lifecycle import RUNNING
from test_bridge_security import SYNTHETIC_TOKEN, Bridge, _bridge_env, _free_port
from test_bridge_websocket import EXTENSION_ORIGIN

SECRET = "diagnosis-secret-token-" + "y" * 24
# The same files as HASH_FILES in tests/connection_diagnostics.cjs, whose background.js
# must compute the same fingerprint.
HASH_FILES = {
    "manifest.json": json.dumps({"background": {"scripts": ["bg/a.js"]},
                                 "content_scripts": [{"js": ["content/b.js", "bg/a.js"]}]}),
    "bg/a.js": "A\n", "content/b.js": "B\n",
}
HASH = "9825576d37c4d475"
MANIFEST_HASH = "2655402d232f0401"


def _unpacked_extension(test, files=HASH_FILES):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    directory = Path(temp.name)
    for name, text in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")
    return directory


class _Clock:
    """server.time's monotonic and sleep; status probes that time out also take their timeout."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def timed_out(self, timeout=2):
        self.now += timeout
        raise httpx.ReadTimeout("timed out")


# Accepts connections on the port in argv[1] (the listen backlog) but never answers, like a hung bridge.
HUNG_BRIDGE = """import socket, sys, time
listener = socket.socket()
listener.bind(("127.0.0.1", int(sys.argv[1])))
listener.listen()
print("listening", flush=True)
time.sleep(120)
"""


def _ensure_bridge_with(status, port_in_use=False):
    """Run _ensure_bridge without spawning: status() is called before and while waiting."""
    with patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": "0"}), \
            patch.object(control, "status", side_effect=status), \
            patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
            patch("builtins.open", mock_open()), patch.object(server.subprocess, "Popen", **RUNNING), \
            patch.object(server.time, "monotonic", side_effect=[0, 0, 16]), patch.object(server.time, "sleep"), \
            patch.object(control, "port_in_use", side_effect=port_in_use) as probe:
        return server._ensure_bridge(), probe


class BridgeDiagnosisTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("extension_ws", None), ("extension_ws_generation", 0),
                            ("extension_info", {}), ("WS_PORT", bridge.EXTENSION_WS_PORT),
                            ("EXTENSION_SOURCE_DIR", Path(tempfile.gettempdir()) / "no-such-kitsune-extension")):
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
        self.assertEqual(diagnosis["state"], {"port": bridge.WS_PORT, "reason": "extension_ws_port",
                                              "extensionPort": bridge.EXTENSION_WS_PORT})
        self.assertIn("KITSUNE_WS_PORT", diagnosis["hint"])
        result = asyncio.run(bridge._send_to_extension_raw("click", {}))
        self.assertEqual((result["code"], result["retryable"]), ("port_conflict", True))
        self.assertEqual(result["state"], {"connected": False, "port": bridge.WS_PORT, "reason": "extension_ws_port",
                                           "extensionPort": bridge.EXTENSION_WS_PORT})
        # An extension that connected once uses this port; it is only disconnected now.
        bridge.extension_ws_generation = 1
        self.assertEqual(bridge._connection_diagnosis()["code"], "extension_disconnected")

    def test_extension_ws_port_is_read_from_the_extension_source(self):
        source = _unpacked_extension(self, {"background/background.js":
                                            "'use strict';\nconst WS_URL = 'ws://127.0.0.1:47700';\n"})
        bridge.EXTENSION_SOURCE_DIR = source
        bridge.WS_PORT = 47700
        self.assertEqual(bridge._connection_diagnosis()["code"], "extension_disconnected",
                         "the bridge listens where this developer build connects")
        bridge.WS_PORT = bridge.EXTENSION_WS_PORT
        diagnosis = bridge._connection_diagnosis()
        self.assertEqual(diagnosis["code"], "port_conflict")
        self.assertEqual(diagnosis["state"], {"port": bridge.EXTENSION_WS_PORT, "reason": "extension_ws_port",
                                              "extensionPort": 47700})
        self.assertIn(f"{source} connects to 47700", diagnosis["hint"])

        # Without a readable WS_URL, the extension's default is assumed.
        for text in (None, "const WS_URL = BRIDGE_URL;\n", "const WS_URL = 'ws://127.0.0.1:99999';\n"):
            with self.subTest(text=text):
                files = {} if text is None else {"background/background.js": text}
                bridge.EXTENSION_SOURCE_DIR = _unpacked_extension(self, files)
                self.assertIsNone(bridge._extension_source_ws_port())
                self.assertEqual(bridge._connection_diagnosis()["code"], "extension_disconnected")
        with open(bridge.SOURCE_ROOT / "extension" / "background" / "background.js", encoding="utf-8") as background:
            self.assertIn("const WS_URL = 'ws://127.0.0.1:47652';", background.read(),
                          "_WS_URL_PORT must match the shipped declaration")
        bridge.EXTENSION_SOURCE_DIR = bridge.SOURCE_ROOT / "extension"
        self.assertEqual(bridge._extension_source_ws_port(), bridge.EXTENSION_WS_PORT)

    def test_extension_of_another_version_is_a_mismatch(self):
        for version in ("0.0.1", None):
            with self.subTest(version=version):
                self.connected(version=version)
                diagnosis = bridge._connection_diagnosis()
                self.assertEqual(diagnosis["code"], "extension_version_mismatch")
                self.assertEqual(diagnosis["state"],
                                 {"extensionVersion": version, "bridgeVersion": bridge.BRIDGE_VERSION})
                self.assertIn("about:debugging", diagnosis["hint"])

    def test_edited_developer_build_is_a_version_mismatch(self):
        source = _unpacked_extension(self)
        patcher = patch.object(bridge, "EXTENSION_SOURCE_DIR", source)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connected(sourceHash=HASH)
        self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})

        (source / "content/b.js").write_text("B edited\n", encoding="utf-8")
        diagnosis = bridge._connection_diagnosis()
        self.assertEqual(diagnosis["code"], "extension_version_mismatch")
        self.assertEqual(diagnosis["state"], {
            "extensionVersion": bridge.BRIDGE_VERSION, "bridgeVersion": bridge.BRIDGE_VERSION,
            "reason": "stale_developer_build", "extensionSourceHash": HASH,
            "expectedSourceHash": extension_source_hash(source)})
        self.assertIn("about:debugging", diagnosis["hint"])
        self.assertIn(str(source), diagnosis["hint"])

        # Unknown is never a mismatch: an extension too old to report it, the signed build,
        # or no unpacked extension next to the bridge.
        for info in ({"sourceHash": None}, {"build": "signed", "sourceHash": HASH}):
            with self.subTest(info=info):
                self.connected(**info)
                self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})
        self.connected()
        self.assertNotIn("sourceHash", bridge.extension_info)
        self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})
        self.connected(sourceHash=HASH)
        with patch.object(bridge, "EXTENSION_SOURCE_DIR", source / "missing"):
            self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})

    def test_manifest_only_edit_is_a_stale_developer_build(self):
        source = _unpacked_extension(self)
        bridge.EXTENSION_SOURCE_DIR = source
        self.connected(sourceHash=HASH, manifestHash=MANIFEST_HASH)
        self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})

        manifest = json.loads(HASH_FILES["manifest.json"])
        manifest["permissions"] = ["tabs"]
        (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(extension_source_hash(source), HASH, "the scripts are unchanged")
        diagnosis = bridge._connection_diagnosis()
        self.assertEqual(diagnosis["code"], "extension_version_mismatch")
        self.assertEqual(diagnosis["state"], {
            "extensionVersion": bridge.BRIDGE_VERSION, "bridgeVersion": bridge.BRIDGE_VERSION,
            "reason": "stale_developer_build", "extensionManifestHash": MANIFEST_HASH,
            "expectedManifestHash": extension_manifest_hash(source)})

        # Unknown is never a mismatch: an extension from before manifestHash compares its scripts only.
        for info in ({"sourceHash": HASH}, {"sourceHash": HASH, "manifestHash": None},
                     {"build": "signed", "manifestHash": MANIFEST_HASH}):
            with self.subTest(info=info):
                self.connected(**info)
                self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})
        self.connected(sourceHash=HASH, manifestHash=MANIFEST_HASH)
        bridge.EXTENSION_SOURCE_DIR = source / "missing"
        self.assertEqual(bridge._connection_diagnosis(), {"code": "connected"})

        # Both edited: both pairs are reported.
        bridge.EXTENSION_SOURCE_DIR = source
        (source / "content/b.js").write_text("B edited\n", encoding="utf-8")
        self.assertEqual(set(bridge._connection_diagnosis()["state"]) - {"extensionVersion", "bridgeVersion", "reason"},
                         {"extensionSourceHash", "expectedSourceHash", "extensionManifestHash", "expectedManifestHash"})

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

    async def unknown_action(self, version, **hello):
        async with connect(self.uri, origin=EXTENSION_ORIGIN, open_timeout=5) as client:
            await client.send(json.dumps({"type": "hello", "version": version, "build": "dev", **hello}))
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

    async def test_unknown_action_of_an_edited_developer_build_that_was_not_reloaded(self):
        source = patch.object(bridge, "EXTENSION_SOURCE_DIR", _unpacked_extension(self))
        source.start()
        self.addCleanup(source.stop)
        result = await self.unknown_action(bridge.BRIDGE_VERSION, sourceHash="0123456789abcdef",
                                           manifestHash=MANIFEST_HASH)
        self.assertEqual((result["error"], result["code"]), ("Unknown action: getSnapshot", "extension_version_mismatch"))
        self.assertEqual((result["state"]["reason"], result["state"]["extensionSourceHash"]),
                         ("stale_developer_build", "0123456789abcdef"))
        self.assertNotIn("extensionManifestHash", result["state"], "the manifest is unchanged")


class SourceHashTests(unittest.TestCase):
    def test_fingerprint_of_the_scripts_the_manifest_loads(self):
        self.assertEqual(extension_source_hash(_unpacked_extension(self)), HASH)
        self.assertIsNone(extension_source_hash(Path(tempfile.gettempdir()) / "no-such-kitsune-extension"))
        self.assertIsNone(extension_source_hash(_unpacked_extension(self, {"manifest.json": "{}"})))
        missing_script = {name: text for name, text in HASH_FILES.items() if name != "content/b.js"}
        self.assertIsNone(extension_source_hash(_unpacked_extension(self, missing_script)))

    def test_fingerprint_of_the_manifest_bytes(self):
        self.assertEqual(extension_manifest_hash(_unpacked_extension(self)), MANIFEST_HASH)
        self.assertIsNone(extension_manifest_hash(Path(tempfile.gettempdir()) / "no-such-kitsune-extension"))


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
        self.assertEqual((error["state"]["port"], error["state"]["reason"]), (server.HTTP_PORT, "not_listening"))
        self.assertIn("did not listen", error["error"])

    def test_started_bridge_that_does_not_answer_is_diagnosed_before_the_start_timeout(self):
        clock = _Clock()
        probes = []

        def status(timeout=2):
            probes.append(timeout)
            if len(probes) <= 2:
                return None  # nothing runs before the start
            clock.timed_out(timeout)

        with patch.dict(os.environ, {"KITSUNE_TOKEN": SECRET}), patch.object(control, "status", side_effect=status), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch("builtins.open", mock_open()), \
                patch.object(server.subprocess, "Popen", **RUNNING, **{"return_value.pid": 4242}), \
                patch.object(server.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(server.time, "sleep", side_effect=clock.sleep), \
                patch.object(control, "port_in_use") as port_in_use:
            error = server._ensure_bridge()
        port_in_use.assert_not_called()
        self.assertLess(clock.now, server.BRIDGE_START_TIMEOUT / 2)
        self.assertEqual(set(probes[2:]), {server.READY_PROBE_TIMEOUT})
        self.assertEqual((error["code"], error["retryable"]), ("bridge_unavailable", True))
        self.assertEqual(error["state"], {"port": server.HTTP_PORT, "reason": "not_responding", "pid": 4242})
        self.assertIn("PID 4242", error["error"])
        self.assertIn("kitsune-bridge stop", error["hint"])
        self.assertIn(f"see {server.LOG_FILE}", error["hint"])
        self.assertNotIn(SECRET, json.dumps(error))

    def test_unanswered_port_after_a_start_that_exits_is_another_program(self):
        """A program that accepts but never answers on the HTTP port: the new bridge cannot listen and exits."""
        clock = _Clock()
        calls = []

        def status(timeout=2):
            calls.append(timeout)
            if len(calls) <= 2:
                return None
            clock.timed_out(timeout)

        process = Mock(**{"poll.side_effect": lambda: None if clock.now < 2 else 1, "returncode": 1, "pid": 4242})
        with tempfile.TemporaryDirectory() as home, patch.object(server, "LOG_FILE", os.path.join(home, "bridge.log")), \
                patch.object(control, "status", side_effect=status), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch.object(server.subprocess, "Popen", return_value=process), \
                patch.object(server.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(server.time, "sleep", side_effect=clock.sleep), \
                patch.object(control, "port_in_use", return_value=True):
            error = server._ensure_bridge()
        self.assertEqual(error["code"], "port_conflict")
        self.assertEqual(error["state"], {"port": server.HTTP_PORT, "reason": "in_use", "exitCode": 1})

    def test_running_bridge_that_does_not_answer_is_not_replaced(self):
        for pid, code, state in ((4242, "bridge_unavailable", {"port": server.HTTP_PORT, "reason": "not_responding",
                                                               "pid": 4242}),
                                 (None, "port_conflict", {"port": server.HTTP_PORT, "reason": "in_use"})):
            with self.subTest(pid=pid), patch.dict(os.environ, {"KITSUNE_TOKEN": SECRET}), \
                    patch.object(control, "status", side_effect=httpx.ReadTimeout("timed out")), \
                    patch.object(control, "daemon_pid", return_value=pid), \
                    patch.object(control, "lifecycle_lock") as lock, patch.object(control, "stop") as stop, \
                    patch.object(server.subprocess, "Popen") as spawn:
                error = server._ensure_bridge()
            for not_called in (lock, stop, spawn):
                not_called.assert_not_called()
            self.assertEqual((error["code"], error["state"]), (code, state))
            self.assertIn("does not answer", error["error"])
            self.assertNotIn(SECRET, json.dumps(error))

    def test_bridge_that_exits_at_startup_is_diagnosed_at_once(self):
        with tempfile.TemporaryDirectory() as home:
            log_file = os.path.join(home, "bridge.log")
            with open(log_file, "w", encoding="utf-8") as log:
                log.write("an earlier bridge's last line\n")

            def exits_at_startup(*args, stdout, **kwargs):
                stdout.write(b"Traceback (most recent call last):\n  ...\n"
                             b"OSError: [Errno 48] address already in use; Authorization: Bearer "
                             + SECRET.encode() + b" token=" + SYNTHETIC_TOKEN.encode() + b"\n\n")
                return Mock(**{"poll.return_value": 1, "returncode": 1})

            for in_use, code in (([False, False], "bridge_unavailable"), ([False, True], "port_conflict")):
                with self.subTest(code=code), patch.dict(os.environ, {"KITSUNE_TOKEN": SECRET}), \
                        patch.object(server, "LOG_FILE", log_file), \
                        patch.object(control, "status", return_value=None), \
                        patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                        patch.object(server.subprocess, "Popen", side_effect=exits_at_startup), \
                        patch.object(server.time, "sleep") as sleep, \
                        patch.object(control, "port_in_use", side_effect=in_use):
                    error = server._ensure_bridge()
                sleep.assert_not_called()  # no wait for BRIDGE_START_TIMEOUT
                self.assertEqual(error["code"], code)
                excerpt = ("OSError: [Errno 48] address already in use; Authorization: Bearer [redacted] "
                           "token=[redacted]")
                self.assertEqual({k: error["state"][k] for k in ("exitCode", "logExcerpt")},
                                 {"exitCode": 1, "logExcerpt": excerpt})
                self.assertNotIn("earlier bridge", json.dumps(error))
                self.assertNotIn(SECRET, json.dumps(error))
                self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(error))
                if code == "bridge_unavailable":
                    self.assertEqual(error["error"], f"Kitsune bridge exited at startup with code 1: {excerpt}. "
                                                     f"See {log_file}")
                else:
                    self.assertEqual((error["state"]["port"], error["state"]["reason"]), (server.WS_PORT, "in_use"))

    def test_startup_log_excerpt_is_short(self):
        with tempfile.TemporaryDirectory() as home:
            log_file = os.path.join(home, "bridge.log")
            with open(log_file, "w", encoding="utf-8") as log:
                log.write("x" * 20000 + "\n")
            with patch.object(server, "LOG_FILE", log_file):
                self.assertEqual(server._startup_log_excerpt(0), "x" * server.STARTUP_EXCERPT_CHARS)
                self.assertEqual(server._startup_log_excerpt(20001), "", "nothing written since the start")
            with patch.object(server, "LOG_FILE", os.path.join(home, "missing.log")):
                self.assertEqual(server._startup_log_excerpt(0), "")

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
                             {"port": daemon.ws_port, "reason": "extension_ws_port",
                              "extensionPort": bridge.EXTENSION_WS_PORT})
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

    def test_bridge_that_cannot_listen_fails_without_waiting_for_the_start_timeout(self):
        """A real bridge spawned by _ensure_bridge on spare ports, its WebSocket port held by this test."""
        with tempfile.TemporaryDirectory() as home, socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            ws_port, http_port = holder.getsockname()[1], _free_port()
            self.assertNotIn(bridge.EXTENSION_WS_PORT, (ws_port, http_port))
            env = _bridge_env(home, http_port, ws_port, SECRET)
            bridge_url = f"http://127.0.0.1:{http_port}"
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(control, "BRIDGE", bridge_url), patch.object(server, "BRIDGE", bridge_url), \
                    patch.object(control, "DATA_DIR", home), \
                    patch.object(control, "LOCK_FILE", os.path.join(home, "bridge.lock")), \
                    patch.object(server, "LOG_FILE", os.path.join(home, "bridge.log")), \
                    patch.object(server, "HTTP_PORT", http_port), patch.object(server, "WS_PORT", ws_port):
                started = time.monotonic()
                error = server._ensure_bridge()
                elapsed = time.monotonic() - started
                self.assertIsNone(control.status(), "nothing may answer on the spare HTTP port")
        self.assertLess(elapsed, server.BRIDGE_START_TIMEOUT - 5, error)
        self.assertEqual(error["code"], "port_conflict", error)
        self.assertEqual({k: error["state"][k] for k in ("port", "reason", "exitCode")},
                         {"port": ws_port, "reason": "in_use", "exitCode": 1})
        self.assertIn("in use", error["state"]["logExcerpt"].lower())
        self.assertLessEqual(len(error["state"]["logExcerpt"]), server.STARTUP_EXCERPT_CHARS)
        self.assertNotIn(SECRET, json.dumps(error))

    def test_hung_bridge_is_diagnosed_without_waiting_for_the_start_timeout(self):
        """_ensure_bridge spawns a real process that listens on a spare HTTP port but never answers."""
        popen = server.subprocess.Popen
        spawned = []

        def spawn_hung_bridge(args, **kwargs):
            process = popen([sys.executable, "-c", HUNG_BRIDGE, str(http_port)], **kwargs)
            spawned.append(process)
            self.addCleanup(process.wait, 10)
            self.addCleanup(process.kill)
            return process

        with tempfile.TemporaryDirectory() as home:
            http_port, ws_port = _free_port(), _free_port()
            self.assertFalse({bridge.EXTENSION_WS_PORT, 47651} & {http_port, ws_port})
            env = _bridge_env(home, http_port, ws_port, SECRET)
            bridge_url = f"http://127.0.0.1:{http_port}"
            log_file = os.path.join(home, "bridge.log")
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(control, "BRIDGE", bridge_url), patch.object(server, "BRIDGE", bridge_url), \
                    patch.object(control, "DATA_DIR", home), \
                    patch.object(control, "LOCK_FILE", os.path.join(home, "bridge.lock")), \
                    patch.object(control, "PID_FILE", os.path.join(home, "bridge.pid")), \
                    patch.object(server, "LOG_FILE", log_file), \
                    patch.object(server, "HTTP_PORT", http_port), patch.object(server, "WS_PORT", ws_port), \
                    patch.object(server.subprocess, "Popen", side_effect=spawn_hung_bridge):
                started = time.monotonic()
                error = server._ensure_bridge()
                elapsed = time.monotonic() - started
                self.assertEqual(len(spawned), 1)
                self.assertIsNone(spawned[0].poll(), "the hung process still runs")
                # The next call finds it before starting anything; it holds no Kitsune pid lock.
                again = server._ensure_bridge()
                self.assertEqual(len(spawned), 1, "nothing else was started")
        self.assertLess(elapsed, server.BRIDGE_START_TIMEOUT - 5, error)
        self.assertEqual(error["code"], "bridge_unavailable", error)
        self.assertEqual(error["state"], {"port": http_port, "reason": "not_responding", "pid": spawned[0].pid})
        self.assertEqual((again["code"], again["state"]), ("port_conflict", {"port": http_port, "reason": "in_use"}))
        for result in (error, again):
            self.assertNotIn(SECRET, json.dumps(result))

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
