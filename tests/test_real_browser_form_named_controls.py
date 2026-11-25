"""A <form> whose controls are named value, action, id or title, in a real browser (issue #648).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
A form's named controls shadow its properties: with <input name=value> inside it, form.value is
that input. The snapshot, tab.forms(), query and the DOM tree read what the form itself carries.
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest

import real_browser_harness as harness

PAGES = {
    # PipeCRM's New Deal dialog: its amount field is name="value".
    "/deal": """<!doctype html><title>New Deal</title>
<form><label>Title <input name=title></label>
<label>Value ($) <input type=number name=value></label><button>Save</button></form>""",
    "/shadowed": """<!doctype html><title>Shadowed</title>
<form id="deal" name="deal" action="/deals" method="post">
<label>Value <input name=value></label><label>Action <input name=action></label>
<label>Id <input name=id></label><label>Name <input name=name></label><label>Method <input name=method></label>
<label>Title <input name=title></label><button>Save</button></form>""",
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


class RealBrowserFormNamedControlTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_a_form_with_a_control_named_value_has_no_value(self):
        tab = self.open_tab(f"{self.site.origin}/deal")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        self.assertRegex(snapshot, r'(?m)^- form "" \[ref=\w+\]$')
        self.assertNotIn("object", snapshot)

    def test_controls_named_value_action_and_id_shadow_nothing_the_form_reports(self):
        tab = self.open_tab(f"{self.site.origin}/shadowed")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        self.assertRegex(snapshot, r'(?m)^- form "" \[ref=\w+\]$')
        self.assertNotIn("object", snapshot)

        query = self.call("/api/query", {"tabId": tab, "selector": "form", "fields": ["id", "name", "value"]})
        self.assertEqual(query["items"], [{"tag": "form", "id": "deal", "name": "deal"}], query)

        fields = self.call("/api/forms", {"tabId": tab})["fields"]
        self.assertEqual([field["name"] for field in fields],
                         ["value", "action", "id", "name", "method", "title"])
        for field in fields:
            with self.subTest(field=field["name"]):
                self.assertTrue(field["selector"].startswith("#deal > "), field)

        dom = json.dumps(self.call("/api/dom", {"tabId": tab}))
        self.assertNotIn("object", dom)
        self.assertRegex(dom, r'"tag": "form", "id": "deal"')


if __name__ == "__main__":
    unittest.main()
