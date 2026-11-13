"""Real local Python workers with a mocked host transport; no user's bridge is contacted."""
import asyncio
import json
import os
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
