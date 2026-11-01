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


def instructions(mode, **settings):
    settings_names = ("KITSUNE_USER_TABS", "KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS")
    env = {**{k: v for k, v in os.environ.items() if k not in settings_names},
           "KITSUNE_MODE": mode, **settings}
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
        # A restarted server has no current tab (#325); its no_current_tab refusal lists the client's
        # earlier tabs in state.ownTabs, so the instructions do not repeat that (#405).
        self.assertIn("Until you open or select a tab, also after an MCP server restart,", text)
        self.assertNotIn("ownTabs", text)
        # Tabs the agent opens in a batch or from its current tab become current.
        self.assertIn("an action on your current tab opens", text)
        self.assertIn("becomes your current tab; the result names it in current_tab", text)
        self.assertIn("User and other clients' tabs are read-only", text)
        self.assertIn("user_tab_read_only, without prompting; open your own tab first.", text)
        for setting in ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS"):
            self.assertNotIn("read-only", instructions("act", **{setting: "0"}))
        # #376: a needs_user result did nothing; the user can be asked. #386: how to approve (confirm token)
        # or resume is in the result's hint, not here.
        self.assertIn("A needs_user result did nothing: tell the user why (state.message).", text)
        self.assertIn("If only the user can go on (a sign-in, a CAPTCHA, a choice), call browser_ask_user.", text)
        self.assertNotIn("confirm", text)
        self.assertNotIn("browser_resume", text)
        # #352: prefer the page's own hand-off control to asking the user in chat.
        self.assertIn("Use a page's own control for handing a step to the user rather than asking in chat.", text)

    def test_restricted_modes_name_no_tab_tools(self):
        # read and draft publish browser_tab_list instead of browser_tabs.
        for mode in ("read", "draft"):
            with self.subTest(mode=mode):
                text = instructions(mode)
                self.assertNotIn("browser_tabs", text)
                self.assertIn("act on the tab the user is viewing", text)
                self.assertIn("browser_tab_list new_tab opens a background tab", text)
                self.assertIn("no_current_tab", text)
                self.assertIn("User and other clients' tabs are read-only", text)
                self.assertNotIn("confirm_token", text)
                # #376: read and draft cannot approve or resume, but can ask the user.
                self.assertIn("A needs_user result did nothing: tell the user why (state.message).", text)
                self.assertNotIn("browser_resume", text)
                self.assertIn("call browser_ask_user.", text)
                # Only act mode can click a page's hand-off control.
                self.assertNotIn("handing a step to the user", text)


if __name__ == "__main__":
    unittest.main()
