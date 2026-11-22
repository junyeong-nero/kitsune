"""Real local Python workers with a mocked host transport; no user's bridge is contacted."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from kitsune import script_runner


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

    async def test_script_cannot_supply_an_approval_token_through_the_provided_api(self):
        client = Client()
        result = await script_runner.run_python("browser.tab(7).click('#send', confirm='pretend-approval')", client)
        self.assertEqual(result["code"], "script_approval_required")
        self.assertEqual(client.calls, [])

    async def test_timeout_reaps_the_worker(self):
        result = await script_runner.run_python("import os\nprint(os.getpid())\nwhile True: pass", Client(), timeout=0.5)
        self.assertEqual(result["code"], "script_timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(result["output"].strip()), 0)

    async def test_timeout_preserves_unknown_inflight_operation_and_does_not_replay(self):
        def slow_reply():
            time.sleep(0.8)
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

    async def test_operation_limit_stops_before_the_extra_call(self):
        client = Client({"/api/tabs": {"tabs": []}})
        with patch.object(script_runner, "OPERATION_LIMIT", 2):
            result = await script_runner.run_python("for _ in range(3): browser.tabs()", client)
        self.assertEqual(result["code"], "script_operation_limit")
        self.assertEqual(len(client.calls), 2)

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
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)

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
