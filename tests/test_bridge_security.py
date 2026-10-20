"""Behaviour-level security and daemon-lock tests against isolated real bridges.

Each bridge is a `python -m kitsune.bridge` subprocess with a temporary
KITSUNE_HOME/HOME, ephemeral ports and a synthetic token, so these tests never
touch a normal user profile or a running user bridge. The WebSocket checks use a
minimal raw client so they assert on wire behaviour only, not on the server's
websockets implementation.
"""
import base64
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

import kitsune.bridge_control as control

START_TIMEOUT = 15
SYNTHETIC_TOKEN = "synthetic-test-token-" + "x" * 24


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _bridge_env(home, http_port, ws_port, token=SYNTHETIC_TOKEN):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KITSUNE_")}
    env.update(HOME=home, KITSUNE_HOME=home, KITSUNE_HTTP_PORT=str(http_port), KITSUNE_WS_PORT=str(ws_port))
    if token is not None:
        env["KITSUNE_TOKEN"] = token
    return env


def _request(port, method, path, headers=None, host=None, body=None):
    """Send a request with an exact Host header; return (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        data = body.encode() if body is not None else b""
        if method == "POST":
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data if data else None)
        response = conn.getresponse()
        return response.status, response.headers, response.read().decode("utf-8", "replace")
    finally:
        conn.close()


class Bridge:
    """A real bridge daemon process owned by one test."""

    def __init__(self, home, token=SYNTHETIC_TOKEN):
        self.home = home
        self.token = token
        self.http_port = _free_port()
        self.ws_port = _free_port()
        self.env = _bridge_env(home, self.http_port, self.ws_port, token)
        self.log_path = Path(home) / f"bridge-{self.http_port}.log"
        self.process = None

    @property
    def host(self):
        return f"127.0.0.1:{self.http_port}"

    def auth(self):
        token = self.token or Path(self.home, "token").read_text(encoding="utf-8").strip()
        return {"Authorization": "Bearer " + token}

    def spawn(self):
        with open(self.log_path, "ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "kitsune.bridge"], env=self.env, cwd=self.home,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
        return self

    def start(self):
        self.spawn()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"bridge exited early ({self.process.returncode}): {self.log()}")
            try:
                if self.status_code() == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError("bridge did not start: " + self.log())

    def status_code(self):
        return _request(self.http_port, "GET", "/api/status", self.auth(), self.host)[0]

    def status(self):
        status, _, body = _request(self.http_port, "GET", "/api/status", self.auth(), self.host)
        assert status == 200, (status, body)
        return json.loads(body)

    def log(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            _request(self.http_port, "POST", "/api/shutdown", self.auth(), self.host)
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=10)


class RawWebSocket:
    """Just enough of RFC 6455 to observe handshake and close behaviour."""

    def __init__(self, port, origin=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET / HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
        ]
        if origin is not None:
            lines.append(f"Origin: {origin}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response += chunk
        head, _, self.buffer = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        self.upgraded = status_line.split(" ")[1:2] == ["101"]

    def _recv_exact(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def closed_by_server(self, timeout=5):
        """True if the server closes (close frame or EOF) within `timeout` seconds."""
        if not self.upgraded:
            return True
        self.sock.settimeout(timeout)
        try:
            while True:
                first, second = self._recv_exact(2)
                length = second & 0x7F
                if length == 126:
                    length = int.from_bytes(self._recv_exact(2), "big")
                elif length == 127:
                    length = int.from_bytes(self._recv_exact(8), "big")
                self._recv_exact(length)
                if first & 0x0F == 0x8:
                    return True
        except ConnectionError:
            return True
        except socket.timeout:
            return False

    def send_text(self, text):
        payload = text.encode()
        assert len(payload) < 126
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)

    def close(self):
        self.sock.close()


class BridgeHttpSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.bridge = Bridge(cls.temp.name)
        try:
            cls.bridge.start()
        except BaseException:
            cls.bridge.stop()
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()
        cls.temp.cleanup()

    def request(self, method, path, headers=None, host=None, body=None):
        return _request(self.bridge.http_port, method, path, headers, self.bridge.host if host is None else host, body)

    def test_valid_token_is_accepted_on_both_loopback_hosts(self):
        for host in (f"127.0.0.1:{self.bridge.http_port}", f"localhost:{self.bridge.http_port}"):
            with self.subTest(host=host):
                status, _, body = self.request("GET", "/api/status", self.bridge.auth(), host)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "running")

    def test_missing_or_invalid_token_is_rejected(self):
        token = SYNTHETIC_TOKEN
        for name, headers in {
            "missing": {},
            "empty bearer": {"Authorization": "Bearer "},
            "wrong": {"Authorization": "Bearer not-the-token"},
            "prefix": {"Authorization": "Bearer " + token[:-1]},
            "suffix": {"Authorization": "Bearer " + token + "x"},
            "wrong scheme": {"Authorization": "Basic " + token},
            "raw token": {"Authorization": token},
            "other header": {"X-Kitsune-Token": token},
        }.items():
            with self.subTest(name=name):
                status, _, body = self.request("GET", "/api/status", headers)
                self.assertEqual(status, 401)
                self.assertNotIn(token, body)

    def test_token_in_query_string_is_not_accepted(self):
        status, _, _ = self.request("GET", "/api/status?token=" + SYNTHETIC_TOKEN)
        self.assertEqual(status, 401)

    def test_unauthenticated_post_routes_and_aliases_are_rejected(self):
        for path in ("/api/shutdown", "/api/js", "/api/executeJS", "/api/navigate", "/api/set-policy", "/api/batch"):
            with self.subTest(path=path):
                status, _, _ = self.request("POST", path, {"Content-Type": "application/json"}, body="{}")
                self.assertEqual(status, 401)
        self.assertEqual(self.bridge.status_code(), 200, "unauthenticated shutdown must not stop the bridge")

    def test_unknown_get_route_requires_auth_before_routing(self):
        status, _, _ = self.request("GET", "/api/does-not-exist")
        self.assertEqual(status, 401)

    def test_foreign_or_missing_host_is_rejected_even_with_token(self):
        port = self.bridge.http_port
        for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", "127.0.0.1",
                     f"localhost.evil.example:{port}", f"[::1]:{port}", f"0.0.0.0:{port}", ""):
            with self.subTest(host=host):
                status, _, body = self.request("GET", "/api/status", self.bridge.auth(), host)
                self.assertEqual(status, 403)
                self.assertNotIn(SYNTHETIC_TOKEN, body)
        status, _, _ = _request(port, "GET", "/api/status", self.bridge.auth(), host=None)
        self.assertEqual(status, 403)

    def test_foreign_host_is_rejected_on_unauthenticated_view(self):
        status, _, _ = self.request("GET", "/view?src=https://example.com/a.png", host="evil.example")
        self.assertEqual(status, 403)

    def test_view_is_tokenless_but_escapes_its_source(self):
        src = "https://example.com/a.png\"><script>alert(1)</script>"
        status, headers, body = self.request("GET", "/view?src=" + quote(src, safe=""))
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertNotIn("<script>", body)
        self.assertNotIn(SYNTHETIC_TOKEN, body)

    def test_cors_preflight_is_refused(self):
        status, headers, _ = self.request("OPTIONS", "/api/status", {
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        })
        self.assertFalse(200 <= status < 300, status)
        self.assertEqual([h for h in headers if h.lower().startswith("access-control-")], [])

    def test_responses_never_grant_cross_origin_access(self):
        origin = {"Origin": "https://evil.example"}
        cases = [
            ("GET", "/api/status", dict(origin, **self.bridge.auth())),
            ("GET", "/api/status", origin),
            ("POST", "/api/shutdown", origin),
            ("GET", "/view?src=x", origin),
        ]
        for method, path, headers in cases:
            with self.subTest(method=method, path=path, authenticated="Authorization" in headers):
                _, response_headers, _ = self.request(method, path, headers, body="" if method == "POST" else None)
                self.assertEqual([h for h in response_headers if h.lower().startswith("access-control-")], [])


class BridgeWebSocketOriginTests(unittest.TestCase):
    EXTENSION_ORIGIN = "moz-extension://00000000-0000-4000-8000-000000000000"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name).start()
        self.addCleanup(self.bridge.stop)

    def connect(self, origin):
        ws = RawWebSocket(self.bridge.ws_port, origin)
        self.addCleanup(ws.close)
        return ws

    def assert_rejected(self, origin):
        ws = self.connect(origin)
        self.assertTrue(ws.closed_by_server(), f"origin {origin!r} was not rejected")

    def test_non_extension_origins_are_rejected(self):
        for origin in (None, "", "null", "https://evil.example", "http://127.0.0.1",
                       f"http://localhost:{self.bridge.http_port}", "https://moz-extension.evil.example",
                       "file://"):
            with self.subTest(origin=origin):
                self.assert_rejected(origin)
                status = self.bridge.status()
                self.assertFalse(status["extension_connected"])
                self.assertEqual(status["extension_generation"], 0)

    def test_extension_origin_connects(self):
        ws = self.connect(self.EXTENSION_ORIGIN)
        self.assertTrue(ws.upgraded)
        ws.send_text(json.dumps({"type": "hello", "version": "0.0.0-test", "build": "test"}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.bridge.status()["extension_version"] != "0.0.0-test":
            time.sleep(0.05)
        status = self.bridge.status()
        self.assertTrue(status["extension_connected"])
        self.assertEqual(status["extension_version"], "0.0.0-test")
        self.assertFalse(ws.closed_by_server(timeout=0.3))

    def test_rejected_origin_cannot_replace_connected_extension(self):
        extension = self.connect(self.EXTENSION_ORIGIN)
        self.assertTrue(extension.upgraded)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.bridge.status()["extension_connected"]:
            time.sleep(0.05)
        before = self.bridge.status()
        self.assertTrue(before["extension_connected"])
        self.assert_rejected("https://evil.example")
        after = self.bridge.status()
        self.assertTrue(after["extension_connected"])
        self.assertEqual(after["extension_generation"], before["extension_generation"])
        self.assertFalse(extension.closed_by_server(timeout=0.3))


class BridgeDaemonLockTests(unittest.TestCase):
    """Single-daemon lock and lifecycle races with real processes in a temp home."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = temp.name
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.stop()
            if bridge.process is not None and bridge.process.poll() is None:
                bridge.process.kill()
                bridge.process.wait(timeout=10)

    def new_bridge(self, token=SYNTHETIC_TOKEN):
        bridge = Bridge(self.home, token)
        self.bridges.append(bridge)
        return bridge

    def cli(self, bridge, *args):
        return subprocess.Popen(
            [sys.executable, "-m", "kitsune.bridge", *args], env=bridge.env, cwd=self.home,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_second_daemon_in_same_home_is_rejected_even_on_other_ports(self):
        first = self.new_bridge().start()
        second = self.new_bridge().spawn()
        self.assertEqual(second.process.wait(timeout=START_TIMEOUT), 1)
        self.assertIn("already running", second.log())
        self.assertEqual(first.status_code(), 200)
        with self.assertRaises(OSError):
            _request(second.http_port, "GET", "/api/status", second.auth(), second.host)

    def test_concurrent_starts_leave_exactly_one_daemon(self):
        bridges = [self.new_bridge().spawn() for _ in range(4)]
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline and sum(b.process.poll() is None for b in bridges) > 1:
            time.sleep(0.05)
        running = [b for b in bridges if b.process.poll() is None]
        self.assertEqual(len(running), 1, [b.log() for b in bridges])
        for loser in bridges:
            if loser is not running[0]:
                self.assertEqual(loser.process.returncode, 1)
                self.assertIn("already running", loser.log())
        winner = running[0]
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if winner.status_code() == 200:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        self.assertEqual(winner.status_code(), 200)
        pid_file = Path(self.home, "bridge.pid")
        self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(winner.process.pid))

    def test_locked_pid_is_trusted_only_while_daemon_holds_lock(self):
        pid_file = os.path.join(self.home, "bridge.pid")
        with patch.object(control, "PID_FILE", pid_file):
            bridge = self.new_bridge().start()
            self.assertEqual(control._locked_pid(), bridge.process.pid)
            bridge.stop()
            self.assertEqual(bridge.process.returncode, 0)
            # The stale file keeps the old PID but is no longer locked.
            self.assertEqual(Path(pid_file).read_text(encoding="utf-8").strip(), str(bridge.process.pid))
            self.assertIsNone(control._locked_pid())

    def test_restart_after_stop_reuses_pid_file(self):
        first = self.new_bridge().start()
        first.stop()
        self.assertEqual(first.process.returncode, 0)
        second = self.new_bridge().start()
        self.assertEqual(Path(self.home, "bridge.pid").read_text(encoding="utf-8").strip(), str(second.process.pid))

    def test_cli_stop_uses_token_file_and_stops_daemon(self):
        bridge = self.new_bridge(token=None).start()
        token_file = Path(self.home, "token")
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        stop = self.cli(bridge, "stop")
        out, err = stop.communicate(timeout=20)
        self.assertEqual(stop.returncode, 0, err)
        self.assertIn("stopped", out)
        self.assertEqual(bridge.process.wait(timeout=10), 0)
        status = self.cli(bridge, "status")
        out, _ = status.communicate(timeout=20)
        self.assertEqual(json.loads(out), {"status": "stopped"})

    def test_cli_without_valid_token_cannot_stop_daemon(self):
        bridge = self.new_bridge().start()
        intruder = Bridge(self.home, token="wrong-token")
        intruder.env = _bridge_env(self.home, bridge.http_port, bridge.ws_port, "wrong-token")
        for command in ("status", "stop"):
            with self.subTest(command=command):
                cli = self.cli(intruder, command)
                cli.communicate(timeout=20)
                self.assertEqual(cli.returncode, 1)
        self.assertIsNone(bridge.process.poll())
        self.assertEqual(bridge.status_code(), 200)

    def test_concurrent_cli_stops_all_succeed(self):
        bridge = self.new_bridge().start()
        stops = [self.cli(bridge, "stop") for _ in range(3)]
        for stop in stops:
            _, err = stop.communicate(timeout=30)
            self.assertEqual(stop.returncode, 0, err)
        self.assertEqual(bridge.process.wait(timeout=10), 0)

    def test_cli_stop_without_daemon_is_noop(self):
        bridge = self.new_bridge()
        stop = self.cli(bridge, "stop")
        out, err = stop.communicate(timeout=20)
        self.assertEqual(stop.returncode, 0, err)
        self.assertIn("stopped", out)

    def test_lifecycle_lock_excludes_other_processes(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, kitsune.bridge_control as c\n"
             "with c.lifecycle_lock():\n    print('locked', flush=True)\n    sys.stdin.read()"],
            env=_bridge_env(self.home, _free_port(), _free_port()), cwd=self.home,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        entered = threading.Event()

        def acquire():
            with control.lifecycle_lock():
                entered.set()

        with patch.object(control, "DATA_DIR", self.home), \
                patch.object(control, "LOCK_FILE", os.path.join(self.home, "bridge.lock")):
            waiter = threading.Thread(target=acquire, daemon=True)
            waiter.start()
            self.assertFalse(entered.wait(0.5), "lifecycle lock was not exclusive")
            holder.stdin.close()
            self.assertEqual(holder.wait(timeout=10), 0)
            self.assertTrue(entered.wait(10))
            waiter.join(timeout=10)
        holder.stdout.close()
