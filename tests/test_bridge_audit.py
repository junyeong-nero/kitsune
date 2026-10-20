"""Audit log entries keep what the agent did but never the text it typed."""
import asyncio
import json
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


if __name__ == "__main__":
    unittest.main()
