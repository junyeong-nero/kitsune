"""#395 in a real browser: reads leave a user tab as it was; writes refuse at once, without a prompt.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The user opens
a long page in a tab of their own (from the browser's parent process, as in
test_real_browser_shared_browser), scrolls it and leaves a selection in a focused text field.
The tab's own devtools console sets that state up and then reads the page's scroll position,
focus, selection and events, and the parent process reads the shown tab: these reads scroll,
focus or select nothing, and the explicitly writable opt-out's scroll shows they see a change.

The core tools run through an MCP client session on this process's server (FastMCP validation
and its approval elicitation). Their bridge requests go to the class's bridge as one client
with the read-only user-tab permission and the server's tool-call headers, but not through the
server's BridgeClient (no client id, approver secret or caps headers).
They act on the agent's current tab, or on the shown tab while there is none. browser_tabs
select refuses a user tab, so to read or write a background user tab through them the test makes
that tab current directly; browser_tabs list then marks it current as for any other tab.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import logging
import time
from unittest.mock import patch

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from kitsune import playwright_tools, server, tools
from kitsune.task_modes import OWNER_HEADER, USER_TABS_HEADER
import real_browser_harness as harness

MCP_SESSION_TIMEOUT = 120
# A refusal answers without waiting for anything: no prompt, approval or page wait.
REFUSAL_SECONDS = 5
# Where the user left the long page: scrolled down and right, with "llo w" of the draft selected.
SCROLL_X, SCROLL_Y = 600, 1100
SELECTION = [2, 7, "forward"]

LONG_PAGE = """<!doctype html><meta charset="utf-8"><title>long</title>
<style>body { margin: 0; width: 3000px } section { height: 600px }</style>
<h1>Long page</h1>
<section style="height: 1200px">Introduction</section>
<textarea id="draft" aria-label="Draft" style="margin-left: 700px">hello world</textarea>
%s
<button id="far" style="margin-left: 2500px">Far button</button>""" % "".join(
    f"<section><h2>Section {n}</h2><p>Paragraph {n} of the long page.</p></section>" for n in range(1, 13))

# The user opens url in a tab of their own, shown (or not), as from the address bar.
USER_OPENS_TAB = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const tab = win.gBrowser.addTrustedTab(%s);
  if (%s) win.gBrowser.selectedTab = tab;
  return true;
})()"""
USER_CLOSES_TAB = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  for (const tab of [...win.gBrowser.tabs]) {
    if (tab.linkedBrowser.currentURI.spec === %s) win.gBrowser.removeTab(tab);
  }
  return true;
})()"""
# The tab the user sees, as the extension names it, and whether its window has focus.
SHOWN_TAB = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const extension = WebExtensionPolicy.getByID(%s).extension;
  return {tabId: extension.tabManager.convert(win.gBrowser.selectedTab).id,
          url: win.gBrowser.selectedBrowser.currentURI.spec,
          windowFocused: Services.focus.activeWindow === win};
})()"""
# Run in the page by its devtools console: the user scrolls, focuses the draft and selects in it,
# then every scroll, focus, selection or visibility event from here on is recorded.
USER_LEAVES_PAGE = """(() => {
  const draft = document.getElementById('draft');
  draft.focus({preventScroll: true});
  draft.setSelectionRange(%d, %d, %s);
  window.scrollTo(%d, %d);
  return new Promise(resolve => setTimeout(() => {
    window.__userTabEvents = [];
    const record = event => window.__userTabEvents.push(event.type);
    for (const type of ['scroll', 'focus', 'blur', 'focusin', 'focusout', 'select', 'pagehide', 'pageshow']) {
      window.addEventListener(type, record, true);
    }
    for (const type of ['selectionchange', 'visibilitychange']) document.addEventListener(type, record, true);
    resolve(true);
  }, 200));
})()"""
PAGE_STATE = """(() => {
  const active = document.activeElement, draft = document.getElementById('draft');
  return {scroll: [window.scrollX, window.scrollY], active: active && (active.id || active.localName),
          selection: [draft.selectionStart, draft.selectionEnd, draft.selectionDirection],
          value: draft.value, visibility: document.visibilityState, events: window.__userTabEvents};
})()"""


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = (LONG_PAGE if path == "/long" else f"<title>{path}</title><h1>{path} page</h1>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _evaluate_in_tab(debugger, url, script):
    """Run script, a JS expression, in the page of the tab at url through its devtools console."""
    (descriptor,) = [tab for tab in debugger.request("root", "listTabs")["tabs"] if tab.get("url") == url]
    console = debugger.request(descriptor["actor"], "getTarget")["frame"]["consoleActor"]
    wrapped = ("(async () => JSON.stringify({value: await (\n" + script + "\n)}))()")
    result_id = debugger.request(console, "evaluateJSAsync", text=wrapped, mapped={"await": True})["resultID"]
    while True:
        reply = debugger._read()
        if reply.get("type") == "evaluationResult" and reply.get("resultID") == result_id:
            break
    if reply.get("hasException") or not isinstance(reply.get("result"), str):
        raise AssertionError(f"page script failed: {reply.get('exceptionMessage') or reply}")
    return json.loads(reply["result"])["value"]


class _Session:
    """Tool calls through an MCP client session; _in_session records every elicitation it gets."""

    def __init__(self, session, prompts):
        self.session, self.prompts = session, prompts

    async def call(self, name, arguments):
        return await self.session.call_tool(name, arguments)

    async def payload(self, name, arguments):
        """The JSON result of a call that returns one, and whether it is an MCP error."""
        result = await self.call(name, arguments)
        return result.isError, json.loads(result.content[0].text)


async def _in_session(body):
    """The prompts the server sent while body ran its calls in one MCP session.

    The client declares elicitation, so an approval hold would prompt here; without it the hold's
    result would carry a confirm token instead, which the refusals are checked not to.
    """
    prompts = []

    async def elicit(context, params):
        prompts.append(params.message)
        return types.ElicitResult(action="decline")

    async with create_connected_server_and_client_session(server.mcp._mcp_server,
                                                         elicitation_callback=elicit) as session:
        await body(_Session(session, prompts))
    return prompts


class RealBrowserUserTabReadTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        auth = cls.bridge.auth
        owner = patch.object(cls.bridge, "auth", side_effect=lambda: {**auth(), OWNER_HEADER: "e2e-user-tab-reads"})
        owner.start()
        cls.addClassCleanup(owner.stop)

    def setUp(self):
        # The lowlevel server logs every request at INFO.
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        for name, value in (("_current_tab", None), ("_clicked", {}), ("_requests", {})):
            patcher = patch.object(playwright_tools, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        get = lambda endpoint: self.tool_request(endpoint)
        post = lambda endpoint, data, timeout=15, start_bridge=True: self.tool_request(endpoint, data)
        for patcher in (patch.object(tools, "_get", get), patch.object(tools, "_post", post),
                        # The server's activity count goes to this bridge too, never to a default one.
                        patch.object(server._client, "post", post)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tool_request(self, endpoint, data=None):
        """A tool's bridge request with the read-only user-tab permission and the server's tool-call headers
        sent explicitly, as its BridgeClient sends them (GET without data)."""
        headers = {**self.bridge.auth(), USER_TABS_HEADER: "read-only", **server._gateway_headers()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, _, text = harness._request(self.bridge.http_port, "GET" if data is None else "POST", endpoint,
                                           headers, self.bridge.host, None if data is None else json.dumps(data),
                                           timeout=90)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def parent(self, script, *args):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script % tuple(json.dumps(a) for a in args))
        finally:
            debugger.close()

    def in_tab(self, url, script):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return _evaluate_in_tab(debugger, url, script)
        finally:
            debugger.close()

    def tabs(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.1)

    def user_tab(self, name, shown):
        """(tab id, url) of a long page the user opened, scrolled and left a selection in.

        Shown, or behind a cover page the user opened and shows after it.
        """
        url = f"{self.site.origin}/long?{name}"
        self.parent(USER_OPENS_TAB, url, True)
        self.addCleanup(self.parent, USER_CLOSES_TAB, url)
        self.wait_for(lambda: any(t["url"] == url and t["title"] == "long"
                                  for t in self.tabs().values()), "the long page did not load")
        tab_id = next(i for i, t in self.tabs().items() if t["url"] == url)
        self.assertIs(self.tabs()[tab_id]["owned"], False)
        self.in_tab(url, USER_LEAVES_PAGE % (SELECTION[0], SELECTION[1], json.dumps(SELECTION[2]),
                                             SCROLL_X, SCROLL_Y))
        if not shown:
            cover = f"{self.site.origin}/cover?{name}"
            self.parent(USER_OPENS_TAB, cover, True)
            self.addCleanup(self.parent, USER_CLOSES_TAB, cover)
            self.wait_for(lambda: any(t["url"] == cover and t["active"] for t in self.tabs().values()),
                          "the cover page is not shown")
            self.wait_for(lambda: self.in_tab(url, "document.visibilityState") == "hidden",
                          "the long page did not go to the background")
            # Going to the background is the user's doing; record from here on.
            self.in_tab(url, "(window.__userTabEvents = [], true)")
        return tab_id, url

    def observe(self, url):
        """What the user would notice: the page's state and events, and the shown tab."""
        return {"page": self.in_tab(url, PAGE_STATE), "shown": self.parent(SHOWN_TAB, harness.EXTENSION_ID),
                "active": sorted(i for i, t in self.tabs().items() if t["active"])}

    def assert_user_left_page(self, observed, tab_id, shown):
        """The precondition: the state user_tab left, observed without changing it."""
        page = observed["page"]
        self.assertEqual(page["scroll"], [SCROLL_X, SCROLL_Y], observed)
        self.assertEqual((page["active"], page["selection"], page["events"]), ("draft", SELECTION, []), observed)
        self.assertEqual(page["visibility"], "visible" if shown else "hidden", observed)
        self.assertEqual(observed["shown"]["tabId"] == tab_id, shown, observed)
        self.assertIs(observed["shown"]["windowFocused"], True, observed)

    def assert_unchanged(self, url, before, step):
        self.assertEqual(self.observe(url), before, f"{step} changed the user's tab or view")

    async def core_reads(self, mcp, url, before, where):
        """The core reads on the tab they act on, each leaving the user's tab and view as they were."""
        error, snapshot = await mcp.payload("browser_snapshot", {})
        self.assertFalse(error, snapshot)
        self.assertIn('button "Far button"', snapshot["snapshot"])
        self.assert_unchanged(url, before, f"browser_snapshot ({where})")
        error, found = await mcp.payload("browser_find", {"text": "Far button"})
        self.assertFalse(error, found)
        self.assertEqual(found["count"], 1, found)
        self.assert_unchanged(url, before, f"browser_find ({where})")
        shot = await mcp.call("browser_take_screenshot", {})
        self.assertFalse(shot.isError, shot)
        self.assertTrue(any(isinstance(item, types.ImageContent) for item in shot.content), shot)
        self.assert_unchanged(url, before, f"browser_take_screenshot ({where})")

    def kitsune_reads(self, tab_id, url, before):
        """The Kitsune read paths of those tools, addressed to the user's tab."""
        reads = (
            ("snapshot", lambda: tools.browser_snapshot(tab_id=tab_id), "snapshot", 'button "Far button"'),
            ("text snapshot", lambda: tools.browser_snapshot(tab_id=tab_id, text=True), "snapshot", "Paragraph 5 of"),
            ("find", lambda: tools.browser_find("far button", tab_id=tab_id), None, "Far button"),
            ("page text", lambda: tools.browser_page_text(tab_id=tab_id), "text", "Paragraph 12"),
            ("viewport screenshot", lambda: tools.browser_screenshot(tab_id=tab_id), None, None),
        )
        for step, read, key, expected in reads:
            result = read()
            if key is None and expected is None:
                self.assertTrue(any(isinstance(item, tools.Image) for item in result), (step, result))
            else:
                self.assertNotIn("error", result, (step, result))
                self.assertIn(expected, result[key] if key else json.dumps(result), (step, result))
            self.assert_unchanged(url, before, f"Kitsune {step}")

    def test_reads_on_the_viewed_user_tab_change_nothing(self):
        tab_id, url = self.user_tab("viewed", shown=True)
        before = self.observe(url)
        self.assert_user_left_page(before, tab_id, shown=True)
        self.assert_unchanged(url, before, "observing")

        async def body(mcp):
            # Without a current tab, the core reads act on the tab the user is viewing.
            await self.core_reads(mcp, url, before, "no current tab")
            self.kitsune_reads(tab_id, url, before)
            playwright_tools._current_tab = tab_id
            await self.core_reads(mcp, url, before, "the user's tab is current")
            # Current is listed for the agent's current tab, whoever owns it (#399).
            error, listed = await mcp.payload("browser_tabs", {"action": "list"})
            self.assertFalse(error, listed)
            (current,) = [tab for tab in listed["tabs"] if tab.get("current")]
            self.assertEqual((current["url"], current.get("owned"), current.get("active")), (url, None, True), listed)
            self.assertIn("read-only", listed["hint"])
            self.assert_unchanged(url, before, "browser_tabs list")

        prompts = asyncio.run(asyncio.wait_for(_in_session(body), MCP_SESSION_TIMEOUT))
        self.assertEqual(prompts, [])

    def test_reads_on_a_background_user_tab_change_nothing(self):
        tab_id, url = self.user_tab("background", shown=False)
        before = self.observe(url)
        self.assert_user_left_page(before, tab_id, shown=False)
        self.assert_unchanged(url, before, "observing")

        async def body(mcp):
            error, opened = await mcp.payload("browser_tabs", {"action": "new", "url": self.site.origin + "/agent"})
            self.assertFalse(error, opened)
            agent = playwright_tools._current_tab
            self.addCleanup(self.call, "/api/close-tab", {"tabId": agent})
            self.assert_unchanged(url, before, "browser_tabs new")
            self.kitsune_reads(tab_id, url, before)
            playwright_tools._current_tab = tab_id
            await self.core_reads(mcp, url, before, "the background user tab is current")
            playwright_tools._current_tab = agent
            error, listed = await mcp.payload("browser_tabs", {"action": "list"})
            self.assertFalse(error, listed)
            (current,) = [tab for tab in listed["tabs"] if tab.get("current")]
            self.assertEqual((current["url"], current.get("owned")), (self.site.origin + "/agent", True), listed)
            (user,) = [tab for tab in listed["tabs"] if tab["url"] == url]
            self.assertEqual((user.get("active"), user.get("current"), user.get("owned")), (None, None, None), listed)

        prompts = asyncio.run(asyncio.wait_for(_in_session(body), MCP_SESSION_TIMEOUT))
        self.assertEqual(prompts, [])

    def assert_refused_at_once(self, step, elapsed, result, tab_id, index, action):
        """A user_tab_read_only refusal of step that answered without waiting for an approval."""
        self.assert_error(result, "user_tab_read_only", False)
        self.assertEqual({key: result["state"].get(key) for key in ("tabId", "index", "action")},
                         {"tabId": tab_id, "index": index, "action": action}, (step, result))
        for way in ("browser_tabs action=new", "browser_tabs action=request"):
            self.assertIn(way, result.get("hint", ""), step)
        self.assertLess(elapsed, REFUSAL_SECONDS, step)

    def check_writes_refuse(self, name, shown):
        tab_id, url = self.user_tab(name, shown)
        before = self.observe(url)
        self.assert_user_left_page(before, tab_id, shown)
        agent_url = f"{self.site.origin}/agent?{name}"

        async def body(mcp):
            error, opened = await mcp.payload("browser_tabs", {"action": "new", "url": agent_url})
            self.assertFalse(error, opened)
            agent = playwright_tools._current_tab
            self.addCleanup(self.call, "/api/close-tab", {"tabId": agent})
            self.assert_unchanged(url, before, "browser_tabs new")
            error, listed = await mcp.payload("browser_tabs", {"action": "list"})
            self.assertFalse(error, listed)
            (index,) = [tab["index"] for tab in listed["tabs"] if tab["url"] == url]

            async def refused_over_mcp(step, tool, arguments, action):
                """An MCP error; a hold would have prompted (prompts below), as this client elicits."""
                started = time.monotonic()
                error, result = await mcp.payload(tool, arguments)
                elapsed = time.monotonic() - started
                self.assertTrue(error, (step, result))
                self.assert_refused_at_once(step, elapsed, result, tab_id, index, action)
                self.assert_unchanged(url, before, step)

            def refused_directly(step, call, action):
                """A direct Kitsune tool call has no elicitation: a hold would return a confirm token."""
                started = time.monotonic()
                result = call()
                elapsed = time.monotonic() - started
                self.assertTrue(tools._failed(result), (step, result))
                self.assert_refused_at_once(step, elapsed, result, tab_id, index, action)
                for token in ("confirmToken", "confirm_token", "resume_token"):
                    self.assertNotIn(token, json.dumps(result), step)
                self.assert_unchanged(url, before, step)

            # Addressed by index or tab_id while the agent's own tab is current.
            for step, tool, arguments, action in (
                    ("select", "browser_tabs", {"action": "select", "index": index}, "switchTab"),
                    ("close", "browser_tabs", {"action": "close", "index": index}, "closeTab"),
                    ("navigate", "browser_navigate", {"url": agent_url, "tab_id": tab_id}, "navigate"),
                    ("back", "browser_navigate_back", {"tab_id": tab_id}, "goBack")):
                await refused_over_mcp(f"browser_tabs {step}" if tool == "browser_tabs" else tool, tool, arguments,
                                       action)
                self.assertEqual(playwright_tools._current_tab, agent, step)
            # The Kitsune tools with tab_id.
            for step, call, action in (
                    ("scroll", lambda: tools.browser_scroll(direction="down", tab_id=tab_id), "scroll"),
                    ("reload", lambda: tools.browser_reload_tab(tab_id=tab_id), "reloadTabBrowser")):
                refused_directly(f"Kitsune {step}", call, action)
            # The core tools on the user's tab as the current tab.
            playwright_tools._current_tab = tab_id
            for tool, arguments, action in (
                    ("browser_hover", {"target": "#far", "element": "Far button"}, "hover"),
                    ("browser_take_screenshot", {"target": "#far", "element": "Far button"}, "elementScreenshot"),
                    ("browser_take_screenshot", {"fullPage": True}, "fullPageScreenshot"),
                    ("browser_find", {"text": "not on the page", "load_more": True}, "find"),
                    ("browser_click", {"target": "#far", "element": "Far button"}, "click"),
                    ("browser_type", {"target": "#draft", "text": "typed"}, "fill"),
                    ("browser_navigate", {"url": agent_url}, "navigate"),
                    ("browser_navigate_back", {}, "goBack"),
                    ("browser_close", {}, "closeTab")):
                await refused_over_mcp(f"{tool} {arguments}", tool, arguments, action)
                self.assertEqual(playwright_tools._current_tab, tab_id, tool)
            # list marks the current tab, whoever owns it, and only that one.
            playwright_tools._current_tab = agent
            error, listed = await mcp.payload("browser_tabs", {"action": "list"})
            self.assertFalse(error, listed)
            self.assertEqual([tab["url"] for tab in listed["tabs"] if tab.get("current")], [agent_url], listed)
            self.assertEqual(listed["tabs"][index]["url"], url, listed)
            self.assertNotIn("current", listed["tabs"][index])

        prompts = asyncio.run(asyncio.wait_for(_in_session(body), MCP_SESSION_TIMEOUT))
        self.assertEqual(prompts, [])
        # The observation is not blind: the explicitly writable opt-out does scroll the tab.
        headers = {**self.bridge.auth(), "Content-Type": "application/json", USER_TABS_HEADER: "writable"}
        status, _, text = harness._request(self.bridge.http_port, "POST", "/api/scroll", headers, self.bridge.host,
                                           json.dumps({"direction": "down", "tabId": tab_id}), timeout=90)
        self.assertEqual(status, 200, text)
        self.assert_ok(json.loads(text))
        after = self.observe(url)["page"]
        self.assertGreater(after["scroll"][1], SCROLL_Y, after)
        self.assertIn("scroll", after["events"], after)

    def test_writes_on_the_viewed_user_tab_refuse_at_once_without_a_prompt(self):
        self.check_writes_refuse("viewed-writes", shown=True)

    def test_writes_on_a_background_user_tab_refuse_at_once_without_a_prompt(self):
        self.check_writes_refuse("background-writes", shown=False)
