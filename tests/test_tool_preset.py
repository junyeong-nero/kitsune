"""KITSUNE_TOOL_PRESET (#31): lean is the default, what it drops, and that read + draft flows stay covered.

This checks the registered catalog and schemas only. Whether the preset changes task
success, safety, calls or input per model turn is browser-mcp-bench's A/B, not tested here.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from kitsune import server

ROOT = Path(__file__).resolve().parent.parent

# Every core tool the lean preset drops, with the lean (tool, parameter) that covers the
# same need, or None when only KITSUNE_TOOL_PRESET=full can do it. docs/guide.md lists the same.
LEAN_DROPPED = {
    "browser_check": ("browser_form_fill", "fields"),  # sets checkbox/radio state; click toggles
    "browser_submit_form": ("browser_click", "expect"),  # click the form's submit button
    "browser_back": ("browser_navigate", "url"),  # loses history state; reloads the URL
    "browser_forward": ("browser_navigate", "url"),
    "browser_markdown": ("browser_snapshot", "text"),  # plain text, no Markdown structure
    "browser_wait_for": ("browser_click", "expect"),  # waits tied to an action, not standalone
    "browser_hover": None,
    "browser_keypress": None,
    "browser_scroll": None,
    "browser_handle_dialog": None,
    "browser_upload_file": None,
    "browser_js": None,
    "browser_watch_console": None,
    "browser_console_logs": None,
    "browser_workflows": None,
    "browser_workflow": None,
}

# Read flow: find or open the page, then read it. Draft flow: fill fields without submitting.
READ_FLOW = {
    "browser_status": set(),
    "browser_tabs": {"action", "tab_id", "url", "wait"},
    "browser_navigate": {"url", "tab_id", "expect", "confirm_token"},
    "browser_page_text": {"tab_id"},
    "browser_snapshot": {"tab_id", "selector", "ref", "max_depth", "text"},
    "browser_page_info": {"tab_id"},
    "browser_forms": {"tab_id"},
    "browser_dom": {"tab_id"},
    "browser_find": {"query", "tab_id"},
    "browser_screenshot": {"selector", "ref", "full_page", "save_to", "tab_id"},
}
DRAFT_FLOW = {
    "browser_click": {"selector", "ref", "element", "wait", "expect", "confirm_token", "tab_id"},
    "browser_type": {"selector", "ref", "element", "text", "mode", "clear", "wait", "expect",
                     "confirm_token", "tab_id"},
    "browser_select_option": {"selector", "ref", "element", "value", "by_text", "wait", "expect",
                              "confirm_token", "tab_id"},
    "browser_form_fill": {"fields", "expect", "confirm_token", "tab_id"},
}


def list_tools(caps="core", preset=None) -> dict:
    env = {**os.environ, "KITSUNE_CAPS": caps}
    env.pop("KITSUNE_TOOL_PRESET", None)
    if preset is not None:
        env["KITSUNE_TOOL_PRESET"] = preset
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio,json; from kitsune.server import mcp; print(json.dumps("
         "{t.name: t.model_dump(mode='json', exclude_none=True) for t in asyncio.run(mcp.list_tools())}))"],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


# Calls known and unknown tool names through FastMCP's call_tool, and browser_status with the bridge mocked.
UNPUBLISHED_SCRIPT = """import asyncio, json, sys
from unittest.mock import patch
from kitsune import server
def call(name):
    try:
        result = asyncio.run(server.mcp.call_tool(name, {}))
    except Exception as error:
        return {"raised": str(error)}
    return {"isError": result.isError, **json.loads(result.content[0].text)}
with patch.object(server, "_get", return_value={"status": "running"}):
    status = server.browser_status()
print(json.dumps({"calls": {name: call(name) for name in sys.argv[1:]}, "mcp_tools": status["mcp_tools"],
                  "unpublished": sorted(server.UNPUBLISHED_TOOLS)}))
"""


def unpublished(caps, preset, *names) -> dict:
    env = {**os.environ, "KITSUNE_CAPS": caps}
    env.pop("KITSUNE_TOOL_PRESET", None)
    if preset is not None:
        env["KITSUNE_TOOL_PRESET"] = preset
    result = subprocess.run([sys.executable, "-c", UNPUBLISHED_SCRIPT, *names],
                            env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def documented_drops() -> set:
    guide = (ROOT / "docs" / "guide.md").read_text()
    section = guide.split("### Lean tool preset", 1)[1].split("\n## ", 1)[0]
    first_cells = re.findall(r"^\| (`browser_[^|]+)\|", section, re.MULTILINE)
    return {name for cell in first_cells for name in re.findall(r"`(browser_\w+)`", cell)}


class LeanToolPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = list_tools(preset="full")
        cls.lean = list_tools(preset="lean")

    def test_lean_registers_exactly_the_preset(self):
        self.assertEqual(set(self.lean), server.TOOL_PRESETS["lean"])
        self.assertEqual(len(self.lean), 14)

    def test_lean_drops_exactly_the_listed_tools(self):
        self.assertEqual(set(self.core) - set(self.lean), set(LEAN_DROPPED))
        self.assertEqual(documented_drops(), set(LEAN_DROPPED))

    def test_lean_keeps_definitions_unchanged(self):
        for name, definition in self.lean.items():
            self.assertEqual(definition, self.core[name], name)

    def test_lean_covers_read_and_draft_flows(self):
        self.assertEqual(set(READ_FLOW) | set(DRAFT_FLOW), set(self.lean))
        for name, parameters in {**READ_FLOW, **DRAFT_FLOW}.items():
            schema = self.lean[name]["inputSchema"]["properties"]
            self.assertLessEqual(parameters, set(schema), name)
        self.assertEqual(set(self.lean["browser_tabs"]["inputSchema"]["properties"]["action"]["enum"]),
                         {"list", "new", "close", "select"})
        self.assertEqual(set(self.lean["browser_type"]["inputSchema"]["properties"]["mode"]["enum"]),
                         {"type", "fill", "editable"})

    def test_dropped_tool_fallbacks_are_in_lean(self):
        for dropped, fallback in LEAN_DROPPED.items():
            if fallback is not None:
                name, parameter = fallback
                self.assertIn(parameter, self.lean[name]["inputSchema"]["properties"], dropped)

    def test_lean_narrows_core_only(self):
        devtools = set(list_tools("devtools"))
        self.assertEqual(set(list_tools("core,devtools", preset="lean")), set(self.lean) | devtools)
        self.assertEqual(set(list_tools("devtools", preset=" LEAN ")), devtools)
        self.assertEqual(set(list_tools("devtools", preset="full")), devtools)

    def test_unknown_preset_fails_at_startup(self):
        env = {**os.environ, "KITSUNE_TOOL_PRESET": "tiny"}
        result = subprocess.run([sys.executable, "-c", "import kitsune.server"],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown KITSUNE_TOOL_PRESET: tiny", result.stderr)

    def test_lean_is_the_default(self):
        self.assertEqual(len(self.core), 30)
        for preset in (None, "", " "):
            self.assertEqual(set(list_tools(preset=preset)), set(self.lean), repr(preset))
        self.assertEqual(set(list_tools("core,devtools")), set(self.lean) | set(list_tools("devtools")))

    def test_preset_and_caps_combinations(self):
        everything = list_tools("all", preset="full")
        self.assertEqual(len(everything), 96)
        for caps, preset, expected in [
            ("core", None, set(self.lean)),
            ("all", None, set(everything)),  # all means every tool unless a preset narrows core
            ("all", "lean", set(everything) - set(LEAN_DROPPED)),
            ("core", "full", set(self.core)),
            ("all", "full", set(everything)),
        ]:
            self.assertEqual(set(list_tools(caps, preset)), expected, (caps, preset))

    def test_tool_preset_rules(self):
        for preset, caps, expected in [
            (None, {"core"}, "lean"), ("", {"core", "devtools"}, "lean"), (None, {"all"}, "full"),
            (" Lean ", {"all"}, "lean"), ("full", {"core"}, "full"), ("FULL", {"all"}, "full"),
        ]:
            self.assertEqual(server.tool_preset(preset, frozenset(caps)), expected, (preset, caps))
        with self.assertRaisesRegex(ValueError, "Unknown KITSUNE_TOOL_PRESET: tiny"):
            server.tool_preset("tiny", frozenset({"all"}))


class UnpublishedToolTests(unittest.TestCase):
    """#171: a known tool left out by KITSUNE_CAPS or the preset names the setting that publishes it."""

    def assert_not_published(self, call, group, hint):
        self.assertEqual(call, {
            "isError": True, "error": call["error"], "code": "tool_not_published", "retryable": False,
            "hint": f"Set {hint} in the MCP server environment, then restart the MCP client.",
            "state": {**call["state"], "group": group}})
        self.assertIn("is not published", call["error"])

    def test_default_names_the_preset_or_the_group(self):
        result = unpublished("core", None, "browser_scroll", "browser_drag", "browser_double_click",
                             "browser_history_search", "browser_nope")
        calls = result["calls"]
        self.assert_not_published(calls["browser_scroll"], "core", "KITSUNE_TOOL_PRESET=full")
        self.assertEqual(calls["browser_scroll"]["state"], {"group": "core", "preset": "lean", "caps": ["core"]})
        # drag and double-click stay in devtools, so the full preset alone does not publish them.
        for name in ("browser_drag", "browser_double_click"):
            self.assert_not_published(calls[name], "devtools", "KITSUNE_CAPS=core,devtools")
        self.assert_not_published(calls["browser_history_search"], "history", "KITSUNE_CAPS=core,history")
        self.assertEqual(calls["browser_nope"], {"raised": "Unknown tool: browser_nope"})

        published = set(list_tools())
        everything = set(list_tools("all,history", preset="full"))
        self.assertFalse(published & set(result["unpublished"]))
        self.assertEqual(published | set(result["unpublished"]), everything)
        self.assertEqual(result["mcp_tools"], {"preset": "lean", "caps": ["core"],
                                                "unpublished": len(everything) - 14})

    def test_core_tool_outside_caps_and_preset_names_both(self):
        result = unpublished("devtools", "lean", "browser_scroll", "browser_click")
        self.assert_not_published(result["calls"]["browser_scroll"], "core",
                                  "KITSUNE_CAPS=core,devtools and KITSUNE_TOOL_PRESET=full")
        self.assert_not_published(result["calls"]["browser_click"], "core", "KITSUNE_CAPS=core,devtools")

    def test_all_full_leaves_only_history_unpublished(self):
        result = unpublished("all", "full", "browser_bookmarks_search")
        self.assert_not_published(result["calls"]["browser_bookmarks_search"], "history",
                                  "KITSUNE_CAPS=all,history")
        self.assertEqual(result["unpublished"],
                         ["browser_bookmarks_search", "browser_history_search", "browser_history_visits"])
        self.assertEqual(result["mcp_tools"], {"preset": "full", "caps": ["all"], "unpublished": 3})


if __name__ == "__main__":
    unittest.main()
