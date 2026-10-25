"""A contenteditable editor through the core tools in a real browser (#255, #256).

Opt-in like every real-browser suite; see tests/real_browser_harness.py. The fixture is
shaped like Papago's source editor: a presentation wrapper around an editing host whose
line is a div holding a span and a br, and an output the page fills from input events.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import re
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>translate</title>
<div role="presentation" aria-label="Type the text to translate">
  <div id="source" contenteditable="true"><div><span>이전 문장</span><br></div></div>
</div>
<p id="output"></p>
<script>
  const source = document.getElementById('source');
  const output = document.getElementById('output');
  let inputs = 0;
  source.addEventListener('input', () => {
    document.body.dataset.inputs = ++inputs;
    const text = source.innerText.trim();
    setTimeout(() => { output.textContent = text ? 'translated: ' + text : ''; }, 50);
  });
</script>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserEditorTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        playwright_tools._current_tab = None

    def call_tool(self, name, arguments=None):
        """(isError, JSON result) of a core tool call through FastMCP, against this class's bridge."""
        with patch.object(server, "_get", self.call), \
                patch.object(server, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = asyncio.run(server.mcp.call_tool(name, arguments or {}))
        return result.isError, json.loads(result.content[0].text)

    def tool(self, name, arguments=None):
        is_error, result = self.call_tool(name, arguments)
        self.assertFalse(is_error, (name, result))
        return result

    def editor_state(self):
        return self.tool("browser_evaluate", {"function": (
            "() => ({ text: document.getElementById('source').innerText.trim(), "
            "inputs: Number(document.body.dataset.inputs || 0) })")})["result"]

    def test_fill_through_the_editor_textbox_ref(self):
        result = self.tool("browser_tabs", {"action": "new", "url": self.site.origin + "/"})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": playwright_tools._current_tab})
        self.assertTrue(result["tabs"][result["index"]]["current"], result)

        # One textbox for the editor; the line div, span and br are not textboxes (#256).
        snapshot = self.tool("browser_snapshot", {})["snapshot"]
        textboxes = re.findall(r'textbox "[^"]*" \[ref=(e\d+)\]', snapshot)
        self.assertEqual(len(textboxes), 1, snapshot)
        ref = textboxes[0]

        # The default (fill) path writes the editor, and the page reacts to it (#255).
        text = "하카타역으로 가는 지하철은 어디에서 타나요?"
        self.tool("browser_type", {"target": ref, "text": text})
        state = self.editor_state()
        self.assertEqual(state["text"], text)
        self.assertGreater(state["inputs"], 0, "the page saw input events")
        self.tool("browser_wait_for", {"text": "translated: " + text})

        # Filling again through the same ref replaces the content instead of appending.
        self.tool("browser_type", {"target": ref, "text": "다시"})
        self.assertEqual(self.editor_state()["text"], "다시")
        self.tool("browser_wait_for", {"text": "translated: 다시"})


if __name__ == "__main__":
    unittest.main()
