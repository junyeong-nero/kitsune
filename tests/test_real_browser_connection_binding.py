"""A stale transport binding must not mutate a real private browser after reconnect.

Uses the temporary-add-on harness, not signed-extension or delegated-runner conformance.
"""
from http.server import BaseHTTPRequestHandler
import json

from kitsune.connection_binding import CONNECTION_HEADER, ConnectionBinding
import real_browser_harness as harness
from test_real_browser import ExtensionControl
from test_real_browser_connection_diagnostics import CONTROL_SCRIPT


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<title>Connection binding fixture</title>
        <p id="count">count 0</p><script>window.clicks = 0;</script>
        <button id="increment" onclick="document.getElementById('count').textContent =
        'count ' + (++window.clicks)">Increment</button>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserConnectionBindingTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        return {"test-binding-control.js": CONTROL_SCRIPT % cls.control.port}

    def bound_click(self, tab_id, binding):
        # Expected 409 replies cannot use harness.call (which asserts HTTP 200).
        # These two requests appear in the bridge log, not the harness request timeline.
        status, _, text = harness._request(
            self.bridge.http_port, "POST", "/api/click",
            {**self.bridge.auth(), CONNECTION_HEADER: binding.header_value(), "Content-Type": "application/json"},
            self.bridge.host, json.dumps({"tabId": tab_id, "selector": "#increment"}))
        return status, json.loads(text)

    def test_old_binding_never_clicks_after_extension_reconnect(self):
        tab_id = self.open_tab(self.page.origin)
        self.assertIn("count 0", self.call("/api/page-text", {"tabId": tab_id})["text"])
        before = ConnectionBinding.from_status(self.bridge.status())
        self.addCleanup(self.control.call, "releaseBridge")
        self.assertEqual(self.control.call("holdBridge"), {"held": True})
        self.assertEqual(self.control.call("releaseBridge"), {"released": True})
        after = ConnectionBinding.from_status(self.wait_for_extension(before.extension_generation))
        self.assertEqual(after.bridge_instance, before.bridge_instance)
        self.assertGreater(after.extension_generation, before.extension_generation)

        status, refused = self.bound_click(tab_id, before)
        self.assertEqual(status, 409, refused)
        self.assert_error(refused, "stale_connection", False)
        self.assertIs(refused["dispatched"], False)
        self.assertIn("count 0", self.call("/api/page-text", {"tabId": tab_id})["text"])

        status, accepted = self.bound_click(tab_id, after)
        self.assertEqual(status, 200, accepted)
        self.assert_ok(accepted)
        self.assertIn("count 1", self.call("/api/page-text", {"tabId": tab_id})["text"])
