"""Audit log entries keep what the agent did but never the text it typed."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from kitsune import bridge

TYPED = "fixture-typed-value-0000"
REDACTED = bridge.AUDIT_REDACTED


class AuditRedactionTests(unittest.TestCase):
    def setUp(self):
        log = patch.object(bridge, "_audit_log", [])
        self.log = log.start()
        self.addCleanup(log.stop)

    def audited(self, action, params, ok=True, error=None, ms=12.7):
        bridge._audit(action, params, ok, error, ms)
        return self.log[-1]

    def test_typed_values_are_redacted_and_targets_kept(self):
        for action, params, typed_key in [
            ("type", {"selector": "#user", "text": TYPED, "clear": True, "tabId": 7}, "text"),
            ("fill", {"ref": "s1e2", "element": "Email box", "value": TYPED, "tabId": 7}, "value"),
            ("setEditableContent", {"selector": "#doc", "value": TYPED, "format": "text", "clear": False}, "value"),
            ("keypress", {"key": "Enter", "selector": "#user", "modifiers": {"shift": True}, "text": TYPED}, "text"),
        ]:
            with self.subTest(action=action):
                entry = self.audited(action, params, ok=False, error="Element not found")
                self.assertEqual(entry["params"], {**params, typed_key: REDACTED})
                self.assertEqual((entry["action"], entry["ok"], entry["error"], entry["ms"]),
                                 (action, False, "Element not found", 12))
                self.assertEqual(params[typed_key], TYPED)  # caller's body is not mutated

    def test_form_fill_keeps_field_keys_and_redacts_every_value(self):
        fields = {"#email": TYPED, "Password": TYPED + "-2", "#remember": True}
        entry = self.audited("formFill", {"fields": fields, "tabId": 3})
        self.assertEqual(entry["params"], {
            "fields": {"#email": REDACTED, "Password": REDACTED, "#remember": REDACTED},
            "tabId": 3,
        })
        self.assertEqual(self.audited("formFill", {"fields": TYPED})["params"], {"fields": REDACTED})

    def test_stored_secrets_are_redacted_and_identifiers_kept(self):
        for action, params, secret_key in [
            ("cookies", {"op": "set", "url": "https://example.test/", "name": "sid",
                         "value": TYPED, "domain": "example.test", "path": "/", "secure": True}, "value"),
            ("storageOp", {"kind": "local", "op": "set", "key": "token", "value": TYPED, "tabId": 5}, "value"),
            ("storageOp", {"kind": "local", "op": "restore", "value": {"token": TYPED}, "tabId": 5}, "value"),
            ("clipboard", {"op": "write", "text": TYPED}, "text"),
        ]:
            with self.subTest(action=action, op=params["op"]):
                entry = self.audited(action, params)
                self.assertEqual(entry["params"], {**params, secret_key: REDACTED})
                self.assertNotIn(TYPED, json.dumps(entry))

    def test_nested_commands_get_the_same_rule(self):
        params = {
            "commands": [
                {"action": "fill", "selector": "#a", "value": TYPED},
                {"action": "executeJS", "code": "secretFixture()", "tabId": 1},
            ],
            "sequences": [[{"action": "type", "selector": "#b", "text": TYPED, "tabId": 2}]],
            "command": {"action": "formFill", "fields": {"#c": TYPED}},
            "do": [{"action": "keypress", "key": "a", "text": TYPED}],
            "catch": [{"action": "click", "selector": "#retry"}],
            "then": [
                {"action": "cookies", "op": "set", "name": "sid", "value": TYPED},
                {"action": "storageOp", "op": "set", "key": "token", "value": TYPED},
                {"action": "clipboard", "op": "write", "text": TYPED},
            ],
        }
        entry = self.audited("batch", params)
        self.assertNotIn(TYPED, json.dumps(entry))
        self.assertNotIn("secretFixture", json.dumps(entry))
        self.assertEqual(entry["params"], {
            "commands": [
                {"action": "fill", "selector": "#a", "value": REDACTED},
                {"action": "executeJS", "tabId": 1},
            ],
            "sequences": [[{"action": "type", "selector": "#b", "text": REDACTED, "tabId": 2}]],
            "command": {"action": "formFill", "fields": {"#c": REDACTED}},
            "do": [{"action": "keypress", "key": "a", "text": REDACTED}],
            "catch": [{"action": "click", "selector": "#retry"}],
            "then": [
                {"action": "cookies", "op": "set", "name": "sid", "value": REDACTED},
                {"action": "storageOp", "op": "set", "key": "token", "value": REDACTED},
                {"action": "clipboard", "op": "write", "text": REDACTED},
            ],
        })

    def test_other_actions_are_unchanged_except_js_bodies(self):
        for action, params in [
            ("click", {"selector": "#go", "text": "Go", "tabId": 4}),
            ("captureNetwork", {"action": "start", "tabId": 4}),
            ("intercept", {"op": "add", "patterns": ["analytics\\.example\\.test"], "effect": "block"}),
            ("cookies", {"op": "get", "url": "https://example.test/", "name": "sid"}),
            ("uploadFile", {"paths": ["/tmp/fixture.txt"], "selector": "#file", "mode": "auto"}),
        ]:
            with self.subTest(action=action):
                self.assertEqual(self.audited(action, params)["params"], params)
        self.assertEqual(self.audited("executeJS", {"code": "1", "tabId": 4})["params"], {"tabId": 4})


class AuditEndpointTests(unittest.TestCase):
    def handler(self, body, command="POST"):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.command = command
        handler.read_body = Mock(return_value=body)
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        return handler

    def test_api_audit_never_returns_typed_value_but_extension_receives_it(self):
        body = {"fields": {"#email": TYPED}, "tabId": 9}
        with patch.object(bridge, "_audit_log", []), \
             patch.object(bridge, "_check_policy", return_value=None), \
             patch.object(bridge, "_invalidate_cache_for_action"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock,
                          return_value={"ok": True}) as send:
            self.handler(body)._forward("formFill")
            send.assert_awaited_once_with("formFill", {"fields": {"#email": TYPED}, "tabId": 9}, timeout=15)

            audit = self.handler({}, command="GET")
            audit.handle_audit()
        status, payload = audit.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertNotIn(TYPED, json.dumps(payload))
        [entry] = payload["entries"]
        self.assertEqual((entry["action"], entry["ok"], entry["params"]),
                         ("formFill", True, {"fields": {"#email": REDACTED}, "tabId": 9}))


class ContainerAuditTests(unittest.TestCase):
    """Commands run inside batch, parallel, broadcast, retry and workflows are audited."""

    def setUp(self):
        self.sent = []
        self.replies = {}
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
        self.sent.append((action, dict(params or {})))
        replies = self.replies.get(action)
        return replies.pop(0) if replies else {"ok": True}

    def call(self, route, body):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.read_body = Mock(return_value=dict(body))
        handler.send_json = Mock()
        handler.run_async = Mock(side_effect=lambda coro, **kwargs: asyncio.run(coro))
        route(handler)
        return handler.send_json.call_args.args

    def entries(self):
        return [(e["action"], e.get("parent", {}).get("path"), e["ok"], e["error"]) for e in bridge._audit_log]

    def test_batch_records_nested_commands_with_parent_and_redaction(self):
        status, _ = self.call(lambda h: h.handle_batch(), {"commands": [
            {"action": "type", "selector": "#user", "text": TYPED, "tabId": 4},
            {"action": "sequence", "do": [
                {"action": "fill", "ref": "s1e2", "value": TYPED + "-2", "tabId": 4},
                {"action": "js", "code": "secretFixture()", "tabId": 4},
            ]},
        ]})
        self.assertEqual(status, 200)
        batch0 = {"action": "batch", "index": 0}
        batch1 = {"action": "batch", "index": 1}
        self.assertEqual(self.entries(), [
            ("type", [batch0], True, None),
            ("fill", [batch1, {"action": "sequence", "index": 0}], True, None),
            ("executeJS", [batch1, {"action": "sequence", "index": 1}], True, None),
            ("sequence", [batch1], True, None),
        ])
        self.assertEqual(len({e["parent"]["call"] for e in bridge._audit_log}), 1)
        self.assertEqual(bridge._audit_log[0]["params"], {"selector": "#user", "text": REDACTED, "tabId": 4})
        self.assertEqual(bridge._audit_log[2]["params"], {"tabId": 4})
        self.assertNotIn(TYPED, json.dumps(bridge._audit_log))
        self.assertNotIn("secretFixture", json.dumps(bridge._audit_log))
        # The extension still receives the typed values.
        self.assertIn(("type", {"selector": "#user", "text": TYPED, "tabId": 4}), self.sent)

    def test_parallel_sequences_record_sequence_and_index(self):
        self.call(lambda h: h.handle_batch(), {"commands": [{"action": "parallel", "sequences": [
            [{"action": "click", "selector": "#a", "tabId": 1},
             {"action": "type", "selector": "#q", "text": TYPED, "tabId": 1}],
            [{"action": "fill", "selector": "#b", "value": TYPED, "tabId": 2}],
        ]}]})
        batch0 = {"action": "batch", "index": 0}
        paths = {(e["action"], json.dumps(e["parent"]["path"])) for e in bridge._audit_log}
        self.assertEqual(paths, {
            ("click", json.dumps([batch0, {"action": "parallel", "sequence": 0, "index": 0}])),
            ("type", json.dumps([batch0, {"action": "parallel", "sequence": 0, "index": 1}])),
            ("fill", json.dumps([batch0, {"action": "parallel", "sequence": 1, "index": 0}])),
            ("parallel", json.dumps([batch0])),
        })
        self.assertEqual(bridge._audit_log[-1]["action"], "parallel")
        self.assertNotIn(TYPED, json.dumps(bridge._audit_log))

    def test_broadcast_and_retry_endpoints_record_each_step_under_a_new_call(self):
        self.call(lambda h: h.handle_broadcast(),
                  {"tabIds": [5, 6], "command": {"action": "fill", "selector": "#q", "value": TYPED}})
        self.replies["click"] = [{"error": "Element not found"}]
        self.call(lambda h: h.handle_retry(),
                  {"command": {"action": "click", "selector": "#go", "tabId": 5}, "maxAttempts": 3, "backoffMs": 0})
        broadcast = [e for e in bridge._audit_log if e["action"] == "fill"]
        retry = [e for e in bridge._audit_log if e["action"] == "click"]
        self.assertEqual(sorted(e["parent"]["path"][0]["tabId"] for e in broadcast), [5, 6])
        self.assertEqual({e["parent"]["path"][0]["action"] for e in broadcast}, {"broadcast"})
        self.assertEqual([(e["parent"]["path"], e["ok"], e["error"]) for e in retry], [
            ([{"action": "retry", "attempt": 1}], False, "Element not found"),
            ([{"action": "retry", "attempt": 2}], True, None),
        ])
        calls = {e["parent"]["call"] for e in broadcast}
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({e["parent"]["call"] for e in retry}), 1)
        self.assertNotIn(retry[0]["parent"]["call"], calls)
        self.assertNotIn(TYPED, json.dumps(bridge._audit_log))

    def test_workflow_records_if_while_and_try_steps(self):
        workflow = {"action": "if", "condition": "fixtureCondition()", "tabId": 3, "then": [
            {"action": "while", "condition": "more()", "tabId": 3, "maxIterations": 3, "do": [
                {"action": "type", "selector": "#q", "text": TYPED, "tabId": 3}]},
            {"action": "try", "do": [{"action": "click", "selector": "#missing", "tabId": 3}],
             "catch": [{"action": "keypress", "key": "Escape", "text": TYPED, "tabId": 3}]},
        ]}
        self.replies["executeJS"] = [{"result": True}, {"result": True}, {"result": False}]
        self.replies["click"] = [{"error": "Element not found"}]
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "fixture.md"), "w", encoding="utf-8") as f:
                f.write("```json\n" + json.dumps(workflow) + "\n```\n")
            with patch.object(bridge, "WORKFLOW_DIR", tmp):
                status, result = self.call(lambda h: h.handle_workflow(), {"name": "fixture"})
        self.assertEqual(status, 200, result)
        wf = {"action": "workflow", "name": "fixture", "index": 0}
        then0 = {"action": "if", "branch": "then", "index": 0}
        then1 = {"action": "if", "branch": "then", "index": 1}
        self.assertEqual(self.entries(), [
            ("type", [wf, then0, {"action": "while", "iteration": 0, "index": 0}], True, None),
            ("while", [wf, then0], True, None),
            ("click", [wf, then1, {"action": "try", "block": "do", "index": 0}], False, "Element not found"),
            ("keypress", [wf, then1, {"action": "try", "block": "catch", "index": 0}], True, None),
            ("try", [wf, then1], False, "Element not found"),
            ("if", [wf], True, None),
        ])
        self.assertNotIn(TYPED, json.dumps(bridge._audit_log))

    def test_held_step_in_container_is_recorded_once_with_parent(self):
        bridge._policy["ask"] = True
        _, payload = self.call(lambda h: h.handle_batch(), {"commands": [
            {"action": "parallel", "sequences": [[
                {"action": "cookies", "op": "set", "name": "sid", "value": TYPED},
                {"action": "click", "selector": "#after", "tabId": 1},
            ]]},
        ]})
        [[[held]]] = payload["results"]
        self.assertEqual(held["code"], "confirmation_required")
        self.assertEqual(self.sent, [], "nothing ran")
        step = [{"action": "batch", "index": 0}, {"action": "parallel", "sequence": 0, "index": 0}]
        self.assertEqual(self.entries(), [
            ("cookies", step, False, "confirmation_required"),
            ("parallel", [{"action": "batch", "index": 0}], True, None),
        ])
        params = bridge._audit_log[0]["params"]
        self.assertEqual((params["confirmation"], params["reason"], params["value"]), ("required", "cookies", REDACTED))
        self.assertNotIn(held["state"]["confirmToken"], json.dumps(bridge._audit_log))

    def test_direct_calls_have_no_parent_and_the_log_stays_bounded(self):
        self.call(lambda h: h._forward("click"), {"selector": "#go"})
        self.assertNotIn("parent", bridge._audit_log[-1])
        with patch.object(bridge, "_AUDIT_MAX", 5):
            self.call(lambda h: h.handle_batch(),
                      {"commands": [{"action": "click", "selector": f"#c{i}"} for i in range(8)]})
        self.assertEqual(len(bridge._audit_log), 5)
        self.assertEqual([e["parent"]["path"][0]["index"] for e in bridge._audit_log], [3, 4, 5, 6, 7])


if __name__ == "__main__":
    unittest.main()
