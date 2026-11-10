"""Opt-in registration and real MCP protocol calls, with only the bridge transport mocked."""
import json
import os
import subprocess
import sys
import unittest

PROBE = '''
import asyncio, json
from unittest.mock import patch
from mcp.shared.memory import create_connected_server_and_client_session
from kitsune import server

def request(method, route, **kwargs):
    if route == "/api/tabs":
        return {"tabs": [{"id": 7}]}
    if route == "/api/click":
        return {"error": "Write needs user help", "code": "needs_user", "call": 1,
                "state": {"reason": "user_active", "tabId": 7}}
    return {"ok": True}

async def main():
    with patch.object(server._client, "request", side_effect=request):
        async with create_connected_server_and_client_session(server.mcp) as session:
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            result = {"tools": names}
            if "browser_run_python" in names:
                result["schema"] = next(tool.inputSchema for tool in listed.tools if tool.name == "browser_run_python")
                ok = await session.call_tool("browser_run_python", {"code": "result = browser.tabs()"})
                failed = await session.call_tool("browser_run_python", {"code": "browser.tab(7).click('#send')"})
                invalid = await session.call_tool("browser_run_python", {"code": "pass", "timeout": 0})
                result.update(ok=json.loads(ok.content[0].text), failed=json.loads(failed.content[0].text),
                              statuses=[ok.isError, failed.isError, invalid.isError])
            return result
print(json.dumps(asyncio.run(main())))
'''


class ScriptMcpTests(unittest.TestCase):
    def probe(self, enabled, mode="act"):
        env = {**os.environ, "KITSUNE_SCRIPTING": "1" if enabled else "0", "KITSUNE_MODE": mode,
               "KITSUNE_CAPS": "core", "KITSUNE_PUBLISH": "core"}
        reply = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        return json.loads(reply.stdout)

    def test_local_code_execution_requires_explicit_opt_in(self):
        self.assertNotIn("browser_run_python", self.probe(False)["tools"])

    def test_mcp_reports_results_errors_and_never_checkpoints_the_whole_script(self):
        result = self.probe(True)
        self.assertEqual(result["statuses"], [False, True, True])
        self.assertEqual(result["ok"]["result"], [{"id": 7}])
        self.assertEqual(result["failed"]["code"], "script_operation_failed")
        self.assertNotIn("resume_token", result["failed"])
        self.assertEqual(set(result["schema"]["properties"]), {"code", "timeout"})

    def test_opted_in_restricted_mode_can_use_python_with_its_browser_client(self):
        for mode in ("read", "draft"):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(True, mode)["ok"]["result"], [{"id": 7}])


if __name__ == "__main__":
    unittest.main()
