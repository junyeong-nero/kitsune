"""#395: transport-authoritative permissions, prompt-free admission, containers and owner pools.

The real extension gate is exercised separately by user_tabs_read_only.cjs. This WebSocket
model isolates bridge metadata/admission and ensures that neither caller fields nor containers
can grant writes. No real browser/profile or OS input is used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from kitsune import bridge, playwright_tools, task_modes, tools
from test_bridge_security import Bridge, _request
from test_tab_inventory_bridge import tab
from test_tab_owners import OwningExtension


class PermissionExtension(OwningExtension):
    def __init__(self, *args):
        self.commands = []
        self.effects = []
        super().__init__(*args)

    def _answer(self, command):
        self.commands.append(command)
        action = command["action"]
        write = command.get("writeAction") if action == "checkTabWrite" else action
        if write in {"click", "fill", "scroll", "switchTab", "closeTab", "executeJS", "trustedClick",
                     "trustedType", "trustedPressKey", "trustedDrag", "uploadFile", "waitForResult", "fullPageScreenshot"}:
            target = next(t for t in self.tabs if t["id"] == command.get("tabId", 1))
            if not command.get("userTabsWritable") and target.get("owner") != command.get("ownerId"):
                return {"error": "read only", "code": "user_tab_read_only", "retryable": False,
                        "state": {"tabId": target["id"], "index": self.tabs.index(target), "action": write},
                        "hint": "Open your own tab with browser_tabs action=new."}
        if action == "checkTabWrite":
            return {"ok": True}
        if action in {"click", "fill", "scroll", "switchTab", "executeJS", "fullPageScreenshot"}:
            self.effects.append(command)
            return {"ok": True}
        if action == "closeTab":
            self.effects.append(command)
            self.tabs = [t for t in self.tabs if t["id"] != command["tabId"]]
            return {"ok": True}
        return super()._answer(command)


class BackgroundPermissionExtension(PermissionExtension):
    """Real WebSocket bridge commands dispatched by the actual background.js in its JS harness."""
    def __init__(self, *args):
        self.background = subprocess.Popen(["node", "-e", r"""
const { harness } = require('./tests/shared_browser.cjs');
const h = harness({ userTabsWritable: false });
let pending = Promise.resolve();
require('node:readline').createInterface({ input: process.stdin }).on('line', line => {
  pending = pending.then(async () => {
    h.sent.length = h.calls.length = 0;
    const result = await h.send({ ...JSON.parse(line), includeSnapshot: false });
    console.log(JSON.stringify({ result, sent: h.sent, calls: h.calls,
      shown: h.shown(), focusedWindow: h.focusedWindow }));
  }).catch(error => { console.error(error); process.exit(1); });
});
"""], cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.dispatched = []
        super().__init__(*args)

    def _answer(self, command):
        self.commands.append(command)
        self.background.stdin.write(json.dumps(command) + "\n")
        self.background.stdin.flush()
        if not select.select([self.background.stdout], [], [], 5)[0]:
            raise AssertionError("actual background did not answer")
        response = json.loads(self.background.stdout.readline())
        self.dispatched.append(response)
        return response["result"]

    def close(self):
        super().close()
        if self.background.poll() is None:
            self.background.terminate()
            self.background.wait(timeout=5)
        for stream in (self.background.stdin, self.background.stdout, self.background.stderr):
            stream.close()


class UserTabsReadOnlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name).start()
        self.addCleanup(self.bridge.stop)
        self.extension = PermissionExtension(self.bridge, [tab(1, "https://user.test/", active=True)])
        self.addCleanup(self.extension.close)

    def call(self, path, body=None, owner="client-a", headers=None):
        status, _, text = _request(self.bridge.http_port, "POST" if body is not None else "GET", path,
            {**self.bridge.auth(), task_modes.OWNER_HEADER: owner, "Content-Type": "application/json", **(headers or {})},
            self.bridge.host, json.dumps(body) if body is not None else None)
        self.assertIn(status, (200, 403), text)
        return json.loads(text)

    @unittest.skipUnless(shutil.which("node"), "actual background integration needs node")
    def test_direct_targets_and_keys_against_actual_background(self):
        self.extension.close()
        self.extension = BackgroundPermissionExtension(self.bridge, [])
        self.addCleanup(self.extension.close)
        for ask in (False, True):
            self.call("/api/set-policy", {"ask": ask})
            for tab_id in (0, False, True, "", [], [1, 2], {}, -1, 1.5, "0", "1.5", 2**53):
                for endpoint, extra in (
                    ("click", {}), ("move-tab", {"windowId": 1}),
                    ("trusted-click", {"modifiers": ["Meta", "Shift"], "clickCount": 2}),
                    ("trusted-click", {"modifiers": ["Control", "Alt"], "button": "middle"}),
                    ("trusted-press-key", {"keys": "Meta+V"}), ("trusted-type", {"text": "hello"}),
                    ("trusted-drag", {"to": "#end"})):
                    with self.subTest(ask=ask, tab_id=tab_id, endpoint=endpoint):
                        count = len(self.extension.commands)
                        result = self.call("/api/" + endpoint, {"tabId": tab_id, "selector": "#go", **extra})
                        self.assertEqual((result["code"], result["retryable"]), ("invalid_tab_id", False))
                        self.assertEqual(len(self.extension.commands), count, "refused before preflight/prompt/OS input")
        self.call("/api/set-policy", {"ask": False})
        self.assertEqual(self.call("/api/click", {"tabId": False},
            headers={task_modes.HOLD_VIEWED_TAB_HEADER: "0"})["code"], "invalid_tab_id")
        self.assertEqual(self.call("/api/click", {"tabId": "1", "selector": "#go"})["code"], "user_tab_read_only")
        self.assertEqual(self.extension.commands[-1]["tabId"], 1)
        self.assertEqual(self.extension.dispatched[-1]["sent"], [])
        self.assertEqual(self.call("/api/click", {"tabId": 999, "selector": "#go"})["code"], "tab_closed")
        self.assertEqual(self.extension.dispatched[-1]["sent"], [])
        self.assertEqual(self.extension.dispatched[-1]["calls"], [])
        own = self.call("/api/new-tab", {})["tabId"]
        self.assertTrue(self.call("/api/click", {"tabId": str(own), "selector": "#go"})["ok"])
        self.assertIn(["click", own], self.extension.dispatched[-1]["sent"])
        result = self.call("/api/batch", {"commands": [{"action": "click", "tabId": str(own), "selector": "#go"}]})
        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(self.extension.commands[-1]["tabId"], own)
        for ask in (False, True):
            self.call("/api/set-policy", {"ask": ask})
            for keys in ("Meta+1", "Control+Tab", "Shift+Control+Tab", "Tab", "Shift+Tab", "Meta+C Tab", "Meta+L"):
                result = self.call("/api/trusted-press-key", {"tabId": own, "selector": "#otp", "keys": keys})
                self.assertIn("error", result)
                self.assertNotEqual(result.get("code"), "needs_user")
                self.assertEqual(self.extension.commands[-1]["action"], "checkTabWrite")
                self.assertEqual(self.extension.dispatched[-1]["sent"], [])
                self.assertEqual(self.extension.dispatched[-1]["calls"], [])
            result = self.call("/api/trusted-type", {"tabId": own, "selector": "#otp", "text": "before\tafter"})
            self.assertEqual(result["code"], "key_denied")
            self.assertEqual(self.extension.commands[-1]["action"], "checkTabWrite")
            for value in (-1, "20", True, float("inf"), float("nan")):
                result = self.call("/api/trusted-click", {"tabId": own, "x": 10, "y": value})
                self.assertEqual(result["code"], "invalid_coordinates")
                self.assertEqual(self.extension.commands[-1]["action"], "checkTabWrite")

    @unittest.skipUnless(shutil.which("node"), "actual background integration needs node")
    def test_window_targets_against_actual_background(self):
        self.extension.close()
        self.extension = BackgroundPermissionExtension(self.bridge, [])
        self.addCleanup(self.extension.close)
        own = self.call("/api/new-tab", {})["tabId"]
        for ask in (False, True):
            self.call("/api/set-policy", {"ask": ask})
            for window_id in (-2, -1, 0, False, True, "", [], [1], {}, 1.5, "-2", "1.5", 2**53,
                              float("inf"), float("nan")):
                for endpoint in ("close-window", "focus-window", "move-tab", "resize-window", "detach-tab"):
                    with self.subTest(ask=ask, endpoint=endpoint, window_id=window_id):
                        before = len(self.extension.commands)
                        result = self.call("/api/" + endpoint,
                            {"windowId": window_id, "tabId": own, "width": 800, "height": 600})
                        self.assertEqual((result.get("code"), result.get("retryable")), ("invalid_window_id", False))
                        self.assertEqual(len(self.extension.commands), before, "invalid before prompt or dispatch")
            for endpoint in ("close-window", "focus-window"):
                for body in ({}, {"windowId": None}):
                    self.assertEqual(self.call("/api/" + endpoint, body)["code"], "invalid_window_id")
            for endpoint in ("close-window", "focus-window", "move-tab"):
                result = self.call("/api/" + endpoint, {"windowId": 999, "tabId": own})
                self.assertEqual((result.get("code"), result.get("retryable")), ("window_unavailable", False))
                self.assertEqual(self.extension.dispatched[-1]["calls"], [])
                self.assertEqual(self.extension.dispatched[-1]["sent"], [])
            for endpoint in ("close-window", "focus-window", "resize-window"):
                result = self.call("/api/" + endpoint,
                    {"windowId": "1", "tabId": own, "width": 800, "height": 600})
                self.assertEqual(result.get("code"), "user_tab_read_only")
                self.assertEqual(self.extension.dispatched[-1]["calls"], [])
                self.assertEqual(self.extension.dispatched[-1]["sent"], [])
        self.call("/api/set-policy", {"ask": False})
        self.assertEqual(self.call("/api/close-window", {"windowId": -2},
            headers={task_modes.HOLD_VIEWED_TAB_HEADER: "0"})["code"], "invalid_window_id")
        window = self.call("/api/create-window", {})
        wid, active = window["windowId"], window["tabIds"][0]
        for target in (str(wid), "1", None):
            result = self.call("/api/move-tab", {"tabId": own, "windowId": target})
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(self.extension.dispatched[-1]["calls"],
                [["tabs.move", own, {"windowId": int(target) if target else 1, "index": -1}]])
            self.assertEqual(self.extension.dispatched[-1]["shown"], [1])
            self.assertEqual(self.extension.dispatched[-1]["focusedWindow"], 1)
        for tab_id, owner in ((1, "client-a"), (own, "client-b")):
            for endpoint in ("move-tab", "detach-tab"):
                result = self.call("/api/" + endpoint, {"tabId": tab_id, "windowId": wid}, owner=owner)
                self.assertEqual(result.get("code"), "user_tab_read_only")
                self.assertEqual(self.extension.dispatched[-1]["calls"], [])
        for endpoint in ("move-tab", "detach-tab"):
            result = self.call("/api/" + endpoint, {"tabId": active, "windowId": 1})
            self.assertEqual(result.get("code"), "unsupported_target")
            self.assertEqual(self.extension.dispatched[-1]["calls"], [])
        self.assertTrue(self.call("/api/move-tab", {"tabId": active})["ok"], "same-window active reorder is safe")
        foreign = self.call("/api/new-tab", {}, owner="client-b")["tabId"]
        self.assertTrue(self.call("/api/move-tab", {"tabId": foreign, "windowId": wid}, owner="client-b")["ok"])
        for endpoint in ("close-window", "focus-window", "resize-window"):
            result = self.call("/api/" + endpoint, {"windowId": wid, "tabId": active, "width": 800, "height": 600})
            self.assertEqual(result.get("code"), "user_tab_read_only")
            self.assertEqual(result["state"]["tabId"], foreign)
            self.assertEqual(self.extension.dispatched[-1]["calls"], [])
        self.assertTrue(self.call("/api/move-tab", {"tabId": foreign, "windowId": 1}, owner="client-b")["ok"])
        for endpoint in ("resize-window", "focus-window", "close-window"):
            result = self.call("/api/" + endpoint, {"windowId": str(wid), "tabId": active, "width": 800, "height": 600})
            self.assertTrue(result.get("ok"), result)
            writes = self.extension.dispatched[-1]["calls"]
            self.assertEqual(writes[0][1], wid, "dispatch uses the canonical admitted window")
        result = self.call("/api/batch", {"commands": [{"action": "focusWindow", "windowId": -2}]})
        self.assertEqual(result["results"][0]["code"], "invalid_window_id")
        self.assertTrue(self.call("/api/detach-tab", {"tabId": own})["ok"], "inactive owned detach remains allowed")

    def test_two_clients_denied_without_confirmation_or_effects(self):
        a = self.call("/api/new-tab", {})["tabId"]
        b = self.call("/api/new-tab", {}, owner="client-b")["tabId"]
        self.call("/api/set-policy", {"ask": True})
        for tid, owner in [(1, "client-a"), (b, "client-a"), (a, "client-b")]:
            for endpoint in ("click", "fill", "scroll", "trusted-click", "trusted-type", "trusted-press-key",
                             "trusted-drag", "upload-file", "wait-for-result", "full-page-screenshot"):
                result = self.call("/api/" + endpoint, {"tabId": tid, "selector": "#pay",
                    "ownerId": "client-b", "userTabsWritable": True, "viewedTabApproved": True,
                    "holdViewedTab": False, "protectUserTabs": False, "confirmToken": "fake"}, owner=owner)
                self.assertEqual((result.get("code"), result.get("retryable")), ("user_tab_read_only", False), result)
                self.assertNotIn("confirmToken", result["state"])
        self.assertEqual(self.extension.effects, [])
        self.assertNotIn("describeTarget", self.extension.actions, "ownership is checked before ask-before-act inspection")
        self.call("/api/set-policy", {"ask": False})
        self.assertTrue(self.call("/api/click", {"tabId": a, "selector": "#go"})["ok"])
        self.assertTrue(self.call("/api/click", {"tabId": b, "selector": "#go"}, owner="client-b")["ok"])

    def test_caller_permission_fields_cannot_cross_transport_in_batches(self):
        command = {"action": "click", "tabId": 1, "selector": "#go", "ownerId": "forged",
                   "userTabsWritable": True, "holdViewedTab": False, "userTabApproved": True}
        result = self.call("/api/batch", {"commands": [command]})
        self.assertEqual(result["results"][0]["code"], "user_tab_read_only")
        result = self.call("/api/batch", {"commands": [{"action": "retry", "command": command, "maxAttempts": 5}]})
        self.assertEqual(result["results"][0]["attempts"], 1)
        for sent in self.extension.commands:
            self.assertEqual(sent.get("ownerId"), "client-a")
            self.assertNotIn("userTabsWritable", sent)
        self.assertEqual(self.extension.effects, [])
        self.call("/api/set-policy", {"ask": True})
        for action in ("if", "while"):
            result = self.call("/api/batch", {"commands": [{"action": action, "tabId": 1, "condition": "1"}]})
            self.assertEqual(result["results"][0]["code"], "user_tab_read_only")
        result = self.call("/api/sync-barrier", {"tabIds": [1], "predicate": "1"})
        self.assertEqual(result["code"], "user_tab_read_only")

    def test_explicit_legacy_headers_only_grant_compatibility_writes(self):
        for header in (task_modes.HOLD_VIEWED_TAB_HEADER, task_modes.PROTECT_USER_TABS_HEADER):
            result = self.call("/api/click", {"tabId": 1}, headers={header: "0"})
            self.assertTrue(result["ok"])
            self.assertIs(self.extension.commands[-1]["userTabsWritable"], True)
        self.assertEqual(self.call("/api/click", {"tabId": 1})["code"], "user_tab_read_only")

    def test_owned_tab_does_not_authorize_unbound_screen_click(self):
        a = self.call("/api/new-tab", {})["tabId"]
        result = self.call("/api/trusted-click", {"tabId": a, "coordinateSpace": "screen",
                                                 "x": 10, "y": 10, "focus": True})
        self.assertEqual((result["code"], result["retryable"]), ("unsupported_target", False))
        result = self.call("/api/trusted-click", {"tabId": 1, "coordinateSpace": "screen", "x": 10, "y": 10})
        self.assertEqual(result["code"], "user_tab_read_only")
        self.assertEqual(self.extension.effects, [])

    def test_concurrent_owner_pools_never_transfer_or_close_others_tabs(self):
        def create(owner):
            return self.call("/api/tab-pool", {"size": 2}, owner=owner)
        with ThreadPoolExecutor(2) as executor:
            a, b = list(executor.map(create, ["client-a", "client-b"]))
        self.assertFalse(set(a["available"]) & set(b["available"]))
        acquired_a = self.call("/api/pool-acquire", {}, owner="client-a")["tabId"]
        acquired_b = self.call("/api/pool-acquire", {}, owner="client-b")["tabId"]
        self.assertIn(acquired_a, a["available"])
        self.assertIn(acquired_b, b["available"])
        self.assertIn("error", self.call("/api/pool-release", {"tabId": acquired_a}, owner="client-b"))
        self.call("/api/pool-release", {"tabId": acquired_a})
        self.assertEqual(self.call("/api/tabs", {"mine": True})["tabs"], [])
        self.call("/api/tab-pool", {"size": 0})
        remaining = [t["id"] for t in self.call("/api/tabs")["tabs"]]
        self.assertTrue(set(b["available"]).issubset(remaining))
        self.assertFalse(set(a["available"]) & set(remaining))


class PermissionSeamTests(unittest.TestCase):
    def test_default_screen_click_never_touches_os_even_with_owned_tab_argument(self):
        token = bridge._user_tabs_writable.set(False)
        self.addCleanup(bridge._user_tabs_writable.reset, token)
        with patch.object(bridge, "_focus_browser_window") as focus, patch.object(bridge, "_native_click_screen") as click:
            result = asyncio.run(bridge.trusted_click({"tabId": 7, "coordinateSpace": "screen", "x": 1, "y": 2}))
        self.assertEqual(result["code"], "unsupported_target")
        focus.assert_not_called()
        click.assert_not_called()

    def test_find_load_more_refuses_before_any_snapshot_or_scroll(self):
        refusal = {"error": "read only", "code": "user_tab_read_only", "retryable": False}
        with patch.object(playwright_tools, "_current_tab", 7), patch.object(tools, "_post", return_value=refusal) as post:
            self.assertEqual(playwright_tools.browser_find(text="already present", load_more=True), refusal)
        post.assert_called_once_with("/api/tab-write-check", {"tabId": 7, "writeAction": "find", "loadMore": True})

    def test_workflow_stops_on_read_only_without_prompt_or_later_steps(self):
        refusal = {"error": "read only", "code": "user_tab_read_only", "retryable": False}
        with patch.object(bridge, "_run_step", new=AsyncMock(return_value=refusal)) as step:
            results = []
            asyncio.run(bridge._run_workflow("test", [{"action": "click"}, {"action": "fill"}], {}, results))
        self.assertEqual(results, [refusal])
        step.assert_awaited_once()

    def test_internal_hold_bypass_does_not_enable_compatibility(self):
        token = bridge._hold_viewed_tab.set(False)
        self.addCleanup(bridge._hold_viewed_tab.reset, token)
        self.assertFalse(bridge._user_tabs_writable.get())
