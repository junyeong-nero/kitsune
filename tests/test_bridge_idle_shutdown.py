"""Opt-in idle shutdown and restart-on-demand against isolated real bridges.

Bridges run with a temporary KITSUNE_HOME/HOME, ephemeral ports and a synthetic
token (see test_bridge_security.Bridge), so a user profile or running user
bridge is never touched.
"""
import logging
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from kitsune import bridge, bridge_client, server
import kitsune.bridge_control as control
from test_bridge_security import SYNTHETIC_TOKEN, Bridge

IDLE_TIMEOUT = 1.0
EXIT_TIMEOUT = 10


class BridgeIdleShutdownProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = temp.name
        self.bridge = Bridge(self.home)
        self.bridge.env[bridge.IDLE_TIMEOUT_ENV] = str(IDLE_TIMEOUT)
        self.addCleanup(self.bridge.stop)

    def wait_exit(self, process, timeout=EXIT_TIMEOUT):
        process.wait(timeout=timeout)
        return process.returncode

    def kill_if_running(self, processes):
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def daemon_paths(self):
        """Point bridge_control at this test's home so lock checks never see a user bridge."""
        return patch.multiple(
            control, DATA_DIR=self.home, LOCK_FILE=os.path.join(self.home, "bridge.lock"),
            PID_FILE=os.path.join(self.home, "bridge.pid"),
        )

    def test_idle_bridge_exits_cleanly_and_releases_locks(self):
        self.bridge.start()
        self.assertEqual(self.wait_exit(self.bridge.process), 0)
        self.assertIn("No client activity", self.bridge.log())
        with self.assertRaises(OSError):
            self.bridge.status_code()
        with self.daemon_paths():
            self.assertIsNone(control._locked_pid())
            lock = control.try_lifecycle_lock()
            self.assertIsNotNone(lock)
            lock.close()

    def test_bridge_without_idle_timeout_keeps_running(self):
        del self.bridge.env[bridge.IDLE_TIMEOUT_ENV]
        self.bridge.start()
        time.sleep(IDLE_TIMEOUT * 2.5)
        self.assertIsNone(self.bridge.process.poll())

    def test_concurrent_client_calls_keep_bridge_alive(self):
        self.bridge.start()
        stop = threading.Event()
        failures = []

        def client():
            while not stop.is_set():
                try:
                    if self.bridge.status_code() != 200:
                        failures.append("non-200")
                except OSError as exc:
                    failures.append(repr(exc))
                stop.wait(IDLE_TIMEOUT / 3)

        clients = [threading.Thread(target=client) for _ in range(2)]
        for thread in clients:
            thread.start()
        try:
            time.sleep(IDLE_TIMEOUT * 3)
            self.assertIsNone(self.bridge.process.poll(), self.bridge.log())
        finally:
            stop.set()
            for thread in clients:
                thread.join(timeout=10)
        self.assertEqual(failures, [])
        self.assertEqual(self.wait_exit(self.bridge.process), 0)

    def test_in_flight_request_blocks_idle_shutdown(self):
        self.bridge.start()
        # An accepted request whose headers are still arriving is in flight.
        with socket.create_connection(("127.0.0.1", self.bridge.http_port), timeout=10) as sock:
            sock.sendall(f"GET /api/status HTTP/1.0\r\nHost: {self.bridge.host}\r\n".encode())
            time.sleep(IDLE_TIMEOUT * 3)
            self.assertIsNone(self.bridge.process.poll(), self.bridge.log())
            sock.sendall(f"Authorization: Bearer {SYNTHETIC_TOKEN}\r\n\r\n".encode())
            response = b""
            while chunk := sock.recv(4096):
                response += chunk
        self.assertTrue(response.startswith(b"HTTP/1.0 200"), response[:200])
        self.assertEqual(self.wait_exit(self.bridge.process), 0)

    def test_next_client_call_restarts_idle_bridge_on_demand(self):
        self.bridge.start()
        self.assertEqual(self.wait_exit(self.bridge.process), 0)
        url = f"http://127.0.0.1:{self.bridge.http_port}"
        log_file = os.path.join(self.home, "bridge.log")
        for patcher in (
            patch.dict(os.environ, self.bridge.env, clear=True), self.daemon_paths(),
            patch.multiple(control, BRIDGE=url, TOKEN_FILE=os.path.join(self.home, "token"), LOG_FILE=log_file),
            patch.multiple(bridge_client, BRIDGE=url, LOG_FILE=log_file),
            patch.object(server._client, "_extension_wait_until", 0.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(control.stop)  # Runs before the patches are undone.
        spawned = []
        self.addCleanup(self.kill_if_running, spawned)
        httpx_logger = logging.getLogger("httpx")
        self.addCleanup(httpx_logger.setLevel, httpx_logger.level)
        httpx_logger.setLevel(logging.WARNING)

        # The MCP server's normal path: ConnectError -> ensure_bridge spawns a new daemon.
        popen = subprocess.Popen

        def spawn(*args, **kwargs):
            spawned.append(popen(*args, **kwargs))
            return spawned[-1]

        with patch.object(bridge_client.subprocess, "Popen", side_effect=spawn):
            status = server._get("/api/status")
        self.assertEqual(status.get("build"), bridge_client.BRIDGE_BUILD, status)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(control._locked_pid(), spawned[0].pid)
        # The respawned daemon inherited the client's idle timeout and idles out too.
        self.assertEqual(self.wait_exit(spawned[0]), 0, Path(log_file).read_text(encoding="utf-8", errors="replace"))
        self.assertIsNone(control._locked_pid())
        self.assertIsNone(control.status())


if __name__ == "__main__":
    unittest.main()
