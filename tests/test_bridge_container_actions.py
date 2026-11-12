"""Container steps the bridge runs, and every route open to every client (#230 removed in #525)."""
import inspect
import json
import re
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, playwright_tools, tools
from test_ask_before_act import AskBeforeActTestCase, FakeExtension


def bridge_run_actions() -> set[str]:
    """Container actions the bridge runs itself rather than forwarding (_execute_command)."""
    direct = set(re.findall(r"action == '(\w+)'", inspect.getsource(bridge._execute_command)))
    return direct | set(bridge._CONTROL_FLOW_HANDLERS)


class ListExtension(FakeExtension):
    """An inactive tab 7 whose list loads one more item after each scroll to the bottom."""

    def __init__(self, owned):
        super().__init__()
        self.owned = owned
        self.items = ["#1"]
        self.scrolls = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        if action == "getTabs":
            return {"tabs": [{"id": 7, "url": "https://user.test/", "active": False, "owned": self.owned}],
                    "ownershipByClient": True}
        if action == "getSnapshot":
            return {"snapshot": "\n".join(["- list [ref=e1]", *(f'  - listitem "{item}"' for item in self.items)])}
        if action == "scroll":
            self.scrolls.append({key: params[key] for key in ("direction", "amount", "loadMore", "tabId")
                                 if key in params})
            if params["direction"] == "bottom":
                self.items.append(f"#{len(self.items) + 1}")
            return {"ok": True, "scrolled_by": 400, "target": "window"}
        return await super().__call__(action, params, timeout)


class ContainerActionTableTests(unittest.TestCase):
    def test_control_flow_handlers_are_not_shadowed_by_forwarded_actions(self):
        self.assertFalse(set(bridge._CONTROL_FLOW_HANDLERS) & set(bridge.BATCH_ACTION_MAP))
        self.assertTrue(all(inspect.iscoroutinefunction(handler)
                            for handler in bridge._CONTROL_FLOW_HANDLERS.values()))

    def test_container_actions_are_the_actions_the_bridge_runs(self):
        actions = set(bridge.BATCH_ACTION_MAP.values()) | bridge_run_actions()
        self.assertEqual(actions - bridge.CONTAINER_ACTIONS, set(), "a container action the table leaves out")
        self.assertEqual(bridge.CONTAINER_ACTIONS - actions, set(), "a listed action the bridge does not run")

    def test_pre_dispatch_refusals_are_failed_not_unknown(self):
        # #249: nothing was dispatched, so these are not outcomes that may have taken effect.
        self.assertEqual(bridge._result_status(bridge._unknown_action_error("html")), "failed")
        self.assertEqual(bridge._result_status({"error": "URL denied by policy (x): y"}), "failed")
        self.assertEqual(bridge._result_status({"error": "timed out", "code": "timeout", "retryable": False}),
                         "unknown")

    def test_unknown_action_names_the_closest_batch_action(self):
        for action, suggestion in (("html", "getHTML"), ("GETHTML", "getHTML"), ("clik", "click"),
                                   ("snap", "snapshot")):
            with self.subTest(action=action):
                error = bridge._unknown_action_error(action)
                self.assertEqual((error["code"], error["retryable"], error["state"]),
                                 ("unknown_action", False, {"action": action, "suggestion": suggestion}))
                self.assertEqual(error["error"], f"Unknown batch action: {action} (did you mean {suggestion}?)")
                self.assertIn(suggestion, error["hint"])
        error = bridge._unknown_action_error("teleport")
        self.assertEqual((error["error"], error["state"]), ("Unknown batch action: teleport", {"action": "teleport"}))
        # Only names a batch step can run are suggested, not the extension actions aliases forward to.
        self.assertNotIn("executeJS", bridge._BATCH_ACTION_NAMES)
        self.assertTrue({"js", "getHTML", "screenshot", "parallel"} <= set(bridge._BATCH_ACTION_NAMES))


class BridgeOpenRoutesTests(AskBeforeActTestCase):
    ask = False

    def request(self, path, body, method="POST", headers=None):
        """Call a route through BridgeHandler."""
        handler = self.handler(body)
        handler.authorized = Mock(return_value=True)
        handler.path = path
        handler.command = method
        handler.headers = dict(headers or {})
        handler.do_POST() if method == "POST" else handler.do_GET()
        return handler.send_json.call_args.args

    def test_every_route_and_container_step_is_open(self):
        # The capability and mode headers of older clients are ignored.
        for headers in ({}, {"X-Kitsune-Caps": "core", "X-Kitsune-Mode": "read"}):
            with self.subTest(headers=headers):
                self.extension.sent.clear()
                status, result = self.request("/api/cookies", {"op": "get", "url": "https://example.com"},
                                              headers=headers)
                self.assertEqual(status, 200, result)
                status, result = self.request("/api/batch", {"commands": [
                    {"action": "clipboard", "op": "read"},
                    {"action": "scroll", "tabId": 7, "direction": "down"}]}, headers=headers)
                self.assertEqual((status, result["results"]), (200, [{"ok": True}, {"ok": True}]))
                self.assertEqual(self.extension.actions(), ["cookies", "clipboard", "scroll"])

    def test_unknown_batch_actions_are_refused_with_the_closest_name(self):
        self.addCleanup(bridge._drop_all_checkpoints)
        status, result = self.request("/api/batch", {"commands": [
            {"action": "html", "selector": "#item"},
            {"action": "parallel", "sequences": [[{"action": "teleport", "tabId": 7}]]}]})
        self.assertEqual(status, 200)
        html = result["results"][0]
        self.assertEqual((html["code"], html["retryable"], html["state"]),
                         ("unknown_action", False, {"action": "html", "suggestion": "getHTML"}))
        self.assertIn('"Unknown batch action: teleport"', json.dumps(result["results"][1]))
        self.assertEqual([record["status"] for record in result["progress"]], ["failed", "failed"])
        self.assertNotIn('"unknown"', json.dumps(result["progress"]))
        self.assertEqual(self.extension.sent, [])

    def test_browser_find_load_more_through_the_bridge(self):
        # #447: browser_find's load_more scrolls a tab this client owns and a background tab it can
        # only read (#449: scrolled back afterwards).
        self.addCleanup(setattr, playwright_tools, "_current_tab", playwright_tools._current_tab)
        playwright_tools._current_tab = 7

        def post(endpoint, data, timeout=15, start_bridge=True):
            return self.request(endpoint, data)[1]

        for owned in (True, False):
            with self.subTest(owned=owned):
                extension = ListExtension(owned)
                with patch.object(bridge, "_send_to_extension_raw", new=extension), \
                        patch.object(tools, "_post", side_effect=post), \
                        patch.object(tools, "_get", side_effect=lambda endpoint: self.request(
                            endpoint, {}, method="GET")[1]), \
                        patch.object(tools, "_sleep"):
                    found = playwright_tools.browser_find(text="#3", load_more=True)
                self.assertEqual((found.get("count"), found.get("scrolls")), (1, 2), found)
                bottom = {"direction": "bottom", "loadMore": True, "tabId": 7}
                back = [{"direction": "up", "amount": 800, "loadMore": True, "tabId": 7}]
                self.assertEqual(extension.scrolls, [bottom, bottom, *([] if owned else back)])


if __name__ == "__main__":
    unittest.main()
