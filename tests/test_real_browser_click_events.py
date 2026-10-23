"""browser_click's synthetic events match a primary-button click, in a real browser (#201).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture page records the properties of every pointer and mouse event it receives, and
counts clicks in sessionStorage so the count survives a link's navigation. Its server
records every link visit and form submission, which proves activation server-side.
"""
from http.server import BaseHTTPRequestHandler
import json
import threading
import time
import unittest
from urllib.parse import urlparse

import real_browser_harness as harness

PAGE = """<!doctype html><title>click events</title>
<style>body { margin: 0 } #go { position: absolute; left: 40px; top: 30px; width: 120px; height: 40px }</style>
<button id="go" type="button">Go</button>
<form method="post" action="/submitted" style="margin-top: 100px">
  <input type="hidden" name="field" value="sent"><button id="submit">Send</button>
</form>
<input type="checkbox" id="box"> <input type="checkbox" id="labelled"><label id="label" for="labelled">Agree</label>
<a id="link" href="/landed">Next</a> <button id="off" type="button" disabled>Off</button>
<pre id="log"></pre>
<script>
  const props = ['type', 'isTrusted', 'clientX', 'clientY', 'button', 'buttons', 'detail',
                 'pointerId', 'pointerType'];
  for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    document.addEventListener(type, event => {
      const entry = Object.fromEntries(props.map(name => [name, name in event ? event[name] : null]));
      entry.target = event.target.id;
      log.textContent += JSON.stringify(entry) + '\\n';
      if (type === 'click') sessionStorage.clicks = Number(sessionStorage.clicks || 0) + 1;
    }, true);
  }
</script>"""

# #205: the notice ignores untrusted clicks, as CHZZK's did in #200; #vanish removes itself.
NOTICE = """<!doctype html><title>notice</title>
<div id="notice" role="alertdialog">Ad blocker notice <button id="ok" type="button">OK</button></div>
<button id="vanish" type="button">Vanish</button>
<script>
  ok.addEventListener('click', event => { if (event.isTrusted) notice.remove(); });
  vanish.addEventListener('click', () => vanish.remove());
</script>"""

EVENTS = ["pointerover", "pointerenter", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]
STATE = """JSON.stringify({events: (document.getElementById('log')?.textContent || '').trim().split('\\n')
    .filter(Boolean).map(line => JSON.parse(line)), clicks: Number(sessionStorage.clicks || 0),
    box: document.getElementById('box')?.checked, labelled: document.getElementById('labelled')?.checked,
    path: location.pathname})"""


class ClickSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                with site.lock:
                    site.requests.append(("GET", path))
                self.reply(PAGE if path == "/" else NOTICE if path == "/notice"
                           else "<!doctype html><title>landed</title>landed")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with site.lock:
                    site.requests.append(("POST", urlparse(self.path).path))
                self.reply("<!doctype html><title>submitted</title>submitted")

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

    def visits(self, method, path, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.count(method, path) < count:
            time.sleep(0.1)
        time.sleep(0.5)  # a second activation would arrive by now
        return self.count(method, path)

    def count(self, method, path):
        with self.lock:
            return self.requests.count((method, path))

    def stop(self):
        self.server.stop()


class RealBrowserClickEventTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ClickSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def state(self, tab):
        return json.loads(self.call("/api/js", {"tabId": tab, "code": STATE})["result"])

    def click(self, tab, target):
        return self.assert_ok(self.call("/api/click", {"tabId": tab, "includeSnapshot": False, **target}))

    def test_click_events_match_a_primary_button_click(self):
        tab = self.open_tab(self.site.origin + "/")
        ref = self.call("/api/find", {"tabId": tab, "query": "#go"})["results"][0]["ref"]
        for name, target, point in [("selector", {"selector": "#go"}, (100, 50)),
                                    ("ref", {"ref": ref}, (100, 50)),
                                    ("coordinates", {"coords": {"x": 60, "y": 45}}, (60, 45))]:
            with self.subTest(name):
                self.call("/api/js", {"tabId": tab, "code": "document.getElementById('log').textContent = ''"})
                self.click(tab, target)
                events = self.state(tab)["events"]
                self.assertEqual([event["type"] for event in events], EVENTS)
                for event in events:
                    pressed = event["type"] in ("pointerover", "pointerenter", "pointerdown", "mousedown")
                    self.assertEqual(event["target"], "go", event)
                    self.assertIs(event["isTrusted"], False, event)
                    self.assertEqual((event["clientX"], event["clientY"]), point, event)
                    self.assertEqual(event["button"], 0, event)
                    self.assertEqual(event["buttons"], 1 if pressed else 0, event)
                    if event["type"].startswith("pointer"):
                        self.assertEqual((event["pointerType"], event["pointerId"]), ("mouse", 1), event)
                click = events[-1]
                # Firefox dispatches a mouse click as a PointerEvent.
                self.assertEqual((click["detail"], click["pointerType"], click["pointerId"]), (1, "mouse", 1), click)

    def test_each_click_activates_once(self):
        tab = self.open_tab(self.site.origin + "/")
        self.click(tab, {"selector": "#box"})
        self.assertEqual(self.state(tab)["clicks"], 1)
        self.assertIs(self.state(tab)["box"], True)
        self.click(tab, {"selector": "#box"})
        self.assertEqual(self.state(tab)["clicks"], 2)
        self.assertIs(self.state(tab)["box"], False)

        self.call("/api/js", {"tabId": tab, "code": "document.getElementById('log').textContent = ''"})
        self.click(tab, {"selector": "#label"})
        state = self.state(tab)
        self.assertIs(state["labelled"], True, "the label forwards one click to its control")
        self.assertEqual([(event["type"], event["target"]) for event in state["events"] if event["type"] == "click"],
                         [("click", "label"), ("click", "labelled")])

        # As el.click() does, a disabled control gets no click.
        self.click(tab, {"selector": "#off"})
        self.assertEqual(self.state(tab)["clicks"], 4)

        self.click(tab, {"selector": "#submit"})
        self.assertEqual(self.site.visits("POST", "/submitted", 1), 1, "one form submission")
        self.assertEqual(self.state(tab)["clicks"], 5)  # sessionStorage outlives the page

        tab = self.open_tab(self.site.origin + "/")
        self.click(tab, {"selector": "#link"})
        self.assertEqual(self.site.visits("GET", "/landed", 1), 1, "one link navigation")
        state = self.state(tab)
        self.assertEqual((state["path"], state["clicks"]), ("/landed", 1))

    def test_unmet_click_suggests_a_trusted_click_only_while_the_target_is_visible(self):
        tab = self.open_tab(self.site.origin + "/notice")
        expect = {"text_gone": "Ad blocker notice", "timeout": 300}
        ignored = self.call("/api/click", {"tabId": tab, "selector": "#ok", "expect": expect})
        self.assertEqual(ignored.get("code"), "outcome_not_observed", ignored)
        self.assertIs(ignored["dispatched"], True)
        self.assertIn("browser_trusted_click", ignored["hint"], ignored)
        self.assertIn("the browse capability", ignored["hint"])

        ref = self.call("/api/find", {"tabId": tab, "query": "#ok"})["results"][0]["ref"]
        by_ref = self.call("/api/click", {"tabId": tab, "ref": ref, "expect": expect})
        self.assertIn("browser_trusted_click", by_ref.get("hint", ""), by_ref)

        gone = self.call("/api/click", {"tabId": tab, "selector": "#vanish", "expect": expect})
        self.assertEqual(gone.get("code"), "outcome_not_observed", gone)
        self.assertNotIn("browser_trusted_click", gone["hint"], gone)

        plain = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#ok"}))
        self.assertNotIn("hint", plain)


if __name__ == "__main__":
    unittest.main()
