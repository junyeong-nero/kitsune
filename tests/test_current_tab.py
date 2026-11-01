"""#232: tools and container commands that name no tab act on the current tab, not the user's.

#303: without a current tab, writes that name no tab are refused instead of writing the active tab.
Tabs the agent opens in a batch or workflow, or by an action on its current tab, become current.
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

def post(endpoint, body, timeout=15, start_bridge=True):
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
        # New comes first; select is only useful for an owned tab under the default policy.
        self.assertLess(result["hint"].index("action=new"), result["hint"].index("action=select"))
        self.assertIn("read-only", result["hint"])

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


# MCP calls whose results open tabs (#303): the bridge reply per endpoint is set before each call.
ADOPT_CALLS = r'''import asyncio, json
from unittest.mock import patch
from kitsune import playwright_tools, server, tools
sent, reply = [], {}

def post(endpoint, body, timeout=15):
    sent.append([endpoint, body.get("tabId")])
    return reply.get(endpoint, {"ok": True})

async def main():
    steps, results, current = {}, {}, {}
    async def step(label, name, arguments, replies=None):
        reply.clear()
        reply.update(replies or {})
        start = len(sent)
        result = await server.mcp.call_tool(name, arguments)
        steps[label] = sent[start:]
        results[label] = json.loads(result.content[0].text)
        current[label] = playwright_tools._current_tab
    def opened(*tabs):
        result = {"ok": True, "opened_tabs": [{"tabId": tab, "url": "https://o.test/"} for tab in tabs]}
        return {"/api/click": result, "/api/check": result}
    with patch.object(tools, "_post", side_effect=post), patch.object(tools, "_get", return_value={"tabs": []}):
        # Without a current tab, the last tab a batch or workflow opened and kept becomes current.
        await step("batch_new", "browser_batch", {"commands": [{"action": "newTab"}, {"action": "newTab"}]},
                   {"/api/batch": {"results": [{"ok": True, "tabId": 50}, {"ok": True, "tabId": 51}],
                                   "newTabs": [50, 51]}})
        await step("scroll_new", "browser_scroll", {"direction": "down"})
        # With one, a batch's new tab still becomes current, even when the batch runs on another tab.
        await step("batch_other", "browser_batch", {"commands": [{"action": "newTab"}], "tab_id": 7},
                   {"/api/batch": {"results": [{"ok": True, "tabId": 52}], "newTabs": [52]}})
        await step("batch_none", "browser_batch", {"commands": [{"action": "scroll"}]})
        await step("workflow_new", "browser_workflow", {"name": "w"},
                   {"/api/workflow": {"workflow": "w", "results": [], "newTabs": [53]}})
        # An action on the current tab: a core click and Kitsune's own check, a link_fallback tab included.
        await step("core_click", "browser_click", {"target": "e1"}, opened(60))
        await step("click_current", "browser_check", {"selector": "#a", "checked": True, "tab_id": 60},
                   {"/api/check": {"ok": True, "opened_tabs": [
                       {"tabId": 61, "url": "https://o.test/"},
                       {"tabId": 62, "url": "https://o.test/", "via": "link_fallback"}]}})
        await step("click_default", "browser_check", {"selector": "#a", "checked": True}, opened(63))
        # A click on another tab the agent names leaves the current tab alone.
        await step("click_other", "browser_check", {"selector": "#a", "checked": True, "tab_id": 7}, opened(64))
        await step("click_no_tab", "browser_check", {"selector": "#a", "checked": True},
                   {"/api/check": {"ok": True, "opened_tabs": []}})
        await step("scroll_after", "browser_scroll", {"direction": "down"})
        # Without a current tab, a click on a named tab hands over the tab it opened.
        playwright_tools._current_tab = None
        await step("click_restarted", "browser_check", {"selector": "#a", "checked": True, "tab_id": 7}, opened(65))
    print(json.dumps({"steps": steps, "results": results, "current": current}))

asyncio.run(main())
'''


class AdoptedTabToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "KITSUNE_CAPS": "all", "KITSUNE_PUBLISH": "core"}
        result = subprocess.run([sys.executable, "-c", ADOPT_CALLS], env=env, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr[-3000:])
        output = json.loads(result.stdout.splitlines()[-1])
        cls.steps, cls.results, cls.current = output["steps"], output["results"], output["current"]

    def test_a_tab_a_batch_or_workflow_opens_becomes_current(self):
        self.assertEqual(self.steps["batch_new"], [["/api/batch", None]])
        self.assertEqual((self.current["batch_new"], self.results["batch_new"]["current_tab"]), (51, 51))
        self.assertEqual(self.steps["scroll_new"], [["/api/scroll", 51]])
        self.assertEqual(self.steps["batch_other"], [["/api/batch", 7]])
        self.assertEqual((self.current["batch_other"], self.results["batch_other"]["current_tab"]), (52, 52))
        self.assertEqual(self.current["batch_none"], 52)
        self.assertNotIn("current_tab", self.results["batch_none"])
        self.assertEqual(self.steps["workflow_new"], [["/api/workflow", 52]])
        self.assertEqual((self.current["workflow_new"], self.results["workflow_new"]["current_tab"]), (53, 53))

    def test_a_tab_a_click_on_the_current_tab_opens_becomes_current(self):
        self.assertEqual(self.steps["core_click"], [["/api/click", 53]])
        self.assertEqual((self.current["core_click"], self.results["core_click"]["current_tab"]), (60, 60))
        # The last tab it opened; a link_fallback tab counts like any other.
        self.assertEqual((self.current["click_current"], self.results["click_current"]["current_tab"]), (62, 62))
        self.assertEqual(self.steps["click_default"], [["/api/check", 62]])
        self.assertEqual(self.current["click_default"], 63)

    def test_a_click_on_another_named_tab_keeps_the_current_tab(self):
        self.assertEqual(self.steps["click_other"], [["/api/check", 7]])
        self.assertEqual(self.current["click_other"], 63)
        self.assertNotIn("current_tab", self.results["click_other"])
        self.assertEqual(self.results["click_other"]["opened_tabs"][0]["tabId"], 64)
        self.assertEqual(self.current["click_no_tab"], 63)
        self.assertEqual(self.steps["scroll_after"], [["/api/scroll", 63]])

    def test_without_a_current_tab_a_click_on_a_named_tab_hands_over_its_new_tab(self):
        self.assertEqual(self.steps["click_restarted"], [["/api/check", 7]])
        self.assertEqual((self.current["click_restarted"], self.results["click_restarted"]["current_tab"]), (65, 65))


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


class NewTabsBridgeTests(ResumeTests):
    def setUp(self):
        super().setUp()
        self.next_tab = iter(range(50, 60))

        async def new_tab(params):
            return {"ok": True, "tabId": next(self.next_tab)}
        self.replies["newTab"] = new_tab

    def test_batch_reports_the_tabs_its_new_tab_steps_opened_and_kept(self):
        payload = self.batch(
            {"action": "newTab", "url": "https://a.test"},
            {"action": "sequence", "do": [{"action": "newTab"}, {"action": "newTab"}]},
            {"action": "closeTab", "tabId": 51},
            {"action": "closeTab", "tabId": 3})
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(payload["newTabs"], [50, 52])

    def test_a_tab_closed_by_its_substituted_id_is_not_reported(self):
        payload = self.batch(
            {"action": "sequence", "do": [{"action": "newTab"}, {"action": "closeTab", "tabId": "${$0.tabId}"}]},
            {"action": "newTab"}, {"action": "closeTab", "tabId": "51"})
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual([params.get("tabId") for action, params in self.sent if action == "closeTab"], [50, 51])
        self.assertNotIn("newTabs", payload)

    def test_failed_new_tabs_and_other_containers_report_none(self):
        self.replies["newTab"] = [{"error": "Invalid URL"}]
        payload = self.batch({"action": "newTab", "url": "x"}, {"action": "scroll", "tabId": 3})
        self.assertNotIn("newTabs", payload)
        status, result = self.respond(lambda h: h.handle_retry(), {"command": {"action": "newTab"}})
        self.assertEqual(status, 200, result)
        self.assertNotIn("newTabs", result)

    def test_workflow_reports_its_new_tabs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge, "WORKFLOW_DIR", tmp):
            payload = self.workflow(tmp, "w", [{"action": "newTab"}, {"action": "scroll"}])
        self.assertEqual(payload["status"], "ok", payload)
        self.assertEqual(payload["newTabs"], [50])


if __name__ == "__main__":
    unittest.main()
