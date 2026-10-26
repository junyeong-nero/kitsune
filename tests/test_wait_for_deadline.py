"""browser_wait_for's text and selector waits end at their timeout in a throttled tab (#209),
and with slow or queued reads (#292)."""
import asyncio
import json
import logging
import time
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect

from kitsune import bridge


class ThrottledPage:
    """A background tab whose timers fire late: an in-page wait overshoots its timeout.

    Each single read (timeout 0) returns the next scripted reply; the last one repeats.
    """

    LATE_SECONDS = 0.5

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, action, params=None, timeout=30):
        self.calls.append((action, dict(params or {})))
        if (params or {}).get("timeout", 10000) > 0:
            await asyncio.sleep(params.get("timeout", 10000) / 1000 + self.LATE_SECONDS)
        return dict(self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


UNMET_TEXT = {"ok": False, "text": "Saved", "elapsed": 0,
              "error": "Timed out after 0ms waiting for visible text"}
UNMET_ELEMENT = {"ok": False, "found": False, "selector": "#menu", "elapsed": 0,
                 "error": 'Timed out after 0ms waiting for "#menu"'}
TIMEOUT_MS = 300


# Test-only guard: a wait that overruns this fails its test instead of hanging the suite.
WATCHDOG_S = 10


async def watchdog(awaitable):
    try:
        return await asyncio.wait_for(awaitable, WATCHDOG_S)
    except asyncio.TimeoutError:
        raise AssertionError(f"still running after the {WATCHDOG_S}s test watchdog") from None


def run_wait(page, action, params):
    start = time.monotonic()
    with patch.object(bridge, "send_to_extension", page):
        result = asyncio.run(watchdog(bridge._poll_page_wait(action, params)))
    return result, (time.monotonic() - start) * 1000


class PageWaitDeadlineTests(unittest.TestCase):
    def assert_timed_out_at_deadline(self, result, wall_ms):
        self.assertIs(result["ok"], False)
        self.assertGreaterEqual(result["elapsed"], TIMEOUT_MS)
        self.assertLess(result["elapsed"], TIMEOUT_MS + 200)
        self.assertLess(wall_ms, TIMEOUT_MS + 200)

    def test_unmet_text_wait_ends_at_its_timeout_with_the_requested_timeout(self):
        page = ThrottledPage(UNMET_TEXT)
        result, wall_ms = run_wait(page, "waitForText", {"tabId": 7, "text": "Saved", "timeout": TIMEOUT_MS})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertEqual(result["error"], f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
        self.assertGreater(len(page.calls), 1)
        self.assertEqual(page.calls[0], ("waitForText", {"tabId": 7, "text": "Saved", "timeout": 0}))

    def test_unmet_selector_wait_ends_at_its_timeout(self):
        page = ThrottledPage(UNMET_ELEMENT)
        result, wall_ms = run_wait(page, "waitForElement", {"tabId": 7, "selector": "#menu",
                                                            "timeout": TIMEOUT_MS, "pollInterval": 20})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertIs(result["found"], False)
        self.assertEqual(result["error"], f'Timed out after {TIMEOUT_MS}ms waiting for "#menu"')
        self.assertGreater(len(page.calls), 2)
        self.assertEqual(page.calls[0][1]["timeout"], 0)

    def test_condition_met_on_a_later_read_ends_the_wait(self):
        found = {"ok": True, "found": True, "selector": "#menu", "elapsed": 0, "ref": "e3", "tag": "nav"}
        page = ThrottledPage(UNMET_ELEMENT, UNMET_ELEMENT, found)
        result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 5000, "pollInterval": 10})
        self.assertEqual(len(page.calls), 3)
        self.assertEqual({k: result[k] for k in ("ok", "found", "ref", "tag")},
                         {"ok": True, "found": True, "ref": "e3", "tag": "nav"})
        self.assertLess(result["elapsed"], 1000)

    def test_page_errors_are_returned_without_polling(self):
        for reply in ({"error": "Invalid selector"},
                      {"error": "Element ref is stale", "code": "stale_ref"},
                      {"error": "Content script unavailable", "code": "content_unavailable"}):
            page = ThrottledPage(reply)
            result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 5000})
            self.assertEqual(result, reply)
            self.assertEqual(len(page.calls), 1)

    def test_a_zero_timeout_is_one_read(self):
        page = ThrottledPage(UNMET_TEXT)
        result, _ = run_wait(page, "waitForText", {"text": "Saved", "timeout": 0})
        self.assertEqual(page.calls, [("waitForText", {"text": "Saved", "timeout": 0, "diagnose": True})])
        self.assertEqual(result["error"], "Timed out after 0ms waiting for visible text")

    def test_only_an_unmet_text_waits_last_read_asks_for_names(self):
        # #275: the text is an element's name, which the timed-out result reports.
        named = {**UNMET_TEXT, "matched_as": "accessible_name", "hint": "Found as the name of textbox",
                 "name_matches": [{"ref": "e0", "role": "textbox", "name": "Saved", "source": "placeholder"}]}
        page = ThrottledPage(UNMET_TEXT, UNMET_TEXT, named)
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": TIMEOUT_MS})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertEqual(result["error"], f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
        self.assertEqual(result["name_matches"], named["name_matches"])
        self.assertEqual(result["matched_as"], "accessible_name")
        asked = [params.get("diagnose") for _, params in page.calls]
        self.assertIs(asked[-1], True)
        self.assertNotIn(True, asked[:-2])

        met = ThrottledPage({"ok": True, "text": "Saved", "elapsed": 0})
        run_wait(met, "waitForText", {"text": "Saved", "timeout": 5000})
        self.assertEqual(met.calls, [("waitForText", {"text": "Saved", "timeout": 0})])

        gone = ThrottledPage(UNMET_TEXT)
        run_wait(gone, "waitForText", {"text_gone": "Saved", "timeout": 0})
        self.assertNotIn("diagnose", gone.calls[0][1])

    def test_batch_wait_for_element_polls_single_reads(self):
        page = ThrottledPage(UNMET_ELEMENT)
        start = time.monotonic()
        with patch.object(bridge, "send_to_extension", page):
            result = asyncio.run(watchdog(bridge._execute_command(
                "waitForElement", "waitForElement",
                {"selector": "#menu", "timeout": TIMEOUT_MS, "pollInterval": 20})))
        self.assert_timed_out_at_deadline(result, (time.monotonic() - start) * 1000)
        self.assertTrue(all(params["timeout"] == 0 for _, params in page.calls))


# A read this slow never answers within a test's timeout: whether it is cut off does not
# depend on scheduling jitter.
NEVER_S = 30


class SlowPage:
    """A page whose single reads answer after a scripted latency (#292).

    Each script entry is (latency_s, reply) for the next read; the last one repeats.
    diagnose_reply, when given, answers reads that ask for names instead.
    """

    def __init__(self, *script, diagnose_reply=None):
        self.script = list(script)
        self.diagnose_reply = diagnose_reply
        self.calls = []
        self.budgets = []
        self.starts = []
        self.answered = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.calls.append((action, params))
        self.budgets.append(timeout)
        self.starts.append(time.monotonic())
        if params.get("diagnose") and self.diagnose_reply is not None:
            latency, reply = self.diagnose_reply
        else:
            latency, reply = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        await asyncio.sleep(latency)
        self.answered.append(len(self.calls) - 1)
        return dict(reply)


class ElementShownAt:
    """A page whose element becomes visible shown_s after its first read."""

    def __init__(self, shown_s):
        self.shown_s = shown_s
        self.first = None
        self.calls = 0

    async def __call__(self, action, params=None, timeout=30):
        self.calls += 1
        now = time.monotonic()
        self.first = self.first or now
        return dict(ELEMENT_FOUND if now - self.first >= self.shown_s else UNMET_ELEMENT)


TEXT_FOUND = {"ok": True, "text": "Saved", "elapsed": 0}
ELEMENT_FOUND = {"ok": True, "found": True, "selector": "#menu", "elapsed": 0, "ref": "e3"}
NAMED_TEXT = {**UNMET_TEXT, "matched_as": "accessible_name", "hint": "Found as the name of textbox",
              "name_matches": [{"ref": "e0", "role": "textbox", "name": "Saved", "source": "placeholder"}]}
INCOMPLETE = "; the read in progress at the timeout did not finish, so this is the last completed read"


class SlowReadDeadlineTests(unittest.TestCase):
    def assert_ends_at(self, result, wall_ms, timeout_ms=TIMEOUT_MS):
        self.assertGreaterEqual(result["elapsed"], timeout_ms)
        self.assertLess(result["elapsed"], timeout_ms + 150)
        self.assertLess(wall_ms, timeout_ms + 150)

    def assert_last_completed_read(self, result, wall_ms, error, timeout_ms=TIMEOUT_MS):
        self.assertIs(result["ok"], False)
        self.assertIs(result["observationIncomplete"], True)
        self.assertEqual(result["error"], error + INCOMPLETE)
        self.assertNotIn("code", result)
        self.assert_ends_at(result, wall_ms, timeout_ms)

    def assert_no_read_answered(self, result, wall_ms, timeout_ms=TIMEOUT_MS):
        self.assertNotIn("ok", result)
        self.assertEqual(result["code"], "timeout")
        self.assertIs(result["retryable"], True)
        self.assertEqual(result["error"], f"The page did not answer a read within {timeout_ms}ms, so "
                                          "whether the wait's condition holds is unknown")
        self.assertEqual(result["state"], {"timeoutSeconds": timeout_ms / 1000})
        self.assert_ends_at(result, wall_ms, timeout_ms)

    def test_the_issue_reads_of_500ms_end_at_a_1050ms_timeout(self):
        # #292's probe: the second read crossed the deadline and a diagnosis followed it.
        page = SlowPage((0.5, UNMET_TEXT))
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": 1050})
        self.assert_last_completed_read(result, wall_ms, "Timed out after 1050ms waiting for visible text", 1050)
        self.assertEqual(len(page.calls), 2)
        self.assertEqual(page.answered, [0])
        self.assertLessEqual(max(page.budgets), 1.05)
        self.assertAlmostEqual(page.budgets[1], 0.45, delta=0.05)

    def test_the_last_read_starts_its_lead_before_the_deadline(self):
        # A read started with a sliver of the timeout left would almost never answer in time.
        page = SlowPage((0.004, UNMET_TEXT))
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": 1050})
        self.assertIs(result["ok"], False)
        self.assertNotIn("observationIncomplete", result)
        self.assert_ends_at(result, wall_ms, 1050)
        self.assertEqual(page.answered, list(range(len(page.calls))))
        self.assertGreaterEqual(min(page.budgets), 0.09)
        self.assertAlmostEqual(page.budgets[-1], 0.1, delta=0.01)
        self.assertEqual([params.get("diagnose") for _, params in page.calls].count(True), 1)
        self.assertIs(page.calls[-1][1].get("diagnose"), True)

    def test_a_poll_interval_longer_than_the_timeout_still_reads_before_the_deadline(self):
        page = SlowPage((0, UNMET_ELEMENT))
        result, wall_ms = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": TIMEOUT_MS,
                                                            "pollInterval": 5000})
        self.assertIs(result["ok"], False)
        self.assertNotIn("observationIncomplete", result)
        self.assert_ends_at(result, wall_ms)
        self.assertEqual(len(page.calls), 2)
        lead = bridge.PAGE_WAIT_LAST_READ_SECONDS
        self.assertAlmostEqual(page.starts[1] - page.starts[0], TIMEOUT_MS / 1000 - lead, delta=0.03)
        self.assertAlmostEqual(page.budgets[1], lead, delta=0.01)

    def test_an_element_shown_between_slow_polls_is_found_by_the_last_read(self):
        # Reads at 0 s and 1 s miss it; the last read, 0.1 s before the deadline, sees it.
        page = ElementShownAt(1.5)
        result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 2000,
                                                      "pollInterval": 1000})
        self.assertIs(result["ok"], True)
        self.assertEqual(page.calls, 3)
        self.assertGreaterEqual(result["elapsed"], 1850)
        self.assertLess(result["elapsed"], 2000)

    def test_a_short_poll_interval_does_not_shorten_the_last_read(self):
        page = SlowPage((0, UNMET_ELEMENT))
        result, wall_ms = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": TIMEOUT_MS,
                                                            "pollInterval": 10})
        self.assertIs(result["ok"], False)
        self.assertNotIn("observationIncomplete", result)
        self.assert_ends_at(result, wall_ms)
        self.assertGreater(len(page.calls), 10)
        self.assertGreaterEqual(page.budgets[-1], bridge.PAGE_WAIT_LAST_READ_SECONDS - 0.005)

    def test_a_late_success_or_error_is_not_an_observation(self):
        for late in (TEXT_FOUND, {"error": "Content script unavailable", "code": "content_unavailable"}):
            with self.subTest(late=late):
                page = SlowPage((0, UNMET_TEXT), (NEVER_S, late))
                result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": TIMEOUT_MS})
                self.assert_last_completed_read(
                    result, wall_ms, f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
                self.assertEqual(page.answered, [0])
                self.assertEqual(len(page.calls), 2)

    def test_a_slow_selector_read_keeps_the_last_completed_read(self):
        page = SlowPage((0, UNMET_ELEMENT), (NEVER_S, ELEMENT_FOUND))
        result, wall_ms = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": TIMEOUT_MS})
        self.assert_last_completed_read(result, wall_ms, f'Timed out after {TIMEOUT_MS}ms waiting for "#menu"')
        self.assertIs(result["found"], False)
        self.assertNotIn("ref", result)

    def test_a_slow_final_name_diagnosis_does_not_extend_the_wait(self):
        page = SlowPage((0, UNMET_TEXT), diagnose_reply=(NEVER_S, NAMED_TEXT))
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": TIMEOUT_MS})
        self.assert_last_completed_read(result, wall_ms, f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
        self.assertNotIn("name_matches", result)
        asked = [params.get("diagnose") for _, params in page.calls]
        self.assertEqual(asked.count(True), 1)
        self.assertIs(asked[-1], True)
        self.assertEqual(page.answered, list(range(len(page.calls) - 1)))

    def test_a_name_diagnosis_that_answers_in_time_is_reported(self):
        page = SlowPage((0, UNMET_TEXT), diagnose_reply=(0.03, NAMED_TEXT))
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": TIMEOUT_MS})
        self.assertIs(result["ok"], False)
        self.assertNotIn("observationIncomplete", result)
        self.assertEqual(result["name_matches"], NAMED_TEXT["name_matches"])
        self.assert_ends_at(result, wall_ms)

    def test_a_wait_whose_reads_never_answer_reports_the_condition_unknown(self):
        for action, params, reply in (("waitForText", {"text": "Saved"}, UNMET_TEXT),
                                      ("waitForElement", {"selector": "#menu"}, UNMET_ELEMENT)):
            with self.subTest(action=action):
                page = SlowPage((NEVER_S, reply))
                result, wall_ms = run_wait(page, action, {**params, "timeout": TIMEOUT_MS})
                self.assert_no_read_answered(result, wall_ms)
                self.assertEqual(len(page.calls), 1)

    def test_an_error_within_the_budget_is_not_replaced_by_an_earlier_read(self):
        error = {"error": "Element ref is stale", "code": "stale_ref"}
        page = SlowPage((0, UNMET_ELEMENT), (0.02, error))
        result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 5000})
        self.assertEqual(result, error)
        self.assertEqual(len(page.calls), 2)

    def test_a_zero_timeout_read_has_its_own_allowance(self):
        page = SlowPage((0.2, NAMED_TEXT))
        result, _ = run_wait(page, "waitForText", {"text": "Saved", "timeout": 0})
        self.assertEqual(result["name_matches"], NAMED_TEXT["name_matches"])
        self.assertEqual(result["error"], "Timed out after 0ms waiting for visible text")
        self.assertEqual(page.calls, [("waitForText", {"text": "Saved", "timeout": 0, "diagnose": True})])
        self.assertEqual(page.budgets, [bridge.PAGE_WAIT_SINGLE_READ_SECONDS])

        page = SlowPage((NEVER_S, NAMED_TEXT))
        with patch.object(bridge, "PAGE_WAIT_SINGLE_READ_SECONDS", 0.2):
            result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": 0})
        self.assert_no_read_answered(result, wall_ms, timeout_ms=200)
        self.assertEqual(len(page.calls), 1)  # no second name-diagnosis request


class _SchedulerState(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name, value in (("_global_command_lock", None), ("_tab_command_locks", {}),
                            ("_scheduler_gate", None), ("_ws_send_lock", None),
                            ("extension_ws", None), ("extension_ws_generation", 0),
                            ("extension_info", {})):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for state in (bridge._scheduler_active, bridge._scheduler_queued, bridge.pending_commands):
            patcher = patch.dict(state, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)


class QueuedReadDeadlineTests(_SchedulerState):
    """The deadline covers a read's wait in the tab's scheduler queue (#292)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent = []

        async def raw(action, params=None, timeout=30):
            self.sent.append((action, dict(params or {})))
            return dict(UNMET_ELEMENT)

        patcher = patch.object(bridge, "_send_to_extension_raw", raw)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def hold_tab_lock(self):
        """Hold tab 7's scheduler lock, as another command on the tab would, until released."""
        lock = bridge._get_scope_lock(("tab", 7))
        await lock.acquire()
        self.addCleanup(lambda: lock.release() if lock.locked() else None)
        return lock

    async def test_a_read_queued_past_the_deadline_is_never_dispatched(self):
        lock = await self.hold_tab_lock()
        start = time.monotonic()
        result = await watchdog(bridge._poll_page_wait(
            "waitForElement", {"tabId": 7, "selector": "#menu", "timeout": TIMEOUT_MS}))
        wall_ms = (time.monotonic() - start) * 1000
        self.assertEqual(result["code"], "timeout")
        self.assertNotIn("ok", result)
        self.assertGreaterEqual(result["elapsed"], TIMEOUT_MS)
        self.assertLess(wall_ms, TIMEOUT_MS + 150)
        self.assertEqual(bridge._scheduler_queued, {})
        self.assertEqual(bridge._scheduler_active, {})
        lock.release()
        await asyncio.sleep(0.05)
        self.assertEqual(self.sent, [])  # nothing detached sends the cancelled read later
        self.assertFalse(lock.locked())

    async def test_a_read_queued_briefly_still_observes_before_the_deadline(self):
        lock = await self.hold_tab_lock()
        asyncio.get_running_loop().call_later(0.1, lambda: lock.release() if lock.locked() else None)
        result = await watchdog(bridge._poll_page_wait(
            "waitForElement", {"tabId": 7, "selector": "#menu", "timeout": TIMEOUT_MS}))
        self.assertIs(result["ok"], False)
        self.assertNotIn("observationIncomplete", result)
        self.assertEqual(result["error"], f'Timed out after {TIMEOUT_MS}ms waiting for "#menu"')
        self.assertGreaterEqual(len(self.sent), 1)
        self.assertLess(result["elapsed"], TIMEOUT_MS + 150)


class LateReplyRoutingTests(_SchedulerState):
    """A read cut off at the deadline leaves no pending command; its late reply resolves nothing."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        log = patch.object(bridge, "_log")
        self.log = log.start()
        self.addCleanup(log.stop)
        library_logs = patch.object(logging.getLogger("websockets.server"), "disabled", True)
        library_logs.start()
        self.addCleanup(library_logs.stop)
        self.server = await bridge.serve_extension_ws(0)
        self.addAsyncCleanup(self.close_server)
        self.uri = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_a_late_reply_is_not_taken_as_a_later_reads_answer(self):
        await watchdog(self.late_reply_is_not_taken_as_a_later_reads_answer())

    async def late_reply_is_not_taken_as_a_later_reads_answer(self):
        async with connect(self.uri, origin="moz-extension://0b5e3f9c-test", open_timeout=5) as client:
            for _ in range(200):
                if bridge.extension_ws is not None:
                    break
                await asyncio.sleep(0.01)
            body = {"tabId": 7, "text": "Saved", "timeout": TIMEOUT_MS}

            first = asyncio.create_task(bridge._poll_page_wait("waitForText", body))
            stale = json.loads(await client.recv())  # never answered within the wait
            result = await first
            self.assertEqual(result["code"], "timeout")
            self.assertEqual(bridge.pending_commands, {})

            second = asyncio.create_task(bridge._poll_page_wait("waitForText", body))
            current = json.loads(await client.recv())
            self.assertNotEqual(current["id"], stale["id"])
            await client.send(json.dumps({"id": stale["id"], "result": TEXT_FOUND}))
            await asyncio.sleep(0.02)
            await client.send(json.dumps({"id": current["id"], "result": UNMET_TEXT}))
            while not second.done():  # answer the remaining reads of the second wait
                try:
                    command = json.loads(await asyncio.wait_for(client.recv(), 0.05))
                except asyncio.TimeoutError:
                    continue
                await client.send(json.dumps({"id": command["id"], "result": UNMET_TEXT}))
            result = await second
            self.assertIn(stale["id"], " ".join(str(call.args[0]) for call in self.log.call_args_list))
            self.assertIs(result["ok"], False)  # the stale success did not answer this wait
            self.assertEqual(bridge.pending_commands, {})


if __name__ == "__main__":
    unittest.main()
