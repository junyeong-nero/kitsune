"""The real-browser harness's launch guard (issue #96); runs without a browser."""
from pathlib import Path
import shutil
import tempfile
import unittest

import real_browser_harness as harness


class ExtensionCopyGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def prepared_browser(self, ws_port=40000):
        # Prepares the copy and profile only; nothing is launched.
        return harness.RealBrowser("unused-binary", self.root, ws_port, harness.POPUPS_ALLOWED,
                                   {"extra.js": "'use strict';\n"})

    def test_prepared_copy_targets_the_test_bridge_and_passes(self):
        browser = self.prepared_browser()
        background = (browser.extension / "background" / "background.js").read_text(encoding="utf-8")
        self.assertIn("'ws://127.0.0.1:40000'", background)
        self.assertIn("background/extra.js", (browser.extension / "manifest.json").read_text(encoding="utf-8"))
        prefs = (browser.profile / "user.js").read_text(encoding="utf-8")
        self.assertIn('user_pref("dom.disable_open_during_load", false);', prefs)
        harness.check_extension_copy(browser.extension, 40000)

    def test_unmodified_extension_is_refused(self):
        with self.assertRaisesRegex(AssertionError, "unmodified extension"):
            harness.check_extension_copy(harness.REPO / "extension", 40000)

    def test_copy_still_naming_the_default_port_is_refused(self):
        extension = self.root / "extension"
        shutil.copytree(harness.REPO / "extension", extension)
        with self.assertRaisesRegex(AssertionError, "default bridge port"):
            harness.check_extension_copy(extension, 40000)

    def test_any_script_naming_the_default_port_is_refused(self):
        browser = self.prepared_browser()
        (browser.extension / "content" / "late.js").write_text("fetch('http://127.0.0.1:47652')", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "late.js still names the default bridge port"):
            harness.check_extension_copy(browser.extension, 40000)

    def test_bridge_on_the_default_port_is_refused_before_launch(self):
        browser = self.prepared_browser(ws_port=47652)
        with self.assertRaisesRegex(AssertionError, "test bridge must not use the default bridge port"):
            browser.start()
        self.assertIsNone(browser.process)


if __name__ == "__main__":
    unittest.main()
