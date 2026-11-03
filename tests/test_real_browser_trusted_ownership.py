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
fixture server with sendBeacon, and a background hook (extension_scripts) reports every
tabs.onCreated with its openerTabId and time.
"""
from http.server import BaseHTTPRequestHandler
import json
import statistics
import sys
import threading
import time
from unittest.mock import patch

import real_browser_harness as harness
from kitsune.task_modes import OWNER_HEADER, USER_TABS_HEADER

OWNER = "e2e-trusted-ownership"
OTHER_OWNER = "e2e-trusted-ownership-other"
# extension/background/background.js USER_OPENED_TAB_WINDOW_MS and content.js USER_INPUT_WINDOW_MS.
USER_OPENED_TAB_WINDOW_MS = 1000
USER_INPUT_WINDOW_S = 3
DELAY_RUNS = 5

# Every page reports its trusted and untrusted events, with the user's field's value and the
# focused element, so the test sees what reached the page without asking the page.
REPORTER = """<script>
const field = document.getElementById('field');
for (const type of ['pointerdown', 'mousedown', 'keydown', 'input', 'click', 'auxclick', 'focus', 'blur']) {
  addEventListener(type, e => navigator.sendBeacon('/event', JSON.stringify({
    type, trusted: e.isTrusted, target: e.target && e.target.id || '', key: e.key || null, at: Date.now(),
    value: field ? field.value : null, focused: document.activeElement && document.activeElement.id || '',
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
    "/opened": "<title>opened</title><h1>opened</h1>",
    "/next": "<title>next</title><h1>next</h1>",
}
# Page coordinates (CSS px) of each element's middle.
POINTS = {"link": (150, 20), "field": (150, 20), "blank": (150, 220)}

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

# The user presses keys (key names: characters or Backspace) into the focused element of the
# page shown at url. Throws if url is not the shown page or a key is not dispatched.
USER_TYPES = """(async () => {
  const [url, keys] = [%s, %s];
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const shown = win.gBrowser.selectedBrowser;
  if (shown.currentURI.spec !== url) throw new Error('the user sees ' + shown.currentURI.spec + ', not ' + url);
  const tip = Cc['@mozilla.org/text-input-processor;1'].createInstance(Ci.nsITextInputProcessor);
  if (!tip.beginInputTransaction(win, () => true)) throw new Error('no input transaction');
  for (const key of keys) {
    const init = key === 'Backspace' ? {key, code: 'Backspace', keyCode: 8}
      : {key, code: 'Key' + key.toUpperCase(), keyCode: key.toUpperCase().charCodeAt(0)};
    const event = new win.KeyboardEvent('', init);
    tip.keydown(event);
    tip.keyup(event);
  }
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

    def user_types(self, url, keys):
        self.assertTrue(self.parent(USER_TYPES, url, list(keys)))

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
        self.assertTrue(all(e["trusted"] for e in self.events()), self.events())

    # -- #397 --

    def agent_writes(self, tab):
        """The agent's type, press_key, click and navigate on its own tab, each answered."""
        return [self.call(path, {**body, "tabId": tab}) for path, body in (
            ("/api/type", {"selector": "#field", "text": "x", "clear": False}),
            ("/api/keypress", {"selector": "#field", "key": "z"}),
            ("/api/click", {"selector": "#act"}),
            ("/api/navigate", {"url": self.site.origin + "/next"}),
        )]

    def user_typed(self, url, keys, value):
        """The user clicks into the field and types keys; returns when the page has value."""
        self.user_clicks(url, "field")
        self.user_types(url, keys)
        self.wait_for(lambda: (inputs := self.events("input")) and inputs[-1]["value"] == value,
                      f"the user's typing did not leave {value!r}: {self.events()}")
        return time.monotonic()

    def assert_only_user_events(self, value):
        events = self.events()
        self.assertTrue(events and all(e["trusted"] for e in events), events)
        self.assertEqual(events[-1]["value"], value, events)
        self.assertNotIn("act", [e["target"] for e in events if e["type"] == "click"], events)

    def test_user_typing_pauses_the_agent_until_the_field_loses_focus(self):
        agent, url = self.shown_agent_tab("/form")
        typed_at = self.user_typed(url, "hi", "hi")
        reasons = [self.assert_user_active(r, ("input", "typing")) for r in self.agent_writes(agent)]
        self.assertLess(time.monotonic() - typed_at, USER_INPUT_WINDOW_S, "the writes ran inside the input window")
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


if __name__ == "__main__":
    import unittest
    unittest.main()
