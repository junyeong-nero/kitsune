"""Action outcome contract: a dispatched action is not proof that its expected outcome happened."""
import asyncio
import re
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge, server

TAB = {"id": 7, "url": "https://shop.test/checkout", "title": "Checkout", "active": True}


class FakeExtension:
    """Scripted send_to_extension: replies per action (the last reply repeats), records calls."""

    def __init__(self, **replies):
        self.replies = {action: list(reply) if isinstance(reply, list) else [reply]
                        for action, reply in replies.items()}
        self.calls = []

    async def __call__(self, action, params=None, timeout=30):
        self.calls.append((action, dict(params or {})))
        queue = self.replies[action]
        return dict(queue.pop(0) if len(queue) > 1 else queue[0])

    def actions(self):
        return [action for action, _ in self.calls]


def run_expect(extension, action, params, expect):
    spec, problem = bridge._parse_expect(action, expect)
    assert problem is None, problem
    with patch.object(bridge, "send_to_extension", extension), \
         patch.object(bridge, "OUTCOME_POLL_SECONDS", 0):
        return asyncio.run(bridge._send_with_expectation(action, params, spec))


class OutcomeNotObservedTests(unittest.TestCase):
    def assert_not_observed(self, result, retryable=False):
        self.assertIs(result["dispatched"], True)
        self.assertIs(result["ok"], False)
        self.assertIs(result["outcome"]["met"], False)
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertIs(result["retryable"], retryable)
        self.assertTrue(result["error"].startswith("Expected outcome not observed within"))
        self.assertIn("browser_snapshot", result["hint"])
        self.assertEqual(result["state"], result["outcome"]["state"])

    def test_blocked_submit_reports_unchanged_url_and_current_state(self):
        extension = FakeExtension(
            getTabs={"tabs": [TAB]},
            submitForm={"ok": True, "snapshot": "- form\n  - textbox \"Card\" [ref=s3e1]"},
            waitForUrl={"ok": False, "url": TAB["url"], "error": "Timed out"},
        )
        result = run_expect(extension, "submitForm", {"tabId": 7, "selector": "#pay"},
                            {"url_changed": True, "timeout": 50})
        self.assert_not_observed(result)
        self.assertEqual(result["outcome"]["expected"], {"url_changed": True, "timeout": 50})
        self.assertEqual(result["outcome"]["observed"], {"source": "tab", "url": TAB["url"]})
        self.assertEqual(result["state"], {"tabId": 7, "url": TAB["url"], "title": "Checkout"})
        # The dispatch result and its post-action snapshot stay intact.
        self.assertEqual(result["snapshot"], "- form\n  - textbox \"Card\" [ref=s3e1]")
        self.assertEqual(extension.actions(), ["getTabs", "submitForm", "waitForUrl", "getTabs"])
        pattern = extension.calls[2][1]["pattern"]
        self.assertIsNone(re.search(pattern, TAB["url"]))
        self.assertIsNotNone(re.search(pattern, TAB["url"] + "/done"))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_url_changed_pattern_is_a_javascript_regex(self):
        url = "https://a.test/p?q=1&r=a-b#x y~"
        extension = FakeExtension(getTabs={"tabs": [{**TAB, "url": url}]}, click={"ok": True},
                                  waitForUrl={"ok": True, "url": "https://a.test/p?q=2"})
        run_expect(extension, "click", {"tabId": 7}, {"url_changed": True})
        pattern = extension.calls[2][1]["pattern"]
        script = ("const re = new RegExp(process.argv[1]);"
                  "console.log(re.test(process.argv[2]), re.test(process.argv[3]))")
        output = subprocess.run(["node", "-e", script, pattern, url, "https://a.test/p?q=2"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(output.split(), ["false", "true"])

    def test_no_op_click_reports_missing_element(self):
        extension = FakeExtension(
            getTabs={"tabs": [TAB]}, click={"ok": True, "tag": "button"},
            waitForElement={"ok": False, "found": False, "error": "Timed out after 50ms"},
        )
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#open"},
                            {"selector": "#menu", "timeout": 50})
        self.assert_not_observed(result)
        self.assertEqual(result["tag"], "button")
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "visible": False})
        self.assertEqual(extension.actions(), ["click", "waitForElement", "getTabs"])
        params = extension.calls[1][1]
        self.assertEqual((params["selector"], params["pollInterval"]), ("#menu", 100))
        self.assertAlmostEqual(params["timeout"], 50, delta=10)

    def test_element_that_never_disappears_is_polled_until_the_deadline(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, click={"ok": True},
                                  waitForElement={"ok": True, "found": True})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#close"},
                            {"selector": ".modal", "gone": True, "timeout": 30})
        self.assert_not_observed(result)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "visible": True})
        self.assertGreater(extension.actions().count("waitForElement"), 1)

    def test_closed_tab_state_after_dispatch(self):
        extension = FakeExtension(getTabs={"tabs": []}, click={"ok": True},
                                  waitForText={"error": "Tab 7 was closed", "code": "tab_closed",
                                               "retryable": True})
        result = run_expect(extension, "click", {"tabId": 7}, {"text": "Saved", "timeout": 1000})
        self.assert_not_observed(result)
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "error": "Tab 7 was closed", "code": "tab_closed"})
        self.assertEqual(result["state"], {"tabId": 7, "closed": True})
        self.assertLess(result["outcome"]["elapsed"], 1000)  # a non-transient error ends the wait

    def test_every_outcome_action_is_a_write_that_is_not_retried(self):
        # retryable follows the #65 rule; the action already ran, so none may be repeated blindly.
        self.assertFalse([action for action in bridge._OUTCOME_ACTIONS if bridge._safe_to_repeat(action)])

    def test_sensitive_value_is_not_verified_or_echoed(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, type={"ok": True},
                                  query={"count": 1, "items": [{"tag": "input", "redacted": True,
                                                                "hasValue": True}]})
        result = run_expect(extension, "type", {"tabId": 7, "selector": "#pw", "text": "hunter2"},
                            {"selector": "#pw", "value": "hunter2", "timeout": 1000})
        self.assert_not_observed(result)
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "found": True, "redacted": True, "hasValue": True})
        self.assertNotIn("hunter2", str(result))
        self.assertEqual(extension.actions().count("query"), 1)

    def test_unchanged_value_after_no_op_click(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, click={"ok": True},
                                  query={"count": 1, "items": [{"tag": "input", "value": "1"}]})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#inc"},
                            {"selector": "#qty", "value_changed": True, "timeout": 30})
        self.assert_not_observed(result)
        self.assertEqual(result["outcome"]["expected"],
                         {"selector": "#qty", "value_changed": True, "timeout": 30})
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "found": True, "value": "1", "before": "1"})
        # The value is read before dispatch, then polled until the deadline.
        self.assertEqual(extension.actions()[:3], ["query", "click", "query"])
        self.assertGreater(extension.actions().count("query"), 2)


class ValueChangedPreconditionTests(unittest.TestCase):
    def test_unreadable_or_sensitive_field_is_rejected_before_dispatch(self):
        for reply, message in [
            ({"error": "Invalid selector", "code": "invalid_selector"},
             "Cannot read the value of #qty before dispatch: Invalid selector"),
            ({"count": 1, "items": [{"tag": "input", "redacted": True, "hasValue": True}]},
             "#qty is a sensitive field"),
        ]:
            with self.subTest(reply=reply):
                extension = FakeExtension(query=reply, click={"ok": True})
                result = run_expect(extension, "click", {"tabId": 7, "selector": "#inc"},
                                    {"selector": "#qty", "value_changed": True})
                self.assertIn(message, result["error"])
                self.assertEqual(extension.actions(), ["query"])

    def test_field_that_turns_sensitive_after_dispatch_is_not_verified(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, click={"ok": True}, query=[
            {"count": 1, "items": [{"tag": "input", "value": "abc"}]},
            {"count": 1, "items": [{"tag": "input", "redacted": True, "hasValue": True}]}])
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#reveal"},
                            {"selector": "#secret", "value_changed": True, "timeout": 1000})
        self.assertEqual((result["ok"], result["code"]), (False, "outcome_not_observed"))
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "found": True, "redacted": True, "hasValue": True})
        self.assertEqual(extension.actions().count("query"), 2)


class OutcomeObservedTests(unittest.TestCase):
    def test_text_in_post_action_snapshot_needs_no_further_read(self):
        extension = FakeExtension(click={"ok": True, "snapshot": '- status "Saved"'})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#save"}, {"text": "Saved"})
        self.assertEqual(extension.actions(), ["click"])
        self.assertIs(result["ok"], True)
        self.assertIs(result["dispatched"], True)
        self.assertEqual(result["outcome"], {
            "expected": {"text": "Saved", "timeout": bridge.OUTCOME_TIMEOUT_MS}, "met": True,
            "observed": {"source": "snapshot", "textVisible": True}, "elapsed": 0,
        })
        self.assertNotIn("code", result)

    def test_delayed_update_is_waited_for(self):
        extension = FakeExtension(click={"ok": True, "snapshot": '- button "Save"'},
                                  waitForText={"ok": True, "text": "Saved", "elapsed": 900})
        result = run_expect(extension, "click", {"tabId": 7}, {"text": "Saved", "timeout": 2000})
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": True})
        action, params = extension.calls[1]
        self.assertEqual((action, params["text"], params["text_gone"]), ("waitForText", "Saved", None))
        self.assertAlmostEqual(params["timeout"], 2000, delta=50)
        self.assertNotIn("error", result)

    def test_delayed_disappearance_is_polled(self):
        extension = FakeExtension(click={"ok": True}, waitForElement=[
            {"ok": True, "found": True}, {"ok": True, "found": True},
            {"ok": False, "found": False, "error": "Timed out after 1ms"}])
        result = run_expect(extension, "click", {"tabId": 7},
                            {"selector": ".spinner", "gone": True, "timeout": 2000})
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "visible": False})
        self.assertEqual(extension.actions().count("waitForElement"), 3)
        self.assertEqual(extension.calls[1][1]["timeout"], 1)

    def test_navigation_mid_wait_keeps_polling(self):
        navigating = {"error": "Page is navigating", "code": "navigating", "retryable": True}
        extension = FakeExtension(click={"ok": True}, waitForElement=[
            navigating, {"ok": True, "found": True, "ref": "s9e0"}])
        result = run_expect(extension, "click", {"tabId": 7}, {"selector": "h1.welcome"})
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(extension.actions(), ["click", "waitForElement", "waitForElement"])

    def test_url_and_value_outcomes(self):
        extension = FakeExtension(navigate={"ok": True, "url": "https://a.test/"},
                                  waitForUrl={"ok": True, "url": "https://a.test/home"})
        result = run_expect(extension, "navigate", {"tabId": 7, "url": "https://a.test/"},
                            {"url": "/home$"})
        self.assertEqual(result["outcome"]["observed"], {"source": "tab", "url": "https://a.test/home"})
        self.assertEqual(extension.calls[1][1]["pattern"], "/home$")

        extension = FakeExtension(fill={"ok": True},
                                  query={"count": 1, "items": [{"tag": "input", "value": "Ada"}]})
        result = run_expect(extension, "fill", {"tabId": 7, "selector": "#name", "value": "Ada"},
                            {"selector": "#name", "value": "Ada"})
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "found": True, "value": "Ada"})
        self.assertEqual(extension.calls[1], ("query", {"tabId": 7, "selector": "#name",
                                                        "fields": ["value"], "limit": 1}))

    def test_delayed_value_change_is_polled(self):
        extension = FakeExtension(click={"ok": True}, query=[
            {"count": 1, "items": [{"tag": "input", "value": "1"}]},
            {"count": 1, "items": [{"tag": "input", "value": "1"}]},
            {"count": 1, "items": [{"tag": "input", "value": "2"}]}])
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#inc"},
                            {"selector": "#qty", "value_changed": True, "timeout": 2000})
        self.assertIs(result["ok"], True)
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "found": True, "value": "2", "before": "1"})
        self.assertEqual(extension.actions(), ["query", "click", "query", "query"])

    def test_field_absent_before_dispatch_has_changed_once_present(self):
        extension = FakeExtension(type={"ok": True}, query=[
            {"count": 0, "items": []}, {"count": 1, "items": [{"tag": "input", "value": ""}]}])
        result = run_expect(extension, "type", {"tabId": 7, "selector": "#a", "text": "x"},
                            {"selector": "#b", "value_changed": True})
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "page", "found": True, "value": "", "before": None})

    def test_missing_tab_id_is_pinned_before_dispatch(self):
        extension = FakeExtension(getZoom={"ok": True, "tabId": 12, "factor": 1},
                                  click={"ok": True}, waitForElement={"ok": True, "found": True})
        run_expect(extension, "click", {"selector": "#go"}, {"selector": "#done"})
        self.assertEqual(extension.calls[1], ("click", {"selector": "#go", "tabId": 12}))
        self.assertEqual(extension.calls[2][1]["tabId"], 12)


class DispatchFailureTests(unittest.TestCase):
    def test_failed_dispatch_is_returned_unchanged_without_outcome(self):
        for reply in ({"error": "Element not found", "code": "not_found", "retryable": True},
                      {"ok": False, "timedOut": True, "url": "https://a.test/"}):
            with self.subTest(reply=reply):
                extension = FakeExtension(click=reply)
                result = run_expect(extension, "click", {"tabId": 7}, {"selector": "#x"})
                self.assertEqual(result, reply)
                self.assertEqual(extension.actions(), ["click"])

    def test_invalid_expect_never_dispatches(self):
        for action, expect, message in [
            ("click", {"ref": "s1e2"}, "expect.ref is not supported"),
            ("click", {"text": "a", "url": "b"}, "exactly one of"),
            ("click", {}, "exactly one of"),
            ("click", {"text": ""}, "non-empty string"),
            ("click", {"url_changed": "yes"}, "must be true"),
            ("click", {"text": "a", "gone": True}, "require expect.selector"),
            ("click", {"selector": "#a", "gone": True, "value": "x"}, "mutually exclusive"),
            ("click", {"selector": "#a", "value": "x", "value_changed": True}, "mutually exclusive"),
            ("click", {"selector": "#a", "value_changed": 1}, "value_changed must be a boolean"),
            ("click", {"text": "a", "value_changed": True}, "require expect.selector"),
            ("click", {"selector": "#a", "timeout": 0}, "positive milliseconds"),
            ("click", {"selector": "#a", "within": 5}, "Unknown expect keys: within"),
            ("click", "#a", "must be an object"),
            ("getSnapshot", {"selector": "#a"}, "not supported for action: getSnapshot"),
            ("newTab", {"url": "x"}, "not supported for action: newTab"),
        ]:
            with self.subTest(expect=expect):
                spec, problem = bridge._parse_expect(action, expect)
                self.assertIsNone(spec)
                self.assertIn(message, problem)

        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send:
            result = asyncio.run(bridge.run_command(
                {"action": "click", "selector": "#a", "expect": {"ref": "s1e2"}}))
        self.assertIn("expect.ref is not supported", result["error"])
        send.assert_not_called()


class OutcomeRoutingTests(unittest.TestCase):
    def test_batch_without_expect_is_unchanged(self):
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True}) as send:
            result = asyncio.run(bridge.run_command({"action": "click", "selector": "#a"}))
        self.assertEqual(result, {"ok": True})
        send.assert_awaited_once_with("click", {"selector": "#a"})

    def test_retry_does_not_repeat_an_unobserved_outcome(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, submitForm={"ok": True},
                                  waitForUrl={"ok": False, "url": TAB["url"], "error": "Timed out"})
        command = {"action": "retry", "maxAttempts": 3, "backoffMs": 0, "command": {
            "action": "submitForm", "tabId": 7, "expect": {"url_changed": True, "timeout": 10}}}
        with patch.object(bridge, "send_to_extension", extension), \
             patch.object(bridge, "OUTCOME_POLL_SECONDS", 0):
            result = asyncio.run(bridge.run_command(command))
        self.assertEqual((result["ok"], result["attempts"], result["reason"]), (False, 1, "not retryable"))
        self.assertEqual(result["result"]["code"], "outcome_not_observed")
        self.assertEqual(extension.actions().count("submitForm"), 1)

    def test_http_action_strips_expect_and_extends_its_timeout(self):
        body = {"selector": "#a", "tabId": 7, "expect": {"selector": "#b", "timeout": 3000}}
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        extension = FakeExtension(click={"ok": True}, waitForElement={"ok": True, "found": True})
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_audit") as audit, \
             patch.object(bridge, "send_to_extension", extension):
            handler.handle_click()
        self.assertEqual(extension.calls[0], ("click", {"selector": "#a", "tabId": 7}))
        self.assertEqual(handler.run_async.call_args.kwargs["timeout"], 35 + 3 + 45)
        self.assertIs(handler.send_json.call_args.args[1]["outcome"]["met"], True)
        self.assertEqual(audit.call_args.args[:3], ("click", body, True))

        handler.send_json.reset_mock()
        handler.read_body.return_value = {"selector": "#a", "expect": {"ref": "s1e0"}}
        with patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send:
            handler.handle_click()
        send.assert_not_called()
        self.assertIn("expect.ref", handler.send_json.call_args.args[1]["error"])


class ServerExpectTests(unittest.TestCase):
    def test_default_timeout_matches_bridge(self):
        self.assertEqual(server.OUTCOME_TIMEOUT_MS, bridge.OUTCOME_TIMEOUT_MS)

    def test_tools_forward_expect_with_a_longer_timeout(self):
        expect = {"url_changed": True, "timeout": 8000}
        cases = [
            ("click", {"selector": "#a"}, "/api/click"),
            ("type", {"selector": "#a", "text": "x"}, "/api/type"),
            ("select_option", {"selector": "#a", "value": "v"}, "/api/select-option"),
            ("check", {"selector": "#a"}, "/api/check"),
            ("keypress", {"key": "Enter"}, "/api/keypress"),
            ("submit_form", {}, "/api/submit-form"),
            ("navigate", {"url": "https://a.test/"}, "/api/navigate"),
            ("double_click", {"selector": "#a"}, "/api/double-click"),
            ("hover", {"selector": "#a"}, "/api/hover"),
            ("scroll", {}, "/api/scroll"),
            ("drag", {"from_selector": "#a", "to_selector": "#b"}, "/api/drag"),
            ("form_fill", {"fields": {"#a": "x"}}, "/api/form-fill"),
            ("back", {}, "/api/back"),
            ("forward", {}, "/api/forward"),
            ("reload_tab", {}, "/api/reload-tab"),
        ]
        for name, kwargs, endpoint in cases:
            with self.subTest(tool=name), patch.object(server, "_post", return_value={"ok": True}) as post:
                getattr(server, "browser_" + name)(tab_id=7, expect=expect, **kwargs)
                self.assertEqual(post.call_args.args[0], endpoint)
                self.assertEqual(post.call_args.args[1]["expect"], expect)
                self.assertEqual(post.call_args.kwargs, {"timeout": 15 + 8 + 45})
                getattr(server, "browser_" + name)(tab_id=7, **kwargs)
                self.assertNotIn("expect", post.call_args.args[1])
                self.assertEqual(post.call_args.kwargs, {})

    def test_every_endpoint_of_an_expect_tool_accepts_expect(self):
        # MCP tools only forward expect to bridge actions that observe outcomes.
        endpoints = {"/api/double-click": "doubleClick", "/api/hover": "hover", "/api/scroll": "scroll",
                     "/api/drag": "drag", "/api/form-fill": "formFill", "/api/back": "goBack",
                     "/api/forward": "goForward", "/api/reload-tab": "reloadTabBrowser"}
        for endpoint, action in endpoints.items():
            with self.subTest(endpoint=endpoint):
                self.assertIn(action, bridge._OUTCOME_ACTIONS)
                self.assertIsNone(bridge._parse_expect(action, {"url_changed": True})[1])

    def test_default_and_invalid_expect_timeouts(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_click("#a", expect={"text": "Saved"})
            self.assertEqual(post.call_args.kwargs, {"timeout": 15 + 5 + 45})
            server.browser_click("#a", expect={"text": "Saved", "timeout": "soon"})
            self.assertEqual(post.call_args.kwargs, {"timeout": 15 + 45})


if __name__ == "__main__":
    unittest.main()
