"""Playwright selector targets (#219) and hover scrolling (#220), in a real browser.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/playwright_selectors.cjs and tests/hover_scroll.cjs check the same rules on a mocked
DOM; here Firefox's own CSS parser, layout and scrolling decide. The fixture is shaped after
the benchmark's ui-select, ui-table and ui-infinite pages, and its list loads ten more items
whenever its last item scrolls into view inside the list's own scroll container.
"""
from http.server import BaseHTTPRequestHandler
import json
import time
import unittest

import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>selectors</title>
<ul role="listbox" id="countries"><li role="option">캐나다</li><li role="option">베트남</li></ul>
<select hidden><option>베트남</option></select>
<table><tr><td>USB 허브</td><td><input type="checkbox" id="usb"></td></tr>
<tr><td>키보드</td><td><input type="checkbox" id="kb"></td></tr></table>
<button id="p1">1</button><button id="p2">2</button><button id="p12">12</button>
<p><a id="policy" href="#policy">공식 정책 — 2027년 <b>현재</b> 적용</a></p>
<input type="checkbox" id="mic"><label for="mic">마이크 선택</label>
<input type="password" id="pw" value="hunter2-secret">
<div id="outer" style="height: 300px; overflow: auto">
  <div style="height: 200px">header</div>
  <ul id="feed" style="height: 120px; overflow: auto; margin: 0"></ul>
  <div style="height: 2000px"></div>
</div>
<script>
  // Logged on the root element, where the extension's /api/js can read and reset them.
  const log = (name, entry) => {
    const list = JSON.parse(document.documentElement.dataset[name] || '[]');
    document.documentElement.dataset[name] = JSON.stringify([...list, entry]);
  };
  document.addEventListener('click', event => log('clicked', event.target.id || event.target.textContent), true);
  document.addEventListener('mousemove', event => {
    const r = event.target.getBoundingClientRect();
    log('hovered', { id: event.target.id, inside: event.clientY >= r.top && event.clientY <= r.bottom });
  }, true);
  const feed = document.getElementById('feed');
  function more() {
    const start = feed.children.length;
    for (let i = start + 1; i <= start + 10; i++) {
      const li = document.createElement('li');
      li.id = 'item' + i;
      li.style.height = '30px';
      li.innerHTML = `계약서 #${i} <button>저장</button>`;
      feed.append(li);
    }
    observer.observe(feed.lastElementChild);
  }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); more(); }
  }, { root: feed });
  more();
</script>"""

STATE = """JSON.stringify({clicked: JSON.parse(document.documentElement.dataset.clicked || '[]'),
    hovered: JSON.parse(document.documentElement.dataset.hovered || '[]'),
    items: document.getElementById('feed').children.length, feedTop: document.getElementById('feed').scrollTop})"""
RESET = "document.documentElement.dataset.clicked = '[]'"


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserPlaywrightSelectorTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def state(self, tab):
        return json.loads(self.call("/api/js", {"tabId": tab, "code": STATE})["result"])

    def test_reported_selectors_click_their_one_target(self):
        tab = self.open_tab(self.page.origin + "/")
        cases = [
            ("text=베트남", "베트남"),
            ('role=option[name="캐나다"]', "캐나다"),
            ('[role=option]:has-text("베트남")', "베트남"),
            ('button:text-is("2")', "p2"),
            ('internal:role=button[name="2"s]', "p2"),
            ('table tr:has-text("USB 허브") input[type=checkbox]', "usb"),
            ("text=공식 정책 — 2027년 현재 적용", "policy"),
            ('role=checkbox[name="마이크 선택"]', "mic"),
        ]
        for selector, target in cases:
            with self.subTest(selector):
                self.call("/api/js", {"tabId": tab, "code": RESET})
                self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": selector, "includeSnapshot": False}))
                self.assertEqual(self.state(tab)["clicked"], [target])

    def test_ambiguous_playwright_selectors_click_nothing(self):
        tab = self.open_tab(self.page.origin + "/")
        for selector in ('internal:role=button[name="2"i]', 'button:has-text("저장")'):
            with self.subTest(selector):
                result = self.call("/api/click", {"tabId": tab, "selector": selector})
                self.assert_error(result, "ambiguous_target", False)
                self.assertEqual(self.state(tab)["clicked"], [])
        last = self.assert_ok(self.call("/api/click", {
            "tabId": tab, "selector": 'li:has-text("계약서 #10") >> button', "includeSnapshot": False}))
        self.assertEqual(last["tag"], "button")
        # A password field's value never matches text, so it is no oracle.
        self.assert_error(self.call("/api/hover", {"tabId": tab, "selector": "text=hunter2-secret"}), "not_found", True)

    def test_unsupported_syntax_is_reported_not_retried(self):
        tab = self.open_tab(self.page.origin + "/")
        for selector in ("xpath=//button", "text=/베트/", 'button:near(#p1)', 'checkbox "마이크 선택"', "저장",
                         "div[", "button >> "):
            with self.subTest(selector):
                result = self.call("/api/click", {"tabId": tab, "selector": selector})
                self.assert_error(result, "unsupported_selector", False)
                self.assertIn("text=", result["hint"])
        snapshot = self.call("/api/snapshot", {"tabId": tab, "selector": "xpath=//ul"})
        self.assert_error(snapshot, "unsupported_selector", False)
        self.assertEqual(self.state(tab)["clicked"], [])
        # A subtree snapshot resolves Playwright forms as targets do.
        listbox = self.call("/api/snapshot", {"tabId": tab, "selector": "role=listbox"})
        self.assertIn("캐나다", listbox.get("snapshot", ""), listbox)

    def test_hover_scrolls_nested_containers_so_more_items_load(self):
        tab = self.open_tab(self.page.origin + "/")
        self.assertEqual(self.state(tab)["items"], 10)
        result = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": "#item10"}))
        self.assertEqual(result["tag"], "li")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.state(tab)["items"] < 20:
            time.sleep(0.1)
        state = self.state(tab)
        self.assertGreater(state["feedTop"], 0, "the inner list scrolled")
        self.assertGreaterEqual(state["items"], 20, "the list loaded more once its end was in view")
        # The point is measured after the scroll, so it lies on the hovered element.
        self.assertEqual(state["hovered"][-1], {"id": "item10", "inside": True})
        # Repeating on the new last item keeps loading, as an agent scrolling the list would.
        self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": 'li:has-text("계약서 #20")'}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.state(tab)["items"] < 30:
            time.sleep(0.1)
        self.assertGreaterEqual(self.state(tab)["items"], 30)


if __name__ == "__main__":
    unittest.main()
