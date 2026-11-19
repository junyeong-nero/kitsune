"""Native dialogs in a real browser (#555): an agent tab dismisses them, a user tab keeps them.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The fixture is
the four-way benchmark's confirm task: a button whose handler submits a form only if confirm()
returns true. No one answers a native dialog in an agent's background tab, so before #555 the
click waited on it until it timed out. An agent tab now dismisses a dialog no policy answers and
the action's result reports it; a user tab still shows it, recorded as pending while it is open,
and a command that waits on it fails with dialog_open instead of timing out.
"""
from http.server import BaseHTTPRequestHandler
import json
import threading
import time
import unittest

from kitsune.task_modes import USER_TABS_HEADER
import real_browser_harness as harness
from test_real_browser_user_tab_reads import USER_CLOSES_TAB, USER_OPENS_TAB

CONFIRM_PAGE = """<!doctype html><meta charset="utf-8"><title>projects</title>
<form method="post" action="/archive"><input type="hidden" name="project" value="beta"></form>
<button id="archive" onclick="if (confirm('Archive project beta?')) document.forms[0].submit()">Archive beta</button>
<button id="notice" onclick="alert('Saved'); document.title = 'after alert'">Save</button>"""
# A command that waits on an open dialog fails within the poll interval, far below the bridge timeout.
DIALOG_SECONDS = 5


class _Site:
    def __init__(self):
        self.lock = threading.Lock()
        self.posts = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                self.reply(CONFIRM_PAGE if path == "/confirm" else "<title>other</title>")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with site.lock:
                    site.posts.append(self.path)
                self.reply("<!doctype html><title>archived</title>archived")

            def reply(self, text):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    def take_posts(self, settle=1.0):
        time.sleep(settle)
        with self.lock:
            posts, self.posts = self.posts, []
        return posts


class RealBrowserDialogTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = _Site()
        cls.addClassCleanup(cls.site.server.stop)
        super().setUpClass()

    def parent(self, script, *args):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script % tuple(json.dumps(a) for a in args))
        finally:
            debugger.close()

    def timed_call(self, path, body):
        started = time.monotonic()
        return self.call(path, body), time.monotonic() - started

    def test_agent_tab_dismisses_and_reports_a_confirm(self):
        tab = self.open_tab(self.site.origin + "/confirm")
        result, seconds = self.timed_call("/api/click", {"tabId": tab, "selector": "#archive"})
        self.assert_ok(result)
        self.assertLess(seconds, DIALOG_SECONDS, result)
        self.assertEqual(result["dialogs"], [{"type": "confirm", "message": "Archive project beta?",
                                              "response": False, "auto_dismissed": True}])
        self.assertIn("browser_handle_dialog(accept=true)", result["dialog_hint"])
        self.assertEqual(self.site.take_posts(), [])
        info = self.call("/api/page-info", {"tabId": tab})
        self.assertEqual((info["default"], info["policy"]["accept"]), ("dismiss", None), info)

        # Accepted, the form posts: the click's result describes the page it loaded.
        self.assert_ok(self.call("/api/handle-dialog", {"tabId": tab, "accept": True}))
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#archive"}))
        self.assertEqual(result["navigated"]["title"], "archived", result)
        self.assertNotIn("dialog_hint", result)
        self.assertEqual(self.site.take_posts(), ["/archive"])

    def test_agent_tab_alert_returns_at_once(self):
        tab = self.open_tab(self.site.origin + "/confirm")
        result, seconds = self.timed_call("/api/click", {"tabId": tab, "selector": "#notice"})
        self.assert_ok(result)
        self.assertLess(seconds, DIALOG_SECONDS, result)
        self.assertEqual(result["dialogs"], [{"type": "alert", "message": "Saved", "response": None,
                                              "auto_dismissed": True}])
        self.assertNotIn("dialog_hint", result)
        self.assertEqual(self.call("/api/page-info", {"tabId": tab})["title"], "after alert")

    def test_user_tab_keeps_a_native_dialog_and_reports_it_open(self):
        url = self.site.origin + "/confirm?user"
        self.parent(USER_OPENS_TAB, url, True)
        self.addCleanup(self.parent, USER_CLOSES_TAB, url)
        deadline = time.monotonic() + 15
        while not (tab := next((t for t in self.call("/api/tabs")["tabs"]
                                if t["url"] == url and t["title"] == "projects"), None)):
            self.assertLess(time.monotonic(), deadline, "the user's tab did not load")
            time.sleep(0.1)
        self.assertEqual(self.call("/api/page-info", {"tabId": tab["id"]})["default"], "native")

        # The explicitly writable opt-out lets this client click the user's tab.
        headers = {**self.bridge.auth(), USER_TABS_HEADER: "writable", "Content-Type": "application/json"}
        started = time.monotonic()
        status, _, text = harness._request(self.bridge.http_port, "POST", "/api/click", headers, self.bridge.host,
                                           json.dumps({"tabId": tab["id"], "selector": "#archive"}), timeout=90)
        seconds = time.monotonic() - started
        self.assertEqual(status, 200, text)
        result = json.loads(text)
        self.assertLess(seconds, DIALOG_SECONDS, result)
        self.assert_error(result, "dialog_open", False)
        open_dialog = {"type": "confirm", "message": "Archive project beta?", "response": None, "pending": True}
        self.assertEqual(result["state"]["dialog"], open_dialog)
        info = self.call("/api/page-info", {"tabId": tab["id"]})
        self.assertEqual([(d["type"], d["pending"]) for d in info["dialogs"]], [("confirm", True)], info)
        self.assertEqual(self.site.take_posts(), [])


if __name__ == "__main__":
    unittest.main()
