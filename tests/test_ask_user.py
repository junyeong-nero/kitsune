"""#376: browser_ask_user asks the user for what Kitsune cannot see or do, in the client's prompt with
elicitation, else by returning needs_user at once; Kitsune's own needs_user hints (sign_in, human_check,
trusted_input) hold nothing, and browser_resume waits for one the agent asked about to go away.

Uses the in-process MCP sessions and bridge of test_elicitation_approval.
"""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

import anyio
from mcp import types
from test_elicitation_approval import AGENT_TAB, ElicitationTestCase, FakeExtension

from kitsune import bridge, playwright_tools, server

LOGIN = {"url": "https://shop.example/login", "title": "Sign in"}
SIGN_IN = {"reason": "sign_in", "evidence": {"field": "password"}}


class PageExtension(FakeExtension):
    """FakeExtension whose AGENT_TAB shows LOGIN with the needs_user hint in `hint`, if any."""

    def __init__(self):
        super().__init__()
        self.hint = None

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        if action in ("getPageInfo", "getSnapshot"):
            self.sent.append((action, params))
            if params.get("tabId") != AGENT_TAB:
                return {"error": f"Tab {params.get('tabId')} no longer exists", "code": "tab_closed",
                        "retryable": False, "state": {"tabId": params.get("tabId")}}
            page = {**LOGIN, "snapshot": "- textbox \"Password\" [ref=e1]"} if action == "getSnapshot" else LOGIN
            return {**page, **({"needs_user": self.hint} if self.hint else {})}
        return await super().__call__(action, params, timeout)


def answer(choice=None, action="accept"):
    async def respond(params):
        return types.ElicitResult(action=action, content={"answer": choice} if choice else None)
    return respond


class AskUserTestCase(ElicitationTestCase):
    def setUp(self):
        super().setUp()
        self.extension = PageExtension()
        for patcher in (patch.dict(server._direct_checkpoints, clear=True),
                        patch.object(playwright_tools, "_current_tab", AGENT_TAB),
                        patch.object(server, "PAGE_STATE_POLL_SECONDS", 0.05)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, answer_with=None, **arguments):
        return self.call("browser_ask_user", {"reason": "other", "message": "Which seat do you want?", **arguments},
                         answer_with)

    def resume(self, payload, answer_with=None, **arguments):
        return self.call("browser_resume", {"call": payload["call"], "resume_token": payload["resume_token"],
                                            **arguments}, answer_with)

    def asked(self):
        return [entry for entry in bridge._audit_log if entry["action"] == "askUser"]


class WithoutElicitationTests(AskUserTestCase):
    def test_it_returns_at_once_for_the_agent_to_ask_in_its_reply(self):
        result, payload = self.ask()
        self.assertTrue(result.isError)
        self.assertEqual((payload["ok"], payload["code"], payload["retryable"]), (False, "needs_user", False))
        self.assertEqual(payload["state"], {"reason": "agent_request", "detail": "other",
                                            "message": "Which seat do you want?", "tabId": AGENT_TAB, **LOGIN})
        self.assertIn("Ask the user in your reply", payload["hint"])
        # Nothing to wait for: no checkpoint.
        self.assertNotIn("resume_token", payload)
        self.assertEqual(server._direct_checkpoints, {})
        # Nothing reached the page; the audit log records the request under its call id, without the message.
        self.assertEqual([action for action, _ in self.extension.sent], ["getPageInfo"])
        (entry,) = self.asked()
        self.assertEqual((entry["call"], entry["params"], entry["error"]),
                         (payload["call"], {"reason": "other", "tabId": AGENT_TAB}, "needs_user"))
        self.assertNotIn("seat", json.dumps(entry))

    def test_a_sign_in_kitsune_sees_on_the_tab_is_waited_for_by_browser_resume(self):
        self.extension.hint = SIGN_IN
        result, payload = self.ask(reason="sign_in", message="Sign in to your shop account, please.")
        self.assertEqual(payload["state"]["evidence"], {"field": "password"})
        self.assertTrue(payload["hint"].endswith(server.RESUME_HINTS["page"]))
        call = payload["call"]
        # Still signed out within the wait: the stop again, with a new token for the same call.
        started = time.monotonic()
        result, again = self.resume(payload, wait=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((again["code"], again["call"], again["state"]["detail"]), ("needs_user", call, "sign_in"))
        self.assertNotEqual(again["resume_token"], payload["resume_token"])
        checks = [request for request in self.requests if request["path"] == "/api/needs-user"]
        self.assertGreater(len(checks), 1)
        self.assertEqual(checks[0]["body"], {"tabId": AGENT_TAB})
        self.assertEqual(len(self.asked()), 1, "a resume does not ask again")
        # Signed in: the sign-in is gone from the tab, so the request is done.
        self.extension.hint = None
        result, done = self.resume(again)
        self.assertFalse(result.isError)
        self.assertEqual(done, {"ok": True, "answer": "done", "via": "page", "reason": "sign_in", "tabId": AGENT_TAB,
                                "hint": "Check the page (browser_snapshot) before going on."})
        self.assertEqual(server._direct_checkpoints, {})

    def test_another_hint_or_none_leaves_nothing_to_wait_for(self):
        self.extension.hint = {"reason": "human_check", "evidence": {"widget": "recaptcha"}}
        for arguments in ({"reason": "sign_in"}, {"reason": "trusted_input"}, {"reason": "human_check", "tab_id": None}):
            with self.subTest(arguments=arguments), patch.object(playwright_tools, "_current_tab", None):
                result, payload = self.ask(**arguments)
                self.assertEqual(payload["code"], "needs_user")
                self.assertNotIn("resume_token", payload)
        # Asked about the hint it shows, it is waited for.
        result, payload = self.ask(reason="human_check")
        self.assertEqual(payload["state"]["evidence"], {"widget": "recaptcha"})
        self.assertIn("resume_token", payload)

    def test_without_browser_resume_there_is_no_resume_token(self):
        self.extension.hint = SIGN_IN
        with patch.dict(server.UNPUBLISHED_TOOLS, {"browser_resume": ("core", ["KITSUNE_MODE=act"])}):
            result, payload = self.ask(reason="sign_in")
        self.assertEqual(payload["state"]["evidence"], {"field": "password"})
        self.assertNotIn("resume_token", payload)

    def test_a_closed_tab_is_reported_and_nothing_is_asked(self):
        result, payload = self.ask(tab_id=99)
        self.assertEqual(payload["code"], "tab_closed")
        self.assertEqual(self.asked(), [])

    def test_the_bridge_checks_reason_and_message(self):
        for body in ({"reason": "captcha", "message": "Solve it"}, {"reason": "other", "message": "  "},
                     {"reason": "other"}):
            with self.subTest(body=body):
                result = asyncio.run(bridge._agent_request(body))
                self.assertEqual(result["code"], "invalid_argument")
        long = asyncio.run(bridge._agent_request({"reason": "other", "message": "x" * 5000}))
        self.assertEqual(len(long["state"]["message"]), bridge.AGENT_REQUEST_MESSAGE_CHARS)


class ElicitationTests(AskUserTestCase):
    def test_done_in_the_prompt_returns_the_answer(self):
        result, payload = self.ask(answer("Done"))
        self.assertFalse(result.isError, payload)
        self.assertEqual(payload, {"ok": True, "answer": "done", "via": "elicitation", "reason": "other",
                                   "tabId": AGENT_TAB, "hint": "Check the page (browser_snapshot) before going on."})
        (prompt,) = self.prompts
        self.assertEqual(prompt.requestedSchema, server.ASK_USER_SCHEMA)
        # The message is the agent's, labeled so, with the tab it is about.
        self.assertEqual(prompt.message.split("\n"), [
            "The agent asks: Which seat do you want?", "Tab: Sign in - https://shop.example/login",
            "Done tells the agent you did it; Can't tells it you could not."])
        self.assertEqual(server._direct_checkpoints, {})

    def test_cant_or_a_declined_prompt_is_the_users_answer(self):
        for respond in (answer("Can't"), answer(action="decline")):
            with self.subTest():
                result, payload = self.ask(respond)
                self.assertTrue(result.isError)
                self.assertEqual((payload["code"], payload["state"]["decision"]), ("needs_user", "declined"))
                self.assertEqual(payload["hint"], server.REQUEST_HINTS["declined"])
                self.assertNotIn("resume_token", payload)

    def test_a_dismissed_prompt_is_asked_again_by_browser_resume(self):
        self.extension.hint = SIGN_IN
        result, payload = self.ask(answer(action="cancel"), reason="sign_in", message="Sign in, please.")
        self.assertEqual((payload["code"], payload["state"]["decision"]), ("needs_user", "cancelled"))
        self.assertEqual(payload["hint"], f'{server.REQUEST_HINTS["cancelled"]} {server.RESUME_HINTS["ask"]}')
        result, unasked = self.resume(payload, answer("Done"), wait=0)
        self.assertEqual((unasked["code"], unasked["hint"].split(" browser_resume(")[0]),
                         ("needs_user", server.UNASKED_REQUEST_HINT))
        self.assertEqual(self.prompts, [])
        result, done = self.resume(unasked, answer("Done"))
        self.assertEqual((done["answer"], done["via"], done["reason"]), ("done", "elicitation", "sign_in"))
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(server._direct_checkpoints, {})

    def test_an_unanswered_prompt_closes_after_its_time(self):
        async def late(params):
            await anyio.sleep(1.5)
            return types.ElicitResult(action="accept", content={"answer": "Done"})

        with patch.object(server, "ASK_USER_SECONDS", 0.3):
            result, payload = self.ask(late)
        self.assertEqual(payload["state"]["decision"], "cancelled", "a late Done counts for nothing")
        self.assertIn("resume_token", payload)


class NeedsUserHintTests(AskUserTestCase):
    def test_a_snapshot_carries_the_pages_hint_without_failing(self):
        self.extension.hint = SIGN_IN
        result, payload = self.call("browser_snapshot", {})
        self.assertFalse(result.isError)
        self.assertEqual(payload["needs_user"], {"reason": "sign_in", "message": "Sign in on this page.",
                                                 "evidence": {"field": "password"}, "hint": bridge.NEEDS_USER_HINT})
        self.assertIn("browser_ask_user", bridge.NEEDS_USER_HINT)

    def test_messages_by_reason_and_evidence(self):
        for hint, message in (
                ({"reason": "sign_in", "evidence": {"field": "username"}}, "Sign in on this page."),
                ({"reason": "sign_in", "evidence": {"popup": "https://accounts.google.com/o/oauth2/v2/auth"}},
                 "Sign in: the browser blocked the sign-in window (accounts.google.com) the agent's click opened. "
                 "Click the sign-in button yourself."),
                ({"reason": "human_check", "evidence": {"widget": "turnstile"}},
                 "Complete the Cloudflare Turnstile check on this page."),
                ({"reason": "human_check"}, "Complete the verification check on this page."),
                ({"reason": "trusted_input", "evidence": {"popup_blocked": "link"}},
                 "Click the link yourself: the browser blocked the tab the agent's click opened.")):
            with self.subTest(hint=hint):
                described = bridge._described_needs_user({"ok": True, "needs_user": hint})["needs_user"]
                self.assertEqual(described["message"], message)
                self.assertEqual(described["hint"], bridge.NEEDS_USER_HINT)
        # Anything else the extension might send is left as it is.
        for result in ({"ok": True}, {"ok": True, "needs_user": {"reason": "approval"}}, {"needs_user": "x"}, None):
            self.assertIs(bridge._described_needs_user(result), result)


if __name__ == "__main__":
    unittest.main()
