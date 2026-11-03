"""What macOS shows at the top of a full-screen browser, for trusted clicks (issue #336).

A check, not a regression test: it prints what the window-at-point helper (_mac_window_at) finds
near the top of a page in native full screen and which browser_not_at_point reason a viewport
trusted click there would get. It asserts only what the docs state, after printing the points: at
the window's top the hidden menu bar is not the window found, and a shown one refuses. The menu bar (a Window Server window
at the main menu level, with Control Center's menu extras at the status level) is told apart from
the browser's own full-screen toolbar strip (other_browser_window).

Opt-in twice, like test_real_browser_trusted_keys.py: besides KITSUNE_E2E_BROWSER (see
tests/real_browser_harness.py), it runs only on macOS with KITSUNE_E2E_TRUSTED_INPUT=1, because it
puts a visible browser window in full screen (a new Space) and brings it to the front. It makes
no click. By default it does not move the pointer either, but a pointer resting at the top of the
screen shows the menu bar, so keep it away from there. Needs Quartz and Accessibility permission:

    KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen KITSUNE_E2E_TRUSTED_INPUT=1 \\
        uv run --extra macos python -m unittest discover -s tests -p test_real_browser_fullscreen_menubar.py -v

With KITSUNE_E2E_REVEAL_MENU_BAR=1 as well, it then shows the menu bar the way a user does: it
moves the real pointer (mouse-move events only, no button) to rest at the top edge of its own
full-screen window, probes the same points again, and moves the pointer back to where it was.
It moves the pointer only while its own browser is the frontmost app and its window is the first
window under the menu bar at that edge; otherwise the step fails without moving it. It fails too
if the menu bar's windows do not change (it did not show), rather than print mislabelled points.

Each point prints one JSON line: `point` names the point and the menu bar's state (hidden,
revealed, hidden again); `case` is menu_bar, tab_window, other_browser_window, other_window or
desktop; `refusal` is the reason _browser_not_at_point would give (null: the click would be
made); `stack` lists every on-screen window at the point, front to back; `menuBarWindows` lists
every on-screen window at the menu bar's levels over the full-screen window's display, wherever
it is, to compare the hidden and the revealed menu bar; `browserWindows` lists the suite's
browser's own on-screen windows (its full-screen window and any toolbar strip over it).

Result on macOS 26.5.1 with Zen 1.24b: the hidden menu bar is not listed; a shown one is (and
refuses); Zen's 68-point full-screen strip refuses (other_browser_window). See
docs/api-reference.md, browser_trusted_click.
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


def on_screen_windows():
    """Every on-screen window, front to back, as _mac_window_at reads them, with `skipped` as its
    filter decides (alpha 0 or the pointer's window)."""
    import Quartz
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    cursor_level = Quartz.CGWindowLevelForKey(Quartz.kCGCursorWindowLevelKey)
    listed = []
    for window in windows:
        bounds = window.get("kCGWindowBounds") or {}
        listed.append({"id": int(window.get("kCGWindowNumber") or 0),
                       "pid": int(window.get("kCGWindowOwnerPID") or 0),
                       "owner": str(window.get("kCGWindowOwnerName") or ""),
                       "layer": int(window.get("kCGWindowLayer") or 0),
                       "alpha": float(window.get("kCGWindowAlpha", 1)),
                       "bounds": [bounds.get("X", 0), bounds.get("Y", 0), bounds.get("Width", 0), bounds.get("Height", 0)],
                       "skipped": not window.get("kCGWindowAlpha", 1) or window.get("kCGWindowLayer") == cursor_level})
    return listed


def windows_at(windows, x, y):
    """Every window of `windows` (on_screen_windows()) containing desktop point (x, y)."""
    return [w for w in windows
            if w["bounds"][0] <= x < w["bounds"][0] + w["bounds"][2] and w["bounds"][1] <= y < w["bounds"][1] + w["bounds"][3]]


def menu_bar_windows(windows, display):
    """Every window of `windows` (on_screen_windows()) at the menu bar's levels whose left edge is
    on display (left, top, width, height), whatever its top."""
    levels = menu_bar_levels()
    return [w for w in windows
            if w["layer"] in levels and display[0] <= w["bounds"][0] < display[0] + display[2]]


def menu_bar_levels():
    import Quartz
    return {Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey),
            Quartz.CGWindowLevelForKey(Quartz.kCGStatusWindowLevelKey)}


def pointer_location():
    import Quartz
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return [round(location.x), round(location.y)]


def move_pointer(x, y):
    """Move the real pointer to desktop point (x, y) with one mouse-move event: no button."""
    import Quartz
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (float(x), float(y)), Quartz.kCGMouseButtonLeft))


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
        for window in self.call("/api/windows", {})["windows"]:
            if any(t["id"] == tab for t in window["tabs"]):
                return window
        self.fail(f"no window holds tab {tab}")

    def probe(self, label, x, y, target_window):
        found = kitsune_bridge._mac_window_at(x, y)
        # One list for stack, menuBarWindows and browserWindows, so they agree with each other
        # while the menu bar slides; the two bridge helpers read their own.
        windows = on_screen_windows()
        stack = windows_at(windows, x, y)
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
                    "refusal": (refusal or {}).get("state", {}).get("reason"), "stack": stack,
                    "menuBarWindows": menu_bar_windows(windows, target_window),
                    "browserWindows": [w for w in windows if w["pid"] == self.browser.process.pid]}
        print(f"\n#336 {json.dumps(evidence)}", flush=True)
        return evidence

    def probe_points(self, state, x, target, content_top):
        """The evidence of each point, the window top + 10 (within the menu bar's height) first."""
        return [self.probe(f"{state}: window top + 10", x, target[1] + 10, target),
                self.probe(f"{state}: page top + 10", x, round(content_top) + 10, target),
                self.probe(f"{state}: window top y=40 (below the menu bar's height)", x, target[1] + 40, target),
                self.probe(f"{state}: window top y=80", x, target[1] + 80, target),
                self.probe(f"{state}: middle (control)", x, round(target[1] + target[3] / 2), target)]

    def assert_menu_bar_hidden(self, top):
        """The documented result (#336): the hidden menu bar is not the window at the window top."""
        self.assertNotEqual(top["case"], "menu_bar",
                            f"the hidden menu bar is the window at {top['screen']}: the documented "
                            f"result does not hold on this setup: {top}")

    def reveal_menu_bar(self, x, target, content_top):
        """Rest the real pointer at the top edge of this suite's full-screen window, so macOS shows
        the menu bar, probe, then move the pointer back and probe again."""
        start = pointer_location()
        self.addCleanup(move_pointer, *start)
        edge = target[1]
        # Safety: the pointer goes only over this suite's own window, while its browser is in front.
        self.assertEqual(kitsune_bridge._mac_frontmost_pid(), self.browser.process.pid,
                         "the suite's browser is not the frontmost app; the pointer was not moved")
        under = next((w for w in windows_at(on_screen_windows(), x, edge)
                      if not w["skipped"] and w["layer"] not in menu_bar_levels()), None)
        self.assertEqual((under or {}).get("pid"), self.browser.process.pid,
                         f"the suite's browser window is not at {[x, edge]}; the pointer was not moved: {under}")
        hidden = menu_bar_windows(on_screen_windows(), target)
        for y in range(edge + 200, edge - 1, -20):
            move_pointer(x, y)
            time.sleep(0.02)
        # The menu bar slides in after a short delay; wait for the list to change, then settle.
        deadline = time.monotonic() + 3
        while menu_bar_windows(on_screen_windows(), target) == hidden and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1)
        changed = menu_bar_windows(on_screen_windows(), target) != hidden
        print(f"\n#336 pointer at {pointer_location()} (top edge), menu bar windows changed: {changed}", flush=True)
        self.assertTrue(changed, "the menu bar did not show; the revealed probes would not be evidence")
        revealed = self.probe_points("menu bar revealed", x, target, content_top)
        move_pointer(*start)
        time.sleep(2)
        print(f"\n#336 pointer back at {pointer_location()}", flush=True)
        hidden_again = self.probe_points("menu bar hidden again", x, target, content_top)
        # A click must never land on a shown menu bar.
        self.assertIsNotNone(revealed[0]["refusal"], f"a click on the shown menu bar would be made: {revealed[0]}")
        self.assert_menu_bar_hidden(hidden_again[0])

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
        hidden = self.probe_points("menu bar hidden", x, target, content_top)
        with self.subTest("menu bar hidden"):
            self.assert_menu_bar_hidden(hidden[0])
        with self.subTest("menu bar revealed"):
            if os.environ.get("KITSUNE_E2E_REVEAL_MENU_BAR") != "1":
                self.skipTest("moves the real pointer to the top edge: set KITSUNE_E2E_REVEAL_MENU_BAR=1")
            self.reveal_menu_bar(x, target, content_top)


if __name__ == "__main__":
    unittest.main()
