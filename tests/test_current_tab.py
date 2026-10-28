"""#232: tools and container commands that name no tab act on the current tab, not the user's.

#303: without a current tab, writes that name no tab are refused instead of writing the active tab.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, playwright_tools, tools
from test_resume import NOT_FOUND, ResumeTests

# MCP calls with the bridge mocked: each request's endpoint and tabId, and each result's code.
# Tab 42 is the agent's new tab and 7 the user's active tab.
CALLS = r'''import asyncio, json
from unittest.mock import patch
from kitsune import playwright_tools, server, tools
sent = []

def post(endpoint, body, timeout=15):
    sent.append([endpoint, body.get("tabId")])
    return {"ok": True, "tabId": 42} if endpoint == "/api/new-tab" else {"ok": True}

def get(endpoint):
    sent.append([endpoint, None])
    return {"tabs": [{"id": 7, "active": True}, {"id": 42}]} if endpoint == "/api/tabs" else {"ok": True}

async def main():
    steps, results = {}, {}
    async def step(label, name, arguments):
        start = len(sent)
        result = await server.mcp.call_tool(name, arguments)
        steps[label] = [entry for entry in sent[start:] if entry[0] != "/api/tabs"]
        results[label] = json.loads(result.content[0].text)
    with patch.object(tools, "_post", side_effect=post), patch.object(tools, "_get", side_effect=get):
        # No current tab: writes are refused, reads go to the active tab (#303).
        await step("before", "browser_scroll", {"direction": "down"})
        await step("navigate_before", "browser_navigate", {"url": "https://a.test/"})
        await step("back_before", "browser_navigate_back", {})
        await step("click_before", "browser_click", {"target": "e1"})
        await step("storage_set_before", "browser_storage", {"kind": "local", "op": "set", "key": "k", "value": "v"})
        await step("gateway_before", "browser_tools_call", {"name": "browser_scroll", "arguments": {}})
        await step("batch_before", "browser_batch", {"commands": [
            {"action": "pageText"}, {"action": "navigate", "url": "https://a.test/"}]})
        await step("nested_before", "browser_batch", {"commands": [
            {"action": "sequence", "do": [{"action": "click", "selector": "#a"}]}]})
        await step("retry_before", "browser_retry", {"command": {"action": "scroll"}})
        await step("workflow_before", "browser_workflow", {"name": "w"})
        await step("page_text_before", "browser_page_text", {})
        await step("snapshot_before", "browser_snapshot", {})
        await step("storage_get_before", "browser_storage", {"kind": "local", "op": "get", "key": "k"})
        await step("batch_reads_before", "browser_batch", {"commands": [
            {"action": "pageText"}, {"action": "newTab", "url": "https://a.test/"},
            {"action": "navigate", "url": "https://b.test/", "tabId": "${$1.tabId}"}]})
        await step("navigate_explicit", "browser_navigate", {"url": "https://a.test/", "tab_id": 7})
        await step("back_explicit", "browser_navigate_back", {"tab_id": 7})
        await step("scroll_explicit", "browser_scroll", {"direction": "down", "tab_id": 7})
        await step("batch_explicit", "browser_batch", {"commands": [{"action": "scroll"}], "tab_id": 7})
        # browser_tabs new makes the agent's tab current.
        await step("new", "browser_tabs", {"action": "new"})
        await step("scroll", "browser_scroll", {"direction": "down"})
        await step("navigate", "browser_navigate", {"url": "https://a.test/"})
        await step("navigate_other", "browser_navigate", {"url": "https://a.test/", "tab_id": 7})
        await step("page_text", "browser_page_text", {})
        await step("explicit", "browser_page_text", {"tab_id": 7})
        await step("gateway", "browser_tools_call", {"name": "browser_markdown", "arguments": {}})
        await step("batch", "browser_batch", {"commands": [{"action": "scroll"}]})
        await step("retry", "browser_retry", {"command": {"action": "scroll"}})
        await step("workflow", "browser_workflow", {"name": "w"})
        await step("cookies", "browser_cookies", {"op": "get", "url": "https://example.com"})
        await step("close", "browser_tabs", {"action": "close"})
        await step("after", "browser_scroll", {"direction": "down"})
        # A restarted MCP server forgets the tab browser_tabs new made current.
        await step("new_again", "browser_tabs", {"action": "new"})
        playwright_tools._current_tab = None
        await step("restarted", "browser_navigate", {"url": "https://a.test/"})
    schemas = {name: tool.inputSchema["properties"].get("tab_id") for name, tool in (await server.mcp.all_tools()).items()}
    print(json.dumps({"steps": steps, "results": results, "current": playwright_tools._current_tab,
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
        cls.results = cls.result["results"]

    def assertRefused(self, label, tool, action=None):
        result = self.results[label]
        self.assertEqual(self.steps[label], [], "nothing is sent, so the user's active tab is not written")
        self.assertEqual((result["code"], result["retryable"]), ("no_current_tab", False), result)
        self.assertEqual(result["state"], {"tool": tool, **({"action": action} if action else {})})
        self.assertIn("pass tab_id", result["hint"].lower())
        self.assertIn("browser_tabs action=new", result["hint"])

    def test_omitted_tab_id_is_the_current_tab(self):
        self.assertEqual(self.steps["new"], [["/api/new-tab", None]])
        self.assertEqual(self.steps["scroll"], [["/api/scroll", 42]])
        self.assertEqual(self.steps["navigate"], [["/api/navigate", 42]])
        self.assertEqual(self.steps["navigate_other"], [["/api/navigate", 7]])
        self.assertEqual(self.steps["page_text"], [["/api/page-text", 42]])
        self.assertEqual(self.steps["explicit"], [["/api/page-text", 7]])
        self.assertEqual(self.steps["gateway"], [["/api/markdown", 42]])
        # Closing the current tab forgets it.
        self.assertEqual(self.steps["close"], [["/api/close-tab", 42]])
        self.assertRefused("after", "browser_scroll")

    def test_writes_without_a_tab_are_refused(self):
        self.assertRefused("before", "browser_scroll")
        self.assertRefused("navigate_before", "browser_navigate")
        self.assertRefused("back_before", "browser_navigate_back")
        self.assertRefused("click_before", "browser_click")
        self.assertRefused("storage_set_before", "browser_storage", "storageOp")
        self.assertRefused("gateway_before", "browser_scroll")
        self.assertIn("browser_navigate and browser_navigate_back accept it", self.results["navigate_before"]["hint"])

    def test_container_steps_that_write_without_a_tab_are_refused(self):
        self.assertRefused("batch_before", "browser_batch", "navigate")
        self.assertIn('step "navigate"', self.results["batch_before"]["error"])
        self.assertRefused("nested_before", "browser_batch", "click")
        self.assertRefused("retry_before", "browser_retry", "scroll")

    def test_workflows_need_a_tab_because_their_steps_are_not_inspected(self):
        self.assertRefused("workflow_before", "browser_workflow")
        self.assertIn("cannot be inspected", self.results["workflow_before"]["error"])
        self.assertIn("even if it only reads", self.results["workflow_before"]["error"])

    def test_reads_without_a_tab_use_the_active_tab(self):
        self.assertEqual(self.steps["page_text_before"], [["/api/page-text", None]])
        self.assertEqual(self.steps["snapshot_before"], [["/api/snapshot", None]])
        self.assertEqual(self.steps["storage_get_before"], [["/api/storage", None]])
        # Reads, newTab, and writes that name their tab.
        self.assertEqual(self.steps["batch_reads_before"], [["/api/batch", None]])

    def test_an_explicit_tab_id_works_without_a_current_tab(self):
        self.assertEqual(self.steps["navigate_explicit"], [["/api/navigate", 7]])
        self.assertEqual(self.steps["back_explicit"], [["/api/back", 7]])
        self.assertEqual(self.steps["scroll_explicit"], [["/api/scroll", 7]])
        self.assertEqual(self.steps["batch_explicit"], [["/api/batch", 7]])

    def test_a_restarted_server_does_not_navigate_the_active_tab(self):
        self.assertEqual(self.steps["new_again"], [["/api/new-tab", None]])
        self.assertRefused("restarted", "browser_navigate")
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


class TabWriteClassificationTests(unittest.TestCase):
    def test_steps_that_write_a_tab_are_the_bridges(self):
        # Every name a container step may use, by the action it runs.
        names = set(bridge.BATCH_ACTION_MAP) | set(bridge.ACTION_GROUPS)
        writes = {name for name in names if bridge.BATCH_ACTION_MAP.get(name, name) not in bridge._TABLESS_ACTIONS
                  and bridge._claims_tab(bridge.BATCH_ACTION_MAP.get(name, name))}
        self.assertEqual(tools.TAB_WRITE_STEPS, writes)

    def test_tab_write_tools_are_registered_writes(self):
        for name in tools.TAB_WRITE_TOOLS:
            with self.subTest(name=name):
                spec = tools.TOOLS[name]
                annotations = spec.annotations or playwright_tools.SCHEMAS[name]["annotations"]
                self.assertFalse(annotations.get("readOnlyHint"))


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
