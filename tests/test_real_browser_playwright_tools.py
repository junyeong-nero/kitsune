"""The core tools (Playwright MCP's) against a real headless browser, through FastMCP validation.

Opt-in like every real-browser suite; see tests/real_browser_harness.py.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>pw tools</title>
<input id="name" aria-label="Name">
<select id="plans" aria-label="Plans" multiple>
  <option value="free">Free</option><option value="pro">Pro</option><option value="team">Team</option>
</select>
<select id="plan" aria-label="Plan"><option value="free">Free</option><option value="pro">Pro</option></select>
<input type="checkbox" id="agree" aria-label="Agree">
<button id="go">Go</button>
<div id="zone" style="width:200px;height:60px">Drop here</div>
<input type="file" id="file" aria-label="File">
<p id="out">idle</p>
<script>
  const out = document.getElementById('out');
  const go = document.getElementById('go');
  go.addEventListener('click', e => { out.textContent = 'clicked shift=' + e.shiftKey; console.warn('go clicked'); });
  go.addEventListener('contextmenu', e => { e.preventDefault(); out.textContent = 'context menu'; });
  go.addEventListener('dblclick', () => { out.textContent = 'double clicked'; });
  const zone = document.getElementById('zone');
  zone.addEventListener('dragover', e => e.preventDefault());
  zone.addEventListener('drop', e => { e.preventDefault(); out.textContent = 'dropped ' + e.dataTransfer.getData('text/plain'); });
  document.getElementById('file').addEventListener('change', e => { out.textContent = 'file ' + e.target.files[0].name; });
  document.getElementById('name').addEventListener('keydown', e => {
    if (e.key === 'Enter') out.textContent = 'entered ' + e.target.value;
  });
  fetch('/api/data');
</script>"""
# Loads with an empty body; its results arrive once the test releases /api/results (#259).
ASYNC_PAGE = """<!doctype html><meta charset="utf-8"><title>async results</title><body><script>
  fetch('/api/results').then(r => r.json()).then(data => {
    const p = document.createElement('p');
    p.textContent = data.found;
    document.body.append(p);
  });
</script>"""
# A completed HTTP 200 document that is genuinely empty.
EMPTY_PAGE = '<!doctype html><meta charset="utf-8"><title>empty</title><body></body>'
results_released = threading.Event()


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/results":
            results_released.wait(30)
        body = (b'{"found":"3 hotels found"}' if self.path == "/api/results" else
                b'{"ok":true}' if self.path.startswith("/api/") else
                f"<title>second</title><p>second page</p>".encode() if self.path == "/second" else
                ASYNC_PAGE.encode() if self.path == "/async" else
                EMPTY_PAGE.encode() if self.path == "/empty" else PAGE.encode())
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/api/") else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserPlaywrightToolTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        results_released.clear()
        self.addCleanup(results_released.set)
        playwright_tools._current_tab = None
        playwright_tools._clicked.clear()
        playwright_tools._requests.clear()

    def call_tool(self, name, arguments=None):
        """(isError, content) of a core tool call through FastMCP, against this class's bridge."""
        with patch.object(server, "_get", self.call), \
                patch.object(server, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = asyncio.run(server.mcp.call_tool(name, arguments or {}))
        if isinstance(result, list):  # image results are a content list
            return False, result
        return result.isError, result.content

    def tool(self, name, arguments=None):
        """A core tool call's JSON result; fails on an MCP error."""
        is_error, content = self.call_tool(name, arguments)
        result = json.loads(content[0].text)
        self.assertFalse(is_error, (name, result))
        return result

    def open_current(self, path="/"):
        result = self.tool("browser_tabs", {"action": "new", "url": self.site.origin + path})
        self.assertTrue(result["tabs"][result["index"]]["current"], result)
        tab = playwright_tools._current_tab
        self.addCleanup(self.call, "/api/close-tab", {"tabId": tab})
        return tab

    def text(self, expected):
        self.tool("browser_wait_for", {"text": expected})

    def ref(self, snapshot, pattern):
        return re.search(pattern + r' \[ref=(e\d+)\]', snapshot).group(1)

    def test_tabs_snapshot_and_find_use_the_current_tab(self):
        tab = self.open_current()
        listed = self.tool("browser_tabs", {"action": "list"})["tabs"]
        current = [entry for entry in listed if entry.get("current")]
        self.assertEqual([entry["title"] for entry in current], ["pw tools"], listed)
        self.assertTrue(current[0]["owned"])

        snapshot = self.tool("browser_snapshot", {})["snapshot"]
        self.assertRegex(snapshot, r'button "Go" \[ref=e\d+\]')
        boxed = self.tool("browser_snapshot", {"boxes": True})["snapshot"]
        self.assertRegex(boxed, r'(?m)button "Go" \[ref=e\d+\]( \[[^\]]*\])? \[box=\d+,\d+,\d+,\d+\]$')
        found = self.tool("browser_find", {"text": "go"})
        self.assertTrue(any('button "Go"' in match["match"] for match in found["matches"]), found)

        self.tool("browser_navigate", {"url": self.site.origin + "/second"})
        self.assertEqual(self.tool("browser_evaluate", {"function": "() => document.title"})["result"], "second")
        self.tool("browser_navigate_back", {})
        self.text("idle")
        self.assertEqual(self.call("/api/page-info", {"tabId": tab})["title"], "pw tools")

    def test_snapshots_report_readiness_and_empty_content(self):
        self.open_current()
        normal = self.tool("browser_snapshot", {})
        self.assertEqual(normal["readyState"], "complete")
        self.assertNotIn("empty", normal)
        self.assertNotIn("hint", normal)

        # Loaded but still waiting for its results: empty, including the saved file.
        self.tool("browser_navigate", {"url": self.site.origin + "/async"})
        waiting = self.tool("browser_snapshot", {})
        self.assertEqual((waiting["snapshot"], waiting["readyState"], waiting["empty"]), ("", "complete", True))
        self.assertIn("browser_wait_for", waiting["hint"])
        with tempfile.TemporaryDirectory() as directory:
            saved = self.tool("browser_snapshot", {"filename": str(Path(directory, "async.md"))})
            self.assertEqual(Path(saved["saved_to"]).read_text(), "")
            self.assertTrue(saved["ok"])
            self.assertTrue(saved["empty"])
            self.assertEqual(saved["readyState"], "complete")
        results_released.set()
        self.text("3 hotels found")
        ready = self.tool("browser_snapshot", {})
        self.assertIn("3 hotels found", ready["snapshot"])
        self.assertNotIn("empty", ready)

        # A completed empty 200 page is the same observation, not an error.
        self.tool("browser_navigate", {"url": self.site.origin + "/empty"})
        empty = self.tool("browser_snapshot", {})
        self.assertEqual((empty["snapshot"], empty["title"], empty["empty"]), ("", "empty", True))
        self.assertEqual(self.call("/api/page-info", {"tabId": playwright_tools._current_tab})["readyState"],
                         "complete")

    def test_pointer_keyboard_and_form_tools(self):
        self.open_current()
        snapshot = self.tool("browser_snapshot", {})["snapshot"]
        name = self.ref(snapshot, r'textbox "Name"')

        self.tool("browser_type", {"target": name, "text": "hello", "submit": True})
        self.text("entered hello")
        self.tool("browser_click", {"target": "#go", "modifiers": ["Shift"]})
        self.text("clicked shift=true")
        self.tool("browser_click", {"target": "#go", "button": "right"})
        self.text("context menu")
        self.tool("browser_click", {"target": "#go", "doubleClick": True})
        self.text("double clicked")
        self.tool("browser_press_key", {"key": "Escape"})

        self.tool("browser_select_option", {"target": "#plans", "values": ["pro", "Team"]})
        filled = self.tool("browser_fill_form", {"fields": [
            {"target": "#agree", "name": "Agree", "type": "checkbox", "value": "true"},
            {"target": "#plan", "name": "Plan", "type": "combobox", "value": "Pro"}]})
        self.assertEqual(filled["filled"], ["Agree", "Plan"])
        state = self.tool("browser_evaluate", {"function": (
            "() => [Array.from(document.querySelector('#plans').selectedOptions).map(o => o.value), "
            "document.querySelector('#plan').value, document.querySelector('#agree').checked]")})
        self.assertEqual(state["result"], [["pro", "team"], "pro", True])
        self.assertEqual(self.tool("browser_evaluate", {"function": "(el) => el.id", "target": "#go"})["result"], "go")

        self.tool("browser_drop", {"target": "#zone", "data": {"text/plain": "fox"}})
        self.text("dropped fox")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "note.txt")
            path.write_text("kitsune")
            self.tool("browser_click", {"target": "#file"})
            self.tool("browser_file_upload", {"paths": [str(path)]})
        self.text("file note.txt")

    def test_network_console_screenshot_resize_and_close(self):
        self.open_current()
        requests = self.tool("browser_network_requests", {"static": False, "filter": "/api/data"})
        self.assertEqual([entry["index"] for entry in requests["requests"]], [1], requests)
        self.assertTrue(self.tool("browser_network_request", {"index": 1})["url"].endswith("/api/data"))
        is_error, content = self.call_tool("browser_network_request", {"index": 1, "part": "response-body"})
        self.assertTrue(is_error)
        self.assertEqual(json.loads(content[0].text)["code"], "not_supported")

        self.tool("browser_console_messages", {"level": "info"})  # starts capture
        self.tool("browser_click", {"target": "#go"})
        self.text("clicked shift=false")
        messages = self.tool("browser_console_messages", {"level": "warning"})
        self.assertIn("go clicked", [log["message"] for log in messages["logs"]], messages)

        is_error, content = self.call_tool("browser_take_screenshot", {"scale": "css"})
        self.assertFalse(is_error)
        self.assertEqual(content[0].type, "image")
        with tempfile.TemporaryDirectory() as directory:
            saved = self.tool("browser_take_screenshot", {"filename": str(Path(directory, "page.png"))})
            self.assertTrue(Path(saved["saved_to"]).read_bytes().startswith(b"\x89PNG"))

        self.assertTrue(self.tool("browser_resize", {"width": 900, "height": 700})["ok"])
        for name in ("browser_emulate_media", "browser_run_code_unsafe"):
            is_error, content = self.call_tool(name, {})
            self.assertTrue(is_error)
            self.assertEqual(json.loads(content[0].text)["code"], "not_supported")

        tab = playwright_tools._current_tab
        self.tool("browser_close", {})
        self.assertIsNone(playwright_tools._current_tab)
        self.assertNotIn(tab, [entry["id"] for entry in self.call("/api/tabs")["tabs"]])


if __name__ == "__main__":
    unittest.main()
