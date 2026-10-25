"""#230: the bridge enforces each client's capability groups on routes and container steps."""
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, capabilities, task_modes
from kitsune.capabilities import ACTION_GROUPS, CAPS_HEADER, OPEN_ROUTES, ROUTE_GROUPS
from test_ask_before_act import AskBeforeActTestCase

# Every tool's bridge routes with the bridge mocked: {tool: [group, [routes]]}. Tools with several
# request shapes are called once per shape.
TOOL_ROUTES = r'''import inspect, json
from unittest.mock import patch
from kitsune import playwright_tools, server, tools
# browser_fill_form gets one field of every type its schema offers (#236).
FIELD_TYPES = playwright_tools.SCHEMAS["browser_fill_form"]["inputSchema"]["properties"]["fields"]["items"][
    "properties"]["type"]["enum"]
SAMPLES = {"fields": [{"target": "e1", "name": kind, "type": kind, "value": "true"} for kind in FIELD_TYPES],
           "url": "https://example.com", "values": ["example"], "index": 1, "commands": [], "sequences": [],
           "tab_ids": [1], "tab_id": 1, "names": ["x"], "paths": ["/tmp/x"], "command": {"action": "sleep"}}
SHAPES = {"browser_tabs": [{"action": action} for action in ("list", "new", "close", "select")],
          "browser_take_screenshot": [{}, {"fullPage": True}, {"target": "e1"}],
          "browser_wait_for": [{"text": "x"}, {"textGone": "x"}, {"time": 0.01}],
          "browser_click": [{"target": "e1"}, {"target": "e1", "doubleClick": True}],
          "browser_type": [{"target": "e1", "text": "x"}, {"target": "e1", "text": "x", "slowly": True}],
          "browser_highlight": [{"selector": "a"}, {"selector": "a", "clear": True}]}
REPLY = {"ok": True, "tabs": [{"id": 1, "active": True}]}
routes = {}
for name, group in server.TOOL_GROUPS.items():
    if name in server.UNPUBLISHED_TOOLS:
        continue
    function = getattr(playwright_tools if name in playwright_tools.SCHEMAS else tools, name)
    required = {p: SAMPLES.get(p, "example") for p, param in inspect.signature(function).parameters.items()
                if param.default is inspect.Parameter.empty}
    seen = set()
    for shape in SHAPES.get(name, [{}]):
        with patch.object(tools, "_get", return_value=REPLY) as get, \
                patch.object(tools, "_post", return_value=REPLY) as post, patch("time.sleep"):
            try:
                function(**{**required, **shape})
            except TypeError:
                function(**required)
            seen |= {call.args[0] for call in get.call_args_list + post.call_args_list}
    routes[name] = [group, sorted(seen)]
print(json.dumps({"routes": routes, "header": server._client.caps}))
'''


def bridge_routes() -> set[str]:
    """The paths BridgeHandler serves, GET and POST."""
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.authorized = Mock(return_value=True)
    served = set()
    handler._dispatch = lambda routes: served.update(routes)
    handler.do_GET()
    handler.do_POST()
    return served


def bridge_run_actions() -> set[str]:
    """Container actions the bridge runs itself rather than forwarding (_execute_command)."""
    direct = set(re.findall(r"action == '(\w+)'", inspect.getsource(bridge._execute_command)))
    return direct | set(bridge._CONTROL_FLOW_HANDLERS)


class CapabilityTableTests(unittest.TestCase):
    def test_control_flow_handlers_are_not_shadowed_by_forwarded_actions(self):
        self.assertFalse(set(bridge._CONTROL_FLOW_HANDLERS) & set(bridge.BATCH_ACTION_MAP))
        self.assertTrue(all(inspect.iscoroutinefunction(handler)
                            for handler in bridge._CONTROL_FLOW_HANDLERS.values()))

    def test_every_route_and_container_action_has_a_group(self):
        served = bridge_routes()
        self.assertEqual(served - set(ROUTE_GROUPS) - OPEN_ROUTES, set(), "a bridge route without a group")
        self.assertEqual(set(ROUTE_GROUPS) - served, set(), "a mapped route the bridge does not serve")
        actions = set(bridge.BATCH_ACTION_MAP.values()) | bridge_run_actions()
        self.assertEqual(actions - set(ACTION_GROUPS), set(), "a container action without a group")
        self.assertEqual(set(ACTION_GROUPS) - actions, set(), "a mapped action the bridge does not run")
        for table in (ROUTE_GROUPS, ACTION_GROUPS):
            self.assertTrue(set().union(*table.values()) <= capabilities.CAPABILITY_GROUPS)

    def test_each_tool_needs_only_its_own_group(self):
        env = {**os.environ, "KITSUNE_CAPS": "all,storage,history"}
        result = subprocess.run([sys.executable, "-c", TOOL_ROUTES], env=env, capture_output=True, text=True,
                                check=True)
        found = json.loads(result.stdout)
        self.assertEqual(found["header"], ",".join(sorted(capabilities.CAPABILITY_GROUPS)))
        for name, (group, routes) in found["routes"].items():
            with self.subTest(tool=name):
                for route in routes:
                    self.assertTrue(route in OPEN_ROUTES or group in ROUTE_GROUPS[route], route)

    def test_mcp_server_sends_its_enabled_groups(self):
        for caps, header in (("core", "core"), ("all", "admin,automation,browse,core,inspect,network,read,tabs"),
                             ("all,storage", "admin,automation,browse,core,inspect,network,read,storage,tabs"),
                             ("", "")):
            env = {**os.environ, "KITSUNE_CAPS": caps}
            result = subprocess.run([sys.executable, "-c", "from kitsune import server; print(server._client.caps)"],
                                    env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), header)

    def test_pre_dispatch_refusals_are_failed_not_unknown(self):
        # #249: nothing was dispatched, so these are not outcomes that may have taken effect.
        caps = frozenset({"core", "automation", "read"})
        for refusal in (capabilities.caps_refusal(caps, "cookies", ACTION_GROUPS["cookies"]),
                        capabilities.caps_refusal(caps, "teleport", None),
                        {"error": "read mode refuses click", "code": "mode_restricted", "retryable": False},
                        bridge._unknown_action_error("html")):
            with self.subTest(code=refusal["code"]):
                self.assertEqual(bridge._result_status(refusal), "failed")
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


class BridgeCapsTests(AskBeforeActTestCase):
    ask = False

    def request(self, path, body, caps=None, method="POST", mode=None):
        """Call a route through BridgeHandler; caps=None sends no capability header."""
        handler = self.handler(body)
        handler.authorized = Mock(return_value=True)
        handler.path = path
        handler.command = method
        handler.headers = {} if caps is None else {CAPS_HEADER: caps}
        if mode is not None:
            handler.headers[bridge.MODE_HEADER] = mode
        handler.do_POST() if method == "POST" else handler.do_GET()
        return handler.send_json.call_args.args

    def test_routes_of_disabled_groups_are_refused(self):
        status, result = self.request("/api/cookies", {"op": "get", "url": "https://example.com"}, "core,automation")
        self.assertEqual((status, result["code"], result["retryable"]), (403, "capability_restricted", False))
        self.assertEqual(result["state"], {"groups": ["storage"], "caps": ["automation", "core"]})
        self.assertIn("KITSUNE_CAPS", result["hint"])
        self.assertEqual(self.extension.sent, [])
        status, result = self.request("/api/cookies", {"op": "get", "url": "https://example.com"}, "core,storage")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.extension.actions(), ["cookies"])
        self.assertIsNone(bridge._client_caps.get())

    def test_shared_and_open_routes(self):
        # core and network both capture network requests.
        for caps in ("core", "network"):
            status, _ = self.request("/api/capture-network", {"op": "get"}, caps)
            self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/capture-network", {"op": "get"}, "read")[0], 403)
        with patch.object(bridge.BridgeHandler, "handle_status", lambda handler: handler.send_json(200, {})):
            self.assertEqual(self.request("/api/status", {}, "", method="GET")[0], 200)

    def test_core_checks_boxes_for_browser_fill_form(self):
        # #236: browser_fill_form (core) sends checkbox and radio fields to /api/check.
        for caps in ("core", "browse"):
            status, result = self.request("/api/check", {"selector": "#agree", "checked": True}, caps)
            self.assertEqual(status, 200, result)
        self.assertEqual(self.request("/api/check", {"selector": "#agree", "checked": True}, "read")[0], 403)
        status, result = self.request("/api/batch", {"commands": [{"action": "checkBox", "selector": "#agree"}]},
                                      "automation,core")
        self.assertEqual((status, result["results"]), (200, [{"ok": True}]))
        self.assertEqual(self.extension.actions(), ["checkBox", "checkBox", "checkBox"])

    def test_core_permits_everything_draft_mode_adds(self):
        # #241: draft mode exists to fill forms, so under the default caps its routes and
        # actions must not be refused by the capability tables instead.
        self.assertEqual(sorted(path for path in task_modes.DRAFT_ROUTES - task_modes.READ_ROUTES
                                if "core" not in ROUTE_GROUPS.get(path, ())), [])
        self.assertEqual(sorted(action for action in task_modes.DRAFT_TOOLS.values()
                                if "core" not in ACTION_GROUPS.get(action, ())), [])
        self.extension.target = {"ok": True, "tag": "input", "submits": False, "sensitive": False}
        status, result = self.request("/api/check", {"selector": "#agree", "checked": True}, "core", mode="draft")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.extension.actions(), ["checkBox"])

    def test_requests_without_the_header_are_not_restricted(self):
        status, result = self.request("/api/cookies", {"op": "get", "url": "https://example.com"})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.request("/api/batch", {"commands": [{"action": "clipboard", "op": "read"}]})[1][
            "results"], [{"ok": True}])

    def test_invalid_header(self):
        status, result = self.request("/api/cookies", {"op": "get"}, "core,devtools")
        self.assertEqual((status, result["code"]), (400, "invalid_caps"))
        self.assertEqual(self.extension.sent, [])

    def test_container_steps_of_disabled_groups_are_refused(self):
        commands = [{"action": "navigate", "url": "https://example.com", "tabId": 7},
                    {"action": "cookies", "op": "get", "url": "https://example.com"},
                    {"action": "retry", "command": {"action": "clipboard", "op": "read"}},
                    {"action": "parallel", "sequences": [[{"action": "scroll", "tabId": 7, "direction": "down"}]]}]
        status, result = self.request("/api/batch", {"commands": commands}, "automation,core")
        self.assertEqual(status, 200)
        navigate, cookies, *rest = result["results"]
        self.assertEqual(navigate, {"ok": True})
        self.assertEqual((cookies["code"], cookies["state"]["groups"]), ("capability_restricted", ["storage"]))
        self.assertEqual(self.extension.actions(), ["navigate"])
        self.assertIn('"capability_restricted"', json.dumps(rest))
        self.assertIn('"browse"', json.dumps(rest))
        # #249: refused before dispatch, so failed, never "may have taken effect".
        self.assertEqual([record["status"] for record in result["progress"]], ["ok", "failed", "failed", "failed"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn('"unknown"', json.dumps(result["progress"]))
        self.assertNotIn("may have taken effect", result["summary"])
        self.assertIn("Failed, safe to repeat after its hint", result["summary"])

        self.extension.sent.clear()
        status, result = self.request("/api/batch", {"commands": commands}, "automation,browse,core,storage")
        self.assertEqual(status, 200)
        self.assertNotIn("capability_restricted", json.dumps(result))
        self.assertEqual(self.extension.actions(), ["navigate", "cookies", "clipboard", "scroll"])

    def test_refused_write_resumes_without_observed_once_its_group_is_enabled(self):
        self.addCleanup(bridge._drop_all_checkpoints)
        status, result = self.request("/api/batch", {"commands": [
            {"action": "scroll", "tabId": 7, "direction": "down"}]}, "automation,core")
        self.assertEqual((status, result["status"], result["progress"][0]["status"]), (200, "failed", "failed"))
        self.assertEqual(self.extension.actions(), [])
        status, resumed = self.request("/api/resume", {"call": result["call"], "resume_token": result["resume_token"]},
                                       "automation,browse,core")
        self.assertEqual((status, resumed["status"]), (200, "ok"), resumed)
        self.assertEqual(self.extension.actions(), ["scroll"])

    def test_mode_restricted_container_steps_are_failed(self):
        # Restricted modes refuse the container routes; a step refused by the mode is failed too.
        token = bridge._task_mode.set("read")
        self.addCleanup(bridge._task_mode.reset, token)
        self.addCleanup(bridge._drop_all_checkpoints)
        status, result = self.call(lambda h: h.handle_batch(), {"commands": [
            {"action": "click", "selector": "#a", "tabId": 7},
            {"action": "sequence", "commands": [{"action": "type", "selector": "#q", "text": "x", "tabId": 7}]}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["results"][0]["code"], "mode_restricted")
        self.assertEqual([record["status"] for record in result["progress"]], ["failed", "failed"])
        self.assertNotIn('"unknown"', json.dumps(result["progress"]))
        self.assertNotIn("may have taken effect", result["summary"])
        self.assertEqual(self.extension.actions(), [])

    def test_unknown_batch_actions_are_refused_with_the_closest_name(self):
        self.addCleanup(bridge._drop_all_checkpoints)
        # The header of KITSUNE_CAPS=all,history, as in the issue, and no header.
        for caps in (",".join(sorted(capabilities.enabled_groups(frozenset({"all", "history"})))), None):
            with self.subTest(caps=caps):
                status, result = self.request("/api/batch", {"commands": [
                    {"action": "html", "selector": "#item"},
                    {"action": "parallel", "sequences": [[{"action": "teleport", "tabId": 7}]]}]}, caps)
                self.assertEqual(status, 200)
                html = result["results"][0]
                self.assertEqual((html["code"], html["retryable"], html["state"]),
                                 ("unknown_action", False, {"action": "html", "suggestion": "getHTML"}))
                self.assertIn('"Unknown batch action: teleport"', json.dumps(result["results"][1]))
                self.assertEqual([record["status"] for record in result["progress"]], ["failed", "failed"])
                self.assertNotIn('"unknown"', json.dumps(result["progress"]))
                self.assertEqual(self.extension.sent, [])

    def test_refused_workflow_steps_are_failed(self):
        self.addCleanup(bridge._drop_all_checkpoints)
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge, "WORKFLOW_DIR", tmp):
            with open(os.path.join(tmp, "refused.md"), "w", encoding="utf-8") as f:
                f.write('```json\n{"action": "scroll", "tabId": 7, "direction": "down"}\n```\n')
            status, result = self.request("/api/workflow", {"name": "refused"}, "automation,core")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["results"][0]["code"], "capability_restricted")
        self.assertEqual((result["status"], result["progress"][0]["status"]), ("failed", "failed"))
        self.assertNotIn("may have taken effect", result["summary"])

    def test_containers_need_automation(self):
        status, result = self.request("/api/batch", {"commands": [{"action": "navigate", "url": "https://a.test"}]},
                                      "core")
        self.assertEqual((status, result["state"]["groups"]), (403, ["automation"]))

    def test_refusal_without_a_group(self):
        refused = capabilities.caps_refusal(frozenset({"core"}), "teleport", None)
        self.assertEqual((refused["code"], refused["state"]["groups"]), ("capability_restricted", []))
        self.assertIsNone(capabilities.caps_refusal(None, "teleport", None))


if __name__ == "__main__":
    unittest.main()
