"""Issues #218/#221: default readable snapshots and document-scoped node refs.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
from http.server import BaseHTTPRequestHandler
import re
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools

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
        # Same-origin link URLs are paths, redacted like absolute ones (#310).
        self.assertIn('- /url: "/order/2"', snapshot)
        self.assertIn('- /url: "/?token=[redacted]#dates"', snapshot)
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

    def test_core_snapshot_find_and_full_toolset_text_modes_on_long_page(self):
        tab = self.open_tab(self.page.origin + "/")
        self.js(tab, "document.body.innerHTML = '<p>Readable static fact</p>' + "
                     "Array.from({length:220}, (_,i) => '<button>Control ' + i + ' ' + 'padding '.repeat(8) + '</button>').join('')"
                     " + '<button id=deep>Deep target 71</button>'; 'done'")
        def post(endpoint, body, **kwargs):
            return self.call(endpoint, body)
        with patch.object(playwright_tools, "_current_tab", tab), patch.object(tools, "_post", side_effect=post):
            # Inline core snapshots are bounded (#262); browser_find searches past the cut.
            default = playwright_tools.browser_snapshot()
            self.assertLessEqual(len(default["snapshot"]), playwright_tools.SNAPSHOT_LIMIT)
            self.assertIs(default["truncated"], True)
            self.assertIn('text "Readable static fact"', default["snapshot"])
            self.assertNotIn("Deep target 71", default["snapshot"])
            found = playwright_tools.browser_find(text="Deep target 71")
            match = next(m["match"] for m in found["matches"] if 'button "Deep target 71"' in m["match"])
            deep = re.search(r"\[ref=(e\d+)\]", match)[1]
            scoped = playwright_tools.browser_snapshot(target=deep)
            self.assertIn(f'- button "Deep target 71" [ref={deep}]', scoped["snapshot"])
            for kwargs in ({}, {"text": False}):
                compact = tools.browser_snapshot(tab_id=tab, **kwargs)
                self.assertNotIn('text "Readable static fact"', compact["snapshot"])
                self.assertIn('button "Deep target 71"', compact["snapshot"])
                self.assertNotIn("truncated", compact)
            bounded = tools.browser_snapshot(tab_id=tab, text=True)
            self.assertLessEqual(len(bounded["snapshot"]), 12000)
            self.assertIs(bounded["truncated"], True)
            self.assertIn("truncated; scope", bounded["snapshot"])
            self.assertIn('text "Readable static fact"', bounded["snapshot"])

    def test_public_budget_after_url_expansion_and_http_max_chars(self):
        tab = self.open_tab(self.page.origin + "/")
        self.js(tab, """document.body.innerHTML = '<a href="https://example.test/?' +
                     Array(1000).fill('token=x').join('&') +
                     '">Long URL</a><main><button id=target>Budget target</button></main>'; 'done'""")
        default = self.call("/api/snapshot", {"tabId": tab})
        self.assertGreater(len(default["snapshot"]), 17000)
        self.assertNotIn("truncated", default)
        for options, limit in (({"text": True}, 12000), ({"maxChars": 200}, 200),
                               ({"text": False, "maxChars": 200}, 200), ({"maxChars": 5}, 5)):
            with self.subTest(options=options):
                bounded = self.call("/api/snapshot", {"tabId": tab, **options})
                self.assertLessEqual(len(bounded["snapshot"]), limit)
                self.assertIs(bounded["truncated"], True)
                self.assertIn("scope", bounded["hint"])
                self.assertNotIn("token=x", bounded["snapshot"])
        # A 300-parameter raw link fits a 4k action snapshot but expands beyond it.
        self.js(tab, "document.querySelector('a').href = 'https://example.test/?' + Array(300).fill('token=x').join('&'); 'done'")
        target = re.search(r'button "Budget target" \[ref=(e\d+)\]', self.snapshot(tab))[1]
        result = self.assert_ok(self.call("/api/click", {
            "tabId": tab, "ref": target, "snapshot": "full", "snapshotText": True}))
        self.assertLessEqual(len(result["snapshot"]), 4000)
        self.assertIs(result["snapshot_truncated"], True)
        self.assertIn("scope", result["snapshot_hint"])
        self.assertEqual(result["target_ref"], target)
        self.assertRegex(result["snapshot"], r'- main "" \[ref=e\d+\]\n  - button "Budget target"')
        self.assertNotIn("token=x", result["snapshot"])

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
