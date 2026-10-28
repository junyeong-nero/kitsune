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
    return SimpleNamespace(
        kCGEventMouseMoved=5, kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2,
        kCGMouseButtonLeft=0, kCGHIDEventTap=0,
        CGEventCreate=Mock(return_value="current"),
        CGEventGetLocation=Mock(return_value=USER_POINTER),
        CGEventCreateMouseEvent=Mock(side_effect=lambda src, kind, point, button: ("event", kind)),
        CGEventPost=Mock(),
        CGWarpMouseCursorPosition=Mock(return_value=0),
        CGAssociateMouseAndMouseCursorPosition=Mock(),
    )


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
        self.assertEqual(quartz.CGEventPost.call_args_list,
                         [call(0, ("event", kind)) for kind in (5, 1, 2)])
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
        click.assert_called_once_with(5, 6, True)
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
        click.assert_called_once_with(110, 220, True)
        self.assertEqual(result["clickBackend"], "cliclick")

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


if __name__ == "__main__":
    unittest.main()
