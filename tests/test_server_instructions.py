"""MCP server instructions: how the shared browser behaves, sent once at initialize."""
import json
import os
import subprocess
import sys
import unittest

SCRIPT = '''import json
from kitsune import server
print(json.dumps(server.mcp._mcp_server.create_initialization_options().instructions))
'''


def instructions(mode):
    env = {**os.environ, "KITSUNE_MODE": mode}
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class ServerInstructionsTests(unittest.TestCase):
    def test_act_mode_explains_background_tabs(self):
        text = instructions("act")
        self.assertIn("opens a tab in the background", text)
        self.assertIn("without switching the user's view", text)
        # #303: without a current tab, writes are refused instead of acting on the user's tab.
        self.assertIn("reads act on the tab the user is viewing", text)
        self.assertIn("no_current_tab", text)
        # Tabs the agent opens in a batch or from its current tab become current.
        self.assertIn("an action on your current tab opens", text)
        self.assertIn("becomes your current tab; the result names it in current_tab", text)
        # #352: prefer the page's own hand-off control to asking the user in chat.
        self.assertIn("When a page offers its own control for handing a step to the user, use that "
                      "control rather than asking in chat.", text)

    def test_restricted_modes_name_no_tab_tools(self):
        # read and draft publish browser_tab_list instead of browser_tabs.
        for mode in ("read", "draft"):
            with self.subTest(mode=mode):
                text = instructions(mode)
                self.assertNotIn("browser_tabs", text)
                self.assertIn("act on the tab the user is viewing", text)
                self.assertIn("browser_tab_list new_tab opens a background tab", text)
                self.assertIn("no_current_tab", text)
                # Only act mode can click a page's hand-off control.
                self.assertNotIn("handing a step to the user", text)


if __name__ == "__main__":
    unittest.main()
