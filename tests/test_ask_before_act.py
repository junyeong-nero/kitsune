"""Ask-before-act policy: held sensitive actions, action-bound one-time tokens, no auto-confirm."""
import asyncio
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, bridge_control, server, tools
from test_bridge_security import Bridge, _request

PAGE = "https://shop.example/products/1"


class FakeExtension:
    """Records extension commands; describeTarget reports the tab, URL and target."""

    def __init__(self, tab=7, url=PAGE, target=None, reply=None):
        self.tab, self.url, self.target = tab, url, target or {"ok": True, "tag": "a", "submits": False}
        self.reply = reply or {"ok": True}
        self.sent = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        if action == "describeTarget":
            if isinstance(self.target, dict) and self.target.get("error") and params.get("inspect"):
                return self.target
            result = {"tabId": params.get("tabId", self.tab), "url": self.url}
            if params.get("inspect"):
                result["target"] = self.target
            return result
        return self.reply

    def actions(self):
        return [action for action, _ in self.sent if action != "describeTarget"]


class AskBeforeActTestCase(unittest.TestCase):
    ask = True

    def setUp(self):
        self.extension = FakeExtension()
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": self.ask,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
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

    def handler(self, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        return handler

    def call(self, route, body):
        """Call a direct HTTP endpoint handler; return (status, result)."""
        handler = self.handler(body)
        route(handler)
        return handler.send_json.call_args.args

    def forward(self, action, body):
        return self.call(lambda h: h._forward(action), body)

    def held(self, route, body):
        status, result = self.call(route, body)
        self.assertEqual(status, 403)
        self.assertEqual(result["code"], "confirmation_required")
        self.assertIs(result["retryable"], True)
        return result

    def batch(self, *commands):
        return self.call(lambda h: h.handle_batch(), {"commands": list(commands)})[1]["results"]


class PolicyOffTests(AskBeforeActTestCase):
    ask = False

    def test_sensitive_actions_run_unchanged(self):
        for route, body, action in [
            (lambda h: h._forward("submitForm"), {"selector": "#pay"}, "submitForm"),
            (lambda h: h._forward("cookies"), {"op": "get"}, "cookies"),
            (lambda h: h._forward("clipboard"), {"op": "read"}, "clipboard"),
            (lambda h: h.handle_js(), {"code": "1"}, "executeJS"),
            (lambda h: h.handle_navigate(), {"url": "https://shop.example/checkout"}, "navigate"),
        ]:
            with self.subTest(action=action):
                self.extension.sent.clear()
                handler = self.handler(body)
                route(handler)
                handler.send_json.assert_called_once_with(200, {"ok": True})
                # No inspection round trip and one bridge-to-loop hop, as before.
                self.assertEqual(self.extension.sent, [(action, body)])
                handler.run_async.assert_called_once()

    def test_batch_runs_unchanged_and_token_is_stripped(self):
        results = self.batch({"action": "submitForm", "selector": "#pay", "confirmToken": "x"})
        self.assertEqual(results, [{"ok": True}])
        self.assertEqual(self.extension.sent, [("submitForm", {"selector": "#pay"})])

    def test_unknown_outcomes_are_not_tracked(self):
        self.extension.reply = {"error": "Command timed out", "code": "timeout", "retryable": False}
        self.forward("click", {"selector": "#go"})
        self.assertEqual(bridge._unknown_outcomes, {})


class ConfirmationTests(AskBeforeActTestCase):
    def test_ambiguous_click_fails_before_confirmation_including_containers(self):
        ambiguous = {"error": "3 elements match", "code": "ambiguous_target", "retryable": False,
                     "state": {"selector": "button", "matches": 3, "candidates": []}}
        self.extension.target = ambiguous
        for action in ("click", "doubleClick", "trustedClick", "clickAndWaitNavigation", "keypress"):
            body = {"selector": "button", "tabId": 7}
            if action == "keypress":
                body["key"] = "Enter"
            with self.subTest(action=action):
                result = (self.call(lambda h: h.handle_trusted_click(), body)[1] if action == "trustedClick"
                          else self.forward(action, body)[1])
                self.assertEqual(result, ambiguous)
                self.assertEqual(self.batch({"action": action, **body})[0], ambiguous)
        self.assertEqual(self.extension.actions(), [])
        self.assertEqual(bridge._confirm_tokens, {}, "no unusable confirmation is offered")

    def test_first_opt_in_is_forwarded_to_confirmation_inspection(self):
        self.extension.target = {"ok": True, "submits": True, "text": "Delete"}
        self.held(lambda h: h._forward("click"), {"selector": "button", "first": True})
        self.assertIs(self.extension.sent[0][1]["first"], True)

    def test_submit_is_held_with_action_bound_token(self):
        result = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay", "tabId": 7})
        self.assertEqual(self.extension.actions(), [], "nothing ran")
        state = result["state"]
        self.assertEqual((state["action"], state["reason"], state["tabId"], state["url"]),
                         ("submitForm", "submit", 7, PAGE))
        self.assertEqual(state["expiresInSeconds"], bridge.CONFIRM_TOKEN_TTL)
        self.assertEqual(state["params"], {"selector": "#pay", "tabId": 7})
        self.assertTrue(result["error"].startswith("Confirmation required: submits a form"))
        self.assertIn("confirmToken", result["hint"])
        self.assertGreaterEqual(len(state["confirmToken"]), 32)

    def test_valid_token_runs_exactly_that_action_once(self):
        body = {"selector": "#pay"}
        token = self.held(lambda h: h._forward("submitForm"), body)["state"]["confirmToken"]
        status, result = self.forward("submitForm", {**body, "confirmToken": token})
        self.assertEqual((status, result), (200, {"ok": True}))
        self.assertEqual(self.extension.sent[-1], ("submitForm", body), "token is not sent to the page")
        reused = self.held(lambda h: h._forward("submitForm"), {**body, "confirmToken": token})
        self.assertEqual(reused["state"]["tokenRejected"], "used")
        self.assertEqual(self.extension.actions(), ["submitForm"])

    def test_expired_token_is_rejected(self):
        token = self.held(lambda h: h._forward("submitForm"), {})["state"]["confirmToken"]
        bridge._confirm_tokens[token]["expires"] -= bridge.CONFIRM_TOKEN_TTL + 1
        result = self.held(lambda h: h._forward("submitForm"), {"confirmToken": token})
        self.assertEqual(result["state"]["tokenRejected"], "expired")
        self.assertEqual(self.extension.actions(), [])

    def test_unknown_token_is_rejected(self):
        result = self.held(lambda h: h._forward("submitForm"), {"confirmToken": "made-up"})
        self.assertEqual(result["state"]["tokenRejected"], "unknown")
        self.assertEqual(self.extension.actions(), [])

    def test_mismatched_action_params_or_tab_burns_the_token(self):
        for name, route, body, tab in [
            ("params", lambda h: h._forward("submitForm"), {"selector": "#other"}, 7),
            ("action", lambda h: h.handle_js(), {"code": "1"}, 7),
            ("tab", lambda h: h._forward("submitForm"), {"selector": "#pay"}, 8),
        ]:
            with self.subTest(mismatch=name):
                self.extension.tab = 7
                token = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay"})["state"]["confirmToken"]
                self.extension.tab = tab   # the active tab changed after approval
                result = self.held(route, {**body, "confirmToken": token})
                self.assertEqual(result["state"]["tokenRejected"], "mismatch")
                self.extension.tab = 7
                again = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay", "confirmToken": token})
                self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertEqual(self.extension.actions(), [])

    def test_audit_records_required_and_accepted_without_token(self):
        token = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay"})["state"]["confirmToken"]
        self.forward("submitForm", {"selector": "#pay", "confirmToken": token})
        entries = [(e["action"], e["params"].get("confirmation"), e["ok"], e["error"]) for e in bridge._audit_log]
        self.assertEqual(entries, [
            ("submitForm", "required", False, "confirmation_required"),
            ("submitForm", "accepted", True, None),
            ("submitForm", None, True, None),
        ])
        self.assertNotIn(token, repr(bridge._audit_log))


class SensitiveActionTests(AskBeforeActTestCase):
    def test_javascript_paths_are_held(self):
        for route, body in [
            (lambda h: h.handle_js(), {"code": "document.cookie"}),
            (lambda h: h.handle_js(), {"code": "document.cookie", "full": True}),
            (lambda h: h.handle_wait_for_result(), {"code": "window.ready"}),
            (lambda h: h.handle_sync_barrier(), {"tabIds": [7], "predicate": "1"}),
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.held(route, body)["state"]["reason"], "javascript")
        for command in ({"action": "js", "code": "1"}, {"action": "if", "condition": "1", "then": []},
                        {"action": "while", "condition": "1", "do": []}):
            with self.subTest(command=command):
                self.assertEqual(self.batch(command)[0]["code"], "confirmation_required")
        self.assertEqual(self.extension.actions(), [])

    def test_cookie_reads_and_sessions_are_held(self):
        self.assertEqual(self.held(lambda h: h._forward("cookies"), {"op": "get"})["state"]["reason"], "cookies")
        self.held(lambda h: h.handle_save_session(), {"name": "s", "urls": []})
        self.held(lambda h: h.handle_load_session(), {"name": "s"})
        self.assertEqual(self.extension.sent, [])

    def test_storage_clipboard_and_downloads_are_held_on_any_page(self):
        for action, body, reason in [
            ("storageOp", {"kind": "local", "op": "get", "key": "auth"}, "storage"),
            ("storageOp", {"kind": "session", "op": "set", "key": "k", "value": "v"}, "storage"),
            ("clipboard", {"op": "read"}, "clipboard"),
            ("clipboard", {"op": "write", "text": "x"}, "clipboard"),
            ("downloads", {"op": "download", "url": "https://example.com/a.zip"}, "downloads"),
            ("downloads", {"op": "list"}, "downloads"),
            ("downloads", {"op": "cancel", "query": {"id": 1}}, "downloads"),
        ]:
            with self.subTest(action=action, op=body["op"]):
                state = self.held(lambda h: h._forward(action), body)["state"]
                self.assertEqual((state["reason"], state["url"]), (reason, PAGE if action == "storageOp" else None))
                self.assertEqual(self.batch({"action": action, **body})[0]["code"], "confirmation_required")
        self.assertEqual(self.extension.actions(), [])
        token = self.held(lambda h: h._forward("clipboard"), {"op": "read"})["state"]["confirmToken"]
        self.assertEqual(self.forward("clipboard", {"op": "read", "confirmToken": token}), (200, {"ok": True}))
        self.assertEqual(self.extension.actions(), ["clipboard"])

    def test_payment_urls_and_pages_are_held(self):
        held = self.held(lambda h: h.handle_navigate(), {"url": "https://shop.example/Checkout/step1"})
        self.assertEqual(held["state"]["reason"], "payment_url")
        self.held(lambda h: h.handle_new_tab(), {"url": "https://pay.example/payment"})
        self.assertEqual(self.forward("navigate", {"url": "https://example.com/"}), (200, {"ok": True}))
        self.extension.url = "https://shop.example/checkout"
        held = self.held(lambda h: h.handle_type(), {"selector": "#card", "text": "4111"})
        self.assertEqual(held["state"]["reason"], "payment_page")
        self.assertEqual(self.extension.actions(), ["navigate"])

    def test_custom_ask_urls(self):
        bridge._policy["askUrls"] = [r"bank\.example"]
        self.held(lambda h: h.handle_navigate(), {"url": "https://bank.example/"})
        self.assertEqual(self.forward("navigate", {"url": "https://shop.example/checkout"}), (200, {"ok": True}))

    def test_submit_controls_are_held_other_clicks_run(self):
        self.extension.target = {"ok": True, "tag": "button", "type": "submit", "submits": True, "text": "Place order"}
        held = self.held(lambda h: h.handle_click(), {"ref": "s1e5"})
        self.assertEqual(held["state"]["reason"], "submit_control")
        self.assertIn("Place order", held["error"])
        inspected = [p for a, p in self.extension.sent if a == "describeTarget"][-1]
        self.assertEqual(inspected, {"ref": "s1e5", "inspect": True})
        self.extension.target = {"ok": True, "tag": "a", "submits": False}
        self.assertEqual(self.forward("click", {"ref": "s1e6"}), (200, {"ok": True}))
        self.assertEqual(self.extension.actions(), ["click"])

    def test_submit_labeled_buttons_are_held(self):
        # JS-driven buttons (type=button, role=button) the page labels like a submit or payment.
        self.extension.target = {"ok": True, "tag": "div", "submits": False, "submitLabel": True, "text": "결제하기"}
        held = self.held(lambda h: h.handle_click(), {"ref": "s1e7"})
        self.assertEqual(held["state"]["reason"], "submit_label")
        self.assertIn("clicks a button labeled like a submit or payment (결제하기)", held["error"])
        held = self.held(lambda h: h._forward("keypress"), {"key": "Enter", "ref": "s1e7"})
        self.assertIn("presses Enter on a button", held["error"])
        self.extension.target = {"ok": True, "tag": "button", "submits": False, "submitLabel": False,
                                 "text": "Submit search"}
        self.assertEqual(self.forward("click", {"ref": "s1e8"}), (200, {"ok": True}))
        self.assertEqual(self.extension.actions(), ["click"])

    def test_consequential_labeled_controls_are_held(self):
        # Buttons and links the page labels like a send, transfer, delete or account change.
        self.extension.target = {"ok": True, "tag": "a", "submits": False, "submitLabel": False,
                                 "consequentialLabel": True, "text": "Delete account"}
        held = self.held(lambda h: h.handle_click(), {"ref": "s1e9"})
        self.assertEqual(held["state"]["reason"], "consequential_label")
        self.assertIn("clicks a button or link labeled like a send, transfer, delete or account change "
                      "(Delete account)", held["error"])
        held = self.held(lambda h: h._forward("keypress"), {"key": "Enter", "ref": "s1e9"})
        self.assertIn("presses Enter on a button or link", held["error"])
        self.extension.target = {"ok": True, "tag": "button", "submits": False, "submitLabel": True,
                                 "consequentialLabel": True, "text": "Pay"}
        held = self.held(lambda h: h.handle_click(), {"ref": "s1e10"})
        self.assertEqual(held["state"]["reason"], "submit_label", "a submit label takes precedence")
        # An extension older than this bridge reports no consequentialLabel: such a click runs.
        self.extension.target = {"ok": True, "tag": "button", "submits": False, "submitLabel": False,
                                 "text": "Delete account"}
        self.assertEqual(self.forward("click", {"ref": "s1e11"}), (200, {"ok": True}))
        self.assertEqual(self.extension.actions(), ["click"])

    def test_enter_in_form_field_is_held(self):
        self.extension.target = {"ok": True, "tag": "input", "type": "text", "submits": True}
        for key in ("Enter", "Return"):
            with self.subTest(key=key):
                held = self.held(lambda h: h._forward("keypress"), {"key": key, "selector": "#q"})
                self.assertIn("presses Enter", held["error"])
                self.assertEqual([p for a, p in self.extension.sent if a == "describeTarget"][-1],
                                 {"selector": "#q", "key": key, "inspect": True})
        self.assertEqual(self.forward("keypress", {"key": "Tab", "selector": "#q"}), (200, {"ok": True}))

    def test_uninspectable_targets_fail_closed(self):
        self.extension.target = {"error": "Unknown action: describeTarget"}
        held = self.held(lambda h: h.handle_click(), {"selector": "#go"})
        self.assertEqual(held["state"]["reason"], "uninspectable_target")
        held = self.held(lambda h: h.handle_trusted_click(),
                         {"coords": {"x": 1, "y": 2}, "coordinateSpace": "screen"})
        self.assertEqual(held["state"]["reason"], "uninspectable_target")
        self.assertEqual(self.extension.actions(), [])

    def test_unknown_outcome_write_is_held_before_repeating(self):
        self.extension.reply = {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}
        self.assertEqual(self.forward("click", {"selector": "#send", "tabId": 7})[1]["code"], "timeout")
        held = self.held(lambda h: h._forward("click"), {"selector": "#send", "tabId": 7})
        self.assertEqual(held["state"]["reason"], "unknown_outcome")
        self.extension.reply = {"ok": True}
        token = held["state"]["confirmToken"]
        self.assertEqual(self.forward("click", {"selector": "#send", "tabId": 7, "confirmToken": token}),
                         (200, {"ok": True}))
        self.assertEqual(self.forward("click", {"selector": "#send", "tabId": 7}), (200, {"ok": True}))

    def test_error_page_write_is_not_held(self):
        # #86: nothing reached an unscriptable error page, so repeating the write is not held.
        self.extension.reply = {"error": "The tab shows an HTTP 404 error response with no scriptable document.",
                                "code": "error_page", "retryable": False}
        for _ in range(2):
            self.assertEqual(self.forward("click", {"selector": "#send", "tabId": 7})[1]["code"], "error_page")
        self.assertEqual(self.extension.actions(), ["click", "click"])
        self.assertEqual(bridge._unknown_outcomes, {})

    def test_ambiguous_target_write_is_not_held(self):
        # #133: an ambiguous target dispatched nothing, so its outcome is not unknown.
        self.extension.reply = {"error": "2 forms", "code": "ambiguous_target", "retryable": False}
        for _ in range(2):
            self.assertEqual(self.forward("click", {"selector": "#send", "tabId": 7})[1]["code"], "ambiguous_target")
        self.assertEqual(self.extension.actions(), ["click", "click"])
        self.assertEqual(bridge._unknown_outcomes, {})

    def test_unsupported_selector_write_is_not_held(self):
        # #219: an unparseable selector dispatched nothing, so its outcome is not unknown.
        self.extension.reply = {"error": "Unsupported selector: the :near() pseudo-class",
                                "code": "unsupported_selector", "retryable": False}
        for _ in range(2):
            result = self.forward("click", {"selector": "button:near(#a)", "tabId": 7})[1]
            self.assertEqual((result["code"], result["retryable"]), ("unsupported_selector", False))
        self.assertEqual(self.extension.actions(), ["click", "click"])
        self.assertEqual(bridge._unknown_outcomes, {})


    def test_expect_outcome_not_observed_is_held_before_repeating(self):
        # #67: a dispatched write whose expected outcome did not happen is retryable: false,
        # so repeating the identical call (expect included) needs confirmation.
        fake = self.extension
        original = fake.__call__

        async def replies(action, params=None, timeout=30):
            if action == "waitForText":
                fake.sent.append((action, dict(params or {})))
                return {"ok": False, "error": "Timed out waiting for text"}
            return await original(action, params, timeout)

        self.extension = replies
        body = {"selector": "#send", "tabId": 7, "expect": {"text": "Sent", "timeout": 50}}
        status, result = self.forward("click", body)
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertIs(result["retryable"], False)
        held = self.held(lambda h: h._forward("click"), body)
        self.assertEqual(held["state"]["reason"], "unknown_outcome")


class NoAutoConfirmTests(AskBeforeActTestCase):
    def test_batch_never_honors_tokens_and_stops_at_held_step(self):
        token = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay"})["state"]["confirmToken"]
        results = self.batch({"action": "submitForm", "selector": "#pay", "confirmToken": token},
                             {"action": "navigate", "url": "https://example.com/"})
        self.assertEqual(len(results), 1, "later steps do not run")
        self.assertEqual(results[0]["state"]["tokenRejected"], "not_direct")
        self.assertTrue(results[0]["_stopped"])
        # The token was spent by the batch attempt.
        again = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay", "confirmToken": token})
        self.assertEqual(again["state"]["tokenRejected"], "used")
        self.assertEqual(self.extension.actions(), [])

    def test_parallel_and_sequence_stop_at_held_step(self):
        parallel = self.batch({"action": "parallel", "sequences": [[
            {"action": "submitForm", "tabId": 7}, {"action": "click", "selector": "#x", "tabId": 7}]]})[0]
        self.assertEqual(len(parallel[0]), 1)
        sequence = self.batch({"action": "sequence", "do": [
            {"action": "js", "code": "1"}, {"action": "click", "selector": "#x"}]})[0]
        self.assertEqual(len(sequence["results"]), 1)
        self.assertEqual(self.extension.actions(), [])

    def test_retry_stops_and_never_confirms(self):
        token = self.held(lambda h: h._forward("submitForm"), {"selector": "#pay"})["state"]["confirmToken"]
        for command in ({"action": "submitForm", "selector": "#pay"},
                        {"action": "submitForm", "selector": "#pay", "confirmToken": token}):
            with self.subTest(command=command):
                status, result = self.call(lambda h: h.handle_retry(),
                                           {"command": command, "maxAttempts": 3, "backoffMs": 0})
                self.assertEqual((result["ok"], result["attempts"], result["reason"]),
                                 (False, 1, "confirmation required"))
                self.assertEqual(result["result"]["code"], "confirmation_required")
        self.assertEqual(self.extension.actions(), [])

    def test_broadcast_and_workflow_never_confirm(self):
        result = self.call(lambda h: h.handle_broadcast(),
                           {"tabIds": [7, 8], "command": {"action": "submitForm", "confirmToken": "x"}})[1]
        self.assertEqual({r["code"] for r in result["results"].values()}, {"confirmation_required"})
        self.assertEqual(self.extension.actions(), [])


class LockedPolicyTests(AskBeforeActTestCase):
    def set_policy(self, body):
        return self.call(lambda h: h.handle_set_policy(), body)

    def test_agent_settable_mode_can_be_turned_off(self):
        self.assertEqual(self.set_policy({"ask": False})[0], 200)
        self.assertFalse(bridge._policy["ask"])

    def test_locked_mode_only_tightens(self):
        bridge._policy["askLocked"] = True
        for body in ({"ask": False}, {"askUrls": []}, {"askUrls": ["checkout"]},
                     {"ask": False, "readonly": True}):
            with self.subTest(body=body):
                status, result = self.set_policy(body)
                self.assertEqual((status, result["code"]), (403, "policy_locked"))
                self.assertTrue(bridge._policy["ask"])
                self.assertFalse(bridge._policy["readonly"], "nothing applied")
                self.assertEqual(bridge._policy["askUrls"], bridge.DEFAULT_ASK_URLS)
        status, result = self.set_policy({"askUrls": bridge.DEFAULT_ASK_URLS + ["bank"], "ask": True})
        self.assertEqual(status, 200)
        self.assertIn("bank", result["policy"]["askUrls"])

    def test_invalid_ask_values_are_rejected_unchanged(self):
        invalid = [{"askUrls": "checkout"}, {"askUrls": None}, {"askUrls": {"checkout": 1}},
                   {"askUrls": ["checkout", ""]}, {"askUrls": ["checkout", 3]}, {"askUrls": ["("]},
                   {"ask": "false"}, {"ask": 1}, {"ask": None},
                   {"ask": "yes", "readonly": True}, {"askUrls": "bank", "ask": True}]
        for locked in (False, True):
            for body in invalid:
                bridge._policy.update(ask=locked, askLocked=locked, readonly=False,
                                      askUrls=list(bridge.DEFAULT_ASK_URLS))
                with self.subTest(locked=locked, body=body):
                    status, result = self.set_policy(body)
                    self.assertEqual((status, result["code"]), (400, "invalid_policy"))
                    self.assertTrue(result["error"].startswith("Invalid policy: "))
                    self.assertEqual((bridge._policy["ask"], bridge._policy["readonly"],
                                      bridge._policy["askUrls"]),
                                     (locked, False, bridge.DEFAULT_ASK_URLS), "nothing applied")
        bridge._policy.update(ask=False, askLocked=False)
        status, result = self.set_policy({"ask": True, "askUrls": [r"bank\.example", "/pay\\b"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["policy"]["askUrls"], [r"bank\.example", "/pay\\b"])
        self.assertIs(result["policy"]["ask"], True)

    def start_bridge(self, home, args=(), env=None, lock_file=None):
        """Run bridge.run(args) up to its event loop with home as KITSUNE_HOME; return the policy."""
        environ = {k: v for k, v in os.environ.items() if k != bridge.ASK_BEFORE_ACT_ENV}
        with patch.dict(os.environ, {**environ, **(env or {})}, clear=True), \
                patch.dict(bridge._policy, {"ask": False, "askLocked": False}), \
                patch.object(bridge, "ASK_LOCK_FILE", lock_file or os.path.join(home, "ask-before-act-locked")), \
                patch.object(bridge_control, "PID_FILE", os.path.join(home, "bridge.pid")), \
                patch.object(bridge.fcntl, "flock"), \
                patch.object(bridge, "main", new=Mock(return_value="coroutine")), \
                patch.object(bridge.asyncio, "run"):
            bridge.run(list(args))
            return bridge._policy["ask"], bridge._policy["askLocked"]

    def test_bridge_start_locks_ask_mode(self):
        for args, env in [(["--ask-before-act"], {}), ([], {bridge.ASK_BEFORE_ACT_ENV: "1"}),
                          (["start"], {}), ([], {bridge.ASK_BEFORE_ACT_ENV: "0"})]:
            locked = args == ["--ask-before-act"] or env.get(bridge.ASK_BEFORE_ACT_ENV) == "1"
            with self.subTest(args=args, env=env), tempfile.TemporaryDirectory() as home:
                self.assertEqual(self.start_bridge(home, args, env), (locked, locked))
                lock_file = Path(home, "ask-before-act-locked")
                self.assertEqual(lock_file.exists(), locked)
                if locked:
                    self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)
                    self.assertIn("delete this file", lock_file.read_text(encoding="utf-8"))

    def test_lock_survives_restart_until_file_is_deleted(self):
        with tempfile.TemporaryDirectory() as home:
            self.start_bridge(home, env={bridge.ASK_BEFORE_ACT_ENV: "1"})
            # A restart from an environment without the variable, or with it turned off.
            for env in ({}, {bridge.ASK_BEFORE_ACT_ENV: "0"}):
                with self.subTest(env=env):
                    self.assertEqual(self.start_bridge(home, env=env), (True, True))
            os.remove(os.path.join(home, "ask-before-act-locked"))
            self.assertEqual(self.start_bridge(home), (False, False))

    def test_unreadable_or_symlinked_lock_file_still_locks(self):
        with tempfile.TemporaryDirectory() as home:
            os.symlink(os.path.join(home, "missing"), os.path.join(home, "ask-before-act-locked"))
            self.assertEqual(self.start_bridge(home), (True, True))
            self.assertFalse(os.path.exists(os.path.join(home, "missing")), "the link is not followed")

    def test_lock_that_cannot_be_persisted_still_locks_this_bridge(self):
        with tempfile.TemporaryDirectory() as home:
            read_only = os.path.join(home, "read-only")
            os.mkdir(read_only, 0o500)
            try:
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(self.start_bridge(home, ["--ask-before-act"],
                                                       lock_file=os.path.join(read_only, "lock")), (True, True))
            finally:
                os.chmod(read_only, 0o700)
            self.assertIn("locked for this bridge only", out.getvalue())

    def test_flag_only_applies_to_start(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
            bridge.run(["status", "--ask-before-act"])
        self.assertEqual(raised.exception.code, 2)


class PersistedLockProcessTests(unittest.TestCase):
    """A real bridge (temporary KITSUNE_HOME, ephemeral ports) restarted without the variable."""

    def policy(self, home, env=None):
        process = Bridge(home)
        process.env.update(env or {})
        process.start()
        try:
            status, _, body = _request(process.http_port, "GET", "/api/get-policy", process.auth(), process.host)
            self.assertEqual(status, 200, body)
            return json.loads(body)
        finally:
            process.stop()

    def test_restarted_bridge_stays_locked(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertTrue(self.policy(home, {bridge.ASK_BEFORE_ACT_ENV: "1"})["askLocked"])
            policy = self.policy(home)
            self.assertEqual((policy["ask"], policy["askLocked"]), (True, True))
            os.remove(os.path.join(home, "ask-before-act-locked"))
            self.assertFalse(self.policy(home)["askLocked"])


class ServerToolTests(unittest.TestCase):
    def test_confirm_token_is_forwarded_only_when_given(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_submit_form(selector="#pay", tab_id=7)
            post.assert_called_with("/api/submit-form", {"selector": "#pay", "tabId": 7})
            tools.browser_submit_form(selector="#pay", tab_id=7, confirm_token="t")
            post.assert_called_with("/api/submit-form", {"selector": "#pay", "tabId": 7, "confirmToken": "t"})
            tools.browser_js("1", confirm_token="t")
            post.assert_called_with("/api/js", {"code": "1", "confirmToken": "t"})
            tools.browser_cookies("get", confirm_token="t")
            post.assert_called_with("/api/cookies", {"op": "get", "secure": False, "confirmToken": "t"})
            tools.browser_clipboard("read", confirm_token="t")
            post.assert_called_with("/api/clipboard", {"op": "read", "confirmToken": "t"})
            tools.browser_downloads("list")
            post.assert_called_with("/api/downloads", {"op": "list"})
            tools.browser_downloads("list", confirm_token="t")
            post.assert_called_with("/api/downloads", {"op": "list", "confirmToken": "t"})
            tools.browser_set_policy(ask=True, ask_urls=["checkout"])
            post.assert_called_with("/api/set-policy", {"ask": True, "askUrls": ["checkout"]})

    def test_annotations_are_unchanged_hints(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertTrue(tools["browser_tabs"].annotations.destructiveHint)
        self.assertFalse(tools["browser_click"].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
