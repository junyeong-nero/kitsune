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

    async def scenario(self, run):
        env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "script-smoke"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            self.assertIn("browser_run_python", names)
            self.assertNotIn("browser_forms", names)
            self.assertNotIn("browser_hover", names)
            opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
            self.assertFalse(opened.isError, opened)
            selected = await session.call_tool("browser_run_python", {"code": "result = tab.id"})
            self.assertFalse(selected.isError, selected)
            tab_id = json.loads(selected.content[0].text)["result"]
            # The fixture's unrestricted operator channel cleans up even if the MCP session fails.
            self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
            await run(session, tab_id)

    def run_scenario(self, run):
        asyncio.run(asyncio.wait_for(self.scenario(run), 60))

    def test_guide_reads_tab_dicts_and_every_method_runs(self):
        async def run(session, tab_id):
            listed = await session.list_tools()
            description = next(tool.description for tool in listed.tools if tool.name == "browser_run_python")
            self.assertIn("list[dict]", description)
            example = 'browser.tab(t["id"]).snapshot(text=True)'
            self.assertIn(example, description)
            # Read the fixture's page, not browser-owned about:blank or welcome tabs.
            code = f't = next(t for t in browser.tabs() if t["id"] == {tab_id})\nresult = {example}'
            snapshot = await session.call_tool("browser_run_python", {"code": code})
            self.assertFalse(snapshot.isError, snapshot)
            self.assertIn("Orders", json.loads(snapshot.content[0].text)["result"])
            query = await session.call_tool("browser_run_python", {
                "code": f'result = browser.tab({tab_id}).query("h1", fields=["text"])'})
            # #525: no capability group limits the supplied browser API.
            self.assertFalse(query.isError, query)
        self.run_scenario(run)

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

        self.run_scenario(run)

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
            self.assertEqual(result["failed_operation"]["recovery_tool"], "browser_submit_form")
            described = await session.call_tool("browser_tools_call", {"name": "browser_submit_form"})
            self.assertIn("inputSchema", json.loads(described.content[0].text))
            arguments = {"selector": "#order", "tab_id": tab_id}
            held = await session.call_tool("browser_tools_call", {
                "name": "browser_submit_form", "arguments": arguments})
            self.assertTrue(held.isError, held)
            token = json.loads(held.content[0].text)["state"]["confirmToken"]
            # Disposable fixture: exercise the non-eliciting client's explicit approval step.
            submitted = await session.call_tool("browser_tools_call", {
                "name": "browser_submit_form", "arguments": {**arguments, "confirm_token": token}})
            self.assertFalse(submitted.isError, submitted)
            done = await session.call_tool("browser_run_python", {
                "code": "tab.wait_for(title='Done'); result = tab.info()['url']"})
            self.assertFalse(done.isError, done)
            self.assertIn("name=Keep+this+draft", json.loads(done.content[0].text)["result"])
        try:
            self.run_scenario(run)
        finally:
            self.call("/api/set-policy", {"ask": False})
