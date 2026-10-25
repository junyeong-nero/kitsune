"""Key defaults through the core tools in a real browser (#267, #257).

Opt-in like every real-browser suite; see tests/real_browser_harness.py. The fixture is the
issue's diagnostic: "abcdef" with the caret at the end, reset before each key, in an input
and an editor, with the text, caret and selection read back from the page. Forms record
their submits and cancel the navigation.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import re
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>keys</title>
<input id="field" aria-label="Field">
<div id="editor" contenteditable="true" aria-label="Editor"></div>
<textarea id="notes" aria-label="Notes"></textarea>
<input id="handled" aria-label="Handled">
<form id="search"><input id="query" aria-label="Search"><button>검색</button></form>
<form id="solo"><input id="solo-query" aria-label="Solo"></form>
<script>
  for (const form of document.forms) form.addEventListener('submit', event => {
    event.preventDefault();
    form.dataset.submits = Number(form.dataset.submits || 0) + 1;
  });
  document.getElementById('handled').addEventListener('keydown', event => {
    if (event.key === 'Backspace') { event.preventDefault(); event.target.dataset.seen = 'Backspace'; }
  });
</script>"""

# Puts "abcdef" in the field or editor and the caret at its end, focused.
RESET = """(id) => {
  const el = document.getElementById(id);
  if ('value' in el) { el.value = 'abcdef'; el.focus(); el.setSelectionRange(6, 6); return; }
  el.textContent = 'abcdef';
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  getSelection().removeAllRanges();
  getSelection().addRange(range);
}"""

# Text, caret (offset of the selection's focus) and selected text.
STATE = """(id) => {
  const el = document.getElementById(id);
  if ('value' in el) return [el.value, el.selectionStart, el.selectionEnd, el.value.slice(el.selectionStart, el.selectionEnd)];
  const selection = getSelection();
  const before = document.createRange();
  before.setStart(el, 0);
  before.setEnd(selection.focusNode, selection.focusOffset);
  const anchor = document.createRange();
  anchor.setStart(el, 0);
  anchor.setEnd(selection.anchorNode, selection.anchorOffset);
  const [start, end] = [anchor.toString().length, before.toString().length].sort((a, b) => a - b);
  return [el.textContent.replace(/\\u00a0/g, ' '), start, end, selection.toString()];
}"""


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


class RealBrowserKeyDefaultTests(harness.RealBrowserTestCase):
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

    def evaluate(self, function, element_id):
        return self.tool("browser_evaluate", {"function": f"() => ({function})({json.dumps(element_id)})"})["result"]

    def press(self, element_id, key):
        self.evaluate(RESET, element_id)
        result = self.tool("browser_press_key", {"key": key})
        return result, self.evaluate(STATE, element_id)

    def test_editing_keys_change_an_input_and_an_editor(self):
        expected = {
            "Backspace": (["abcde", 5, 5, ""], "deleteContentBackward"),
            "ArrowLeft": (["abcdef", 5, 5, ""], "moveCaret"),
            "Shift+ArrowLeft": (["abcdef", 5, 6, "f"], "extendSelection"),
            "Space": (["abcdef ", 7, 7, ""], "insertText"),
            "x": (["abcdefx", 7, 7, ""], "insertText"),
            # Negative control: already at the end.
            "End": (["abcdef", 6, 6, ""], None),
        }
        for element_id in ("field", "editor"):
            for key, (state, effect) in expected.items():
                with self.subTest(element=element_id, key=key):
                    result, after = self.press(element_id, key)
                    self.assertEqual(after, state)
                    self.assertEqual(result.get("effect"), effect, result)
                    self.assertEqual(result.get("key"), key)

    def test_enter_adds_a_line_in_an_editor_and_a_textarea(self):
        result, (text, start, end, _) = self.press("editor", "Enter")
        self.assertEqual(result["effect"], "insertParagraph", result)
        lines = self.tool("browser_evaluate", {"function": "() => document.getElementById('editor').innerText"})
        self.assertEqual(lines["result"].split("\n")[0], "abcdef")
        self.assertGreater(lines["result"].count("\n"), 0, lines)
        self.assertEqual((text, start, end), ("abcdef", 6, 6), "the caret is on the new line")

        result, after = self.press("notes", "Enter")
        self.assertEqual(result["effect"], "insertLineBreak", result)
        self.assertEqual(after, ["abcdef\n", 7, 7, ""])

    def test_a_page_that_handles_the_key_is_not_edited_as_well(self):
        result, after = self.press("handled", "Backspace")
        self.assertTrue(result.get("prevented"), result)
        self.assertEqual(after, ["abcdef", 6, 6, ""])
        seen = self.tool("browser_evaluate", {"function": "() => document.getElementById('handled').dataset.seen"})
        self.assertEqual(seen["result"], "Backspace")

    def test_type_with_submit_submits_the_form_by_enter(self):
        snapshot = self.tool("browser_snapshot", {})["snapshot"]
        for label, form in (("Search", "search"), ("Solo", "solo")):
            with self.subTest(form=form):
                ref = re.search(rf'textbox "{label}" \[ref=(e\d+)\]', snapshot).group(1)
                result = self.tool("browser_type", {"target": ref, "text": "성수 조용한 카페", "submit": True})
                self.assertEqual(result.get("effect"), "implicitSubmit", result)
                submits = self.tool("browser_evaluate", {
                    "function": f"() => document.getElementById('{form}').dataset.submits"})
                self.assertEqual(submits["result"], "1")


if __name__ == "__main__":
    unittest.main()
