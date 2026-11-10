"""The bounded-snapshot MCP helper must not report activity to the user's bridge."""
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server
import test_real_browser_bounded_snapshot as bounded


class BoundedSnapshotIsolationTests(unittest.TestCase):
    def test_core_helper_routes_activity_to_the_fixture(self):
        calls = []

        def fixture(endpoint, data=None):
            calls.append(endpoint)
            if endpoint == "/api/snapshot":
                return {"snapshot": '- heading "Fixture"', "url": "http://fixture.invalid/", "title": "Fixture"}
            if endpoint == "/api/mcp-activity":
                return {"ok": True}
            raise AssertionError(f"Unexpected fixture endpoint: {endpoint}")

        helper = bounded.RealBrowserBoundedSnapshotTests()
        helper.call = fixture
        # Never contact a real/default bridge, even while demonstrating the regression.
        # The server catches best-effort activity errors, so also assert no attempted call.
        with patch.object(playwright_tools, "_current_tab", 7), \
                patch.object(server._client, "post", side_effect=AssertionError("default activity client reached")) as default:
            result = helper.core("browser_snapshot")
        default.assert_not_called()
        self.assertEqual(result["title"], "Fixture")
        self.assertEqual(calls, ["/api/snapshot", "/api/mcp-activity"])


if __name__ == "__main__":
    unittest.main()
