"""#398: one permission from config through MCP instructions, client headers and bridge context."""
import contextlib
from http.client import parse_headers
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import httpx

from kitsune import bridge, bridge_client, task_modes
from kitsune.bridge_client import BridgeClient


ALIASES = ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS")


def cases():
    yield {}, "read-only"
    for value in ("read-only", "writable", " WRITABLE ", " Read-Only "):
        yield {"KITSUNE_USER_TABS": value}, value.strip().lower()
    for name in ALIASES:
        for value in ("0", "false", "no", "off", " OFF "):
            yield {name: value}, "writable"
        for value in ("1", "true", "", "invalid"):
            yield {name: value}, "read-only"
        yield {name: "0", "KITSUNE_USER_TABS": "read-only"}, "read-only"
        yield {name: "1", "KITSUNE_USER_TABS": "writable"}, "writable"
    yield dict.fromkeys(ALIASES, "0"), "writable"
    yield {ALIASES[0]: "0", ALIASES[1]: "1"}, "writable"
    yield {ALIASES[0]: "1", ALIASES[1]: "0"}, "writable"
    yield {**dict.fromkeys(ALIASES, "0"), "KITSUNE_USER_TABS": "read-only"}, "read-only"


# Real MCP sessions in one server process: lifespan warnings are once per session, not process,
# initialize/list/call, request, or alias. All protocol output remains parseable JSON.
MCP_SESSIONS = r'''
import asyncio, json
from unittest.mock import Mock, patch
from mcp.shared.memory import create_connected_server_and_client_session
from kitsune import bridge_client, server

requests = []
def request(method, url, **kwargs):
    requests.append(kwargs["headers"])
    return Mock(status_code=200, json=Mock(return_value={"ok": True}))

async def main():
    with patch.object(bridge_client.httpx, "request", side_effect=request), \
         patch.object(server._client, "await_extension"):
        for _ in range(2):
            async with create_connected_server_and_client_session(server.mcp) as session:
                await session.list_tools()
                await session.list_tools()
                await session.call_tool("browser_snapshot", {})
                await session.call_tool("browser_snapshot", {})
    print(json.dumps({"permission": server._client.user_tabs,
                      "instructions": server.mcp._mcp_server.instructions, "headers": requests}))
asyncio.run(main())
'''


def run(script, env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("KITSUNE_")}
    return subprocess.run([sys.executable, "-c", script], env={**clean, **env},
                          text=True, capture_output=True)


def dispatched_permission(headers):
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.path = "/api/status"
    handler.headers = headers
    handler.drain_body = Mock()
    handler.refuse = Mock()
    permissions = []
    handler._dispatch({"/api/status": lambda: permissions.append(bridge._user_tabs_writable.get())})
    return handler, permissions


class UserTabSettingsTests(unittest.TestCase):
    def test_config_instructions_transport_and_session_warnings_agree(self):
        for env, expected in cases():
            with self.subTest(env=env):
                self.assertEqual(task_modes.user_tabs_from_env(env)[0], expected)
                result = run(MCP_SESSIONS, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["permission"], expected)
                self.assertEqual("User and other clients' tabs are read-only" in data["instructions"],
                                 expected == "read-only")
                self.assertNotIn("user_viewing", data["instructions"])
                warnings = [line for line in result.stderr.splitlines() if "Deprecated user-tab settings:" in line]
                self.assertEqual(len(warnings), 2 if any(name in env for name in ALIASES) else 0)
                for warning in warnings:
                    for name in ALIASES:
                        self.assertEqual(name in warning, name in env)
                self.assertGreaterEqual(len(data["headers"]), 4)
                for headers in data["headers"]:
                    self.assertEqual(headers[task_modes.USER_TABS_HEADER], expected)
                    self.assertNotIn("X-Kitsune-Hold-Viewed-Tab", headers)
                    self.assertNotIn("X-Kitsune-Protect-User-Tabs", headers)
                    handler, permissions = dispatched_permission(headers)
                    self.assertEqual(permissions, [expected == "writable"])
                    handler.refuse.assert_not_called()
                self.assertFalse(bridge._user_tabs_writable.get(), "request permission leaked")

    def test_invalid_explicit_config_never_falls_back_to_alias(self):
        for value in ("", "0", "1", "false", "true", "readonly", "write", " "):
            env = {"KITSUNE_USER_TABS": value, **dict.fromkeys(ALIASES, "0")}
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be read-only or writable"):
                    task_modes.user_tabs_from_env(env)
                result = run("from kitsune import server", env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("must be read-only or writable", result.stderr)
                handler, permissions = dispatched_permission({task_modes.USER_TABS_HEADER: value})
                self.assertEqual(permissions, [])
                self.assertEqual(handler.refuse.call_args.args[0], 400)
                self.assertEqual(handler.refuse.call_args.args[1]["code"], "invalid_user_tabs")

    def test_sdk_alias_precedence_and_one_warning_per_client_not_request(self):
        for options, expected, warned in (
            ({}, "read-only", False),
            ({"user_tabs": "writable"}, "writable", False),
            ({"user_tabs": " Read-Only "}, "read-only", False),
            ({"protect_user_tabs": False}, "writable", True),
            ({"hold_viewed_tab": False}, "writable", True),
            ({"hold_viewed_tab": True}, "read-only", True),
            ({"hold_viewed_tab": False, "protect_user_tabs": False}, "writable", True),
            ({"user_tabs": "read-only", "hold_viewed_tab": False}, "read-only", True),
            ({"user_tabs": "writable", "protect_user_tabs": True}, "writable", True),
        ):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(bridge_client.httpx, "request") as request:
                request.return_value.status_code = 200
                request.return_value.json.return_value = {"ok": True}
                client = BridgeClient(caps=None, **options)
                for _ in range(3):
                    client.request("GET", "/api/status", start_bridge=False)
                self.assertEqual(client.user_tabs, expected)
                self.assertEqual(stderr.getvalue().count("Deprecated user-tab settings:"), int(warned))
                self.assertEqual(request.call_args.kwargs["headers"][task_modes.USER_TABS_HEADER], expected)
        with self.assertRaises(ValueError):
            BridgeClient(caps=None, user_tabs="invalid", hold_viewed_tab=False)

    def test_permission_header_is_authoritative_over_extra_headers(self):
        def request(method, url, headers, **kwargs):
            # Exercise HTTPX's actual header representation and the HTTP server's parser,
            # not dictionary lookups that conceal case-variant duplicate wire headers.
            wire = httpx.Request(method, url, headers=headers).headers.raw
            parsed = parse_headers(io.BytesIO(b"\r\n".join(key + b": " + value for key, value in wire) + b"\r\n\r\n"))
            self.assertEqual(parsed.get_all(task_modes.USER_TABS_HEADER), [permission])
            handler, admitted = dispatched_permission(parsed)
            handler.refuse.assert_not_called()
            self.assertEqual(admitted, [permission == "writable"])
            return httpx.Response(200, json={"ok": True})

        spellings = (task_modes.USER_TABS_HEADER, "x-kitsune-user-tabs", "X-KITSUNE-USER-TABS", "x-KiTsUnE-uSeR-TaBs",
                     b"x-kitsune-user-tabs", b"X-KITSUNE-USER-TABS")
        for permission in ("read-only", "writable"):
            opposite = "writable" if permission == "read-only" else "read-only"
            for extra in [*({name: opposite} for name in spellings), dict.fromkeys(spellings, opposite)]:
                with self.subTest(permission=permission, extra=extra), \
                        patch.object(bridge_client.httpx, "request", side_effect=request):
                    client = BridgeClient(caps=None, user_tabs=permission, extra_headers=lambda: extra)
                    self.assertEqual(client.request("GET", "/api/status", start_bridge=False), {"ok": True})
                self.assertFalse(bridge._user_tabs_writable.get(), "permission escaped request scope")


if __name__ == "__main__":
    unittest.main()
