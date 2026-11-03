"""#395 over MCP: every write on a user tab or another client's tab refuses at once, without a prompt.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Unlike
test_real_browser_user_tab_reads, the tools run in real MCP servers over stdio (as in
test_real_browser_elicitation), so their requests go through the server's BridgeClient with its
client and owner ids, caps and user-tab headers. Each client declares elicitation and records
every prompt it gets: a write held for approval would prompt there.

The core tools write the agent's current tab, and a stdio session can only select a tab it may
write. So the tab becomes the agent's current tab the way a real session can get there: the
agent requests the user's tab (browser_tabs request), the user approves it in the toolbar popup,
the agent selects it, and the user takes it back in the popup (as in test_real_browser_handoff).
For another client's tab, a second client (another owner id) then requests the same tab and the
user approves that. A tab the second client opened itself is checked with every write the first
client can address to it by index or tab_id. browser_batch runs every step and reports each
refusal; browser_workflow stops at its first. browser_run_code_unsafe is not supported on any tab,
so it answers not_supported before anything is sent.

Afterwards the user leaves state in the page: scroll, focus and a selection in a text field, form
values, a checked box, a selected option, a dialog policy and a beforeunload handler. The page's
own devtools console reads that state and logs page events; the parent process reads the shown
tab. No Kitsune write is used to observe, and a write the owning client may make shows that the
observation sees a change.
"""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import real_browser_harness as harness
import test_real_browser_handoff as handoff
import test_real_browser_user_tab_reads as reads

SESSION_TIMEOUT = 150
# A refusal answers without waiting for anything: no prompt, approval or page wait.
REFUSAL_SECONDS = 5
# Published to the MCP servers: the core tools, Kitsune's page actions (browse), tab tools such as
# browser_reload_tab (tabs), and browser_batch and browser_workflow (automation).
CAPS = "core,browse,tabs,automation"
AGENT, OTHER = ("e2e-user-tab-writes-agent", "Kitsune E2E Agent"), ("e2e-user-tab-writes-other", "Kitsune E2E Other")
REASON = "Check the form on this page"
SCROLL_X, SCROLL_Y = 300, 900
SELECTION = [2, 7, "forward"]

FORM_PAGE = """<!doctype html><meta charset="utf-8"><title>form</title>
<style>body { margin: 0; width: 2000px } .spacer { height: 800px }</style>
<h1>Form page</h1>
<div class="spacer">Introduction</div>
<textarea id="draft" aria-label="Draft">hello world</textarea>
<input id="name" aria-label="Name" value="Ada">
<select id="plan" aria-label="Plan">
  <option value="basic">Basic</option><option value="pro" selected>Pro</option>
</select>
<label><input id="agree" type="checkbox" checked> Agree</label>
<label><input id="news" type="checkbox"> News</label>
<label><input id="size-s" type="radio" name="size" value="s"> Small</label>
<label><input id="size-m" type="radio" name="size" value="m" checked> Medium</label>
<input id="file" type="file" aria-label="File">
<div id="card" draggable="true">Card</div>
<div id="bin" style="width: 200px; height: 100px">Bin</div>
<button id="act" ondblclick="document.title = 'double-clicked'">Act</button>
<div class="spacer">More</div>
<div class="spacer">End</div>"""

# Run in the page by its devtools console: the user leaves a beforeunload guard, focuses the draft,
# selects in it and scrolls, then every event a write would cause is recorded from here on.
USER_LEAVES_PAGE = """(() => {
  window.onbeforeunload = event => { event.preventDefault(); };
  const draft = document.getElementById('draft');
  draft.focus({preventScroll: true});
  draft.setSelectionRange(%d, %d, %s);
  window.scrollTo(%d, %d);
  return new Promise(resolve => setTimeout(() => {
    window.__userTabEvents = [];
    const record = event => window.__userTabEvents.push(event.type);
    for (const type of ['scroll', 'focus', 'blur', 'focusin', 'focusout', 'select', 'pagehide', 'pageshow',
                        'input', 'change', 'click', 'dblclick', 'mousedown', 'mouseup', 'pointerdown',
                        'keydown', 'keyup', 'dragstart', 'dragover', 'drop', 'beforeunload']) {
      window.addEventListener(type, record, true);
    }
    for (const type of ['selectionchange', 'visibilitychange']) document.addEventListener(type, record, true);
    resolve(true);
  }, 200));
})()"""
PAGE_STATE = """(() => {
  const $ = id => document.getElementById(id);
  const active = document.activeElement, draft = $('draft');
  const dialogs = window.__kitsuneDialogSnapshot ? JSON.parse(window.__kitsuneDialogSnapshot()) : null;
  return {url: location.href, title: document.title, history: history.length,
          scroll: [window.scrollX, window.scrollY], active: active && (active.id || active.localName),
          selection: [draft.selectionStart, draft.selectionEnd, draft.selectionDirection],
          values: {draft: draft.value, name: $('name').value, plan: $('plan').value, file: $('file').files.length},
          checked: ['agree', 'news', 'size-s', 'size-m'].filter(id => $(id).checked),
          dialogs, beforeunload: typeof window.onbeforeunload,
          visibility: document.visibilityState, events: window.__userTabEvents};
})()"""
# The page as the fixture serves it with the user's state on top: what assert_user_left_page expects.
USER_STATE = {"title": "form", "history": 1, "scroll": [SCROLL_X, SCROLL_Y], "active": "draft",
              "selection": SELECTION, "values": {"draft": "hello world", "name": "Ada", "plan": "pro", "file": 0},
              "checked": ["agree", "size-m"], "beforeunload": "function", "events": [],
              "dialogs": {"policy": {"accept": None, "prompt_text": None}, "dialogs": []}}


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = (FORM_PAGE if path == "/form" else f"<title>{path}</title><h1>{path} page</h1>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Session:
    """One stdio MCP session; tool results are (isError, decoded payload, seconds)."""

    def __init__(self, session, prompts):
        self.session, self.prompts = session, prompts

    async def tool(self, name, arguments):
        started = time.monotonic()
        result = await self.session.call_tool(name, arguments)
        return result.isError, json.loads(result.content[0].text), time.monotonic() - started

    async def ok(self, name, arguments):
        error, payload, _ = await self.tool(name, arguments)
        if error:
            raise AssertionError(f"{name} {arguments} failed: {payload}")
        return payload

    async def listed(self, url):
        (tab,) = [tab for tab in (await self.ok("browser_tabs", {"action": "list"}))["tabs"] if tab["url"] == url]
        return tab


@asynccontextmanager
async def _mcp(env, client):
    """A session of the MCP server for client (owner id, name); every prompt is recorded and declined."""
    owner, name = client
    prompts = []

    async def elicit(context, params):
        prompts.append(params.message)
        return types.ElicitResult(action="decline")

    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"],
                                   env=dict(env, KITSUNE_OWNER_ID=owner, KITSUNE_CAPS=CAPS))
    async with stdio_client(server) as (read, write), ClientSession(
            read, write, elicitation_callback=elicit,
            client_info=types.Implementation(name=name, version="1")) as session:
        await session.initialize()
        yield _Session(session, prompts)


class RealBrowserUserTabWriteTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        upload = tempfile.TemporaryDirectory(prefix="kitsune-user-tab-writes-")
        cls.addClassCleanup(upload.cleanup)
        cls.upload = Path(upload.name, "upload.txt")
        cls.upload.write_text("not for the user's page\n", encoding="utf-8")

    def setUp(self):
        self.addCleanup(self.parent, handoff.POPUP_CLOSES % json.dumps(harness.EXTENSION_ID))

    def parent(self, script):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script)
        finally:
            debugger.close()

    def in_tab(self, url, script):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return reads._evaluate_in_tab(debugger, url, script)
        finally:
            debugger.close()

    def tabs(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def wait_for(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while not (value := predicate()):
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.2)
        return value

    def form_tab(self, name):
        """(tab id, url) of the form page the user opened and shows."""
        url = f"{self.site.origin}/form?{name}"
        self.parent(reads.USER_OPENS_TAB % (json.dumps(url), "true"))
        self.addCleanup(self.parent, reads.USER_CLOSES_TAB % json.dumps(url))
        self.wait_for(lambda: any(t["url"] == url and t["title"] == "form" for t in self.tabs().values()),
                      "the form page did not load")
        time.sleep(0.5)  # the load is complete, not a navigation in progress
        tab_id = next(i for i, t in self.tabs().items() if t["url"] == url)
        self.assertIs(self.tabs()[tab_id]["owned"], False)
        return tab_id, url

    # ── The toolbar popup, as in test_real_browser_handoff ──

    def popup(self, click=None):
        state = self.parent(handoff.POPUP % {"extension": json.dumps(harness.EXTENSION_ID),
                                             "frame": json.dumps(handoff.POPUP_FRAME), "click": json.dumps(click)})
        self.assertEqual(state["panelState"], "open", state)
        return state

    def click_card(self, url, label, button, client):
        """Wait for the popup's card for url with label from client, then click its button."""
        def card():
            state = self.popup()
            return state["ready"] and any(c["url"] == url and c["label"] == label and c["client"] == client
                                          for c in state["cards"])
        self.wait_for(card, f"the popup did not show a {label!r} card for {url} from {client}")
        self.assertTrue(self.popup({"label": label, "url": url, "button": button})["clicked"])

    async def approved(self, session, client, url):
        """client requests the tab at url and the user approves it in the popup."""
        index = (await session.listed(url))["index"]
        pending = asyncio.ensure_future(session.tool("browser_tabs", {"action": "request", "index": index,
                                                                      "reason": REASON}))
        await asyncio.to_thread(self.click_card, url, "Tab access request", "Approve handoff", client[1])
        error, granted, _ = await pending
        self.assertFalse(error, granted)
        self.assertIs(granted.get("owned"), True, granted)

    async def taken_back(self, session, client, url):
        """The user takes back the tab at url that client holds."""
        await asyncio.to_thread(self.click_card, url, "Handed off", "Take back", client[1])
        deadline = time.monotonic() + 10
        while (await session.listed(url)).get("owned"):
            self.assertLess(time.monotonic(), deadline, "the taken-back tab is still owned")
            await asyncio.sleep(0.2)

    # ── What the user would notice ──

    def leave_page(self, url):
        """The user scrolls, focuses and selects in the page; its events are recorded from then on."""
        self.parent(handoff.POPUP_CLOSES % json.dumps(harness.EXTENSION_ID))
        self.in_tab(url, USER_LEAVES_PAGE % (SELECTION[0], SELECTION[1], json.dumps(SELECTION[2]),
                                             SCROLL_X, SCROLL_Y))

    def observe(self, url):
        """What the user would notice: the page's state and events, and the shown tab."""
        return {"page": self.in_tab(url, PAGE_STATE),
                "shown": self.parent(reads.SHOWN_TAB % json.dumps(harness.EXTENSION_ID)),
                "active": sorted(i for i, t in self.tabs().items() if t["active"])}

    def assert_user_left_page(self, observed, url, tab_id, shown):
        page = observed["page"]
        self.assertEqual({key: page[key] for key in USER_STATE}, USER_STATE, observed)
        self.assertEqual((page["url"], page["visibility"]), (url, "visible" if shown else "hidden"), observed)
        self.assertEqual(observed["shown"]["tabId"] == tab_id, shown, observed)
        self.assertIs(observed["shown"]["windowFocused"], True, observed)

    # ── The writes ──

    def core_writes(self, url):
        """(step, tool, arguments, refused action) of the core writes on the current tab, and of Kitsune's
        browser_check without tab_id, which runs on it too."""
        return [
            ("file_upload", "browser_file_upload", {"paths": [str(self.upload)]}, "uploadFile"),
            ("press_key", "browser_press_key", {"key": "Backspace"}, "keypress"),
            ("press_key End", "browser_press_key", {"key": "End"}, "keypress"),
            ("select_option", "browser_select_option", {"target": "#plan", "values": ["basic"], "element": "Plan"},
             "selectOption"),
            ("fill_form textbox", "browser_fill_form", {"fields": [
                {"name": "Name", "type": "textbox", "target": "#name", "value": "Bob"},
                {"name": "Agree", "type": "checkbox", "target": "#agree", "value": "false"}]}, "fill"),
            ("fill_form uncheck", "browser_fill_form", {"fields": [
                {"name": "Agree", "type": "checkbox", "target": "#agree", "value": "false"}]}, "checkBox"),
            ("fill_form check", "browser_fill_form", {"fields": [
                {"name": "News", "type": "checkbox", "target": "#news", "value": "true"}]}, "checkBox"),
            ("fill_form radio", "browser_fill_form", {"fields": [
                {"name": "Small", "type": "radio", "target": "#size-s", "value": "true"}]}, "checkBox"),
            ("fill_form combobox", "browser_fill_form", {"fields": [
                {"name": "Plan", "type": "combobox", "target": "#plan", "value": "basic"}]}, "selectOption"),
            ("drag", "browser_drag", {"startTarget": "#card", "startElement": "Card", "endTarget": "#bin",
                                      "endElement": "Bin"}, "drag"),
            ("evaluate", "browser_evaluate", {"function": "() => { document.title = 'evaluated'; return 1; }"},
             "executeJS"),
            ("evaluate on element", "browser_evaluate", {"function": "element => { element.value = 'x'; }",
                                                         "target": "#name", "element": "Name"}, "executeJS"),
            ("handle_dialog accept", "browser_handle_dialog", {"accept": True, "promptText": "yes"}, "handleDialog"),
            ("handle_dialog dismiss", "browser_handle_dialog", {"accept": False}, "handleDialog"),
            ("double-click", "browser_click", {"target": "#act", "element": "Act", "doubleClick": True},
             "doubleClick"),
            ("check", "browser_check", {"selector": "#news"}, "checkBox"),
            ("uncheck", "browser_check", {"selector": "#agree", "checked": False}, "checkBox"),
            # Also refused in-process by test_real_browser_user_tab_reads; here through the stdio server.
            ("drop", "browser_drop", {"target": "#bin", "data": {"text/plain": "dropped"}}, "uploadFile"),
            ("click", "browser_click", {"target": "#act", "element": "Act"}, "click"),
            ("type", "browser_type", {"target": "#draft", "text": "typed", "submit": True}, "fill"),
            ("hover", "browser_hover", {"target": "#act", "element": "Act"}, "hover"),
            ("find load_more", "browser_find", {"text": "not on the page", "load_more": True}, "find"),
            ("element screenshot", "browser_take_screenshot", {"target": "#act", "element": "Act"},
             "elementScreenshot"),
            ("fullPage screenshot", "browser_take_screenshot", {"fullPage": True}, "fullPageScreenshot"),
            ("navigate", "browser_navigate", {"url": url + "&navigated"}, "navigate"),
            ("back", "browser_navigate_back", {}, "goBack"),
            ("close", "browser_close", {}, "closeTab"),
        ]

    def addressed_writes(self, tab_id, index, agent_url):
        """The writes that name the tab, by index or tab_id."""
        return [
            ("select", "browser_tabs", {"action": "select", "index": index}, "switchTab"),
            ("close", "browser_tabs", {"action": "close", "index": index}, "closeTab"),
            ("navigate", "browser_navigate", {"url": agent_url, "tab_id": tab_id}, "navigate"),
            ("back", "browser_navigate_back", {"tab_id": tab_id}, "goBack"),
            ("check by tab_id", "browser_check", {"selector": "#news", "tab_id": tab_id}, "checkBox"),
            ("uncheck by tab_id", "browser_check", {"selector": "#agree", "checked": False, "tab_id": tab_id},
             "checkBox"),
            ("scroll", "browser_scroll", {"direction": "down", "tab_id": tab_id}, "scroll"),
            ("focus", "browser_focus", {"selector": "#name", "tab_id": tab_id}, "focusElement"),
            ("blur", "browser_blur", {"tab_id": tab_id}, "blurElement"),
            ("submit_form", "browser_submit_form", {"selector": "#name", "tab_id": tab_id}, "submitForm"),
            ("click_and_wait_navigation", "browser_click_and_wait_navigation", {"selector": "#act", "tab_id": tab_id},
             "clickAndWaitNavigation"),
            ("forward", "browser_forward", {"tab_id": tab_id}, "goForward"),
            ("reload", "browser_reload_tab", {"tab_id": tab_id}, "reloadTabBrowser"),
        ]

    def container_writes(self, tab_id, agent_url, current):
        """(step, tool, arguments, each step's refused action or None for a read) of a batch and a workflow.

        On the current tab they name no tab; otherwise the batch's tab_id or each step's tabId names it.
        A batch runs every step; a workflow stops at its first refused one (navigate).
        """
        steps = [
            ({"action": "snapshot"}, None),
            ({"action": "checkBox", "selector": "#news", "checked": True}, "checkBox"),
            ({"action": "checkBox", "selector": "#agree", "checked": False}, "checkBox"),
            ({"action": "keypress", "key": "a"}, "keypress"),
            ({"action": "fill", "selector": "#name", "value": "Bob"}, "fill"),
            ({"action": "type", "selector": "#draft", "text": "typed"}, "type"),
            ({"action": "formFill", "fields": {"#name": "Bob"}}, "formFill"),
            ({"action": "selectOption", "selector": "#plan", "values": ["basic"]}, "selectOption"),
            ({"action": "click", "selector": "#act"}, "click"),
            ({"action": "doubleClick", "selector": "#act"}, "doubleClick"),
            ({"action": "hover", "selector": "#act"}, "hover"),
            ({"action": "drag", "from": "#card", "to": "#bin"}, "drag"),
            ({"action": "js", "code": "document.title = 'batched'"}, "executeJS"),
            ({"action": "scroll", "direction": "down"}, "scroll"),
            ({"action": "navigate", "url": agent_url}, "navigate"),
            ({"action": "goBack"}, "goBack"),
        ]
        tab = {} if current else {"tab_id": tab_id}
        writes = [("batch", "browser_batch", {"commands": [command for command, _ in steps], **tab},
                   [action for _, action in steps])]
        if not current:
            writes.append(("batch with each step's tabId", "browser_batch",
                           {"commands": [{**command, "tabId": tab_id} for command, _ in steps]},
                           [action for _, action in steps]))
        writes.append(("workflow", "browser_workflow", {"name": "test", "variables": {"url": agent_url}, **tab},
                       ["navigate"]))
        return writes

    async def check_refusals(self, session, url, before, tab_id, index, writes, containers):
        """Each write refuses at once with user_tab_read_only, naming the tab, and changes nothing."""
        for step, tool, arguments, action in writes:
            error, result, seconds = await session.tool(tool, arguments)
            self.assertTrue(error, (step, result))
            self.assert_refused_at_once(step, seconds, result, tab_id, index, action)
            for token in ("confirmToken", "confirm_token", "resume_token"):
                self.assertNotIn(token, json.dumps(result), step)
            await asyncio.to_thread(self.assert_unchanged, url, before, step)
        for step, tool, arguments, actions in containers:
            error, result, seconds = await session.tool(tool, arguments)
            # A container is no MCP error: it reports each step it ran.
            self.assertFalse(error, (step, result))
            self.assertEqual(len(result["results"]), len(actions), (step, result))
            for n, (outcome, action) in enumerate(zip(result["results"], actions)):
                if action is None:
                    self.assertNotIn("error", outcome, (step, n, result))
                else:
                    self.assert_refused_at_once(f"{step} step {n}", seconds, outcome, tab_id, index, action)
            self.assertNotIn("confirmToken", json.dumps(result), step)
            await asyncio.to_thread(self.assert_unchanged, url, before, step)
        # Kitsune has no Playwright page: run_code is refused for every tab before anything is sent.
        error, result, _ = await session.tool("browser_run_code_unsafe", {"code": "async page => page.click('#act')"})
        self.assertTrue(error, result)
        self.assert_error(result, "not_supported", False)
        await asyncio.to_thread(self.assert_unchanged, url, before, "run_code")

    def assert_refused_at_once(self, step, seconds, result, tab_id, index, action):
        self.assertEqual((result.get("code"), result.get("retryable")), ("user_tab_read_only", False), (step, result))
        self.assertEqual({key: result.get("state", {}).get(key) for key in ("tabId", "index", "action")},
                         {"tabId": tab_id, "index": index, "action": action}, (step, result))
        for way in ("browser_tabs action=new", "browser_tabs action=request"):
            self.assertIn(way, result.get("hint", ""), (step, result))
        self.assertLess(seconds, REFUSAL_SECONDS, step)

    def assert_unchanged(self, url, before, step):
        self.assertEqual(self.observe(url), before, f"{step} changed the user's tab or view")

    def run_sessions(self, scenario, *clients):
        async def run():
            async with AsyncExitStack() as stack:
                sessions = [await stack.enter_async_context(_mcp(self.bridge.env, client)) for client in clients]
                await scenario(*sessions)
                return [session.prompts for session in sessions]
        prompts = asyncio.run(asyncio.wait_for(run(), SESSION_TIMEOUT))
        self.assertEqual(prompts, [[] for _ in clients], "a write asked a client")

    # ── The tests ──

    def test_every_write_on_a_user_tab_refuses_without_a_prompt(self):
        tab_id, url = self.form_tab("user")
        agent_url = f"{self.site.origin}/agent?user"

        async def scenario(agent):
            await self.approved(agent, AGENT, url)
            await agent.ok("browser_tabs", {"action": "select", "index": (await agent.listed(url))["index"]})
            await self.taken_back(agent, AGENT, url)
            # The tab the user took back stays the agent's current tab, as list shows.
            listed = await agent.listed(url)
            self.assertEqual((listed.get("current"), listed.get("owned")), (True, None), listed)
            await asyncio.to_thread(self.leave_page, url)
            before = await asyncio.to_thread(self.observe, url)
            self.assert_user_left_page(before, url, tab_id, shown=True)
            await asyncio.to_thread(self.assert_unchanged, url, before, "observing")
            index = listed["index"]
            await self.check_refusals(agent, url, before, tab_id, index,
                                      self.core_writes(url) + self.addressed_writes(tab_id, index, agent_url),
                                      self.container_writes(tab_id, agent_url, current=True)
                                      + self.container_writes(tab_id, agent_url, current=False))
            self.assertEqual((await agent.listed(url)).get("current"), True, "a refusal moved the current tab")

        self.run_sessions(scenario, AGENT)
        # The observation is not blind: the user's own change of the page shows up.
        self.in_tab(url, "(document.getElementById('news').click(), true)")
        self.assertIn("news", self.observe(url)["page"]["checked"])

    def test_every_write_on_a_tab_handed_to_another_client_refuses_without_a_prompt(self):
        tab_id, url = self.form_tab("other")
        agent_url = f"{self.site.origin}/agent?other"

        async def scenario(agent, other):
            await self.approved(agent, AGENT, url)
            await agent.ok("browser_tabs", {"action": "select", "index": (await agent.listed(url))["index"]})
            await self.taken_back(agent, AGENT, url)
            await self.approved(other, OTHER, url)
            listed = await agent.listed(url)
            self.assertEqual((listed.get("current"), listed.get("owned")), (True, None), listed)
            self.assertIs((await other.listed(url)).get("owned"), True)
            await asyncio.to_thread(self.leave_page, url)
            before = await asyncio.to_thread(self.observe, url)
            self.assert_user_left_page(before, url, tab_id, shown=True)
            index = listed["index"]
            await self.check_refusals(agent, url, before, tab_id, index,
                                      self.core_writes(url) + self.addressed_writes(tab_id, index, agent_url),
                                      self.container_writes(tab_id, agent_url, current=True)
                                      + self.container_writes(tab_id, agent_url, current=False))
            self.assertIs((await other.listed(url)).get("owned"), True, "a refusal changed the other client's grant")
            # The observation is not blind: the client holding the tab checks the box.
            await other.ok("browser_check", {"selector": "#news", "tab_id": tab_id})
            after = (await asyncio.to_thread(self.observe, url))["page"]
            self.assertIn("news", after["checked"], after)
            self.assertIn("change", after["events"], after)

        self.run_sessions(scenario, AGENT, OTHER)

    def test_every_addressed_write_on_a_tab_another_client_opened_refuses_without_a_prompt(self):
        url = f"{self.site.origin}/form?opened"
        agent_url = f"{self.site.origin}/agent?opened"
        self.addCleanup(self.parent, reads.USER_CLOSES_TAB % json.dumps(url))
        self.addCleanup(self.parent, reads.USER_CLOSES_TAB % json.dumps(agent_url))

        async def scenario(agent, other):
            await other.ok("browser_tabs", {"action": "new", "url": url})
            tab_id = next(i for i, t in self.tabs().items() if t["url"] == url)
            await agent.ok("browser_tabs", {"action": "new", "url": agent_url})
            listed = await agent.listed(url)
            self.assertEqual((listed.get("current"), listed.get("owned")), (None, None), listed)
            await asyncio.to_thread(self.leave_page, url)
            before = await asyncio.to_thread(self.observe, url)
            self.assert_user_left_page(before, url, tab_id, shown=False)
            index = listed["index"]
            await self.check_refusals(agent, url, before, tab_id, index,
                                      self.addressed_writes(tab_id, index, agent_url),
                                      self.container_writes(tab_id, agent_url, current=False))
            self.assertEqual((await agent.listed(agent_url)).get("current"), True, "a refusal moved the current tab")
            # The observation is not blind: the client that opened the tab checks the box.
            await other.ok("browser_check", {"selector": "#news", "tab_id": tab_id})
            after = (await asyncio.to_thread(self.observe, url))["page"]
            self.assertIn("news", after["checked"], after)

        self.run_sessions(scenario, AGENT, OTHER)
