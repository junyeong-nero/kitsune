"""Issues #218/#221: default readable snapshots and document-scoped node refs.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import re
import unittest

import real_browser_harness as harness

PAGE = """<!doctype html><title>Orders</title>
<h1>주문 T-3</h1><p>주문일: 2027-05-12</p><p>배송 예정일: 2027-05-20</p>
<p>See <a href="/order/2">order detail</a> for details.</p>
<a href="#dates">Dates</a>
<a href="https://user:password@example.invalid/order/2?token=SECRET#password=HASH">Private link</a>
<a href="javascript:alert('PAYLOAD')">Script link</a>
<a href="data:text/plain,PAYLOAD">Data link</a>
<a href="file:///private/LOCALPATH">Local link</a>
<input type="password" value="SECRET_FIELD">
<textarea autocomplete="cc-number">SECRET_CARD</textarea>
<button id="act">Inspect order</button>
<div id="slot"></div>
"""


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


class RealBrowserObservationTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def snapshot(self, tab, **scope):
        result = self.call("/api/snapshot", {"tabId": tab, **scope})
        self.assertNotIn("error", result)
        return result["snapshot"]

    def js(self, tab, code):
        result = self.call("/api/js", {"tabId": tab, "code": code})
        self.assertNotIn("error", result)
        return result

    def test_default_snapshot_text_urls_and_ref_lifetime(self):
        tab = self.open_tab(self.page.origin + "/?token=PAGE_SECRET#password=PAGE_HASH")
        metadata = self.call("/api/snapshot", {"tabId": tab})
        self.assertEqual(metadata["url"], self.page.origin + "/?token=[redacted]#password=[redacted]")
        self.assertEqual(metadata["title"], "Orders")
        snapshot = self.snapshot(tab)
        self.assertIn('text "주문일: 2027-05-12"', snapshot)
        self.assertIn('text "배송 예정일: 2027-05-20"', snapshot)
        self.assertIn('text "See order detail for details."', snapshot)
        self.assertIn(f'- /url: "{self.page.origin}/order/2"', snapshot)
        self.assertIn(f'- /url: "{self.page.origin}/?token=[redacted]#dates"', snapshot)
        self.assertIn('- /url: "https://example.invalid/order/2?token=[redacted]#password=[redacted]"', snapshot)
        for secret in ("SECRET", "HASH", "PAYLOAD", "LOCALPATH", "user:password"):
            self.assertNotIn(secret, snapshot)
        ref = re.search(r'button "Inspect order" \[ref=(e\d+)\]', snapshot).group(1)
        found = self.call("/api/find", {"tabId": tab, "query": "#act"})["results"][0]["ref"]
        self.assertEqual(found, ref)
        self.assertIn(f'[ref={ref}]', self.snapshot(tab, ref=ref))
        self.assertIn(f'[ref={ref}]', self.snapshot(tab))
        self.assert_ok(self.call("/api/focus", {"tabId": tab, "ref": found}))

        # Re-rendering an identical control must not redirect its predecessor's ref.
        self.js(tab, "document.getElementById('act').remove(); "
                     "document.getElementById('slot').innerHTML = '<button id=act>Inspect order</button>'; 'done'")
        replacement = re.search(r'button "Inspect order" \[ref=(e\d+)\]', self.snapshot(tab)).group(1)
        self.assertNotEqual(replacement, ref)
        self.assert_error(self.call("/api/focus", {"tabId": tab, "ref": ref}), "stale_ref", True)
        self.assert_ok(self.call("/api/focus", {"tabId": tab, "ref": replacement}))
        self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": self.page.origin + "/next", "wait": True}))
        self.snapshot(tab)
        stale = self.call("/api/focus", {"tabId": tab, "ref": replacement})
        self.assert_error(stale, "stale_ref", True)
        self.assertEqual(stale["state"]["reason"], "other_document")

    def test_retired_frame_document_refs_are_invalid(self):
        tab = self.open_tab(self.page.origin + "/")
        self.js(tab, "const frame = document.createElement('iframe'); frame.id = 'frame'; "
                     "frame.src = '/frame'; document.body.append(frame); 'done'")
        ready = self.call("/api/wait-for-element", {"tabId": tab, "selector": "iframe", "timeout": 5000})
        self.assert_ok(ready)
        # Wait for the frame's load without page-facing automation dependencies.
        self.js(tab, "new Promise(resolve => { const f = document.getElementById('frame'); "
                     "if (f.contentDocument.readyState === 'complete') resolve(true); "
                     "else f.addEventListener('load', () => resolve(true), {once:true}); })")
        frame_snapshot = self.snapshot(tab)
        refs = re.findall(r'button "Inspect order" \[ref=(e\d+)\]', frame_snapshot)
        self.assertEqual(len(refs), 2, frame_snapshot)
        child_ref = refs[1]
        self.js(tab, "new Promise(resolve => { const f = document.getElementById('frame'); "
                     "window.retired = f.contentDocument; "
                     "f.addEventListener('load', () => resolve(true), {once:true}); f.src = '/frame-next'; })")
        self.assert_error(self.call("/api/focus", {"tabId": tab, "ref": child_ref}), "stale_ref", True)
        child_ref = re.findall(r'button "Inspect order" \[ref=(e\d+)\]', self.snapshot(tab))[1]
        self.js(tab, "window.retired = document.getElementById('frame').contentDocument; "
                     "document.getElementById('frame').remove(); 'done'")
        # Retaining the child document must not keep its refs actionable after removal.
        self.assert_error(self.call("/api/focus", {"tabId": tab, "ref": child_ref}), "stale_ref", True)



if __name__ == "__main__":
    unittest.main()
