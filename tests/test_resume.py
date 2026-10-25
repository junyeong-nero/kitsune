"""browser_resume (#115): checkpoints of batch, parallel and workflow calls against a mocked extension."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, server, tools, waits

from test_container_progress import TIMEOUT, ContainerTests

NOT_FOUND = {"error": "Element not found", "code": "not_found", "retryable": True}
WRITES = bridge._RESUME_WRITE_ACTIONS
FAKE_TYPED = "KITSUNE_FAKE_TYPED_VALUE_115"
FAKE_JS = "KITSUNE_FAKE_JS_BODY_115"


class ResumeTests(ContainerTests):
    def setUp(self):
        super().setUp()
        self.open_tabs = set(range(1, 10))
        self.replies["getTabs"] = self.list_tabs
        self.now = 1000.0
        clock = patch.object(bridge, "_checkpoint_clock", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    async def list_tabs(self, params):
        return {"tabs": [{"id": t} for t in sorted(self.open_tabs - self.closed)]}

    def respond(self, route, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        route(handler)
        return handler.send_json.call_args.args

    def resume(self, payload, observed=None, status=200, **body):
        request = {"call": payload["call"], "resume_token": payload["resume_token"], **body}
        if observed is not None:
            request["observed"] = observed
        code, response = self.respond(lambda h: h.handle_resume(), request)
        self.assertEqual(code, status, response)
        return response

    def refused(self, payload, code, observed=None, status=400, **body):
        sent = len(self.sent)
        response = self.resume(payload, observed, status=status, **body)
        self.assertEqual(response["code"], code, response)
        self.assertNotIn("results", response)
        self.assertEqual(self.sent[sent:], [], "a refused resume sends nothing")
        return response

    def sent_since(self, mark, writes_only=True):
        return [(a, p) for a, p in self.sent[mark:] if not writes_only or a in WRITES]

    def workflow(self, tmp, name, steps, **body):
        with open(os.path.join(tmp, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write("".join("```json\n" + json.dumps(s) + "\n```\n" for s in steps))
        code, payload = self.respond(lambda h: h.handle_workflow(), {"name": name, **body})
        self.assertEqual(code, 200, payload)
        return payload


class CheckpointTests(ResumeTests):
    def test_only_calls_that_did_not_finish_ok_get_a_checkpoint(self):
        ok = self.batch({"action": "click", "selector": "#a", "tabId": 3})
        self.assertEqual(ok["status"], "ok")
        self.assertNotIn("call", ok)
        self.assertNotIn("resume_token", ok)
        self.assertEqual(bridge._checkpoints, {})

        self.replies["fill"] = [NOT_FOUND]
        failed = self.batch({"action": "fill", "selector": "#q", "value": "x", "tabId": 3})
        self.assertIsInstance(failed["call"], int)
        self.assertGreaterEqual(len(failed["resume_token"]), 32)
        [checkpoint] = bridge._checkpoints.values()
        self.assertEqual(checkpoint["call"], failed["call"])
        self.assertNotIn(failed["resume_token"], json.dumps(checkpoint, default=str), "only its hash is kept")

    def test_each_not_ok_status_gets_a_checkpoint(self):
        self.replies["fill"] = [TIMEOUT]
        self.replies["click"] = [{"ok": True, "warning": "hidden_or_covered"}]
        unknown = self.batch({"action": "fill", "selector": "#q", "value": "x", "tabId": 3})
        skipped = self.batch({"action": "click", "selector": "#a"}, {"action": "click", "selector": "#b"},
                             stopOnWarning=True)
        bridge._policy["ask"] = True
        held = self.batch({"action": "cookies", "op": "set", "name": "sid", "value": "v"})
        self.assertEqual([p["status"] for p in (unknown, skipped, held)], ["unknown", "skipped", "held"])
        self.assertEqual(set(bridge._checkpoints), {unknown["call"], skipped["call"], held["call"]})
        self.assertEqual(len({unknown["resume_token"], skipped["resume_token"], held["resume_token"]}), 3)

    def test_checkpoints_expire_30_minutes_after_the_call(self):
        self.replies["fill"] = [NOT_FOUND, NOT_FOUND]
        first = self.batch({"action": "fill", "selector": "#q", "value": FAKE_TYPED, "tabId": 3})
        second = self.batch({"action": "fill", "selector": "#q", "value": FAKE_TYPED, "tabId": 3})
        stored = bridge._checkpoints[first["call"]]
        self.now += 30 * 60 - 1
        self.assertEqual(self.resume(second)["status"], "ok", "still there just before expiry")
        self.now += 1
        response = self.refused(first, "checkpoint_not_found", status=404)
        self.assertEqual(response["retryable"], False)
        self.assertEqual(bridge._checkpoints, {})
        self.assertEqual(stored, {}, "its commands and results were dropped")

    def test_at_most_20_checkpoints_are_kept(self):
        self.replies["click"] = [NOT_FOUND] * 22
        calls = [self.batch({"action": "click", "selector": f"#c{i}", "tabId": 3}) for i in range(20)]
        oldest = bridge._checkpoints[calls[0]["call"]]
        calls += [self.batch({"action": "click", "selector": f"#late{i}", "tabId": 3}) for i in range(2)]
        self.assertEqual(len(bridge._checkpoints), 20)
        self.assertEqual(list(bridge._checkpoints), [c["call"] for c in calls[2:]])
        self.assertEqual(oldest, {})
        self.refused(calls[0], "checkpoint_not_found", status=404)
        self.refused(calls[1], "checkpoint_not_found", status=404)
        self.assertEqual(self.resume(calls[-1])["status"], "ok")

    def test_resume_needs_the_calls_token(self):
        self.replies["click"] = [NOT_FOUND]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3})
        for token in ("", "x" * 32, payload["resume_token"][:-1], None):
            with self.subTest(token=token):
                self.refused({**payload, "resume_token": token}, "resume_token_invalid", status=403)
        self.assertIn(payload["call"], bridge._checkpoints, "a bad token does not drop the checkpoint")
        for call in (payload["call"] + 1, None, "nope", True):
            with self.subTest(call=call):
                self.refused({**payload, "call": call}, "checkpoint_not_found", status=404)
        self.assertEqual(self.resume(payload)["status"], "ok")
        self.refused(payload, "checkpoint_not_found", status=404)

    def test_a_running_resume_holds_its_checkpoint(self):
        self.replies["click"] = [NOT_FOUND]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3})
        body = {"call": payload["call"], "resume_token": payload["resume_token"]}
        checkpoint, plan, refused = bridge._claim_checkpoint(body)
        self.assertIsNone(refused)
        _, _, (status, error) = bridge._claim_checkpoint(body)
        self.assertEqual((status, error["code"]), (404, "checkpoint_not_found"))

    def test_shutdown_drops_every_checkpoint(self):
        self.replies["click"] = [NOT_FOUND, NOT_FOUND]
        self.batch({"action": "click", "selector": "#a", "tabId": 3})
        self.batch({"action": "click", "selector": "#b", "tabId": 3})
        stored = list(bridge._checkpoints.values())
        bridge._drop_all_checkpoints()
        self.assertEqual(bridge._checkpoints, {})
        self.assertEqual(stored, [{}, {}])


class ResumeDecisionTests(ResumeTests):
    def test_ok_writes_are_not_sent_and_failed_ones_are(self):
        self.replies["fill"] = [NOT_FOUND]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "click", "selector": "#b", "tabId": 3})
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "failed", "ok"])
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual(self.sent_since(mark), [("fill", {"selector": "#q", "value": "x", "tabId": 3})])
        self.assertEqual(resumed["results"], [{"ok": True}, {"ok": True}, {"ok": True}])
        self.assertEqual(resumed["progress"], [
            {"index": 0, "action": "click", "tabId": 3, "status": "ok", "resumed": "kept"},
            {"index": 1, "action": "fill", "tabId": 3, "status": "ok"},
            {"index": 2, "action": "click", "tabId": 3, "status": "ok", "resumed": "kept"}])
        self.assertEqual((resumed["status"], resumed["summary"]), ("ok", "3 commands: 3 ok."))
        self.assertNotIn("call", resumed)
        self.assertEqual(bridge._checkpoints, {}, "a resume that finished ok drops its checkpoint")

    def test_unknown_write_needs_an_observation(self):
        self.replies["fill"] = [TIMEOUT]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "pageText", "tabId": 3})
        response = self.refused(payload, "observed_required")
        self.assertEqual(response["state"], {"call": payload["call"], "steps": [
            {"step": "1", "action": "fill", "tabId": 3, "status": "unknown", "accepts": ["applied", "not_applied"]}]})
        self.assertIn(payload["call"], bridge._checkpoints)

        mark = len(self.sent)
        resumed = self.resume(payload, {"1": "not_applied"})
        self.assertEqual(self.sent_since(mark), [("fill", {"selector": "#q", "value": "x", "tabId": 3})])
        self.assertEqual(self.statuses(resumed["progress"]), ["ok", "ok", "ok"])

    def test_applied_marks_an_unknown_write_ok_without_sending_it(self):
        self.replies["fill"] = [TIMEOUT]
        payload = self.batch({"action": "fill", "selector": "#q", "value": FAKE_TYPED, "tabId": 3},
                             {"action": "click", "selector": "#go", "tabId": 3})
        self.assertEqual(self.statuses(payload["progress"]), ["unknown", "ok"])
        mark = len(self.sent)
        resumed = self.resume(payload, {"0": "applied"})
        self.assertEqual(self.sent[mark:], [])
        self.assertEqual(resumed["results"][0], {"ok": True, "observed": "applied"})
        self.assertEqual(resumed["progress"][0],
                         {"index": 0, "action": "fill", "tabId": 3, "status": "ok", "resumed": "applied"})
        entry = bridge._audit_log[-1]
        self.assertEqual((entry["action"], entry["ok"], entry["params"]["observed"], entry["params"]["sent"]),
                         ("fill", True, "applied", False))
        self.assertEqual(entry["params"]["value"], bridge.AUDIT_REDACTED)

    def test_observed_is_checked_before_anything_is_sent(self):
        self.replies["fill"] = [TIMEOUT]
        self.replies["click"] = [{"ok": True}, NOT_FOUND]
        payload = self.batch(
            {"action": "click", "selector": "#a", "tabId": 3},
            {"action": "sequence", "do": [{"action": "fill", "selector": "#q", "value": "x", "tabId": 3}]},
            {"action": "click", "selector": "#b", "tabId": 3},
            {"action": "pageText", "tabId": 3}, stopOnWarning=True)
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "unknown", "failed", "skipped"])
        response = self.refused(payload, "observed_invalid", {
            "0": "not_applied", "1": "applied", "1.0": "maybe", "3": "applied", "7": "applied", "1.3": "applied"})
        self.assertEqual(response["state"]["steps"], [
            {"step": "1.0", "reason": "value not accepted", "accepts": ["applied", "not_applied"]},
            {"step": "0", "reason": "ok, never sent again"},
            {"step": "1", "reason": "a sequence block: name the step inside it"},
            {"step": "3", "reason": "skipped, never started"},
            {"step": "7", "reason": "no such step"},
            {"step": "1.3", "reason": "no such step"}])
        self.refused(payload, "observed_invalid", ["1.0"])
        self.assertEqual(self.resume(payload, {"1.0": "applied", "2": "applied"})["status"], "ok",
                         "a failed step also accepts an observation")

    def test_no_ok_or_unknown_write_is_sent_again_without_not_applied(self):
        self.replies["click"] = [{"ok": True}, {"ok": True}, {"ok": True}]
        self.replies["find"] = [NOT_FOUND]
        self.replies["fill"] = [TIMEOUT]
        self.replies["submitForm"] = [TIMEOUT]
        self.replies["type"] = [{"ok": True}]
        commands = [
            {"action": "click", "selector": "#a", "tabId": 3},                                     # ok write
            {"action": "sequence", "do": [{"action": "type", "selector": "#q", "text": "t", "tabId": 3},
                                          {"action": "fill", "selector": "#f", "value": "v", "tabId": 3},
                                          {"action": "click", "selector": "#c", "tabId": 3}]},  # ok, unknown, ok
            {"action": "try", "do": [{"action": "click", "selector": "#t", "tabId": 3},
                                     {"action": "find", "query": "x", "tabId": 3}]},             # ok write inside
            {"action": "submitForm", "tabId": 3},                                                  # unknown write
            {"action": "pageText", "tabId": 3}]                                                    # ok read
        payload = self.batch(*commands)
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "unknown", "failed", "unknown", "ok"])
        response = self.refused(payload, "observed_required")
        self.assertEqual([(s["step"], s["accepts"]) for s in response["state"]["steps"]],
                         [("1.1", ["applied", "not_applied"]), ("2", ["applied"]), ("3", ["applied", "not_applied"])])
        for observed in ({"1.1": "not_applied", "3": "applied"}, {"1.1": "applied", "2": "applied"}):
            self.refused(payload, "observed_required", observed)
        self.refused(payload, "observed_invalid", {"1.1": "applied", "2": "not_applied", "3": "applied"})

        mark = len(self.sent)
        resumed = self.resume(payload, {"1.1": "not_applied", "2": "applied", "3": "applied"})
        self.assertEqual(self.sent_since(mark, writes_only=False),
                         [("fill", {"selector": "#f", "value": "v", "tabId": 3})], "nothing else, not even reads")
        self.assertEqual(resumed["status"], "ok")
        self.assertEqual(resumed["results"][1], {"results": [{"ok": True}, {"ok": True}, {"ok": True}]})

    def test_a_block_whose_write_ran_ok_accepts_only_applied(self):
        self.replies["click"] = [{"ok": True}]
        self.replies["find"] = [NOT_FOUND, NOT_FOUND]
        self.replies["executeJS"] = [{"result": True}]
        self.replies["getPageText"] = [NOT_FOUND, NOT_FOUND]
        payload = self.batch(
            {"action": "retry", "maxAttempts": 1, "command": {"action": "find", "query": "x", "tabId": 3}},
            {"action": "try", "do": [{"action": "click", "selector": "#t", "tabId": 3},
                                     {"action": "find", "query": "x", "tabId": 3}],
             "catch": [{"action": "pageText", "tabId": 3}]},
            {"action": "if", "condition": "1", "tabId": 3, "then": [{"action": "pageText", "tabId": 3}]})
        self.assertEqual(self.statuses(payload["progress"]), ["failed", "failed", "failed"])
        response = self.refused(payload, "observed_required")
        self.assertEqual([(s["step"], s["status"], s["accepts"]) for s in response["state"]["steps"]],
                         [("1", "failed", ["applied"]), ("2", "failed", ["applied"])],
                         "the try clicked and the if ran its condition; the retry sent nothing that took effect")
        self.refused(payload, "observed_invalid", {"1": "not_applied", "2": "applied"})

        mark = len(self.sent)
        resumed = self.resume(payload, {"1": "applied", "2": "applied"})
        self.assertEqual(self.sent_since(mark, writes_only=False), [("find", {"query": "x", "tabId": 3})])
        self.assertEqual(resumed["status"], "ok")

    def test_ok_read_runs_again_only_when_a_later_template_uses_it(self):
        self.replies["getPageInfo"] = [{"tabId": 4}, {"tabId": 5}]
        self.replies["newTab"] = [{"tabId": 7}]
        self.replies["click"] = [NOT_FOUND, NOT_FOUND]
        payload = self.batch({"action": "sequence", "do": [
            {"action": "pageInfo", "tabId": 2},
            {"action": "pageText", "tabId": 2},
            {"action": "newTab", "url": "about:blank"},
            {"action": "click", "selector": "#a", "tabId": "${$0.tabId}"},
            {"action": "click", "selector": "#b", "tabId": "${$2.tabId}"}]})
        self.assertEqual([p["tabId"] for a, p in self.sent if a == "click"], [4, 7])
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual([a for a, _ in self.sent[mark:]], ["getPageInfo", "click", "click"],
                         "pageText and newTab were not sent again")
        self.assertEqual([p["tabId"] for a, p in self.sent[mark:] if a == "click"], [5, 7],
                         "the template read fresh state; the newTab result was reused")
        self.assertEqual(resumed["results"][0]["results"][1:3], [{"ok": True}, {"tabId": 7}])

    def test_kept_steps_keep_their_results_and_do_not_stop_the_batch_again(self):
        self.replies["click"] = [{"ok": True, "warning": "hidden_or_covered"}, {"ok": True}]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "click", "selector": "#b", "tabId": 3}, stopOnWarning=True)
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "skipped"])
        resumed = self.resume(payload)
        self.assertEqual(resumed["results"], [{"ok": True, "warning": "hidden_or_covered"}, {"ok": True}])
        self.assertEqual(self.statuses(resumed["progress"]), ["ok", "ok"])


class ResumePolicyTests(ResumeTests):
    def test_held_step_stays_held_until_reported_applied(self):
        bridge._policy["ask"] = True
        payload = self.batch({"action": "sleep", "ms": 0},
                             {"action": "cookies", "op": "set", "name": "sid", "value": FAKE_TYPED},
                             {"action": "click", "selector": "#after", "tabId": 1})
        self.assertEqual(self.statuses(payload["progress"]), ["ok", "held", "skipped"])
        again = self.resume(payload)
        self.assertEqual(self.statuses(again["progress"]), ["ok", "held", "skipped"])
        self.assertEqual(again["results"][1]["code"], "confirmation_required")
        self.assertEqual(again["call"], payload["call"])
        self.assertNotEqual(again["resume_token"], payload["resume_token"])
        self.refused(payload, "resume_token_invalid", status=403)
        self.assertNotIn("cookies", [a for a, _ in self.sent])

        # The caller confirmed it with a direct call, so it reports it applied.
        audited = len(bridge._audit_log)
        resumed = self.resume(again, {"1": "applied"})
        self.assertNotIn("cookies", [a for a, _ in self.sent], "ask-before-act is not bypassed")
        self.assertEqual([a for a, _ in self.sent if a in WRITES], ["click"])
        self.assertEqual(self.statuses(resumed["progress"]), ["ok", "ok", "ok"])
        skipped = [e for e in bridge._audit_log[audited:] if e["action"] == "cookies"]
        self.assertEqual([(e["ok"], e["params"]["observed"], e["params"]["sent"], e["params"]["value"])
                          for e in skipped], [(True, "applied", False, bridge.AUDIT_REDACTED)])

    def test_kept_writes_after_a_held_step_stay_kept(self):
        self.replies["cookies"] = [NOT_FOUND]
        payload = self.batch({"action": "cookies", "op": "set", "name": "sid", "value": "v"},
                             {"action": "click", "selector": "#go", "tabId": 3})
        self.assertEqual(self.statuses(payload["progress"]), ["failed", "ok"])
        bridge._policy["ask"] = True
        held = self.resume(payload)
        self.assertEqual(held["progress"], [
            {"index": 0, "action": "cookies", "status": "held"},
            {"index": 1, "action": "click", "tabId": 3, "status": "ok", "resumed": "kept"}],
            "the batch stopped at the held step, and the click it did not reach is still ok")
        bridge._policy["ask"] = False
        mark = len(self.sent)
        self.assertEqual(self.resume(held)["status"], "ok")
        self.assertEqual([a for a, _ in self.sent[mark:]], ["cookies"])

    def test_resumed_steps_go_through_ask_before_act_again(self):
        self.replies["click"] = [NOT_FOUND]
        payload = self.batch({"action": "click", "selector": "#pay", "tabId": 3})
        bridge._policy["ask"] = True
        self.replies["describeTarget"] = [{"tabId": 3, "url": "https://shop.example/checkout", "target": {}}]
        resumed = self.resume(payload)
        self.assertEqual(resumed["progress"][0]["status"], "held")
        self.assertEqual([a for a, _ in self.sent if a == "click"], ["click"], "only the original click")

    def test_read_only_mode_refuses_resumed_writes(self):
        self.replies["fill"] = [NOT_FOUND]
        payload = self.batch({"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "pageText", "tabId": 3})
        bridge._policy["readonly"] = True
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual(resumed["results"][0], {"error": "Read-only mode is active; refusing action: fill"})
        self.assertEqual(self.sent[mark:], [])
        self.assertEqual(self.statuses(resumed["progress"]), ["failed", "ok"])
        self.assertIn("resume_token", resumed)

    def test_url_policy_applies_to_resumed_steps(self):
        self.replies["navigate"] = [NOT_FOUND]
        payload = self.batch({"action": "navigate", "url": "https://blocked.example/", "tabId": 3})
        bridge._policy["deny"] = [r"blocked\.example"]
        resumed = self.resume(payload)
        self.assertIn("URL denied by policy", resumed["results"][0]["error"])
        self.assertEqual([a for a, _ in self.sent], ["navigate"], "only the original call")


class ResumeWorkflowTests(ResumeTests):
    def test_workflow_resumes_with_its_shape_and_refuses_a_changed_file(self):
        self.replies["click"] = [{"ok": True}, NOT_FOUND]
        steps = [{"action": "click", "selector": f"#s{i}", "tabId": 8} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp, patch.object(bridge, "WORKFLOW_DIR", tmp):
            payload = self.workflow(tmp, "fixture", steps)
            self.assertEqual(self.statuses(payload["progress"]), ["ok", "failed", "skipped"])
            path = os.path.join(tmp, "fixture.md")
            with open(path, encoding="utf-8") as f:
                original = f.read()
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
            response = self.refused(payload, "workflow_changed", status=409)
            self.assertEqual(response["state"], {"call": payload["call"], "workflow": "fixture"})
            os.remove(path)
            self.refused(payload, "workflow_changed", status=409)
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)
            mark = len(self.sent)
            resumed = self.resume(payload)
        self.assertEqual([p["selector"] for _, p in self.sent[mark:]], ["#s1", "#s2"])
        self.assertEqual({k: resumed[k] for k in ("workflow", "steps", "status")},
                         {"workflow": "fixture", "steps": 3, "status": "ok"})
        self.assertEqual(self.statuses(resumed["progress"]), ["ok", "ok", "ok"])
        self.assertEqual(len(resumed["results"]), 3)


class ResumeParallelTests(ResumeTests):
    def sequences(self):
        return [[{"action": "click", "selector": "#a", "tabId": 1}, {"action": "fill", "selector": "#q", "value": "x", "tabId": 1}],
                [{"action": "click", "selector": "#b", "tabId": 2}],
                [{"action": "pageText", "tabId": 3}]]

    def failed_parallel(self):
        async def click(params):
            return NOT_FOUND if params["selector"] == "#b" else {"ok": True}
        self.replies["click"] = click
        self.replies["fill"] = [NOT_FOUND]
        payload = self.parallel(*self.sequences())
        self.assertEqual([self.statuses(s) for s in payload["progress"][0]["sequences"]],
                         [["ok", "failed"], ["failed"], ["ok"]])
        self.replies["click"] = None
        return payload

    def test_each_sequence_resumes_on_its_own(self):
        payload = self.failed_parallel()
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual(sorted(a + p.get("selector", "") for a, p in self.sent_since(mark)), ["click#b", "fill#q"])
        self.assertEqual(resumed["results"], [[[{"ok": True}, {"ok": True}], [{"ok": True}], [{"ok": True}]]])
        self.assertEqual(resumed["progress"][0]["sequences"][0][0],
                         {"sequence": 0, "index": 0, "action": "click", "tabId": 1, "status": "ok", "resumed": "kept"})
        self.assertEqual(resumed["status"], "ok")

    def test_a_sequence_whose_tab_was_closed_is_refused(self):
        payload = self.failed_parallel()
        self.closed.add(2)
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual([p["selector"] for _, p in self.sent_since(mark)], ["#q"], "sequence 1 sent nothing")
        refused = resumed["results"][0][1][0]
        self.assertEqual((refused["code"], refused["state"]), ("tab_closed", {"sequence": 1, "tabId": 2}))
        self.assertEqual([self.statuses(s) for s in resumed["progress"][0]["sequences"]],
                         [["ok", "ok"], ["failed"], ["ok"]])
        self.assertIn("resume_token", resumed)

    def test_tabs_that_cannot_be_listed_refuse_the_sequence(self):
        payload = self.failed_parallel()
        self.replies["getTabs"] = [{"error": "Extension not connected", "code": "extension_disconnected",
                                    "retryable": True}]
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual(self.sent_since(mark), [])
        self.assertEqual([r[-1]["code"] for r in resumed["results"][0][:2]], ["extension_disconnected"] * 2)

    def test_a_sequence_whose_tab_another_call_writes_is_refused(self):
        payload = self.failed_parallel()
        bridge._running_parallels[0] = (999, {2: 0}, {})
        mark = len(self.sent)
        resumed = self.resume(payload)
        self.assertEqual([p["selector"] for _, p in self.sent_since(mark)], ["#q"])
        refused = resumed["results"][0][1][0]
        self.assertEqual((refused["code"], refused["state"]), ("tab_claimed", {"sequence": 1, "tabId": 2, "call": 999}))

    def test_claims_from_the_first_run_still_hold(self):
        """Sequence 0 claimed tab 4 through a template; sequence 1 may not write it on resume either."""
        self.replies["getPageInfo"] = [{"tabId": 4}, {"tabId": 4}, {"tabId": 4}]
        payload = self.parallel(
            [{"action": "sequence", "do": [{"action": "pageInfo", "tabId": 9},
                                           {"action": "click", "selector": "#a", "tabId": "${$0.tabId}"}]}],
            [{"action": "sequence", "do": [{"action": "sleep", "ms": 50},
                                           {"action": "pageInfo", "tabId": 8},
                                           {"action": "click", "selector": "#b", "tabId": "${$1.tabId}"}]}])
        [[_, [sequence]]] = payload["results"]
        self.assertEqual(sequence["results"][2]["code"], "tab_owned")
        mark = len(self.sent)
        resumed = self.resume(payload)
        [[_, [sequence]]] = resumed["results"]
        self.assertEqual((sequence["results"][2]["code"], sequence["results"][2]["state"]["owner"]), ("tab_owned", 0))
        self.assertEqual([a for a, _ in self.sent[mark:]], ["getTabs", "getPageInfo"])
        self.assertEqual(self.sent_since(mark), [], "tab 4 was not written by sequence 1")


class ResumeFailureTests(ResumeTests):
    def test_deadline_in_a_resume_leaves_a_new_checkpoint(self):
        async def hang(params):
            await asyncio.sleep(10)
        self.replies["fill"] = [NOT_FOUND]
        payload = self.batch({"action": "fill", "selector": "#q", "value": "x", "tabId": 3},
                             {"action": "pageText", "tabId": 3})
        self.replies["fill"] = hang
        within = bridge._within_deadline
        with patch.object(bridge, "_within_deadline", lambda coro, seconds: within(coro, 0.05)):
            resumed = self.resume(payload)
        self.assertEqual((resumed["code"], resumed["call"]), ("container_timeout", payload["call"]))
        self.assertTrue(resumed["error"].startswith("Resumed batch stopped at its 300s deadline"))
        self.assertEqual(self.statuses(resumed["progress"]), ["unknown", "ok"])
        response = self.refused(resumed, "observed_required")
        self.assertEqual([s["step"] for s in response["state"]["steps"]], ["0"])

    def test_unknown_read_is_sent_again_without_an_observation(self):
        async def hang(params):
            await asyncio.sleep(10)
        self.replies["getPageText"] = hang
        within = bridge._within_deadline
        with patch.object(bridge, "_within_deadline", lambda coro, seconds: within(coro, 0.05)):
            payload = self.batch({"action": "pageText", "tabId": 3})
        self.assertEqual(self.statuses(payload["progress"]), ["unknown"])
        self.replies["getPageText"] = None
        mark = len(self.sent)
        self.assertEqual(self.resume(payload)["status"], "ok")
        self.assertEqual(self.sent[mark:], [("getPageText", {"tabId": 3})])

    def test_an_internal_error_drops_the_checkpoint(self):
        self.replies["click"] = [NOT_FOUND, NOT_FOUND]
        payload = self.batch({"action": "click", "selector": "#a", "tabId": 3},
                             {"action": "click", "selector": "#b", "tabId": 3})
        run_command = bridge.run_command

        async def fail_second(cmd):
            if cmd["selector"] == "#b":
                raise RuntimeError(FAKE_TYPED)
            return await run_command(cmd)
        with patch.object(bridge, "run_command", fail_second), patch.object(bridge, "_log") as log:
            resumed = self.resume(payload)
        self.assertNotIn(FAKE_TYPED, str(log.call_args_list))
        self.assertEqual((resumed["code"], resumed["retryable"]), ("resume_failed", False))
        self.assertNotIn("resume_token", resumed)
        self.assertNotIn(FAKE_TYPED, json.dumps(resumed))
        self.assertEqual(self.statuses(resumed["progress"]), ["ok", "unknown"])
        self.assertEqual(bridge._checkpoints, {})
        self.refused(payload, "checkpoint_not_found", status=404)


class ResumeAuditTests(ResumeTests):
    def test_resumed_steps_are_audited_under_their_call_without_the_token(self):
        self.replies["fill"] = [TIMEOUT]
        self.replies["executeJS"] = [{"result": True}]
        log = patch.object(bridge, "_log_buffer", [])
        log.start()
        self.addCleanup(log.stop)
        payload = self.batch({"action": "js", "code": FAKE_JS, "tabId": 3},
                             {"action": "fill", "selector": "#q", "value": FAKE_TYPED, "tabId": 3})
        response = self.refused(payload, "observed_required")
        audited = len(bridge._audit_log)
        resumed = self.resume(payload, {"1": "not_applied"})
        self.assertEqual(resumed["status"], "ok")
        [entry] = bridge._audit_log[audited:]
        self.assertEqual(entry["parent"], {"call": payload["call"], "resumed": True,
                                           "path": [{"action": "batch", "index": 1}]})
        self.assertEqual(entry["params"]["value"], bridge.AUDIT_REDACTED)
        original = [e["parent"]["call"] for e in bridge._audit_log[:audited]]
        self.assertEqual(original, [payload["call"]] * 2)
        everything = json.dumps([bridge._audit_log, bridge._log_buffer, response])
        for secret in (payload["resume_token"], FAKE_TYPED, FAKE_JS):
            self.assertNotIn(secret, everything)

    def test_resume_errors_carry_no_stored_values(self):
        self.replies["fill"] = [TIMEOUT]
        payload = self.batch({"action": "fill", "selector": "#q", "value": FAKE_TYPED, "tabId": 3})
        errors = [self.refused(payload, "observed_required"),
                  self.refused(payload, "observed_invalid", {"0": "maybe"}),
                  self.refused({**payload, "resume_token": "x"}, "resume_token_invalid", status=403)]
        for error in errors:
            text = json.dumps(error)
            self.assertNotIn(FAKE_TYPED, text)
            self.assertNotIn(payload["resume_token"], text)


class ResumeToolTests(unittest.TestCase):
    def test_tool_posts_to_the_bridge_and_outlasts_its_deadline(self):
        with patch.object(tools, "_post", return_value={"results": []}) as post:
            tools.browser_resume(7, "token", {"1": "not_applied"})
            tools.browser_resume(7, "token")
        first, second = post.call_args_list
        self.assertEqual(first.args, ("/api/resume", {"call": 7, "resume_token": "token",
                                                      "observed": {"1": "not_applied"}}))
        self.assertEqual(second.args[1], {"call": 7, "resume_token": "token"})
        self.assertGreater(first.kwargs["timeout"], waits.container_settle_seconds(waits.RESUME_DEADLINE_SECONDS))
        self.assertEqual(waits.RESUME_DEADLINE_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
