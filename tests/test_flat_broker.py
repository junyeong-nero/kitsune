"""FlatBroker against a real private bridge and a scripted extension.

The HTTP/WebSocket transport, bridge policy and dispatch receipts are real; the
extension is scripted, so this is not a browser pass, and the broker is not yet
reachable from MCP or a worker.
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from kitsune import bridge, bridge_client, dispatch_receipts, flat_broker
from kitsune.connection_binding import CONNECTION_HEADER, ConnectionBinding
from kitsune.dispatch_receipts import RECEIPT_HEADER, DispatchReceipt
from kitsune.flat_broker import FlatBroker
from kitsune.flat_operations import OperationRefused
from kitsune.flat_session import CallerAuthority
from kitsune.version import PACKAGE_VERSION
from test_bridge_security import Bridge, _free_port, _request
from test_tab_inventory_bridge import EXTENSION_ORIGIN

DROP = object()  # close the extension connection instead of replying


def tab(tab_id, url, *, title=None, active=False, owned=False):
    return {"id": tab_id, "url": url, "title": url if title is None else title, "active": active,
            "windowId": 1, "index": tab_id, "owned": owned}


class BrokerExtension:
    """A scripted extension of the bridge's own version, so the bridge diagnoses it as connected."""

    def __init__(self, bridge_process, tabs=(), version=PACKAGE_VERSION):
        self.lock = threading.Lock()
        self.tabs = [dict(t) for t in tabs]
        self.commands = []
        self.behavior = {}
        self.ws = None
        before = bridge_process.status()["extension_generation"]
        connected = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(bridge_process.ws_port, version, connected),
                                       daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while True:
            status = bridge_process.status()
            if (connected.is_set() and status["extension_generation"] > before
                    and status["extension_version"] == version):
                return
            if time.monotonic() > deadline:
                raise AssertionError("scripted extension did not register: " + bridge_process.log())
            time.sleep(0.02)

    def _serve(self, ws_port, version, connected):
        try:
            with connect(f"ws://127.0.0.1:{ws_port}", origin=EXTENSION_ORIGIN, open_timeout=5) as ws:
                self.ws = ws
                ws.send(json.dumps({"type": "hello", "version": version, "build": "test"}))
                connected.set()
                for message in ws:
                    command = json.loads(message)
                    with self.lock:
                        self.commands.append(command)
                    reply = self._answer(command)
                    if reply is DROP:
                        return
                    ws.send(json.dumps({"id": command["id"], "result": reply}))
        except ConnectionClosed:
            pass

    def _answer(self, command):
        action = command["action"]
        if action in self.behavior:
            return self.behavior[action](command)
        with self.lock:
            if action == "getTabs":
                return {"ownershipByClient": True, "tabs": [dict(t) for t in self.tabs]}
            if action == "newTab":
                tab_id = max((t["id"] for t in self.tabs), default=0) + 1
                landed = command["url"].replace("/redirect", "/landed")
                self.tabs.append(tab(tab_id, landed, owned=True))
                return {"ok": True, "tabId": tab_id, "url": landed, "status": "complete"}
            if action == "closeTab":
                self.tabs = [t for t in self.tabs if t["id"] != command["tabId"]]
                return {"ok": True}
            if action == "navigate":
                return {"ok": True, "url": command["url"]}
            if action in {"goBack", "goForward", "reloadTabBrowser"}:
                return {"ok": True, "tabId": command["tabId"]}
        return {"error": f"unscripted action {action}"}

    def actions(self):
        with self.lock:
            return [command["action"] for command in self.commands]

    def last(self, action):
        with self.lock:
            return next(c for c in reversed(self.commands) if c["action"] == action)

    def set_tabs(self, *tabs):
        with self.lock:
            self.tabs = [dict(t) for t in tabs]

    def close(self):
        if self.ws is not None:
            self.ws.close()
        self.thread.join(timeout=5)


class FlatBrokerWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.bridge = Bridge(home.name).start()
        self.addCleanup(self.bridge.stop)
        # The trusted client reads its endpoint and token at import: point it at the private bridge.
        for name, value in (("BRIDGE", f"http://127.0.0.1:{self.bridge.http_port}"),
                            ("_headers", self.bridge.auth)):
            patcher = patch.object(bridge_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def extension(self, *tabs, **kwargs):
        extension = BrokerExtension(self.bridge, tabs or [tab(7, "https://user.test/", active=True),
                                                           tab(9, "https://agent.test/", owned=True)], **kwargs)
        self.addCleanup(extension.close)
        return extension

    async def connect(self, mode="act", **authority):
        return await FlatBroker.connect(CallerAuthority("flat-client", "flat-owner", mode, **authority))

    async def handle(self, broker, url="https://agent.test/"):
        listed = await broker.call("browser_tabs", {})
        self.assertTrue(listed.result["ok"], listed.result)
        return next(t["tab_id"] for t in listed.result["data"]["tabs"] if t["url"] == url)

    def assert_failed(self, reply, code, outcome):
        self.assertIs(reply.result["ok"], False, reply.result)
        self.assertEqual((reply.result["error"]["code"], reply.result["outcome"]), (code, outcome), reply.result)
        self.assertIs(reply.result["error"]["retryable"], False)

    async def test_list_open_navigate_close_use_handles_and_bridge_receipts(self):
        extension = self.extension()
        broker = await self.connect()
        listed = await broker.call("browser_tabs", {})
        result = listed.result
        self.assertEqual((result["ok"], result["outcome"], result["truncated"]), (True, "observed", False))
        self.assertEqual(result["data"]["tab_count"], 2)
        self.assertEqual([(t["url"], t["owned"], t["active"]) for t in result["data"]["tabs"]],
                         [("https://user.test/", False, True), ("https://agent.test/", True, False)])
        for entry in result["data"]["tabs"]:
            self.assertEqual(set(entry), {"tab_id", "url", "title", "owned", "active"})
            self.assertRegex(entry["tab_id"], r"^t_[A-Za-z0-9_-]{32}$")
        self.assertEqual(listed.receipt["commands"],
                         [{"sequence": 1, "action": "getTabs", "state": "replied", "finished": True}])
        self.assertEqual((listed.receipt["call"], listed.receipt["complete"]), (result["call"], True))

        opened = await broker.call("browser_tabs", {"action": "open", "url": "https://new.test/redirect"})
        new = opened.result["tab_id"]
        self.assertEqual((opened.result["ok"], opened.result["outcome"]), (True, "observed"))
        self.assertEqual(opened.result["data"],
                         {"tab_id": new, "url": "https://new.test/landed", "status": "complete"})
        command = extension.last("newTab")
        self.assertEqual((command["url"], command["wait"], command["timeout"], command["ownerId"],
                          command["includeSnapshot"]),
                         ("https://new.test/redirect", True, 15000, "flat-owner", False))

        for arguments, action in (({"url": "https://next.test/"}, "navigate"), ({"action": "back"}, "goBack"),
                                  ({"action": "forward"}, "goForward"), ({"action": "reload"}, "reloadTabBrowser")):
            with self.subTest(action=action):
                reply = await broker.call("browser_navigate", {"tab_id": new, **arguments})
                # A reply is not a load observation: navigate echoes the URL it was given.
                self.assertEqual((reply.result["ok"], reply.result["outcome"], reply.result["data"]),
                                 (True, "dispatched", {"tab_id": new}))
                self.assertEqual(extension.last(action)["tabId"], 10)
                self.assertEqual(reply.receipt["commands"][-1],
                                 {"sequence": 1, "action": action, "state": "replied", "finished": True, "tab_id": 10})

        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": new})
        self.assertEqual((closed.result["ok"], closed.result["outcome"]), (True, "dispatched"))
        again = await broker.call("browser_navigate", {"tab_id": new, "url": "https://next.test/"})
        self.assert_failed(again, "stale_tab_handle", "not_dispatched")
        self.assertIsNone(again.receipt)
        self.assertEqual(extension.actions(), ["getTabs", "newTab", "navigate", "goBack", "goForward",
                                               "reloadTabBrowser", "closeTab"])
        # Handles and call ids, never the backend integers.
        self.assertNotIn("tabId", json.dumps([listed.result, opened.result, closed.result]))

    async def test_read_mode_is_refused_by_the_broker_and_by_the_bridge(self):
        extension = self.extension()
        broker = await self.connect("read")
        handle = await self.handle(broker)
        for name, arguments in (("browser_tabs", {"action": "close", "tab_id": handle}),
                                ("browser_navigate", {"action": "back", "tab_id": handle}),
                                ("browser_click", {"tab_id": handle, "target": {"selector": "#go"}})):
            with self.subTest(name=name):
                reply = await broker.call(name, arguments)
                self.assert_failed(reply, "mode_restricted", "not_dispatched")
                self.assertIsNone(reply.receipt)
        # The broker admits the argument shape; the bridge's own mode policy refuses the URL.
        refused = await broker.call("browser_tabs", {"action": "open", "url": "file:///etc/passwd"})
        self.assert_failed(refused, "mode_restricted", "not_dispatched")
        self.assertEqual((refused.receipt["commands"], refused.receipt["complete"]), ([], True))
        self.assertEqual(extension.actions(), ["getTabs"])
        opened = await broker.call("browser_tabs", {"action": "open", "url": "https://ok.test/"})
        self.assertTrue(opened.result["ok"], opened.result)

    async def test_bridge_url_policy_applies_to_flat_navigation(self):
        extension = self.extension()
        status, _, text = _request(self.bridge.http_port, "POST", "/api/set-policy", self.bridge.auth(),
                                   self.bridge.host, json.dumps({"deny": [r"blocked\.test"]}))
        self.assertEqual(status, 200, text)
        broker = await self.connect()
        handle = await self.handle(broker)
        for name, arguments in (("browser_navigate", {"tab_id": handle, "url": "https://blocked.test/a"}),
                                ("browser_tabs", {"action": "open", "url": "https://blocked.test/b"})):
            with self.subTest(name=name):
                reply = await broker.call(name, arguments)
                self.assert_failed(reply, "browser_operation_failed", "not_dispatched")
                self.assertEqual(reply.receipt["commands"], [])
        self.assertEqual(extension.actions(), ["getTabs"])
        allowed = await broker.call("browser_navigate", {"tab_id": handle, "url": "https://allowed.test/"})
        self.assertTrue(allowed.result["ok"], allowed.result)

    async def test_unavailable_forged_and_foreign_requests_send_nothing(self):
        extension = self.extension()
        broker = await self.connect()
        other = await self.connect()
        handle = await self.handle(broker)
        foreign = await self.handle(other)
        goto = {"tab_id": handle, "url": "https://next.test/"}
        for name, arguments, code in (
                ("browser_click", {"tab_id": handle, "target": {"selector": "#go"}}, "operation_unavailable"),
                ("browser_read", {"tab_id": handle}, "operation_unavailable"),
                ("browser_evaluate", {}, "unknown_operation"), ("browser_script", {"code": "1"}, "unknown_operation"),
                ("/api/set-policy", {}, "unknown_operation"),
                ("browser_navigate", {**goto, "headers": {"X-Kitsune-Mode": "act"}}, "invalid_arguments"),
                ("browser_navigate", {**goto, "ownerId": "other"}, "invalid_arguments"),
                ("browser_navigate", {**goto, "tab_id": 9}, "invalid_arguments"),
                ("browser_navigate", {**goto, "tab_id": "9"}, "invalid_arguments"),
                ("browser_navigate", {**goto, "tab_id": "t_forged"}, "invalid_tab_handle"),
                ("browser_navigate", {**goto, "tab_id": foreign}, "invalid_tab_handle")):
            with self.subTest(name=name, arguments=arguments):
                reply = await broker.call(name, arguments)
                self.assert_failed(reply, code, "not_dispatched")
                self.assertIsNone(reply.receipt)
        long_name = await broker.call("x" * 5000, {})
        self.assertEqual(long_name.result["operation"], "unknown")
        self.assertEqual(extension.actions(), ["getTabs", "getTabs"])
        scoped = await self.connect(operations=frozenset({"browser_tabs"}))
        own = await self.handle(scoped)
        self.assert_failed(await scoped.call("browser_navigate", {"tab_id": own, "url": "https://next.test/"}),
                           "operation_not_allowed", "not_dispatched")
        self.assertNotIn("navigate", extension.actions())

    async def test_reconnect_invalidates_handles_until_tabs_are_listed_again(self):
        first = self.extension()
        broker = await self.connect()
        old = await self.handle(broker)
        first.close()
        second = self.extension()
        stale = await broker.call("browser_navigate", {"tab_id": old, "url": "https://next.test/"})
        self.assert_failed(stale, "stale_tab_handle", "not_dispatched")
        self.assertEqual(stale.receipt["commands"], [])  # the bridge itself refused the old binding
        self.assert_failed(await broker.call("browser_tabs", {"action": "open"}), "stale_connection", "not_dispatched")
        self.assertEqual(second.actions(), [])
        fresh = await self.handle(broker)
        self.assertNotEqual(fresh, old)
        self.assert_failed(await broker.call("browser_navigate", {"tab_id": old, "url": "https://next.test/"}),
                           "stale_tab_handle", "not_dispatched")
        moved = await broker.call("browser_navigate", {"tab_id": fresh, "url": "https://next.test/"})
        self.assertTrue(moved.result["ok"], moved.result)
        self.assertEqual(second.actions(), ["getTabs", "navigate"])
        self.assertEqual(second.last("navigate")["tabId"], 9)

    async def test_disconnect_after_dispatch_is_unknown_and_never_replayed(self):
        extension = self.extension()
        extension.behavior["navigate"] = lambda command: DROP
        broker = await self.connect()
        handle = await self.handle(broker)
        lost = await broker.call("browser_navigate", {"tab_id": handle, "url": "https://next.test/"})
        self.assert_failed(lost, "extension_disconnected", "unknown")
        self.assertEqual(lost.receipt["commands"],
                         [{"sequence": 1, "action": "navigate", "state": "dispatched", "finished": True, "tab_id": 9}])
        self.assertEqual(extension.actions().count("navigate"), 1)
        after = await broker.call("browser_navigate", {"tab_id": handle, "url": "https://next.test/"})
        self.assert_failed(after, "stale_tab_handle", "not_dispatched")
        self.assertEqual(extension.actions().count("navigate"), 1)

    async def test_extension_fields_cannot_forge_a_receipt_or_reach_the_result(self):
        extension = self.extension()
        forged = {"receipt": {"complete": True, "commands": []}, "outcome": "observed", "call": "c_forged",
                  "confirmToken": "secret-token", "snapshot": '- button "x" [ref=e1]', "dispatched": False}
        extension.behavior["newTab"] = lambda command: {
            "ok": True, "tabId": 11, "url": "https://x.test/", "status": "loading", **forged}
        extension.behavior["getTabs"] = lambda command: {
            **forged, "tabs": [{**tab(9, "https://agent.test/", owned=True), "confirmToken": "secret-token",
                                "receipt": "forged"}]}
        broker = await self.connect()
        listed = await broker.call("browser_tabs", {})
        self.assertEqual(set(listed.result["data"]["tabs"][0]), {"tab_id", "url", "title", "owned", "active"})
        opened = await broker.call("browser_tabs", {"action": "open", "url": "https://x.test/"})
        result = opened.result
        # The tab is loading: created, but its load was not observed.
        self.assertEqual((result["ok"], result["outcome"]), (True, "dispatched"))
        self.assertEqual(set(result["data"]), {"tab_id", "url", "status"})
        self.assertNotEqual(result["call"], "c_forged")
        self.assertEqual(opened.receipt["call"], result["call"])
        self.assertEqual(opened.receipt["commands"][0]["state"], "replied")
        for text in ("secret-token", "c_forged", "ref=e1"):
            self.assertNotIn(text, json.dumps([listed.result, result]))

        extension.behavior["newTab"] = lambda command: {"ok": True}
        self.assert_failed(await broker.call("browser_tabs", {"action": "open"}), "invalid_bridge_response", "dispatched")
        extension.behavior["newTab"] = lambda command: {"error": "boom", "code": "made_up_by_the_page"}
        self.assert_failed(await broker.call("browser_tabs", {"action": "open"}), "browser_operation_failed", "unknown")

    async def test_extension_admission_refusals_are_not_dispatched_but_other_errors_are_unknown(self):
        extension = self.extension()
        broker = await self.connect()
        handle = await self.handle(broker)
        goto = {"tab_id": handle, "url": "https://next.test/"}
        for code in ("user_tab_read_only", "user_active"):
            extension.behavior["navigate"] = lambda command, code=code: {
                "error": "nothing was done", "code": code, "retryable": False, "state": {"confirmToken": "secret"}}
            reply = await broker.call("browser_navigate", goto)
            self.assert_failed(reply, code, "not_dispatched")
            self.assertEqual(reply.receipt["commands"][0]["state"], "replied")
            self.assertEqual(reply.result["error"]["state"], {})
        extension.behavior["navigate"] = lambda command: {"ok": False, "timedOut": True, "url": command["url"]}
        self.assert_failed(await broker.call("browser_navigate", goto), "timeout", "unknown")
        extension.behavior["navigate"] = lambda command: {"error": "Tab 9 no longer exists", "code": "tab_closed"}
        self.assert_failed(await broker.call("browser_navigate", goto), "stale_tab_handle", "not_dispatched")
        sent = extension.actions().count("navigate")
        # The handle was retired on the extension's observation: the next use never reaches it.
        self.assert_failed(await broker.call("browser_navigate", goto), "stale_tab_handle", "not_dispatched")
        self.assertEqual(extension.actions().count("navigate"), sent)

    async def test_open_that_times_out_names_the_created_tab(self):
        extension = self.extension()
        extension.behavior["newTab"] = lambda command: {
            "ok": False, "timedOut": True, "tabId": 12, "url": "about:blank", "status": "loading"}
        broker = await self.connect()
        opened = await broker.call("browser_tabs", {"action": "open", "url": "https://slow.test/", "timeout_ms": 50})
        self.assert_failed(opened, "timeout", "dispatched")
        self.assertEqual(extension.last("newTab")["timeout"], 50)
        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": opened.result["tab_id"]})
        self.assertTrue(closed.result["ok"], closed.result)
        self.assertEqual(extension.last("closeTab")["tabId"], 12)

    async def test_oversized_inventories_are_bounded_without_replacing_handles(self):
        extension = self.extension()
        broker = await self.connect()
        handle = await self.handle(broker)
        agent = tab(9, "https://agent.test/", owned=True)
        extension.set_tabs(agent, *(tab(100 + i, "https://big.test/" + "a" * 1000) for i in range(400)))
        flooded = await broker.call("browser_tabs", {})
        self.assert_failed(flooded, "bridge_response_limit", "unknown")
        self.assertIsNone(flooded.receipt)
        self.assertTrue((await broker.call("browser_navigate", {"tab_id": handle, "url": "https://next.test/"}))
                        .result["ok"])

        extension.set_tabs(agent, tab(20, "https://long.test/" + "b" * 5000, title="c" * 1000))
        cut = await broker.call("browser_tabs", {})
        self.assertEqual((cut.result["ok"], cut.result["truncated"]), (True, True))
        kept, long = cut.result["data"]["tabs"]
        self.assertEqual((kept["tab_id"], "truncated" in kept), (handle, False))
        self.assertEqual((len(long["url"]), len(long["title"]), long["truncated"]), (2048, 256, True))

        extension.set_tabs(agent, *(tab(100 + i, "https://many.test/" + "d" * 600) for i in range(150)))
        limited = await broker.call("browser_tabs", {})
        self.assert_failed(limited, "output_limited", "observed")
        self.assertIs(limited.result["truncated"], True)
        self.assertLess(len(json.dumps(limited.result)), 1024)

    async def test_broker_requires_a_matching_connected_bridge_and_never_starts_one(self):
        with self.assertRaises(OperationRefused) as none_connected:
            await self.connect()
        self.assertEqual(none_connected.exception.code, "bridge_not_ready")
        self.extension(version="0.0.0-test")
        with self.assertRaises(OperationRefused) as other_version:
            await self.connect()
        self.assertEqual(other_version.exception.code, "bridge_not_ready")
        with patch.object(bridge_client, "BRIDGE", f"http://127.0.0.1:{_free_port()}"), \
                patch.object(bridge_client.subprocess, "Popen", side_effect=AssertionError("started a bridge")), \
                self.assertRaises(OperationRefused) as absent:
            await self.connect()
        self.assertEqual(absent.exception.code, "bridge_not_ready")

    async def test_receipts_are_an_explicit_bound_request_on_reviewed_routes(self):
        extension = self.extension()
        binding = ConnectionBinding.from_status(self.bridge.status())
        self.assertEqual(self.bridge.status()["dispatch_receipts"], 1)
        bound = {CONNECTION_HEADER: binding.header_value()}

        def call(method, path, headers, body=None):
            status, _, text = _request(self.bridge.http_port, method, path, {**self.bridge.auth(), **headers},
                                       self.bridge.host, body)
            return status, json.loads(text)

        for method, path, headers, body in (
                ("GET", "/api/tabs", {RECEIPT_HEADER: "c_1"}, None),
                ("POST", "/api/click", {**bound, RECEIPT_HEADER: "c_1"}, json.dumps({"tabId": 9, "selector": "#x"})),
                ("POST", "/api/batch", {**bound, RECEIPT_HEADER: "c_1"}, json.dumps({"commands": []})),
                ("GET", "/api/tabs", {**bound, RECEIPT_HEADER: "bad id!"}, None),
                ("GET", "/api/tabs", {**bound, RECEIPT_HEADER: "c_1", RECEIPT_HEADER.lower(): "c_1"}, None)):
            with self.subTest(path=path, headers=headers):
                status, result = call(method, path, headers, body)
                self.assertEqual((status, result["code"]), (400, "invalid_receipt_request"))
        self.assertEqual(extension.actions(), [])
        status, envelope = call("GET", "/api/tabs", {**bound, RECEIPT_HEADER: "c_1"})
        self.assertEqual((status, set(envelope), envelope["receipt"]["call"]), (200, {"result", "receipt"}, "c_1"))
        status, legacy = call("GET", "/api/tabs", {})
        self.assertEqual((status, set(legacy)), (200, {"tabs"}))


class ReceiptTests(unittest.TestCase):
    def envelope(self, **receipt):
        return {"result": {}, "receipt": {"version": 1, "call": "c_1", "requested": 1, "limited": False,
                                          "complete": True, "commands": [
                                              {"sequence": 1, "action": "navigate", "state": "replied",
                                               "finished": True, "tab_id": 9}], **receipt}}

    def test_only_the_exact_bounded_envelope_of_this_call_is_accepted(self):
        self.assertEqual(flat_broker._receipt(self.envelope(), "c_1")["call"], "c_1")
        command = self.envelope()["receipt"]["commands"][0]
        for invalid in ({"ok": True}, [], None, {**self.envelope(), "extra": 1},
                        self.envelope(call="c_2"), self.envelope(version=2), self.envelope(requested=2),
                        self.envelope(complete="true"), self.envelope(commands="none"),
                        self.envelope(commands=[{**command, "sequence": 2}]),
                        self.envelope(commands=[{**command, "state": "observed"}]),
                        self.envelope(commands=[{**command, "tab_id": "9"}]),
                        self.envelope(commands=[{**command, "note": "forged"}]),
                        self.envelope(commands=[{**command, "finished": False}]),
                        self.envelope(requested=200, commands=[{**command, "sequence": i} for i in range(1, 201)])):
            with self.subTest(invalid=str(invalid)[:80]), self.assertRaises((ValueError, TypeError)):
                flat_broker._receipt(invalid, "c_1")

    def test_outcome_is_conservative(self):
        def outcome(state, complete=True, action="navigate"):
            receipt = self.envelope(complete=complete)["receipt"]
            receipt["commands"][0]["state"] = state
            return flat_broker._outcome(receipt, action)

        self.assertEqual([outcome(state) for state in ("not_dispatched", "unknown", "dispatched", "replied")],
                         ["not_dispatched", "unknown", "unknown", "dispatched"])
        self.assertEqual(outcome("replied", complete=False), "unknown")
        # Another command's reply (a preparatory read) is not this action's dispatch.
        self.assertEqual(outcome("replied", action="closeTab"), "not_dispatched")

    def test_receipt_counts_queued_work_and_stops_at_its_limit(self):
        receipt = DispatchReceipt("c_1")
        receipt.task_started()
        self.assertIs(receipt.snapshot()["complete"], False)
        record = receipt.begin("navigate", {"tabId": 9})
        receipt.task_finished()
        self.assertIs(receipt.snapshot()["complete"], False)  # begun, not finished
        receipt.finish(record)
        self.assertEqual(receipt.snapshot(), self.envelope(commands=[
            {"sequence": 1, "action": "navigate", "state": "not_dispatched", "finished": True, "tab_id": 9}])["receipt"])
        with patch.object(dispatch_receipts, "MAX_NATIVE_COMMANDS", 1):
            self.assertIsNone(receipt.begin("navigate", {"tabId": 9}))
        snapshot = receipt.snapshot()
        self.assertEqual((snapshot["requested"], snapshot["limited"], len(snapshot["commands"])), (2, True, 1))
        flat_broker._receipt({"result": {}, "receipt": snapshot}, "c_1")

    def send(self, socket, action="navigate", limit=dispatch_receipts.MAX_NATIVE_COMMANDS, timeout=0.01):
        receipt = DispatchReceipt("c_1")

        async def run():
            token = bridge._dispatch_receipt.set(receipt)
            try:
                return await bridge._send_to_extension_raw(action, {"tabId": 9}, timeout=timeout)
            finally:
                bridge._dispatch_receipt.reset(token)

        with patch.object(bridge, "extension_ws", socket), patch.object(bridge, "_ws_send_lock", None), \
                patch.dict(bridge.pending_commands, clear=True), \
                patch.object(dispatch_receipts, "MAX_NATIVE_COMMANDS", limit):
            return asyncio.run(run()), receipt.snapshot()

    def test_bridge_send_states(self):
        class Socket:
            def __init__(self, mode):
                self.mode, self.sent = mode, 0

            async def send(self, message):
                self.sent += 1
                if self.mode == "raise":
                    raise RuntimeError("write failed part-way")
                if self.mode in {"reply", "lost"}:
                    pending = bridge.pending_commands[json.loads(message)["id"]]
                    # As ws_handler does: only an extension's own message counts as its reply.
                    pending["replied"] = self.mode == "reply"
                    pending["future"].set_result({"ok": True} if self.mode == "reply" else
                                                 bridge._disconnected_error("Extension disconnected", "navigate"))

        for mode, state in (("reply", "replied"), ("silent", "dispatched"), ("lost", "dispatched"),
                            ("raise", "unknown")):
            with self.subTest(mode=mode):
                socket = Socket(mode)
                _, snapshot = self.send(socket)
                self.assertEqual((socket.sent, snapshot["complete"]), (1, True))
                self.assertEqual(snapshot["commands"][0]["state"], state)
        result, snapshot = self.send(None)
        self.assertEqual((result["code"], snapshot["commands"][0]["state"]), ("extension_disconnected", "not_dispatched"))
        socket = Socket("reply")
        result, snapshot = self.send(socket, limit=0)
        self.assertEqual((result["code"], result["dispatched"], socket.sent), ("native_operation_limit", False, 0))
        self.assertEqual((snapshot["limited"], snapshot["commands"]), (True, []))


if __name__ == "__main__":
    unittest.main()
