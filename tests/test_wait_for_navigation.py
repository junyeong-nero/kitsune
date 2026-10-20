"""URL/title routing and transport deadlines, without a running browser/bridge."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge, server


class WaitForNavigationTests(unittest.TestCase):
    def test_url_title_http_routing_and_timeout(self):
        for condition, action in (("url", "waitForUrl"), ("title", "waitForTitle")):
            with self.subTest(condition=condition):
                handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
                handler.send_json = Mock()
                handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
                body = {"pattern": "Ready", "timeout": 60000, "tabId": 7}
                handler.read_body = Mock(return_value=body)
                with patch.object(server, "_post", return_value={"ok": True}) as post:
                    if condition == "url":
                        server.browser_wait_for(url="Ready", timeout=60000, tab_id=7)
                    else:
                        server.browser_wait_for(title="Ready", timeout=60000, tab_id=7)
                    post.assert_called_once_with("/api/wait-for-" + condition, body, timeout=70)
                with patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                                  return_value={"ok": True}) as send, \
                     patch.object(bridge, "_audit"), \
                     patch.object(bridge, "_check_policy", return_value=None):
                    handler._forward(action, default_timeout=20)
                    send.assert_awaited_once_with(action, body, timeout=65)
                    self.assertEqual(handler.run_async.call_args.kwargs["timeout"], 70)
                    handler.send_json.assert_called_once_with(200, {"ok": True})

    def test_batch_wait_deadline(self):
        for action in ("waitForUrl", "waitForTitle"):
            with self.subTest(action=action), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                              return_value={"ok": True}) as send, \
                 patch.object(bridge, "_check_policy", return_value=None):
                body = {"pattern": "Ready", "timeout": 60000, "tabId": 7}
                self.assertEqual(asyncio.run(bridge.run_command({"action": action, **body})), {"ok": True})
                send.assert_awaited_once_with(action, body, timeout=65)

    def test_batch_http_deadline(self):
        for action in ("waitForUrl", "waitForTitle"):
            with self.subTest(action=action), patch.object(server, "_post", return_value={"ok": True}) as post:
                commands = [{"action": action, "pattern": "Ready", "timeout": 60000, "tabId": 7}]
                server.browser_batch(commands)
                post.assert_called_once_with("/api/batch", {"commands": commands}, timeout=75)

    def test_mixed_navigation_and_document_conditions_still_rejected(self):
        with patch.object(server, "_post") as post:
            results = (
                server.browser_wait_for(url="done", selector="h1"),
                server.browser_wait_for(title="Ready", text="Hello"),
                server.browser_wait_for(url="done", title="Ready"),
            )
            for result in results:
                self.assertIn("exactly one", result["error"])
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
