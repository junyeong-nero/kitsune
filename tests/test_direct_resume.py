"""#376: a direct call stopped on needs_user keeps a checkpoint in the MCP server, and browser_resume
continues it: from the held request, through the current policy, asking again only with elicitation.

Uses the in-process MCP sessions and bridge of test_elicitation_approval.
"""
import hashlib
import json
import time
import unittest
from unittest.mock import patch

from mcp import types
from test_elicitation_approval import AGENT_TAB, VIEWED_TAB, ElicitationTestCase, FakeExtension, answer_with

from kitsune import bridge, playwright_tools, server, waits


class Extension(FakeExtension):
    """FakeExtension whose user can look away from VIEWED_TAB (viewed False), or start viewing it after
    an action (view_after)."""

    def __init__(self):
        super().__init__()
        self.viewed = True
        self.view_after = None

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        if self.viewed or params.get("tabId") != VIEWED_TAB or action == "describeTarget":
            result = await super().__call__(action, params, timeout)
        else:
            self.sent.append((action, params))
            result = {"ok": True}
        if action == self.view_after:
            self.viewed = True
        return result


class ResumeTestCase(ElicitationTestCase):
    def setUp(self):
        super().setUp()
        self.extension = Extension()
        patcher = patch.dict(server._direct_checkpoints, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resume(self, payload, answer=None, **arguments):
        return self.call("browser_resume", {"call": payload["call"], "resume_token": payload["resume_token"],
                                            **arguments}, answer)

    def paths(self, path):
        return [request for request in self.requests if request["path"] == path]


class ViewedTabResumeTests(ResumeTestCase):
    def hold(self, answer=None):
        result, payload = self.call("browser_click", {"target": "#send"}, answer)
        self.assertEqual(payload["code"], "needs_user", payload)
        return payload

    def test_a_held_call_keeps_a_checkpoint_with_only_a_hash_of_its_token(self):
        payload = self.hold()
        call, token = payload["call"], payload["resume_token"]
        self.assertIs(type(call), int)
        self.assertIn(payload["state"]["confirmToken"], bridge._confirm_tokens, "without elicitation it stays")
        self.assertTrue(payload["hint"].endswith(server.RESUME_HINTS["token"]))
        checkpoint = server._direct_checkpoints[call]
        self.assertEqual(checkpoint["token"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual((checkpoint["tool"], checkpoint["arguments"], checkpoint["tabId"]),
                         ("browser_click", {"target": "#send"}, VIEWED_TAB))
        self.assertNotIn(token, json.dumps(server._direct_checkpoints, default=str))
        self.assertNotIn("confirmToken", checkpoint["held"]["state"])
        # call is the audit call id of the hold.
        self.assertEqual([(e["action"], e["error"] is not None) for e in bridge._audit_log if e.get("call") == call],
                         [("click", True)])

    def test_without_elicitation_a_resume_checks_the_hold_once_and_returns_at_once(self):
        payload = self.hold()
        started = time.monotonic()
        result, again = self.resume(payload, wait=300)
        self.assertLess(time.monotonic() - started, 5, "a resume never waits for a hold to lift")
        self.assertEqual((again["code"], again["state"]["detail"], again["call"]),
                         ("needs_user", "user_viewing", payload["call"]))
        self.assertNotEqual(again["resume_token"], payload["resume_token"])
        self.assertNotEqual(again["state"]["confirmToken"], payload["state"]["confirmToken"])
        self.assertIn(again["state"]["confirmToken"], bridge._confirm_tokens)
        self.assertEqual(len(self.clicks()), 2, "the held click was checked again once")
        self.assertFalse(any(p.get("viewedTabApproved") for p in self.extension.actions("click")))
        # The old token stopped working; the new one still does.
        result, refused = self.resume(payload)
        self.assertEqual(refused["code"], "resume_token_invalid")
        self.assertEqual(len(self.clicks()), 2)
        self.assertEqual(self.resume(again)[1]["code"], "needs_user")

    def test_a_resume_runs_the_call_only_when_a_new_call_would_too(self):
        payload = self.hold()
        # The user still views the tab: a new call is held, and so is the resume.
        self.assertEqual(self.call("browser_click", {"target": "#send"})[1]["code"], "needs_user")
        payload = self.resume(payload)[1]
        self.assertEqual(payload["code"], "needs_user")
        # The user looked away: a new call runs, and so does the resume, without any approval.
        self.extension.viewed = False
        self.assertIs(self.call("browser_click", {"target": "#other"})[1].get("ok"), True)
        result, resumed = self.resume(payload)
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertNotIn("resume_token", resumed)
        self.assertNotIn(payload["call"], server._direct_checkpoints)
        self.assertEqual([p["selector"] for p in self.extension.actions("click")[-2:]], ["#other", "#send"])
        self.assertNotIn("viewedTabApproved", self.extension.actions("click")[-1])
        # Its checkpoint is gone: the bridge answers.
        self.assertEqual(self.resume(payload)[1]["code"], "checkpoint_not_found")

    def test_with_elicitation_a_resume_asks_again_and_runs_the_approved_call(self):
        payload = self.hold(answer_with("decline"))
        self.assertEqual(payload["state"]["decision"], "declined")
        self.assertNoToken()
        result, resumed = self.resume(payload, answer_with("accept", "Approve"))
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("Allow the agent's click in the tab you are viewing (Inbox)?", self.prompts[0].message)
        self.assertIs(self.extension.actions("click")[-1]["viewedTabApproved"], True)
        self.assertEqual(bridge._audit_log[-1]["approval"], "elicitation")
        self.assertNoToken()
        self.assertEqual(server._direct_checkpoints, {})

    def test_a_resume_prompt_ends_at_wait(self):
        payload = self.hold(answer_with("cancel"))
        # The user approves after wait: too late.
        result, again = self.resume(payload, answer_with("accept", "Approve", delay=3), wait=1)
        self.assertEqual((again["code"], again["state"]["decision"], again["call"]),
                         ("needs_user", "cancelled", payload["call"]))
        self.assertNotIn("viewedTabApproved", self.extension.actions("click")[-1])
        # wait=0 asks nothing; a decline is reported as such, still resumable.
        result, unasked = self.resume(again, answer_with("accept", "Approve"), wait=0)
        self.assertEqual(self.prompts, [])
        self.assertNotIn("decision", unasked["state"])
        self.assertEqual(unasked["hint"], f'{server.UNASKED_APPROVAL_HINT} {server.RESUME_HINTS["elicitation"]}')
        result, declined = self.resume(unasked, answer_with("decline"))
        self.assertEqual((declined["state"]["decision"], declined["call"]), ("declined", payload["call"]))
        self.assertIn("resume_token", declined)
        self.assertNoToken()
        self.assertFalse(any(p.get("viewedTabApproved") for p in self.extension.actions("click")))

    def test_wait_is_capped(self):
        self.assertEqual([waits.resume_wait_seconds(w) for w in (1000, 300, 12.5, 0, -5, None, True, float("nan"))],
                         [300, 300, 12.5, 0, 0, 120, 120, 120])
        payload = self.hold(answer_with("decline"))
        limits = []

        async def ask(session, request, name, arguments, state, limit=None):
            limits.append(limit)
            return "declined"

        with patch.object(server, "_ask_user", ask):
            again = self.resume(payload, answer_with("decline"), wait=1000)[1]
            self.resume(again, answer_with("decline"))
        self.assertEqual(limits, [waits.RESUME_MAX_WAIT_SECONDS, waits.RESUME_WAIT_SECONDS])

    def test_a_resume_refuses_another_current_tab_and_observed(self):
        payload = self.hold()
        with patch.object(playwright_tools, "_current_tab", AGENT_TAB):
            result, refused = self.resume(payload)
        self.assertEqual((refused["code"], refused["state"]["tabId"], refused["state"]["currentTab"]),
                         ("current_tab_changed", VIEWED_TAB, AGENT_TAB))
        result, refused = self.resume(payload, observed={"0": "applied"})
        self.assertEqual(refused["code"], "observed_invalid")
        self.assertEqual(len(self.clicks()), 1, "nothing was sent")
        # The checkpoint is kept.
        self.assertEqual(self.resume(payload)[1]["code"], "needs_user")

    def test_another_call_goes_to_the_bridge(self):
        result, payload = self.call("browser_resume", {"call": 987654, "resume_token": "x" * 32})
        self.assertEqual(payload["code"], "checkpoint_not_found")
        self.assertEqual(len(self.paths("/api/resume")), 1)

    def test_type_whose_enter_is_held_on_the_viewed_tab_resumes_at_the_enter(self):
        # The user starts viewing the tab after the text was typed: the bridge names the held action.
        self.extension.viewed, self.extension.view_after = False, "type"
        result, payload = self.call("browser_type", {"target": "#q", "text": "hi", "slowly": True, "submit": True})
        self.assertEqual((payload["code"], payload["state"]["detail"], payload["state"]["action"]),
                         ("needs_user", "user_viewing", "keypress"))
        result, resumed = self.resume(payload, answer_with("accept", "Approve"))
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertEqual([p["text"] for p in self.extension.actions("type")], ["hi"], "typed once")
        self.assertEqual(len(self.paths("/api/type")), 1)
        # Held in the call, held again in the resume, then pressed once approved.
        self.assertEqual([p.get("viewedTabApproved") for p in self.extension.actions("keypress")], [None, None, True])

    def test_a_page_state_stop_waits_for_its_check(self):
        checks = []

        def signed_in(state):
            checks.append(state["reason"])
            return len(checks) >= done_after

        held = {"error": "Sign in", "code": "needs_user", "retryable": False, "call": 77, "hint": "Sign in first.",
                "state": {"reason": "sign_in", "tabId": AGENT_TAB, "message": "Sign in to continue"}}
        payload = {"call": 77, "resume_token": server._save_direct_checkpoint(
            77, tool="browser_click", arguments={"target": "#next"}, held=held, tabId=AGENT_TAB)}
        with (patch.object(playwright_tools, "_current_tab", AGENT_TAB),
              patch.dict(server.PAGE_STATE_CHECKS, {"sign_in": signed_in}),
              patch.object(server, "PAGE_STATE_POLL_SECONDS", 0.05)):
            done_after = 1000
            started = time.monotonic()
            result, again = self.resume(payload, wait=0.3)
            self.assertLess(time.monotonic() - started, 2)
            self.assertGreater(len(checks), 1)
            self.assertEqual((again["code"], again["state"]["reason"], again["call"]), ("needs_user", "sign_in", 77))
            self.assertEqual(again["hint"], f'Sign in first. {server.RESUME_HINTS["page"]}')
            self.assertEqual(self.clicks(), [], "nothing was sent while the user signs in")
            checks.clear()
            done_after = 2
            result, resumed = self.resume(again)
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertEqual([p["selector"] for p in self.extension.actions("click")], ["#next"])
        self.assertEqual(server._direct_checkpoints, {})


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(server._direct_checkpoints, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_bridge_rules_for_container_checkpoints_apply(self):
        self.assertEqual((server.RESUME_MAX_CHECKPOINTS, server.RESUME_TTL_SECONDS),
                         (bridge.RESUME_MAX_CHECKPOINTS, bridge.RESUME_TTL_SECONDS))
        tokens = {call: server._save_direct_checkpoint(call, tool="browser_click") for call in range(1, 22)}
        self.assertEqual(list(server._direct_checkpoints), list(range(2, 22)), "the oldest is dropped")
        self.assertIsNone(server._direct_checkpoint(1, tokens[1]))
        self.assertIsNone(server._direct_checkpoint(2, tokens[3]))
        self.assertIsNotNone(server._direct_checkpoint(2, tokens[2]))
        now = time.monotonic()
        with patch.object(server, "_checkpoint_clock", return_value=now + server.RESUME_TTL_SECONDS + 1):
            self.assertIsNone(server._direct_checkpoint(2, tokens[2]))
            self.assertEqual(server._direct_checkpoints, {})

    def test_only_direct_stops_the_user_can_resolve_are_resumable(self):
        held = {"code": "needs_user", "call": 3, "state": {"reason": "approval", "approvable": True}}
        self.assertTrue(server.mcp._resumable("browser_click", held))
        for name, payload in (
                ("browser_click", {**held, "state": {"reason": "approval", "approvable": False}}),  # read, draft
                ("browser_snapshot", held),  # takes no confirm token
                ("browser_click", {**held, "call": None}),
                ("browser_click", {**held, "resume_token": "x"}),
                ("browser_click", {**held, "state": {"reason": "sign_in"}}),  # no tab to check
                ("browser_click", {"results": [held], "status": "held", "call": 3})):
            with self.subTest(name=name, payload=payload):
                self.assertFalse(server.mcp._resumable(name, payload))

    def test_a_container_step_hold_takes_no_call_of_its_own(self):
        hold = bridge._direct_hold.set({"call": None})
        self.addCleanup(bridge._direct_hold.reset, hold)
        parent = bridge._audit_parent.set({"call": 5, "path": []})
        self.assertIsNone(bridge._hold_call())
        bridge._audit_parent.reset(parent)
        call = bridge._hold_call()
        self.assertIs(type(call), int)
        self.assertEqual(bridge._hold_call(), call, "one per request")


class AskBeforeActResumeTests(ResumeTestCase):
    ask = True

    def setUp(self):
        super().setUp()
        patcher = patch.object(playwright_tools, "_current_tab", AGENT_TAB)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fill_form_resumes_at_the_held_field(self):
        self.extension.url = "https://shop.example/checkout"
        fields = [{"name": name, "type": "textbox", "target": f"#{name}", "value": value}
                  for name, value in (("card", "4242"), ("expiry", "12/30"), ("cvc", "123"), ("zip", "10001"))]
        answers = iter(("Approve", "Decline"))

        async def approve_then_decline(params):
            return types.ElicitResult(action="accept", content={"decision": next(answers)})

        result, payload = self.call("browser_fill_form", {"fields": fields}, approve_then_decline)
        self.assertEqual((payload["code"], payload["filled"]), ("needs_user", ["card"]))
        self.requests.clear()
        # Without elicitation the resume sends the held field again, and it is held again.
        result, again = self.resume(payload)
        self.assertEqual((again["code"], again["filled"], again["call"]), ("needs_user", ["card"], payload["call"]))
        self.assertEqual([r["body"]["selector"] for r in self.paths("/api/fill")], ["#expiry"])
        # Approved field by field: the first field is never filled again.
        result, resumed = self.resume(again, answer_with("accept", "Approve"))
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertEqual(resumed["filled"], ["card", "expiry", "cvc", "zip"])
        self.assertEqual([p["selector"] for p in self.extension.actions("fill")], ["#card", "#expiry", "#cvc", "#zip"])
        self.assertNotIn("#card", [r["body"]["selector"] for r in self.paths("/api/fill")])

    def test_type_and_submit_resumes_at_the_enter(self):
        result, payload = self.call("browser_type", {"target": "#q", "text": "hi", "slowly": True, "submit": True})
        self.assertEqual((payload["code"], payload["state"]["action"]), ("needs_user", "keypress"))
        result, again = self.resume(payload)
        self.assertEqual(again["code"], "needs_user")
        result, resumed = self.resume(again, answer_with("accept", "Approve"))
        self.assertIs(resumed.get("ok"), True, resumed)
        self.assertEqual(len(self.paths("/api/type")), 1, "the text was typed once")
        self.assertEqual([p["text"] for p in self.extension.actions("type")], ["hi"])
        self.assertEqual(len(self.extension.actions("keypress")), 1)


if __name__ == "__main__":
    unittest.main()
