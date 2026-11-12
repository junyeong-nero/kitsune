"""Code-first scripts stop safely; the existing direct dispatcher recovers just the held operation."""
import unittest

from kitsune import bridge, server
from test_elicitation_approval import ElicitationTestCase, answer_with


class CodeFirstRecoveryTests(ElicitationTestCase):
    ask = True

    def test_describe_does_not_execute_and_does_not_publish(self):
        for name in ("browser_click", "browser_scroll", "browser_cookies"):
            with self.subTest(name=name):
                result, payload = self.call("browser_tools_call", {"name": name})
                self.assertFalse(result.isError, payload)
                self.assertEqual(payload["name"], name)
                self.assertIn("inputSchema", payload)
        self.assertEqual(self.extension.sent, [])
        self.assertFalse(set(server.MINIMAL_TOOLS) & {"browser_click", "browser_scroll", "browser_cookies"})

    def test_script_then_direct_approval_never_replays_the_previous_fill(self):
        self.extension.url = "https://example.test/form"
        result, stopped = self.call("browser_run_python", {
            "code": "tab.fill('#name', 'Ada'); tab.click('#send'); tab.fill('#after', 'not yet')"})
        self.assertTrue(result.isError)
        self.assertEqual(stopped["code"], "script_operation_failed")
        self.assertEqual(stopped["failed_operation"]["recovery_tool"], "browser_click")
        self.assertEqual(len(self.extension.actions("fill")), 1)
        self.assertEqual(self.extension.actions("click"), [])
        self.assertNotIn("resume_token", stopped, "a whole script must never be checkpointed")
        self.assertNoToken()
        result, recovered = self.call("browser_tools_call", {
            "name": "browser_click", "arguments": {"target": "#send"}}, answer_with("accept", "Approve"))
        self.assertFalse(result.isError, recovered)
        self.assertEqual(len(self.extension.actions("click")), 1)
        self.assertEqual(len(self.extension.actions("fill")), 1)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("Tool: browser_click", self.prompts[0].message)
        self.assertNoToken()
        result, completed = self.call("browser_run_python", {"code": "tab.fill('#after', 'remaining')"})
        self.assertFalse(result.isError, completed)
        self.assertEqual([item["value"] for item in self.extension.actions("fill")], ["Ada", "remaining"])

    def test_empty_accept_or_decline_never_runs_the_recovery_action(self):
        for answer in (answer_with("accept"), answer_with("accept", "Decline"), answer_with("decline")):
            result, payload = self.call("browser_tools_call", {
                "name": "browser_click", "arguments": {"target": "#send"}}, answer)
            self.assertTrue(result.isError, payload)
            self.assertIn(payload["state"]["decision"], ("declined", "cancelled"))
            self.assertEqual(self.extension.actions("click"), [])
            self.assertNoToken()

    def test_non_eliciting_recovery_keeps_the_existing_explicit_token_flow(self):
        result, held = self.call("browser_tools_call", {
            "name": "browser_click", "arguments": {"target": "#send"}})
        self.assertTrue(result.isError)
        token = held["state"]["confirmToken"]
        self.assertIn(token, bridge._confirm_tokens)
        self.assertEqual(self.extension.actions("click"), [])
        result, done = self.call("browser_tools_call", {
            "name": "browser_click", "arguments": {"target": "#send", "confirm_token": token}})
        self.assertFalse(result.isError, done)
        self.assertEqual(len(self.extension.actions("click")), 1)

    def test_recovery_rejects_recursive_calls_and_validates_arguments(self):
        for name, arguments in (("browser_run_python", {"code": "pass"}),
                                ("browser_tools_call", {"name": "browser_click"}),
                                ("browser_tools_publish", {"names": ["browse"]}),
                                ("browser_click", {"not_an_argument": "#send"})):
            with self.subTest(name=name):
                result, _ = self.call_raw(name, arguments)
                self.assertTrue(result.isError)
        self.assertEqual(self.extension.actions("click"), [])

    def call_raw(self, name, arguments):
        # Validation errors are text, not Kitsune JSON results.
        import anyio
        from mcp.shared.memory import create_connected_server_and_client_session

        async def run():
            async with create_connected_server_and_client_session(server.mcp) as session:
                return await session.call_tool("browser_tools_call", {"name": name, "arguments": arguments})
        return anyio.run(run), None


if __name__ == "__main__":
    unittest.main()
