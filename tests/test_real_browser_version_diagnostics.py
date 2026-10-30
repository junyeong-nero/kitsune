"""Read-only runtime/installed build comparison on an isolated browser/bridge (#366).

Opt-in via KITSUNE_E2E_BROWSER; see real_browser_harness.py. No user profile or bridge.
The macOS test compares the live extension's Gecko build ID with its own app bundle;
other platforms assert an explicit unsupported result rather than a false healthy one.
"""

import sys
import time
import unittest

import real_browser_harness as harness


class RealBrowserVersionDiagnosticsTests(harness.RealBrowserTestCase):
    def test_diagnostics_identifies_the_connected_browser_without_touching_tabs(self):
        before = self.call("/api/tabs")["tabs"]
        normal = self.call("/api/status")
        self.assertNotIn("browser_diagnostics", normal)
        deadline = time.monotonic() + 5
        while True:
            result = self.call("/api/status?diagnostics=true")
            detail = result["browser_diagnostics"]
            if detail.get("reason") != "runtime_info_unavailable" or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertEqual(result["diagnosis"]["code"], "connected", result)
        self.assertTrue(detail["runtime"]["version"], detail)
        self.assertEqual(detail["runtime"]["source"], "browser.runtime.getBrowserInfo")
        if sys.platform == "darwin":
            self.assertEqual(detail["status"], "match", detail)
            self.assertFalse(detail["restart_recommended"])
            self.assertEqual(detail["runtime"]["buildID"], detail["installed"]["buildID"])
            self.assertEqual(detail["process"]["source"], "websocket_peer")
        else:
            self.assertEqual(detail["status"], "unknown", detail)
            self.assertEqual(detail["reason"], "unsupported_platform")
        after = self.call("/api/tabs")["tabs"]
        self.assertEqual([(t["id"], t["active"]) for t in before],
                         [(t["id"], t["active"]) for t in after])
        self.assertNotIn(str(self.browser.profile), str(detail))
        self.assertNotIn(str(self.browser.binary), str(detail))


if __name__ == "__main__":
    unittest.main()
