"""Page waits end at their requested timeout in a throttled tab (issues #202 and #209).

Covers an action's expect check and browser_wait_for's text and selector waits.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
Firefox throttles timers in background tabs: at least dom.min_background_timeout_value
(1 s) apart, and on a heavy page up to dom.timeout.budget_throttling_max_delay (15 s).
A check that waits with the page's own timers ends when the browser lets it, not at
expect.timeout. Agent tabs open in the background, so the fixture tab is throttled.
"""
from http.server import BaseHTTPRequestHandler
import time

import real_browser_harness as harness

PAGE = """<!doctype html><title>Ad-block notice</title>
<div class="modal">Are you using an ad blocker? <span class="still">Still shown</span></div>
<button type="button">Close</button>"""
# Just past a whole second, so a 1 s throttled timer overshoots it by most of a second.
TIMEOUT_MS = 1050
# Margin for the final observation, under the throttled timer's overshoot.
SLACK_MS = 500


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/notice":
            self.send_error(404)
            return
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RealBrowserOutcomeDeadlineTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)

    def throttled_tab(self):
        return self.open_tab(self.site.origin + "/notice")  # opened in the background

    def click_expecting(self, tab, expect):
        start = time.monotonic()
        result = self.call("/api/click", {"tabId": tab, "selector": "button",
                                          "expect": {**expect, "timeout": TIMEOUT_MS}})
        return result, (time.monotonic() - start) * 1000

    def assert_unmet_near_deadline(self, result, wall_ms):
        self.assertEqual(result.get("code"), "outcome_not_observed", result)
        self.assertIs(result["dispatched"], True, result)
        outcome = result["outcome"]
        self.assertIs(outcome["met"], False, result)
        self.assertNotIn("error", outcome["observed"], result)  # observed from the page, not a timeout
        self.assertGreaterEqual(outcome["elapsed"], TIMEOUT_MS, result)
        self.assertLess(outcome["elapsed"], TIMEOUT_MS + SLACK_MS, result)
        self.assertLess(wall_ms, TIMEOUT_MS + 3 * SLACK_MS, result)
        return outcome["observed"]

    def test_text_gone_on_persistent_text_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.click_expecting(tab, {"text_gone": "Still shown"})
        observed = self.assert_unmet_near_deadline(result, wall_ms)
        self.assertEqual(observed, {"source": "page", "textVisible": True})

    def test_text_that_never_appears_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.click_expecting(tab, {"text": "Notice closed"})
        observed = self.assert_unmet_near_deadline(result, wall_ms)
        self.assertEqual(observed, {"source": "page", "textVisible": False})

    def test_selector_that_never_appears_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.click_expecting(tab, {"selector": ".closed-banner"})
        observed = self.assert_unmet_near_deadline(result, wall_ms)
        self.assertEqual(observed, {"source": "page", "visible": False})

    def test_met_text_gone_is_still_observed(self):
        # Unchanged behaviour: a check that is met ends early with met=true.
        tab = self.throttled_tab()
        armed = self.call("/api/js", {"tabId": tab, "code":
            "document.querySelector('button').onclick = () => document.querySelector('.modal').remove(); true"})
        self.assertNotIn("error", armed)
        result, _ = self.click_expecting(tab, {"text_gone": "Still shown"})
        self.assertIs(result.get("ok"), True, result)
        self.assertIs(result["outcome"]["met"], True, result)
        self.assertEqual(result["outcome"]["observed"], {"source": "page", "textVisible": False})

    def wait_for(self, path, body):
        start = time.monotonic()
        result = self.call(path, {**body, "timeout": TIMEOUT_MS})
        return result, (time.monotonic() - start) * 1000

    def assert_timed_out_near_deadline(self, result, wall_ms):
        self.assertIs(result.get("ok"), False, result)
        self.assertNotIn("code", result)  # observed from the page, not a command timeout
        if "observationIncomplete" in result:
            # A final read the throttled tab did not answer in time (#292) is reported as such.
            self.assertIs(result["observationIncomplete"], True, result)
            self.assertIn("the read in progress at the timeout did not finish", result["error"], result)
        self.assertIn(f"Timed out after {TIMEOUT_MS}ms", result.get("error", ""), result)
        self.assertGreaterEqual(result["elapsed"], TIMEOUT_MS, result)
        self.assertLess(result["elapsed"], TIMEOUT_MS + SLACK_MS, result)
        self.assertLess(wall_ms, TIMEOUT_MS + 3 * SLACK_MS, result)

    def test_wait_for_text_that_never_appears_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.wait_for("/api/wait-for-text", {"tabId": tab, "text": "Notice closed"})
        self.assert_timed_out_near_deadline(result, wall_ms)

    def test_wait_for_text_gone_on_persistent_text_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.wait_for("/api/wait-for-text", {"tabId": tab, "text_gone": "Still shown"})
        self.assert_timed_out_near_deadline(result, wall_ms)

    def test_wait_for_selector_that_never_appears_ends_at_its_timeout(self):
        tab = self.throttled_tab()
        result, wall_ms = self.wait_for("/api/wait-for-element", {"tabId": tab, "selector": ".closed-banner"})
        self.assert_timed_out_near_deadline(result, wall_ms)
        self.assertIs(result["found"], False, result)

    def test_met_waits_are_still_reported(self):
        # Unchanged behaviour: a wait that is met ends early with ok=true.
        tab = self.throttled_tab()
        result, _ = self.wait_for("/api/wait-for-text", {"tabId": tab, "text": "Still shown"})
        self.assertIs(result.get("ok"), True, result)
        result, _ = self.wait_for("/api/wait-for-element", {"tabId": tab, "selector": "button"})
        self.assertIs(result.get("ok"), True, result)
        self.assertIs(result["found"], True, result)
        self.assertEqual(result["tag"], "button", result)
        self.assertTrue(result["ref"], result)

    def test_text_that_appears_during_a_wait_is_observed(self):
        tab = self.throttled_tab()
        armed = self.call("/api/js", {"tabId": tab, "code":
            "document.querySelector('.modal').remove(); true"})
        self.assertNotIn("error", armed)
        result, _ = self.wait_for("/api/wait-for-text", {"tabId": tab, "text_gone": "Still shown"})
        self.assertIs(result.get("ok"), True, result)


if __name__ == "__main__":
    import unittest
    unittest.main()
