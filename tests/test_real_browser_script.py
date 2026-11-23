"""Python scripting over an actual stdio MCP client and the isolated Zen/Firefox fixture."""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import re
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness
from test_real_browser_python_api import _Site

# #570: the visible text and the snapshot of this page are each over the 16 KB output limit. The
# buttons are leaf children of one parent, which a snapshot lists in full, not as "more similar items".
LONG_PAGE = ('<!doctype html><meta charset="utf-8"><title>Long page</title><input id="draft">'
             + "".join(f'<button>배송과 반품 정책 안내 항목 {i}</button> {"참고 자료입니다. " * 10}' for i in range(400)))


class _LongSite(BaseHTTPRequestHandler):
    def do_GET(self):
        data = LONG_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# #572: two pages that share a navigation link. Page B also has a lone namesake of A's button, a
# link named like A's that leads elsewhere, two links named like one of A's, and nothing named
# like A's "Only here".
NAV = '<nav><a href="/bookmarks">Bookmarks</a></nav>'
PAGES = {
    "/a": ("Page A", NAV + '<a href="/b">Next</a> <a href="/old">Moved</a> <a href="/twin">Twin</a> '
           '<a href="/only">Only here</a> <button>Go</button>'),
    "/b": ("Page B", NAV + '<a href="/new">Moved</a> <a href="/twin">Twin</a> <a href="/twin2">Twin</a> '
           """<button onclick="document.title = 'Go clicked'">Go</button>"""),
}


class _NavSite(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":  # #618: answers after a short script's deadline
            time.sleep(4)
        title, body = PAGES.get(self.path, (self.path.strip("/").capitalize(), "<p>Arrived</p>"))
        data = f"<!doctype html><title>{title}</title>{body}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserScriptTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        cls.nav = harness.FixtureServer(_NavSite)
        cls.addClassCleanup(cls.nav.stop)
        super().setUpClass()

    async def scenario(self, run, **env):
        env = {**self.bridge.env, "KITSUNE_SCRIPTING": "1", "KITSUNE_OWNER_ID": "script-smoke", **env}
        params = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            self.assertIn("browser_run_python", names)
            self.assertNotIn("browser_forms", names)
            self.assertNotIn("browser_hover", names)
            opened = await session.call_tool("browser_tabs", {"action": "new", "url": self.site.origin})
            self.assertFalse(opened.isError, opened)
            selected = await session.call_tool("browser_run_python", {"code": "result = tab.id"})
            self.assertFalse(selected.isError, selected)
            tab_id = json.loads(selected.content[0].text)["result"]
            # The fixture's unrestricted operator channel cleans up even if the MCP session fails.
            self.addCleanup(self.call, "/api/close-tab", {"tabId": tab_id})
            await run(session, tab_id)

    def run_scenario(self, run, **env):
        asyncio.run(asyncio.wait_for(self.scenario(run, **env), 60))

    def test_guide_reads_tab_dicts_and_every_method_runs(self):
        async def run(session, tab_id):
            listed = await session.list_tools()
            description = next(tool.description for tool in listed.tools if tool.name == "browser_run_python")
            self.assertIn("list[dict]", description)
            example = 'browser.tab(t["id"]).snapshot(text=True)'
            self.assertIn(example, description)
            # Read the fixture's page, not browser-owned about:blank or welcome tabs.
            code = f't = next(t for t in browser.tabs() if t["id"] == {tab_id})\nresult = {example}'
            snapshot = await session.call_tool("browser_run_python", {"code": code})
            self.assertFalse(snapshot.isError, snapshot)
            self.assertIn("Orders", json.loads(snapshot.content[0].text)["result"])
            query = await session.call_tool("browser_run_python", {
                "code": f'result = browser.tab({tab_id}).query("h1", fields=["text"])'})
            # #525: no capability group limits the supplied browser API.
            self.assertFalse(query.isError, query)
        self.run_scenario(run)

    def test_actions_return_what_changed_and_a_script_of_only_actions_returns_it(self):
        # #575: agents printed the whole page after each action; the action's result already had the change.
        async def run(session, tab_id):
            async def python(code):
                reply = await session.call_tool("browser_run_python", {"code": code})
                self.assertFalse(reply.isError, reply)
                return json.loads(reply.content[0].text)

            listed = await session.list_tools()
            description = next(tool.description for tool in listed.tools if tool.name == "browser_run_python")
            page = (await python("result = tab.snapshot()"))["result"]
            name = re.search(r'textbox "Name" \[ref=(e\d+)\]', page).group(1)
            save = re.search(r'button "Save" \[ref=(e\d+)\]', page).group(1)

            # The description's example, with this page's refs: its key is the one a real action returns.
            example = re.search(r"Example: (.+)\.\n", description).group(1)
            self.assertIn("r['snapshot']", example)
            documented = (await python(example.replace("e12", name).replace("e15", save)))["result"]
            self.assertIn('text "Saved Ada"', documented)
            self.assertIn(f"[ref={save}]", documented)
            self.assertNotIn("Orders", documented, "what changed, not the page")

            only_actions = await python(f"tab.fill({name!r}, 'Grace'); tab.click({save!r})")
            self.assertEqual(only_actions["output"], "")
            changed = only_actions["result"]
            self.assertEqual(set(changed), {"last_action", "snapshot", "snapshot_delta"})
            self.assertEqual(changed["last_action"], only_actions["operations"][-1]["route"])
            self.assertEqual(changed["last_action"], "/api/click")
            self.assertIs(changed["snapshot_delta"], True)
            self.assertIn('text "Saved Grace"', changed["snapshot"])
            self.assertNotIn("Orders", changed["snapshot"])

            # A script that chose its output, or whose last operation is a read, returns only that.
            for chosen in ("print('saved')", "result = None", "tab.info()"):
                reply = await python(f"tab.click({save!r})\n{chosen}")
                self.assertNotIn("result", reply, chosen)
                self.assertEqual(reply["output"], "saved\n" if "print" in chosen else "", chosen)

            # A click that loads another document returns that page, not a delta.
            loaded = (await python("tab.click('button[type=submit]')"))["result"]
            self.assertEqual(loaded["last_action"], "/api/click")
            self.assertEqual(loaded["navigated"]["title"], "Done")
            self.assertIn("name=Grace", loaded["navigated"]["url"])
            self.assertIn("Thanks", loaded["snapshot"])
            self.assertNotIn("snapshot_delta", loaded)

            opened = (await python(f"tab.navigate({self.site.origin + '/'!r})"))["result"]
            self.assertEqual(opened["last_action"], "/api/navigate")
            self.assertIn('heading "Orders"', opened["snapshot"])
            self.assertNotIn("snapshot_delta", opened)

            # submit_form's snapshot is taken before its navigation: it is not returned as what changed.
            submitted = await python("tab.fill('#name', 'Bo'); tab.submit_form('#order')")
            self.assertNotIn("result", submitted)
            self.assertEqual([entry["route"] for entry in submitted["operations"]], ["/api/fill", "/api/submit-form"])
            done = await python("tab.wait_for(title='Done'); result = tab.info()['url']")
            self.assertIn("name=Bo", done["result"])
        self.run_scenario(run)

    def test_direct_and_scripted_draft_share_the_tab_and_refuse_foreign_writes(self):
        foreign = self.open_tab(self.site.origin)

        async def run(session, tab_id):
            code = ("for name in ['Ada', 'Grace']:\n    tab.fill('#name', name)\n"
                    "tab.check('#agree')\n"
                    "result = [field['value'] for field in tab.forms() if field.get('name') == 'name']")
            scripted = await session.call_tool("browser_run_python", {"code": code})
            self.assertFalse(scripted.isError, scripted)
            self.assertEqual(json.loads(scripted.content[0].text)["result"], ["Grace"])
            observed = await session.call_tool("browser_snapshot", {})
            self.assertFalse(observed.isError, observed)
            self.assertIn("Grace", json.loads(observed.content[0].text)["snapshot"])

            next_call = await session.call_tool("browser_run_python", {
                "code": f"result = {{'fresh': 'name' not in globals(), 'title': browser.tab({tab_id}).info()['title']}}"})
            self.assertFalse(next_call.isError, next_call)
            self.assertEqual(json.loads(next_call.content[0].text)["result"], {"fresh": True, "title": "Python API"})

            refused = await session.call_tool("browser_run_python", {"code": f"browser.tab({foreign}).fill('#name', 'wrong')"})
            self.assertTrue(refused.isError, refused)
            self.assertEqual(json.loads(refused.content[0].text)["failed_operation"]["code"], "user_tab_read_only")
            fields = self.call("/api/forms", {"tabId": foreign})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), "")

        self.run_scenario(run)

    def test_queried_href_navigates_and_an_origin_prefix_is_a_coded_failure(self):
        # #571: the script shape that failed, the origin prepended to a query href that is already absolute.
        async def run(session, tab_id):
            find = ("tab.navigate('/deals')\n"
                    "r = tab.query('a[href^=\"/deals/\"]', fields=['text', 'href'], limit=200)\n"
                    "m = [x for x in r if 'Radiant Solar' in x['text']]\n")
            stopped = await session.call_tool("browser_run_python", {
                "code": find + f"tab.navigate({self.site.origin!r} + m[0]['href'])"})
            self.assertTrue(stopped.isError, stopped)
            failed = json.loads(stopped.content[0].text)["failed_operation"]
            self.assertEqual((failed["code"], failed["retryable"]), ("invalid_url", False), failed)
            self.assertIn("already absolute", failed["hint"])
            self.assertEqual(self.call("/api/page-info", {"tabId": tab_id})["title"], "Deals", "nothing was loaded")
            for url in ("m[0]['href']", "'/deals/48'"):
                loaded = await session.call_tool("browser_run_python", {
                    "code": find + f"tab.navigate({url})\nresult = tab.info()"})
                self.assertFalse(loaded.isError, loaded)
                info = json.loads(loaded.content[0].text)["result"]
                self.assertEqual((info["url"], info["title"]), (self.site.origin + "/deals/48", "Deal 48"))
        self.run_scenario(run)

    def test_argument_error_explains_python_api_and_preserves_completed_draft(self):
        async def run(session, tab_id):
            for index, call in enumerate(("tab.find(regex='.')", "tab.find(text='원')",
                                          "tab.snapshot(boxes=True)", "tab.snapshot(depth=2)")):
                draft = f"Keep draft {index}"
                stopped = await session.call_tool("browser_run_python", {
                    "code": f"tab.fill('#name', {draft!r})\n{call}\ntab.fill('#name', 'must not run')"})
                self.assertTrue(stopped.isError, stopped)
                payload = json.loads(stopped.content[0].text)
                self.assertEqual(payload["code"], "script_error")
                self.assertIn("Python API: Tab.", payload["output"])
                self.assertEqual([entry["status"] for entry in payload["operations"]], ["ok"])
                self.assertNotIn("resume_token", payload)
                fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
                self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), draft)
            help_result = await session.call_tool("browser_run_python", {"code": "help(tab.find); help(tab.snapshot)"})
            self.assertFalse(help_result.isError, help_result)
            self.assertEqual(json.loads(help_result.content[0].text)["operations"], [])
        self.run_scenario(run)

    def name_field(self, tab_id):
        fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
        return next(field["value"] for field in fields if field.get("name") == "name")

    def test_caught_read_and_wait_failures_continue_but_writes_and_policy_still_stop(self):
        async def run(session, tab_id):
            # #568: unmet waits and a failed read raise in the script, which goes on.
            code = ("seen = []\n"
                    "for condition in ({'text': 'Never shown'}, {'target': '#missing'}, {'title': 'Never'}):\n"
                    "    try:\n        tab.wait_for(timeout=300, **condition)\n"
                    "    except TimedOut as error:\n        seen.append(type(error).__name__)\n"
                    "try:\n    tab.snapshot(target='e99999')\n"
                    "except KitsuneError as error:\n    seen.append(error.code)\n"
                    "tab.fill('#name', 'After the waits')\n"
                    "result = seen + [tab.wait_for(text='Orders', timeout=2000)['ok']]")
            continued = await session.call_tool("browser_run_python", {"code": code})
            self.assertFalse(continued.isError, continued)
            payload = json.loads(continued.content[0].text)
            self.assertEqual(payload["result"], ["TimedOut", "TimedOut", "TimedOut", "stale_ref", True])
            self.assertEqual([entry["status"] for entry in payload["operations"]],
                             ["failed", "failed", "failed", "failed", "ok", "ok"])
            self.assertEqual(self.name_field(tab_id), "After the waits")

            # Uncaught, the same wait ends the script as before.
            uncaught = await session.call_tool("browser_run_python", {
                "code": "print('progress')\ntab.wait_for(text='Never shown', timeout=300)\ntab.fill('#name', 'must not run')"})
            self.assertTrue(uncaught.isError, uncaught)
            payload = json.loads(uncaught.content[0].text)
            self.assertEqual(payload["code"], "script_operation_failed")
            self.assertIn("Timed out after 300ms", payload["failed_operation"]["error"])
            self.assertEqual((payload["output"], [entry["status"] for entry in payload["operations"]]),
                             ("progress\n", ["failed"]))

            # A failed write stops the script even when it is caught.
            code = ("tab.fill('#name', 'Before the write')\nprint('progress')\n"
                    "try:\n    tab.click('#missing')\n"
                    "except BaseException:\n    tab.fill('#name', 'must not run')\n")
            stopped = await session.call_tool("browser_run_python", {"code": code})
            self.assertTrue(stopped.isError, stopped)
            payload = json.loads(stopped.content[0].text)
            self.assertEqual((payload["code"], payload["failed_operation"]["code"]),
                             ("script_operation_failed", "not_found"))
            self.assertEqual((payload["output"], [entry["status"] for entry in payload["operations"]]),
                             ("progress\n", ["ok", "failed"]))
            self.assertEqual(self.name_field(tab_id), "Before the write")

            # So does the bridge's policy refusal of a read, which has no code.
            self.call("/api/set-policy", {"deny": ["blocked\\.test"]})
            code = ("try:\n"
                    f"    browser.request('/api/query', {{'tabId': {tab_id}, 'selector': 'h1', 'url': 'https://blocked.test/'}})\n"
                    "except BaseException:\n    tab.fill('#name', 'must not run')\n")
            refused = await session.call_tool("browser_run_python", {"code": code})
            self.assertTrue(refused.isError, refused)
            payload = json.loads(refused.content[0].text)
            self.assertEqual(payload["code"], "script_operation_failed")
            self.assertIn("URL denied by policy", payload["failed_operation"]["error"])
            self.assertEqual([entry["status"] for entry in payload["operations"]], ["failed"])
            self.assertEqual(self.name_field(tab_id), "Before the write")
        try:
            self.run_scenario(run)
        finally:
            self.call("/api/set-policy", {"deny": []})

    def test_reads_over_the_output_limit_are_cut_and_marked_not_failed(self):
        # #570: a page whose text is over the 16 KB output limit.
        long_site = harness.FixtureServer(_LongSite)
        self.addCleanup(long_site.stop)

        async def run(session, tab_id):
            async def python(code):
                reply = await session.call_tool("browser_run_python", {"code": code})
                self.assertFalse(reply.isError, reply)
                payload = json.loads(reply.content[0].text)
                self.assertIs(payload["ok"], True)
                self.assertIs(payload["truncated"], True, payload)
                self.assertGreater(payload["dropped_bytes"], 0)
                self.assertIn("not page-wide absence", payload["hint"])
                shown = len(payload["output"].encode())
                if "result" in payload:
                    shown += len(json.dumps(payload["result"], ensure_ascii=False).encode())
                self.assertGreater(shown, 15000, "usable content")
                self.assertLessEqual(shown, 16000)
                return payload

            selected = await python(f"tab.navigate({long_site.origin!r})\n"
                                    "tab.fill('#draft', 'keep this draft')\nresult = tab.text()")
            self.assertEqual([entry["status"] for entry in selected["operations"]], ["ok", "ok", "ok"])
            self.assertTrue(selected["result"].startswith("배송과 반품 정책 안내 항목 0 참고 자료입니다. "), selected["result"][:80])
            # text() takes no target: the bridge-cap warning names reads that do.
            self.assertIn("tab.snapshot(target=..., text=True) or tab.query(...)", selected["output"])
            printed = await python("print(tab.text())")
            self.assertIn(selected["result"][:5000], printed["output"], "print and result show the same start")
            snapshot = await python("print(tab.snapshot())\ntab.fill('#draft', 'after the read')")
            self.assertRegex(snapshot["output"], r"\[ref=e\d+\]")
            self.assertEqual([entry["status"] for entry in snapshot["operations"]], ["ok", "ok"])
            wrapped = await python("result = {'scope': 'page', 'evidence': tab.snapshot()}")
            self.assertTrue(wrapped["result"].startswith('{"scope": "page", "evidence": "'), wrapped["result"][:80])
            fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("id") == "draft"), "after the read")
        self.run_scenario(run)

    def test_a_read_loop_over_the_call_limit_prints_what_it_collected(self):
        # #618: wap-144/wap-233 looped navigate + text and lost every page at the 101st call.
        async def run(session, tab_id):
            code = (f"tab.navigate({self.nav.origin + '/a'!r})\nrows = []\ntry:\n    while True:\n"
                    "        try:\n            rows.append(tab.text()[:6])\n        except KitsuneError:\n"
                    "            continue\nfinally:\n    print(len(rows), set(rows), browser.calls_left)")
            reply = await session.call_tool("browser_run_python", {"code": code, "timeout": 60})
            self.assertTrue(reply.isError)
            payload = json.loads(reply.content[0].text)
            self.assertEqual(payload["code"], "script_operation_limit", payload)
            self.assertEqual(payload["output"], "99 {'Bookma'} 0\n")
            self.assertIn("Nothing was written", payload["hint"])
            # #617: summarized, not 100 entries.
            self.assertEqual((payload["operations"]["count"], payload["operations"]["by_route"]),
                             (100, {"/api/navigate": 1, "/api/page-text": 99}))
            self.assertLess(len(json.dumps(payload["operations"])), 500)
        self.run_scenario(run)

    def test_a_navigation_cut_by_the_deadline_is_a_timeout_and_the_script_still_prints(self):
        # #618: wap-247's deadline came during a navigate; everything read before it was lost.
        async def run(session, tab_id):
            code = (f"tab.navigate({self.nav.origin + '/a'!r})\nrows = []\ntry:\n    rows.append(tab.text()[:9])\n"
                    f"    tab.navigate({self.nav.origin + '/slow'!r})\nfinally:\n    print(rows)")
            reply = await session.call_tool("browser_run_python", {"code": code, "timeout": 2})
            self.assertTrue(reply.isError)
            payload = json.loads(reply.content[0].text)
            self.assertEqual((payload["code"], payload["output"]), ("script_timeout", "['Bookmarks']\n"), payload)
            self.assertEqual([entry["status"] for entry in payload["operations"]], ["ok", "ok", "unknown"])
            self.assertNotIn("failed_operation", payload)
            self.assertIn("Nothing was written", payload["hint"])
        self.run_scenario(run)

    def test_approval_stop_keeps_the_draft_and_does_not_offer_script_replay(self):
        self.call("/api/set-policy", {"ask": True})

        async def run(session, tab_id):
            code = (f"tab = browser.tab({tab_id})\n"
                    "tab.fill('#name', 'Keep this draft')\n"
                    "tab.submit_form('#order')\n"
                    "tab.fill('#name', 'must not run')")
            stopped = await session.call_tool("browser_run_python", {"code": code})
            self.assertTrue(stopped.isError, stopped)
            result = json.loads(stopped.content[0].text)
            self.assertEqual(result["failed_operation"]["code"], "needs_user")
            self.assertNotIn("confirmToken", result["failed_operation"]["state"])
            self.assertNotIn("resume_token", result)
            self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
            fields = self.call("/api/forms", {"tabId": tab_id})["fields"]
            self.assertEqual(next(field["value"] for field in fields if field.get("name") == "name"), "Keep this draft")
            self.assertEqual(self.call("/api/page-info", {"tabId": tab_id})["title"], "Python API")
            self.assertEqual(result["failed_operation"]["recovery_tool"], "browser_submit_form")
            described = await session.call_tool("browser_tools_call", {"name": "browser_submit_form"})
            self.assertIn("inputSchema", json.loads(described.content[0].text))
            arguments = {"selector": "#order", "tab_id": tab_id}
            held = await session.call_tool("browser_tools_call", {
                "name": "browser_submit_form", "arguments": arguments})
            self.assertTrue(held.isError, held)
            token = json.loads(held.content[0].text)["state"]["confirmToken"]
            # Disposable fixture: exercise the non-eliciting client's explicit approval step.
            submitted = await session.call_tool("browser_tools_call", {
                "name": "browser_submit_form", "arguments": {**arguments, "confirm_token": token}})
            self.assertFalse(submitted.isError, submitted)
            done = await session.call_tool("browser_run_python", {
                "code": "tab.wait_for(title='Done'); result = tab.info()['url']"})
            self.assertFalse(done.isError, done)
            self.assertIn("name=Keep+this+draft", json.loads(done.content[0].text)["result"])
        try:
            self.run_scenario(run)
        finally:
            self.call("/api/set-policy", {"ask": False})

    # -- #572: a stale link ref after an in-script navigation ----------------

    async def page_a_refs(self, session, tab_id):
        """Open page A in the tab; the refs its snapshot lists, by `role "name"`."""
        read = await session.call_tool("browser_run_python", {
            "code": f"tab = browser.tab({tab_id}); tab.navigate({self.nav.origin + '/a'!r}); result = tab.snapshot()"})
        self.assertFalse(read.isError, read)
        return {label: ref for label, ref in re.findall(r'- (\w+ "[^"]*") \[ref=(e\d+)\]',
                                                         json.loads(read.content[0].text)["result"])}

    async def click_next_then(self, session, tab_id, old):
        """A script that follows page A's Next link to page B, then clicks a ref of page A."""
        refs = await self.page_a_refs(session, tab_id)
        code = (f"tab = browser.tab({tab_id})\ntab.click({refs['link \"Next\"']!r})\n"
                f"result = tab.click({refs[old]!r})")
        ran = await session.call_tool("browser_run_python", {"code": code})
        return refs[old], json.loads(ran.content[0].text)

    def title(self, tab_id):
        return self.call("/api/page-info", {"tabId": tab_id})["title"]

    def assert_stale(self, result, old, same_role=None):
        """The script stopped on the old ref as before #572: stale_ref, nothing followed."""
        self.assertEqual(result["code"], "script_operation_failed", result)
        failed = result["failed_operation"]
        self.assertEqual((failed["code"], failed["state"]["reason"], failed["state"]["ref"]),
                         ("stale_ref", "other_document", old), failed)
        self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
        self.assertNotIn("remapped", json.dumps(result))
        if same_role is None:
            self.assertNotIn("same_element", failed["state"])
        else:
            self.assertEqual(sorted(failed["state"]["same_element"]), ["name", "ref", "role"])
            self.assertEqual(failed["state"]["same_element"]["role"], same_role)

    def test_stale_ref_of_a_shared_nav_link_is_followed_and_reported(self):
        async def run(session, tab_id):
            old, result = await self.click_next_then(session, tab_id, 'link "Bookmarks"')
            self.assertTrue(result.get("ok"), result)
            remapped = result["result"]["remapped"]
            self.assertEqual({**remapped, "to": None}, {"from": old, "to": None, "role": "link", "name": "Bookmarks"})
            self.assertRegex(remapped["to"], r"^e\d+$")
            self.assertNotEqual(remapped["to"], old)
            self.assertEqual(result["operations"], [
                {"route": "/api/click", "status": "ok", "tab_id": tab_id},
                {"route": "/api/click", "status": "ok", "tab_id": tab_id, "remapped": remapped}])
            self.assertEqual(self.title(tab_id), "Bookmarks", "the link of page B was clicked")
            self.assertNotIn("follow", json.dumps(result["result"]))
        self.run_scenario(run)

    def test_stale_ref_without_one_same_link_to_the_same_destination_still_fails(self):
        async def run(session, tab_id):
            # No element with that name, two of them, a button with a lone namesake, and a link
            # with that name that leads elsewhere: nothing on page B is clicked.
            for old, same_role in (('link "Only here"', None), ('link "Twin"', None),
                                   ('button "Go"', "button"), ('link "Moved"', "link")):
                with self.subTest(old=old):
                    ref, result = await self.click_next_then(session, tab_id, old)
                    self.assert_stale(result, ref, same_role)
                    self.assertEqual(self.title(tab_id), "Page B")
        self.run_scenario(run)

    def test_ref_from_another_tab_is_not_followed(self):
        async def run(session, tab_id):
            refs = await self.page_a_refs(session, tab_id)
            code = (f"other = browser.new_tab({self.nav.origin + '/b'!r})\nprint(other.id)\n"
                    f"other.click({refs['link \"Bookmarks\"']!r})")
            result = json.loads((await session.call_tool("browser_run_python", {"code": code})).content[0].text)
            other = int(result["output"])
            self.addCleanup(self.call, "/api/close-tab", {"tabId": other})
            failed = result["failed_operation"]
            self.assertEqual((failed["code"], failed["state"]["reason"]), ("stale_ref", "other_document"), failed)
            self.assertEqual(sorted(failed["state"]["same_element"]), ["name", "ref", "role"])
            self.assertNotIn("remapped", json.dumps(result))
            self.assertEqual((self.title(other), self.title(tab_id)), ("Page B", "Page A"))
        self.run_scenario(run)

    def test_stale_ref_on_a_tab_the_client_does_not_own_is_not_followed(self):
        foreign = self.open_tab(self.nav.origin + "/a")

        async def run(session, tab_id):
            old, result = await self.click_next_then(session, foreign, 'link "Bookmarks"')
            self.assert_stale(result, old, "link")
            self.assertEqual(self.title(foreign), "Page B")
        # The permission that lets a script write the tab does not make the tab the agent's.
        self.run_scenario(run, KITSUNE_USER_TABS="writable")

    def test_direct_tool_with_the_same_stale_ref_fails_as_before(self):
        async def run(session, tab_id):
            refs = await self.page_a_refs(session, tab_id)
            old = refs['link "Bookmarks"']
            clicked = await session.call_tool("browser_tools_call", {
                "name": "browser_click", "arguments": {"target": refs['link "Next"'], "element": "Next"}})
            self.assertFalse(clicked.isError, clicked)
            stale = await session.call_tool("browser_tools_call", {
                "name": "browser_click", "arguments": {"target": old, "element": "Bookmarks"}})
            self.assertTrue(stale.isError, stale)
            failed = json.loads(stale.content[0].text)
            self.assertEqual((failed["code"], failed["state"]["reason"]), ("stale_ref", "other_document"), failed)
            same = failed["state"]["same_element"]
            self.assertEqual({**same, "ref": None}, {"ref": None, "role": "link", "name": "Bookmarks"})
            self.assertIn(f'The same link "Bookmarks" here is {same["ref"]}; retry with it.', failed["hint"])
            self.assertNotIn("remapped", failed)
            self.assertEqual(self.title(tab_id), "Page B")
        self.run_scenario(run)

    def test_ask_before_act_still_holds_a_stale_ref_instead_of_following_it(self):
        self.call("/api/set-policy", {"ask": True})

        async def run(session, tab_id):
            old, result = await self.click_next_then(session, tab_id, 'link "Bookmarks"')
            self.assertEqual(result["code"], "script_operation_failed", result)
            failed = result["failed_operation"]
            self.assertEqual((failed["code"], failed["state"]["detail"], failed["state"]["params"]["ref"]),
                             ("needs_user", "confirmation_required", old), failed)
            self.assertNotIn("remapped", json.dumps(result))
            self.assertEqual(self.title(tab_id), "Page B")
        try:
            self.run_scenario(run)
        finally:
            self.call("/api/set-policy", {"ask": False})
