"""Action error contract: stable codes, retry safety, and no blind repeats of writes."""
import asyncio
import json
import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from kitsune import bridge, playwright_tools, server


class SilentSocket:
    """An extension socket that accepts commands and never replies."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class ReplyingSocket(SilentSocket):
    """An extension socket that answers every command with reply."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    async def send(self, message):
        await super().send(message)
        bridge.pending_commands[json.loads(message)["id"]]["future"].set_result(dict(self.reply))


class BridgeTransportErrorTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("extension_ws", None), ("_ws_send_lock", None)):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pending = patch.dict(bridge.pending_commands, clear=True)
        pending.start()
        self.addCleanup(pending.stop)

    def test_not_connected_is_retryable_because_nothing_was_sent(self):
        for action in ("submitForm", "getSnapshot"):
            with self.subTest(action=action):
                result = asyncio.run(bridge._send_to_extension_raw(action, {}))
                self.assertTrue(result["error"].startswith("Kitsune extension not connected"))
                self.assertEqual(result["code"], "extension_disconnected")
                self.assertIs(result["retryable"], True)
                self.assertEqual(result["state"], {"connected": False})
                self.assertIn("browser_status", result["hint"])

    def test_timeout_after_send_is_retryable_only_for_reads(self):
        for action, retryable in (("submitForm", False), ("click", False), ("keypress", False),
                                  ("uploadCommit", False), ("getSnapshot", True), ("getTabs", True)):
            with self.subTest(action=action), patch.object(bridge, "extension_ws", SilentSocket()):
                result = asyncio.run(bridge._send_to_extension_raw(action, {}, timeout=0.01))
                self.assertEqual(result["error"], "Command timed out after 0.01s")
                self.assertEqual(result["code"], "timeout")
                self.assertIs(result["retryable"], retryable)
                self.assertEqual(result["state"], {"timeoutSeconds": 0.01})
                self.assertEqual(len(bridge.extension_ws.sent), 1)

    def test_unsupported_selector_stays_non_retryable_for_reads_and_writes(self):
        # #219: the selector failed to parse before anything was dispatched; repeating cannot help.
        unsupported = {"error": "Unsupported selector: XPath other than \"..\" (the parent element)",
                       "code": "unsupported_selector", "retryable": False, "hint": "Pass a ref"}
        for action in ("getSnapshot", "getHTML", "click", "hover"):
            with self.subTest(action=action), patch.object(bridge, "extension_ws", ReplyingSocket(unsupported)):
                result = asyncio.run(bridge._send_to_extension_raw(action, {"selector": "xpath=//ul"}))
                self.assertEqual(result, unsupported)
        # Other read failures stay safe to repeat.
        not_found = {"error": "Element not found", "code": "not_found", "retryable": False}
        with patch.object(bridge, "extension_ws", ReplyingSocket(not_found)):
            self.assertIs(asyncio.run(bridge._send_to_extension_raw("getHTML", {}))["retryable"], True)


class RetryActionTests(unittest.TestCase):
    def run_retry(self, replies, **params):
        command = {"action": "retry", "command": {"action": "submitForm", "selector": "#pay"},
                   "maxAttempts": 3, "backoffMs": 0, **params}
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_invalidate_cache_for_action"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          side_effect=replies) as send:
            return asyncio.run(bridge.run_command(command)), send

    def test_unknown_outcome_is_never_repeated(self):
        timeout = bridge._action_error("Command timed out after 30s", "timeout",
                                       retryable=False, hint="check", timeoutSeconds=30)
        result, send = self.run_retry([timeout, {"ok": True}])
        self.assertEqual(result, {"ok": False, "attempts": 1, "result": timeout, "reason": "not retryable"})
        send.assert_awaited_once_with("submitForm", {"selector": "#pay"})

    def test_not_retryable_wins_over_on_errors(self):
        timeout = {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}
        result, send = self.run_retry([timeout], onErrors=["timed out"])
        self.assertEqual(result["reason"], "not retryable")
        send.assert_awaited_once()

    def test_failures_before_dispatch_are_retried(self):
        missing = {"error": "Element not found", "code": "not_found", "retryable": True, "hint": "snapshot"}
        result, send = self.run_retry([missing, {"ok": True}])
        self.assertEqual(result, {"ok": True, "attempts": 2, "result": {"ok": True}})
        self.assertEqual(send.await_count, 2)

    def test_unclassified_errors_keep_previous_retry_behavior(self):
        result, send = self.run_retry([{"error": "boom"}] * 3)
        self.assertEqual(result, {"ok": False, "attempts": 3, "result": {"error": "boom"}})
        self.assertEqual(send.await_count, 3)


class ServerTransportErrorTests(unittest.TestCase):
    def test_unreachable_bridge_is_retryable(self):
        with patch.object(server.httpx, "request", side_effect=httpx.ConnectError("refused")), \
             patch.object(server, "_ensure_bridge", return_value=None):
            result = server._request("POST", "/api/click", json={}, timeout=1)
        self.assertTrue(result["error"].startswith("Kitsune bridge is not reachable"))
        self.assertEqual((result["code"], result["retryable"]), ("bridge_unavailable", True))

        with patch.object(server.httpx, "request", side_effect=httpx.ConnectError("refused")), \
             patch.object(server, "_ensure_bridge",
                          return_value=server._bridge_unavailable("Cannot start Kitsune bridge: denied")):
            result = server._request("POST", "/api/click", json={}, timeout=1)
        self.assertEqual(result["error"], "Cannot start Kitsune bridge: denied")
        self.assertEqual(result["code"], "bridge_unavailable")

    def test_timeout_is_retryable_only_for_get(self):
        for method, retryable in (("POST", False), ("GET", True)):
            with self.subTest(method=method), \
                 patch.object(server.httpx, "request", side_effect=httpx.ReadTimeout("read timed out")):
                result = server._request(method, "/api/submit-form", timeout=1)
            self.assertEqual(result["error"], "read timed out")
            self.assertEqual((result["code"], result["retryable"]), ("timeout", retryable))

    def test_closed_connection_says_whether_the_command_may_have_run(self):
        # #235: a reset reached the agent as a bare error string.
        for method, error, sent, retryable in (
                ("POST", httpx.ReadError("[Errno 54] Connection reset by peer"), True, False),
                ("POST", httpx.RemoteProtocolError("Server disconnected without sending a response."), True, False),
                ("GET", httpx.ReadError("[Errno 54] Connection reset by peer"), True, True),
                ("POST", httpx.WriteError("[Errno 32] Broken pipe"), False, True)):
            with self.subTest(method=method, error=type(error).__name__), \
                 patch.object(server.httpx, "request", side_effect=error), \
                 patch.object(server, "_ensure_bridge") as ensure:
                result = server._request(method, "/api/check", json={}, timeout=1)
            ensure.assert_not_called()
            self.assertEqual(result["error"], str(error))
            self.assertEqual((result["code"], result["retryable"], result["state"]),
                             ("bridge_disconnected", retryable, {"requestSent": sent}))
            self.assertEqual("may have run" in result["hint"], sent and method == "POST", result["hint"])

    def test_bridge_error_fields_pass_through_unchanged(self):
        error = {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
                 "hint": "snapshot", "state": {"reason": "other_document", "ref": "s1e0", "tabId": 7}}
        with patch.object(server.httpx, "request", return_value=Mock(json=Mock(return_value=error))):
            self.assertEqual(server.browser_click(ref="s1e0", tab_id=7), error)


class McpErrorResultTests(unittest.TestCase):
    """Failures reach MCP clients as isError results through FastMCP serialization."""

    def setUp(self):
        # The lowlevel server logs every request at INFO.
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        playwright_tools._current_tab = None

    def call(self, name, arguments, reply=None, mcp=server.mcp, **request):
        if reply is not None:
            request["return_value"] = Mock(json=Mock(return_value=reply))

        async def run():
            async with create_connected_server_and_client_session(mcp._mcp_server) as session:
                return await session.call_tool(name, arguments)

        with patch.object(server.httpx, "request", **request):
            return asyncio.run(run())

    def assertMcpResult(self, result, payload, is_error):
        self.assertIs(result.isError, is_error)
        self.assertEqual(len(result.content), 1)
        self.assertEqual(json.loads(result.content[0].text), payload)

    def registered(self, *functions):
        mcp = FastMCP("test")
        for function in functions:
            mcp.tool()(server._mcp_errors(function))
        return mcp

    def test_stale_and_missing_refs_are_errors_with_full_details(self):
        stale = {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
                 "hint": "A newer snapshot replaced this ref; state.currentRef is the same element in it.",
                 "state": {"reason": "snapshot_replaced", "ref": "s1e0", "latestObservation": "browser_click",
                           "currentRef": "s2e3", "tabId": 7, "url": "https://example.com/é",
                           "status": "complete"}}
        missing = {"error": "Element not found", "code": "not_found", "retryable": True,
                   "hint": "No element matches; take a new browser_snapshot."}
        for name, arguments, reply in (("browser_click", {"target": "e1"}, stale),
                                       ("browser_type", {"target": "e1", "text": "hi"}, stale),
                                       ("browser_snapshot", {"target": "e1"}, stale),
                                       ("browser_click", {"target": "#gone"}, missing)):
            with self.subTest(name=name, code=reply["code"]):
                self.assertMcpResult(self.call(name, arguments, reply), reply, True)

    def test_extension_disconnected_is_an_error(self):
        with patch.object(bridge, "extension_ws", None):
            disconnected = asyncio.run(bridge._send_to_extension_raw("click", {}))
        self.assertEqual(disconnected["code"], "extension_disconnected")
        self.assertMcpResult(self.call("browser_click", {"target": "e1"}, disconnected), disconnected, True)

    def test_bridge_unavailable_is_an_error(self):
        with patch.object(server, "_ensure_bridge",
                          return_value=server._bridge_unavailable("Cannot start Kitsune bridge: denied")):
            result = self.call("browser_snapshot", {}, side_effect=httpx.ConnectError("refused"))
        self.assertMcpResult(result, server._bridge_unavailable("Cannot start Kitsune bridge: denied"), True)

    def test_server_validation_and_ok_false_are_errors(self):
        self.assertMcpResult(self.call("browser_tabs", {"action": "select"}),
                             {"error": "index is required for action=select"}, True)
        timed_out = {"ok": False, "timedOut": True, "tabId": 9, "url": "https://example.com/"}
        # Core browser_tabs new reports the tab's index, not its tabId (#224).
        self.assertMcpResult(self.call("browser_tabs", {"action": "new", "url": "https://example.com/"}, timed_out),
                             {"ok": False, "timedOut": True, "url": "https://example.com/"}, True)

    def test_successes_and_warnings_are_not_errors(self):
        warned = {"ok": True, "tag": "input", "warning": {"code": "hidden_or_covered",
                  "state": {"reason": "not_rendered"}, "hint": "prefer its label"}}
        self.assertMcpResult(self.call("browser_click", {"target": "e1"}, warned), warned, False)
        # The click happened; only the expected navigation was not observed.
        clicked = {"ok": True, "clicked": True, "navigation": {"ok": False, "timedOut": True}}
        self.assertMcpResult(self.call("browser_click_and_wait_navigation", {"ref": "s1e0"}, clicked,
                                       mcp=self.registered(server.browser_click_and_wait_navigation)),
                             clicked, False)
        status = self.call("browser_status", {}, {"status": "running", "extension_connected": False},
                           mcp=self.registered(server.browser_status))
        self.assertIs(status.isError, False)
        # browser_status also reports the published tool surface (#171).
        self.assertEqual({key: value for key, value in json.loads(status.content[0].text).items()
                          if key != "mcp_tools"}, {"status": "running", "extension_connected": False})

    def test_containers_report_per_command_failures_without_failing_the_call(self):
        mcp = self.registered(server.browser_batch, server.browser_parallel, server.browser_retry)
        partial = {"results": [{"ok": True, "snapshot": "- button \"Pay\" [ref=s2e0]"},
                               {"error": "stale ref, call browser_snapshot again", "code": "stale_ref",
                                "retryable": True}]}
        self.assertMcpResult(self.call("browser_batch", {"commands": [{"action": "click"}] * 2}, partial,
                                       mcp=mcp), partial, False)
        sequences = {"results": [[partial["results"]]]}
        self.assertMcpResult(self.call("browser_parallel", {"sequences": [[{"action": "click"}] * 2]},
                                       sequences, mcp=mcp), sequences, False)
        unavailable = server._bridge_unavailable("Kitsune bridge is not reachable")
        self.assertMcpResult(self.call("browser_batch", {"commands": []}, unavailable, mcp=mcp), unavailable, True)
        exhausted = {"ok": False, "attempts": 1, "reason": "not retryable",
                     "result": {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}}
        self.assertMcpResult(self.call("browser_retry", {"command": {"action": "submitForm"}}, exhausted,
                                       mcp=mcp), exhausted, True)

    def test_outcome_not_observed_is_an_error_that_keeps_the_fresh_snapshot(self):
        missed = {"ok": False, "dispatched": True, "snapshot": "- button \"Pay\" [ref=s2e0]",
                  "target_ref": "s2e0", "error": "Expected outcome not observed within 5000ms",
                  "code": "outcome_not_observed", "retryable": False, "hint": "inspect the page"}
        self.assertMcpResult(self.call("browser_check", {"ref": "s1e0", "expect": {"url_changed": True}}, missed,
                                       mcp=self.registered(server.browser_check)), missed, True)

    def test_form_fill_fails_only_when_no_field_was_filled(self):
        mcp = self.registered(server.browser_form_fill)
        none = {"error": "No field was filled", "results": {"Email": {"error": "Not found"}}}
        self.assertMcpResult(self.call("browser_form_fill", {"fields": {"Email": "a@b"}}, none, mcp=mcp), none, True)
        partial = {"results": {"Email": {"ok": True, "tag": "input"}, "Color": {"error": "Not found"}}}
        self.assertMcpResult(self.call("browser_form_fill", {"fields": {"Email": "a@b", "Color": "x"}}, partial,
                                       mcp=mcp), partial, False)

    def test_non_json_bridge_response_keeps_http_status(self):
        response = Mock(status_code=502, text="<html>Bad gateway</html>",
                        json=Mock(side_effect=json.JSONDecodeError("Expecting value", "", 0)))
        result = self.call("browser_snapshot", {}, return_value=response)
        self.assertMcpResult(result, {"error": "Kitsune bridge returned HTTP 502 without a JSON body",
                                      "state": {"httpStatus": 502, "body": "<html>Bad gateway</html>"}}, True)

    def test_python_callers_still_get_plain_results(self):
        error = {"error": "Element not found", "code": "not_found", "retryable": True}
        with patch.object(server.httpx, "request", return_value=Mock(json=Mock(return_value=error))):
            self.assertEqual(server.browser_click(selector="#gone"), error)


class SessionFailureTests(unittest.TestCase):
    """Session save/load report failed extension commands instead of ok: true."""

    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(bridge, "_sessions_dir", directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch.object(bridge.asyncio, "sleep", AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)

    def send(self, replies):
        """A send_to_extension double answering by action; records every command."""
        self.sent = []

        async def send(action, params=None, timeout=30):
            self.sent.append(action)
            reply = replies[action]
            return reply() if callable(reply) else reply
        return patch.object(bridge, "send_to_extension", side_effect=send)

    def test_disconnected_save_fails_and_keeps_the_saved_session(self):
        good = {"cookies": {"cookies": [{"name": "sid", "value": "1", "domain": "example.com"}]},
                "newTab": {"tabId": 5}, "storageOp": {"ok": True, "items": {"k": "v"}}, "closeTab": {"ok": True}}
        with self.send(good):
            saved = asyncio.run(bridge._save_session_async("work", ["https://example.com/"]))
        self.assertIs(saved["ok"], True)
        path = bridge._session_path("work")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        with patch.object(bridge, "extension_ws", None):
            disconnected = asyncio.run(bridge._send_to_extension_raw("cookies", {}))
        with self.send({**good, "cookies": disconnected, "newTab": disconnected}):
            failed = asyncio.run(bridge._save_session_async("work", ["https://example.com/"]))
        self.assertEqual(failed["error"], "Session 'work' not saved: 2 read(s) failed")
        self.assertEqual([(f["step"], f["code"]) for f in failed["failures"]],
                         [("cookies", "extension_disconnected"), ("newTab", "extension_disconnected")])
        self.assertTrue(server._failed(failed))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "a failed save never replaces the saved session")

    def test_failed_storage_read_closes_its_tab_and_fails(self):
        with self.send({"cookies": {"cookies": []}, "newTab": {"tabId": 5},
                        "storageOp": {"error": "Cannot access browser internal page", "code": "restricted_page"},
                        "closeTab": {"ok": True}}):
            failed = asyncio.run(bridge._save_session_async("work", ["about:config"]))
        self.assertEqual(failed["failures"], [{"url": "about:config", "step": "storage",
                                               "error": "Cannot access browser internal page",
                                               "code": "restricted_page"}])
        self.assertEqual(self.sent[-1], "closeTab")
        self.assertFalse(bridge.os.path.exists(bridge._session_path("work")))

    def test_load_counts_only_writes_that_succeeded(self):
        bundle = {"cookies": [{"name": "a", "value": "1", "domain": "example.com"},
                              {"name": "b", "value": "2", "domain": "example.com"}],
                  "storage": {"https://example.com/": {"k": "v"}}}
        with open(bridge._session_path("work"), "w", encoding="utf-8") as f:
            json.dump(bundle, f)
        cookie_replies = iter([{"ok": True}, {"error": "Extension disconnected", "code": "extension_disconnected"}])
        with self.send({"cookies": lambda: next(cookie_replies), "newTab": {"tabId": 5},
                        "storageOp": {"ok": True, "restored": 1}, "closeTab": {"ok": True}}):
            partial = asyncio.run(bridge._load_session_async("work"))
        self.assertEqual(partial["error"], "Session 'work' partly restored: 1 write(s) failed")
        self.assertEqual((partial["cookies_set"], partial["origins_restored"]), (1, 1))
        self.assertNotIn("ok", partial)
        self.assertEqual(partial["failures"][0]["code"], "extension_disconnected")
        with self.send({"cookies": {"ok": True}, "newTab": {"tabId": 5},
                        "storageOp": {"ok": True, "restored": 1}, "closeTab": {"ok": True}}):
            self.assertEqual(asyncio.run(bridge._load_session_async("work")),
                             {"ok": True, "name": "work", "cookies_set": 2, "origins_restored": 1})


if __name__ == "__main__":
    unittest.main()
