"""MCP server instructions: shared-browser permissions and recovery, sent at initialize."""
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
    @classmethod
    def setUpClass(cls):
        cls.texts = {(mode, setting): instructions(mode, KITSUNE_USER_TABS=setting)
                     for mode in ("act", "read", "draft") for setting in ("read-only", "writable")}

    def test_permission_rule_in_every_mode_recovery_left_to_hints(self):
        for (mode, setting), text in self.texts.items():
            with self.subTest(mode=mode, setting=setting):
                self.assertIn("browser_ask_user", text)
                self.assertIn("an MCP restart clears current", text)
                self.assertIn("reads use the viewed tab and writes/navigation need tab_id", text)
                if setting == "read-only":
                    self.assertIn("User and other clients' tabs are read-only: open your own before writes, "
                                  "scroll, hover, focus or select.", text)
                else:
                    self.assertNotIn("read-only", text)
                # Legacy viewed-tab approval is gone; how to answer needs_user, user_active, user_tab_read_only
                # and no_current_tab is in those results' hints (#405).
                for left_to_hints in ("user_viewing", "hold", "ownTabs", "confirm_token", "browser_resume",
                                      "needs_user", "user_active", "user_tab_read_only", "no_current_tab"):
                    self.assertNotIn(left_to_hints, text)

    def test_act_mode_explains_background_current_tabs_and_handoff(self):
        for setting in ("read-only", "writable"):
            with self.subTest(setting=setting):
                text = self.texts["act", setting]
                self.assertIn("browser_tabs new opens a background current tab", text)
                self.assertIn("select sets current without switching the user's view", text)
                self.assertIn("Until new/select", text)
                self.assertIn("Batch/workflow newTab and tabs opened from current become current.", text)
                self.assertIn("browser_tabs request asks the user to hand over a tab (DOM writes, no native input)",
                              text)
                # #352: prefer page controls to asking in chat when handing a step to the user.
                self.assertIn("Prefer page controls, else browser_ask_user, to hand a step to the user.", text)

    def test_deprecated_opt_out_aliases_do_not_advertise_read_only(self):
        for setting in ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS"):
            with self.subTest(setting=setting):
                self.assertNotIn("read-only", instructions("act", **{setting: "0"}))

    def test_restricted_modes_do_not_advertise_unavailable_tab_actions(self):
        for mode in ("read", "draft"):
            for setting in ("read-only", "writable"):
                with self.subTest(mode=mode, setting=setting):
                    text = self.texts[mode, setting]
                    self.assertIn("browser_tab_list new_tab opens a background current tab", text)
                    self.assertIn("Until new_tab", text)
                    self.assertIn("Need the user? browser_ask_user.", text)
                    for unavailable in ("browser_tabs", "handoff", "Prefer page controls"):
                        self.assertNotIn(unavailable, text)


if __name__ == "__main__":
    unittest.main()
