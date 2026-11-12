"""#539: field restrictions through actual stdio MCP, in an isolated browser/profile."""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

PAGE = """<!doctype html><meta charset="utf-8"><title>Restricted fields</title>
<input id=date readonly value="before">
<textarea id=area readonly>before</textarea>
<input id=disabled disabled value="before">
<fieldset disabled><input id=fieldset value="before"></fieldset>
<div id=host></div><input id=editable value="before"><output id=events></output>
<button id=pick>Pick date</button>
<script>
const root = document.getElementById('host').attachShadow({mode: 'closed'});
root.innerHTML = '<input id=shadow readonly value="before">';
const record = event => {
  document.getElementById('events').textContent += event.target.id + ':' + event.type + ';';
};
for (const field of [...document.querySelectorAll('input, textarea'), root.querySelector('input')]) {
  field.addEventListener('input', record);
  field.addEventListener('change', record);
}
document.getElementById('pick').onclick = () => {
  const field = document.getElementById('date');
  field.value = '12/23/2016';
  field.dispatchEvent(new Event('input'));
  field.dispatchEvent(new Event('change'));
};
</script>"""


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserReadonlyFillTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def test_restricted_fields_and_partial_writes_through_mcp(self):
        async def run():
            env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "readonly-fill-test"}
            params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    return result.isError, json.loads(result.content[0].text)

                async def python(code):
                    error, value = await call("browser_run_python", {"code": code})
                    self.assertFalse(error, value)
                    return value.get("result")

                error, opened = await call("browser_tabs", {"action": "new", "url": self.site.origin})
                self.assertFalse(error, opened)
                tab_id = await python("result = tab.id")
                self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})

                async def state():
                    # Test-only read: CSS query does not enumerate closed shadow roots. Inspect
                    # actual native values, including the field the host resolves to, without writes.
                    function = ("() => ({fields: [...document.querySelectorAll('input, textarea'), "
                                "document.getElementById('host').openOrClosedShadowRoot.querySelector('input')]"
                                ".map(el => ({id: el.id, value: el.value})), "
                                "events: [{text: document.getElementById('events').textContent}]})")
                    return await python(f"result = tab.evaluate({function!r})")

                initial = await state()
                self.assertEqual({item['id'] for item in initial['fields']},
                                 {'date', 'area', 'disabled', 'fieldset', 'shadow', 'editable'})
                self.assertEqual({item['value'] for item in initial['fields']}, {'before'})
                for selector in ('#date', '#area', '#disabled', '#fieldset', '#host'):
                    calls = [
                        ('browser_run_python', {'code': f'tab.fill({selector!r}, "wrong")'}),
                        ('browser_run_python', {'code': f'tab.type({selector!r}, "wrong")'}),
                        ('browser_run_python', {'code': f'tab.fill_form({{{selector!r}: "wrong"}})'}),
                        ('browser_tools_call', {'name': 'browser_type',
                                               'arguments': {'target': selector, 'text': 'wrong'}}),
                        ('browser_tools_call', {'name': 'browser_fill_form', 'arguments': {'fields': [
                            {'name': 'restricted', 'type': 'textbox', 'target': selector, 'value': 'wrong'}]}}),
                    ]
                    for name, arguments in calls:
                        with self.subTest(selector=selector, tool=name, arguments=arguments):
                            error, stopped = await call(name, arguments)
                            self.assertTrue(error, stopped)
                            held = stopped.get('failed_operation', stopped)
                            field = held.get('results', {}).get(selector, held)
                            self.assertEqual(field['code'], 'not_editable', stopped)
                            self.assertFalse(field['retryable'])
                            self.assertEqual(field['state']['reason'],
                                             'disabled' if selector in ('#disabled', '#fieldset') else 'readonly')
                            self.assertEqual(await state(), initial, 'no value change or input/change event')

                error, stopped = await call('browser_run_python', {
                    'code': 'tab.fill("#editable", "keep"); tab.fill("#date", "wrong"); '
                            'tab.fill("#editable", "must not run")'})
                self.assertTrue(error, stopped)
                self.assertEqual([entry['status'] for entry in stopped['operations']], ['ok', 'failed'])
                after = await state()
                self.assertEqual(next(item['value'] for item in after['fields'] if item['id'] == 'editable'), 'keep')
                self.assertEqual(after['events'], [{'text': 'editable:input;editable:change;'}])

                error, stopped = await call('browser_tools_call', {'name': 'browser_fill_form', 'arguments': {
                    'fields': [{'name': 'draft', 'type': 'textbox', 'target': '#editable', 'value': 'direct'},
                               {'name': 'date', 'type': 'textbox', 'target': '#date', 'value': 'wrong'}]}})
                self.assertTrue(error, stopped)
                self.assertEqual(stopped['filled'], ['draft'])
                self.assertEqual(stopped['code'], 'not_editable')
                after = await state()
                self.assertEqual(after['events'], [{'text': 'editable:input;editable:change;' * 2}])

                # The page's own picker is still allowed to update its readonly input.
                await python('tab.click("#pick")')
                after = await state()
                self.assertEqual(next(item['value'] for item in after['fields'] if item['id'] == 'date'), '12/23/2016')
                self.assertTrue(after['events'][0]['text'].endswith('date:input;date:change;'))
                listed = await python('result = next(t for t in browser.tabs() if t["id"] == tab.id)')
                self.assertFalse(listed['active'], 'testing the agent-owned background tab never steals focus')

        asyncio.run(asyncio.wait_for(run(), 120))
