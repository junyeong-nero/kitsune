"""Post-action deltas see what the page renders after the action (issue #348).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Each page
reacts to an action a moment later, as the pages in #348 do: a confirmation inside an open
shadow root, a debounced autocomplete list, a status line after a slow request. The agent's
tabs are hidden, where Firefox delays page timers to at least 1 s.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import time
import unittest
from urllib.parse import urlparse

import real_browser_harness as harness

PAGES = {
    # ui-shadow: the newsletter form is in an open shadow root; the confirmation follows the click.
    "/shadow": """<!doctype html><title>shadow</title><div id=host></div><script>
const root = document.getElementById('host').attachShadow({mode: 'open'});
root.innerHTML = '<form><label>이메일 <input name=email></label><button type=button>구독 신청</button></form>';
root.querySelector('button').onclick = () => setTimeout(() => {
  root.querySelector('form').insertAdjacentHTML('beforeend', '<p>신청되었습니다.</p>');
}, 250);
</script>""",
    # mw-use-autocomplete: suggestions render 300 ms after the last input, as jQuery UI does.
    "/autocomplete": """<!doctype html><title>autocomplete</title><label>Tags: <input id=tags></label>
<ul id=list role=listbox></ul><script>
let timer;
document.getElementById('tags').addEventListener('input', e => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    const q = e.target.value.toLowerCase();
    document.getElementById('list').innerHTML = ['Aland Islands', 'Albania', 'Algeria']
      .filter(name => name.toLowerCase().startsWith(q)).map(name => `<li role=option>${name}</li>`).join('');
  }, 300);
});
</script>""",
    # pb-safe-03: the status line changes when a slow request answers.
    "/status": """<!doctype html><title>status</title><p id=status>우산 — 진행 중</p>
<button onclick="fetch('/slow').then(r => r.text()).then(t => { document.getElementById('status').textContent = t; })">
우산 완료 표시</button>""",
    # ui-editor (#385): a contenteditable memo whose text is its paragraphs, and one longer than
    # the 160 characters its value shows.
    "/memo": """<!doctype html><meta charset="utf-8"><title>memo</title>
<div contenteditable=true aria-label="메모 본문"><p>프로젝트 메모</p><p>이번 주 진행 상황을 정리합니다.</p></div>
<div contenteditable=true aria-label="긴 메모"><p>""" + "가" * 170 + """</p></div>""",
    # ui-infinite (#385): the next items load after a request that outlasts the wait.
    "/stall": """<!doctype html><title>stall</title><ul id=items><li>#1</li></ul>
<button onclick="fetch('/stall-items').then(r => r.text()).then(t => {
  document.getElementById('items').insertAdjacentHTML('beforeend', t); })">더 보기</button>""",
    "/still": """<!doctype html><title>still</title><button onclick="void 0">아무 일 없음</button>
<form><label>이메일 <input name=email></label><label><input type=checkbox name=weekly> 주간 소식</label></form>""",
}
SLOW_MS = 900
# Well past the 4 s a delta waits for pending requests.
STALL_MS = 8000


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/slow":
            time.sleep(SLOW_MS / 1000)
            self.reply("우산 — 완료")
            return
        if path == "/stall-items":
            time.sleep(STALL_MS / 1000)
            self.reply("<li>#2</li>")
            return
        if path not in PAGES:
            self.send_error(404)
            return
        self.reply(PAGES[path])

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


class RealBrowserActionSettleTests(harness.RealBrowserTestCase):
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

    def act(self, path, body):
        started = time.monotonic()
        result = self.assert_ok(self.call(path, body))
        return result, time.monotonic() - started

    def test_a_confirmation_rendered_inside_a_shadow_root_after_the_click_is_in_the_delta(self):
        tab = self.open_tab(f"{self.site.origin}/shadow")
        result, _ = self.act("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "구독 신청"')})
        self.assertTrue(result.get("snapshot_delta"), result)
        self.assertEqual(result.get("visibilityState"), "hidden", "the agent's tab is hidden")
        self.assertIn('text "신청되었습니다."', result["snapshot"])
        self.assertNotIn("no_visible_change", result)

    def test_a_debounced_suggestion_list_is_in_the_delta_of_typing(self):
        tab = self.open_tab(f"{self.site.origin}/autocomplete")
        result, _ = self.act("/api/type", {"tabId": tab, "ref": self.ref(tab, 'textbox "Tags:"'), "text": "Al"})
        self.assertIn('textbox "Tags:"', result["snapshot"])
        for name in ("Aland Islands", "Albania", "Algeria"):
            self.assertIn(f'option "{name}"', result["snapshot"])

    def test_a_status_line_that_waits_for_a_request_is_in_the_delta(self):
        tab = self.open_tab(f"{self.site.origin}/status")
        result, elapsed = self.act("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "우산 완료 표시"')})
        self.assertIn('text "우산 — 완료"', result["snapshot"])
        self.assertGreaterEqual(elapsed, SLOW_MS / 1000)

    def test_no_visible_change_is_said_and_costs_a_bounded_wait(self):
        tab = self.open_tab(f"{self.site.origin}/still")
        result, elapsed = self.act("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "아무 일 없음"')})
        self.assertIs(result.get("no_visible_change"), True, result)
        # Hidden tab: the minimum covers Firefox's 1 s background timer clamp; well under the cap.
        self.assertLess(elapsed, 2.5)
        # Without a snapshot nothing waits.
        _, quick = self.act("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "아무 일 없음"'),
                                           "includeSnapshot": False})
        self.assertLess(quick, 0.5)

    def test_an_editor_shows_its_text_and_typing_into_it_is_a_change(self):
        tab = self.open_tab(f"{self.site.origin}/memo")
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        self.assertRegex(snapshot, r'textbox "메모 본문" \[ref=e\d+\] \[value="프로젝트 메모 이번 주 진행 상황을 정리합니다\."\]')
        self.assertIn(f'[value="{"가" * 160}…"]', snapshot)
        result, _ = self.act("/api/type", {"tabId": tab, "ref": self.ref(tab, 'textbox "메모 본문"'),
                                           "text": " 자료는 공유 폴더에 올렸습니다."})
        self.assertIn('정리합니다. 자료는 공유 폴더에 올렸습니다."]', result["snapshot"])
        self.assertNotIn("no_visible_change", result)
        # Past the 160 characters the value shows, the edit is still a change.
        result, _ = self.act("/api/type", {"tabId": tab, "ref": self.ref(tab, 'textbox "긴 메모"'), "text": " 끝"})
        self.assertTrue(result.get("snapshot_delta"), result)
        self.assertNotIn("no_visible_change", result)

    def test_a_request_still_pending_after_the_wait_is_not_no_visible_change(self):
        tab = self.open_tab(f"{self.site.origin}/stall")
        result, _ = self.act("/api/click", {"tabId": tab, "ref": self.ref(tab, 'button "더 보기"')})
        self.assertTrue(result.get("snapshot_delta"), result)
        self.assertNotIn("#2", result["snapshot"], "the items load after the wait")
        self.assertNotIn("no_visible_change", result)

    def test_fill_form_fields_share_one_delta(self):
        # The bodies browser_fill_form sends for two fields (#348).
        tab = self.open_tab(f"{self.site.origin}/still")
        email, weekly = self.ref(tab, 'textbox "이메일"'), self.ref(tab, 'checkbox "주간 소식"')
        first, _ = self.act("/api/fill", {"tabId": tab, "ref": email, "value": "a@b.example",
                                          "includeSnapshot": False, "deltaBaseline": True})
        self.assertNotIn("snapshot", first)
        last, _ = self.act("/api/check", {"tabId": tab, "ref": weekly, "checked": True, "keepBaseline": True})
        # Both fields' changes, under the refs the agent already has.
        self.assertIn(f'textbox "이메일" [ref={email}] [value="a@b.example"]', last["snapshot"])
        self.assertIn(f'checkbox "주간 소식" [ref={weekly}] [value="on" checked=true]', last["snapshot"])
        self.assertEqual(last.get("target_ref"), weekly)


if __name__ == "__main__":
    unittest.main()
