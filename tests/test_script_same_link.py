"""#572: only a browser_run_python script's own command may follow a stale link ref.

The extension decides whether a stale ref has a same link to follow, and only when the bridge
marks the command (followSameLink). The bridge marks the commands of a request whose tool header
is the script tool's, never a container step's, and a caller cannot supply the mark.
"""
import json
import tempfile
import unittest

from kitsune import task_modes, tool_approvals
from test_bridge_security import Bridge, _request
from test_tab_inventory_bridge import ScriptedExtension, tab


class ClickingExtension(ScriptedExtension):
    def __init__(self, bridge, tabs):
        self.clicks = []
        super().__init__(bridge, tabs)

    def _answer(self, command):
        if command["action"] == "click":
            with self.lock:
                self.clicks.append(command)
            return {"ok": True}
        return super()._answer(command)


class ScriptRequestMarkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name).start()
        self.addCleanup(self.bridge.stop)
        self.extension = ClickingExtension(self.bridge, [tab(1, "https://a.test/", active=True)])
        self.addCleanup(self.extension.close)

    def click(self, path, body, tool=None):
        """The click command the extension received for one request."""
        headers = {**self.bridge.auth(), "Content-Type": "application/json",
                   **({task_modes.TOOL_HEADER: tool} if tool else {})}
        status, _, text = _request(self.bridge.http_port, "POST", path, headers, self.bridge.host, json.dumps(body))
        self.assertEqual(status, 200, text)
        self.assertEqual(len(self.extension.clicks), 1, text)
        return self.extension.clicks.pop()

    def test_only_a_scripts_own_command_is_marked(self):
        target = {"tabId": 1, "selector": "", "ref": "e7"}
        self.assertIs(self.click("/api/click", target, tool_approvals.SCRIPT_TOOL)["followSameLink"], True)
        # A direct tool, the Python API (no tool header) and a caller's own mark.
        self.assertNotIn("followSameLink", self.click("/api/click", target, "browser_click"))
        self.assertNotIn("followSameLink", self.click("/api/click", target))
        self.assertNotIn("followSameLink", self.click("/api/click", {**target, "followSameLink": True}))
        self.assertNotIn("followSameLink", self.click("/api/click", {**target, "followSameLink": True}, "browser_click"))
        # A container a script runs: its steps' results do not reach the script runner one by one.
        batch = {"commands": [{"action": "click", **target, "followSameLink": True}]}
        self.assertNotIn("followSameLink", self.click("/api/batch", batch, tool_approvals.SCRIPT_TOOL))


if __name__ == "__main__":
    unittest.main()
