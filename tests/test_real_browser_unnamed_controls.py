"""#632: an icon-only control's snapshot line carries what tells it apart, in a real browser.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is an x-lite-like post whose like, repost and bookmark buttons have no name: one
posts to its form's action, one carries a data-testid, one shows a sprite icon.
"""
from http.server import BaseHTTPRequestHandler
import re
import threading
import time
import unittest
from urllib.parse import urlparse

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


class XLiteSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.posts = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply(PAGE)

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
            '- button "" [ref] [testid="bookmark-post-t836" value=""]',
            '- button "0" [ref] [value=""]',
            # The action's path only: its query and fragment can hold tokens.
            '- button "" [ref] [action="/bookmark/t835" value=""]',
            '- button "" [ref] [icon="icon-repost" value=""]',
            # A type=button does not submit to its form's action.
            '- button "" [ref] [value=""]',
            '- button "Follow" [ref] [value=""]',
        ], snapshot)
        self.assertNotIn("s3cret", snapshot)

        ref = re.search(r'button "" \[ref=(\w+)\] \[action="/bookmark/t835"', snapshot).group(1)
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref}))
        self.assertEqual(self.site.take_posts(1), ["/bookmark/t835"])


if __name__ == "__main__":
    unittest.main()
