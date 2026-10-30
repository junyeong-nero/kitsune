"""Long-press and hover-intent controls accept browser_click hold_ms and browser_hover
duration_ms, in a real browser (#351).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Synthetic
input only: nothing here moves the OS pointer. The fixtures are like kitsune-benchmark's
hostile-UI long-press and hover-intent pages: one deletes after a 600 ms press, the other
reveals its Open button after the pointer has moved over the tile for 400 ms. The tabs stay in
the background, where page timers fire after 1 s at the earliest, so the timings also show that
the background times the pauses.
"""
from http.server import BaseHTTPRequestHandler
import json

import real_browser_harness as harness

LONG_PRESS = """<!doctype html><title>Trash</title>
<button id="b">Hold to delete</button><p id="status">Kept</p>
<script>
  // Hold for 600 ms, as touch-first UIs do.
  // Each press's duration goes to data-presses, which the test reads.
  const b = document.getElementById('b'); let t0 = null;
  const presses = [];
  b.addEventListener('pointerdown', () => { t0 = performance.now(); });
  b.addEventListener('pointerup', () => {
    const held = t0 ? performance.now() - t0 : 0; t0 = null;
    presses.push(held);
    document.body.dataset.presses = JSON.stringify(presses);
    if (held >= 600) document.getElementById('status').textContent = 'Deleted';
  });
</script>"""

HOVER_INTENT = """<!doctype html><title>Gallery</title>
<div id="tile" role="group" aria-label="Preview tile"
     style="width:260px;height:120px;border:1px solid #888;padding:8px">Preview tile
  <button id="o" hidden>Open</button></div><p id="status">Closed</p>
<script>
  // Hover intent: the button appears after the pointer has moved over the tile for 400 ms.
  const tile = document.getElementById('tile'), o = document.getElementById('o');
  let since = null, moves = 0;
  tile.addEventListener('mouseenter', () => { since = performance.now(); moves = 0; });
  tile.addEventListener('mousemove', () => {
    tile.dataset.moves = ++moves;
    if (since && moves >= 3 && performance.now() - since >= 400) o.hidden = false;
  });
  tile.addEventListener('mouseleave', () => { since = null; o.hidden = true; });
  o.addEventListener('click', () => { document.getElementById('status').textContent = 'Opened'; });
</script>"""

PAGES = {"/long-press": LONG_PRESS, "/hover-intent": HOVER_INTENT}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGES.get(self.path, "<!doctype html><title>missing</title>").encode()
        self.send_response(200 if self.path in PAGES else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserPointerGestureTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def js(self, tab, code):
        return json.loads(self.call("/api/js", {"tabId": tab, "code": f"JSON.stringify({code})"})["result"])

    def test_a_held_click_satisfies_a_long_press(self):
        tab = self.open_tab(self.site.origin + "/long-press")
        self.assertIs(self.js(tab, "document.hidden"), True, "the agent's tab is in the background")

        # A plain click is a 0 ms press: the page keeps the item.
        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#b", "includeSnapshot": False}))
        self.assertEqual(self.js(tab, "document.getElementById('status').textContent"), "Kept")

        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#b", "holdMs": 700}))
        self.assertGreaterEqual(clicked["held_ms"], 700)
        state = self.js(tab, "{status: document.getElementById('status').textContent, "
                             "presses: JSON.parse(document.body.dataset.presses)}")
        self.assertEqual(state["status"], "Deleted")
        # Timed by the background: a page timer in this hidden tab would hold for 1 s or more.
        self.assertGreaterEqual(state["presses"][-1], 600)
        self.assertLess(state["presses"][-1], 1000, state)
        # The delta shows what the hold changed.
        self.assertIn("Deleted", clicked.get("snapshot", ""))

        refused = self.call("/api/click", {"tabId": tab, "selector": "#b", "holdMs": 10001})
        self.assertEqual(refused.get("error"), "holdMs must be an integer from 0 to 10000", refused)
        self.assertEqual(len(self.js(tab, "JSON.parse(document.body.dataset.presses)")), 2,
                         "a refused hold dispatches nothing")

    def test_a_sustained_hover_satisfies_hover_intent(self):
        tab = self.open_tab(self.site.origin + "/hover-intent")
        self.assertIs(self.js(tab, "document.hidden"), True, "the agent's tab is in the background")

        # One move does not reveal the button.
        self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#tile", "includeSnapshot": False}))
        self.assertIs(self.js(tab, "document.getElementById('o').hidden"), True)

        hovered = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#tile", "durationMs": 500}))
        self.assertEqual(hovered["moves"], 6)
        self.assertGreaterEqual(hovered["hovered_ms"], 500)
        self.assertLess(hovered["hovered_ms"], 1000, "timed by the background, not 5 throttled page timers")
        self.assertEqual(self.js(tab, "{hidden: document.getElementById('o').hidden, "
                                      "moves: Number(document.getElementById('tile').dataset.moves)}"),
                         {"hidden": False, "moves": 6})
        # The revealed button comes back in the delta with a ref that a click can use.
        line = next((line for line in hovered.get("snapshot", "").split("\n") if 'button "Open"' in line), "")
        self.assertIn("[ref=", line, hovered.get("snapshot"))
        ref = line.split("[ref=")[1].split("]")[0]
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref, "includeSnapshot": False}))
        self.assertEqual(self.js(tab, "document.getElementById('status').textContent"), "Opened")
