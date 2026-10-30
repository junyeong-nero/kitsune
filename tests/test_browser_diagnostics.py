"""Opt-in browser build diagnostics: exact WS peer, bounded reads and explicit unknowns (#366)."""

import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, browser_diagnostics as diag, server, tools
from kitsune.bridge_client import BridgeClient
from kitsune.browser import Browser

RUNTIME = {"name": "Zen", "vendor": "Mozilla", "version": "1.23.4b", "buildID": "20261020090730"}
LOCAL = ("127.0.0.1", 47652)
REMOTE = ("127.0.0.1", 56730)
# Include the bridge's forward connection and a second browser to catch name/port-only matching.
SOCKETS = ("p10\nf5\nn127.0.0.1:47652->127.0.0.1:56730\n"
           "p20\nf8\nn127.0.0.1:56730->127.0.0.1:47652\n"
           "p30\nf9\nn127.0.0.1:56731->127.0.0.1:47652\n")


class BrowserDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name, "A private browser.app")
        for contents in ("Contents", "Contents.bak"):
            root = self.bundle / contents
            root.mkdir(parents=True)
            (root / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "zen"}))
        resources = self.bundle / "Contents" / "Resources"
        resources.mkdir()
        self.ini = resources / "application.ini"
        self.ini.write_text("[App]\nName=Zen\nVersion=1.24b\nBuildID=20261029123729\nVendor=Mozilla\n")
        platform = patch.object(diag.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)

    def run_diagnosis(self, runtime=RUNTIME, contents="Contents.bak", sockets=SOCKETS, after=None):
        files = f"p20\nftxt\nn{self.bundle}/{contents}/MacOS/zen\nn{self.bundle}/{contents}/MacOS/lib.dylib\n"
        with patch.object(diag, "_lsof", side_effect=[sockets, files, sockets if after is None else after]) as probe:
            result = diag.diagnose(runtime, LOCAL, REMOTE)
        return result, probe

    def test_mismatch_uses_only_connected_process_and_installed_not_backup_metadata(self):
        result, probe = self.run_diagnosis()
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["restart_recommended"])
        self.assertEqual(result["comparison"], "gecko_build_id")
        self.assertEqual(result["runtime"]["buildID"], RUNTIME["buildID"])
        self.assertEqual(result["installed"]["buildID"], "20261029123729")
        self.assertEqual(result["process"], {"source": "websocket_peer", "mapped_bundle": "backup"})
        self.assertIn("save work", result["hint"])
        self.assertIn("not proof", result["hint"])
        self.assertEqual(probe.call_args_list[1].args, ("-a", "-p", "20", "-d", "txt", "-Fn"))
        public = json.dumps(result)
        self.assertNotIn(str(self.bundle), public)
        self.assertNotIn("127.0.0.1", public)
        self.assertNotIn('"pid"', public)

    def test_matching_builds_do_not_compare_product_and_engine_version_strings(self):
        result, _ = self.run_diagnosis({**RUNTIME, "version": "148.0", "buildID": "20261029123729"},
                                      contents="Contents")
        self.assertEqual(result["status"], "match")
        self.assertFalse(result["restart_recommended"])
        self.assertNotIn("hint", result)

    def test_backup_path_alone_does_not_claim_a_build_mismatch(self):
        result, _ = self.run_diagnosis({**RUNTIME, "buildID": "20261029123729"})
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["process"]["mapped_bundle"], "backup")

    def test_missing_or_invalid_build_is_unknown_not_version_comparison(self):
        for build in (None, "", "Firefox-148", "2026", "9" * 129):
            with self.subTest(build=build):
                result, _ = self.run_diagnosis({**RUNTIME, "buildID": build})
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "build_id_unavailable")
                self.assertIsNone(result["restart_recommended"])

    def test_unknown_runtime_and_unsupported_os_never_inspect_processes(self):
        with patch.object(diag, "_lsof") as probe:
            self.assertEqual(diag.diagnose(None, LOCAL, REMOTE)["reason"], "runtime_info_unavailable")
            with patch.object(diag.sys, "platform", "linux"):
                result = diag.diagnose(RUNTIME, LOCAL, REMOTE)
            self.assertEqual(result["reason"], "unsupported_platform")
            self.assertEqual(result["runtime"]["version"], RUNTIME["version"])
            probe.assert_not_called()

    def test_no_matching_peer_or_multiple_owners_is_unknown(self):
        for sockets, reason in (("p30\nn127.0.0.1:56731->127.0.0.1:47652\n", "browser_process_unavailable"),
                                (SOCKETS + "p40\nn127.0.0.1:56730->127.0.0.1:47652\n", "ambiguous_browser_process")):
            result, probe = self.run_diagnosis(sockets=sockets)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(probe.call_count, 1)

    def test_process_exit_or_reuse_during_probe_is_unknown(self):
        for after in ("", SOCKETS.replace("p20", "p99")):
            result, _ = self.run_diagnosis(after=after)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("installed", result)

    def test_missing_malformed_and_oversize_metadata_are_unknown(self):
        for content in (b"not an ini", b"\xff", b"x" * (diag.METADATA_LIMIT + 1)):
            self.ini.write_bytes(content)
            result, _ = self.run_diagnosis()
            self.assertEqual(result["reason"], "installed_metadata_unavailable")
        self.ini.unlink()
        result, _ = self.run_diagnosis()
        self.assertEqual(result["reason"], "installed_metadata_unavailable")

    def test_invalid_plist_and_nonmatching_executable_are_unknown(self):
        plist = self.bundle / "Contents.bak" / "Info.plist"
        for content in (b"not a plist", b"<?xml version='1.0'?><plist><dict>",
                        plistlib.dumps([]), plistlib.dumps({"CFBundleExecutable": "another-browser"})):
            with self.subTest(content=content):
                plist.write_bytes(content)
                result, _ = self.run_diagnosis()
                self.assertEqual(result["reason"], "browser_bundle_unavailable")

    def test_process_probe_is_bounded_and_does_not_expose_errors(self):
        for error, reason in ((FileNotFoundError("private path"), "process_lookup_unavailable"),
                              (PermissionError("private path"), "process_lookup_unavailable"),
                              (subprocess.TimeoutExpired("lsof", 1), "process_lookup_timeout")):
            with patch.object(diag.subprocess, "run", side_effect=error) as run:
                result = diag.diagnose(RUNTIME, LOCAL, REMOTE)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(run.call_args.kwargs["timeout"], 1)
            self.assertNotIn("private", json.dumps(result))
        with patch.object(diag.subprocess, "run", return_value=SimpleNamespace(returncode=2, stdout="", stderr="secret")):
            self.assertEqual(diag.diagnose(RUNTIME, LOCAL, REMOTE)["reason"], "process_lookup_failed")

    def test_invalid_connection_addresses_never_probe(self):
        for address in (None, (), ("example.com", 1), ("192.0.2.1", 1), ("127.0.0.1", "1")):
            with patch.object(diag, "_lsof") as probe:
                result = diag.diagnose(RUNTIME, address, REMOTE)
                self.assertEqual(result["reason"], "connection_address_unavailable")
                probe.assert_not_called()

    def test_ipv6_loopback_matches_exact_reverse_connection(self):
        with patch.object(diag, "_lsof", return_value="p22\nn[::1]:1234->[::1]:5678\n"):
            self.assertEqual(diag._peer_pid(("::1", 5678, 0, 0), ("::1", 1234, 0, 0)), 22)

    def test_runtime_fields_are_allowlisted_and_bounded(self):
        self.assertEqual(diag.browser_info({**RUNTIME, "url": "private", "token": "secret"}), RUNTIME)
        for value in (None, [], "text", {"name": "x" * 129}, {"name": "private\npath"}):
            self.assertEqual(diag.browser_info(value), {})


class StatusDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.socket = SimpleNamespace(local_address=LOCAL, remote_address=REMOTE)
        for name, value in (("extension_ws", self.socket), ("extension_ws_generation", 4),
                            ("extension_info", {"version": bridge.BRIDGE_VERSION, "build": "signed", "runtime": RUNTIME})):
            p = patch.object(bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        self.handler.send_json = Mock()

    def status(self, path):
        self.handler.path = path
        self.handler.handle_status()
        return self.handler.send_json.call_args.args[1]

    def test_default_status_never_runs_detailed_probe(self):
        with patch.object(diag, "diagnose") as probe:
            for path in ("/api/status", "/api/status?diagnostics=false"):
                result = self.status(path)
                self.assertNotIn("browser_diagnostics", result)
                self.assertEqual(result["diagnosis"]["code"], "connected")
            probe.assert_not_called()

    def test_explicit_diagnostics_preserves_connection_diagnosis(self):
        with patch.object(diag, "diagnose", return_value={"status": "mismatch"}) as probe:
            result = self.status("/api/status?diagnostics=true")
        probe.assert_called_once_with(RUNTIME, LOCAL, REMOTE)
        self.assertEqual(result["browser_diagnostics"], {"status": "mismatch"})
        self.assertEqual(result["diagnosis"]["code"], "connected")

    def test_connection_replacement_discards_result(self):
        def replace(*args):
            bridge.extension_ws_generation += 1
            return {"status": "match"}
        with patch.object(diag, "diagnose", side_effect=replace):
            result = self.status("/api/status?diagnostics=true")
        self.assertEqual(result["browser_diagnostics"]["reason"], "connection_changed")

    def test_runtime_metadata_change_discards_result(self):
        def replace(*args):
            bridge.extension_info["runtime"] = {**RUNTIME, "buildID": "20261029123729"}
            return {"status": "match"}
        with patch.object(diag, "diagnose", side_effect=replace):
            result = self.status("/api/status?diagnostics=true")
        self.assertEqual(result["browser_diagnostics"]["reason"], "connection_changed")

    def test_disconnected_has_no_process_probe(self):
        bridge.extension_ws = None
        with patch.object(diag, "diagnose") as probe:
            result = self.status("/api/status?diagnostics=true")
            probe.assert_not_called()
        self.assertEqual(result["browser_diagnostics"]["reason"], "extension_disconnected")

    def test_tool_and_mcp_wrapper_forward_flag(self):
        with patch.object(tools, "_get", side_effect=lambda path: {"diagnosis": {"code": "connected"}}) as get:
            server.browser_status()
            get.assert_called_with("/api/status")
            result = server.browser_status(diagnostics=True)
            get.assert_called_with("/api/status?diagnostics=true")
            self.assertIn("mcp_tools", result)

    def test_python_api_forwards_flag(self):
        with patch.object(Browser, "request", return_value={}) as request:
            Browser(Mock()).status(diagnostics=True)
            request.assert_called_once_with("/api/status?diagnostics=true")

    def test_diagnostics_status_does_not_wait_for_extension_reconnection(self):
        client = BridgeClient(caps=None)
        with patch.object(client, "await_extension") as wait, patch("kitsune.bridge_client.httpx.request") as request:
            request.return_value.status_code = 200
            request.return_value.json.return_value = {"diagnosis": {"code": "extension_disconnected"}}
            client.get("/api/status?diagnostics=true")
            wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
