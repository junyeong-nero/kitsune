"""Action races, tab unloads and reconnects in a real browser (issue #68).

Opt-in: KITSUNE_REAL_BROWSER=1 uv run python -m unittest discover -s tests -p test_real_browser.py -v
Set KITSUNE_REAL_BROWSER_BINARY to choose the browser executable; otherwise the first
Firefox, Zen or LibreWolf found is used. The run takes about a minute.

Everything is owned by the test: a headless browser on a new temporary profile and HOME,
a real bridge on spare ports (test_bridge_security.Bridge), and a local fixture site. The
extension is copied to a temporary directory with its bridge URL pointed at the owned
bridge, so the normal bridge on 47652 is never contacted. A test-only background script
added to the copy connects to a harness socket to discard tabs and drop the bridge
WebSocket, and the copy is installed as a temporary add-on over the browser's remote
debugging protocol. Fixture buttons report every click to the fixture site, which is
how the tests prove that no action reached the wrong page or ran twice.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from test_bridge_security import Bridge, _free_port, _request

REPO = Path(__file__).resolve().parent.parent
EXTENSION_ID = "kitsune@junyeong-nero.github.io"
DEFAULT_WS_URL = "ws://127.0.0.1:47652"
BROWSER_CANDIDATES = (
    "/Applications/Firefox.app/Contents/MacOS/firefox",
    "/Applications/Zen.app/Contents/MacOS/zen",
    "/Applications/LibreWolf.app/Contents/MacOS/librewolf",
    "firefox", "zen", "librewolf",
)
START_TIMEOUT = 60
# The batch (`retry`) bridge timeout for a click; a page held longer makes the bridge give up.
BATCH_COMMAND_TIMEOUT = 30
PROFILE_PREFS = {
    "devtools.debugger.remote-enabled": True,
    "devtools.chrome.enabled": True,
    "devtools.debugger.prompt-connection": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.startup.page": 0,
    "browser.sessionstore.resume_from_crash": False,
    "browser.tabs.unloadOnLowMemory": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.update.auto": False,
    "app.update.enabled": False,
    "extensions.update.enabled": False,
}

# Test-only background script: harness commands use the public tabs API, and `ws` is the
# bridge socket declared by background.js in the same background page.
CONTROL_SCRIPT = """'use strict';
(function connectControl() {
  const control = new WebSocket('ws://127.0.0.1:%d');
  control.onmessage = async event => {
    const { id, op, tabId } = JSON.parse(event.data);
    let result;
    try {
      if (op === 'discard') {
        await browser.tabs.discard(tabId);
        result = { discarded: (await browser.tabs.get(tabId)).discarded };
      } else if (op === 'dropBridge') {
        ws.close();
        result = { dropped: true };
      } else {
        result = { error: 'unknown op ' + op };
      }
    } catch (e) {
      result = { error: e.message };
    }
    control.send(JSON.stringify({ id, result }));
  };
  control.onclose = () => setTimeout(connectControl, 500);
})();
"""

# Every fixture button reports its click to /hit, keyed by page name.
PAGES = {
    "buttons": """<button id="act" onclick="fetch('/hit?page={name}')">Act {name}</button>
<a id="slow" href="/slow?name={name}-next&amp;ms={ms}">Slow next</a>""",
    "delayed": """<div id="slot">Loading</div>
<script>setTimeout(() => {{ slot.innerHTML =
  '<button id="late" onclick="fetch(\\'/hit?page={name}\\')">Late {name}</button>'; }}, {ms});</script>""",
    "form": """<form id="f" onsubmit="fetch('/hit?page={name}'); return false">
<span>v1</span><input id="email" name="email" aria-label="Email"></form>
<script>setTimeout(() => {{ f.innerHTML =
  '<span>v2</span><input id="email" name="email" aria-label="Email">'; }}, {ms});</script>""",
    # A synchronous request blocks the page, so the click's reply only arrives after `ms`.
    "hold": """<button id="act" onclick="const x = new XMLHttpRequest();
  x.open('GET', '/hold?page={name}&amp;ms={ms}', false); x.send()">Hold {name}</button>""",
}


def find_browser():
    configured = os.environ.get("KITSUNE_REAL_BROWSER_BINARY")
    for candidate in ([configured] if configured else BROWSER_CANDIDATES):
        path = shutil.which(candidate)
        if path:
            return path
    return None


class FixtureSite:
    """A local HTTP site whose pages report button clicks back to it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path in ("/hit", "/hold"):
                    with site.lock:
                        site.hits.append(query["page"])
                    if url.path == "/hold":
                        time.sleep(int(query["ms"]) / 1000)
                    self.reply("ok", "text/plain")
                elif url.path in ("/page", "/slow"):
                    if url.path == "/slow":
                        time.sleep(int(query["ms"]) / 1000)
                    kind = query.get("kind", "buttons")
                    body = PAGES[kind].format(name=query["name"], ms=query.get("ms", 4000))
                    self.reply(f"<!doctype html><title>{query['name']}</title>{body}", "text/html")
                else:
                    self.send_error(404)

            def reply(self, text, content_type):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name, kind="buttons", ms=None):
        return f"{self.origin}/page?name={name}&kind={kind}" + (f"&ms={ms}" if ms is not None else "")

    def count(self, page):
        with self.lock:
            return self.hits.count(page)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ExtensionControl:
    """Harness side of CONTROL_SCRIPT; the latest background page connection wins."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ws = None
        self.replies = {}
        self.counter = 0
        self.connected = threading.Condition(self.lock)
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, ws):
        with self.connected:
            self.ws = ws
            self.connected.notify_all()
        try:
            for message in ws:
                reply = json.loads(message)
                with self.connected:
                    self.replies[reply["id"]] = reply["result"]
                    self.connected.notify_all()
        except ConnectionClosed:
            pass

    def call(self, op, **params):
        with self.connected:
            if not self.connected.wait_for(lambda: self.ws is not None, timeout=10):
                raise AssertionError("extension control script did not connect")
            self.counter += 1
            command_id = self.counter
            ws = self.ws
        ws.send(json.dumps({"id": command_id, "op": op, **params}))
        with self.connected:
            if not self.connected.wait_for(lambda: command_id in self.replies, timeout=10):
                raise AssertionError(f"extension control op {op} did not answer")
            return self.replies.pop(command_id)

    def stop(self):
        self.server.shutdown()


class RemoteDebugger:
    """Minimal Firefox remote debugging protocol client: length-prefixed JSON packets."""

    def __init__(self, port):
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        self.buffer = b""
        self._read()  # root greeting

    def _read(self):
        while b":" not in self.buffer:
            self._recv()
        length, rest = self.buffer.split(b":", 1)
        self.buffer = rest
        while len(self.buffer) < int(length):
            self._recv()
        packet, self.buffer = self.buffer[:int(length)], self.buffer[int(length):]
        return json.loads(packet)

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("remote debugger closed the connection")
        self.buffer += chunk

    def request(self, to, packet_type, **params):
        data = json.dumps({"to": to, "type": packet_type, **params}).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        while True:
            reply = self._read()
            if reply.get("from") == to:
                return reply

    def install_temporary_addon(self, path):
        addons = self.request("root", "getRoot")["addonsActor"]
        reply = self.request(addons, "installTemporaryAddon", addonPath=str(path))
        if reply.get("addon", {}).get("id") != EXTENSION_ID:
            raise AssertionError(f"temporary add-on install failed: {reply}")

    def close(self):
        self.sock.close()


class RealBrowser:
    """A headless browser on a temporary profile, running the extension against one bridge."""

    def __init__(self, binary, root, ws_port, control_port):
        self.binary = binary
        self.root = Path(root)
        self.extension = self.root / "extension"
        self.profile = self.root / "profile"
        self.home = self.root / "home"
        self.log_path = self.root / "browser.log"
        self.process = None
        shutil.copytree(REPO / "extension", self.extension)
        background = self.extension / "background" / "background.js"
        source = background.read_text(encoding="utf-8")
        if source.count(DEFAULT_WS_URL) != 1:
            raise AssertionError("background.js no longer declares the bridge URL once")
        background.write_text(source.replace(DEFAULT_WS_URL, f"ws://127.0.0.1:{ws_port}"), encoding="utf-8")
        (self.extension / "background" / "test-control.js").write_text(CONTROL_SCRIPT % control_port,
                                                                       encoding="utf-8")
        manifest_path = self.extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["background"]["scripts"].append("background/test-control.js")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.profile.mkdir()
        self.home.mkdir()
        (self.profile / "user.js").write_text(
            "".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in PROFILE_PREFS.items()), encoding="utf-8")

    def start(self):
        rdp_port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KITSUNE_", "MOZ_"))}
        env.update(HOME=str(self.home), MOZ_NO_REMOTE="1", MOZ_HEADLESS="1")
        with open(self.log_path, "ab") as log:
            # Its own session, so stop() can kill the browser with all of its child processes.
            self.process = subprocess.Popen(
                [self.binary, "--headless", "-no-remote", "-new-instance", "-profile", str(self.profile),
                 "-start-debugger-server", str(rdp_port), "about:blank"],
                env=env, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        debugger = RemoteDebugger(rdp_port)
        try:
            debugger.install_temporary_addon(self.extension)
        finally:
            debugger.close()
        return self

    def log(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=10)


def refs_named(snapshot, text):
    """Refs of snapshot lines whose accessible name contains text."""
    return re.findall(r'"[^"\n]*' + re.escape(text) + r'[^"\n]*" \[ref=(s\d+e\d+)\]', snapshot)


@unittest.skipUnless(os.environ.get("KITSUNE_REAL_BROWSER") == "1",
                     "real-browser tests are opt-in: set KITSUNE_REAL_BROWSER=1")
class RealBrowserActionRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = find_browser()
        if binary is None:
            raise unittest.SkipTest("no Firefox, Zen or LibreWolf found; set KITSUNE_REAL_BROWSER_BINARY")
        temp = tempfile.TemporaryDirectory(prefix="kitsune-real-browser-")
        cls.addClassCleanup(temp.cleanup)
        home = Path(temp.name, "bridge")
        home.mkdir()
        cls.bridge = Bridge(str(home)).start()
        cls.addClassCleanup(cls.bridge.stop)
        cls.site = FixtureSite()
        cls.addClassCleanup(cls.site.stop)
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        cls.browser = RealBrowser(binary, temp.name, cls.bridge.ws_port, cls.control.port)
        cls.addClassCleanup(cls.browser.stop)
        cls.browser.start()
        cls.wait_for_extension()

    @classmethod
    def wait_for_extension(cls, after_generation=0):
        # The first connection after a temporary install can drop at once; wait for one that answers.
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            status = cls.bridge.status()
            if status["extension_connected"] and status["extension_generation"] > after_generation:
                tabs = _request(cls.bridge.http_port, "GET", "/api/tabs", cls.bridge.auth(), cls.bridge.host)
                if tabs[0] == 200 and "tabs" in json.loads(tabs[2]):
                    return status
            time.sleep(0.1)
        raise AssertionError("extension did not connect to the owned bridge:\n"
                             + cls.bridge.log() + cls.browser.log())

    def call(self, path, body):
        headers = self.bridge.auth()
        headers["Content-Type"] = "application/json"
        # The bridge answers every action within its own timeout; allow for that here.
        status, _, text = _request(self.bridge.http_port, "POST", path, headers, self.bridge.host, json.dumps(body),
                                   timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def open_tab(self, url):
        tab = self.assert_ok(self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 15000}))
        self.addCleanup(self.call, "/api/close-tab", {"tabId": tab["tabId"]})
        return tab["tabId"]

    def snapshot_ref(self, tab_id, text):
        snapshot = self.call("/api/snapshot", {"tabId": tab_id})
        refs = refs_named(snapshot.get("snapshot", ""), text)
        self.assertEqual(len(refs), 1, snapshot)
        return refs[0]

    def assert_ok(self, result):
        self.assertTrue(result.get("ok"), result)
        return result

    def assert_error(self, result, code, retryable):
        self.assertEqual(result.get("code"), code, result)
        self.assertIs(result.get("retryable"), retryable, result)
        self.assertIn("error", result)

    def assert_hits(self, page, expected, settle=0.5):
        """Hits are async page requests: wait until the expected count, then confirm it holds."""
        deadline = time.monotonic() + 5
        while self.site.count(page) < expected and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(settle)
        self.assertEqual(self.site.count(page), expected, f"clicks reported by {page}: {self.site.hits}")

    def wait_until_scriptable(self, tab_id):
        """Poll a read until the tab's page answers; every miss must be a retryable navigating."""
        deadline = time.monotonic() + 10
        while True:
            info = self.call("/api/page-info", {"tabId": tab_id})
            if "error" not in info:
                return
            self.assert_error(info, "navigating", True)
            self.assertLess(time.monotonic(), deadline, info)
            time.sleep(0.1)

    def in_background(self, path, body):
        """Send a bridge call from another thread; join() returns its result."""
        result = {}
        thread = threading.Thread(target=lambda: result.update(value=self.call(path, body)), daemon=True)
        thread.start()

        def join():
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), f"{path} did not return")
            return result["value"]
        return join

    # ── Navigation and focus races ──

    def test_ref_after_navigation_fails_as_other_document_without_clicking_the_new_page(self):
        tab_id = self.open_tab(self.site.url("nav-a"))
        ref = self.snapshot_ref(tab_id, "Act nav-a")
        self.assert_ok(self.call("/api/navigate", {"url": self.site.url("nav-b"), "tabId": tab_id}))

        result = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        self.assert_error(result, "stale_ref", True)
        self.assertEqual(result["state"]["reason"], "other_document")
        self.assert_hits("nav-b", 0)
        self.assert_hits("nav-a", 0, settle=0)

    def test_missing_target_while_the_next_document_loads_reports_navigating(self):
        tab_id = self.open_tab(self.site.url("load-a", ms=4000))
        link = self.snapshot_ref(tab_id, "Slow next")
        self.assert_ok(self.call("/api/click", {"ref": link, "tabId": tab_id, "includeSnapshot": False}))
        # The old document stays until the slow response arrives, so the target looks gone mid-navigation.
        result = self.call("/api/click", {"selector": "#missing", "tabId": tab_id})
        self.assert_error(result, "navigating", True)
        self.assertEqual(result["state"]["cause"], "not_found")
        self.assertEqual(result["state"]["status"], "loading")
        self.assert_ok(self.call("/api/wait-for-url", {"tabId": tab_id, "pattern": "load-a-next", "timeout": 10000}))
        self.assert_hits("load-a", 0)

    def test_ref_from_another_tab_after_a_focus_change_never_acts_on_the_focused_tab(self):
        first = self.open_tab(self.site.url("focus-a"))
        second = self.open_tab(self.site.url("focus-b"))
        self.assert_ok(self.call("/api/switch-tab", {"tabId": first}))
        ref = self.snapshot_ref(first, "Act focus-a")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": second}))

        for body in ({"ref": ref}, {"ref": ref, "tabId": second}):
            with self.subTest(body=body):
                result = self.call("/api/click", body)
                self.assert_error(result, "stale_ref", True)
                self.assertEqual(result["state"]["reason"], "other_document")
        self.assert_hits("focus-b", 0)
        self.assert_hits("focus-a", 0, settle=0)

        self.assert_ok(self.call("/api/click", {"ref": ref, "tabId": first}))
        self.assert_hits("focus-a", 1)
        self.assert_hits("focus-b", 0, settle=0)

    # ── Dynamic forms and delayed content ──

    def test_rerendered_form_field_ref_fails_as_element_removed(self):
        tab_id = self.open_tab(self.site.url("form-a", kind="form", ms=1500))
        ref = self.snapshot_ref(tab_id, "Email")
        self.assert_ok(self.call("/api/wait-for-text", {"tabId": tab_id, "text": "v2", "timeout": 10000}))

        result = self.call("/api/type", {"ref": ref, "text": "agent@example.test", "tabId": tab_id})
        self.assert_error(result, "stale_ref", True)
        self.assertEqual(result["state"]["reason"], "element_removed")
        value = "document.querySelector('#email').value"
        self.assertEqual(self.call("/api/js", {"code": value, "tabId": tab_id})["result"], "")

        self.assert_ok(self.call("/api/type", {"ref": self.snapshot_ref(tab_id, "Email"),
                                        "text": "agent@example.test", "tabId": tab_id}))
        self.assertEqual(self.call("/api/js", {"code": value, "tabId": tab_id})["result"], "agent@example.test")
        self.assert_hits("form-a", 0)

    def test_delayed_button_is_not_found_until_the_actionability_wait_sees_it(self):
        tab_id = self.open_tab(self.site.url("late-a", kind="delayed", ms=1500))
        result = self.call("/api/click", {"selector": "#late", "tabId": tab_id})
        self.assert_error(result, "not_found", True)
        short = self.call("/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 100})
        self.assert_error(short, "not_found", True)
        self.assertEqual(short["state"]["timeoutMs"], 100)
        self.assert_hits("late-a", 0, settle=0)

        self.assert_ok(self.call("/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 5000}))
        self.assert_hits("late-a", 1)

    # ── Tab closes and unloads ──

    def test_closed_tab_reports_tab_closed(self):
        tab_id = self.call("/api/new-tab", {"url": self.site.url("closed-a"), "wait": True})["tabId"]
        self.assert_ok(self.call("/api/close-tab", {"tabId": tab_id}))
        result = self.call("/api/click", {"selector": "#act", "tabId": tab_id})
        self.assert_error(result, "tab_closed", True)
        self.assertEqual(result["state"]["tabId"], tab_id)
        self.assert_hits("closed-a", 0)

    def test_discarded_tab_reports_tab_unloaded_until_woken(self):
        tab_id = self.open_tab(self.site.url("unload-a"))
        self.open_tab(self.site.url("unload-other"))  # a tab must be inactive to be discarded
        self.assertEqual(self.control.call("discard", tabId=tab_id), {"discarded": True})

        result = self.call("/api/click", {"selector": "#act", "tabId": tab_id})
        self.assert_error(result, "tab_unloaded", True)
        self.assertIs(result["state"]["discarded"], True)
        self.assert_hits("unload-a", 0)

        woken = self.call("/api/wake-tab", {"tabId": tab_id})
        self.assertEqual((woken.get("ok"), woken.get("woken")), (True, True), woken)
        # wakeTab reports ready before the reload has replaced the discarded document, so the
        # first commands can find no page script; they must fail before dispatch as navigating.
        early = self.call("/api/click", {"selector": "#act", "tabId": tab_id})
        if not early.get("ok"):
            self.assert_error(early, "navigating", True)
            self.assert_hits("unload-a", 0, settle=0)
            self.wait_until_scriptable(tab_id)
            self.assert_ok(self.call("/api/click", {"selector": "#act", "tabId": tab_id}))
        self.assert_hits("unload-a", 1)
        self.assert_hits("unload-other", 0, settle=0)

    # ── Extension reconnects and unknown outcomes ──

    def drop_bridge_during(self, path, body, page):
        """Drop the extension's bridge socket while a write waits in the page for its target."""
        generation = self.bridge.status()["extension_generation"]
        join = self.in_background(path, body)
        time.sleep(1)  # the command is in the page, waiting for the delayed button
        self.assert_hits(page, 0, settle=0)
        self.assertEqual(self.control.call("dropBridge"), {"dropped": True})
        result = join()
        self.wait_for_extension(after_generation=generation)
        return result

    def test_write_in_flight_during_disconnect_is_not_retryable_and_runs_once(self):
        tab_id = self.open_tab(self.site.url("drop-a", kind="delayed", ms=3000))
        result = self.drop_bridge_during(
            "/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 8000}, "drop-a")
        self.assert_error(result, "extension_disconnected", False)
        # The outcome really was unknown: the page still runs the click it had received.
        self.assert_hits("drop-a", 1, settle=3)

    def test_retry_stops_after_an_unknown_outcome_instead_of_repeating_the_write(self):
        tab_id = self.open_tab(self.site.url("drop-retry", kind="delayed", ms=3000))
        command = {"action": "click", "selector": "#late", "tabId": tab_id, "wait": True, "timeout": 8000}
        result = self.drop_bridge_during(
            "/api/retry", {"command": command, "maxAttempts": 3, "backoffMs": 0,
                           "onErrors": ["Extension disconnected"]}, "drop-retry")
        self.assertEqual((result["ok"], result["attempts"], result["reason"]), (False, 1, "not retryable"), result)
        self.assert_error(result["result"], "extension_disconnected", False)
        self.assert_hits("drop-retry", 1, settle=3)

    def test_timed_out_write_is_not_retryable_and_retry_does_not_repeat_it(self):
        # Holds the page past the bridge timeout; this test alone takes about 30 seconds.
        hold_ms = (BATCH_COMMAND_TIMEOUT + 3) * 1000
        tab_id = self.open_tab(self.site.url("hold-a", kind="hold", ms=hold_ms))
        command = {"action": "click", "selector": "#act", "tabId": tab_id, "includeSnapshot": False}
        result = self.call("/api/retry", {"command": command, "maxAttempts": 3, "backoffMs": 0})
        self.assertEqual((result["ok"], result["attempts"], result["reason"]), (False, 1, "not retryable"), result)
        self.assert_error(result["result"], "timeout", False)
        self.assertEqual(result["result"]["state"], {"timeoutSeconds": BATCH_COMMAND_TIMEOUT})
        self.assert_hits("hold-a", 1, settle=4)

    def test_extension_reload_disconnects_then_old_refs_are_stale(self):
        tab_id = self.open_tab(self.site.url("reload-a"))
        ref = self.snapshot_ref(tab_id, "Act reload-a")
        generation = self.bridge.status()["extension_generation"]
        self.assert_ok(self.call("/api/reload-extension", {}))
        deadline = time.monotonic() + 10
        while self.bridge.status()["extension_connected"] and time.monotonic() < deadline:
            time.sleep(0.01)

        result = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        # Nothing was sent, so even a write is safe to repeat.
        self.assert_error(result, "extension_disconnected", True)
        self.assertEqual(result["state"], {"connected": False})

        self.wait_for_extension(after_generation=generation)
        # The reloaded extension's content script cannot resolve refs issued before the reload.
        stale = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        self.assert_error(stale, "stale_ref", True)
        self.assert_hits("reload-a", 0)
        self.assert_ok(self.call("/api/click", {"ref": self.snapshot_ref(tab_id, "Act reload-a"),
                                                 "tabId": tab_id}))
        self.assert_hits("reload-a", 1)


if __name__ == "__main__":
    unittest.main()
