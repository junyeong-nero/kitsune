"""A navigate's relative url is resolved against its tab's URL before the URL policy and
ask-before-act read it, and a url that does not parse is a coded error (#571)."""
import unittest

from kitsune import bridge, server
from kitsune.browser import Tab
from test_ask_before_act import AskBeforeActTestCase

ORIGIN = "http://localhost:9101"
TAB = 7
NAVIGATE = lambda h: h.handle_navigate()  # noqa: E731
# What the extension answers for a url that is not absolute (tests/navigate_invalid_url.cjs).
INVALID_URL = {"error": "Invalid URL: /deals/48", "code": "invalid_url", "retryable": False,
               "state": {"url": "/deals/48"}, "hint": "Pass an absolute URL"}


class RelativeNavigateTests(AskBeforeActTestCase):
    ask = False

    def setUp(self):
        super().setUp()
        self.extension.url = ORIGIN + "/deals?page=2"

    def navigated(self):
        return [params for action, params in self.extension.sent if action == "navigate"]

    def test_direct_navigate_resolves_against_the_tab_url(self):
        for url, loaded in (("/deals/48", ORIGIN + "/deals/48"), ("deals/48", ORIGIN + "/deals/48"),
                            ("?page=3", ORIGIN + "/deals?page=3"), ("#notes", ORIGIN + "/deals?page=2#notes"),
                            (" /deals/48 ", ORIGIN + "/deals/48"), ("//other.example/x", "http://other.example/x")):
            with self.subTest(url=url):
                self.extension.sent.clear()
                self.assertEqual(self.call(NAVIGATE, {"url": url, "tabId": TAB}), (200, {"ok": True}))
                self.assertEqual(self.extension.sent, [("describeTarget", {"tabId": TAB, "inspect": False}),
                                                       ("navigate", {"url": loaded, "tabId": TAB})])
                self.assertEqual(bridge._audit_log[-1]["params"]["url"], loaded)

    def test_implicit_tab_is_pinned_to_the_tab_the_url_was_resolved_against(self):
        self.call(NAVIGATE, {"url": "/deals/48"})
        self.assertEqual(self.navigated(), [{"url": ORIGIN + "/deals/48", "tabId": self.extension.tab}])

    def test_batch_step_is_resolved_too(self):
        self.assertEqual(self.batch({"action": "navigate", "url": "/deals/48", "tabId": TAB}), [{"ok": True}])
        self.assertEqual(self.navigated(), [{"url": ORIGIN + "/deals/48", "tabId": TAB}])

    def test_absolute_and_malformed_urls_are_sent_as_given_without_a_round_trip(self):
        for url in (ORIGIN + "/deals/48", f"{ORIGIN}{ORIGIN}/deals/48", "about:blank", "file:///tmp/a.html", ""):
            with self.subTest(url=url):
                self.extension.sent.clear()
                self.call(NAVIGATE, {"url": url, "tabId": TAB})
                self.assertEqual(self.extension.sent, [("navigate", {"url": url, "tabId": TAB})])

    def test_unresolvable_relative_url_is_left_for_the_extension_to_refuse(self):
        self.extension.reply = INVALID_URL
        for base in ("about:blank", "moz-extension://0a1b/options/options.html", None):
            with self.subTest(base=base):
                self.extension.sent.clear()
                self.extension.url = base
                self.assertEqual(self.call(NAVIGATE, {"url": "/deals/48", "tabId": TAB}), (200, INVALID_URL))
                self.assertEqual(self.navigated(), [{"url": "/deals/48", "tabId": TAB}])

    def test_url_policy_reads_the_resolved_url(self):
        bridge._policy["deny"] = [r"^http://localhost:9101/admin"]
        self.assertEqual(self.call(NAVIGATE, {"url": "/admin/users", "tabId": TAB}),
                         (403, {"error": "URL denied by policy (^http://localhost:9101/admin): "
                                         f"{ORIGIN}/admin/users"}))
        self.assertEqual(self.batch({"action": "navigate", "url": "../admin", "tabId": TAB}),
                         [{"error": f"URL denied by policy (^http://localhost:9101/admin): {ORIGIN}/admin"}])
        self.assertEqual(self.navigated(), [])

        bridge._policy.update(deny=[], allow=[r"^http://localhost:9101/"])
        self.assertEqual(self.call(NAVIGATE, {"url": "//other.example/x", "tabId": TAB}),
                         (403, {"error": "URL not in allowlist: http://other.example/x"}))
        self.assertEqual(self.navigated(), [])
        self.assertEqual(self.call(NAVIGATE, {"url": "/deals/48", "tabId": TAB}), (200, {"ok": True}))
        self.assertEqual(self.navigated(), [{"url": ORIGIN + "/deals/48", "tabId": TAB}])

    def test_relative_image_url_is_wrapped_like_an_absolute_one(self):
        self.call(NAVIGATE, {"url": "/img/a.png", "tabId": TAB})
        [params] = self.navigated()
        self.assertEqual(params["wrappedImage"], ORIGIN + "/img/a.png")
        self.assertTrue(params["url"].startswith(f"http://127.0.0.1:{bridge.HTTP_PORT}/view?src="), params["url"])

    def test_invalid_url_is_a_failure_that_reached_nothing(self):
        # Not "unknown": retryable is false because repeating cannot succeed, not because it may have run.
        self.assertEqual(bridge._result_status(INVALID_URL), "failed")
        [result] = self.batch({"action": "navigate", "url": f"{ORIGIN}{ORIGIN}/deals/48", "tabId": TAB})
        self.assertEqual(result, {"ok": True}, "a malformed absolute url is the extension's to refuse")


class RelativeNavigateAskTests(AskBeforeActTestCase):
    def test_ask_before_act_reads_the_resolved_url(self):
        result = self.held(NAVIGATE, {"url": "/checkout", "tabId": TAB})
        self.assertEqual(result["state"]["holdReason"], "payment_url")
        self.assertEqual(result["state"]["params"], {"url": "https://shop.example/checkout", "tabId": TAB})
        self.assertEqual(self.extension.actions(), [])

        confirmed = {"url": "/checkout", "tabId": TAB, "confirmToken": result["state"]["confirmToken"]}
        self.assertEqual(self.call(NAVIGATE, confirmed), (200, {"ok": True}))
        self.assertEqual(self.extension.sent[-1], ("navigate", {"url": "https://shop.example/checkout", "tabId": TAB}))

    def test_refused_invalid_url_is_not_held_as_an_unknown_outcome(self):
        self.extension.reply = INVALID_URL
        self.extension.url = "about:blank"
        for _ in range(2):
            self.assertEqual(self.call(NAVIGATE, {"url": "/deals/48", "tabId": TAB}), (200, INVALID_URL))
        self.assertEqual(bridge._unknown_outcomes, {})


class DocumentationTests(unittest.TestCase):
    def test_query_and_navigate_say_how_urls_are_read(self):
        self.assertIn("href and src are absolute URLs", " ".join(Tab.query.__doc__.split()))
        self.assertIn("relative", Tab.navigate.__doc__)
        self.assertIn("Its href/src are absolute URLs", server.browser_run_python.__doc__)


if __name__ == "__main__":
    unittest.main()
