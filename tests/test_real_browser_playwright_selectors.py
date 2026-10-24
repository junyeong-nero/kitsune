"""Playwright selector targets (#219, #240) and hover scrolling (#220), in a real browser.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/playwright_selectors.cjs and tests/hover_scroll.cjs check the same rules on a mocked
DOM; here Firefox's own CSS parser, layout and scrolling decide. The fixture is shaped after
the benchmark's ui-select, ui-table and ui-infinite pages, and its list loads ten more items
whenever its last item scrolls into view inside the list's own scroll container.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
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

# #240: the gallery, table and meeting-notes forms. No redacted content, so XPath runs.
GALLERY = """<!doctype html><meta charset="utf-8"><title>gallery</title>
<img id="puzzle" alt="왜곡된 문자 퍼즐" width="40" height="20">
<div id="grid"><div id="cell1-wrap"><button aria-label="1번 칸"><img alt="1번 칸 그림" width="20" height="20"></button></div>
<button id="cell2" aria-label="2번 칸"><img alt="2번 칸 그림" width="20" height="20"></button></div>
<section id="stock"><h2>재고 표</h2><p>사과 3개</p></section>
<input type="checkbox" id="mic"><label for="mic">마이크 선택</label>
<span id="t71">#71 완료</span> <span id="t8">#8 진행</span>
<button id="save68" aria-label="회의록 #68 저장">저장</button><button id="save66">회의록 #66 저장</button>"""

# #240: a sign-in form in a shadow root; ?value gives its password field a value attribute.
SHADOW = """<!doctype html><meta charset="utf-8"><title>shadow</title>
<h2>Sign in</h2><div id="host"></div>
<script>
  document.getElementById('host').attachShadow({ mode: 'open' }).innerHTML =
    '<form class="in-shadow"><input type="password" id="pw"' + (location.search ? ' value="hunter2"' : '') + '></form>';
</script>"""

STATE = """JSON.stringify({clicked: JSON.parse(document.documentElement.dataset.clicked || '[]'),
    hovered: JSON.parse(document.documentElement.dataset.hovered || '[]'),
    items: document.getElementById('feed').children.length, feedTop: document.getElementById('feed').scrollTop})"""
RESET = "document.documentElement.dataset.clicked = '[]'"


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = (GALLERY if path == "/gallery" else SHADOW if path == "/shadow" else PAGE).encode()
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
        # This page has a password field with a value attribute, which XPath would read.
        for selector in ("xpath=//button", "text=/(/", 'button:near(#p1)', 'checkbox "마이크 선택"', "저장",
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

    def test_remaining_playwright_forms_resolve(self):
        tab = self.open_tab(self.page.origin + "/gallery")
        cases = [
            ('role=img[name="왜곡된 문자 퍼즐"]', "img"),
            ('role=img[name="2번 칸 그림"]', "img"),
            ("xpath=//h2[text()='재고 표']/..", "section"),
            ("xpath=//button[@aria-label='1번 칸']/..", "div"),
            ('#grid >> xpath=//h2', None),
            ('internal:label="마이크 선택"i', "input"),
            ("text=/#7\\d/", "span"),
            ("role=button[name=/#66 저장/]", "button"),
            ('button[aria-label="회의록 #68 저장"], button:has-text("회의록 #68 저장")', "button"),
        ]
        for selector, tag in cases:
            with self.subTest(selector):
                result = self.call("/api/hover", {"tabId": tab, "selector": selector})
                if tag:
                    self.assertEqual(self.assert_ok(result)["tag"], tag)
                else:
                    self.assert_error(result, "not_found", True)
        observed = self.call("/api/snapshot", {"tabId": tab})
        ref = re.search(r'- img "2번 칸 그림" \[ref=(e\d+)\]', observed.get("snapshot", "")).group(1)
        parent = self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": f"{ref} >> xpath=.."}))
        self.assertEqual(parent["tag"], "button")
        missing = 'button[aria-label="회의록 #69 저장"], button:has-text("회의록 #69 저장")'
        self.assert_error(self.call("/api/snapshot", {"tabId": tab, "selector": missing}), "not_found", True)

    def test_xpath_in_a_shadow_root_is_refused_when_the_root_holds_a_password_value(self):
        probe = "css=form.in-shadow >> xpath=.//input"
        tab = self.open_tab(self.page.origin + "/shadow")
        self.assertEqual(self.assert_ok(self.call("/api/hover", {"tabId": tab, "selector": probe}))["tag"], "input")
        tab = self.open_tab(self.page.origin + "/shadow?value")
        for selector in (probe, "css=form.in-shadow >> xpath=.//input[starts-with(@value,'h')]", "xpath=//h2"):
            with self.subTest(selector):
                self.assert_error(self.call("/api/hover", {"tabId": tab, "selector": selector}),
                                  "unsupported_selector", False)

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
