"""A field named by a <label> that is not associated with it, in a real browser (issue #655).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Like the flight-search booking form: each field's visible <label> has no `for` and does not
hold the field. The snapshot, tab.forms() and role= selectors name the field by that label
when it is the field's previous sibling or the only label of the field's wrapper, and the
placeholder stays on the line as a state. A label two fields share names neither.
"""
from http.server import BaseHTTPRequestHandler
import re
import unittest

import real_browser_harness as harness

PAGES = {
    "/booking": """<!doctype html><title>Booking</title>
<form><div><label>First name</label><input type=text></div>
<div><label>Email</label><input type=email placeholder="john@example.com"></div>
<div><label for=ph>Phone</label><input id=ph type=tel></div></form>""",
    "/shared": """<!doctype html><title>Shared</title>
<form><div><label>Travel dates</label><input name=from><input name=to></div>
<div><label>Card number</label><input name=card><label>Postcode</label><input name=zip placeholder="12345"></div>
<div><span><label>Cardholder name</label></span><input name=holder></div></form>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path)
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


def textbox_lines(snapshot):
    return [re.sub(r"\[ref=\w+\]", "[ref]", line.strip()) for line in snapshot.splitlines()
            if line.strip().startswith("- textbox")]


class RealBrowserUnassociatedLabelTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_a_field_is_named_by_its_unassociated_label_and_keeps_its_placeholder(self):
        tab = self.open_tab(f"{self.site.origin}/booking")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        self.assertEqual(textbox_lines(snapshot), [
            '- textbox "First name" [ref] [value=""]',
            '- textbox "Email" [ref] [placeholder="john@example.com" value=""]',
            '- textbox "Phone" [ref] [value=""]',
        ], snapshot)

        fields = self.call("/api/forms", {"tabId": tab})["fields"]
        self.assertEqual([field["label"] for field in fields], ["First name", "Email", "Phone"], fields)

        self.assert_ok(self.call("/api/fill", {"tabId": tab, "selector": 'role=textbox[name="First name"]',
                                               "value": "Ada"}))
        self.assertEqual(self.call("/api/forms", {"tabId": tab})["fields"][0]["value"], "Ada")

    def test_a_label_two_fields_share_names_neither(self):
        tab = self.open_tab(f"{self.site.origin}/shared")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        self.assertEqual(textbox_lines(snapshot), [
            '- textbox "" [ref] [value=""]',
            '- textbox "" [ref] [value=""]',
            '- textbox "Card number" [ref] [value=""]',
            '- textbox "Postcode" [ref] [placeholder="12345" value=""]',
            '- textbox "Cardholder name" [ref] [value=""]',
        ], snapshot)
        labels = {field["name"]: field["label"] for field in self.call("/api/forms", {"tabId": tab})["fields"]}
        self.assertEqual(labels, {"from": "", "to": "", "card": "Card number", "zip": "Postcode",
                                  "holder": "Cardholder name"})


if __name__ == "__main__":
    unittest.main()
