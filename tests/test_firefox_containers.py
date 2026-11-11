"""#495: browser_tabs new and batch/workflow newTab open a tab in a Firefox container.

container is the container's cookieStoreId; the tab list shows it on each tab that is in a
container. A tab opened without container is in the default store, whatever the current tab.
Batch, parallel and workflow calls are the bridge's containers; a Firefox container is always
named "container" here, as in the tool's parameter.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kitsune import bridge, playwright_tools, script_runner, tools
from test_container_progress import ContainerTests
from test_python_api import BridgeTestCase
from test_script_runner import Client

PERSONAL = "firefox-container-1"
UNAVAILABLE = {"error": "This Kitsune build cannot resolve container names; no tab was opened",
               "code": "container_unavailable", "retryable": True, "hint": "Pass a cookieStoreId ...",
               "state": {"container": "Personal", "reason": "names_unsupported"}}


class CoreTabContainerTests(unittest.TestCase):
    def setUp(self):
        playwright_tools._current_tab = None
        # As GET /api/tabs lists them: only a tab in a container has cookieStoreId.
        self.open = [{"id": 3, "title": "A", "url": "https://a/", "active": True},
                     {"id": 8, "title": "B", "url": "https://b/", "owned": True, "cookieStoreId": PERSONAL}]
        self.reply = None
        for patcher in (patch.object(tools.client, "user_tabs", "read-only"),
                        patch.object(tools, "_post", side_effect=self.bridge),
                        patch.object(tools, "_get", side_effect=lambda endpoint: {"tabs": list(self.open)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, playwright_tools, "_current_tab", None)
        self.posted = []

    def bridge(self, endpoint, data, timeout=15):
        self.posted.append((endpoint, dict(data)))
        if self.reply is not None:
            return self.reply
        store = {"cookieStoreId": data["container"]} if "container" in data else {}
        self.open.append({"id": 11, "title": "D", "url": data["url"], "owned": True, **store})
        return {"ok": True, "tabId": 11, **({"url": data["url"], "status": "complete"} if data.get("wait") else {}),
                **store}

    def test_new_sends_the_container_and_returns_the_tab_in_it(self):
        # The bridge's cookieStoreId is left out like its tabId: the tab names its container.
        self.assertEqual(playwright_tools.browser_tabs("new", url="https://d/", container=PERSONAL), {
            "ok": True, "url": "https://d/", "status": "complete", "index": 2,
            "tab": {"index": 2, "title": "D", "url": "https://d/", "current": True, "owned": True,
                    "cookieStoreId": PERSONAL},
            "tab_count": 3})
        self.assertEqual(self.posted, [("/api/new-tab", {"url": "https://d/", "wait": True, "pageText": True,
                                                         "timeout": playwright_tools.NEW_TAB_TIMEOUT_MS,
                                                         "container": PERSONAL})])

    def test_new_without_a_url_sends_the_container_too(self):
        result = playwright_tools.browser_tabs("new", container=PERSONAL)
        self.assertEqual(result["tab"]["cookieStoreId"], PERSONAL)
        self.assertEqual(self.posted, [("/api/new-tab", {"url": "about:blank", "container": PERSONAL})])

    def test_new_without_container_does_not_inherit_the_current_tabs(self):
        # Tab 8 is the agent's own, in a container, and current: the new tab still names no container,
        # so the extension opens it in the default store.
        playwright_tools._current_tab = 8
        for arguments in ({"url": "https://d/"}, {}, {"url": "https://d/", "container": None},
                          {"container": ""}):
            with self.subTest(arguments=arguments):
                self.posted.clear()
                result = playwright_tools.browser_tabs("new", **arguments)
                [(endpoint, body)] = self.posted
                self.assertEqual(endpoint, "/api/new-tab")
                self.assertNotIn("container", body)
                self.assertNotIn("cookieStoreId", result["tab"])
                self.open.pop()
                playwright_tools._current_tab = 8

    def test_list_names_the_container_only_of_tabs_in_one(self):
        self.assertEqual(playwright_tools.browser_tabs("list"), {"tabs": [
            {"index": 0, "title": "A", "url": "https://a/", "active": True},
            {"index": 1, "title": "B", "url": "https://b/", "owned": True, "cookieStoreId": PERSONAL}]})
        self.assertEqual(playwright_tools.browser_tabs("select", index=1)["tab"]["cookieStoreId"], PERSONAL)
        # Kitsune's own list keeps the extension's field as it is.
        self.assertEqual([tab.get("cookieStoreId") for tab in tools.browser_tabs("list")["tabs"]], [None, PERSONAL])

    def test_a_container_that_cannot_be_used_opens_nothing_and_keeps_the_current_tab(self):
        playwright_tools._current_tab = 8
        self.reply = UNAVAILABLE
        for arguments in ({"url": "https://d/"}, {}):
            with self.subTest(arguments=arguments):
                self.assertEqual(playwright_tools.browser_tabs("new", container="Personal", **arguments), UNAVAILABLE)
                self.assertEqual(playwright_tools._current_tab, 8)
        self.assertEqual([body["container"] for _, body in self.posted], ["Personal", "Personal"])

    def test_kitsune_browser_tabs_new_sends_the_container(self):
        tools.browser_tabs("new", url="https://d/", container=PERSONAL)
        tools.browser_tabs("new", url="https://d/", container=PERSONAL, wait=True, timeout=500)
        tools.browser_tabs("new", url="https://d/")
        self.assertEqual([body for _, body in self.posted], [
            {"url": "https://d/", "container": PERSONAL},
            {"url": "https://d/", "container": PERSONAL, "wait": True, "timeout": 500},
            {"url": "https://d/"}])

    def test_an_old_extension_error_keeps_the_current_tab_and_created_tab_id(self):
        playwright_tools._current_tab = 8
        self.reply = bridge._container_ignored("newTab", {"container": PERSONAL}, {"ok": True, "tabId": 11})
        for arguments in ({"url": "https://d/"}, {}):
            with self.subTest(arguments=arguments):
                result = playwright_tools.browser_tabs("new", container=PERSONAL, **arguments)
                self.assertEqual(result, self.reply)
                self.assertEqual(result["state"]["tabId"], 11)
                self.assertEqual(playwright_tools._current_tab, 8)

    def test_core_schema_publishes_container(self):
        properties = playwright_tools.SCHEMAS["browser_tabs"]["inputSchema"]["properties"]
        self.assertEqual(properties["container"], {"type": "string", "description": "new: cookieStoreId."})


class PythonNewTabContainerTests(BridgeTestCase):
    def test_new_tab_sends_the_container_and_tabs_keep_cookie_store_id(self):
        self.browser.new_tab("https://d/", container=PERSONAL)
        self.assertEqual(self.bridge.last.body, {"url": "https://d/", "container": PERSONAL,
                                                 "wait": True, "timeout": 15000})
        self.browser.new_tab(wait=False, container=PERSONAL)
        self.assertEqual(self.bridge.last.body, {"url": "about:blank", "container": PERSONAL})
        self.browser.new_tab("https://d/", wait=False)
        self.assertEqual(self.bridge.last.body, {"url": "https://d/"})
        self.reply("/api/tabs", {"tabs": [{"id": 3}, {"id": 8, "cookieStoreId": PERSONAL}]})
        self.assertEqual([tab.get("cookieStoreId") for tab in self.browser.tabs()], [None, PERSONAL])


class ScriptNewTabContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_script_opens_a_tab_in_the_container_of_a_listed_tab(self):
        # browser_run_python: the tab dicts name the container, and new_tab forwards it to the host's client.
        client = Client({"/api/tabs": {"tabs": [{"id": 8, "cookieStoreId": PERSONAL}]},
                         "/api/new-tab": {"ok": True, "tabId": 11, "cookieStoreId": PERSONAL}})
        code = ("store = browser.tabs()[0]['cookieStoreId']\n"
                "result = browser.new_tab('https://d/', container=store).id")
        result = await script_runner.run_python(code, client)
        self.assertEqual(result.get("result"), 11, result)
        self.assertEqual(client.calls[1][:2], ("POST", "/api/new-tab"))
        self.assertEqual(client.calls[1][2]["json"], {"url": "https://d/", "container": PERSONAL,
                                                      "wait": True, "timeout": 15000})


class BridgeNewTabContainerTests(ContainerTests):
    def setUp(self):
        super().setUp()
        self.replies["newTab"] = self.new_tab
        self.ignores_container = False  # an extension older than container
        self.opened = 0

    async def new_tab(self, params):
        self.opened += 1
        store = {} if self.ignores_container or "container" not in params else {"cookieStoreId": params["container"]}
        return {"ok": True, "tabId": 40 + self.opened, **store}

    def test_new_tab_route_and_batch_steps_forward_the_container(self):
        opened = self.call(lambda h: h.handle_new_tab(), {"url": "https://a.test/", "container": PERSONAL})
        self.assertEqual(opened, {"ok": True, "tabId": 41, "cookieStoreId": PERSONAL})
        payload = self.batch({"action": "newTab", "url": "https://b.test/", "container": PERSONAL},
                             {"action": "newTab", "url": "https://c.test/"})
        self.assertEqual(payload["results"], [{"ok": True, "tabId": 42, "cookieStoreId": PERSONAL},
                                              {"ok": True, "tabId": 43}])
        self.assertEqual(self.sent, [("newTab", {"url": "https://a.test/", "container": PERSONAL}),
                                     ("newTab", {"url": "https://b.test/", "container": PERSONAL}),
                                     ("newTab", {"url": "https://c.test/"})])

    def test_workflow_new_tab_forwards_the_container(self):
        step = {"action": "newTab", "url": "https://a.test/", "container": "{{container}}"}
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "fixture.md"), "w", encoding="utf-8") as f:
                f.write("```json\n" + json.dumps(step) + "\n```\n")
            with patch.object(bridge, "WORKFLOW_DIR", tmp):
                payload = self.call(lambda h: h.handle_workflow(),
                                    {"name": "fixture", "variables": {"container": PERSONAL}})
        self.assertEqual((payload["status"], payload["newTabs"]), ("ok", [41]))
        self.assertEqual(self.sent, [("newTab", {"url": "https://a.test/", "container": PERSONAL})])

    def test_an_extension_that_ignores_the_container_is_reported_not_passed_off_as_the_container(self):
        # An extension older than container opens the tab in the default store and answers as usual.
        self.ignores_container = True
        expected = {
            "error": "The extension opened the tab without its container; it is older than this bridge",
            "code": "extension_version_mismatch", "retryable": False,
            "hint": "Update and reload the Kitsune extension. The tab (state.tabId) is open in the default store, "
                    "not in the container; close it.",
            "state": {"reason": "container_unsupported", "tabId": 41, "container": PERSONAL}}
        owner = bridge._owner_id.set("owner-495")
        try:
            with patch.object(bridge, "_tab_openers", {}) as openers:
                opened = self.call(lambda h: h.handle_new_tab(), {"url": "https://a.test/", "container": PERSONAL})
                self.assertEqual({tab: opener["owner"] for tab, opener in openers.items()}, {41: "owner-495"},
                                 "the tab is still its client's to close")
        finally:
            bridge._owner_id.reset(owner)
        self.assertEqual(opened, expected)
        payload = self.batch({"action": "newTab", "url": "https://a.test/", "container": PERSONAL, "wait": True},
                             {"action": "newTab", "url": "https://a.test/"},
                             {"action": "newTab", "url": "https://a.test/", "container": ""})
        self.assertEqual(payload["results"], [{**expected, "state": {**expected["state"], "tabId": 42}},
                                              {"ok": True, "tabId": 43}, {"ok": True, "tabId": 44}])
        # The tab exists, so the step may have taken effect; it is not among the tabs the call opened on purpose.
        self.assertEqual(self.statuses(payload["progress"]), ["unknown", "ok", "ok"])
        self.assertEqual(payload["newTabs"], [43, 44])

    def test_container_echo_preserves_load_timeout_and_stall_results(self):
        for extra in ({"timedOut": True}, {"timedOut": True, "code": "new_tab_stalled", "retryable": False}):
            with self.subTest(extra=extra):
                reply = {"ok": False, "tabId": 41, "cookieStoreId": PERSONAL, **extra}
                self.replies["newTab"] = [reply]
                self.assertEqual(self.call(lambda h: h.handle_new_tab(),
                                           {"url": "https://a.test/", "container": PERSONAL, "wait": True}), reply)

    def test_old_extension_load_timeout_also_reports_the_created_tab(self):
        self.replies["newTab"] = [{"ok": False, "tabId": 41, "timedOut": True}]
        result = self.call(lambda h: h.handle_new_tab(),
                           {"url": "https://a.test/", "container": PERSONAL, "wait": True})
        self.assertEqual(result["code"], "extension_version_mismatch")
        self.assertEqual(result["state"]["tabId"], 41)
        self.assertFalse(result["retryable"])
        self.assertEqual(len(self.sent), 1, "creation is never retried in the default store")

    def test_a_refused_container_is_a_failed_step_that_is_safe_to_repeat(self):
        self.replies["newTab"] = [dict(UNAVAILABLE)]
        payload = self.batch({"action": "newTab", "url": "https://a.test/", "container": "Personal"},
                             {"action": "pageText", "tabId": 4})
        self.assertEqual(payload["results"][0]["code"], "container_unavailable")
        self.assertEqual(self.statuses(payload["progress"]), ["failed", "ok"])
        self.assertNotIn("newTabs", payload)

    def test_an_approval_of_one_container_does_not_cover_another(self):
        binding = lambda container: bridge._confirmation_binding(  # noqa: E731
            "newTab", {"url": "https://pay.example/", **({"container": container} if container else {})}, None)
        self.assertEqual(len({binding(None), binding(PERSONAL), binding("firefox-container-2")}), 3)


if __name__ == "__main__":
    unittest.main()
