"""#396 in a real browser: browser_tabs action=request handoff through the real toolbar popup.

Opt-in via tests/real_browser_harness.py. The MCP server runs over stdio against the
isolated bridge, as in test_real_browser_elicitation. A privileged script opens the
extension's own toolbar popup (the browserAction's openPopup, what a toolbar click
does) in the headless browser and reads or clicks its handoff cards through a frame
script, so popup.js sends its real messages. The popup is opened programmatically, not
by a toolbar click, and nothing looks at its pixels. popup.html loaded in a tab is not
an equivalent: background.js answers handoff messages only from a page outside a tab.

User tabs are opened by the browser itself, not by the agent. Every MCP server shares
one stable KITSUNE_OWNER_ID, so a later session is the same client in a new session,
except the second client of the two-client test, which has an owner id of its own. The
copy of the extension also runs test_real_browser's control script, which closes the
extension's bridge WebSocket so that the extension reconnects without a reload.
"""
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler
import json
import os
import signal
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import real_browser_harness as harness
from test_real_browser import CONTROL_SCRIPT, ExtensionControl

CLIENT_NAME = "Kitsune E2E Handoff Client"
OWNER_ID = "e2e-handoff-owner"
OTHER_CLIENT_NAME = "Kitsune E2E Other Client"
OTHER_OWNER_ID = "e2e-handoff-other"
REASON = "Fill in the form on this page"
SESSION_TIMEOUT = 90
# The extension's browser-first window before the client fallback (background.js HANDOFF_BROWSER_MS).
BROWSER_FALLBACK_SECONDS = 30
# How long a fallback confirm token stays valid (background.js HANDOFF_TOKEN_MS).
TOKEN_SECONDS = 120

# The browser, not the agent, opens url in a new background tab: a user tab.
USER_OPENS = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  win.gBrowser.addTab(%s, {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
                           inBackground: true});
  return true;
})()"""
CLOSE_TABS_AT = """(async () => {
  for (const win of Services.wm.getEnumerator('navigator:browser')) {
    for (const tab of [...win.gBrowser.tabs]) {
      if (tab.linkedBrowser.currentURI.spec === %s) win.gBrowser.removeTab(tab);
    }
  }
  return true;
})()"""
# Runs in the popup's own content process: reads the handoff cards and optionally clicks one
# card's button, which runs popup.js's click handler and so its runtime.sendMessage.
POPUP_FRAME = r"""(name, click) => {
  const doc = content.document;
  const text = (card, selector) => card.querySelector(selector)?.textContent ?? null;
  const cards = [...doc.querySelectorAll('#handoffs .handoff')];
  let clicked = false;
  if (click) {
    const card = cards.find(c => text(c, '.handoff-label') === click.label && text(c, '.handoff-url') === click.url);
    const button = card && [...card.querySelectorAll('button')].find(b => b.textContent === click.button && !b.disabled);
    if (button) {
      button.click();
      clicked = true;
    }
  }
  sendAsyncMessage(name, {
    ready: doc.readyState === 'complete', connection: text(doc, '#connection'), clicked,
    cards: cards.map(card => ({label: text(card, '.handoff-label'), client: text(card, '.handoff-client'),
      title: text(card, 'h3'), url: text(card, '.handoff-url'), reason: text(card, '.handoff-reason'),
      buttons: [...card.querySelectorAll('button')].map(b => b.textContent)})),
  });
}"""
# Opens the toolbar popup unless it is open, then runs POPUP_FRAME in it.
POPUP = """(async () => {
  const policy = WebExtensionPolicy.getByID(%(extension)s);
  const url = `moz-extension://${policy.mozExtensionHostname}/popup/popup.html`;
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const find = () => [...win.document.querySelectorAll('browser')].find(b => b.currentURI?.spec === url);
  const sleep = ms => new Promise(resolve => win.setTimeout(resolve, ms));
  const shown = () => {
    const popup = find();
    return popup?.closest('panel')?.state === 'open' ? popup : null;
  };
  if (!find()) {
    const {ExtensionParent} = ChromeUtils.importESModule('resource://gre/modules/ExtensionParent.sys.mjs');
    await ExtensionParent.apiManager.global.browserActionFor(policy.extension).openPopup(win);
  }
  // The popup preloads in a hidden browser and moves into the panel when it opens.
  let popup;
  for (let i = 0; i < 100 && !(popup = shown()); i++) await sleep(100);
  if (!popup) throw new Error('the toolbar popup did not open');
  const panel = popup.closest('panel');
  const name = 'kitsune-e2e-popup-' + Math.random();
  let listener, timer;
  const reply = new Promise((resolve, reject) => {
    listener = message => resolve(message.data);
    timer = win.setTimeout(() => reject(new Error('the popup frame script did not answer')), 5000);
  });
  popup.messageManager.addMessageListener(name, listener);
  try {
    popup.messageManager.loadFrameScript('data:,' + encodeURIComponent(
      `(${%(frame)s})(${JSON.stringify(name)}, ${JSON.stringify(%(click)s)})`), false);
    return {...await reply, panelState: panel.state};
  } finally {
    win.clearTimeout(timer);
    popup.messageManager.removeMessageListener(name, listener);
  }
})()"""
# The action button's badge: the text background.js set, and the button's badge attribute
# (absent when the text is empty). Zen puts the button in the extensions panel unless pinned.
BADGE = """(async () => {
  const {ExtensionParent} = ChromeUtils.importESModule('resource://gre/modules/ExtensionParent.sys.mjs');
  const action = ExtensionParent.apiManager.global.browserActionFor(WebExtensionPolicy.getByID(%s).extension);
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const button = action.widget.forWindow(win).node.querySelector('.webextension-browser-action');
  return {text: action.action.getProperty(null, 'badgeText'), shown: button.getAttribute('badge') ?? ''};
})()"""
# The popup blocker drops a window.open without input; the child-tab test turns it off for itself.
POPUPS_BLOCKED = """(async () => {
  if (%s) Services.prefs.clearUserPref('dom.disable_open_during_load');
  else Services.prefs.setBoolPref('dom.disable_open_during_load', false);
  return Services.prefs.getBoolPref('dom.disable_open_during_load');
})()"""
# Reloads the add-on, as about:debugging's Reload button does.
EXTENSION_RELOADS = """(async () => {
  const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
  await (await AddonManager.getAddonByID(%s)).reload();
  return true;
})()"""
# The URL of the tab that opened the tab at url (tabs.openerTabId), read in the parent process.
OPENER_URL = """(async () => {
  for (const win of Services.wm.getEnumerator('navigator:browser')) {
    const tab = win.gBrowser.tabs.find(t => t.linkedBrowser.currentURI.spec === %s);
    if (tab) return tab.openerTab?.linkedBrowser.currentURI.spec ?? null;
  }
  return null;
})()"""
POPUP_CLOSES = """(async () => {
  const policy = WebExtensionPolicy.getByID(%s);
  const url = `moz-extension://${policy.mozExtensionHostname}/popup/popup.html`;
  for (const win of Services.wm.getEnumerator('navigator:browser')) {
    for (const browser of win.document.querySelectorAll('browser')) {
      if (browser.currentURI?.spec === url) browser.closest('panel')?.hidePopup();
    }
  }
  return true;
})()"""


class _Pages(BaseHTTPRequestHandler):
    """A titled page whose button counts its clicks in the title. An opens-X page also opens
    opened-X in a new tab as it loads, with no input."""

    def do_GET(self):
        name = self.path.strip("/") or "index"
        opens = f"<script>window.open('/opened-{name[6:]}')</script>" if name.startswith("opens-") else ""
        body = (f"<title>Handoff {name}</title><p>{name} page</p>"
                "<button id=act onclick=\"document.title='acted:' + (window.actions=(window.actions||0)+1)\">"
                f"Act</button>{opens}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Session:
    """One MCP client session; tool results are (isError, decoded payload)."""

    def __init__(self, session, prompts):
        self.session = session
        self.prompts = prompts

    async def tool(self, name, arguments):
        result = await self.session.call_tool(name, arguments)
        return result.isError, json.loads(result.content[0].text)

    async def index(self, url):
        error, listed = await self.tool("browser_tabs", {"action": "list"})
        if error:
            raise AssertionError(listed)
        (tab,) = [tab for tab in listed["tabs"] if tab.get("url") == url]
        return tab

    async def request(self, url, **extra):
        return await self.tool("browser_tabs", {"action": "request", "index": (await self.index(url))["index"],
                                                "reason": REASON, **extra})

    async def write(self, url):
        """Select the tab at url, then click its button: (isError, payload) of the refused step or the click."""
        selected = await self.tool("browser_tabs", {"action": "select", "index": (await self.index(url))["index"]})
        if selected[0]:
            return selected
        return await self.click()

    async def click(self):
        return await self.tool("browser_click", {"target": "#act", "element": "Act button"})


@asynccontextmanager
async def _mcp(env, answers=None, observe=None, name=CLIENT_NAME):
    """An MCP session of the client name. answers: the elicitation answers in order, or None for a
    client without elicitation. Each prompt is recorded with observe() before it is answered."""
    prompts = []

    async def elicit(context, params):
        prompts.append({"message": params.message, "at": time.monotonic(),
                        "observed": await asyncio.to_thread(observe) if observe else None})
        if len(prompts) > len(answers):
            return types.ElicitResult(action="cancel")
        action, decision = answers[len(prompts) - 1]
        return types.ElicitResult(action=action, content={"decision": decision} if decision else None)

    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(
            read, write, elicitation_callback=None if answers is None else elicit,
            client_info=types.Implementation(name=name, version="1")) as session:
        await session.initialize()
        yield _Session(session, prompts)


def _server_pids():
    """The kitsune.server processes this test process started."""
    found = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "kitsune.server"],
                           capture_output=True, text=True, check=False)
    return [int(pid) for pid in found.stdout.split()]


class RealBrowserHandoffTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        cls.control = ExtensionControl()
        cls.addClassCleanup(cls.control.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        return {"test-control.js": CONTROL_SCRIPT % cls.control.port}

    def setUp(self):
        self.env = dict(self.bridge.env, KITSUNE_OWNER_ID=OWNER_ID)
        self.addCleanup(self.parent, POPUP_CLOSES % json.dumps(harness.EXTENSION_ID))

    def parent(self, script):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script)
        finally:
            debugger.close()

    def tabs(self):
        return self.call("/api/tabs")["tabs"]

    def tab(self, url):
        (tab,) = [tab for tab in self.tabs() if tab["url"] == url]
        return tab

    def wait_for(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while not (value := predicate()):
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.2)
        return value

    def agent_url(self, name):
        """The URL of a page the agent opens itself."""
        return f"{self.site.origin}/{name}"

    def user_tab(self, name):
        """The URL of a loaded background tab the user, not the agent, opened."""
        url = f"{self.site.origin}/{name}"
        self.parent(USER_OPENS % json.dumps(url))
        self.addCleanup(self.parent, CLOSE_TABS_AT % json.dumps(url))
        self.wait_for(lambda: any(t["url"] == url and t["title"] == f"Handoff {name}" for t in self.tabs()),
                      "the user tab did not load")
        time.sleep(0.5)  # the load is complete, not a navigation in progress
        return url

    def popup(self, click=None):
        """The open toolbar popup's state; click={"label", "url", "button"} clicks that card's button."""
        state = self.parent(POPUP % {"extension": json.dumps(harness.EXTENSION_ID), "frame": json.dumps(POPUP_FRAME),
                                     "click": json.dumps(click)})
        self.assertEqual(state["panelState"], "open", state)
        return state

    def popup_cards(self, url):
        state = self.popup()
        return [card for card in state["cards"] if card["url"] == url] if state["ready"] else None

    def retired_cards(self, url):
        """The popup's cards for url once popup.js, which polls every second, drops them (or after 3 s)."""
        deadline = time.monotonic() + 3
        while (cards := self.popup_cards(url)) != [] and time.monotonic() < deadline:
            time.sleep(0.2)
        return cards

    def wait_for_card(self, url, label):
        """The popup's card for url with label, once popup.js lists it."""
        return self.wait_for(lambda: next((c for c in self.popup_cards(url) or () if c["label"] == label), None),
                             f"the popup did not show a {label!r} card for {url}")

    def badge(self):
        return self.parent(BADGE % json.dumps(harness.EXTENSION_ID))

    def assert_badge(self, text):
        """The toolbar badge counts the requests waiting in the popup (it is cleared with "")."""
        expected = {"text": text, "shown": text}
        deadline = time.monotonic() + 3
        while (badge := self.badge()) != expected and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(badge, expected)

    def assert_card(self, card, url, label, buttons, title=None, client=CLIENT_NAME):
        """card names the client, the tab's title (by default the fixture's unclicked title), url and REASON."""
        self.assertEqual(card, {"label": label, "client": client,
                                "title": title or "Handoff " + url.rsplit("/", 1)[1],
                                "url": url, "reason": REASON, "buttons": buttons})

    def decide(self, url, button, title=None, client=CLIENT_NAME):
        """Wait for the popup's request card for url, check what it names, and click button."""
        self.assert_card(self.wait_for_card(url, "Tab access request"), url, "Tab access request",
                         ["Decline", "Approve handoff"], title, client)
        self.assert_badge("1")
        self.assertTrue(self.popup({"label": "Tab access request", "url": url, "button": button})["clicked"])

    def assert_read_only(self, outcome):
        error, payload = outcome
        self.assertTrue(error, payload)
        self.assert_error(payload, "user_tab_read_only", False)
        for token in ("confirmToken", "confirm_token"):
            self.assertNotIn(token, json.dumps(payload))

    def assert_acted(self, outcome, url, count):
        error, payload = outcome
        self.assertFalse(error, payload)
        self.wait_for(lambda: self.tab(url)["title"] == f"acted:{count}", f"the click on {url} did not act")

    def run_session(self, scenario, answers=None, observe=None, timeout=SESSION_TIMEOUT):
        async def run():
            async with _mcp(self.env, answers, observe) as session:
                return await scenario(session)
        return asyncio.run(asyncio.wait_for(run(), timeout))

    async def approved(self, session, url, client=CLIENT_NAME):
        """Request the tab at url and approve it in the popup; returns the request's (isError, payload)."""
        pending = asyncio.ensure_future(session.request(url))
        await asyncio.to_thread(self.decide, url, "Approve handoff", None, client)
        granted = await pending
        await asyncio.to_thread(self.assert_badge, "")
        return granted

    def test_popup_approval_hands_over_the_tab_without_a_prompt(self):
        url = self.user_tab("approve")

        async def scenario(session):
            refused = await session.write(url)
            granted = await self.approved(session, url)
            listed = await session.index(url)
            clicked = await session.write(url)
            return refused, granted, listed, clicked, session.prompts

        refused, granted, listed, clicked, prompts = self.run_session(scenario, answers=[])
        self.assert_read_only(refused)
        self.assertEqual(granted, (False, {"ok": True, "tabId": self.tab(url)["id"], "owned": True}))
        self.assertIs(listed.get("owned"), True, listed)
        self.assert_acted(clicked, url, 1)
        self.assertEqual(prompts, [], "neither the handoff nor the write asked the client")

    def test_popup_decline_leaves_the_tab_read_only(self):
        url = self.user_tab("decline")

        async def scenario(session):
            pending = asyncio.ensure_future(session.request(url))
            await asyncio.to_thread(self.decide, url, "Decline")
            declined = await pending
            await asyncio.to_thread(self.assert_badge, "")
            return declined, await session.index(url), await session.write(url), session.prompts

        (error, declined), listed, write, prompts = self.run_session(scenario, answers=[])
        self.assertTrue(error, declined)
        self.assert_error(declined, "needs_user", False)
        self.assertEqual((declined["state"]["detail"], declined["state"]["decision"]), ("tab_handoff", "declined"))
        self.assertIn("Work in a tab of your own (browser_tabs action=new)", declined["hint"])
        self.assertNotIn("confirmToken", json.dumps(declined))
        self.assertEqual(prompts, [])
        self.assertNotIn("owned", listed)
        self.assert_read_only(write)
        self.assertEqual(self.tab(url)["title"], "Handoff decline")
        self.assertEqual(self.retired_cards(url), [], "the declined request left the popup")

    def test_take_back_in_the_popup_refuses_later_writes(self):
        url = self.user_tab("take-back")

        async def scenario(session):
            granted = await self.approved(session, url)
            first = await session.write(url)
            await asyncio.to_thread(self.wait_for, lambda: self.tab(url)["title"] == "acted:1", "the first click")
            card = await asyncio.to_thread(self.wait_for_card, url, "Handed off")
            taken = await asyncio.to_thread(self.popup, {"label": "Handed off", "url": url, "button": "Take back"})
            await asyncio.to_thread(self.wait_for, lambda: self.popup_cards(url) == [],
                                    "the popup still lists the taken-back tab")
            return granted, first, card, taken, await session.index(url), await session.click()

        granted, first, card, taken, listed, after = self.run_session(scenario, answers=[])
        self.assertFalse(granted[0], granted)
        self.assert_acted(first, url, 1)
        self.assert_card(card, url, "Handed off", ["Take back"])
        self.assertTrue(taken["clicked"])
        self.assertNotIn("owned", listed)
        self.assert_read_only(after)
        self.assertEqual(self.tab(url)["title"], "acted:1", "nothing ran after the take back")

    def ended_session_checks(self, url, own):
        """After the session that held url ended: its grant is gone and the tab it opened is still its own."""
        self.wait_for(lambda: self.popup_cards(url) == [], "the popup still lists the ended session's handoff")

        async def scenario(session):
            listed = (await session.index(url), await session.index(own), await session.write(url),
                      await session.write(own), await session.request(own))
            # A grant left behind would refuse this at once ("Another client or session owns this tab").
            pending = asyncio.ensure_future(session.request(url))
            await asyncio.to_thread(self.decide, url, "Decline", "acted:1")
            return (*listed, await pending)

        user, agent, refused, acted, requested, (error, declined) = self.run_session(scenario, answers=[])
        self.assertNotIn("owned", user)
        self.assertIs(agent.get("owned"), True, agent)
        self.assert_read_only(refused)
        self.assert_acted(acted, own, 1)
        self.assertEqual(requested, (False, {"ok": True, "tabId": self.tab(own)["id"], "owned": True}),
                         "the agent's own tab needs no handoff")
        self.assertTrue(error, declined)
        self.assert_error(declined, "needs_user", False)
        self.assertEqual(declined["state"]["decision"], "declined", "the tab could be requested afresh")
        self.assertEqual(self.tab(url)["title"], "acted:1", "the user tab kept the first session's click only")

    async def hand_over_and_open_own(self, session, url, own):
        granted = await self.approved(session, url)
        acted = await session.write(url)
        await asyncio.to_thread(self.wait_for, lambda: self.tab(url)["title"] == "acted:1", "the handed-off click")
        error, opened = await session.tool("browser_tabs", {"action": "new", "url": own})
        if error:
            raise AssertionError(opened)
        self.addCleanup(self.parent, CLOSE_TABS_AT % json.dumps(own))
        return granted, acted

    def test_session_end_revokes_the_handoff_and_keeps_the_agents_tabs(self):
        url, own = self.user_tab("session-end"), self.agent_url("own-session-end")

        async def scenario(session):
            return await self.hand_over_and_open_own(session, url, own)

        granted, acted = self.run_session(scenario, answers=[])
        self.assertFalse(granted[0], granted)
        self.assertFalse(acted[0], acted)
        self.ended_session_checks(url, own)

    def test_client_disconnect_revokes_the_handoff_and_keeps_the_agents_tabs(self):
        url, own = self.user_tab("disconnect"), self.agent_url("own-disconnect")

        async def scenario(session):
            outcome = await self.hand_over_and_open_own(session, url, own)
            (pid,) = _server_pids()
            os.kill(pid, signal.SIGKILL)  # no MCP shutdown and no handoff session close
            await asyncio.to_thread(self.wait_for, lambda: self.popup_cards(url) == [],
                                    "the popup still lists the killed client's handoff")
            return outcome

        granted, acted = self.run_session(scenario, answers=[])
        self.assertFalse(granted[0], granted)
        self.assertFalse(acted[0], acted)
        self.ended_session_checks(url, own)

    def wait_for_unanswered_request(self, url):
        """The popup lists the request (wait_for_card's 15 s are within the browser's 30 s)."""
        card = self.wait_for_card(url, "Tab access request")
        self.assert_card(card, url, "Tab access request", ["Decline", "Approve handoff"])
        self.assert_badge("1")

    def test_unanswered_browser_prompt_falls_back_to_client_elicitation(self):
        url = self.user_tab("fallback-elicitation")

        async def scenario(session):
            start = time.monotonic()
            pending = asyncio.ensure_future(session.request(url))
            await asyncio.to_thread(self.wait_for_unanswered_request, url)
            granted = await pending
            return start, granted, await session.index(url), await session.write(url), session.prompts

        start, granted, listed, acted, prompts = self.run_session(
            scenario, answers=[("accept", "Approve")], observe=lambda: (self.retired_cards(url), self.badge()))
        (prompt,) = prompts
        self.assertGreaterEqual(prompt["at"] - start, BROWSER_FALLBACK_SECONDS - 1, "the browser had 30 s first")
        self.assertEqual(prompt["observed"], ([], {"text": "", "shown": ""}),
                         "the browser prompt and its badge retired before the client asked")
        for part in (f"{CLIENT_NAME} requests this tab: {REASON}", f"Tab: Handoff fallback-elicitation - {url}"):
            self.assertIn(part, prompt["message"])
        self.assertEqual(granted, (False, {"ok": True, "tabId": self.tab(url)["id"], "owned": True}))
        self.assertIs(listed.get("owned"), True, listed)
        self.assert_acted(acted, url, 1)

    def test_unanswered_browser_prompt_falls_back_to_a_confirm_token_without_elicitation(self):
        url = self.user_tab("fallback-token")

        async def scenario(session):
            start = time.monotonic()
            pending = asyncio.ensure_future(session.request(url))
            await asyncio.to_thread(self.wait_for_unanswered_request, url)
            error, held = await pending
            answered = time.monotonic()
            retired = await asyncio.to_thread(self.retired_cards, url)
            await asyncio.to_thread(self.assert_badge, "")
            token = held.get("state", {}).get("confirmToken")
            unconfirmed = await session.write(url)
            granted = await session.request(url, confirm_token=token)
            return (start, answered, (error, held), retired, unconfirmed, granted,
                    await session.index(url), await session.write(url))

        start, answered, (error, held), retired, unconfirmed, granted, listed, acted = self.run_session(scenario)
        self.assertGreaterEqual(answered - start, BROWSER_FALLBACK_SECONDS - 1, "the browser had 30 s first")
        self.assertTrue(error, held)
        self.assert_error(held, "needs_user", False)
        state = held["state"]
        self.assertEqual((state["detail"], state["approval"], state["expiresInSeconds"]), ("tab_handoff", "token", 120))
        self.assertEqual((state["title"], state["url"], state["message"]),
                         ("Handoff fallback-token", url, f"{CLIENT_NAME} requests this tab: {REASON}"))
        self.assertIsInstance(state["confirmToken"], str)
        self.assertEqual(retired, [], "the browser prompt retired with the fallback")
        self.assert_read_only(unconfirmed)
        self.assertEqual(granted, (False, {"ok": True, "tabId": self.tab(url)["id"], "owned": True}))
        self.assertIs(listed.get("owned"), True, listed)
        self.assert_acted(acted, url, 1)

    def test_a_tab_the_handed_off_page_opens_stays_owned_after_take_back(self):
        # Only the handed-off tab returns; a tab its page opens is an ordinary, persistent agent tab
        # (docs/api-reference.md#requesting-and-returning-a-tab).
        url = self.user_tab("child-from")
        opener, child = self.agent_url("opens-child"), self.agent_url("opened-child")
        for opened in (opener, child):
            self.addCleanup(self.parent, CLOSE_TABS_AT % json.dumps(opened))
        self.assertIs(self.parent(POPUPS_BLOCKED % "false"), False)
        self.addCleanup(self.parent, POPUPS_BLOCKED % "true")

        async def scenario(session):
            granted = await self.approved(session, url)
            selected = await session.tool("browser_tabs", {"action": "select",
                                                           "index": (await session.index(url))["index"]})
            # The handed-off tab loads a page that opens its own tab: no click, key or other input.
            navigated = await session.tool("browser_navigate", {"url": opener})
            await asyncio.to_thread(self.wait_for, lambda: any(
                t["url"] == child and t["title"] == "Handoff opened-child" for t in self.tabs()),
                "the handed-off page did not open its tab")
            opened = await session.index(child)
            handed = await asyncio.to_thread(self.wait_for_card, url, "Handed off")
            child_cards = await asyncio.to_thread(self.popup_cards, child)
            taken = await asyncio.to_thread(self.popup, {"label": "Handed off", "url": url, "button": "Take back"})
            await asyncio.to_thread(self.wait_for, lambda: self.popup_cards(url) == [],
                                    "the popup still lists the taken-back tab")
            return (granted, selected, navigated, opened, handed, child_cards, taken, await session.index(opener),
                    await session.index(child), await session.write(opener), await session.write(child))

        (granted, selected, navigated, opened, handed, child_cards, taken, returned, kept, refused,
         acted) = self.run_session(scenario, answers=[])
        self.assertFalse(granted[0], granted)
        self.assertFalse(selected[0], selected)
        self.assertFalse(navigated[0], navigated)
        self.assertEqual(self.parent(OPENER_URL % json.dumps(child)), opener, "the handed-off tab opened it")
        self.assertIs(opened.get("owned"), True, opened)
        self.assert_card(handed, url, "Handed off", ["Take back"])
        self.assertEqual(child_cards, [], "the opened tab is not a handoff of its own")
        self.assertTrue(taken["clicked"])
        self.assertNotIn("owned", returned)
        self.assertIs(kept.get("owned"), True, kept)
        self.assert_read_only(refused)
        self.assertEqual(self.tab(opener)["title"], "Handoff opens-child", "nothing ran after the take back")
        self.assert_acted(acted, child, 1)

        async def later(session):
            return await session.index(child), await session.write(child), await session.index(opener)

        # The opened tab outlives the session that opened it: the same client still owns it.
        kept, acted, returned = self.run_session(later, answers=[])
        self.assertIs(kept.get("owned"), True, kept)
        self.assert_acted(acted, child, 2)
        self.assertNotIn("owned", returned)

    def lost_transport_checks(self, name, lose):
        """A grant and a pending request, then lose() drops the extension's bridge connection and
        returns once it is back: both are gone, the agent's own tab stays its own, and the same
        session can ask afresh (docs/api-reference.md#requesting-and-returning-a-tab)."""
        url, waiting = self.user_tab(f"{name}-granted"), self.user_tab(f"{name}-pending")
        own = self.agent_url(f"own-{name}")

        async def scenario(session):
            await self.hand_over_and_open_own(session, url, own)
            handed = await asyncio.to_thread(self.wait_for_card, url, "Handed off")
            pending = asyncio.ensure_future(session.request(waiting))
            await asyncio.to_thread(self.wait_for_unanswered_request, waiting)
            lost_at = time.monotonic()
            await asyncio.to_thread(lose)
            dropped = await pending
            answered = time.monotonic() - lost_at
            await asyncio.to_thread(self.wait_for, lambda: self.popup_cards(url) == self.popup_cards(waiting) == [],
                                    "the popup still lists the grant or the request")
            await asyncio.to_thread(self.assert_badge, "")
            after = (await session.index(url), await session.index(own), await session.write(url),
                     await session.write(own))
            asked = await self.approved(session, waiting)
            return handed, dropped, answered, after, asked, await session.index(waiting), await session.write(waiting)

        handed, (error, dropped), answered, (user, agent, refused, acted), asked, listed, regranted = \
            self.run_session(scenario, answers=[])
        self.assert_card(handed, url, "Handed off", ["Take back"])
        self.assertLess(answered, BROWSER_FALLBACK_SECONDS - 5, "the pending request ended with the connection")
        self.assertTrue(error, dropped)
        self.assert_error(dropped, "extension_disconnected", False)
        self.assertNotIn("owned", user)
        self.assertIs(agent.get("owned"), True, agent)
        self.assert_read_only(refused)
        self.assertEqual(self.tab(url)["title"], "acted:1", "nothing ran after the grant was dropped")
        self.assert_acted(acted, own, 1)
        self.assertEqual(asked, (False, {"ok": True, "tabId": self.tab(waiting)["id"], "owned": True}),
                         "a fresh request after the reconnect is approved afresh")
        self.assertIs(listed.get("owned"), True, listed)
        self.assert_acted(regranted, waiting, 1)

    def test_a_bridge_reconnect_drops_grants_and_pending_requests(self):
        def lose():
            generation = self.bridge.status()["extension_generation"]
            self.assertEqual(self.control.call("dropBridge"), {"dropped": True})
            self.wait_for_extension(after_generation=generation)

        self.lost_transport_checks("reconnect", lose)

    def test_an_extension_reload_drops_grants_and_pending_requests(self):
        def lose():
            generation = self.bridge.status()["extension_generation"]
            self.parent(POPUP_CLOSES % json.dumps(harness.EXTENSION_ID))
            # Not /api/reload-extension: the bridge queues that command behind the pending request.
            self.parent(EXTENSION_RELOADS % json.dumps(harness.EXTENSION_ID))
            self.wait_for_extension(after_generation=generation)

        self.lost_transport_checks("reload", lose)

    def test_an_expired_confirm_token_is_refused_and_grants_nothing(self):
        # Waits out the real 30 s browser window and the 120 s token, about two and a half minutes.
        url = self.user_tab("token-expiry")

        async def scenario(session):
            pending = asyncio.ensure_future(session.request(url))
            await asyncio.to_thread(self.wait_for_unanswered_request, url)
            error, held = await pending
            issued = time.monotonic()
            token = held.get("state", {}).get("confirmToken")
            await asyncio.sleep(TOKEN_SECONDS + 3)
            age = time.monotonic() - issued
            expired = await session.request(url, confirm_token=token)
            return ((error, held), age, expired, await session.index(url),
                    await session.write(url), await asyncio.to_thread(self.popup_cards, url))

        (error, held), age, (refused_error, refused), listed, write, cards = self.run_session(
            scenario, timeout=SESSION_TIMEOUT + TOKEN_SECONDS)
        self.assertTrue(error, held)
        self.assert_error(held, "needs_user", False)
        state = held["state"]
        self.assertEqual((state["detail"], state["approval"], state["expiresInSeconds"]),
                         ("tab_handoff", "token", TOKEN_SECONDS))
        self.assertIsInstance(state["confirmToken"], str)
        self.assertGreater(age, TOKEN_SECONDS, "the repeat came after the token expired")
        self.assertTrue(refused_error, refused)
        self.assert_error(refused, "needs_user", False)
        self.assertEqual((refused["state"]["detail"], refused["state"]["decision"]), ("tab_handoff", "cancelled"))
        self.assertNotIn("confirmToken", json.dumps(refused), "an expired token is not renewed")
        self.assertNotIn("owned", listed)
        self.assert_read_only(write)
        self.assertEqual(self.tab(url)["title"], "Handoff token-expiry")
        self.assertEqual(cards, [], "no handoff and no new request")
        self.assert_badge("")

    def test_a_grant_to_one_client_is_not_the_other_clients(self):
        mine, theirs = self.user_tab("client-a"), self.user_tab("client-b")
        other_env = dict(self.bridge.env, KITSUNE_OWNER_ID=OTHER_OWNER_ID)

        async def run():
            async with _mcp(self.env, []) as one, _mcp(other_env, [], name=OTHER_CLIENT_NAME) as other:
                granted = await self.approved(one, mine)
                foreign = (await other.index(mine), await other.write(mine))
                started = time.monotonic()
                requested = await other.request(mine)
                requested_in = time.monotonic() - started
                other_granted = await self.approved(other, theirs, OTHER_CLIENT_NAME)
                cards = await asyncio.to_thread(lambda: (self.popup_cards(mine), self.popup_cards(theirs)))
                return (granted, foreign, requested, requested_in, other_granted, cards,
                        (await one.index(theirs), await one.write(theirs)),
                        await one.write(mine), await other.write(theirs), one.prompts + other.prompts)

        (granted, (listed, refused), (error, requested), requested_in, other_granted, (mine_cards, theirs_cards),
         (other_listed, other_refused), acted, other_acted, prompts) = asyncio.run(
            asyncio.wait_for(run(), SESSION_TIMEOUT))
        self.assertEqual(granted, (False, {"ok": True, "tabId": self.tab(mine)["id"], "owned": True}))
        self.assertNotIn("owned", listed)
        self.assert_read_only(refused)
        # Another client's grant cannot be requested away: refused at once, with no prompt anywhere.
        self.assert_read_only((error, requested))
        self.assertLess(requested_in, 5)
        self.assertEqual(other_granted, (False, {"ok": True, "tabId": self.tab(theirs)["id"], "owned": True}))
        self.assertNotIn("owned", other_listed)
        self.assert_read_only(other_refused)
        (mine_card,), (theirs_card,) = mine_cards, theirs_cards
        self.assert_card(mine_card, mine, "Handed off", ["Take back"])
        self.assert_card(theirs_card, theirs, "Handed off", ["Take back"], client=OTHER_CLIENT_NAME)
        self.assert_acted(acted, mine, 1)
        self.assert_acted(other_acted, theirs, 1)
        self.assertEqual(prompts, [])
