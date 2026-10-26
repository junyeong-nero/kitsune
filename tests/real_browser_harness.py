"""Shared harness for the opt-in real-browser suites (tests/test_real_browser*.py).

Opt-in: every suite is skipped unless KITSUNE_E2E_BROWSER names a Firefox, Zen or
LibreWolf binary. Run all of them (a little over a minute) with:
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
    uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v

API, kept small so suites can rely on it:
- RealBrowserTestCase: base class for a suite, skipped without KITSUNE_E2E_BROWSER.
  setUpClass gives each class a temporary directory holding the browser profile, the
  browser's HOME and the bridge's HOME/KITSUNE_HOME; a real bridge on spare ports
  (cls.bridge, test_bridge_security.Bridge); and a headless browser running a copy of the
  extension whose bridge URL points at that bridge (cls.browser). The bridge's
  KITSUNE_EXTENSION_DIR names that copy, so its developer-build check compares the
  extension with the files the browser loaded. Class cleanups are
  registered before anything starts, so they run even if setUpClass fails: they kill
  the browser's whole process group, stop the bridge and delete the directory.
  Hooks:
    PROFILE_PREFS          prefs added to BASE_PREFS. The default {} keeps the browser's
                           own defaults, including its popup blocker; POPUPS_ALLOWED
                           turns the blocker off.
    extension_scripts()    {file name: source} added to the copy as background scripts
                           after background.js; override it to drive the browser from a test.
  Helpers: call(path, body=None) for bridge requests (GET without a body),
  wait_for_extension(after_generation=0), open_tab(url), assert_ok(result) and
  assert_error(result, code, retryable).
- FixtureServer(handler_class): a local HTTP server on a spare port with .origin and .stop().
- RemoteDebugger(cls.browser.rdp_port).evaluate_in_parent(script): runs a privileged
  script (a JS expression) in the browser's parent process and returns its awaited,
  JSON-serializable result, e.g. to seed the temporary profile's password manager with a
  disposable login. A throw or a rejected promise raises AssertionError with its message;
  so does a remote debugging request that answers with an error.
- check_extension_copy(extension, ws_port): the launch guard below; RealBrowser.start()
  runs it before launching anything.
- BROWSER, BASE_PREFS, POPUPS_ALLOWED.

Failure artifacts (issue #293, diagnostics only): with KITSUNE_E2E_ARTIFACTS set to a
directory, a failing test or setUpClass keeps its class's browser and bridge log tails, a
timeline of bridge requests, fixture request receive/finish times and the browser's
top-level navigation events, and owned-process resource samples. Unset, nothing is
recorded and fixture handlers are used unchanged. real_browser_failure_artifacts.py
documents what is kept and what is redacted. Failures are detected through the unittest
runner's result; the class cleanups copy the evidence out before the directory is deleted.
Only bridge requests made through call() (open_tab included) are in the timeline; a suite
that sends harness._request directly bypasses it, and only the bridge log lists those.

Safety: the browser never sees a user profile or the user's bridge. Its environment
drops KITSUNE_* and MOZ_* variables and sets HOME to the temporary directory. Right
before launch, RealBrowser refuses to start if it would load the repository's extension
directory, if any .js file in the copy still contains the default bridge port 47652, or
if the test bridge uses that port.

Install method: the copy is installed as a temporary add-on over Firefox's remote
debugging protocol (-start-debugger-server, then the addons actor's
installTemporaryAddon). WebDriver BiDi's webExtension.install needs
--remote-debugging-port, which starts the Remote Agent. The Remote Agent applies its
"recommended preferences" over the profile, and they include
dom.disable_open_during_load=false, so a BiDi profile only keeps the default popup
blocker if it also turns off remote.prefs.recommended. The remote debugging protocol
leaves prefs as user.js sets them.
"""
import atexit
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import util

import real_browser_failure_artifacts
from test_bridge_security import Bridge, _free_port, _request

REPO = Path(__file__).resolve().parent.parent
EXTENSION_ID = "kitsune@junyeong-nero.github.io"
DEFAULT_WS_URL = "ws://127.0.0.1:47652"
DEFAULT_WS_PORT = "47652"
BROWSER = os.environ.get("KITSUNE_E2E_BROWSER")
START_TIMEOUT = 60
# Needed by the harness or to keep runs quiet and stable; none of them changes page behaviour.
BASE_PREFS = {
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
# The popup blocker drops a tab opened by a synthetic click; benchmark profiles allow popups.
POPUPS_ALLOWED = {"dom.disable_open_during_load": False}


class FixtureServer:
    """A local HTTP server on a spare port, answering with one BaseHTTPRequestHandler class."""

    def __init__(self, handler_class):
        if real_browser_failure_artifacts.enabled():
            handler_class = real_browser_failure_artifacts.recorded_handler(handler_class)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RemoteDebugger:
    """Minimal Firefox remote debugging protocol client: length-prefixed JSON packets."""

    def __init__(self, port, connect_timeout=START_TIMEOUT):
        deadline = time.monotonic() + connect_timeout
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
                if "error" in reply:
                    raise AssertionError(f"{packet_type} to {to} failed: {reply['error']}: {reply.get('message')}")
                return reply

    def evaluate_in_parent(self, script):
        """Run script, a JS expression, with chrome privileges in the parent process.

        Returns its awaited result, which must be JSON-serializable. Raises AssertionError
        with the exception message if the script throws or its promise rejects.
        """
        process = self.request("root", "getProcess", id=0)["processDescriptor"]["actor"]
        console = self.request(process, "getTarget")["process"]["consoleActor"]
        # The console reports a rejected top-level promise only as topLevelAwaitRejected, without
        # its reason, so the wrapper catches the reason and returns it in band.
        wrapped = ("(async () => { try { return JSON.stringify({value: await (\n" + script + "\n)}); }"
                   " catch (e) { return JSON.stringify({error: String(e && e.message || e),"
                   " stack: String(e && e.stack || '')}); } })()")
        result_id = self.request(console, "evaluateJSAsync", text=wrapped, mapped={"await": True})["resultID"]
        while True:
            reply = self._read()
            if reply.get("type") == "evaluationResult" and reply.get("resultID") == result_id:
                break
        if reply.get("hasException") or reply.get("topLevelAwaitRejected") or not isinstance(reply.get("result"), str):
            message = reply.get("exceptionMessage") or reply.get("exception", {}).get("preview", {}).get("message")
            raise AssertionError(f"parent process script failed: {message or reply}")
        outcome = json.loads(reply["result"])
        if "error" in outcome:
            raise AssertionError(f"parent process script failed: {outcome['error']}\n{outcome['stack']}")
        return outcome.get("value")

    def install_temporary_addon(self, path):
        addons = self.request("root", "getRoot")["addonsActor"]
        reply = self.request(addons, "installTemporaryAddon", addonPath=str(path))
        if reply.get("addon", {}).get("id") != EXTENSION_ID:
            raise AssertionError(f"temporary add-on install failed: {reply}")

    def close(self):
        self.sock.close()


def check_extension_copy(extension, ws_port):
    """Refuse a copy that could reach the default bridge; raises AssertionError."""
    if str(ws_port) == DEFAULT_WS_PORT:
        raise AssertionError("the test bridge must not use the default bridge port")
    if Path(extension).resolve() == (REPO / "extension").resolve():
        raise AssertionError("refusing to load the unmodified extension")
    for path in Path(extension).rglob("*.js"):
        if DEFAULT_WS_PORT in path.read_text(encoding="utf-8"):
            raise AssertionError(f"{path} still names the default bridge port")


class RealBrowser:
    """A headless browser on a temporary profile, running the extension against one bridge."""

    def __init__(self, binary, root, ws_port, prefs, scripts):
        self.binary = binary
        self.root = Path(root)
        self.ws_port = ws_port
        self.extension = self.root / "extension"
        self.profile = self.root / "profile"
        self.home = self.root / "home"
        self.log_path = self.root / "browser.log"
        self.process = None
        self.rdp_port = None
        shutil.copytree(REPO / "extension", self.extension)
        background = self.extension / "background" / "background.js"
        source = background.read_text(encoding="utf-8")
        if source.count(DEFAULT_WS_URL) != 1:
            raise AssertionError("background.js no longer declares the bridge URL once")
        background.write_text(source.replace(DEFAULT_WS_URL, f"ws://127.0.0.1:{ws_port}"), encoding="utf-8")
        manifest_path = self.extension / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, script in scripts.items():
            (self.extension / "background" / name).write_text(script, encoding="utf-8")
            manifest["background"]["scripts"].append(f"background/{name}")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.profile.mkdir()
        self.home.mkdir()
        (self.profile / "user.js").write_text(
            "".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in {**BASE_PREFS, **prefs}.items()),
            encoding="utf-8")

    def start(self):
        check_extension_copy(self.extension, self.ws_port)
        self.rdp_port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KITSUNE_", "MOZ_"))}
        env.update(HOME=str(self.home), MOZ_NO_REMOTE="1", MOZ_HEADLESS="1")
        # Also covers runs that are interrupted before the class cleanups.
        atexit.register(self.stop)
        with open(self.log_path, "ab") as log:
            # Its own session, so stop() can kill the browser with all of its child processes.
            self.process = subprocess.Popen(
                [self.binary, "--headless", "-no-remote", "-new-instance", "-profile", str(self.profile),
                 "-start-debugger-server", str(self.rdp_port), "about:blank"],
                env=env, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        debugger = RemoteDebugger(self.rdp_port)
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


@unittest.skipUnless(BROWSER, "set KITSUNE_E2E_BROWSER to a Firefox, Zen or LibreWolf binary")
class RealBrowserTestCase(unittest.TestCase):
    """One owned bridge and headless browser per test class; see the module docstring."""

    PROFILE_PREFS = {}
    failure_artifacts = None

    @classmethod
    def extension_scripts(cls):
        return {}

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="kitsune-real-browser-")
        cls.addClassCleanup(temp.cleanup)
        artifacts = real_browser_failure_artifacts.FailureArtifacts.from_environment(util.strclass(cls))
        cls.failure_artifacts = artifacts
        if artifacts:
            # Runs after the browser and bridge stop and before the temporary directory is deleted.
            cls.addClassCleanup(lambda: artifacts.finish_class(cls._setup_failed()))
        home = Path(temp.name, "bridge")
        home.mkdir()
        cls.bridge = Bridge(str(home))
        # Compare the loaded build with the copy the browser runs, not the repository's extension/.
        cls.bridge.env["KITSUNE_EXTENSION_DIR"] = str(Path(temp.name, "extension"))
        cls.addClassCleanup(cls.bridge.stop)
        if artifacts:
            artifacts.watch_bridge(cls.bridge)
        cls.bridge.start()
        cls.browser = RealBrowser(BROWSER, temp.name, cls.bridge.ws_port, cls.PROFILE_PREFS,
                                  cls.extension_scripts())
        cls.addClassCleanup(cls.browser.stop)
        if artifacts:
            artifacts.watch_browser(cls.browser)
            # Runs before the browser stops, so a failed setUpClass keeps the live browser's events.
            cls.addClassCleanup(lambda: artifacts.capture_setup_snapshot(cls._setup_failed()))
        cls.browser.start()
        if artifacts:
            artifacts.install_browser_recorder(
                lambda: cls.evaluate_for_artifacts(real_browser_failure_artifacts.BROWSER_RECORDER_INSTALL),
                lambda: cls.evaluate_for_artifacts(real_browser_failure_artifacts.BROWSER_RECORDER_READ))
        cls.wait_for_extension()

    @classmethod
    def _setup_failed(cls):
        # unittest sets this before it runs the class cleanups of a setUpClass that raised.
        return getattr(cls, "_classSetupFailed", False)

    @classmethod
    def evaluate_for_artifacts(cls, script):
        debugger = RemoteDebugger(cls.browser.rdp_port, connect_timeout=5)
        try:
            return debugger.evaluate_in_parent(script)
        finally:
            debugger.close()

    def run(self, result=None):
        # With KITSUNE_E2E_ARTIFACTS set, a test that adds a failure or error preserves the class's evidence.
        artifacts = type(self).failure_artifacts
        problems = getattr(result, "failures", None), getattr(result, "errors", None)
        if artifacts is None or None in problems:
            return super().run(result)
        before = len(problems[0]) + len(problems[1])
        outcome = super().run(result)
        if len(problems[0]) + len(problems[1]) > before:
            artifacts.preserve(self.id())
        return outcome

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

    @classmethod
    def call(cls, path, body=None):
        """GET path without a body, else POST it as JSON; returns the decoded 200 reply."""
        headers = cls.bridge.auth()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = real_browser_failure_artifacts.bridge_request_sent(path, body) if cls.failure_artifacts else None
        try:
            # The bridge answers every action within its own timeout; allow for that here.
            status, _, text = _request(cls.bridge.http_port, "GET" if body is None else "POST", path, headers,
                                       cls.bridge.host, None if body is None else json.dumps(body), timeout=90)
        except Exception as error:
            if request is not None:
                real_browser_failure_artifacts.bridge_request_failed(request, path, error)
            raise
        if request is not None:
            real_browser_failure_artifacts.bridge_request_answered(request, path, status, text)
        if status != 200:
            raise AssertionError(f"{path} answered HTTP {status}: {text}")
        return json.loads(text)

    def open_tab(self, url):
        """Open url in a new tab, wait for it to load, and close it after the test."""
        tab = self.assert_ok(self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 15000}))
        self.addCleanup(self.call, "/api/close-tab", {"tabId": tab["tabId"]})
        return tab["tabId"]

    def assert_ok(self, result):
        self.assertTrue(result.get("ok"), result)
        return result

    def assert_error(self, result, code, retryable):
        self.assertEqual(result.get("code"), code, result)
        self.assertIs(result.get("retryable"), retryable, result)
        self.assertIn("error", result)
