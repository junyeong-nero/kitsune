"""#329: the popup a click or hover opened (opened_popup) reaches the agent unchanged, with or
without expect, through the core and Kitsune tools. The extension side is tests/opened_popup.cjs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from kitsune import tools
from test_action_outcome import TAB, FakeExtension, run_expect

ROOT = Path(__file__).resolve().parent.parent
POPUP = {"ref": "e90", "role": "menu", "name": "Video actions", "opened_by": "e41", "rendered": False,
         "items": [{"ref": "e91", "role": "menuitem", "name": "Save to Watch later"}]}

# MCP calls with the bridge mocked: each pointer action's reply carries POPUP.
CALLS = r'''import asyncio, json, sys
from unittest.mock import patch
from kitsune import server, tools
popup = json.loads(sys.argv[1])
sent = []

def post(endpoint, body, timeout=15):
    sent.append(endpoint)
    if endpoint == "/api/new-tab":
        return {"ok": True, "tabId": 42}
    return {"ok": True, "snapshot_delta": True, "opened_popup": popup}

async def main():
    results = {}
    with patch.object(tools, "_post", side_effect=post), \
         patch.object(tools, "_get", return_value={"tabs": [{"id": 42}]}):
        await server.mcp.call_tool("browser_tabs", {"action": "new"})
        for label, name, arguments in [
                ("click", "browser_click", {"target": "e41"}),
                ("double_click", "browser_click", {"target": "e41", "doubleClick": True}),
                ("hover", "browser_hover", {"target": "e41"})]:
            result = await server.mcp.call_tool(name, arguments)
            results[label] = json.loads(result.content[0].text)
    print(json.dumps({"sent": sent, "results": results}))

asyncio.run(main())
'''


class OpenedPopupToolTests(unittest.TestCase):
    def test_core_click_and_hover_return_opened_popup(self):
        env = {**os.environ, "KITSUNE_CAPS": "all", "KITSUNE_PUBLISH": "core"}
        run = subprocess.run([sys.executable, "-c", CALLS, json.dumps(POPUP)], env=env, capture_output=True,
                             text=True)
        if run.returncode:
            raise AssertionError(run.stderr[-3000:])
        output = json.loads(run.stdout.splitlines()[-1])
        self.assertEqual(output["sent"], ["/api/new-tab", "/api/click", "/api/double-click", "/api/hover"])
        for label, result in output["results"].items():
            with self.subTest(label=label):
                self.assertEqual(result["opened_popup"], POPUP)

    def test_with_expect_the_result_keeps_opened_popup(self):
        for met in (True, False):
            with self.subTest(met=met):
                extension = FakeExtension(
                    getTabs={"tabs": [TAB]}, hover={"ok": True, "opened_popup": POPUP},
                    waitForElement={"ok": True, "found": True} if met else {"ok": False, "error": "Timed out"},
                    getOpenedTabs={"opened_tabs": []})
                result = run_expect(extension, "hover", {"tabId": 7, "ref": "e41"},
                                    {"selector": "[role=menu]", "timeout": 30})
                self.assertIs(result["ok"], met)
                self.assertEqual(result["opened_popup"], POPUP)

    def test_tool_docstrings_name_opened_popup(self):
        for function in (tools.browser_check, tools.browser_click, tools.browser_hover, tools.browser_double_click):
            with self.subTest(tool=function.__name__):
                self.assertIn("opened_popup", function.__doc__)

    def test_api_reference_documents_every_field(self):
        reference = (ROOT / "docs" / "api-reference.md").read_text(encoding="utf-8")
        section = reference.split("### Opened popups", 1)[1].split("\n### ", 1)[0]
        for field in ("opened_by", "rendered", "items", "[not_rendered]", "aria-controls", "aria-expanded"):
            self.assertIn(field, section)
        self.assertIn("(#opened-popups)", reference.split("### Opened popups", 1)[0])


if __name__ == "__main__":
    unittest.main()
