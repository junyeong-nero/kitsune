"""Issue #402: a text wait met by text already on the page (a field's label) says so, and
new_only waits for a new occurrence. The extension side is tests/wait_for_new_text.cjs."""
import asyncio
import unittest
from unittest.mock import patch

from kitsune import bridge, playwright_tools, tools


class TextPage:
    """A page whose visible text is the next of texts at each read (the last one repeats);
    each read answers as content.js waitForText does with timeout 0."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    async def __call__(self, action, params=None, timeout=30):
        params = dict(params or {})
        self.calls.append(params)
        visible = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        text, seen = params["text"], params.get("text_seen")
        count = visible.count(text)
        appeared = count > seen if seen is not None else text in visible
        result = {"ok": appeared, "text": text, "text_gone": None, "elapsed": 0,
                  **({"text_count": count} if seen is not None else {})}
        if not appeared:
            result["error"] = f"Timed out after 0ms waiting for {'new ' if seen else ''}visible text"
        return result


LABEL = 'Generate report\nConfirmation code'
TOAST = LABEL + '\nReport ready. Confirmation code: 4A159A'


def run_wait(page, params):
    with patch.object(bridge, "send_to_extension", page):
        return asyncio.run(asyncio.wait_for(bridge._poll_page_wait("waitForText", params), 10))


class AlreadyPresentTests(unittest.TestCase):
    def test_text_in_a_label_at_the_start_reports_already_present(self):
        page = TextPage(LABEL)
        result = run_wait(page, {"text": "code", "timeout": 5000})
        self.assertIs(result["ok"], True)
        self.assertIs(result["already_present"], True)
        self.assertIn("new_only", result["hint"])
        self.assertEqual(page.calls, [{"text": "code", "timeout": 0}])

    def test_text_that_appears_later_is_not_already_present(self):
        result = run_wait(TextPage("Generating…", "Generating…", "Report ready"),
                          {"text": "ready", "timeout": 5000, "pollInterval": 10})
        self.assertIs(result["ok"], True)
        self.assertNotIn("already_present", result)
        self.assertNotIn("hint", result)

    def test_new_only_waits_past_the_label_for_a_new_occurrence(self):
        page = TextPage(LABEL, LABEL, TOAST)
        result = run_wait(page, {"text": "code", "timeout": 5000, "pollInterval": 10, "new_only": True})
        self.assertIs(result["ok"], True)
        self.assertNotIn("already_present", result)
        self.assertNotIn("text_count", result)
        self.assertEqual([call["text_seen"] for call in page.calls], [0, 1, 1])
        self.assertTrue(all("new_only" not in call for call in page.calls))

    def test_new_only_times_out_while_only_the_label_shows(self):
        result = run_wait(TextPage(LABEL), {"text": "code", "timeout": 300, "new_only": True})
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error"], "Timed out after 300ms waiting for new visible text")
        self.assertNotIn("text_count", result)
        zero = run_wait(TextPage(LABEL), {"text": "code", "timeout": 0, "new_only": True})
        self.assertEqual(zero["error"], "Timed out after 0ms waiting for new visible text")

    def test_new_only_text_absent_at_the_start_waits_for_any_occurrence(self):
        page = TextPage("Generating…", TOAST)
        result = run_wait(page, {"text": "4A159A", "timeout": 5000, "pollInterval": 10, "new_only": True})
        self.assertIs(result["ok"], True)
        self.assertEqual([call["text_seen"] for call in page.calls], [0, 0])


class ToolTests(unittest.TestCase):
    def setUp(self):
        post = patch.object(tools, "_post", return_value={"ok": True})
        self.post = post.start()
        self.addCleanup(post.stop)
        playwright_tools._current_tab = None

    def test_kitsune_tool_sends_new_only_only_for_text(self):
        tools.browser_wait_for(text="code", new_only=True, tab_id=9)
        self.assertEqual(self.post.call_args.args[:2], ("/api/wait-for-text", {
            "timeout": 10000, "text": "code", "text_gone": None, "new_only": True, "tabId": 9}))
        tools.browser_wait_for(text="code", tab_id=9)
        self.assertNotIn("new_only", self.post.call_args.args[1])
        self.post.reset_mock()
        self.assertIn("error", tools.browser_wait_for(text_gone="code", new_only=True))
        self.post.assert_not_called()

    def test_core_tool_accepts_new_only_and_names_it_in_the_hint(self):
        self.post.return_value = {"ok": True, "text": "code", "already_present": True,
                                  "hint": bridge.WAIT_ALREADY_PRESENT_HINT}
        result = playwright_tools.browser_wait_for(text="code")
        self.assertIs(result["already_present"], True)
        self.assertIn("newOnly", result["hint"])
        self.assertNotIn("new_only", self.post.call_args.args[1])
        playwright_tools.browser_wait_for(text="code", newOnly=True)
        self.assertIs(self.post.call_args.args[1]["new_only"], True)


if __name__ == "__main__":
    unittest.main()
