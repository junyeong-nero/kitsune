"""Python scripting over an actual stdio MCP client and the isolated Zen/Firefox fixture."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness
from test_real_browser_python_api import _Site


class RealBrowserScriptTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    async def scenario(self, mode, run):
        env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_CAPS": "core,read,browse",
               "KITSUNE_PUBLISH": "core,read,browse", "KITSUNE_MODE": mode, "KITSUNE_OWNER_ID": "script-smoke"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            self.assertIn("browser_run_python", names)
            self.assertNotIn("browser_forms", names)
            self.assertNotIn("browser_hover", names)
            if mode == "act":
                opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
                self.assertFalse(opened.isError, opened)
                selected = await session.call_tool("browser_run_python", {"code": "result = tab.id"})
                self.assertFalse(selected.isError, selected)
                tab_id = json.loads(selected.content[0].text)["result"]
            else:
                opened = await session.call_tool("browser_tab_list", {"new_tab": self.site.origin})
                self.assertFalse(opened.isError, opened)
                tab_id = json.loads(opened.content[0].text)["tabId"]
            # The fixture's unrestricted operator channel cleans up even if the MCP session fails.
            self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
            await run(session, tab_id)

    def run_scenario(self, mode, run):
        asyncio.run(asyncio.wait_for(self.scenario(mode, run), 60))

    def test_direct_and_scripted_draft_share_the_tab_and_refuse_foreign_writes(self):
        foreign = self.open_tab(self.site.origin)

        async def run(session, tab_id):
            code = ("for name in ['Ada', 'Grace']:\n    tab.fill('#name', name)\n"
                    "tab.check('#agree')\n"
                    "result = [field['value'] for field in tab.forms() if field.get('name') == 'name']")
            scripted = await session.call_tool("browser_run_python", {"code": code})
            self.assertFalse(scripted.isError, scripted)
            self.assertEqual(json.loads(scripted.content[0].text)["result"], ["Grace"])
            observed = await session.call_tool("browser_snapshot", {})
            self.assertFalse(observed.isError, observed)
            self.assertIn("Grace", json.loads(observed.content[0].text)["snapshot"])

            next_call = await session.call_tool("browser_run_python", {
                "code": f"result = {{'fresh': 'name' not in globals(), 'title': browser.tab({tab_id}).info()['title']}}"})
            self.assertFalse(next_call.isError, next_call)
            self.assertEqual(json.loads(next_call.content[0].text)["result"], {"fresh": True, "title": "Python API"})

            refused = await session.call_tool("browser_run_python", {"code": f"browser.tab({foreign}).fill('#name', 'wrong')"})
            self.assertTrue(refused.isError, refused)
            self.assertEqual(json.loads(refused.content[0].text)["failed_operation"]["code"], "user_tab_read_only")
            fields = self.call("/api/forms", {"tabId": foreign})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), "")

            cookies = await session.call_tool("browser_run_python", {"code": "result = browser.cookies.get()"})
            self.assertTrue(cookies.isError, cookies)
            self.assertEqual(json.loads(cookies.content[0].text)["failed_operation"]["code"], "capability_restricted")

        self.run_scenario("act", run)

    def test_read_mode_refuses_scripted_writes(self):
        async def run(session, tab_id):
            read = await session.call_tool("browser_run_python", {"code": f"result = browser.tab({tab_id}).text()"})
            self.assertFalse(read.isError, read)
            self.assertIn("Orders", json.loads(read.content[0].text)["result"])
            write = await session.call_tool("browser_run_python", {"code": f"browser.tab({tab_id}).fill('#name', 'wrong')"})
            self.assertTrue(write.isError, write)
            self.assertEqual(json.loads(write.content[0].text)["failed_operation"]["code"], "mode_restricted")
            fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), "")
        self.run_scenario("read", run)

    def test_approval_stop_keeps_the_draft_and_does_not_offer_script_replay(self):
        self.call("/api/set-policy", {"ask": True})

        async def run(session, tab_id):
            code = (f"tab = browser.tab({tab_id})\n"
                    "tab.fill('#name', 'Keep this draft')\n"
                    "tab.submit_form('#order')\n"
                    "tab.fill('#name', 'must not run')")
            stopped = await session.call_tool("browser_run_python", {"code": code})
            self.assertTrue(stopped.isError, stopped)
            result = json.loads(stopped.content[0].text)
            self.assertEqual(result["failed_operation"]["code"], "needs_user")
            self.assertNotIn("confirmToken", result["failed_operation"]["state"])
            self.assertNotIn("resume_token", result)
            self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
            fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), "Keep this draft")
            self.assertEqual(self.call("/api/page-info", {"tabId": tab_id})["title"], "Python API")
        try:
            self.run_scenario("act", run)
        finally:
            self.call("/api/set-policy", {"ask": False})
