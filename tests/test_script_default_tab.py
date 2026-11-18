"""A script without an MCP selection gets the one tab the user views as its tab, if it is the client's."""
import unittest
from unittest.mock import patch

from kitsune import server


class ViewedTabTests(unittest.TestCase):
    def viewed(self, reply):
        def request(method, route, **kwargs):
            if isinstance(reply, Exception):
                raise reply
            self.assertEqual((method, route, kwargs.get("start_bridge")), ("GET", "/api/tabs", False))
            return reply
        with patch.object(server._client, "request", side_effect=request):
            return server._viewed_tab_id()

    def test_the_single_active_own_tab(self):
        self.assertEqual(self.viewed({"tabs": [{"id": 3, "active": False}, {"id": 5, "active": True, "owned": True}]}), 5)

    def test_the_users_viewed_tab_is_not_a_default_while_user_tabs_are_read_only(self):
        listed = {"tabs": [{"id": 5, "active": True, "owned": False}]}
        with patch.object(server, "USER_TABS", "read-only"):
            self.assertIsNone(self.viewed(listed))
        with patch.object(server, "USER_TABS", "writable"):
            self.assertEqual(self.viewed(listed), 5)

    def test_unclear_or_unavailable_views_leave_tab_unset(self):
        self.assertIsNone(self.viewed({"tabs": [{"id": 3, "active": True, "owned": True},
                                                {"id": 5, "active": True, "owned": True}]}))
        self.assertIsNone(self.viewed({"tabs": []}))
        self.assertIsNone(self.viewed({"error": "down", "code": "bridge_unavailable"}))
        self.assertIsNone(self.viewed(OSError("no bridge")))


if __name__ == "__main__":
    unittest.main()
