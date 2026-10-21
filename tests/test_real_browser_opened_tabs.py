"""Issues #87 and #94 in a real browser: a target=_blank click reports the tab it opened.

Opt-in: set KITSUNE_E2E_BROWSER to a Firefox or Zen binary. The test runs it headless
with a temporary profile and HOME, installs a temporary copy of the extension over
WebDriver BiDi, and talks to its own bridge on spare ports; it never touches a user
profile or a running Kitsune bridge.

The WebDriver BiDi agent applies Firefox's recommended automation prefs, which turn the
popup blocker off unless the profile sets dom.disable_open_during_load itself. The
default-prefs class pins it to the browser default (true).
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

BROWSER = os.environ.get("KITSUNE_E2E_BROWSER")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "kitsune-e2e-token"
PAGES = {
    "/ok": '<title>ok</title><a id="new" href="/ok2" target="_blank">new tab link</a>'
           '<button id="noop">noop</button>'
           '<a id="opener" href="/ok2" target="_blank" rel="opener">opener link</a>'
           '<a id="handled" href="/ok2" target="_blank" onclick="event.preventDefault()">handled link</a>',
    "/ok2": "<title>ok2</title><h1>second</h1>",
}
PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "app.update.enabled": False,
}


def _spare_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "not found").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(BROWSER, "set KITSUNE_E2E_BROWSER to a Firefox or Zen binary")
class RealBrowserOpenedTabTests(unittest.TestCase):
    # The popup blocker drops a synthetic click's new tab, so allow popups here.
    prefs = {**PREFS, "dom.disable_open_during_load": False}
    opened_via = None  # the browser opens the link's tab itself

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="kitsune-e2e-")
        cls.processes = []
        cls.addClassCleanup(cls._cleanup)
        http_port, ws_port, page_port, bidi_port = _spare_ports(4)
        cls.http_port = http_port
        cls.page_url = f"http://127.0.0.1:{page_port}"
        home, profile, extension = (os.path.join(cls.root, d) for d in ("home", "profile", "extension"))
        os.makedirs(os.path.join(home, ".kitsune"))
        os.makedirs(profile)
        with open(os.path.join(profile, "user.js"), "w") as f:
            f.writelines(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in cls.prefs.items())
        # The extension's bridge URL is fixed, so point a temporary copy at this bridge.
        shutil.copytree(os.path.join(REPO, "extension"), extension)
        background = os.path.join(extension, "background", "background.js")
        with open(background) as f:
            source = f.read()
        assert "ws://127.0.0.1:47652" in source
        with open(background, "w") as f:
            f.write(source.replace("ws://127.0.0.1:47652", f"ws://127.0.0.1:{ws_port}"))

        cls.page = ThreadingHTTPServer(("127.0.0.1", page_port), _Page)
        threading.Thread(target=cls.page.serve_forever, daemon=True).start()
        env = {**os.environ, "HOME": home, "KITSUNE_HOME": os.path.join(home, ".kitsune"),
               "KITSUNE_HTTP_PORT": str(http_port), "KITSUNE_WS_PORT": str(ws_port), "KITSUNE_TOKEN": TOKEN}
        cls._start([sys.executable, "-m", "kitsune.bridge"], env, "bridge.log")
        cls._start([BROWSER, "--headless", "--no-remote", "--profile", profile,
                    f"--remote-debugging-port={bidi_port}"], {**os.environ, "HOME": home}, "browser.log")
        cls.loop = asyncio.new_event_loop()
        cls.bidi = cls.loop.run_until_complete(cls._install_extension(bidi_port, extension))
        deadline = time.monotonic() + 30
        while not cls._extension_connected():
            if time.monotonic() > deadline:
                raise RuntimeError(f"extension did not connect; logs in {cls.root}")
            time.sleep(0.5)

    @classmethod
    def _start(cls, command, env, log):
        with open(os.path.join(cls.root, log), "w") as out:
            cls.processes.append(subprocess.Popen(command, env=env, cwd=REPO, stdout=out,
                                                  stderr=subprocess.STDOUT))

    @classmethod
    def _cleanup(cls):
        if getattr(cls, "bidi", None):
            cls.loop.run_until_complete(cls.bidi.close())
        if getattr(cls, "loop", None):
            cls.loop.close()
        for process in reversed(cls.processes):
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if getattr(cls, "page", None):
            cls.page.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    @staticmethod
    async def _install_extension(port, path):
        deadline = time.monotonic() + 30
        while True:
            try:
                ws = await websockets.connect(f"ws://127.0.0.1:{port}/session", max_size=None)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.5)
        for command_id, (method, params) in enumerate([
            ("session.new", {"capabilities": {}}),
            ("webExtension.install", {"extensionData": {"type": "path", "path": path}}),
        ], 1):
            await ws.send(json.dumps({"id": command_id, "method": method, "params": params}))
            while (reply := json.loads(await ws.recv())).get("id") != command_id:
                pass
            if reply.get("type") != "success":
                raise RuntimeError(f"{method} failed: {reply}")
        return ws  # the temporary extension stays installed while the session is open

    @classmethod
    def _extension_connected(cls):
        try:
            return cls.api("/api/status").get("extension_connected") is True
        except OSError:
            return False

    @classmethod
    def api(cls, path, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.http_port}{path}", method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def click_on_new_page(self, selector, expect=None):
        tab = self.api("/api/new-tab", {"url": self.page_url + "/ok", "wait": True})
        self.assertIs(tab.get("ok"), True, tab)
        body = {"tabId": tab["tabId"], "selector": selector}
        if expect:
            body["expect"] = expect
        return self.api("/api/click", body)

    def assert_opened_ok2(self, result):
        self.assertEqual(len(result["opened_tabs"]), 1, result)
        opened = result["opened_tabs"][0]
        self.assertEqual(opened["url"], self.page_url + "/ok2")
        self.assertEqual(opened.get("via"), self.opened_via, result)
        tabs = {t["id"]: t["url"] for t in self.api("/api/tabs")["tabs"]}
        self.assertEqual(tabs.get(opened["tabId"]), self.page_url + "/ok2")
        return opened

    def test_target_blank_click_reports_the_opened_tab_without_expect(self):
        result = self.click_on_new_page("#new")
        self.assertIs(result["ok"], True, result)
        self.assert_opened_ok2(result)

    def test_url_changed_failure_names_the_opened_tab(self):
        result = self.click_on_new_page("#new", {"url_changed": True, "timeout": 1000})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        opened = self.assert_opened_ok2(result)
        self.assertIn(f"opened tab {opened['tabId']}", result["hint"])
        self.assertIn(f"tab_id={opened['tabId']}", result["hint"])

    def test_new_tab_expect_is_met(self):
        result = self.click_on_new_page("#new", {"new_tab": True, "timeout": 3000})
        self.assertIs(result["ok"], True, result)
        self.assertIs(result["outcome"]["met"], True)
        self.assert_opened_ok2(result)

    def test_no_op_click_opens_no_tab(self):
        result = self.click_on_new_page("#noop", {"url_changed": True, "timeout": 500})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        self.assertNotIn("opened_tabs", result)
        self.assertNotIn("opened tab", result["hint"])


class RealBrowserDefaultPrefsOpenedTabTests(RealBrowserOpenedTabTests):
    """#94: under the default popup blocker the extension opens the link's tab itself."""
    prefs = {**PREFS, "dom.disable_open_during_load": True}
    opened_via = "link_fallback"

    def ok2_tabs(self):
        return sum(t["url"] == self.page_url + "/ok2" for t in self.api("/api/tabs")["tabs"])

    def assert_no_tab_opened(self, result):
        self.assertNotIn("opened_tabs", result)
        self.assertEqual(self.ok2_tabs(), self.ok2_before, result)

    def click_on_new_page(self, selector, expect=None):
        self.ok2_before = self.ok2_tabs()
        return super().click_on_new_page(selector, expect)

    def test_rel_opener_link_reports_popup_blocked(self):
        result = self.click_on_new_page("#opener")
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["warning"]["code"], "popup_blocked", result)
        self.assertEqual(result["warning"]["state"]["url"], self.page_url + "/ok2")
        self.assert_no_tab_opened(result)

    def test_new_tab_expect_names_the_popup_blocker(self):
        result = self.click_on_new_page("#opener", {"new_tab": True, "timeout": 1000})
        self.assertEqual(result["code"], "outcome_not_observed", result)
        self.assertIn("popup blocker", result["hint"])
        self.assert_no_tab_opened(result)

    def test_link_whose_click_the_page_cancelled_opens_no_tab(self):
        result = self.click_on_new_page("#handled")
        self.assertIs(result["ok"], True, result)
        self.assertNotIn("warning", result)
        self.assert_no_tab_opened(result)


if __name__ == "__main__":
    unittest.main()
