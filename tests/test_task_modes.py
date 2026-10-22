"""Task mode catalogs, per-request policy ceilings and shared-bridge isolation (#141)."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, server, task_modes
from test_ask_before_act import AskBeforeActTestCase

CATALOG = '''import asyncio, inspect, json
from unittest.mock import patch
from kitsune import server
published = asyncio.run(server.mcp.list_tools())
routes = {}
for tool in published:
    function = getattr(server, tool.name)
    args = {name: ({"title": "draft"} if name == "fields" else "https://example.com" if name == "url" else "example")
            for name, param in inspect.signature(function).parameters.items() if param.default is inspect.Parameter.empty}
    with patch.object(server, "_get", return_value={}) as get, patch.object(server, "_post", return_value={}) as post:
        function(**args)
        routes[tool.name] = [call.args[0] for call in get.call_args_list + post.call_args_list]
excluded = {}
for name in ("browser_click", "browser_type", "browser_submit_form", "browser_tabs", "browser_js", "browser_set_policy"):
    if name not in {tool.name for tool in published}:
        result = asyncio.run(server.mcp.call_tool(name, {}))
        excluded[name] = {"isError": result.isError, **json.loads(result.content[0].text)}
print(json.dumps({"tools": [tool.model_dump(mode="json", exclude_none=True) for tool in published],
                  "routes": routes, "excluded": excluded}))
'''


def catalog(mode, preset="lean", caps="core"):
    env = {**os.environ, "KITSUNE_MODE": mode, "KITSUNE_TOOL_PRESET": preset, "KITSUNE_CAPS": caps}
    result = subprocess.run([sys.executable, "-c", CATALOG], env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class TaskModeCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogs = {(mode, preset): catalog(mode, preset, "all,history")
                        for mode in ("read", "draft") for preset in ("lean", "full")}
        cls.read = catalog("read")

    def test_every_published_tool_is_permitted_by_the_mode(self):
        for (mode, preset), result in self.catalogs.items():
            for tool in result["tools"]:
                name = tool["name"]
                with self.subTest(mode=mode, preset=preset, tool=name):
                    self.assertTrue(task_modes.permits_tool(mode, name))
                    action = {**task_modes.READ_TOOLS, **task_modes.DRAFT_TOOLS}[name]
                    if action:
                        token = bridge._task_mode.set(mode)
                        try:
                            self.assertIsNone(bridge._check_policy(action, {"url": "https://example.com"} if action == "navigate" else {}))
                        finally:
                            bridge._task_mode.reset(token)
                    self.assertNotIn("confirm_token", tool["inputSchema"]["properties"])
                    self.assertTrue(result["routes"][name], "tool must reach the bridge")
                    for route in result["routes"][name]:
                        self.assertTrue(task_modes.permits_route(mode, route), route)

    def test_read_lean_is_a_small_list_only_surface(self):
        self.assertEqual({tool["name"] for tool in self.read["tools"]}, {
            "browser_status", "browser_tab_list", "browser_navigate", "browser_page_info", "browser_page_text",
            "browser_forms", "browser_dom", "browser_snapshot", "browser_find"})
        tabs = next(tool for tool in self.read["tools"] if tool["name"] == "browser_tab_list")
        self.assertEqual(tabs["inputSchema"]["properties"], {})
        # Character guard, not a tokenizer claim; exact tokens/task success belong to kitsune-benchmark.
        definitions = json.dumps(self.read["tools"], separators=(",", ":"), ensure_ascii=False)
        self.assertLess(len(definitions), 6000)

    def test_modes_intersect_presets_and_capabilities(self):
        for mode in ("read", "draft"):
            lean = {tool["name"] for tool in self.catalogs[mode, "lean"]["tools"]}
            full = {tool["name"] for tool in self.catalogs[mode, "full"]["tools"]}
            self.assertLess(lean, full)
            self.assertNotIn("browser_scroll", lean)
            self.assertIn("browser_scroll", full)
            self.assertIn("browser_history_search", full)
        self.assertNotIn("browser_history_search", {tool["name"] for tool in self.read["tools"]})
        read = {tool["name"] for tool in self.catalogs["read", "full"]["tools"]}
        draft = {tool["name"] for tool in self.catalogs["draft", "full"]["tools"]}
        self.assertEqual(draft - read, set(task_modes.DRAFT_TOOLS))

    def test_excluded_calls_by_name_explain_the_mode(self):
        for (mode, preset), result in self.catalogs.items():
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
        with patch.object(server, "MODE", "read"), patch.object(server, "_post") as post:
            result = asyncio.run(server.mcp.call_tool("browser_navigate", {
                "url": "https://example.com", "confirm_token": "token"}))
        self.assertTrue(result.isError)
        self.assertEqual(json.loads(result.content[0].text)["code"], "mode_restricted")
        post.assert_not_called()

    def test_mcp_forwards_mode_on_every_bridge_request(self):
        with patch.object(server, "MODE", "read"), patch.object(server.httpx, "request") as request:
            request.return_value.status_code = 200
            request.return_value.json.return_value = {"ok": True}
            server._get("/api/tabs")
            server._post("/api/navigate", {"url": "https://example.com"})
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
        for action in ("click", "type", "submitForm", "executeJS", "if", "while", "newTab", "handleDialog"):
            self.assertEqual(bridge._check_policy(action, {})["code"], "mode_restricted")
            result = asyncio.run(bridge.run_command({"action": action}))
            self.assertEqual(result["code"], "mode_restricted")
        self.assertEqual(self.extension.sent, [])

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
        for path in ("/api/batch", "/api/resume", "/api/shutdown", "/api/new-tab", "/api/screenshot"):
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
