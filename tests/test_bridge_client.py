"""kitsune.bridge_client: the bridge connection the MCP server's tools use, without MCP code."""
import json
import os
import subprocess
import sys
import unittest

from kitsune.bridge_client import BridgeClient


def _run(code: str, env: dict | None = None) -> str:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("KITSUNE_")}
    return subprocess.run([sys.executable, "-c", code], env={**clean, **(env or {})},
                          capture_output=True, text=True, check=True).stdout


class BridgeClientTests(unittest.TestCase):
    def test_imports_no_mcp_code(self):
        loaded = _run("import json, sys, kitsune.bridge_client; "
                      "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'mcp')))")
        self.assertEqual(json.loads(loaded), [])

    def test_caps_have_no_default(self):
        # Requests without the caps header are not restricted to any group.
        with self.assertRaises(TypeError):
            BridgeClient()
        self.assertEqual(BridgeClient(caps={"tabs", "core"}).caps, "core,tabs")

    def test_mcp_server_client_takes_the_server_settings(self):
        code = ("import json; from kitsune import server; client = server._client; "
                "print(json.dumps([client.caps, client.mode, client.protect_user_tabs]))")
        for env, settings in (({}, ["core", "act", False]),
                              ({"KITSUNE_CAPS": "core,tabs", "KITSUNE_MODE": "draft",
                                "KITSUNE_PROTECT_USER_TABS": "1"}, ["core,tabs", "draft", True])):
            with self.subTest(env=env):
                self.assertEqual(json.loads(_run(code, env)), settings)


if __name__ == "__main__":
    unittest.main()
