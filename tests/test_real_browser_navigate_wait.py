"""A page whose DOM is parsed while its load is still pending, in a real browser (issue #561).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
An image-heavy page is readable long before its load finishes. browser_navigate and a new tab
that waits for its load return ok with load_pending once their wait times out on such a page, so a
browser_run_python script goes on. A navigation that no document answered, or whose document has
not parsed its DOM, is still a timeout, after the full 15 s. Since #601 a parsed page does not wait
that long: like a click (#588), navigate and a waiting new tab return shortly after its DOM is parsed.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys
import threading
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

PAGES = {
    "/start": """<!doctype html><title>Start</title><h1>Start</h1>
<a href="/heavy">Listings</a> <a href="/plain">Plain</a>""",
    "/plain": "<!doctype html><title>Plain</title><h1>Plain page</h1>",
    # The image never arrives during a test, so the page stays interactive and its load pending.
    "/heavy": """<!doctype html><title>Heavy</title><h1>Listings</h1>
<a href="/start">Back to start</a><img src="/held.png" alt="Home">""",
    # A parser-blocking script that never arrives: the document exists, but its DOM is not parsed.
    "/blocked": """<!doctype html><title>Blocked</title><script src="/held.js"></script><h1>Never parsed</h1>""",
}
# Longer than the 15 s load wait; a test's cleanup ends the held requests sooner.
HOLD_SECONDS = 40


class Handler(BaseHTTPRequestHandler):
    # Replaced by each test and set in its cleanup: held requests answer only then.
    release = threading.Event()

    def do_GET(self):
        page = PAGES.get(self.path)
        if page is None:  # /held.png, /held.js and /unanswered, a document that never answers
            self.release.wait(HOLD_SECONDS)
        try:
            if page is None:
                self.send_error(404)
                return
            data = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the tab was closed while its request was held

    def log_message(self, *args):
        pass


class RealBrowserNavigateWaitTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        Handler.release = threading.Event()
        self.addCleanup(Handler.release.set)

    def assert_heavy_page_is_readable(self, tab):
        self.assertIn('heading "Listings"', self.call("/api/snapshot", {"tabId": tab})["snapshot"])
        found = self.call("/api/find", {"tabId": tab, "query": "Back to start"})["results"]
        self.assertEqual(found[0]["text"], "Back to start", found)
        self.assertEqual(self.call("/api/page-info", {"tabId": tab})["readyState"], "interactive")

    def test_a_script_goes_on_after_navigating_to_a_page_still_loading_its_images(self):
        heavy = f"{self.site.origin}/heavy"
        code = (f"opened = tab.navigate({heavy!r})\n"
                "result = {'ok': opened['ok'], 'load_pending': opened.get('load_pending'),\n"
                "          'carried': opened.get('snapshot'), 'snapshot': tab.snapshot(text=True),\n"
                "          'found': tab.find('Back to start')[0]['text']}")

        async def scenario():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "navigate-wait"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                opened = await session.call_tool("browser_tabs", {"action": "new", "url": f"{self.site.origin}/start"})
                self.assertFalse(opened.isError, opened)
                selected = await session.call_tool("browser_run_python", {"code": "result = tab.id"})
                self.assertFalse(selected.isError, selected)
                tab = json.loads(selected.content[0].text)["result"]
                self.addCleanup(self.call, "/api/close-tab", {"tabId": tab})
                script = await session.call_tool("browser_run_python", {"code": code, "timeout": 60})
                self.assertFalse(script.isError, script)
                return tab, json.loads(script.content[0].text)["result"]

        started = time.monotonic()
        tab, result = asyncio.run(asyncio.wait_for(scenario(), 90))
        self.assertLess(time.monotonic() - started, 10, "the navigate did not wait out its 15 s (#601)")
        self.assertEqual({key: result[key] for key in ("ok", "load_pending", "found")},
                         {"ok": True, "load_pending": True, "found": "Back to start"})
        self.assertIn("Listings", result["carried"], "the navigate result carries the page's snapshot")
        self.assertIn("Listings", result["snapshot"])
        self.assert_heavy_page_is_readable(tab)

    def test_a_click_into_a_page_still_loading_its_images_returns_once_it_is_parsed(self):
        # #588: the click does not wait for the load, only a short grace period after the DOM is parsed.
        tab = self.open_tab(f"{self.site.origin}/start")
        started = time.monotonic()
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "a[href='/heavy']"}))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, result)
        self.assertEqual(result.get("navigated"), {"url": f"{self.site.origin}/heavy", "title": "Heavy"})
        self.assertIs(result.get("load_pending"), True, result)
        self.assertNotIn("navigation_pending", result)
        self.assertIn('heading "Listings"', result["snapshot"])
        self.assert_heavy_page_is_readable(tab)

    def test_a_click_into_an_ordinary_page_still_waits_for_its_load(self):
        tab = self.open_tab(f"{self.site.origin}/start")
        result = self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "a[href='/plain']"}))
        self.assertEqual(result.get("navigated"), {"url": f"{self.site.origin}/plain", "title": "Plain"})
        self.assertNotIn("load_pending", result)
        self.assertNotIn("navigation_pending", result)
        self.assertIn('heading "Plain page"', result["snapshot"])

    def test_navigating_away_from_a_page_still_loading_waits_for_the_new_page(self):
        # Leaving a page whose load is pending ends that load with a late 'complete' for its URL. It is
        # not the destination's: each navigate waited one page behind and returned at once (#561).
        tab = self.open_tab(f"{self.site.origin}/start")
        self.assertIs(self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "a[href='/heavy']"}))
                      .get("load_pending"), True)
        for path, heading in (("/plain", "Plain page"), ("/start", "Start")):
            result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": self.site.origin + path}))
            self.assertIn(f'heading "{heading}"', result.get("snapshot", ""), result)
            info = self.call("/api/page-info", {"tabId": tab})
            self.assertEqual((info["url"], info["readyState"]), (self.site.origin + path, "complete"), info)

    def test_a_navigation_no_document_answered_is_still_a_timeout(self):
        # The page navigated away from stays shown and loaded: its DOM is not the destination's.
        tab = self.open_tab(f"{self.site.origin}/start")
        url = f"{self.site.origin}/unanswered"
        result = self.call("/api/navigate", {"tabId": tab, "url": url})
        self.assertEqual({key: result.get(key) for key in ("ok", "timedOut", "load_pending", "url")},
                         {"ok": False, "timedOut": True, "load_pending": None, "url": url})
        self.assertNotIn("snapshot", result)
        self.assertEqual(self.call("/api/page-info", {"tabId": tab})["title"], "Start")

    def test_a_new_tab_still_loading_its_images_is_ok_with_load_pending(self):
        url = f"{self.site.origin}/heavy"
        result = self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 3000})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result.get("tabId")})
        self.assertEqual(result, {"ok": True, "load_pending": True, "tabId": result.get("tabId"), "url": url,
                                  "status": "loading"})
        self.assert_heavy_page_is_readable(result["tabId"])

    def test_navigate_and_a_waiting_new_tab_return_once_a_page_is_parsed(self):
        # #601: within the default 15 s wait, a parsed page still loading returns after a short grace.
        url = f"{self.site.origin}/heavy"
        tab = self.open_tab(f"{self.site.origin}/start")
        started = time.monotonic()
        result = self.call("/api/navigate", {"tabId": tab, "url": url})
        self.assertLess(time.monotonic() - started, 5, result)
        self.assertEqual({key: result.get(key) for key in ("ok", "load_pending", "url")},
                         {"ok": True, "load_pending": True, "url": url}, result)
        self.assertIn('heading "Listings"', result["snapshot"])
        self.assert_heavy_page_is_readable(tab)

        started = time.monotonic()
        result = self.call("/api/new-tab", {"url": url, "wait": True})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result.get("tabId")})
        self.assertLess(time.monotonic() - started, 5, result)
        self.assertEqual(result, {"ok": True, "load_pending": True, "tabId": result.get("tabId"), "url": url,
                                  "status": "loading"})
        self.assert_heavy_page_is_readable(result["tabId"])

    def test_navigate_to_an_ordinary_page_still_waits_for_its_load(self):
        tab = self.open_tab(f"{self.site.origin}/start")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/plain"}))
        self.assertNotIn("load_pending", result)
        self.assertIn('heading "Plain page"', result["snapshot"])
        self.assertEqual(self.call("/api/page-info", {"tabId": tab})["readyState"], "complete")

    def test_a_new_tab_whose_dom_is_not_parsed_is_still_a_timeout(self):
        url = f"{self.site.origin}/blocked"
        result = self.call("/api/new-tab", {"url": url, "wait": True, "timeout": 3000})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result.get("tabId")})
        self.assertEqual(result, {"ok": False, "timedOut": True, "tabId": result.get("tabId"), "url": url,
                                  "status": "loading"})


if __name__ == "__main__":
    unittest.main()
