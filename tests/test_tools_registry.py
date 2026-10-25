"""kitsune.tools holds the agent tools without MCP code; kitsune.server registers them on FastMCP."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from kitsune import server, tools


def _run(code: str, env: dict | None = None) -> str:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("KITSUNE_")}
    return subprocess.run([sys.executable, "-c", code], env={**clean, **(env or {})},
                          capture_output=True, text=True, check=True).stdout


class ToolsRegistryTests(unittest.TestCase):
    def test_tools_load_no_mcp_code(self):
        loaded = _run("import json, sys, kitsune.tools; "
                      "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'mcp')))")
        self.assertEqual(json.loads(loaded), [])

    def test_the_server_registers_the_tools_in_their_order(self):
        # Every registered MCP tool is one of kitsune.tools', in its order, then the gateway's.
        code = """import asyncio, json
from kitsune import server, tools
print(json.dumps({"registered": list(asyncio.run(server.mcp.all_tools())), "defined": list(tools.TOOLS),
                  "unpublished": list(server.UNPUBLISHED_TOOLS)}))"""
        for env in ({}, {"KITSUNE_CAPS": "all,storage,history", "KITSUNE_PUBLISH": "core"},
                    {"KITSUNE_CAPS": "all", "KITSUNE_MODE": "read"}):
            with self.subTest(env=env):
                result = json.loads(_run(code, env))
                gateway = [name for name in result["registered"] if name in server.GATEWAY_TOOLS]
                own = [name for name in result["registered"] if name not in server.GATEWAY_TOOLS]
                restricted = env.get("KITSUNE_MODE", "act") != "act"
                expected = [name for name in result["defined"] if name not in result["unpublished"]
                            and (name != "browser_tab_list" or restricted)]
                self.assertEqual(own, expected)
                self.assertEqual(result["registered"][len(own):], gateway, "the gateway tools come last")

    def test_records_group_annotations_and_modes(self):
        status = tools.TOOLS["browser_status"]
        self.assertEqual((status.group, status.annotations["title"], status.annotations["readOnlyHint"]),
                         ("admin", "Check browser status", True))
        self.assertEqual(tools.TOOLS["browser_click"].group, "core")
        self.assertEqual(tools.TOOLS["browser_tab_list"].modes, {"read", "draft"})
        self.assertNotIn("browser_tab_list", server.TOOL_GROUPS, "act mode publishes browser_tabs instead")
        with self.assertRaises(ValueError):
            tools.tool("unknown")

    def test_browser_status_adds_this_servers_tool_surface(self):
        status = {"status": "running", "extension_connected": True, "diagnosis": {"code": "connected"}}
        with patch.object(tools, "_get", return_value=dict(status)):
            self.assertEqual(tools.browser_status(), status)
        with patch.object(tools, "_get", return_value=dict(status)):
            self.assertEqual(server.browser_status()["mcp_tools"]["caps"], sorted(server.ENABLED_CAPS))
        self.assertEqual(server.browser_status.__doc__, tools.browser_status.__doc__)

    def test_the_tools_send_through_the_servers_client(self):
        self.assertIs(tools.client, server._client)


if __name__ == "__main__":
    unittest.main()
