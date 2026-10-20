"""Regression coverage for fresh tab inventories and navigation-ready tab creation (issue #28)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge


def tabs(*entries):
    return {"tabs": [{"id": i, "url": u, "active": a, "windowId": 1, "index": n}
                     for n, (i, u, a) in enumerate(entries)]}


class TabInventoryTests(unittest.TestCase):
    def setUp(self):
        bridge._cache.clear()
        self.addCleanup(bridge._cache.clear)

    def handler(self, body=None):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=body or {})
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler._tab_params = Mock(return_value={})
        return handler

    def test_inventory_is_read_fresh_with_cache_enabled(self):
        # Delayed navigation, an external URL change, selection, close and reopen all happen
        # without a bridge mutation in between, so only a fresh read can observe them.
        states = [
            tabs((1, "about:blank", True)),
            tabs((1, "https://example.com/after-redirect", True)),
            tabs((1, "https://example.com/changed-externally", True)),
            tabs((1, "https://example.com/changed-externally", False), (2, "https://a.test/", True)),
            tabs((2, "https://a.test/", True)),
            tabs((2, "https://a.test/", True), (3, "https://a.test/reopened", False)),
        ]
        self.assertGreater(bridge.CACHE_TTL, 0)
        handler = self.handler()
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          side_effect=states) as send:
            for _ in states:
                handler.handle_tabs()
        self.assertEqual(send.await_count, len(states))
        self.assertEqual([c.args[1] for c in handler.send_json.call_args_list], states)
        self.assertEqual(bridge._cache, {})

    def test_page_text_cache_is_unchanged(self):
        handler = self.handler()
        page = {"text": "hello"}
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value=page) as send:
            handler.handle_page_text()
            handler.handle_page_text()
        send.assert_awaited_once()
        self.assertEqual(handler.send_json.call_args.args[1], {**page, "_cached": True})


class NewTabWaitTests(unittest.TestCase):
    def handler(self, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        return handler

    def send_new_tab(self, body):
        handler = self.handler(body)
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_audit"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True, "tabId": 4}) as send:
            handler.handle_new_tab()
        return send, handler

    def test_immediate_mode_keeps_default_timeout(self):
        send, handler = self.send_new_tab({"url": "https://example.com"})
        send.assert_awaited_once_with("newTab", {"url": "https://example.com"}, timeout=35)
        self.assertEqual(handler.run_async.call_args.kwargs["timeout"], 40)

    def test_wait_mode_extends_transport_timeout_to_the_wait_bound(self):
        for body, expected in [
            ({"url": "https://example.com", "wait": True}, 20),
            ({"url": "https://example.com", "wait": True, "timeout": 60000}, 65),
        ]:
            with self.subTest(body=body):
                send, handler = self.send_new_tab(body)
                send.assert_awaited_once_with("newTab", body, timeout=expected)
                self.assertEqual(handler.run_async.call_args.kwargs["timeout"], expected + 5)

    def test_batch_new_tab_wait_extends_timeout(self):
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True}) as send:
            asyncio.run(bridge.run_command(
                {"action": "newTab", "url": "https://example.com", "wait": True, "timeout": 30000}))
            asyncio.run(bridge.run_command({"action": "newTab", "url": "https://example.com"}))
        self.assertEqual(send.await_args_list[0].kwargs, {"timeout": 35})
        self.assertEqual(send.await_args_list[1].kwargs, {})


if __name__ == "__main__":
    unittest.main()
