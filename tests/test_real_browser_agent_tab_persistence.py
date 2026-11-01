"""Issue #376 in a real browser: agent tab ownership survives an extension reload and a
browser restart that restores the tabs.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
browser's startup tab stands in for the user's tab: the agent did not open it.
"""
from http.server import BaseHTTPRequestHandler
import json
import time
from unittest.mock import patch

import real_browser_harness as harness
from kitsune.task_modes import OWNER_HEADER


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
        auth = cls.bridge.auth
        owner = patch.object(cls.bridge, "auth", side_effect=lambda: {**auth(), OWNER_HEADER: "e2e-persistence-owner"})
        owner.start()
        cls.addClassCleanup(owner.stop)

    def tabs(self):
        return self.call("/api/tabs")["tabs"]

    def open_agent_tab(self, path):
        url = self.page.origin + path
        tab_id = self.assert_ok(self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 15000}))["tabId"]
        # Showing an owned tab does not change its owner or require approval.
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab_id}))
        self.assertTrue(next(t for t in self.tabs() if t["id"] == tab_id)["owned"])
        return url

    def assert_read_only(self, result):
        self.assert_error(result, "user_tab_read_only", False)
        for token in ("confirmToken", "confirm_token", "resume_token"):
            self.assertNotIn(token, json.dumps(result))
        self.assertNotEqual(result.get("state", {}).get("reason"), "approval")

    def assert_foreign_read_only(self, tab_id):
        """A different stable client can list the tab, but cannot select, navigate or close it."""
        original = next(t for t in self.tabs() if t["id"] == tab_id)
        auth = self.bridge.auth()
        with patch.object(self.bridge, "auth", return_value={**auth, OWNER_HEADER: "e2e-persistence-foreign"}):
            self.assertIs(next(t for t in self.tabs() if t["id"] == tab_id)["owned"], False)
            for path, body in (
                ("/api/switch-tab", {"tabId": tab_id}),
                ("/api/navigate", {"url": self.page.origin + "/foreign", "tabId": tab_id}),
                ("/api/close-tab", {"tabId": tab_id}),
            ):
                self.assert_read_only(self.call(path, body))
                unchanged = next(t for t in self.tabs() if t["id"] == tab_id)
                for key in ("url", "title", "active"):
                    self.assertEqual(unchanged[key], original[key])
        self.assertIs(next(t for t in self.tabs() if t["id"] == tab_id)["owned"], True)

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
        self.assert_foreign_read_only(agent["id"])
        self.assert_ok(self.call("/api/switch-tab", {"tabId": agent["id"]}))
        # Only the stable owner can mutate its tab; the startup user tab stays read-only.
        self.assert_ok(self.call("/api/navigate", {"url": self.page.origin + "/next", "tabId": agent["id"]}))
        for path, body in (
            ("/api/navigate", {"url": self.page.origin + "/next", "tabId": users[0]["id"]}),
            ("/api/close-tab", {"tabId": users[0]["id"]}),
        ):
            self.assert_read_only(self.call(path, body))
            user = next(t for t in self.tabs() if t["id"] == users[0]["id"])
            self.assertEqual((user["url"], user["title"]), (users[0]["url"], users[0]["title"]))
        self.assert_ok(self.call("/api/close-tab", {"tabId": agent["id"]}))


class RealBrowserAgentTabReloadTests(_OwnershipChecks, harness.RealBrowserTestCase):
    def reload_extension(self):
        generation = self.bridge.status()["extension_generation"]
        self.assert_ok(self.call("/api/reload-extension", {}))
        self.wait_for_extension(after_generation=generation)

    def test_owned_survives_an_extension_reload(self):
        url = self.open_agent_tab("/agent-reload")
        self.assert_foreign_read_only(next(t["id"] for t in self.tabs() if t["url"] == url))
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

    def test_privileged_duplicate_without_trusted_input_inherits_its_opener_after_reload(self):
        url = self.open_agent_tab("/agent-original")
        original_id = next(t["id"] for t in self.tabs() if t["url"] == url)
        # Keep the original settling delay, beyond the agent backgrounding grace. It is
        # not a trusted-input timing test: this debugger operation sends no user input.
        time.sleep(2.5)
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            duplicate = debugger.evaluate_in_parent(f"""(async () => {{
              const window = Services.wm.getMostRecentWindow('navigator:browser');
              const original = window.gBrowser.tabs.find(tab => tab.linkedBrowser.currentURI.spec === {url!r});
              const duplicate = window.gBrowser.duplicateTab(original);
              await new Promise(resolve => window.setTimeout(resolve, 1000));
              const extension = WebExtensionPolicy.getByID({harness.EXTENSION_ID!r}).extension;
              return extension.tabManager.convert(duplicate);
            }})()""")
        finally:
            debugger.close()
        # Check real WebExtension metadata before attributing ownership to opener inheritance.
        self.assertEqual(duplicate.get("openerTabId"), original_id, duplicate)
        for when in ("before", "after"):
            tabs = self.tabs()
            copies = [t for t in tabs if t["url"] == url]
            self.assertEqual({t["id"] for t in copies}, {original_id, duplicate["id"]}, tabs)
            self.assertTrue(all(t["owned"] for t in copies), f"{when} a reload: {tabs}")
            self.assert_foreign_read_only(duplicate["id"])
            if when == "before":
                self.reload_extension()
        for tab_id in (original_id, duplicate["id"]):
            self.assert_ok(self.call("/api/close-tab", {"tabId": tab_id}))

    def test_privileged_undo_close_drops_copied_ownership_after_reload(self):
        url = self.page.origin + "/agent-closed"
        closed_id = self.assert_ok(self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 15000}))["tabId"]
        self.assert_ok(self.call("/api/close-tab", {"tabId": closed_id}))
        time.sleep(2.5)  # Retain SessionStore settling, not a trusted user-input classification delay.
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            debugger.evaluate_in_parent("""(async () => {
              const window = Services.wm.getMostRecentWindow('navigator:browser');
              window.SessionStore.undoCloseTab(window, 0);
              await new Promise(resolve => window.setTimeout(resolve, 1000));
              return true;
            })()""")
        finally:
            debugger.close()
        for when in ("before", "after"):
            (reopened,) = [t for t in self.tabs() if t["url"] == url]
            self.assertIs(reopened["owned"], False, f"{when} a reload: {reopened}")
            for path, body in (
                ("/api/navigate", {"url": self.page.origin + "/next", "tabId": reopened["id"]}),
                ("/api/close-tab", {"tabId": reopened["id"]}),
            ):
                self.assert_read_only(self.call(path, body))
                tab = next(t for t in self.tabs() if t["id"] == reopened["id"])
                self.assertEqual((tab["url"], tab["title"]), (url, reopened["title"]))
            if when == "before":
                self.reload_extension()
        # The unowned reopened tab closes with this class's temporary browser.


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
        self.assert_foreign_read_only(next(t["id"] for t in self.tabs() if t["url"] == url))
        self.restart_browser(url)
        self.assert_still_owned(url)
