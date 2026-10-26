"""Enter in a text input, and browser_submit_form, submit by form ownership, not containment,
in a real browser (#288).

Opt-in like every real-browser suite; see tests/real_browser_harness.py. Each form of the
fixture is one case of HTML implicit submission. Submit handlers cancel the navigation, and
the page logs every submit (form and submitter) and every click on a button or image input,
so each case checks what was submitted, by which control, and that nothing else was.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools
import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Form ownership</title>
<form id="owner">
  <input id="owner-q" aria-label="Owner">
  <button id="foreign-button" form="foreign">Foreign action</button>
</form>
<form id="foreign"></form>

<form id="external"><input id="external-q" aria-label="External"></form>
<button id="external-send" form="external">Search</button>

<form id="blocked"><input id="blocked-q" aria-label="Blocked"></form>
<input id="blocked-other" form="blocked" aria-label="Blocked other">

<button id="early" form="ordered">Early</button>
<form id="ordered"><input id="ordered-q" aria-label="Ordered"><button id="ordered-inner">Inner</button></form>

<form id="image"><input id="image-q" aria-label="Image"></form>
<input type="image" id="image-send" form="image" alt="Go">

<form id="disabled">
  <input id="disabled-q" aria-label="Disabled">
  <button id="disabled-send" disabled>Go</button><button id="disabled-other">Other</button>
</form>

<form id="cancelled"><input id="cancelled-q" aria-label="Cancelled"><button id="cancelled-send">Go</button></form>

<pre id="events"></pre>
<script>
  const log = entry => { document.getElementById('events').textContent += JSON.stringify(entry) + '\\n'; };
  for (const form of document.forms) form.addEventListener('submit', event => {
    event.preventDefault();
    log({ submit: form.id, submitter: event.submitter ? event.submitter.id : null });
  });
  for (const control of document.querySelectorAll('button, input[type=image]')) {
    control.addEventListener('click', () => log({ click: control.id }));
  }
  document.getElementById('cancelled-q').addEventListener('keydown', event => {
    if (event.key === 'Enter') event.preventDefault();
  });
</script>"""

# The page's log of each case: (input, effect, events).
CASES = {
    # The issue's reproduction: the button inside "owner" belongs to "foreign", so "owner" has
    # no default button, and its one text input submits it, with no submitter.
    "owner": ("owner-q", "implicitSubmit", [{"submit": "owner", "submitter": None}]),
    # A button associated from outside the form is its default button, and is clicked.
    "external": ("external-q", "implicitSubmit",
                 [{"click": "external-send"}, {"submit": "external", "submitter": "external-send"}]),
    # A second text input associated from outside blocks implicit submission.
    "blocked": ("blocked-q", None, []),
    # The default button is the first in tree order, here before the form.
    "ordered": ("ordered-q", "implicitSubmit",
                [{"click": "early"}, {"submit": "ordered", "submitter": "early"}]),
    # An image input is a submit control, which form.elements does not list.
    "image": ("image-q", "implicitSubmit",
              [{"click": "image-send"}, {"submit": "image", "submitter": "image-send"}]),
    # A disabled default button submits nothing; the next button is not used instead.
    "disabled": ("disabled-q", None, []),
}

# browser_submit_form on each form: (input, submitted, the result's button label, events). It
# clicks the form's default button by the same ownership, else calls requestSubmit.
SUBMIT_FORM_CASES = {
    # The descendant button belongs to "foreign", so "owner" has no default button.
    "owner": ("owner-q", "requestSubmit", "", [{"submit": "owner", "submitter": None}]),
    "external": ("external-q", "button", "Search",
                 [{"click": "external-send"}, {"submit": "external", "submitter": "external-send"}]),
    # An explicit submission is not blocked by a second text input.
    "blocked": ("blocked-q", "requestSubmit", "", [{"submit": "blocked", "submitter": None}]),
    "ordered": ("ordered-q", "button", "Early",
                [{"click": "early"}, {"submit": "ordered", "submitter": "early"}]),
    "image": ("image-q", "button", "Go",
              [{"click": "image-send"}, {"submit": "image", "submitter": "image-send"}]),
    # A form whose button is its own descendant, as before #288.
    "cancelled": ("cancelled-q", "button", "Go",
                  [{"click": "cancelled-send"}, {"submit": "cancelled", "submitter": "cancelled-send"}]),
}


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


class RealBrowserFormOwnershipTests(harness.RealBrowserTestCase):
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

    def submit_form(self, selector):
        """browser_submit_form's result in the current tab. It is a browse tool, which the core
        MCP server does not publish, so it is called directly."""
        with patch.object(tools, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = tools.browser_submit_form(selector, tab_id=playwright_tools._current_tab)
        self.assertNotIn("error", result)
        return result

    def events(self):
        """The page's log since the last call, which it clears."""
        text = self.tool("browser_evaluate", {"function": (
            "() => { const log = document.getElementById('events'); "
            "const text = log.textContent; log.textContent = ''; return text; }")})["result"]
        return [json.loads(line) for line in text.splitlines() if line]

    def type_and_submit(self, element_id, separate_enter):
        """browser_type with submit=true, or browser_type and then browser_press_key("Enter")."""
        target = "#" + element_id
        if not separate_enter:
            return self.tool("browser_type", {"target": target, "text": "query", "submit": True})
        typed = self.tool("browser_type", {"target": target, "text": "query"})
        self.assertNotIn("effect", typed)
        return self.tool("browser_press_key", {"key": "Enter"})

    def test_enter_submits_the_form_that_owns_the_input(self):
        for separate_enter in (False, True):
            for form, (element_id, effect, expected) in CASES.items():
                with self.subTest(form=form, separate_enter=separate_enter):
                    self.events()
                    result = self.type_and_submit(element_id, separate_enter)
                    self.assertEqual(result.get("key"), "Enter", result)
                    self.assertEqual(result.get("effect"), effect, result)
                    self.assertNotIn("prevented", result)
                    self.assertEqual(self.events(), expected)
                    value = self.tool("browser_evaluate", {
                        "function": f"() => document.getElementById('{element_id}').value"})["result"]
                    self.assertEqual(value, "query")

    def test_submit_form_clicks_the_button_the_form_owns(self):
        # The form named by selector, or without one, the form of the field last filled.
        for by_filled_field in (False, True):
            for form, (element_id, submitted, button, expected) in SUBMIT_FORM_CASES.items():
                with self.subTest(form=form, by_filled_field=by_filled_field):
                    self.tool("browser_type", {"target": "#" + element_id, "text": "query"})
                    self.events()
                    result = self.submit_form(None if by_filled_field else "#" + form)
                    self.assertEqual((result.get("submitted"), result.get("form", {}).get("button")),
                                     (submitted, button), result)
                    self.assertEqual(self.events(), expected)

    def test_a_page_that_cancels_enter_is_not_submitted(self):
        for separate_enter in (False, True):
            with self.subTest(separate_enter=separate_enter):
                self.events()
                result = self.type_and_submit("cancelled-q", separate_enter)
                self.assertTrue(result.get("prevented"), result)
                self.assertIsNone(result.get("effect"), result)
                self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
