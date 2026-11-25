"""#632: an icon-only control's snapshot line carries what tells it apart, in a real browser.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is an x-lite-like post whose like, repost and bookmark buttons have no name: one
posts to its form's action, one carries a data-testid, one shows a sprite icon.
#653: a selector that reads those states as attributes names the selector that works.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import re
import threading
import time
import unittest
from urllib.parse import urlparse

from kitsune import script_runner
import real_browser_harness as harness

ICON = '<svg width="16" height="16" aria-hidden="true"><path d="M0 0h16v16H0z"/></svg>'
PAGE = f"""<!doctype html><meta charset="utf-8"><title>x-lite</title>
<main><article>
  <p>First post</p>
  <button class="p-2" data-testid="bookmark-post-t836">{ICON}</button>
  <form method="post" action="/like/t835?csrf=s3cret"><button class="p-2">0</button></form>
  <form method="post" action="/bookmark/t835?csrf=s3cret#top"><button class="p-2">{ICON}</button></form>
  <button class="p-2" type="button"><svg width="16" height="16"><use href="/sprite.svg#icon-repost"/></svg></button>
  <form method="post" action="/share/t835"><button class="p-2" type="button">{ICON}</button></form>
  <form method="post" action="/follow/u1"><button class="p-2" aria-label="Follow">{ICON}</button></form>
</article></main>"""
# #653: the issue's reproduction.
SELECTORS_PAGE = """<!doctype html><meta charset="utf-8"><title>x-lite</title>
<form action="/bookmark/t833" method=post><button type=submit><svg></svg></button></form>
<button data-testid="retweet-post-t833"><svg></svg></button>
<form action="/like/t834?csrf=s3cret" method=post><button type=submit><svg></svg></button></form>"""


class XLiteSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.posts = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply(SELECTORS_PAGE if self.path == "/selectors" else PAGE)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with site.lock:
                    site.posts.append(urlparse(self.path).path)
                self.reply("<!doctype html><title>done</title>bookmarked")

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

    def take_posts(self, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.posts) >= count:
                    break
            time.sleep(0.1)
        with self.lock:
            posts, self.posts = self.posts, []
        return posts

    def stop(self):
        self.server.stop()


class _SuiteBridge:
    """The script host's bridge client (script_runner.run_python), over the suite's own bridge."""

    def __init__(self, call):
        self.call = call

    def request(self, method, route, *, json=None, **_):
        return self.call(route, json)


class RealBrowserUnnamedControlTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = XLiteSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_an_unnamed_control_carries_its_form_action_testid_or_icon(self):
        tab = self.open_tab(self.site.origin + "/post/t835")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": False})["snapshot"]
        lines = [line.strip() for line in snapshot.splitlines() if line.strip().startswith("- button")]
        self.assertEqual([re.sub(r"\[ref=\w+\]", "[ref]", line) for line in lines], [
            '- button "" [ref] [testid="bookmark-post-t836"]',
            '- button "0" [ref]',
            # The action's path only: its query and fragment can hold tokens.
            '- button "" [ref] [action="/bookmark/t835"]',
            '- button "" [ref] [icon="icon-repost"]',
            # A type=button does not submit to its form's action.
            '- button "" [ref]',
            '- button "Follow" [ref]',
        ], snapshot)
        self.assertNotIn("s3cret", snapshot)

        ref = re.search(r'button "" \[ref=(\w+)\] \[action="/bookmark/t835"', snapshot).group(1)
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref}))
        self.assertEqual(self.site.take_posts(1), ["/bookmark/t835"])

    def test_a_selector_reading_action_or_testid_as_an_attribute_names_the_working_one(self):
        tab = self.open_tab(self.site.origin + "/selectors")
        cases = [
            ('button[action="/bookmark/t833"]', 'form[action="/bookmark/t833"] button'),
            # The snapshot shows the path only; the attribute also holds a query.
            ('button[action="/like/t834"]', 'form[action*="/like/t834"] button'),
            ('button[testid="retweet-post-t833"]', 'button[data-testid="retweet-post-t833"]'),
            ('[testid="retweet-post-t833"]', '[data-testid="retweet-post-t833"]'),
        ]
        for selector, working in cases:
            with self.subTest(selector=selector):
                query = self.call("/api/query", {"tabId": tab, "selector": selector})
                self.assertEqual(query["items"], [])
                self.assertTrue(query["hint"].endswith(f"use {working}."), query)
                clicked = self.call("/api/click", {"tabId": tab, "selector": selector})
                self.assert_error(clicked, "not_found", True)
                self.assertIn(f"use {working}.", clicked["hint"])
                self.assertEqual(len(self.call("/api/query", {"tabId": tab, "selector": working})["items"]), 1)
        # In a script: query warns with the hint, and a failed click carries it.
        def run(code):
            return asyncio.run(script_runner.run_python(code, _SuiteBridge(self.call), tab_id=tab))

        warned = run("import warnings\n"
                     "with warnings.catch_warnings(record=True) as caught:\n"
                     "    warnings.simplefilter('always')\n"
                     "    rows = tab.query('button[action=\"/bookmark/t833\"]')\n"
                     "result = [rows, [str(w.message) for w in caught]]")
        self.assertEqual(warned.get("result"), [[], [
            "Query matched no elements: Snapshots print a control's form action as action, which is the "
            "form's attribute: use form[action=\"/bookmark/t833\"] button."]], warned)
        stopped = run("tab.click('[testid=\"retweet-post-t833\"]')")
        self.assert_error(stopped["failed_operation"], "not_found", True)
        self.assertIn('use [data-testid="retweet-post-t833"].', stopped["failed_operation"]["hint"])
        # A form action that is not on the page has no hint to give.
        self.assertNotIn("hint", self.call("/api/query", {"tabId": tab, "selector": 'button[action="/none"]'}))


if __name__ == "__main__":
    unittest.main()
