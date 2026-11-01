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

    def test_permission_rule_and_recovery_in_every_mode_and_setting(self):
        for (mode, setting), text in self.texts.items():
            with self.subTest(mode=mode, setting=setting):
                self.assertIn("On your own tab, user_active: wait a few seconds and retry.", text)
                self.assertIn("needs_user error: nothing ran; show state.message.", text)
                self.assertIn("Need the user? browser_ask_user.", text)
                self.assertIn("MCP restart clears current", text)
                self.assertIn("reads use the viewed tab", text)
                self.assertIn("writes/navigation without tab_id fail no_current_tab", text)
                if setting == "read-only":
                    self.assertIn("User and other clients' tabs are read-only (user_tab_read_only, no prompt)", text)
                    self.assertIn("open your own before writes, scroll, hover, focus or select", text)
                else:
                    self.assertNotIn("read-only", text)
                    self.assertNotIn("user_tab_read_only", text)
                # Legacy viewed-tab approval is gone, not needs_user for other approvals/user help.
                for obsolete in ("user_viewing", "hold", "ownTabs", "confirm_token", "browser_resume"):
                    self.assertNotIn(obsolete, text)

    def test_act_mode_explains_background_current_tabs_and_handoff(self):
        for setting in ("read-only", "writable"):
            with self.subTest(setting=setting):
                text = self.texts["act", setting]
                self.assertIn("browser_tabs new opens a background current tab", text)
                self.assertIn("select sets current without switching the user's view", text)
                self.assertIn("until new/select", text)
                self.assertIn("Batch/workflow newTab or tabs opened from current become current (current_tab)", text)
                self.assertIn("browser_tabs request needs approval: session-only DOM/browser handoff, no native input", text)
                # #352: prefer page controls to asking in chat when handing a step to the user.
                self.assertIn("Prefer page controls to hand a step to the user.", text)

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
                    self.assertIn("until new_tab", text)
                    for unavailable in ("browser_tabs", "handoff", "Prefer page controls"):
                        self.assertNotIn(unavailable, text)


if __name__ == "__main__":
    unittest.main()
