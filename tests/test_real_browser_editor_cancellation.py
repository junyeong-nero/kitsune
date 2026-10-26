"""An editor's beforeinput cancellation is reported, not claimed as an edit, in a real browser (#289).

Opt-in like every real-browser suite; see tests/real_browser_harness.py. The fixture is the
issue's: a contenteditable editor whose page-world beforeinput listener cancels every edit
while data-cancel is set. The page counts its beforeinput (by inputType) and input events,
and each case reads back the editor's text. Non-cancelling and unchanged-value controls
keep a cancellation apart from an edit that applied or changed nothing.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Cancel an edit</title>
<div id="editor" contenteditable="true" aria-label="Editor">original</div>
<script>
  // The counts are in body's dataset, which browser_evaluate reads.
  const editor = document.getElementById('editor');
  const counts = document.body.dataset;
  editor.addEventListener('beforeinput', event => {
    counts.beforeinput = JSON.stringify([...JSON.parse(counts.beforeinput || '[]'), event.inputType]);
    if (editor.dataset.cancel) event.preventDefault();
  });
  editor.addEventListener('input', () => { counts.input = Number(counts.input || 0) + 1; });
</script>"""

# Sets the editor's text and whether the page cancels edits, clears the event counts, and
# focuses it with the caret at its end (as tests/test_real_browser_key_defaults.py does).
RESET = """(text, cancel) => {
  const editor = document.getElementById('editor');
  editor.textContent = text;
  if (cancel) editor.dataset.cancel = '1'; else delete editor.dataset.cancel;
  editor.focus();
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  getSelection().removeAllRanges();
  getSelection().addRange(range);
  delete document.body.dataset.beforeinput;
  delete document.body.dataset.input;
}"""

STATE = """() => ({ text: document.getElementById('editor').textContent,
  beforeinput: JSON.parse(document.body.dataset.beforeinput || '[]'), input: Number(document.body.dataset.input || 0) })"""


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


class RealBrowserEditorCancellationTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        playwright_tools._current_tab = None
        result = self.tool("browser_tabs", {"action": "new", "url": self.site.origin + "/"})
        self.addCleanup(self.call, "/api/close-tab", {"tabId": playwright_tools._current_tab})
        self.assertTrue(result["tabs"][result["index"]]["current"], result)

    def tool(self, name, arguments=None):
        """A core tool call's JSON result through FastMCP, against this class's bridge."""
        with patch.object(tools, "_get", self.call), \
                patch.object(tools, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = asyncio.run(server.mcp.call_tool(name, arguments or {}))
        content = json.loads(result.content[0].text)
        self.assertFalse(result.isError, (name, content))
        return content

    def act(self, text, cancel, name, arguments):
        """The tool's result and the editor's state after it, from text with cancel set or not."""
        self.tool("browser_evaluate", {"function": f"() => ({RESET})({json.dumps(text)}, {json.dumps(cancel)})"})
        result = self.tool(name, arguments)
        return result, self.tool("browser_evaluate", {"function": STATE})["result"]

    def test_a_cancelled_edit_is_reported_as_prevented(self):
        cases = {
            "replacement": ("browser_type", {"target": "#editor", "text": "replacement"}, "insertText"),
            "clearing": ("browser_type", {"target": "#editor", "text": ""}, "deleteContentBackward"),
            "typing": ("browser_type", {"target": "#editor", "text": "typed", "slowly": True}, "insertText"),
            "character": ("browser_press_key", {"key": "x"}, "insertText"),
            "backspace": ("browser_press_key", {"key": "Backspace"}, "deleteContentBackward"),
        }
        for case, (name, arguments, input_type) in cases.items():
            with self.subTest(case=case):
                result, state = self.act("original", True, name, arguments)
                self.assertEqual(state, {"text": "original", "beforeinput": [input_type], "input": 0},
                                 "the page's cancellation holds, and the edit is not repeated")
                self.assertIs(result.get("prevented"), True, result)
                self.assertTrue(result["ok"], result)
                if name == "browser_press_key":
                    self.assertIsNone(result.get("effect"), result)

    def test_an_applied_edit_is_not_prevented(self):
        cases = {
            "replacement": ("browser_type", {"target": "#editor", "text": "replacement"}, "replacement", None),
            "clearing": ("browser_type", {"target": "#editor", "text": ""}, "", None),
            "typing": ("browser_type", {"target": "#editor", "text": " typed", "slowly": True}, "original typed",
                       None),
            "character": ("browser_press_key", {"key": "x"}, "originalx", "insertText"),
            "backspace": ("browser_press_key", {"key": "Backspace"}, "origina", "deleteContentBackward"),
        }
        for case, (name, arguments, text, effect) in cases.items():
            with self.subTest(case=case):
                result, state = self.act("original", False, name, arguments)
                self.assertEqual(state["text"].replace("\u00a0", " "), text)
                self.assertEqual(len(state["beforeinput"]), 1, state)
                self.assertGreater(state["input"], 0, state)
                self.assertNotIn("prevented", result)
                if name == "browser_press_key":
                    self.assertEqual(result.get("effect"), effect, result)

    def test_an_edit_to_the_same_content_is_not_prevented(self):
        # Filling the editor with its own text still replaces it, and clearing an empty
        # editor changes nothing; neither was cancelled.
        result, state = self.act("original", False, "browser_type", {"target": "#editor", "text": "original"})
        self.assertEqual(state["text"], "original")
        self.assertEqual(state["beforeinput"], ["insertText"])
        self.assertNotIn("prevented", result)

        result, state = self.act("", False, "browser_type", {"target": "#editor", "text": ""})
        self.assertEqual(state["text"], "")
        self.assertEqual(state["input"], 0, state)
        self.assertNotIn("prevented", result)

        # Backspace at the start of an empty editor has no effect, and is not prevented.
        result, state = self.act("", False, "browser_press_key", {"key": "Backspace"})
        self.assertEqual(state["text"], "")
        self.assertIsNone(result.get("effect"), result)
        self.assertNotIn("prevented", result)


if __name__ == "__main__":
    unittest.main()
