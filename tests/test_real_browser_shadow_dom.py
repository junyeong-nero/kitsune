"""Issue #156 in a real browser: a form inside an open shadow root is observable and actionable.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/shadow_dom_traversal.cjs checks the same rules on a mocked DOM. The fixture is the
`<newsletter-signup>` task from #156: an email input, two checkboxes and a submit button in
an open shadow root, next to a light-DOM signup form whose email field has the same label.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import unittest

import real_browser_harness as harness

EMAIL = "reader@example.invalid"

PAGE = """<!doctype html><title>Newsletter</title>
<h1>Gallery</h1>
<form id="signup"><label for="signup-email">이메일</label><input id="signup-email" type="email" name="email"></form>
<newsletter-signup></newsletter-signup>
<script>
customElements.define('newsletter-signup', class extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).innerHTML = `<form><h3>주간 소식</h3>
      <label>이메일 <input type="email" name="email"></label>
      <label><input type="checkbox" name="weekly"> 주간 소식 받기</label>
      <label><input type="checkbox" name="offers"> 이벤트 알림</label>
      <button type="submit">구독</button></form>`;
  }
});
</script>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserShadowDomTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def state(self, tab_id):
        """[page email, component email, weekly, offers] as page code sees them."""
        code = ("(() => { const root = document.querySelector('newsletter-signup').shadowRoot;"
                " const field = name => root.querySelector(`[name=${name}]`);"
                " return JSON.stringify([document.getElementById('signup-email').value, field('email').value,"
                " field('weekly').checked, field('offers').checked]); })()")
        result = self.call("/api/js", {"code": code, "tabId": tab_id})
        self.assertNotIn("error", result)
        return json.loads(result["result"])

    def test_open_shadow_form_is_observable_and_form_fill_stays_in_its_component(self):
        tab = self.open_tab(self.page.origin + "/")
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        refs = {}
        for line in ('heading "주간 소식"', 'checkbox "주간 소식 받기"', 'checkbox "이벤트 알림"', 'button "구독"'):
            match = re.search(re.escape(line) + r" \[ref=(e\d+)\]", snapshot)
            self.assertIsNotNone(match, f"{line} in:\n{snapshot}")
            refs[line] = match.group(1)
        self.assertEqual(len(re.findall(r'textbox "이메일" \[ref=', snapshot)), 2, snapshot)

        found = self.call("/api/find", {"query": "주간 소식", "tabId": tab})["results"]
        self.assertIn("h3", [r["tag"] for r in found], found)
        fields = self.call("/api/forms", {"tabId": tab})["fields"]
        self.assertEqual(sorted(f["name"] for f in fields), ["email", "email", "offers", "weekly"], fields)

        # A snapshot ref toggles the checkbox inside the shadow root.
        self.assert_ok(self.call("/api/check", {"tabId": tab, "ref": refs['checkbox "주간 소식 받기"'], "checked": True}))
        self.assertEqual(self.state(tab), ["", "", True, False])

        # A coordinate click lands on the checkbox, not on its host.
        offers = next(f for f in fields if f["name"] == "offers")
        x, y, width, height = offers["bounds"]
        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab,
                                                          "coords": {"x": x + width / 2, "y": y + height / 2}}))
        self.assertEqual(clicked["tag"], "input", clicked)
        self.assertEqual(self.state(tab), ["", "", True, True])

        # "이메일" labels a field on the page and one in the component: nothing is written.
        result = self.call("/api/form-fill", {"tabId": tab, "fields": {"이메일": EMAIL}})
        self.assert_error(result["results"]["이메일"], "ambiguous_target", False)
        self.assertEqual(len(result["results"]["이메일"]["state"]["candidates"]), 2, result)
        self.assertEqual(self.state(tab), ["", "", True, True])
        # A label only the component has writes into the component only.
        result = self.call("/api/form-fill", {"tabId": tab, "fields": {"주간 소식 받기": False}})
        self.assertEqual(result["results"], {"주간 소식 받기": {"ok": True, "tag": "input", "checked": False}})
        self.assertEqual(self.state(tab), ["", "", False, True])


if __name__ == "__main__":
    unittest.main()
