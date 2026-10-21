"""Issue #140 in a real browser: history and bookmark lookup behind the user's grant.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. Everything
happens in the harness's temporary profile: the history and bookmarks are disposable fixtures
seeded through the parent process (RemoteDebugger.evaluate_in_parent), and the optional
permission is granted and revoked there with ExtensionPermissions, as the browser's own prompt
on the options page would. No user profile, history or bookmarks are read.
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest

import real_browser_harness as harness

FIXTURE_TOKEN = "kitsune-fixture-token-1"
FIXTURE_SESSION = "kitsune-fixture-session"
DENIED_HOST = "denied.kitsune-fixture.test"

# Visits a day or two old, one outside the default 30-day window, and a bookmark folder.
SEED_SCRIPT = """(async () => {
  const [origin, deniedHost, token, session] = %s;
  const { PlacesUtils } = ChromeUtils.importESModule('resource://gre/modules/PlacesUtils.sys.mjs');
  const DAY = 86400000, now = Date.now();
  const { LINK, TYPED } = PlacesUtils.history.TRANSITIONS;
  const visit = (days, transition = LINK) => ({ date: new Date(now - days * DAY), transition });
  await PlacesUtils.history.insertMany([
    { url: origin + '/article', title: 'Kitsune fixture article', visits: [visit(1), visit(2, TYPED)] },
    { url: origin + '/dl?token=' + token, title: 'Kitsune fixture download', visits: [visit(1)] },
    { url: 'https://' + deniedHost + '/page', title: 'Kitsune fixture denied', visits: [visit(1)] },
    { url: origin + '/old', title: 'Kitsune fixture old', visits: [visit(40)] },
  ]);
  const folder = await PlacesUtils.bookmarks.insert({
    parentGuid: PlacesUtils.bookmarks.toolbarGuid, type: PlacesUtils.bookmarks.TYPE_FOLDER, title: 'Kitsune fixtures',
  });
  await PlacesUtils.bookmarks.insert({
    parentGuid: folder.guid, url: origin + '/bookmarked?sessionid=' + session, title: 'Kitsune fixture bookmark',
  });
  return 'seeded';
})()"""

# op is "add" (what the browser does when the user accepts the prompt) or "remove" (revoke).
PERMISSION_SCRIPT = """(async () => {
  const [id, op] = %s;
  const { ExtensionPermissions } = ChromeUtils.importESModule('resource://gre/modules/ExtensionPermissions.sys.mjs');
  const policy = WebExtensionPolicy.getByID(id);
  await ExtensionPermissions[op](id, { permissions: ['history', 'bookmarks'], origins: [] }, policy.extension);
  return (await ExtensionPermissions.get(id)).permissions.filter(p => p === 'history' || p === 'bookmarks').join(',');
})()"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<title>fixture {self.path.split('?')[0]}</title><h1>fixture</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _HistoryCase(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.page.stop)
        super().setUpClass()
        seeded = cls.parent(SEED_SCRIPT % json.dumps([cls.page.origin, DENIED_HOST, FIXTURE_TOKEN, FIXTURE_SESSION]))
        if seeded != "seeded":
            raise AssertionError(f"seeding the temporary profile failed: {seeded}")

    @classmethod
    def parent(cls, script):
        debugger = harness.RemoteDebugger(cls.browser.rdp_port)
        try:
            return debugger.evaluate_in_parent(script)
        finally:
            debugger.close()

    @classmethod
    def set_permission(cls, op):
        return cls.parent(PERMISSION_SCRIPT % json.dumps([harness.EXTENSION_ID, op]))

    def lookups(self):
        return {
            "search": self.call("/api/history-search", {"query": "Kitsune fixture"}),
            "visits": self.call("/api/history-visits", {"url": self.page.origin + "/article"}),
            "bookmarks": self.call("/api/bookmarks-search", {"query": "Kitsune fixture"}),
        }


class RealBrowserHistoryPermissionTests(_HistoryCase):
    def test_lookups_need_the_users_grant_and_stop_after_revocation(self):
        for name, result in self.lookups().items():
            with self.subTest(before="grant", lookup=name):
                self.assert_error(result, "permission_required", False)
                self.assertIn("Options", result["hint"])

        self.assertEqual(self.set_permission("add"), "history,bookmarks")
        for name, result in self.lookups().items():
            with self.subTest(after="grant", lookup=name):
                self.assert_ok(result)

        self.assertEqual(self.set_permission("remove"), "")
        for name, result in self.lookups().items():
            with self.subTest(after="revocation", lookup=name):
                self.assert_error(result, "permission_required", False)


class RealBrowserHistoryTests(_HistoryCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.set_permission("add")

    def search(self, query="Kitsune fixture"):
        return self.assert_ok(self.call("/api/history-search", {"query": query}))

    def test_search_returns_redacted_results_inside_the_window(self):
        result = self.search()
        by_title = {item["title"]: item for item in result["results"]}
        self.assertEqual(set(by_title["Kitsune fixture article"]), {"url", "title", "last_visit", "visit_count"})
        self.assertEqual(by_title["Kitsune fixture article"]["url"], self.page.origin + "/article")
        self.assertGreaterEqual(by_title["Kitsune fixture article"]["visit_count"], 2)
        self.assertEqual(by_title["Kitsune fixture download"]["url"], self.page.origin + "/dl?token=[redacted]")
        self.assertNotIn("Kitsune fixture old", by_title, "older than the 30-day default window")
        self.assertNotIn(FIXTURE_TOKEN, json.dumps(result))
        self.assertNotIn(FIXTURE_TOKEN, json.dumps(self.call("/api/audit")))

    def test_policy_denied_domains_leave_the_results(self):
        self.assertIn(f"https://{DENIED_HOST}/page", [item["url"] for item in self.search()["results"]])
        self.assert_ok(self.call("/api/set-policy", {"historyDenyDomains": [DENIED_HOST]}))
        self.assertNotIn(f"https://{DENIED_HOST}/page", [item["url"] for item in self.search()["results"]])

    def test_visits_follow_the_trail(self):
        article = self.assert_ok(self.call("/api/history-visits", {"url": self.page.origin + "/article"}))
        # Newest first; test_a_history_result_opens_in_a_tab may have added a newer visit.
        self.assertEqual([visit["transition"] for visit in article["visits"]][-2:], ["link", "typed"])
        redacted = self.assert_ok(self.call("/api/history-visits", {"url": self.page.origin + "/dl?token=[redacted]"}))
        self.assertEqual(redacted["count"], 1, "a redacted result resolves to the URL it stands for")
        self.assertNotIn(FIXTURE_TOKEN, json.dumps(redacted))

    def test_bookmarks_report_their_folder(self):
        result = self.assert_ok(self.call("/api/bookmarks-search", {"query": "Kitsune fixture"}))
        self.assertEqual(len(result["results"]), 1, result)
        bookmark = result["results"][0]
        self.assertEqual(bookmark["url"], self.page.origin + "/bookmarked?sessionid=[redacted]")
        self.assertTrue(bookmark["folder"].endswith("/Kitsune fixtures"), bookmark)
        self.assertNotIn(FIXTURE_SESSION, json.dumps(result))

    def test_a_history_result_opens_in_a_tab(self):
        # Trace following: a result's url is enough to reopen the page (#139 adds agent-owned
        # background tabs; this uses the tab opening that exists today).
        url = next(item["url"] for item in self.search()["results"] if item["title"] == "Kitsune fixture article")
        tab_id = self.open_tab(url)
        self.assertEqual(self.call("/api/page-info", {"tabId": tab_id})["title"], "fixture /article")


if __name__ == "__main__":
    unittest.main()
