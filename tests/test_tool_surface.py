"""Exercise MCP registration and merged tools without a running browser/bridge."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools
from kitsune.bridge import _parse_workflow


class ToolSurfaceTests(unittest.TestCase):
    def registered(self, caps):
        env = {**os.environ, "KITSUNE_CAPS": caps}
        result = subprocess.run(
            [sys.executable, "-c", "import asyncio,json; from kitsune.server import mcp; "
             "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))"],
            env=env, capture_output=True, text=True, check=True,
        )
        return set(json.loads(result.stdout))

    def test_registration(self):
        core = self.registered("core")
        all_tools = self.registered("all")
        # The core group is exactly Playwright MCP's core tools.
        self.assertEqual(core, set(playwright_tools.SCHEMAS))
        self.assertEqual(len(core), 25)
        self.assertEqual(len(all_tools), 94)
        removed = {
            "browser_page_text_by_tab_id", "browser_fill", "browser_set_editable_content",
            "browser_new_tab", "browser_close_tab", "browser_switch_tab",
            "browser_wait_for_element", "browser_wait_for_result", "browser_wait_for_url",
            "browser_wait_for_title", "browser_wait_for_network_idle",
            "browser_element_screenshot", "browser_full_page_screenshot", "browser_wp_html",
            # Replaced by Playwright's core tools (browser_press_key, browser_evaluate, ...).
            "browser_keypress", "browser_js", "browser_screenshot", "browser_back", "browser_form_fill",
            "browser_upload_file", "browser_console_logs", "browser_watch_console", "browser_double_click",
        }
        self.assertFalse(all_tools & removed)
        # `all` enables every group except storage and history, which read personal data and must be
        # named (#140).
        self.assertEqual(server.NAMED_ONLY_GROUPS, {"storage", "history"})
        storage = {"browser_cookies", "browser_storage", "browser_clipboard", "browser_clear_browsing_data",
                   "browser_save_session", "browser_load_session", "browser_list_sessions", "browser_delete_session"}
        history = {"browser_history_search", "browser_history_visits", "browser_bookmarks_search"}
        self.assertEqual(self.registered("storage"), storage)
        self.assertEqual(self.registered("history"), history)
        self.assertFalse(all_tools & (storage | history))
        self.assertEqual(self.registered("all,storage,history"), all_tools | storage | history)
        groups = [self.registered(g) for g in server.CAPABILITY_GROUPS - server.NAMED_ONLY_GROUPS]
        self.assertEqual(set.union(*groups), all_tools)
        self.assertEqual(sum(map(len, groups)), len(all_tools))
        self.assertEqual(self.registered(" CORE, automation "), core | self.registered("automation"))
        self.assertTrue({"browser_scheduler", "browser_batch", "browser_retry", "browser_tab_pool",
                         "browser_parallel", "browser_broadcast", "browser_sync_barrier", "browser_resume",
                         "browser_workflow"} <= self.registered("automation"))
        # Everyday page actions are not grouped with the bridge and policy administration tools.
        self.assertTrue({"browser_scroll", "browser_forward", "browser_check"} <= self.registered("browse"))
        self.assertTrue({"browser_status", "browser_set_policy", "browser_reload_extension"}
                        <= self.registered("admin"))
        self.assertTrue({"browser_windows", "browser_tag_tab", "browser_keep_alive"} <= self.registered("tabs"))

    def test_default_registration(self):
        with patch.dict(os.environ):
            os.environ.pop("KITSUNE_CAPS", None)
            self.assertEqual(self.registered_default(), set(playwright_tools.SCHEMAS))

    def registered_default(self):
        result = subprocess.check_output(
            [sys.executable, "-c", "import asyncio,json; from kitsune.server import mcp; "
             "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))"], text=True,
        )
        return set(json.loads(result))

    def test_type_modes(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            for mode, endpoint, fields in [
                ("type", "type", {"text": "hello", "clear": True}),
                ("fill", "fill", {"value": "hello"}),
                ("editable", "set-editable-content", {"value": "hello", "format": "text", "clear": True}),
            ]:
                tools.browser_type("#input", "hello", mode, clear=True, tab_id=7)
                post.assert_called_with("/api/" + endpoint, {"selector": "#input", "tabId": 7, **fields})
            post.reset_mock()
            self.assertIn("error", tools.browser_type("#input", "hello", "invalid"))
            post.assert_not_called()
            tools.browser_type("#editor", "more", "editable")
            self.assertFalse(post.call_args.args[1]["clear"])

    def test_tabs(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post, \
             patch.object(tools, "_get", return_value={"tabs": []}) as get:
            tools.browser_tabs()
            get.assert_called_once_with("/api/tabs")
            tools.browser_tabs("new", raw=True)
            post.assert_called_with("/api/new-tab", {"url": "about:blank", "raw": True})
            tools.browser_tabs("new", url="https://example.com")
            post.assert_called_with("/api/new-tab", {"url": "https://example.com"})
            tools.browser_tabs("new", url="https://example.com", wait=True, timeout=30000)
            post.assert_called_with("/api/new-tab", {"url": "https://example.com", "wait": True,
                                                      "timeout": 30000}, timeout=40)
            post.reset_mock()
            self.assertIn("error", tools.browser_tabs("new", wait=True, timeout=0))
            post.assert_not_called()
            tools.browser_tabs("close", tab_id=7, force=True)
            post.assert_called_with("/api/close-tab", {"tabId": 7, "force": True})
            # #139: closing a user tab needs the confirm token of its user_tab refusal.
            tools.browser_tabs("close", tab_id=7, confirm_token="tok")
            post.assert_called_with("/api/close-tab", {"tabId": 7, "confirmToken": "tok"})
            tools.browser_tabs("select", tab_id=7, confirm_token="tok")
            post.assert_called_with("/api/switch-tab", {"tabId": 7})
            for action in ["close", "select", "invalid"]:
                self.assertIn("error", tools.browser_tabs(action))

    def test_wait_conditions(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            for condition, value, endpoint, key in [
                ("selector", "#ready", "element", "selector"),
                ("js", "document.title", "result", "code"),
                ("url", "/done$", "url", "pattern"),
                ("title", "Done", "title", "pattern"),
                ("network_idle", True, "network-idle", "idleMs"),
            ]:
                tools.browser_wait_for(**{condition: value}, timeout=12000, tab_id=9)
                self.assertEqual(post.call_args.args[0], "/api/wait-for-" + endpoint)
                self.assertEqual(post.call_args.args[1][key], 500 if condition == "network_idle" else value)
                self.assertEqual(post.call_args.args[1]["tabId"], 9)
                self.assertGreater(post.call_args.kwargs["timeout"], 12)
            for condition in ("text", "text_gone"):
                needle = 'say "hello"\n\\ world'
                tools.browser_wait_for(**{condition: needle}, tab_id=9)
                self.assertEqual(post.call_args.args[0], "/api/wait-for-text")
                self.assertEqual(post.call_args.args[1][condition], needle)
                self.assertEqual(post.call_args.args[1]["tabId"], 9)

    def test_wait_validation_and_sleep(self):
        with patch.object(tools, "_post") as post, patch.object(tools, "_sleep") as sleep:
            for args in ({}, {"selector": "x", "text": "y"}, {"time": 0, "network_idle": True},
                         {"text": "", "text_gone": ""}, {"time": -1}, {"time": float("inf")},
                         {"selector": "x", "timeout": 0}):
                self.assertIn("error", tools.browser_wait_for(**args))
            post.assert_not_called()
            sleep.assert_not_called()
            # After the sleep, what changed since the agent last looked (#350).
            post.return_value = {"snapshot": '- text "Item 71"', "delta": True}
            self.assertEqual(tools.browser_wait_for(time=0, tab_id=9),
                             {"ok": True, "slept": 0, "snapshot": '- text "Item 71"', "snapshot_delta": True})
            sleep.assert_called_once_with(0)
            post.assert_called_once_with("/api/snapshot", {"sinceObservation": True, "maxChars": 4000, "tabId": 9})
            post.return_value = {"snapshot": "", "delta": True, "unchanged": True}
            self.assertEqual(tools.browser_wait_for(time=0)["no_visible_change"], True)
            # A page that cannot say (or snapshots turned off: {"ok": true}) leaves the sleep's result alone.
            for reply in ({"ok": True}, {"error": "No tab", "code": "no_tab"}):
                post.return_value = reply
                self.assertEqual(tools.browser_wait_for(time=0), {"ok": True, "slept": 0})
            post.reset_mock()
            self.assertIn("error", tools.browser_wait_for(text=""))
            post.assert_not_called()

    def test_screenshot_files(self):
        png = b"fake png bytes"
        data = "data:image/png;base64," + base64.b64encode(png).decode()
        with tempfile.TemporaryDirectory() as directory:
            destination = str(Path(directory) / "capture.png")
            with patch.object(tools, "_post", return_value={"dataUrl": data}) as post:
                for options, endpoint in [({"selector": "#chart"}, "element-screenshot"),
                                          ({"full_page": True}, "full-page-screenshot")]:
                    result = tools.browser_screenshot(**options, tab_id=3, save_to=destination)
                    self.assertEqual(result["saved_to"], destination)
                    self.assertNotIn("dataUrl", result)
                    self.assertEqual(Path(destination).read_bytes(), png)
                    self.assertEqual(post.call_args.args[0], "/api/" + endpoint)
                    self.assertEqual(post.call_args.args[1]["tabId"], 3)
                post.reset_mock()
                self.assertIn("error", tools.browser_screenshot(selector="x", full_page=True))
                post.assert_not_called()
            with patch.object(tools, "_post", return_value={"dataUrl": data}) as post:
                copied = str(Path(directory) / "copy.png")
                result = tools.browser_screenshot(save_to=copied, data_url=True)
                self.assertEqual(result["dataUrl"], data)
                self.assertEqual(Path(copied).read_bytes(), png)
                post.assert_called_once_with("/api/screenshot", {"imagePreview": False})

    def test_viewport_tab_and_errors(self):
        # #139: a tab's viewport is captured in place; the user's tab and window focus stay.
        data = "data:image/png;base64," + base64.b64encode(b"fake png bytes").decode()
        with patch.object(tools, "_get") as get, \
             patch.object(tools, "_post", return_value={"dataUrl": data, "url": "u"}) as post:
            image, meta = tools.browser_screenshot(tab_id=7)
            self.assertIsInstance(image, tools.Image)
            self.assertEqual(meta, {"url": "u"})
            post.assert_called_once_with("/api/screenshot", {"imagePreview": True, "tabId": 7})
            get.assert_not_called()
        with patch.object(tools, "_post", return_value={"error": "Invalid tab ID: 7", "code": "tab_closed"}):
            self.assertEqual(tools.browser_screenshot(tab_id=7)["code"], "tab_closed")

    def test_handle_dialog(self):
        with patch.object(tools, "_post", return_value={"ok": True}) as post:
            tools.browser_handle_dialog(accept=False, tab_id=4)
            post.assert_called_with("/api/handle-dialog", {"accept": False, "prompt_text": None, "tabId": 4})
            tools.browser_handle_dialog(accept=None)
            post.assert_called_with("/api/handle-dialog", {"accept": None, "prompt_text": None})

    def test_wordpress_workflow(self):
        path = Path(server.__file__).parent / "workflows/wp-html.md"
        commands, error = _parse_workflow(path, {"site_url": "https://example.com", "page_id": 5719})
        self.assertIsNone(error)
        self.assertEqual(commands[0], {"action": "navigate", "url": "https://example.com/wp-admin/post.php?post=5719&action=elementor"})
        self.assertEqual(commands[1]["action"], "waitForResult")
        self.assertIn("page_id: 5719", commands[2]["code"])


if __name__ == "__main__":
    unittest.main()
