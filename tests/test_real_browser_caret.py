"""Clicks place the caret and typing appends in a contenteditable note, in a real browser (#159).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is the note task from #159: a two-paragraph contenteditable note whose save
button posts its text and the number of input events the page saw.
"""
from http.server import BaseHTTPRequestHandler
import json
import threading
import time
import unittest

import real_browser_harness as harness

FIRST = "프로젝트 메모: 금요일까지 초안을 공유합니다."
LAST = "회의는 오후 3시에 시작합니다."
SENTENCE = " 추가 문장입니다."
PAGE = f"""<!doctype html><title>note</title>
<div id="note" contenteditable="true"><p id="first">{FIRST}</p><p id="last">{LAST}</p></div>
<button id="save">저장</button>
<script>
  let inputs = 0;
  note.addEventListener('input', () => inputs++);
  save.addEventListener('click', () => fetch('/save', {{ method: 'POST',
    body: JSON.stringify({{ paragraphs: [...note.querySelectorAll('p')].map(p => p.textContent), inputs }}) }}));
</script>"""


class NoteSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.saves = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with site.lock:
                    site.saves.append(json.loads(body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    def wait_for_save(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.saves:
                    return self.saves[0]
            time.sleep(0.1)
        raise AssertionError("the note was not saved")

    def stop(self):
        self.server.stop()


class RealBrowserCaretTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = NoteSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_click_last_paragraph_then_type_appends_to_the_note(self):
        tab = self.open_tab(f"{self.site.origin}/note")
        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#last", "includeSnapshot": False}))
        self.assert_ok(self.call("/api/type", {"tabId": tab, "selector": "#note", "text": SENTENCE,
                                               "includeSnapshot": False}))
        self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#save", "includeSnapshot": False}))
        saved = self.site.wait_for_save()
        self.assertEqual(saved["paragraphs"], [FIRST, LAST + SENTENCE], "the sentence ends the note")
        self.assertGreater(saved["inputs"], 0, "the page saw input events")


if __name__ == "__main__":
    unittest.main()
