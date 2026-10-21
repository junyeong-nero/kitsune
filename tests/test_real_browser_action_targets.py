"""Action targeting on a page with repeated controls, in a real browser (issue #133).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is the wishlist page from #133: per-item delete forms above an add form. Its
server records every form submission, which is how the tests prove server-side that
nothing was deleted.
"""
from http.server import BaseHTTPRequestHandler
import html
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

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


if __name__ == "__main__":
    unittest.main()
