"""The real-browser harness's opt-in failure artifacts (issue #293); runs without a browser."""
import collections
import contextlib
from http.server import BaseHTTPRequestHandler
import io
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
from unittest import mock
import weakref

import real_browser_failure_artifacts as artifacts
import real_browser_harness as harness

TOKEN = "synthetic-artifact-token-0123456789"


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<p>page-body-text</p>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _AbortedReply(BaseHTTPRequestHandler):
    """Sends a 200 and its headers, then fails as if the browser had dropped the connection."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        raise BrokenPipeError("synthetic abort detail")

    def log_message(self, *args):
        pass


def _artifact_root(test):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    patcher = mock.patch.dict(os.environ, {artifacts.ARTIFACTS_ENV: temp.name})
    patcher.start()
    test.addCleanup(patcher.stop)
    return Path(temp.name)


def _failure_dirs(root):
    return sorted(path for path in root.glob("kitsune-e2e-*/*/*") if path.is_dir())


class SanitizingTests(unittest.TestCase):
    def test_urls_keep_only_a_loopback_origin_and_path(self):
        cases = {
            "http://127.0.0.1:8123/select?q=secret#frag": "http://127.0.0.1:8123/select",
            "http://user:pass@localhost:9/a": "http://localhost:9/a",
            "https://example.com/private?token=x": "https://[non-loopback]",
            "about:blank": "about:blank",
            "data:text/html,<p>page contents</p>": "data:[omitted]",
            "moz-extension://uuid/popup.html": "moz-extension:[omitted]",
            "": None,
            None: None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(artifacts.sanitize_url(url), expected)

    def test_credentials_are_redacted(self):
        text = (f"token {TOKEN}\nAuthorization: Bearer abc.def\n{{\"authorization\": \"Basic zzz\"}}\n"
                "GET /api/status?token=qqq&x=1\nsent bearer sk-123\n")
        redacted = artifacts.redact(text, [TOKEN])
        for secret in (TOKEN, "abc.def", "Basic zzz", "qqq", "sk-123"):
            self.assertNotIn(secret, redacted)
        self.assertIn("x=1", redacted)


class FixtureTimingTests(unittest.TestCase):
    def request(self, server, path, headers):
        request = (f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
                   + "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n")
        with socket.create_connection(server.server.server_address, timeout=5) as client:
            client.sendall(request.encode("ascii"))
            reply = b""
            # Content-Length bytes can arrive before request-handled is recorded. EOF follows
            # the handler's return and the server's shutdown_request, so it is the barrier.
            while chunk := client.recv(4096):
                reply += chunk
        head, body = reply.split(b"\r\n\r\n", 1)
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head)
        return body

    def test_disabled_leaves_the_handler_and_class_alone(self):
        with mock.patch.dict(os.environ, {artifacts.ARTIFACTS_ENV: ""}):
            server = harness.FixtureServer(_Page)
            self.addCleanup(server.stop)
            self.assertIs(server.server.RequestHandlerClass, _Page)
            self.assertIsNone(artifacts.FailureArtifacts.from_environment("x.Y"))

    def test_enabled_records_receive_and_handled_without_query_or_headers(self):
        _artifact_root(self)
        started = artifacts.now_ms()
        server = harness.FixtureServer(_Page)
        self.addCleanup(server.stop)
        self.assertEqual(self.request(server, "/page?secret=query-value",
                                      {"Authorization": "Bearer header-secret", "Cookie": "c=cookie-secret"}),
                         b"<p>page-body-text</p>")
        port = server.server.server_address[1]
        events = [e for e in artifacts.events_since(started) if e["source"] == "fixture" and e["port"] == port]
        self.assertEqual([e["kind"] for e in events], ["connection-accepted", "request-received", "request-handled"])
        received, handled = events[1], events[2]
        self.assertEqual((received["method"], received["path"]), ("GET", "/page"))
        self.assertEqual((handled["request"], handled["status"]), (received["request"], 200))
        self.assertNotIn("error", handled)
        self.assertLessEqual(received["t"], handled["t"])
        dumped = json.dumps(events)
        for secret in ("query-value", "header-secret", "cookie-secret", "page-body-text"):
            self.assertNotIn(secret, dumped)

    def test_handler_that_raises_after_its_200_is_recorded_as_failed_and_still_raises(self):
        _artifact_root(self)
        started = artifacts.now_ms()
        server = harness.FixtureServer(_AbortedReply)
        self.addCleanup(server.stop)
        handled_errors = []
        errored = threading.Event()

        def handle_error(request, client_address):
            handled_errors.append(sys.exc_info()[0])
            errored.set()

        server.server.handle_error = handle_error  # the server's own path for a raising handler
        with socket.create_connection(server.server.server_address, timeout=5) as client:
            client.sendall(b"GET /aborted?secret=query-value HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                           b"Authorization: Bearer header-secret\r\n\r\n")
            reply = b""
            while chunk := client.recv(4096):  # the server closes the connection after handle_error
                reply += chunk
        self.assertTrue(errored.wait(5))
        self.assertEqual(handled_errors, [BrokenPipeError])
        self.assertTrue(reply.startswith(b"HTTP/1.0 200"), reply)
        port = server.server.server_address[1]
        events = [e for e in artifacts.events_since(started) if e["source"] == "fixture" and e["port"] == port]
        self.assertEqual([e["kind"] for e in events], ["connection-accepted", "request-received", "request-failed"])
        received, failed = events[1], events[2]
        self.assertEqual((failed["request"], failed["status"], failed["error"]),
                         (received["request"], 200, "BrokenPipeError"))
        dumped = json.dumps(events)
        for secret in ("query-value", "header-secret", "synthetic abort detail"):
            self.assertNotIn(secret, dumped)


class EventLossTests(unittest.TestCase):
    """The process-wide harness/fixture buffer reports what it evicted."""

    def setUp(self):
        self.root = _artifact_root(self)
        for name, value in (("_events", collections.deque(maxlen=3)), ("_dropped_in_process", 0),
                            ("_watchers", weakref.WeakSet())):
            patcher = mock.patch.object(artifacts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, count):
        for _ in range(count):
            artifacts.record("harness", "synthetic")
            time.sleep(0.002)  # distinct timestamps on either side of a class start

    def summary(self, owner):
        with contextlib.redirect_stderr(io.StringIO()):
            target = owner.preserve("test_x", (None, None, {}))
        return json.loads((target / "summary.json").read_text())["harnessAndFixtureEvents"]

    def test_overflow_since_the_class_started_is_reported(self):
        owner = artifacts.FailureArtifacts(self.root, "suite.Overflow")
        self.record(5)
        self.assertEqual(self.summary(owner), {"limit": 3, "droppedSinceClassStart": 2, "complete": False,
                                               "droppedInProcess": 2})

    def test_evictions_from_before_the_class_started_are_not_its_loss(self):
        self.record(3)  # an earlier class's events fill the buffer
        time.sleep(0.002)
        owner = artifacts.FailureArtifacts(self.root, "suite.Later")
        self.record(3)  # evicts the three earlier events only
        self.assertEqual(self.summary(owner), {"limit": 3, "droppedSinceClassStart": 0, "complete": True,
                                               "droppedInProcess": 3})
        self.record(1)  # now one of this class's events goes
        self.assertEqual(self.summary(owner)["droppedSinceClassStart"], 1)

    def test_a_finished_class_stops_counting(self):
        owner = artifacts.FailureArtifacts(self.root, "suite.Done")
        owner.finish_class(setup_failed=False)
        self.record(5)
        self.assertEqual(owner.dropped_events, 0)


class _Process:
    """An owned stand-in process in its own session, like the harness's browser."""

    def __init__(self):
        self.popen = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.pid = self.popen.pid

    def poll(self):
        return self.popen.poll()

    def kill(self):
        self.popen.kill()
        self.popen.wait(timeout=10)

    @property
    def alive(self):
        return self.popen.poll() is None


class _FakeBridge:
    instances = []

    def __init__(self, home):
        self.home = home
        self.token = TOKEN
        self.env = {}
        self.ws_port = 40001
        self.process = None
        self.log_path = Path(home, "bridge-40000.log")
        self.log_path.write_text(f"bridge ready\nAuthorization: Bearer {TOKEN}\nHTTP POST /api/new-tab\n")
        _FakeBridge.instances.append(self)

    def start(self):
        self.process = _Process()

    def stop(self):
        if self.process:
            self.process.kill()


class _FakeBrowser:
    fail_start = False
    instances = []

    def __init__(self, binary, root, ws_port, prefs, scripts):
        self.root = Path(root)
        self.log_path = self.root / "browser.log"
        self.log_path.write_text("x" * (artifacts.MAX_LOG_BYTES + 10) + "\nbrowser tail line\n")
        self.process = None
        self.rdp_port = None
        self.stopped = False
        _FakeBrowser.instances.append(self)

    def start(self):
        if self.fail_start:
            raise AssertionError("synthetic browser launch failure")
        self.process = _Process()

    def stop(self):
        self.stopped = True
        if self.process:
            self.process.kill()


class HarnessRetentionTests(unittest.TestCase):
    """Runs RealBrowserTestCase subclasses on a fake bridge and browser."""

    def setUp(self):
        self.root = _artifact_root(self)
        _FakeBridge.instances.clear()
        _FakeBrowser.instances.clear()
        for name, fake in (("Bridge", _FakeBridge), ("RealBrowser", _FakeBrowser)):
            patcher = mock.patch.object(harness, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_suite(self, case):
        result = unittest.TestResult()
        with contextlib.redirect_stderr(io.StringIO()):
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def fake_case(self, fail_start=False, recorder_events=None):
        browser_events = {"events": recorder_events or [], "dropped": 0}

        class Case(harness.RealBrowserTestCase):
            __unittest_skip__ = False

            @classmethod
            def setUpClass(cls):
                _FakeBrowser.fail_start = fail_start
                super().setUpClass()

            @classmethod
            def wait_for_extension(cls, after_generation=0):
                return {}

            @classmethod
            def evaluate_for_artifacts(cls, script):
                return browser_events if script == artifacts.BROWSER_RECORDER_READ else {"installed": True}

            def test_passes(self):
                pass

            def test_zz_fails(self):
                artifacts.bridge_request_sent("/api/new-tab", {"url": "http://127.0.0.1:1/p?q=1", "wait": True,
                                                               "timeout": 15000, "text": "typed secret"})
                self.fail("synthetic failure")

        return Case

    def temp_root(self):
        return Path(_FakeBridge.instances[0].home).parent

    def test_failed_setup_keeps_its_logs_and_still_cleans_up(self):
        result = self.run_suite(self.fake_case(fail_start=True))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("synthetic browser launch failure", result.errors[0][1])
        self.assertFalse(self.temp_root().exists())
        self.assertTrue(_FakeBrowser.instances[0].stopped)
        self.assertFalse(_FakeBridge.instances[0].process.alive)
        [failure] = _failure_dirs(self.root)
        self.assertTrue(failure.name.endswith("-setUpClass"))
        self.assertEqual(sorted(p.name for p in failure.iterdir()),
                         ["bridge.log", "browser.log", "resources.json", "summary.json", "timeline.jsonl"])
        summary = json.loads((failure / "summary.json").read_text())
        self.assertEqual(summary["browserEvents"]["error"], "browser recorder not installed")
        browser_log = (failure / "browser.log").read_text()
        self.assertTrue(browser_log.endswith("browser tail line\n"))
        self.assertEqual(len(browser_log.encode()), artifacts.MAX_LOG_BYTES)
        self.assertTrue(summary["logs"]["browser.log"]["truncated"])
        bridge_log = (failure / "bridge.log").read_text()
        self.assertNotIn(TOKEN, bridge_log)
        self.assertIn("HTTP POST /api/new-tab", bridge_log)

    def test_passing_class_writes_nothing(self):
        case = self.fake_case()
        del case.test_zz_fails
        result = self.run_suite(case)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(self.temp_root().exists())

    def test_failed_test_keeps_a_merged_timeline_from_the_live_browser(self):
        browser_events = [{"t": artifacts.now_ms(), "kind": "state", "browser": 7, "flags": ["STATE_STOP"],
                           "status": 0, "url": "http://127.0.0.1:1/p?private=1"}]
        result = self.run_suite(self.fake_case(recorder_events=browser_events))
        self.assertEqual((len(result.failures), len(result.errors)), (1, 0))
        self.assertFalse(self.temp_root().exists())
        [failure] = _failure_dirs(self.root)
        self.assertTrue(failure.name.startswith("01-") and failure.name.endswith("test_zz_fails"))
        timeline = [json.loads(line) for line in (failure / "timeline.jsonl").read_text().splitlines()]
        self.assertEqual(timeline, sorted(timeline, key=lambda event: event["t"]))
        sent = next(e for e in timeline if e["kind"] == "bridge-request-sent")
        self.assertEqual({k: sent[k] for k in ("path", "url", "wait", "timeout")},
                         {"path": "/api/new-tab", "url": "http://127.0.0.1:1/p", "wait": True, "timeout": 15000})
        self.assertNotIn("text", sent)
        browser = next(e for e in timeline if e["source"] == "browser")
        self.assertEqual((browser["url"], browser["flags"]), ("http://127.0.0.1:1/p", ["STATE_STOP"]))
        resources = json.loads((failure / "resources.json").read_text())
        self.assertEqual([sample["label"] for sample in resources], ["browser-started", "failure"])
        owned = {os.getpid(): "test-runner", _FakeBridge.instances[0].process.pid: "bridge",
                 _FakeBrowser.instances[0].process.pid: "browser-group"}
        for sample in resources:
            self.assertEqual({row["pid"]: row["role"] for row in sample["processes"]}, owned)
        self.assertFalse(_FakeBrowser.instances[0].process.alive)
        self.assertEqual(_FakeBridge.instances[0].env["PYTHONUNBUFFERED"], "1")

    def test_each_class_keeps_only_its_first_failures(self):
        owner = artifacts.FailureArtifacts(self.root, "suite.Class")
        with contextlib.redirect_stderr(io.StringIO()):
            written = [owner.preserve(f"test_{i}", (None, None, {}))
                       for i in range(artifacts.MAX_PRESERVED_FAILURES_PER_CLASS + 2)]
        self.assertEqual(written[-2:], [None, None])
        self.assertEqual(owner.omitted, 2)
        self.assertEqual([path.name for path in _failure_dirs(self.root)],
                         [f"{i + 1:02d}-test_{i}" for i in range(artifacts.MAX_PRESERVED_FAILURES_PER_CLASS)])

    def test_unwritable_artifact_root_is_reported_not_raised(self):
        root = self.root / "a-file"
        root.write_text("")
        owner = artifacts.FailureArtifacts(root, "suite.Class")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(owner.preserve("test_x", (None, None, {})))
        self.assertIn("not written", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
