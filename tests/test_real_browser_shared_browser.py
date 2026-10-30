"""Issue #139 in a real browser: the agent shares the browser without moving the user.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
user's actions come from the browser's parent process over the remote debugging protocol
(harness.RemoteDebugger): it loads the user's pages like the address bar does, and its
nsIDOMWindowUtils mouse events reach the page as trusted input, as a user's clicks do.
Kitsune's own events are synthetic, so only these count as the user. It also reads the
shown tab with browser_tabs(include_active=True) (#138).

The first class allows popups, so the browser itself opens (and shows) a target=_blank
link's tab; the default-prefs class keeps the popup blocker, so the extension opens it.
"""
from http.server import BaseHTTPRequestHandler
import json
import time
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, task_modes, tools
import real_browser_harness as harness

PAGES = {
    "/user": "<title>user</title><h1>user page</h1><textarea id='draft'></textarea><input id='name'>"
             "<button id='act' onclick=\"document.title='acted'\">Act</button>"
             "<script>addEventListener('mousedown', e => { document.title = 'down:' + e.isTrusted; }, true);</script>",
    "/agent": "<title>agent</title><h1>agent page</h1>"
              "<a id='new' href='/opened' target='_blank'>new tab link</a>",
    "/opened": "<title>opened</title><h1>opened</h1>",
    "/second": "<title>second</title><h1>second user tab</h1>",
}

# The user loads url in the tab they see, as from the address bar.
USER_NAVIGATES = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  win.openTrustedLinkIn(%s, 'current');
  return true;
})()"""

# The user opens url in a tab of their own without switching to it.
USER_OPENS_BACKGROUND_TAB = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  win.gBrowser.addTrustedTab(%s);
  return true;
})()"""

# The user presses and releases the mouse over the page they see. Returns false when the
# browser has no windowUtils.sendMouseEvent (Firefox 158 and Zen built on it removed it).
USER_CLICKS = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  if (typeof win.windowUtils.sendMouseEvent !== 'function') return false;
  const rect = win.gBrowser.selectedBrowser.getBoundingClientRect();
  for (const type of ['mousedown', 'mouseup']) {
    win.windowUtils.sendMouseEvent(type, rect.left + 200, rect.top + 200, 0, 1, 0);
  }
  return true;
})()"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "not found").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserSharedBrowserTests(harness.RealBrowserTestCase):
    PROFILE_PREFS = harness.POPUPS_ALLOWED

    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def parent(self, script, *args):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script % tuple(json.dumps(a) for a in args))
        finally:
            debugger.close()

    def tabs(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.1)

    def user_view(self, agent_windows=()):
        """What the user sees: their shown tab and the URLs of their tabs, outside the agent's
        windows, and which window has focus."""
        tabs = {i: t for i, t in self.tabs().items() if t["windowId"] not in agent_windows}
        windows = self.call("/api/windows", {})["windows"]
        return {"shown": [t["id"] for t in tabs.values() if t["active"] and not t["owned"]],
                "user_tabs": {i: t["url"] for i, t in tabs.items() if not t["owned"]},
                "focused": [w["id"] for w in windows if w["focused"]]}

    def user_page(self):
        """The user's shown tab, on /user, loaded by the user (not owned by the agent)."""
        url = self.page.origin + "/user"
        shown = next(t for t in self.tabs().values() if t["active"])
        if (shown["url"], shown["title"]) != (url, "user"):
            self.parent(USER_NAVIGATES, url)
            self.wait_for(lambda: self.tabs()[shown["id"]]["url"] == url
                          and self.tabs()[shown["id"]]["title"] == "user", "the user page did not load")
            time.sleep(0.5)  # the load is complete, not a navigation in progress
        self.assertIs(self.tabs()[shown["id"]]["owned"], False)
        return shown["id"]

    def test_agent_opens_reads_and_closes_its_own_tabs_without_moving_the_user(self):
        user_tab = self.user_page()
        before = self.user_view()
        agent_windows = set()

        def assert_user_view_unchanged(step):
            self.assertEqual(self.user_view(agent_windows), before, step)

        agent = self.assert_ok(self.call("/api/new-tab", {"url": self.page.origin + "/agent", "wait": True}))["tabId"]
        blank = self.assert_ok(self.call("/api/new-tab", {"url": "about:blank"}))["tabId"]
        tabs = self.tabs()
        self.assertEqual((tabs[agent]["owned"], tabs[blank]["owned"], tabs[user_tab]["owned"]), (True, True, False))
        self.assertEqual((tabs[agent]["active"], tabs[blank]["active"]), (False, False))
        assert_user_view_unchanged("new tabs open in the background")

        self.assertIn("agent page", self.call("/api/snapshot", {"tabId": agent})["snapshot"])
        self.assertIn("agent page", self.call("/api/page-text", {"tabId": agent})["text"])
        for path, body in (("/api/screenshot", {}), ("/api/element-screenshot", {"selector": "h1"}),
                           ("/api/full-page-screenshot", {})):
            shot = self.call(path, {**body, "tabId": agent, "imagePreview": True})
            self.assertTrue(shot.get("dataUrl", "").startswith("data:image/"), (path, shot))
            assert_user_view_unchanged(path)

        clicked = self.assert_ok(self.call("/api/click", {"selector": "#new", "tabId": agent}))
        self.assertEqual(len(clicked["opened_tabs"]), 1, clicked)
        opened = clicked["opened_tabs"][0]["tabId"]
        time.sleep(1)  # let any late activation of the opened tab happen
        self.assertIs(self.tabs()[opened]["owned"], True)
        assert_user_view_unchanged("a tab the agent's click opened stays in the background")

        duplicate = self.assert_ok(self.call("/api/duplicate-tab", {"tabId": agent}))["tabId"]
        self.assertIs(self.tabs()[duplicate]["owned"], True)
        assert_user_view_unchanged("duplicate")

        before_window = {i: t["url"] for i, t in self.tabs().items()}
        window = self.assert_ok(self.call("/api/create-window", {"url": self.page.origin + "/agent"}))
        agent_windows.add(window["windowId"])
        time.sleep(1)  # let the browser add any synced copies and move the focus
        tabs = self.tabs()
        self.assertTrue(window["tabIds"] and all(tabs[t]["owned"] for t in window["tabIds"]), window)
        assert_user_view_unchanged("a new window does not take focus")
        # Zen's window sync fills the agent window with lazy copies of the other windows' tabs.
        # Closing a copy closes its original, so they are not the agent's.
        copies = {i: t for i, t in tabs.items() if t["windowId"] == window["windowId"] and i not in window["tabIds"]}
        originals = {(t["title"], t["url"]) for t in tabs.values() if t["windowId"] != window["windowId"]}
        for t in copies.values():
            self.assertIs(t["owned"], False, t)
            self.assertTrue(any(title == t["title"] and t["url"] in ("about:blank", url) for title, url in originals), t)
        user_copy = next((i for i, t in copies.items() if t["title"] == "user"), None)
        if user_copy is not None:
            self.assert_error(self.call("/api/close-tab", {"tabId": user_copy}), "user_tab", False)
            self.assertIn(user_copy, self.tabs())
            assert_user_view_unchanged("a copy of the user's tab is not closed")
        # Its own copy in the user's window is the agent's.
        self.assertTrue(all(t["owned"] for i, t in tabs.items() if t["windowId"] != window["windowId"]
                            and i not in before_window), tabs)
        self.assert_ok(self.call("/api/close-window", {"windowId": window["windowId"]}))
        assert_user_view_unchanged("close-window")
        # Every tab from before is open on its URL, and no copy is left showing the agent's page.
        self.assertEqual({i: t["url"] for i, t in self.tabs().items()}, before_window, "close-window")

        for tab_id in (opened, duplicate, blank, agent):
            self.assert_ok(self.call("/api/close-tab", {"tabId": tab_id}))
        self.assertFalse({opened, duplicate, blank, agent} & set(self.tabs()))
        assert_user_view_unchanged("closing owned tabs")
        self.assertEqual(self.tabs()[user_tab]["url"], self.page.origin + "/user")

    def test_closing_a_user_tab_is_refused_until_the_user_approves(self):
        self.user_page()
        second_url = self.page.origin + "/second"
        self.parent(USER_OPENS_BACKGROUND_TAB, second_url)
        self.wait_for(lambda: any(t["url"] == second_url for t in self.tabs().values()), "no second user tab")
        second = next(i for i, t in self.tabs().items() if t["url"] == second_url)
        self.assertIs(self.tabs()[second]["owned"], False)

        for body in ({"tabId": second}, {"tabId": second, "force": True}):
            refused = self.call("/api/close-tab", body)
            self.assert_error(refused, "user_tab", False)
            self.assertIn(second, self.tabs(), "the user tab stays open")
        window_id = self.tabs()[second]["windowId"]
        self.assert_error(self.call("/api/close-window", {"windowId": window_id}), "user_tab", False)
        self.assertIn(second, self.tabs(), "the user's window stays open")
        # Batch steps never get approval.
        batch = self.call("/api/batch", {"commands": [{"action": "closeTab", "tabId": second}]})
        self.assertEqual(batch["results"][0]["code"], "user_tab", batch)
        self.assertNotIn("confirmToken", batch["results"][0]["state"])
        self.assertIn(second, self.tabs())

        token = refused["state"]["confirmToken"]
        self.assert_ok(self.call("/api/close-tab", {"tabId": second, "force": True, "confirmToken": token}))
        self.assertNotIn(second, self.tabs())

    def test_recent_trusted_user_input_blocks_agent_writes(self):
        user_tab = self.user_page()
        if not self.parent(USER_CLICKS):
            self.skipTest("the parent process has no windowUtils.sendMouseEvent to send trusted mouse input")
        try:
            self.wait_for(lambda: self.tabs()[user_tab]["title"].startswith("down:"), "no mousedown", timeout=3)
        except AssertionError:
            self.skipTest("the parent process could not deliver mouse input to the page in this browser")
        if self.tabs()[user_tab]["title"] != "down:true":
            self.skipTest("the harness's mouse input reached the page untrusted, so it cannot stand in for a user")

        for path, body in (("/api/click", {"selector": "#act"}), ("/api/type", {"selector": "#draft", "text": "x"}),
                           ("/api/navigate", {"url": self.page.origin + "/agent"})):
            result = self.call(path, {**body, "tabId": user_tab})
            self.assert_error(result, "user_active", True)
            self.assertEqual(result["state"]["reason"], "input", result)
            self.assertIn(result["state"]["input"], ("pointerdown", "mousedown"), result)
        tab = self.tabs()[user_tab]
        self.assertEqual((tab["url"], tab["title"]), (self.page.origin + "/user", "down:true"), "nothing acted")
        # Reads go on.
        self.assertIn("user page", self.call("/api/page-text", {"tabId": user_tab})["text"])

        time.sleep(3.2)  # the user-input window is 3 s
        # The user still views the tab (#326): the click needs their approval.
        held = self.call("/api/click", {"selector": "#act", "tabId": user_tab})
        self.assert_error(held, "user_viewing", False)
        self.assert_ok(self.call("/api/click", {"selector": "#act", "tabId": user_tab,
                                                "confirmToken": held["state"]["confirmToken"]}))
        self.wait_for(lambda: self.tabs()[user_tab]["title"] == "acted", "the click after the window did not act")

    def call_without_hold(self, path, body):
        """POST as a client with KITSUNE_HOLD_VIEWED_TAB=0."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json", task_modes.HOLD_VIEWED_TAB_HEADER: "0"}
        status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                           json.dumps(body), timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def test_the_tab_the_user_views_is_held_until_the_user_approves(self):
        # #326: viewing counts as using. The user gives no input; their tab is just shown.
        user_tab = self.user_page()
        window = self.tabs()[user_tab]["windowId"]
        for path, body in (("/api/click", {"selector": "#act"}), ("/api/type", {"selector": "#draft", "text": "x"}),
                           ("/api/scroll", {"direction": "down"}), ("/api/navigate", {"url": self.page.origin + "/agent"}),
                           ("/api/reload-tab", {})):
            result = self.call(path, {**body, "tabId": user_tab})
            self.assert_error(result, "user_viewing", False)
            self.assertEqual((result["state"]["tabId"], result["state"]["windowId"], result["state"]["reason"]),
                             (user_tab, window, "viewing"), result)
            self.assertIsInstance(result["state"]["browserFocused"], bool)
            self.assertIn("confirmToken", result["state"])
        tab = self.tabs()[user_tab]
        self.assertEqual((tab["url"], tab["title"], tab["active"]), (self.page.origin + "/user", "user", True),
                         "nothing acted")
        # Reads go on, and the agent's own background tab is not held.
        self.assertIn("user page", self.call("/api/page-text", {"tabId": user_tab})["text"])
        agent = self.open_tab(self.page.origin + "/agent")
        self.assert_ok(self.call("/api/navigate", {"url": self.page.origin + "/second", "tabId": agent}))

        # The user approves one click: its token acts once.
        held = self.call("/api/click", {"selector": "#act", "tabId": user_tab})
        approved = {"selector": "#act", "tabId": user_tab, "confirmToken": held["state"]["confirmToken"]}
        self.assert_ok(self.call("/api/click", approved))
        self.wait_for(lambda: self.tabs()[user_tab]["title"] == "acted", "the approved click did not act")
        self.assertEqual(self.call("/api/click", approved)["state"]["tokenRejected"], "used")

        # A client with KITSUNE_HOLD_VIEWED_TAB=0 acts without a confirm step.
        self.assert_ok(self.call_without_hold("/api/navigate", {"url": self.page.origin + "/second", "tabId": user_tab}))
        self.wait_for(lambda: self.tabs()[user_tab]["title"] == "second", "the opted-out navigation did not load")
        self.assertEqual(self.user_view()["shown"], [user_tab], "the user's tab stayed shown")

    def test_fill_form_on_the_viewed_tab_needs_one_approval(self):
        # #326 review: each field is its own bridge request; one token approves the whole tool call.
        user_tab = self.user_page()

        def post(endpoint, data, timeout=15, start_bridge=True):
            headers = {**self.bridge.auth(), "Content-Type": "application/json", **server._gateway_headers()}
            status, _, text = harness._request(self.bridge.http_port, "POST", endpoint, headers, self.bridge.host,
                                               json.dumps(data), timeout=90)
            return json.loads(text)

        fields = [{"name": "Draft", "type": "textbox", "target": "#draft", "value": "hello"},
                  {"name": "Name", "type": "textbox", "target": "#name", "value": "Ada"}]
        fill_form = server._mcp_errors(playwright_tools.browser_fill_form)
        with patch.object(tools, "_post", post), patch.object(playwright_tools, "_current_tab", user_tab):
            refused = json.loads(fill_form(fields=fields).content[0].text)
            self.assert_error(refused, "user_viewing", False)
            self.assertEqual(refused["filled"], [])
            filled = json.loads(fill_form(fields=fields, confirm_token=refused["state"]["confirmToken"]).content[0].text)
        self.assertEqual(filled.get("filled"), ["Draft", "Name"], filled)
        snapshot = self.call("/api/snapshot", {"tabId": user_tab})["snapshot"]  # a read: not held
        self.assertIn('[value="hello"]', snapshot)
        self.assertIn('[value="Ada"]', snapshot)

    def test_tab_list_with_include_active_reads_the_shown_tab_in_one_call(self):
        # #138: the MCP tool composes the inventory and the active page; route it to this bridge.
        user_tab = self.user_page()
        before = self.user_view()
        with patch.object(tools, "_get", self.call), \
                patch.object(tools, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = tools.browser_tabs(include_active=True)

        self.assertIs({t["id"]: t for t in result["tabs"]}[user_tab]["active"], True, result)
        page = result["active_page"]
        self.assertEqual((page["tabId"], page["url"], page["title"]), (user_tab, self.page.origin + "/user", "user"))
        self.assertIn('heading "user page"', page["snapshot"])
        self.assertRegex(page["snapshot"], r'button "Act" \[ref=e\d+\]')
        self.assertEqual(self.user_view(), before, "reading the shown tab moves nothing")


class RealBrowserDefaultPrefsSharedBrowserTests(RealBrowserSharedBrowserTests):
    """Under the default popup blocker the extension opens the link's tab itself (#97), in the background."""
    PROFILE_PREFS = {}


if __name__ == "__main__":
    unittest.main()
