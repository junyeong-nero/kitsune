"""Issue #139 in a real browser: the agent shares the browser without moving the user.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
user's actions come from the browser's parent process over the remote debugging protocol
(harness.RemoteDebugger): it loads the user's pages like the address bar does, and its
nsIDOMWindowUtils mouse events reach the page as trusted input, as a user's clicks do.
Kitsune's own events are synthetic, so only these count as the user.

The first class allows popups, so the browser itself opens (and shows) a target=_blank
link's tab; the default-prefs class keeps the popup blocker, so the extension opens it.
"""
from http.server import BaseHTTPRequestHandler
import json
import time
import unittest

import real_browser_harness as harness

PAGES = {
    "/user": "<title>user</title><h1>user page</h1><textarea id='draft'></textarea>"
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

# The user presses and releases the mouse over the page they see.
USER_CLICKS = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
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

    def user_view(self):
        """What the user sees: the shown tab, the URLs of their tabs, and which window has focus."""
        tabs = self.tabs()
        windows = self.call("/api/windows")["windows"]
        return {"shown": [t["id"] for t in tabs.values() if t["active"]],
                "user_tabs": {i: t["url"] for i, t in tabs.items() if not t["owned"]},
                "focused": [w["id"] for w in windows if w["focused"]]}

    def user_page(self):
        """The user's shown tab, on /user, loaded by the user (not owned by the agent)."""
        url = self.page.origin + "/user"
        shown = next(t for t in self.tabs().values() if t["active"])
        if shown["url"] != url:
            self.parent(USER_NAVIGATES, url)
            self.wait_for(lambda: self.tabs()[shown["id"]]["url"] == url
                          and self.tabs()[shown["id"]]["title"] == "user", "the user page did not load")
            time.sleep(0.5)  # the load is complete, not a navigation in progress
        self.assertIs(self.tabs()[shown["id"]]["owned"], False)
        return shown["id"]

    def test_agent_opens_reads_and_closes_its_own_tabs_without_moving_the_user(self):
        user_tab = self.user_page()
        before = self.user_view()

        def assert_user_view_unchanged(step):
            self.assertEqual(self.user_view(), before, step)

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
        self.parent(USER_CLICKS)
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
        self.assert_ok(self.call("/api/click", {"selector": "#act", "tabId": user_tab}))
        self.assertEqual(self.tabs()[user_tab]["title"], "acted")


class RealBrowserDefaultPrefsSharedBrowserTests(RealBrowserSharedBrowserTests):
    """Under the default popup blocker the extension opens the link's tab itself (#97), in the background."""
    PROFILE_PREFS = {}


if __name__ == "__main__":
    unittest.main()
