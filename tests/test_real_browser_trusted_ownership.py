"""Issues #394 and #397 in a real browser: tab ownership inheritance and user_active under
trusted user input.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The browser
stays headless. The user's input is privileged synthesized trusted DOM input, not OS input: the
browser's parent process (harness.RemoteDebugger) sends mouse events with the chrome window's
synthesizeMouseEvent at the shown page's coordinates and keys with nsITextInputProcessor. Gecko
dispatches them through its own widget and content-process event path, so the page sees them with
isTrusted true (every page event is reported and checked), but no OS event is posted: the real
pointer and keyboard are never used. Acceptance with real OS input stays open.

Side channels that change no page or tab state: the fixture pages report their events to the
fixture server with sendBeacon, a background hook (extension_scripts) reports every
tabs.onCreated with its openerTabId and time, and the fixture server records when a slow page
was requested.

A cross-origin child frame is served by the same fixture server under localhost instead of
127.0.0.1, another site, so the browser runs it out of process.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import statistics
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import real_browser_harness as harness
from kitsune.task_modes import OWNER_HEADER, USER_TABS_HEADER

OWNER = "e2e-trusted-ownership"
OTHER_OWNER = "e2e-trusted-ownership-other"
# extension/background/background.js USER_OPENED_TAB_WINDOW_MS and content.js USER_INPUT_WINDOW_MS.
USER_OPENED_TAB_WINDOW_MS = 1000
USER_INPUT_WINDOW_S = 3
# background.js USER_NAVIGATION_WINDOW_MS and AGENT_EFFECT_GRACE_MS: a load that starts within the
# grace after an agent command on the tab is the agent's.
USER_NAVIGATION_WINDOW_S = 10
AGENT_EFFECT_GRACE_S = 2
DELAY_RUNS = 5

# Every page reports its trusted and untrusted events, with the user's field's value and the
# focused element, so the test sees what reached the page without asking the page. Beacons can
# arrive out of order; seq numbers each document's events in dispatch order.
REPORTER = """<script>
const field = document.getElementById('field');
let seq = 0;
for (const type of ['pointerdown', 'mousedown', 'keydown', 'input', 'click', 'auxclick', 'focus', 'blur']) {
  addEventListener(type, e => navigator.sendBeacon('/event', JSON.stringify({
    type, seq: ++seq, trusted: e.isTrusted, target: e.target && e.target.id || '', key: e.key || null, at: Date.now(),
    value: field ? field.value : null, focused: document.activeElement && document.activeElement.id || '',
    frame: window !== top,
  })), true);
}
</script>"""
# Elements sit at fixed page coordinates, so the user can click them by position.
STYLE = "<style>body{margin:0} .at{position:absolute;left:0;width:300px;height:40px}</style>"
PAGES = {
    "/agent": STYLE + "<title>agent</title><a id=link class=at style=top:0 href='/opened?user'>link</a>" + REPORTER,
    # Opens a tab by script and through a target=_blank link, without any input.
    "/auto": "<title>auto</title><a id=blank href='/opened?link' target=_blank>blank</a>"
             "<script>setTimeout(() => { window.open('/opened?script'); document.getElementById('blank').click(); },"
             " 300);</script>",
    "/form": STYLE + "<title>form</title><input id=field class=at style=top:0>"
             "<button id=act class=at style=top:100px onclick=\"document.title='acted'\">act</button>"
             "<p id=blank class=at style=top:200px>blank</p>" + REPORTER,
    # The field sits in a cross-origin child frame (FRAME_SRC) at the top left of the page. #blank
    # comes first, so Tab from a click on it focuses the frame's field.
    "/framed": STYLE + "<title>framed</title><p id=blank class=at style=top:200px>blank</p>"
               "<iframe id=frame title=editor src=FRAME_SRC"
               " style='position:absolute;left:0;top:0;width:400px;height:60px;border:0'></iframe>"
               "<button id=act class=at style=top:100px onclick=\"document.title='acted'\">act</button>" + REPORTER,
    "/frame": STYLE + "<title>frame</title><input id=field class=at style=top:0 aria-label=draft>" + REPORTER,
    # Links the user opens from the keyboard. #start comes first, so Tab from a click on it focuses
    # #plain, then #popup. The page opens a tab of its own 1.5 s after Enter on #popup.
    "/keys": STYLE + "<title>keys</title><p id=start class=at style=top:200px>start</p>"
             "<a id=plain class=at style=top:0 href='/opened?accel-enter'>plain</a>"
             "<a id=popup class=at style=top:100px href='/opened?enter' target=_blank>popup</a>"
             "<script>addEventListener('keydown', e => { if (e.isTrusted && e.key === 'Enter' &&"
             " e.target.id === 'popup') setTimeout(() => window.open('/opened?late'), 1500); }, true);</script>"
             + REPORTER,
    # A same-tab link to a document that takes 3 s to answer.
    "/nav": STYLE + "<title>nav</title><a id=link class=at style=top:0 href='/slow?ms=3000&page=/form'>link</a>"
             + REPORTER,
    "/opened": "<title>opened</title><h1>opened</h1>",
    "/next": "<title>next</title><h1>next</h1>",
}
# Page coordinates (CSS px) of each element's middle.
POINTS = {"link": (150, 20), "field": (150, 20), "blank": (150, 220), "start": (150, 220)}

# The user presses and releases a mouse button at a point of the page shown at url. modifiers
# "accel" holds Cmd on macOS, Ctrl elsewhere. Throws if url is not the shown page.
USER_CLICKS = """(async () => {
  const [url, x, y, button, modifiers] = [%s, %s, %s, %s, %s];
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const shown = win.gBrowser.selectedBrowser;
  if (shown.currentURI.spec !== url) throw new Error('the user sees ' + shown.currentURI.spec + ', not ' + url);
  const U = Ci.nsIDOMWindowUtils;
  const mods = modifiers === 'accel' ? (Services.appinfo.OS === 'Darwin' ? U.MODIFIER_META : U.MODIFIER_CONTROL) : 0;
  const rect = shown.getBoundingClientRect();
  for (const type of ['mousedown', 'mouseup']) {
    win.synthesizeMouseEvent(type, rect.left + x, rect.top + y,
      {button, buttons: type === 'mousedown' ? (button === 1 ? 4 : 1) : 0, clickCount: 1, modifiers: mods},
      {isDOMEventSynthesized: false, isWidgetEventSynthesized: false, isAsyncEnabled: false});
  }
  return true;
})()"""

# The user presses keys (key names: characters, Backspace, Tab or Enter) into the focused element
# of the page shown at url; modifiers "accel" holds Cmd on macOS, Ctrl elsewhere, around them.
# Throws if url is not the shown page or a key is not dispatched.
USER_TYPES = """(async () => {
  const [url, keys, modifiers] = [%s, %s, %s];
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const shown = win.gBrowser.selectedBrowser;
  if (shown.currentURI.spec !== url) throw new Error('the user sees ' + shown.currentURI.spec + ', not ' + url);
  const tip = Cc['@mozilla.org/text-input-processor;1'].createInstance(Ci.nsITextInputProcessor);
  if (!tip.beginInputTransaction(win, () => true)) throw new Error('no input transaction');
  const named = {Backspace: 8, Tab: 9, Enter: 13, Meta: 224, Control: 17};
  const event = key => new win.KeyboardEvent('', key in named
    ? {key, code: key === 'Meta' || key === 'Control' ? key + 'Left' : key, keyCode: named[key]}
    : {key, code: 'Key' + key.toUpperCase(), keyCode: key.toUpperCase().charCodeAt(0)});
  const held = modifiers === 'accel' ? event(Services.appinfo.OS === 'Darwin' ? 'Meta' : 'Control') : null;
  if (held) tip.keydown(held);
  for (const key of keys) {
    tip.keydown(event(key));
    tip.keyup(event(key));
  }
  if (held) tip.keyup(held);
  return true;
})()"""

# The user loads url in the shown tab as from the address bar: no page input. Throws if from is
# not the shown page.
USER_LOADS = """(async () => {
  const [from, url] = [%s, %s];
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const shown = win.gBrowser.selectedBrowser;
  if (shown.currentURI.spec !== from) throw new Error('the user sees ' + shown.currentURI.spec + ', not ' + from);
  win.openTrustedLinkIn(url, 'current');
  return true;
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
                path, query = urlparse(self.path).path, parse_qs(urlparse(self.path).query)
                if path == "/slow":
                    # /slow?ms=N&page=P: P's page after N ms, so its load stays observable.
                    reports.add("slow", {"at": time.monotonic()})
                    time.sleep(int(query["ms"][0]) / 1000)
                    path = query["page"][0]
                port = self.server.server_address[1]
                body = PAGES.get(path, "not found").replace("FRAME_SRC", f"http://localhost:{port}/frame").encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # a slow page's load was replaced before it answered

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path in ("/event", "/created"):
                    reports.add(self.path[1:], json.loads(data))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        super().__init__(Handler)


class RealBrowserTrustedOwnershipTests(harness.RealBrowserTestCase):
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

    def parent(self, script, *args):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script % tuple(json.dumps(a) for a in args))
        finally:
            debugger.close()

    def post(self, path, body, owner=OWNER, user_tabs=None):
        """A bridge request as client owner, optionally with an explicit user-tab permission."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json", OWNER_HEADER: owner,
                   **({USER_TABS_HEADER: user_tabs} if user_tabs else {})}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def tabs(self, owner=OWNER):
        with patch.object(self.bridge, "auth", return_value={**self.bridge.auth(), OWNER_HEADER: owner}):
            return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for(self, predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.05)

    def shown_agent_tab(self, path):
        """An agent tab on path, shown to the user, with no page events reported yet."""
        tab = self.assert_ok(self.call("/api/new-tab", {"url": self.site.origin + path, "wait": True,
                                                         "timeout": 15000}))["tabId"]
        self.addCleanup(self.close_agent_tab, tab)
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab}))
        self.assertIs(self.tabs()[tab]["owned"], True)
        time.sleep(0.3)
        self.site.reports.clear()
        return tab, self.site.origin + path

    def user_clicks(self, url, element, button=0, modifiers=None):
        self.assertTrue(self.parent(USER_CLICKS, url, *POINTS[element], button, modifiers))

    def user_types(self, url, keys, modifiers=None):
        self.assertTrue(self.parent(USER_TYPES, url, list(keys), modifiers))

    def events(self, type_=None):
        return [e for e in self.site.reports.get("event") if type_ is None or e["type"] == type_]

    def created_by(self, opener, count):
        created = self.wait_for(lambda: len(c := [t for t in self.site.reports.get("created")
                                                  if t["opener"] == opener]) >= count and c,
                                f"{count} tabs opened by tab {opener}: {self.site.reports.get('created')}")
        self.assertEqual(len(created), count, created)
        return created

    def close_agent_tab(self, tab_id):
        """Close an agent tab once the user's last input on it is old enough."""
        deadline = time.monotonic() + USER_INPUT_WINDOW_S + 2
        while (result := self.call("/api/close-tab", {"tabId": tab_id})).get("code") == "user_active":
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(0.2)
        self.assert_ok(result)

    def close_user_tab(self, tab_id):
        self.assert_ok(self.post("/api/close-tab", {"tabId": tab_id}, user_tabs="writable"))

    def assert_read_only(self, result):
        self.assert_error(result, "user_tab_read_only", False)
        self.assertNotIn("confirmToken", result.get("state", {}))

    def assert_user_active(self, result, reasons):
        self.assert_error(result, "user_active", True)
        self.assertIn(result["state"]["reason"], reasons, result)
        return result["state"]["reason"]

    # -- #394 --

    def test_tabs_the_page_opens_without_user_input_are_this_clients(self):
        # The agent's own click on a target=_blank link is covered by test_real_browser_shared_browser.
        agent = self.open_tab(self.site.origin + "/auto")
        created = self.created_by(agent, 2)
        for tab in created:
            self.addCleanup(self.close_user_tab, tab["id"])
        opened = {t["id"] for t in created}
        tabs = self.wait_for(lambda: (t := self.tabs()) and all(t[i]["url"].startswith(self.site.origin + "/opened")
                                                                for i in opened) and t, "the opened tabs did not load")
        self.assertEqual({tabs[i]["url"].split("?")[1] for i in opened}, {"script", "link"}, tabs)
        self.assertEqual({i: tabs[i]["owned"] for i in (agent, *opened)}, dict.fromkeys((agent, *opened), True))

        # #394 (d): another client sees them as user tabs and cannot write to them.
        other = self.tabs(OTHER_OWNER)
        self.assertEqual({i: other[i]["owned"] for i in (agent, *opened)}, dict.fromkeys((agent, *opened), False))
        for tab in (agent, *opened):
            self.assert_read_only(self.post("/api/navigate", {"tabId": tab, "url": self.site.origin + "/next"},
                                            owner=OTHER_OWNER))
        self.assertEqual({i: self.tabs()[i]["url"] for i in opened}, {i: tabs[i]["url"] for i in opened})

    def test_trusted_middle_and_accel_clicks_open_user_tabs_within_the_window(self):
        agent, url = self.shown_agent_tab("/agent")
        delays = {"middle": [], "accel": []}
        for run in range(DELAY_RUNS):
            for kind, button, modifiers in (("middle", 1, None), ("accel", 0, "accel")):
                self.site.reports.clear()
                self.user_clicks(url, "link", button, modifiers)
                [created] = self.created_by(agent, 1)
                self.addCleanup(self.close_user_tab, created["id"])
                downs = self.wait_for(lambda: self.events("mousedown"), "no mousedown reported")
                self.assertEqual([(e["trusted"], e["target"]) for e in downs], [(True, "link")], downs)
                delays[kind].append(created["at"] - downs[0]["at"])
                # The content's input report and tabs.onCreated can arrive in either order; wait out the window.
                time.sleep(USER_OPENED_TAB_WINDOW_MS / 1000 + 0.2)
                self.assertTrue(all(e["trusted"] for e in self.events()), self.events())
                tabs = self.tabs()
                self.assertTrue(tabs[created["id"]]["url"].endswith("/opened?user"), tabs[created["id"]])
                self.assertIs(tabs[created["id"]]["owned"], False, (kind, run, tabs[created["id"]]))
                self.assertIs(tabs[agent]["owned"], True)
                self.assertTrue(tabs[agent]["active"], "the opened tab stays in the background")
                if run == 0:
                    for path, body in (("/api/navigate", {"url": self.site.origin + "/next"}),
                                       ("/api/close-tab", {})):
                        self.assert_read_only(self.post(path, {**body, "tabId": created["id"]}))
                    self.assertIn(created["id"], self.tabs())
        for kind, values in delays.items():
            sys.stderr.write(f"\n#394 trusted mousedown -> tabs.onCreated, {kind} click, ms over {len(values)} runs: "
                             f"min {min(values)}, median {statistics.median(values)}, max {max(values)} {values}\n")
            for value in values:
                self.assertGreaterEqual(value, 0, values)
                self.assertLess(value, USER_OPENED_TAB_WINDOW_MS, values)

    def user_focuses_with_tab(self, url, link):
        """The user presses Tab until link has focus, then waits out the 1 s window after that input."""
        self.user_types(url, ["Tab"])
        self.wait_for(lambda: any(e["type"] == "focus" and e["target"] == link for e in self.events()),
                      f"Tab did not focus {link}: {self.events()}")
        time.sleep(USER_OPENED_TAB_WINDOW_MS / 1000 + 0.2)

    def assert_opened_by_enter(self, created, link):
        """The trusted Enter on link is the only page input in the 1 s before the tab was created."""
        enter = self.wait_for(lambda: [e for e in self.events("keydown") if e["key"] == "Enter"], "no Enter reported")
        self.assertEqual([(e["trusted"], e["target"]) for e in enter], [(True, link)], enter)
        inputs = [e for e in self.events() if e["type"] in ("keydown", "mousedown", "pointerdown")]
        self.assertTrue(all(e["trusted"] for e in inputs), inputs)
        delay = created["at"] - enter[0]["at"]
        self.assertGreaterEqual(delay, 0, (created, enter))
        self.assertLess(delay, USER_OPENED_TAB_WINDOW_MS, (created, enter))
        earlier = [e for e in inputs if e["at"] < enter[0]["at"] and e["key"] not in ("Meta", "Control")]
        self.assertTrue(all(created["at"] - e["at"] > USER_OPENED_TAB_WINDOW_MS for e in earlier), (created, inputs))
        return delay

    def test_links_the_user_opens_from_the_keyboard_open_user_tabs(self):
        agent, url = self.shown_agent_tab("/keys")
        self.user_clicks(url, "start")
        self.user_focuses_with_tab(url, "plain")
        self.user_types(url, ["Enter"], "accel")
        [accel] = self.created_by(agent, 1)
        self.addCleanup(self.close_user_tab, accel["id"])
        delays = [self.assert_opened_by_enter(accel, "plain")]
        # The content's input report and tabs.onCreated can arrive in either order; wait out the window.
        time.sleep(USER_OPENED_TAB_WINDOW_MS / 1000 + 0.2)
        tabs = self.tabs()
        self.assertTrue(tabs[accel["id"]]["url"].endswith("/opened?accel-enter"), tabs[accel["id"]])
        self.assertIs(tabs[accel["id"]]["owned"], False, tabs[accel["id"]])
        self.assertEqual((tabs[agent]["owned"], tabs[agent]["active"]), (True, True), "accel+Enter opens in the background")
        self.assert_read_only(self.post("/api/navigate", {"tabId": accel["id"], "url": self.site.origin + "/next"}))

        # Enter on a target=_blank link opens a shown tab. The page's own open 1.5 s later, without
        # input in the 1 s before it, stays the agent's.
        self.site.reports.clear()
        self.user_focuses_with_tab(url, "popup")
        self.user_types(url, ["Enter"])
        [enter] = self.created_by(agent, 1)
        self.addCleanup(self.close_user_tab, enter["id"])
        delays.append(self.assert_opened_by_enter(enter, "popup"))
        late = next(t for t in self.created_by(agent, 2) if t["id"] != enter["id"])
        self.addCleanup(self.close_user_tab, late["id"])
        self.assertGreater(late["at"] - enter["at"], USER_OPENED_TAB_WINDOW_MS, (enter, late))
        inputs = [e for e in self.events() if e["type"] in ("keydown", "mousedown", "pointerdown")]
        self.assertEqual([e for e in inputs if late["at"] - USER_OPENED_TAB_WINDOW_MS <= e["at"] <= late["at"]], [],
                         (late, inputs))
        time.sleep(USER_OPENED_TAB_WINDOW_MS / 1000 + 0.2)
        tabs = self.wait_for(lambda: (t := self.tabs()) and t[late["id"]]["url"].endswith("/opened?late") and t,
                             "the page's own tab did not load")
        self.assertTrue(tabs[enter["id"]]["url"].endswith("/opened?enter"), tabs[enter["id"]])
        self.assertEqual({i: tabs[i]["owned"] for i in (agent, enter["id"], late["id"])},
                         {agent: True, enter["id"]: False, late["id"]: True}, tabs)
        self.assertIs(tabs[agent]["active"], False, "Enter on a target=_blank link shows its tab")
        self.assert_read_only(self.post("/api/close-tab", {"tabId": enter["id"]}))
        self.assertIn(enter["id"], self.tabs())
        sys.stderr.write(f"\n#394 trusted Enter keydown -> tabs.onCreated, ms (accel+Enter, Enter): {delays}\n")

    # -- #397 --

    def agent_writes(self, tab):
        """The agent's type, press_key, click and navigate on its own tab, each answered."""
        return [self.call(path, {**body, "tabId": tab}) for path, body in (
            ("/api/type", {"selector": "#field", "text": "x", "clear": False}),
            ("/api/keypress", {"selector": "#field", "key": "z"}),
            ("/api/click", {"selector": "#act"}),
            ("/api/navigate", {"url": self.site.origin + "/next"}),
        )]

    def user_typed(self, url, keys, value, click=True):
        """The user clicks into the field (unless click is False) and types keys; returns when the
        page has value."""
        if click:
            self.user_clicks(url, "field")
        self.user_types(url, keys)
        self.wait_for(lambda: (inputs := self.events("input"))
                      and max(inputs, key=lambda e: e["seq"])["value"] == value,
                      f"the user's typing did not leave {value!r}: {self.events()}")
        return time.monotonic()

    def assert_only_user_events(self, value, frame=False):
        """Only the user's events reached the page, and the field (in the child frame) holds value."""
        events = self.events()
        self.assertTrue(events and all(e["trusted"] for e in events), events)
        latest = max((e for e in events if e["frame"] == frame), key=lambda e: e["seq"])
        self.assertEqual(latest["value"], value, events)
        self.assertNotIn("act", [e["target"] for e in events if e["type"] == "click"], events)

    def test_user_typing_pauses_the_agent_until_the_field_loses_focus(self):
        agent, url = self.shown_agent_tab("/form")
        typed_at = self.user_typed(url, "hi", "hi")
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
        self.user_clicks(url, "blank")
        self.wait_for(lambda: any(e["type"] == "blur" and e["target"] == "field" for e in self.events()),
                      f"the field kept focus: {self.events()}")
        clicked_at = time.monotonic()
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}),
                                ("input",))
        self.assert_only_user_events("hi")
        time.sleep(max(0, clicked_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        results = self.agent_writes(agent)
        for result in results:
            self.assert_ok(result)
        agent_events = [e for e in self.events() if not e["trusted"]]
        self.assertIn(("input", "field", "hix"), [(e["type"], e["target"], e["value"]) for e in agent_events])
        self.assertIn(("click", "act"), [(e["type"], e["target"]) for e in agent_events])
        tab = self.wait_for(lambda: (t := self.tabs()[agent])["url"] == self.site.origin + "/next" and t,
                            "the agent's navigation did not load")
        self.assertIs(tab["owned"], True)

    def test_user_typing_releases_when_the_user_empties_the_field(self):
        agent, url = self.shown_agent_tab("/form")
        typed_at = self.user_typed(url, ["a", "b", "Backspace", "Backspace"], "")
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}),
                                ("input",))
        self.assertEqual(self.events()[-1]["focused"], "field")
        self.assert_only_user_events("")
        time.sleep(max(0, typed_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        # The field keeps focus, but it is empty. The agent's own text there does not hold it again.
        self.assert_ok(self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"}))
        self.assert_ok(self.call("/api/keypress", {"tabId": agent, "selector": "#field", "key": "z"}))
        values = [(e["type"], e["value"]) for e in self.events() if not e["trusted"]]
        self.assertIn(("input", "x"), values)
        self.assertIn(("input", "xz"), values)
        self.assertIs(self.tabs()[agent]["owned"], True)

    def user_tabs_into_the_frame(self, url):
        """The user clicks the page and presses Tab into the frame's field. The parent's synthesized
        click is not routed into an out-of-process frame, and focus reaches the frame asynchronously."""
        self.user_clicks(url, "blank")
        self.wait_for(lambda: self.events("click"), f"the user's click was not reported: {self.events()}")
        self.user_types(url, ["Tab"])
        self.wait_for(lambda: any((e["type"], e["target"], e["frame"]) == ("focus", "field", True)
                                  for e in self.events()), f"Tab did not focus the frame's field: {self.events()}")

    def frame_field_ref(self, tab):
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        self.assertRegex(snapshot, r'iframe "editor" \[ref=e\d+\] \[frame=cross_origin src="http://localhost:\d+/frame"\]')
        match = re.search(r'textbox "draft" \[ref=(e\d+)\]', snapshot)
        self.assertIsNotNone(match, snapshot)
        return match.group(1)

    def agent_frame_writes(self, tab, field):
        """The agent's type and press_key into the frame's field, click and navigate, each answered."""
        return [self.call(path, {**body, "tabId": tab}) for path, body in (
            ("/api/type", {"ref": field, "text": "x", "clear": False}),
            ("/api/keypress", {"ref": field, "key": "z"}),
            ("/api/click", {"selector": "#act"}),
            ("/api/navigate", {"url": self.site.origin + "/next"}),
        )]

    def test_user_typing_in_a_cross_origin_frame_pauses_the_agent_until_the_field_loses_focus(self):
        agent, url = self.shown_agent_tab("/framed")
        field = self.frame_field_ref(agent)
        self.user_tabs_into_the_frame(url)
        typed_at = self.user_typed(url, "hi", "hi", click=False)
        typing = sorted((e for e in self.events() if e["type"] == "input" or e["key"] in ("h", "i")),
                        key=lambda e: (e["frame"], e["seq"]))
        self.assertEqual([(e["type"], e["target"], e["frame"]) for e in typing],
                         [("keydown", "field", True), ("input", "field", True)] * 2, self.events())
        reasons = [self.assert_user_active(r, ("input", "typing")) for r in self.agent_frame_writes(agent, field)]
        self.assertEqual(reasons[0], "input", reasons)

        time.sleep(max(0, typed_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))
        for result in self.agent_frame_writes(agent, field) + [self.call("/api/close-tab", {"tabId": agent})]:
            self.assert_user_active(result, ("typing",))
            self.assertEqual(result["state"]["field"], "input", result)
        self.assert_only_user_events("hi", frame=True)
        tab = self.tabs()[agent]
        self.assertEqual((tab["url"], tab["title"], tab["owned"]), (url, "framed", True))

        # The user clicks the page outside the frame, so the frame's field loses focus; the click
        # starts a new input window.
        self.user_clicks(url, "blank")
        self.wait_for(lambda: any((e["type"], e["target"], e["frame"]) == ("blur", "field", True) for e in self.events()),
                      f"the frame's field kept focus: {self.events()}")
        clicked_at = time.monotonic()
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "ref": field, "text": "x"}), ("input",))
        self.assert_only_user_events("hi", frame=True)
        time.sleep(max(0, clicked_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        for result in self.agent_frame_writes(agent, field):
            self.assert_ok(result)
        agent_events = [e for e in self.events() if not e["trusted"]]
        self.assertIn(("input", "field", True, "hix"), [(e["type"], e["target"], e["frame"], e["value"])
                                                      for e in agent_events])
        self.assertIn(("input", "field", True, "hixz"), [(e["type"], e["target"], e["frame"], e["value"])
                                                       for e in agent_events])
        self.assertIn(("click", "act", False), [(e["type"], e["target"], e["frame"]) for e in agent_events])
        tab = self.wait_for(lambda: (t := self.tabs()[agent])["url"] == self.site.origin + "/next" and t,
                            "the agent's navigation did not load")
        self.assertIs(tab["owned"], True)

    def test_user_typing_in_a_cross_origin_frame_releases_when_the_user_empties_the_field(self):
        agent, url = self.shown_agent_tab("/framed")
        field = self.frame_field_ref(agent)
        self.user_tabs_into_the_frame(url)
        typed_at = self.user_typed(url, ["a", "b", "Backspace", "Backspace"], "", click=False)
        self.assert_user_active(self.call("/api/type", {"tabId": agent, "ref": field, "text": "x"}), ("input",))
        latest = max((e for e in self.events() if e["frame"]), key=lambda e: e["seq"])
        self.assertEqual(latest["focused"], "field", self.events())
        self.assert_only_user_events("", frame=True)
        time.sleep(max(0, typed_at + USER_INPUT_WINDOW_S + 0.3 - time.monotonic()))

        # The field keeps focus, but it is empty. The agent's own text there does not hold it again.
        self.assert_ok(self.call("/api/type", {"tabId": agent, "ref": field, "text": "x"}))
        self.assert_ok(self.call("/api/keypress", {"tabId": agent, "ref": field, "key": "z"}))
        values = [(e["type"], e["frame"], e["value"]) for e in self.events() if not e["trusted"]]
        self.assertIn(("input", True, "x"), values)
        self.assertIn(("input", True, "xz"), values)
        self.assertIs(self.tabs()[agent]["owned"], True)

    def slow_request(self):
        """When the fixture server got the slow page's request."""
        return self.wait_for(lambda: self.site.reports.get("slow"), "the slow page was not requested")[0]["at"]

    def test_a_document_the_user_loads_from_a_link_pauses_the_agent_until_it_loads(self):
        agent, url = self.shown_agent_tab("/nav")
        # Past the grace in which a load counts as the agent's after its last command on the tab.
        time.sleep(AGENT_EFFECT_GRACE_S + 0.2)
        self.user_clicks(url, "link")
        requested = self.slow_request()
        for result in self.agent_writes(agent):
            self.assert_user_active(result, ("navigating",))
            self.assertLess(result["state"]["sinceMs"], 3000, result)
        self.assertLess(time.monotonic() - requested, 3, "the writes were not refused while the document loaded")
        self.assertEqual([e for e in self.events() if not e["trusted"]], [])

        # Once the document has loaded, the agent's writes act on it.
        loaded = self.site.origin + "/slow?ms=3000&page=/form"
        self.wait_for(lambda: (t := self.tabs()[agent])["url"] == loaded and t["title"] == "form",
                      "the user's document did not load")
        deadline = time.monotonic() + 2
        while (result := self.call("/api/type", {"tabId": agent, "selector": "#field", "text": "x"})).get("code") \
                == "user_active":
            self.assert_user_active(result, ("navigating",))
            self.assertLess(time.monotonic(), deadline, result)
            time.sleep(0.1)
        self.assert_ok(result)
        self.assertIn(("input", "x"), [(e["type"], e["value"]) for e in self.events() if not e["trusted"]])
        self.assertIs(self.tabs()[agent]["owned"], True)

    def test_a_document_the_user_loads_pauses_the_agent_for_at_most_10_s(self):
        agent, url = self.shown_agent_tab("/form")
        time.sleep(AGENT_EFFECT_GRACE_S + 0.2)
        started = time.monotonic()
        # As from the address bar; the page answers only after the window.
        self.assertTrue(self.parent(USER_LOADS, url, self.site.origin + "/slow?ms=15000&page=/opened"))
        requested = self.slow_request()
        for result in self.agent_writes(agent):
            self.assert_user_active(result, ("navigating",))
        self.assertEqual([e for e in self.events() if not e["trusted"] or e["type"] in ("keydown", "mousedown")], [],
                         "the user gave the page no input and the agent's writes did not run")

        refused = []
        while (result := self.call("/api/navigate", {"tabId": agent, "url": self.site.origin + "/next"})).get("code") \
                == "user_active":
            self.assert_user_active(result, ("navigating",))
            refused.append(result["state"]["sinceMs"])
            self.assertLess(time.monotonic(), requested + USER_NAVIGATION_WINDOW_S + 1.5, refused)
            time.sleep(0.2)
        succeeded = time.monotonic()
        self.assert_ok(result)
        self.assertTrue(refused, "the agent's navigation was never refused")
        self.assertLess(max(refused), USER_NAVIGATION_WINDOW_S * 1000, refused)
        self.assertGreater(max(refused), (USER_NAVIGATION_WINDOW_S - 1) * 1000, refused)
        self.assertGreaterEqual(succeeded - started, USER_NAVIGATION_WINDOW_S - 0.1, refused)
        sys.stderr.write(f"\n#397 navigating: the agent's navigate succeeded {succeeded - requested:.2f} s after the "
                         f"user's request reached the server; last refusal at sinceMs {max(refused)}\n")
        # The tab's url changes before its title does.
        tab = self.wait_for(lambda: (t := self.tabs()[agent])["url"] == self.site.origin + "/next"
                            and t["title"] == "next" and t, "the agent's navigation did not load")
        self.assertIs(tab["owned"], True)


if __name__ == "__main__":
    import unittest
    unittest.main()
