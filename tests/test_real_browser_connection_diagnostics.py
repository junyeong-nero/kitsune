"""Issue #34 in a real browser: the browser_status diagnosis and the tool errors behind it.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Covers
what a browser can show: no extension connected, then connected; an extension that drops
its bridge connection; a developer build edited on disk but not reloaded; a restricted
domain (host_permission_denied) beside an empty error page (error_page); and, in its own
browser, a profile without a normal window (browser_window_unavailable).
"""
from http.server import BaseHTTPRequestHandler
import time

import real_browser_harness as harness
from test_real_browser import ExtensionControl

# Runs after background.js in the extension copy; answers ExtensionControl calls.
CONTROL_SCRIPT = """'use strict';
let diagnosticsHeldConnect = null;
(function connectDiagnosticsControl() {
  const control = new WebSocket('ws://127.0.0.1:%d');
  control.onmessage = async event => {
    const { id, op } = JSON.parse(event.data);
    let result;
    try {
      if (op === 'holdBridge') {
        // Disconnect and stay disconnected: reconnects call the replaced connect().
        diagnosticsHeldConnect = diagnosticsHeldConnect || connect;
        connect = () => {};
        ws?.close();
        result = { held: true };
      } else if (op === 'releaseBridge') {
        const held = diagnosticsHeldConnect;
        if (held) {
          connect = held;
          diagnosticsHeldConnect = null;
          connect();
        }
        result = { released: Boolean(held) };
      } else if (op === 'popupOnly') {
        // A popup window keeps the browser running while no normal window is open.
        await browser.windows.create({ type: 'popup', url: 'about:blank' });
        for (const win of await browser.windows.getAll({ windowTypes: ['normal'] })) {
          await browser.windows.remove(win.id);
        }
        result = { normal: (await browser.windows.getAll({ windowTypes: ['normal'] })).length };
      } else if (op === 'restoreWindow') {
        if (!(await browser.windows.getAll({ windowTypes: ['normal'] })).length) {
          await browser.windows.create({ url: 'about:blank' });
        }
        for (const win of await browser.windows.getAll({ windowTypes: ['popup'] })) {
          await browser.windows.remove(win.id);
        }
        result = { normal: (await browser.windows.getAll({ windowTypes: ['normal'] })).length };
      } else {
        result = { error: 'unknown op ' + op };
      }
    } catch (e) {
      result = { error: e.message };
    }
    control.send(JSON.stringify({ id, result }));
  };
  control.onclose = () => setTimeout(connectDiagnosticsControl, 500);
})();
"""

# One of Mozilla's default extensions.webextensions.restrictedDomains (RESTRICTED_DOMAINS in
# background.js). network.dns.forceResolve sends it to the local fixture server, so no request
# leaves the machine; HSTS preloading and HTTPS-First would otherwise upgrade it to https.
RESTRICTED_HOST = "support.mozilla.org"
RESTRICTED_PREFS = {
    "network.dns.forceResolve": "127.0.0.1",
    "network.proxy.type": 0,
    "network.stricttransportsecurity.preloadlist": False,
    "dom.security.https_first": False,
    "dom.security.https_first_schemeless": False,
    "dom.security.https_only_mode": False,
}
FIXTURE_TITLE = "Kitsune diagnosis fixture"
# Window and permission changes reach the bridge as a state message; allow for that.
DIAGNOSIS_TIMEOUT = 15


class _Pages(BaseHTTPRequestHandler):
    """/404 answers with an empty 404; anything else with a small titled page."""

    def do_GET(self):
        status, body = (404, b"") if self.path == "/404" else (
            200, f"<title>{FIXTURE_TITLE}</title><p>fixture</p>".encode())
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _DiagnosisTestCase(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        # Called once the owned bridge runs and before the browser starts.
        cls.status_before_browser = cls.bridge.status()
        return {"test-diagnostics-control.js": CONTROL_SCRIPT % cls.control.port}

    def wait_for_diagnosis(self, code):
        deadline = time.monotonic() + DIAGNOSIS_TIMEOUT
        while True:
            status = self.bridge.status()
            if status["diagnosis"]["code"] == code or time.monotonic() > deadline:
                self.assertEqual(status["diagnosis"]["code"], code, status)
                return status
            time.sleep(0.1)


class RealBrowserConnectionDiagnosisTests(_DiagnosisTestCase):
    PROFILE_PREFS = RESTRICTED_PREFS

    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_no_extension_then_connected_then_disconnected(self):
        before = self.status_before_browser
        self.assertFalse(before["extension_connected"], before)
        # The owned bridge listens on a spare port, not the extension's default 47652, so before
        # any extension connects it reports that port as the likely cause.
        self.assertEqual(before["diagnosis"]["code"], "port_conflict", before)
        self.assertEqual(before["diagnosis"]["state"], {"port": self.bridge.ws_port, "reason": "extension_ws_port"})

        status = self.bridge.status()
        self.assertTrue(status["extension_connected"], status)
        # Also proves that background.js fingerprints the loaded scripts as the bridge does.
        self.assertEqual(status["diagnosis"], {"code": "connected"}, status)

        generation = status["extension_generation"]
        self.addCleanup(self.wait_for_extension, generation)
        self.addCleanup(self.control.call, "releaseBridge")
        self.assertEqual(self.control.call("holdBridge"), {"held": True})
        status = self.wait_for_diagnosis("extension_disconnected")
        self.assertFalse(status["extension_connected"], status)
        self.assertIn("about:addons", status["diagnosis"]["hint"])
        result = self.call("/api/tabs")
        self.assert_error(result, "extension_disconnected", True)
        self.assertEqual(result["state"], {"connected": False}, result)

        self.assertEqual(self.control.call("releaseBridge"), {"released": True})
        self.wait_for_extension(after_generation=generation)
        self.assertEqual(self.wait_for_diagnosis("connected")["diagnosis"], {"code": "connected"})

    def test_developer_build_edited_on_disk_is_a_version_mismatch(self):
        content = self.browser.extension / "content" / "content.js"
        source = content.read_text(encoding="utf-8")
        self.addCleanup(content.write_text, source, encoding="utf-8")
        content.write_text(source + "\n// edited after the browser loaded it\n", encoding="utf-8")

        status = self.wait_for_diagnosis("extension_version_mismatch")
        state = status["diagnosis"]["state"]
        self.assertEqual(state["reason"], "stale_developer_build", status)
        self.assertNotEqual(state["extensionSourceHash"], state["expectedSourceHash"])
        self.assertIn("about:debugging", status["diagnosis"]["hint"])

        content.write_text(source, encoding="utf-8")
        self.wait_for_diagnosis("connected")

    def test_restricted_domain_is_host_permission_denied_and_empty_error_page_is_error_page(self):
        port = self.site.origin.rsplit(":", 1)[1]
        restricted_url = f"http://{RESTRICTED_HOST}:{port}/"
        restricted = self.open_tab(restricted_url)
        tabs = {tab["id"]: tab for tab in self.call("/api/tabs")["tabs"]}
        # The fixture answered, not a DNS error page for the real host.
        self.assertEqual(tabs[restricted]["title"], FIXTURE_TITLE, tabs[restricted])

        error_page = self.open_tab(f"{self.site.origin}/404")
        for path, body in (("/api/page-info", {}), ("/api/snapshot", {}),
                           ("/api/click", {"selector": "p"})):
            with self.subTest(path=path):
                result = self.call(path, {**body, "tabId": restricted})
                self.assert_error(result, "host_permission_denied", True)
                self.assertEqual((result["state"]["reason"], result["state"]["url"]),
                                 ("restricted_domain", restricted_url), result)
                self.assertIn("about:addons", result["hint"])

                result = self.call(path, {**body, "tabId": error_page})
                self.assert_error(result, "error_page", False)
                self.assertEqual(result["state"]["httpStatus"], 404, result)
        # A refused page is not a missing site permission for the whole extension.
        self.assertEqual(self.bridge.status()["diagnosis"], {"code": "connected"})


class RealBrowserNoWindowTests(_DiagnosisTestCase):
    """Its own browser: this class closes every normal window."""

    def test_browser_without_a_normal_window_is_unavailable(self):
        self.assertEqual(self.bridge.status()["diagnosis"], {"code": "connected"})
        self.addCleanup(self.control.call, "restoreWindow")
        self.assertEqual(self.control.call("popupOnly"), {"normal": 0})
        status = self.wait_for_diagnosis("browser_window_unavailable")
        self.assertEqual(status["diagnosis"]["state"], {"windows": 0}, status)
        self.assertIn("normal browser window", status["diagnosis"]["hint"])

        self.assertEqual(self.control.call("restoreWindow"), {"normal": 1})
        self.wait_for_diagnosis("connected")
