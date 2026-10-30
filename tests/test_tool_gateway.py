"""#227: authorized tools outside KITSUNE_PUBLISH are found, called and listed without a restart."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock

from kitsune import bridge, task_modes
from test_ask_before_act import AskBeforeActTestCase

# Connected MCP sessions on in-memory streams against a mocked bridge: every request the MCP
# server sends is recorded, and the bridge answers from RESPONSES by endpoint.
SESSIONS = r'''import asyncio, base64, json, sys, tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import anyio
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from kitsune import bridge_client, server

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
RESPONSES = {"/api/screenshot": {"ok": True, "dataUrl": PNG}, "/api/snapshot": {"ok": True, "snapshot": "- button"},
             "/api/submit-form": {"error": "no form", "code": "no_form", "retryable": False}}
requests = []
ROOT = Path(tempfile.mkdtemp())


def bridge(method, url, headers=None, json=None, **kwargs):
    endpoint = url.split("47651", 1)[-1] if "47651" in url else url[url.index("/api"):]
    requests.append({"method": method, "endpoint": endpoint, "body": json, "headers": dict(headers or {})})
    return Mock(status_code=200, json=Mock(return_value=RESPONSES.get(endpoint, {"ok": True})))


def dump(result):
    return {"isError": bool(result.isError),
            "content": [item.model_dump(mode="json", exclude_none=True) for item in result.content]}


async def roots(context):
    return types.ListRootsResult(roots=[types.Root(uri=ROOT.as_uri())])


async def connect(stack, notes):
    async def handler(message):
        if isinstance(message, types.ServerNotification):
            notes.append(type(message.root).__name__)
    session = await stack.enter_async_context(create_connected_server_and_client_session(
        server.mcp, message_handler=handler, list_roots_callback=roots))
    return session


async def main(calls):
    from contextlib import AsyncExitStack
    out = {}
    with patch.object(bridge_client.httpx, "request", side_effect=bridge):
        async with AsyncExitStack() as stack:
            notes_a, notes_b = [], []
            a = await connect(stack, notes_a)
            b = await connect(stack, notes_b)
            out["listed"] = sorted(tool.name for tool in (await a.list_tools()).tools)
            results = []
            for step in calls:
                session = b if step.get("session") == "b" else a
                start = len(requests)
                if step["kind"] == "list":
                    results.append(sorted(tool.name for tool in (await session.list_tools()).tools))
                    continue
                result = await session.call_tool(step["name"], step.get("arguments", {}))
                await anyio.sleep(0.05)  # let notifications arrive
                results.append({**dump(result), "requests": requests[start:]})
            out["results"] = results
            out["notes"] = {"a": notes_a, "b": notes_b}
            out["root"] = str(ROOT)
    print(json.dumps(out))


asyncio.run(main(json.loads(sys.argv[1])))
'''

CAPABILITY = r'''import asyncio, json
from mcp.shared.memory import create_connected_server_and_client_session
from kitsune import server

async def main():
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = session.get_server_capabilities().tools
        print(json.dumps({"listChanged": bool(tools and tools.listChanged),
                          "listed": sorted(tool.name for tool in (await session.list_tools()).tools)}))

asyncio.run(main())
'''

GATEWAY = {"browser_tools_search", "browser_tools_call", "browser_tools_publish"}


def environment(caps, publish=None, mode=None, **extra):
    env = {**os.environ, "KITSUNE_CAPS": caps, "KITSUNE_TOKEN": "test-token", **extra}
    for name, value in (("KITSUNE_PUBLISH", publish), ("KITSUNE_MODE", mode)):
        env.pop(name, None)
        if value is not None:
            env[name] = value
    return env


def run(script, env, *args):
    result = subprocess.run([sys.executable, "-c", script, *args], env=env, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr[-3000:])
    return json.loads(result.stdout.splitlines()[-1])


def sessions(env, calls):
    return run(SESSIONS, env, json.dumps(calls))


def call(name, arguments=None, session="a"):
    return {"kind": "call", "name": name, "arguments": arguments or {}, "session": session}


def via(name, arguments=None, session="a"):
    return call("browser_tools_call", {"name": name, "arguments": arguments or {}}, session)


def without_gateway_header(result, call_ids=False):
    """result without the gateway header; call_ids: also without the random tool call id (#326)."""
    dropped = {task_modes.GATEWAY_TOOL_HEADER, *([task_modes.TOOL_CALL_HEADER] if call_ids else [])}
    requests = [{**request, "headers": {key: value for key, value in request["headers"].items()
                                        if key not in dropped}}
                for request in result["requests"]]
    return {**result, "requests": requests}


def payload(result):
    return json.loads(result["content"][0]["text"])


class DefaultSurfaceTests(unittest.TestCase):
    def test_without_publish_the_surface_and_capabilities_are_unchanged(self):
        for caps in ("core", "all", "all,storage,history"):
            with self.subTest(caps=caps):
                result = run(CAPABILITY, environment(caps))
                self.assertFalse(result["listChanged"])
                self.assertFalse(set(result["listed"]) & GATEWAY)

    def test_publishing_every_authorized_group_adds_no_gateway(self):
        result = run(CAPABILITY, environment("core,browse", "browse,core"))
        self.assertFalse(result["listChanged"])
        self.assertFalse(set(result["listed"]) & GATEWAY)

    def test_publish_must_name_authorized_groups(self):
        for publish, message in (("core,browse", "KITSUNE_PUBLISH names groups KITSUNE_CAPS does not enable: browse"),
                                 ("devtools", "Unknown KITSUNE_PUBLISH: devtools")):
            result = subprocess.run([sys.executable, "-c", "import kitsune.server"],
                                    env=environment("core", publish), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)


class GatewayTests(unittest.TestCase):
    """KITSUNE_CAPS authorizes core, browse, read and admin; tools/list starts with core."""

    @classmethod
    def setUpClass(cls):
        cls.env = environment("core,browse,read,admin", "core", KITSUNE_PROTECT_USER_TABS="0")
        cls.capability = run(CAPABILITY, cls.env)

    def test_capability_and_initial_list(self):
        self.assertTrue(self.capability["listChanged"])
        listed = set(self.capability["listed"])
        self.assertTrue(GATEWAY <= listed)
        self.assertNotIn("browser_scroll", listed)
        self.assertEqual(len(listed), 25 + len(GATEWAY))

    def test_discover_then_invoke_in_one_connection(self):
        result = sessions(self.env, [
            call("browser_tools_search", {"query": "scroll the page"}),
            via("browser_scroll", {"direction": "down", "tab_id": 3}),
            call("browser_scroll", {"direction": "down", "tab_id": 3}),
            via("browser_take_screenshot"),
            call("browser_take_screenshot"),
            via("browser_submit_form", {"selector": "form", "tab_id": 3, "confirm_token": "token-1"}),
            call("browser_submit_form", {"selector": "form", "tab_id": 3, "confirm_token": "token-1"}),
            via("browser_scroll", {"amount": "far"}),
            call("browser_scroll", {"amount": "far"}),
            via("browser_snapshot", {"filename": "page.md"}),
            call("browser_snapshot", {"filename": "page.md"}),
        ])
        search, *pairs = result["results"]
        found = payload(search)
        self.assertFalse(search["isError"])
        scroll = found["tools"][0]
        self.assertEqual((scroll["name"], scroll["group"], scroll["listed"]), ("browser_scroll", "browse", False))
        self.assertIn("direction", scroll["inputSchema"]["properties"])
        self.assertEqual(scroll["annotations"]["title"], "Scroll")
        self.assertLessEqual(len(found["tools"]), 5)
        for gateway, direct in zip(pairs[::2], pairs[1::2]):
            with self.subTest(direct=direct["content"][0]):
                # Same content, error status and bridge requests (protect-user-tabs header included).
                self.assertEqual(without_gateway_header(gateway, call_ids=True),
                                 without_gateway_header(direct, call_ids=True))
                for request in gateway["requests"] + direct["requests"]:
                    self.assertRegex(request["headers"][task_modes.TOOL_CALL_HEADER], r"^[0-9a-f]{16}$")
                for request in gateway["requests"]:
                    self.assertTrue(request["headers"][task_modes.GATEWAY_TOOL_HEADER].startswith("browser_"))
                    self.assertEqual(request["headers"][task_modes.PROTECT_USER_TABS_HEADER], "0")
                for request in direct["requests"]:
                    self.assertNotIn(task_modes.GATEWAY_TOOL_HEADER, request["headers"])
        scrolled, screenshot, submit, invalid, snapshot = pairs[::2]
        self.assertEqual(scrolled["requests"][0]["headers"][task_modes.GATEWAY_TOOL_HEADER], "browser_scroll")
        self.assertEqual([item["type"] for item in screenshot["content"]], ["image", "text"])
        self.assertEqual(submit["requests"][0]["body"]["confirmToken"], "token-1")
        self.assertTrue(submit["isError"])
        self.assertEqual(payload(submit)["code"], "no_form")
        self.assertTrue(invalid["isError"])
        self.assertIn("validation error", invalid["content"][0]["text"])
        self.assertEqual(invalid["requests"], [])
        # A relative file name resolves in the client's root either way.
        self.assertEqual(payload(snapshot)["saved_to"], os.path.join(result["root"], "page.md"))

    def test_unknown_unauthorized_and_recursive_names(self):
        result = sessions(self.env, [
            via("browser_cookies"), call("browser_cookies"),
            via("browser_nope"),
            via("browser_tools_call", {"name": "browser_scroll"}),
            via("browser_tools_publish", {"names": ["browse"]}),
            call("browser_tools_search", {"query": "cookies storage clipboard history"}),
            call("browser_tools_search", {"query": "", "limit": 10}),
            call("browser_tools_search", {"query": "scroll", "limit": 11}),
        ])
        gateway_cookies, direct_cookies, unknown, recursive, publish, search, overview, limit = result["results"]
        self.assertEqual(gateway_cookies, direct_cookies)
        self.assertEqual(payload(direct_cookies)["code"], "tool_not_published")
        self.assertEqual((unknown["isError"], payload(unknown)["code"]), (True, "unknown_tool"))
        for refused in (recursive, publish):
            self.assertTrue(refused["isError"])
            self.assertEqual(payload(refused)["code"], "invalid_argument")
            self.assertEqual(refused["requests"], [])
        found = payload(search)
        names = {tool["name"] for tool in found["tools"]} | {
            name for tools in found.get("groups", {}).values() for name in tools}
        self.assertFalse(names & {"browser_cookies", "browser_storage", "browser_clipboard", "browser_history_search"})
        groups = payload(overview)["groups"]
        self.assertEqual(set(groups), {"browse", "read", "admin"})
        self.assertFalse(set().union(*groups.values()) & GATEWAY)
        self.assertEqual(payload(limit)["code"], "invalid_argument")

    def test_publish_lists_tools_for_one_session_only(self):
        result = sessions(self.env, [
            call("browser_tools_publish", {"names": ["browse", "browser_page_text"]}),
            {"kind": "list"},
            {"kind": "list", "session": "b"},
            call("browser_tools_publish", {"names": ["browse", "browser_navigate"]}),
            call("browser_tools_publish", {"names": ["storage"]}),
            call("browser_tools_publish", {"names": ["browser_nope"]}),
            call("browser_tools_search", {"query": "scroll"}),
            call("browser_tools_search", {"query": "scroll"}, session="b"),
            call("browser_scroll", {"direction": "down", "tab_id": 3}, session="b"),
        ])
        published, listed_a, listed_b, again, storage, unknown, search_a, search_b, direct_b = result["results"]
        added = payload(published)
        self.assertEqual(set(added["published"]), {
            "browser_scroll", "browser_forward", "browser_check", "browser_focus", "browser_blur",
            "browser_submit_form", "browser_click_and_wait_navigation", "browser_trusted_click",
            "browser_trusted_drag", "browser_trusted_type", "browser_trusted_press_key", "browser_page_text"})
        self.assertTrue(added["notified"])
        self.assertEqual(result["notes"], {"a": ["ToolListChangedNotification"], "b": []})
        self.assertTrue(set(added["published"]) <= set(listed_a))
        self.assertEqual(listed_b, result["listed"])
        # Listed tools, at session start or by the earlier call, add nothing and send no notification.
        self.assertEqual((payload(again)["published"], payload(again)["notified"]), ([], False))
        for refused in (storage, unknown):
            self.assertTrue(refused["isError"])
            self.assertEqual(payload(refused)["code"], "tool_not_published")
        # Unknown and unauthorized names get the same answer.
        self.assertEqual(payload(storage)["error"].split(": ")[0], payload(unknown)["error"].split(": ")[0])
        self.assertTrue(payload(search_a)["tools"][0]["listed"])
        self.assertFalse(payload(search_b)["tools"][0]["listed"])
        # Authorized tools stay callable directly in any session, listed or not.
        self.assertFalse(direct_b["isError"])


class GatewayModeTests(unittest.TestCase):
    def test_read_mode_limits_apply_through_the_gateway(self):
        env = environment("core,browse,read", "core", mode="read")
        result = sessions(env, [
            call("browser_tools_search", {"query": "click submit form check focus", "limit": 10}),
            via("browser_click", {"target": "e1"}), call("browser_click", {"target": "e1"}),
            via("browser_scroll", {"direction": "down", "confirm_token": "x"}),
            call("browser_scroll", {"direction": "down", "confirm_token": "x"}),
            via("browser_scroll", {"direction": "down", "tab_id": 3}),
        ])
        search, gateway_click, direct_click, gateway_token, direct_token, scroll = result["results"]
        names = {tool["name"] for tool in payload(search)["tools"]}
        self.assertFalse(names & {"browser_click", "browser_submit_form", "browser_check", "browser_focus",
                                  "browser_trusted_click"})
        self.assertEqual(gateway_click, direct_click)
        self.assertIn("KITSUNE_MODE=act", payload(direct_click)["hint"])
        self.assertEqual(gateway_token, direct_token)
        self.assertEqual(payload(direct_token)["code"], "mode_restricted")
        self.assertFalse(scroll["isError"])
        self.assertEqual(scroll["requests"][0]["headers"][task_modes.MODE_HEADER], "read")
        self.assertIn("browser_tools_call", result["listed"])

    def test_all_leaves_personal_data_groups_unauthorized(self):
        env = environment("all", "core")
        result = sessions(env, [
            call("browser_tools_search", {"query": "", "limit": 10}),
            via("browser_cookies"), via("browser_history_search", {"query": "x"}),
            call("browser_tools_publish", {"names": ["storage", "history"]}),
        ])
        overview, cookies, history, publish = result["results"]
        self.assertFalse({"storage", "history"} & set(payload(overview)["groups"]))
        for refused in (cookies, history, publish):
            self.assertEqual(payload(refused)["code"], "tool_not_published")


class GatewayAuditTests(AskBeforeActTestCase):
    ask = False

    def test_audit_entries_name_the_gateway_and_its_tool(self):
        for header, expected in (("browser_scroll", {"gateway": "browser_tools_call", "tool": "browser_scroll"}),
                                 ("not a tool\nname", None), (None, None)):
            with self.subTest(header=header):
                bridge._audit_log.clear()
                handler = self.handler({"tabId": 1, "direction": "down"})
                handler.path = "/api/scroll"
                handler.headers = {task_modes.GATEWAY_TOOL_HEADER: header} if header else {}
                handler._dispatch({"/api/scroll": lambda: handler._forward("scroll")})
                self.assertEqual(handler.send_json.call_args.args[0], 200, handler.send_json.call_args)
                self.assertEqual(bridge._audit_log[-1].get("via"), expected)
                self.assertIsNone(bridge._gateway_tool.get())


if __name__ == "__main__":
    unittest.main()
