"""Minimal-surface registration and real MCP protocol calls, with only the bridge transport mocked."""
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
            result = {"tools": names,
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
                described = await session.call_tool("browser_tools_call", {"name": "browser_click"})
                recovered = await session.call_tool("browser_tools_call", {
                    "name": "browser_click", "arguments": {"target": "#send"}})
                gateway = await session.call_tool("browser_tools_publish", {"names": ["browser_hover"]})
                result.update(ok=json.loads(ok.content[0].text), failed=json.loads(failed.content[0].text),
                              described=json.loads(described.content[0].text),
                              recovered=json.loads(recovered.content[0].text), gateway=json.loads(gateway.content[0].text),
                              statuses=[ok.isError, failed.isError, invalid.isError])
            return result
print(json.dumps(asyncio.run(main())))
'''


class ScriptMcpTests(unittest.TestCase):
    def probe(self, enabled):
        """enabled None leaves KITSUNE_SCRIPTING unset."""
        env = {key: value for key, value in os.environ.items() if key != "KITSUNE_SCRIPTING"}
        if enabled is not None:
            env["KITSUNE_SCRIPTING"] = "1" if enabled else "0"
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

    def test_code_first_list_has_six_tools_with_direct_recovery_but_no_publication(self):
        expected = {"browser_tabs", "browser_take_screenshot", "browser_ask_user", "browser_resume",
                    "browser_run_python", "browser_tools_call"}
        result = self.probe(True)
        self.assertEqual(set(result["tools"]), expected)
        self.assertEqual(result["described"]["name"], "browser_click")
        self.assertIn("target", result["described"]["inputSchema"]["properties"])
        self.assertNotIn("inputSchema", result["recovered"])
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
        self.assertEqual(len(shared), 2)
        for name in shared:
            with self.subTest(tool=name):
                self.assertEqual(legacy[name], playwright_tools.SCHEMAS[name]["inputSchema"], "legacy stays verbatim")
                self.assertEqual(structure(minimal[name]), structure(legacy[name]))
                self.assertLess(len(json.dumps(minimal[name])), len(json.dumps(legacy[name])))

    def test_removed_settings_are_refused_at_startup(self):
        # #525: a configuration that still sets one expects a limit the server no longer applies.
        for name, value in (("KITSUNE_CAPS", "core"), ("KITSUNE_PUBLISH", "core"), ("KITSUNE_MODE", "read")):
            with self.subTest(name=name):
                env = {**os.environ, name: value}
                reply = subprocess.run([sys.executable, "-c", "import kitsune.server"], env=env,
                                       capture_output=True, text=True, timeout=30)
                self.assertNotEqual(reply.returncode, 0)
                self.assertIn(f"Remove {name} from the MCP server environment", reply.stderr)

    def test_python_guide_names_the_methods_agents_got_wrong(self):
        description = self.probe(True)["description"]
        for example in ("browser.tabs()", "browser.new_tab(url)", ".snapshot(text=True)", "tab.fill(target, value)",
                        "tab.query(", "tab.scroll(", "tab.hover(", "tab.drag(", "tab.press(", "tab.wait_for(",
                        "browser_tools_call", "NOT a sandbox", "tab.find(query)", "not text/regex",
                        "max_depth=None", "no depth/boxes args", "CSS-only", "wait_for(target=...) accepts CSS/refs",
                        "fields append, editors insert at caret", "omitted target uses focus, not the last target"):
            self.assertIn(example, description)
        self.assertNotIn("help(tab.query)", description)
        self.assertNotIn("appends with key events", description)

    def test_python_guide_explains_tab_dicts_and_the_snapshot_example_works(self):
        result = self.probe(True)
        self.assertIn("list[dict]", result["description"])
        self.assertIn("id, url, title", result["description"])
        self.assertIn('browser.tab(t["id"]).snapshot(text=True)', result["description"])
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


if __name__ == "__main__":
    unittest.main()
