"""#542: bounded evidence and no write replay through real stdio MCP, without inference."""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

EVIDENCE = "Delivery window: three business days."
TITLES = ["Delivery" if i == 167 else f"Topic {i}" for i in range(180)]
PAGE = ('<!doctype html><meta charset="utf-8"><title>Long reference</title>'
        '<input id="draft"><output id="writes">0</output><input id="secret" type="password" value="private-secret">'
        '<script>draft.oninput = () => writes.textContent = Number(writes.textContent) + 1;</script><nav>'
        + ''.join(f'<a href="#section-{i}">{title}</a> ' for i, title in enumerate(TITLES)) + '</nav><main>'
        + ''.join(f'<section id="section-{i}"><h2>{title}</h2><p>{"참고 자료입니다. " * 80}</p>'
                  f'<p>{EVIDENCE if i == 167 else "No delivery information in this topic."}</p></section>'
                  for i, title in enumerate(TITLES)) + '</main>')


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserLongpageTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_overflow_preserves_draft_and_scoped_recovery_finds_evidence(self):
        async def run():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "longpage-test"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()

                async def python(code, error=False):
                    reply = await session.call_tool("browser_run_python", {"code": code})
                    self.assertEqual(bool(reply.isError), error, reply)
                    return json.loads(reply.content[0].text)

                opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
                self.assertFalse(opened.isError, opened)
                tab_id = (await python("result = tab.id"))["result"]
                self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
                before = [t["id"] for t in self.call("/api/tabs")["tabs"] if t["active"]]

                stopped = await python("tab.fill('#draft', 'keep this draft')\nresult = tab.text()", error=True)
                self.assertEqual(stopped["code"], "script_output_limit")
                self.assertFalse(stopped["retryable"])
                self.assertNotIn("result", stopped)
                self.assertNotIn("resume_token", stopped)
                self.assertIn("incomplete", stopped["hint"])
                self.assertIn("Page text was cut", stopped["output"])
                self.assertEqual([op["status"] for op in stopped["operations"]], ["ok", "ok"])

                # Omitted rows must remain visible as a warning, not a page-wide "not found".
                narrow = await python("links = tab.query('nav a', fields=['text'], limit=20)\n"
                                      "result = [link for link in links if 'Delivery' in link['text']]")
                self.assertEqual(narrow["result"], [])
                self.assertIn("Query results were truncated", narrow["output"])

                # The documented local-filter flow inspects all 180 short link labels, returning
                # only the selected subtree's evidence. No full-page output, JS or filesystem escape.
                found = await python("links = tab.query('nav a[href]', fields=['text', 'href'], limit=200)\n"
                                     "matches = [link for link in links if 'delivery' in link['text'].casefold()]\n"
                                     "assert len(matches) == 1\n"
                                     "scope = '#' + matches[0]['href'].rsplit('#', 1)[1]\n"
                                     "result = {'scope': scope, 'evidence': tab.snapshot(scope, text=True)}")
                self.assertEqual(found["result"]["scope"], "#section-167")
                self.assertIn(EVIDENCE, found["result"]["evidence"])
                self.assertEqual(found["output"], "")
                self.assertLess(len(json.dumps(found, ensure_ascii=False).encode()), 6000)
                self.assertEqual([op["route"] for op in found["operations"]], ["/api/query", "/api/snapshot"])

                # Even with all matching rows returned, text snippets can omit later evidence.
                snippet = await python("result = tab.query('#section-167', fields=['text'], limit=1)")
                self.assertEqual(snippet["output"], "")
                self.assertEqual(len(snippet["result"][0]["text"]), 400)
                self.assertNotIn(EVIDENCE, snippet["result"][0]["text"])

                state = await python("result = {'draft': tab.query('#draft', fields=['value'], limit=1),\n"
                                     "          'writes': tab.query('#writes', fields=['text'], limit=1),\n"
                                     "          'secret': tab.query('#secret', fields=['value'], limit=1)}")
                self.assertEqual(state["result"]["draft"][0]["value"], "keep this draft")
                self.assertEqual(state["result"]["writes"][0]["text"], "1", "draft was not replayed")
                self.assertTrue(state["result"]["secret"][0]["hasValue"])
                self.assertNotIn("private-secret", json.dumps(state))
                after = [t["id"] for t in self.call("/api/tabs")["tabs"] if t["active"]]
                self.assertEqual(after, before)
                self.assertNotIn(tab_id, after)
        asyncio.run(asyncio.wait_for(run(), 60))
