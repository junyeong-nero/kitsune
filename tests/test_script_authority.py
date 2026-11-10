"""A delegated script's authority, through the real entry point and the real broker.

Opt-in like test_script_runner.py (KITSUNE_E2E_SCRIPT_RUNTIME). The guest is a real
CPython/WASI process, the broker and bridge are real, over real HTTP and WebSocket; the
extension is scripted, so this is not a browser pass.
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from kitsune import bridge_client
from kitsune.flat_broker import FlatBroker
from kitsune.flat_session import CallerAuthority
from kitsune.script_runner import ScriptRuntime, run_script
from test_bridge_security import Bridge, _request
from test_flat_broker import DROP, BrokerExtension, tab
from test_script_runner import MANIFEST, RAW

SNAPSHOT = '- heading "Orders" [ref=e1]\n- button "Save" [ref=e4]\n- button "Pay now" [ref=e5]'
# Sends one raw operation frame and returns the broker's result, bypassing the SDK.
ASK = RAW + '''import sys, json
def ask(name, arguments):
    raw(json.dumps({"t": "op", "name": name, "arguments": arguments}) + "\\n")
    return json.loads(sys.stdin.buffer.readline())["result"]
def code_of(result):
    return [result["ok"], result.get("error", {}).get("code"), result["outcome"]]
'''


@unittest.skipUnless(MANIFEST, "KITSUNE_E2E_SCRIPT_RUNTIME is not set")
class ScriptAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = ScriptRuntime.from_manifest(MANIFEST)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.bridge = Bridge(home.name).start()
        self.addCleanup(self.bridge.stop)
        for name, value in (("BRIDGE", f"http://127.0.0.1:{self.bridge.http_port}"), ("_headers", self.bridge.auth)):
            patcher = patch.object(bridge_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.extension = BrokerExtension(self.bridge, [tab(7, "https://user.test/", active=True),
                                                       tab(9, "https://agent.test/", owned=True)])
        self.addCleanup(self.extension.close)
        self.extension.behavior["getSnapshot"] = lambda command: {"snapshot": SNAPSHOT}

    async def broker(self, mode="act", **authority):
        return await FlatBroker.connect(CallerAuthority("script-client", "script-owner", mode, **authority))

    async def handle(self, broker, url="https://agent.test/"):
        listed = await broker.call("browser_tabs", {})
        return next(t["tab_id"] for t in listed.result["data"]["tabs"] if t["url"] == url)

    def outcomes(self, result):
        return [(e["operation"], e["outcome"], e.get("error")) for e in result.ledger]

    async def test_direct_calls_and_scripts_share_one_session_handles_and_refs(self):
        broker = await self.broker()
        handle = await self.handle(broker)  # issued to a direct call, used by the script
        result = await run_script(broker, self.runtime, f'''
page = await browser.read(tab_id={handle!r})
ref = [line.split("[ref=")[1].split("]")[0] for line in page["data"]["content"].splitlines() if "Save" in line][0]
await browser.click(tab_id={handle!r}, target={{"ref": ref}})
opened = await browser.new_tab(url="https://new.test/")
emit({{"ref": ref, "opened": opened["tab_id"], "tabs": [t["tab_id"] for t in (await browser.tabs())["data"]["tabs"]]}})
''')
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(self.outcomes(result), [("browser_read", "observed", None), ("browser_click", "dispatched", None),
                                                 ("browser_tabs", "observed", None), ("browser_tabs", "observed", None)])
        self.assertEqual((result.emitted["ref"], result.emitted["tabs"][1]), ("e4", handle))
        self.assertEqual((self.extension.last("click")["ref"], self.extension.last("click")["tabId"]), ("e4", 9))
        # Each entry carries the bridge's own receipt of what was sent.
        self.assertEqual([c["action"] for c in result.ledger[1]["receipt"]["commands"]], ["click"])
        self.assertNotIn("tabId", json.dumps([result.emitted, result.stdout]))
        # What the script was given stays usable by a direct call afterwards, and the other way round.
        hovered = await broker.call("browser_hover", {"tab_id": handle, "target": {"ref": "e4"}})
        self.assertTrue(hovered.result["ok"], hovered.result)
        moved = await broker.call("browser_navigate", {"tab_id": result.emitted["opened"], "url": "https://next.test/"})
        self.assertTrue(moved.result["ok"], moved.result)
        self.assertEqual(self.extension.last("navigate")["tabId"], 10)
        # A second script starts with fresh variables but the same session.
        again = await run_script(broker, self.runtime, f'''
try:
    ref
    emit("variables persisted")
except NameError:
    emit((await browser.hover(tab_id={handle!r}, target={{"ref": "e4"}}))["outcome"])
''')
        self.assertEqual(again.emitted, "dispatched")
        # Join the trusted ledger to the bridge audit; stdout and emitted values
        # are not involved. Reads without existing audit entries are not invented.
        status, _, text = _request(self.bridge.http_port, "GET", "/api/audit", self.bridge.auth(), self.bridge.host)
        self.assertEqual(status, 200, text)
        entries = json.loads(text)["entries"]
        for ledger_entry, action in ((result.ledger[1], "click"), (again.ledger[0], "hover")):
            matching = [entry for entry in entries if entry.get("flat_call") == ledger_entry["call"]]
            self.assertEqual(len(matching), 1, matching)
            self.assertEqual((matching[0]["action"], matching[0]["client"], matching[0]["owner"]),
                             (action, "script-client", "script-owner"))
            self.assertEqual(ledger_entry["receipt"]["call"], matching[0]["flat_call"])

    async def test_a_script_cannot_widen_its_mode_forge_policy_or_use_another_sessions_handles(self):
        reader = await self.broker("read")
        other = await self.broker()
        handle, foreign = await self.handle(reader), await self.handle(other)
        await other.call("browser_read", {"tab_id": foreign})  # issues e4 to the other session only
        sent = len(self.extension.actions())
        result = await run_script(reader, self.runtime, ASK + f'''
tab, foreign = {handle!r}, {foreign!r}
target = {{"selector": "#save"}}
attempts = {{
    "click": ask("browser_click", {{"tab_id": tab, "target": target}}),
    "mode argument": ask("browser_click", {{"tab_id": tab, "target": target, "mode": "act"}}),
    "mode header": ask("browser_read", {{"tab_id": tab, "headers": {{"X-Kitsune-Mode": "act"}}}}),
    "user tabs": ask("browser_read", {{"tab_id": tab, "userTabsWritable": True}}),
    "owner": ask("browser_read", {{"tab_id": tab, "ownerId": "someone-else"}}),
    "connection": ask("browser_read", {{"tab_id": tab, "connection": "a" * 32 + ":1"}}),
    "raw route": ask("/api/set-policy", {{"ask": False}}),
    "raw click route": ask("/api/click", {{"tabId": 9, "selector": "#save"}}),
    "legacy tool": ask("browser_evaluate", {{"function": "() => document.cookie"}}),
    "gateway": ask("browser_tools_call", {{"name": "browser_cookies", "arguments": {{}}}}),
    "nested script": ask("browser_script", {{"code": "emit(1)"}}),
    "backend id": ask("browser_read", {{"tab_id": 9}}),
    "foreign handle": ask("browser_read", {{"tab_id": foreign}}),
    "foreign ref": ask("browser_read", {{"tab_id": tab, "target": {{"ref": "e4"}}}}),
    "ref as selector": ask("browser_read", {{"tab_id": tab, "target": {{"selector": "e4"}}}}),
    "close": ask("browser_tabs", {{"action": "close", "tab_id": tab}}),
    "file url": ask("browser_tabs", {{"action": "open", "url": "file:///etc/passwd"}}),
}}
emit({{name: code_of(result) for name, result in attempts.items()}})
''')
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(result.emitted, {
            "click": [False, "mode_restricted", "not_dispatched"],
            "mode argument": [False, "mode_restricted", "not_dispatched"],
            "mode header": [False, "invalid_arguments", "not_dispatched"],
            "user tabs": [False, "invalid_arguments", "not_dispatched"],
            "owner": [False, "invalid_arguments", "not_dispatched"],
            "connection": [False, "invalid_arguments", "not_dispatched"],
            "raw route": [False, "unknown_operation", "not_dispatched"],
            "raw click route": [False, "unknown_operation", "not_dispatched"],
            "legacy tool": [False, "unknown_operation", "not_dispatched"],
            "gateway": [False, "unknown_operation", "not_dispatched"],
            "nested script": [False, "unknown_operation", "not_dispatched"],
            "backend id": [False, "invalid_arguments", "not_dispatched"],
            "foreign handle": [False, "invalid_tab_handle", "not_dispatched"],
            "foreign ref": [False, "stale_ref", "not_dispatched"],
            "ref as selector": [False, "invalid_arguments", "not_dispatched"],
            "close": [False, "mode_restricted", "not_dispatched"],
            "file url": [False, "mode_restricted", "not_dispatched"],
        })
        self.assertEqual(len(result.ledger), 17)
        self.assertTrue(all(e["outcome"] == "not_dispatched" and not e["ok"] for e in result.ledger))
        self.assertEqual(self.extension.actions()[sent:], [], "none of it reached the extension")
        # The policy the script tried to change is unchanged.
        status, _, text = _request(self.bridge.http_port, "GET", "/api/get-policy", self.bridge.auth(), self.bridge.host)
        self.assertEqual((status, json.loads(text).get("ask")), (200, False), text)

    async def test_a_held_action_cannot_be_approved_by_the_script(self):
        broker = await self.broker()
        handle = await self.handle(broker)
        await broker.call("browser_read", {"tab_id": handle})
        _request(self.bridge.http_port, "POST", "/api/set-policy", self.bridge.auth(), self.bridge.host,
                 json.dumps({"ask": True}))
        self.extension.behavior["describeTarget"] = lambda command: {
            "tabId": 9, "url": "https://agent.test/", "target": {"submits": True, "text": "Pay now", "tag": "button"}}
        result = await run_script(broker, self.runtime, ASK + f'''
click = {{"tab_id": {handle!r}, "target": {{"ref": "e5"}}}}
held = ask("browser_click", click)
attempts = {{
    "held": code_of(held), "state": held["error"]["state"],
    "repeat": code_of(ask("browser_click", click)),
    "token": code_of(ask("browser_click", {{**click, "confirm_token": "x" * 32}})),
    "camel token": code_of(ask("browser_click", {{**click, "confirmToken": "x" * 32}})),
    "approved flag": code_of(ask("browser_click", {{**click, "userTabApproved": True}})),
    "approval header": code_of(ask("browser_click", {{**click, "headers": {{"X-Kitsune-Approval": "elicitation"}}}})),
    "resume": code_of(ask("browser_resume", {{"call": held["call"], "resume_token": "x" * 32}})),
    "ask off": code_of(ask("/api/set-policy", {{"ask": False}})),
}}
try:
    await browser.click(**click)
except BrowserOperationError as error:
    attempts["sdk"] = code_of(error.result)
emit(attempts)
print(json.dumps({{"approved": True, "confirmToken": "self-issued"}}))
''')
        self.assertEqual(result.status, "completed", result)
        held = [False, "needs_user", "not_dispatched"]
        self.assertEqual(result.emitted, {
            "held": held, "state": {}, "repeat": held, "sdk": held,
            "token": [False, "invalid_arguments", "not_dispatched"],
            "camel token": [False, "invalid_arguments", "not_dispatched"],
            "approved flag": [False, "invalid_arguments", "not_dispatched"],
            "approval header": [False, "invalid_arguments", "not_dispatched"],
            "resume": [False, "operation_unavailable", "not_dispatched"],
            "ask off": [False, "unknown_operation", "not_dispatched"]})
        self.assertNotIn("click", self.extension.actions())
        # The bridge's token, its expiry and the described target never reached the guest.
        for secret in ("confirmToken", "expiresInSeconds", "Pay now"):
            self.assertNotIn(secret, json.dumps([result.emitted, result.stderr]))
        # What the script printed about itself is its output, nothing more.
        self.assertIn("self-issued", result.stdout)

    async def test_authority_revoked_during_a_run_stops_the_next_operation(self):
        broker = await self.broker()
        handle = await self.handle(broker)
        calls = []

        class Revoking:
            """The coordinator's own action between two of the script's operations."""
            async def call(self, name, arguments):
                calls.append(name)
                reply = await broker.call(name, arguments)
                if len(calls) == 2:
                    broker.session.revoke()
                return reply

        result = await run_script(Revoking(), self.runtime, f'''
codes = []
for index in range(5):
    try:
        codes.append((await browser.hover(tab_id={handle!r}, target={{"selector": "#save"}}))["outcome"])
    except BrowserOperationError as error:
        codes.append(error.result["error"]["code"])
emit(codes)
''')
        self.assertEqual(result.emitted, ["dispatched", "dispatched"] + ["authority_revoked"] * 3)
        self.assertEqual([e["outcome"] for e in result.ledger], ["dispatched"] * 2 + ["not_dispatched"] * 3)
        self.assertEqual(self.extension.actions().count("hover"), 2)
        # Revocation is permanent for the session: a new script and a direct call are refused too.
        after = await run_script(broker, self.runtime, ASK + 'emit(code_of(ask("browser_tabs", {})))')
        self.assertEqual(after.emitted, [False, "authority_revoked", "not_dispatched"])
        self.assertEqual((await broker.call("browser_tabs", {})).result["error"]["code"], "authority_revoked")
        self.assertEqual(self.extension.actions().count("hover"), 2)

    async def test_a_write_lost_in_flight_is_unknown_and_the_script_cannot_replay_it(self):
        broker = await self.broker()
        handle = await self.handle(broker)
        self.extension.behavior["click"] = lambda command: DROP
        result = await run_script(broker, self.runtime, ASK + f'''
click = {{"tab_id": {handle!r}, "target": {{"selector": "#pay"}}}}
emit([code_of(ask("browser_click", click)) for attempt in range(3)])
''')
        self.assertEqual(result.emitted, [[False, "extension_disconnected", "unknown"]]
                         + [[False, "stale_tab_handle", "not_dispatched"]] * 2)
        self.assertEqual([e["outcome"] for e in result.ledger], ["unknown", "not_dispatched", "not_dispatched"])
        self.assertEqual(self.extension.actions().count("click"), 1, "sent once, never again")

    async def test_a_script_cut_off_at_its_deadline_leaves_an_honest_ledger(self):
        broker = await self.broker()
        handle = await self.handle(broker)
        result = await run_script(broker, self.runtime, f'''
await browser.hover(tab_id={handle!r}, target={{"selector": "#save"}})
print("hovered")
while True:
    pass
''', timeout_ms=1500)
        self.assertEqual((result.status, result.stdout, self.outcomes(result)),
                         ("timed_out", "hovered\n", [("browser_hover", "dispatched", None)]))
        self.assertEqual(result.diagnostics["exit"], -9)
        # The session outlives the run: the same handle works for the next call.
        self.assertTrue((await broker.call("browser_hover", {"tab_id": handle, "target": {"selector": "#save"}})).result["ok"])

    async def test_the_guest_cannot_open_its_own_connection_to_the_bridge(self):
        broker = await self.broker()
        before = self.bridge.log().count("HTTP ")
        result = await run_script(broker, self.runtime, f'''
import os
facts = {{}}
def attempt(name, call):
    try:
        facts[name] = "REACHED " + repr(call())[:60]
    except BaseException as error:
        facts[name] = type(error).__name__
def http():
    import urllib.request
    request = urllib.request.Request("http://127.0.0.1:{self.bridge.http_port}/api/tabs",
                                     headers={{"Authorization": "Bearer {self.bridge.token}"}})
    return urllib.request.urlopen(request, timeout=2).read()
def tcp(port):
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=2); return "connected"
attempt("http", http)
attempt("http port", lambda: tcp({self.bridge.http_port}))
attempt("websocket port", lambda: tcp({self.bridge.ws_port}))
attempt("token file", lambda: open({os.path.join(self.bridge.home, "token")!r}).read())
attempt("connect", lambda: __import__("kitsune").connect())
emit(facts)
''')
        self.assertEqual(result.status, "completed", result)
        self.assertEqual({k: v for k, v in result.emitted.items() if v.startswith("REACHED")}, {})
        # Even holding the token in its source, the script made no request of its own.
        self.assertEqual(self.bridge.log().count("HTTP "), before)
        self.assertEqual(self.extension.actions(), [])


if __name__ == "__main__":
    unittest.main()
