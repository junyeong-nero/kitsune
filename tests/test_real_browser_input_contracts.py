"""#543: actual caret/focus/order through stdio Python, without model or user profile.

Native JS below only seeds/observes the synthetic fixture; it is not a new product
API or an agent recovery path. Existing #159/#267 suites remain independent controls.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Input contracts</title>
<input id="field" value="abcdef"><textarea id="notes">abcdef</textarea>
<div id="editor" contenteditable="true">abcdef</div>
<ul id="sortable"><li><button id="one">First</button></li><li><button id="two">Second</button></li></ul>
<button id="save">Save</button>
<script>
const params = new URLSearchParams(location.search);
const el = document.getElementById(params.get('field') || 'field');
const start = Number(params.get('start') || 0), end = Number(params.get('end') || start);
el.focus();
if ('value' in el) el.setSelectionRange(start, end);
else {
  const range = document.createRange();
  range.setStart(el.firstChild, start); range.setEnd(el.firstChild, end);
  getSelection().removeAllRanges(); getSelection().addRange(range);
}
let lifted = null, saves = 0, saved = null;
const events = [];
const order = () => [...document.querySelectorAll('#sortable button')].map(el => el.id);
document.addEventListener('keydown', event => {
  events.push({key: event.key, target: event.target.id, ctrl: event.ctrlKey, shift: event.shiftKey});
  if (params.has('handled') && event.key === 'x') event.preventDefault();
}, true);
sortable.addEventListener('keydown', event => {
  const item = event.target.closest('li');
  if (event.key === ' ') {
    event.preventDefault(); lifted = lifted ? null : item;
    if (params.has('replace')) event.target.replaceWith(event.target.cloneNode(true));
  } else if (event.key === 'ArrowUp' && lifted) {
    event.preventDefault();
    if (lifted.previousElementSibling) sortable.insertBefore(lifted, lifted.previousElementSibling);
  }
});
const fields = () => Object.fromEntries(['field', 'notes', 'editor'].map(id => {
  const el = document.getElementById(id), text = 'value' in el ? el.value : el.textContent;
  let start = el.selectionStart ?? null, end = el.selectionEnd ?? null;
  const sel = getSelection();
  if (el.isContentEditable && el.contains(sel.anchorNode) && el.contains(sel.focusNode)) {
    const offset = (node, at) => {
      const range = document.createRange(); range.setStart(el, 0); range.setEnd(node, at);
      return range.toString().length;
    };
    [start, end] = [offset(sel.anchorNode, sel.anchorOffset), offset(sel.focusNode, sel.focusOffset)].sort((a, b) => a-b);
  }
  return [id, {text, start, end}];
}));
save.addEventListener('click', () => { saves++; saved = {order: order(), fields: fields()}; });
window.state = () => ({fields: fields(), active: document.activeElement.id || document.activeElement.tagName,
  order: order(), lifted: lifted?.querySelector('button').id || null, events, saves, saved});
</script>"""


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


class RealBrowserInputContractTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    async def state(self, session):
        # Use the owning test session even for developer-build observation; the separate
        # operator channel correctly refuses executeJS on another client's tab.
        observed = await self.script(session, 'result = tab.evaluate("() => window.wrappedJSObject.state()")')
        return observed["result"]

    async def script(self, session, code, error=False):
        reply = await session.call_tool("browser_run_python", {"code": code})
        self.assertEqual(bool(reply.isError), error, reply)
        return json.loads(reply.content[0].text)

    def scenario(self, run):
        async def main():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "input-contracts"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
                self.assertFalse(opened.isError, opened)
                tab_id = (await self.script(session, "result = tab.id"))["result"]
                self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
                before = [t["id"] for t in self.call("/api/tabs")["tabs"] if t["active"]]
                await run(session, tab_id)
                after = [t["id"] for t in self.call("/api/tabs")["tabs"] if t["active"]]
                self.assertEqual(after, before, "synthetic editing never activates the task tab")
                self.assertNotIn(tab_id, after)
        asyncio.run(asyncio.wait_for(main(), 120))

    async def reset(self, session, query=""):
        await self.script(session, f"tab.navigate({(self.site.origin + '/?' + query)!r})")

    def test_type_fill_and_key_insertion_have_distinct_contracts(self):
        async def run(session, tab_id):
            for field in ("field", "notes", "editor"):
                for start, end in ((0, 0), (3, 3), (6, 6), (1, 4)):
                    with self.subTest(field=field, start=start, end=end):
                        await self.reset(session, f"field={field}&start={start}&end={end}")
                        await self.script(session, f"tab.type('#{field}', 'X')")
                        expected = "abcdef"[:start] + "X" + "abcdef"[end:] if field == "editor" else "abcdefX"
                        caret = start + 1 if field == "editor" else 7
                        state = await self.state(session)
                        self.assertEqual(state["active"], field)
                        self.assertEqual(state["fields"][field], {"text": expected, "start": caret, "end": caret})
                        # Unlike native-field type(), press inserts at the selection for all three kinds.
                        await self.reset(session, f"field={field}&start={start}&end={end}")
                        await self.script(session, f"tab.press('X', target='#{field}')")
                        self.assertEqual((await self.state(session))["fields"][field], {
                            "text": "abcdef"[:start] + "X" + "abcdef"[end:], "start": start + 1, "end": start + 1})
                for operation in (f"tab.type('#{field}', 'Replacement', clear=True)",
                                  f"tab.fill('#{field}', 'Replacement')"):
                    await self.reset(session, f"field={field}&start=1&end=4")
                    await self.script(session, operation + "\ntab.click('#save')")
                    self.assertEqual((await self.state(session))["saved"]["fields"][field]["text"], "Replacement")
                # Explicit end placement makes append intent unambiguous, also in an editor.
                await self.reset(session, f"field={field}&start=1&end=4")
                await self.script(session, f"tab.press('End', target='#{field}', modifiers={{'ctrl': True}})\n"
                                          f"tab.type('#{field}', ' Added.')\ntab.click('#save')")
                state = await self.state(session)
                self.assertEqual(state["saved"]["fields"][field]["text"], "abcdef Added.")
                self.assertTrue(state["events"][0]["ctrl"])
        self.scenario(run)

    def test_prevented_keys_modifiers_and_noop_boundaries(self):
        async def run(session, tab_id):
            for field in ("field", "notes", "editor"):
                await self.reset(session, f"field={field}&start=6&end=6&handled=1")
                reply = await self.script(session, f"result = tab.press('End', target='#{field}')")
                self.assertIsNone(reply["result"]["effect"])
                reply = await self.script(session, f"result = tab.press('x', target='#{field}')")
                self.assertTrue(reply["result"]["prevented"])
                self.assertEqual((await self.state(session))["fields"][field], {"text": "abcdef", "start": 6, "end": 6})
                await self.script(session, f"tab.press('ArrowLeft', target='#{field}', modifiers={{'shift': True}})")
                state = await self.state(session)
                self.assertEqual(state["fields"][field], {"text": "abcdef", "start": 5, "end": 6})
                self.assertEqual(state["events"][-1], {"key": "ArrowLeft", "target": field, "ctrl": False, "shift": True})
        self.scenario(run)

    def test_targeted_sorting_focus_loss_and_no_write_replay(self):
        async def run(session, tab_id):
            # Targeting a button dispatches there, but is not a focus request.
            for batched in (False, True):
                await self.reset(session)
                if batched:
                    await self.script(session, "tab.press('Space', target='#two'); tab.press('ArrowUp')")
                else:
                    await self.script(session, "tab.press('Space', target='#two')")
                    self.assertEqual((await self.state(session))["active"], "field")
                    await self.script(session, "tab.press('ArrowUp')")
                state = await self.state(session)
                self.assertEqual(state["order"], ["one", "two"])
                self.assertEqual(state["events"][-1]["target"], "field")
                await self.script(session, "tab.press('ArrowUp', target='#two'); tab.press('Space', target='#two')")
                self.assertEqual((await self.state(session))["order"], ["two", "one"])
            for replace in (False, True):
                for batched in (False, True):
                    with self.subTest(replace=replace, batched=batched):
                        await self.reset(session, "replace=1" if replace else "")
                        await self.script(session, "tab.click('#two')")
                        self.assertEqual((await self.state(session))["active"], "two")
                        commands = ["tab.press('Space', target='#two')", "tab.press('ArrowUp', target='#two')",
                                    "tab.press('ArrowUp', target='#two')", "tab.press('Space', target='#two')",
                                    "tab.click('#save')"]
                        if batched:
                            await self.script(session, "\n".join(commands))
                        else:
                            await self.script(session, commands[0])
                            self.assertEqual((await self.state(session))["active"], "BODY" if replace else "two")
                            for command in commands[1:]:
                                await self.script(session, command)
                        state = await self.state(session)
                        self.assertEqual(state["order"], ["two", "one"], "second ArrowUp is a handled boundary no-op")
                        self.assertIsNone(state["lifted"])
                        self.assertEqual(state["saved"]["order"], ["two", "one"])
                        failed = await self.script(session,
                            "tab.query('button:has-text(\"First\")')\ntab.click('#save')", error=True)
                        self.assertEqual(failed["operations"][0]["status"], "failed")
                        await self.script(session, "result = tab.query('#sortable button', fields=['id'])")
                        self.assertEqual((await self.state(session))["saves"], 1, "observation failure does not undo or replay the save")
        self.scenario(run)
