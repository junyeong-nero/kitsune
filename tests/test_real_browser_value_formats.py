"""#614: a value a date/time/number/color input does not keep is refused, not reported as written.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import os
from unittest.mock import patch

import kitsune
from kitsune import bridge_client, bridge_control
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Flights</title>
<label>Departure <input id=depart type=date min=2026-04-01 max=2027-04-01></label>
<label>Boarding <input id=boarding type=datetime-local></label>
<label>Seats <input id=seats type=number min=1 max=9></label>
<label>Price <input id=price type=number step=5></label>
<label>Volume <input id=volume type=range min=0 max=10></label>
<label>Tint <input id=tint type=color></label>
<output id=events></output>
<script>
for (const field of document.querySelectorAll('input')) {
  field.addEventListener('input', e => document.getElementById('events').textContent += e.target.id + ';');
}
</script>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserValueFormatTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        url = f"http://127.0.0.1:{self.bridge.http_port}"
        token = self.bridge.auth()["Authorization"].removeprefix("Bearer ")
        for patcher in (patch.object(bridge_client, "BRIDGE", url), patch.object(bridge_control, "BRIDGE", url),
                        patch.object(bridge_client, "HTTP_PORT", self.bridge.http_port),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": token})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = kitsune.connect()
        self.tab = self.browser.new_tab(self.site.origin + "/")
        self.addCleanup(self.tab.close)

    def value(self, selector):
        return self.tab.evaluate(f"() => document.querySelector('{selector}').value")

    def events(self):
        return self.tab.evaluate("() => document.getElementById('events').textContent")

    def assert_rejected(self, action, written, value, expected_format):
        with self.assertRaises(kitsune.KitsuneError) as raised:
            action()
        error = raised.exception
        self.assertEqual((error.code, error.retryable), ("value_rejected", False), error)
        self.assertEqual((error.state["written"], error.state["value"], error.state["expected_format"]),
                         (written, value, expected_format))
        self.assertIn("Nothing was changed", error.hint)
        return error

    def test_fill_and_type_refuse_a_date_in_another_format(self):
        error = self.assert_rejected(lambda: self.tab.fill("#depart", "05/03/2026"), "05/03/2026", "", "YYYY-MM-DD")
        self.assertEqual((error.state["type"], error.state["min"], error.state["max"]),
                         ("date", "2026-04-01", "2027-04-01"))
        self.assert_rejected(lambda: self.tab.type("#depart", "12/10/2026"), "12/10/2026", "", "YYYY-MM-DD")
        self.assertEqual(self.events(), "", "a refused value dispatches nothing")

        self.assertTrue(self.tab.fill("#depart", "2026-05-03")["ok"])
        self.assert_rejected(lambda: self.tab.fill("#depart", "05/04/2026"), "05/04/2026", "2026-05-03",
                             "YYYY-MM-DD")
        self.assertEqual(self.value("#depart"), "2026-05-03", "the previous value is put back")
        # type replaces a date rather than appending to it.
        self.assertTrue(self.tab.type("#depart", "2026-06-01")["ok"])
        self.assertEqual(self.value("#depart"), "2026-06-01")
        self.assertEqual(self.events(), "depart;depart;")

    def test_a_value_out_of_bounds_is_written_and_reported(self):
        result = self.tab.fill("#depart", "2027-06-04")
        self.assertEqual(result["invalid"], {"reason": "rangeOverflow", "value": "2027-06-04",
                                             "min": "2026-04-01", "max": "2027-04-01"})
        self.assertIn("after max", result["hint"])
        self.assertNotIn("invalid", self.tab.fill("#depart", "2026-12-24"))
        self.assertEqual(self.tab.fill("#seats", "0")["invalid"]["reason"], "rangeUnderflow")
        off_step = self.tab.fill("#price", "7")
        self.assertEqual(off_step["invalid"], {"reason": "stepMismatch", "value": "7", "step": "5"})
        self.assertIn("write a value on its step", off_step["hint"])

    def test_type_still_appends_to_a_number(self):
        self.tab.fill("#price", "1")
        self.assertTrue(self.tab.type("#price", "5")["ok"])
        self.assertEqual(self.value("#price"), "15")
        self.assert_rejected(lambda: self.tab.type("#price", "x"), "15x", "15", "a number such as 12 or -3.5")

    def test_other_sanitized_types(self):
        self.assertTrue(self.tab.fill("#boarding", "2026-05-03 09:00")["ok"], "a normalized value is kept")
        self.assertEqual(self.value("#boarding"), "2026-05-03T09:00")
        self.assert_rejected(lambda: self.tab.fill("#seats", "three"), "three", "", "a number such as 12 or -3.5")
        self.assert_rejected(lambda: self.tab.fill("#volume", "50"), "50", "5", "a number between min and max")
        self.assertTrue(self.tab.fill("#tint", "#FF0000")["ok"])
        self.assert_rejected(lambda: self.tab.fill("#tint", "red"), "red", "#ff0000", "#rrggbb")

    def test_form_fill_refuses_the_field_and_fills_the_rest(self):
        result = self.browser.request("/api/form-fill", {"tabId": self.tab.id,
                                                          "fields": {"#depart": "05/03/2026", "#seats": "2"}})
        self.assertEqual(result["results"]["#depart"]["code"], "value_rejected")
        self.assertIs(result["results"]["#seats"]["ok"], True)
        self.assertEqual((self.value("#depart"), self.value("#seats")), ("", "2"))

    def test_snapshot_names_the_input_type_and_bounds(self):
        snapshot = self.tab.snapshot()
        self.assertRegex(snapshot, r'textbox "Departure" \[ref=\w+\] \[type=date min=2026-04-01 max=2027-04-01 '
                                   r'value=""\]')
        self.assertRegex(snapshot, r'textbox "Boarding" \[ref=\w+\] \[type=datetime-local value=""\]')
        self.assertRegex(snapshot, r'spinbutton "Seats" \[ref=\w+\] \[min=1 max=9 value=""\]')
