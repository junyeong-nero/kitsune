"""Approval through MCP elicitation, in a real browser (#376).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The MCP server
runs over stdio against the harness bridge. A "user" tab is shown, so the core browser_click on
it is held (the viewed tab); the client's elicitation callback answers as the user.
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
    """/<name> answers with a page titled <name> whose #act button retitles it "acted", and whose
    form retitles it "submitted:" and its #q field's value."""

    def do_GET(self):
        name = self.path.strip("/") or "index"
        body = (f"<title>{name}</title><p>{name} page</p>"
                "<button id=act onclick=\"document.title='acted'\">Act</button>"
                "<form onsubmit=\"event.preventDefault(); document.title='submitted:' + q.value\">"
                "<input id=q></form>").encode()
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


async def _calls(env, user_url, calls, answers, observe):
    """Over stdio: select the user's tab as the current tab, then make each (tool, arguments) call,
    arguments a dict or made from the results so far; the user gives answers in turn, and observe()
    runs at each prompt."""
    prompts, results = [], []

    async def elicit(context, params):
        prompts.append((params.message, observe()))
        action, decision = answers[len(prompts) - 1]
        return types.ElicitResult(action=action, content={"decision": decision} if decision else None)

    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write, elicitation_callback=elicit) as session:
        await session.initialize()
        tabs = _payload(await session.call_tool("browser_tabs", {"action": "list"}))["tabs"]
        (index,) = [tab["index"] for tab in tabs if tab.get("url") == user_url]
        await session.call_tool("browser_tabs", {"action": "select", "index": index})
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

    def test_a_click_on_the_viewed_tab_runs_only_once_the_user_approves_in_the_prompt(self):
        user_url = self.site.origin + "/user"
        user = self.user_page(user_url)
        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), user_url, [("browser_click", {"target": "#act", "element": "Act button"})] * 2,
                   [("decline", None), ("accept", "Approve")], lambda: self.tabs()[user]["title"]),
            MCP_SESSION_TIMEOUT))
        (declined_error, declined), (approved_error, approved) = results
        self.assertTrue(declined_error)
        state = json.loads(declined)["state"]
        self.assertEqual((state["detail"], state["decision"], state["approval"]),
                         ("user_viewing", "declined", "elicitation"), declined)
        for text in (declined, approved):
            self.assertNotIn("confirmToken", text)
        self.assertFalse(approved_error, approved)
        self.wait_for(lambda: self.tabs()[user]["title"] == "acted", "the approved click did not act")
        self.assertEqual([title for _, title in prompts], ["user", "user"], "nothing ran before an approval")
        self.assertIn("Target: #act\nAgent's description of the target: Act button\n", prompts[0][0])
        self.assertIn(f"Tab: user - {user_url}", prompts[0][0])
        clicks = [entry for entry in self.call("/api/audit", {"limit": 50})["entries"] if entry["action"] == "click"]
        self.assertEqual([entry.get("approval") for entry in clicks[-2:]], ["elicitation", "elicitation"], clicks)

    def test_typing_slowly_then_submitting_types_the_text_once_after_each_approval(self):
        # The text is held for the viewed tab, then the Enter that submits the form by ask-before-act:
        # the second repeat presses Enter without typing the text again (slowly does not clear).
        self.assert_ok(self.call("/api/set-policy", {"ask": True}))
        self.addCleanup(self.call, "/api/set-policy", {"ask": False})
        user_url = self.site.origin + "/user"
        user = self.user_page(user_url)
        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), user_url,
                   [("browser_type", {"target": "#q", "text": "hi", "slowly": True, "submit": True})],
                   [("accept", "Approve")] * 2, lambda: self.tabs()[user]["title"]),
            MCP_SESSION_TIMEOUT))
        ((error, text),) = results
        self.assertFalse(error, text)
        self.assertEqual([title for _, title in prompts], ["user", "user"])
        self.assertIn("Held because you are viewing this tab.", prompts[0][0])
        self.assertIn("Held because ask-before-act", prompts[1][0])
        self.wait_for(lambda: self.tabs()[user]["title"] == "submitted:hi",
                      f"the form was not submitted with hi: {self.tabs()[user]['title']}")

    def test_a_declined_click_resumes_once_the_user_approves_in_the_prompt_again(self):
        user_url = self.site.origin + "/user"
        user = self.user_page(user_url)

        def resume(results):
            declined = json.loads(results[0][1])
            return {"call": declined["call"], "resume_token": declined["resume_token"]}

        prompts, results = asyncio.run(asyncio.wait_for(
            _calls(dict(self.bridge.env), user_url,
                   [("browser_click", {"target": "#act"}), ("browser_resume", resume)],
                   [("decline", None), ("accept", "Approve")], lambda: self.tabs()[user]["title"]),
            MCP_SESSION_TIMEOUT))
        (declined_error, declined), (resumed_error, resumed) = results
        self.assertTrue(declined_error)
        declined = json.loads(declined)
        self.assertEqual((declined["code"], declined["state"]["decision"]), ("needs_user", "declined"))
        self.assertFalse(resumed_error, resumed)
        self.assertNotIn("resume_token", json.loads(resumed))
        self.wait_for(lambda: self.tabs()[user]["title"] == "acted", "the resumed click did not act")
        self.assertEqual([title for _, title in prompts], ["user", "user"], "nothing ran before the approval")
        # call is the audit call id of the hold.
        entries = self.call("/api/audit", {"limit": 50})["entries"]
        self.assertEqual([e["action"] for e in entries if e.get("call") == declined["call"]], ["click"])
