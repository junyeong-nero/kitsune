"""Real local Python workers with a mocked host transport; no user's bridge is contacted."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from kitsune import script_runner, script_worker


def _exited(pid):
    """Whether no process runs with pid. On Windows os.kill(pid, 0) would terminate it."""
    if os.name == "nt":
        import psutil

        return not psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False

ROOT = Path(__file__).resolve().parent.parent
# A successful action's reply as the bridge returns it (#575), with fields a script result leaves out.
DELTA = {"ok": True, "tag": "button", "text": "Save", "visibilityState": "hidden", "target_ref": "e6",
         "snapshot": '- button "Save" [ref=e6]\n- text "Saved Ada"\n- button "Undo save" [ref=e24]',
         "snapshot_delta": True}
ABSENT = object()


class Client:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.called = threading.Event()

    def request(self, method, route, **kwargs):
        self.calls.append((method, route, kwargs))
        self.called.set()
        reply = self.replies.get(route, {"ok": True})
        return reply() if callable(reply) else reply


class RefClient(Client):
    """Replies by the request's ref."""

    def request(self, method, route, **kwargs):
        self.calls.append((method, route, kwargs))
        return json.loads(json.dumps(self.replies[kwargs["json"]["ref"]]))


def _stale(ref, same=None, follow=False):
    """The extension's other_document error, with same_element and the script-only follow marker (#572)."""
    state = {"reason": "other_document", "ref": ref, "tabId": 7}
    if same:
        state["same_element"] = {"ref": same, "role": "link", "name": "Bookmarks", **({"follow": True} if follow else {})}
    return {"error": "stale ref, call browser_snapshot again", "code": "stale_ref", "retryable": True,
            "hint": "Take a new browser_snapshot.", "state": state}


class ScriptWorkerStartTests(unittest.TestCase):
    def test_worker_imports_no_bridge_client(self):
        # Every browser_run_python call starts a worker, whose Browser sends through the host: the
        # bridge client and httpx, most of a worker's start time, are not loaded for it.
        loaded = set(subprocess.check_output(
            [sys.executable, "-c", "import sys, kitsune.script_worker; print(' '.join(sys.modules))"],
            text=True).split())
        self.assertIn("kitsune.browser", loaded)
        self.assertEqual(loaded & {"kitsune.bridge_client", "kitsune.version", "httpx"}, set())


class ScriptRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_print_and_selected_structured_results(self):
        result = await script_runner.run_python("print('hello'); result = {'values': [n * 2 for n in range(3)]}", Client())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"], "hello\n")
        self.assertEqual(result["result"], {"values": [0, 2, 4]})
        self.assertEqual(result["operations"], [])

    async def test_browser_calls_use_the_supplied_client_and_do_not_echo_intermediate_results(self):
        client = Client({"/api/tabs": {"tabs": [{"id": 7}]}, "/api/page-text": {"text": "private intermediate"}})
        code = "for tab in browser.tabs():\n    text = browser.tab(tab['id']).text()\nresult = len(text)"
        result = await script_runner.run_python(code, client)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], len("private intermediate"))
        self.assertNotIn("private intermediate", json.dumps(result))
        self.assertEqual([(method, route) for method, route, _ in client.calls],
                         [("GET", "/api/tabs"), ("POST", "/api/page-text")])
        self.assertEqual(client.calls[1][2]["json"], {"tabId": 7})
        self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "ok"])

    async def test_selected_tab_uses_the_same_browser_without_an_extra_request(self):
        client = Client()
        result = await script_runner.run_python("result = [tab.id, tab.browser is browser]", client, tab_id=7)
        self.assertEqual(result["result"], [7, True])
        self.assertEqual(client.calls, [])
        self.assertIs((await script_runner.run_python("result = tab is None", client))["result"], True)

    async def test_variables_reset_between_calls(self):
        client = Client()
        self.assertTrue((await script_runner.run_python("secret = 42", client))["ok"])
        result = await script_runner.run_python("result = 'secret' in globals()", client)
        self.assertIs(result["result"], False)

    async def test_time_re_and_json_are_preloaded_and_can_still_be_imported(self):
        # #574: scripts used time.sleep, re and json after earlier operations had been sent.
        client = Client()
        code = ("tab.fill('#city', 'Vancouver'); time.sleep(0.01)\n"
                "result = [re.sub(r'\\d+', '#', 'a1b22'), json.dumps({'a': 1}), json.loads('[1]')]")
        result = await script_runner.run_python(code, client, tab_id=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], ["a#b#", '{"a": 1}', [1]])
        self.assertEqual([route for _, route, _ in client.calls], ["/api/fill"])
        code = "import time, re, json\nimport json as j\nresult = [j is json, time.monotonic() > 0, re.escape('.')]"
        result = await script_runner.run_python(code, client)
        self.assertEqual(result["result"], [True, True, "\\."])

    async def test_errors_and_non_json_results_are_reported(self):
        for code, expected in [("if", "SyntaxError"), ("1 / 0", "ZeroDivisionError"),
                               ("result = b'bytes'", "TypeError"), ("raise SystemExit(2)", "SystemExit")]:
            with self.subTest(code=code):
                result = await script_runner.run_python(code, Client())
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "script_error")
                self.assertIn(expected, result["output"])
                self.assertFalse(result["retryable"])

    async def test_direct_tool_keywords_explain_the_python_contract_without_replaying_writes(self):
        cases = [("tab.find(regex='.')", "Tab.find", "description", "query"),
                 ("tab.find(text='원')", "Tab.find", "description", "query"),
                 ("tab.snapshot(boxes=True)", "Tab.snapshot", "tab.bounds(target)", "max_depth"),
                 ("tab.snapshot(depth=2)", "Tab.snapshot", "max_depth", "text")]
        for call, method, guidance, parameter in cases:
            with self.subTest(call=call):
                client = Client()
                code = f"tab.fill('#draft', 'keep')\n{call}\ntab.fill('#draft', 'must not run')"
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertEqual(result["code"], "script_error")
                self.assertFalse(result["retryable"])
                self.assertIn("TypeError", result["output"])
                self.assertIn(f"Python API: {method}(", result["output"])
                self.assertIn(guidance, result["output"])
                self.assertIn(parameter, result["output"])
                self.assertEqual([route for _, route, _ in client.calls], ["/api/fill"])
                self.assertEqual([entry["status"] for entry in result["operations"]], ["ok"])
                self.assertIn("do not replay", result["hint"])

    async def test_method_help_is_local_and_unrelated_type_errors_are_not_rewritten(self):
        client = Client()
        result = await script_runner.run_python("help(tab.find); help(tab.snapshot)", client, tab_id=7)
        self.assertTrue(result["ok"], result)
        self.assertIn("description", result["output"])
        self.assertIn("CSS fast path", result["output"])
        self.assertIn("tab.bounds(target)", result["output"])
        self.assertEqual(client.calls, [])
        result = await script_runner.run_python("len(value='wrong')", client)
        self.assertEqual(result["code"], "script_error")
        self.assertIn("TypeError", result["output"])
        self.assertNotIn("Python API:", result["output"])

    async def test_any_tab_or_browser_call_that_does_not_bind_prints_the_real_signature(self):
        # #574: #524's help covered Tab.find and Tab.snapshot only. The first two calls are the run's.
        cases = [("tab.select_option('e1', label='x')", "Tab.select_option(self, target: str, value: str, *, by_text: bool"),
                 ("tab.wait_for('e1')", "Tab.wait_for(self, *, target: str | None"),
                 ("tab.check()", "Tab.check(self, target: str, checked: bool = True"),
                 ("tab.fill('e1', 'a', value='b')", "Tab.fill(self, target: str, value: str"),
                 ("tab.uncheck('e1', label='x')", "Tab.check(self, target: str"),
                 ("tab.back(1)", "Tab.back(self, *, expect"),
                 ("browser.new_tab(link='https://example.com')", "Browser.new_tab(self, url: str = 'about:blank'"),
                 ("browser.tab()", "Browser.tab(self, tab_id: int)")]
        for call, signature in cases:
            with self.subTest(call=call):
                client = Client()
                code = f"tab.fill('#draft', 'keep')\n{call}\ntab.fill('#draft', 'must not run')"
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertEqual(result["code"], "script_error")
                self.assertIn("TypeError: " + signature.split("(")[0] + "() ", result["output"])
                self.assertIn("Python API: " + signature, result["output"])
                self.assertEqual([route for _, route, _ in client.calls], ["/api/fill"])
        output = (await script_runner.run_python("tab.back(1)", Client(), tab_id=7))["output"]
        self.assertTrue(output.endswith("-> dict"), "a method without a docstring prints no 'None'")

    async def test_type_errors_raised_inside_a_method_or_outside_the_api_get_no_signature(self):
        for code in ("tab.click(5)",  # the target is matched as a string inside the call
                     "tab.wait_for(text='x', timeout='5')",
                     "tab._request()",  # not public
                     # The words of a call that did not bind, raised while the method runs.
                     "def refuse(*args, **kwargs):\n"
                     "    raise TypeError(\"Tab.text() got an unexpected keyword argument 'x'\")\n"
                     "browser.client.request = refuse\ntab.text()"):
            with self.subTest(code=code):
                client = Client()
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertEqual(result["code"], "script_error")
                self.assertIn("TypeError", result["output"])
                self.assertNotIn("Python API:", result["output"])
                self.assertEqual(client.calls, [])

    async def test_failed_operation_stops_even_a_catching_script_and_removes_tokens(self):
        failure = {"code": "needs_user", "error": "Approval required", "retryable": False,
                   "state": {"reason": "approval", "confirmToken": "secret-token"}}
        client = Client({"/api/click": failure})
        code = ("browser.tab(7).fill('#name', 'draft')\n"
                "try:\n    browser.tab(7).click('#send')\n"
                "except Exception:\n    browser.tab(7).fill('#other', 'must not run')\n")
        result = await script_runner.run_python(code, client)
        self.assertEqual(result["code"], "script_operation_failed")
        self.assertEqual([route for _, route, _ in client.calls], ["/api/fill", "/api/click"])
        self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
        self.assertEqual(result["failed_operation"]["code"], "needs_user")
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertNotIn("resume_token", result)

    async def test_caught_read_and_wait_failures_raise_the_documented_exceptions_and_continue(self):
        # #568: a read or wait changed nothing, so the script gets its exception as outside scripts.
        unmet = {"ok": False, "error": "Timed out after 2000ms waiting for visible text", "elapsed": 2000}
        stale = {"error": "Ref e99 is no longer valid", "code": "stale_ref", "retryable": False}
        client = Client({"/api/wait-for-text": unmet, "/api/snapshot": stale})
        code = ("seen = []\n"
                "try:\n    tab.wait_for(text='Saved', timeout=2000)\n"
                "except TimedOut as error:\n    seen.append([type(error).__name__, error.result['elapsed']])\n"
                "try:\n    tab.wait_for(text='Saved', timeout=2000)\n"
                "except TimeoutError:\n    seen.append('builtin')\n"
                "try:\n    tab.snapshot(target='e99')\n"
                "except KitsuneError as error:\n    seen.append(error.code)\n"
                "tab.fill('#name', 'after the caught failures')\nresult = seen")
        result = await script_runner.run_python(code, client, tab_id=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], [["TimedOut", 2000], "builtin", "stale_ref"])
        self.assertEqual([route for _, route, _ in client.calls],
                         ["/api/wait-for-text", "/api/wait-for-text", "/api/snapshot", "/api/fill"])
        self.assertEqual([entry["status"] for entry in result["operations"]], ["failed", "failed", "failed", "ok"])
        self.assertNotIn("failed_operation", result)

    async def test_wait_for_help_in_a_script_says_which_unmet_waits_it_can_catch(self):
        result = await script_runner.run_python("help(tab.wait_for)", Client(), tab_id=7)
        self.assertTrue(result["ok"], result)
        shown = " ".join(result["output"].split())
        self.assertIn("else raise TimedOut", shown)
        self.assertIn("catch TimedOut (or TimeoutError) to go on after an unmet wait", shown)
        self.assertIn("A js wait is the exception", shown)

    async def test_every_read_route_delivers_its_failure_and_no_other_route_does(self):
        failure = {"error": "The tab is loading", "code": "navigating", "retryable": True}
        code = ("try:\n    browser.request({route!r}, {{'tabId': 7}})\n"
                "except KitsuneError as error:\n    result = error.code\n")
        self.assertEqual(script_runner.READ_ROUTES, {
            "/api/find", "/api/forms", "/api/page-info", "/api/page-text", "/api/query", "/api/snapshot",
            "/api/wait-for-element", "/api/wait-for-network-idle", "/api/wait-for-text", "/api/wait-for-title",
            "/api/wait-for-url"})
        for route in sorted(script_runner.READ_ROUTES):
            with self.subTest(route=route):
                result = await script_runner.run_python(code.format(route=route), Client({route: failure}))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["result"], "navigating")
        # Agent JavaScript, a navigation, a view change, a read that scrolls, and spellings the
        # bridge also accepts for a read: none is delivered.
        for route in ("/api/wait-for-result", "/api/js", "/api/navigate", "/api/scroll", "/api/hover",
                      "/api/element-screenshot", "/api/storage", "/api/batch", "/api/pageText",
                      "/api/snapshot?tab=7"):
            with self.subTest(route=route):
                client = Client({route: failure})
                result = await script_runner.run_python(code.format(route=route), client)
                self.assertEqual(result["code"], "script_operation_failed", result)
                self.assertEqual(result["failed_operation"], failure)
                self.assertEqual(len(client.calls), 1)

    async def test_read_routes_are_not_bridge_writes_or_held_actions(self):
        from kitsune import bridge
        actions = {"/api/find": "find", "/api/forms": "getFormFields", "/api/page-info": "getPageInfo",
                   "/api/page-text": "getPageText", "/api/query": "query", "/api/snapshot": "getSnapshot",
                   "/api/wait-for-element": "waitForElement", "/api/wait-for-network-idle": "waitForNetworkIdle",
                   "/api/wait-for-text": "waitForText", "/api/wait-for-title": "waitForTitle",
                   "/api/wait-for-url": "waitForUrl"}
        self.assertEqual(set(actions), script_runner.READ_ROUTES)
        guarded = (bridge.WRITE_ACTIONS | bridge._REPEAT_UNSAFE_ACTIONS | bridge._ASK_JS_ACTIONS
                   | bridge._ASK_COOKIE_ACTIONS | set(bridge._ASK_DATA_ACTIONS) | bridge._ASK_NAVIGATION_ACTIONS
                   | bridge._ASK_TAB_ACTIONS)
        self.assertEqual(set(actions.values()) & guarded, set())

    async def test_a_find_that_loads_more_scrolls_the_page_and_stops_like_a_write(self):
        failure = {"error": "The tab is loading", "code": "navigating", "retryable": True}
        for key in ("loadMore", "load_more"):
            with self.subTest(key=key):
                client = Client({"/api/find": failure})
                code = (f"try:\n    browser.request('/api/find', {{'tabId': 7, 'query': 'row', {key!r}: True}})\n"
                        "except Exception:\n    browser.tab(7).fill('#other', 'must not run')\n")
                result = await script_runner.run_python(code, client)
                self.assertEqual(result["code"], "script_operation_failed", result)
                self.assertEqual([route for _, route, _ in client.calls], ["/api/find"])

    async def test_uncaught_read_failure_ends_the_script_as_a_failed_operation(self):
        unmet = {"ok": False, "found": False, "selector": "#row", "elapsed": 2000,
                 "error": 'Timed out after 2000ms waiting for "#row"', "confirmToken": "secret-token"}
        client = Client({"/api/wait-for-element": unmet})
        code = ("tab.fill('#name', 'draft')\nprint('filled')\n"
                "tab.wait_for(target='#row', timeout=2000)\n"
                "tab.fill('#other', 'must not run')\n")
        result = await script_runner.run_python(code, client, tab_id=7)
        self.assertEqual(result["code"], "script_operation_failed", result)
        self.assertEqual(result["error"], "A browser operation failed; the script was stopped")
        self.assertEqual(result["failed_operation"], {key: value for key, value in unmet.items() if key != "confirmToken"})
        self.assertEqual([route for _, route, _ in client.calls], ["/api/fill", "/api/wait-for-element"])
        self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
        self.assertEqual(result["output"], "filled\n")
        self.assertFalse(result["retryable"])
        self.assertNotIn("secret-token", json.dumps(result))
        # An oversized reply is cut as for a write.
        client = Client({"/api/snapshot": {"error": "failed", "code": "not_found", "snapshot": "x" * 20000}})
        result = await script_runner.run_python("tab.snapshot(target='#gone')", client, tab_id=7)
        self.assertEqual(result["failed_operation"], {"error": "failed", "code": "not_found", "truncated": True})

    async def test_a_caught_failure_raised_again_later_is_an_ordinary_script_error(self):
        unmet = {"ok": False, "error": "Timed out after 100ms waiting for visible text"}
        client = Client({"/api/wait-for-text": unmet, "/api/page-text": {"text": "later read"}})
        code = ("try:\n    tab.wait_for(text='Saved', timeout=100)\n"
                "except TimedOut as error:\n    kept = error\n"
                "tab.text()\nraise kept\n")
        result = await script_runner.run_python(code, client, tab_id=7)
        self.assertEqual(result["code"], "script_error", result)
        self.assertNotIn("failed_operation", result)
        self.assertIn("TimedOut", result["output"])
        self.assertEqual([entry["status"] for entry in result["operations"]], ["failed", "ok"])

    async def test_caught_failed_write_still_stops_the_script_with_earlier_operations_and_output(self):
        cases = [("tab.click('#send')", "/api/click", {"error": "No element matches", "code": "not_found"}),
                 ("tab.navigate('https://example.test/next')", "/api/navigate",
                  {"ok": False, "timedOut": True, "url": "https://example.test/next"}),
                 ("tab.wait_for(js='window.ready', timeout=100)", "/api/wait-for-result",
                  {"ok": False, "error": "Timed out after 100ms"})]
        for call, route, failure in cases:
            with self.subTest(call=call):
                client = Client({route: failure})
                code = ("tab.fill('#name', 'draft')\nprint('collected so far')\n"
                        f"try:\n    {call}\n"
                        "except BaseException:\n    tab.fill('#other', 'must not run')\n"
                        "result = 'must not be returned'\n")
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertEqual(result["code"], "script_operation_failed", result)
                self.assertEqual(result["failed_operation"], failure)
                self.assertEqual([called for _, called, _ in client.calls], ["/api/fill", route])
                self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
                self.assertEqual(result["output"], "collected so far\n")
                self.assertNotIn("result", result)

    async def test_user_and_policy_outcomes_on_a_read_route_stop_even_a_catching_script(self):
        approval = {"reason": "approval", "detail": "tool_approval", "tool": "browser_snapshot"}
        failures = [
            {"error": "browser_snapshot needs the user's approval", "code": "needs_user", "retryable": False,
             "state": approval},
            {"error": "Approval required", "code": "needs_user", "retryable": False,
             "state": {"reason": "approval", "detail": "confirmation_required", "confirmToken": "secret-token"}},
            {"error": "The agent needs the user", "code": "needs_user", "state": {"reason": "agent_request"}},
            {"error": "Held", "code": "confirmation_required", "state": {"confirmToken": "secret-token"}},
            {"error": "A user owns the tab", "code": "user_tab_read_only", "retryable": False},
            {"error": "The user is using the tab", "code": "user_active", "retryable": True},
            {"error": "Permission needed", "code": "permission_required", "retryable": False},
            {"error": "Site access is withheld", "code": "host_permission_denied", "retryable": True},
            # The bridge's policy refusals carry no code.
            {"error": "Read-only mode is active; refusing action: waitForResult"},
            {"error": "URL denied by policy (example): https://example.test/"},
            {"error": "URL not in allowlist: https://example.test/"},
            {"ok": False, "error": "Timed out after 100ms", "needs_user": {"reason": "human_check"}},
        ]
        for failure in failures:
            for call, route in (("tab.snapshot()", "/api/snapshot"),
                                ("tab.wait_for(text='Saved', timeout=100)", "/api/wait-for-text")):
                with self.subTest(failure=failure, route=route):
                    client = Client({route: failure})
                    code = ("tab.fill('#name', 'draft')\n"
                            f"try:\n    {call}\n"
                            "except BaseException:\n    tab.fill('#other', 'must not run')\n")
                    result = await script_runner.run_python(code, client, tab_id=7)
                    self.assertEqual(result["code"], "script_operation_failed", result)
                    self.assertEqual([called for _, called, _ in client.calls], ["/api/fill", route])
                    self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
                    self.assertEqual(result["failed_operation"].get("code"), failure.get("code"))
                    self.assertNotIn("secret-token", json.dumps(result))
                    if failure.get("code") == "needs_user":
                        self.assertEqual(result["failed_operation"]["recovery_tool"],
                                         "browser_snapshot" if route == "/api/snapshot" else "browser_wait_for")

    async def test_a_declined_tool_approval_on_a_read_route_stops_even_a_catching_script(self):
        refusal = {"error": "browser_snapshot needs the user's approval", "code": "needs_user", "retryable": False,
                   "state": {"reason": "approval", "detail": "tool_approval", "tool": "browser_snapshot"}}
        declined = {**refusal, "state": {**refusal["state"], "decision": "declined"}}
        asked = []

        async def approve(tool):
            asked.append(tool)
            return declined

        client = Client({"/api/snapshot": refusal})
        code = "try:\n    tab.snapshot()\nexcept BaseException:\n    tab.fill('#other', 'must not run')\n"
        result = await script_runner.run_python(code, client, tab_id=7, approve=approve)
        self.assertEqual(result["code"], "script_operation_failed", result)
        self.assertEqual(result["failed_operation"]["state"]["decision"], "declined")
        self.assertEqual(asked, ["browser_snapshot"])
        self.assertEqual([route for _, route, _ in client.calls], ["/api/snapshot"])

    async def test_stale_link_ref_the_extension_says_to_follow_is_sent_again_and_reported(self):
        # #572: one operation, two requests; the second is the same request with the same link's ref.
        client = RefClient({"e334": _stale("e334", "e373", follow=True), "e373": {"ok": True, "navigated": {"url": "/b"}}})
        result = await script_runner.run_python("result = browser.tab(7).click('e334', expect={'url': '/b'})", client)
        self.assertTrue(result["ok"], result)
        bodies = [kwargs["json"] for _, _, kwargs in client.calls]
        self.assertEqual([route for _, route, _ in client.calls], ["/api/click", "/api/click"])
        self.assertEqual(bodies, [{"selector": "", "ref": ref, "expect": {"url": "/b"}, "tabId": 7} for ref in ("e334", "e373")])
        remapped = {"from": "e334", "to": "e373", "role": "link", "name": "Bookmarks"}
        self.assertEqual(result["result"], {"ok": True, "navigated": {"url": "/b"}, "remapped": remapped})
        self.assertEqual(result["operations"], [{"route": "/api/click", "status": "ok", "tab_id": 7,
                                                "remapped": remapped}])

    async def test_stale_ref_without_the_follow_marker_fails_as_before(self):
        for same in ("e373", None):
            with self.subTest(same=same):
                client = RefClient({"e334": _stale("e334", same)})
                result = await script_runner.run_python("browser.tab(7).click('e334')", client)
                self.assertEqual(result["code"], "script_operation_failed")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result["failed_operation"], _stale("e334", same))
                self.assertEqual(result["operations"], [{"route": "/api/click", "status": "failed", "tab_id": 7}])

    async def test_followed_request_is_checked_like_any_other_and_never_followed_twice(self):
        held = {"code": "needs_user", "error": "Confirmation required", "retryable": False,
                "state": {"reason": "approval", "confirmToken": "secret-token", "params": {"ref": "e373"}}}
        for second, code in ((held, "needs_user"), (_stale("e373", "e400", follow=True), "stale_ref")):
            with self.subTest(code=code):
                client = RefClient({"e334": _stale("e334", "e373", follow=True), "e373": second})
                result = await script_runner.run_python("browser.tab(7).click('e334')", client)
                self.assertEqual(result["code"], "script_operation_failed")
                self.assertEqual([kwargs["json"]["ref"] for _, _, kwargs in client.calls], ["e334", "e373"])
                remapped = {"from": "e334", "to": "e373", "role": "link", "name": "Bookmarks"}
                self.assertEqual(result["failed_operation"]["code"], code)
                self.assertEqual(result["failed_operation"]["remapped"], remapped)
                self.assertEqual(result["operations"][0]["status"], "failed")
                self.assertEqual(result["operations"][0]["remapped"], remapped)
                self.assertNotIn("secret-token", json.dumps(result))
                self.assertNotIn("follow", json.dumps(result["failed_operation"].get("state")))

    async def test_follow_marker_for_another_target_is_dropped_not_followed(self):
        # Only the request's own ref is replaced: a selector, or a ref the reply does not name, is not.
        for code in ("browser.tab(7).evaluate('el => el.id', 'e334')", "browser.tab(7).click('e335')"):
            with self.subTest(code=code):
                client = Client({route: _stale("e334", "e373", follow=True) for route in ("/api/js", "/api/click")})
                result = await script_runner.run_python(code, client)
                self.assertEqual(result["code"], "script_operation_failed")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result["failed_operation"], _stale("e334", "e373"))
                self.assertNotIn("remapped", result["operations"][0])

    async def test_ambiguous_target_hint_names_the_python_argument(self):
        # #573: the extension words its hint for the JSON tools (first: true); a script passes first=True.
        hint = "first: true acts on the first match."
        content = Path(__file__).parent.parent.joinpath("extension/content/content.js").read_text(encoding="utf-8")
        self.assertIn(hint, content, "the extension's ambiguous_target hint is the text the script host rewrites")
        failure = {"error": "2 elements match the selector; nothing was dispatched", "code": "ambiguous_target",
                   "retryable": False, "hint": f"Act on one element: pass its ref or a more specific selector. {hint}",
                   "state": {"selector": "input", "matches": 2, "candidates": []}}
        client = Client({"/api/fill": failure})
        result = await script_runner.run_python("tab.fill('input', 'x')", client, tab_id=7)
        self.assertEqual(result["code"], "script_operation_failed")
        self.assertEqual(result["failed_operation"]["code"], "ambiguous_target")
        self.assertIn("first=True acts on the first match.", result["failed_operation"]["hint"])
        self.assertNotIn("first: true", json.dumps(result))

    async def test_script_cannot_supply_an_approval_token_through_the_provided_api(self):
        client = Client()
        result = await script_runner.run_python("browser.tab(7).click('#send', confirm='pretend-approval')", client)
        self.assertEqual(result["code"], "script_approval_required")
        self.assertEqual(client.calls, [])

    async def test_non_ascii_output_and_result_cross_the_pipes(self):
        # Without UTF-8 mode a Windows worker would write its pipes in the ANSI code page.
        with patch.dict(os.environ, {"PYTHONUTF8": "0"}):
            result = await script_runner.run_python("print('여우 ✓')\nresult = '꼬리'", Client())
        self.assertEqual((result["output"], result["result"]), ("여우 ✓\n", "꼬리"), result)

    async def test_timeout_reaps_the_worker(self):
        result = await script_runner.run_python("import os\nprint(os.getpid())\nwhile True: pass", Client(), timeout=0.5)
        self.assertEqual(result["code"], "script_timeout")
        self.assertTrue(_exited(int(result["output"].strip())))

    async def test_timeout_preserves_unknown_inflight_operation_and_does_not_replay(self):
        class SlowClient(Client):
            """As BridgeClient: gives up at the request's timeout, which the script's deadline cut."""
            def request(self, method, route, **kwargs):
                self.calls.append((method, route, kwargs))
                time.sleep(kwargs["timeout"])
                return {"error": "timed out", "code": "timeout", "retryable": False}
        client = SlowClient()
        # #618: the script still gets to print what it had.
        result = await script_runner.run_python(
            "try:\n    browser.tab(7).click('#send'); browser.tab(7).click('#again')\n"
            "finally:\n    print('kept')", client, timeout=0.5)
        self.assertEqual(result["code"], "script_timeout", result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["operations"][0]["status"], "unknown")
        self.assertNotIn("failed_operation", result)
        self.assertEqual(result["output"], "kept\n")
        self.assertIn("do not replay the whole script", result["hint"], "the click may have happened")

    async def test_a_call_whose_own_timeout_ends_before_the_deadline_is_a_failed_call(self):
        timed_out = {"error": "timed out", "code": "timeout", "retryable": False}
        result = await script_runner.run_python("browser.tab(7).click('#send')", Client({"/api/click": timed_out}))
        self.assertEqual((result["code"], result["failed_operation"]), ("script_operation_failed", timed_out))
        self.assertEqual(result["operations"][0]["status"], "failed")

    async def test_a_reply_that_never_comes_ends_at_the_grace(self):
        def slow_reply():  # a client that ignores its timeout: the worker is killed after STOP_GRACE
            time.sleep(0.8 + script_runner.STOP_GRACE)
            return {"ok": True}
        client = Client({"/api/click": slow_reply})
        result = await script_runner.run_python("browser.tab(7).click('#send'); browser.tab(7).click('#again')",
                                                client, timeout=0.5)
        self.assertEqual(result["code"], "script_timeout")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["operations"][0]["status"], "unknown")

    async def test_output_and_selected_result_over_the_limit_are_cut_not_failed(self):
        # #570: a read over the limit is not a failed call. Printed output keeps its first 16,000
        # bytes; a string result gets what the output left, its JSON quotes included.
        for code, output, selected, dropped in (
                ("print('x' * 20000)", "x" * 16000, None, 4001),
                ("import sys; sys.stderr.write('x' * 20000)", "x" * 16000, None, 4000),
                ("result = 'x' * 20000", "", "x" * 15998, 4002),
                ("print('x' * 10000); result = 'y' * 10000", "x" * 10000 + "\n", "y" * 5997, 4003),
                ("print('x' * 16000, end=''); print('tail'); result = 'gone'", "x" * 16000, None, 9),
                # A result left out whole is marked even when it is the empty string.
                ("print('x' * 16000, end=''); result = ''", "x" * 16000, None, 1)):
            with self.subTest(code=code):
                result = await script_runner.run_python(code, Client())
                self.assertTrue(result["ok"], result)
                self.assertNotIn("error", result)
                self.assertNotIn("code", result)
                self.assertIs(result["truncated"], True)
                self.assertEqual(result["dropped_bytes"], dropped)
                self.assertEqual(result["output"], output)
                self.assertEqual(result.get("result"), selected)
                self.assertIn("not page-wide absence", result["hint"])
                self.assertLess(list(result).index("truncated"), list(result).index("output"),
                                "the marker is read before the cut content")
        for code in ("print('x' * 16000, end='')", "result = 'x' * 15998"):
            with self.subTest(code=code):
                result = await script_runner.run_python(code, Client())
                self.assertTrue(result["ok"], result)
                for key in ("truncated", "dropped_bytes", "hint"):
                    self.assertNotIn(key, result, "what fits is not marked")

    async def test_utf8_combined_output_boundary_is_not_a_character_budget(self):
        # 7,998 printed bytes + 8,002 bytes for the JSON string, including its quotes.
        for suffix, dropped in (("xx", 0), ("xxx", 1)):
            code = f"print('é' * 3999, end=''); result = '한' * 2666 + {suffix!r}"
            result = await script_runner.run_python(code, Client())
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["output"], "é" * 3999)
            self.assertEqual(result["result"], "한" * 2666 + "xx")
            self.assertEqual(len(result["output"].encode()) +
                             len(json.dumps(result["result"], ensure_ascii=False).encode()), 16000)
            self.assertEqual(result.get("dropped_bytes", 0), dropped)
            self.assertEqual("truncated" in result, bool(dropped))
        result = await script_runner.run_python("print('한' * 6000, end=''); print('x')", Client())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"], "한" * 5333, "no partial UTF-8 character, and nothing after the cut")
        self.assertEqual(result["dropped_bytes"], 6000 * 3 - 5333 * 3 + 2)
        # A result is cut between characters too; its JSON escapes count against the limit.
        result = await script_runner.run_python(r"result = '한\n' * 5000", Client())
        self.assertEqual(result["result"], "한\n" * 3199 + "한")
        self.assertEqual(len(json.dumps(result["result"], ensure_ascii=False).encode()), 16000)
        self.assertEqual(result["dropped_bytes"], 5000 * 4 - (3199 * 4 + 3))

    async def test_non_string_result_over_the_limit_is_the_start_of_its_json_text(self):
        # #570: the same cut and marker as a string, so wrapping a read in a dict or list is no error.
        rows = [{"text": "한"} for _ in range(2000)]
        whole = json.dumps({"rows": rows}, ensure_ascii=False)
        result = await script_runner.run_python("result = {'rows': [{'text': '한'} for _ in range(2000)]}", Client())
        self.assertTrue(result["ok"], result)
        self.assertIs(result["truncated"], True)
        self.assertIsInstance(result["result"], str)
        self.assertTrue(whole.startswith(result["result"]))
        self.assertTrue(result["result"].startswith('{"rows": [{"text": "한"}, '))
        self.assertLessEqual(len(json.dumps(result["result"], ensure_ascii=False).encode()), script_runner.OUTPUT_LIMIT)
        self.assertGreater(len(json.dumps(result["result"] + whole[len(result["result"])], ensure_ascii=False).encode()),
                           script_runner.OUTPUT_LIMIT, "as much as fits")
        self.assertEqual(result["dropped_bytes"], len(whole.encode()) - len(result["result"].encode()))
        self.assertIn("start of its JSON text", result["hint"])
        fits = await script_runner.run_python("result = {'rows': [{'text': '한'} for _ in range(200)]}", Client())
        self.assertEqual(fits["result"], {"rows": rows[:200]})
        self.assertNotIn("truncated", fits)

    async def test_observation_overflow_keeps_completed_writes_and_gives_scoped_recovery(self):
        # #570: the script runs to its end, so a write after an overflowing print is done and listed.
        for observation, routes in (("result = tab.text()", ["/api/fill", "/api/page-text"]),
                                    ("print(tab.text())\ntab.fill('#note', 'after the read')",
                                     ["/api/fill", "/api/page-text", "/api/fill"])):
            with self.subTest(observation=observation):
                client = Client({"/api/page-text": {"text": "한" * 6000},
                                 "/api/query": {"items": [{"text": "Delivery: three days"}]}})
                cut = await script_runner.run_python("tab.fill('#draft', 'keep')\n" + observation,
                                                     client, tab_id=7)
                self.assertTrue(cut["ok"], cut)
                self.assertIs(cut["truncated"], True)
                self.assertEqual(cut["dropped_bytes"], 6000 * 3 - 5332 * 3 if "result" in cut else 6000 * 3 + 1 - 5333 * 3)
                self.assertEqual(cut.get("result", cut["output"]), "한" * (5332 if "result" in cut else 5333))
                self.assertNotIn("resume_token", cut)
                self.assertEqual([route for _, route, _ in client.calls], routes)
                self.assertEqual([(op["route"], op["status"]) for op in cut["operations"]],
                                 [(route, "ok") for route in routes])
                for guidance in ("was cut", "tab.snapshot", "tab.query", "not page-wide absence", "do not replay"):
                    self.assertIn(guidance, cut["hint"])
                recovered = await script_runner.run_python(
                    "result = tab.query('#delivery', fields=['text'], limit=1)", client, tab_id=7)
                self.assertEqual(recovered["result"], [{"text": "Delivery: three days"}])
                self.assertEqual([route for _, route, _ in client.calls],
                                 routes + ["/api/query"], "only observe; never repeat the draft")

    async def test_failure_after_cut_output_still_says_why_and_is_marked(self):
        # #570: output over the limit no longer stops the script, so a later failure must not lose its reason.
        failed = await script_runner.run_python("print('x' * 20000)\nraise ValueError('boom')", Client())
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["code"], "script_error")
        self.assertIn("ValueError: boom", failed["error"])
        self.assertIs(failed["truncated"], True)
        self.assertEqual(failed["output"], "x" * 16000)
        self.assertIn("do not replay", failed["hint"])
        client = Client({"/api/click": {"error": "No element", "code": "not_found"}})
        stopped = await script_runner.run_python("print('x' * 20000)\ntab.click('#gone')", client, tab_id=7)
        self.assertEqual(stopped["code"], "script_operation_failed")
        self.assertEqual(stopped["failed_operation"]["code"], "not_found")
        self.assertEqual((stopped["truncated"], stopped["dropped_bytes"]), (True, 4001))
        endless = await script_runner.run_python("while True: print('x' * 1000)", Client(), timeout=0.5)
        self.assertEqual(endless["code"], "script_timeout", "the deadline, not the output limit, ends a print loop")
        self.assertIs(endless["truncated"], True)
        self.assertEqual(len(endless["output"]), 16000)

    # -- #575: a script of only actions returns what its last action changed ----------------

    async def test_script_of_only_actions_returns_its_last_actions_snapshot(self):
        client = Client({"/api/page-text": {"text": "Orders"}, "/api/click": DELTA,
                         "/api/fill": {**DELTA, "snapshot": '- textbox "Name" [ref=e2] [value="Ada"]'}})
        result = await script_runner.run_python("s = tab.text()\ntab.fill('e2', 'Ada')\ntab.click('e6')",
                                                client, tab_id=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"], "")
        # Labelled with the operation it came from; no other field of the action's reply.
        self.assertEqual(result["result"], {"last_action": "/api/click", "snapshot": DELTA["snapshot"],
                                            "snapshot_delta": True})
        self.assertEqual(result["operations"][-1]["route"], result["result"]["last_action"])
        self.assertEqual([route for _, route, _ in client.calls], ["/api/page-text", "/api/fill", "/api/click"],
                         "nothing is read for it")

    async def test_returned_snapshot_says_whether_it_is_a_delta_a_new_page_cut_or_unchanged(self):
        page = {"ok": True, "url": "https://shop.example/cart", "snapshot": '- heading "Cart" [ref=e40]'}
        cases = [
            ("tab.hover('e6')", "/api/hover",
             {"ok": True, "snapshot": "", "snapshot_delta": True, "no_visible_change": True},
             {"snapshot": "", "snapshot_delta": True, "no_visible_change": True}),
            ("tab.press('Enter')", "/api/keypress",
             {**DELTA, "snapshot_truncated": True, "snapshot_hint": "Snapshot truncated; scope it."},
             {"snapshot": DELTA["snapshot"], "snapshot_delta": True, "snapshot_truncated": True,
              "snapshot_hint": "Snapshot truncated; scope it."}),
            # A click that loaded another document: the new page, not a delta.
            ("tab.click('e6')", "/api/click",
             {**page, "navigated": {"url": "https://shop.example/cart", "title": "Cart"}},
             {"snapshot": page["snapshot"], "navigated": {"url": "https://shop.example/cart", "title": "Cart"}}),
            # A submit that loaded another page waits for it like a click (#586).
            ("tab.submit_form('#order')", "/api/submit-form",
             {**page, "submitted": "button", "navigated": {"url": "https://shop.example/cart", "title": "Cart"}},
             {"snapshot": page["snapshot"], "navigated": {"url": "https://shop.example/cart", "title": "Cart"}}),
            ("tab.navigate('https://shop.example/cart')", "/api/navigate", page, {"snapshot": page["snapshot"]}),
        ]
        for code, route, reply, expected in cases:
            with self.subTest(code=code):
                result = await script_runner.run_python(code, Client({route: reply}), tab_id=7)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["result"], {"last_action": route, **expected})

    async def test_printed_output_or_an_assigned_result_is_all_a_script_returns(self):
        for chosen, output, selected in [
                ("print('saved')", "saved\n", ABSENT), ("result = {'saved': True}", "", {"saved": True}),
                # Assigning a falsy value is still the script's choice.
                ("result = None", "", ABSENT), ("result = 0", "", 0), ("result = ''", "", ""),
                ("result = False", "", False), ("result = []", "", []),
                ("import sys; sys.stderr.write('warned')", "warned", ABSENT)]:
            with self.subTest(chosen=chosen):
                result = await script_runner.run_python(f"tab.click('e6')\n{chosen}", Client({"/api/click": DELTA}),
                                                        tab_id=7)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output"], output)
                self.assertEqual(result.get("result", ABSENT), selected)

    async def test_no_snapshot_is_returned_unless_the_last_operation_is_an_action_that_has_one(self):
        replies = {"/api/click": DELTA, "/api/snapshot": {"snapshot": '- heading "Orders" [ref=e1]'},
                   "/api/page-text": {"text": "Orders"}, "/api/find": {"results": [{"ref": "e6", "snapshot": "x"}]},
                   "/api/query": {"items": []}, "/api/wait-for-text": {"ok": True, "snapshot": "- text"},
                   "/api/tabs": {"tabs": []}, "/api/scroll": {"ok": True, "scrollY": 300}}
        for last in ("tab.snapshot()", "tab.text()", "tab.find('save button')", "tab.query('h1')",
                     "tab.wait_for(text='Saved')", "browser.tabs()", "tab.scroll('down', 300)", "pass"):
            with self.subTest(last=last):
                code = f"tab.click('e6')\n{last}" if last != "pass" else last
                result = await script_runner.run_python(code, Client(replies), tab_id=7)
                self.assertTrue(result["ok"], result)
                self.assertNotIn("result", result)
                self.assertEqual(result["output"], "")

    async def test_snapshot_taken_before_the_page_settled_is_not_returned(self):
        # It may show the page as it was before the action, which reads as "nothing happened" and
        # invites doing the action again.
        form = {"ok": True, "submitted": True, "target_ref": "e1",
                "snapshot": '- form "" [ref=e1]\n  - textbox "Name" [ref=e2] [value="Ada"]'}
        navigated = {"url": "https://shop.example/cart", "title": ""}
        for code, route, reply in [
                # A submit that loaded nothing: a full snapshot, which the reply does not mark as
                # settled (an extension older than #586 took it straight after dispatch).
                ("tab.submit_form('#order')", "/api/submit-form", form),
                # A full snapshot on request (KITSUNE_POST_ACTION_SNAPSHOT=full) has no settle wait either.
                ("tab.click('e6')", "/api/click", {**DELTA, "snapshot_delta": False}),
                ("tab.fill('e2', 'Ada')", "/api/fill", {key: DELTA[key] for key in ("ok", "snapshot")}),
                # The page the click started loading had not finished when the snapshot was taken.
                ("tab.click('e6')", "/api/click",
                 {"ok": True, "snapshot": form["snapshot"], "navigated": navigated, "navigation_pending": True})]:
            with self.subTest(code=code, reply=reply):
                client = Client({route: reply})
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertTrue(result["ok"], result)
                self.assertNotIn("result", result)
                self.assertEqual([entry["status"] for entry in result["operations"]], ["ok"])
                # The action's own return value is untouched for a script that asks for it.
                asked = await script_runner.run_python(f"result = {code}['snapshot']", client, tab_id=7)
                self.assertEqual(asked["result"], reply["snapshot"])

    async def test_failed_last_action_keeps_its_failure_and_returns_no_snapshot(self):
        failure = {"ok": False, "error": "Expected outcome was not observed", "code": "outcome_not_observed",
                   "retryable": False, "snapshot": DELTA["snapshot"], "snapshot_delta": True}
        result = await script_runner.run_python("tab.fill('e2', 'Ada')\ntab.click('e6')",
                                                Client({"/api/fill": DELTA, "/api/click": failure}), tab_id=7)
        self.assertEqual(result["code"], "script_operation_failed")
        self.assertEqual(result["failed_operation"], failure)
        self.assertNotIn("result", result)
        self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "failed"])
        # Nor from a failed reply itself, should one ever reach the worker.
        self.assertIsNone(script_worker._last_action_snapshot(("/api/click", failure)))
        self.assertIsNotNone(script_worker._last_action_snapshot(("/api/click", DELTA)))
        # A script that failed after its last action reports the error only.
        result = await script_runner.run_python("tab.click('e6')\n1 / 0", Client({"/api/click": DELTA}), tab_id=7)
        self.assertEqual(result["code"], "script_error")
        self.assertNotIn("result", result)

    async def test_returned_snapshot_shares_the_output_limit_with_a_selected_result(self):
        # Bridge snapshots are capped at 4,000 characters; the limit holds for any reply.
        # Over it, the snapshot is cut and marked like any result (#570), not failed.
        for size, fits in ((15000, True), (16000, False)):
            with self.subTest(size=size):
                client = Client({"/api/click": {**DELTA, "snapshot": "x" * size}})
                result = await script_runner.run_python("tab.fill('e2', 'Ada')\ntab.click('e6')", client, tab_id=7)
                self.assertIs(result["ok"], True, result)
                self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "ok"])
                if fits:
                    self.assertNotIn("truncated", result)
                    self.assertEqual(len(result["result"]["snapshot"]), size)
                else:
                    self.assertIs(result["truncated"], True)
                    self.assertGreater(result["dropped_bytes"], 0)
                    self.assertTrue(result["result"].startswith('{"last_action": "/api/click", "snapshot": "x'))
                    self.assertIn("do not replay", result["hint"])

    def test_snapshot_action_routes_are_the_extensions_post_snapshot_actions_that_settle_first(self):
        # The extension decides which actions return a snapshot; the worker lists their routes.
        source = (ROOT / "extension" / "background" / "background.js").read_text(encoding="utf-8")
        declared = re.search(r"const POST_SNAPSHOT_ACTIONS = new Map\(\[(.*?)\]\);", source, re.S).group(1)
        routes = {"click": "/api/click", "type": "/api/type", "fill": "/api/fill", "selectOption": "/api/select-option",
                  "checkBox": "/api/check", "keypress": "/api/keypress", "submitForm": "/api/submit-form",
                  "navigate": "/api/navigate", "hover": "/api/hover"}
        # Only an action with a delta snapshot, or submitForm (#586), gets the settle and navigation
        # watch (runCommand). navigate, the other full one, waits for its own load. An action added
        # to the full set needs the same check.
        self.assertIn("const settles = (options.deltaSnapshot || options.settleOnly) &&", source)
        self.assertIn("const settleOnly = withSnapshot && params.action === 'submitForm';", source)
        unwatched = re.search(r"const FULL_SNAPSHOT_ACTIONS = new Set\(\[(.*?)\]\);", source).group(1)
        self.assertEqual(set(re.findall(r"'(\w+)'", unwatched)), {"submitForm", "navigate"})
        self.assertEqual({routes[action] for action in re.findall(r"\['(\w+)',", declared)},
                         script_worker.SNAPSHOT_ACTION_ROUTES)

    async def test_operation_limit_stops_before_the_extra_call(self):
        client = Client({"/api/tabs": {"tabs": []}})
        with patch.object(script_runner, "OPERATION_LIMIT", 2):
            result = await script_runner.run_python("for _ in range(3): browser.tabs()", client)
        self.assertEqual(result["code"], "script_operation_limit")
        self.assertEqual(len(client.calls), 2)

    async def test_a_read_loop_over_the_limit_keeps_what_it_collected(self):
        # #618: wap-144 read 50 pages and printed them only at the end; the limit lost all of them.
        client = Client({"/api/page-text": {"text": "page"}})
        with patch.object(script_runner, "OPERATION_LIMIT", 3):
            caught = await script_runner.run_python(
                "rows = []\ntry:\n    for p in range(10):\n        rows.append(tab.text())\n"
                "except Exception:\n    print('swallowed')\nfinally:\n    print(len(rows), browser.calls_left)",
                client, tab_id=7)
            self.assertEqual(caught["code"], "script_operation_limit", caught)
            self.assertEqual(caught["output"], "3 0\n", "except Exception does not catch it; finally runs")
            self.assertEqual(len(client.calls), 3, "the call over the limit is not sent")
            self.assertIn("Nothing was written", caught["hint"])

            kept = await script_runner.run_python(
                "result = []\nfor p in range(10):\n    result.append(tab.text())", Client({"/api/page-text": {"text": "p"}}),
                tab_id=7)
            self.assertEqual((kept["code"], kept["result"]), ("script_operation_limit", ["p", "p", "p"]), kept)

            handled = await script_runner.run_python(
                "result = []\ntry:\n    while True: result.append(tab.text())\nexcept ScriptLimit as stop:\n"
                "    print(stop.code)", Client({"/api/page-text": {"text": "p"}}), tab_id=7)
            self.assertEqual((handled["ok"], handled["output"], handled["result"]),
                             (True, "script_operation_limit\n", ["p", "p", "p"]), handled)

            again = Client({"/api/page-text": {"text": "p"}})
            ignored = await script_runner.run_python(
                "for p in range(10):\n    try:\n        tab.text()\n    except BaseException:\n        pass", again, tab_id=7)
            self.assertEqual(ignored["code"], "script_operation_limit", "a call after ScriptLimit stops the script")
            self.assertEqual(len(again.calls), 3)

    async def test_a_dismissed_confirm_is_named_in_the_result_with_the_script_call(self):
        # #615: wap-096's click printed nothing about its dismissed confirm; the script read as a success.
        confirm = {"type": "confirm", "message": "Delete this job offer?", "response": False, "auto_dismissed": True}
        client = Client({"/api/click": {**DELTA, "dialogs": [confirm],
                                        "dialog_hint": "... call browser_handle_dialog(accept=true) ..."},
                         "/api/page-text": {"text": "Posted Jobs"},
                         # Reads and the policy call list the tab's recent dialogs again.
                         "/api/page-info": {"url": "/jobs", "dialogs": [confirm]},
                         "/api/handle-dialog": {"ok": True, "dialogs": [confirm]}})
        result = await script_runner.run_python("r = tab.click('e758')\nprint(r['dialog_hint'])\ntab.text()\n"
                                                "tab.info()\ntab.handle_dialogs(accept=True)", client, tab_id=7)
        self.assertIs(result["ok"], True)
        self.assertEqual(result["dialogs"], [{"operation": 0, **confirm}])
        self.assertIn("tab.handle_dialogs(accept=True)", result["dialog_hint"])
        self.assertIn("tab.handle_dialogs(accept=True)", result["output"], "the action's own reply is reworded")
        self.assertNotIn("browser_handle_dialog", json.dumps(result))
        self.assertEqual([entry.get("dialog") for entry in result["operations"]], ["dismissed", None, None, None])

    async def test_an_alert_or_an_accepted_confirm_has_no_hint(self):
        for dialog in ({"type": "alert", "message": "Saved", "response": None, "auto_dismissed": True},
                       {"type": "confirm", "message": "Delete?", "response": True}):
            with self.subTest(dialog=dialog["type"]):
                result = await script_runner.run_python(
                    "tab.click('#x')", Client({"/api/click": {"ok": True, "dialogs": [dialog]}}), tab_id=7)
                self.assertEqual(result["dialogs"], [{"operation": 0, **dialog}])
                self.assertNotIn("dialog_hint", result)
                self.assertEqual(result["operations"][0]["dialog"], "shown")

    async def test_a_long_operations_list_is_summarized(self):
        # #617: 100 navigate/page-text entries were about 7,000 characters of every such reply.
        loop = "for p in range(50):\n    tab.navigate(f'/p/{p}')\n    tab.text()\n"
        for code, error in ((loop, None), (loop + "tab.text()", "script_operation_limit")):
            with self.subTest(error=error):
                client = Client({"/api/page-text": {"text": "p"}})
                result = await script_runner.run_python(code, client, tab_id=7)
                self.assertEqual(result.get("code"), error, result)
                self.assertLess(len(json.dumps(result["operations"])), 500)
                self.assertEqual(result["operations"], {
                    "count": 100, "by_route": {"/api/navigate": 50, "/api/page-text": 50}, "tab_id": 7,
                    "last": [{"index": 97, "route": "/api/page-text", "status": "ok"},
                             {"index": 98, "route": "/api/navigate", "status": "ok"},
                             {"index": 99, "route": "/api/page-text", "status": "ok"}]})

    async def test_a_summary_keeps_failed_remapped_and_other_tab_entries(self):
        replies = iter([{"text": "p"}] * 4 + [{"error": "gone", "code": "not_found"}] + [{"text": "p"}] * 20)
        client = Client({"/api/page-text": lambda: next(replies), "/api/click": {"error": "x", "code": "not_found"}})
        result = await script_runner.run_python(
            "for p in range(12):\n    try: tab.text()\n    except KitsuneError: pass\n"
            "browser.tab(9).text()\ntab.click('#go')", client, tab_id=7)
        self.assertEqual(result["code"], "script_operation_failed")
        operations = result["operations"]
        self.assertEqual((operations["count"], operations["tab_id"]), (14, 7))
        self.assertEqual(operations["notable"], [
            {"index": 4, "route": "/api/page-text", "status": "failed"},
            {"index": 12, "route": "/api/page-text", "status": "ok", "tab_id": 9},
            {"index": 13, "route": "/api/click", "status": "failed"}])
        self.assertEqual(operations["last"], [{"index": 11, "route": "/api/page-text", "status": "ok"}],
                         "an entry is not repeated in last")

    async def test_the_budget_is_visible_and_counts_only_sent_calls(self):
        result = await script_runner.run_python(
            "before = (browser.calls_left, browser.seconds_left)\nbrowser.tabs()\n"
            "result = [before, browser.calls_left, browser.seconds_left]",
            Client({"/api/tabs": {"tabs": []}}), timeout=10)
        (calls, seconds), after, later = result["result"]
        self.assertEqual((calls, after), (script_runner.OPERATION_LIMIT, script_runner.OPERATION_LIMIT - 1))
        self.assertTrue(8 < later <= seconds <= 10, result)

    async def test_a_call_after_the_deadline_raises_and_the_script_can_still_print(self):
        client = Client({"/api/page-text": {"text": "p"}})
        result = await script_runner.run_python(
            "rows = []\ntry:\n    while True:\n        rows.append(tab.text()); time.sleep(0.2)\n"
            "finally:\n    print(len(rows) > 0, browser.seconds_left)", client, timeout=0.5, tab_id=7)
        self.assertEqual((result["code"], result["output"]), ("script_timeout", "True 0.0\n"), result)
        self.assertIn("Nothing was written", result["hint"])

    async def test_a_limit_after_a_write_keeps_the_replay_warning(self):
        with patch.object(script_runner, "OPERATION_LIMIT", 1):
            result = await script_runner.run_python("tab.click('#a')\ntab.click('#b')", Client(), tab_id=7)
        self.assertEqual(result["code"], "script_operation_limit")
        self.assertIn("do not replay the whole script", result["hint"])

    async def test_large_internal_response_is_not_echoed_to_the_model(self):
        client = Client({"/api/page-text": {"text": "x" * script_runner.FRAME_LIMIT}})
        result = await script_runner.run_python("browser.tab(7).text()", client)
        self.assertEqual(result["code"], "script_response_limit")
        self.assertEqual(result["operations"][0]["status"], "ok")
        self.assertLess(len(json.dumps(result)), 2000)

    async def test_large_failed_operation_is_explicitly_truncated(self):
        client = Client({"/api/click": {"error": "failed", "code": "unknown_outcome", "snapshot": "x" * 20000}})
        result = await script_runner.run_python("browser.tab(7).click('#send')", client)
        self.assertEqual(result["code"], "script_operation_failed")
        self.assertEqual(result["failed_operation"], {"error": "failed", "code": "unknown_outcome", "truncated": True})
        self.assertLess(len(json.dumps(result)), 2000)

    async def test_cancellation_reaps_the_worker(self):
        client = Client({"/api/tabs": {"tabs": []}})
        spawn = asyncio.create_subprocess_exec
        processes = []

        async def record_process(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(script_runner.asyncio, "create_subprocess_exec", side_effect=record_process):
            task = asyncio.create_task(script_runner.run_python("browser.tabs()\nwhile True: pass", client))
            try:
                self.assertTrue(await asyncio.to_thread(client.called.wait, 5))
            finally:
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(_exited(processes[0].pid))

    async def test_raw_stdout_is_a_protocol_error_not_a_fake_success(self):
        result = await script_runner.run_python("import os; os.write(1, b'not-json\\n')", Client())
        self.assertEqual(result["code"], "script_protocol_error")

    async def test_invalid_inputs_do_not_start_a_worker(self):
        with patch.object(script_runner.asyncio, "create_subprocess_exec") as spawn:
            for code, timeout in [("", 30), (" " * 4, 30), ("x" * 64001, 30), ("pass", 0),
                                  ("pass", float("nan")), ("pass", 121), ("pass", True)]:
                with self.subTest(timeout=timeout):
                    self.assertEqual((await script_runner.run_python(code, Client(), timeout=timeout))["code"],
                                     "invalid_argument")
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
