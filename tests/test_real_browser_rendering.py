"""navigate and a waiting new tab say when a loaded page still renders, in a real browser (issue #631).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Since #601 both return once the DOM is parsed, so a client-rendered page may show nothing yet or
a "Loading..." placeholder. The result says so with rendering, and the wait it names finds the content.
In a script the warning comes at the first read after the navigation, unless a wait_for comes first (#651).
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import unittest

from kitsune import script_runner
import real_browser_harness as harness

PAGES = {
    # Like PipeCRM's detail pages: a placeholder until a client fetch returns.
    "/loading": """<!doctype html><title>Deal</title><nav>PipeCRM Deals Contacts</nav>
<div id="root"><div>Loading...</div></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Radiant Solar</h1><p>OPEN</p>' }, 2500)</script>""",
    # Like a React SPA: an empty root until its bundle renders.
    "/empty": """<!doctype html><title>Flights</title><div id="root"></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Flight search</h1>' }, 2500)</script>""",
    # #651: PipeCRM's deal page, rendered 800 ms after its DOM.
    "/deal": """<!doctype html><title>Deal</title><div id="root"><div>Loading...</div></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Deal Info</h1><p>$97,700</p>' }, 800)</script>""",
    # #652: the flight-search start page, empty for 800 ms.
    "/flights": """<!doctype html><title>Flights</title><div id="root"></div>
<script>setTimeout(() => { root.innerHTML = '<h1>Flights</h1><p>Explore the world</p>' }, 800)</script>""",
    "/busy": """<!doctype html><title>Vault</title><h1>Vault</h1><section aria-busy="true">Items</section>""",
    "/plain": "<!doctype html><title>Plain</title><h1>Plain page</h1>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path)
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

    def log_message(self, *args):
        pass


class _SuiteBridge:
    """The script host's bridge client (script_runner.run_python), over the suite's own bridge."""

    def __init__(self, call):
        self.call = call

    def request(self, method, route, *, json=None, **_):
        return self.call(route, json)


class RealBrowserRenderingTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_navigate_to_a_page_showing_loading_says_so_and_its_wait_finds_the_content(self):
        tab = self.open_tab(f"{self.site.origin}/plain")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/loading"}))
        self.assertEqual((result.get("rendering"), result.get("placeholder")), (True, "Loading..."), result)
        self.assertIn('text_gone="Loading..."', result["rendering_hint"])
        waited = self.call("/api/wait-for-text", {"tabId": tab, "text_gone": "Loading...", "timeout": 10000})
        self.assertIs(waited.get("ok"), True, waited)
        self.assertIn("Radiant Solar", self.call("/api/page-text", {"tabId": tab})["text"])

    def test_a_new_tab_on_an_empty_root_says_so(self):
        result = self.call("/api/new-tab", {"url": f"{self.site.origin}/empty", "wait": True})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": result.get("tabId")})
        self.assertIs(result.get("rendering"), True, result)
        self.assertNotIn("placeholder", result)
        self.assertIn("browser_wait_for(content=true)", result["rendering_hint"])
        # #652: the wait it names needs no text of the page.
        waited = self.call("/api/wait-for-text", {"tabId": result["tabId"], "content": True, "timeout": 10000})
        self.assertEqual((waited.get("ok"), waited.get("content")), (True, True), waited)
        self.assertIn("Flight search", self.call("/api/page-text", {"tabId": result["tabId"]})["text"])

    def test_an_aria_busy_section_says_so(self):
        tab = self.open_tab(f"{self.site.origin}/plain")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/busy"}))
        self.assertIs(result.get("rendering"), True, result)

    def test_a_rendered_page_and_a_blank_tab_add_nothing(self):
        tab = self.open_tab(f"{self.site.origin}/loading")
        result = self.assert_ok(self.call("/api/navigate", {"tabId": tab, "url": f"{self.site.origin}/plain"}))
        self.assertNotIn("rendering", result)
        blank = self.call("/api/new-tab", {"url": "about:blank", "wait": True})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": blank.get("tabId")})
        self.assertNotIn("rendering", blank)

    def run_script(self, tab, lines):
        code = ("import warnings\n"
                "with warnings.catch_warnings(record=True) as caught:\n"
                "    warnings.simplefilter('always')\n" + "".join(f"    {line}\n" for line in lines) +
                "result = {'text': text, 'warnings': [[str(w.message), w.lineno] for w in caught]}")
        done = asyncio.run(script_runner.run_python(code, _SuiteBridge(self.call), tab_id=tab, timeout=60))
        self.assertNotIn("failed_operation", done, done)
        return done["result"]

    def test_a_script_is_warned_at_its_first_read_and_not_after_a_wait(self):
        url = f"{self.site.origin}/deal"
        tab = self.open_tab(f"{self.site.origin}/plain")
        waited = self.run_script(tab, [f"tab.navigate({url!r})", "tab.wait_for(text_gone='Loading...')",
                                       "text = tab.text()"])
        self.assertEqual(waited, {"text": "Deal Info\n\n$97,700", "warnings": []})

        read = self.run_script(tab, [f"tab.navigate({url!r})", "text = tab.text()", "tab.text()"])
        self.assertEqual(read["text"], "Loading...")
        self.assertEqual(len(read["warnings"]), 1, read)
        message, line = read["warnings"][0]
        self.assertIn("tab.wait_for(text_gone='Loading...')", message)
        self.assertEqual(line, 5, "the warning names the text() line")

    def test_a_script_waits_for_content_it_knows_no_text_of(self):
        url = f"{self.site.origin}/flights"
        tab = self.open_tab(f"{self.site.origin}/plain")
        read = self.run_script(tab, [f"tab.navigate({url!r})", "text = tab.text()"])
        self.assertEqual(read["text"], "")
        self.assertEqual(len(read["warnings"]), 1, read)
        self.assertIn("wait with tab.wait_for(content=True)", read["warnings"][0][0])
        waited = self.run_script(tab, [f"tab.navigate({url!r})", "tab.wait_for(content=True)", "text = tab.text()"])
        self.assertEqual(waited, {"text": "Flights\n\nExplore the world", "warnings": []})
        # A page that keeps rendering ends the wait at its timeout.
        busy = self.run_script(tab, [f"tab.navigate({self.site.origin + '/busy'!r})",
                                     "try:\n        tab.wait_for(content=True, timeout=500)\n        text = 'met'\n"
                                     "    except TimeoutError as error:\n        text = str(error)"])
        self.assertIn("waiting for page content", busy["text"])


if __name__ == "__main__":
    unittest.main()
