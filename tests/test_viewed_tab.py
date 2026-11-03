"""The Python client still understands legacy user_viewing results until the alias release ends (#398).

Neither the extension nor the bridge produces them any more; errors.APPROVAL_DETAILS keeps the detail
as documented compatibility. Held tools keep taking confirm tokens, which ask-before-act issues.
"""
import unittest
from unittest.mock import Mock, patch

from kitsune import errors, playwright_tools, tools
from kitsune.browser import Browser, Tab


class ViewedTabClientTests(unittest.TestCase):
    def test_raise_for_asks_for_confirmation(self):
        refused = {"error": "The user is viewing tab 4; nothing was done", "code": "user_viewing",
                   "retryable": False, "state": {"tabId": 4, "confirmToken": "tok", "expiresInSeconds": 120}}
        with self.assertRaises(errors.ConfirmationRequired) as raised:
            errors.raise_for(refused)
        self.assertEqual(raised.exception.token, "tok")
        # read and draft get no token: a plain error.
        with self.assertRaises(errors.KitsuneError) as plain:
            errors.raise_for({**refused, "state": {"tabId": 4}})
        self.assertNotIsInstance(plain.exception, errors.ConfirmationRequired)

    def test_held_tools_take_a_confirm_token(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_scroll(tab_id=4, confirm_token="t")
            tools.browser_hover(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_focus(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_blur(selector="#go", tab_id=4, confirm_token="t")
            tools.browser_reload_tab(tab_id=4, confirm_token="t")
            tools.browser_back(tab_id=4, confirm_token="t")
            tools.browser_forward(tab_id=4, confirm_token="t")
            tools.browser_hover(selector="#go", tab_id=4, duration_ms=400, confirm_token="t")
            tools.browser_trusted_drag(selector="#a", to_selector="#b", tab_id=4, confirm_token="t")
            playwright_tools.browser_hover(target="e1", confirm_token="t")
            playwright_tools.browser_navigate_back(tab_id=4, confirm_token="t")
        self.assertEqual([call.args[1].get("confirmToken") for call in post.call_args_list], ["t"] * 11)
        self.assertEqual(post.call_args_list[7].args[1]["durationMs"], 400)
        for name in ("browser_hover", "browser_navigate_back"):
            self.assertNotIn("confirm_token", playwright_tools.SCHEMAS[name]["inputSchema"]["properties"])

    def test_python_tab_methods_take_confirm(self):
        client = Mock()
        client.request.return_value = {"ok": True}
        tab = Tab(Browser(client), 4)
        for method, args in ((tab.back, ()), (tab.forward, ()), (tab.reload, ()), (tab.hover, ("#go",)),
                             (tab.scroll, ()), (tab.focus, ("#go",)), (tab.blur, ())):
            with self.subTest(method=method.__name__):
                method(*args, confirm="t")
                self.assertEqual(client.request.call_args.kwargs["json"]["confirmToken"], "t")


if __name__ == "__main__":
    unittest.main()
