"""#396: real Python native paths + actual background authorization, with OS APIs mocked.

No browser/profile or OS input is used. The single-worker executor and regrant hooks
reproduce the reviewer's gaps; the narrowed policy must refuse before reaching either.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import select
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from kitsune import bridge


@unittest.skipUnless(shutil.which("node"), "Node is required for background/bridge integration")
class HandoffNativePolicyTests(unittest.TestCase):
    def setUp(self):
        self.background = subprocess.Popen(
            ["node", "tests/tab_handoff_native.cjs", "--bridge"],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.addCleanup(self.close_background)
        self.target = self.request({"op": "setup"})
        self.commands = []
        self.after_point = None
        token = bridge._handoff_identity.set({"sessionId": "session-a"})
        self.addCleanup(bridge._handoff_identity.reset, token)
        writable = bridge._user_tabs_writable.set(False)
        self.addCleanup(bridge._user_tabs_writable.reset, writable)
        mocks = {
            "_mac_click_backend": "quartz", "_mac_drag_unavailable": None,
            "_mac_keyboard_unavailable": None, "_focus_check_click": (True, {"ok": True}),
            "_focus_check_drag": (True, {"ok": True}), "_focus_browser_window": True,
            "_mac_window_at": (42, "Firefox", (0, 0, 1920, 1080)), "_mac_frontmost_pid": 42,
            "_mac_post_char": None, "_mac_post_key": None, "_mac_post_chord": None,
        }
        self.os = {}
        for name, value in mocks.items():
            target = patch.object(bridge, name, return_value=value)
            self.os[name] = target.start()
            self.addCleanup(target.stop)
        for target in [patch.object(bridge.sys, "platform", "darwin"),
                       patch.object(bridge, "TRUSTED_CLICK_RESTORE_DELAY", 0),
                       patch.object(bridge, "send_to_extension", side_effect=self.extension)]:
            target.start()
            self.addCleanup(target.stop)

    def request(self, message):
        self.background.stdin.write(json.dumps(message) + "\n")
        self.background.stdin.flush()
        self.assertTrue(select.select([self.background.stdout], [], [], 5)[0], "background reply timed out")
        line = self.background.stdout.readline()
        self.assertTrue(line, "background driver exited")
        result = json.loads(line)
        self.assertNotIn("testError", result)
        return result

    def close_background(self):
        self.background.stdin.close()
        try:
            self.background.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.background.kill()
            self.background.wait()
        self.background.stdout.close()
        self.background.stderr.close()

    async def extension(self, action, params=None, timeout=30):
        self.commands.append(action)
        result = self.request({"op": "command", "command": {**(params or {}), "action": action,
                               "userTabsWritable": bridge._user_tabs_writable.get()}})
        if action == "screenPoint" and "error" not in result and self.after_point:
            self.after_point()
        return result

    def params(self, **extra):
        return {"tabId": self.target["tabId"], "selector": "#field", "focus": True,
                "to": "#end", "text": "a", "keys": "a", "delayMs": 0, **extra}

    def assert_denied(self, result):
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("code"), "unsupported_target", result)
        self.assertFalse(result["retryable"])
        self.assertEqual(result["state"]["reason"], "handoff_native_input")
        self.assertIn("DOM", result["hint"])
        self.assertIn("new own tab", result["hint"])

    def assert_no_preparation(self):
        state = self.request({"op": "state"})
        self.assertEqual(state, {"sent": [], "calls": [], "shown": [1], "focusedWindow": 1})
        for name in ("_focus_check_click", "_focus_check_drag", "_focus_browser_window",
                     "_mac_post_char", "_mac_post_key", "_mac_post_chord"):
            self.os[name].assert_not_called()

    def test_all_native_variants_refuse_before_preparation_even_writable(self):
        for writable in (False, True):
            bridge._user_tabs_writable.set(writable)
            for function, extra in (
                (bridge.trusted_click, {}), (bridge.trusted_click, {"clickCount": 2}),
                (bridge.trusted_click, {"holdMs": 600}),
                (bridge.trusted_click, {"restorePointer": False}),  # hover-menu pointer retention
                (bridge.trusted_click, {"selector": "", "x": 1, "y": 2}),
                (bridge.trusted_click, {"selector": "e1"}),  # ref resolved by background
                (bridge.trusted_drag, {}), (bridge.trusted_type, {}), (bridge.trusted_press_key, {}),
            ):
                with self.subTest(writable=writable, function=function.__name__, extra=extra):
                    self.assert_denied(asyncio.run(function(self.params(**extra))))
                    self.assert_no_preparation()
        self.assertEqual(set(self.commands), {"screenPoint"})

    def test_bridge_admission_never_skips_native_provenance_for_writable(self):
        for writable in (False, True):
            bridge._user_tabs_writable.set(writable)
            for action in ("trustedClick", "trustedDrag", "trustedType", "trustedPressKey",
                           "screenPoint", "agentInput", "focusState"):
                with self.subTest(writable=writable, action=action):
                    self.assert_denied(asyncio.run(bridge._tab_write_refusal(action, self.params())))
        self.assertEqual(set(self.commands), {"checkTabWrite"})
        self.assert_no_preparation()

    def test_queued_executor_revocation_never_schedules_handoff_native_work(self):
        async def run():
            executor = ThreadPoolExecutor(max_workers=1)
            release, entered = threading.Event(), threading.Event()
            def occupy():
                entered.set()
                release.wait(5)
            executor.submit(occupy)
            self.assertTrue(entered.wait(2))
            asyncio.get_running_loop().set_default_executor(executor)
            submitted = asyncio.Event()
            original = executor.submit
            def submit(*args, **kwargs):
                submitted.set()
                return original(*args, **kwargs)
            task = waiter = None
            try:
                with patch.object(executor, "submit", side_effect=submit) as schedule:
                    task = asyncio.create_task(bridge.trusted_click(self.params()))
                    waiter = asyncio.create_task(submitted.wait())
                    done, _ = await asyncio.wait([task, waiter], timeout=2, return_when=asyncio.FIRST_COMPLETED)
                    self.assertTrue(done, "neither refusal nor executor scheduling completed")
                    # The old implementation queued input here; revoke before releasing its worker.
                    self.request({"op": "revoke"})
                    release.set()
                    result = await asyncio.wait_for(task, 2)
                    schedule.assert_not_called()
                    self.assert_denied(result)
                    self.assert_no_preparation()
            finally:
                release.set()
                if waiter:
                    waiter.cancel()
                if task and not task.done():
                    task.cancel()
                executor.shutdown(wait=True)
        asyncio.run(run())

    def test_revoke_regrant_between_preparations_cannot_authorize_native_input(self):
        for function in (bridge.trusted_click, bridge.trusted_drag, bridge.trusted_type):
            regrants = []
            def regrant():
                regrants.append(True)
                self.after_point = None
                self.request({"op": "regrant"})
            self.after_point = regrant
            with self.subTest(function=function.__name__):
                result = asyncio.run(function(self.params()))
                self.assertEqual(regrants, [], "handoff preparation must never return a point to reauthorize")
                self.assert_denied(result)
                self.assert_no_preparation()
        # A new grant still has no native capability, but remains useful for DOM work.
        self.request({"op": "regrant"})
        self.assert_denied(asyncio.run(bridge.trusted_click(self.params())))
        self.assertTrue(asyncio.run(self.extension("click", self.params()))["ok"])

    def test_original_agent_created_tab_retains_native_click_drag_and_keyboard(self):
        for writable in (False, True):
            bridge._user_tabs_writable.set(writable)
            for function, extra in (
                (bridge.trusted_click, {}),
                (bridge.trusted_click, {"selector": "", "x": 1, "y": 2, "coordinateSpace": "viewport"}),
                (bridge.trusted_drag, {}), (bridge.trusted_type, {}), (bridge.trusted_press_key, {}),
            ):
                with self.subTest(writable=writable, function=function.__name__, extra=extra):
                    result = asyncio.run(function(self.params(tabId=self.target["ownTabId"], **extra)))
                    self.assertTrue(result.get("ok"), result)
        self.os["_focus_check_click"].assert_called()
        self.os["_focus_check_drag"].assert_called()
        self.os["_mac_post_char"].assert_called()
        self.os["_mac_post_chord"].assert_called()
        self.assertEqual(self.request({"op": "state"})["shown"], [1])

    def test_writable_raw_screen_and_malformed_aliases_refuse_even_with_owned_tab_id(self):
        bridge._user_tabs_writable.set(True)
        for coordinates in ({"coordinateSpace": "screen"}, {"space": "screen"},
                            {"coordinateSpace": "viewport", "space": "screen"},
                            {"coordinateSpace": "SCREEN"}, {"coordinateSpace": None},
                            {"coordinateSpace": {}}, {"space": ["screen"]}):
            with self.subTest(coordinates=coordinates):
                params = self.params(tabId=self.target["ownTabId"], selector="", x=10, y=20, **coordinates)
                result = asyncio.run(bridge.trusted_click(params))
                self.assertEqual(result.get("code"), "unsupported_target", result)
                self.assertFalse(result["retryable"])
                self.assert_no_preparation()
        self.assertEqual(self.commands, [], "raw coordinates fail before extension preparation too")

    def test_unowned_tab_keeps_read_only_error(self):
        self.request({"op": "revoke"})
        result = asyncio.run(bridge.trusted_click(self.params()))
        self.assertEqual(result.get("code"), "user_tab_read_only", result)
        self.assert_no_preparation()
