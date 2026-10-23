"""Issue #222: real JPEG encoding and exact shadow-root capture targets.

Opt-in via KITSUNE_E2E_BROWSER; uses an isolated profile and bridge (real_browser_harness).
"""
import base64
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Screenshot fixture</title>
<style>body { margin: 0; } button { width: 160px; height: 64px; }</style>
<h1>Capture formats</h1><div id="components"></div><div style="height:1400px">Full page tail</div>
<script>
for (const mode of ['open', 'closed']) {
  const host = document.createElement('capture-host');
  document.querySelector('#components').append(host);
  const root = host.attachShadow({mode});
  const innerHost = document.createElement('capture-inner');
  root.append(innerHost);
  const innerRoot = innerHost.attachShadow({mode});
  const button = document.createElement('button');
  button.textContent = mode + ' shadow capture';
  button.style.cssText = 'display:block;width:160px;height:64px;background:#f80;color:#000';
  innerRoot.append(button);
}
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserScreenshotTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def assert_image(self, shot, image_format):
        self.assertNotIn("error", shot)
        prefix = f"data:image/{image_format};base64,"
        self.assertTrue(shot.get("dataUrl", "").startswith(prefix), str(shot)[:500])
        raw = base64.b64decode(shot["dataUrl"][len(prefix):])
        self.assertTrue(raw.startswith(b"\xff\xd8\xff" if image_format == "jpeg" else b"\x89PNG\r\n\x1a\n"))
        return raw

    def test_viewport_and_full_page_formats(self):
        tab = self.open_tab(self.site.origin + "/")
        for endpoint in ("screenshot", "full-page-screenshot"):
            for image_format in ("png", "jpeg"):
                with self.subTest(endpoint=endpoint, image_format=image_format):
                    self.assert_image(self.call("/api/" + endpoint, {
                        "tabId": tab, "format": image_format, "imagePreview": False}), image_format)
        self.assert_image(self.call("/api/screenshot", {"tabId": tab}), "png")

    def test_nested_open_and_closed_shadow_ref_capture_and_cleanup(self):
        tab = self.open_tab(self.site.origin + "/")
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        for mode in ("open", "closed"):
            ref = re.search(r'button "' + mode + r' shadow capture" \[ref=(e\d+)\]', snapshot)
            self.assertIsNotNone(ref, snapshot)
            for image_format in ("png", "jpeg"):
                shot = self.call("/api/element-screenshot", {
                    "tabId": tab, "ref": ref[1], "selector": "", "format": image_format, "imagePreview": False})
                self.assert_image(shot, image_format)
                self.assertEqual(shot["bounds"]["width"], 160)
                self.assertEqual(shot["bounds"]["height"], 64)
                self.assertTrue(shot["complete"], shot)
                # Bind the same exact ref to inspect even the closed-root node after cleanup.
                clean = self.call("/api/js", {"tabId": tab, "selector": ref[1], "code":
                    "!Array.from(__kitsuneElement.attributes).some(a => a.name.startsWith('data-kitsune-capture-'))"})
                self.assertEqual(clean.get("result"), "true", clean)

    def test_playwright_jpg_filename_saves_actual_jpeg(self):
        tab = self.open_tab(self.site.origin + "/")
        def post(endpoint, body, **kwargs):
            return self.call(endpoint, body)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(playwright_tools, "_current_tab", tab), patch.object(server, "_post", side_effect=post):
            destination = Path(directory, "page.jpg")
            result = playwright_tools.browser_take_screenshot(type="jpeg", fullPage=True, filename=str(destination))
            self.assertEqual(result["saved_to"], str(destination))
            self.assertTrue(destination.read_bytes().startswith(b"\xff\xd8\xff"))
            self.assertNotIn("dataUrl", result)


if __name__ == "__main__":
    unittest.main()
