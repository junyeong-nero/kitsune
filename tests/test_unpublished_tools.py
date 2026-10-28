"""#171: a known tool left out by KITSUNE_CAPS names the setting that publishes it."""
import json
import os
import subprocess
import sys
import unittest


def list_tools(caps="core") -> dict:
    env = {**os.environ, "KITSUNE_CAPS": caps}
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio,json; from kitsune.server import mcp; print(json.dumps("
         "{t.name: t.model_dump(mode='json', exclude_none=True) for t in asyncio.run(mcp.list_tools())}))"],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


# Calls known and unknown tool names through FastMCP's call_tool, and browser_status with the bridge mocked.
UNPUBLISHED_SCRIPT = """import asyncio, json, sys
from unittest.mock import patch
from kitsune import server, tools
def call(name):
    try:
        result = asyncio.run(server.mcp.call_tool(name, {}))
    except Exception as error:
        return {"raised": str(error)}
    return {"isError": result.isError, **json.loads(result.content[0].text)}
with patch.object(tools, "_get", return_value={"status": "running"}):
    status = server.browser_status()
print(json.dumps({"calls": {name: call(name) for name in sys.argv[1:]}, "mcp_tools": status["mcp_tools"],
                  "unpublished": sorted(server.UNPUBLISHED_TOOLS)}))
"""


def unpublished(caps, *names) -> dict:
    env = {**os.environ, "KITSUNE_CAPS": caps, "KITSUNE_OWNER_ID": "owner-171"}
    result = subprocess.run([sys.executable, "-c", UNPUBLISHED_SCRIPT, *names],
                            env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class UnpublishedToolTests(unittest.TestCase):
    def assert_not_published(self, call, group, hint):
        self.assertEqual(call, {
            "isError": True, "error": call["error"], "code": "tool_not_published", "retryable": False,
            "hint": f"Set {hint} in the MCP server environment, then restart the MCP client.",
            "state": {**call["state"], "group": group}})
        self.assertIn("is not published", call["error"])

    def test_default_names_the_group(self):
        result = unpublished("core", "browser_scroll", "browser_history_search", "browser_nope")
        calls = result["calls"]
        self.assert_not_published(calls["browser_scroll"], "browse", "KITSUNE_CAPS=browse,core")
        self.assertEqual(calls["browser_scroll"]["state"], {"group": "browse", "caps": ["core"]})
        self.assert_not_published(calls["browser_history_search"], "history", "KITSUNE_CAPS=core,history")
        self.assertEqual(calls["browser_nope"], {"raised": "Unknown tool: browser_nope"})

        published = set(list_tools())
        everything = set(list_tools("all,storage,history"))
        self.assertFalse(published & set(result["unpublished"]))
        self.assertEqual(published | set(result["unpublished"]), everything)
        self.assertRegex(result["mcp_tools"].pop("client"), r"^\d+-[0-9a-f]{6}$")
        self.assertEqual(result["mcp_tools"].pop("owner"), "owner-171")  # #325
        self.assertEqual(result["mcp_tools"], {"caps": ["core"], "unpublished": len(everything) - 25})

    def test_core_tool_outside_caps_names_core(self):
        result = unpublished("browse", "browser_click")
        self.assert_not_published(result["calls"]["browser_click"], "core", "KITSUNE_CAPS=browse,core")

    def test_all_leaves_only_named_groups_unpublished(self):
        result = unpublished("all", "browser_bookmarks_search", "browser_clipboard")
        self.assert_not_published(result["calls"]["browser_bookmarks_search"], "history",
                                  "KITSUNE_CAPS=all,history")
        self.assert_not_published(result["calls"]["browser_clipboard"], "storage", "KITSUNE_CAPS=all,storage")
        self.assertEqual(result["unpublished"], [
            "browser_bookmarks_search", "browser_clear_browsing_data", "browser_clipboard", "browser_cookies",
            "browser_delete_session", "browser_history_search", "browser_history_visits",
            "browser_list_sessions", "browser_load_session", "browser_save_session", "browser_storage"])
        self.assertRegex(result["mcp_tools"].pop("client"), r"^\d+-[0-9a-f]{6}$")
        self.assertEqual(result["mcp_tools"].pop("owner"), "owner-171")  # #325
        self.assertEqual(result["mcp_tools"], {"caps": ["all"], "unpublished": 11})


if __name__ == "__main__":
    unittest.main()
