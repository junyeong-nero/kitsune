"""#232: tools and container commands that name no tab act on the current tab, not the user's."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge
from test_resume import NOT_FOUND, ResumeTests

# MCP calls with the bridge mocked: each request's endpoint and tabId. Tab 42 is the agent's new
# tab and 7 the user's active tab.
CALLS = r'''import asyncio, json
from unittest.mock import patch
from kitsune import playwright_tools, server
sent = []

def post(endpoint, body, timeout=15):
    sent.append([endpoint, body.get("tabId")])
    return {"ok": True, "tabId": 42} if endpoint == "/api/new-tab" else {"ok": True}

def get(endpoint):
    sent.append([endpoint, None])
    return {"tabs": [{"id": 7, "active": True}, {"id": 42}]} if endpoint == "/api/tabs" else {"ok": True}

async def main():
    steps = {}
    async def step(label, name, arguments):
        start = len(sent)
        await server.mcp.call_tool(name, arguments)
        steps[label] = [entry for entry in sent[start:] if entry[0] != "/api/tabs"]
    with patch.object(server, "_post", side_effect=post), patch.object(server, "_get", side_effect=get):
        await step("before", "browser_scroll", {"direction": "down"})
        await step("new", "browser_tabs", {"action": "new"})
        await step("scroll", "browser_scroll", {"direction": "down"})
        await step("page_text", "browser_page_text", {})
        await step("explicit", "browser_page_text", {"tab_id": 7})
        await step("gateway", "browser_tools_call", {"name": "browser_markdown", "arguments": {}})
        await step("batch", "browser_batch", {"commands": [{"action": "scroll"}]})
        await step("retry", "browser_retry", {"command": {"action": "scroll"}})
        await step("workflow", "browser_workflow", {"name": "w"})
        await step("cookies", "browser_cookies", {"op": "get", "url": "https://example.com"})
        await step("close", "browser_tabs", {"action": "close"})
        await step("after", "browser_scroll", {"direction": "down"})
    schemas = {name: tool.inputSchema["properties"].get("tab_id") for name, tool in (await server.mcp.all_tools()).items()}
    print(json.dumps({"steps": steps, "current": playwright_tools._current_tab,
                      "tab_id": {name: schemas[name] for name in ("browser_scroll", "browser_batch")}}))

asyncio.run(main())
'''


class CurrentTabToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "KITSUNE_CAPS": "all,storage", "KITSUNE_PUBLISH": "core"}
        result = subprocess.run([sys.executable, "-c", CALLS], env=env, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr[-3000:])
        cls.result = json.loads(result.stdout.splitlines()[-1])
        cls.steps = cls.result["steps"]

    def test_omitted_tab_id_is_the_current_tab(self):
        # Before browser_tabs opens or selects a tab, the active tab (no tabId) as before.
        self.assertEqual(self.steps["before"], [["/api/scroll", None]])
        self.assertEqual(self.steps["new"], [["/api/new-tab", None]])
        self.assertEqual(self.steps["scroll"], [["/api/scroll", 42]])
        self.assertEqual(self.steps["page_text"], [["/api/page-text", 42]])
        self.assertEqual(self.steps["explicit"], [["/api/page-text", 7]])
        self.assertEqual(self.steps["gateway"], [["/api/markdown", 42]])
        # Closing the current tab forgets it.
        self.assertEqual(self.steps["close"], [["/api/close-tab", 42]])
        self.assertEqual(self.steps["after"], [["/api/scroll", None]])
        self.assertIsNone(self.result["current"])

    def test_containers_default_to_the_current_tab(self):
        self.assertEqual(self.steps["batch"], [["/api/batch", 42]])
        self.assertEqual(self.steps["retry"], [["/api/retry", 42]])
        self.assertEqual(self.steps["workflow"], [["/api/workflow", 42]])

    def test_tools_without_a_tab_are_unchanged(self):
        self.assertEqual(self.steps["cookies"], [["/api/cookies", None]])

    def test_schemas(self):
        self.assertEqual(self.result["tab_id"]["browser_scroll"]["description"], "Default: current tab.")
        self.assertEqual(self.result["tab_id"]["browser_batch"]["description"],
                         "Tab for commands that name no tabId. Default: current tab.")


class DefaultTabBridgeTests(ResumeTests):
    def tab_of(self, action, mark=0):
        return [params.get("tabId") for sent, params in self.sent[mark:] if sent == action]

    def test_steps_without_a_tab_run_on_the_default_tab(self):
        payload = self.batch(
            {"action": "scroll", "direction": "down"},
            {"action": "pageText", "tabId": None},
            {"action": "newTab", "url": "https://a.test"},
            {"action": "tabs"},
            {"action": "scroll", "direction": "up", "tabId": 9},
            {"action": "retry", "command": {"action": "focusElement", "selector": "#q"}},
            {"action": "sequence", "do": [{"action": "blurElement"}]},
            tabId=5)
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(self.tab_of("scroll"), [5, 9])
        self.assertEqual(self.tab_of("getPageText"), [5])
        self.assertEqual(self.tab_of("newTab"), [None])
        self.assertEqual(self.tab_of("getTabs"), [None])
        self.assertEqual(self.tab_of("focusElement"), [5])
        self.assertEqual(self.tab_of("blurElement"), [5])

    def test_parallel_sequences_still_name_their_tabs(self):
        payload = self.batch({"action": "parallel", "sequences": [[{"action": "scroll", "direction": "down"}]]},
                             tabId=5)
        self.assertIn("tab_required", json.dumps(payload))
        self.assertEqual(self.tab_of("scroll"), [])

    def test_without_a_default_the_active_tab_as_before(self):
        self.batch({"action": "scroll", "direction": "down"})
        self.assertEqual(self.tab_of("scroll"), [None])

    def test_an_invalid_default_tab_dispatches_nothing(self):
        for route, body in ((lambda h: h.handle_batch(), {"commands": [{"action": "scroll"}]}),
                            (lambda h: h.handle_retry(), {"command": {"action": "scroll"}}),
                            (lambda h: h.handle_workflow(), {"name": "w"})):
            status, result = self.respond(route, {**body, "tabId": "abc"})
            self.assertEqual((status, result["code"]), (400, "invalid_tab_id"))
        self.assertEqual(self.sent, [])

    def test_retry_and_workflow_steps(self):
        status, result = self.respond(lambda h: h.handle_retry(), {"command": {"action": "scroll"}, "tabId": 5})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.tab_of("scroll"), [5])
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge, "WORKFLOW_DIR", tmp):
            mark = len(self.sent)
            payload = self.workflow(tmp, "w", [{"action": "scroll"}, {"action": "click", "selector": "#a", "tabId": 3}],
                                    tabId=6)
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(self.tab_of("scroll", mark), [6])
        self.assertEqual(self.tab_of("click", mark), [3])

    def test_resume_keeps_the_original_default_tab(self):
        self.replies["fill"] = [NOT_FOUND]
        failed = self.batch({"action": "scroll"}, {"action": "fill", "selector": "#q", "value": "x"}, tabId=5)
        self.assertEqual(failed["status"], "failed", failed)
        [checkpoint] = bridge._checkpoints.values()
        self.assertEqual(checkpoint["tabId"], 5)
        mark = len(self.sent)
        resumed = self.resume(failed)
        self.assertEqual(resumed["status"], "ok", resumed)
        self.assertEqual(self.tab_of("fill", mark), [5])
        self.assertEqual(self.tab_of("scroll", mark), [], "the ok step is not sent again")


if __name__ == "__main__":
    unittest.main()
