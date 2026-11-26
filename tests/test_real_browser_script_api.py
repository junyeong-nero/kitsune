"""The scripting API gaps of #574 over a stdio MCP client and a real browser: preloaded modules,
signature help, positional query fields, coded failures, and check on ARIA checkboxes."""
import asyncio
from http.server import BaseHTTPRequestHandler
import inspect
import json
import re
import sys
import textwrap
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kitsune.browser import Browser
import real_browser_harness as harness

PAGE = """<!doctype html><title>Settings</title>
<h1>Settings</h1>
<input id=city>
<select id=sort name=sort>
  <option value=relevance>Relevance</option><option value=price_low>Price: low to high</option>
</select>
<button id=dark type=button role=checkbox aria-checked=false>Dark mode</button>
<div id=wifi role=switch aria-checked=true tabindex=0>Wi-Fi</div>
<button id=saved type=button role=checkbox aria-checked=false>Saved by the server</button>
<button id=stuck type=button role=checkbox aria-checked=false>Stuck</button>
<a id=stock role=checkbox aria-checked=false href="/filtered">In stock</a>
<button id=jump type=button role=switch aria-checked=false>Compact view</button>
<form id=news action="/done" method=get>
  <button id=subscribe role=checkbox aria-checked=false>Subscribe</button>
  <label><input type=checkbox id=agree name=agree> Agree</label>
</form>
<table><tr><td>Row 1</td></tr><tr><td>Row 2</td></tr></table>
<script>
const flip = el => el.setAttribute('aria-checked', el.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
for (const id of ['dark', 'wifi', 'subscribe']) {
  document.getElementById(id).addEventListener('click', event => flip(event.currentTarget));
}
// Publishes its state once the server has answered, as a control without optimistic rendering.
document.getElementById('saved').addEventListener('click', event => {
  const el = event.currentTarget;
  fetch('/save').then(() => flip(el));
});
document.getElementById('jump').addEventListener('click', () => { location.href = '/filtered'; });
</script>
"""
FILTERED = """<!doctype html><title>Filtered</title>
<a id=stock role=checkbox aria-checked=true href="/">In stock</a><p>3 results</p>"""
# Longer than the check's wait for aria-checked, so only the unload can end that wait in time.
SLOW_LOAD_SECONDS = 2


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/filtered"):
            time.sleep(SLOW_LOAD_SECONDS)
        if self.path.startswith("/save"):
            time.sleep(0.3)
        page = FILTERED if self.path.startswith("/filtered") else "<title>Done</title>" if self.path.startswith(
            "/done") else "saved" if self.path.startswith("/save") else PAGE
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserScriptApiTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def run_scenario(self, run):
        async def scenario():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "script-api"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
                self.assertFalse(opened.isError, opened)
                tab_id = (await self.script(session, "result = tab.id"))["result"]
                self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
                await run(session, tab_id)
        asyncio.run(asyncio.wait_for(scenario(), 90))

    async def script(self, session, code, failed=False):
        reply = await session.call_tool("browser_run_python", {"code": code})
        self.assertEqual(reply.isError, failed, reply)
        return json.loads(reply.content[0].text)

    def checked(self, tab_id, selector):
        html = self.call("/api/html", {"tabId": tab_id, "selector": selector})["html"]
        return re.search(r'aria-checked="(\w+)"', html).group(1)

    def test_a_script_sleeps_checks_an_aria_checkbox_by_its_ref_and_queries_positionally(self):
        async def run(session, tab_id):
            # The script of wap-069 and wap-231: no import, and the ref the snapshot calls a checkbox.
            code = ("import re as regex\n"
                    "tab.fill('#city', 'Vancouver'); time.sleep(0.2)\n"
                    "line = next(l for l in tab.snapshot().splitlines() if 'checkbox \"Dark mode\"' in l)\n"
                    "ref = regex.search(r'ref=(e\\d+)', line).group(1)\n"
                    "result = {'line': line, 'check': tab.check(ref, True), 'again': tab.check(ref, True),\n"
                    "          'off': tab.check('#wifi', False), 'saved': tab.check('#saved', True),\n"
                    "          'rows': tab.query('tr', ['text'], limit=30), 'dump': json.dumps({'a': 1})}")
            result = (await self.script(session, code))["result"]
            self.assertIn("checked=false", result["line"])
            check = result["check"]
            self.assertEqual((check["ok"], check["role"], check["checked"], check["clicked"]),
                             (True, "checkbox", True, True), check)
            self.assertEqual((result["again"]["checked"], result["again"]["clicked"]), (True, False))
            self.assertEqual((result["off"]["role"], result["off"]["checked"], result["off"]["clicked"]),
                             ("switch", False, True))
            self.assertEqual((result["saved"]["checked"], result["saved"]["clicked"]), (True, True),
                             "a state published after a request is waited for")
            self.assertEqual([row["text"] for row in result["rows"]], ["Row 1", "Row 2"])
            self.assertEqual(result["dump"], '{"a": 1}')
            self.assertEqual([self.checked(tab_id, selector) for selector in ("#dark", "#wifi", "#saved")],
                             ["true", "false", "true"])
        self.run_scenario(run)

    def test_failures_are_coded_and_a_wrong_argument_prints_the_signature(self):
        async def run(session, tab_id):
            stopped = await self.script(session, "tab.select_option('#sort', 'price_asc')", failed=True)
            failure = stopped["failed_operation"]
            self.assertEqual((failure["code"], failure["retryable"]), ("no_such_option", False), failure)
            self.assertEqual((failure["state"]["total"], failure["state"]["options"]), (2, [
                {"value": "relevance", "text": "Relevance"}, {"value": "price_low", "text": "Price: low to high"}]))
            self.assertIn("state.options", failure["hint"])
            label = await self.script(session, "tab.select_option('#sort', 'Relevance')", failed=True)
            self.assertIn("by_text=True", label["failed_operation"]["hint"])
            self.assertEqual(self.call("/api/forms", {"tabId": tab_id})["fields"][1]["value"], "relevance")

            stopped = await self.script(session, "tab.check('#city', True)", failed=True)
            failure = stopped["failed_operation"]
            self.assertEqual((failure["code"], failure["retryable"]), ("wrong_element_type", False), failure)
            self.assertEqual((failure["state"]["tag"], failure["state"]["role"]), ("input", "textbox"))
            self.assertIn("browser_click", failure["hint"])

            # A control that ignores the click: one click was dispatched, and the result says so.
            stopped = await self.script(session, "tab.check('#stuck', True)", failed=True)
            failure = stopped["failed_operation"]
            self.assertEqual((failure["code"], failure["retryable"]), ("value_not_reached", False), failure)
            self.assertEqual({key: failure["state"][key] for key in ("role", "checked", "target", "clicked", "reason")},
                             {"role": "checkbox", "checked": "false", "target": True, "clicked": True,
                              "reason": "unchanged"})
            self.assertIn("The click was dispatched", failure["hint"])

            stopped = await self.script(session, "tab.wait_for(text='Never shown', timeout=300)", failed=True)
            failure = stopped["failed_operation"]
            self.assertEqual((failure["ok"], failure["code"], failure["retryable"]), (False, "timeout", True), failure)
            self.assertEqual(failure["error"], "Timed out after 300ms waiting for visible text")
            self.assertTrue(failure["hint"])

            for call, signature in (("tab.select_option('#sort', label='Relevance')", "Tab.select_option(self, target"),
                                    ("tab.query('tr', ['text'], 30)", "Tab.query(self, selector: str, fields")):
                stopped = await self.script(session, f"tab.fill('#city', 'kept')\n{call}", failed=True)
                self.assertEqual(stopped["code"], "script_error")
                self.assertIn("Python API: " + signature, stopped["output"])
                self.assertEqual([entry["status"] for entry in stopped["operations"]], ["ok"])
        self.run_scenario(run)

    def test_a_check_whose_click_loads_another_page_answers_as_the_click(self):
        # The new page takes SLOW_LOAD_SECONDS, longer than the wait for aria-checked: the old
        # document's unload ends that wait, and the result reports the page the click loaded.
        for selector in ("#stock", "#jump"):
            with self.subTest(selector=selector):
                tab_id = self.open_tab(self.site.origin)
                checked = self.assert_ok(self.call("/api/check", {"tabId": tab_id, "selector": selector,
                                                                   "checked": True}))
                self.assertNotIn("code", checked)
                self.assertEqual(checked["clicked"], True)
                self.assertNotIn("checked", checked, "the old document published no state")
                self.assertEqual(checked["navigated"]["url"], self.site.origin + "/filtered", checked)
                self.assertEqual(checked["navigated"]["title"], "Filtered")
                clicked_tab = self.open_tab(self.site.origin)
                clicked = self.assert_ok(self.call("/api/click", {"tabId": clicked_tab, "selector": selector}))
                self.assertEqual(checked["navigated"], clicked["navigated"], "as a click on the same element")

    def test_ask_before_act_holds_a_check_that_would_submit_like_a_click(self):
        tab_id = self.open_tab(self.site.origin)
        self.call("/api/set-policy", {"ask": True})
        self.addCleanup(self.call, "/api/set-policy", {"ask": False})
        # A <button role=checkbox> without type=button in a form is its submit control.
        headers = {**self.bridge.auth(), "Content-Type": "application/json"}
        for path, body in (("/api/check", {"selector": "#subscribe", "checked": True}),
                           ("/api/click", {"selector": "#subscribe"})):
            with self.subTest(path=path):
                status, _, text = harness._request(self.bridge.http_port, "POST", path, headers, self.bridge.host,
                                                   json.dumps({"tabId": tab_id, **body}))
                held = json.loads(text)
                self.assertEqual(status, 403, held)
                self.assert_needs_approval(held, "confirmation_required")
                self.assertEqual(held["state"]["holdReason"], "submit_control")
                self.assertIn("clicks a submit control (Subscribe)", held["error"])
        self.assertEqual(self.checked(tab_id, "#subscribe"), "false")
        self.assertEqual(self.call("/api/page-info", {"tabId": tab_id})["title"], "Settings")
        # A native checkbox and a plain ARIA checkbox are not held.
        self.assertEqual(self.assert_ok(self.call("/api/check", {"tabId": tab_id, "selector": "#agree",
                                                                  "checked": True}))["checked"], True)
        dark = self.assert_ok(self.call("/api/check", {"tabId": tab_id, "selector": "#dark", "checked": True}))
        self.assertEqual((dark["checked"], dark["clicked"]), (True, True))
        self.assertEqual(self.checked(tab_id, "#dark"), "true")

    def test_the_parallel_help_example_reads_24_pages_through_4_tabs_as_written(self):
        # #669: wap-247 needed six calls to learn browser.parallel's action names and result shape.
        example = textwrap.dedent(re.search(r"\n\n((?: {4}.*\n)+)", inspect.getdoc(Browser.parallel))[1])

        async def run(session, tab_id):
            code = example.replace("https://example.com", self.site.origin) + "result = [t.id for t in tabs]"
            reply = await self.script(session, code)
            for opened in reply["result"]:
                self.addCleanup(self.call, "/api/close-tab", {"tabId": opened})
            printed = re.findall(rf"^({re.escape(self.site.origin)}\S*) (.*)$", reply["output"], re.M)
            self.assertEqual(printed, [(f"{self.site.origin}/?page={n}", "Settings") for n in range(24)], reply)

            # A failed step does not raise, and the rest of its sequence runs.
            failed = await self.script(session, (
                f"t = browser.tab({reply['result'][0]})\n"
                "r = browser.parallel([[{'action': 'click', 'selector': '#missing', 'tabId': t.id, 'timeout': 300},\n"
                "                       {'action': 'text', 'tabId': t.id}]])\n"
                "result = [r['results'], r['status']]"))
            [(click, read)], status = failed["result"]
            self.assertTrue(click["error"] and click["code"], click)
            self.assertTrue(read["text"].startswith("Settings"), read)
            self.assertNotEqual(status, "ok")
        self.run_scenario(run)


if __name__ == "__main__":
    unittest.main()
