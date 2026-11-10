"""Connection preconditions for flat handles; these are not authorization grants."""
import asyncio
from email.message import Message
import json
import threading
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge
from kitsune.connection_binding import CONNECTION_HEADER, ConnectionBinding


HEADER = "X-Kitsune-Connection"
INSTANCE = "a" * 32


class ReplyingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        command = json.loads(message)
        self.sent.append(command)
        bridge.pending_commands[command["id"]]["future"].set_result({"ok": True})


class ConnectionBindingTests(unittest.TestCase):
    def setUp(self):
        self.socket = ReplyingSocket()
        for name, value in (("BRIDGE_INSTANCE", INSTANCE), ("extension_ws_generation", 7),
                            ("extension_ws", self.socket), ("_ws_send_lock", None),
                            ("pending_commands", {})):
            patcher = patch.object(bridge, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def dispatch(self, headers=(), route=None, path="/api/click"):
        handler = object.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = Message()
        for name, value in headers:
            handler.headers[name] = value
        handler.refuse = Mock()
        handler.drain_body = Mock()
        route = route or Mock()
        handler._dispatch({path: route})
        return handler, route

    def assert_stale(self, result):
        self.assertEqual(result["code"], "stale_connection")
        self.assertIs(result["dispatched"], False)
        self.assertIs(result["retryable"], False)

    def test_old_instance_or_generation_never_reaches_handler(self):
        for value in (f'{"b" * 32}:7', f"{INSTANCE}:6", f"{INSTANCE}:8"):
            with self.subTest(value=value):
                handler, route = self.dispatch([(HEADER, value)])
                route.assert_not_called()
                status, result = handler.refuse.call_args.args
                self.assertEqual(status, 409)
                self.assert_stale(result)
        self.assertEqual(self.socket.sent, [])

    def test_absent_precondition_preserves_legacy_dispatch(self):
        handler, route = self.dispatch()
        route.assert_called_once_with()
        handler.refuse.assert_not_called()

    def test_malformed_or_duplicate_preconditions_are_not_ignored(self):
        for headers in ([(HEADER, "")], [(HEADER, "anything")], [(HEADER, f"{INSTANCE}:0")],
                        [(HEADER, f"{INSTANCE}:-1")], [(HEADER, f"{INSTANCE}:01")],
                        [(HEADER, f"{INSTANCE}:1.0")], [(HEADER, f"{INSTANCE}:" + "9" * 100)],
                        [(HEADER, f"{INSTANCE}:7"), (HEADER.lower(), f"{INSTANCE}:7")]):
            with self.subTest(headers=headers):
                handler, route = self.dispatch(headers)
                route.assert_not_called()
                status, result = handler.refuse.call_args.args
                self.assertEqual((status, result["code"]), (400, "invalid_connection_binding"))

    def test_matching_binding_reaches_send_without_granting_a_mode(self):
        results = []
        handler, _ = self.dispatch([(HEADER, f"{INSTANCE}:7")],
                                  lambda: results.append(asyncio.run(
                                      bridge._send_to_extension_raw("click", {"tabId": 9}))))
        handler.refuse.assert_not_called()
        self.assertEqual(results, [{"ok": True}])
        self.assertEqual(len(self.socket.sent), 1)
        self.assertEqual(self.socket.sent[0]["tabId"], 9)
        handler, route = self.dispatch([(HEADER, f"{INSTANCE}:7"), ("X-Kitsune-Mode", "read")])
        route.assert_not_called()
        self.assertEqual(handler.refuse.call_args.args[1]["code"], "mode_restricted")

    def test_reconnection_after_http_admission_is_rechecked_before_send(self):
        results = []

        def route():
            bridge.extension_ws_generation += 1
            results.append(asyncio.run(bridge._send_to_extension_raw("click", {"tabId": 9})))

        self.dispatch([(HEADER, f"{INSTANCE}:7")], route)
        self.assert_stale(results[0])
        self.assertEqual(self.socket.sent, [])
        self.assertEqual(bridge.pending_commands, {})

    def test_generation_changes_while_queued_are_checked_under_send_lock(self):
        async def run():
            lock = bridge._get_ws_send_lock()
            await lock.acquire()
            task = asyncio.create_task(bridge._send_to_extension_raw("click", {"tabId": 9}))
            try:
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                # The binding, not just socket object identity, must still match.
                bridge.extension_ws_generation += 1
            finally:
                lock.release()
            return await task

        results = []
        self.dispatch([(HEADER, f"{INSTANCE}:7")], lambda: results.append(asyncio.run(run())))
        self.assert_stale(results[0])
        self.assertEqual(self.socket.sent, [])
        self.assertEqual(bridge.pending_commands, {})

    def test_disconnected_binding_does_not_reach_handler(self):
        with patch.object(bridge, "extension_ws", None):
            handler, route = self.dispatch([(HEADER, f"{INSTANCE}:7")])
        route.assert_not_called()
        self.assert_stale(handler.refuse.call_args.args[1])

    def test_http_context_crosses_the_real_run_async_thread_boundary(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()
        results = []

        async def after_admission():
            bridge.extension_ws_generation += 1
            return await bridge._send_to_extension_raw("click", {"tabId": 9})

        try:
            with patch.object(bridge, "loop", loop):
                runner = object.__new__(bridge.BridgeHandler)
                self.dispatch([(HEADER, f"{INSTANCE}:7")],
                              lambda: results.append(runner.run_async(after_admission(), timeout=2)))
            self.assert_stale(results[0])
            self.assertEqual(self.socket.sent, [])
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            loop.close()

    def test_reconnect_after_send_does_not_claim_nothing_was_dispatched(self):
        async def accept_without_reply(message):
            self.socket.sent.append(json.loads(message))
            bridge.extension_ws_generation += 1

        results = []
        with patch.object(self.socket, "send", accept_without_reply):
            self.dispatch([(HEADER, f"{INSTANCE}:7")], lambda: results.append(asyncio.run(
                bridge._send_to_extension_raw("click", {"tabId": 9}, timeout=0.001))))
        self.assertEqual(results[0]["code"], "timeout")
        self.assertIs(results[0]["retryable"], False)
        self.assertNotIn("dispatched", results[0])
        self.assertEqual(len(self.socket.sent), 1)
        self.assertEqual(bridge.pending_commands, {})

    def test_body_fields_cannot_replace_transport_precondition(self):
        results = []

        def route():
            bridge.extension_ws_generation += 1
            results.append(asyncio.run(bridge._send_to_extension_raw("click", {
                "tabId": 9, "bridge_instance": INSTANCE, "extension_generation": 8,
                "headers": {HEADER: f"{INSTANCE}:8"}})))

        self.dispatch([(HEADER, f"{INSTANCE}:7")], route)
        self.assert_stale(results[0])
        self.assertEqual(self.socket.sent, [])

    def test_request_binding_is_reset_even_when_handler_raises(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            self.dispatch([(HEADER, f"{INSTANCE}:7")], Mock(side_effect=RuntimeError("fixture")))
        bridge.extension_ws_generation += 1
        handler, route = self.dispatch()
        handler.refuse.assert_not_called()
        route.assert_called_once_with()
        self.assertEqual(asyncio.run(bridge._send_to_extension_raw("getTabs")), {"ok": True})

    def test_status_identifies_process_and_extension_generation(self):
        handler = object.__new__(bridge.BridgeHandler)
        handler.path = "/api/status"
        handler.send_json = Mock()
        handler.handle_status()
        status, result = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result["bridge_instance"], INSTANCE)
        self.assertEqual(result["extension_generation"], 7)


class ConnectionBindingValueTests(unittest.TestCase):
    def test_round_trip_and_immutability(self):
        from dataclasses import FrozenInstanceError
        binding = ConnectionBinding.from_status({"bridge_instance": INSTANCE, "extension_generation": 7,
                                                 "extension_connected": True})
        self.assertEqual(CONNECTION_HEADER, HEADER)
        self.assertEqual(binding.header_value(), f"{INSTANCE}:7")
        self.assertEqual(ConnectionBinding.from_header(binding.header_value()), binding)
        with self.assertRaises(FrozenInstanceError):
            binding.extension_generation = 8

    def test_missing_or_invalid_status_has_no_legacy_fallback(self):
        good = {"bridge_instance": INSTANCE, "extension_generation": 7, "extension_connected": True}
        for invalid in ({}, {**good, "bridge_instance": None}, {**good, "extension_generation": True},
                        {**good, "extension_generation": 0}, {**good, "extension_generation": 2**63},
                        {**good, "extension_connected": False}, {**good, "extension_connected": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ConnectionBinding.from_status(invalid)


if __name__ == "__main__":
    unittest.main()
