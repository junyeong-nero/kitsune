"""Trusted drag (#351): browser_trusted_drag presses, moves and releases the OS pointer on macOS.

Every OS call (focus, the window check, Quartz events, the pointer warp) is mocked: these tests
never move the pointer.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from kitsune import bridge, capabilities, tools

WINDOW = {"left": 0, "top": 0, "width": 1920, "height": 1080}
START = {"screenX": 140, "screenY": 300, "viewportX": 140, "viewportY": 220, "zoom": 1, "title": "Brightness",
         "window": WINDOW, "tabId": 396, "previousTabId": 324}
END = {"screenX": 420, "screenY": 330, "viewportX": 420, "viewportY": 250, "zoom": 1, "title": "Brightness",
       "window": WINDOW}
USER_POINTER = (900.0, 15.0)


def fake_quartz():
    return SimpleNamespace(
        kCGEventMouseMoved=5, kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2, kCGEventLeftMouseDragged=6,
        kCGMouseButtonLeft=0, kCGHIDEventTap=0, kCGMouseEventClickState=1,
        CGEventCreate=Mock(return_value="current"),
        CGEventGetLocation=Mock(return_value=USER_POINTER),
        CGEventCreateMouseEvent=Mock(side_effect=lambda src, kind, point, button: {"kind": kind, "at": point}),
        CGEventSetIntegerValueField=Mock(side_effect=lambda event, field, value: event.update(clickState=value)),
        CGEventPost=Mock(),
        CGWarpMouseCursorPosition=Mock(return_value=0),
        CGAssociateMouseAndMouseCursorPosition=Mock(),
    )


class Extension:
    """send_to_extension: the first screenPoint answers START, the second END; every call is recorded."""

    def __init__(self, start=START, end=END):
        self.points, self.sent = [start, end], []

    async def __call__(self, action, params=None, timeout=30):
        self.sent.append((action, dict(params or {})))
        return self.points.pop(0) if action == "screenPoint" else {"ok": True}


class MacDragTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(bridge.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.quartz = fake_quartz()
        modules = patch.dict("sys.modules", {"Quartz": self.quartz})
        modules.start()
        self.addCleanup(modules.stop)

    def posted(self):
        return [c.args[1] for c in self.quartz.CGEventPost.call_args_list]

    def test_presses_moves_in_steps_releases_and_restores_the_pointer(self):
        order = Mock()
        order.attach_mock(self.quartz.CGEventPost, "post")
        order.attach_mock(self.sleep, "sleep")
        order.attach_mock(self.quartz.CGWarpMouseCursorPosition, "warp")
        result = bridge._mac_drag_screen((100, 200), (200, 240), 4)
        self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": True})
        self.assertEqual(self.posted(), [
            {"kind": 5, "at": (100.0, 200.0)}, {"kind": 1, "at": (100.0, 200.0), "clickState": 1},
            {"kind": 6, "at": (125.0, 210.0)}, {"kind": 6, "at": (150.0, 220.0)}, {"kind": 6, "at": (175.0, 230.0)},
            {"kind": 6, "at": (200.0, 240.0)}, {"kind": 2, "at": (200.0, 240.0), "clickState": 1}])
        self.assertEqual([c for c in order.mock_calls if c[0] != "post"], [
            call.sleep(0.05), call.sleep(bridge.TRUSTED_DRAG_PRESS_DELAY),
            *[call.sleep(bridge.TRUSTED_DRAG_STEP_DELAY)] * 4, call.sleep(bridge.POINTER_RESTORE_DELAY),
            call.warp(USER_POINTER)])
        self.quartz.CGAssociateMouseAndMouseCursorPosition.assert_called_once_with(True)

    def test_a_failure_mid_drag_still_releases_the_button(self):
        self.quartz.CGEventPost.side_effect = [None, None, None, RuntimeError("tap disabled"), None]
        with self.assertRaises(RuntimeError):
            bridge._mac_drag_screen((100, 200), (200, 240), 4)
        self.assertEqual([event["kind"] for event in self.posted()], [5, 1, 6, 6, 2])
        self.assertEqual(self.posted()[-1]["at"], (200.0, 240.0))

    def test_an_unreadable_pointer_still_drags(self):
        self.quartz.CGEventCreate.return_value = None
        result = bridge._mac_drag_screen((100, 200), (200, 240), 1)
        self.assertEqual(result, {"ok": True, "backend": "quartz", "pointerRestored": False,
                                  "pointerRestoreError": "could not read the pointer position: "
                                                         "CGEventCreate returned no event"})
        self.assertEqual([event["kind"] for event in self.posted()], [5, 1, 6, 2])
        self.quartz.CGWarpMouseCursorPosition.assert_not_called()


class TrustedDragTests(unittest.TestCase):
    def setUp(self):
        self.extension = Extension()
        self.checks = []
        targets = [
            patch.object(bridge, "_mac_drag_unavailable", return_value=None),
            patch.object(bridge, "send_to_extension", new=lambda *a, **k: self.extension(*a, **k)),
            patch.object(bridge, "_focus_browser_window", return_value=True),
            patch.object(bridge, "_browser_not_at_point",
                         side_effect=lambda *args: self.checks.append(args) or None),
            patch.object(bridge, "_mac_drag_screen",
                         return_value={"ok": True, "backend": "quartz", "pointerRestored": True}),
            patch.object(bridge.asyncio, "sleep", new_callable=AsyncMock),
        ]
        for target in targets:
            target.start()
            self.addCleanup(target.stop)

    def test_drags_between_the_two_points_in_the_tabs_window(self):
        result = asyncio.run(bridge.trusted_drag({"selector": "e4", "to": "#mark", "tabId": 396, "steps": 12}))
        self.assertEqual(result, {
            "ok": True, "trusted": True, "steps": 12, "focused": True,
            "from": {"screenX": 140, "screenY": 300, "viewportX": 140, "viewportY": 220},
            "to": {"screenX": 420, "screenY": 330, "viewportX": 420, "viewportY": 250},
            "zoom": 1, "clickBackend": "quartz", "pointerRestored": True})
        # The source is scrolled into view and hit-tested; the destination is measured where it is,
        # in the same tab; the user's tab is shown again after the drag.
        self.assertEqual(self.extension.sent, [
            ("screenPoint", {"click": True, "tabId": 396, "selector": "e4"}),
            ("screenPoint", {"click": True, "tabId": 396, "selector": "#mark", "scroll": False}),
            ("restoreActiveTab", {"tabId": 324, "from": 396}),
        ])
        # Both points must show the tab's own window; focusing waits at the first one only.
        target = (0, 0, 1920, 1080)
        self.assertEqual(self.checks, [(140, 300, "", True, bridge.BROWSER_AT_POINT_WAIT, target),
                                       (420, 330, "", True, 0.0, target)])
        bridge._mac_drag_screen.assert_called_once_with((140, 300), (420, 330), 12)

    def test_a_window_refusal_at_either_point_drags_nothing(self):
        refused = {"error": "Another app's window ...", "code": "browser_not_at_point", "retryable": True}
        for refused_at in (0, 1):
            with self.subTest(refused_at=refused_at):
                self.extension = Extension()
                bridge._mac_drag_screen.reset_mock()
                answers = [None, None]
                answers[refused_at] = refused
                with patch.object(bridge, "_browser_not_at_point", side_effect=answers):
                    self.assertEqual(asyncio.run(bridge.trusted_drag({"selector": "#knob", "to": "#mark"})), refused)
                bridge._mac_drag_screen.assert_not_called()
                self.assertEqual(self.extension.sent[-1], ("restoreActiveTab", {"tabId": 324, "from": 396}))

    def test_a_destination_that_cannot_be_measured_drags_nothing(self):
        missing = {"error": "The element at the target's center is not the target; the trusted click was not made",
                   "code": "target_not_at_point", "retryable": True}
        self.extension = Extension(end=missing)
        self.assertEqual(asyncio.run(bridge.trusted_drag({"selector": "#knob", "to": "#mark"})), missing)
        bridge._mac_drag_screen.assert_not_called()
        self.assertEqual([action for action, _ in self.extension.sent],
                         ["screenPoint", "screenPoint", "restoreActiveTab"])
        # A source that cannot be measured is refused before the destination is looked at; the
        # background shows the user's tab again itself.
        self.extension = Extension(start={"error": "No target", "code": "not_found"})
        self.assertEqual(asyncio.run(bridge.trusted_drag({"selector": "#knob", "to": "#mark"}))["code"], "not_found")
        self.assertEqual([action for action, _ in self.extension.sent], ["screenPoint"])

    def test_focus_false_leaves_the_browser_where_it_is(self):
        result = asyncio.run(bridge.trusted_drag({"selector": "#knob", "to": "#mark", "focus": False, "first": True}))
        self.assertIs(result["focused"], False)
        bridge._focus_browser_window.assert_not_called()
        self.assertEqual([check[3:5] for check in self.checks], [(None, 0.0), (None, 0.0)])
        self.assertEqual(self.extension.sent[0], ("screenPoint", {"click": True, "first": True, "selector": "#knob"}))

    def test_invalid_requests_send_nothing(self):
        for params, error in (
            ({"to": "#mark"}, "trustedDrag needs selector and to"),
            ({"selector": "#knob"}, "trustedDrag needs selector and to"),
            ({"selector": "#knob", "to": "#mark", "steps": 0}, "steps must be an integer from 1 to 50"),
            ({"selector": "#knob", "to": "#mark", "steps": 51}, "steps must be an integer from 1 to 50"),
            ({"selector": "#knob", "to": "#mark", "steps": 5.0}, "steps must be an integer from 1 to 50"),
        ):
            with self.subTest(params=params):
                self.assertIn(error, asyncio.run(bridge.trusted_drag(params))["error"])
        self.assertEqual(self.extension.sent, [])

    def test_unavailable_input_drags_nothing(self):
        refused = {"error": "Trusted drag is only implemented on macOS; nothing was dragged",
                   "code": "unsupported_platform", "retryable": False}
        with patch.object(bridge, "_mac_drag_unavailable", return_value=refused):
            self.assertEqual(asyncio.run(bridge.trusted_drag({"selector": "#knob", "to": "#mark"})), refused)
        self.assertEqual(self.extension.sent, [])

    def test_focus_checks_and_drag_leave_the_event_loop_free(self):
        # As for trusted clicks (#336), other bridge requests are served during the drag.
        def slow_drag(*args):
            time.sleep(0.3)
            return {"ok": True, "backend": "quartz", "pointerRestored": True}

        async def drag_with_other_requests():
            ticks = []

            async def other_request():
                for _ in range(5):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.04)

            started = time.monotonic()
            result, _ = await asyncio.gather(bridge.trusted_drag({"selector": "#knob", "to": "#mark"}),
                                             other_request())
            return result, [tick - started for tick in ticks]

        with patch.object(bridge.asyncio, "sleep", new=asyncio.sleep), \
             patch.object(bridge, "_mac_drag_screen", side_effect=slow_drag):
            result, ticks = asyncio.run(drag_with_other_requests())
        self.assertTrue(result["ok"])
        self.assertLess(ticks[-1], 0.3, "the loop ran while the drag was sent")


class UnavailableTests(unittest.TestCase):
    def test_other_platforms_missing_quartz_and_permission(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), patch.object(bridge.sys, "platform", platform):
                self.assertEqual(bridge._mac_drag_unavailable()["code"], "unsupported_platform")
        with patch.object(bridge.sys, "platform", "darwin"), patch.dict("sys.modules", {"Quartz": None}):
            self.assertEqual(bridge._mac_drag_unavailable()["code"], "unsupported_platform")
        with patch.object(bridge.sys, "platform", "darwin"), patch.dict("sys.modules", {"Quartz": object()}), \
             patch.object(bridge, "_mac_accessibility_trusted", return_value=False):
            refused = bridge._mac_drag_unavailable()
        self.assertEqual((refused["code"], refused["retryable"]), ("permission_required", False))
        with patch.object(bridge.sys, "platform", "darwin"), patch.dict("sys.modules", {"Quartz": object()}), \
             patch.object(bridge, "_mac_accessibility_trusted", return_value=True):
            self.assertIsNone(bridge._mac_drag_unavailable())


class SurfaceTests(unittest.TestCase):
    def test_the_tool_posts_to_the_browse_route(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_trusted_drag(selector="e4", to_selector="e9", tab_id=3)
        post.assert_called_once_with("/api/trusted-drag", {"selector": "e4", "to": "e9", "steps": 10, "tabId": 3})
        self.assertEqual(tools.TOOLS["browser_trusted_drag"].group, "browse")
        self.assertIn("browser_trusted_drag", tools.TAB_WRITE_TOOLS)
        self.assertEqual(capabilities.ROUTE_GROUPS["/api/trusted-drag"], frozenset({"browse"}))

    def test_it_is_a_page_write_that_ask_mode_and_read_only_mode_hold(self):
        self.assertIn("trustedDrag", bridge.WRITE_ACTIONS)
        self.assertIn("trustedDrag", bridge._ASK_PAGE_ACTIONS)
        self.assertIn("trustedDrag", bridge._GLOBAL_SCOPE_ACTIONS)
        self.assertEqual(bridge._API_ALIASES["/api/trustedDrag"], "/api/trusted-drag")
        with patch.dict(bridge._policy, {"readonly": True}):
            self.assertEqual(bridge._check_policy("trustedDrag", {"selector": "#knob", "to": "#mark"}),
                             {"error": "Read-only mode is active; refusing action: trustedDrag"})


if __name__ == "__main__":
    unittest.main()
