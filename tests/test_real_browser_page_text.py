"""A script reads a long page's whole text, in a real browser (issue #630).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
A script's reads are not model-visible, only what it prints, so tab.text() is not cut at the
direct tool's 20,000 characters. The direct page-text result stays bounded.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

# 200 fares of about 310 characters each: 60,000 characters of body text, as on wap-069's result pages.
ROWS = 200
PAGE = "<!doctype html><title>Fares</title>" + "".join(
    f"<p>Fare {i:03d} ${100 + i}\nfrom {'x' * 290}</p>" for i in range(ROWS))


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


class RealBrowserPageTextTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_a_script_reads_a_long_page_whole_and_the_direct_read_stays_bounded(self):
        code = ("import re, warnings\n"
                "with warnings.catch_warnings(record=True) as caught:\n"
                "    warnings.simplefilter('always')\n"
                "    text = tab.text()\n"
                "result = {'length': len(text), 'fares': len(re.findall(r'Fare \\d{3}', text)),\n"
                "          'warnings': [str(w.message) for w in caught]}")

        async def scenario():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "page-text"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                opened = await session.call_tool("browser_tabs", {"action": "new", "url": f"{self.site.origin}/"})
                self.assertFalse(opened.isError, opened)
                script = await session.call_tool("browser_run_python", {"code": code, "timeout": 60})
                self.assertFalse(script.isError, script)
                tab = await session.call_tool("browser_run_python", {"code": "result = tab.id"})
                return json.loads(tab.content[0].text)["result"], json.loads(script.content[0].text)["result"]

        tab, result = asyncio.run(asyncio.wait_for(scenario(), 90))
        self.addCleanup(self.call, "/api/close-tab", {"tabId": tab})
        self.assertEqual(result["fares"], ROWS, result)
        self.assertGreater(result["length"], 60000)
        self.assertEqual(result["warnings"], [])

        direct = self.call("/api/page-text", {"tabId": tab})
        self.assertEqual((len(direct["text"]), direct.get("truncated")), (20000, True))
        self.assertEqual(direct["fullLength"], result["length"])


if __name__ == "__main__":
    unittest.main()
