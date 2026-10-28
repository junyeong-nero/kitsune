"""#325: the bridge records which MCP client opened each tab, so a restarted server can offer its tab.

The owner id (X-Kitsune-Owner, BridgeClient.owner_id) survives an MCP server restart, unlike the
client id. Only the requesting owner sees mine: true, and a no_current_tab refusal lists its
open tabs in state.ownTabs without making one current.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge, bridge_client, playwright_tools, task_modes, tools
from kitsune.bridge_client import BridgeClient
from test_bridge_security import Bridge, _request
from test_container_progress import ContainerTests
from test_tab_inventory_bridge import ScriptedExtension, tab

OWNER, OTHER = "claude-300", "claude-301"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A client process that starts stand-ins for its MCP server, directly and through uv run when uv is
# installed, and prints its pid and the owner id each of them derives.
SERVERS_OF_ONE_CLIENT = r'''import json, os, shutil, subprocess, sys
code = "from kitsune import bridge_client; print(bridge_client.owner_id())"
starts = [[sys.executable], [sys.executable]] + ([["uv", "run", "--no-sync", "python"]] if shutil.which("uv") else [])
owners = [subprocess.run(start + ["-c", code], capture_output=True, text=True, check=True).stdout.strip()
          for start in starts]
print(json.dumps({"pid": os.getpid(), "owners": owners}))
'''


def ps_replies(chain):
    """subprocess.run for `ps -o ppid=,comm= -p <pid>` answering from chain: pid -> (ppid, comm)."""
    def run(command, **kwargs):
        pid = int(command[-1])
        ppid, comm = chain.get(pid, (None, None))
        return Mock(stdout="" if ppid is None else f"{ppid:>5} {comm}\n")
    return run


class OwnerIdTests(unittest.TestCase):
    def setUp(self):
        bridge_client._ancestor_owner.cache_clear()
        self.addCleanup(bridge_client._ancestor_owner.cache_clear)
        for patcher in (patch.dict("os.environ", {}), patch.object(bridge_client.sys, "platform", "darwin"),
                        patch.object(bridge_client.os, "getppid", return_value=100)):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge_client.os.environ.pop("KITSUNE_OWNER_ID", None)

    def owner(self, chain):
        bridge_client._ancestor_owner.cache_clear()
        with patch.object(bridge_client.subprocess, "run", side_effect=ps_replies(chain)) as run:
            return bridge_client.owner_id(), run

    def test_kitsune_owner_id_is_the_owner_when_valid(self):
        bridge_client.os.environ["KITSUNE_OWNER_ID"] = "my-session:1"
        owner, run = self.owner({100: (50, "claude")})
        self.assertEqual(owner, "my-session:1")
        run.assert_not_called()
        for invalid in ("has space", "x" * 65, "new\nline", ""):
            with self.subTest(invalid=invalid):
                bridge_client.os.environ["KITSUNE_OWNER_ID"] = invalid
                self.assertEqual(self.owner({100: (50, "claude")})[0], "claude-100")

    def test_the_nearest_ancestor_that_is_not_a_launcher(self):
        # uv run: the server's parent is uv; uvx runs as uv too.
        chain = {100: (300, "/Users/me/.local/bin/uv"), 300: (1, "/Applications/Claude.app/Contents/MacOS/claude")}
        owner, run = self.owner(chain)
        self.assertEqual(owner, "claude-300")
        self.assertEqual(run.call_args_list[0].args[0], ["ps", "-o", "ppid=,comm=", "-p", "100"])
        # A Python process is the MCP client (an MCP Python SDK client), with or without uv between.
        self.assertEqual(self.owner({100: (200, "uv"), 200: (300, "python3.12"), 300: (1, "zsh")})[0],
                         "python3.12-200")
        self.assertEqual(self.owner({100: (300, "/opt/homebrew/bin/python3.13"), 300: (1, "zsh")})[0],
                         "python3.13-100", "a Python client that starts the server without uv")
        self.assertEqual(self.owner({100: (1, "Python")})[0], "Python-100")
        self.assertEqual(self.owner({100: (300, "uvx"), 300: (1, "Code Helper (Plugin)")})[0],
                         "Code_Helper__Plugin_-300")
        long_name = self.owner({100: (1, "n" * 80)})[0]
        self.assertEqual((len(long_name), long_name[-4:]), (64, "-100"))
        self.assertTrue(bridge._CLIENT_ID.fullmatch(long_name))

    def test_falls_back_to_the_parent_pid(self):
        launchers = {100: (101, "uv"), 101: (102, "uvx"), 102: (103, "uv"), 103: (104, "uvx"),
                     104: (1, "claude")}
        self.assertEqual(self.owner(launchers)[0], "ppid-100", "only a few levels are walked")
        self.assertEqual(self.owner({})[0], "ppid-100", "ps does not know the process")
        bridge_client._ancestor_owner.cache_clear()
        with patch.object(bridge_client.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(bridge_client.owner_id(), "ppid-100")
        bridge_client._ancestor_owner.cache_clear()
        with patch.object(bridge_client.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertEqual(bridge_client.owner_id(), "ppid-100")
        with patch.object(bridge_client.sys, "platform", "win32"):
            owner, run = self.owner({100: (1, "claude")})
        self.assertEqual(owner, "ppid-100")
        run.assert_not_called()

    def test_an_orphan_gets_an_id_no_other_process_shares(self):
        # Started by launchd or systemd, or as a container's first process.
        for ppid in (1, 0):
            with self.subTest(ppid=ppid), patch.object(bridge_client.os, "getppid", return_value=ppid), \
                    patch.object(bridge_client.os, "getpid", return_value=4242):
                owner, run = self.owner({ppid: (0, "launchd")})
                self.assertEqual(owner, "pid-4242")
                run.assert_not_called()

    def test_this_machine_gives_a_valid_owner(self):
        bridge_client._ancestor_owner.cache_clear()
        with patch.object(bridge_client.os, "getppid", wraps=bridge_client.os.getpid):
            self.assertTrue(bridge._CLIENT_ID.fullmatch(bridge_client.owner_id()))

    @unittest.skipIf(sys.platform == "win32" or not shutil.which("ps"), "the owner walk needs ps")
    def test_servers_of_one_client_share_its_owner_and_sibling_clients_do_not(self):
        # Real processes: two sibling clients, each starting servers directly and through uv run.
        env = {k: v for k, v in os.environ.items() if k != "KITSUNE_OWNER_ID"}
        clients = [json.loads(subprocess.run([sys.executable, "-c", SERVERS_OF_ONE_CLIENT], env=env, cwd=ROOT,
                                             check=True, capture_output=True, text=True, timeout=60).stdout)
                   for _ in range(2)]
        for client in clients:
            with self.subTest(client=client):
                self.assertEqual(len(set(client["owners"])), 1, client)
                self.assertTrue(client["owners"][0].endswith(f"-{client['pid']}"), client)
        self.assertNotEqual(clients[0]["owners"][0], clients[1]["owners"][0])

    def test_each_request_sends_the_owner_next_to_the_client(self):
        bridge_client.os.environ["KITSUNE_OWNER_ID"] = OWNER
        first, restarted = BridgeClient(caps=None), BridgeClient(caps=None)
        self.assertEqual((first.owner_id, restarted.owner_id), (OWNER, OWNER))
        self.assertNotEqual(first.client_id, restarted.client_id)
        response = Mock(status_code=200, json=Mock(return_value={"ok": True}))
        with patch("kitsune.bridge_client.httpx.request", return_value=response) as request, \
                patch.object(BridgeClient, "await_extension"):
            first.post("/api/scroll", {}, 5)
        headers = request.call_args.kwargs["headers"]
        self.assertEqual((headers[task_modes.OWNER_HEADER], headers[task_modes.CLIENT_HEADER]),
                         (OWNER, first.client_id))

    def test_a_request_that_must_not_start_a_bridge_neither_starts_nor_waits_for_one(self):
        client = BridgeClient(caps=None)
        with patch("kitsune.bridge_client.httpx.request", side_effect=bridge_client.httpx.ConnectError("refused")), \
                patch.object(BridgeClient, "ensure_bridge") as ensure, \
                patch.object(BridgeClient, "await_extension") as wait:
            result = client.post("/api/tabs", {"mine": True}, 3, start_bridge=False)
        self.assertEqual(result["code"], "bridge_unavailable")
        ensure.assert_not_called()
        wait.assert_not_called()


class OwnerHeaderTests(ContainerTests):
    def setUp(self):
        super().setUp()
        self.replies["newTab"] = lambda params: asyncio.sleep(0, {"ok": True, "tabId": 70})

    def dispatch(self, path, route, body, headers):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = headers
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler._dispatch({path: lambda: route(handler)})
        return handler.send_json.call_args.args

    def test_a_valid_owner_header_is_recorded_on_audit_entries_and_tabs(self):
        for header, expected in ((OWNER, OWNER), ("not an id\n", None), ("x" * 65, None), (None, None)):
            with self.subTest(header=header), patch.object(bridge, "_audit_log", []) as log, \
                    patch.object(bridge, "_tab_openers", {}) as openers:
                headers = {task_modes.OWNER_HEADER: header} if header else {}
                self.dispatch("/api/new-tab", lambda h: h.handle_new_tab(), {"url": "https://a.test/"}, headers)
                self.assertEqual([entry.get("owner") for entry in log], [expected])
                self.assertEqual({tab_id: opener["owner"] for tab_id, opener in openers.items()},
                                 {70: expected} if expected else {})
                self.assertIsNone(bridge._owner_id.get())


class TabOpenersTests(ContainerTests):
    """The bridge's tab id -> owner map, recorded from every source of new tabs, against a mocked extension."""

    def setUp(self):
        super().setUp()
        openers = patch.object(bridge, "_tab_openers", {})
        self.openers = openers.start()
        self.addCleanup(openers.stop)
        pool = patch.dict(bridge._tab_pool, {"size": 0, "available": [], "in_use": set(), "url": "about:blank"})
        pool.start()
        self.addCleanup(pool.stop)
        self.next_tab = iter(range(50, 70))
        self.open_tabs = [1, 2]
        self.user_tabs = {1, 2}  # the extension's owned: false

        async def new_tab(params):
            tab_id = next(self.next_tab)
            self.open_tabs.append(tab_id)
            return {"ok": True, "tabId": tab_id}

        async def get_tabs(params):
            return {"tabs": [{"id": t, "url": f"https://t{t}.test/", "title": f"T{t}", "owned": t not in self.user_tabs}
                             for t in self.open_tabs if t not in self.closed]}

        self.replies.update(newTab=new_tab, getTabs=get_tabs)

    def as_owner(self, owner, route, body):
        token = bridge._owner_id.set(owner)
        try:
            return self.call(route, body)
        finally:
            bridge._owner_id.reset(token)

    def owners(self):
        return {tab_id: opener["owner"] for tab_id, opener in self.openers.items()}

    def tabs(self, owner, mine=False):
        return self.as_owner(owner, lambda h: h.handle_tabs(), {"mine": True} if mine else {})["tabs"]

    def test_direct_new_tabs_and_tabs_actions_open_are_recorded(self):
        self.as_owner(OWNER, lambda h: h.handle_new_tab(), {"url": "https://a.test/"})
        self.replies["click"] = [{"ok": True, "opened_tabs": [{"tabId": 61, "url": "https://b.test/"},
                                                              {"tabId": 62, "url": "https://c.test/",
                                                               "via": "link_fallback"}]}]
        self.as_owner(OTHER, lambda h: h.handle_click(), {"selector": "a", "tabId": 1})
        self.assertEqual(self.owners(), {50: OWNER, 61: OTHER, 62: OTHER})
        opener = self.openers[50]
        self.assertIsInstance(opener["opened"], float)

    def test_container_and_pool_tabs_are_recorded(self):
        payload = self.as_owner(OWNER, lambda h: h.handle_batch(), {"commands": [
            {"action": "newTab"}, {"action": "sequence", "do": [{"action": "newTab"}]}]})
        self.assertEqual(payload["newTabs"], [50, 51])
        self.replies["click"] = [{"ok": True, "opened_tabs": [{"tabId": 63, "url": "https://b.test/"}]}]
        self.as_owner(OTHER, lambda h: h.handle_batch(), {"commands": [{"action": "click", "selector": "a",
                                                                         "tabId": 1}]})
        self.as_owner(OTHER, lambda h: h.handle_tab_pool(), {"size": 1})
        self.assertEqual(self.owners(), {50: OWNER, 51: OWNER, 63: OTHER, 52: OTHER})

    def test_a_session_temp_tab_is_recorded_until_it_closes(self):
        at_close = []

        async def close_tab(params):
            at_close.append(self.owners())
            return {"ok": True}
        self.replies.update(cookies=[{"cookies": []}], storageOp=[{"ok": True, "items": {}}], closeTab=close_tab)
        token = bridge._owner_id.set(OWNER)
        try:
            with tempfile.TemporaryDirectory() as home, patch.object(bridge, "_sessions_dir", home), \
                    patch.object(bridge.asyncio, "sleep", new=AsyncMock()):
                saved = asyncio.run(bridge._save_session_async("work", ["https://a.test/"]))
        finally:
            bridge._owner_id.reset(token)
        self.assertIs(saved["ok"], True, saved)
        self.assertEqual((at_close, self.owners()), ([{50: OWNER}], {}))

    def test_requests_without_an_owner_and_failed_new_tabs_record_nothing(self):
        self.call(lambda h: h.handle_new_tab(), {"url": "https://a.test/"})
        self.replies["newTab"] = [{"error": "Invalid URL"}]
        self.as_owner(OWNER, lambda h: h.handle_new_tab(), {"url": "x"})
        self.assertEqual(self.owners(), {})

    def test_a_tab_keeps_its_first_owner(self):
        self.replies["click"] = [{"ok": True, "opened_tabs": [{"tabId": 61}]}] * 2
        self.as_owner(OWNER, lambda h: h.handle_click(), {"selector": "a", "tabId": 1})
        self.as_owner(OTHER, lambda h: h.handle_click(), {"selector": "a", "tabId": 1})
        self.assertEqual(self.owners(), {61: OWNER})

    def test_closed_tabs_are_forgotten(self):
        for _ in range(3):
            self.as_owner(OWNER, lambda h: h.handle_new_tab(), {})
        self.as_owner(OTHER, lambda h: h.handle_close_tab(), {"tabId": 50})
        self.as_owner(OWNER, lambda h: h.handle_batch(), {"commands": [{"action": "closeTab", "tabId": "51"}]})
        self.assertEqual(self.owners(), {52: OWNER})
        # A tab the user closed is forgotten at the next tab list, whoever asks for it.
        self.open_tabs.remove(52)
        self.call(lambda h: h.handle_tabs(), {})
        self.assertEqual(self.owners(), {})

    def test_a_failed_close_or_tab_list_forgets_nothing(self):
        self.as_owner(OWNER, lambda h: h.handle_new_tab(), {})
        self.replies["closeTab"] = [{"error": "user_tab", "code": "user_tab"}]
        self.call(lambda h: h.handle_close_tab(), {"tabId": 50})
        self.replies["getTabs"] = [{"error": "Extension not connected"}]
        self.call(lambda h: h.handle_tabs(), {})
        self.assertEqual(self.owners(), {50: OWNER})

    def test_mine_marks_only_the_requesting_owners_tabs(self):
        self.as_owner(OWNER, lambda h: h.handle_new_tab(), {})
        self.as_owner(OTHER, lambda h: h.handle_new_tab(), {})
        self.assertEqual([tab.get("mine") for tab in self.tabs(OWNER)], [None, None, True, None])
        self.assertEqual([tab.get("mine") for tab in self.tabs(OTHER)], [None, None, None, True])
        self.assertEqual([tab.get("mine") for tab in self.tabs(None)], [None] * 4)

    def test_mine_lists_the_owners_tabs_most_recent_first(self):
        self.open_tabs += [50, 51, 52]
        self.openers.update({50: {"owner": OWNER, "opened": 3.0}, 51: {"owner": OTHER, "opened": 4.0},
                             52: {"owner": OWNER, "opened": 5.0}, 53: {"owner": OWNER, "opened": 6.0}})
        self.assertEqual(self.tabs(OWNER, mine=True), [
            {"id": 52, "url": "https://t52.test/", "title": "T52", "owned": True, "mine": True},
            {"id": 50, "url": "https://t50.test/", "title": "T50", "owned": True, "mine": True}])
        self.assertNotIn(53, self.openers, "the closed tab 53 was forgotten")
        self.assertEqual(self.tabs(None, mine=True), [])

    def test_a_tab_the_extension_no_longer_owns_is_not_mine(self):
        # An extension reload or a synced copy going back to the user makes the agent's tab the user's.
        self.as_owner(OWNER, lambda h: h.handle_new_tab(), {})
        self.user_tabs.add(50)
        self.assertEqual([tab.get("mine") for tab in self.tabs(OWNER)], [None, None, None])
        self.assertEqual(self.tabs(OWNER, mine=True), [])
        self.assertEqual(self.owners(), {50: OWNER}, "the tab is still open, so its record stays")

    def test_tab_pool_tabs_are_never_mine(self):
        # Any client may acquire a pool tab: OWNER grew the pool, OTHER drives tab 50, 51 waits.
        self.as_owner(OWNER, lambda h: h.handle_tab_pool(), {"size": 2})
        acquired = self.as_owner(OTHER, lambda h: h.handle_pool_acquire(), {})
        self.assertEqual((acquired["tabId"], self.owners()), (50, {50: OWNER, 51: OWNER}))
        self.assertEqual(self.tabs(OWNER, mine=True), [])
        self.assertEqual([tab.get("mine") for tab in self.tabs(OWNER)], [None] * 4)

    def test_a_new_extension_connection_forgets_every_owner(self):
        self.openers.update({50: {"owner": OWNER, "opened": 1.0}})

        class Socket:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration
        with patch.object(bridge, "extension_ws", None), patch.object(bridge, "extension_info", {}), \
                patch.object(bridge, "_log"):
            asyncio.run(bridge.ws_handler(Socket()))
        self.assertEqual(self.owners(), {})


class OwningExtension(ScriptedExtension):
    """A scripted extension whose newTab tabs are the agent's (getTabs owned: true), like agentTabs."""

    def _new_tab(self, command):
        result = super()._new_tab(command)
        self.tabs[-1]["owned"] = True
        return result


class OwnedBridgeTests(unittest.TestCase):
    """A real bridge and HTTP headers: a tab a client opened is offered again after its server restarts."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name).start()
        self.addCleanup(self.bridge.stop)
        self.extension = self.connect_extension()

    def connect_extension(self):
        extension = OwningExtension(self.bridge, [tab(1, "https://user.test/", active=True)])
        self.addCleanup(extension.close)
        return extension

    def call(self, method, path, client, owner, body=None):
        headers = {**self.bridge.auth(), task_modes.CLIENT_HEADER: client, task_modes.OWNER_HEADER: owner}
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, _, text = _request(self.bridge.http_port, method, path, headers, self.bridge.host,
                                   json.dumps(body) if body is not None else None)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def test_a_restarted_server_of_the_same_client_finds_its_tab(self):
        opened = self.call("POST", "/api/new-tab", "100-aaaaaa", OWNER, {"url": "https://a.test/"})
        self.call("POST", "/api/new-tab", "200-bbbbbb", OTHER, {"url": "https://b.test/"})
        # The restarted server has another client id but the same owner id.
        listed = self.call("GET", "/api/tabs", "101-cccccc", OWNER)["tabs"]
        self.assertEqual({t["id"]: t.get("mine") for t in listed}, {1: None, opened["tabId"]: True, 3: None})
        own = self.call("POST", "/api/tabs", "101-cccccc", OWNER, {"mine": True})["tabs"]
        self.assertEqual([t["id"] for t in own], [opened["tabId"]])
        self.assertEqual([t["id"] for t in self.call("POST", "/api/tabs", "200-bbbbbb", OTHER, {"mine": True})["tabs"]],
                         [3])
        # The user closed it: it is no longer offered.
        self.extension.set_tabs(tab(1, "https://user.test/", active=True), tab(3, "https://b.test/"))
        self.assertEqual(self.call("POST", "/api/tabs", "101-cccccc", OWNER, {"mine": True})["tabs"], [])

    def test_a_restarted_browser_reusing_a_tab_id_does_not_offer_another_clients_tab(self):
        opened = self.call("POST", "/api/new-tab", "100-aaaaaa", OWNER, {"url": "https://a.test/"})
        self.extension.close()
        self.extension = self.connect_extension()
        # The restarted browser gives the next tab, another client's, the same id.
        reused = self.call("POST", "/api/new-tab", "200-bbbbbb", OTHER, {"url": "https://b.test/"})
        self.assertEqual(reused["tabId"], opened["tabId"])
        self.assertEqual(self.call("POST", "/api/tabs", "101-cccccc", OWNER, {"mine": True})["tabs"], [])
        self.assertEqual([t["id"] for t in self.call("POST", "/api/tabs", "200-bbbbbb", OTHER, {"mine": True})["tabs"]],
                         [reused["tabId"]])


class OwnTabsRefusalTests(unittest.TestCase):
    """tools._on_current_tab offers the client's own tabs in a no_current_tab refusal."""

    def setUp(self):
        current = patch.object(playwright_tools, "_current_tab", None)
        current.start()
        self.addCleanup(current.stop)
        self.sent = []

        def browser_scroll(direction: str = "down", tab_id: int | None = None) -> dict:
            self.sent.append(tab_id)
            return {"ok": True}
        self.scroll = tools._on_current_tab(browser_scroll)

    def refuse(self, listed):
        posts = []

        def post(endpoint, body, timeout=15, start_bridge=True):
            posts.append((endpoint, body, timeout, start_bridge))
            if isinstance(listed, Exception):
                raise listed
            return listed
        with patch.object(tools, "_post", side_effect=post):
            result = self.scroll()
        self.assertEqual(self.sent, [], "nothing is dispatched")
        self.assertEqual(posts, [("/api/tabs", {"mine": True}, tools.OWN_TABS_TIMEOUT, False)],
                         "a quick lookup that starts no bridge")
        self.assertIsNone(playwright_tools._current_tab, "no tab is adopted")
        return result

    def test_the_refusal_lists_the_clients_own_tabs(self):
        tabs = [{"id": i, "url": f"https://t{i}.test/", "title": f"T{i}", "mine": True, "active": False}
                for i in (58, 57, 56, 55, 54, 53)]
        result = self.refuse({"tabs": tabs})
        self.assertEqual((result["code"], result["retryable"]), ("no_current_tab", False))
        self.assertEqual(result["state"]["tool"], "browser_scroll")
        self.assertEqual(result["state"]["ownTabs"], [
            {"tabId": i, "url": f"https://t{i}.test/", "title": f"T{i}"} for i in (58, 57, 56, 55, 54)])
        unchanged = tools._no_current_tab("browser_scroll", {"direction": "down", "tab_id": None})
        self.assertTrue(result["hint"].startswith(unchanged["hint"]))
        self.assertIn("Tab 58 is the tab this MCP client opened most recently", result["hint"])
        self.assertTrue(result["hint"].endswith("to keep working in it, pass tab_id=58."), result["hint"])
        self.assertEqual({k: v for k, v in result.items() if k not in ("hint", "state")},
                         {k: v for k, v in unchanged.items() if k not in ("hint", "state")})

    def test_the_refusal_is_unchanged_without_own_tabs_or_when_the_lookup_fails(self):
        unchanged = tools._no_current_tab("browser_scroll", {"direction": "down", "tab_id": None})
        for listed in ({"tabs": []}, {"tabs": [{"id": 7, "url": "https://user.test/", "active": True}]},
                       {"error": "Kitsune extension not connected", "code": "extension_disconnected"},
                       {"ok": True}, {"tabs": "nope"}, ["not", "a", "dict"], RuntimeError("bridge gone")):
            with self.subTest(listed=listed):
                self.assertEqual(self.refuse(listed), unchanged)

    def test_reads_and_named_tabs_do_not_look_up_own_tabs(self):
        def browser_snapshot(tab_id: int | None = None) -> dict:
            self.sent.append(tab_id)
            return {"ok": True}
        snapshot = tools._on_current_tab(browser_snapshot)
        with patch.object(tools, "_post") as post:
            self.assertEqual(self.scroll(tab_id=7), {"ok": True})
            self.assertEqual(snapshot(), {"ok": True})
        post.assert_not_called()
        self.assertEqual(self.sent, [7, None], "the read went to the active tab")


if __name__ == "__main__":
    unittest.main()
