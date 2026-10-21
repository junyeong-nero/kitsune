"""Compact MCP tool results (#136): unindented JSON, the trimmed browser_tabs list and its active page (#138)."""
import asyncio
import json
import logging
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from kitsune import server

SNAPSHOT = {
    "snapshot": "\n".join([
        '- banner',
        '  - link "홈" [ref=s17925763275161094491287e0]',
        '  - searchbox "검색" [ref=s17925763275161094491287e1]',
        '- main',
        '  - heading "Kitsune 장바구니" [level=1]',
        '  - button "결제하기" [ref=s17925763275161094491287e2]',
        '  - checkbox "Remember me" [ref=s17925763275161094491287e3] [checked]',
    ]),
    "truncated": True,
}
TABS = {"tabs": [
    {"id": 3, "url": "https://example.com/", "title": "Example Domain", "active": False,
     "windowId": 1, "index": 0},
    {"id": 5, "url": "https://ko.wikipedia.org/wiki/여우", "title": "여우 - 위키백과", "active": True,
     "windowId": 1, "index": 1, "owned": True},
    {"id": 8, "url": "about:blank", "title": "New Tab", "active": False, "windowId": 1, "index": 2},
]}
CLICK = {"ok": True, "tag": "button", "text": "결제하기", "target_ref": "s17925763275161094491288e2",
         "snapshot": '- main\n  - button "결제하기" [ref=s17925763275161094491288e2]',
         "navigation": {"ok": True, "url": "https://example.com/checkout"}}
STALE = {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
         "hint": "A newer snapshot replaced this ref; state.currentRef is the same element in it.",
         "state": {"reason": "snapshot_replaced", "ref": "s17925763275161094491287e2",
                   "latestObservation": "browser_click", "currentRef": "s17925763275161094491288e2",
                   "tabId": 5, "url": "https://ko.wikipedia.org/wiki/여우", "status": "complete"}}


def without_window_fields(result):
    return {**result, "tabs": [{key: value for key, value in tab.items() if key not in ("windowId", "index")}
                               for tab in result["tabs"]]}


def indented(result) -> str:
    """The text results had before #136 (FastMCP's and _mcp_errors' indent=2 JSON)."""
    return json.dumps(result, indent=2, ensure_ascii=False)


class CompactResultTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)

    def call(self, name, arguments, reply, mcp=server.mcp):
        async def run():
            async with create_connected_server_and_client_session(mcp._mcp_server) as session:
                return await session.call_tool(name, arguments)

        with patch.object(server.httpx, "request", return_value=Mock(json=Mock(return_value=reply))):
            return asyncio.run(run())

    def text(self, result) -> str:
        self.assertEqual(len(result.content), 1)
        return result.content[0].text

    def test_results_are_compact_json_parsing_to_the_same_objects(self):
        for name, arguments, reply, expected, is_error in (
                ("browser_snapshot", {}, SNAPSHOT, SNAPSHOT, False),
                ("browser_tabs", {}, TABS, without_window_fields(TABS), False),
                ("browser_click", {"ref": "s17925763275161094491287e2"}, CLICK, CLICK, False),
                ("browser_click", {"ref": "s17925763275161094491287e2"}, STALE, STALE, True)):
            with self.subTest(name=name, is_error=is_error):
                result = self.call(name, arguments, reply)
                text = self.text(result)
                self.assertIs(result.isError, is_error)
                self.assertEqual(json.loads(text), json.loads(indented(expected)))
                self.assertEqual(text, json.dumps(expected, separators=(",", ":"), ensure_ascii=False))
                self.assertNotIn("\n", text)

    def test_non_ascii_text_stays_readable(self):
        text = self.text(self.call("browser_click", {"ref": "s1e0"}, STALE))
        self.assertIn("ko.wikipedia.org/wiki/여우", text)
        self.assertNotIn("\\u", text)

    def test_compact_sizes(self):
        """Characters before (indent=2) and after (compact) for representative results."""
        sizes = {}
        for label, name, arguments, reply, before in (
                ("snapshot", "browser_snapshot", {}, SNAPSHOT, SNAPSHOT),
                ("tabs list", "browser_tabs", {}, TABS, TABS),
                ("action result", "browser_click", {"ref": "s1e0"}, CLICK, CLICK),
                ("error", "browser_click", {"ref": "s1e0"}, STALE, STALE)):
            sizes[label] = (len(indented(before)), len(self.text(self.call(name, arguments, reply))))
        self.assertEqual(sizes, {"snapshot": (328, 319), "tabs list": (506, 247),
                                 "action result": (258, 218), "error": (464, 398)})

    def test_screenshot_metadata_in_list_results_is_compact(self):
        def listing():
            return [{"saved": "/tmp/shot.png", "제목": "화면"}, "plain text"]

        mcp = FastMCP("test")
        mcp.tool()(server._mcp_errors(listing))

        async def run():
            async with create_connected_server_and_client_session(mcp._mcp_server) as session:
                return await session.call_tool("listing", {})

        result = asyncio.run(run())
        self.assertEqual([item.text for item in result.content],
                         ['{"saved":"/tmp/shot.png","제목":"화면"}', "plain text"])
        self.assertFalse(result.isError)


class TabListTests(unittest.TestCase):
    def list_tabs(self, reply, **kwargs):
        with patch.object(server, "_get", return_value=reply) as get:
            result = server.browser_tabs(**kwargs)
        get.assert_called_once_with("/api/tabs")
        return result

    def test_single_window_drops_window_id_and_index_and_keeps_other_fields(self):
        result = self.list_tabs(TABS)
        self.assertEqual(result, without_window_fields(TABS))
        self.assertEqual(list(result["tabs"][1]), ["id", "url", "title", "active", "owned"])

    def test_window_fields_on_request(self):
        self.assertEqual(self.list_tabs(TABS, window_fields=True), TABS)

    def test_window_fields_when_tabs_span_windows(self):
        tabs = {"tabs": [*TABS["tabs"], {"id": 9, "url": "https://example.org/", "title": "Other",
                                         "active": True, "windowId": 2, "index": 0}]}
        self.assertEqual(self.list_tabs(tabs), tabs)

    def test_errors_and_empty_lists_pass_through(self):
        for reply in ({"error": "Kitsune extension not connected", "code": "extension_disconnected"},
                      {"tabs": []}):
            with self.subTest(reply=reply):
                self.assertEqual(self.list_tabs(reply), reply)


class ActivePageTests(unittest.TestCase):
    """browser_tabs list with include_active returns the active page in the same call (#138)."""
    ACTIVE_PAGE = {**SNAPSHOT, "url": "https://ko.wikipedia.org/wiki/여우", "title": "여우 - 위키백과",
                   "tabId": 5}

    def list_tabs(self, tabs, page, **kwargs):
        with patch.object(server, "_get", return_value=tabs) as get, \
                patch.object(server, "_post", return_value=page) as post:
            result = server.browser_tabs(**kwargs)
        get.assert_called_once_with("/api/tabs")
        return result, post

    def test_include_active_adds_the_active_tab_text_snapshot(self):
        result, post = self.list_tabs(TABS, self.ACTIVE_PAGE, include_active=True)
        post.assert_called_once_with("/api/snapshot", {"text": True})
        self.assertEqual(result, {**without_window_fields(TABS), "active_page": self.ACTIVE_PAGE})

    def test_list_reads_no_page_by_default(self):
        result, post = self.list_tabs(TABS, self.ACTIVE_PAGE)
        post.assert_not_called()
        self.assertNotIn("active_page", result)

    def test_page_read_error_keeps_the_inventory(self):
        error = {"error": "Cannot access this page", "code": "restricted_page"}
        result, _ = self.list_tabs(TABS, error, include_active=True)
        self.assertEqual(result, {**without_window_fields(TABS), "active_page": error})
        self.assertFalse(server._failed(result))

    def test_inventory_error_skips_the_page_read(self):
        error = {"error": "Kitsune extension not connected", "code": "extension_disconnected"}
        result, post = self.list_tabs(error, self.ACTIVE_PAGE, include_active=True)
        post.assert_not_called()
        self.assertEqual(result, error)

    def test_include_active_is_ignored_by_other_actions(self):
        with patch.object(server, "_post", return_value={"ok": True, "tabId": 9}) as post:
            result = server.browser_tabs("new", include_active=True)
        post.assert_called_once_with("/api/new-tab", {"url": "about:blank"})
        self.assertEqual(result, {"ok": True, "tabId": 9})


if __name__ == "__main__":
    unittest.main()
