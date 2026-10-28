"""An action's expect check ends at expect.timeout with slow or queued reads, and the reads
after an unmet outcome have their own allowance (#295)."""
import asyncio
import json
import logging
import time
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect

from kitsune import bridge

TAB = {"id": 7, "url": "https://shop.test/checkout", "title": "Checkout", "active": True}
TIMEOUT_MS = 300
# A read this slow never answers within a test's timeout: whether it is cut off does not
# depend on scheduling jitter.
NEVER_S = 30
# Test-only guard: a check that overruns this fails its test instead of hanging the suite.
WATCHDOG_S = 10
SLACK_MS = 150

UNMET_TEXT = {"ok": False, "error": "Timed out after 0ms waiting for visible text"}
TEXT_FOUND = {"ok": True, "text": "Saved"}
UNMET_ELEMENT = {"ok": False, "found": False, "error": 'Timed out after 0ms waiting for "#menu"'}
ELEMENT_FOUND = {"ok": True, "found": True}
TAB_CLOSED = {"error": "Tab 7 was closed", "code": "tab_closed"}
INCOMPLETE = "; the check in progress at the timeout did not finish, so this is the last completed check"
UNKNOWN = "; no check finished in time, so whether it happened is unknown"


async def watchdog(awaitable):
    try:
        return await asyncio.wait_for(awaitable, WATCHDOG_S)
    except asyncio.TimeoutError:
        raise AssertionError(f"still running after the {WATCHDOG_S}s test watchdog") from None


class SlowExtension:
    """Scripted send_to_extension whose replies arrive after a latency.

    Each action has a list of (latency_s, reply) entries; the last one repeats. An unscripted
    action answers at once with the extension's unknown-action reply.
    """

    def __init__(self, **script):
        self.script = {action: list(entries) for action, entries in script.items()}
        self.calls = []
        self.budgets = []
        self.starts = []
        self.answered = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        index = len(self.calls)
        self.calls.append((action, params))
        self.budgets.append(timeout)
        self.starts.append(time.monotonic())
        entries = self.script.get(action, [(0, {"error": f"Unknown action: {action}"})])
        latency, reply = entries.pop(0) if len(entries) > 1 else entries[0]
        if callable(reply):
            reply = reply(params)
        await asyncio.sleep(latency)
        self.answered.append(index)
        return dict(reply)

    def actions(self):
        return [action for action, _ in self.calls]


def fast(reply):
    return [(0, reply)]


def run_click(extension, expect, params=None):
    """Run a click with expect; returns the result, its wall time in ms and when the click answered."""
    spec, problem = bridge._parse_expect("click", {**expect, "timeout": expect.get("timeout", TIMEOUT_MS)})
    assert problem is None, problem
    start = time.monotonic()
    with patch.object(bridge, "send_to_extension", extension), \
         patch.object(bridge, "OUTCOME_POLL_SECONDS", 0.02):
        result = asyncio.run(watchdog(bridge._send_with_expectation("click", {"tabId": 7, **(params or {})}, spec)))
    return result, (time.monotonic() - start) * 1000


class OutcomeAssertions(unittest.TestCase):
    def assert_dispatched_once(self, result, extension):
        self.assertIs(result["dispatched"], True)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertIs(result["retryable"], False)  # the click already ran
        self.assertEqual(extension.actions().count("click"), 1)

    def assert_ends_at(self, result, wall_ms, timeout_ms=TIMEOUT_MS, diagnosis_ms=SLACK_MS):
        self.assertGreaterEqual(result["outcome"]["elapsed"], timeout_ms)
        self.assertLess(result["outcome"]["elapsed"], timeout_ms + SLACK_MS)
        self.assertLess(wall_ms, timeout_ms + SLACK_MS + diagnosis_ms)

    def assert_last_completed_check(self, result, extension, wall_ms):
        self.assert_dispatched_once(result, extension)
        self.assertIs(result["outcome"]["met"], False)
        self.assertIs(result["outcome"]["observationIncomplete"], True)
        self.assertEqual(result["error"], f"Expected outcome not observed within {TIMEOUT_MS}ms" + INCOMPLETE)
        self.assert_ends_at(result, wall_ms)

    def assert_no_check_answered(self, result, extension, wall_ms, source):
        self.assert_dispatched_once(result, extension)
        self.assertIs(result["outcome"]["met"], False)
        self.assertIs(result["outcome"]["observationIncomplete"], True)
        self.assertEqual(result["outcome"]["observed"], {
            "source": source, "code": "timeout",
            "error": f"No check answered within {TIMEOUT_MS}ms, so whether the outcome happened is unknown"})
        self.assertEqual(result["error"], f"Expected outcome not observed within {TIMEOUT_MS}ms" + UNKNOWN)
        self.assert_ends_at(result, wall_ms)


class SlowObservationTests(OutcomeAssertions):
    """Every check, slow or not, answers by the deadline or is not an observation."""

    def test_a_check_that_does_not_answer_keeps_the_last_completed_check(self):
        extension = SlowExtension(click=fast({"ok": True}), getTabs=fast({"tabs": [TAB]}),
                                  getOpenedTabs=fast({"opened_tabs": []}),
                                  waitForText=[(0, UNMET_TEXT), (NEVER_S, UNMET_TEXT)])
        result, wall_ms = run_click(extension, {"text": "Saved"})
        self.assert_last_completed_check(result, extension, wall_ms)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": False})
        self.assertEqual(result["state"], {"tabId": 7, "url": TAB["url"], "title": "Checkout"})

    def test_no_check_answering_leaves_the_outcome_unknown(self):
        for expect, action, source in (({"text": "Saved"}, "waitForText", "page"),
                                       ({"selector": "#menu"}, "waitForElement", "page"),
                                       ({"selector": "#qty", "value": "2"}, "query", "page"),
                                       ({"new_tab": True}, "getOpenedTabs", "tabs"),
                                       ({"url": "/done$"}, "waitForUrl", "tab")):
            with self.subTest(expect=expect):
                extension = SlowExtension(**{"click": fast({"ok": True}), "getTabs": fast({"tabs": [TAB]}),
                                             "getOpenedTabs": fast({"opened_tabs": []}),
                                             action: [(NEVER_S, {"ok": True})]})
                result, wall_ms = run_click(extension, expect)
                self.assert_no_check_answered(result, extension, wall_ms, source)
                self.assertEqual(extension.actions()[1:2], [action])

    def test_a_late_success_or_error_is_not_an_observation(self):
        for late in (TEXT_FOUND, TAB_CLOSED):
            with self.subTest(late=late):
                extension = SlowExtension(click=fast({"ok": True}), getTabs=fast({"tabs": [TAB]}),
                                          getOpenedTabs=fast({"opened_tabs": []}),
                                          waitForText=[(0, UNMET_TEXT), (NEVER_S, late)])
                result, wall_ms = run_click(extension, {"text": "Saved"})
                self.assert_last_completed_check(result, extension, wall_ms)
                self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": False})

    def test_each_check_is_given_only_the_time_left(self):
        extension = SlowExtension(click=fast({"ok": True}), getTabs=fast({"tabs": [TAB]}),
                                  getOpenedTabs=fast({"opened_tabs": []}),
                                  waitForElement=[(0.004, UNMET_ELEMENT)])
        result, wall_ms = run_click(extension, {"selector": "#menu"})
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assertEqual(result["error"], f"Expected outcome not observed within {TIMEOUT_MS}ms")
        self.assert_ends_at(result, wall_ms)
        checks = [i for i, action in enumerate(extension.actions()) if action == "waitForElement"]
        self.assertGreater(len(checks), 2)
        budgets = [extension.budgets[i] for i in checks]
        self.assertLessEqual(max(budgets), TIMEOUT_MS / 1000)
        self.assertEqual(budgets, sorted(budgets, reverse=True))
        # The last check starts early enough to answer before the deadline.
        self.assertAlmostEqual(budgets[-1], bridge.PAGE_WAIT_LAST_READ_SECONDS, delta=0.01)

    def test_a_url_wait_ends_in_the_extension_before_its_budget(self):
        def wait_then_unmet(params):
            return {"ok": False, "url": TAB["url"], "error": "Timed out"}

        class InExtensionWait(SlowExtension):
            async def __call__(self, action, params=None, timeout=30):
                if action == "waitForUrl":
                    self.waits = getattr(self, "waits", []) + [(params["timeout"], timeout)]
                    await asyncio.sleep(params["timeout"] / 1000)
                return await super().__call__(action, params, timeout)

        extension = InExtensionWait(click=fast({"ok": True}), getTabs=fast({"tabs": [TAB]}),
                                    getOpenedTabs=fast({"opened_tabs": []}),
                                    waitForUrl=fast(wait_then_unmet))
        result, wall_ms = run_click(extension, {"url_changed": True})
        self.assert_dispatched_once(result, extension)
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assertEqual(result["outcome"]["observed"], {"source": "tab", "url": TAB["url"]})
        self.assert_ends_at(result, wall_ms)
        for wait_ms, budget_s in extension.waits:
            self.assertLess(wait_ms / 1000, budget_s)
            self.assertLessEqual(budget_s, TIMEOUT_MS / 1000)
        # It waits in the extension until the last check, which answers before the deadline.
        self.assertEqual(len(extension.waits), 2)
        self.assertGreater(extension.waits[1][0], 0)

    def test_a_url_wait_that_overshoots_is_cut_off_at_the_deadline(self):
        extension = SlowExtension(click=fast({"ok": True}), getTabs=fast({"tabs": [TAB]}),
                                  getOpenedTabs=fast({"opened_tabs": []}),
                                  waitForUrl=[(NEVER_S, {"ok": True, "url": TAB["url"] + "/done"})])
        result, wall_ms = run_click(extension, {"url_changed": True})
        self.assert_no_check_answered(result, extension, wall_ms, "tab")

    def test_a_met_outcome_still_ends_the_check_early(self):
        extension = SlowExtension(click=fast({"ok": True}),
                                  waitForText=[(0, UNMET_TEXT), (0.02, TEXT_FOUND)])
        result, _ = run_click(extension, {"text": "Saved", "timeout": 5000})
        self.assertIs(result["ok"], True)
        self.assertIs(result["outcome"]["met"], True)
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assertLess(result["outcome"]["elapsed"], 1000)
        self.assertEqual(extension.actions(), ["click", "waitForText", "waitForText"])

    def test_a_genuine_error_still_ends_the_check_with_its_code(self):
        extension = SlowExtension(click=fast({"ok": True}), getTabs=fast({"tabs": []}),
                                  waitForText=fast(TAB_CLOSED))
        result, _ = run_click(extension, {"text": "Saved", "timeout": 5000})
        self.assert_dispatched_once(result, extension)
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assertEqual(result["outcome"]["observed"], {"source": "page", **TAB_CLOSED})
        self.assertEqual(result["error"], "Expected outcome not observed within 5000ms")
        self.assertLess(result["outcome"]["elapsed"], 1000)


class DiagnosisAllowanceTests(OutcomeAssertions):
    """The reads after an unmet outcome end within OUTCOME_DIAGNOSIS_SECONDS (#295)."""

    DIAGNOSIS_S = 0.2

    def run_diagnosed(self, extension, expect, params=None):
        with patch.object(bridge, "OUTCOME_DIAGNOSIS_SECONDS", self.DIAGNOSIS_S):
            return run_click(extension, expect, params)

    def diagnoses(self, extension):
        """Actions after the observation's last check."""
        actions = extension.actions()
        last_check = max(i for i, action in enumerate(actions) if action == "waitForText")
        return actions[last_check + 1:]

    def test_a_state_read_that_does_not_answer_starts_no_further_diagnosis(self):
        extension = SlowExtension(click=fast({"ok": True, "target_ref": "e3", "snapshot": "- button"}),
                                  waitForText=fast(UNMET_TEXT),
                                  getTabs=[(NEVER_S, {"tabs": [TAB]})],
                                  getOpenedTabs=[(NEVER_S, {"opened_tabs": [{"tabId": 9}]})],
                                  waitForElement=[(NEVER_S, ELEMENT_FOUND)])
        result, wall_ms = self.run_diagnosed(extension, {"text": "Saved"})
        self.assert_dispatched_once(result, extension)
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assert_ends_at(result, wall_ms, diagnosis_ms=self.DIAGNOSIS_S * 1000 + SLACK_MS)
        self.assertEqual(result["state"], {"tabId": 7})
        self.assertEqual(result["outcome"]["state"], {"tabId": 7})
        self.assertNotIn("opened_tabs", result)
        self.assertNotIn("browser_trusted_click", result["hint"])
        self.assertEqual(self.diagnoses(extension), ["getTabs"])

    def test_slow_optional_diagnoses_end_within_the_allowance(self):
        extension = SlowExtension(click=fast({"ok": True, "target_ref": "e3", "snapshot": "- button"}),
                                  waitForText=fast(UNMET_TEXT), getTabs=fast({"tabs": [TAB]}),
                                  getOpenedTabs=[(NEVER_S, {"opened_tabs": [{"tabId": 9}]})],
                                  waitForElement=[(NEVER_S, ELEMENT_FOUND)])
        result, wall_ms = self.run_diagnosed(extension, {"text": "Saved"})
        self.assert_ends_at(result, wall_ms, diagnosis_ms=self.DIAGNOSIS_S * 1000 + SLACK_MS)
        self.assertEqual(result["state"], {"tabId": 7, "url": TAB["url"], "title": "Checkout"})
        self.assertNotIn("opened_tabs", result)
        self.assertNotIn("browser_trusted_click", result["hint"])
        self.assertEqual(self.diagnoses(extension), ["getTabs", "getOpenedTabs"])
        # Each diagnosis is given only what is left of the allowance.
        for i in range(len(extension.calls)):
            if extension.calls[i][0] in ("getTabs", "getOpenedTabs"):
                self.assertLessEqual(extension.budgets[i], self.DIAGNOSIS_S)

    def test_diagnoses_that_answer_in_time_keep_their_hints(self):
        extension = SlowExtension(click=fast({"ok": True, "target_ref": "e3", "snapshot": "- button"}),
                                  waitForText=fast(UNMET_TEXT), getTabs=[(0.01, {"tabs": [TAB]})],
                                  getOpenedTabs=[(0.01, {"opened_tabs": []})],
                                  waitForElement=[(0.01, ELEMENT_FOUND)])
        result, _ = self.run_diagnosed(extension, {"text": "Saved"})
        self.assertIn("browser_trusted_click", result["hint"])
        self.assertEqual(self.diagnoses(extension), ["getTabs", "getOpenedTabs", "waitForElement"])
        visible_check = extension.calls[-1][1]
        self.assertEqual(visible_check, {"tabId": 7, "selector": "e3", "timeout": 0})

        extension = SlowExtension(click=fast({"ok": True}), waitForText=fast(UNMET_TEXT),
                                  getTabs=[(0.01, {"tabs": [TAB]})],
                                  getOpenedTabs=[(0.01, {"opened_tabs": [{"tabId": 9, "url": "about:blank"}]})])
        result, _ = self.run_diagnosed(extension, {"text": "Saved"})
        self.assertEqual(result["opened_tabs"], [{"tabId": 9, "url": "about:blank"}])
        self.assertIn("tab_id=9", result["hint"])


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


class QueuedObservationTests(_SchedulerState, OutcomeAssertions):
    """The deadline covers a check's wait behind other commands on the tab (#295)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent = []
        self.release_tab = None

        async def raw(action, params=None, timeout=30):
            self.sent.append(action)
            if action == "click":
                # Another client's command on the tab is queued behind the click.
                asyncio.get_running_loop().create_task(self.hold_tab())
                await asyncio.sleep(0)  # it waits for the tab before the click's check does
                return {"ok": True}
            if action == "getTabs":
                return {"tabs": [TAB]}
            if action == "getOpenedTabs":
                return {"opened_tabs": []}
            return dict(UNMET_TEXT)

        patcher = patch.object(bridge, "_send_to_extension_raw", raw)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def hold_tab(self):
        lock = bridge._get_scope_lock(("tab", 7))
        await lock.acquire()
        self.release_tab = asyncio.Event()
        await self.release_tab.wait()
        lock.release()

    DIAGNOSIS_S = 0.1

    async def run_click(self, timeout_ms=TIMEOUT_MS):
        spec, problem = bridge._parse_expect("click", {"text": "Saved", "timeout": timeout_ms})
        assert problem is None, problem
        start = time.monotonic()
        # getOpenedTabs waits for the tab too, until the diagnosis allowance ends.
        with patch.object(bridge, "OUTCOME_DIAGNOSIS_SECONDS", self.DIAGNOSIS_S):
            result = await watchdog(bridge._send_with_expectation("click", {"tabId": 7}, spec))
        return result, (time.monotonic() - start) * 1000

    async def test_a_check_queued_past_the_deadline_is_never_dispatched(self):
        result, wall_ms = await self.run_click()
        self.assertEqual(result["code"], "outcome_not_observed")
        self.assertIs(result["outcome"]["observationIncomplete"], True)
        self.assertEqual(result["outcome"]["observed"]["code"], "timeout")
        self.assertGreaterEqual(result["outcome"]["elapsed"], TIMEOUT_MS)
        self.assertLess(wall_ms, TIMEOUT_MS + self.DIAGNOSIS_S * 1000 + 2 * SLACK_MS)
        self.assertEqual(bridge._scheduler_queued, {})
        self.assertNotIn("tab:7", bridge._scheduler_active)
        self.release_tab.set()
        await asyncio.sleep(0.05)
        # Nothing detached sends the cancelled check later, and the click was sent once.
        self.assertEqual(self.sent.count("click"), 1)
        self.assertNotIn("waitForText", self.sent)
        self.assertNotIn("getOpenedTabs", self.sent)
        self.assertFalse(bridge._get_scope_lock(("tab", 7)).locked())

    async def test_a_check_queued_briefly_still_observes_before_the_deadline(self):
        async def release_soon():
            while self.release_tab is None:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.1)
            self.release_tab.set()

        releaser = asyncio.create_task(release_soon())
        result, _ = await self.run_click()
        await releaser
        self.assertNotIn("observationIncomplete", result["outcome"])
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": False})
        self.assertIn("waitForText", self.sent)
        self.assertLess(result["outcome"]["elapsed"], TIMEOUT_MS + SLACK_MS)


class LateCheckReplyTests(_SchedulerState):
    """A check cut off at the deadline leaves no pending command, and its late reply is ignored."""

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

    async def test_a_late_check_reply_resolves_nothing(self):
        await watchdog(self.late_check_reply_resolves_nothing())

    async def late_check_reply_resolves_nothing(self):
        async with connect(self.uri, origin="moz-extension://0b5e3f9c-test", open_timeout=5) as client:
            for _ in range(200):
                if bridge.extension_ws is not None:
                    break
                await asyncio.sleep(0.01)
            spec, _ = bridge._parse_expect("click", {"text": "Saved", "timeout": TIMEOUT_MS})
            task = asyncio.create_task(bridge._send_with_expectation("click", {"tabId": 7}, spec))
            replies = {"click": {"ok": True}, "getTabs": {"tabs": [TAB]}, "getOpenedTabs": {"opened_tabs": []}}
            stale = None
            while not task.done():
                try:
                    command = json.loads(await asyncio.wait_for(client.recv(), 0.05))
                except asyncio.TimeoutError:
                    continue
                if command["action"] == "waitForText":
                    stale = command  # never answered within the check
                    continue
                await client.send(json.dumps({"id": command["id"], "result": replies[command["action"]]}))
            result = await task
            self.assertIsNotNone(stale)
            self.assertIs(result["outcome"]["observationIncomplete"], True)
            self.assertEqual(bridge.pending_commands, {})
            await client.send(json.dumps({"id": stale["id"], "result": TEXT_FOUND}))
            await asyncio.sleep(0.05)
            self.assertIn(stale["id"], " ".join(str(call.args[0]) for call in self.log.call_args_list))
            self.assertEqual(bridge.pending_commands, {})


if __name__ == "__main__":
    unittest.main()
