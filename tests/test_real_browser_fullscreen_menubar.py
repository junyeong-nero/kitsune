"""What macOS shows at the top of a full-screen browser, for trusted clicks (issue #336).

A check, not a regression test: it prints what the window-at-point helper (_mac_window_at) finds
near the top of a page in native full screen and which browser_not_at_point reason a viewport
trusted click there would get, and asserts nothing about it. The menu bar (a Window Server window
at the main menu level, with Control Center's menu extras at the status level) is told apart from
the browser's own full-screen toolbar strip (other_browser_window).

Opt-in twice, like test_real_browser_trusted_keys.py: besides KITSUNE_E2E_BROWSER (see
tests/real_browser_harness.py), it runs only on macOS with KITSUNE_E2E_TRUSTED_INPUT=1, because it
puts a visible browser window in full screen (a new Space) and brings it to the front. It makes
no click and does not move the pointer, but a pointer resting at the top of the screen shows the
menu bar, so keep it away from there. Needs Quartz and Accessibility permission:

    KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen KITSUNE_E2E_TRUSTED_INPUT=1 \\
        uv run --extra macos python -m unittest discover -s tests -p test_real_browser_fullscreen_menubar.py -v

Each point prints one JSON line: `case` is menu_bar, tab_window, other_browser_window,
other_window or desktop; `refusal` is the reason _browser_not_at_point would give (null: the
click would be made); `stack` lists every on-screen window at the point, front to back.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import subprocess
import sys
import time
import unittest

import real_browser_harness as harness
from kitsune import bridge as kitsune_bridge

PAGE = """<!doctype html><title>top</title>
<style>body{margin:0} header{height:60px;background:#36c}</style><header></header><p>page</p>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def windows_at(x, y):
    """Every on-screen window containing desktop point (x, y), front to back, with `skipped` as
    _mac_window_at's filter decides (alpha 0 or the pointer's window)."""
    import Quartz
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    cursor_level = Quartz.CGWindowLevelForKey(Quartz.kCGCursorWindowLevelKey)
    stack = []
    for window in windows:
        bounds = window.get("kCGWindowBounds") or {}
        left, top = bounds.get("X", 0), bounds.get("Y", 0)
        width, height = bounds.get("Width", 0), bounds.get("Height", 0)
        if left <= x < left + width and top <= y < top + height:
            stack.append({"pid": int(window.get("kCGWindowOwnerPID") or 0),
                          "owner": str(window.get("kCGWindowOwnerName") or ""),
                          "layer": int(window.get("kCGWindowLayer") or 0),
                          "bounds": [left, top, width, height],
                          "skipped": not window.get("kCGWindowAlpha", 1) or window.get("kCGWindowLayer") == cursor_level})
    return stack


def menu_bar_levels():
    import Quartz
    return {Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey),
            Quartz.CGWindowLevelForKey(Quartz.kCGStatusWindowLevelKey)}


def pointer_location():
    import Quartz
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return [round(location.x), round(location.y)]


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("KITSUNE_E2E_TRUSTED_INPUT") == "1",
                     "puts a real browser window in full screen: set KITSUNE_E2E_TRUSTED_INPUT=1 on macOS")
class RealBrowserFullScreenMenuBarTests(harness.RealBrowserTestCase):
    HEADED = True

    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        if kitsune_bridge._mac_window_at(0, 0) is None:
            self.skipTest("needs Quartz: uv run --extra macos")

    def set_full_screen(self, on):
        """Bring this suite's browser (by pid, not by name) to the front and set its window's
        native full screen."""
        process = f"(first process whose unix id is {self.browser.process.pid})"
        script = (f'tell application "System Events"\n'
                  f'  set frontmost of {process} to true\n'
                  f'  delay 0.8\n'
                  f'  set value of attribute "AXFullScreen" of window 1 of {process} to {str(on).lower()}\n'
                  f'end tell')
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        # Entering or leaving full screen animates a Space switch.
        time.sleep(2)

    def tab_window(self, tab):
        for window in self.call("/api/windows")["windows"]:
            if any(t["id"] == tab for t in window["tabs"]):
                return window
        self.fail(f"no window holds tab {tab}")

    def probe(self, label, x, y, target_window):
        found = kitsune_bridge._mac_window_at(x, y)
        stack = windows_at(x, y)
        refusal = kitsune_bridge._browser_not_at_point(x, y, target_window=target_window)
        pid, owner, bounds = found
        front = next((w for w in stack if not w["skipped"]), None)
        layer = front["layer"] if front else None
        if not pid:
            case = "desktop"
        elif owner in ("Window Server", "Control Center") or layer in menu_bar_levels():
            case = "menu_bar"
        elif pid == self.browser.process.pid and kitsune_bridge._same_window(bounds, target_window):
            case = "tab_window"
        elif pid == self.browser.process.pid or (refusal or {}).get("state", {}).get("reason") == "other_browser_window":
            case = "other_browser_window"
        else:
            case = "other_window"
        evidence = {"point": label, "screen": [x, y], "case": case,
                    "windowAt": {"pid": pid, "owner": owner, "layer": layer, "bounds": list(bounds) if bounds else None},
                    "refusal": (refusal or {}).get("state", {}).get("reason"), "stack": stack}
        print(f"\n#336 {json.dumps(evidence)}", flush=True)

    def test_reports_the_window_at_the_top_of_a_full_screen_page(self):
        tab = self.open_tab(f"{self.site.origin}/top")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab}))
        self.addCleanup(self.set_full_screen, False)
        self.set_full_screen(True)
        window = self.tab_window(tab)
        # A precondition, not the check: without full screen the evidence says nothing.
        self.assertEqual(window["state"], "fullscreen", window)
        target = (window["left"], window["top"], window["width"], window["height"])
        reply = self.call("/api/js", {"tabId": tab, "code": "JSON.stringify(window.mozInnerScreenY)"})
        content_top = json.loads(reply["result"])
        print(f"\n#336 window {list(target)} state {window['state']}, page top at y={content_top}, "
              f"pointer at {pointer_location()}, browser pid {self.browser.process.pid}", flush=True)
        x = round(window["left"] + window["width"] / 2)
        self.probe("top y=10", x, window["top"] + 10, target)
        self.probe("just below the toolbar", x, round(content_top) + 10, target)
        self.probe("middle (control)", x, round(window["top"] + window["height"] / 2), target)


if __name__ == "__main__":
    unittest.main()
