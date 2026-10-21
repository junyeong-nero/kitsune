"""browser_submit_form runs the page's submit handler, in a real browser (issue #154).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Each
fixture form posts to /raw, and its submit handler rewrites the request to /handled with
a token. form.submit() skips the handler, so the server seeing /handled proves it ran.
"""
from http.server import BaseHTTPRequestHandler
import json
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import real_browser_harness as harness
from test_bridge_security import _request

HANDLER = ("<script>document.querySelector('form').addEventListener('submit', e => {"
           "e.target.action = '/handled'; e.target.elements.token.value = 'from-handler'; });</script>")
PAGES = {
    # The only button has no type attribute, so it is the submit button.
    "/typeless": '<button>Save</button>',
    # No submit control: submitted with requestSubmit.
    "/no-button": '<button type="button">Preview</button>',
    "/modes": '<input type="password" name="password"><button>Save</button>',
}


class SubmitHandlerSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.posts = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                controls = PAGES.get(urlparse(self.path).path)
                if controls is None:
                    self.send_error(404)
                    return
                self.reply(f'<!doctype html><title>submit</title><form method="post" action="/raw">'
                           f'<input name="title"><input type="hidden" name="token" value="">{controls}</form>'
                           f'{HANDLER}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with site.lock:
                    site.posts.append((urlparse(self.path).path, form))
                self.reply("<!doctype html><title>done</title>done")

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
        time.sleep(0.3)  # a second submission would arrive by now
        with self.lock:
            posts, self.posts = self.posts, []
        return posts

    def stop(self):
        self.server.stop()


class RealBrowserSubmitFormHandlerTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = SubmitHandlerSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_task_modes_read_and_draft_without_submitting(self):
        tab = self.open_tab(self.site.origin + "/modes")

        def request(mode, path, body):
            headers = {**self.bridge.auth(), "X-Kitsune-Mode": mode, "Content-Type": "application/json"}
            status, _, text = _request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                       json.dumps({"tabId": tab, **body}), timeout=10)
            return status, json.loads(text)

        status, result = request("read", "/api/fill", {"selector": "input[name=title]", "value": "blocked"})
        self.assertEqual((status, result["code"]), (403, "mode_restricted"))
        status, result = request("draft", "/api/fill", {"selector": "input[name=title]", "value": "draft"})
        self.assertEqual(status, 200, result)
        self.assert_ok(result)
        status, result = request("draft", "/api/form-fill", {"fields": {"title": "blocked", "password": "secret"}})
        self.assertEqual((status, result["code"]), (403, "mode_restricted"))
        self.assertEqual(result["state"]["reason"], "sensitive_field")
        status, result = request("draft", "/api/submit-form", {})
        self.assertEqual((status, result["code"]), (403, "mode_restricted"))
        status, forms = request("read", "/api/forms", {})
        self.assertEqual(status, 200, forms)
        self.assertIn('"value": "draft"', json.dumps(forms))
        self.assertNotIn('"value": "blocked"', json.dumps(forms))
        with self.site.lock:
            self.assertEqual(self.site.posts, [])
        # A different act client sharing this daemon remains unrestricted.
        self.assert_ok(self.call("/api/fill", {"tabId": tab, "selector": "input[name=title]", "value": "act"}))

    def test_submit_runs_the_page_submit_handler(self):
        for path, submitted, button in [("/typeless", "button", "Save"), ("/no-button", "requestSubmit", "")]:
            with self.subTest(path):
                tab = self.open_tab(self.site.origin + path)
                self.assert_ok(self.call("/api/fill", {"tabId": tab, "selector": "input[name=title]", "value": "hello"}))
                result = self.assert_ok(self.call("/api/submit-form", {"tabId": tab}))
                self.assertEqual((result["submitted"], result["form"]["button"]), (submitted, button), result)
                self.assertEqual(self.site.take_posts(1),
                                 [("/handled", {"title": "hello", "token": "from-handler"})])


if __name__ == "__main__":
    unittest.main()
