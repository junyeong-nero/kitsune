"""The #499 design inventory accounts for registrations, not only today's default list."""
import ast
from copy import deepcopy
import inspect
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from kitsune import tools
from kitsune.capabilities import ACTION_GROUPS, OPEN_ROUTES, ROUTE_GROUPS
from scripts.toolset_inventory import _batch_actions, collect, mapping_errors

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

    def test_legacy_python_helpers_are_not_confused_with_registered_tools(self):
        expected = {name for name, function in vars(tools).items()
                    if name.startswith("browser_") and inspect.isfunction(function)
                    and function.__module__ == tools.__name__}
        self.assertEqual(set(self.inventory["legacy_python"]), expected)
        for name in ("browser_js", "browser_screenshot", "browser_form_fill", "browser_upload_file"):
            self.assertIn(name, self.inventory["legacy_python"])
            self.assertNotIn(name, self.inventory["tools"])

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

    def test_explicit_dispositions_cover_every_surface_once(self):
        mapping = json.loads((ROOT / "docs/design/499-capability-map.json").read_text())
        self.assertEqual(mapping_errors(self.inventory, mapping), [])

    def test_mapping_has_no_catch_all_for_new_or_stale_entries(self):
        mapping = json.loads((ROOT / "docs/design/499-capability-map.json").read_text())
        inventory = deepcopy(self.inventory)
        inventory["http"]["POST /api/new-operation"] = {}
        mapping["groups"][0]["entries"]["tools"].extend(["browser_tabs", "removed_tool"])
        self.assertEqual(mapping_errors(inventory, mapping), [
            "duplicate entry: tools:browser_tabs",
            "stale entry: tools:removed_tool",
            "unmapped entry: http:POST /api/new-operation",
        ])

    def test_mapping_requires_rationale_replacement_and_coverage_loss(self):
        mapping = json.loads((ROOT / "docs/design/499-capability-map.json").read_text())
        mapping["groups"][0].update(destination="unspecified", rationale="", replacement="", coverage_loss="")
        self.assertEqual(mapping_errors(self.inventory, mapping), [
            "invalid destination: tabs", "missing coverage_loss: tabs",
            "missing rationale: tabs", "missing replacement: tabs",
        ])

    def test_cli_summary_matches_entries(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/toolset_inventory.py")],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {name: len(entries) for name, entries in self.inventory.items()})


if __name__ == "__main__":
    unittest.main()
