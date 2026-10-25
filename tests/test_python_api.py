"""The Python API (kitsune.connect, Browser, Tab) against a mocked bridge."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings

import kitsune
from kitsune import bridge_client, capabilities, playwright_tools, server, task_modes
from kitsune.bridge_client import BridgeClient
from kitsune.browser import Browser, Tab

PNG = b"\x89PNG\r\n\x1a\nimage"
REPLIES = {
    "/api/status": {"status": "running", "extension_connected": True, "diagnosis": {"code": "connected"}},
    "/api/tabs": {"tabs": [{"id": 3, "active": True, "windowId": 1}, {"id": 7, "active": False, "windowId": 1}]},
    "/api/new-tab": {"ok": True, "tabId": 9, "url": "https://example.com/", "status": "complete"},
    "/api/page-text": {"text": "Hello"},
    "/api/snapshot": {"snapshot": '- button "Go" [ref=e3]'},
    "/api/forms": {"fields": [{"ref": "e1", "name": "q"}]},
    "/api/find": {"results": [{"ref": "e3"}]},
    "/api/query": {"count": 1, "total": 1, "truncated": False, "items": [{"text": "a"}]},
    "/api/html": {"html": "<p>x</p>", "length": 8},
    "/api/markdown": {"markdown": "# x", "length": 3},
    "/api/links": {"count": 1, "links": [{"href": "https://example.com/"}]},
    "/api/js": {"result": '{"title": "Example"}'},
    "/api/screenshot": {"dataUrl": "data:image/png;base64," + base64.b64encode(PNG).decode()},
    "/api/element-screenshot": {"dataUrl": "data:image/png;base64," + base64.b64encode(PNG).decode()},
    "/api/workflows": {"workflows": ["test"]},
    "/api/cookies": {"ok": True, "count": 1, "cookies": [{"name": "sid"}]},
    "/api/history-search": {"results": [{"url": "https://example.com/"}], "count": 1},
    "/api/history-visits": {"ok": True, "visits": [{"visit_time": 1}], "count": 1},
    "/api/bookmarks-search": {"results": [{"url": "https://example.com/"}], "count": 1},
}


class FakeBridge:
    """Answers bridge requests by route (a list answers in turn, a function when called) and records them."""

    def __init__(self, **replies):
        self.replies = {**REPLIES, **replies}
        self.requests = []

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        route = url.removeprefix(bridge_client.BRIDGE)
        self.requests.append(SimpleNamespace(method=method, route=route, headers=headers, body=json, timeout=timeout))
        reply = self.replies.get(route, {"ok": True})
        if isinstance(reply, list):
            reply = reply.pop(0)
        if callable(reply):
            reply = reply()
        return Mock(status_code=200, json=Mock(return_value=reply))

    @property
    def last(self):
        return self.requests[-1]


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        for patcher in (patch.object(bridge_client.httpx, "request", side_effect=self.bridge),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": "test-token"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = Browser(BridgeClient(caps=None))
        self.tab = self.browser.tab(7)

    def reply(self, route, result):
        self.bridge.replies[route] = result


class TabRequestTests(BridgeTestCase):
    def test_methods_send_their_route_and_body_with_the_tab_id(self):
        cases = [
            (lambda t: t.click("e3"), "/api/click", {"selector": "", "ref": "e3"}),
            (lambda t: t.click("text=Save", button="middle", modifiers=["Shift"], first=True), "/api/click",
             {"selector": "text=Save", "button": "middle", "modifiers": ["Shift"], "first": True}),
            (lambda t: t.double_click("#row"), "/api/double-click", {"selector": "#row"}),
            (lambda t: t.hover("e5"), "/api/hover", {"selector": "", "ref": "e5"}),
            (lambda t: t.type("#q", "hi", clear=True), "/api/type", {"selector": "#q", "text": "hi", "clear": True}),
            (lambda t: t.fill("e1", "a@b.c"), "/api/fill", {"selector": "", "ref": "e1", "value": "a@b.c"}),
            (lambda t: t.fill_form({"email": "a@b.c"}), "/api/form-fill", {"fields": {"email": "a@b.c"}}),
            (lambda t: t.press("Enter"), "/api/keypress", {"key": "Enter"}),
            (lambda t: t.press("a", "e1", modifiers={"ctrl": True}), "/api/keypress",
             {"key": "a", "ref": "e1", "modifiers": {"ctrl": True}}),
            (lambda t: t.select_option("#size", "L", by_text=True), "/api/select-option",
             {"selector": "#size", "value": "L", "byText": True}),
            (lambda t: t.check("e4"), "/api/check", {"selector": "", "ref": "e4", "checked": True}),
            (lambda t: t.uncheck("#agree"), "/api/check", {"selector": "#agree", "checked": False}),
            (lambda t: t.scroll(), "/api/scroll", {"direction": "down", "amount": 500}),
            (lambda t: t.scroll("bottom", target="e9"), "/api/scroll", {"direction": "bottom", "amount": 500, "ref": "e9"}),
            (lambda t: t.submit_form(), "/api/submit-form", {}),
            (lambda t: t.submit_form("#signup", confirm="tok"), "/api/submit-form",
             {"selector": "#signup", "confirmToken": "tok"}),
            (lambda t: t.navigate("https://example.com/"), "/api/navigate", {"url": "https://example.com/"}),
            (lambda t: t.back(), "/api/back", {}),
            (lambda t: t.forward(), "/api/forward", {}),
            (lambda t: t.reload(bypass_cache=True), "/api/reload-tab", {"bypassCache": True}),
            (lambda t: t.handle_dialogs(False), "/api/handle-dialog", {"accept": False, "prompt_text": None}),
            (lambda t: t.show(), "/api/switch-tab", {}),
            (lambda t: t.close(force=True), "/api/close-tab", {"force": True}),
            (lambda t: t.info(), "/api/page-info", {}),
            (lambda t: t.text(), "/api/page-text", {}),
            (lambda t: t.snapshot(), "/api/snapshot", {"text": False}),
            (lambda t: t.snapshot("main", max_depth=2, text=True), "/api/snapshot",
             {"selector": "main", "maxDepth": 2, "text": True}),
            (lambda t: t.forms(), "/api/forms", {}),
            (lambda t: t.find("login button"), "/api/find", {"query": "login button"}),
            (lambda t: t.query("a", fields=["href"], limit=5), "/api/query", {"selector": "a", "fields": ["href"], "limit": 5}),
            (lambda t: t.html("e2"), "/api/html", {"selector": "", "ref": "e2"}),
            (lambda t: t.markdown(), "/api/markdown", {}),
            (lambda t: t.links(internal_only=True), "/api/links", {"internalOnly": True}),
            (lambda t: t.wait_for(text="Done", timeout=3000), "/api/wait-for-text",
             {"timeout": 3000, "text": "Done", "text_gone": None}),
            (lambda t: t.wait_for(target="e2"), "/api/wait-for-element", {"timeout": 10000, "ref": "e2", "pollInterval": 200}),
            (lambda t: t.wait_for(url="/done$"), "/api/wait-for-url", {"timeout": 10000, "pattern": "/done$"}),
            (lambda t: t.wait_for(title="Done"), "/api/wait-for-title", {"timeout": 10000, "pattern": "Done"}),
            (lambda t: t.wait_for(js="window.ready"), "/api/wait-for-result",
             {"timeout": 10000, "code": "window.ready", "pollInterval": 500}),
            (lambda t: t.wait_for(network_idle=True), "/api/wait-for-network-idle", {"timeout": 10000, "idleMs": 500}),
        ]
        for call, route, body in cases:
            with self.subTest(route=route, body=body):
                call(self.tab)
                self.assertEqual((self.bridge.last.method, self.bridge.last.route, self.bridge.last.body),
                                 ("POST", route, {**body, "tabId": 7}))

    def test_waits_and_outcome_checks_lengthen_the_request_timeout(self):
        self.tab.click("e3")
        self.assertEqual(self.bridge.last.timeout, 15)
        self.tab.click("e3", wait=True, timeout=3000, expect={"url_changed": True, "timeout": 2000})
        self.assertEqual(self.bridge.last.body["expect"], {"url_changed": True, "timeout": 2000})
        self.assertEqual(self.bridge.last.timeout, 15 + 3 + 2 + 45)
        self.tab.wait_for(text="Done", timeout=4000)
        self.assertEqual(self.bridge.last.timeout, 14)
        with self.assertRaises(ValueError):
            self.tab.click("e3", wait=True, timeout=0)

    def test_reads_return_what_they_read(self):
        self.assertEqual(self.tab.text(), "Hello")
        self.assertEqual(self.tab.snapshot(), '- button "Go" [ref=e3]')
        self.assertEqual(self.tab.forms(), [{"ref": "e1", "name": "q"}])
        self.assertEqual(self.tab.find("go"), [{"ref": "e3"}])
        self.assertEqual(self.tab.query("a"), [{"text": "a"}])
        self.assertEqual(self.tab.html("p"), "<p>x</p>")
        self.assertEqual(self.tab.markdown("main"), "# x")
        self.assertEqual(self.tab.links(), [{"href": "https://example.com/"}])
        self.assertEqual(self.tab.click("e3"), {"ok": True}, "actions return the bridge's result")

    def test_reads_the_bridge_cut_warn(self):
        self.reply("/api/page-text", {"text": "x" * 20000, "truncated": True, "fullLength": 30000})
        with self.assertWarnsRegex(UserWarning, "Page text was cut"):
            self.assertEqual(len(self.tab.text()), 20000)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.tab.snapshot()

    def test_snapshot_rejects_a_bad_depth_before_sending(self):
        for depth in (-1, 1.5, True):
            with self.subTest(depth=depth), self.assertRaises(ValueError):
                self.tab.snapshot(max_depth=depth)
        self.assertEqual(self.bridge.requests, [])

    def test_evaluate_calls_a_function_and_returns_its_json_value(self):
        self.assertEqual(self.tab.evaluate("() => ({title: document.title})"), {"title": "Example"})
        self.assertEqual(self.bridge.last.body, {
            "code": "Promise.resolve((() => ({title: document.title}))()).then(value => JSON.stringify(value) ?? 'undefined')",
            "tabId": 7})
        self.tab.evaluate("el => el.textContent", "e2")
        self.assertEqual(self.bridge.last.body["selector"], "e2")
        self.assertIn("(el => el.textContent)(__kitsuneElement)", self.bridge.last.body["code"])
        self.reply("/api/js", {"result": "undefined"})
        self.assertIsNone(self.tab.evaluate("() => undefined"))

    def test_screenshot_returns_the_image_and_writes_it_to_path(self):
        self.assertEqual(self.tab.screenshot(), PNG)
        self.assertEqual((self.bridge.last.route, self.bridge.last.body), ("/api/screenshot", {"imagePreview": False, "tabId": 7}))
        with tempfile.TemporaryDirectory() as home:
            path = Path(home, "shots", "button.png")
            self.assertEqual(self.tab.screenshot(path, target="e3"), PNG)
            self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual((self.bridge.last.route, self.bridge.last.body, self.bridge.last.timeout),
                         ("/api/element-screenshot", {"imagePreview": False, "selector": "", "ref": "e3", "tabId": 7}, 45))
        with self.assertRaises(ValueError):
            self.tab.screenshot(target="e3", full_page=True)

    def test_tabs_compare_by_id(self):
        self.assertEqual(self.browser.tab(7), self.tab)
        self.assertNotEqual(self.browser.tab(8), self.tab)
        self.assertEqual(len({self.tab, self.browser.tab(7)}), 1)
        self.assertEqual(repr(self.tab), "<Tab 7>")


class BrowserRequestTests(BridgeTestCase):
    def test_tabs_and_new_tabs(self):
        self.assertEqual([tab["id"] for tab in self.browser.tabs()], [3, 7])
        self.assertEqual(self.bridge.last.method, "GET")
        tab = self.browser.new_tab("https://example.com/")
        self.assertEqual(tab, self.browser.tab(9))
        self.assertEqual((self.bridge.last.body, self.bridge.last.timeout),
                         ({"url": "https://example.com/", "wait": True, "timeout": 15000}, 25))
        self.browser.new_tab(wait=False)
        self.assertEqual(self.bridge.last.body, {"url": "about:blank"})

    def test_active_tab_is_the_only_active_one(self):
        self.assertEqual(self.browser.active_tab(), self.browser.tab(3))
        self.reply("/api/tabs", {"tabs": [{"id": 3, "active": True, "windowId": 1}, {"id": 5, "active": True, "windowId": 2}]})
        with self.assertRaises(kitsune.KitsuneError) as raised:
            self.browser.active_tab()
        self.assertEqual((raised.exception.code, raised.exception.state), ("ambiguous_target", {"tabIds": [3, 5]}))
        self.reply("/api/tabs", {"tabs": []})
        with self.assertRaises(kitsune.ConnectionFailed):
            self.browser.active_tab()

    def test_containers_name_their_default_tab_and_outlast_the_bridge_deadline(self):
        commands = [{"action": "click", "selector": "#a"}]
        self.browser.batch(commands, tab=self.tab)
        self.assertEqual(self.bridge.last.body, {"commands": commands, "tabId": 7})
        self.assertGreater(self.bridge.last.timeout, 300)
        self.browser.parallel([[{"action": "pageText", "tabId": 7}]])
        self.assertEqual(self.bridge.last.body, {"commands": [{"action": "parallel", "sequences": [[{"action": "pageText", "tabId": 7}]]}]})
        self.browser.workflow("test", {"url": "https://example.com/"}, tab=7)
        self.assertEqual(self.bridge.last.body, {"name": "test", "variables": {"url": "https://example.com/"}, "tabId": 7})
        self.browser.resume(42, "token", {"1": "applied"})
        self.assertEqual(self.bridge.last.body, {"call": 42, "resume_token": "token", "observed": {"1": "applied"}})
        self.assertEqual(self.browser.workflows(), ["test"])

    def test_cookies_history_and_bookmarks(self):
        self.assertEqual(self.browser.cookies.get(domain="example.com"), [{"name": "sid"}])
        self.assertEqual(self.bridge.last.body, {"op": "get", "domain": "example.com"})
        self.browser.cookies.set("https://example.com", "sid", "1", secure=True)
        self.assertEqual(self.bridge.last.body, {"op": "set", "url": "https://example.com", "name": "sid", "value": "1",
                                                 "secure": True})
        self.browser.cookies.remove("https://example.com", "sid", confirm="tok")
        self.assertEqual(self.bridge.last.body, {"op": "remove", "url": "https://example.com", "name": "sid",
                                                 "confirmToken": "tok"})
        self.browser.cookies.clear(domain="example.com")
        self.assertEqual(self.bridge.last.body, {"op": "clear", "domain": "example.com"})
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(self.browser.history.search("flights", since=since, limit=5), [{"url": "https://example.com/"}])
        self.assertEqual(self.bridge.last.body, {"query": "flights", "limit": 5, "since": int(since.timestamp() * 1000)})
        self.assertEqual(self.browser.history.visits("https://example.com/"), [{"visit_time": 1}])
        self.assertEqual(self.browser.bookmarks.search("docs"), [{"url": "https://example.com/"}])
        self.assertEqual(self.bridge.last.body, {"query": "docs", "limit": 20})

    def test_request_reaches_any_route(self):
        self.assertEqual(self.browser.request("/api/meta", {"tabId": 7}), {"ok": True})
        self.assertEqual((self.bridge.last.method, self.bridge.last.route), ("POST", "/api/meta"))
        self.browser.request("/api/version")
        self.assertEqual(self.bridge.last.method, "GET")


class ErrorTests(BridgeTestCase):
    def test_failed_results_raise_their_kitsune_error(self):
        cases = [
            ({"error": "Element not found", "code": "not_found", "retryable": True, "hint": "Take a snapshot",
              "state": {"tabId": 7}}, kitsune.KitsuneError),
            ({"ok": False, "dispatched": True, "error": "Expected outcome not observed within 5000ms",
              "code": "outcome_not_observed", "retryable": False}, kitsune.KitsuneError),
            ({"error": "Confirmation required: submit", "code": "confirmation_required", "retryable": True,
              "state": {"confirmToken": "tok", "expiresInSeconds": 120}}, kitsune.ConfirmationRequired),
            ({"error": "Kitsune extension not connected", "code": "extension_disconnected", "retryable": True},
             kitsune.ConnectionFailed),
            ({"error": "Command timed out after 30s", "code": "timeout", "retryable": False}, kitsune.TimedOut),
        ]
        for result, error_type in cases:
            with self.subTest(code=result["code"]):
                self.reply("/api/click", result)
                with self.assertRaises(error_type) as raised:
                    self.tab.click("e3")
                self.assertIs(type(raised.exception), error_type)
                self.assertEqual((raised.exception.code, raised.exception.retryable, raised.exception.result),
                                 (result["code"], result["retryable"], result))
                self.assertTrue(str(raised.exception).startswith(f"{result['error']} [{result['code']}]"))
        self.assertIn("\nHint: Take a snapshot", str(kitsune.KitsuneError(cases[0][0])))
        held = kitsune.ConfirmationRequired(cases[2][0])
        self.assertEqual((held.token, held.expires_in), ("tok", 120))
        self.assertIsInstance(kitsune.ConnectionFailed(cases[3][0]), ConnectionError)
        self.assertIsInstance(kitsune.TimedOut(cases[4][0]), TimeoutError)

    def test_warnings_and_container_failures_do_not_raise(self):
        warned = {"ok": True, "warning": {"code": "hidden_or_covered"}}
        self.reply("/api/click", warned)
        self.assertEqual(self.tab.click("e3"), warned)
        partial = {"results": [{"ok": True}, {"error": "stale ref", "code": "stale_ref"}], "status": "failed"}
        self.reply("/api/batch", partial)
        self.assertEqual(self.browser.batch([{"action": "click"}] * 2), partial)

    def test_a_load_that_times_out_raises_with_the_open_tab(self):
        self.reply("/api/new-tab", {"ok": False, "timedOut": True, "tabId": 9, "url": "https://slow.example/",
                                    "status": "loading"})
        with self.assertRaises(kitsune.TimedOut) as raised:
            self.browser.new_tab("https://slow.example/")
        self.assertEqual(raised.exception.result["tabId"], 9)
        self.assertEqual(str(raised.exception), "Timed out")

    def test_a_wait_that_runs_out_raises_timed_out(self):
        self.reply("/api/wait-for-text", {"ok": False, "text": "Done", "elapsed": 300,
                                          "error": "Timed out after 300ms waiting for visible text"})
        with self.assertRaises(kitsune.TimedOut) as raised:
            self.tab.wait_for(text="Done", timeout=300)
        self.assertEqual(raised.exception.result["elapsed"], 300)
        # Other wait failures keep their own error.
        self.reply("/api/wait-for-url", {"error": "Tab closed while waiting for url"})
        with self.assertRaises(kitsune.KitsuneError) as raised:
            self.tab.wait_for(url="/done")
        self.assertIs(type(raised.exception), kitsune.KitsuneError)
        self.reply("/api/wait-for-element", {"ok": False, "error": "stale ref", "code": "stale_ref", "retryable": True})
        with self.assertRaises(kitsune.KitsuneError) as raised:
            self.tab.wait_for(target="e2")
        self.assertIs(type(raised.exception), kitsune.KitsuneError)
        for conditions in ({}, {"text": "a", "url": "b"}, {"text": ""}):
            with self.subTest(conditions=conditions), self.assertRaises(ValueError):
                self.tab.wait_for(**conditions)

    def test_confirm_callback_repeats_a_held_action_with_its_token(self):
        held = {"error": "Confirmation required: submit form", "code": "confirmation_required", "retryable": True,
                "state": {"confirmToken": "tok", "expiresInSeconds": 120, "action": "submitForm"}}
        asked = []
        self.browser.confirm = lambda error: asked.append(error) or True
        self.reply("/api/submit-form", [held, {"ok": True, "form": "#signup"}])
        self.assertEqual(self.tab.submit_form("#signup"), {"ok": True, "form": "#signup"})
        self.assertEqual([request.body for request in self.bridge.requests],
                         [{"selector": "#signup", "tabId": 7}, {"selector": "#signup", "tabId": 7, "confirmToken": "tok"}])
        self.assertEqual([error.token for error in asked], ["tok"])
        # Declined: the action is not sent again.
        self.browser.confirm = lambda error: False
        self.reply("/api/submit-form", [held])
        with self.assertRaises(kitsune.ConfirmationRequired):
            self.tab.submit_form("#signup")
        self.assertEqual(len(self.bridge.requests), 3)

    def test_a_user_tab_closes_once_approved(self):
        refused = {"error": "Tab 7 was not opened by the agent, so it was not closed", "code": "user_tab",
                   "retryable": False, "state": {"tabId": 7, "confirmToken": "tok", "expiresInSeconds": 120}}
        self.reply("/api/close-tab", [refused])
        with self.assertRaises(kitsune.ConfirmationRequired) as raised:
            self.tab.close()
        self.assertEqual((raised.exception.code, raised.exception.token), ("user_tab", "tok"))
        self.browser.confirm = lambda error: True
        self.reply("/api/close-tab", [refused, {"ok": True}])
        self.assertEqual(self.tab.close(), {"ok": True})
        self.assertEqual(self.bridge.last.body, {"tabId": 7, "confirmToken": "tok"})
        # read and draft refuse without a token, which nothing can approve.
        self.reply("/api/close-tab", [{**refused, "state": {"tabId": 7}}])
        with self.assertRaises(kitsune.KitsuneError) as raised:
            self.tab.close()
        self.assertIs(type(raised.exception), kitsune.KitsuneError)


class PackageTests(unittest.TestCase):
    def test_the_package_loads_no_mcp_code(self):
        code = ("import json, sys, kitsune; kitsune.connect; "
                "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'mcp')))")
        loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(loaded), [])


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        for patcher in (patch.object(bridge_client.httpx, "request", side_effect=self.bridge),
                        patch.dict(os.environ, {"KITSUNE_TOKEN": "test-token"}),
                        patch.object(bridge_client.bridge_control, "status",
                                     return_value={"build": bridge_client.BRIDGE_BUILD})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requests_carry_no_caps_header_and_the_chosen_restrictions(self):
        browser = kitsune.connect()
        browser.tab(7).text()
        headers = self.bridge.last.headers
        self.assertNotIn(capabilities.CAPS_HEADER, headers)
        self.assertNotIn(task_modes.MODE_HEADER, headers)
        self.assertNotIn(task_modes.PROTECT_USER_TABS_HEADER, headers)
        kitsune.connect(mode="draft", protect_user_tabs=True).tab(7).text()
        headers = self.bridge.last.headers
        self.assertEqual((headers[task_modes.MODE_HEADER], headers[task_modes.PROTECT_USER_TABS_HEADER]), ("draft", "1"))
        with self.assertRaises(ValueError):
            kitsune.connect(mode="unsafe")

    def test_connect_waits_for_the_extension_before_reading_its_status(self):
        # A bridge that connect() started answers before the extension reconnects (#258).
        order = []
        self.bridge.replies["/api/status"] = lambda: order.append("status") or REPLIES["/api/status"]
        with patch.object(BridgeClient, "await_extension", lambda client: order.append("await")):
            kitsune.connect()
        self.assertEqual(order, ["await", "status"])

    def test_a_disconnected_extension_fails_at_connect(self):
        self.bridge.replies["/api/status"] = {"status": "running", "extension_connected": False, "diagnosis": {
            "code": "extension_disconnected", "hint": "Open the browser profile where Kitsune is installed"}}
        with self.assertRaises(kitsune.ConnectionFailed) as raised:
            kitsune.connect()
        self.assertEqual((raised.exception.code, raised.exception.hint),
                         ("extension_disconnected", "Open the browser profile where Kitsune is installed"))

    def test_a_bridge_of_another_build_is_left_running(self):
        with patch.object(bridge_client.bridge_control, "status", return_value={"build": "0.4.1+other"}), \
                patch.object(bridge_client.bridge_control, "stop") as stop, \
                patch.object(bridge_client.subprocess, "Popen") as spawn, \
                patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": ""}):
            with self.assertRaises(kitsune.ConnectionFailed) as raised:
                kitsune.connect()
            self.assertEqual(raised.exception.code, "bridge_version_mismatch")
            self.assertEqual(raised.exception.state["bridgeBuild"], "0.4.1+other")
            stop.assert_not_called()
            spawn.assert_not_called()
            # replace_bridge=True takes the MCP server's path: stop the old bridge first.
            stop.return_value = "cannot stop"
            with patch.object(bridge_client.bridge_control, "lifecycle_lock"), \
                    self.assertRaises(kitsune.ConnectionFailed) as raised:
                kitsune.connect(replace_bridge=True)
            stop.assert_called_once()
            self.assertEqual((raised.exception.code, str(raised.exception).split(" [")[0]),
                             ("bridge_unavailable", "cannot stop"))


class McpParityTests(BridgeTestCase):
    """The library sends what the matching MCP tools send, so both rest on the same tested requests."""

    def sent(self, call):
        self.bridge.requests.clear()
        call()
        return [(request.route, request.body, request.timeout) for request in self.bridge.requests]

    def test_actions_and_reads_match_the_mcp_tools(self):
        expect = {"url_changed": True, "timeout": 2000}
        with patch.object(playwright_tools, "_current_tab", 7), patch.object(playwright_tools, "_clicked", {}):
            pairs = [
                (lambda: playwright_tools.browser_click(target="e3"), lambda: self.tab.click("e3")),
                (lambda: playwright_tools.browser_click(target="#go"), lambda: self.tab.click("#go")),
                (lambda: playwright_tools.browser_hover(target="e5"), lambda: self.tab.hover("e5")),
                (lambda: playwright_tools.browser_type(target="#q", text="hi"), lambda: self.tab.fill("#q", "hi")),
                (lambda: playwright_tools.browser_evaluate(function="() => 1"), lambda: self.tab.evaluate("() => 1")),
                (lambda: playwright_tools.browser_navigate(url="https://example.com/"),
                 lambda: self.tab.navigate("https://example.com/")),
            ]
            for mcp_call, library_call in pairs:
                with self.subTest(tool=mcp_call.__code__.co_names[-1]):
                    sent = self.sent(library_call)
                    self.assertTrue(sent)
                    self.assertEqual(sent, self.sent(mcp_call))
        pairs = [
            (lambda: server.browser_click(ref="e3", tab_id=7, wait=True, timeout=3000, expect=expect, first=True),
             lambda: self.tab.click("e3", wait=True, timeout=3000, expect=expect, first=True)),
            (lambda: server.browser_type(selector="#q", text="hi", clear=True, tab_id=7),
             lambda: self.tab.type("#q", "hi", clear=True)),
            (lambda: server.browser_check(ref="e4", checked=False, tab_id=7), lambda: self.tab.uncheck("e4")),
            (lambda: server.browser_select_option(selector="#s", value="b", by_text=True, tab_id=7),
             lambda: self.tab.select_option("#s", "b", by_text=True)),
            (lambda: server.browser_keypress(key="Enter", selector="#q", tab_id=7), lambda: self.tab.press("Enter", "#q")),
            (lambda: server.browser_scroll(direction="up", amount=200, ref="e9", tab_id=7),
             lambda: self.tab.scroll("up", 200, target="e9")),
            (lambda: server.browser_submit_form(tab_id=7, confirm_token="tok"), lambda: self.tab.submit_form(confirm="tok")),
            (lambda: server.browser_form_fill(fields={"email": "a@b.c"}, tab_id=7, expect=expect),
             lambda: self.tab.fill_form({"email": "a@b.c"}, expect=expect)),
            (lambda: server.browser_navigate(url="https://example.com/", tab_id=7, expect=expect),
             lambda: self.tab.navigate("https://example.com/", expect=expect)),
            (lambda: server.browser_reload_tab(bypass_cache=True, tab_id=7), lambda: self.tab.reload(bypass_cache=True)),
            (lambda: server.browser_back(tab_id=7), lambda: self.tab.back()),
            (lambda: server.browser_handle_dialog(accept=True, prompt_text="Ada", tab_id=7),
             lambda: self.tab.handle_dialogs(True, "Ada")),
            (lambda: server.browser_wait_for(text="Done", timeout=3000, tab_id=7),
             lambda: self.tab.wait_for(text="Done", timeout=3000)),
            (lambda: server.browser_wait_for(url="/done$", tab_id=7), lambda: self.tab.wait_for(url="/done$")),
            (lambda: server.browser_page_text(tab_id=7), lambda: self.tab.text()),
            (lambda: server.browser_snapshot(tab_id=7, ref="e2", max_depth=1), lambda: self.tab.snapshot("e2", max_depth=1)),
            (lambda: server.browser_query(selector="a", fields=["href"], limit=5, tab_id=7),
             lambda: self.tab.query("a", fields=["href"], limit=5)),
            (lambda: server.browser_html(ref="e5", tab_id=7), lambda: self.tab.html("e5")),
            (lambda: server.browser_markdown(selector="main", tab_id=7), lambda: self.tab.markdown("main")),
            (lambda: server.browser_find(query="login", tab_id=7), lambda: self.tab.find("login")),
            (lambda: server.browser_links(internal_only=True, tab_id=7), lambda: self.tab.links(internal_only=True)),
            (lambda: server.browser_screenshot(ref="e1", tab_id=7, data_url=True),
             lambda: self.tab.screenshot(target="e1")),
            (lambda: server.browser_tabs("new", url="https://example.com/", wait=True),
             lambda: self.browser.new_tab("https://example.com/")),
            (lambda: server.browser_tabs("close", tab_id=7, force=True), lambda: self.tab.close(force=True)),
            (lambda: server.browser_batch([{"action": "click", "selector": "#a"}], tab_id=7),
             lambda: self.browser.batch([{"action": "click", "selector": "#a"}], tab=7)),
            (lambda: server.browser_workflow("test", {"q": "x"}, tab_id=7), lambda: self.browser.workflow("test", {"q": "x"}, tab=7)),
            (lambda: server.browser_history_visits("https://example.com/"),
             lambda: self.browser.history.visits("https://example.com/")),
            (lambda: server.browser_bookmarks_search("docs"), lambda: self.browser.bookmarks.search("docs")),
        ]
        for mcp_call, library_call in pairs:
            with self.subTest(tool=mcp_call.__code__.co_names[-1]):
                sent = self.sent(library_call)
                self.assertTrue(sent)
                self.assertEqual(sent, self.sent(mcp_call))


if __name__ == "__main__":
    unittest.main()
