"""Opt-in registration and real MCP protocol calls, with only the bridge transport mocked."""
import json
import os
import subprocess
import sys
import unittest

PROBE = '''
import asyncio, json
from unittest.mock import patch
from mcp.shared.memory import create_connected_server_and_client_session
from kitsune import server

def request(method, route, **kwargs):
    if route == "/api/tabs":
        return {"tabs": [{"id": 7}]}
    if route == "/api/snapshot":
        return {"snapshot": '- heading "Example"'}
    if route == "/api/click":
        return {"error": "Write needs user help", "code": "needs_user", "call": 1,
                "state": {"reason": "user_active", "tabId": 7}}
    return {"ok": True}

async def main():
    with patch.object(server._client, "request", side_effect=request):
        async with create_connected_server_and_client_session(server.mcp) as session:
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            result = {"tools": names, "caps": server._client.caps, "mode": server._client.mode,
                      "definition_chars": sum(len(json.dumps(tool.model_dump(mode="json", exclude_none=True),
                                                             ensure_ascii=False, separators=(",", ":"))) for tool in listed.tools),
                      "instructions_chars": len(server.mcp._mcp_server.instructions),
                      "schemas": {tool.name: tool.inputSchema for tool in listed.tools}}
            if "browser_run_python" in names:
                tool = next(tool for tool in listed.tools if tool.name == "browser_run_python")
                result.update(schema=tool.inputSchema, description=tool.description)
                ok = await session.call_tool("browser_run_python", {"code": "result = browser.tabs()"})
                snapshots = await session.call_tool("browser_run_python", {
                    "code": 'result = [browser.tab(t["id"]).snapshot() for t in browser.tabs()]'})
                result["snapshots"] = json.loads(snapshots.content[0].text)
                failed = await session.call_tool("browser_run_python", {"code": "browser.tab(7).click('#send')"})
                invalid = await session.call_tool("browser_run_python", {"code": "pass", "timeout": 0})
                moved = await session.call_tool("browser_hover", {"target": "#item"})
                gateway = await session.call_tool("browser_tools_publish", {"names": ["browser_hover"]})
                result.update(ok=json.loads(ok.content[0].text), failed=json.loads(failed.content[0].text),
                              moved=json.loads(moved.content[0].text), gateway=json.loads(gateway.content[0].text),
                              statuses=[ok.isError, failed.isError, invalid.isError])
            return result
print(json.dumps(asyncio.run(main())))
'''


class ScriptMcpTests(unittest.TestCase):
    def probe(self, enabled, mode="act", caps="core", publish=None):
        """enabled None leaves KITSUNE_SCRIPTING unset."""
        env = {key: value for key, value in os.environ.items() if key != "KITSUNE_SCRIPTING"}
        if enabled is not None:
            env["KITSUNE_SCRIPTING"] = "1" if enabled else "0"
        env.update(KITSUNE_MODE=mode, KITSUNE_CAPS=caps, KITSUNE_PUBLISH=caps if publish is None else publish)
        reply = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        return json.loads(reply.stdout)

    def test_minimal_surface_is_the_default_and_zero_keeps_the_legacy_list(self):
        # #525: unset selects the minimal surface; KITSUNE_SCRIPTING=0 is the legacy opt-out.
        self.assertEqual(set(self.probe(None)["tools"]), set(self.probe(True)["tools"]))
        legacy = self.probe(False)["tools"]
        self.assertNotIn("browser_run_python", legacy)
        self.assertIn("browser_hover", legacy)

    def test_other_values_are_refused_at_startup(self):
        for value in ("true", "yes", "2"):
            with self.subTest(value=value):
                env = {**os.environ, "KITSUNE_SCRIPTING": value}
                reply = subprocess.run([sys.executable, "-c", "import kitsune.server"], env=env,
                                       capture_output=True, text=True, timeout=30)
                self.assertNotEqual(reply.returncode, 0)
                self.assertIn("KITSUNE_SCRIPTING must be 0 or 1", reply.stderr)

    def test_minimal_list_has_fourteen_tools_without_discovery_or_publication(self):
        expected = {"browser_tabs", "browser_navigate", "browser_snapshot", "browser_find", "browser_click",
                    "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
                    "browser_wait_for", "browser_take_screenshot", "browser_ask_user", "browser_resume",
                    "browser_run_python"}
        for caps in ("core", "all", "all,storage,history"):
            with self.subTest(caps=caps):
                result = self.probe(True, caps=caps)
                self.assertEqual(set(result["tools"]), expected)
                self.assertEqual(result["moved"]["code"], "tool_moved_to_python")
                self.assertIn("KITSUNE_SCRIPTING=0", result["moved"]["hint"])
                self.assertEqual(result["gateway"]["code"], "tool_moved_to_python")

    def test_minimal_definitions_and_instructions_are_smaller_with_the_script_guide_included(self):
        legacy, minimal = self.probe(False), self.probe(True)
        self.assertLess(minimal["definition_chars"] + minimal["instructions_chars"],
                        legacy["definition_chars"] + legacy["instructions_chars"])

    def test_scripting_shortens_only_core_parameter_descriptions(self):
        from kitsune import playwright_tools

        def structure(schema):
            if isinstance(schema, dict):
                return {keyword: structure(value) for keyword, value in schema.items()
                        if keyword not in ("description", "$schema")}
            return schema

        legacy, minimal = self.probe(False)["schemas"], self.probe(True)["schemas"]
        shared = set(minimal) & set(playwright_tools.SCHEMAS)
        self.assertEqual(len(shared), 11)
        for name in shared:
            with self.subTest(tool=name):
                self.assertEqual(legacy[name], playwright_tools.SCHEMAS[name]["inputSchema"], "legacy stays verbatim")
                self.assertEqual(structure(minimal[name]), structure(legacy[name]))
                self.assertLess(len(json.dumps(minimal[name])), len(json.dumps(legacy[name])))

    def test_explicit_caps_and_publication_limits_are_not_broadened(self):
        result = self.probe(True, caps="core,read", publish="read")
        self.assertEqual(result["tools"], ["browser_run_python"])
        self.assertEqual(result["caps"], "core,read")
        self.assertEqual(self.probe(True)["caps"], "core")

    def test_python_guide_names_only_methods_allowed_by_caps_and_mode(self):
        for caps in ("core", "core,read", "read", "all", ""):
            for mode in ("act", "read", "draft"):
                with self.subTest(caps=caps, mode=mode):
                    description = self.probe(True, mode=mode, caps=caps)["description"]
                    core = "core" in caps or caps == "all"
                    read = "read" in caps or caps == "all"
                    self.assertEqual("browser.tabs()" in description, core)
                    self.assertEqual("browser.new_tab(url)" in description, core)
                    self.assertEqual(".snapshot()" in description, core)
                    self.assertEqual("tab.fill(target, value)" in description, core and mode != "read")
                    self.assertEqual("tab.query(" in description, read)
                    self.assertNotIn("help(tab.query)", description)

    def test_python_guide_uses_execution_caps_not_publication_caps(self):
        description = self.probe(True, caps="core,read", publish="read")["description"]
        self.assertIn("browser.tabs()", description)
        self.assertIn("tab.query(", description)

    def test_python_guide_explains_tab_dicts_and_the_snapshot_example_works(self):
        result = self.probe(True)
        self.assertIn("list[dict]", result["description"])
        self.assertIn("id, url, title", result["description"])
        self.assertIn('browser.tab(t["id"]).snapshot()', result["description"])
        self.assertTrue(result["snapshots"]["ok"], result["snapshots"])
        self.assertEqual(result["snapshots"]["result"], ['- heading "Example"'])
        self.assertEqual(result["ok"]["result"], [{"id": 7}], "tabs() must keep returning JSON-compatible dicts")

    def test_mcp_reports_results_errors_and_never_checkpoints_the_whole_script(self):
        result = self.probe(True)
        self.assertEqual(result["statuses"], [False, True, True])
        self.assertEqual(result["ok"]["result"], [{"id": 7}])
        self.assertEqual(result["failed"]["code"], "script_operation_failed")
        self.assertNotIn("resume_token", result["failed"])
        self.assertEqual(set(result["schema"]["properties"]), {"code", "timeout"})

    def test_opted_in_restricted_mode_can_use_python_with_its_browser_client(self):
        for mode in ("read", "draft"):
            with self.subTest(mode=mode):
                result = self.probe(True, mode)
                self.assertEqual(result["ok"]["result"], [{"id": 7}])
                self.assertEqual(result["mode"], mode)
                self.assertIn("browser_tab_list", result["tools"])
                self.assertNotIn("browser_tabs", result["tools"])
                self.assertNotIn("browser_resume", result["tools"])
                self.assertNotIn("browser_click", result["tools"])


if __name__ == "__main__":
    unittest.main()
