"""FlatBroker tabs, navigation, read, find, waits, screenshots and page actions in a real private browser.

Opt-in, built on tests/real_browser_harness.py (temporary add-on, not the signed build).
The broker is a trusted host object here: nothing is reached through MCP or a worker.
What the browser did is read through the harness's own bridge requests, not the broker's result.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import time
import unittest
from unittest.mock import patch

from kitsune import bridge_client
from kitsune.flat_broker import FlatBroker
from kitsune.flat_session import CallerAuthority
from kitsune.script_runner import ScriptRuntime, run_script
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


# The page changes 1.5 s after it loads: text, a new button and the title.
WAITS = """<!doctype html><title>Waiting</title><h1>Waits</h1><p id="status">Loading</p>
<a id="go" href="/one">Go</a>
<script>setTimeout(() => {
  document.getElementById('status').textContent = 'Saved';
  const late = document.createElement('button');
  late.id = 'late'; late.textContent = 'Late'; late.onclick = () => { document.title = 'Clicked'; };
  document.body.appendChild(late);
  document.title = 'Done (1)';
}, 1500);</script>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/") or "root"
        body = {"form": FORM, "actions": ACTIONS, "waits": WAITS}.get(name, f"<title>{name}</title><h1>{name}</h1>").encode()
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

    def test_waits_through_the_broker(self):
        asyncio.run(self.wait_scenario())

    def test_user_help_is_a_stop_without_page_effects_or_approval(self):
        asyncio.run(self.help_scenario())

    async def help_scenario(self):
        owner = "e2e-flat-help"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-help-a", owner, "act"))
        before_tabs = set(self.native())
        opened = await broker.call("browser_tabs", {"action": "open", "url": self.page.origin + "/actions"})
        self.assertTrue(opened.result["ok"], opened.result)
        handle = opened.result["tab_id"]
        native_id, = set(self.native()) - before_tabs
        before = json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])
        tabs_before_help = self.native()
        reply = await broker.call("browser_ask_user", {
            "reason": "other", "message": "Please check this page.", "tab_id": handle})
        self.assert_failed(reply, "needs_user", "not_dispatched")
        self.assertEqual(reply.result["error"]["state"], {
            "reason": "agent_request", "detail": "other", "message": "Please check this page."})
        self.assertEqual(reply.result["data"], {})
        self.assertEqual([command["action"] for command in reply.receipt["commands"]], ["getPageInfo"])
        after = json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])
        self.assertEqual(after, before)
        self.assertEqual({i: (t["url"], t["active"]) for i, t in self.native().items()},
                         {i: (t["url"], t["active"]) for i, t in tabs_before_help.items()})
        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": handle})
        self.assertTrue(closed.result["ok"], closed.result)

    @unittest.skipUnless(os.environ.get("KITSUNE_E2E_SCRIPT_RUNTIME"), "KITSUNE_E2E_SCRIPT_RUNTIME is not set")
    def test_a_delegated_script_drives_the_browser_through_the_broker(self):
        asyncio.run(self.script_scenario())

    async def script_scenario(self):
        owner = "e2e-flat-script"
        runtime = ScriptRuntime.from_manifest(os.environ["KITSUNE_E2E_SCRIPT_RUNTIME"])
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-script-a", owner, "act"))
        listed = await broker.call("browser_tabs", {})
        user = next(t["tab_id"] for t in listed.result["data"]["tabs"] if t["active"])
        user_native = next(i for i, t in self.native().items() if t["active"])
        user_url = self.native()[user_native]["url"]
        before_tabs = set(self.native())
        result = await run_script(broker, runtime, f'''
opened = await browser.new_tab(url={self.page.origin + "/actions"!r})
tab = opened["tab_id"]
page = await browser.read(tab_id=tab)
refs = {{}}
for line in page["data"]["content"].splitlines():
    if "[ref=" in line:
        refs[line.split('"')[1]] = line.split("[ref=")[1].split("]")[0]
await browser.input(tab_id=tab, target={{"ref": refs["Name"]}}, text="Script")
for attempt in range(3):
    await browser.click(tab_id=tab, target={{"ref": refs["Increment"]}})
await browser.select(tab_id=tab, target={{"selector": "#country"}}, values=["jp"])
found = await browser.find(tab_id=tab, text="drop here")
refused = {{}}
for name, call in (("user tab", lambda: browser.navigate(tab_id={user!r}, url={self.page.origin + "/one"!r})),
                   ("ambiguous", lambda: browser.click(tab_id=tab, target={{"selector": "button"}})),
                   ("made-up ref", lambda: browser.click(tab_id=tab, target={{"ref": "e424242"}}))):
    try:
        await call()
        refused[name] = "ok"
    except BrowserOperationError as error:
        refused[name] = [error.result["error"]["code"], error.result["outcome"]]
def reach():
    import socket
    return socket.create_connection(("127.0.0.1", {self.bridge.http_port}), timeout=2)
try:
    reach()
    refused["bridge"] = "REACHED"
except BaseException as error:
    refused["bridge"] = type(error).__name__
shot = await browser.screenshot(tab_id=tab, scope="element", target={{"ref": refs["Increment"]}})
emit_image(shot["data"]["image"])
print("clicked", 3)
emit({{"tab": tab, "matches": found["data"]["count"], "refused": refused, "image": shot["data"]["image"]}})
''')
        self.assertEqual((result.status, result.error, result.stdout), ("completed", None, "clicked 3\n"), result)
        native_id = (set(self.native()) - before_tabs).pop()
        state = json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])
        # What the page itself recorded, read through a separate request.
        self.assertEqual((state["name"], state["events"], state["country"]),
                         ("Script", ["click", "click", "click", "country=jp"], "jp"))
        self.assertEqual(result.emitted["refused"], {
            "user tab": ["user_tab_read_only", "not_dispatched"], "ambiguous": ["ambiguous_target", "not_dispatched"],
            "made-up ref": ["stale_ref", "not_dispatched"], "bridge": result.emitted["refused"]["bridge"]})
        self.assertNotEqual(result.emitted["refused"]["bridge"], "REACHED")
        self.assertEqual(self.native()[user_native]["url"], user_url)
        image, = result.images
        self.assertEqual((image.handle, image.mime_type, image.data[:8]),
                         (result.emitted["image"], "image/png", b"\x89PNG\r\n\x1a\n"))
        # The coordinator's ledger, not the script's output, says what happened.
        self.assertEqual([(e["operation"], e["outcome"]) for e in result.ledger], [
            ("browser_tabs", "observed"), ("browser_read", "observed"), ("browser_input", "dispatched"),
            ("browser_click", "dispatched"), ("browser_click", "dispatched"), ("browser_click", "dispatched"),
            ("browser_select", "dispatched"), ("browser_find", "observed"), ("browser_navigate", "not_dispatched"),
            ("browser_click", "not_dispatched"), ("browser_click", "not_dispatched"), ("browser_screenshot", "observed")])
        self.assertTrue(all(e["receipt"]["commands"][-1]["tab_id"] == native_id for e in result.ledger[2:8]))
        # The handle the script was given works for a direct call afterwards.
        closed = await broker.call("browser_tabs", {"action": "close", "tab_id": result.emitted["tab"]})
        self.assertTrue(closed.result["ok"], closed.result)

    def test_screenshots_through_the_broker(self):
        asyncio.run(self.screenshot_scenario())

    async def screenshot_scenario(self):
        owner = "e2e-flat-shooter"
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-shot", owner, "act"))
        before_tabs = set(self.native())
        opened = await broker.call("browser_tabs", {"action": "open", "url": self.page.origin + "/actions"})
        tab = opened.result["tab_id"]
        native_id = (set(self.native()) - before_tabs).pop()

        def scroll_y():
            return json.loads(self.as_owner(owner, "/api/js", {"tabId": native_id, "code": STATE})["result"])["y"]

        async def shoot(**arguments):
            reply = await broker.call("browser_screenshot", {"tab_id": tab, **arguments})
            self.assertEqual((reply.result["ok"], reply.result["outcome"]), (True, "observed"), reply.result)
            image, = reply.images
            self.assertEqual(reply.result["data"], {"tab_id": tab, "image": image.handle, "mime_type": image.mime_type,
                                                    "bytes": len(image.data)})
            self.assertLessEqual(len(image.data), 8 * 1024 * 1024)
            self.assertNotIn("base64", json.dumps(reply.result))
            return image

        def png_size(image):
            self.assertEqual((image.mime_type, image.data[:8]), ("image/png", b"\x89PNG\r\n\x1a\n"))
            return int.from_bytes(image.data[16:20], "big"), int.from_bytes(image.data[20:24], "big")

        self.assertTrue((await broker.call("browser_scroll", {"tab_id": tab, "amount_px": 300})).result["ok"])
        self.assertEqual(scroll_y(), 300)
        view_w, view_h = png_size(await shoot())
        self.assertGreater(min(view_w, view_h), 100)
        refs = dict(re.findall(r'- (\w+ "[^"]*") \[ref=(e\d+)\]',
                               (await broker.call("browser_read", {"tab_id": tab})).result["data"]["content"]))
        by_ref = png_size(await shoot(scope="element", target={"ref": refs['button "Increment"']}))
        by_selector = png_size(await shoot(scope="element", target={"selector": "#inc"}))
        self.assertEqual(by_ref, by_selector)
        self.assertLess(by_ref[0], view_w)
        self.assertLess(by_ref[1], view_h)
        # The page is far taller than the window: the full-page image is too, whatever its encoding.
        full = await shoot(scope="full_page")
        self.assertIn(full.mime_type, {"image/png", "image/jpeg"})
        if full.mime_type == "image/png":
            self.assertGreater(png_size(full)[1], view_h * 2)
        # Element and full-page captures scroll the page while they work, and put it back.
        self.assertEqual(scroll_y(), 300)

        for arguments, code in (({"scope": "element", "target": {"ref": "e424242"}}, "stale_ref"),
                                ({"scope": "element", "target": {"selector": "#missing"}}, "not_found")):
            with self.subTest(arguments=arguments):
                reply = await broker.call("browser_screenshot", {"tab_id": tab, **arguments})
                self.assert_failed(reply, code, "not_dispatched")
                self.assertEqual(reply.images, ())
        # A full-page capture would scroll the tab the user is looking at: refused, nothing captured.
        user = next(t["tab_id"] for t in (await broker.call("browser_tabs", {})).result["data"]["tabs"] if t["active"])
        refused = await broker.call("browser_screenshot", {"tab_id": user, "scope": "full_page"})
        self.assert_failed(refused, "user_tab_read_only", "not_dispatched")
        self.assertEqual(refused.images, ())
        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-shot-ro", owner, "read"))
        handle = next(t["tab_id"] for t in (await reader.call("browser_tabs", {})).result["data"]["tabs"]
                      if t["url"].endswith("/actions"))
        self.assert_failed(await reader.call("browser_screenshot", {"tab_id": handle}), "mode_restricted", "not_dispatched")
        self.assertEqual(scroll_y(), 300)
        self.assertTrue((await broker.call("browser_tabs", {"action": "close", "tab_id": tab})).result["ok"])

    async def wait_scenario(self):
        broker = await FlatBroker.connect(CallerAuthority("e2e-flat-wait", "e2e-flat-waiter", "act"))
        before_tabs = set(self.native())
        opened = await broker.call("browser_tabs", {"action": "open", "url": self.page.origin + "/waits"})
        tab = opened.result["tab_id"]
        native_id = (set(self.native()) - before_tabs).pop()

        async def wait(condition, timeout_ms=8000, caller=broker, handle=tab):
            return await caller.call("browser_wait", {"tab_id": handle, "condition": condition, "timeout_ms": timeout_ms})

        def assert_met(reply, kind):
            self.assertEqual((reply.result["ok"], reply.result["outcome"], reply.result["data"]["kind"]),
                             (True, "observed", kind), reply.result)

        def assert_unmet(reply):
            self.assertEqual((reply.result["ok"], reply.result["error"]["code"], reply.result["outcome"],
                              reply.result["error"]["retryable"]), (False, "condition_unmet", "observed", True),
                             reply.result)

        # The page still says Loading: the text wait is met by a later read, not by the first.
        saved = await wait({"kind": "text", "value": "Saved"})
        assert_met(saved, "text")
        self.assertNotIn("already_present", saved.result["data"])
        self.assertGreater(len(saved.receipt["commands"]), 1)
        self.assertTrue(all(c["action"] == "waitForText" and c["tab_id"] == native_id for c in saved.receipt["commands"]))
        self.assertIs((await wait({"kind": "text", "value": "Saved"})).result["data"]["already_present"], True)
        assert_met(await wait({"kind": "text_gone", "value": "Loading"}), "text_gone")
        started = time.monotonic()
        assert_unmet(await wait({"kind": "text", "value": "Never shown"}, 700))
        self.assertLess(time.monotonic() - started, 3)

        # Title and URL are literal: a pattern that would match as a regular expression does not.
        assert_met(await wait({"kind": "title", "value": "Done (1)"}), "title")
        assert_unmet(await wait({"kind": "title", "value": "Done .1."}, 700))
        assert_unmet(await wait({"kind": "title", "value": "Done"}, 700))

        # The element a target wait found can be acted on by its ref.
        late = await wait({"kind": "target", "target": {"selector": "#late"}})
        assert_met(late, "target")
        clicked = await broker.call("browser_click", {"tab_id": tab, "target": {"ref": late.result["data"]["ref"]}})
        self.assertTrue(clicked.result["ok"], clicked.result)
        assert_met(await wait({"kind": "title", "value": "Clicked"}), "title")
        assert_unmet(await wait({"kind": "target", "target": {"selector": "#never"}}, 700))

        self.assertTrue((await broker.call("browser_click", {"tab_id": tab, "target": {"selector": "#go"}})).result["ok"])
        assert_met(await wait({"kind": "url", "value": self.page.origin + "/one"}), "url")
        assert_unmet(await wait({"kind": "url", "value": self.page.origin + "/o.e"}, 700))
        assert_unmet(await wait({"kind": "url", "value": self.page.origin + "/"}, 700))
        assert_met(await wait({"kind": "network_idle"}, 8000), "network_idle")
        self.assertEqual(self.native()[native_id]["url"], self.page.origin + "/one")

        # Read mode waits for text only; the user's tab can be watched, not changed.
        reader = await FlatBroker.connect(CallerAuthority("e2e-flat-wait-ro", "e2e-flat-waiter", "read"))
        handle = next(t["tab_id"] for t in (await reader.call("browser_tabs", {})).result["data"]["tabs"]
                      if t["url"].endswith("/one"))
        assert_met(await wait({"kind": "text", "value": "one"}, 3000, reader, handle), "text")
        for condition in ({"kind": "url", "value": self.page.origin + "/one"}, {"kind": "network_idle"},
                          {"kind": "target", "target": {"selector": "h1"}}):
            self.assert_failed(await wait(condition, 700, reader, handle), "mode_restricted", "not_dispatched")
        self.assertTrue((await broker.call("browser_tabs", {"action": "close", "tab_id": tab})).result["ok"])

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
