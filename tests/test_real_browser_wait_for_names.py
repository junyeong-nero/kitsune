"""Issue #275: a text wait on a name the snapshot shows says why it timed out.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import re

import real_browser_harness as harness

PAGE = """<!doctype html><title>Names</title>
<input id=ph placeholder="Where are you going?">
<input id=aria aria-label="Search the catalog">
<label>Visible label<input id=lab></label>
<button id=icon aria-label="Open menu"></button>
<span id=hidden-label hidden>Hidden caption</span><input aria-labelledby="hidden-label">
<input placeholder="Collapsed field" style="display:none">
<p id=body>Plain body text</p>
"""
TIMEOUT_MS = 300


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


class RealBrowserWaitForNameTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def wait(self, tab, text):
        return self.call("/api/wait-for-text", {"tabId": tab, "text": text, "timeout": TIMEOUT_MS})

    def test_names_time_out_with_their_ref_and_source(self):
        tab = self.open_tab(self.page.origin + "/")
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        for text, role, source in (("Where are you going?", "textbox", "placeholder"),
                                   ("Search the catalog", "textbox", "aria-label"),
                                   ("Open menu", "button", "aria-label"),
                                   ("Hidden caption", "textbox", "aria-labelledby")):
            with self.subTest(text=text):
                result = self.wait(tab, text)
                self.assertIs(result["ok"], False)
                self.assertEqual(result["error"], f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
                self.assertEqual(result["matched_as"], "accessible_name")
                [match] = result["name_matches"]
                self.assertEqual((match["role"], match["source"]), (role, source))
                self.assertRegex(match["ref"], r"^e\d+$")
                line = re.search(rf'- {role} "{re.escape(text)}" \[ref=(e\d+)\]', snapshot)
                self.assertEqual(match["ref"], line.group(1))
                self.assertIn(f'(' + source + '), not page text', result["hint"])
                self.assert_ok(self.call("/api/focus", {"tabId": tab, "ref": match["ref"]}))

    def test_page_text_and_unknown_text_keep_their_results(self):
        tab = self.open_tab(self.page.origin + "/")
        for text in ("Visible label", "Plain body text"):
            with self.subTest(text=text):
                self.assertIs(self.wait(tab, text)["ok"], True)
        for text in ("Nowhere at all", "Collapsed field"):
            with self.subTest(text=text):
                result = self.wait(tab, text)
                self.assertIs(result["ok"], False)
                self.assertNotIn("name_matches", result)
                self.assertNotIn("hint", result)
