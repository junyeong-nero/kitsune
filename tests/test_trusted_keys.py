"""Trusted keyboard input (#346): browser_trusted_type.

The OS layer (focus, the window check and click, the frontmost app, posted key events) is
mocked; tests/test_real_browser_trusted_keys.py sends real keys to an isolated browser.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from kitsune import bridge

POINT = {"screenX": 760, "screenY": 604, "viewportX": 700, "viewportY": 562, "tabId": 396, "previousTabId": 324,
         "window": {"left": 0, "top": 0, "width": 1920, "height": 1080}}


class Extension:
    """send_to_extension: screenPoint answers POINT, focusState answers from `focused` (one
    value per call), every call is recorded with the posted keys in one timeline."""

    def __init__(self, timeline, focused=None, point=None):
        self.timeline, self.focused, self.point = timeline, list(focused or []), point or POINT

    async def __call__(self, action, params=None, timeout=30):
        self.timeline.append((action, dict(params or {})))
        if action == "screenPoint":
            return self.point
        if action == "focusState":
            return {"ok": True, "focused": self.focused.pop(0) if self.focused else True}
        return {"ok": True}


class TrustedKeysTestCase(unittest.TestCase):
    def setUp(self):
        self.timeline = []
        self.extension = Extension(self.timeline)
        self.frontmost = [42]
        targets = [
            patch.object(bridge.sys, "platform", "darwin"),
            patch.object(bridge, "_mac_keyboard_unavailable", return_value=None),
            patch.object(bridge, "send_to_extension", new=lambda *a, **k: self.extension(*a, **k)),
            patch.object(bridge, "_focus_check_click", return_value=(True, {"ok": True, "backend": "quartz"})),
            patch.object(bridge, "_mac_window_at", return_value=(42, "Zen", (0, 0, 1920, 1080))),
            patch.object(bridge, "_mac_frontmost_pid", side_effect=lambda: self.frontmost[0]
                         if len(self.frontmost) == 1 else self.frontmost.pop(0)),
            patch.object(bridge, "_mac_post_char", side_effect=lambda c: self.timeline.append(("char", c))),
            patch.object(bridge, "_mac_post_key", side_effect=lambda code, flags=0, text=None:
                         self.timeline.append(("key", code))),
            patch.object(bridge, "_mac_post_chord", side_effect=lambda mods, key:
                         self.timeline.append(("chord", "+".join((*mods, key))))),
            patch.object(bridge.asyncio, "sleep", new_callable=AsyncMock),
        ]
        for target in targets:
            target.start()
            self.addCleanup(target.stop)

    def posted(self):
        return [entry for entry in self.timeline if entry[0] in ("char", "key", "chord")]

    def actions(self):
        return [entry[0] for entry in self.timeline if entry[0] not in ("char", "key", "chord")]


class TrustedTypeTests(TrustedKeysTestCase):
    def test_types_each_character_after_checking_the_browser_and_the_field(self):
        result = asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "4821\n9"}))
        self.assertEqual(result, {"ok": True, "trusted": True, "keysSent": 6, "focused": True,
                                  "clickBackend": "quartz", "typed": 6})
        self.assertNotIn("4821", repr(result))
        self.assertEqual(self.posted(), [("char", c) for c in "4821\n9"])
        # Each key is preceded by a focus check; the user's tab comes back after the last key.
        self.assertEqual(self.actions(), ["screenPoint", "agentInput"] + ["focusState"] * 6 + ["restoreActiveTab"])
        self.assertEqual(self.timeline[0][1], {"selector": "#otp", "click": True})
        self.assertEqual(self.timeline[-1], ("restoreActiveTab", {"tabId": 324, "from": 396}))
        self.assertTrue(all(params.get("tabId") == 396 for action, params in self.timeline
                            if action in ("focusState", "agentInput")), "the keys stay with the clicked tab")

    def test_clear_and_submit(self):
        result = asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "12", "clear": True, "submit": True}))
        self.assertEqual((result["keysSent"], result["typed"]), (5, 2))
        self.assertEqual(self.posted(), [("chord", "Meta+a"), ("key", bridge._MAC_KEY_CODES["Backspace"]),
                                         ("char", "1"), ("char", "2"), ("key", bridge._MAC_KEY_CODES["Enter"])])

    def test_stops_when_another_app_comes_to_the_front(self):
        self.frontmost[:] = [42, 42, 4515]
        result = asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "4821"}))
        self.assertEqual((result["code"], result["state"]), ("keyboard_focus_lost",
                                                             {"reason": "not_frontmost", "keysSent": 2}))
        self.assertEqual(self.posted(), [("char", "4"), ("char", "8")])
        self.assertEqual(self.actions()[-1], "restoreActiveTab")

    def test_stops_when_the_field_loses_the_pages_focus(self):
        self.extension.focused = [True, False]
        result = asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "4821"}))
        self.assertEqual(result["state"], {"reason": "target_not_focused", "keysSent": 1})
        self.assertEqual(self.posted(), [("char", "4")])
        self.assertEqual(self.actions()[-1], "restoreActiveTab")

    def test_a_refused_click_types_nothing(self):
        refused = {"error": "Another browser window or panel ...", "code": "browser_not_at_point", "retryable": True}
        with patch.object(bridge, "_focus_check_click", return_value=(True, refused)):
            self.assertEqual(asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "4821"})), refused)
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.actions(), ["screenPoint", "restoreActiveTab"])

    def test_an_unknown_browser_pid_types_nothing(self):
        with patch.object(bridge, "_mac_window_at", return_value=None):
            result = asyncio.run(bridge.trusted_type({"selector": "#otp", "text": "4821"}))
        self.assertEqual(result["state"]["reason"], "not_frontmost")
        self.assertEqual(self.posted(), [])

    def test_invalid_requests_send_nothing(self):
        for params, error in (({"selector": "#otp"}, "non-empty text"), ({"text": "1"}, "selector or ref"),
                              ({"selector": "#otp", "text": "x" * 1001}, "1000 characters"),
                              ({"selector": "#otp", "text": "1", "delayMs": 5000}, "delayMs")):
            with self.subTest(error=error):
                self.assertIn(error, asyncio.run(bridge.trusted_type(params))["error"])
        self.assertEqual(self.timeline, [])

    def test_other_platforms_and_missing_permission_type_nothing(self):
        for platform, code in (("linux", "unsupported_platform"), ("win32", "unsupported_platform")):
            with self.subTest(platform=platform), patch.object(bridge.sys, "platform", platform):
                self.assertEqual(KEYBOARD_UNAVAILABLE()["code"], code)
        # pyobjc is there (faked: the check only imports it), the permission is not.
        with patch.dict("sys.modules", {"AppKit": object(), "Quartz": object()}), \
             patch.object(bridge, "_mac_accessibility_trusted", return_value=False):
            refused = KEYBOARD_UNAVAILABLE()
        with patch.dict("sys.modules", {"AppKit": None}):
            self.assertEqual(KEYBOARD_UNAVAILABLE()["code"], "unsupported_platform")
        self.assertEqual((refused["code"], refused["retryable"]), ("permission_required", False))

    def test_the_text_is_kept_out_of_the_audit_log(self):
        self.assertEqual(bridge._audit_params("trustedType", {"selector": "#otp", "text": "482193"}),
                         {"selector": "#otp", "text": bridge.AUDIT_REDACTED})

    def test_the_request_timeout_covers_every_key(self):
        self.assertGreater(bridge._trusted_keys_timeout({"text": "x" * 1000, "delayMs": 30}), 35 + 1000 * 0.03)
        self.assertEqual(bridge._trusted_keys_timeout({"text": "", "delayMs": 30}), 35)


# The real check, captured before TrustedKeysTestCase patches it.
KEYBOARD_UNAVAILABLE = bridge._mac_keyboard_unavailable


class AskBeforeActTests(unittest.TestCase):
    def held(self, action, params):
        async def describe(params, inspect):
            return {"tabId": 7, "url": "https://shop.example/"}

        with patch.object(bridge, "_describe_target", new=describe):
            return asyncio.run(bridge._held_reason(action, params))

    def test_submit_is_held_typing_follows_the_page(self):
        self.assertEqual(self.held("trustedType", {"selector": "#q", "text": "1", "submit": True})[0], "submit")
        self.assertIsNone(self.held("trustedType", {"selector": "#q", "text": "1"}))


if __name__ == "__main__":
    unittest.main()
