"""Tab inventory freshness against an owned real bridge (issue #28).

Each test runs `python -m kitsune.bridge` in a temporary KITSUNE_HOME/HOME on
ephemeral ports (see test_bridge_security.Bridge). A scripted extension connects
over the real WebSocket and answers from an in-memory tab model that the test
mutates directly, the way async navigation, redirects and user actions change a
browser without any bridge command. No normal user profile or bridge is touched.
"""
import json
import tempfile
import threading
import time
import unittest

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from test_bridge_security import Bridge, _request

EXTENSION_ORIGIN = "moz-extension://00000000-0000-4000-8000-000000000028"
# Every read must be fresh well inside the five-second inventory TTL the bridge used to apply.
FRESH_READ_SECONDS = 2


def tab(tab_id, url, active=False, status="complete"):
    return {"id": tab_id, "url": url, "title": url, "active": active, "windowId": 1, "status": status}


class ScriptedExtension:
    """Answers bridge commands from a tab model the test changes behind the bridge's back."""

    def __init__(self, bridge, tabs):
        self.lock = threading.Lock()
        self.tabs = [dict(t) for t in tabs]
        self.actions = []
        self.ws = None
        connected = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(bridge.ws_port, connected), daemon=True)
        self.thread.start()
        if not connected.wait(5):
            raise AssertionError("scripted extension could not connect: " + bridge.log())
        deadline = time.monotonic() + 5
        while bridge.status()["extension_version"] != "0.0.0-test":
            if time.monotonic() > deadline:
                raise AssertionError("scripted extension did not register: " + bridge.log())
            time.sleep(0.02)

    def _serve(self, ws_port, connected):
        try:
            with connect(f"ws://127.0.0.1:{ws_port}", origin=EXTENSION_ORIGIN, open_timeout=5) as ws:
                self.ws = ws
                ws.send(json.dumps({"type": "hello", "version": "0.0.0-test", "build": "test"}))
                connected.set()
                for message in ws:
                    command = json.loads(message)
                    with self.lock:
                        self.actions.append(command["action"])
                    ws.send(json.dumps({"id": command["id"], "result": self._answer(command)}))
        except ConnectionClosed:
            pass

    def _answer(self, command):
        action = command["action"]
        with self.lock:
            if action == "getTabs":
                return {"tabs": [dict(t) for t in self.tabs]}
            if action == "getPageText":
                active = next(t for t in self.tabs if t["active"])
                return {"text": "text of " + active["url"], "url": active["url"]}
            if action == "newTab":
                return self._new_tab(command)
        return {"error": f"unscripted action {action}"}

    def _new_tab(self, command):
        tab_id = max((t["id"] for t in self.tabs), default=0) + 1
        for t in self.tabs:
            t["active"] = False
        if not command.get("wait"):
            # Immediate mode returns before the navigation commits.
            self.tabs.append(tab(tab_id, "about:blank", active=True, status="loading"))
            return {"ok": True, "tabId": tab_id}
        # Navigation-ready mode: the requested URL redirects before the load completes.
        landed = command["url"].replace("/redirect", "/landed")
        self.tabs.append(tab(tab_id, landed, active=True))
        return {"ok": True, "tabId": tab_id, "url": landed, "status": "complete"}

    def set_tabs(self, *tabs):
        with self.lock:
            self.tabs = [dict(t) for t in tabs]

    def count(self, action):
        with self.lock:
            return self.actions.count(action)

    def close(self):
        if self.ws is not None:
            self.ws.close()
        self.thread.join(timeout=5)


class OwnedBridgeTabInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # token=None: a fresh home where the bridge must create its own token.
        self.bridge = Bridge(temp.name, token=None).start()
        self.addCleanup(self.bridge.stop)

    def extension(self, *tabs):
        extension = ScriptedExtension(self.bridge, tabs)
        self.addCleanup(extension.close)
        return extension

    def call(self, method, path, body=None):
        headers = self.bridge.auth()
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, _, text = _request(self.bridge.http_port, method, path, headers, self.bridge.host,
                                   json.dumps(body) if body is not None else None)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def list_tabs(self):
        start = time.monotonic()
        result = self.call("GET", "/api/tabs")
        self.assertLess(time.monotonic() - start, FRESH_READ_SECONDS)
        self.assertNotIn("_cached", result)
        return result

    def test_fresh_home_reports_no_inventory_until_extension_then_its_first_tabs(self):
        before = self.call("GET", "/api/tabs")
        self.assertIn("error", before)
        self.assertNotIn("tabs", before)

        first_profile = self.extension(tab(1, "about:home", active=True))
        self.assertEqual(self.list_tabs(), {"tabs": [tab(1, "about:home", active=True)]})
        first_profile.close()

        # A different fresh profile reconnecting must not inherit the previous inventory.
        second_profile = self.extension(tab(7, "about:newtab", active=True))
        self.assertEqual(self.list_tabs(), {"tabs": [tab(7, "about:newtab", active=True)]})
        self.assertEqual(second_profile.count("getTabs"), 1)

    def test_out_of_band_tab_changes_are_listed_on_the_next_read(self):
        steps = [
            ("delayed navigation leaves a protected page",
             [tab(1, "https://a.test/start", active=True)]),
            ("redirect", [tab(1, "https://a.test/landed", active=True)]),
            ("external URL change", [tab(1, "https://b.test/elsewhere", active=True)]),
            ("selection in another tab",
             [tab(1, "https://b.test/elsewhere"), tab(2, "https://c.test/", active=True)]),
            ("navigation back to a protected page",
             [tab(1, "about:preferences"), tab(2, "https://c.test/", active=True)]),
            ("external close", [tab(1, "about:preferences", active=True)]),
            ("reopen", [tab(1, "about:preferences"), tab(3, "https://c.test/", active=True)]),
        ]
        # Default TTL, a long TTL and TTL 0: tab reads never depend on the read cache.
        for ttl in (None, 60, 0):
            with self.subTest(ttl=ttl):
                if ttl is not None:
                    self.assertEqual(self.call("POST", "/api/cache", {"action": "ttl", "seconds": ttl})["ttl"], ttl)
                extension = self.extension(tab(1, "about:newtab", active=True))
                self.assertEqual(self.list_tabs()["tabs"], [tab(1, "about:newtab", active=True)])
                for name, state in steps:
                    extension.set_tabs(*state)
                    # Populate the page-text read cache first so a cached response is available.
                    self.call("GET", "/api/page-text")
                    self.assertEqual(self.list_tabs()["tabs"], state, name)
                self.assertEqual(extension.count("getTabs"), len(steps) + 1)
                extension.close()

    def test_new_tab_results_agree_with_the_next_inventory(self):
        extension = self.extension(tab(1, "https://a.test/", active=True))
        self.list_tabs()

        ready = self.call("POST", "/api/new-tab", {"url": "https://b.test/redirect", "wait": True, "timeout": 2000})
        self.assertEqual(ready, {"ok": True, "tabId": 2, "url": "https://b.test/landed", "status": "complete"})
        self.assertIn(tab(2, "https://b.test/landed", active=True), self.list_tabs()["tabs"])

        immediate = self.call("POST", "/api/new-tab", {"url": "https://c.test/slow"})
        self.assertEqual(immediate, {"ok": True, "tabId": 3})
        self.assertIn(tab(3, "about:blank", active=True, status="loading"), self.list_tabs()["tabs"])
        # The delayed navigation commits after newTab returned.
        extension.set_tabs(tab(1, "https://a.test/"), tab(2, "https://b.test/landed"),
                           tab(3, "https://c.test/slow", active=True))
        self.assertIn(tab(3, "https://c.test/slow", active=True), self.list_tabs()["tabs"])


if __name__ == "__main__":
    unittest.main()
