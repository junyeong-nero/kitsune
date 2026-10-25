"""The Python API (kitsune.connect) against a real bridge and headless browser.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import os
import re
from unittest.mock import patch

import kitsune
from kitsune import bridge_client, bridge_control
import real_browser_harness as harness

PAGE = """<!doctype html><title>Python API</title>
<h1>Orders</h1>
<form id=order action="/done" method=get>
  <label>Name <input id=name name=name></label>
  <label><input type=checkbox id=agree name=agree> Agree</label>
  <select id=size name=size><option value=s>Small</option><option value=l>Large</option></select>
  <button id=save type=button>Save</button>
  <button type=submit>Send</button>
</form>
<p id=status>Idle</p>
<button class=twin>One</button><button class=twin>Two</button>
<button id=log>Log</button>
<script>
window.appState = 'ready';
document.getElementById('save').addEventListener('click', () => {
  document.getElementById('status').textContent = 'Saved ' + document.getElementById('name').value;
});
document.getElementById('log').addEventListener('click', () => console.warn('kitsune-python'));
</script>
"""
DONE = "<!doctype html><title>Done</title><p>Thanks</p>"


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = (DONE if self.path.startswith("/done") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserPythonApiTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        # Point the library at this class's bridge, as KITSUNE_HTTP_PORT and KITSUNE_TOKEN would.
        url = f"http://127.0.0.1:{self.bridge.http_port}"
        token = self.bridge.auth()["Authorization"].removeprefix("Bearer ")
        for patcher in (patch.object(bridge_client, "BRIDGE", url), patch.object(bridge_control, "BRIDGE", url),
                        patch.object(bridge_client, "HTTP_PORT", self.bridge.http_port),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": token})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = kitsune.connect()
        self.tab = self.browser.new_tab(self.site.origin + "/")
        self.addCleanup(self.tab.close)

    def test_reads_and_actions_on_a_background_tab(self):
        listed = {tab["id"]: tab for tab in self.browser.tabs()}
        self.assertIs(listed[self.tab.id]["owned"], True)
        self.assertIs(listed[self.tab.id]["active"], False, "the new tab opened in the background")
        self.assertEqual(self.tab.info()["title"], "Python API")
        save = re.search(r'button "Save" \[ref=(e\d+)\]', self.tab.snapshot()).group(1)

        self.assertTrue(self.tab.fill("#name", "Ada")["ok"])
        self.assertTrue(self.tab.check("#agree")["ok"])
        self.assertTrue(self.tab.select_option("#size", "Large", by_text=True)["ok"])
        clicked = self.tab.click(save, expect={"text": "Saved Ada"})
        self.assertIs(clicked["outcome"]["met"], True, clicked)

        self.assertIn("Saved Ada", self.tab.text())
        self.assertIn("Saved Ada", self.tab.html("#status"))
        self.assertIn("Orders", self.tab.markdown())
        self.assertEqual([item["value"] for item in self.tab.query("#size option", fields=["value"])], ["s", "l"])
        self.assertIn("name", [field.get("name") for field in self.tab.forms()])
        self.assertEqual(self.tab.evaluate("() => [document.getElementById('agree').checked, "
                                           "document.getElementById('size').value]"), [True, "l"])
        self.assertTrue(self.tab.screenshot().startswith(b"\x89PNG"))
        batch = self.browser.batch([{"action": "pageText"}], tab=self.tab)
        self.assertIn("Saved Ada", batch["results"][0]["text"])

    def test_storage_console_tabs_windows_and_tags(self):
        self.tab.local_storage.set("theme", "dark")
        self.assertEqual(self.tab.local_storage.get("theme"), "dark")
        self.assertEqual(self.tab.local_storage.items(), {"theme": "dark"})
        self.tab.local_storage.clear()
        self.assertIsNone(self.tab.local_storage.get("theme"))

        self.tab.watch_console()
        self.tab.click("#log")  # the page's own console, which watch_console records
        self.assertIn("kitsune-python", [log.get("message") for log in self.tab.console_logs()])
        # evaluate runs in the extension's content-script world: the page's DOM, not its globals.
        self.assertEqual(self.tab.evaluate("() => typeof window.appState"), "undefined")
        self.assertEqual(self.tab.evaluate("() => window.wrappedJSObject.appState"), "ready")
        self.assertEqual(self.tab.computed_style("#status", ["display"]), {"display": "block"})
        self.assertEqual(self.tab.meta()["title"], "Python API")

        copy = self.tab.duplicate()
        self.addCleanup(copy.close)
        self.assertNotEqual(copy, self.tab)
        self.assertIs({tab["id"]: tab for tab in self.browser.tabs()}[copy.id]["owned"], True)
        self.assertIn(self.tab.id, [tab["id"] for window in self.browser.windows.list() for tab in window["tabs"]])

        self.tab.tag("orders")
        self.addCleanup(self.browser.tags.remove, "orders")
        self.assertEqual(self.browser.tags.tab("orders"), self.tab)
        self.assertEqual(self.browser.tags.list()["orders"], self.tab.id)

    def test_failures_raise_their_codes(self):
        with self.assertRaises(kitsune.KitsuneError) as raised:
            self.tab.click(".twin")
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("ambiguous_target", False))
        self.assertTrue(self.tab.click(".twin", first=True)["ok"])
        with self.assertRaises(kitsune.TimedOut):
            self.tab.wait_for(text="Never shown", timeout=300)

    def test_a_held_submit_runs_once_confirm_approves_it(self):
        self.browser.request("/api/set-policy", {"ask": True})
        self.addCleanup(self.browser.request, "/api/set-policy", {"ask": False})
        with self.assertRaises(kitsune.ConfirmationRequired) as raised:
            self.tab.submit_form("#order")
        self.assertTrue(raised.exception.token)
        self.assertEqual(self.tab.info()["title"], "Python API", "nothing ran")

        reasons = []
        approving = kitsune.connect(confirm=lambda held: reasons.append(held.state.get("reason")) or True)
        result = approving.tab(self.tab.id).submit_form("#order", expect={"url": "/done"})
        self.assertIs(result["outcome"]["met"], True, result)
        self.assertEqual(reasons, ["submit"])
        self.tab.wait_for(text="Thanks")

    def test_a_bridge_of_another_build_is_left_running(self):
        with patch.object(bridge_client, "BRIDGE_BUILD", "0.0.0+another"), \
                self.assertRaises(kitsune.ConnectionFailed) as raised:
            kitsune.connect()
        self.assertEqual(raised.exception.code, "bridge_version_mismatch")
        self.assertEqual(self.bridge.status()["extension_connected"], True, "the bridge still runs")
