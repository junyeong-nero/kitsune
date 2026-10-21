"""Action races, tab unloads and reconnects in a real browser (issue #68).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it and
what it owns: a headless browser on a temporary profile and HOME, a bridge on spare
ports, and a port-rewritten copy of the extension. This suite adds a local fixture site
and a test-only background script in the copy, which connects to a harness socket to
discard tabs and drop the bridge WebSocket. Fixture buttons report every click to the
fixture site, which is how the tests prove that no action reached the wrong page or
ran twice.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

import real_browser_harness as harness

# The batch (`retry`) bridge timeout for a click; a page held longer makes the bridge give up.
BATCH_COMMAND_TIMEOUT = 30

# Test-only background script: harness commands use the public tabs API, and `ws` is the
# bridge socket declared by background.js in the same background page.
CONTROL_SCRIPT = """'use strict';
let restoreReceiver = null;
(function connectControl() {
  const control = new WebSocket('ws://127.0.0.1:%d');
  control.onmessage = async event => {
    const { id, op, tabId } = JSON.parse(event.data);
    let result;
    try {
      if (op === 'discard') {
        await browser.tabs.discard(tabId);
        result = { discarded: (await browser.tabs.get(tabId)).discarded };
      } else if (op === 'dropBridge') {
        ws.close();
        result = { dropped: true };
      } else if (op === 'missingReceiverOnce') {
        if (restoreReceiver) throw new Error('receiver fault already armed');
        const sendMessage = browser.tabs.sendMessage;
        const stats = { pings: 0, attempts: 0, failures: 0 };
        browser.tabs.sendMessage = async (target, message, ...options) => {
          if (target === tabId) {
            if (message.action === 'ping') stats.pings++;
            if (message.action === 'click') {
              stats.attempts++;
              if (!stats.failures) {
                stats.failures++;
                throw new Error('Could not establish connection. Receiving end does not exist.');
              }
            }
          }
          return sendMessage.call(browser.tabs, target, message, ...options);
        };
        restoreReceiver = () => { browser.tabs.sendMessage = sendMessage; return stats; };
        result = { armed: true };
      } else if (op === 'restoreReceiver') {
        result = restoreReceiver ? restoreReceiver() : {};
        restoreReceiver = null;
      } else {
        result = { error: 'unknown op ' + op };
      }
    } catch (e) {
      result = { error: e.message };
    }
    control.send(JSON.stringify({ id, result }));
  };
  control.onclose = () => setTimeout(connectControl, 500);
})();
"""

# Every fixture button reports its click to /hit, keyed by page name.
PAGES = {
    "buttons": """<button id="act" onclick="fetch('/hit?page={name}')">Act {name}</button>
<a id="slow" href="/slow?name={name}-next&amp;ms={ms}">Slow next</a>""",
    "delayed": """<div id="slot">Loading</div>
<script>setTimeout(() => {{ slot.innerHTML =
  '<button id="late" onclick="fetch(\\'/hit?page={name}\\')">Late {name}</button>'; }}, {ms});</script>""",
    "form": """<form id="f" onsubmit="fetch('/hit?page={name}'); return false">
<span>v1</span><input id="email" name="email" aria-label="Email"></form>
<script>setTimeout(() => {{ f.innerHTML =
  '<span>v2</span><input id="email" name="email" aria-label="Email">'; }}, {ms});</script>""",
    # A synchronous request blocks the page, so the click's reply only arrives after `ms`.
    "hold": """<button id="act" onclick="const x = new XMLHttpRequest();
  x.open('GET', '/hold?page={name}&amp;ms={ms}', false); x.send()">Hold {name}</button>""",
    # A pointer sortable (#174): pointerdown captures the pointer and listens on the item;
    # moves past 5px start the drag and pointerup moves the item to the one under the point.
    # Firefox can throw from setPointerCapture for a synthetic pointer, so it is guarded.
    "sortable": """<style>li {{ height: 30px; margin: 4px 0; background: #eee; user-select: none; }}</style>
<ul id="list"><li id="a">Alpha</li><li id="b">Beta</li><li id="c">Gamma</li></ul>
<script>for (const item of list.children) item.addEventListener('pointerdown', down => {{
  try {{ item.setPointerCapture(down.pointerId); }} catch {{}}
  let dragging = false;
  const move = event => {{
    dragging ||= Math.hypot(event.clientX - down.clientX, event.clientY - down.clientY) > 5;
  }};
  const up = event => {{
    item.removeEventListener('pointermove', move);
    item.removeEventListener('pointerup', up);
    const over = document.elementFromPoint(event.clientX, event.clientY)?.closest('li');
    if (dragging && over && over !== item) over.after(item);
  }};
  item.addEventListener('pointermove', move);
  item.addEventListener('pointerup', up);
}});</script>""",
    # #137: review filters below a 150-link header. The toggle shows the options and pushes a
    # URL; an option click selects it. Home reports clicks.
    "reviews": """<header><button id="home" onclick="fetch('/hit?page={name}')">Home</button>
<script>for (let i = 0; i < 150; i++) document.currentScript.before(Object.assign(document.createElement('a'),
  {{ href: '#store-' + i, textContent: 'Store link ' + i }}));</script></header>
<main><h2>Reviews</h2><div role="region" aria-label="Review filters">
<button id="toggle" aria-expanded="false" onclick="this.setAttribute('aria-expanded', 'true');
  document.getElementById('options').hidden = false; history.pushState(null, '', '?filter=open')">Review type</button>
<div id="options" role="listbox" aria-label="Review options" hidden
  onclick="event.target.setAttribute('aria-selected', 'true')"><div role="option" tabindex="-1">All</div>
<div role="option" tabindex="-1">Positive</div><div role="option" tabindex="-1">Negative</div></div></div></main>""",
}


class FixtureSite:
    """A local HTTP site whose pages report button clicks back to it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path in ("/hit", "/hold"):
                    with site.lock:
                        site.hits.append(query["page"])
                    if url.path == "/hold":
                        time.sleep(int(query["ms"]) / 1000)
                    self.reply("ok", "text/plain")
                elif url.path in ("/page", "/slow"):
                    if url.path == "/slow":
                        time.sleep(int(query["ms"]) / 1000)
                    kind = query.get("kind", "buttons")
                    body = PAGES[kind].format(name=query["name"], ms=query.get("ms", 4000))
                    self.reply(f"<!doctype html><title>{query['name']}</title>{body}", "text/html")
                else:
                    self.send_error(404)

            def reply(self, text, content_type):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    def url(self, name, kind="buttons", ms=None):
        return f"{self.origin}/page?name={name}&kind={kind}" + (f"&ms={ms}" if ms is not None else "")

    def count(self, page):
        with self.lock:
            return self.hits.count(page)

    def stop(self):
        self.server.stop()


class ExtensionControl:
    """Harness side of CONTROL_SCRIPT; the latest background page connection wins."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ws = None
        self.replies = {}
        self.counter = 0
        self.connected = threading.Condition(self.lock)
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, ws):
        with self.connected:
            self.ws = ws
            self.connected.notify_all()
        try:
            for message in ws:
                reply = json.loads(message)
                with self.connected:
                    self.replies[reply["id"]] = reply["result"]
                    self.connected.notify_all()
        except ConnectionClosed:
            pass

    def call(self, op, **params):
        with self.connected:
            if not self.connected.wait_for(lambda: self.ws is not None, timeout=10):
                raise AssertionError("extension control script did not connect")
            self.counter += 1
            command_id = self.counter
            ws = self.ws
        ws.send(json.dumps({"id": command_id, "op": op, **params}))
        with self.connected:
            if not self.connected.wait_for(lambda: command_id in self.replies, timeout=10):
                raise AssertionError(f"extension control op {op} did not answer")
            return self.replies.pop(command_id)

    def stop(self):
        self.server.shutdown()


def refs_named(snapshot, text):
    """Refs of snapshot lines whose accessible name contains text."""
    return re.findall(r'"[^"\n]*' + re.escape(text) + r'[^"\n]*" \[ref=(e\d+)\]', snapshot)


class RealBrowserActionRaceTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite()
        cls.addClassCleanup(cls.site.stop)
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        return {"test-control.js": CONTROL_SCRIPT % cls.control.port}

    def snapshot_ref(self, tab_id, text):
        snapshot = self.call("/api/snapshot", {"tabId": tab_id})
        refs = refs_named(snapshot.get("snapshot", ""), text)
        self.assertEqual(len(refs), 1, snapshot)
        return refs[0]

    def assert_hits(self, page, expected, settle=0.5):
        """Hits are async page requests: wait until the expected count, then confirm it holds."""
        deadline = time.monotonic() + 5
        while self.site.count(page) < expected and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(settle)
        self.assertEqual(self.site.count(page), expected, f"clicks reported by {page}: {self.site.hits}")

    def in_background(self, path, body):
        """Send a bridge call from another thread; join() returns its result."""
        result = {}
        thread = threading.Thread(target=lambda: result.update(value=self.call(path, body)), daemon=True)
        thread.start()

        def join():
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), f"{path} did not return")
            return result["value"]
        return join

    # ── Navigation and focus races ──

    def test_short_refs_reject_replaced_snapshot_and_post_action_refs_without_extra_clicks(self):
        tab_id = self.open_tab(self.site.url("short-refs"))
        old = self.snapshot_ref(tab_id, "Act short-refs")
        self.assertRegex(old, r"^e\d+$")
        fresh = self.snapshot_ref(tab_id, "Act short-refs")
        self.assertNotEqual(old, fresh)
        stale = self.call("/api/click", {"tabId": tab_id, "ref": old})
        self.assert_error(stale, "stale_ref", True)
        self.assertEqual(stale["state"]["reason"], "snapshot_replaced", stale)
        self.assertEqual(stale["state"]["ref"], old, stale)
        self.assertEqual(stale["state"]["currentRef"], fresh, stale)
        self.assertEqual(stale["state"]["latestObservation"], "browser_snapshot", stale)
        self.assert_hits("short-refs", 0, settle=0)

        # A full post-action snapshot replaces refs like browser_snapshot; a delta (the default) adds them.
        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab_id, "ref": fresh, "snapshot": "full"}))
        self.assertNotIn("snapshot_delta", clicked)
        self.assertRegex(clicked["target_ref"], r"^e\d+$")
        self.assertNotEqual(clicked["target_ref"], fresh)
        self.assertIn(f'[ref={clicked["target_ref"]}]', clicked["snapshot"])
        replaced = self.call("/api/click", {"tabId": tab_id, "ref": fresh})
        self.assert_error(replaced, "stale_ref", True)
        self.assertEqual(replaced["state"]["reason"], "snapshot_replaced", replaced)
        self.assertEqual(replaced["state"]["currentRef"], clicked["target_ref"], replaced)
        self.assertEqual(replaced["state"]["latestObservation"], "browser_click", replaced)
        self.assert_hits("short-refs", 1)

    def test_click_returns_a_delta_of_what_changed_below_the_fold_and_keeps_earlier_refs(self):
        tab_id = self.open_tab(self.site.url("reviews", kind="reviews"))
        page = self.call("/api/snapshot", {"tabId": tab_id})["snapshot"]
        [home], [toggle] = refs_named(page, "Home"), refs_named(page, "Review type")
        self.assertGreater(len(page), 4000, "a capped full snapshot would end inside the header")

        opened = self.assert_ok(self.call("/api/click", {"tabId": tab_id, "ref": toggle}))
        self.assertIs(opened.get("snapshot_delta"), True, opened)
        self.assertEqual(opened["target_ref"], toggle, "an element that already had a ref keeps it")
        self.assertTrue(opened.get("url", "").endswith("?filter=open"), opened)
        delta = opened["snapshot"]
        self.assertNotIn("Store link", delta)
        self.assertNotIn('heading "Reviews"', delta)
        self.assertRegex(delta, rf'button "Review type" \[ref={toggle}\] \[[^\]\n]*expanded=true\]')
        [negative] = refs_named(delta, "Negative")
        self.assertEqual(len(refs_named(delta, "Positive")), 1, delta)

        picked = self.assert_ok(self.call("/api/click", {"tabId": tab_id, "ref": negative}))
        self.assertEqual(picked["target_ref"], negative)
        self.assertIn(f'option "Negative" [ref={negative}] [selected=true]', picked["snapshot"])
        self.assertNotIn("Positive", picked["snapshot"])
        # The header ref from before both actions still names its button.
        self.assert_ok(self.call("/api/click", {"tabId": tab_id, "ref": home}))
        self.assert_hits("reviews", 1)

    def test_ref_after_navigation_fails_as_other_document_without_clicking_the_new_page(self):
        tab_id = self.open_tab(self.site.url("nav-a"))
        ref = self.snapshot_ref(tab_id, "Act nav-a")
        self.assert_ok(self.call("/api/navigate", {"url": self.site.url("nav-b"), "tabId": tab_id}))

        result = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        self.assert_error(result, "stale_ref", True)
        self.assertEqual(result["state"]["reason"], "other_document")
        self.assert_hits("nav-b", 0)
        self.assert_hits("nav-a", 0, settle=0)

    def test_missing_target_while_the_next_document_loads_reports_navigating(self):
        tab_id = self.open_tab(self.site.url("load-a", ms=4000))
        link = self.snapshot_ref(tab_id, "Slow next")
        self.assert_ok(self.call("/api/click", {"ref": link, "tabId": tab_id, "includeSnapshot": False}))
        # The old document stays until the slow response arrives, so the target looks gone mid-navigation.
        result = self.call("/api/click", {"selector": "#missing", "tabId": tab_id})
        self.assert_error(result, "navigating", True)
        self.assertEqual(result["state"]["cause"], "not_found")
        self.assertEqual(result["state"]["status"], "loading")
        self.assert_ok(self.call("/api/wait-for-url", {"tabId": tab_id, "pattern": "load-a-next", "timeout": 10000}))
        self.assert_hits("load-a", 0)

    def test_ref_from_another_tab_after_a_focus_change_never_acts_on_the_focused_tab(self):
        first = self.open_tab(self.site.url("focus-a"))
        second = self.open_tab(self.site.url("focus-b"))
        self.assert_ok(self.call("/api/switch-tab", {"tabId": first}))
        ref = self.snapshot_ref(first, "Act focus-a")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": second}))

        for body in ({"ref": ref}, {"ref": ref, "tabId": second}):
            with self.subTest(body=body):
                result = self.call("/api/click", body)
                self.assert_error(result, "stale_ref", True)
                self.assertEqual(result["state"]["reason"], "other_document")
        self.assert_hits("focus-b", 0)
        self.assert_hits("focus-a", 0, settle=0)

        self.assert_ok(self.call("/api/click", {"ref": ref, "tabId": first}))
        self.assert_hits("focus-a", 1)
        self.assert_hits("focus-b", 0, settle=0)

    # ── Dynamic forms and delayed content ──

    def test_rerendered_form_field_ref_fails_as_element_removed(self):
        tab_id = self.open_tab(self.site.url("form-a", kind="form", ms=1500))
        ref = self.snapshot_ref(tab_id, "Email")
        self.assert_ok(self.call("/api/wait-for-text", {"tabId": tab_id, "text": "v2", "timeout": 10000}))

        result = self.call("/api/type", {"ref": ref, "text": "agent@example.test", "tabId": tab_id})
        self.assert_error(result, "stale_ref", True)
        self.assertEqual(result["state"]["reason"], "element_removed")
        value = "document.querySelector('#email').value"
        self.assertEqual(self.call("/api/js", {"code": value, "tabId": tab_id})["result"], "")

        self.assert_ok(self.call("/api/type", {"ref": self.snapshot_ref(tab_id, "Email"),
                                        "text": "agent@example.test", "tabId": tab_id}))
        self.assertEqual(self.call("/api/js", {"code": value, "tabId": tab_id})["result"], "agent@example.test")
        self.assert_hits("form-a", 0)

    def test_delayed_button_is_not_found_until_the_actionability_wait_sees_it(self):
        tab_id = self.open_tab(self.site.url("late-a", kind="delayed", ms=1500))
        result = self.call("/api/click", {"selector": "#late", "tabId": tab_id})
        self.assert_error(result, "not_found", True)
        short = self.call("/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 100})
        self.assert_error(short, "not_found", True)
        self.assertEqual(short["state"]["timeoutMs"], 100)
        self.assert_hits("late-a", 0, settle=0)

        self.assert_ok(self.call("/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 5000}))
        self.assert_hits("late-a", 1)

    # ── Drag ──

    def test_drag_reorders_a_pointer_sortable(self):
        tab_id = self.open_tab(self.site.url("sort-a", kind="sortable"))
        result = self.assert_ok(self.call("/api/drag", {"from": "#a", "to": "#c", "tabId": tab_id}))
        self.assertEqual(result["via"], "pointer", result)
        order = "[...document.querySelectorAll('#list li')].map(item => item.id).join()"
        self.assertEqual(self.call("/api/js", {"code": order, "tabId": tab_id})["result"], "b,c,a")

    # ── Tab closes and unloads ──

    def test_closed_tab_reports_tab_closed(self):
        tab_id = self.call("/api/new-tab", {"url": self.site.url("closed-a"), "wait": True})["tabId"]
        self.assert_ok(self.call("/api/close-tab", {"tabId": tab_id}))
        result = self.call("/api/click", {"selector": "#act", "tabId": tab_id})
        self.assert_error(result, "tab_closed", True)
        self.assertEqual(result["state"]["tabId"], tab_id)
        self.assert_hits("closed-a", 0)

    def test_discarded_tab_reports_tab_unloaded_until_woken(self):
        tab_id = self.open_tab(self.site.url("unload-a"))
        self.open_tab(self.site.url("unload-other"))  # a tab must be inactive to be discarded
        self.assertEqual(self.control.call("discard", tabId=tab_id), {"discarded": True})

        result = self.call("/api/click", {"selector": "#act", "tabId": tab_id})
        self.assert_error(result, "tab_unloaded", True)
        self.assertIs(result["state"]["discarded"], True)
        self.assert_hits("unload-a", 0)

        self.wake_and_click(tab_id, "unload-a")
        self.assert_hits("unload-other", 0, settle=0)

    def wake_and_click(self, tab_id, page):
        """Wake a discarded tab; it is ready only once its page answers, so the first click lands."""
        woken = self.call("/api/wake-tab", {"tabId": tab_id})
        self.assertEqual(woken, {"ok": True, "woken": True, "ready": True})
        self.assert_ok(self.call("/api/click", {"selector": "#act", "tabId": tab_id}))
        self.assert_hits(page, 1)

    def test_every_wake_is_ready_for_the_first_click(self):
        # Issue #95: the restored placeholder reports complete before the reload replaces it.
        other = self.open_tab(self.site.url("wake-other"))
        for i in range(5):
            with self.subTest(wake=i):
                tab_id = self.open_tab(self.site.url(f"wake-{i}"))
                self.assert_ok(self.call("/api/switch-tab", {"tabId": other}))  # discard needs an inactive tab
                self.assertEqual(self.control.call("discard", tabId=tab_id), {"discarded": True})
                self.wake_and_click(tab_id, f"wake-{i}")
        self.assert_hits("wake-other", 0, settle=0)

    def test_first_click_after_wake_recovers_from_missing_receiver(self):
        # Issue #169: deterministically lose delivery after a real readiness ping. The
        # fault never delivers the first attempt; the real content script handles the retry.
        tab_id = self.open_tab(self.site.url("wake-receiver"))
        other = self.open_tab(self.site.url("wake-receiver-other"))
        self.assert_ok(self.call("/api/switch-tab", {"tabId": other}))
        self.assertEqual(self.control.call("discard", tabId=tab_id), {"discarded": True})
        self.assertEqual(self.call("/api/wake-tab", {"tabId": tab_id}),
                         {"ok": True, "woken": True, "ready": True})
        self.assertEqual(self.control.call("missingReceiverOnce", tabId=tab_id), {"armed": True})
        try:
            result = self.call("/api/click", {"selector": "#act", "tabId": tab_id, "includeSnapshot": False})
        finally:
            stats = self.control.call("restoreReceiver")
        self.assert_ok(result)
        self.assertEqual(stats, {"pings": 2, "attempts": 2, "failures": 1})
        self.assert_hits("wake-receiver", 1)
        self.assert_hits("wake-receiver-other", 0, settle=0)

    # ── Extension reconnects and unknown outcomes ──

    def drop_bridge_during(self, path, body, page):
        """Drop the extension's bridge socket while a write waits in the page for its target."""
        generation = self.bridge.status()["extension_generation"]
        join = self.in_background(path, body)
        time.sleep(1)  # the command is in the page, waiting for the delayed button
        self.assert_hits(page, 0, settle=0)
        self.assertEqual(self.control.call("dropBridge"), {"dropped": True})
        result = join()
        self.wait_for_extension(after_generation=generation)
        return result

    def test_write_in_flight_during_disconnect_is_not_retryable_and_runs_once(self):
        tab_id = self.open_tab(self.site.url("drop-a", kind="delayed", ms=3000))
        result = self.drop_bridge_during(
            "/api/click", {"selector": "#late", "tabId": tab_id, "wait": True, "timeout": 8000}, "drop-a")
        self.assert_error(result, "extension_disconnected", False)
        # The outcome really was unknown: the page still runs the click it had received.
        self.assert_hits("drop-a", 1, settle=3)

    def test_retry_stops_after_an_unknown_outcome_instead_of_repeating_the_write(self):
        tab_id = self.open_tab(self.site.url("drop-retry", kind="delayed", ms=3000))
        command = {"action": "click", "selector": "#late", "tabId": tab_id, "wait": True, "timeout": 8000}
        result = self.drop_bridge_during(
            "/api/retry", {"command": command, "maxAttempts": 3, "backoffMs": 0,
                           "onErrors": ["Extension disconnected"]}, "drop-retry")
        self.assertEqual((result["ok"], result["attempts"], result["reason"]), (False, 1, "not retryable"), result)
        self.assert_error(result["result"], "extension_disconnected", False)
        self.assert_hits("drop-retry", 1, settle=3)

    def test_timed_out_write_is_not_retryable_and_retry_does_not_repeat_it(self):
        # Holds the page past the bridge timeout; this test alone takes about 30 seconds.
        hold_ms = (BATCH_COMMAND_TIMEOUT + 3) * 1000
        tab_id = self.open_tab(self.site.url("hold-a", kind="hold", ms=hold_ms))
        command = {"action": "click", "selector": "#act", "tabId": tab_id, "includeSnapshot": False}
        result = self.call("/api/retry", {"command": command, "maxAttempts": 3, "backoffMs": 0})
        self.assertEqual((result["ok"], result["attempts"], result["reason"]), (False, 1, "not retryable"), result)
        self.assert_error(result["result"], "timeout", False)
        self.assertEqual(result["result"]["state"], {"timeoutSeconds": BATCH_COMMAND_TIMEOUT})
        self.assert_hits("hold-a", 1, settle=4)

    def test_extension_reload_disconnects_then_old_refs_are_stale(self):
        tab_id = self.open_tab(self.site.url("reload-a"))
        ref = self.snapshot_ref(tab_id, "Act reload-a")
        generation = self.bridge.status()["extension_generation"]
        self.assert_ok(self.call("/api/reload-extension", {}))
        deadline = time.monotonic() + 10
        while self.bridge.status()["extension_connected"] and time.monotonic() < deadline:
            time.sleep(0.01)

        result = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        # Nothing was sent, so even a write is safe to repeat.
        self.assert_error(result, "extension_disconnected", True)
        self.assertEqual(result["state"], {"connected": False})

        self.wait_for_extension(after_generation=generation)
        # The reloaded extension's content script cannot resolve refs issued before the reload.
        stale = self.call("/api/click", {"ref": ref, "tabId": tab_id})
        self.assert_error(stale, "stale_ref", True)
        self.assert_hits("reload-a", 0)
        self.assert_ok(self.call("/api/click", {"ref": self.snapshot_ref(tab_id, "Act reload-a"),
                                                 "tabId": tab_id}))
        self.assert_hits("reload-a", 1)


if __name__ == "__main__":
    unittest.main()
