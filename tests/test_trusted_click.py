"""Cross-platform trusted click and browser focus, with every OS call mocked."""
import asyncio
import os
import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from kitsune import bridge, tools


USER_POINTER = (300.5, -40.0)


def fake_quartz():
    def set_field(event, field, value):
        assert field == 1, "only kCGMouseEventClickState is set"
        event["clickState"] = value

    return SimpleNamespace(
        kCGEventMouseMoved=5, kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2,
        kCGEventRightMouseDown=3, kCGEventRightMouseUp=4, kCGEventOtherMouseDown=25, kCGEventOtherMouseUp=26,
        kCGMouseButtonLeft=0, kCGMouseButtonRight=1, kCGMouseButtonCenter=2, kCGHIDEventTap=0,
        kCGMouseEventClickState=1,
        kCGEventFlagMaskShift=0x20000, kCGEventFlagMaskControl=0x40000,
        kCGEventFlagMaskAlternate=0x80000, kCGEventFlagMaskCommand=0x100000,
        CGEventCreate=Mock(return_value="current"),
        CGEventGetLocation=Mock(return_value=USER_POINTER),
        CGEventCreateMouseEvent=Mock(side_effect=lambda src, kind, point, button: {"kind": kind}),
        CGEventSetIntegerValueField=Mock(side_effect=set_field),
        CGEventSetFlags=Mock(side_effect=lambda event, flags: event.update(flags=flags)),
        CGEventPost=Mock(),
        CGWarpMouseCursorPosition=Mock(return_value=0),
        CGAssociateMouseAndMouseCursorPosition=Mock(),
    )


def posted(quartz):
    """The Quartz events the click posted, in order, with the fields it set on them."""
    assert all(c.args[0] == quartz.kCGHIDEventTap for c in quartz.CGEventPost.call_args_list)
    return [c.args[1] for c in quartz.CGEventPost.call_args_list]


def windows_user32(cursor=(3, 4), read_ok=1):
    def get_cursor_pos(ref):
        ref._obj.x, ref._obj.y = cursor
        return read_ok
    return Mock(GetCursorPos=Mock(side_effect=get_cursor_pos), SetCursorPos=Mock(return_value=1))


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class NativeClickTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(bridge.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def mac(self, trusted=True):
        return patch.object(bridge.sys, "platform", "darwin"), \
            patch.object(bridge, "_mac_accessibility_trusted", return_value=trusted)

    def windows(self, user32):
        return patch.object(bridge.sys, "platform", "win32"), \
            patch.object(bridge.ctypes, "windll", SimpleNamespace(user32=user32), create=True)

    def test_windows_click_uses_user32_and_restores_the_pointer(self):
        user32 = windows_user32(cursor=(-30, 4))
        user32.attach_mock(self.sleep, "sleep")
        platform, windll = self.windows(user32)
        with platform, windll:
            result = bridge._native_click_screen(10.4, 19.6)
        self.assertEqual(result, {"ok": True, "backend": "win32", "pointerRestored": True})
        self.assertEqual(user32.SetCursorPos.call_args_list, [call(10, 20), call(-30, 4)])
        self.assertEqual(user32.mouse_event.call_args_list,
                         [call(0x0002, 0, 0, 0, 0), call(0x0004, 0, 0, 0, 0)])
        names = [name for name, *_ in user32.mock_calls]
        self.assertEqual(names, ["GetCursorPos", "SetCursorPos", "sleep", "mouse_event", "sleep",
                                 "mouse_event", "sleep", "SetCursorPos"])
        self.assertEqual(user32.mock_calls[-2], call.sleep(bridge.POINTER_RESTORE_DELAY))

    def test_windows_without_restore_leaves_the_pointer(self):
        user32 = windows_user32()
        platform, windll = self.windows(user32)
        with platform, windll:
            result = bridge._native_click_screen(10, 20, restore_pointer=False)
        self.assertEqual(result, {"ok": True, "backend": "win32", "pointerRestored": False})
        user32.GetCursorPos.assert_not_called()
        user32.SetCursorPos.assert_called_once_with(10, 20)

    def test_windows_restore_failures_keep_the_click(self):
        unread = windows_user32(read_ok=0)
        platform, windll = self.windows(unread)
        with platform, windll:
            result = bridge._native_click_screen(10, 20)
        self.assertEqual(result, {"ok": True, "backend": "win32", "pointerRestored": False,
                                  "pointerRestoreError": "could not read the pointer position: GetCursorPos failed"})
        unread.SetCursorPos.assert_called_once_with(10, 20)
        self.assertEqual(len(unread.mouse_event.call_args_list), 2)

        unmoved = windows_user32()
        unmoved.SetCursorPos.side_effect = [1, 0]
        platform, windll = self.windows(unmoved)
        with platform, windll:
            result = bridge._native_click_screen(10, 20)
        self.assertEqual(result, {"ok": True, "backend": "win32", "pointerRestored": False,
                                  "pointerRestoreError": "could not move the pointer back: SetCursorPos failed"})

    def test_windows_buttons_double_click_and_modifiers(self):
        # Issue #306: the button's DOWN/UP flags, two pairs for a double click, modifier keys
        # pressed around the click and released in reverse order; the pointer still goes back.
        cases = (
            ("right", 1, (), [call.mouse_event(0x0008, 0, 0, 0, 0), call.sleep(0.03),
                              call.mouse_event(0x0010, 0, 0, 0, 0)]),
            ("middle", 1, (), [call.mouse_event(0x0020, 0, 0, 0, 0), call.sleep(0.03),
                               call.mouse_event(0x0040, 0, 0, 0, 0)]),
            ("left", 2, (), [call.mouse_event(0x0002, 0, 0, 0, 0), call.sleep(0.03),
                             call.mouse_event(0x0004, 0, 0, 0, 0), call.sleep(bridge.DOUBLE_CLICK_GAP),
                             call.mouse_event(0x0002, 0, 0, 0, 0), call.sleep(0.03),
                             call.mouse_event(0x0004, 0, 0, 0, 0)]),
            ("left", 1, ("Shift", "Meta"), [
                call.keybd_event(0x10, 0, 0, 0), call.keybd_event(0x5B, 0, 0, 0),
                call.mouse_event(0x0002, 0, 0, 0, 0), call.sleep(0.03), call.mouse_event(0x0004, 0, 0, 0, 0),
                call.keybd_event(0x5B, 0, 0x0002, 0), call.keybd_event(0x10, 0, 0x0002, 0)]),
            ("right", 2, ("Control", "Alt"), [
                call.keybd_event(0x11, 0, 0, 0), call.keybd_event(0x12, 0, 0, 0),
                call.mouse_event(0x0008, 0, 0, 0, 0), call.sleep(0.03), call.mouse_event(0x0010, 0, 0, 0, 0),
                call.sleep(bridge.DOUBLE_CLICK_GAP),
                call.mouse_event(0x0008, 0, 0, 0, 0), call.sleep(0.03), call.mouse_event(0x0010, 0, 0, 0, 0),
                call.keybd_event(0x12, 0, 0x0002, 0), call.keybd_event(0x11, 0, 0x0002, 0)]),
        )
        for button, count, modifiers, clicked in cases:
            with self.subTest(button=button, count=count, modifiers=modifiers):
                self.sleep.reset_mock()
                user32 = windows_user32(cursor=(-30, 4))
                user32.attach_mock(self.sleep, "sleep")
                platform, windll = self.windows(user32)
                with platform, windll:
                    result = bridge._native_click_screen(10, 20, True, button, count, modifiers)
                self.assertEqual(result, {"ok": True, "backend": "win32", "pointerRestored": True})
                self.assertEqual(user32.mock_calls[2:], [
                    call.sleep(0.05), *clicked, call.sleep(bridge.POINTER_RESTORE_DELAY), call.SetCursorPos(-30, 4)])

    def test_windows_releases_modifiers_when_the_click_fails(self):
        user32 = windows_user32()
        user32.mouse_event.side_effect = OSError("blocked")
        platform, windll = self.windows(user32)
        with platform, windll, self.assertRaises(OSError):
            bridge._native_click_screen(10, 20, True, "left", 1, ("Shift",))
        self.assertEqual(user32.keybd_event.call_args_list, [call(0x10, 0, 0, 0), call(0x10, 0, 0x0002, 0)])

    def test_macos_prefers_quartz_events(self):
        quartz = fake_quartz()
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}), \
             patch.object(bridge.shutil, "which") as which, \
             patch.object(bridge.subprocess, "run") as run:
            result = bridge._native_click_screen(10, 20.5)
        self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": True})
        self.assertEqual(quartz.CGEventCreateMouseEvent.call_args_list,
                         [call(None, kind, (10.0, 20.5), 0) for kind in (5, 1, 2)])
        self.assertEqual(posted(quartz), [{"kind": 5}, {"kind": 1, "clickState": 1}, {"kind": 2, "clickState": 1}])
        which.assert_not_called()
        run.assert_not_called()

    def test_quartz_reads_the_pointer_first_and_warps_it_back_after_the_click(self):
        quartz = fake_quartz()
        order = Mock()
        for name in ("CGEventGetLocation", "CGEventPost", "CGWarpMouseCursorPosition",
                     "CGAssociateMouseAndMouseCursorPosition"):
            order.attach_mock(getattr(quartz, name), name)
        order.attach_mock(self.sleep, "sleep")
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}):
            bridge._native_click_screen(10, 20)
        quartz.CGEventCreate.assert_called_once_with(None)
        self.assertEqual([name for name, *_ in order.mock_calls], [
            "CGEventGetLocation", "CGEventPost", "sleep", "CGEventPost", "sleep", "CGEventPost",
            "sleep", "CGWarpMouseCursorPosition", "CGAssociateMouseAndMouseCursorPosition"])
        self.assertEqual(order.mock_calls[-3], call.sleep(bridge.POINTER_RESTORE_DELAY))
        quartz.CGWarpMouseCursorPosition.assert_called_once_with(USER_POINTER)
        quartz.CGAssociateMouseAndMouseCursorPosition.assert_called_once_with(True)

    def test_quartz_without_restore_leaves_the_pointer(self):
        quartz = fake_quartz()
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}):
            result = bridge._native_click_screen(10, 20, restore_pointer=False)
        self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": False})
        quartz.CGEventCreate.assert_not_called()
        quartz.CGWarpMouseCursorPosition.assert_not_called()
        self.assertEqual(len(quartz.CGEventPost.call_args_list), 3)

    def test_quartz_restore_failures_keep_the_click(self):
        cases = (
            ("CGEventCreate", {"return_value": None},
             "could not read the pointer position: CGEventCreate returned no event"),
            ("CGEventGetLocation", {"side_effect": ValueError("no session")},
             "could not read the pointer position: no session"),
            ("CGWarpMouseCursorPosition", {"return_value": 1001},
             "could not move the pointer back: CGWarpMouseCursorPosition returned 1001"),
        )
        for name, behavior, error in cases:
            with self.subTest(name=name):
                quartz = fake_quartz()
                getattr(quartz, name).configure_mock(**behavior)
                platform, trusted = self.mac()
                with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}):
                    result = bridge._native_click_screen(10, 20)
                self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": False,
                                          "pointerRestoreError": error})
                self.assertEqual(len(quartz.CGEventPost.call_args_list), 3)
                quartz.CGAssociateMouseAndMouseCursorPosition.assert_not_called()

    def test_quartz_buttons_double_click_and_modifiers(self):
        # Issue #306: right/other mouse event types and buttons, click state 1 then 2 for a
        # double click, and the modifiers as event flags (no key events are posted).
        shift_cmd = 0x20000 | 0x100000
        cases = (
            ("right", 1, (), 1, [{"kind": 3, "clickState": 1}, {"kind": 4, "clickState": 1}]),
            ("middle", 1, (), 2, [{"kind": 25, "clickState": 1}, {"kind": 26, "clickState": 1}]),
            ("left", 2, (), 0, [{"kind": 1, "clickState": 1}, {"kind": 2, "clickState": 1},
                                {"kind": 1, "clickState": 2}, {"kind": 2, "clickState": 2}]),
            ("left", 1, ("Shift", "Meta"), 0, [{"kind": 1, "clickState": 1, "flags": shift_cmd},
                                               {"kind": 2, "clickState": 1, "flags": shift_cmd}]),
            ("right", 2, ("Control", "Alt"), 1, [
                {"kind": kind, "clickState": state, "flags": 0x40000 | 0x80000}
                for state in (1, 2) for kind in (3, 4)]),
        )
        for button, count, modifiers, mouse_button, events in cases:
            with self.subTest(button=button, count=count, modifiers=modifiers):
                quartz = fake_quartz()
                order = Mock()
                for name in ("CGEventPost", "CGWarpMouseCursorPosition"):
                    order.attach_mock(getattr(quartz, name), name)
                order.attach_mock(self.sleep, "sleep")
                platform, trusted = self.mac()
                with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}):
                    result = bridge._native_click_screen(10, 20, True, button, count, modifiers)
                self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": True})
                self.assertEqual(posted(quartz), [{"kind": 5}, *events])
                self.assertEqual({c.args[3] for c in quartz.CGEventCreateMouseEvent.call_args_list}, {mouse_button})
                sleeps = [c for c in order.mock_calls if c[0] == "sleep"]
                expected = [call.sleep(0.05), call.sleep(0.03)]
                if count == 2:
                    expected += [call.sleep(bridge.DOUBLE_CLICK_GAP), call.sleep(0.03)]
                self.assertEqual(sleeps, expected + [call.sleep(bridge.POINTER_RESTORE_DELAY)])
                self.assertEqual(order.mock_calls[-1][0], "CGWarpMouseCursorPosition")
                if not modifiers:
                    quartz.CGEventSetFlags.assert_not_called()

    @contextmanager
    def cliclick(self, *results):
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": None}), \
             patch.object(bridge.shutil, "which", return_value="/opt/homebrew/bin/cliclick"), \
             patch.object(bridge.subprocess, "run", side_effect=list(results)) as run:
            yield run

    def test_macos_falls_back_to_cliclick(self):
        with self.cliclick(completed("-1440,25\n"), completed()) as run:
            run.attach_mock(self.sleep, "sleep")
            result = bridge._native_click_screen(-5.2, 20)
        self.assertEqual(result, {"ok": True, "backend": "cliclick", "pointerRestored": True})
        # The position is printed by the same run as the click, and restored after it.
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ["/opt/homebrew/bin/cliclick", "p", "c:=-5,20"],
            ["/opt/homebrew/bin/cliclick", "m:=-1440,25"],
        ])
        self.assertEqual([name for name, *_ in run.mock_calls], ["", "sleep", ""])

    def test_cliclick_without_restore_only_clicks(self):
        with self.cliclick(completed()) as run:
            result = bridge._native_click_screen(5, 20, restore_pointer=False)
        self.assertEqual(result, {"ok": True, "backend": "cliclick", "pointerRestored": False})
        self.assertEqual(run.call_args_list, [call(["/opt/homebrew/bin/cliclick", "c:5,20"],
                                                   capture_output=True, text=True, timeout=10)])

    def test_cliclick_restore_failures_keep_the_click(self):
        with self.cliclick(completed("\n")) as run:
            result = bridge._native_click_screen(5, 20)
        self.assertEqual(result, {"ok": True, "backend": "cliclick", "pointerRestored": False,
                                  "pointerRestoreError": "could not read the pointer position from cliclick output ''"})
        run.assert_called_once()

        for move, error in ((completed(returncode=1, stderr="nope\n"), "nope"),
                            (subprocess.TimeoutExpired("cliclick", 10), "timed out")):
            with self.subTest(error=error):
                with self.cliclick(completed("12,34\n"), move):
                    result = bridge._native_click_screen(5, 20)
                self.assertEqual((result["ok"], result["pointerRestored"]), (True, False))
                self.assertIn("could not move the pointer back: ", result["pointerRestoreError"])
                self.assertIn(error, result["pointerRestoreError"])

    def test_cliclick_buttons_double_click_and_modifiers(self):
        # Issue #306: rc: and dc:, and kd:/ku: around the click; "p" still comes first.
        for button, count, modifiers, commands in (
            ("right", 1, (), ["rc:5,20"]),
            ("left", 2, (), ["dc:5,20"]),
            ("left", 1, ("Shift", "Control", "Alt", "Meta"),
             ["kd:shift,ctrl,alt,cmd", "c:5,20", "ku:shift,ctrl,alt,cmd"]),
        ):
            with self.subTest(button=button, count=count, modifiers=modifiers):
                with self.cliclick(completed("-1440,25\n"), completed()) as run:
                    result = bridge._native_click_screen(5, 20, True, button, count, modifiers)
                self.assertEqual(result, {"ok": True, "backend": "cliclick", "pointerRestored": True})
                self.assertEqual([c.args[0] for c in run.call_args_list], [
                    ["/opt/homebrew/bin/cliclick", "p", *commands],
                    ["/opt/homebrew/bin/cliclick", "m:=-1440,25"],
                ])
        with self.cliclick(completed()) as run:
            bridge._native_click_screen(5, 20, False, "right", 1, ("Meta",))
        self.assertEqual(run.call_args.args[0], ["/opt/homebrew/bin/cliclick", "kd:cmd", "rc:5,20", "ku:cmd"])

    def test_cliclick_refuses_what_it_cannot_click(self):
        # Never a left click instead: middle and right double clicks need Quartz.
        for button, count, what in (("middle", 1, "middle-click"), ("middle", 2, "middle-click"),
                                    ("right", 2, "double-click with the right button")):
            with self.subTest(button=button, count=count), self.cliclick() as run:
                result = bridge._native_click_screen(5, 20, True, button, count, ("Shift",))
            self.assertEqual(set(result), {"error"})
            self.assertIn(f"cannot {what} with cliclick", result["error"])
            self.assertIn("kitsune-browser-mcp[macos]", result["error"])
            run.assert_not_called()

    def test_cliclick_failure_releases_held_modifiers(self):
        for failure, error in ((completed(returncode=1, stderr="boom\n"), "cliclick failed: boom"),
                               (subprocess.TimeoutExpired("cliclick", 10), "cliclick failed: ")):
            with self.subTest(error=error), self.cliclick(failure, completed()) as run:
                result = bridge._native_click_screen(5, 20, True, "left", 1, ("Shift", "Meta"))
            self.assertEqual(set(result), {"error"})
            self.assertTrue(result["error"].startswith(error), result)
            self.assertEqual(run.call_args.args[0], ["/opt/homebrew/bin/cliclick", "ku:shift,cmd"])
            self.assertEqual(run.call_count, 2)

    def test_macos_reports_cliclick_failure(self):
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": None}), \
             patch.object(bridge.shutil, "which", return_value="/usr/local/bin/cliclick"), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "", "boom\n")):
            result = bridge._native_click_screen(1, 2)
        self.assertEqual(result, {"error": "cliclick failed: boom"})

    def test_macos_without_backend_explains_dependencies(self):
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": None}), \
             patch.object(bridge.shutil, "which", return_value=None):
            result = bridge._native_click_screen(1, 2)
        for text in ("kitsune-browser-mcp[macos]", "cliclick", "Accessibility",
                     "kitsune-bridge stop", "MCP server that started it"):
            self.assertIn(text, result["error"])

    def test_macos_without_accessibility_does_not_click(self):
        quartz = fake_quartz()
        platform, trusted = self.mac(trusted=False)
        with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}), \
             patch.object(bridge.subprocess, "run") as run:
            result = bridge._native_click_screen(1, 2)
        self.assertIn("Accessibility permission", result["error"])
        quartz.CGEventPost.assert_not_called()
        run.assert_not_called()

    def test_accessibility_check_uses_ax_is_process_trusted(self):
        lib = SimpleNamespace(AXIsProcessTrusted=Mock(return_value=0))
        with patch.object(bridge.ctypes.cdll, "LoadLibrary", return_value=lib):
            self.assertIs(bridge._mac_accessibility_trusted(), False)
        with patch.object(bridge.ctypes.cdll, "LoadLibrary", side_effect=OSError("missing")):
            self.assertIsNone(bridge._mac_accessibility_trusted())

    def linux(self):
        return patch.object(bridge.sys, "platform", "linux"), \
            patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

    def test_linux_clicks_with_xdotool(self):
        platform, env = self.linux()
        with platform, env, patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = bridge._native_click_screen(10.4, 19.6)
        self.assertEqual(result, {"ok": True, "backend": "xdotool", "pointerRestored": True})
        # One xdotool command: the move back follows the click.
        self.assertEqual(run.call_args.args[0],
                         ["/usr/bin/xdotool", "mousemove", "--sync", "10", "20", "click", "1",
                          "mousemove", "restore"])
        run.assert_called_once()

    def test_linux_without_restore_leaves_the_pointer(self):
        platform, env = self.linux()
        with platform, env, patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
             patch.object(bridge.subprocess, "run", return_value=completed()) as run:
            result = bridge._native_click_screen(10, 20, restore_pointer=False)
        self.assertEqual(result, {"ok": True, "backend": "xdotool", "pointerRestored": False})
        self.assertEqual(run.call_args.args[0],
                         ["/usr/bin/xdotool", "mousemove", "--sync", "10", "20", "click", "1"])

    def test_linux_buttons_double_click_and_modifiers(self):
        # Issue #306: click 3/2, --repeat 2, keydown/keyup around the click, all in one command
        # that still ends with the move back.
        platform, env = self.linux()
        for button, count, modifiers, clicked in (
            ("right", 1, (), ["click", "3"]),
            ("middle", 1, (), ["click", "2"]),
            ("left", 2, (), ["click", "--repeat", "2", "1"]),
            ("right", 2, ("Shift", "Control", "Alt", "Meta"),
             ["keydown", "shift+ctrl+alt+super", "click", "--repeat", "2", "3", "keyup", "shift+ctrl+alt+super"]),
        ):
            with self.subTest(button=button, count=count, modifiers=modifiers), platform, env, \
                 patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
                 patch.object(bridge.subprocess, "run", return_value=completed()) as run:
                result = bridge._native_click_screen(10, 20, True, button, count, modifiers)
            self.assertEqual(result, {"ok": True, "backend": "xdotool", "pointerRestored": True})
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["/usr/bin/xdotool", "mousemove", "--sync", "10", "20",
                                                     *clicked, "mousemove", "restore"])

    def test_xdotool_failure_releases_held_modifiers(self):
        platform, env = self.linux()
        for failure, error in ((completed(returncode=1, stderr="BadWindow\n"), "xdotool failed: BadWindow"),
                               (subprocess.TimeoutExpired("xdotool", 10), "xdotool failed: ")):
            with self.subTest(error=error), platform, env, \
                 patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
                 patch.object(bridge.subprocess, "run", side_effect=[failure, completed()]) as run:
                result = bridge._native_click_screen(10, 20, False, "left", 1, ("Control",))
            self.assertEqual(set(result), {"error"})
            self.assertTrue(result["error"].startswith(error), result)
            self.assertEqual([c.args[0] for c in run.call_args_list], [
                ["/usr/bin/xdotool", "mousemove", "--sync", "10", "20", "keydown", "ctrl", "click", "1",
                 "keyup", "ctrl"],
                ["/usr/bin/xdotool", "keyup", "ctrl"],
            ])

    def test_linux_without_xdotool_explains_install(self):
        platform, env = self.linux()
        with platform, env, patch.object(bridge.shutil, "which", return_value=None), \
             patch.object(bridge.subprocess, "run") as run:
            result = bridge._native_click_screen(1, 2)
        self.assertIn("sudo apt install xdotool", result["error"])
        run.assert_not_called()

    def test_linux_reports_xdotool_failure(self):
        platform, env = self.linux()
        with platform, env, patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"):
            with patch.object(bridge.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 1, "", "Can't open display\n")):
                self.assertEqual(bridge._native_click_screen(1, 2),
                                 {"error": "xdotool failed: Can't open display"})
            with patch.object(bridge.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 2, "", "")):
                self.assertEqual(bridge._native_click_screen(1, 2), {"error": "xdotool failed: exit code 2"})
            with patch.object(bridge.subprocess, "run",
                              side_effect=subprocess.TimeoutExpired("xdotool", 10)):
                self.assertIn("xdotool failed:", bridge._native_click_screen(1, 2)["error"])

    def test_linux_without_x11_does_not_click(self):
        for session, display in (("wayland", ":0"), ("x11", None)):
            with self.subTest(session=session, display=display), \
                 patch.object(bridge.sys, "platform", "linux"), \
                 patch.dict(os.environ, {"XDG_SESSION_TYPE": session}), \
                 patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
                 patch.object(bridge.subprocess, "run") as run:
                os.environ.pop("DISPLAY", None)
                if display:
                    os.environ["DISPLAY"] = display
                result = bridge._native_click_screen(1, 2)
                self.assertIn("X11", result["error"])
                run.assert_not_called()

    def test_other_platforms_are_unsupported(self):
        with patch.object(bridge.sys, "platform", "freebsd14"):
            result = bridge._native_click_screen(1, 2)
        self.assertEqual(result, {"error": "trustedClick is only implemented on Windows, macOS and Linux (X11)"})


class BrowserFocusTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(bridge.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KITSUNE_BROWSER_WINDOW_TITLE", None)
        os.environ.pop("KITSUNE_BROWSER_PROCESS", None)

    def test_windows_tries_zen_then_firefox_titles(self):
        with patch.object(bridge.sys, "platform", "win32"), \
             patch.object(bridge, "_focus_window_by_title", side_effect=[False, True]) as focus:
            self.assertTrue(bridge._focus_browser_window())
        self.assertEqual(focus.call_args_list, [call("Zen Browser"), call("Mozilla Firefox")])

    def test_windows_title_from_env_or_request(self):
        os.environ["KITSUNE_BROWSER_WINDOW_TITLE"] = "Floorp, LibreWolf"
        with patch.object(bridge.sys, "platform", "win32"), \
             patch.object(bridge, "_focus_window_by_title", return_value=False) as focus:
            self.assertFalse(bridge._focus_browser_window())
            self.assertEqual(focus.call_args_list, [call("Floorp"), call("LibreWolf")])
            focus.reset_mock()
            bridge._focus_browser_window("Inbox - Mail")
            focus.assert_called_once_with("Inbox - Mail")

    def test_macos_activates_browser_process(self):
        with patch.object(bridge.sys, "platform", "darwin"), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "zen\n", "")) as run:
            self.assertTrue(bridge._focus_browser_window("Page title"))
        self.assertEqual(run.call_args.args[0],
                         ["osascript", "-e", bridge._MAC_FOCUS_SCRIPT, "zen", "firefox", "librewolf"])

    def test_macos_process_from_env_or_request(self):
        os.environ["KITSUNE_BROWSER_PROCESS"] = "Firefox Nightly"
        done = subprocess.CompletedProcess([], 0, "x\n", "")
        with patch.object(bridge.sys, "platform", "darwin"), \
             patch.object(bridge.subprocess, "run", return_value=done) as run:
            bridge._focus_browser_window()
            self.assertEqual(run.call_args.args[0][3:], ["Firefox Nightly"])
            bridge._focus_browser_window(process="librewolf")
            self.assertEqual(run.call_args.args[0][3:], ["librewolf"])

    def test_macos_focus_failures_return_false(self):
        with patch.object(bridge.sys, "platform", "darwin"):
            with patch.object(bridge.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "\n", "")):
                self.assertFalse(bridge._focus_browser_window())
            with patch.object(bridge.subprocess, "run", side_effect=OSError("no osascript")):
                self.assertFalse(bridge._focus_browser_window())

    def test_other_platforms_do_not_focus(self):
        with patch.object(bridge.sys, "platform", "linux"), \
             patch.object(bridge.subprocess, "run") as run:
            self.assertFalse(bridge._focus_browser_window("x"))
        run.assert_not_called()


class TrustedClickTests(unittest.TestCase):
    def test_screen_click_focus_is_browser_neutral(self):
        with patch.object(bridge, "_focus_browser_window", return_value=True) as focus, \
             patch.object(bridge, "_native_click_screen",
                          return_value={"ok": True, "backend": "quartz"}) as click:
            result = asyncio.run(bridge.trusted_click(
                {"coords": {"x": 5, "y": 6}, "coordinateSpace": "screen", "focus": True}))
        focus.assert_called_once_with("", "")
        click.assert_called_once_with(5, 6, True, "left", 1, ())
        self.assertEqual((result["focused"], result["clickBackend"]), (True, "quartz"))

    def test_viewport_click_focuses_page_title_and_process(self):
        point = {"screenX": 110, "screenY": 220, "viewportX": 10, "viewportY": 20, "title": "Docs"}
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point), \
             patch.object(bridge, "_focus_browser_window", return_value=False) as focus, \
             patch.object(bridge, "_native_click_screen",
                          return_value={"ok": True, "backend": "cliclick"}) as click:
            result = asyncio.run(bridge.trusted_click(
                {"selector": "#go", "focus": True, "process": "firefox"}))
        focus.assert_called_once_with("Docs", "firefox")
        click.assert_called_once_with(110, 220, True, "left", 1, ())
        self.assertEqual(result["clickBackend"], "cliclick")

    def test_viewport_click_reports_the_page_zoom_it_used(self):
        # The extension already converted the point with the zoom; the bridge clicks it as is.
        for point, zoom in (
            ({"screenX": 285, "screenY": 585, "viewportX": 90, "viewportY": 310, "zoom": 1.5}, 1.5),
            ({"screenX": 152, "screenY": 312, "viewportX": 90, "viewportY": 310, "zoom": 0.8}, 0.8),
            # An extension older than #307 sends no zoom.
            ({"screenX": 190, "screenY": 390, "viewportX": 90, "viewportY": 310}, 1),
        ):
            with self.subTest(zoom=zoom), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point), \
                 patch.object(bridge, "_native_click_screen",
                              return_value={"ok": True, "backend": "quartz"}) as click:
                result = asyncio.run(bridge.trusted_click({"selector": "#go"}))
            click.assert_called_once_with(point["screenX"], point["screenY"], True, "left", 1, ())
            self.assertEqual(result["zoom"], zoom)

    def test_target_not_at_point_clicks_nothing(self):
        refused = {"error": "The element at the target's center is not the target; the trusted click was not made",
                   "code": "target_not_at_point", "retryable": True,
                   "state": {"viewportX": 90, "viewportY": 310, "hit": None}}
        with patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=refused), \
             patch.object(bridge, "_focus_browser_window") as focus, \
             patch.object(bridge, "_native_click_screen") as click:
            result = asyncio.run(bridge.trusted_click({"selector": "#go", "focus": True}))
        self.assertEqual(result, refused)
        focus.assert_not_called()
        click.assert_not_called()

    def test_restore_pointer_is_passed_to_the_backend_and_reported(self):
        point = {"screenX": 110, "screenY": 220, "viewportX": 10, "viewportY": 20}
        failed = {"ok": True, "backend": "quartz", "pointerRestored": False,
                  "pointerRestoreError": "could not move the pointer back: x"}
        for params, clicked, restore in (
            ({"selector": "#go"}, failed, True),
            ({"selector": "#go", "restorePointer": False},
             {"ok": True, "backend": "quartz", "pointerRestored": False}, False),
            ({"coords": {"x": 5, "y": 6}, "coordinateSpace": "screen", "restorePointer": False},
             {"ok": True, "backend": "win32", "pointerRestored": False}, False),
        ):
            with self.subTest(params=params), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point), \
                 patch.object(bridge, "_native_click_screen", return_value=clicked) as click:
                result = asyncio.run(bridge.trusted_click(params))
            self.assertIs(click.call_args.args[2], restore)
            self.assertTrue(result["ok"])
            self.assertEqual({k: result[k] for k in result if k.startswith("pointer")},
                             {k: clicked[k] for k in clicked if k.startswith("pointer")})

    def test_click_options_are_validated_and_passed_to_the_backend(self):
        point = {"screenX": 110, "screenY": 220, "viewportX": 10, "viewportY": 20}
        with patch.object(bridge.sys, "platform", "linux"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point) as send, \
             patch.object(bridge, "_native_click_screen", return_value={"ok": True, "backend": "xdotool"}) as click:
            result = asyncio.run(bridge.trusted_click({"selector": "#go", "tabId": 7, "button": "right",
                                                       "clickCount": 2, "modifiers": ["Meta", "Shift", "Meta"]}))
        self.assertTrue(result["ok"])
        # One press per key, in a fixed order; the content script learns which key presses are the agent's.
        click.assert_called_once_with(110, 220, True, "right", 2, ("Shift", "Meta"))
        self.assertEqual(send.call_args_list[0].args, ("screenPoint", {
            "selector": "#go", "x": None, "y": None, "click": True, "tabId": 7, "modifiers": ["Shift", "Meta"]}))

        for params, error in (
            ({"button": "back"}, "trustedClick button must be left, right or middle, not 'back'"),
            ({"clickCount": 3}, "trustedClick clickCount must be 1 or 2, not 3"),
            ({"clickCount": True}, "trustedClick clickCount must be 1 or 2, not True"),
            # 2.0 == 2, but the backends count presses with range() and would fail mid-click.
            ({"clickCount": 2.0}, "trustedClick clickCount must be 1 or 2, not 2.0"),
            ({"modifiers": ["Control", "Cmd"]},
             "trustedClick modifiers must be a list of Shift, Control, Alt and Meta, not ['Control', 'Cmd']"),
            ({"modifiers": "Shift"},
             "trustedClick modifiers must be a list of Shift, Control, Alt and Meta, not 'Shift'"),
        ):
            for target in ({"selector": "#go"}, {"coords": {"x": 1, "y": 2}, "coordinateSpace": "screen"}):
                with self.subTest(params=params, target=target), \
                     patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send, \
                     patch.object(bridge, "_native_click_screen") as click:
                    self.assertEqual(asyncio.run(bridge.trusted_click({**target, **params})), {"error": error})
                send.assert_not_called()
                click.assert_not_called()

    def test_backend_refusal_shows_the_users_tab_again(self):
        point = {"screenX": 110, "screenY": 220, "viewportX": 10, "viewportY": 20, "tabId": 7, "previousTabId": 3}
        refused = {"error": "trustedClick cannot middle-click with cliclick; ..."}
        with patch.object(bridge.sys, "platform", "linux"), \
             patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point) as send, \
             patch.object(bridge.asyncio, "sleep", new_callable=AsyncMock), \
             patch.object(bridge, "_native_click_screen", return_value=refused):
            result = asyncio.run(bridge.trusted_click({"selector": "#go", "tabId": 7, "button": "middle"}))
        self.assertEqual(result, refused)
        self.assertEqual(send.call_args_list[-1].args, ("restoreActiveTab", {"tabId": 3, "from": 7}))

    def test_cliclick_refuses_before_the_tab_is_shown(self):
        for target in ({"selector": "#go", "tabId": 7}, {"coords": {"x": 1, "y": 2}, "coordinateSpace": "screen"}):
            with self.subTest(target=target), patch.object(bridge.sys, "platform", "darwin"), \
                 patch.dict("sys.modules", {"Quartz": None}), \
                 patch.object(bridge.shutil, "which", return_value="/opt/homebrew/bin/cliclick"), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock) as send, \
                 patch.object(bridge, "_focus_browser_window") as focus, \
                 patch.object(bridge, "_native_click_screen") as click:
                result = asyncio.run(bridge.trusted_click({**target, "focus": True, "button": "middle"}))
            self.assertIn("cannot middle-click with cliclick", result["error"])
            send.assert_not_called()
            focus.assert_not_called()
            click.assert_not_called()

    def test_only_backends_that_press_keys_excuse_them_in_the_page(self):
        point = {"screenX": 110, "screenY": 220, "viewportX": 10, "viewportY": 20}
        for quartz, sent in ((SimpleNamespace(), None), (None, ["Alt"])):
            with self.subTest(quartz=quartz), patch.object(bridge.sys, "platform", "darwin"), \
                 patch.dict("sys.modules", {"Quartz": quartz}), \
                 patch.object(bridge.shutil, "which", return_value="/opt/homebrew/bin/cliclick"), \
                 patch.object(bridge, "send_to_extension", new_callable=AsyncMock, return_value=point) as send, \
                 patch.object(bridge, "_native_click_screen", return_value={"ok": True}):
                asyncio.run(bridge.trusted_click({"selector": "#go", "modifiers": ["Alt"]}))
            # Quartz sets event flags and presses no keys, so the user's Alt still counts.
            self.assertEqual(send.call_args_list[0].args[1].get("modifiers"), sent)

    def test_linux_screen_click_reports_xdotool_backend(self):
        with patch.object(bridge.sys, "platform", "linux"), \
             patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}), \
             patch.object(bridge.shutil, "which", return_value="/usr/bin/xdotool"), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = asyncio.run(bridge.trusted_click(
                {"coords": {"x": 5, "y": 6}, "coordinateSpace": "screen", "focus": True}))
        run.assert_called_once()
        self.assertEqual((result["focused"], result["clickBackend"], result["pointerRestored"]),
                         (False, "xdotool", True))

    def test_click_errors_are_returned(self):
        error = {"error": "trustedClick needs macOS Accessibility permission: ..."}
        with patch.object(bridge, "_focus_browser_window") as focus, \
             patch.object(bridge, "_native_click_screen", return_value=error):
            result = asyncio.run(bridge.trusted_click(
                {"coords": {"x": 1, "y": 2}, "coordinateSpace": "screen"}))
        self.assertEqual(result, error)
        focus.assert_not_called()

    def test_tool_sends_restore_pointer_only_when_it_is_off(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_trusted_click(selector="#menu")
            tools.browser_trusted_click(selector="#menu", restore_pointer=False)
        self.assertNotIn("restorePointer", post.call_args_list[0].args[1])
        self.assertIs(post.call_args_list[1].args[1]["restorePointer"], False)

    def test_tool_sends_click_options_only_when_they_are_not_the_default(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_trusted_click(selector="#go")
            tools.browser_trusted_click(selector="#go", button="right", click_count=2, modifiers=["Shift"])
        self.assertFalse({"button", "clickCount", "modifiers"} & set(post.call_args_list[0].args[1]))
        body = post.call_args_list[1].args[1]
        self.assertEqual((body["button"], body["clickCount"], body["modifiers"]), ("right", 2, ["Shift"]))


if __name__ == "__main__":
    unittest.main()
