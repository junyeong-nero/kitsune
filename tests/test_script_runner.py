"""Real local Python workers with a mocked host transport; no user's bridge is contacted."""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kitsune import script_runner, script_worker

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

    async def test_output_and_selected_result_limits(self):
        for code in ("print('é' * 20000)", "import sys; sys.stderr.write('x' * 20000)",
                     "result = 'x' * 20000", "print('x' * 10000); result = 'y' * 10000"):
            with self.subTest(code=code):
                result = await script_runner.run_python(code, Client())
                self.assertEqual(result["code"], "script_output_limit")
                self.assertLessEqual(len(result["output"].encode()), script_runner.OUTPUT_LIMIT)
                self.assertNotIn("result", result)

    async def test_utf8_combined_output_boundary_is_not_a_character_budget(self):
        # 7,998 printed bytes + 8,002 bytes for the JSON string, including its quotes.
        for suffix, succeeds in (("xx", True), ("xxx", False)):
            code = f"print('é' * 3999, end=''); result = '한' * 2666 + {suffix!r}"
            result = await script_runner.run_python(code, Client())
            self.assertEqual(result["ok"], succeeds, result)
            self.assertEqual(result["output"], "é" * 3999)
            if succeeds:
                self.assertEqual(len(result["output"].encode()) +
                                 len(json.dumps(result["result"], ensure_ascii=False).encode()), 16000)
            else:
                self.assertEqual(result["code"], "script_output_limit")
                self.assertNotIn("result", result)
        result = await script_runner.run_python("print('한' * 6000, end='')", Client())
        self.assertEqual(result["code"], "script_output_limit")
        self.assertEqual(result["output"], "한" * 5333, "do not expose a partial UTF-8 character")

    async def test_observation_overflow_keeps_completed_writes_and_gives_scoped_recovery(self):
        for observation in ("result = tab.text()", "print(tab.text())\ntab.fill('#draft', 'must not run')"):
            with self.subTest(observation=observation):
                client = Client({"/api/page-text": {"text": "한" * 6000},
                                 "/api/query": {"items": [{"text": "Delivery: three days"}]}})
                stopped = await script_runner.run_python("tab.fill('#draft', 'keep')\n" + observation,
                                                         client, tab_id=7)
                self.assertEqual(stopped["code"], "script_output_limit")
                self.assertFalse(stopped["retryable"])
                self.assertNotIn("result", stopped)
                self.assertNotIn("resume_token", stopped)
                self.assertEqual([route for _, route, _ in client.calls], ["/api/fill", "/api/page-text"])
                self.assertEqual([op["status"] for op in stopped["operations"]], ["ok", "ok"])
                for guidance in ("incomplete", "tab.snapshot", "tab.query", "coverage", "do not replay"):
                    self.assertIn(guidance, stopped["hint"])
                recovered = await script_runner.run_python(
                    "result = tab.query('#delivery', fields=['text'], limit=1)", client, tab_id=7)
                self.assertEqual(recovered["result"], [{"text": "Delivery: three days"}])
                self.assertEqual([route for _, route, _ in client.calls],
                                 ["/api/fill", "/api/page-text", "/api/query"], "only observe; never repeat the draft")

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
            ("tab.navigate('https://shop.example/cart')", "/api/navigate", page, {"snapshot": page["snapshot"]}),
            ("tab.submit_form('#order')", "/api/submit-form", page, {"snapshot": page["snapshot"]}),
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
        for size, succeeds in ((15000, True), (16000, False)):
            with self.subTest(size=size):
                client = Client({"/api/click": {**DELTA, "snapshot": "x" * size}})
                result = await script_runner.run_python("tab.fill('e2', 'Ada')\ntab.click('e6')", client, tab_id=7)
                self.assertEqual(result["ok"], succeeds, result)
                self.assertEqual([entry["status"] for entry in result["operations"]], ["ok", "ok"])
                if succeeds:
                    self.assertEqual(len(result["result"]["snapshot"]), size)
                else:
                    self.assertEqual(result["code"], "script_output_limit")
                    self.assertNotIn("result", result)
                    self.assertIn("do not replay", result["hint"])

    def test_snapshot_action_routes_are_the_extensions_post_snapshot_actions(self):
        # The extension decides which actions return a snapshot; the worker lists their routes.
        source = (ROOT / "extension" / "background" / "background.js").read_text(encoding="utf-8")
        declared = re.search(r"const POST_SNAPSHOT_ACTIONS = new Map\(\[(.*?)\]\);", source, re.S).group(1)
        routes = {"click": "/api/click", "type": "/api/type", "fill": "/api/fill", "selectOption": "/api/select-option",
                  "checkBox": "/api/check", "keypress": "/api/keypress", "submitForm": "/api/submit-form",
                  "navigate": "/api/navigate", "hover": "/api/hover"}
        self.assertEqual({routes[action] for action in re.findall(r"\['(\w+)',", declared)},
                         script_worker.SNAPSHOT_ACTION_ROUTES)

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
