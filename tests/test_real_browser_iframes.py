"""Same-origin iframe forms in a real browser (issue #157).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
checkout page embeds the address editor from #157 in a same-origin
<iframe title="배송지 편집"> and a widget from another origin (localhost instead of
127.0.0.1). Its server records every address it saves, which is how the test proves
that typing and saving through refs reached the frame's form.
"""
from http.server import BaseHTTPRequestHandler
import re
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import real_browser_harness as harness

ZIP = "06236"
DETAIL = "KITSUNE_FAKE_DETAIL_157"


class AddressSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.saved = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_address[1]
                pages = {
                    "/checkout": "<!doctype html><title>checkout</title><h1>주문서</h1>"
                                 '<iframe title="배송지 편집" src="/address-editor" width="500" height="250"></iframe>'
                                 f'<iframe title="결제 위젯" src="http://localhost:{port}/widget?session=KITSUNE_FAKE_TOKEN"'
                                 ' width="300" height="100"></iframe>',
                    "/address-editor": "<!doctype html><title>address</title>"
                                       '<form method="post" action="/address">'
                                       '<label>우편번호 <input name="zip" value="04524"></label>'
                                       '<label>상세주소 <input name="detail"></label>'
                                       "<button>저장</button></form>",
                    "/widget": '<!doctype html><label>카드 번호 <input name="card"></label>',
                }
                page = pages.get(urlparse(self.path).path)
                if page is None:
                    self.send_error(404)
                    return
                self.reply(page)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with site.lock:
                    site.saved.append((urlparse(self.path).path, form))
                self.reply("<!doctype html><p>저장되었습니다</p>")

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

    def wait_for_save(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.saved:
                    return list(self.saved)
            time.sleep(0.1)
        with self.lock:
            return list(self.saved)

    def stop(self):
        self.server.stop()


class RealBrowserIframeTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = AddressSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def ref(self, tab, line):
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        match = re.search(re.escape(line) + r" \[ref=(s\d+e\d+)\]", snapshot)
        self.assertIsNotNone(match, f"{line} in\n{snapshot}")
        return match.group(1), snapshot

    def test_same_origin_frame_form_is_observed_filled_and_saved_through_refs(self):
        tab = self.open_tab(f"{self.site.origin}/checkout")
        _, snapshot = self.ref(tab, 'iframe "배송지 편집"')
        lines = snapshot.splitlines()
        frame = next(i for i, line in enumerate(lines) if 'iframe "배송지 편집"' in line)
        self.assertTrue(any('textbox "우편번호"' in line for line in lines[frame:]), snapshot)
        self.assertRegex(snapshot, r'textbox "상세주소" \[ref=s\d+e\d+\]')
        self.assertRegex(snapshot, r'button "저장" \[ref=s\d+e\d+\]')
        # The cross-origin widget is reported, without its query, and its field stays unread.
        self.assertRegex(snapshot, r'iframe "결제 위젯" \[ref=s\d+e\d+\] '
                                   r'\[content=cross_origin src="http://localhost:\d+/widget"\]')
        self.assertNotIn("KITSUNE_FAKE_TOKEN", snapshot)
        self.assertNotIn("카드 번호", snapshot)

        forms = self.call("/api/forms", {"tabId": tab})
        fields = {f["name"]: f for f in forms["fields"]}
        self.assertEqual(fields["zip"]["frame"], "배송지 편집", forms)
        self.assertEqual((fields["zip"]["label"], fields["zip"]["value"]), ("우편번호", "04524"))
        self.assertEqual(fields["detail"]["frame"], "배송지 편집", forms)
        self.assertNotIn("card", fields)
        self.assertEqual([(f["title"], f["content"]) for f in forms["unavailableFrames"]],
                         [("결제 위젯", "cross_origin")])

        zip_ref, _ = self.ref(tab, 'textbox "우편번호"')
        self.assert_ok(self.call("/api/fill", {"tabId": tab, "ref": zip_ref, "value": ZIP}))
        detail_ref, _ = self.ref(tab, 'textbox "상세주소"')
        self.assert_ok(self.call("/api/type", {"tabId": tab, "ref": detail_ref, "text": DETAIL}))
        save_ref, _ = self.ref(tab, 'button "저장"')
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": save_ref}))
        self.assertEqual(self.site.wait_for_save(), [("/address", {"zip": ZIP, "detail": DETAIL})])


if __name__ == "__main__":
    unittest.main()
