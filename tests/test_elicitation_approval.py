"""#376: a held direct call is approved through MCP elicitation, so the model never sees a confirm token.

Connected MCP sessions on in-memory streams drive the MCP server; its bridge requests reach an
in-process bridge handler whose extension is faked, so the bridge's needs_user results, tokens,
headers and audit log are the real ones.
"""
import asyncio
import hashlib
import json
import logging
import re
import threading
import unittest
from unittest.mock import Mock, patch

import anyio
from mcp import types
from mcp.server.session import ServerSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from kitsune import bridge, bridge_client, playwright_tools, server, tools
from kitsune.task_modes import APPROVAL_HEADER, APPROVER_HEADER

USER_TAB, AGENT_TAB = 4, 9
TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_-]{32}")
# Codex's clientInfo: its prompts keep a field to choose in (#494); other clients' are one step.
CODEX = types.Implementation(name="codex-mcp-client", version="0.163.0")


class FakeExtension:
    """AGENT_TAB is the agent's. describeTarget reports a submit control, which ask-before-act holds."""

    def __init__(self):
        self.sent = []
        self.described_tab = None  # set: the tab describeTarget reports, as if the active tab changed
        self.url = "https://shop.example/cart"  # a payment-like URL holds every write under ask-before-act
        self.tabs = None  # set: what getTabs lists

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        if action == "checkTabWrite":
            return {"ok": True}  # every tab is writable here; UserTabReadOnlyElicitationTests refuses
        self.sent.append((action, params))
        tab_id = params.get("tabId")
        if action == "getTabs" and self.tabs is not None:
            return {"tabs": self.tabs}
        if action == "describeTarget":
            described = {"tabId": self.described_tab or tab_id, "url": self.url}
            if params.get("inspect"):
                described["target"] = {"ok": True, "tag": "button", "submits": True, "text": "Pay"}
            return described
        return {"ok": True}

    def actions(self, action):
        return [params for sent, params in self.sent if sent == action]


class ElicitationTestCase(unittest.TestCase):
    ask = False

    @classmethod
    def setUpClass(cls):
        # The bridge's own event loop, on its own thread as in the daemon (BridgeHandler.run_async).
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(5)
        cls.loop.close()

    def setUp(self):
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        self.extension = FakeExtension()
        self.requests = []
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": self.ask,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
            patch.dict(bridge._unknown_outcomes, clear=True),
            patch.object(bridge, "_audit_log", []),
            patch.object(bridge, "_global_command_lock", None),
            patch.object(bridge, "_tab_command_locks", {}),
            patch.object(bridge, "_scheduler_gate", None),
            patch.object(bridge, "_send_to_extension_raw", new=lambda *a, **k: self.extension(*a, **k)),
            patch.object(bridge_client, "_headers", return_value={}),
            patch.object(bridge_client.httpx, "request", side_effect=self.bridge_http),
            patch.object(playwright_tools, "_current_tab", AGENT_TAB),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def bridge_http(self, method, url, headers=None, json=None, **kwargs):
        """One bridge request, answered by an in-process BridgeHandler."""
        path = url[url.index("/api"):]
        self.requests.append({"path": path, "headers": dict(headers or {}), "body": dict(json or {})})
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path, handler.command, handler.headers = path, method, dict(headers or {})
        handler.read_body = Mock(return_value=dict(json or {}))
        handler.send_json = Mock()
        handler.drain_body = Mock()
        handler.authorized = Mock(return_value=True)
        handler.run_async = lambda coro, timeout=35: asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        handler.do_POST() if method == "POST" else handler.do_GET()
        status, result = handler.send_json.call_args.args
        return Mock(status_code=status, json=Mock(return_value=result))

    def call(self, name, arguments, answer=None, progress=None, protocol=types.LATEST_PROTOCOL_VERSION, client=None):
        """Call a tool from a client that elicits with answer(params) -> ElicitResult, or without elicitation,
        on the protocol version the client asks for; client is its clientInfo (the SDK's by default)."""
        self.prompts = []

        async def elicit(context, params):
            self.prompts.append(params)
            return await answer(params)

        async def run():
            async with create_connected_server_and_client_session(
                    server.mcp, elicitation_callback=elicit if answer else None, client_info=client) as session:
                result = await session.call_tool(name, arguments, progress_callback=progress)
                await anyio.sleep(0.1)  # a cancelled prompt's late reply reaches the server before it closes
                return result

        with patch.object(types, "LATEST_PROTOCOL_VERSION", protocol):
            result = anyio.run(run)
        self.text = result.content[0].text
        return result, json.loads(self.text)

    def clicks(self):
        return [request for request in self.requests if request["path"] == "/api/click"]

    def assertNoToken(self):
        """No confirm token, issued or token-shaped, anywhere in the model-visible result. A
        resume_token (docs/checkpoint-resume.md) is the one token-shaped value it may carry."""
        for token in bridge._confirm_tokens:
            self.assertNotIn(token, self.text)

        def walk(value, key=None):
            if isinstance(value, dict):
                self.assertNotIn("confirmToken", value)
                for name, item in value.items():
                    walk(item, name)
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, str) and key != "resume_token":
                self.assertEqual(TOKEN_SHAPED.findall(value), [], (key, value))

        walk(json.loads(self.text))


class ElicitationCapabilityTests(unittest.TestCase):
    def test_form_elicitation_is_declared_by_form_or_an_empty_capability(self):
        for capability, elicits in (({}, True), ({"form": {}}, True), ({"form": {}, "url": {}}, True),
                                    ({"url": {}}, False), (None, False)):
            with self.subTest(capability=capability):
                params = types.InitializeRequestParams(
                    protocolVersion=types.LATEST_PROTOCOL_VERSION, clientInfo=types.Implementation(name="c", version="1"),
                    capabilities=types.ClientCapabilities(
                        elicitation=None if capability is None else types.ElicitationCapability(**capability)))
                session = Mock(client_params=params)
                self.assertIs(server._elicits(session), elicits)
                self.assertIs(server._elicits(Mock(client_params=None)), False)


def answer_with(action, decision=None, delay=0):
    async def answer(params):
        await anyio.sleep(delay)
        return types.ElicitResult(action=action, content={"decision": decision} if decision else None)
    return answer


class ApprovalElicitationTests(ElicitationTestCase):
    ask = True

    def test_approve_repeats_the_identical_call_once_with_the_token(self):
        elicits, negotiated = server._elicits, []

        def recording(session):
            negotiated.append(session.client_params.protocolVersion)
            return elicits(session)

        for protocol in ("2025-06-18", types.LATEST_PROTOCOL_VERSION):
            with self.subTest(protocol=protocol), patch.object(server, "_elicits", recording):
                bridge._audit_log.clear()
                bridge._confirm_tokens.clear()
                self.requests.clear()
                self.extension.sent.clear()
                self.approve(protocol)
                self.assertEqual(negotiated[-1], protocol)

    def test_all_clients_choose_approve_in_a_field(self):
        for name in ("codex-mcp-client", "claude-code", "opencode", "unknown-client"):
            with self.subTest(client=name):
                self.requests.clear()
                self.extension.sent.clear()
                bridge._audit_log.clear()
                self.approve(types.LATEST_PROTOCOL_VERSION, types.Implementation(name=name, version="test"))

    def test_bare_accept_and_invalid_decisions_never_approve(self):
        for name in ("codex-mcp-client", "claude-code", "opencode", "unknown-client"):
            for decision in (None, "", "invalid"):
                with self.subTest(client=name, decision=decision):
                    self.requests.clear()
                    self.extension.sent.clear()
                    result, payload = self.call("browser_click", {"target": "#send"},
                                                answer_with("accept", decision),
                                                client=types.Implementation(name=name, version="test"))
                    self.assertTrue(result.isError)
                    self.assertEqual(payload["state"]["decision"], "cancelled")
                    self.assertEqual(self.extension.actions("click"), [])

    def approve(self, protocol, client=None):
        result, payload = self.call("browser_click", {"target": "#send", "element": "Send button"},
                                    answer_with("accept", "Approve"), protocol=protocol,
                                    client=client)
        self.assertFalse(result.isError, payload)
        self.assertIs(payload.get("ok"), True, payload)
        held, repeat = self.clicks()
        self.assertEqual(len(self.prompts), 1)
        # The identical call, with the token the model never saw, and the elicitation claim.
        self.assertEqual({k: v for k, v in repeat["body"].items() if k != "confirmToken"}, held["body"])
        self.assertIn(repeat["body"]["confirmToken"], bridge._confirm_tokens)
        self.assertNotIn(APPROVAL_HEADER, held["headers"])
        self.assertEqual(repeat["headers"][APPROVAL_HEADER], "elicitation")
        self.assertEqual(held["headers"][APPROVER_HEADER], repeat["headers"][APPROVER_HEADER])
        self.assertEqual([p["selector"] for p in self.extension.actions("click")], ["#send"])
        self.assertNoToken()
        # The prompt: action, target, tab URL, and why it was held; form mode. The model's element
        # description is labeled as the agent's.
        prompt = self.prompts[0]
        self.assertEqual(prompt.mode, "form")
        # Every client must submit an explicit approval decision.
        self.assertEqual(prompt.requestedSchema, server.APPROVAL_SCHEMA)
        approve = "Approve"
        for part in ("Allow the agent's click? It clicks a submit control (Pay).", "Tool: browser_click",
                     "Target: #send\nAgent's description of the target: Send button\n",
                     "Tab: https://shop.example/cart", "Held because ask-before-act: it clicks a submit control (Pay).",
                     f"{approve} runs this action once; Decline leaves it undone."):
            self.assertIn(part, prompt.message)
        self.assertNotIn("Arguments:", prompt.message)
        # The audit log records how the write was approved.
        approved = [(e["action"], e["params"].get("confirmation"), e.get("approval")) for e in bridge._audit_log]
        self.assertEqual(approved[-2:], [("click", "accepted", "elicitation"), ("click", None, "elicitation")])

    def test_the_prompt_shows_the_other_arguments_truncated(self):
        self.call("browser_type", {"target": "e7", "element": "Search box", "text": "x" * 1000, "submit": True},
                  answer_with("decline"))
        message = self.prompts[0].message
        self.assertIn("Target: e7\nAgent's description of the target: Search box\nArguments: {\"text\": \"xxx",
                      message)
        arguments = next(line for line in message.splitlines() if line.startswith("Arguments: "))
        self.assertEqual(len(arguments), len("Arguments: ") + server.APPROVAL_ARGUMENTS_CHARS)
        self.assertTrue(arguments.endswith("x…"))
        self.extension.url = "https://shop.example/checkout"  # a payment-like page holds each field
        self.call("browser_fill_form", {"fields": [{"name": "Card", "type": "textbox", "target": "#card",
                                                    "value": "4242"}]}, answer_with("decline"))
        self.assertIn('Arguments: {"fields": [{"name": "Card", "type": "textbox", "target": "#card", "value": "4242"}]}',
                      self.prompts[0].message)

    def test_a_cancelled_call_closes_its_prompt(self):
        sent, prompts = [], []
        send_notification = ServerSession.send_notification

        async def recording(session, notification, related_request_id=None):
            sent.append(notification.root)
            await send_notification(session, notification, related_request_id)

        async def run():
            async def elicit(context, params):
                prompts.append(context.request_id)
                # The client cancels the tool call while its prompt is open; the user approves late.
                await client.send_notification(types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=call_id))))
                await anyio.sleep(0.2)
                return types.ElicitResult(action="accept", content={"decision": "Approve"})

            async with create_connected_server_and_client_session(server.mcp, elicitation_callback=elicit) as client:
                call_id = client._request_id
                with self.assertRaises(McpError):
                    await client.call_tool("browser_click", {"target": "#send"})
                with anyio.fail_after(5):
                    while not any(isinstance(n, types.CancelledNotification) for n in sent):
                        await anyio.sleep(0.01)
                await anyio.sleep(0.1)  # the late reply reaches the server before it closes

        with patch.object(ServerSession, "send_notification", recording):
            anyio.run(run)
        cancelled = [n.params for n in sent if isinstance(n, types.CancelledNotification)]
        self.assertEqual([(c.requestId, c.reason) for c in cancelled], [(prompts[0], "The tool call was cancelled")])
        self.assertEqual(len(self.clicks()), 1, "nothing was repeated")

    def test_decline_and_cancel_return_needs_user_without_a_token(self):
        for answer, decision, client in ((answer_with("decline"), "declined", None),
                                         (answer_with("cancel"), "cancelled", None),
                                         (answer_with("accept", "Decline"), "declined", CODEX),
                                         (answer_with("accept"), "cancelled", CODEX)):
            with self.subTest(decision=decision, client=client):
                self.requests.clear()
                result, payload = self.call("browser_click", {"target": "#send"}, answer, client=client)
                self.assertTrue(result.isError)
                self.assertEqual((payload["code"], payload["retryable"]), ("needs_user", False))
                state = payload["state"]
                self.assertEqual((state["reason"], state["detail"], state["decision"], state["approval"]),
                                 ("approval", "confirmation_required", decision, "elicitation"))
                self.assertNotIn("expiresInSeconds", state)
                self.assertEqual(payload["hint"], f'{server.DECISION_HINTS[decision]} {server.RESUME_HINTS["elicitation"]}')
                self.assertEqual(len(self.clicks()), 1, "nothing was repeated")
                self.assertNoToken()
        self.assertEqual(self.extension.actions("click"), [], "nothing ran")

    def test_a_client_without_elicitation_keeps_the_token(self):
        result, payload = self.call("browser_click", {"target": "#send"})
        self.assertTrue(result.isError)
        state = payload["state"]
        self.assertEqual((payload["code"], state["detail"], state["approval"]), ("needs_user", "confirmation_required", "token"))
        self.assertIn(state["confirmToken"], bridge._confirm_tokens)
        self.assertEqual(len(self.clicks()), 1)
        # The model repeats the call with the token itself: the audit log tells that path apart.
        result, payload = self.call("browser_click", {"target": "#send", "confirm_token": state["confirmToken"]})
        self.assertIs(payload.get("ok"), True, payload)
        self.assertNotIn(APPROVAL_HEADER, self.clicks()[-1]["headers"])
        self.assertEqual([e.get("approval") for e in bridge._audit_log if e["action"] == "click"][-2:],
                         ["token", "token"])

    def test_an_unanswered_prompt_ends_before_the_token_expires(self):
        margin = bridge.CONFIRM_TOKEN_TTL - 0.5  # the prompt stays open for 1 s at least
        with patch.object(server, "ELICITATION_MARGIN_SECONDS", margin):
            result, payload = self.call("browser_click", {"target": "#send"}, answer_with("accept", "Approve", delay=3))
        self.assertEqual(payload["state"]["decision"], "cancelled")
        self.assertEqual(len(self.clicks()), 1)
        self.assertNoToken()

    def test_progress_keeps_the_request_alive_while_the_prompt_is_open(self):
        progress = []

        async def on_progress(value, total, message):
            progress.append((value, message))

        with patch.object(server, "ELICITATION_PROGRESS_SECONDS", 0.05):
            result, payload = self.call("browser_click", {"target": "#send"},
                                        answer_with("accept", "Approve", delay=0.4), on_progress)
        self.assertIs(payload.get("ok"), True, payload)
        self.assertGreaterEqual(len(progress), 2, progress)
        self.assertEqual([value for value, _ in progress], list(range(1, len(progress) + 1)))
        self.assertEqual(progress[0][1], "Waiting for the user's approval")

    def test_the_approver_secret_never_reaches_the_model_or_the_audit_log(self):
        secret = server._client._approver
        self.assertTrue(secret)
        hashed = hashlib.sha256(secret.encode()).hexdigest()
        outputs = []
        for answer in (answer_with("accept", "Approve"), answer_with("decline"), None):
            self.call("browser_click", {"target": "#send"}, answer)
            outputs.append(self.text)
        self.call("browser_status", {})
        outputs.append(self.text)
        self.assertTrue(all(request["headers"].get(APPROVER_HEADER) == secret for request in self.requests))
        audit = self.bridge_http("GET", "http://127.0.0.1/api/audit", {}).json()
        outputs += [json.dumps(audit), json.dumps(bridge._audit_log), "\n".join(bridge._log_buffer)]
        for output in outputs:
            self.assertNotIn(secret, output)
            self.assertNotIn(hashed, output)


class UserTabReadOnlyElicitationTests(ElicitationTestCase):
    def test_read_only_refusal_never_elicits_or_repeats_even_with_ask_enabled(self):
        refusal = {"error": "read only", "code": "user_tab_read_only", "retryable": False,
                   "state": {"tabId": USER_TAB, "index": 0, "action": "click"},
                   "hint": "Open a tab of your own with browser_tabs action=new."}
        sent = []

        async def read_only(action, params=None, timeout=30):
            sent.append(action)
            return refusal

        self.extension = read_only
        for asking in (False, True):
            with self.subTest(ask=asking), patch.dict(bridge._policy, {"ask": asking}):
                sent.clear()
                result, payload = self.call("browser_click", {"target": "#go"}, answer_with("accept", "Approve"))
                self.assertTrue(result.isError)
                self.assertEqual(payload["code"], "user_tab_read_only")
                self.assertEqual(self.prompts, [])
                self.assertEqual(sent, ["checkTabWrite"] if asking else ["click"])
                self.assertNoToken()


class AskBeforeActElicitationTests(ElicitationTestCase):
    ask = True

    def test_a_repeat_that_no_longer_matches_is_not_repeated_again(self):
        async def approve_then_switch_tab(params):
            self.extension.described_tab = AGENT_TAB + 1  # the target now resolves in another tab
            return types.ElicitResult(action="accept", content={"decision": "Approve"})

        result, payload = self.call("browser_click", {"target": "#pay"}, approve_then_switch_tab)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.clicks()), 2, "one repeat")
        state = payload["state"]
        self.assertEqual((payload["code"], state["detail"], state["holdReason"], state["tokenRejected"]),
                         ("needs_user", "confirmation_required", "submit_control", "mismatch"))
        self.assertEqual(payload["hint"], f'{server.UNMATCHED_APPROVAL_HINT} {server.RESUME_HINTS["elicitation"]}')
        self.assertNotIn("decision", state)
        self.assertEqual(self.extension.actions("click"), [], "nothing ran")
        self.assertNoToken()
        self.assertIn("Held because ask-before-act: it clicks a submit control (Pay).", self.prompts[0].message)
        self.assertIn("Approve runs this action once; Decline leaves it undone.", self.prompts[0].message)

    def test_each_held_request_of_a_call_is_asked_about_once(self):
        # browser_type(submit) on a payment-like page: its text and then its Enter are held; the second
        # repeat presses Enter only.
        self.extension.url = "https://shop.example/checkout"
        result, payload = self.call("browser_type", {"target": "#q", "text": "hi", "submit": True},
                                    answer_with("accept", "Approve"))
        self.assertIs(payload.get("ok"), True, payload)
        self.assertEqual([p.message.splitlines()[0] for p in self.prompts],
                         ["Allow the agent's fill? It acts on a payment-like page (checkout).",
                          "Allow the agent's keypress? It acts on a payment-like page (checkout)."])
        self.assertEqual(len([r for r in self.requests if r["path"] == "/api/fill"]), 2)
        self.assertEqual(len([r for r in self.requests if r["path"] == "/api/keypress"]), 2)
        self.assertEqual(len(self.extension.actions("keypress")), 1, "Enter was pressed once")
        self.assertEqual(len(self.extension.actions("fill")), 1, "the text was entered once")
        self.assertNoToken()

    def test_typing_slowly_then_submitting_types_the_text_once(self):
        # slowly types without clearing: a repeat that typed again would leave "hihi" in the field.
        result, payload = self.call("browser_type", {"target": "#q", "text": "hi", "slowly": True, "submit": True},
                                    answer_with("accept", "Approve"))
        self.assertIs(payload.get("ok"), True, payload)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual([p["text"] for p in self.extension.actions("type")], ["hi"])
        self.assertEqual(len(self.extension.actions("keypress")), 1)
        self.assertEqual([e.get("approval") for e in bridge._audit_log if e["action"] == "keypress"][-1], "elicitation")

    def test_a_form_whose_every_field_is_held_is_filled_field_by_field(self):
        self.extension.url = "https://shop.example/checkout"
        fields = [{"name": name, "type": "textbox", "target": f"#{name}", "value": value}
                  for name, value in (("card", "4242"), ("expiry", "12/30"), ("cvc", "123"), ("zip", "10001"))]
        result, payload = self.call("browser_fill_form", {"fields": fields}, answer_with("accept", "Approve"))
        self.assertIs(payload.get("ok"), True, payload)
        self.assertEqual(payload["filled"], ["card", "expiry", "cvc", "zip"])
        self.assertEqual(len(self.prompts), 4, "one approval per held field")
        self.assertEqual([p["selector"] for p in self.extension.actions("fill")], ["#card", "#expiry", "#cvc", "#zip"])
        self.assertNoToken()
        # Declined at the second field: the first stays filled, and no token is reported as rejected.
        self.extension.sent.clear()
        answers = iter(("accept", "decline"))

        async def approve_then_decline(params):
            return types.ElicitResult(action=next(answers), content={"decision": "Approve"})

        result, payload = self.call("browser_fill_form", {"fields": fields}, approve_then_decline)
        self.assertEqual((payload["code"], payload["state"]["decision"], payload["filled"]),
                         ("needs_user", "declined", ["card"]))
        self.assertNotIn("tokenRejected", payload["state"])
        self.assertEqual([p["selector"] for p in self.extension.actions("fill")], ["#card"])
        self.assertNoToken()

    def test_a_container_held_step_loses_its_token(self):
        # Containers never confirm a step; the held step's token would approve its direct call.
        # It is a legacy direct tool, not on the default minimal surface (#525).
        patcher = patch.object(server, "SCRIPTING", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        commands = [{"action": "parallel", "sequences": [
            [{"action": "click", "selector": "#pay", "tabId": AGENT_TAB}],
            [{"action": "submitForm", "selector": "form", "tabId": AGENT_TAB + 1}]]},
            {"action": "click", "selector": "#pay", "tabId": AGENT_TAB}]
        result, payload = self.call("browser_batch", {"commands": commands}, answer_with("accept", "Approve"))
        self.assertEqual(self.prompts, [], "a container does not ask")
        self.assertTrue(bridge._confirm_tokens, "the bridge issued tokens for the held steps")
        self.assertNoToken()
        held = payload["results"][0][0][0]
        self.assertEqual((held["code"], held["state"]["detail"], held["state"]["approval"], held["hint"]),
                         ("needs_user", "confirmation_required", "elicitation", server.CONTAINER_APPROVAL_HINT))
        self.assertEqual(self.extension.actions("click"), [])
        # Without elicitation the tokens stay, as before.
        result, payload = self.call("browser_batch", {"commands": commands})
        self.assertIn("confirmToken", payload["results"][0][0][0]["state"])


if __name__ == "__main__":
    unittest.main()
