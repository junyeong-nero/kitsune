"""Real private HTTP/WebSocket transport; a scripted extension is not a browser pass."""
import json
import tempfile
import unittest

from kitsune.connection_binding import CONNECTION_HEADER, ConnectionBinding
from test_bridge_security import Bridge, _request
from test_tab_inventory_bridge import ScriptedExtension, tab


class ConnectionBindingWireTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.bridge = Bridge(home.name).start()
        self.addCleanup(self.bridge.stop)

    def extension(self, url="https://first.test/"):
        extension = ScriptedExtension(self.bridge, [tab(9, url, active=True)])
        self.addCleanup(extension.close)
        return extension

    def binding(self):
        return ConnectionBinding.from_status(self.bridge.status())

    def tabs(self, binding=None, headers=None):
        auth = self.bridge.auth()
        if binding is not None:
            auth[CONNECTION_HEADER] = binding.header_value()
        auth.update(headers or {})
        status, _, text = _request(self.bridge.http_port, "GET", "/api/tabs", auth, self.bridge.host)
        return status, json.loads(text)

    def assert_refused(self, binding):
        status, result = self.tabs(binding)
        self.assertEqual(status, 409, result)
        self.assertEqual(result["code"], "stale_connection")
        self.assertIs(result["dispatched"], False)
        self.assertIs(result["retryable"], False)

    def test_replaced_extension_cannot_receive_old_bound_request(self):
        first = self.extension()
        old = self.binding()
        self.assertEqual(self.tabs(old)[0], 200)
        self.assertEqual(first.count("getTabs"), 1)
        second = self.extension("https://second.test/")
        fresh = self.binding()
        self.assertEqual(fresh.bridge_instance, old.bridge_instance)
        self.assertGreater(fresh.extension_generation, old.extension_generation)
        self.assert_refused(old)
        self.assertEqual(second.count("getTabs"), 0)
        status, result = self.tabs(fresh)
        self.assertEqual(status, 200)
        self.assertEqual(result["tabs"][0]["url"], "https://second.test/")
        self.assertEqual(second.count("getTabs"), 1)
        # A later unbound developer request is unaffected by the stale request context.
        self.assertEqual(self.tabs()[0], 200)
        self.assertEqual(second.count("getTabs"), 2)

    def test_bridge_restart_invalidates_identical_tab_and_generation_numbers(self):
        first = self.extension()
        old = self.binding()
        self.assertEqual(old.extension_generation, 1)
        self.assertEqual(self.tabs(old)[1]["tabs"][0]["id"], 9)
        first.close()
        self.bridge.stop()
        self.bridge.start()  # same private ports/HOME/token, but a new process
        second = self.extension("https://replacement.test/")
        fresh = self.binding()
        self.assertEqual(fresh.extension_generation, old.extension_generation)
        self.assertNotEqual(fresh.bridge_instance, old.bridge_instance)
        self.assert_refused(old)
        self.assertEqual(second.count("getTabs"), 0)
        status, result = self.tabs(fresh)
        self.assertEqual(status, 200)
        self.assertEqual(result["tabs"][0]["id"], 9)
        self.assertEqual(result["tabs"][0]["url"], "https://replacement.test/")

    def test_duplicate_case_variant_headers_fail_on_real_http_parser(self):
        extension = self.extension()
        binding = self.binding()
        status, result = self.tabs(binding, {CONNECTION_HEADER.lower(): binding.header_value()})
        self.assertEqual((status, result["code"]), (400, "invalid_connection_binding"))
        self.assertEqual(extension.count("getTabs"), 0)

    def test_matching_binding_is_not_an_authentication_credential(self):
        extension = self.extension()
        binding = self.binding()
        status, _ = self.tabs(binding, {"Authorization": "Bearer wrong-test-token"})
        self.assertEqual(status, 401)
        self.assertEqual(extension.count("getTabs"), 0)


if __name__ == "__main__":
    unittest.main()
