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


def instructions(**settings):
    env = {**{k: v for k, v in os.environ.items() if k != "KITSUNE_USER_TABS"},
           "KITSUNE_SCRIPTING": "0", **settings}
    result = subprocess.run([sys.executable, "-c", SCRIPT], env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class ServerInstructionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = {setting: instructions(KITSUNE_USER_TABS=setting) for setting in ("read-only", "writable")}

    def test_permission_rule_recovery_left_to_hints(self):
        for setting, text in self.texts.items():
            with self.subTest(setting=setting):
                self.assertIn("browser_ask_user", text)
                self.assertIn("an MCP restart clears current", text)
                self.assertIn("reads use the viewed tab and writes/navigation need tab_id", text)
                if setting == "read-only":
                    # #449: every tab is readable; select takes any tab, only writes are gated.
                    self.assertIn("Every tab is readable; writes, scroll, hover and focus need your own tab.", text)
                else:
                    self.assertNotIn("readable", text)
                self.assertNotIn("read-only", text)
                # Legacy viewed-tab approval is gone; how to answer needs_user, user_active, user_tab_read_only
                # and no_current_tab is in those results' hints (#405).
                for left_to_hints in ("user_viewing", "hold", "ownTabs", "confirm_token", "browser_resume",
                                      "needs_user", "user_active", "user_tab_read_only", "no_current_tab"):
                    self.assertNotIn(left_to_hints, text)

    def test_explains_background_current_tabs_and_handoff(self):
        for setting in ("read-only", "writable"):
            with self.subTest(setting=setting):
                text = self.texts[setting]
                self.assertIn("browser_tabs new opens a background current tab", text)
                self.assertIn("select makes any tab current without switching the user's view", text)
                self.assertIn("Until new/select", text)
                self.assertIn("Batch/workflow newTab and tabs opened from current become current.", text)
                self.assertIn("browser_tabs request asks the user to hand over a tab (DOM writes, no native input)",
                              text)
                # #352: prefer page controls to asking in chat when handing a step to the user.
                self.assertIn("Prefer page controls, else browser_ask_user, to hand a step to the user.", text)

    def test_removed_opt_out_aliases_keep_the_read_only_rule(self):
        # #446: the aliases no longer opt out, so the instructions still state the read-only rule.
        for setting in ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS"):
            with self.subTest(setting=setting):
                self.assertIn("Every tab is readable", instructions(**{setting: "0"}))


if __name__ == "__main__":
    unittest.main()
