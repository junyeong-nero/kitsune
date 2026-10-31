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
    env = {**{k: v for k, v in os.environ.items() if k not in ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS")},
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
        # Tabs the agent opens in a batch or from its current tab become current.
        self.assertIn("an action on your current tab opens", text)
        self.assertIn("becomes your current tab; the result names it in current_tab", text)
        # #326: the tab the user is viewing is held, unless this client turned the hold off.
        self.assertIn("tab the user is viewing fail with needs_user (detail user_viewing) unless you opened that tab", text)
        self.assertNotIn("user_viewing", instructions("act", KITSUNE_HOLD_VIEWED_TAB="0"))
        # #326: navigations on user tabs are refused, unless this client turned the protection off.
        self.assertIn("forward on a tab no client opened fail with needs_user (detail user_tab): open a tab of your own",
                      text)
        self.assertNotIn("user_tab", instructions("act", KITSUNE_PROTECT_USER_TABS="0"))
        # #376: approvals come through the client's prompt; the confirm token only without one.
        self.assertIn("A needs_user result did nothing: tell the user why (state.message).", text)
        self.assertIn("An approval (state.reason approval) is asked in your MCP client's own prompt", text)
        self.assertIn("only a client without that prompt gets state.confirmToken: ask the user, then repeat the call "
                      "with confirm_token.", text)
        self.assertNotIn("repeating the call with its confirm_token", text)
        self.assertIn("browser_resume(call, resume_token) continues a result that has a resume_token", text)
        self.assertIn("call browser_ask_user.", text)
        for settings in ({"KITSUNE_HOLD_VIEWED_TAB": "0"}, {"KITSUNE_PROTECT_USER_TABS": "0"}):
            with self.subTest(**settings):
                self.assertIn("state.confirmToken", instructions("act", **settings))
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
                self.assertIn("fail with needs_user (detail user_viewing); work in your own tab", text)
                self.assertIn("no client opened fail with needs_user (detail user_tab); work in your own tab", text)
                self.assertNotIn("confirm_token", text)
                # #376: read and draft cannot approve or resume, but can ask the user.
                self.assertIn("A needs_user result did nothing: tell the user why (state.message).", text)
                self.assertNotIn("browser_resume", text)
                self.assertIn("call browser_ask_user.", text)
                # Only act mode can click a page's hand-off control.
                self.assertNotIn("handing a step to the user", text)


if __name__ == "__main__":
    unittest.main()
