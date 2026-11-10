"""FlatBroker tabs and navigation in a real private browser.

Opt-in, built on tests/real_browser_harness.py (temporary add-on, not the signed build).
The broker is a trusted host object here: nothing is reached through MCP or a worker.
What the browser did is read through the harness's own bridge requests, not the broker's result.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import time
from unittest.mock import patch

from kitsune import bridge_client
from kitsune.flat_broker import FlatBroker
from kitsune.flat_session import CallerAuthority
import real_browser_harness as harness


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/") or "root"
        body = f"<title>{name}</title><h1>{name}</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserFlatBrokerTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def setUp(self):
        for name, value in (("BRIDGE", f"http://127.0.0.1:{self.bridge.http_port}"), ("_headers", self.bridge.auth)):
            patcher = patch.object(bridge_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for_url(self, tab_id, url):
        deadline = time.monotonic() + 15
        while self.native().get(tab_id, {}).get("url") != url:
            self.assertLess(time.monotonic(), deadline, f"tab {tab_id} did not reach {url}: {self.native()}")
            time.sleep(0.1)

    def assert_failed(self, reply, code, outcome):
        self.assertIs(reply.result["ok"], False, reply.result)
        self.assertEqual((reply.result["error"]["code"], reply.result["outcome"]), (code, outcome), reply.result)

    def test_tabs_and_navigation_through_the_broker(self):
        asyncio.run(self.scenario())

    async def scenario(self):
        one, two = self.page.origin + "/one", self.page.origin + "/two"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat", "e2e-flat-owner", "act"))
        before = self.native()
        listed = await broker.call("browser_tabs", {})
        self.assertEqual((listed.result["ok"], listed.result["outcome"]), (True, "observed"), listed.result)
        self.assertEqual(listed.result["data"]["tab_count"], len(before))
        self.assertFalse(any(t["owned"] for t in listed.result["data"]["tabs"]))
        user = next(t["tab_id"] for t in listed.result["data"]["tabs"] if t["active"])
        user_native = next(i for i, t in before.items() if t["active"])

        opened = await broker.call("browser_tabs", {"action": "open", "url": one})
        self.assertEqual((opened.result["ok"], opened.result["outcome"]), (True, "observed"), opened.result)
        handle = opened.result["tab_id"]
        self.assertEqual(opened.result["data"], {"tab_id": handle, "url": one, "status": "complete"})
        created = set(self.native()) - set(before)
        self.assertEqual(len(created), 1)
        native_id = created.pop()
        self.assertEqual(self.native()[native_id]["url"], one)
        self.assertIs(self.native()[user_native]["active"], True, "the open stayed in the background")
        relisted = {t["tab_id"]: t for t in (await broker.call("browser_tabs", {})).result["data"]["tabs"]}
        self.assertEqual((relisted[handle]["url"], relisted[handle]["owned"]), (one, True))
        self.assertIn(user, relisted, "a live tab keeps its handle across listings")

        for arguments, landed in (({"url": two}, two), ({"action": "back"}, one), ({"action": "forward"}, two)):
            with self.subTest(arguments=arguments):
                reply = await broker.call("browser_navigate", {"tab_id": handle, **arguments})
                self.assertEqual((reply.result["ok"], reply.result["outcome"]), (True, "dispatched"), reply.result)
                self.assertEqual(reply.receipt["commands"][-1]["tab_id"], native_id)
                self.wait_for_url(native_id, landed)
        reloaded = await broker.call("browser_navigate", {"tab_id": handle, "action": "reload"})
        self.assertEqual((reloaded.result["ok"], reloaded.result["outcome"]), (True, "dispatched"), reloaded.result)
        self.wait_for_url(native_id, two)

        # The user's tab stays read-only for this caller: the extension refuses before any browser call.
        user_url = self.native()[user_native]["url"]
        for name, arguments in (("browser_navigate", {"tab_id": user, "url": one}),
                                ("browser_navigate", {"tab_id": user, "action": "reload"}),
                                ("browser_tabs", {"action": "close", "tab_id": user})):
            with self.subTest(name=name, arguments=arguments):
                self.assert_failed(await broker.call(name, arguments), "user_tab_read_only", "not_dispatched")
        time.sleep(0.5)  # a navigation would have started by now
        self.assertEqual(self.native()[user_native]["url"], user_url)

        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": handle})
        self.assertEqual((closed.result["ok"], closed.result["outcome"]), (True, "dispatched"), closed.result)
        deadline = time.monotonic() + 10
        while native_id in self.native():
            self.assertLess(time.monotonic(), deadline, "the owned tab did not close")
            time.sleep(0.1)
        self.assert_failed(await broker.call("browser_navigate", {"tab_id": handle, "url": one}),
                           "stale_tab_handle", "not_dispatched")

        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-read", "e2e-flat-owner", "read"))
        count = len(self.native())
        self.assert_failed(await reader.call("browser_tabs", {"action": "open", "url": "file:///etc/passwd"}),
                           "mode_restricted", "not_dispatched")
        self.assert_failed(await reader.call("browser_tabs", {"action": "close", "tab_id": "t_forged"}),
                           "mode_restricted", "not_dispatched")
        self.assertEqual(len(self.native()), count)
