"""Default user-tab refusal and ask-before-act MCP elicitation in a real browser (#376).

Opt-in via tests/real_browser_harness.py. The MCP server runs over stdio against the
isolated bridge. User tabs refuse writes without prompting; generic approval/resume
uses tabs created by that MCP session, not a writable-user-tabs opt-out or handoff.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

MCP_SESSION_TIMEOUT = 60
# The user loads url in the shown tab, so the agent does not own it (as in test_real_browser_shared_browser).
USER_NAVIGATES = """(async () => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  win.openTrustedLinkIn(%s, 'current');
  return true;
})()"""


class _Pages(BaseHTTPRequestHandler):
    """A consequential button counts clicks; input and submit expose their state in the title."""

    def do_GET(self):
        name = self.path.strip("/") or "index"
        body = (f"<title>{name}</title><p>{name} page</p>"
                "<button id=act onclick=\"document.title='acted:' + (window.actions=(window.actions||0)+1)\">"
                "Delete</button>"
                "<form onsubmit=\"event.preventDefault(); document.title='submitted:' + q.value\">"
                "<input id=q oninput=\"document.title='typed:' + this.value\"></form>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _payload(result):
    return json.loads(result.content[0].text)


async def _calls(env, url, calls, answers, observe, *, new_tab=False):
    """Create an owned current tab, or record the refusal to select a user tab, then call tools.

    Arguments can depend on earlier results. Each elicitation records the page before answering.
    The harness disposes of all tabs with the class's temporary browser/profile.
    """
    prompts, results = [], []

    async def elicit(context, params):
        prompts.append((params.message, observe()))
        action, decision = answers[len(prompts) - 1]
        return types.ElicitResult(action=action, content={"decision": decision} if decision else None)

    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write, elicitation_callback=elicit) as session:
        await session.initialize()
        if new_tab:
            opened = await session.call_tool("browser_tabs", {"action": "new", "url": url})
            if opened.isError:
                raise AssertionError(opened.content[0].text)
            (tab,) = [tab for tab in _payload(opened)["tabs"] if tab.get("url") == url]
            if not tab.get("owned") or not tab.get("current"):
                raise AssertionError(f"new tab is not owned and current: {tab}")
        else:
            tabs = _payload(await session.call_tool("browser_tabs", {"action": "list"}))["tabs"]
            (index,) = [tab["index"] for tab in tabs if tab.get("url") == url]
            selected = await session.call_tool("browser_tabs", {"action": "select", "index": index})
            results.append((selected.isError, selected.content[0].text))
        for name, arguments in calls:
            result = await session.call_tool(name, arguments(results) if callable(arguments) else arguments)
            results.append((result.isError, result.content[0].text))
    return prompts, results


class RealBrowserElicitationTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        cls.wait_for_extension()

    def tabs(self):
        return {t["id"]: t for t in self.call("/api/tabs")["tabs"]}

    def title(self, url):
        (tab,) = [tab for tab in self.tabs().values() if tab["url"] == url]
        return tab["title"]

    def wait_for(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.1)

    def user_page(self, url):
        """The user's shown tab, which the user loaded url in."""
        user = next(t["id"] for t in self.tabs().values() if t["active"])
        debugger = harness.RemoteDebugger(self.browser.rdp_port)
        try:
            debugger.evaluate_in_parent(USER_NAVIGATES % json.dumps(url))
        finally:
            debugger.close()
        self.wait_for(lambda: (self.tabs()[user]["url"], self.tabs()[user]["title"]) == (url, "user"),
                      "the user page did not load")
        time.sleep(0.5)  # the load is complete, not a navigation in progress
        self.assertIs(self.tabs()[user]["owned"], False)
        return user

    def ask_before_act(self):
        self.assert_ok(self.call("/api/set-policy", {"ask": True}))
        self.addCleanup(self.call, "/api/set-policy", {"ask": False})

    def test_user_tab_selection_and_writes_refuse_without_a_prompt(self):
        user_url = self.site.origin + "/user"
        user = self.user_page(user_url)
        before = self.tabs()[user]
        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), user_url,
                   [("browser_click", {"target": "#act", "element": "Delete button"}),
                    ("browser_navigate", {"url": self.site.origin + "/changed", "tab_id": user})],
                   [], lambda: self.tabs()[user]["title"]),
            MCP_SESSION_TIMEOUT))
        self.assertEqual(len(results), 3, "selection, unaddressed click and addressed navigation must refuse")
        for (error, text), code in zip(results, ("user_tab_read_only", "no_current_tab", "user_tab_read_only")):
            self.assertTrue(error, text)
            self.assert_error(json.loads(text), code, False)
            for token in ("confirmToken", "confirm_token", "resume_token"):
                self.assertNotIn(token, text)
        self.assertEqual(prompts, [])
        # Refused selection never invents a current tab or dispatches the following click.
        click = json.loads(results[1][1])
        self.assertEqual(click["state"]["tool"], "browser_click")
        self.assertIn("Nothing was dispatched", click["hint"])
        after = self.tabs()[user]
        for key in ("url", "title", "active", "owned"):
            self.assertEqual(after[key], before[key], key)

    def test_slow_typing_is_not_repeated_when_submission_is_approved(self):
        # Typing on this owned page is allowed; ask-before-act holds only the submitting Enter.
        # Its approved repeat must not type the text again (slowly does not clear the field).
        self.ask_before_act()
        url = self.site.origin + "/agent-type"
        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), url,
                   [("browser_type", {"target": "#q", "text": "hi", "slowly": True, "submit": True})],
                   [("accept", "Approve")], lambda: self.title(url), new_tab=True),
            MCP_SESSION_TIMEOUT))
        ((error, text),) = results
        self.assertFalse(error, text)
        self.assertEqual([title for _, title in prompts], ["typed:hi"], "typed once, not yet submitted")
        self.assertIn("Held because ask-before-act", prompts[0][0])
        self.wait_for(lambda: self.title(url) == "submitted:hi", "the form was not submitted with hi exactly once")

    def test_a_declined_ask_before_act_click_resumes_after_approval(self):
        self.ask_before_act()
        url = self.site.origin + "/agent-resume"

        def resume(results):
            declined = json.loads(results[0][1])
            return {"call": declined["call"], "resume_token": declined["resume_token"]}

        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), url,
                   [("browser_click", {"target": "#act", "element": "Delete button"}), ("browser_resume", resume)],
                   [("decline", None), ("accept", "Approve")], lambda: self.title(url), new_tab=True),
            MCP_SESSION_TIMEOUT))
        (declined_error, declined), (resumed_error, resumed) = results
        self.assertTrue(declined_error)
        declined = json.loads(declined)
        self.assertEqual((declined["code"], declined["state"]["detail"], declined["state"]["decision"],
                          declined["state"]["approval"]),
                         ("needs_user", "confirmation_required", "declined", "elicitation"))
        self.assertNotIn("confirmToken", json.dumps(declined))
        self.assertFalse(resumed_error, resumed)
        self.assertNotIn("resume_token", json.loads(resumed))
        self.wait_for(lambda: self.title(url) == "acted:1", "the resumed click did not act exactly once")
        self.assertEqual([title for _, title in prompts], ["agent-resume", "agent-resume"],
                         "nothing ran before the approval")
        for message, _ in prompts:
            self.assertIn("Held because ask-before-act", message)
            self.assertIn("Target: #act\nAgent's description of the target: Delete button\n", message)
            self.assertIn(f"Tab: {url}", message)
        # call is the audit call id of the generic hold, not a tab handoff.
        entries = self.call("/api/audit", {"limit": 50})["entries"]
        self.assertEqual([entry["action"] for entry in entries if entry.get("call") == declined["call"]], ["click"])
        # The resumed attempt has its own audit call id; the original entry remains a hold.
        clicks = [entry for entry in entries if entry["action"] == "click"]
        self.assertEqual(clicks[-1].get("approval"), "elicitation", clicks)
