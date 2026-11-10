"""The #499 design inventory accounts for registrations, not only today's default list."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from kitsune import tools
from kitsune.capabilities import ACTION_GROUPS, OPEN_ROUTES, ROUTE_GROUPS
from scripts.toolset_inventory import _batch_actions, collect

ROOT = Path(__file__).resolve().parent.parent


class ToolsetInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("kitsune.bridge_client.httpx.request", side_effect=AssertionError("inventory must not connect")):
            cls.inventory = collect()

    def test_all_registered_tools_and_gateway_are_accounted_for(self):
        self.assertEqual(set(self.inventory["tools"]), set(tools.TOOLS))
        self.assertEqual(set(self.inventory["gateway"]),
                         {"browser_tools_search", "browser_tools_call", "browser_tools_publish"})
        self.assertEqual(self.inventory["tools"]["browser_tab_list"]["modes"], ["draft", "read"])
        self.assertIn("browser_run_code_unsafe", self.inventory["tools"], "unsupported public entries still count")

    def test_http_methods_and_capability_exempt_routes_are_not_lost(self):
        routes = self.inventory["http"]
        self.assertIn("GET /api/tabs", routes)
        self.assertIn("POST /api/tabs", routes)
        self.assertIn("POST /api/request-tab", routes)
        self.assertEqual({name.split(" ", 1)[1] for name in routes}, set(ROUTE_GROUPS) | OPEN_ROUTES)
        self.assertTrue(routes["GET /view"]["capability_exempt"])
        self.assertFalse(routes["POST /api/cookies"]["capability_exempt"])
        self.assertEqual(routes["POST /api/cookies"]["groups"], ["storage"])

    def test_python_developer_escape_hatches_and_sensitive_namespaces_are_visible(self):
        api = self.inventory["python"]
        for name in ("connect", "Browser.request", "Tab.evaluate", "Policy.set", "Cookies.get",
                     "Clipboard.read", "History.search", "Bookmarks.search", "PageStorage.items",
                     "Tab.screenshot", "Tab.wait_for", "Browser.active_tab"):
            self.assertIn(name, api)
        self.assertIn("confirm", api["connect"]["signature"])
        self.assertNotIn("Browser._send", api)
        self.assertTrue(all("signature" in entry for entry in api.values()))

    def test_container_aliases_and_control_flow_are_explicit(self):
        actions = self.inventory["container"]
        self.assertEqual(actions["js"]["operation"], "executeJS")
        self.assertNotIn("executeJS", actions)
        self.assertEqual(actions["snapshot"]["operation"], "getSnapshot")
        self.assertIn("while", actions)
        self.assertIn("try", actions)
        self.assertEqual({entry["operation"] for entry in actions.values()}, set(ACTION_GROUPS))

    def test_changed_identity_comprehension_requires_review(self):
        tree = ast.parse("BATCH_ACTION_MAP = {**{name: 'different' for name in ('click',)}}")
        with self.assertRaisesRegex(ValueError, "Review changed"):
            _batch_actions(tree)

    def test_cli_summary_matches_entries(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/toolset_inventory.py")],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {name: len(entries) for name, entries in self.inventory.items()})


if __name__ == "__main__":
    unittest.main()
