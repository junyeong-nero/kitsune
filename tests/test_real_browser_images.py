"""Images in a real browser (issue #207): named images are snapshot lines whose ref an element
screenshot accepts; decorative images and image sources are not listed.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/snapshot_images.cjs covers the same rules on a mocked DOM.
"""
import base64
from http.server import BaseHTTPRequestHandler
import re
import struct
import unittest
import zlib

import real_browser_harness as harness


def png(width, height):
    """A solid grey PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


IMAGE = png(42, 14)
DATA_URL = "data:image/png;base64," + base64.b64encode(IMAGE).decode()
PAGE = f"""<!doctype html><meta charset="utf-8"><title>Image puzzle</title><main>
<h2>이미지 퍼즐: 왜곡된 문자</h2>
<p>이미지에 보이는 문자 6개를 입력하세요.</p>
<img id="puzzle" src="/puzzle.png" alt="왜곡된 문자 퍼즐" width="420" height="140">
<img src="/puzzle.png" alt="" width="20" height="20">
<img src="/puzzle.png" alt="Spacer" role="presentation" width="20" height="20">
<img src="/puzzle.png" alt="Hidden" aria-hidden="true" width="20" height="20">
<img src="{DATA_URL}" alt="Inline chart" width="20" height="20">
<label>문자 <input></label> <button>확인</button>
<canvas role="img" aria-label="Video player" width="160" height="80"></canvas>
<span role="img" aria-label="80% mark">mark</span>
<svg role="img" aria-label="Vector icon" width="32" height="32"><circle r="10"/></svg>
<canvas width="100" height="60"></canvas>
<div role="group" aria-label="Preview tile">Preview tile</div>
<form name="host"></form></main>"""
IMAGE_LINE = re.compile(r'^ {2}- img "왜곡된 문자 퍼즐" \[ref=(\S+)\]$', re.M)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body, kind = {"/puzzle": (PAGE.encode(), "text/html; charset=utf-8"),
                      "/puzzle.png": (IMAGE, "image/png")}.get(self.path, (None, None))
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserImageTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_named_image_is_listed_with_a_ref_and_decorative_images_are_not(self):
        tab = self.open_tab(f"{self.site.origin}/puzzle")
        for text in (False, True):
            snapshot = self.call("/api/snapshot", {"tabId": tab, **({"text": True} if text else {})})["snapshot"]
            self.assertRegex(snapshot, IMAGE_LINE, snapshot)
            self.assertRegex(snapshot, r'(?m)- img "Inline chart" \[ref=\S+\]$')
            self.assertNotRegex(snapshot, r'Spacer|Hidden|img ""|puzzle\.png|data:|src=')
            self.assertRegex(snapshot, r'- textbox "문자" \[ref=\S+\]')
            if text:
                self.assertIn('- text "이미지에 보이는 문자 6개를 입력하세요."', snapshot)

    def test_graphics_and_named_containers_are_addressable(self):
        tab = self.open_tab(f"{self.site.origin}/puzzle")
        snapshot = self.call("/api/snapshot", {"tabId": tab, "text": True})["snapshot"]
        for role, name in [("img", "Video player"), ("img", "80% mark"),
                           ("img", "Vector icon"), ("canvas", ""), ("group", "Preview tile")]:
            match = re.search(rf'- {role} "{name}" \[ref=(\S+)\]', snapshot)
            self.assertIsNotNone(match, snapshot)
            result = self.call("/api/hover", {"tabId": tab, "selector": match[1]})
            self.assertTrue(result.get("ok"), result)
        self.assertNotIn('text "Preview tile"', snapshot)

    def test_named_svg_ref_dispatches_exactly_one_click(self):
        tab = self.open_tab(f"{self.site.origin}/puzzle")
        self.call("/api/js", {"tabId": tab, "code": """
            window.svgClicks = 0;
            document.querySelector('svg').addEventListener('click', () => window.svgClicks++);
        """})
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        ref = re.search(r'img "Vector icon" \[ref=(\S+)\]', snapshot)[1]
        result = self.call("/api/click", {"tabId": tab, "ref": ref, "includeSnapshot": False})
        self.assertTrue(result.get("ok"), result)
        state = self.call("/api/js", {"tabId": tab, "code": "window.svgClicks"})
        self.assertEqual(state["result"], "1", state)

    def test_element_screenshot_takes_the_image_ref(self):
        tab = self.open_tab(f"{self.site.origin}/puzzle")
        ref = IMAGE_LINE.search(self.call("/api/snapshot", {"tabId": tab})["snapshot"])[1]
        self.assertRegex(ref, r"^e\d+$")
        shot = self.call("/api/element-screenshot", {"tabId": tab, "selector": "", "ref": ref, "imagePreview": True})
        self.assertTrue(shot.get("dataUrl", "").startswith("data:image/"), shot)
        self.assertEqual(shot["bounds"]["width"], 420, shot)


if __name__ == "__main__":
    unittest.main()
