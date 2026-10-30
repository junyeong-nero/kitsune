"""Real OS keyboard input into an isolated, visible browser (issues #346 and #347).

Opt-in twice: besides KITSUNE_E2E_BROWSER (see tests/real_browser_harness.py), it runs only on
macOS with KITSUNE_E2E_TRUSTED_INPUT=1, because it sends real key events and clicks with the
real pointer. Run it while nobody uses the machine, with Quartz and Accessibility permission:

    KITSUNE_E2E_BROWSER=/path/to/Firefox.app/Contents/MacOS/firefox KITSUNE_E2E_TRUSTED_INPUT=1 \\
        uv run --extra macos python -m unittest discover -s tests -p test_real_browser_trusted_keys.py -v

On a fresh headed profile, Zen's first-run welcome screen hides every page, so tabs report
about:blank and have no size (#360). The harness now sets zen.welcome-screen.seen, so Zen should
work, but this suite has not been verified on Zen yet; it is verified on Firefox.

The suite's browser runs headed on a temporary profile. Before each command the test brings that
browser process (by pid, not by name, so another browser of the same name is left alone) to the
front, and the requests pass focus=false. The paste test saves the clipboard's text and puts it
back; other clipboard content (an image) is not kept.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import subprocess
import sys
import time
import unittest

import real_browser_harness as harness
from test_bridge_security import _request

OTP = "482193"
PASTED = "KITSUNE_FAKE_PASTE_314159"

PAGES = {
    # Rejects synthetic input like the #346 sign-in field: an untrusted key or input event clears it.
    "/otp": """<!doctype html><title>otp</title><input id=otp autocomplete=one-time-code>
<script>
window.events = [];
const otp = document.getElementById('otp');
for (const type of ['keydown', 'beforeinput', 'input']) otp.addEventListener(type, e => {
  window.events.push([type, e.isTrusted]);
  if (!e.isTrusted) otp.value = '';
});
</script>""",
    # The page moves the focus away after the second character.
    "/blur": """<!doctype html><title>blur</title><input id=field><button id=other>other</button>
<script>
const field = document.getElementById('field');
field.addEventListener('input', () => { if (field.value.length >= 2) document.getElementById('other').focus(); });
</script>""",
    "/paste": """<!doctype html><title>paste</title><textarea id=box></textarea>
<script>
window.pastes = [];
document.getElementById('box').addEventListener('paste', e => window.pastes.push(e.isTrusted));
</script>""",
    # The user's tab: records every key it receives.
    "/user": """<!doctype html><title>user</title><p>The user's page</p>
<script>window.keys = []; addEventListener('keydown', e => window.keys.push(e.key), true);</script>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path.split("?")[0])
        if page is None:
            self.send_error(404)
            return
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("KITSUNE_E2E_TRUSTED_INPUT") == "1",
                     "sends real OS keys: set KITSUNE_E2E_TRUSTED_INPUT=1 on macOS")
class RealBrowserTrustedKeysTests(harness.RealBrowserTestCase):
    HEADED = True

    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def front(self):
        """Bring this suite's browser (by pid) to the front, as a user would show it."""
        script = (f'tell application "System Events" to set frontmost of '
                  f'(first process whose unix id is {self.browser.process.pid}) to true')
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        time.sleep(0.8)

    def post(self, path, body):
        """(status, reply) of a bridge request, without call()'s 200 assertion."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json"}
        status, _, text = _request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                   json.dumps(body), timeout=60)
        return status, json.loads(text)

    def js(self, tab, code):
        """code's value in the tab, as JSON. browser_js runs in the content script, which reaches
        the page's own globals (page.events, ...) through `page`."""
        reply = self.call("/api/js", {"tabId": tab, "code": f"(page => JSON.stringify({code}))(window.wrappedJSObject)"})
        self.assertIn("result", reply, reply)
        return json.loads(reply["result"])

    def user_and_agent_tabs(self, agent_path):
        user = self.open_tab(f"{self.site.origin}/user")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": user}))
        agent = self.open_tab(f"{self.site.origin}{agent_path}")
        self.front()
        return user, agent

    def test_types_with_the_os_keyboard_where_synthetic_input_is_rejected(self):
        user, agent = self.user_and_agent_tabs("/otp")
        # The control: Kitsune's synthetic typing is rejected by the field.
        self.call("/api/type", {"tabId": agent, "selector": "#otp", "text": OTP})
        self.assertEqual(self.js(agent, "document.getElementById('otp').value"), "")
        self.js(agent, "page.events.length = 0")

        self.front()
        status, result = self.post("/api/trusted-type", {"tabId": agent, "selector": "#otp", "text": OTP,
                                                         "focus": False})
        self.assertEqual(status, 200, result)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual((result["keysSent"], result["typed"]), (6, 6))
        self.assertNotIn(OTP, json.dumps(result))
        self.assertEqual(self.js(agent, "document.getElementById('otp').value"), OTP)
        events = self.js(agent, "page.events")
        self.assertTrue(events and all(trusted for _, trusted in events), events)
        # The user's tab was shown again and received no key.
        time.sleep(0.5)
        self.assertEqual(self.js(user, "page.keys"), [])
        tabs = {tab["id"]: tab for tab in self.call("/api/tabs")["tabs"]}
        self.assertTrue(tabs[user]["active"], tabs[user])

    def test_stops_when_the_page_moves_the_focus(self):
        _, agent = self.user_and_agent_tabs("/blur")
        status, result = self.post("/api/trusted-type", {"tabId": agent, "selector": "#field", "text": "abcdef",
                                                         "focus": False, "delayMs": 100})
        self.assertEqual(result.get("code"), "keyboard_focus_lost", result)
        self.assertEqual(result["state"], {"reason": "target_not_focused", "keysSent": 2})
        self.assertEqual(self.js(agent, "document.getElementById('field').value"), "ab")

    def test_pastes_with_a_real_key_chord(self):
        saved = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        self.addCleanup(lambda: subprocess.run(["pbcopy"], input=saved, text=True))
        subprocess.run(["pbcopy"], input=PASTED, text=True, check=True)
        _, agent = self.user_and_agent_tabs("/paste")
        status, result = self.post("/api/trusted-press-key", {"tabId": agent, "selector": "#box", "keys": "Meta+V",
                                                              "focus": False})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.js(agent, "document.getElementById('box').value"), PASTED)
        self.assertEqual(self.js(agent, "page.pastes"), [True])

    def test_a_denied_chord_presses_nothing(self):
        user, agent = self.user_and_agent_tabs("/paste")
        status, result = self.post("/api/trusted-press-key", {"tabId": agent, "selector": "#box", "keys": "Meta+W",
                                                              "focus": False})
        self.assertEqual(result.get("code"), "key_denied", result)
        tabs = {tab["id"] for tab in self.call("/api/tabs")["tabs"]}
        self.assertIn(agent, tabs, "the tab is still open")
        self.assertEqual(self.js(user, "page.keys"), [])


if __name__ == "__main__":
    unittest.main()
