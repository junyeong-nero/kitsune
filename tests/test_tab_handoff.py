"""#396: isolated real lifecycle transport plus both browser_tabs facades and MCP approval."""
import asyncio
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import anyio
from mcp import ClientSession, types
from mcp.server.session import ServerSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams, create_connected_server_and_client_session
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from kitsune import bridge_client, playwright_tools, server, tools
from kitsune.task_modes import CLIENT_HEADER, OWNER_HEADER, SESSION_HEADER, CLIENT_NAME_HEADER
from test_bridge_security import Bridge, _request
from test_tab_inventory_bridge import EXTENSION_ORIGIN, ScriptedExtension


class HandoffExtension(ScriptedExtension):
    def __init__(self, bridge):
        self.commands = []
        self.request_result = None
        super().__init__(bridge, [])

    def _answer(self, command):
        with self.lock:
            self.commands.append(command)
        if command["action"] == "requestTab" and self.request_result is not None:
            return self.request_result(command)
        return {"ok": True}


class PendingHandoffExtension(HandoffExtension):
    """Keep the request in flight, but continue servicing the independent session channel."""
    def __init__(self, bridge):
        self.requested = threading.Event()
        self.pending = None
        self.timer = None
        super().__init__(bridge)

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
                        self.commands.append(command)
                    if command["action"] == "requestTab":
                        self.pending = command
                        self.requested.set()
                        # Bound the deliberately broken, pre-async implementation too: its event
                        # loop cannot process EOF, so let HTTP finish and expose the failed invariant.
                        def expire():
                            pending, self.pending = self.pending, None
                            if pending is not None:
                                ws.send(json.dumps({"id": pending["id"], "result": {"error": "test deadline"}}))
                        self.timer = threading.Timer(2, expire)
                        self.timer.daemon = True
                        self.timer.start()
                        continue
                    if command["action"] == "endHandoffSession" and self.pending is not None:
                        self.timer.cancel()
                        ws.send(json.dumps({"id": self.pending["id"], "result": {
                            "error": "Session ended", "code": "needs_user", "state": {"decision": "cancelled"}}}))
                        self.pending = None
                    ws.send(json.dumps({"id": command["id"], "result": {"ok": True}}))
        except ConnectionClosed:
            pass


OWNED = {"ok": True, "tabId": 1, "owned": True}


def not_handed_off(decision):
    return {"error": "The tab was not handed off", "code": "needs_user", "retryable": False,
            "state": {"reason": "approval", "detail": "tab_handoff", "decision": decision}, "hint": "own tab"}


class TogetherExtension(HandoffExtension):
    """requestTab as background.js answers a client that asks the user too (#494): the token at once
    (together), then a wait that the popup's answer (popup, popup_delay s after the wait) or the client's
    answer (the token, or the token and a decision) settles."""
    def __init__(self, bridge):
        self.popup, self.popup_delay = None, 0.3
        self.waiting, self.settled = None, False
        super().__init__(bridge)

    def reset(self, popup, popup_delay=0.3):
        with self.lock:
            self.popup, self.popup_delay = popup, popup_delay
            self.waiting, self.settled = None, False
            self.commands.clear()

    def requests(self):
        with self.lock:
            return [c for c in self.commands if c["action"] == "requestTab"]

    def _reply(self, command, result):
        with self.lock:
            self.ws.send(json.dumps({"id": command["id"], "result": result}))

    def _settle(self, result):
        """The request's answer: the wait returns it; True if it had none."""
        with self.lock:
            waiting, first = self.waiting, not self.settled
            self.waiting, self.settled = None, True
        if first and waiting is not None:
            self._reply(waiting, result)
        return first

    def _answer(self, command):
        with self.lock:
            self.commands.append(command)
        if command["action"] != "requestTab":
            return {"ok": True}
        if command.get("wait"):
            with self.lock:
                if self.settled:  # the client's answer came first: background.js has no request left
                    return not_handed_off("cancelled")
                self.waiting = command
            if self.popup is not None:
                timer = threading.Timer(self.popup_delay, self._settle, (self.popup,))
                timer.daemon = True
                timer.start()
            return None  # the answer settles it
        if command.get("confirmToken"):
            result = not_handed_off(command["decision"]) if "decision" in command else OWNED
            return result if self._settle(result) else not_handed_off("cancelled")
        return {"error": "Tab handoff needs approval", "code": "needs_user", "retryable": False, "state": {
            "reason": "approval", "detail": "tab_handoff", "action": "requestTab", "tabId": 1, "title": "Page",
            "url": "https://user.example/", "message": "Client A requests this tab: work", "confirmToken": "test-token",
            "expiresInSeconds": 120, "approval": "token", "together": True}}

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
                    result = self._answer(command)
                    if result is not None:
                        self._reply(command, result)
        except ConnectionClosed:
            pass


class HandoffTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name).start()
        self.addCleanup(self.bridge.stop)
        self.extension = HandoffExtension(self.bridge)
        self.addCleanup(self.extension.close)
        self.headers = {**self.bridge.auth(), CLIENT_HEADER: "process-a", OWNER_HEADER: "stable-owner",
                        SESSION_HEADER: "a" * 64, CLIENT_NAME_HEADER: "Client%20A"}

    def request(self, body, headers=None):
        status, _, data = _request(self.bridge.http_port, "POST", "/api/request-tab",
                                  headers=headers or self.headers, host=self.bridge.host, body=json.dumps(body))
        return status, json.loads(data)

    def channel(self, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.bridge.http_port, timeout=5)
        conn.request("GET", "/api/handoff-session", headers=headers or self.headers)
        response = conn.getresponse()
        self.addCleanup(conn.close)
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        return conn, response

    def wait_for(self, action, count=1):
        deadline = time.monotonic() + 3
        while self.extension.count(action) < count:
            if time.monotonic() > deadline:
                self.fail(f"No {action}: {self.bridge.log()}")
            time.sleep(.01)

    def test_channel_eof_revokes_and_json_has_no_authority(self):
        body = {"tabId": 1, "reason": "work", "sessionId": "forged", "ownerId": "other",
                "clientName": "forged", "handoffApproved": True}
        self.assertEqual(self.request(body)[0], 403)
        conn, response = self.channel()
        self.assertEqual(self.request(body)[0], 200)
        command = next(c for c in self.extension.commands if c["action"] == "requestTab")
        self.assertEqual(command["ownerId"], "stable-owner")
        self.assertEqual(command["clientName"], "Client A")
        self.assertEqual(command["sessionId"], hashlib.sha256(("a" * 64).encode()).hexdigest())
        self.assertNotIn("handoffApproved", command)
        self.assertEqual(self.request(body, {**self.headers, CLIENT_HEADER: "process-b"})[0], 403)
        response.close()
        conn.close()
        self.wait_for("endHandoffSession")
        self.assertEqual(self.request(body)[0], 403)

    def test_abrupt_process_exit_closes_channel(self):
        code = """import http.client, json, sys, time
c = http.client.HTTPConnection('127.0.0.1', int(sys.argv[1]))
c.request('GET', '/api/handoff-session', headers=json.loads(sys.argv[2]))
r = c.getresponse()
print(r.status, flush=True)
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.bridge.http_port), json.dumps(self.headers)],
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "200")
            process.kill()
            process.wait(timeout=5)
            self.wait_for("endHandoffSession")
            self.assertEqual(self.request({"tabId": 1, "reason": "work"})[0], 403)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

    def test_bridge_client_channel_close_and_extension_loss(self):
        async def run():
            client = bridge_client.BridgeClient(approver=True)
            with patch.object(bridge_client, "BRIDGE", "http://" + self.bridge.host), \
                    patch.object(bridge_client, "_headers", return_value=self.bridge.auth()), \
                    patch.object(client, "await_extension"):
                await client.open_handoff_session("Client A")
                secret = client._handoff_secret
                self.assertIsNotNone(secret)
                await client.close_handoff_session()
                self.assertIsNone(client._handoff_secret)
                await asyncio.to_thread(self.wait_for, "endHandoffSession")
                await client.open_handoff_session("Client A")
                self.assertNotEqual(client._handoff_secret, secret)
                await asyncio.to_thread(self.extension.close)
                await asyncio.wait_for(client._handoff_task, 5)
                self.assertIsNone(client._handoff_secret)
                await client.close_handoff_session()
        asyncio.run(run())

    def test_mcp_eof_during_browser_wait_revokes_session_without_waiting_for_http(self):
        self.extension.close()
        self.extension = PendingHandoffExtension(self.bridge)
        self.addCleanup(self.extension.close)

        async def run():
            client = bridge_client.BridgeClient(approver=True)
            call_finished = asyncio.Event()
            async def call(mcp_client):
                try:
                    await mcp_client.call_tool("browser_tabs", {"action": "request", "tab_id": 1, "reason": "work"})
                except Exception:
                    pass  # EOF cancels the outstanding client request.
                finally:
                    call_finished.set()
            with patch.object(bridge_client, "BRIDGE", "http://" + self.bridge.host), \
                    patch.object(bridge_client, "_headers", return_value=self.bridge.auth()), \
                    patch.object(client, "await_extension"), patch.object(server, "_client", client), \
                    patch.object(tools, "client", client):
                async with create_client_server_memory_streams() as (client_streams, server_streams):
                    async with anyio.create_task_group() as group:
                        group.start_soon(server.mcp._mcp_server.run, *server_streams,
                                         server.mcp._mcp_server.create_initialization_options())
                        async with ClientSession(*client_streams) as mcp_client:
                            await mcp_client.initialize()
                            group.start_soon(call, mcp_client)
                            with anyio.fail_after(5):
                                while not self.extension.requested.is_set():
                                    await anyio.sleep(.01)
                                self.assertIsNotNone(self.extension.pending, "MCP event loop was blocked by browser wait")
                                # Actual MCP transport EOF, not just cancellation of call_tool.
                                await client_streams[1].aclose()
                                while self.extension.count("endHandoffSession") == 0:
                                    await anyio.sleep(.01)
                                await call_finished.wait()
                            self.assertIsNone(client._handoff_secret)
                        group.cancel_scope.cancel()
                self.assertIsNone(self.extension.pending)
                command = next(c for c in self.extension.commands if c["action"] == "requestTab")
                self.assertEqual(command["ownerId"], client.owner_id, "worker retains transport ContextVars")
        anyio.run(run)

    def test_a_pending_request_holds_no_other_command(self):
        # #494: a request waiting for the user holds no scheduler lease, so a global command (and the tab
        # commands queued behind it) does not wait for the answer.
        self.extension.close()
        self.extension = PendingHandoffExtension(self.bridge)
        self.addCleanup(self.extension.close)
        self.channel()
        threading.Thread(target=self.request, args=({"tabId": 1, "reason": "work"},), daemon=True).start()
        self.assertTrue(self.extension.requested.wait(3))
        started = time.monotonic()
        status, _, _ = _request(self.bridge.http_port, "POST", "/api/new-tab", headers=self.headers,
                                host=self.bridge.host, body=json.dumps({"url": "about:blank"}))
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 1, "the new tab waited for the user's answer")

    def test_connected_mcp_browser_answers_and_client_fallbacks(self):
        # An extension that predates #494 ignores together: it asks in the browser first, then returns the
        # token, which the client's prompt approves.
        async def run():
            for decision, elicits in (("browser-approved", True), ("browser-declined", True),
                                      ("approved", True), ("declined", True), ("token", False)):
                client = bridge_client.BridgeClient(approver=True)
                prompts = []
                held = {"error": "approval", "code": "needs_user", "state": {"reason": "approval",
                        "detail": "tab_handoff", "tabId": 1, "confirmToken": "test-token", "expiresInSeconds": 120}}
                def result(command):
                    if decision == "browser-approved" or command.get("confirmToken") == "test-token":
                        return {"ok": True, "tabId": 1, "owned": True}
                    if decision == "browser-declined":
                        return {"error": "declined", "code": "needs_user", "state": {"decision": "declined"}}
                    return held
                self.extension.request_result = result
                async def answer(context, params):
                    prompts.append(params)
                    return types.ElicitResult(action="decline" if decision == "declined" else "accept",
                                             content={"decision": "Approve"})
                with patch.object(bridge_client, "BRIDGE", "http://" + self.bridge.host), \
                        patch.object(bridge_client, "_headers", return_value=self.bridge.auth()), \
                        patch.object(client, "await_extension"), patch.object(server, "_client", client), \
                        patch.object(tools, "client", client):
                    async with create_connected_server_and_client_session(server.mcp,
                            elicitation_callback=answer if elicits else None) as mcp_client:
                        args = {"action": "request", "tab_id": 1, "reason": "work"}
                        response = await mcp_client.call_tool("browser_tabs", args)
                        value = json.loads(response.content[0].text)
                        self.assertNotIn("resume_token", value)
                        self.assertEqual(len(prompts), 1 if decision in {"approved", "declined"} else 0)
                        request = next(c for c in self.extension.commands if c["action"] == "requestTab")
                        self.assertEqual(request.get("together"), True if elicits else None)
                        self.extension.commands.clear()
                        if decision == "token":
                            listed = await mcp_client.list_tools()
                            schema = next(t.inputSchema for t in listed.tools if t.name == "browser_tabs")
                            self.assertIn("confirm_token", schema["properties"])
                            token = value["state"]["confirmToken"]
                            response = await mcp_client.call_tool("browser_tabs", {**args, "confirm_token": token})
                            self.assertTrue(json.loads(response.content[0].text)["owned"])
                        elif "declined" in decision:
                            self.assertEqual(value["state"]["decision"], "declined")
                            self.assertNotIn("confirmToken", value["state"])
                        else:
                            self.assertTrue(value["owned"])
        anyio.run(run)

    def test_with_elicitation_the_browser_and_the_client_ask_at_once(self):
        self.extension.close()
        self.extension = TogetherExtension(self.bridge)
        self.addCleanup(self.extension.close)

        async def request(popup, answer, delay=0.0, popup_delay=0.3, cancel=False, decision="Approve"):
            """browser_tabs request: the popup answers popup after popup_delay s (None: never), the client's
            prompt answers after delay s (None: accept after 1 s, too late); cancel: the client cancels the
            call first. The result (None when cancelled), the client's answers, and whether the server
            closed the client's prompt."""
            self.extension.reset(popup, popup_delay)
            client = bridge_client.BridgeClient(approver=True)
            closed = []
            send_notification = ServerSession.send_notification

            async def recording(session, notification, related_request_id=None):
                closed.append(isinstance(notification.root, types.CancelledNotification))
                await send_notification(session, notification, related_request_id)

            async def elicit(context, params):
                self.assertEqual(params.requestedSchema, server.APPROVAL_SCHEMA)
                self.assertIn("Client A requests this tab: work", params.message)
                self.assertIn("Tab: Page - https://user.example/", params.message)
                if cancel:
                    await mcp.send_notification(types.ClientNotification(types.CancelledNotification(
                        params=types.CancelledNotificationParams(requestId=call_id))))
                # The SDK's client reads nothing else meanwhile, so a late answer stands for an open prompt.
                await anyio.sleep(1 if answer is None else delay)
                return types.ElicitResult(action=answer or "accept",
                                         content={"decision": decision} if decision is not None else None)

            with patch.object(bridge_client, "BRIDGE", "http://" + self.bridge.host), \
                    patch.object(bridge_client, "_headers", return_value=self.bridge.auth()), \
                    patch.object(client, "await_extension"), patch.object(server, "_client", client), \
                    patch.object(tools, "client", client), patch.object(ServerSession, "send_notification", recording):
                async with create_connected_server_and_client_session(server.mcp, elicitation_callback=elicit) as mcp:
                    with anyio.fail_after(10):
                        call_id = mcp._request_id
                        try:
                            response = await mcp.call_tool("browser_tabs", {"action": "request", "tab_id": 1,
                                                                           "reason": "work"})
                        except McpError:
                            response = None
                        # The client's answer can reach the extension before the popup's wait does; a
                        # cancelled call's answer comes after the client is told, and a call cancelled
                        # before the popup's wait was sent sends none: its answer ends the request alone.
                        while (not cancel and not any(c.get("wait") for c in self.extension.requests())) or (
                                cancel and not any("decision" in c for c in self.extension.requests())):
                            await anyio.sleep(0.01)
                    await anyio.sleep(0.1)  # a closed prompt's late reply reaches the server before it closes
            value = None
            if response is not None:
                self.assertNotIn("test-token", response.content[0].text, "the model never sees the token")
                value = json.loads(response.content[0].text)
                self.assertIs(response.isError, not value.get("ok"))
            first, *rest = self.extension.requests()
            waits = [c for c in rest if c.get("wait")]
            answers = [c for c in rest if not c.get("wait")]
            self.assertEqual((first.get("together"), first.get("confirmToken")), (True, None))
            self.assertEqual(len(waits), 1 if not cancel else len(waits[:1]))
            for wait in waits:
                self.assertEqual((wait["confirmToken"], wait["wait"], wait["tabId"], wait["reason"]),
                                 ("test-token", True, 1, "work"))
            return value, [a.get("decision", "approve") for a in answers], any(closed)

        async def run():
            # The popup answers first: the client's prompt closes, and nothing answers for the client.
            self.assertEqual(await request(OWNED, None), (OWNED, [], True))
            declined, answers, closed = await request(not_handed_off("declined"), None)
            self.assertEqual((declined["state"]["decision"], answers, closed), ("declined", [], True))
            # The client's explicit Approve hands the tab off, the popup's wait returns.
            self.assertEqual(await request(None, "accept"), (OWNED, ["approve"], False))
            # Empty/invalid automatic accepts do not grant access; the popup can still decline.
            for decision in (None, "", "invalid"):
                value, answers, _ = await request(not_handed_off("declined"), "accept", decision=decision)
                self.assertEqual((value["state"]["decision"], answers), ("declined", []))
            with patch.object(server, "CLIENT_POLICY_SECONDS", 0.1):
                for answer, decision in (("decline", "declined"), ("cancel", "cancelled")):
                    value, answers, _ = await request(None, answer, delay=0.2)
                    self.assertEqual((value["state"]["decision"], answers), (decision, [decision]))
            # A decline at once is the client's policy, not the user's: the popup keeps asking.
            value, answers, _ = await request(OWNED, "decline", popup_delay=0.5)
            self.assertEqual((value, answers), (OWNED, []))
            # A cancelled call stops the popup's prompt too.
            value, answers, _ = await request(None, "accept", delay=0.2, cancel=True)
            self.assertEqual((value, answers), (None, ["cancelled"]))
        anyio.run(run)

    def test_simultaneous_owner_sessions(self):
        first, response = self.channel()
        second_headers = {**self.headers, SESSION_HEADER: "b" * 64, CLIENT_HEADER: "process-b"}
        self.channel(second_headers)
        response.close()
        first.close()
        self.wait_for("endHandoffSession")
        self.assertEqual(self.request({"tabId": 1, "reason": "work"}, second_headers)[0], 200)


class HandoffFacadeTests(unittest.TestCase):
    def test_native_request_and_core_index_use_same_endpoint(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_tabs("request", tab_id=5, reason="work", confirm_token="token")
            post.assert_called_once_with("/api/request-tab", {"tabId": 5, "reason": "work", "confirmToken": "token"},
                                         timeout=45)
        with patch.object(playwright_tools, "_tab_at", return_value=(5, None)), \
                patch.object(tools, "browser_tabs", return_value={"ok": True}) as request:
            playwright_tools.browser_tabs("request", index=1, reason="work")
            request.assert_called_once_with("request", tab_id=5, reason="work", confirm_token=None)
        self.assertIn("error", playwright_tools.browser_tabs("request", index=1, tab_id=5, reason="work"))

    def test_browser_decision_does_not_elicit_and_fallback_uses_existing_approval(self):
        async def run():
            held = {"error": "approval", "code": "needs_user", "state": {"reason": "approval",
                    "detail": "tab_handoff", "tabId": 5, "confirmToken": "opaque", "expiresInSeconds": 120}}
            payload = lambda value: server.CallToolResult(content=[server._compact_json(value)])
            with patch.object(type(server.mcp._mcp_server), "request_context", new_callable=PropertyMock, return_value=None), \
                    patch.object(server, "_ask_user", new_callable=AsyncMock, return_value="approved") as ask, \
                    patch.object(server.mcp, "_run_tool", new_callable=AsyncMock, return_value=payload({"ok": True})) as repeat:
                for result in ({"ok": True}, {"code": "needs_user", "state": {"decision": "declined"}}):
                    await server.mcp._approved(SimpleNamespace(), "browser_tabs", {"action": "request"}, payload(result))
                ask.assert_not_called()
                await server.mcp._approved(SimpleNamespace(), "browser_tabs", {"action": "request"}, payload(held))
                ask.assert_awaited_once()
                self.assertEqual(repeat.call_args.args[1]["confirm_token"], "opaque")
        asyncio.run(run())
