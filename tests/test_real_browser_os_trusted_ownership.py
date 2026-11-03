"""Issues #394 and #397 with real OS input: tab ownership inheritance and user_active when the
user clicks and types with the OS mouse and keyboard.

The user acts with Quartz CGEvents that this test posts itself, not with Kitsune's native input
(browser_trusted_click / browser_trusted_type), which the extension announces and leaves out of
user activity. tests/test_real_browser_trusted_ownership.py covers the same rules with
privileged synthesized trusted DOM input in a headless browser.

Opt-in twice, like test_real_browser_trusted_keys.py: besides KITSUNE_E2E_BROWSER (see
tests/real_browser_harness.py), it runs only on macOS with KITSUNE_E2E_TRUSTED_INPUT=1, because it
clicks with the real pointer and types with the real keyboard into a visible browser. Run it
while nobody uses the machine, with Accessibility permission for the app that runs it:

    KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen KITSUNE_E2E_TRUSTED_INPUT=1 \\
        uv run --extra macos python -m unittest discover -s tests -p test_real_browser_os_trusted_ownership.py -v

Before every posted event the test checks that this suite's browser process (by pid, not by
name, so another browser of the same name is left alone) is the frontmost app and that the
window at the screen point is the agent tab's window of that process; otherwise it fails
without posting. The pointer goes back to where it was after each test.

Side channels that change no page or tab state: the fixture pages report their events to the
fixture server with sendBeacon, and a background hook (extension_scripts) reports every
tabs.onCreated with its openerTabId and time.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import real_browser_harness as harness
from kitsune import bridge as kitsune_bridge
from kitsune.task_modes import OWNER_HEADER, USER_TABS_HEADER

OWNER = "e2e-os-trusted-ownership"
# extension/background/background.js USER_OPENED_TAB_WINDOW_MS and content.js USER_INPUT_WINDOW_MS.
USER_OPENED_TAB_WINDOW_MS = 1000
USER_INPUT_WINDOW_S = 3
# Delay samples per click kind; each holds the button down for a time spread over HOLDS_MS.
DELAY_RUNS = 10
HOLDS_MS = (50, 150)
# A user's key press: the key stays down this long, then the next one follows after the gap.
KEY_HOLD_S, KEY_GAP_S = 0.06, 0.12
# macOS virtual key codes (ANSI layout) of the keys the user presses.
KEY_CODES = {"a": 0, "b": 11, "h": 4, "i": 34, "Backspace": 51}
COMMAND_KEY_CODE = 55

# Every page reports its trusted and untrusted events, with the user's field's value and the
# focused element, so the test sees what reached the page without asking the page.
REPORTER = """<script>
const field = document.getElementById('field');
for (const type of ['pointerdown', 'mousedown', 'mouseup', 'keydown', 'input', 'click', 'auxclick', 'focus', 'blur']) {
  addEventListener(type, e => navigator.sendBeacon('/event', JSON.stringify({
    type, trusted: e.isTrusted, target: e.target && e.target.id || '', key: e.key || null, at: Date.now(),
    value: field ? field.value : null, focused: document.activeElement && document.activeElement.id || '',
  })), true);
}
</script>"""
# Elements sit at fixed page coordinates, away from the window's edges, so the user can click
# them by position.
STYLE = "<style>body{margin:0} .at{position:absolute;left:40px;width:300px;height:40px}</style>"
PAGES = {
    "/agent": STYLE + "<title>agent</title><a id=link class=at style=top:40px href='/opened?user'>link</a>" + REPORTER,
    # Opens a tab by script, without any input.
    "/auto": "<title>auto</title><script>setTimeout(() => window.open('/opened?script'), 300);</script>",
    "/form": STYLE + "<title>form</title><input id=field class=at style=top:40px>"
             "<button id=act class=at style=top:140px onclick=\"document.title='acted'\">act</button>"
             "<p id=blank class=at style=top:240px>blank</p>" + REPORTER,
    "/opened": "<title>opened</title><h1>opened</h1>",
    "/next": "<title>next</title><h1>next</h1>",
}
# Page coordinates (CSS px) of each element's middle.
POINTS = {"link": (190, 60), "field": (190, 60), "blank": (190, 260)}

# The desktop point (macOS points) of page point (x, y) in the page shown at url. Throws if url is
# not the shown page. Read in the parent process, so the page and the extension see nothing.
SCREEN_POINT = """(async () => {
  const [url, x, y] = [%s, %s, %s];
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const shown = win.gBrowser.selectedBrowser;
  if (shown.currentURI.spec !== url) throw new Error('the user sees ' + shown.currentURI.spec + ', not ' + url);
  const rect = shown.getBoundingClientRect();
  return [win.mozInnerScreenX + rect.left + x, win.mozInnerScreenY + rect.top + y];
})()"""

TAB_CREATED_HOOK = """'use strict';
browser.tabs.onCreated.addListener(tab => {
  fetch('%s/created', { method: 'POST', mode: 'no-cors',
    body: JSON.stringify({ id: tab.id, opener: tab.openerTabId ?? null, at: Date.now() }) }).catch(() => {});
});
"""


class _Reports:
    """What the pages and the background hook reported, in arrival order."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []

    def add(self, kind, data):
        with self.lock:
            self.items.append((kind, data))

    def get(self, kind):
        with self.lock:
            return [data for k, data in self.items if k == kind]

    def clear(self):
        with self.lock:
            self.items.clear()


class _Site(harness.FixtureServer):
    def __init__(self):
        reports = self.reports = _Reports()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = PAGES.get(self.path.split("?")[0], "not found").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path in ("/event", "/created"):
                    reports.add(self.path[1:], json.loads(data))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        super().__init__(Handler)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("KITSUNE_E2E_TRUSTED_INPUT") == "1",
                     "sends real OS clicks and keys: set KITSUNE_E2E_TRUSTED_INPUT=1 on macOS")
class RealBrowserOsTrustedOwnershipTests(harness.RealBrowserTestCase):
    HEADED = True
    # The page's own window.open without input opens its tab only with popups allowed.
    PROFILE_PREFS = harness.POPUPS_ALLOWED

    @classmethod
    def setUpClass(cls):
        cls.site = _Site()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        auth = cls.bridge.auth
        owner = patch.object(cls.bridge, "auth", side_effect=lambda: {**auth(), OWNER_HEADER: OWNER})
        owner.start()
        cls.addClassCleanup(owner.stop)

    @classmethod
    def extension_scripts(cls):
        return {"test-tab-created.js": TAB_CREATED_HOOK % cls.site.origin}

    def setUp(self):
        if kitsune_bridge._mac_window_at(0, 0) is None:
            self.skipTest("needs Quartz: uv run --extra macos")
        if kitsune_bridge._mac_accessibility_trusted() is False:
            self.fail("macOS drops the posted events: grant Accessibility permission to the app running this test "
                      "(System Settings > Privacy & Security > Accessibility)")
        import Quartz
        self.quartz = Quartz
        saved = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        self.addCleanup(self.restore_pointer, saved)

    def restore_pointer(self, position):
        time.sleep(kitsune_bridge.POINTER_RESTORE_DELAY)
        kitsune_bridge._quartz_warp(self.quartz, position)

    # -- the browser and its tabs --

    def front(self):
        """Bring this suite's browser (by pid) to the front, as a user would show it."""
        script = (f'tell application "System Events" to set frontmost of '
                  f'(first process whose unix id is {self.browser.process.pid}) to true')
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        time.sleep(0.8)

    def parent(self, script, *args):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script % tuple(json.dumps(a) for a in args))
        finally:
            debugger.close()

    def post(self, path, body, user_tabs=None):
        """A bridge request, optionally with an explicit user-tab permission."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json",
                   **({USER_TABS_HEADER: user_tabs} if user_tabs else {})}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def tabs(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def tab_window(self, tab):
        for window in self.call("/api/windows", {})["windows"]:
            if any(t["id"] == tab for t in window["tabs"]):
                return window["left"], window["top"], window["width"], window["height"]
        self.fail(f"no window holds tab {tab}")

    def wait_for(self, predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.05)

    def shown_agent_tab(self, path):
        """An agent tab on path, shown in the frontmost browser, with no page events reported yet."""
        tab = self.assert_ok(self.call("/api/new-tab", {"url": self.site.origin + path, "wait": True,
                                                         "timeout": 15000}))["tabId"]
        self.addCleanup(self.close_agent_tab, tab)
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab}))
        self.assertIs(self.tabs()[tab]["owned"], True)
        self.front()
        self.site.reports.clear()
        return tab, self.site.origin + path

    def close_agent_tab(self, tab_id):
        """Close an agent tab once the user's last input on it is old enough."""
        deadline = time.monotonic() + USER_INPUT_WINDOW_S + 2
        while (result := self.call("/api/close-tab", {"tabId": tab_id})).get("code") == "user_active":
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(0.2)
        self.assert_ok(result)

    def close_user_tab(self, tab_id):
        self.assert_ok(self.post("/api/close-tab", {"tabId": tab_id}, user_tabs="writable"))

    # -- the user's OS input --

    def assert_browser_frontmost(self):
        frontmost = kitsune_bridge._mac_frontmost_pid()
        self.assertEqual(frontmost, self.browser.process.pid,
                         "the test browser is not the frontmost app; no OS input was posted")

    def assert_browser_at(self, point, window):
        """Fail, before any event is posted, unless the test browser is frontmost and the window at
        the desktop point is its window that shows the agent tab."""
        self.assert_browser_frontmost()
        found = kitsune_bridge._mac_window_at(*point)
        self.assertIsNotNone(found, "the window list (Quartz) is not available; no OS input was posted")
        pid, owner, bounds = found
        self.assertEqual(pid, self.browser.process.pid,
                         f"{owner or 'the desktop'} (pid {pid}), not the test browser, is at {point}; "
                         "no OS input was posted")
        self.assertTrue(kitsune_bridge._same_window(bounds, window),
                        f"another window {bounds} of the test browser, not the tab's {window}, is at {point}")

    def screen_point(self, tab, url, element):
        point = self.parent(SCREEN_POINT, url, *POINTS[element])
        return (float(point[0]), float(point[1])), self.tab_window(tab)

    def post_mouse(self, kind, point, button, flags=0):
        Q = self.quartz
        event = Q.CGEventCreateMouseEvent(None, kind, point, button)
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 1)
        if flags:
            Q.CGEventSetFlags(event, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, event)

    def post_key(self, code, down, flags=0, text=None):
        Q = self.quartz
        event = Q.CGEventCreateKeyboardEvent(None, code, down)
        if text is not None:
            Q.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        Q.CGEventSetFlags(event, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, event)

    def os_click(self, tab, url, element, button="left", command=False, hold=0.08):
        """The user clicks element of the page shown at url with the OS pointer, holding the button
        for hold seconds; command holds the Command key around the click. Returns the time
        (epoch ms) just before the button went down."""
        Q = self.quartz
        point, window = self.screen_point(tab, url, element)
        down, up, mouse_button = {
            "left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft),
            "middle": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGMouseButtonCenter),
        }[button]
        flags = Q.kCGEventFlagMaskCommand if command else 0
        self.assert_browser_at(point, window)
        self.post_mouse(Q.kCGEventMouseMoved, point, mouse_button)
        time.sleep(0.1)
        if command:
            self.assert_browser_at(point, window)
            self.post_key(COMMAND_KEY_CODE, True, flags)
        try:
            if command:
                time.sleep(0.1)
            self.assert_browser_at(point, window)
            pressed_at = time.time() * 1000
            self.post_mouse(down, point, mouse_button, flags)
            try:
                time.sleep(hold)
            finally:
                self.post_mouse(up, point, mouse_button, flags)
        finally:
            if command:
                time.sleep(0.05)
                self.post_key(COMMAND_KEY_CODE, False)
        return pressed_at

    def os_type(self, keys):
        """The user presses keys (characters or Backspace) with the OS keyboard."""
        for key in keys:
            self.assert_browser_frontmost()
            text = None if key == "Backspace" else key
            self.post_key(KEY_CODES[key], True, text=text)
            try:
                time.sleep(KEY_HOLD_S)
            finally:
                self.post_key(KEY_CODES[key], False, text=text)
            time.sleep(KEY_GAP_S)

    # -- what reached the page --

    def events(self, type_=None):
        return [e for e in self.site.reports.get("event") if type_ is None or e["type"] == type_]

    def created_by(self, opener, count):
        created = self.wait_for(lambda: len(c := [t for t in self.site.reports.get("created")
                                                  if t["opener"] == opener]) >= count and c,
                                f"{count} tabs opened by tab {opener}: {self.site.reports.get('created')}")
        self.assertEqual(len(created), count, created)
        return created

    def assert_read_only(self, result):
        self.assert_error(result, "user_tab_read_only", False)
        self.assertNotIn("confirmToken", result.get("state", {}))

    def assert_user_active(self, result, reasons):
        self.assert_error(result, "user_active", True)
        self.assertIn(result["state"]["reason"], reasons, result)
        return result["state"]["reason"]

    # -- #394 --

    def test_a_tab_the_page_opens_without_user_input_is_this_clients(self):
        # The control: no OS input reaches the agent tab, so window.open's tab inherits its owner.
        agent = self.assert_ok(self.call("/api/new-tab", {"url": self.site.origin + "/auto", "wait": True,
                                                           "timeout": 15000}))["tabId"]
        self.addCleanup(self.close_agent_tab, agent)
        self.assert_ok(self.call("/api/switch-tab", {"tabId": agent}))
        [created] = self.created_by(agent, 1)
        self.addCleanup(self.close_user_tab, created["id"])
        tabs = self.wait_for(lambda: (t := self.tabs()) and t[created["id"]]["url"].endswith("/opened?script") and t,
                             "the opened tab did not load")
        self.assertIs(tabs[created["id"]]["owned"], True, tabs[created["id"]])
        self.assertIs(tabs[agent]["owned"], True)

    def test_os_middle_and_command_clicks_open_user_tabs(self):
        agent, url = self.shown_agent_tab("/agent")
        delays = {"middle": [], "command": []}
        for run in range(DELAY_RUNS):
            hold_ms = HOLDS_MS[0] + round((HOLDS_MS[1] - HOLDS_MS[0]) * run / (DELAY_RUNS - 1))
            for kind, button, command in (("middle", "middle", False), ("command", "left", True)):
                self.site.reports.clear()
                pressed_at = self.os_click(agent, url, "link", button, command, hold_ms / 1000)
                [created] = self.created_by(agent, 1)
                self.addCleanup(self.close_user_tab, created["id"])
                downs = self.wait_for(lambda: self.events("mousedown"), "no mousedown reported")
                self.assertEqual([(e["trusted"], e["target"]) for e in downs], [(True, "link")], downs)
                delays[kind].append({"hold": hold_ms, "posted": round(created["at"] - pressed_at),
                                     "page": created["at"] - downs[0]["at"]})
                # The content's input report and tabs.onCreated can arrive in either order; wait out the window.
                time.sleep(USER_OPENED_TAB_WINDOW_MS / 1000 + 0.2)
                self.assertEqual(len([t for t in self.site.reports.get("created") if t["opener"] == agent]), 1,
                                 f"one click opened more than one tab: {self.site.reports.get('created')}")
                self.assertTrue(all(e["trusted"] for e in self.events()), self.events())
                tabs = self.tabs()
                self.assertTrue(tabs[created["id"]]["url"].endswith("/opened?user"), tabs[created["id"]])
                self.assertIs(tabs[created["id"]]["owned"], False, (kind, run, tabs[created["id"]]))
                self.assertIs(tabs[agent]["owned"], True)
                self.assertTrue(tabs[agent]["active"], "the opened tab stays in the background")
                if run == 0:
                    self.assert_read_only(self.post("/api/navigate", {"tabId": created["id"],
                                                                      "url": self.site.origin + "/next"}))
                    self.assertEqual(self.tabs()[created["id"]]["url"], tabs[created["id"]]["url"])
        samples = [d for values in delays.values() for d in values]
        for kind, values in (*delays.items(), ("all", samples)):
            for measure in ("posted", "page"):
                ms = [d[measure] for d in values]
                sys.stderr.write(f"\n#394 OS mousedown ({measure}) -> tabs.onCreated, {kind} click, ms over "
                                 f"{len(ms)} clicks: min {min(ms)}, median {statistics.median(ms)}, max {max(ms)}\n")
        sys.stderr.write(f"#394 samples (hold ms, posted ms, page ms): {json.dumps(delays)}\n")
        for sample in samples:
            self.assertGreaterEqual(sample["page"], 0, samples)
            self.assertLessEqual(sample["page"], USER_OPENED_TAB_WINDOW_MS, samples)

    # -- #397 --

    def agent_writes(self, tab):
        """The agent's type, press_key, click and navigate on its own tab, each answered."""
        return [self.call(path, {**body, "tabId": tab}) for path, body in (
            ("/api/type", {"selector": "#field", "text": "x", "clear": False}),
            ("/api/keypress", {"selector": "#field", "key": "z"}),
            ("/api/click", {"selector": "#act"}),
            ("/api/navigate", {"url": self.site.origin + "/next"}),
        )]

    def user_typed(self, tab, url, keys, value):
        """The user clicks into the field and types keys; returns when the page has value."""
        self.os_click(tab, url, "field")
        self.wait_for(lambda: any(e["type"] == "focus" and e["target"] == "field" for e in self.events()),
                      f"the user's click did not focus the field: {self.events()}")
        self.os_type(keys)
        self.wait_for(lambda: (inputs := self.events("input")) and inputs[-1]["value"] == value,
                      f"the user's typing did not leave {value!r}: {self.events()}")
        return time.monotonic()

    def wait_for_agent_inputs(self, values):
        """Wait until the field's untrusted (agent) input events, in order, left exactly values."""
        self.wait_for(lambda: [e["value"] for e in self.events("input") if not e["trusted"]] == values,
                      f"the agent's input events did not leave exactly {values}: {self.events()}")

    def assert_only_user_events(self, value):
        events = self.events()
        self.assertTrue(events and all(e["trusted"] for e in events), events)
        self.assertEqual(events[-1]["value"], value, events)
        self.assertNotIn("act", [e["target"] for e in events if e["type"] == "click"], events)

    def test_os_typing_pauses_the_agent_until_the_field_loses_focus(self):
        agent, url = self.shown_agent_tab("/form")
        typed_at = self.user_typed(agent, url, "hi", "hi")
        reasons = [self.assert_user_active(r, ("input", "typing")) for r in self.agent_writes(agent)]
        self.assertEqual(reasons[0], "input", reasons)

        time.sleep(max(0, typed_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))
        for result in self.agent_writes(agent):
            self.assert_user_active(result, ("typing",))
            self.assertEqual(result["state"]["field"], "input", result)
        self.assert_only_user_events("hi")
        tab = self.tabs()[agent]
        self.assertEqual((tab["url"], tab["title"], tab["owned"]), (url, "form", True))

        # The user clicks outside the field, which loses focus; the click starts a new input window.
        self.os_click(agent, url, "blank")
        self.wait_for(lambda: any(e["type"] == "blur" and e["target"] == "field" for e in self.events()),
                      f"the field kept focus: {self.events()}")
        clicked_at = time.monotonic()
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}),
                                ("input",))
        self.assert_only_user_events("hi")
        time.sleep(max(0, clicked_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        for result in self.agent_writes(agent):
            self.assert_ok(result)
        # The agent's text follows the user's, which no agent keystroke changed.
        self.wait_for_agent_inputs(["hix", "hixz"])
        self.wait_for(lambda: ("click", "act") in [(e["type"], e["target"]) for e in self.events() if not e["trusted"]],
                      f"the agent's click did not reach the page: {self.events()}")
        tab = self.wait_for(lambda: (t := self.tabs()[agent])["url"] == self.site.origin + "/next" and t,
                            "the agent's navigation did not load")
        self.assertIs(tab["owned"], True)

    def test_os_typing_releases_when_the_user_empties_the_field(self):
        agent, url = self.shown_agent_tab("/form")
        typed_at = self.user_typed(agent, url, ["a", "b", "Backspace", "Backspace"], "")
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}),
                                ("input",))
        self.assertEqual(self.events()[-1]["focused"], "field")
        self.assert_only_user_events("")
        time.sleep(max(0, typed_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        # The field keeps focus, but it is empty. The agent's own text there does not hold it again.
        self.assert_ok(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}))
        self.assert_ok(self.call("/api/keypress", {"tabId": agent, "selector": "#field", "key": "z"}))
        self.wait_for_agent_inputs(["x", "xz"])
        self.assertIs(self.tabs()[agent]["owned"], True)


if __name__ == "__main__":
    unittest.main()
