"""FlatBroker tabs, navigation, read, find and page actions in a real private browser.

Opt-in, built on tests/real_browser_harness.py (temporary add-on, not the signed build).
The broker is a trusted host object here: nothing is reached through MCP or a worker.
What the browser did is read through the harness's own bridge requests, not the broker's result.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import re
import time
from unittest.mock import patch

from kitsune import bridge_client
from kitsune.flat_broker import FlatBroker
from kitsune.flat_session import CallerAuthority
from kitsune.task_modes import OWNER_HEADER
import real_browser_harness as harness
from test_real_browser_shared_browser import USER_NAVIGATES


FORM = """<!doctype html><title>Orders</title><h1>Orders</h1>
<p>Static prose mentioning [ref=e424242] and the Save control.</p>
<a href="/one?token=secret123&q=1">Next page</a>
<form><label>Name <input id="name" value="Ada"></label>
<label>Password <input type="password" value="hunter2"></label>
<button type="button" id="save">Save</button></form>
<ul>""" + "".join(f"<li><a href='/item/{i}'>Item {i}</a></li>" for i in range(12)) + "</ul>"


# Every control records what reached it, so effects are read back from the page itself.
ACTIONS = """<!doctype html><title>Actions</title><h1>Actions</h1>
<button id="inc" onclick="log('click')">Increment</button>
<button id="ctx" oncontextmenu="log('context'); return false">Context</button>
<button id="dbl" ondblclick="log('dbl')">Double</button>
<input id="name" aria-label="Name" value="old">
<input id="pw" aria-label="Secret" type="password" value="">
<select id="country" aria-label="Country" onchange="log('country=' + this.value)">
<option value="kr">Korea</option><option value="jp">Japan</option></select>
<label><input type="checkbox" id="agree" onchange="log('agree=' + this.checked)"> Agree</label>
<input id="keys" aria-label="Keys" onkeydown="log('key=' + (event.ctrlKey ? 'ctrl+' : '') + event.key)">
<div id="hover" role="button" tabindex="0" onmouseover="log('hover')">Hover me</div>
<div id="src" draggable="true">Drag me</div>
<div id="dst" ondragover="event.preventDefault()" ondrop="log('drop')">Drop here</div>
<a id="blank" href="/opened" target="_blank">Open in a new tab</a>
<div style="height: 4000px"></div>
<pre id="log" hidden></pre>
<script>function log(entry) { document.getElementById('log').textContent += entry + '\\n'; }</script>"""
# The content script cannot see the page's own variables, so the log is an element.
STATE = ("JSON.stringify({events: document.getElementById('log').textContent.split('\\n').filter(Boolean), "
         "name: document.getElementById('name').value, "
         "pw: document.getElementById('pw').value, country: document.getElementById('country').value, "
         "agree: document.getElementById('agree').checked, y: Math.round(window.scrollY)})")


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/") or "root"
        body = {"form": FORM, "actions": ACTIONS}.get(name, f"<title>{name}</title><h1>{name}</h1>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserFlatBrokerTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()

    def setUp(self):
        for name, value in (("BRIDGE", f"http://127.0.0.1:{self.bridge.http_port}"), ("_headers", self.bridge.auth)):
            patcher = patch.object(bridge_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for_url(self, tab_id, url):
        deadline = time.monotonic() + 15
        while self.native().get(tab_id, {}).get("url") != url:
            self.assertLess(time.monotonic(), deadline, f"tab {tab_id} did not reach {url}: {self.native()}")
            time.sleep(0.1)

    def assert_failed(self, reply, code, outcome):
        self.assertIs(reply.result["ok"], False, reply.result)
        self.assertEqual((reply.result["error"]["code"], reply.result["outcome"]), (code, outcome), reply.result)

    def test_tabs_and_navigation_through_the_broker(self):
        asyncio.run(self.scenario())

    def test_read_and_find_through_the_broker(self):
        asyncio.run(self.read_scenario())

    def test_page_actions_through_the_broker(self):
        asyncio.run(self.action_scenario())

    def test_a_ref_spelled_as_a_selector_is_not_a_way_around_the_ref_registry(self):
        asyncio.run(self.ref_selector_scenario())

    async def ref_selector_scenario(self):
        # The extension reads a selector that starts with a ref as that ref. A session that was
        # never given the ref must not reach its element by spelling it as a selector.
        owner = "e2e-flat-refsel"
        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-refsel-a", owner, "act"))
        other = await FlatBroker.connect(CallerAuthority("e2e-flat-refsel-b", owner, "act"))
        before_tabs = set(self.native())
        opened = await reader.call("browser_tabs", {"action": "open", "url": self.page.origin + "/actions"})
        native_id = (set(self.native()) - before_tabs).pop()
        content = (await reader.call("browser_read", {"tab_id": opened.result["tab_id"]})).result["data"]["content"]
        ref = re.search(r'button "Increment" \[ref=(e\d+)\]', content)[1]
        tab = next(t["tab_id"] for t in (await other.call("browser_tabs", {})).result["data"]["tabs"]
                   if t["url"].endswith("/actions"))
        self.assert_failed(await other.call("browser_click", {"tab_id": tab, "target": {"ref": ref}}),
                           "stale_ref", "not_dispatched")
        for selector in (ref, f"  {ref} ", f"\ufeff{ref}", f"{ref} >> xpath=.", f"{ref}>>css=*"):
            with self.subTest(selector=selector):
                reply = await other.call("browser_click", {"tab_id": tab, "target": {"selector": selector}})
                self.assert_failed(reply, "invalid_arguments", "not_dispatched")
                self.assertIsNone(reply.receipt)
        events = json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])["events"]
        self.assertEqual(events, [], "no click reached the page")
        # An ordinary selector still works for that session, and so does the ref for its own.
        self.assertTrue((await other.call("browser_click", {"tab_id": tab, "target": {"selector": "#inc"}})).result["ok"])
        self.assertTrue((await reader.call("browser_click", {"tab_id": opened.result["tab_id"],
                                                             "target": {"ref": ref}})).result["ok"])
        self.assertTrue((await reader.call("browser_tabs", {"action": "close",
                                                            "tab_id": opened.result["tab_id"]})).result["ok"])

    def as_owner(self, owner, path, body):
        """A legacy request of the same owner: the harness's own client may not write the broker's tabs."""
        status, _, text = harness._request(
            self.bridge.http_port, "POST", path,
            {**self.bridge.auth(), OWNER_HEADER: owner, "Content-Type": "application/json"},
            self.bridge.host, json.dumps(body), timeout=30)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    async def action_scenario(self):
        owner = "e2e-flat-actor"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-act", owner, "act"))
        before_tabs = set(self.native())
        opened = await broker.call("browser_tabs", {"action": "open", "url": self.page.origin + "/actions"})
        tab = opened.result["tab_id"]
        native_id = (set(self.native()) - before_tabs).pop()

        def state():
            return json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])

        async def act(name, **arguments):
            reply = await broker.call(name, {"tab_id": tab, **arguments})
            self.assertEqual((reply.result["ok"], reply.result["outcome"]), (True, "dispatched"), reply.result)
            self.assertEqual(reply.receipt["commands"][-1]["tab_id"], native_id)
            return reply

        read = await broker.call("browser_read", {"tab_id": tab})
        refs = dict(re.findall(r'- (\w+ "[^"]*") \[ref=(e\d+)\]', read.result["data"]["content"]))
        self.assertEqual(state(), {"events": [], "name": "old", "pw": "", "country": "kr", "agree": False, "y": 0})

        await act("browser_click", target={"ref": refs['button "Increment"']})
        await act("browser_click", target={"selector": "#ctx"}, button="right")
        await act("browser_click", target={"ref": refs['button "Double"']}, click_count=2)
        self.assertEqual(state()["events"], ["click", "context", "dbl"])
        await act("browser_input", target={"ref": refs['textbox "Name"']}, text="Ada")
        self.assertEqual(state()["name"], "Ada")
        await act("browser_input", target={"selector": "#name"}, text=" L", mode="type")
        self.assertEqual(state()["name"], "Ada L")
        await act("browser_select", target={"selector": "#country"}, values=["jp"])
        await act("browser_check", target={"selector": "#agree"})
        await act("browser_check", target={"selector": "#agree"}, checked=False)
        await act("browser_press", target={"selector": "#keys"}, key="Enter", modifiers=["Control"])
        await act("browser_hover", target={"ref": refs['button "Hover me"']})
        await act("browser_drag", source={"selector": "#src"}, destination={"selector": "#dst"})
        await act("browser_scroll", amount_px=400)
        after = state()
        self.assertEqual(after["events"][3:], ["country=jp", "agree=true", "agree=false", "key=ctrl+Enter", "hover", "drop"])
        self.assertEqual((after["country"], after["agree"], after["y"]), ("jp", False, 400))
        await act("browser_scroll", direction="top")
        self.assertEqual(state()["y"], 0)

        # Refusals change nothing: an ambiguous or missing target, an unissued ref, the user's tab.
        user = next(t["tab_id"] for t in (await broker.call("browser_tabs", {})).result["data"]["tabs"] if t["active"])
        for name, arguments, code in (
                ("browser_click", {"tab_id": tab, "target": {"selector": "button"}}, "ambiguous_target"),
                ("browser_click", {"tab_id": tab, "target": {"selector": "#missing"}, "timeout_ms": 300}, "not_found"),
                ("browser_input", {"tab_id": tab, "target": {"selector": "#missing"}, "text": "x", "timeout_ms": 300},
                 "not_found"),
                ("browser_click", {"tab_id": tab, "target": {"ref": "e424242"}}, "stale_ref"),
                ("browser_select", {"tab_id": tab, "target": {"selector": "#country"}, "values": ["Korea"],
                                    "by": "label"}, "unsupported_variant"),
                ("browser_click", {"tab_id": user, "target": {"selector": "body"}}, "user_tab_read_only"),
                ("browser_scroll", {"tab_id": user}, "user_tab_read_only")):
            with self.subTest(name=name, arguments=arguments):
                self.assert_failed(await broker.call(name, arguments), code, "not_dispatched")
        self.assertEqual(state(), {**after, "y": 0})

        # A draft caller of the same owner may fill an ordinary field, never a sensitive one, and may not click.
        drafter = await FlatBroker.connect(CallerAuthority("e2e-flat-draft", owner, "draft"))
        draft_tab = next(t["tab_id"] for t in (await drafter.call("browser_tabs", {})).result["data"]["tabs"]
                         if t["url"].endswith("/actions"))
        filled = await drafter.call("browser_input", {"tab_id": draft_tab, "target": {"selector": "#name"}, "text": "Draft"})
        self.assertEqual((filled.result["ok"], filled.result["outcome"]), (True, "dispatched"), filled.result)
        self.assertEqual(filled.receipt["commands"][-1]["action"], "fill")
        secret = await drafter.call("browser_input", {"tab_id": draft_tab, "target": {"selector": "#pw"}, "text": "hunter2"})
        self.assert_failed(secret, "mode_restricted", "not_dispatched")
        self.assertNotIn("fill", [command["action"] for command in secret.receipt["commands"]])
        self.assert_failed(await drafter.call("browser_click", {"tab_id": draft_tab, "target": {"selector": "#inc"}}),
                           "mode_restricted", "not_dispatched")
        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-ro2", owner, "read"))
        read_tab = next(t["tab_id"] for t in (await reader.call("browser_tabs", {})).result["data"]["tabs"]
                        if t["url"].endswith("/actions"))
        self.assert_failed(await reader.call("browser_input", {"tab_id": read_tab, "target": {"selector": "#name"},
                                                               "text": "x"}), "mode_restricted", "not_dispatched")
        scrolled = await reader.call("browser_scroll", {"tab_id": read_tab, "amount_px": 250})
        self.assertEqual((scrolled.result["ok"], scrolled.result["outcome"]), (True, "dispatched"), scrolled.result)
        self.assertEqual(state(), {**after, "name": "Draft", "y": 250})

        # A target=_blank link: the tab the click opened gets a handle of this session.
        link = await broker.call("browser_click", {"tab_id": tab, "target": {"selector": "#blank"}})
        self.assertEqual((link.result["ok"], link.result["outcome"]), (True, "dispatched"), link.result)
        popups = link.result["data"].get("opened_tabs", [])
        self.assertEqual(len(popups), 1, link.result)
        popup = await broker.call("browser_read", {"tab_id": popups[0]["tab_id"], "format": "text"})
        self.assertEqual(popup.result["data"]["url"], self.page.origin + "/opened", popup.result)
        for handle in (popups[0]["tab_id"], tab):
            self.assertTrue((await broker.call("browser_tabs", {"action": "close", "tab_id": handle})).result["ok"])

    async def read_scenario(self):
        form = self.page.origin + "/form"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-read", "e2e-flat-reader", "act"))
        other = await FlatBroker.connect(CallerAuthority("e2e-flat-other", "e2e-flat-reader", "act"))
        opened = await broker.call("browser_tabs", {"action": "open", "url": form})
        tab = opened.result["tab_id"]
        native_id = next(i for i, t in self.native().items() if t["url"] == form)

        async def ref_refused(caller, handle, ref):
            reply = await caller.call("browser_read", {"tab_id": handle, "target": {"ref": ref}})
            return (reply.result["ok"], reply.result.get("error", {}).get("code"), reply.result["outcome"])

        read = await broker.call("browser_read", {"tab_id": tab})
        self.assertEqual((read.result["ok"], read.result["outcome"], read.result["truncated"]),
                         (True, "observed", False), read.result)
        content = read.result["data"]["content"]
        refs = dict(re.findall(r'- (\w+ "[^"]*") \[ref=(e\d+)\]', content))
        for line in ('heading "Orders"', 'textbox "Name"', 'textbox "Password"', 'button "Save"', 'link "Item 11"'):
            self.assertIn(line, refs)
        # The same redaction as the existing snapshot: no password value, no URL secret, no prose.
        self.assertIn('/url: "/one?token=[redacted]&q=1"', content)
        for absent in ("hunter2", "secret123", "Static prose"):
            self.assertNotIn(absent, content)
        self.assertEqual(read.receipt["commands"], [
            {"sequence": 1, "action": "getSnapshot", "state": "replied", "finished": True, "tab_id": native_id}])

        text = await broker.call("browser_read", {"tab_id": tab, "format": "text"})
        data = text.result["data"]
        self.assertEqual((data["url"], data["title"], data["format"]), (form, "Orders", "text"))
        self.assertIn('- text "Static prose mentioning [ref=e424242] and the Save control."', data["content"])
        self.assertNotIn("hunter2", data["content"])
        small = await broker.call("browser_read", {"tab_id": tab, "max_chars": 200})
        self.assertEqual((small.result["ok"], small.result["truncated"]), (True, True))
        self.assertLessEqual(len(small.result["data"]["content"]), 200)

        # A ref scopes a later read for the session that received it, and for no other.
        scoped = await broker.call("browser_read", {"tab_id": tab, "target": {"ref": refs['textbox "Name"']}})
        self.assertEqual(scoped.result["data"]["content"].count("[ref="), 1, scoped.result)
        self.assertEqual(await ref_refused(broker, tab, "e424242"), (False, "stale_ref", "not_dispatched"))
        other_tab = next(t["tab_id"] for t in (await other.call("browser_tabs", {})).result["data"]["tabs"]
                         if t["url"] == form)
        self.assertEqual(await ref_refused(other, other_tab, refs['button "Save"']),
                         (False, "stale_ref", "not_dispatched"))
        missing = await broker.call("browser_read", {"tab_id": tab, "target": {"selector": "#missing"}})
        self.assert_failed(missing, "not_found", "dispatched")

        found = await broker.call("browser_find", {"tab_id": tab, "text": "ITEM 1"})
        self.assertEqual((found.result["ok"], found.result["outcome"], found.result["data"]["count"]),
                         (True, "observed", 3), found.result)
        self.assertEqual([m["line"] for m in found.result["data"]["matches"]],
                         ['- %s [ref=%s]' % (link, refs[link]) for link in
                          ('link "Item 1"', 'link "Item 10"', 'link "Item 11"')])
        prose = await broker.call("browser_find", {"tab_id": tab, "text": "static prose"})
        self.assertEqual(prose.result["data"]["count"], 1)
        self.assertNotIn("ref", prose.result["data"]["matches"][0])
        for text_, count in (("hunter2", 0), ("secret123", 0), ("item", 24)):
            reply = await broker.call("browser_find", {"tab_id": tab, "text": text_, "limit": 3})
            self.assertEqual((reply.result["data"]["count"], reply.result["truncated"]), (count, count > 3))

        # Reading needs no ownership. The user's tab starts blank, with no page to read; once the
        # user loads a page there, it is readable, and writes to it are still refused.
        user = next(t for t in (await broker.call("browser_tabs", {})).result["data"]["tabs"] if t["active"])
        self.assertEqual((user["owned"], user["url"]), (False, "about:blank"))
        blank = await broker.call("browser_read", {"tab_id": user["tab_id"]})
        self.assert_failed(blank, "content_unavailable", "dispatched")
        self.assertIs(blank.result["error"]["retryable"], True)
        user_native = next(i for i, t in self.native().items() if t["active"])
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            debugger.evaluate_in_parent(USER_NAVIGATES % json.dumps(self.page.origin + "/user"))
        finally:
            debugger.close()
        self.wait_for_url(user_native, self.page.origin + "/user")
        deadline = time.monotonic() + 15
        while True:
            theirs = await broker.call("browser_read", {"tab_id": user["tab_id"]})
            if theirs.result["ok"] or time.monotonic() > deadline:
                break
            time.sleep(0.2)  # the load is still completing
        self.assertIn('heading "user"', theirs.result.get("data", {}).get("content", ""), theirs.result)
        self.assert_failed(await broker.call("browser_navigate", {"tab_id": user["tab_id"], "url": form}),
                           "user_tab_read_only", "not_dispatched")
        self.assertEqual(self.native()[user_native]["url"], self.page.origin + "/user")

        # The tab loads another document without the broker. A client without the owner's id may
        # not navigate it; a legacy client of the same owner may, and the extension then reports
        # the stale ref.
        save = refs['button "Save"']
        self.assertEqual(self.call("/api/navigate", {"tabId": native_id, "url": form}).get("code"),
                         "user_tab_read_only")
        status, _, body = harness._request(
            self.bridge.http_port, "POST", "/api/navigate",
            {**self.bridge.auth(), OWNER_HEADER: "e2e-flat-reader", "Content-Type": "application/json"},
            self.bridge.host, json.dumps({"tabId": native_id, "url": form}), timeout=30)
        self.assertEqual((status, json.loads(body).get("ok")), (200, True), body)
        self.assertEqual(await ref_refused(broker, tab, save), (False, "stale_ref", "dispatched"))
        self.assertEqual(await ref_refused(broker, tab, save), (False, "stale_ref", "not_dispatched"))
        # A navigation through the broker ends every ref of the tab before anything is sent.
        again = await broker.call("browser_read", {"tab_id": tab})
        name = re.search(r'textbox "Name" \[ref=(e\d+)\]', again.result["data"]["content"])[1]
        self.assertNotEqual(name, refs['textbox "Name"'])
        self.assertTrue((await broker.call("browser_navigate", {"tab_id": tab, "action": "reload"})).result["ok"])
        self.assertEqual(await ref_refused(broker, tab, name), (False, "stale_ref", "not_dispatched"))

        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-ro", "e2e-flat-reader", "read"))
        handle = next(t["tab_id"] for t in (await reader.call("browser_tabs", {})).result["data"]["tabs"]
                      if t["url"] == form)
        self.assertTrue((await reader.call("browser_read", {"tab_id": handle})).result["ok"])
        self.assertTrue((await reader.call("browser_find", {"tab_id": handle, "text": "save"})).result["ok"])
        self.assertTrue((await broker.call("browser_tabs", {"action": "close", "tab_id": tab})).result["ok"])

    async def scenario(self):
        one, two = self.page.origin + "/one", self.page.origin + "/two"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat", "e2e-flat-owner", "act"))
        before = self.native()
        listed = await broker.call("browser_tabs", {})
        self.assertEqual((listed.result["ok"], listed.result["outcome"]), (True, "observed"), listed.result)
        self.assertEqual(listed.result["data"]["tab_count"], len(before))
        self.assertFalse(any(t["owned"] for t in listed.result["data"]["tabs"]))
        user = next(t["tab_id"] for t in listed.result["data"]["tabs"] if t["active"])
        user_native = next(i for i, t in before.items() if t["active"])

        opened = await broker.call("browser_tabs", {"action": "open", "url": one})
        self.assertEqual((opened.result["ok"], opened.result["outcome"]), (True, "observed"), opened.result)
        handle = opened.result["tab_id"]
        self.assertEqual(opened.result["data"], {"tab_id": handle, "url": one, "status": "complete"})
        created = set(self.native()) - set(before)
        self.assertEqual(len(created), 1)
        native_id = created.pop()
        self.assertEqual(self.native()[native_id]["url"], one)
        self.assertIs(self.native()[user_native]["active"], True, "the open stayed in the background")
        relisted = {t["tab_id"]: t for t in (await broker.call("browser_tabs", {})).result["data"]["tabs"]}
        self.assertEqual((relisted[handle]["url"], relisted[handle]["owned"]), (one, True))
        self.assertIn(user, relisted, "a live tab keeps its handle across listings")

        for arguments, landed in (({"url": two}, two), ({"action": "back"}, one), ({"action": "forward"}, two)):
            with self.subTest(arguments=arguments):
                reply = await broker.call("browser_navigate", {"tab_id": handle, **arguments})
                self.assertEqual((reply.result["ok"], reply.result["outcome"]), (True, "dispatched"), reply.result)
                self.assertEqual(reply.receipt["commands"][-1]["tab_id"], native_id)
                self.wait_for_url(native_id, landed)
        reloaded = await broker.call("browser_navigate", {"tab_id": handle, "action": "reload"})
        self.assertEqual((reloaded.result["ok"], reloaded.result["outcome"]), (True, "dispatched"), reloaded.result)
        self.wait_for_url(native_id, two)

        # The user's tab stays read-only for this caller: the extension refuses before any browser call.
        user_url = self.native()[user_native]["url"]
        for name, arguments in (("browser_navigate", {"tab_id": user, "url": one}),
                                ("browser_navigate", {"tab_id": user, "action": "reload"}),
                                ("browser_tabs", {"action": "close", "tab_id": user})):
            with self.subTest(name=name, arguments=arguments):
                self.assert_failed(await broker.call(name, arguments), "user_tab_read_only", "not_dispatched")
        time.sleep(0.5)  # a navigation would have started by now
        self.assertEqual(self.native()[user_native]["url"], user_url)

        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": handle})
        self.assertEqual((closed.result["ok"], closed.result["outcome"]), (True, "dispatched"), closed.result)
        deadline = time.monotonic() + 10
        while native_id in self.native():
            self.assertLess(time.monotonic(), deadline, "the owned tab did not close")
            time.sleep(0.1)
        self.assert_failed(await broker.call("browser_navigate", {"tab_id": handle, "url": one}),
                           "stale_tab_handle", "not_dispatched")

        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-read", "e2e-flat-owner", "read"))
        count = len(self.native())
        self.assert_failed(await reader.call("browser_tabs", {"action": "open", "url": "file:///etc/passwd"}),
                           "mode_restricted", "not_dispatched")
        self.assert_failed(await reader.call("browser_tabs", {"action": "close", "tab_id": "t_forged"}),
                           "mode_restricted", "not_dispatched")
        self.assertEqual(len(self.native()), count)
