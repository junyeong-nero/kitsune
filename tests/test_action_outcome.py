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
    """Scripted send_to_extension: replies per action (the last reply repeats), records calls.

    An unscripted action gets the extension's unknown-action reply.
    """

    def __init__(self, **replies):
        self.replies = {action: list(reply) if isinstance(reply, list) else [reply]
                        for action, reply in replies.items()}
        self.calls = []

    async def __call__(self, action, params=None, timeout=30):
        self.calls.append((action, dict(params or {})))
        queue = self.replies.get(action, [{"error": f"Unknown action: {action}"}])
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
        self.assertEqual(extension.actions(), ["getTabs", "submitForm", "waitForUrl", "getTabs", "getOpenedTabs"])
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
        self.assertGreater(extension.actions().count("waitForElement"), 1)
        # The last read checks whether the clicked element is still visible (#205).
        self.assertEqual(extension.actions()[-3:], ["getTabs", "getOpenedTabs", "waitForElement"])
        self.assertEqual(extension.calls[1][1], {"tabId": 7, "selector": "#menu", "timeout": 0})

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


class ThrottledPageExtension(FakeExtension):
    """A background tab whose timers fire late: an in-page wait overshoots its timeout (#202)."""

    LATE_SECONDS = 0.5

    async def __call__(self, action, params=None, timeout=30):
        wait_ms = (params or {}).get("timeout", 0)
        if action in ("waitForText", "waitForElement") and wait_ms > 1:
            await asyncio.sleep(wait_ms / 1000 + self.LATE_SECONDS)
        return await super().__call__(action, params, timeout)


class OutcomeDeadlineTests(unittest.TestCase):
    TIMEOUT_MS = 300

    def run_throttled(self, expect, **replies):
        extension = ThrottledPageExtension(getTabs={"tabs": [TAB]}, click={"ok": True}, **replies)
        spec, problem = bridge._parse_expect("click", {**expect, "timeout": self.TIMEOUT_MS})
        assert problem is None, problem
        with patch.object(bridge, "send_to_extension", extension), \
             patch.object(bridge, "OUTCOME_POLL_SECONDS", 0.02):
            return asyncio.run(bridge._send_with_expectation("click", {"tabId": 7}, spec)), extension

    def assert_ends_at_timeout(self, result):
        self.assertIs(result["outcome"]["met"], False)
        self.assertIs(result["dispatched"], True)
        self.assertGreaterEqual(result["outcome"]["elapsed"], self.TIMEOUT_MS)
        self.assertLess(result["outcome"]["elapsed"], self.TIMEOUT_MS + 200)

    def test_page_checks_are_single_reads_so_late_page_timers_cannot_extend_them(self):
        unmet_text = {"ok": False, "error": "Timed out after 0ms waiting for visible text"}
        result, extension = self.run_throttled({"text_gone": "Still shown"}, waitForText=unmet_text)
        self.assert_ends_at_timeout(result)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": True})
        self.assertGreater(extension.actions().count("waitForText"), 1)

        result, extension = self.run_throttled({"text": "Saved"}, waitForText=unmet_text)
        self.assert_ends_at_timeout(result)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": False})

        result, extension = self.run_throttled(
            {"selector": "#menu"}, waitForElement={"ok": False, "found": False, "error": "Timed out"})
        self.assert_ends_at_timeout(result)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "visible": False})


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

    def test_text_line_in_post_action_text_snapshot_needs_no_further_read(self):
        extension = FakeExtension(click={"ok": True, "snapshot": '- text "Saved 3 items."'})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#save"}, {"text": "Saved 3"})
        self.assertEqual(extension.actions(), ["click"])
        self.assertEqual(result["outcome"]["observed"], {"source": "snapshot", "textVisible": True})

    def test_delayed_update_is_waited_for(self):
        extension = FakeExtension(click={"ok": True, "snapshot": '- button "Save"'},
                                  waitForText={"ok": True, "text": "Saved", "elapsed": 900})
        result = run_expect(extension, "click", {"tabId": 7}, {"text": "Saved", "timeout": 2000})
        self.assertIs(result["outcome"]["met"], True)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": True})
        action, params = extension.calls[1]
        self.assertEqual((action, params["text"], params["text_gone"]), ("waitForText", "Saved", None))
        self.assertEqual(params["timeout"], 0)
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
        self.assertEqual(extension.calls[1][1]["timeout"], 0)

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


class OpenedTabTests(unittest.TestCase):
    """Issue #87: a target=_blank click opens a tab instead of changing this one."""

    OPENED = [{"tabId": 4, "url": "http://127.0.0.1:8000/ok2"}]

    def test_unobserved_outcome_names_the_tab_the_click_opened(self):
        extension = FakeExtension(
            getTabs={"tabs": [TAB]},
            click={"ok": True, "opened_tabs": [{"tabId": 4, "url": "about:blank"}]},
            waitForUrl={"ok": False, "url": TAB["url"], "error": "Timed out"},
            getOpenedTabs={"opened_tabs": self.OPENED},
        )
        result = run_expect(extension, "click", {"tabId": 7, "ref": "s1e2"},
                            {"url_changed": True, "timeout": 30})
        self.assertEqual((result["ok"], result["code"]), (False, "outcome_not_observed"))
        # Read after the wait, so the opened tab's URL is current.
        self.assertEqual(result["opened_tabs"], self.OPENED)
        self.assertIn("opened tab 4", result["hint"])
        self.assertIn("browser_snapshot tab_id=4", result["hint"])
        self.assertEqual(extension.calls[-1], ("getOpenedTabs", {"tabId": 7}))

    def test_unobserved_outcome_without_an_opened_tab_keeps_the_generic_hint(self):
        for reply in [{"opened_tabs": []}, {"error": "Unknown action: getOpenedTabs"}]:
            with self.subTest(reply=reply):
                extension = FakeExtension(
                    getTabs={"tabs": [TAB]}, click={"ok": True},
                    waitForUrl={"ok": False, "url": TAB["url"], "error": "Timed out"},
                    getOpenedTabs=reply,
                )
                result = run_expect(extension, "click", {"tabId": 7, "selector": "#noop"},
                                    {"url_changed": True, "timeout": 30})
                self.assertEqual(result["code"], "outcome_not_observed")
                self.assertNotIn("opened_tabs", result)
                self.assertTrue(result["hint"].startswith("The command was dispatched but the expected"))

    def test_met_outcome_keeps_the_dispatch_opened_tabs(self):
        extension = FakeExtension(click={"ok": True, "opened_tabs": self.OPENED},
                                  waitForElement={"ok": True, "found": True})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#a"}, {"selector": "#b"})
        self.assertIs(result["ok"], True)
        self.assertEqual(result["opened_tabs"], self.OPENED)
        self.assertEqual(extension.actions(), ["click", "waitForElement"])

    def test_new_tab_outcome_is_polled_until_a_tab_opens(self):
        extension = FakeExtension(click={"ok": True},
                                  getOpenedTabs=[{"opened_tabs": []}, {"opened_tabs": self.OPENED}])
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#new"},
                            {"new_tab": True, "timeout": 2000})
        self.assertIs(result["ok"], True)
        self.assertEqual(result["outcome"]["expected"], {"new_tab": True, "timeout": 2000})
        self.assertEqual(result["outcome"]["observed"], {"source": "tabs", "opened_tabs": self.OPENED})
        self.assertEqual(extension.actions(), ["click", "getOpenedTabs", "getOpenedTabs"])

    def test_new_tab_outcome_not_observed(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, click={"ok": True},
                                  getOpenedTabs={"opened_tabs": []})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#noop"},
                            {"new_tab": True, "timeout": 30})
        self.assertEqual((result["ok"], result["code"]), (False, "outcome_not_observed"))
        self.assertEqual(result["outcome"]["observed"], {"source": "tabs", "opened_tabs": []})
        self.assertNotIn("opened_tabs", result)
        self.assertGreater(extension.actions().count("getOpenedTabs"), 1)

    def test_new_tab_outcome_on_an_extension_without_opened_tab_tracking(self):
        extension = FakeExtension(getTabs={"tabs": [TAB]}, click={"ok": True})
        result = run_expect(extension, "click", {"tabId": 7, "selector": "#new"},
                            {"new_tab": True, "timeout": 1000})
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertEqual(result["outcome"]["observed"],
                         {"source": "tabs", "error": "Unknown action: getOpenedTabs"})
        self.assertEqual(extension.actions(), ["click", "getOpenedTabs", "getTabs", "waitForElement"])

    def test_unobserved_outcome_after_a_blocked_popup_uses_its_hint(self):
        # #94: the extension could not open the link's tab and said why.
        warning = {"code": "popup_blocked", "state": {"url": "http://127.0.0.1:8000/ok2"},
                   "hint": "The link opens in a new tab, but no tab opened; the popup blocker ..."}
        for expect in [{"new_tab": True, "timeout": 30}, {"url_changed": True, "timeout": 30}]:
            with self.subTest(expect=expect):
                extension = FakeExtension(
                    getTabs={"tabs": [TAB]}, click={"ok": True, "warning": warning},
                    waitForUrl={"ok": False, "url": TAB["url"], "error": "Timed out"},
                    getOpenedTabs={"opened_tabs": []},
                )
                result = run_expect(extension, "click", {"tabId": 7, "selector": "#opener"}, expect)
                self.assertEqual(result["code"], "outcome_not_observed")
                self.assertEqual(result["warning"], warning)
                self.assertEqual(result["hint"], warning["hint"])

    def test_invalid_new_tab_expect(self):
        for expect, message in [
            ({"new_tab": "yes"}, "expect.new_tab must be true"),
            ({"new_tab": True, "url_changed": True}, "expect needs exactly one of"),
            ({"new_tab": True, "gone": True}, "require expect.selector"),
        ]:
            with self.subTest(expect=expect):
                self.assertIn(message, bridge._parse_expect("click", expect)[1])


class TrustedClickHintTests(unittest.TestCase):
    """Issue #205: a click whose target is still visible after an unmet expect may need trusted input."""

    UNMET = {"text_gone": "Notice", "timeout": 30}

    def run_click(self, click, target=None, action="click", params=None, opened=None):
        extension = FakeExtension(
            getTabs={"tabs": [TAB]}, **{action: click},
            waitForText={"ok": False, "error": "Timed out"},
            getOpenedTabs={"opened_tabs": opened or []},
            **({"waitForElement": target} if target is not None else {}),
        )
        result = run_expect(extension, action, params or {"tabId": 7, "ref": "e3"}, self.UNMET)
        return result, extension

    def assert_no_trusted_hint(self, result):
        self.assertNotIn("browser_trusted_click", result.get("hint", ""))

    def test_hint_when_the_clicked_element_is_still_visible(self):
        result, extension = self.run_click({"ok": True, "snapshot": "- button \"OK\" [ref=e9]", "target_ref": "e9"},
                                           target={"ok": True, "found": True})
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertTrue(result["hint"].startswith("The command was dispatched but the expected"))
        self.assertIn("may", result["hint"])
        self.assertIn("browser_trusted_click", result["hint"])
        self.assertIn("devtools", result["hint"])
        self.assertIn(("waitForElement", {"tabId": 7, "selector": "e9", "timeout": 1, "pollInterval": 100}),
                      extension.calls)
        # No retry and no OS click: the click ran once.
        self.assertEqual(extension.actions().count("click"), 1)
        self.assertNotIn("trustedClick", extension.actions())
        self.assertNotIn("screenPoint", extension.actions())

    def test_without_a_post_action_snapshot_the_request_target_is_checked(self):
        for params, target in [({"tabId": 7, "ref": "e3", "selector": ""}, "e3"),
                               ({"tabId": 7, "selector": "#ok"}, "#ok")]:
            with self.subTest(params=params):
                result, extension = self.run_click({"ok": True}, target={"ok": True, "found": True},
                                                   params=params)
                self.assertIn("browser_trusted_click", result["hint"])
                self.assertEqual(extension.calls[-1][1]["selector"], target)

    def test_no_hint_when_the_target_is_gone(self):
        for target in [{"ok": False, "found": False, "error": "Timed out after 1ms"},
                       {"error": "stale ref, call browser_snapshot again", "code": "stale_ref"}]:
            with self.subTest(target=target):
                result, _ = self.run_click({"ok": True, "snapshot": "- button [ref=e9]", "target_ref": "e9"},
                                           target=target)
                self.assertEqual(result["code"], "outcome_not_observed")
                self.assert_no_trusted_hint(result)

    def test_no_hint_when_the_snapshot_no_longer_has_the_target(self):
        result, extension = self.run_click({"ok": True, "snapshot": "- heading \"Live\""},
                                           target={"ok": True, "found": True})
        self.assert_no_trusted_hint(result)
        self.assertNotIn("waitForElement", extension.actions())

    def test_no_hint_for_a_coordinate_click_without_a_target_ref(self):
        result, extension = self.run_click({"ok": True}, target={"ok": True, "found": True},
                                           params={"tabId": 7, "coords": {"x": 5, "y": 5}})
        self.assert_no_trusted_hint(result)
        self.assertNotIn("waitForElement", extension.actions())

    def test_no_hint_when_nothing_was_dispatched(self):
        result, extension = self.run_click({"error": "Element not found", "code": "not_found"},
                                           target={"ok": True, "found": True})
        self.assertNotIn("dispatched", result)
        self.assert_no_trusted_hint(result)
        self.assertEqual(extension.actions(), ["click"])

    def test_no_hint_when_the_outcome_is_observed(self):
        extension = FakeExtension(click={"ok": True, "target_ref": "e9"}, waitForText={"ok": True},
                                  waitForElement={"ok": True, "found": True})
        result = run_expect(extension, "click", {"tabId": 7, "ref": "e3"}, self.UNMET)
        self.assertIs(result["ok"], True)
        self.assert_no_trusted_hint(result)
        self.assertEqual(extension.actions(), ["click", "waitForText"])

    def test_no_hint_without_expect(self):
        extension = FakeExtension(click={"ok": True, "target_ref": "e9"},
                                  waitForElement={"ok": True, "found": True})
        with patch.object(bridge, "send_to_extension", extension):
            result = asyncio.run(bridge._execute_command("click", "click", {"tabId": 7, "ref": "e3"}))
        self.assertEqual(result, {"ok": True, "target_ref": "e9"})
        self.assertEqual(extension.actions(), ["click"])

    def test_no_hint_for_other_actions_or_an_opened_tab(self):
        result, _ = self.run_click({"ok": True, "target_ref": "e9"}, target={"ok": True, "found": True},
                                   action="type", params={"tabId": 7, "ref": "e3", "text": "x"})
        self.assert_no_trusted_hint(result)
        result, _ = self.run_click({"ok": True, "target_ref": "e9"}, target={"ok": True, "found": True},
                                   opened=[{"tabId": 4, "url": "about:blank"}])
        self.assertIn("opened tab 4", result["hint"])
        self.assert_no_trusted_hint(result)


if __name__ == "__main__":
    unittest.main()
