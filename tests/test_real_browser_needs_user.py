"""needs_user hints and browser_ask_user in a real browser (#376).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Fixture pages show
a sign-in form, a sign-up form, a reCAPTCHA frame (its src never loads: srcdoc wins) and page text
that only talks about signing in. The MCP server runs over stdio against the harness bridge, from a
client without elicitation.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import real_browser_harness as harness

MCP_SESSION_TIMEOUT = 60
RECAPTCHA = "https://www.google.com/recaptcha/api2/anchor"
PAGES = {
    "/login": ("<form><input id=user autocomplete=username><input id=pw type=password>"
               "<button id=go type=button onclick=\"document.querySelector('form').remove()\">Sign in</button></form>"),
    "/signup": "<input id=user><input id=pw type=password autocomplete=new-password>",
    "/captcha": (f"<iframe width=304 height=78 srcdoc='<p>widget</p>' src='{RECAPTCHA}?k=key&size=normal'></iframe>"
                 "<textarea name=g-recaptcha-response style=display:none></textarea>"
                 "<button id=solve onclick=\"document.querySelector('textarea').value='token'\">Solve</button>"),
    "/text": "<p>Please sign in to continue. Are you a robot? Complete the CAPTCHA.</p><input id=q>",
}


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in PAGES:
            self.send_error(404)
            return
        body = (f"<title>{self.path.strip('/')}</title>" + PAGES[self.path]).encode()
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


async def _ask_then_resume(env, url, user_acts):
    """Over stdio, without elicitation: open url in a tab of the agent's, ask the user to sign in there,
    then resume while user_acts() runs on its own thread."""
    server = StdioServerParameters(command=sys.executable, args=["-m", "kitsune.server"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("browser_tabs", {"action": "new", "url": url})
        asked = _payload(await session.call_tool("browser_ask_user", {"reason": "sign_in",
                                                                      "message": "Sign in to the shop, please."}))
        user = asyncio.create_task(asyncio.to_thread(user_acts))
        resumed = await session.call_tool("browser_resume", {"call": asked["call"],
                                                             "resume_token": asked["resume_token"], "wait": 30})
        await user
    return asked, resumed


class RealBrowserNeedsUserTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Pages)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()
        cls.wait_for_extension()

    def test_page_info_snapshots_and_actions_carry_the_pages_hint_and_fail_nothing(self):
        login = self.open_tab(self.site.origin + "/login")
        for result in (self.call("/api/page-info", {"tabId": login}), self.call("/api/snapshot", {"tabId": login})):
            hint = result["needs_user"]
            self.assertEqual((hint["reason"], hint["evidence"]), ("sign_in", {"field": "username"}), result)
            self.assertEqual(hint["message"], "Sign in on this page.")
            self.assertIn("browser_ask_user", hint["hint"])
        typed = self.assert_ok(self.call("/api/type", {"tabId": login, "selector": "#user", "text": "agent"}))
        self.assertEqual(typed["needs_user"]["reason"], "sign_in", typed)
        # Signed in: the form is gone, and so is the hint.
        clicked = self.assert_ok(self.call("/api/click", {"tabId": login, "selector": "#go"}))
        self.assertNotIn("needs_user", clicked)
        self.assertNotIn("needs_user", self.call("/api/page-info", {"tabId": login}))

        captcha = self.open_tab(self.site.origin + "/captcha")
        hint = self.call("/api/page-info", {"tabId": captcha})["needs_user"]
        self.assertEqual((hint["reason"], hint["evidence"]),
                         ("human_check", {"widget": "recaptcha", "frame": RECAPTCHA}))
        self.assertEqual(hint["message"], "Complete the reCAPTCHA check on this page.")
        solved = self.assert_ok(self.call("/api/click", {"tabId": captcha, "selector": "#solve"}))
        self.assertNotIn("needs_user", solved, "a solved widget holds its token")

        # A new password is not a sign-in, and page text never makes a hint.
        for path in ("/signup", "/text"):
            tab = self.open_tab(self.site.origin + path)
            self.assertNotIn("needs_user", self.call("/api/page-info", {"tabId": tab}), path)
            self.assertNotIn("needs_user", self.call("/api/snapshot", {"tabId": tab}), path)

    def test_browser_resume_waits_for_the_user_to_sign_in(self):
        url = self.site.origin + "/login"

        def user_signs_in():
            time.sleep(1.5)
            (tab,) = [t["id"] for t in self.call("/api/tabs")["tabs"] if t["url"] == url]
            self.addCleanup(self.call, "/api/close-tab", {"tabId": tab})
            self.assert_ok(self.call("/api/click", {"tabId": tab, "selector": "#go"}))

        started = time.monotonic()
        asked, resumed = asyncio.run(asyncio.wait_for(
            _ask_then_resume(dict(self.bridge.env), url, user_signs_in), MCP_SESSION_TIMEOUT))
        self.assertEqual((asked["code"], asked["state"]["reason"], asked["state"]["detail"]),
                         ("needs_user", "agent_request", "sign_in"), asked)
        self.assertEqual(asked["state"]["evidence"], {"field": "username"})
        self.assertEqual(asked["state"]["url"], url)
        self.assertFalse(resumed.isError, resumed.content[0].text)
        done = _payload(resumed)
        self.assertEqual((done["answer"], done["via"], done["reason"]), ("done", "page", "sign_in"), done)
        self.assertLess(time.monotonic() - started, 30, "the resume ended once the sign-in was gone")
        entries = self.call("/api/audit", {"limit": 50})["entries"]
        self.assertEqual([e["action"] for e in entries if e.get("call") == asked["call"]], ["askUser"])
