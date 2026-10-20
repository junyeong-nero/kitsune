"""Cross-platform trusted click and browser focus, with every OS call mocked."""
import asyncio
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from kitsune import bridge


def fake_quartz():
    return SimpleNamespace(
        kCGEventMouseMoved=5, kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2,
        kCGMouseButtonLeft=0, kCGHIDEventTap=0,
        CGEventCreateMouseEvent=Mock(side_effect=lambda src, kind, point, button: ("event", kind)),
        CGEventPost=Mock(),
    )


class NativeClickTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(bridge.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def mac(self, trusted=True):
        return patch.object(bridge.sys, "platform", "darwin"), \
            patch.object(bridge, "_mac_accessibility_trusted", return_value=trusted)

    def test_windows_click_uses_user32(self):
        user32 = Mock()
        with patch.object(bridge.sys, "platform", "win32"), \
             patch.object(bridge.ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            result = bridge._native_click_screen(10.4, 19.6)
        self.assertEqual(result, {"ok": True, "backend": "win32"})
        user32.SetCursorPos.assert_called_once_with(10, 20)
        self.assertEqual(user32.mouse_event.call_args_list,
                         [call(0x0002, 0, 0, 0, 0), call(0x0004, 0, 0, 0, 0)])

    def test_macos_prefers_quartz_events(self):
        quartz = fake_quartz()
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": quartz}), \
             patch.object(bridge.shutil, "which") as which, \
             patch.object(bridge.subprocess, "run") as run:
            result = bridge._native_click_screen(10, 20.5)
        self.assertEqual(result, {"ok": True, "backend": "quartz"})
        self.assertEqual(quartz.CGEventCreateMouseEvent.call_args_list,
                         [call(None, kind, (10.0, 20.5), 0) for kind in (5, 1, 2)])
        self.assertEqual(quartz.CGEventPost.call_args_list,
                         [call(0, ("event", kind)) for kind in (5, 1, 2)])
        which.assert_not_called()
        run.assert_not_called()

    def test_macos_falls_back_to_cliclick(self):
        platform, trusted = self.mac()
        with platform, trusted, patch.dict("sys.modules", {"Quartz": None}), \
             patch.object(bridge.shutil, "which", return_value="/opt/homebrew/bin/cliclick"), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = bridge._native_click_screen(-5.2, 20)
        self.assertEqual(result, {"ok": True, "backend": "cliclick"})
        self.assertEqual(run.call_args.args[0], ["/opt/homebrew/bin/cliclick", "c:=-5,20"])

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
        for text in ("kitsune-browser-mcp[macos]", "cliclick", "Accessibility"):
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

    def test_other_platforms_are_unsupported(self):
        with patch.object(bridge.sys, "platform", "linux"):
            result = bridge._native_click_screen(1, 2)
        self.assertEqual(result, {"error": "trustedClick is only implemented on Windows and macOS"})


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
        click.assert_called_once_with(5, 6)
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
        click.assert_called_once_with(110, 220)
        self.assertEqual(result["clickBackend"], "cliclick")

    def test_click_errors_are_returned(self):
        error = {"error": "trustedClick needs macOS Accessibility permission: ..."}
        with patch.object(bridge, "_focus_browser_window") as focus, \
             patch.object(bridge, "_native_click_screen", return_value=error):
            result = asyncio.run(bridge.trusted_click(
                {"coords": {"x": 1, "y": 2}, "coordinateSpace": "screen"}))
        self.assertEqual(result, error)
        focus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
