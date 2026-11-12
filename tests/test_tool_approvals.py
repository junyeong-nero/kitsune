"""#525: per-tool approvals. Every tool is available; the user marks in the extension's options which
ones wait for their answer, in the toolbar popup or the MCP client's prompt (the first answer counts).

The tables (kitsune.tool_approvals) are checked against the bridge and the extension's catalog; the
bridge's refusals and container steps run in-process; MCP calls go through connected sessions to an
in-process bridge whose extension is faked (test_elicitation_approval's harness).
"""
import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp import types

from kitsune import bridge, server, tool_approvals
from kitsune.task_modes import APPROVER_HEADER, CALL_HEADER, CLIENT_HEADER, TOOL_HEADER
from test_ask_before_act import AskBeforeActTestCase
from test_bridge_container_actions import bridge_run_actions
from test_elicitation_approval import CODEX, ElicitationTestCase, FakeExtension as ElicitationExtension

ROOT = Path(__file__).resolve().parent.parent
CATALOG = (ROOT / "extension" / "background" / "tool-approvals.js").read_text()
KNOWN_TOOLS = set(server.TOOL_GROUPS) | {"browser_run_python"}


def bridge_routes() -> set[str]:
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.authorized = Mock(return_value=True)
    served = set()
    handler._dispatch = lambda routes: served.update(routes)
    handler.do_GET()
    handler.do_POST()
    return served


class TableTests(unittest.TestCase):
    def test_the_extension_catalog_lists_every_tool_by_group(self):
        groups = {group: re.findall(r"'(browser_[a-z0-9_]+)'", names)
                  for group, names in re.findall(r"^  (\w+): \[(.*)\],$", CATALOG, re.M)}
        expected = {}
        for name, group in server.TOOL_GROUPS.items():
            expected.setdefault(group, set()).add(name)
        expected["core"].add("browser_run_python")
        self.assertEqual({group: set(names) for group, names in groups.items()}, expected)
        self.assertIn("const DEFAULT_APPROVAL_TOOLS = [...KITSUNE_TOOL_GROUPS.storage, ...KITSUNE_TOOL_GROUPS.history];",
                      CATALOG)
        self.assertEqual(tool_approvals.DEFAULT_TOOLS, set(groups["storage"]) | set(groups["history"]))

    def test_every_route_and_container_action_names_a_tool(self):
        served = bridge_routes()
        self.assertEqual(served - set(tool_approvals.ROUTE_TOOLS) - tool_approvals.UNCHECKED_ROUTES, set())
        self.assertEqual(set(tool_approvals.ROUTE_TOOLS) - served, set(), "a mapped route the bridge does not serve")
        self.assertTrue(set(tool_approvals.ROUTE_TOOLS.values()) <= KNOWN_TOOLS)
        self.assertEqual(set(tool_approvals.ACTION_TOOLS), set(bridge.CONTAINER_ACTIONS))
        self.assertTrue(set(tool_approvals.ACTION_TOOLS.values()) - {None} <= KNOWN_TOOLS)
        self.assertTrue(set(bridge._CONTROL_FLOW_HANDLERS) <= {action for action, tool in
                                                               tool_approvals.ACTION_TOOLS.items() if tool is None}
                        | bridge_run_actions())

    def test_request_tool(self):
        self.assertEqual(tool_approvals.request_tool("/api/cookies", "browser_cookies"), "browser_cookies")
        self.assertEqual(tool_approvals.request_tool("/api/tabs", "browser_cookies"), "browser_cookies",
                         "a direct call's every request is its tool's")
        self.assertEqual(tool_approvals.request_tool("/api/cookies", "browser_run_python"), "browser_cookies")
        self.assertIsNone(tool_approvals.request_tool("/api/cookies", None), "outside a tool call")
        self.assertIsNone(tool_approvals.request_tool("/api/tool-approval", "browser_cookies"))


class FirstAnswerTests(unittest.TestCase):
    """Which answer counts when the popup's wait and the client's answer both come back (#494, #525)."""

    def refusal(self, decision):
        return {"error": "x", "code": "needs_user", "state": {"decision": decision}}

    def test_the_clients_answer_counts_over_a_wait_it_settled(self):
        allowed = {"ok": True, "scope": "once"}
        self.assertEqual(server._first_answer(allowed, [self.refusal("cancelled")]), allowed)
        self.assertEqual(server._first_answer(self.refusal("declined"), [self.refusal("cancelled")]),
                         self.refusal("declined"))

    def test_a_popup_decline_given_first_stands(self):
        self.assertEqual(server._first_answer(self.refusal("cancelled"), [self.refusal("declined")]),
                         self.refusal("declined"))

    def test_a_request_the_popup_settled_waits_for_the_popups_answer(self):
        self.assertIsNone(server._first_answer(self.refusal("cancelled"), []))
        self.assertEqual(server._first_answer(self.refusal("declined"), []), self.refusal("declined"))


class BridgeApprovalTests(AskBeforeActTestCase):
    ask = False

    def setUp(self):
        super().setUp()
        for patcher in (patch.object(bridge, "_approval_tools", set(tool_approvals.DEFAULT_TOOLS)),
                        patch.object(bridge, "_call_approvals", {}), patch.object(bridge, "_session_approvals", set())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.approval = {"ok": True, "tool": "browser_cookies", "scope": "once"}
        extension = self.extension

        async def reply(action, params=None, timeout=30):
            if action == "toolApproval":
                extension.sent.append((action, dict(params or {})))
                return self.approval
            return await type(extension).__call__(extension, action, params, timeout)

        patcher = patch.object(bridge, "_send_to_extension_raw", new=reply)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, body, tool="browser_cookies", call="call-1", client="client-1", approver=True):
        handler = self.handler(body)
        handler.authorized = Mock(return_value=True)
        handler.path, handler.command = path, "POST"
        handler.headers = {**({APPROVER_HEADER: "secret"} if approver else {}), **({TOOL_HEADER: tool} if tool else {}),
                           CALL_HEADER: call, CLIENT_HEADER: client}
        handler.do_POST()
        return handler.send_json.call_args.args

    def test_a_marked_tool_is_refused_before_anything_is_sent(self):
        status, result = self.request("/api/cookies", {"op": "get"})
        self.assertEqual((status, result["code"], result["retryable"]), (403, "needs_user", False))
        self.assertEqual(result["state"], {"reason": "approval", "detail": "tool_approval", "tool": "browser_cookies"})
        self.assertEqual(self.extension.sent, [])
        # Any request of that call, a tab lookup included, is the call's tool's.
        self.assertEqual(self.request("/api/tabs", {}, tool="browser_cookies")[0], 403)

    def test_unmarked_tools_outside_calls_and_other_clients_pass(self):
        self.assertEqual(self.request("/api/snapshot", {}, tool="browser_snapshot")[0], 200)
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, tool=None)[0], 200, "outside a tool call")
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, approver=False)[0], 200, "the Python API")
        bridge._set_approval_tools([])
        self.assertEqual(self.request("/api/cookies", {"op": "get"})[0], 200)
        bridge._set_approval_tools(["not a tool"])
        self.assertEqual(bridge._approval_tools, set(), "an invalid list changes nothing")

    def test_scripts_are_checked_by_route(self):
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, tool="browser_run_python")[1]["state"]["tool"],
                         "browser_cookies")
        self.assertEqual(self.request("/api/snapshot", {}, tool="browser_run_python")[0], 200)

    def test_an_allow_lasts_for_its_call_or_its_session(self):
        status, result = self.request("/api/tool-approval", {"tool": "browser_cookies", "summary": "{}"})
        self.assertEqual((status, result), (200, self.approval))
        asked = self.extension.sent[-1][1]
        self.assertEqual((asked["tool"], asked["clientId"]), ("browser_cookies", "client-1"))
        self.assertEqual(self.request("/api/cookies", {"op": "get"})[0], 200)
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, call="call-2")[0], 403, "once is one call")
        self.approval = {**self.approval, "scope": "session"}
        self.request("/api/tool-approval", {"tool": "browser_cookies"}, call="call-2")
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, call="call-3")[0], 200)
        self.assertEqual(self.request("/api/cookies", {"op": "get"}, call="call-4", client="client-2")[0], 403)

    def test_tool_approval_answers_at_once_when_nothing_is_needed(self):
        status, result = self.request("/api/tool-approval", {"tool": "browser_snapshot"})
        self.assertEqual((status, result), (200, {"ok": True, "required": False}))
        self.assertEqual(self.extension.sent, [])
        self.assertEqual(self.request("/api/tool-approval", {"tool": "x y"})[0], 400)

    def test_container_steps_wait_for_the_popup(self):
        self.addCleanup(bridge._drop_all_checkpoints)
        batch = {"commands": [{"action": "navigate", "url": "https://a.test/", "tabId": 7},
                              {"action": "cookies", "op": "get", "url": "https://a.test/"}]}
        status, result = self.request("/api/batch", batch, tool="browser_batch")
        self.assertEqual((status, result["results"]), (200, [{"ok": True}, {"ok": True}]))
        self.assertEqual(self.extension.actions(), ["navigate", "toolApproval", "cookies"])
        self.assertEqual(self.extension.sent[1][1]["timeoutMs"], bridge.STEP_APPROVAL_SECONDS * 1000)
        self.extension.sent.clear()
        self.approval = {"error": "x", "code": "needs_user", "state": {"decision": "declined"}}
        status, result = self.request("/api/batch", batch, tool="browser_batch", call="call-2")
        cookies = result["results"][1]
        self.assertEqual((cookies["code"], cookies["state"]["decision"]), ("needs_user", "declined"))
        self.assertEqual(self.extension.actions(), ["navigate", "toolApproval"], "the step did not run")
        self.assertEqual(result["progress"][1]["status"], "failed")


class ApprovalExtension(ElicitationExtension):
    """toolApproval as the background page answers: a token for the server to ask with, then the popup's
    answer (popup, after popup_delay seconds; None never answers) or the server's decision."""

    def __init__(self):
        super().__init__()
        self.popup = None
        self.popup_delay = 0
        self.decided = asyncio.Event()

    async def __call__(self, action, params=None, timeout=30):
        if action == "cookies":
            self.sent.append((action, dict(params or {})))
            return {"ok": True, "cookies": []}
        if action != "toolApproval":
            return await super().__call__(action, params, timeout)
        params = dict(params or {})
        self.sent.append((action, params))
        tool = params["tool"]
        refused = {"error": f"{tool} was not allowed", "code": "needs_user", "retryable": False,
                   "state": {"reason": "approval", "detail": "tool_approval", "tool": tool, "decision": "declined"}}
        if params.get("decision"):
            self.answer = ({"ok": True, "tool": tool, "scope": params["decision"]}
                           if params["decision"] in ("once", "session") else refused)
            self.decided.set()
            return self.answer
        if params.get("wait"):
            if self.popup is None:
                await self.decided.wait()
                return self.answer
            await asyncio.sleep(self.popup_delay)
            return self.popup
        if params.get("together"):
            return {**refused, "state": {**refused["state"], "decision": None, "confirmToken": "tok",
                                         "expiresInSeconds": 120, "together": True}}
        return self.popup or refused


class McpApprovalTests(ElicitationTestCase):
    def setUp(self):
        super().setUp()
        self.extension = ApprovalExtension()
        for patcher in (patch.object(bridge, "_send_to_extension_raw", new=lambda *a, **k: self.extension(*a, **k)),
                        patch.object(bridge, "_approval_tools", set(tool_approvals.DEFAULT_TOOLS)),
                        patch.object(bridge, "_call_approvals", {}), patch.object(bridge, "_session_approvals", set()),
                        patch.object(server, "SCRIPTING", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The state the held reply carries: decision None means not answered yet.
        original = ApprovalExtension.__call__

        async def held_without_decision(extension, action, params=None, timeout=30):
            result = await original(extension, action, params, timeout)
            if isinstance(result, dict) and (result.get("state") or {}).get("decision", 0) is None:
                result = {**result, "state": {key: value for key, value in result["state"].items() if key != "decision"}}
            return result

        patcher = patch.object(ApprovalExtension, "__call__", held_without_decision)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cookies(self):
        return [params for action, params in self.extension.sent if action == "cookies"]

    def test_the_clients_prompt_allows_the_call(self):
        async def allow(params):
            self.assertIn("browser_cookies", params.message)
            self.assertEqual(params.requestedSchema, server.TOOL_APPROVAL_SCHEMA)
            return types.ElicitResult(action="accept", content={"decision": "Allow once"})

        result, payload = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"}, allow)
        self.assertFalse(result.isError, payload)
        self.assertEqual(len(self.cookies()), 1)
        self.assertEqual(len(self.prompts), 1)
        decided = [params for action, params in self.extension.sent if action == "toolApproval" and "decision" in params]
        self.assertEqual(decided[0]["decision"], "once")
        self.assertNotIn("tok", self.text, "the model never sees the token")

    def test_bare_accept_does_not_allow_the_tool(self):
        self.extension.popup = {"error": "declined", "code": "needs_user",
                                "state": {"reason": "approval", "detail": "tool_approval",
                                          "tool": "browser_cookies", "decision": "declined"}}

        async def auto_accept(params):
            self.assertEqual(params.requestedSchema, server.TOOL_APPROVAL_SCHEMA)
            return types.ElicitResult(action="accept")

        result, _ = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"}, auto_accept)
        self.assertTrue(result.isError)
        self.assertEqual(self.cookies(), [])
        self.assertFalse(any("decision" in params for action, params in self.extension.sent
                             if action == "toolApproval"))

    def test_the_popup_answers_first(self):
        self.extension.popup = {"ok": True, "tool": "browser_cookies", "scope": "session"}

        async def never(params):
            await asyncio.sleep(30)

        result, payload = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"}, never, client=CODEX)
        self.assertFalse(result.isError, payload)
        self.assertEqual(len(self.cookies()), 1)
        # Allow for this session: the next call asks nothing.
        self.extension.sent.clear()
        result, _ = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"})
        self.assertFalse(result.isError)
        self.assertEqual([action for action, _ in self.extension.sent if action == "toolApproval"], [])

    def test_a_decline_runs_nothing(self):
        async def decline(params):
            await asyncio.sleep(1.2)  # a user's answer, not the client's policy (CLIENT_POLICY_SECONDS)
            return types.ElicitResult(action="accept", content={"decision": "Decline"})

        result, payload = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"}, decline, client=CODEX)
        self.assertTrue(result.isError)
        self.assertEqual((payload["code"], payload["state"]["decision"]), ("needs_user", "declined"))
        self.assertEqual(self.cookies(), [])

    def test_without_elicitation_the_popup_alone_asks(self):
        self.extension.popup = {"ok": True, "tool": "browser_cookies", "scope": "once"}
        result, payload = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"})
        self.assertFalse(result.isError, payload)
        asked = [params for action, params in self.extension.sent if action == "toolApproval"]
        self.assertFalse(asked[0].get("together"))
        self.extension.popup = None
        self.extension.sent.clear()
        result, payload = self.call("browser_cookies", {"op": "get", "url": "https://a.test/"})
        self.assertTrue(result.isError)
        self.assertEqual(payload["state"]["detail"], "tool_approval")
        self.assertEqual(self.cookies(), [])

    def test_unmarked_tools_ask_nothing(self):
        result, _ = self.call("browser_snapshot", {})
        self.assertFalse(result.isError)
        self.assertEqual([action for action, _ in self.extension.sent if action == "toolApproval"], [])

    def test_a_script_asks_for_the_tool_of_its_request_then_sends_it_again(self):
        with patch.object(server, "SCRIPTING", True):
            self.extension.popup = {"ok": True, "tool": "browser_cookies", "scope": "once"}
            result, payload = self.call("browser_run_python", {"code": "result = browser.cookies.get()"})
        self.assertFalse(result.isError, payload)
        self.assertEqual(len(self.cookies()), 1)
        asked = [params["tool"] for action, params in self.extension.sent if action == "toolApproval"]
        self.assertEqual(asked, ["browser_cookies"])

    def test_browser_run_python_itself_can_need_approval(self):
        bridge._set_approval_tools(["browser_run_python"])
        with patch.object(server, "SCRIPTING", True):
            result, payload = self.call("browser_run_python", {"code": "result = 1"})
        self.assertTrue(result.isError)
        self.assertEqual(payload["state"]["tool"], "browser_run_python")
        with patch.object(server, "SCRIPTING", True):
            self.extension.popup = {"ok": True, "tool": "browser_run_python", "scope": "once"}
            result, payload = self.call("browser_run_python", {"code": "result = 1"})
        self.assertEqual(payload.get("result"), 1, payload)


if __name__ == "__main__":
    unittest.main()
