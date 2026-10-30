"""A navigate request outlasts the extension's load wait and page check (#366), so the caller
gets navigation_stalled and its hint instead of a client-side timeout."""
import asyncio
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, playwright_tools, tools, waits
from kitsune.browser import Tab

BACKGROUND = Path(__file__).resolve().parents[1] / "extension" / "background" / "background.js"

# How long the extension may take before it answers a navigate with navigation_stalled.
EXTENSION_SECONDS = waits.NAVIGATION_LOAD_SECONDS + waits.PAGE_URL_SECONDS


class NavigateWaitTests(unittest.TestCase):
    def test_wait_bounds_match_the_extension(self):
        source = BACKGROUND.read_text()
        load_ms = re.search(r"function waitForTabLoad\([^)]*timeout = (\d+)", source).group(1)
        page_ms = re.search(r"const PAGE_URL_TIMEOUT_MS = (\d+);", source).group(1)
        self.assertEqual(waits.NAVIGATION_LOAD_SECONDS, int(load_ms) / 1000)
        self.assertEqual(waits.PAGE_URL_SECONDS, int(page_ms) / 1000)

    def test_navigate_request_outlasts_the_load_wait_and_page_check(self):
        self.assertGreater(waits.NAVIGATE_REQUEST_SECONDS, EXTENSION_SECONDS)
        self.assertGreater(waits.action_request_seconds(False, 10000, None, waits.NAVIGATE_REQUEST_SECONDS),
                           EXTENSION_SECONDS)

    def test_mcp_and_core_navigate_requests_outlast_the_extension(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_navigate("https://a.test/", tab_id=7)
            self.assertEqual(post.call_args.args[0], "/api/navigate")
            self.assertEqual(post.call_args.kwargs, {"timeout": waits.NAVIGATE_REQUEST_SECONDS})
            tools.browser_navigate("https://a.test/", tab_id=7, expect={"url_changed": True, "timeout": 8000})
            self.assertEqual(post.call_args.kwargs, {"timeout": waits.NAVIGATE_REQUEST_SECONDS + 8 + 45})
            playwright_tools.browser_navigate(url="https://a.test/", tab_id=7)
            self.assertEqual(post.call_args.args[0], "/api/navigate")
            self.assertGreater(post.call_args.kwargs["timeout"], EXTENSION_SECONDS)

    def test_python_api_navigate_request_outlasts_the_extension(self):
        browser = Mock()
        Tab(browser, 7).navigate("https://a.test/")
        self.assertEqual(browser.request.call_args.args[0], "/api/navigate")
        self.assertGreater(browser.request.call_args.kwargs["timeout"], EXTENSION_SECONDS)

    def test_bridge_waits_for_a_direct_or_batch_navigate_past_the_extension(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value={"url": "https://a.test/", "tabId": 7})
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))

        async def navigate(action, params=None, timeout=30):
            return {"ok": True, "url": params["url"]}

        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_audit"), \
             patch.object(bridge, "send_to_extension", side_effect=navigate) as send:
            handler.handle_navigate()
        self.assertGreater(send.call_args.kwargs["timeout"], EXTENSION_SECONDS)
        self.assertGreater(handler.run_async.call_args.kwargs["timeout"], EXTENSION_SECONDS)
        self.assertGreater(bridge._dispatch_timeout("navigate", {"url": "https://a.test/"}), EXTENSION_SECONDS)


if __name__ == "__main__":
    unittest.main()
