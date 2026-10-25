"""browser_wait_for's text and selector waits end at their timeout in a throttled tab (#209)."""
import asyncio
import time
import unittest
from unittest.mock import patch

from kitsune import bridge


class ThrottledPage:
    """A background tab whose timers fire late: an in-page wait overshoots its timeout.

    Each single read (timeout 0) returns the next scripted reply; the last one repeats.
    """

    LATE_SECONDS = 0.5

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, action, params=None, timeout=30):
        self.calls.append((action, dict(params or {})))
        if (params or {}).get("timeout", 10000) > 0:
            await asyncio.sleep(params.get("timeout", 10000) / 1000 + self.LATE_SECONDS)
        return dict(self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


UNMET_TEXT = {"ok": False, "text": "Saved", "elapsed": 0,
              "error": "Timed out after 0ms waiting for visible text"}
UNMET_ELEMENT = {"ok": False, "found": False, "selector": "#menu", "elapsed": 0,
                 "error": 'Timed out after 0ms waiting for "#menu"'}
TIMEOUT_MS = 300


def run_wait(page, action, params):
    start = time.monotonic()
    with patch.object(bridge, "send_to_extension", page):
        result = asyncio.run(bridge._poll_page_wait(action, params))
    return result, (time.monotonic() - start) * 1000


class PageWaitDeadlineTests(unittest.TestCase):
    def assert_timed_out_at_deadline(self, result, wall_ms):
        self.assertIs(result["ok"], False)
        self.assertGreaterEqual(result["elapsed"], TIMEOUT_MS)
        self.assertLess(result["elapsed"], TIMEOUT_MS + 200)
        self.assertLess(wall_ms, TIMEOUT_MS + 200)

    def test_unmet_text_wait_ends_at_its_timeout_with_the_requested_timeout(self):
        page = ThrottledPage(UNMET_TEXT)
        result, wall_ms = run_wait(page, "waitForText", {"tabId": 7, "text": "Saved", "timeout": TIMEOUT_MS})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertEqual(result["error"], f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
        self.assertGreater(len(page.calls), 1)
        self.assertEqual(page.calls[0], ("waitForText", {"tabId": 7, "text": "Saved", "timeout": 0}))

    def test_unmet_selector_wait_ends_at_its_timeout(self):
        page = ThrottledPage(UNMET_ELEMENT)
        result, wall_ms = run_wait(page, "waitForElement", {"tabId": 7, "selector": "#menu",
                                                            "timeout": TIMEOUT_MS, "pollInterval": 20})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertIs(result["found"], False)
        self.assertEqual(result["error"], f'Timed out after {TIMEOUT_MS}ms waiting for "#menu"')
        self.assertGreater(len(page.calls), 2)
        self.assertEqual(page.calls[0][1]["timeout"], 0)

    def test_condition_met_on_a_later_read_ends_the_wait(self):
        found = {"ok": True, "found": True, "selector": "#menu", "elapsed": 0, "ref": "e3", "tag": "nav"}
        page = ThrottledPage(UNMET_ELEMENT, UNMET_ELEMENT, found)
        result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 5000, "pollInterval": 10})
        self.assertEqual(len(page.calls), 3)
        self.assertEqual({k: result[k] for k in ("ok", "found", "ref", "tag")},
                         {"ok": True, "found": True, "ref": "e3", "tag": "nav"})
        self.assertLess(result["elapsed"], 1000)

    def test_page_errors_are_returned_without_polling(self):
        for reply in ({"error": "Invalid selector"},
                      {"error": "Element ref is stale", "code": "stale_ref"},
                      {"error": "Content script unavailable", "code": "content_unavailable"}):
            page = ThrottledPage(reply)
            result, _ = run_wait(page, "waitForElement", {"selector": "#menu", "timeout": 5000})
            self.assertEqual(result, reply)
            self.assertEqual(len(page.calls), 1)

    def test_a_zero_timeout_is_one_read(self):
        page = ThrottledPage(UNMET_TEXT)
        result, _ = run_wait(page, "waitForText", {"text": "Saved", "timeout": 0})
        self.assertEqual(page.calls, [("waitForText", {"text": "Saved", "timeout": 0, "diagnose": True})])
        self.assertEqual(result["error"], "Timed out after 0ms waiting for visible text")

    def test_only_an_unmet_text_waits_last_read_asks_for_names(self):
        # #275: the text is an element's name, which the timed-out result reports.
        named = {**UNMET_TEXT, "matched_as": "accessible_name", "hint": "Found as the name of textbox",
                 "name_matches": [{"ref": "e0", "role": "textbox", "name": "Saved", "source": "placeholder"}]}
        page = ThrottledPage(UNMET_TEXT, UNMET_TEXT, named)
        result, wall_ms = run_wait(page, "waitForText", {"text": "Saved", "timeout": TIMEOUT_MS})
        self.assert_timed_out_at_deadline(result, wall_ms)
        self.assertEqual(result["error"], f"Timed out after {TIMEOUT_MS}ms waiting for visible text")
        self.assertEqual(result["name_matches"], named["name_matches"])
        self.assertEqual(result["matched_as"], "accessible_name")
        asked = [params.get("diagnose") for _, params in page.calls]
        self.assertIs(asked[-1], True)
        self.assertNotIn(True, asked[:-2])

        met = ThrottledPage({"ok": True, "text": "Saved", "elapsed": 0})
        run_wait(met, "waitForText", {"text": "Saved", "timeout": 5000})
        self.assertEqual(met.calls, [("waitForText", {"text": "Saved", "timeout": 0})])

        gone = ThrottledPage(UNMET_TEXT)
        run_wait(gone, "waitForText", {"text_gone": "Saved", "timeout": 0})
        self.assertNotIn("diagnose", gone.calls[0][1])

    def test_batch_wait_for_element_polls_single_reads(self):
        page = ThrottledPage(UNMET_ELEMENT)
        start = time.monotonic()
        with patch.object(bridge, "send_to_extension", page):
            result = asyncio.run(bridge._execute_command(
                "waitForElement", "waitForElement",
                {"selector": "#menu", "timeout": TIMEOUT_MS, "pollInterval": 20}))
        self.assert_timed_out_at_deadline(result, (time.monotonic() - start) * 1000)
        self.assertTrue(all(params["timeout"] == 0 for _, params in page.calls))


if __name__ == "__main__":
    unittest.main()
