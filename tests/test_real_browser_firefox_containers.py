"""#495: explicit container routing and cookie isolation in a private real browser."""
import asyncio
from http.server import BaseHTTPRequestHandler
import os
import unittest
from unittest.mock import patch

import kitsune
from kitsune import bridge_client, bridge_control, script_runner
import real_browser_harness as harness


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Only a synthetic fixture cookie; no user profile or account is involved.
        text = "container-cookie-present" if "kitsune_container=fixture" in self.headers.get("Cookie", "") else "container-cookie-absent"
        data = f"<!doctype html><title>Container fixture</title><h1>{text}</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.path == "/set":
            self.send_header("Set-Cookie", "kitsune_container=fixture; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserFirefoxContainerTests(harness.RealBrowserTestCase):
    # Only the disposable test profile enables containers; the product manifest does not.
    PROFILE_PREFS = {"privacy.userContext.enabled": True, "privacy.userContext.ui.enabled": True}

    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        # Zen's fresh profile need not contain Firefox's default container IDs.
        debugger = harness.RemoteDebugger(cls.browser.rdp_port)
        try:
            identity = debugger.evaluate_in_parent("""(() => {
              const { ContextualIdentityService } = ChromeUtils.importESModule(
                'moz-src:///toolkit/components/contextualidentity/ContextualIdentityService.sys.mjs');
              return ContextualIdentityService.create('Kitsune fixture', 'fingerprint', 'blue');
            })()""")
        finally:
            debugger.close()
        cls.store = f"firefox-container-{identity['userContextId']}"

    def new_tab(self, path, container=None):
        body = {"url": self.site.origin + path, "wait": True}
        if container is not None:
            body["container"] = container
        result = self.call("/api/new-tab", body)
        if isinstance(result.get("tabId"), int):
            self.addCleanup(self.call, "/api/close-tab", {"tabId": result["tabId"]})
        return self.assert_ok(result)

    def test_explicit_store_shares_its_cookie_but_not_the_default_store(self):
        store = self.store
        seeded = self.new_tab("/set", store)
        self.assertEqual(seeded["cookieStoreId"], store)
        same = self.new_tab("/read", store)
        default = self.new_tab("/read")
        inventory = {tab["id"]: tab for tab in self.call("/api/tabs")["tabs"]}
        self.assertEqual(inventory[same["tabId"]]["cookieStoreId"], store)
        self.assertNotIn("cookieStoreId", inventory[default["tabId"]])
        self.assertIn("container-cookie-present", self.call("/api/snapshot", {"tabId": same["tabId"]})["snapshot"])
        self.assertIn("container-cookie-absent", self.call("/api/snapshot", {"tabId": default["tabId"]})["snapshot"])

    def connect_python_api(self):
        # Point the library at this class's bridge, as KITSUNE_HTTP_PORT and KITSUNE_TOKEN would.
        url = f"http://127.0.0.1:{self.bridge.http_port}"
        token = self.bridge.auth()["Authorization"].removeprefix("Bearer ")
        for patcher in (patch.object(bridge_client, "BRIDGE", url), patch.object(bridge_control, "BRIDGE", url),
                        patch.object(bridge_client, "HTTP_PORT", self.bridge.http_port),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": token})):
            patcher.start()
            self.addCleanup(patcher.stop)
        return kitsune.connect()

    def python_new_tab(self, browser, path, container=None):
        tab = browser.new_tab(self.site.origin + path, container=container)
        self.addCleanup(self.call, "/api/close-tab", {"tabId": tab.id})
        return tab

    def test_python_new_tab_and_script_open_tabs_in_the_container(self):
        browser = self.connect_python_api()
        seeded = self.python_new_tab(browser, "/set", self.store)
        same = self.python_new_tab(browser, "/read", self.store)
        default = self.python_new_tab(browser, "/read")
        listed = {tab["id"]: tab for tab in browser.tabs()}
        self.assertEqual(listed[seeded.id]["cookieStoreId"], self.store)
        self.assertEqual(listed[same.id]["cookieStoreId"], self.store)
        self.assertNotIn("cookieStoreId", listed[default.id])
        self.assertIn("container-cookie-present", same.text())
        self.assertIn("container-cookie-absent", default.text())

        # browser_run_python runs scripts with the MCP server's client; here the library's own client.
        code = (f"store = next(t for t in browser.tabs() if t['id'] == {seeded.id})['cookieStoreId']\n"
                f"tab = browser.new_tab({self.site.origin + '/read'!r}, container=store)\n"
                "listed = next(t for t in browser.tabs() if t['id'] == tab.id)\n"
                "result = {'id': tab.id, 'store': listed.get('cookieStoreId'), 'text': tab.text()}")
        result = asyncio.run(script_runner.run_python(code, browser.client))
        self.assertTrue(result.get("ok"), result)
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result["result"]["id"]})
        self.assertEqual(result["result"]["store"], self.store)
        self.assertIn("container-cookie-present", result["result"]["text"])

    def test_unavailable_name_and_id_do_not_open_a_default_tab(self):
        before = {tab["id"] for tab in self.call("/api/tabs")["tabs"]}
        for container in ("Personal", "firefox-container-999999"):
            with self.subTest(container=container):
                result = self.call("/api/new-tab", {"url": self.site.origin + "/read", "container": container})
                self.assert_error(result, "container_unavailable", True)
                self.assertNotIn("tabId", result)
                self.assertEqual(before, {tab["id"] for tab in self.call("/api/tabs")["tabs"]})


if __name__ == "__main__":
    unittest.main()
