"""Action targeting on a page with repeated controls, in a real browser (issues #133, #134).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is the wishlist page from #133: per-item delete forms above an add form. Its
server records every form submission, which is how the tests prove server-side that
nothing was deleted. One test drives the tools through MCP argument validation, whose
parameters are documented as schema descriptions (#135).
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import html
import json
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kitsune import server
import real_browser_harness as harness

ITEMS = ["캠핑 랜턴", "머그컵"]
NEW_ITEM = "KITSUNE_FAKE_ITEM_133"


class WishlistSite:
    """Wishlists keyed by the `list` query parameter; every submission is recorded."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lists = {}
        self.submissions = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                name = parse_qs(url.query).get("list", [""])[0]
                if url.path == "/strict-targets":
                    self.reply("""<!doctype html><title>Strict targets</title>
<label>Delivery address <input class="delivery"></label>
<label>Delivery address <input class="delivery"></label>
<input placeholder="Parcel code"><input placeholder="Parcel code">
<div class="pane" style="height:40px;overflow:auto"><div style="height:400px">One</div></div>
<div class="pane" style="height:40px;overflow:auto"><div style="height:400px">Two</div></div>
<input type="file" hidden><input type="file" hidden>
<button type="button">Delete one</button><button type="button">Delete two</button>
<script>window.clicks = 0; document.addEventListener('click', () => window.clicks++);</script>""")
                    return
                if url.path != "/wishlist" or not name:
                    self.send_error(404)
                    return
                with site.lock:
                    items = list(site.lists.setdefault(name, list(ITEMS)))
                self.reply(site.page(name, items))

            def do_POST(self):
                url = urlparse(self.path)
                name = parse_qs(url.query).get("list", [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with site.lock:
                    site.submissions.append((name, url.path, form))
                    items = site.lists.setdefault(name, list(ITEMS))
                    if url.path == "/wishlist/delete" and form.get("item") in items:
                        items.remove(form["item"])
                    elif url.path == "/wishlist/add":
                        items.append(form.get("name", ""))
                self.send_response(303)
                self.send_header("Location", f"/wishlist?list={name}")
                self.send_header("Content-Length", "0")
                self.end_headers()

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

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    @staticmethod
    def page(name, items):
        # Typeless buttons, as on the page in #133.
        deletes = "".join(
            f'<form method="post" action="/wishlist/delete?list={name}"><span>{html.escape(item)}</span>'
            f'<input type="hidden" name="item" value="{html.escape(item)}"><button>{html.escape(item)} 삭제</button></form>'
            for item in items)
        return (f"<!doctype html><title>wishlist {name}</title><h1>위시리스트</h1>{deletes}"
                f'<form method="post" action="/wishlist/add?list={name}">'
                '<label>상품명 <input name="name"></label><label>가격 <input name="price"></label>'
                '<label>링크 <input name="link"></label><button>추가</button></form>')

    def url(self, name):
        return f"{self.origin}/wishlist?list={name}"

    def state(self, name):
        with self.lock:
            return (list(self.lists.get(name, ITEMS)),
                    [(path, form) for list_name, path, form in self.submissions if list_name == name])

    def stop(self):
        self.server.stop()


class RealBrowserSubmitFormTargetTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = WishlistSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def wait_for_submissions(self, name, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            items, submissions = self.site.state(name)
            if len(submissions) >= count:
                return items, submissions
            time.sleep(0.1)
        return self.site.state(name)

    def test_submit_without_target_submits_the_filled_add_form(self):
        tab = self.open_tab(self.site.url("filled"))
        filled = self.call("/api/form-fill", {"tabId": tab, "fields": {
            "상품명": NEW_ITEM, "가격": "12900", "링크": "https://example.invalid/item"}})
        self.assertEqual([r.get("ok") for r in filled["results"].values()], [True, True, True], filled)
        result = self.assert_ok(self.call("/api/submit-form", {"tabId": tab}))
        self.assertEqual(result["form"]["action"], "/wishlist/add?list=filled", result)
        self.assertEqual(result["form"]["button"], "추가", result)
        items, submissions = self.wait_for_submissions("filled", 1)
        time.sleep(0.5)  # a second, wrong submission would arrive by now
        items, submissions = self.site.state("filled")
        self.assertEqual([path for path, _ in submissions], ["/wishlist/add"], submissions)
        self.assertEqual(items, [*ITEMS, NEW_ITEM], "existing items are kept")

    def test_submit_without_target_or_filled_field_is_ambiguous_and_submits_nothing(self):
        tab = self.open_tab(self.site.url("unfilled"))
        result = self.call("/api/submit-form", {"tabId": tab})
        self.assert_error(result, "ambiguous_target", False)
        self.assertEqual(result["state"]["forms"], 3, result)
        self.assertEqual([(c["action"], c["method"], c["button"]) for c in result["state"]["candidates"]], [
            ("/wishlist/delete?list=unfilled", "post", "캠핑 랜턴 삭제"),
            ("/wishlist/delete?list=unfilled", "post", "머그컵 삭제"),
            ("/wishlist/add?list=unfilled", "post", "추가"),
        ])
        time.sleep(1)
        items, submissions = self.site.state("unfilled")
        self.assertEqual(submissions, [], "nothing was submitted")
        self.assertEqual(items, ITEMS)
        # A candidate's ref submits exactly that form.
        add_ref = result["state"]["candidates"][2]["ref"]
        self.assertEqual(self.assert_ok(self.call("/api/submit-form", {"tabId": tab, "ref": add_ref}))["form"]["button"],
                         "추가")
        items, submissions = self.wait_for_submissions("unfilled", 1)
        self.assertEqual([path for path, _ in submissions], ["/wishlist/add"], submissions)
        self.assertEqual(items, [*ITEMS, ""], "existing items are kept")

    def call_tool(self, name, arguments):
        """Call a tool through FastMCP's argument validation, against this class's bridge."""
        with patch.object(server, "_get", self.call), \
                patch.object(server, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = asyncio.run(server.mcp.call_tool(name, arguments))
        return json.loads(result.content[0].text)

    def test_mcp_calls_pass_the_schema_documented_contract_parameters(self):
        tab = self.open_tab(self.site.url("mcp"))
        read = self.call_tool("browser_snapshot", {"tab_id": tab, "text": True})
        self.assertEqual(read.get("title"), "wishlist mcp", read)  # url/title come only with text=True
        name_ref = re.search(r'textbox "상품명[^"]*" \[ref=([^\]]+)\]', read["snapshot"]).group(1)

        typed = self.call_tool("browser_type", {
            "tab_id": tab, "ref": name_ref, "element": "상품명", "text": NEW_ITEM, "mode": "fill",
            "wait": True, "expect": {"selector": "input[name=name]", "value": NEW_ITEM}})
        self.assertIs(typed.get("ok"), True, typed)
        self.assertIs(typed["outcome"]["met"], True, typed)

        # An unmet expect fails the call, but the action already ran.
        unmet = self.call_tool("browser_type", {
            "tab_id": tab, "selector": "input[name=name]", "text": "KITSUNE_FAKE_UNMET_135", "mode": "fill",
            "expect": {"selector": "input[name=name]", "value": "never", "timeout": 300}})
        self.assertEqual(unmet.get("code"), "outcome_not_observed", unmet)
        self.assertIs(unmet["dispatched"], True)
        self.assertEqual(unmet["outcome"]["observed"]["value"], "KITSUNE_FAKE_UNMET_135")
        self.assertEqual(self.site.state("mcp"), (ITEMS, []), "typing submitted nothing")


class RealBrowserStrictSelectorTests(harness.RealBrowserTestCase):
    # The selector from #134, which matches both delete buttons and the add button.
    SELECTOR = "form button, form input[type=submit]"

    @classmethod
    def setUpClass(cls):
        cls.site = WishlistSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def fill_add_form(self, name):
        tab = self.open_tab(self.site.url(name))
        self.call("/api/form-fill", {"tabId": tab, "fields": {"상품명": NEW_ITEM}})
        return tab

    def test_selector_matching_three_buttons_is_ambiguous_and_clicks_nothing(self):
        tab = self.fill_add_form("ambiguous")
        result = self.call("/api/click", {"tabId": tab, "selector": self.SELECTOR})
        self.assert_error(result, "ambiguous_target", False)
        self.assertEqual(result["state"]["matches"], 3, result)
        self.assertEqual([(c["role"], c["name"]) for c in result["state"]["candidates"]],
                         [("button", "캠핑 랜턴 삭제"), ("button", "머그컵 삭제"), ("button", "추가")])
        time.sleep(1)
        self.assertEqual(self.site.state("ambiguous"), (ITEMS, []), "zero clicks: nothing submitted")

    def test_remaining_targets_are_strict_without_side_effects(self):
        # No sleeps or OS clicks: all ambiguous requests must fail before dispatch.
        tab = self.open_tab(f"{self.site.origin}/strict-targets")
        for endpoint, params in [
            ("trusted-click", {"selector": "button"}),
            ("focus", {"selector": ".delivery"}),
            ("blur", {"selector": ".delivery"}),
            ("scroll", {"selector": ".pane", "direction": "down", "amount": 20}),
            ("click-and-wait-navigation", {"selector": "button", "timeout": 100}),
        ]:
            with self.subTest(endpoint=endpoint):
                self.assert_error(self.call(f"/api/{endpoint}", {"tabId": tab, **params}),
                                  "ambiguous_target", False)
        filled = self.call("/api/form-fill", {"tabId": tab, "fields": {"Delivery": "address", "Parcel": "code"}})
        for result in filled["results"].values():
            self.assert_error(result, "ambiguous_target", False)
            self.assertEqual(result["state"]["matches"], 2)
        with tempfile.NamedTemporaryFile(suffix=".txt") as upload:
            upload.write(b"KITSUNE_FAKE_UPLOAD_153")
            upload.flush()
            self.assert_error(self.call("/api/upload-file", {"tabId": tab, "path": upload.name}),
                              "ambiguous_target", False)

        state_code = """JSON.stringify({values: Array.from(document.querySelectorAll('input:not([type=file])'), e => e.value),
            scroll: Array.from(document.querySelectorAll('.pane'), e => e.scrollTop),
            files: Array.from(document.querySelectorAll('input[type=file]'), e => e.files.length),
            focused: document.activeElement === document.body, clicks: window.wrappedJSObject.clicks})"""
        state = self.call("/api/js", {"tabId": tab, "code": state_code})
        self.assertEqual(json.loads(state["result"]), {"values": ["", "", "", ""], "scroll": [0, 0],
                                                      "files": [0, 0], "focused": True, "clicks": 0})
        self.assert_ok(self.call("/api/focus", {"tabId": tab, "selector": ".delivery", "first": True}))
        self.assert_ok(self.call("/api/blur", {"tabId": tab, "selector": ".delivery", "first": True}))
        self.assert_ok(self.call("/api/scroll", {"tabId": tab, "selector": ".pane", "amount": 20, "first": True}))
        self.call("/api/form-fill", {"tabId": tab, "fields": {"Delivery": "address", "Parcel": "code"}, "first": True})
        state = self.call("/api/js", {"tabId": tab, "code": state_code})
        self.assertEqual(json.loads(state["result"])["values"], ["address", "", "code", ""])
        self.assertEqual(json.loads(state["result"])["scroll"], [20, 0])

    def test_first_true_clicks_the_first_match_exactly_once(self):
        tab = self.fill_add_form("first")
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": self.SELECTOR, "first": True}))
        self.assertEqual(result["text"], "캠핑 랜턴 삭제", result)
        deadline = time.monotonic() + 10
        while not self.site.state("first")[1] and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1)  # a second click would have arrived by now
        items, submissions = self.site.state("first")
        self.assertEqual(submissions, [("/wishlist/delete", {"item": "캠핑 랜턴"})], "exactly one click")
        self.assertEqual(items, ITEMS[1:])


if __name__ == "__main__":
    unittest.main()
