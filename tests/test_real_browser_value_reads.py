"""A script reads a long field value whole and writes it back, in a real browser (issue #650).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Like a Roundcube reply: a script reads the quoted body with tab.query(..., fields=['value']) or
tab.forms() and fills it back in the same call. A script's reads are not model-visible, so values
come whole; the direct tools keep their 400 and 100 characters. A value still cut ends with …,
its row has valueClipped, and both calls warn.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import unittest

from kitsune import script_runner
import real_browser_harness as harness

BODY = ("On Tue, Ada wrote:\n" + "> The quarterly report is attached. " * 44)[:1599]
PAGE = f"""<!doctype html><title>Compose</title>
<textarea id=body>{BODY}</textarea><textarea id=out></textarea><textarea id=huge></textarea>"""


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


class _SuiteBridge:
    """The script host's bridge client (script_runner.run_python), over the suite's own bridge."""

    def __init__(self, call):
        self.call = call

    def request(self, method, route, *, json=None, **_):
        return self.call(route, json)


RECORDED = ("import warnings\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n")


class RealBrowserValueReadTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def run_script(self, tab, code):
        done = asyncio.run(script_runner.run_python(code, _SuiteBridge(self.call), tab_id=tab, timeout=60))
        self.assertNotIn("failed_operation", done, done)
        return done["result"]

    def test_a_script_reads_a_long_value_whole_and_writes_it_back(self):
        tab = self.open_tab(f"{self.site.origin}/")
        result = self.run_script(tab, RECORDED + (
            "    real = tab.markdown('#body')\n"
            "    form = tab.forms()[0]\n"
            "    row = tab.query('#body', fields=['value'])[0]\n"
            "    tab.fill('#out', 'Hi,\\n\\n' + row['value'])\n"
            "result = {'real': len(real), 'form': len(form['value']), 'query': len(row['value']),\n"
            "          'out': len(tab.markdown('#out')), 'clipped': ['valueClipped' in form, 'valueClipped' in row],\n"
            "          'warnings': [str(w.message) for w in caught]}"))
        self.assertEqual(result, {"real": 1599, "form": 1599, "query": 1599, "out": 1604,
                                  "clipped": [False, False], "warnings": []})

        # The direct tools stay bounded, and mark their cut.
        query = self.call("/api/query", {"tabId": tab, "selector": "#body", "fields": ["value"]})
        self.assertEqual((len(query["items"][0]["value"]), query["items"][0]["value"][-1]), (401, "…"))
        self.assertEqual((query["items"][0]["valueClipped"], query["clipped"]), (True, {"value": 1}))
        form = self.call("/api/forms", {"tabId": tab})["fields"][0]
        self.assertEqual((len(form["value"]), form["value"][-1], form["valueClipped"]), (101, "…", True))

    def test_a_value_still_cut_carries_a_marker_and_warns(self):
        tab = self.open_tab(f"{self.site.origin}/")
        self.call("/api/js", {"tabId": tab, "code": "document.getElementById('huge').value = 'z'.repeat(100005)"})
        result = self.run_script(tab, RECORDED + (
            "    form = tab.forms()[2]\n"
            "    row = tab.query('#huge', fields=['value'])[0]\n"
            "result = {'form': [len(form['value']), form['value'][-1], form.get('valueClipped')],\n"
            "          'query': [len(row['value']), row['value'][-1], row.get('valueClipped')],\n"
            "          'warnings': [str(w.message) for w in caught]}"))
        self.assertEqual(result["form"], [100001, "…", True])
        self.assertEqual(result["query"], [100001, "…", True])
        self.assertEqual(len(result["warnings"]), 2, result["warnings"])
        self.assertIn("Form field values were cut (1 field;", result["warnings"][0])
        self.assertIn("Query fields were clipped (value in 1 rows)", result["warnings"][1])


if __name__ == "__main__":
    unittest.main()
