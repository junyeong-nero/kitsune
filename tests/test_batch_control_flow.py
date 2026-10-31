"""Control-flow dispatch preserves result shapes, step order and audit frames."""
import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from kitsune import bridge


class BatchControlFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parent = {"call": 7, "path": []}
        self.step = AsyncMock(return_value={"ok": True})
        self.send = AsyncMock(return_value={"result": True})
        self.sleep = AsyncMock()
        for patcher in (
            patch.object(bridge, "_audit_call", return_value=self.parent),
            patch.object(bridge, "_run_step", self.step),
            patch.object(bridge, "send_to_extension", self.send),
            patch.object(bridge.asyncio, "sleep", self.sleep),
            patch.object(bridge, "_running_parallels", {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def execute(self, action, **params):
        return await bridge._execute_command(action, action, params)

    async def test_if_selects_one_branch_and_keeps_step_frames(self):
        then = [{"action": "pageInfo"}, {"action": "pageText"}]
        otherwise = [{"action": "tabs"}]
        for value in (True, "ready", False, 0, None, "false", "0", "null", "undefined", ""):
            branch = "then" if value in (True, "ready") else "else"
            commands = then if branch == "then" else otherwise
            with self.subTest(value=value):
                self.step.reset_mock()
                self.send.return_value = {"result": value}
                result = await self.execute("if", condition="ready", tabId=3, **{"then": then, "else": otherwise})
                self.assertEqual(result, {"branch": branch, "condition": value,
                                          "results": [{"ok": True}] * len(commands)})
                self.assertEqual(self.step.await_args_list, [
                    call(cmd, self.parent, action="if", branch=branch, index=i)
                    for i, cmd in enumerate(commands)
                ])
                self.send.assert_awaited_with("executeJS", {"tabId": 3, "code": "ready"}, timeout=10)

    async def test_if_condition_failure_runs_neither_branch(self):
        error = {"error": "cannot evaluate", "code": "tab_closed"}
        self.send.return_value = error
        self.assertEqual(await self.execute("if", **{"then": [{"action": "tabs"}]}), error)
        self.step.assert_not_awaited()

    async def test_empty_condition_defaults_differ_for_if_and_while(self):
        for action, default in (("if", "1"), ("while", "0")):
            with self.subTest(action=action):
                self.send.reset_mock()
                self.send.return_value = {"result": False}
                await self.execute(action, condition="")
                self.send.assert_awaited_once_with("executeJS", {"tabId": None, "code": default}, timeout=10)

    async def test_while_keeps_iteration_results_and_frames(self):
        commands = [{"action": "pageInfo"}, {"action": "pageText"}]
        self.send.side_effect = [{"result": True}, {"result": "ready"}, {"result": "false"}]
        result = await self.execute("while", condition="ready", tabId=4, do=commands)
        self.assertEqual(result, {"iterations": 2, "results": [[{"ok": True}] * 2] * 2,
                                  "stoppedBy": "condition"})
        self.assertEqual(self.step.await_args_list, [
            call(cmd, self.parent, action="while", iteration=iteration, index=i)
            for iteration in range(2) for i, cmd in enumerate(commands)
        ])

    async def test_while_condition_error_keeps_completed_iterations(self):
        error = {"error": "document replaced", "code": "stale_document"}
        self.send.side_effect = [{"result": True}, error]
        result = await self.execute("while", do=[{"action": "tabs"}])
        self.assertEqual(result, {**error, "iterations": 1, "results": [[{"ok": True}]],
                                  "stoppedBy": "conditionError"})
        self.step.assert_awaited_once()

    async def test_while_iteration_limit(self):
        for limit in (0, 2):
            with self.subTest(limit=limit):
                self.send.reset_mock()
                result = await self.execute("while", maxIterations=str(limit))
                self.assertEqual(result, {"iterations": limit, "results": [[]] * limit,
                                          "stoppedBy": "maxIterations"})
                self.assertEqual(self.send.await_count, limit)

    async def test_retry_recovers_with_exponential_backoff_and_attempt_frames(self):
        command = {"action": "find", "query": "ready"}
        self.step.side_effect = [{"error": "not ready"}, {"error": "not ready"}, {"ok": True}]
        result = await self.execute("retry", command=command, maxAttempts=3, backoffMs=250, onErrors=["ready"])
        self.assertEqual(result, {"ok": True, "attempts": 3, "result": {"ok": True}})
        self.assertEqual(self.sleep.await_args_list, [call(0.25), call(0.5)])
        self.assertEqual(self.step.await_args_list, [
            call(command, self.parent, action="retry", attempt=i) for i in (1, 2, 3)
        ])

    async def test_retry_refusals_do_not_sleep_or_repeat(self):
        for reply, options, reason in (
            ({"error": "confirm", "code": "needs_user", "retryable": False,
              "state": {"reason": "approval", "detail": "confirmation_required"}}, {},
             "confirmation required"),
            ({"error": "unknown effect", "retryable": False}, {}, "not retryable"),
            ({"error": "closed"}, {"onErrors": ["not ready"]}, "error not in onErrors"),
        ):
            with self.subTest(reason=reason):
                self.step.reset_mock()
                self.step.return_value = reply
                self.assertEqual(await self.execute("retry", **options), {
                    "ok": False, "attempts": 1, "result": reply, "reason": reason,
                })
                self.step.assert_awaited_once()
                self.sleep.assert_not_awaited()

    async def test_retry_exhaustion_keeps_the_final_backoff(self):
        self.step.return_value = {"error": "not ready"}
        for attempts, expected in ((0, 1), (3, 3)):
            with self.subTest(attempts=attempts):
                self.step.reset_mock()
                self.sleep.reset_mock()
                result = await self.execute("retry", maxAttempts=attempts, backoffMs=100)
                self.assertEqual(result, {"ok": False, "attempts": expected, "result": {"error": "not ready"}})
                self.assertEqual(self.step.await_count, expected)
                self.assertEqual(self.sleep.await_args_list, [call(0.1 * 2 ** i) for i in range(expected)])

    async def test_try_success_never_runs_catch(self):
        commands = [{"action": "tabs"}, {"action": "pageInfo"}]
        result = await self.execute("try", do=commands, catch=[{"action": "sleep"}])
        self.assertEqual(result, {"ok": True, "results": [{"ok": True}] * 2})
        self.assertEqual(self.step.await_args_list, [
            call(cmd, self.parent, action="try", block="do", index=i) for i, cmd in enumerate(commands)
        ])

    async def test_try_catches_result_errors_and_exceptions_without_running_later_steps(self):
        commands = [{"action": "pageInfo"}, {"action": "click"}, {"action": "tabs"}]
        recovery = [{"action": "pageText"}, {"action": "find"}]
        for failure in ({"error": "failed"}, RuntimeError("failed")):
            with self.subTest(failure=failure):
                self.step.reset_mock()
                self.step.side_effect = [{"ok": True}, failure, {"error": "recovery failed"}, {"ok": True}]
                result = await self.execute("try", do=commands, catch=recovery)
                self.assertEqual(result, {"ok": False, "error": "failed",
                                          "catch_results": [{"error": "recovery failed"}, {"ok": True}]})
                self.assertEqual(self.step.await_args_list, [
                    call(cmd, self.parent, action="try", block=block, index=i)
                    for block, group in (("do", commands[:2]), ("catch", recovery))
                    for i, cmd in enumerate(group)
                ])

    async def test_parallel_stops_only_the_held_sequence_and_releases_claims(self):
        held = {"error": "confirm", "code": "needs_user",
                "state": {"reason": "approval", "detail": "confirmation_required"}}
        first = {"action": "click", "tabId": 1}
        second = {"action": "click", "tabId": 2}

        async def step(cmd, parent, **frame):
            self.assertEqual(len(bridge._running_parallels), 1)
            return held if cmd == first else {"ok": True}

        self.step.side_effect = step
        result = await self.execute("parallel", sequences=[[first, {"action": "tabs"}], [second]])
        self.assertEqual(result, [[held], [{"ok": True}]])
        self.assertEqual(self.step.await_args_list, [
            call(first, self.parent, action="parallel", sequence=0, index=0),
            call(second, self.parent, action="parallel", sequence=1, index=0),
        ])
        self.assertEqual(bridge._running_parallels, {})

    async def test_parallel_cancellation_propagates_and_releases_claims(self):
        self.step.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.execute("parallel", sequences=[[{"action": "tabs"}]])
        self.assertEqual(bridge._running_parallels, {})

    async def test_sequence_substitutes_results_then_stops_at_a_held_step(self):
        held = {"error": "confirm", "code": "needs_user",
                "state": {"reason": "approval", "detail": "confirmation_required"}}
        self.step.side_effect = [{"selector": "#go"}, held]
        commands = [{"action": "pageInfo"}, {"action": "click", "selector": "${$0.selector}"},
                    {"action": "tabs"}]
        result = await self.execute("sequence", do=commands)
        self.assertEqual(result, {"results": [{"selector": "#go"}, held]})
        self.assertEqual(self.step.await_args_list, [
            call(commands[0], self.parent, action="sequence", index=0),
            call({"action": "click", "selector": "#go"}, self.parent, action="sequence", index=1),
        ])
        self.assertEqual(commands[1]["selector"], "${$0.selector}")

    async def test_dispatch_converts_exceptions_but_not_cancellation(self):
        for action, params in (("retry", {}), ("if", {"then": [{}]}),
                               ("while", {"do": [{}]}), ("sequence", {"do": [{}]}),
                               ("parallel", {"sequences": [[{"action": "tabs"}]]}),
                               ("try", {"do": [{}], "catch": [{}]})):
            with self.subTest(action=action):
                self.step.side_effect = RuntimeError("failed")
                self.assertEqual(await self.execute(action, **params), {"error": "failed"})
                self.step.side_effect = asyncio.CancelledError
                with self.assertRaises(asyncio.CancelledError):
                    await self.execute(action, **params)
                self.assertEqual(bridge._running_parallels, {})

    async def test_unknown_actions_are_rejected(self):
        self.assertEqual(await self.execute("notAnAction"), {"error": "Unknown batch action: notAnAction"})
        self.step.assert_not_awaited()
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
