"""Task mode catalogs, per-request policy ceilings and shared-bridge isolation (#141)."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, bridge_client, server, task_modes, tools
from test_ask_before_act import AskBeforeActTestCase

CATALOG = '''import asyncio, inspect, json
from unittest.mock import patch
from kitsune import playwright_tools, server, tools
SAMPLES = {"fields": [{"target": "e1", "name": "Title", "type": "textbox", "value": "draft"}],
           "url": "https://example.com", "values": ["example"], "index": 1}
OPTIONAL = {"browser_wait_for": {"text": "example"}, "browser_find": {"text": "example"}}
published = asyncio.run(server.mcp.list_tools())
routes = {}
for tool in published:
    function = getattr(playwright_tools if tool.name in playwright_tools.SCHEMAS else tools, tool.name)
    args = {name: SAMPLES.get(name, "example")
            for name, param in inspect.signature(function).parameters.items() if param.default is inspect.Parameter.empty}
    args.update(OPTIONAL.get(tool.name, {}))
    with patch.object(tools, "_get", return_value={}) as get, patch.object(tools, "_post", return_value={}) as post:
        function(**args)
        routes[tool.name] = [call.args[0] for call in get.call_args_list + post.call_args_list]
excluded = {}
for name in ("browser_click", "browser_type", "browser_submit_form", "browser_tabs", "browser_evaluate", "browser_set_policy"):
    if name not in {tool.name for tool in published}:
        result = asyncio.run(server.mcp.call_tool(name, {}))
        excluded[name] = {"isError": result.isError, **json.loads(result.content[0].text)}
print(json.dumps({"tools": [tool.model_dump(mode="json", exclude_none=True) for tool in published],
                  "routes": routes, "excluded": excluded}))
'''


# Tools that answer from state the MCP server keeps (the last browser_network_requests list).
LOCAL_TOOLS = {"browser_network_request"}


def catalog(mode, caps="core"):
    env = {**os.environ, "KITSUNE_MODE": mode, "KITSUNE_CAPS": caps}
    result = subprocess.run([sys.executable, "-c", CATALOG], env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class TaskModeCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = {mode: catalog(mode, "all,storage,history") for mode in ("read", "draft")}
        cls.core = {mode: catalog(mode) for mode in ("read", "draft")}

    def test_every_published_tool_is_permitted_by_the_mode(self):
        for mode, result in [*self.catalogs.items(), *self.core.items()]:
            for tool in result["tools"]:
                name = tool["name"]
                with self.subTest(mode=mode, tool=name):
                    self.assertTrue(task_modes.permits_tool(mode, name))
                    action = {**task_modes.READ_TOOLS, **task_modes.DRAFT_TOOLS}[name]
                    if action:
                        token = bridge._task_mode.set(mode)
                        try:
                            self.assertIsNone(bridge._check_policy(action, {"url": "https://example.com"} if action == "navigate" else {}))
                        finally:
                            bridge._task_mode.reset(token)
                    self.assertNotIn("confirm_token", tool["inputSchema"]["properties"])
                    if name not in LOCAL_TOOLS:
                        self.assertTrue(result["routes"][name], "tool must reach the bridge")
                    for route in result["routes"][name]:
                        self.assertTrue(task_modes.permits_route(mode, route), route)

    def test_read_core_is_a_small_list_only_surface(self):
        read = self.core["read"]["tools"]
        self.assertEqual({tool["name"] for tool in read}, {
            "browser_tab_list", "browser_navigate", "browser_snapshot", "browser_find", "browser_wait_for",
            "browser_console_messages", "browser_network_requests", "browser_network_request"})
        tabs = next(tool for tool in read if tool["name"] == "browser_tab_list")
        # new_tab opens a background tab (#270); there is no close or select.
        self.assertEqual(set(tabs["inputSchema"]["properties"]), {"new_tab"})
        self.assertEqual(tabs["inputSchema"].get("required", []), [])
        # Character guard, not a tokenizer claim; exact tokens/task success belong to kitsune-benchmark.
        # Mostly Playwright MCP's definitions, published verbatim.
        definitions = json.dumps(read, separators=(",", ":"), ensure_ascii=False)
        self.assertLess(len(definitions), 6600)

    def test_modes_intersect_capabilities(self):
        for mode in ("read", "draft"):
            core = {tool["name"] for tool in self.core[mode]["tools"]}
            everything = {tool["name"] for tool in self.catalogs[mode]["tools"]}
            self.assertLess(core, everything)
            self.assertNotIn("browser_scroll", core)
            self.assertIn("browser_scroll", everything)
            self.assertIn("browser_history_search", everything)
            self.assertNotIn("browser_history_search", core)
        read = {tool["name"] for tool in self.catalogs["read"]["tools"]}
        draft = {tool["name"] for tool in self.catalogs["draft"]["tools"]}
        self.assertEqual(draft - read, set(task_modes.DRAFT_TOOLS))

    def test_excluded_calls_by_name_explain_the_mode(self):
        for mode, result in self.catalogs.items():
            for name, call in result["excluded"].items():
                self.assertTrue(call["isError"], name)
                self.assertEqual(call["code"], "tool_not_published")
                self.assertIn("KITSUNE_MODE=", call["hint"])
                self.assertEqual(call["state"]["mode"], mode)

    def test_modes_validate_at_startup_and_default_to_act(self):
        self.assertEqual(task_modes.task_mode(None), "act")
        self.assertEqual(task_modes.task_mode(" DRAFT "), "draft")
        env = {**os.environ, "KITSUNE_MODE": "unsafe"}
        result = subprocess.run([sys.executable, "-c", "import kitsune.server"], env=env,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown KITSUNE_MODE: unsafe", result.stderr)

    def test_confirmation_is_refused_on_a_published_tool(self):
        with patch.object(server, "MODE", "read"), patch.object(tools, "_post") as post:
            result = asyncio.run(server.mcp.call_tool("browser_navigate", {
                "url": "https://example.com", "confirm_token": "token"}))
        self.assertTrue(result.isError)
        self.assertEqual(json.loads(result.content[0].text)["code"], "mode_restricted")
        post.assert_not_called()

    def test_tab_list_new_tab_opens_the_current_tab_without_selecting_it(self):
        tools.browser_tab_list  # published only in read and draft
        tabs = {"tabs": [{"id": 3, "title": "user", "url": "https://user.example/", "active": True},
                         {"id": 9, "title": "Example", "url": "https://example.com/", "owned": True}]}
        opened = {"ok": True, "tabId": 9, "url": "https://example.com/", "status": "complete", "snapshot": "x"}
        with patch.object(tools, "_post", return_value=opened) as post, \
                patch.object(tools, "_get", return_value=tabs), \
                patch.object(server.playwright_tools, "_current_tab", None):
            result = tools.browser_tab_list(new_tab="https://example.com/")
            current = server.playwright_tools._current_tab
        self.assertEqual(post.call_args.args[0], "/api/new-tab")
        self.assertEqual(post.call_args.args[1], {"url": "https://example.com/", "wait": True, "timeout": 15000})
        self.assertEqual(current, 9, "later tools act on the new tab")
        self.assertEqual(result, {"tabId": 9, "url": "https://example.com/", "status": "complete",
                                  "tabs": tabs["tabs"]})
        # A refused or failed open changes nothing and returns the error.
        refused = {"error": "read mode refuses newTab", "code": "mode_restricted", "retryable": False}
        with patch.object(tools, "_post", return_value=refused), patch.object(tools, "_get") as get, \
                patch.object(server.playwright_tools, "_current_tab", 3):
            self.assertEqual(tools.browser_tab_list(new_tab="file:///etc/passwd"), refused)
            self.assertEqual(server.playwright_tools._current_tab, 3)
        get.assert_not_called()

    def test_mcp_forwards_mode_on_every_bridge_request(self):
        with patch.object(server._client, "mode", "read"), patch.object(bridge_client.httpx, "request") as request:
            request.return_value.status_code = 200
            request.return_value.json.return_value = {"ok": True}
            tools._get("/api/tabs")
            tools._post("/api/navigate", {"url": "https://example.com"})
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"][task_modes.MODE_HEADER], "read")


class TaskModePolicyTests(AskBeforeActTestCase):
    ask = False

    def setUp(self):
        super().setUp()
        self.extension.target["sensitive"] = False

    def mode(self, mode):
        token = bridge._task_mode.set(mode)
        self.addCleanup(bridge._task_mode.reset, token)

    def test_read_refuses_writes_and_container_steps_before_dispatch(self):
        self.mode("read")
        for action in ("click", "type", "submitForm", "executeJS", "if", "while", "closeTab", "switchTab",
                       "handleDialog"):
            self.assertEqual(bridge._check_policy(action, {})["code"], "mode_restricted")
            result = asyncio.run(bridge.run_command({"action": action}))
            # handleDialog is no batch action, which a container step learns first (#249).
            self.assertEqual(result["code"], "unknown_action" if action == "handleDialog" else "mode_restricted")
        self.assertEqual(self.extension.sent, [])

    def test_read_and_draft_open_background_tabs_for_http_urls(self):
        # A tab of its own keeps a restricted agent's navigation off the user's tabs (#270).
        for mode in ("read", "draft"):
            with self.subTest(mode=mode):
                self.mode(mode)
                self.extension.sent.clear()
                for url in ("https://example.com/a", "http://example.com/", "about:blank", None):
                    body = {} if url is None else {"url": url}
                    self.assertEqual(self.call(lambda h: h.handle_new_tab(), body), (200, {"ok": True}), url)
                self.assertEqual(self.extension.actions(), ["newTab"] * 4)
                self.extension.sent.clear()
                for url in ("file:///etc/passwd", "javascript:alert(1)", "moz-extension://x/page.html"):
                    status, result = self.call(lambda h: h.handle_new_tab(), {"url": url})
                    self.assertEqual((status, result["code"]), (403, "mode_restricted"), url)
                self.assertEqual(self.extension.actions(), [])

    def test_a_protected_user_tab_points_restricted_modes_to_a_tab_of_their_own(self):
        refused = {"error": "Tab 3 was not opened by the agent and this session protects user tabs, so it was "
                            "not navigated", "code": "user_tab", "retryable": False,
                   "hint": "Nothing navigated. Open a tab of your own (browser_tabs action=new) and work there, "
                           "or ask the user and repeat a direct call with tab_id and the "
                           "confirm_token it returns."}
        self.extension.reply = refused
        for mode in ("read", "draft", "act"):
            with self.subTest(mode=mode):
                self.mode(mode)
                status, result = self.forward("navigate", {"tabId": 3, "url": "https://example.com"})
                self.assertEqual((status, result["code"], result["state"]["detail"], result["state"]["approvable"]),
                                 (200, "needs_user", "user_tab", mode == "act"))
                if mode == "act":
                    self.assertEqual(result["hint"], refused["hint"])
                else:
                    self.assertIn("browser_tab_list new_tab", result["hint"])
                    self.assertNotIn("confirm_token", result["hint"])
                    self.assertNotIn("browser_tabs", result["hint"])

    def test_draft_refuses_a_payment_like_new_tab_as_it_refuses_navigate(self):
        # Draft's forced ask-before-act holds a payment-like URL; draft cannot confirm it.
        self.mode("draft")
        for route in (lambda h: h.handle_new_tab(), lambda h: h._forward("navigate")):
            status, result = self.call(route, {"url": "https://shop.example/checkout"})
            self.assertEqual((status, result["code"], result["state"]["reason"]),
                             (403, "mode_restricted", "payment_url"))
        self.assertEqual(self.extension.actions(), [])

    def test_draft_fills_ordinary_fields_without_changing_shared_policy(self):
        self.mode("draft")
        self.assertEqual(self.forward("fill", {"selector": "#title", "value": "draft"}), (200, {"ok": True}))
        self.assertEqual(self.extension.actions(), ["fill"])
        self.assertFalse(bridge._policy["ask"])
        self.assertEqual(bridge._check_policy("submitForm", {})["code"], "mode_restricted")

    def test_draft_sensitive_payment_and_uninspectable_fields_are_refused_without_tokens(self):
        self.mode("draft")
        for target, url in [({"sensitive": True}, "https://example.com"),
                            ({"sensitive": False}, "https://example.com/checkout"),
                            ({}, "https://example.com"),  # older extension without the inspection flag
                            ({"error": "cannot inspect"}, "https://example.com")]:
            self.extension.target, self.extension.url = target, url
            self.extension.sent.clear()
            status, result = self.forward("fill", {"selector": "#password", "value": "secret"})
            self.assertEqual(status, 403)
            self.assertEqual(result["code"], "mode_restricted")
            self.assertNotIn("confirmToken", result.get("state", {}))
            self.assertEqual(self.extension.actions(), [])

    def test_draft_form_preflight_sends_only_field_keys_and_never_partial_writes(self):
        self.mode("draft")
        self.extension.target = {"sensitive": True}
        status, result = self.forward("formFill", {"fields": {"title": "draft", "Password": "secret"}})
        self.assertEqual((status, result["code"]), (403, "mode_restricted"))
        self.assertEqual(self.extension.actions(), [])
        inspected = self.extension.sent[0][1]
        self.assertEqual(inspected["fieldKeys"], ["title", "Password"])
        self.assertNotIn("secret", json.dumps(inspected))

    def test_restricted_modes_refuse_confirmation_script_urls_and_html_insertion(self):
        for mode in ("read", "draft"):
            token = bridge._task_mode.set(mode)
            try:
                self.assertIsNone(bridge._check_policy("navigate", {"url": "https://example.com"}))
                for url in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///tmp/x"):
                    self.assertEqual(bridge._check_policy("navigate", {"url": url})["code"], "mode_restricted")
                self.assertEqual(bridge._check_policy("navigate", {"confirmToken": "token"})["code"], "mode_restricted")
            finally:
                bridge._task_mode.reset(token)
        self.mode("draft")
        self.assertEqual(bridge._check_policy("setEditableContent", {"format": "html"})["code"], "mode_restricted")

    def test_dispatch_enforces_routes_aliases_and_restores_request_context(self):
        handler = self.handler({})
        handler.path = "/api/setPolicy"
        handler.headers = {task_modes.MODE_HEADER: "read"}
        route = Mock()
        handler._dispatch({"/api/set-policy": route})
        route.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[1]["code"], "mode_restricted")
        self.assertEqual(bridge._task_mode.get(), "act")
        for path in ("/api/batch", "/api/resume", "/api/shutdown", "/api/close-tab", "/api/switch-tab",
                     "/api/screenshot"):
            handler.path = path
            handler._dispatch({path: route})
            route.assert_not_called()
        handler.headers = {task_modes.MODE_HEADER: "invalid"}
        handler._dispatch({})
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        handler.headers = {}
        handler._dispatch({handler.path: route})
        route.assert_called_once()

    def test_effective_policy_reports_mode_without_mutating_shared_policy(self):
        self.mode("draft")
        status, policy = self.call(lambda h: h.handle_get_policy(), {})
        self.assertEqual(status, 200)
        self.assertEqual(policy["mode"], "draft")
        self.assertTrue(policy["ask"])
        self.assertFalse(policy["confirmAllowed"])
        self.assertNotIn("mode", bridge._policy)


if __name__ == "__main__":
    unittest.main()
