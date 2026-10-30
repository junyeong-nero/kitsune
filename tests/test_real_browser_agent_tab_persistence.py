"""Issue #376 in a real browser: agent tab ownership survives an extension reload and a
browser restart that restores the tabs.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
browser's startup tab stands in for the user's tab: the agent did not open it.
"""
from http.server import BaseHTTPRequestHandler
import time

import real_browser_harness as harness


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<title>{self.path}</title><h1>{self.path}</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _OwnershipChecks:
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def tabs(self):
        return self.call("/api/tabs")["tabs"]

    def open_agent_tab(self, path):
        url = self.page.origin + path
        tab_id = self.assert_ok(self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 15000}))["tabId"]
        # The agent's tab is the one the user views, so only ownership exempts it from the hold (#364).
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab_id}))
        self.assertTrue(next(t for t in self.tabs() if t["id"] == tab_id)["owned"])
        return url

    def assert_still_owned(self, url):
        """After the reload or restart: the tab at url is the agent's, every other tab the user's."""
        tabs = self.tabs()
        agent = [t for t in tabs if t["url"] == url]
        self.assertEqual(len(agent), 1, tabs)
        agent = agent[0]
        self.assertIs(agent["owned"], True, tabs)
        users = [t for t in tabs if t["id"] != agent["id"]]
        self.assertTrue(users, tabs)
        self.assertTrue(all(t["owned"] is False for t in users), tabs)
        self.assert_ok(self.call("/api/switch-tab", {"tabId": agent["id"]}))
        # Neither held for being viewed nor a protected user tab (both on by default), and it
        # closes without the user's approval.
        self.assert_ok(self.call("/api/navigate", {"url": self.page.origin + "/next", "tabId": agent["id"]}))
        self.assert_error(self.call("/api/navigate", {"url": self.page.origin + "/next", "tabId": users[0]["id"]}),
                          "user_tab", False)
        self.assert_ok(self.call("/api/close-tab", {"tabId": agent["id"]}))
        self.assert_error(self.call("/api/close-tab", {"tabId": users[0]["id"]}), "user_tab", False)


class RealBrowserAgentTabReloadTests(_OwnershipChecks, harness.RealBrowserTestCase):
    def reload_extension(self):
        generation = self.bridge.status()["extension_generation"]
        self.assert_ok(self.call("/api/reload-extension", {}))
        self.wait_for_extension(after_generation=generation)

    def test_owned_survives_an_extension_reload(self):
        url = self.open_agent_tab("/agent-reload")
        self.reload_extension()
        self.assert_still_owned(url)

    def test_the_agents_duplicate_stays_owned_after_a_reload(self):
        # Firefox copies the original's value to the duplicate before markAgentTab runs.
        url = self.open_agent_tab("/agent-duplicated")
        original = next(t["id"] for t in self.tabs() if t["url"] == url)
        duplicate = self.assert_ok(self.call("/api/duplicate-tab", {"tabId": original}))["tabId"]
        time.sleep(1)
        self.reload_extension()
        owned = {t["id"]: t["owned"] for t in self.tabs()}
        self.assertEqual((owned[original], owned[duplicate]), (True, True), owned)
        for tab_id in (original, duplicate):
            self.assert_ok(self.call("/api/close-tab", {"tabId": tab_id}))

    def test_tabs_the_user_duplicates_or_reopens_stay_the_users_after_a_reload(self):
        # Firefox copies tab values to a duplicate and restores them with a reopened tab.
        original = self.open_agent_tab("/agent-original")
        closed = self.page.origin + "/agent-closed"
        closed_id = self.assert_ok(self.call("/api/new-tab", {"url": closed, "wait": True, "timeout": 15000}))["tabId"]
        self.assert_ok(self.call("/api/close-tab", {"tabId": closed_id}))
        # A tab opened from a tab the agent just acted on counts as the agent's (AGENT_EFFECT_GRACE_MS).
        time.sleep(2.5)
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            debugger.evaluate_in_parent(f"""(async () => {{
              const window = Services.wm.getMostRecentWindow('navigator:browser');
              const original = window.gBrowser.tabs.find(tab => tab.linkedBrowser.currentURI.spec === {original!r});
              window.gBrowser.duplicateTab(original);
              window.SessionStore.undoCloseTab(window, 0);
              await new Promise(resolve => window.setTimeout(resolve, 1000));
              return true;
            }})()""")
        finally:
            debugger.close()
        for when in ("before", "after"):
            tabs = self.tabs()
            owned = sorted((t["url"], t["owned"]) for t in tabs if t["url"] in (original, closed))
            self.assertEqual(owned, [(closed, False), (original, False), (original, True)], f"{when} a reload: {tabs}")
            if when == "before":
                self.reload_extension()
        # The user's copies close with this class's browser.
        for tab in self.tabs():
            if tab["url"] == original and tab["owned"]:
                self.assert_ok(self.call("/api/close-tab", {"tabId": tab["id"]}))


class RealBrowserAgentTabRestartTests(_OwnershipChecks, harness.RealBrowserTestCase):
    # Restore the previous session's windows and tabs at startup.
    PROFILE_PREFS = {"browser.startup.page": 3}

    def restart_browser(self, url):
        """Quit the browser normally, so it saves the session, and start it on the same profile.

        The extension is a temporary add-on, so the restart installs it again, after the session
        is restored. A permanently installed extension starts with the browser instead.
        """
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            debugger.evaluate_in_parent(
                "(setTimeout(() => Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit), 100), true)")
        finally:
            debugger.close()
        self.browser.process.wait(timeout=60)
        generation = self.bridge.status()["extension_generation"]
        self.browser.start()
        self.wait_for_extension(after_generation=generation)
        # The agent's tab is listed once the session is back.
        deadline = time.monotonic() + harness.START_TIMEOUT
        while not any(t["url"] == url for t in self.tabs()):
            if time.monotonic() > deadline:
                raise AssertionError(f"the session restore did not bring back {url}: {self.tabs()}")
            time.sleep(0.2)

    def test_owned_survives_a_browser_restart_with_session_restore(self):
        url = self.open_agent_tab("/agent-restart")
        self.restart_browser(url)
        self.assert_still_owned(url)
