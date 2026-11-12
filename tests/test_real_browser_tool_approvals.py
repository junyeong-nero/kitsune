"""#525 in a real browser: a tool the user requires approval for waits for the real toolbar popup's
answer, or for the MCP client's prompt, whichever comes first.

Opt-in via real_browser_harness.py. The default approval list (the storage and history tools) is what
the real extension sends the bridge when it connects; browser_cookies is one of them. The popup is
opened and clicked as in test_real_browser_handoff.
"""
import asyncio
import json
from contextlib import asynccontextmanager
import sys
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import real_browser_harness as harness
from test_real_browser_handoff import POPUP, POPUP_CLOSES

# Reads the popup's tool approval cards and optionally clicks one's button.
APPROVAL_FRAME = r"""(name, click) => {
  const doc = content.document;
  const cards = [...doc.querySelectorAll('#tool-approvals .handoff')];
  const text = (card, selector) => card.querySelector(selector)?.textContent ?? null;
  let clicked = false;
  if (click) {
    const card = cards.find(c => text(c, 'h3') === click.tool);
    const button = card && [...card.querySelectorAll('button')].find(b => b.textContent === click.button && !b.disabled);
    if (button) {
      button.click();
      clicked = true;
    }
  }
  sendAsyncMessage(name, {ready: doc.readyState === 'complete', clicked,
    cards: cards.map(card => ({tool: text(card, 'h3'), client: text(card, '.handoff-client'),
      buttons: [...card.querySelectorAll('button')].map(b => b.textContent)}))});
}"""
COOKIES = {"op": "get", "url": "https://example.com/"}


@asynccontextmanager
async def _mcp(env, answers=None, name="kitsune-e2e-approvals"):
    """An MCP session; answers: the elicitation answers in order, or None for a client without elicitation."""
    prompts = []

    async def elicit(context, params):
        prompts.append(params.message)
        if answers is None or len(prompts) > len(answers):
            return types.ElicitResult(action="cancel")
        action, decision, delay = answers[len(prompts) - 1]
        await asyncio.sleep(delay)
        return types.ElicitResult(action=action, content={"decision": decision} if decision else None)

    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(
            read, write, elicitation_callback=None if answers is None else elicit,
            client_info=types.Implementation(name=name, version="1")) as session:
        await session.initialize()
        session.prompts = prompts
        yield session


class RealBrowserToolApprovalTests(harness.RealBrowserTestCase):
    def setUp(self):
        self.env = dict(self.bridge.env, KITSUNE_OWNER_ID="e2e-tool-approvals")
        self.addCleanup(self.parent, POPUP_CLOSES % json.dumps(harness.EXTENSION_ID))

    def parent(self, script):
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script)
        finally:
            debugger.close()

    def popup(self, click=None):
        return self.parent(POPUP % {"extension": json.dumps(harness.EXTENSION_ID),
                                    "frame": json.dumps(APPROVAL_FRAME), "click": json.dumps(click)})

    def answer_in_popup(self, button, timeout=20):
        """Wait for the browser_cookies card, then click button on it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.popup()
            if any(card["tool"] == "browser_cookies" for card in state["cards"]):
                self.assertTrue(self.popup({"tool": "browser_cookies", "button": button})["clicked"])
                return state["cards"]
            time.sleep(0.3)
        self.fail("the popup showed no approval card")

    async def call(self, session, name, arguments):
        result = await session.call_tool(name, arguments)
        return result.isError, json.loads(result.content[0].text)

    def test_the_popup_allows_or_declines_a_marked_tool(self):
        async def run():
            async with _mcp(self.env) as session:
                pending = asyncio.create_task(self.call(session, "browser_cookies", COOKIES))
                cards = await asyncio.to_thread(self.answer_in_popup, "Allow once")
                self.assertEqual(cards[0]["buttons"], ["Decline", "Allow for this session", "Allow once"])
                error, allowed = await pending
                self.assertFalse(error, allowed)
                # Once is one call: the next one asks again, and Decline runs nothing.
                pending = asyncio.create_task(self.call(session, "browser_cookies", COOKIES))
                await asyncio.to_thread(self.answer_in_popup, "Decline")
                error, refused = await pending
                self.assertTrue(error)
                self.assertEqual((refused["code"], refused["state"]["decision"]), ("needs_user", "declined"))
                # An unmarked tool asks nothing.
                error, listed = await self.call(session, "browser_tabs", {"action": "list"})
                self.assertFalse(error, listed)
                self.assertEqual(self.popup()["cards"], [])

        asyncio.run(asyncio.wait_for(run(), 90))

    def test_the_clients_prompt_allows_for_the_session(self):
        async def run():
            # A field prompt (Codex) chooses Allow for this session; the popup's card then goes away.
            async with _mcp(self.env, [("accept", "Allow for this session", 1.5)], name="codex-mcp-client") as session:
                pending = asyncio.create_task(self.call(session, "browser_cookies", COOKIES))
                await asyncio.sleep(1)
                cards = await asyncio.to_thread(lambda: self.popup()["cards"])
                self.assertEqual([card["tool"] for card in cards], ["browser_cookies"], "both ask at once")
                error, allowed = await pending
                self.assertFalse(error, allowed)
                self.assertEqual(len(session.prompts), 1)
                self.assertIn("browser_cookies", session.prompts[0])
                await asyncio.sleep(1.5)  # popup.js polls every second
                self.assertEqual(await asyncio.to_thread(lambda: self.popup()["cards"]), [])
                error, again = await self.call(session, "browser_cookies", COOKIES)
                self.assertFalse(error, again)
                self.assertEqual(len(session.prompts), 1, "the session's later calls ask nothing")

        asyncio.run(asyncio.wait_for(run(), 90))
