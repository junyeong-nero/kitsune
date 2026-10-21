"""Strict selectors for write actions (#134): the `first` opt-in reaches the content script.

The content script decides ambiguity (tests/strict_selectors.cjs); these tests cover the
plumbing from MCP tools, uploads, batches and parallel sequences.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from kitsune import bridge, server
from test_container_progress import ContainerTests

AMBIGUOUS = {"error": "3 elements match the selector; nothing was dispatched", "code": "ambiguous_target",
             "retryable": False, "state": {"selector": "form button", "matches": 3, "candidates": []}}


class ServerFirstTests(unittest.TestCase):
    def test_write_tools_forward_first_only_when_set(self):
        cases = [
            ("click", {"selector": "#a"}, "/api/click"),
            ("double_click", {"selector": "#a"}, "/api/double-click"),
            ("type", {"selector": "#a", "text": "x"}, "/api/type"),
            ("type", {"selector": "#a", "text": "x", "mode": "fill"}, "/api/fill"),
            ("type", {"selector": "#a", "text": "x", "mode": "editable"}, "/api/set-editable-content"),
            ("check", {"selector": "#a"}, "/api/check"),
            ("select_option", {"selector": "#a", "value": "v"}, "/api/select-option"),
            ("submit_form", {"selector": "form"}, "/api/submit-form"),
            ("hover", {"selector": "#a"}, "/api/hover"),
            ("drag", {"from_selector": "#a", "to_selector": "#b"}, "/api/drag"),
            ("keypress", {"key": "Enter", "selector": "#a"}, "/api/keypress"),
            ("form_fill", {"fields": {"#a": "x"}}, "/api/form-fill"),
            ("upload_file", {"path": "/tmp/x", "selector": "#a"}, "/api/upload-file"),
        ]
        for name, kwargs, endpoint in cases:
            with self.subTest(tool=name, endpoint=endpoint), \
                    patch.object(server, "_post", return_value={"ok": True}) as post:
                getattr(server, "browser_" + name)(tab_id=7, first=True, **kwargs)
                self.assertEqual(post.call_args.args[0], endpoint)
                self.assertIs(post.call_args.args[1]["first"], True)
                getattr(server, "browser_" + name)(tab_id=7, **kwargs)
                self.assertNotIn("first", post.call_args.args[1])


class UploadFirstTests(unittest.TestCase):
    def test_upload_commit_carries_first(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"KITSUNE_FAKE_UPLOAD_134")
        self.addCleanup(os.unlink, f.name)
        for body, first in (({"first": True}, True), ({}, False)):
            send = AsyncMock(return_value={"ok": True})
            with self.subTest(first=first), patch.object(bridge, "send_to_extension", send):
                asyncio.run(bridge._upload_files_async({"path": f.name, "tabId": 3, "selector": ".upload", **body}))
                action, params = send.await_args.args[:2]
                self.assertEqual(action, "uploadCommit")
                self.assertIs(params["first"], first)


class ContainerFirstTests(ContainerTests):
    def test_batch_and_parallel_steps_keep_first(self):
        self.replies["click"] = [AMBIGUOUS]
        payload = self.batch({"action": "click", "selector": "form button", "tabId": 3},
                             {"action": "click", "selector": "form button", "tabId": 3, "first": True})
        self.assertEqual(payload["results"][0]["code"], "ambiguous_target")
        # Nothing was dispatched, so the step failed rather than having an unknown outcome.
        self.assertEqual(self.statuses(payload["progress"]), ["failed", "ok"])
        self.assertEqual(self.sent, [("click", {"selector": "form button", "tabId": 3}),
                                     ("click", {"selector": "form button", "tabId": 3, "first": True})])

        self.sent.clear()
        self.parallel([{"action": "click", "selector": "form button", "tabId": 4, "first": True}],
                      [{"action": "formFill", "fields": {"title": "x"}, "tabId": 5, "first": True}])
        sent = {action: params for action, params in self.sent}
        self.assertIs(sent["click"]["first"], True)
        self.assertIs(sent["formFill"]["first"], True)


if __name__ == "__main__":
    unittest.main()
