import asyncio
import json
import logging
import os
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, server, waits


TIMEOUT = {"error": "Command timed out after 30s", "code": "timeout", "retryable": False}


class ContainerTests(unittest.TestCase):
    """Batch, parallel and workflow calls against a mocked extension."""

    def setUp(self):
        self.sent = []
        self.replies = {}
        self.closed = set()
        patches = [
            patch.dict(bridge._policy, {"allow": [], "deny": [], "readonly": False, "ask": False,
                                        "askUrls": list(bridge.DEFAULT_ASK_URLS), "askLocked": False}),
            patch.dict(bridge._confirm_tokens, clear=True),
            patch.dict(bridge._unknown_outcomes, clear=True),
            patch.object(bridge, "_audit_log", []),
            patch.object(bridge, "_global_command_lock", None),
            patch.object(bridge, "_tab_command_locks", {}),
            patch.object(bridge, "_scheduler_gate", None),
            patch.object(bridge, "_send_to_extension_raw", new=self.extension),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def extension(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.sent.append((action, params))
        tab_id = params.get("tabId")
        if tab_id in self.closed:
            return {"error": f"Tab {tab_id} no longer exists", "code": "tab_closed", "retryable": True,
                    "state": {"tabId": tab_id}}
        if action == "closeTab":
            self.closed.add(tab_id)
        reply = self.replies.get(action)
        if callable(reply):
            return await reply(params)
        return reply.pop(0) if reply else {"ok": True}

    def call(self, route, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        route(handler)
        status, payload = handler.send_json.call_args.args
        self.assertEqual(status, 200, payload)
        return payload

    def batch(self, *commands, **body):
        return self.call(lambda h: h.handle_batch(), {"commands": list(commands), **body})

    def parallel(self, *sequences):
        return self.batch({"action": "parallel", "sequences": list(sequences)})

    @staticmethod
    def statuses(steps):
        return [step["status"] for step in steps]


class ParallelTabOwnershipTests(ContainerTests):
    def test_steps_without_their_tab_are_refused_before_anything_runs(self):
        for implicit in ({"action": "click", "selector": "#b"},
                         {"action": "click", "selector": "#b", "tabId": None},
                         {"action": "js", "code": "1", "tabId": "null"},
                         {"action": "sequence", "do": [{"action": "pageText"}]},
                         {"action": "retry", "command": {"action": "type", "selector": "#q", "text": "x"}},
                         {"action": "while", "condition": "0", "do": []}):
            with self.subTest(step=implicit):
                payload = self.parallel([{"action": "click", "selector": "#a", "tabId": 1}],
                                        [{"action": "sleep", "ms": 0}, implicit])
                [rejected] = payload["results"]
                self.assertEqual((rejected["code"], rejected["retryable"]), ("tab_required", True))
                self.assertEqual((rejected["state"]["sequence"], rejected["state"]["index"]), (1, 1))
                self.assertIn("inside parallel requires explicit tabId", rejected["error"])
                self.assertEqual(payload["progress"][0]["status"], "failed")
                self.assertEqual(self.sent, [], "no sequence started")

    def test_tabless_steps_and_reads_of_another_sequences_tab_run(self):
        payload = self.parallel(
            [{"action": "newTab", "url": "about:blank"}, {"action": "click", "selector": "#a", "tabId": 1}],
            [{"action": "sleep", "ms": 0}, {"action": "tabs"}, {"action": "pageText", "tabId": 1}])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual([self.statuses(seq) for seq in payload["progress"][0]["sequences"]],
                         [["ok", "ok"], ["ok", "ok", "ok"]])

    def test_two_sequences_cannot_write_the_same_tab(self):
        payload = self.parallel([{"action": "click", "selector": "#a", "tabId": 4}],
                                [{"action": "pageText", "tabId": 4},
                                 {"action": "if", "condition": "1", "tabId": 5, "then": [
                                     {"action": "type", "selector": "#q", "text": "x", "tabId": "4"}]}])
        [rejected] = payload["results"]
        self.assertEqual(rejected["code"], "tab_owned")
        self.assertEqual({k: rejected["state"][k] for k in ("sequence", "index", "tabId", "owner")},
                         {"sequence": 1, "index": 1, "tabId": 4, "owner": 0})
        self.assertEqual(self.sent, [])

    def test_if_and_while_conditions_claim_their_tab(self):
        """A condition is JavaScript run on its tab, so it owns the tab like a js step."""
        for condition in ({"action": "if", "condition": "1", "tabId": 4, "then": []},
                          {"action": "while", "condition": "0", "tabId": 4, "do": []}):
            with self.subTest(action=condition["action"]):
                for sequences in ([[{"action": "click", "selector": "#a", "tabId": 4}], [condition]],
                                  [[condition], [dict(condition)]]):
                    payload = self.parallel(*sequences)
                    [rejected] = payload["results"]
                    self.assertEqual((rejected["code"], rejected["state"]["tabId"], rejected["state"]["owner"]),
                                     ("tab_owned", 4, 0))
                    self.assertEqual(self.sent, [])

    def test_conditions_run_on_their_own_tab_beside_another_sequences_writes(self):
        self.replies["executeJS"] = [{"result": True}, {"result": True}, {"result": False}]
        payload = self.parallel(
            [{"action": "click", "selector": "#a", "tabId": 4}],
            [{"action": "if", "condition": "document.title", "tabId": 5, "then": [
                {"action": "click", "selector": "#b", "tabId": 5}]},
             {"action": "while", "condition": "!done", "tabId": 5, "do": [{"action": "pageText", "tabId": 4}]}])
        self.assertEqual(payload["status"], "ok")
        conditions = [p for a, p in self.sent if a == "executeJS"]
        self.assertEqual([p["tabId"] for p in conditions], [5, 5, 5])
        self.assertEqual([self.statuses(seq) for seq in payload["progress"][0]["sequences"]], [["ok"], ["ok", "ok"]])

    def test_substituted_condition_tab_is_claimed_before_the_condition_runs(self):
        self.replies["getPageInfo"] = [{"tabId": 4, "url": "https://example.com/"}]
        payload = self.parallel(
            [{"action": "click", "selector": "#a", "tabId": 4}],
            [{"action": "sequence", "do": [
                {"action": "pageInfo", "tabId": 2},
                {"action": "while", "condition": "1", "tabId": "${$0.tabId}", "do": [
                    {"action": "click", "selector": "#b", "tabId": 2}]}]}])
        [[_, [sequence]]] = payload["results"]
        self.assertEqual((sequence["results"][1]["code"], sequence["results"][1]["state"]["owner"]), ("tab_owned", 0))
        self.assertNotIn("executeJS", [a for a, _ in self.sent])

    def test_substituted_tab_ids_are_claimed_when_their_step_runs(self):
        self.replies["getPageInfo"] = [{"tabId": 4, "url": "https://example.com/"}]
        payload = self.parallel(
            [{"action": "click", "selector": "#a", "tabId": 4}],
            [{"action": "sequence", "do": [
                {"action": "pageInfo", "tabId": 2},
                {"action": "click", "selector": "#b", "tabId": "${$0.tabId}"},
                {"action": "click", "selector": "#c", "tabId": "${$0.tabId}"}]}])
        [[[own], [sequence]]] = payload["results"]
        self.assertEqual(own, {"ok": True})
        info, refused, _ = sequence["results"]
        self.assertEqual(info["tabId"], 4)
        self.assertEqual((refused["code"], refused["state"]["owner"]), ("tab_owned", 0))
        self.assertEqual([a for a, p in self.sent if a == "click"], ["click"], "only sequence 0 clicked tab 4")
        self.assertEqual(payload["progress"][0]["sequences"][1][0]["status"], "failed")

    def test_closing_or_switching_tabs_never_redirects_another_step(self):
        payload = self.parallel(
            [{"action": "click", "selector": "#a", "tabId": 5}, {"action": "closeTab", "tabId": 5},
             {"action": "type", "selector": "#q", "text": "late", "tabId": 5}],
            [{"action": "switchTab", "tabId": 6}, {"action": "click", "selector": "#b", "tabId": 6}])
        [[first, second]] = payload["results"]
        self.assertEqual(first[2]["code"], "tab_closed")
        self.assertEqual(second, [{"ok": True}, {"ok": True}])
        self.assertTrue(all(p.get("tabId") in (5, 6) for _, p in self.sent), self.sent)
        self.assertEqual([p["tabId"] for a, p in self.sent if a == "type"], [5])
        self.assertEqual([self.statuses(seq) for seq in payload["progress"][0]["sequences"]],
                         [["ok", "ok", "failed"], ["ok", "ok"]])


class StepTabIdTests(ContainerTests):
    def test_unresolved_or_falsy_tab_ids_are_not_sent_to_the_active_tab(self):
        self.replies["newTab"] = [{"error": "could not open the tab"}]
        payload = self.batch(
            {"action": "sequence", "do": [
                {"action": "newTab", "url": "about:blank"},
                {"action": "click", "selector": "#go", "tabId": "${$0.tabId}"},
                {"action": "click", "selector": "#go", "tabId": "${$5.tabId}"}]},
            {"action": "fill", "selector": "#q", "value": "x", "tabId": 0},
            {"action": "fill", "selector": "#q", "value": "x", "tabId": ""})
        _, null_id, missing = payload["results"][0]["results"]
        self.assertEqual([r["code"] for r in (null_id, missing, *payload["results"][1:])], ["invalid_tab_id"] * 4)
        self.assertEqual((null_id["state"]["tabId"], null_id["retryable"]), ("null", True))
        self.assertEqual(self.sent, [("newTab", {"url": "about:blank"})])
        self.assertEqual(self.statuses(payload["progress"]), ["failed", "failed", "failed"])


class ContainerProgressTests(ContainerTests):
    def test_batch_progress_reports_each_step(self):
        self.replies["fill"] = [TIMEOUT]
        self.replies["getPageText"] = [{"error": "Tab 3 no longer exists", "code": "tab_closed", "retryable": True}]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "pageText", "tabId": 3},
                             {"action": "retry", "backoffMs": 0, "command": {"action": "find", "query": "a"}})
        self.assertEqual(len(payload["results"]), 4)
        self.assertEqual(payload["progress"][:3], [
            {"index": 0, "action": "click", "tabId": 3, "status": "ok"},
            {"index": 1, "action": "fill", "tabId": 3, "status": "unknown"},
            {"index": 2, "action": "pageText", "tabId": 3, "status": "failed"},
        ])
        self.assertEqual(payload["progress"][3]["status"], "ok")
        self.assertEqual(payload["status"], "unknown")

    def test_retry_that_recovers_is_ok_and_nested_unknown_outcomes_surface(self):
        self.replies["find"] = [{"error": "Element not found", "code": "not_found", "retryable": True}]
        self.replies["submitForm"] = [TIMEOUT]
        payload = self.batch({"action": "retry", "backoffMs": 0, "command": {"action": "find", "query": "a"}},
                             {"action": "sequence", "do": [{"action": "submitForm", "tabId": 2},
                                                           {"action": "pageText", "tabId": 2}]})
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "unknown"])

    def test_stopped_batch_marks_later_steps_skipped(self):
        self.replies["click"] = [{"ok": True, "warning": "hidden_or_covered"}]
        payload = self.batch({"action": "click", "selector": "#a"}, {"action": "click", "selector": "#b"},
                             stopOnWarning=True)
        self.assertTrue(payload["results"][0]["_stopped"])
        self.assertEqual(payload["progress"][1], {"index": 1, "action": "click", "status": "skipped"})
        self.assertEqual(payload["status"], "skipped")

    def test_held_parallel_step_skips_the_rest_of_its_sequence(self):
        bridge._policy["ask"] = True
        payload = self.parallel([{"action": "cookies", "op": "set", "name": "sid", "value": "v"},
                                 {"action": "click", "selector": "#after", "tabId": 1}],
                                [{"action": "sleep", "ms": 0}])
        [step] = payload["progress"]
        self.assertEqual(step["status"], "held")
        self.assertEqual(step["sequences"], [
            [{"sequence": 0, "index": 0, "action": "cookies", "status": "held"},
             {"sequence": 0, "index": 1, "action": "click", "tabId": 1, "status": "skipped"}],
            [{"sequence": 1, "index": 0, "action": "sleep", "status": "ok"}],
        ])
        self.assertEqual(self.sent, [])

    def test_summary_says_what_ran_what_is_unknown_held_and_skipped(self):
        self.replies["fill"] = [TIMEOUT]
        self.replies["getPageText"] = [{"error": "Tab 3 no longer exists", "code": "tab_closed", "retryable": True}]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "pageText", "tabId": 3})
        self.assertEqual(payload["summary"],
                         "3 commands: 1 ok, 1 unknown, 1 failed. Unknown, may have taken effect (check the page "
                         "before repeating): step 1 (fill on tab 3). Failed, safe to repeat after its hint: "
                         "step 2 (pageText on tab 3).")
        bridge._policy["ask"] = True
        payload = self.batch({"action": "sleep", "ms": 0},
                             {"action": "cookies", "op": "set", "name": "sid", "value": "v"},
                             {"action": "click", "selector": "#after", "tabId": 1})
        self.assertEqual(payload["summary"],
                         "3 commands: 1 ok, 1 held, 1 skipped. Held for confirmation, not run (confirm each with "
                         "a direct call): step 1 (cookies). Skipped, never started: step 2 (click on tab 1).")
        self.assertEqual(self.batch()["summary"], "0 commands.")

    def test_summary_lists_parallel_commands_per_sequence_and_caps_long_lists(self):
        self.replies["click"] = [{"error": "Element not found", "code": "not_found", "retryable": True}]
        payload = self.parallel([{"action": "click", "selector": "#a", "tabId": 1},
                                 {"action": "pageText", "tabId": 1}],
                                [{"action": "pageText", "tabId": 2}])
        self.assertEqual(payload["summary"],
                         "3 commands: 2 ok, 1 failed. Failed, safe to repeat after its hint: "
                         "sequence 0 step 0 (click on tab 1).")
        refused = self.parallel([{"action": "click", "selector": "#a"}])
        self.assertEqual(refused["summary"],
                         "1 command: 1 failed. Failed, safe to repeat after its hint: step 0 (parallel).")
        self.replies["click"] = [{"ok": True, "warning": "hidden_or_covered"}]
        listed = self.batch(*[{"action": "click", "selector": f"#s{i}"} for i in range(8)], stopOnWarning=True)
        self.assertEqual(listed["summary"], "8 commands: 1 ok, 7 skipped. Skipped, never started: step 1 (click), "
                                            "step 2 (click), step 3 (click), step 4 (click), step 5 (click), "
                                            "and 2 more.")

    def test_workflow_progress_keeps_the_step_count(self):
        self.replies["click"] = [{"ok": True}, {"error": "Element not found", "code": "not_found", "retryable": True}]
        steps = [{"action": "click", "selector": f"#s{i}", "tabId": 8} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "fixture.md"), "w", encoding="utf-8") as f:
                f.write("".join("```json\n" + json.dumps(s) + "\n```\n" for s in steps))
            with patch.object(bridge, "WORKFLOW_DIR", tmp):
                payload = self.call(lambda h: h.handle_workflow(), {"name": "fixture"})
        self.assertEqual((payload["workflow"], payload["steps"], len(payload["results"])), ("fixture", 3, 2))
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "failed", "skipped"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["summary"], "3 commands: 1 ok, 1 failed, 1 skipped. Failed, safe to repeat after "
                                             "its hint: step 1 (click on tab 8). Skipped, never started: "
                                             "step 2 (click on tab 8).")

    def test_deadline_cancels_the_batch_and_reports_the_step_in_flight(self):
        async def hang(params):
            await asyncio.sleep(10)
        self.replies["fill"] = hang
        within = bridge._within_deadline
        with patch.object(bridge, "_within_deadline", lambda coro, seconds: within(coro, 0.05)):
            payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                                 {"action": "parallel", "sequences": [
                                     [{"action": "fill", "selector": "#q", "value": "x", "tabId": 3}],
                                     [{"action": "pageText", "tabId": 4}]]},
                                 {"action": "click", "selector": "#b", "tabId": 3})
        self.assertEqual((payload["code"], payload["retryable"]), ("container_timeout", False))
        self.assertEqual(payload["results"], [{"ok": True}])
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "unknown", "skipped"])
        self.assertEqual([self.statuses(seq) for seq in payload["progress"][1]["sequences"]], [["unknown"], ["ok"]])
        self.assertEqual(payload["status"], "unknown")
        self.assertNotIn(("click", {"selector": "#b", "tabId": 3}), self.sent, "no step starts after the deadline")
        self.assertEqual(payload["summary"],
                         "Stopped at its 300s deadline. 4 commands: 2 ok, 1 unknown, 1 skipped. Unknown, may have "
                         "taken effect (check the page before repeating): step 1, sequence 0 step 0 (fill on tab 3). "
                         "Skipped, never started: step 2 (click on tab 3).")
        self.assertIn(("fill", False, "cancelled"), [(e["action"], e["ok"], e["error"]) for e in bridge._audit_log])


class ContainerDeadlineTests(ContainerTests):
    """The MCP request outlasts the bridge's container deadline, so the partial report arrives."""

    def bridge_wait_seconds(self, route, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        route(handler)
        return handler.run_async.call_args.kwargs["timeout"]

    def test_mcp_request_timeout_exceeds_the_bridge_deadline(self):
        click = {"action": "click", "selector": "#go", "tabId": 3}
        slow = {"action": "waitForElement", "selector": "#a", "timeout": 60000, "tabId": 3}
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge, "WORKFLOW_DIR", tmp), \
                patch.object(server, "_post", return_value={"results": []}) as post:
            with open(os.path.join(tmp, "fixture.md"), "w", encoding="utf-8") as f:
                f.write("```json\n" + json.dumps(click) + "\n```\n")
            calls = [
                (lambda: server.browser_batch([click]), lambda h: h.handle_batch(), {"commands": [click]}),
                (lambda: server.browser_batch([slow] * 10), lambda h: h.handle_batch(), {"commands": [slow] * 10}),
                (lambda: server.browser_parallel([[slow] * 10, [click]]), lambda h: h.handle_batch(),
                 {"commands": [{"action": "parallel", "sequences": [[slow] * 10, [click]]}]}),
                (lambda: server.browser_workflow("fixture"), lambda h: h.handle_workflow(), {"name": "fixture"}),
            ]
            for tool, route, body in calls:
                with self.subTest(body=body):
                    tool()
                    request_s = post.call_args.kwargs["timeout"]
                    bridge_s = self.bridge_wait_seconds(route, body)
                    self.assertGreater(request_s, bridge_s)
                    self.assertGreaterEqual(bridge_s, 300 + 15)

    def test_mcp_client_receives_the_partial_report_at_the_deadline(self):
        """A real HTTP bridge with a mocked extension; deadlines are scaled down to fractions of a second."""
        async def hang(params):
            await asyncio.sleep(30)
        self.replies["fill"] = hang
        logger = logging.getLogger("httpx")  # logs every request at INFO
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        loop = asyncio.new_event_loop()
        loop_thread = Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        self.addCleanup(loop.close)
        self.addCleanup(loop_thread.join)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        http = bridge.BridgeHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
        Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        port = http.server_address[1]
        token = "container-deadline-test-token"
        with patch.object(bridge, "loop", loop), patch.object(bridge, "AUTH_TOKEN", token), \
                patch.object(bridge.BridgeHandler, "log_message"), \
                patch.object(bridge, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"}), \
                patch.object(server, "BRIDGE", f"http://127.0.0.1:{port}"), \
                patch.dict(os.environ, {"KITSUNE_TOKEN": token}), \
                patch.object(server, "_ensure_bridge") as ensure, \
                patch.object(waits, "CONTAINER_MIN_DEADLINE_SECONDS", 0.5), \
                patch.object(waits, "CONTAINER_MARGIN_SECONDS", 0.5):
            payload = server.browser_batch([
                {"action": "click", "selector": "#a", "tabId": 3},
                {"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                {"action": "click", "selector": "#b", "tabId": 3}])
            ensure.assert_not_called()
        self.assertEqual((payload["code"], payload["retryable"]), ("container_timeout", False))
        self.assertEqual(payload["state"], {"deadlineSeconds": 0.5})
        self.assertEqual(payload["results"], [{"ok": True}])
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "unknown", "skipped"])
        self.assertEqual([a for a, _ in self.sent], ["click", "fill"])
        self.assertTrue(payload["summary"].startswith("Stopped at its 0.5s deadline. 3 commands: 1 ok, 1 unknown"),
                        payload["summary"])


if __name__ == "__main__":
    unittest.main()
