"""Tools and batch commands that name no tab act on the agent's current tab, in a real browser (#232).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Each test has a
"user" tab, made active, and an "agent" tab in the background; the pages say which one they are.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

MCP_SESSION_TIMEOUT = 60


class _Pages(BaseHTTPRequestHandler):
    """/<name> answers with a tall page whose text is "<name> page"."""

    def do_GET(self):
        name = self.path.strip("/") or "index"
        body = f"<title>{name}</title><p>{name} page</p><div style='height: 5000px'></div>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _payload(result):
    return json.loads(result.content[0].text)


async def _mcp_calls(env, url):
    """Over stdio: open url with the core tools, then read and batch without naming a tab."""
    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("browser_tabs", {"action": "new"})
        await session.call_tool("browser_navigate", {"url": url})
        text = await session.call_tool("browser_tools_call", {"name": "browser_page_text"})
        batch = await session.call_tool("browser_tools_call", {"name": "browser_batch", "arguments": {
            "commands": [{"action": "scroll", "direction": "down"}, {"action": "pageInfo"}]}})
        closed = await session.call_tool("browser_tabs", {"action": "close"})
    return _payload(text), _payload(batch), closed


class RealBrowserCurrentTabTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        cls.wait_for_extension()

    def setUp(self):
        self.user = self.open_tab(self.site.origin + "/user")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": self.user}))
        self.agent = self.open_tab(self.site.origin + "/agent")

    def page_text(self, result):
        return result["results"][0]["text"]

    def scroll_y(self, tab):
        info = self.call("/api/page-info", {"tabId": tab})
        self.assertNotIn("error", info)
        return info["scrollY"]

    def test_batch_commands_without_a_tab_run_on_its_default_tab(self):
        # Without a default tab, a step that names no tab still goes to the active (user's) tab.
        self.assertIn("user page", self.page_text(self.call("/api/batch", {"commands": [{"action": "pageText"}]})))
        result = self.call("/api/batch", {"commands": [{"action": "pageText"},
                                                       {"action": "scroll", "direction": "down"}],
                                          "tabId": self.agent})
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("agent page", self.page_text(result))
        self.assertGreater(self.scroll_y(self.agent), 0)
        self.assertEqual(self.scroll_y(self.user), 0)

    def test_mcp_tools_act_on_the_tab_the_core_tools_opened(self):
        env = {**self.bridge.env, "KITSUNE_CAPS": "all", "KITSUNE_PUBLISH": "core"}
        text, batch, closed = asyncio.run(asyncio.wait_for(
            _mcp_calls(env, self.site.origin + "/mcp"), MCP_SESSION_TIMEOUT))
        self.assertIn("mcp page", text["text"])
        self.assertEqual(batch["status"], "ok", batch)
        info = batch["results"][1]
        self.assertTrue(info["url"].endswith("/mcp"), info)
        self.assertGreater(info["scrollY"], 0)
        self.assertFalse(closed.isError, closed)
        self.assertEqual(self.scroll_y(self.user), 0)


if __name__ == "__main__":
    unittest.main()
