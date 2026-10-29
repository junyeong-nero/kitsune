"""What an action's result says about the page in a real browser (issues #349 and #350).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
- #349: a click that loads another page reports navigated {url, title} with a snapshot of the
  new page, not a delta of the old one.
- #350: browser_hover returns a delta, and browser_wait_for(time) returns what changed since the
  agent last looked, as on an infinite list that loads a batch when its sentinel is hovered.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import time
import unittest
from urllib.parse import parse_qs, urlparse

import real_browser_harness as harness

PAGES = {
    "/list": """<!doctype html><title>Awards</title><h1>Winners</h1>
<a href="/item/1">Hollow Knight Silksong</a>
<button onclick="setTimeout(() => { location.href = '/item/2'; }, 200)">Open by script</button>
<button onclick="void 0">Nothing</button>
<a href="/empty">No content</a>""",
    "/item/1": "<!doctype html><title>Hollow Knight Silksong</title><h1>Hollow Knight Silksong</h1>",
    "/item/2": "<!doctype html><title>Item 2</title><h1>Item 2</h1>",
    # ui-infinite: hovering the sentinel loads the next batch; a slow batch lands after the hover.
    "/infinite": """<!doctype html><title>Infinite</title><ul id=items><li>Item 1</li></ul>
<p id=sentinel>불러오는 중…</p><div id=card>Card<span id=preview hidden>Preview: Item 1 details</span></div>
<script>
let next = 2;
document.getElementById('card').addEventListener('mouseenter', () => { document.getElementById('preview').hidden = false; });
document.getElementById('sentinel').addEventListener('mouseenter', () => {
  fetch('/batch?from=' + next + '&delay=' + (next > 2 ? 5000 : 0)).then(r => r.text()).then(html => {
    document.getElementById('items').insertAdjacentHTML('beforeend', html);
  });
  next += 2;
});
</script>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/item/1":
            time.sleep(0.6)  # a slow page: the click waits for it to load
        if url.path == "/empty":
            self.send_response(204)
            self.end_headers()
            return
        if url.path == "/batch":
            query = parse_qs(url.query)
            first, delay = int(query["from"][0]), int(query["delay"][0])
            time.sleep(delay / 1000)
            self.reply("".join(f"<li>Item {i}</li>" for i in (first, first + 1)))
            return
        if url.path not in PAGES:
            self.send_error(404)
            return
        self.reply(PAGES[url.path])

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


class RealBrowserActionObservationTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def ref(self, tab, line):
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        match = re.search(re.escape(line) + r" \[ref=(e\d+)\]", snapshot)
        self.assertIsNotNone(match, f"{line} in\n{snapshot}")
        return match.group(1)

    def test_a_link_click_reports_the_page_it_loaded(self):
        tab = self.open_tab(f"{self.site.origin}/list")
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": self.ref(tab, 'link "Hollow Knight Silksong"')}))
        self.assertEqual(result.get("navigated"), {"url": f"{self.site.origin}/item/1", "title": "Hollow Knight Silksong"})
        self.assertNotIn("navigation_pending", result)
        self.assertNotIn("snapshot_delta", result)
        self.assertIn('heading "Hollow Knight Silksong"', result["snapshot"])

    def test_a_script_navigation_after_the_click_is_reported(self):
        tab = self.open_tab(f"{self.site.origin}/list")
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "Open by script"')}))
        self.assertEqual(result.get("navigated"), {"url": f"{self.site.origin}/item/2", "title": "Item 2"})
        self.assertIn('heading "Item 2"', result["snapshot"])

    def test_clicks_that_load_nothing_return_a_delta(self):
        tab = self.open_tab(f"{self.site.origin}/list")
        for name in ('button "Nothing"', 'link "No content"'):
            with self.subTest(target=name):
                started = time.monotonic()
                result = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": self.ref(tab, name)}))
                self.assertNotIn("navigated", result)
                self.assertTrue(result.get("snapshot_delta"), result)
                self.assertLess(time.monotonic() - started, 3.5)

    def test_hover_returns_what_it_revealed_and_loaded(self):
        tab = self.open_tab(f"{self.site.origin}/infinite")
        revealed = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#card"}))
        self.assertTrue(revealed.get("snapshot_delta"), revealed)
        self.assertIn("Preview: Item 1 details", revealed["snapshot"])
        loaded = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#sentinel"}))
        self.assertIn('text "Item 2"', loaded["snapshot"])
        self.assertIn('text "Item 3"', loaded["snapshot"])
        self.assertNotIn('text "Item 1"', loaded["snapshot"], "only what changed")

    def test_wait_for_time_returns_what_changed_since_the_agent_last_looked(self):
        tab = self.open_tab(f"{self.site.origin}/infinite")
        self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#sentinel"}))  # batch 2-3, at once
        # The next batch takes 5 s, longer than an action waits for its requests (4 s): the hover's
        # delta is taken before it lands, and it lands while the agent thinks.
        hover = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#sentinel"}))
        self.assertNotIn('text "Item 4"', hover["snapshot"])
        time.sleep(1.5)
        sleeping = time.monotonic()
        time.sleep(1.5)
        changes = self.call("/api/snapshot", {"tabId": tab, "sinceObservation": True, "maxChars": 4000})
        self.assertGreaterEqual(time.monotonic() - sleeping, 1.5)
        self.assertTrue(changes.get("delta"), changes)
        self.assertIn('text "Item 4"', changes["snapshot"])
        self.assertIn('text "Item 5"', changes["snapshot"])
        self.assertNotIn('text "Item 2"', changes["snapshot"], "seen in an earlier result")
        # Nothing new since then.
        again = self.call("/api/snapshot", {"tabId": tab, "sinceObservation": True, "maxChars": 4000})
        self.assertIs(again.get("unchanged"), True, again)


if __name__ == "__main__":
    unittest.main()
