"""Issue #326: viewing counts as using.

The extension refuses writes and navigations on the tab shown in the browser window the user last
focused (user_viewing, extension side in tests/shared_browser.cjs). The bridge turns a direct
call's refusal into a confirm step like user_tab's, and a client can lift the hold with
KITSUNE_HOLD_VIEWED_TAB=0.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, bridge_client, errors, playwright_tools, server, task_modes, tools
from kitsune.browser import Browser, Tab

VIEWED_TAB = 4
HELD_ACTIONS = {"click", "fill", "type", "checkBox", "selectOption", "keypress", "navigate", "goBack", "scroll",
                "executeJS", "hover"}


class FakeExtension:
    """Holds the shown tab VIEWED_TAB like background.js viewedTabError; tab 9 is the agent's.

    target: what describeTarget reports when ask-before-act inspects a target.
    """

    def __init__(self):
        self.sent = []
        self.target = {}

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        tab_id = params.get("tabId") or VIEWED_TAB
        if action == "describeTarget":
            return {"tabId": tab_id, "url": "https://user.example/",
                    "target": self.target if params.get("inspect") else {}}
        held = action in HELD_ACTIONS or (action == "screenPoint" and params.get("click"))
        navigation_approved = params.get("userTabApproved") and action in {"navigate", "goBack"}
        if (held and tab_id == VIEWED_TAB and params.get("holdViewedTab") is not False
                and not params.get("viewedTabApproved") and not navigation_approved):
            return {"error": f"The user is viewing tab {tab_id}; nothing was done", "code": "user_viewing",
                    "retryable": False, "hint": "extension hint",
                    "state": {"tabId": tab_id, "url": "https://user.example/", "windowId": 1,
                              "reason": "viewing", "browserFocused": False}}
        if action == "screenPoint":
            return {"screenX": 10, "screenY": 20, "viewportX": 1, "viewportY": 2, "title": "t"}
        if action == "executeJS":
            return {"result": "true"}
        return {"ok": True}

    def last(self, action):
        return [params for sent, params in self.sent if sent == action][-1]


class BridgeCase(unittest.TestCase):
    """Direct bridge requests with the extension faked by FakeExtension."""

    def setUp(self):
        self.extension = FakeExtension()
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": False,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
            patch.dict(bridge._viewed_tab_calls, clear=True),
            patch.dict(bridge._unknown_outcomes, clear=True),
            patch.object(bridge, "_audit_log", []),
            patch.object(bridge, "_global_command_lock", None),
            patch.object(bridge, "_tab_command_locks", {}),
            patch.object(bridge, "_scheduler_gate", None),
            patch.object(bridge, "_send_to_extension_raw", new=lambda *a, **k: self.extension(*a, **k)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, path, body, headers=None, status=200):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = path
        handler.headers = dict(headers or {})
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        handler.authorized = Mock(return_value=True)
        handler.do_POST()
        sent_status, result = handler.send_json.call_args.args
        if status is not None:
            self.assertEqual(sent_status, status, result)
        return result

    def refuse(self, path, body, headers=None):
        result = self.call(path, body, headers)
        self.assertEqual((result["code"], result["state"]["detail"], result["retryable"]), ("needs_user", "user_viewing", False), result)
        return result


class ViewedTabBridgeTests(BridgeCase):
    def test_direct_write_gets_a_token_that_approves_it_once(self):
        body = {"selector": "#go", "tabId": VIEWED_TAB}
        refused = self.refuse("/api/click", body)
        token = refused["state"]["confirmToken"]
        self.assertEqual(refused["state"]["expiresInSeconds"], bridge.CONFIRM_TOKEN_TTL)
        self.assertIn(f"tab_id={VIEWED_TAB}", refused["hint"])
        self.assertIn("(browser_tabs action=new) and work there", refused["hint"])
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))

        self.assertEqual(self.call("/api/click", {**body, "confirmToken": token}), {"ok": True})
        self.assertIs(self.extension.last("click")["viewedTabApproved"], True)
        self.assertNotIn("confirmToken", self.extension.last("click"))
        again = self.refuse("/api/click", {**body, "confirmToken": token})
        self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))
        self.assertTrue(any(entry.get("params", {}).get("reason") == "user_viewing"
                            for entry in bridge._audit_log), "the accepted approval is audited")

    def test_token_binds_the_call_and_the_refused_tab(self):
        refused = self.refuse("/api/navigate", {"url": "https://a.example/"})  # no tabId: the shown tab
        token = refused["state"]["confirmToken"]
        self.assertEqual(refused["state"]["tabId"], VIEWED_TAB)
        # Another URL, or the call without tab_id, does not carry it.
        self.refuse("/api/navigate", {"url": "https://b.example/", "tabId": VIEWED_TAB, "confirmToken": token})
        self.refuse("/api/navigate", {"url": "https://a.example/", "confirmToken": token})
        approved = {"url": "https://a.example/", "tabId": VIEWED_TAB, "confirmToken": token}
        self.assertEqual(self.call("/api/navigate", approved), {"ok": True})
        self.assertIs(self.extension.last("navigate")["viewedTabApproved"], True)

    def test_callers_cannot_release_the_hold_themselves(self):
        # userTabApproved approves only a navigation; the extension ignores it on other actions.
        for fields in ({"viewedTabApproved": True}, {"holdViewedTab": False}, {"userTabApproved": True}):
            with self.subTest(fields=fields):
                self.refuse("/api/click", {"selector": "#go", "tabId": VIEWED_TAB, **fields})
                sent = self.extension.last("click")
                self.assertNotIn("viewedTabApproved", sent)
                self.assertNotIn("holdViewedTab", sent)
        # The navigation routes strip a caller's own userTabApproved.
        self.refuse("/api/navigate", {"url": "https://a.example/", "tabId": VIEWED_TAB, "userTabApproved": True})
        self.assertNotIn("userTabApproved", self.extension.last("navigate"))

    def test_batch_steps_are_refused_without_a_token(self):
        result = self.call("/api/batch", {"commands": [
            {"action": "click", "selector": "#go", "tabId": VIEWED_TAB, "viewedTabApproved": True}]})
        step = result["results"][0]
        self.assertEqual(step["state"]["detail"], "user_viewing")
        self.assertNotIn("confirmToken", step["state"])
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))

    def test_client_setting_lifts_the_hold(self):
        headers = {task_modes.HOLD_VIEWED_TAB_HEADER: "0"}
        self.assertEqual(self.call("/api/click", {"selector": "#go", "tabId": VIEWED_TAB}, headers), {"ok": True})
        self.assertIs(self.extension.last("click")["holdViewedTab"], False)
        self.assertEqual(self.call("/api/batch", {"commands": [
            {"action": "fill", "selector": "#q", "value": "x", "tabId": VIEWED_TAB}]}, headers)["results"][0],
            {"ok": True})
        # Without the header the bridge adds nothing.
        self.call("/api/click", {"selector": "#go", "tabId": 9})
        self.assertNotIn("holdViewedTab", self.extension.last("click"))

    def test_restricted_modes_get_no_token(self):
        self.extension.target = {"sensitive": False}  # draft fills only inspected fields
        result = self.refuse("/api/fill", {"selector": "#q", "value": "x", "tabId": VIEWED_TAB},
                             {task_modes.MODE_HEADER: "draft"})
        self.assertEqual(result["hint"], bridge.RESTRICTED_VIEWED_TAB_HINT)
        self.assertNotIn("confirmToken", result["state"])
        self.assertEqual(bridge._confirm_tokens, {})

    def test_ask_before_act_approval_covers_the_hold(self):
        # One approval of a held submit: confirmation_required's token also passes the viewed tab.
        bridge._policy["ask"] = True
        self.extension.target = {"submits": True, "text": "Pay", "tag": "button"}
        body = {"selector": "#pay", "tabId": VIEWED_TAB}
        held = self.call("/api/click", body, status=403)
        self.assertEqual(held["state"]["detail"], "confirmation_required")
        self.assertEqual(self.call("/api/click", {**body, "confirmToken": held["state"]["confirmToken"]}),
                         {"ok": True})
        self.assertIs(self.extension.last("click")["viewedTabApproved"], True)

    def test_a_refusal_is_not_an_unknown_outcome(self):
        # retryable: false, but nothing ran: ask mode must not hold the repeat as unknown_outcome.
        bridge._policy["ask"] = True
        body = {"selector": "#go", "tabId": VIEWED_TAB}
        token = self.refuse("/api/click", body)["state"]["confirmToken"]
        self.assertEqual(bridge._unknown_outcomes, {})
        self.assertEqual(self.call("/api/click", {**body, "confirmToken": token}), {"ok": True})

    def test_trusted_click_screen_point_is_approved_by_the_token(self):
        # No OS input: the focus check and the click are mocked, and the real ones would fail the test.
        clicked = Mock(return_value=(False, {"backend": "test"}))
        with patch.object(bridge, "_focus_check_click", clicked), \
                patch.object(bridge, "_mac_click_backend", return_value=None), \
                patch.object(bridge, "_native_click_screen", side_effect=AssertionError("OS click")), \
                patch.object(bridge, "_focus_browser_window", side_effect=AssertionError("OS focus")):
            body = {"selector": "#go", "tabId": VIEWED_TAB}
            token = self.refuse("/api/trusted-click", body)["state"]["confirmToken"]
            clicked.assert_not_called()
            self.assertEqual(self.call("/api/trusted-click", {**body, "confirmToken": token})["ok"], True)
        self.assertIs(self.extension.last("screenPoint")["viewedTabApproved"], True)
        clicked.assert_called_once()


    def test_a_held_trusted_click_keeps_its_hold_ms_through_the_confirm_retry(self):
        # hold_ms (#351) is part of the call, so the token is bound to it.
        clicked = Mock(return_value=(False, {"backend": "test"}))
        with patch.object(bridge, "_focus_check_click", clicked), \
                patch.object(bridge, "_mac_click_backend", return_value=None), \
                patch.object(bridge, "_native_click_screen", side_effect=AssertionError("OS click")), \
                patch.object(bridge, "_focus_browser_window", side_effect=AssertionError("OS focus")):
            body = {"selector": "#go", "tabId": VIEWED_TAB, "holdMs": 800}
            token = self.refuse("/api/trusted-click", body)["state"]["confirmToken"]
            self.refuse("/api/trusted-click", {**body, "holdMs": 500, "confirmToken": token})
            clicked.assert_not_called()
            self.assertIs(self.call("/api/trusted-click", {**body, "confirmToken": token})["ok"], True)
        self.assertEqual(clicked.call_args.args[-1], 0.8, "the approved click holds for its hold_ms")

    def trusted_drag(self, body, dragged=None, unavailable=None):
        """POST /api/trusted-drag with the OS drag mocked: (result, the drag mock)."""
        drag = Mock(return_value=(True, dragged or {"ok": True, "backend": "test"}))
        with patch.object(bridge, "_mac_drag_unavailable", return_value=unavailable), \
                patch.object(bridge, "_focus_check_drag", drag), \
                patch.object(bridge, "_mac_drag_screen", side_effect=AssertionError("OS drag")), \
                patch.object(bridge, "_focus_browser_window", side_effect=AssertionError("OS focus")):
            return self.call("/api/trusted-drag", body), drag

    def test_a_trusted_drag_on_the_viewed_tab_needs_one_approval(self):
        body = {"selector": "#knob", "to": "#end", "tabId": VIEWED_TAB, "steps": 20}
        refused, drag = self.trusted_drag(body)
        self.assertEqual((refused["code"], refused["state"]["detail"], refused["retryable"]), ("needs_user", "user_viewing", False), refused)
        drag.assert_not_called()
        # Refused at the source, before the destination was measured or any tab shown.
        self.assertEqual([action for action, _ in self.extension.sent], ["screenPoint"])
        token = refused["state"]["confirmToken"]
        self.assertEqual(self.trusted_drag({**body, "steps": 5, "confirmToken": token})[0]["state"]["detail"], "user_viewing")
        dragged, drag = self.trusted_drag({**body, "confirmToken": token})
        self.assertIs(dragged.get("ok"), True, dragged)
        self.assertEqual(dragged["steps"], 20)
        drag.assert_called_once()
        points = [params for action, params in self.extension.sent if action == "screenPoint"][-2:]
        self.assertEqual([(p["selector"], p.get("viewedTabApproved")) for p in points], [("#knob", True), ("#end", True)])

    def test_a_trusted_drag_refusals_come_in_order(self):
        body = {"selector": "#knob", "to": "#end", "tabId": VIEWED_TAB}
        # Where no OS drag can run, nothing needs approval: unsupported_platform before the hold.
        unsupported = {"error": "Trusted drag is only implemented on macOS", "code": "unsupported_platform",
                       "retryable": False}
        result, _ = self.trusted_drag(body, unavailable=unsupported)
        self.assertEqual(result["code"], "unsupported_platform")
        self.assertEqual(self.extension.sent, [])
        # A drag that stops partway reports drag_interrupted after the approval, which is spent.
        token = self.trusted_drag(body)[0]["state"]["confirmToken"]
        interrupted = {"error": "The trusted drag stopped partway", "code": "drag_interrupted", "retryable": False}
        result, _ = self.trusted_drag({**body, "confirmToken": token}, dragged=interrupted)
        self.assertEqual(result["code"], "drag_interrupted", result)
        again, drag = self.trusted_drag({**body, "confirmToken": token})
        self.assertEqual((again["state"]["detail"], again["state"]["tokenRejected"]), ("user_viewing", "used"), again)
        drag.assert_not_called()

    def test_sync_barrier_returns_the_refusal_at_once(self):
        body = {"tabIds": [VIEWED_TAB], "predicate": "1", "timeout": 5000}
        started = time.monotonic()
        refused = self.call("/api/sync-barrier", body)
        self.assertLess(time.monotonic() - started, 2, "the barrier did not poll until its timeout")
        self.assertEqual((refused["ok"], refused["code"], refused["state"]["detail"]), (False, "needs_user", "user_viewing"), refused)
        self.assertNotIn("tab_id=", refused["hint"], "tabIds already names the tab")
        approved = self.call("/api/sync-barrier", {**body, "confirmToken": refused["state"]["confirmToken"]})
        self.assertIs(approved["ok"], True, approved)
        self.assertIs(self.extension.last("executeJS")["viewedTabApproved"], True)


class ViewedTabToolCallTests(BridgeCase):
    """Core tools that send several bridge requests need one approval (#326 review)."""

    def setUp(self):
        super().setUp()
        playwright_tools._current_tab = VIEWED_TAB
        self.addCleanup(setattr, playwright_tools, "_current_tab", None)

        def post(endpoint, data, timeout=15, start_bridge=True):
            # server._gateway_headers is what the MCP server's client sends: the tool call id.
            return self.call(endpoint, data, headers=server._gateway_headers(), status=None)
        post_patch = patch.object(tools, "_post", post)
        post_patch.start()
        self.addCleanup(post_patch.stop)

    def tool(self, function, **arguments):
        """The tool as the MCP server runs it (one tool call id), returning its result dict."""
        result = server._mcp_errors(function)(**arguments)
        return json.loads(result.content[0].text)

    FIELDS = [{"name": "Name", "type": "textbox", "target": "#name", "value": "Ada"},
              {"name": "Agree", "type": "checkbox", "target": "#agree", "value": "true"},
              {"name": "Plan", "type": "combobox", "target": "#plan", "value": "pro"}]

    def test_fill_form_needs_one_approval_for_all_fields(self):
        refused = self.tool(playwright_tools.browser_fill_form, fields=self.FIELDS)
        self.assertEqual((refused["state"]["detail"], refused["filled"]), ("user_viewing", []), refused)
        token = refused["state"]["confirmToken"]
        filled = self.tool(playwright_tools.browser_fill_form, fields=self.FIELDS, confirm_token=token)
        self.assertEqual(filled["filled"], ["Name", "Agree", "Plan"], filled)
        for action in ("fill", "checkBox", "selectOption"):
            self.assertIs(self.extension.last(action)["viewedTabApproved"], True, action)
        # The approval ends with its tool call: the token is spent, and a new call is held again.
        again = self.tool(playwright_tools.browser_fill_form, fields=self.FIELDS, confirm_token=token)
        self.assertEqual((again["state"]["detail"], again["state"]["tokenRejected"]), ("user_viewing", "used"), again)
        self.assertEqual(self.tool(playwright_tools.browser_click, target="#go")["state"]["detail"], "user_viewing")

    def test_type_with_submit_needs_one_approval(self):
        refused = self.tool(playwright_tools.browser_type, target="#q", text="hi", submit=True)
        self.assertEqual(refused["state"]["detail"], "user_viewing", refused)
        typed = self.tool(playwright_tools.browser_type, target="#q", text="hi", submit=True,
                          confirm_token=refused["state"]["confirmToken"])
        self.assertIs(typed.get("ok"), True, typed)
        self.assertIs(self.extension.last("fill")["viewedTabApproved"], True)
        self.assertIs(self.extension.last("keypress")["viewedTabApproved"], True)

    def test_type_with_submit_under_ask_before_act_ends_after_both_approvals(self):
        # The text needs the viewed-tab approval, the Enter in a submitting field ask-before-act's.
        bridge._policy["ask"] = True
        self.extension.target = {"submits": True, "tag": "input"}
        arguments = {"target": "#q", "text": "hi", "submit": True}
        viewed = self.tool(playwright_tools.browser_type, **arguments)
        self.assertEqual(viewed["state"]["detail"], "user_viewing", viewed)
        held = self.tool(playwright_tools.browser_type, **arguments, confirm_token=viewed["state"]["confirmToken"])
        self.assertEqual(held["state"]["detail"], "confirmation_required", held)
        self.assertNotIn("tokenRejected", held["state"], "the viewed-tab token was this tool call's")
        typed = self.tool(playwright_tools.browser_type, **arguments, confirm_token=held["state"]["confirmToken"])
        self.assertIs(typed.get("ok"), True, typed)
        self.assertIs(self.extension.last("keypress")["viewedTabApproved"], True)
        # The ask-before-act token carried the viewed-tab approval once.
        self.assertEqual(self.tool(playwright_tools.browser_type, **arguments,
                                   confirm_token=held["state"]["confirmToken"])["state"]["detail"], "user_viewing")

    def test_a_carrying_ask_token_on_another_request_is_held(self):
        # The ask token carries the viewed-tab approval only to the request that viewed-tab token approved.
        bridge._policy["ask"] = True
        self.extension.target = {"submits": True, "tag": "input"}
        arguments = {"target": "#q", "text": "hi", "submit": True}
        viewed = self.tool(playwright_tools.browser_type, **arguments)
        held = self.tool(playwright_tools.browser_type, **arguments, confirm_token=viewed["state"]["confirmToken"])
        token = held["state"]["confirmToken"]
        self.extension.target = {}  # a click ask-before-act does not hold
        clicked = self.tool(playwright_tools.browser_click, target="#other", confirm_token=token)
        self.assertEqual((clicked.get("state") or {}).get("detail"), "user_viewing", clicked)
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))
        # The token still finishes the call it was issued for.
        self.extension.target = {"submits": True, "tag": "input"}
        typed = self.tool(playwright_tools.browser_type, **arguments, confirm_token=token)
        self.assertIs(typed.get("ok"), True, typed)

    def test_the_tool_call_grant_expires_after_the_token_ttl(self):
        call = {task_modes.TOOL_CALL_HEADER: "c1"}
        body = {"selector": "#a", "tabId": VIEWED_TAB}
        token = self.refuse("/api/click", body, call)["state"]["confirmToken"]
        now = time.monotonic()
        with patch.object(bridge.time, "monotonic", return_value=now):
            self.assert_ok(self.call("/api/click", {**body, "confirmToken": token}, call))
        fill = {"selector": "#b", "value": "x", "tabId": VIEWED_TAB}
        with patch.object(bridge.time, "monotonic", return_value=now + bridge.CONFIRM_TOKEN_TTL - 1):
            self.assert_ok(self.call("/api/fill", fill, call))
        with patch.object(bridge.time, "monotonic", return_value=now + bridge.CONFIRM_TOKEN_TTL + 1):
            self.refuse("/api/fill", fill, call)

    def test_a_grant_belongs_to_its_client(self):
        mine = {task_modes.TOOL_CALL_HEADER: "c1", task_modes.CLIENT_HEADER: "1-aaaaaa"}
        other = {**mine, task_modes.CLIENT_HEADER: "2-bbbbbb"}
        body = {"selector": "#a", "tabId": VIEWED_TAB}
        token = self.refuse("/api/click", body, mine)["state"]["confirmToken"]
        self.assert_ok(self.call("/api/click", {**body, "confirmToken": token}, mine))
        fill = {"selector": "#b", "value": "x", "tabId": VIEWED_TAB}
        self.refuse("/api/fill", fill, other)  # the same call id from another client
        self.assert_ok(self.call("/api/fill", fill, mine))

    def test_approval_covers_only_its_tool_call_and_tab(self):
        call = lambda name: {task_modes.TOOL_CALL_HEADER: name}
        token = self.refuse("/api/click", {"selector": "#a", "tabId": VIEWED_TAB}, call("c1"))["state"]["confirmToken"]
        self.assert_ok(self.call("/api/click", {"selector": "#a", "tabId": VIEWED_TAB, "confirmToken": token},
                                 call("c2")))
        # Another request of the same tool call on that tab is approved, on another tab it is not.
        self.assert_ok(self.call("/api/fill", {"selector": "#b", "value": "x", "tabId": VIEWED_TAB}, call("c2")))
        self.assertIs(self.extension.last("fill")["viewedTabApproved"], True)
        self.call("/api/click", {"selector": "#c", "tabId": 9}, call("c2"))
        self.assertNotIn("viewedTabApproved", self.extension.last("click"))
        # Another tool call, or a request without one, is held again.
        self.refuse("/api/fill", {"selector": "#b", "value": "x", "tabId": VIEWED_TAB}, call("c3"))
        self.refuse("/api/fill", {"selector": "#b", "value": "x", "tabId": VIEWED_TAB})

    def assert_ok(self, result):
        self.assertIs(result.get("ok"), True, result)


class ViewedTabClientTests(unittest.TestCase):
    def test_raise_for_asks_for_confirmation(self):
        refused = {"error": "The user is viewing tab 4; nothing was done", "code": "user_viewing",
                   "retryable": False, "state": {"tabId": 4, "confirmToken": "tok", "expiresInSeconds": 120}}
        with self.assertRaises(errors.ConfirmationRequired) as raised:
            errors.raise_for(refused)
        self.assertEqual(raised.exception.token, "tok")
        # read and draft get no token: a plain error.
        with self.assertRaises(errors.KitsuneError) as plain:
            errors.raise_for({**refused, "state": {"tabId": 4}})
        self.assertNotIsInstance(plain.exception, errors.ConfirmationRequired)

    def test_the_hold_is_on_unless_set_to_zero(self):
        for value, held in ((None, True), ("1", True), ("0", False), ("off", False)):
            env = {k: v for k, v in os.environ.items() if k != "KITSUNE_HOLD_VIEWED_TAB"}
            if value is not None:
                env["KITSUNE_HOLD_VIEWED_TAB"] = value
            out = subprocess.run([sys.executable, "-c", "from kitsune import server; print(server.HOLD_VIEWED_TAB)"],
                                 env=env, text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(out, str(held), value)

    def test_mcp_sends_the_setting_only_when_off(self):
        for hold in (True, False):
            with patch.object(server._client, "hold_viewed_tab", hold), \
                    patch.object(bridge_client.httpx, "request") as request:
                request.return_value.status_code = 200
                request.return_value.json.return_value = {"ok": True}
                tools._post("/api/click", {"selector": "#go", "tabId": 4})
            headers = request.call_args.kwargs["headers"]
            self.assertEqual(headers.get(task_modes.HOLD_VIEWED_TAB_HEADER), None if hold else "0")

    def test_held_tools_take_a_confirm_token(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_scroll(tab_id=4, confirm_token="t")
            tools.browser_hover(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_focus(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_blur(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_reload_tab(tab_id=4, confirm_token="t")
            tools.browser_back(tab_id=4, confirm_token="t")
            tools.browser_forward(tab_id=4, confirm_token="t")
            tools.browser_hover(selector="#go", tab_id=4, duration_ms=400, confirm_token="t")
            tools.browser_trusted_drag(selector="#a", to_selector="#b", tab_id=4, confirm_token="t")
            playwright_tools.browser_hover(target="e1", confirm_token="t")
            playwright_tools.browser_navigate_back(tab_id=4, confirm_token="t")
        self.assertEqual([call.args[1].get("confirmToken") for call in post.call_args_list], ["t"] * 11)
        self.assertEqual(post.call_args_list[7].args[1]["durationMs"], 400)
        for name in ("browser_hover", "browser_navigate_back"):
            self.assertNotIn("confirm_token", playwright_tools.SCHEMAS[name]["inputSchema"]["properties"])

    def test_python_tab_methods_take_confirm(self):
        client = Mock()
        client.request.return_value = {"ok": True}
        tab = Tab(Browser(client), 4)
        for method, args in ((tab.back, ()), (tab.forward, ()), (tab.reload, ()), (tab.hover, ("#go",)),
                             (tab.scroll, ()), (tab.focus, ("#go",)), (tab.blur, ())):
            with self.subTest(method=method.__name__):
                method(*args, confirm="t")
                self.assertEqual(client.request.call_args.kwargs["json"]["confirmToken"], "t")


if __name__ == "__main__":
    unittest.main()
