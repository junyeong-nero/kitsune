"""History and bookmark lookup (#140): MCP tools, bridge scope and policy, audit, containers.

The owned-bridge tests run `python -m kitsune.bridge` in a temporary KITSUNE_HOME/HOME on spare
ports (test_bridge_security.Bridge); a scripted extension answers over the real WebSocket. No
browser profile, history, bookmarks or user bridge is touched. The extension handlers themselves
are tested in tests/history_lookup.cjs.
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from kitsune import bridge, server

from test_bridge_security import Bridge, _request
from test_tab_inventory_bridge import ScriptedExtension

DAY_MS = 86_400_000
RESULT_URL = "https://result.example/private-path"


class HistoryToolTests(unittest.TestCase):
    def test_tools_post_their_arguments(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_history_search("kitsune")
            post.assert_called_with("/api/history-search", {"query": "kitsune", "limit": 20})
            server.browser_history_search("", since="2026-10-01T00:00:00Z", limit=5)
            post.assert_called_with("/api/history-search", {"query": "", "limit": 5, "since": 1790812800000})
            server.browser_history_search("", since="2026-10-01T09:00:00+09:00")
            self.assertEqual(post.call_args.args[1]["since"], 1790812800000)
            server.browser_history_visits(RESULT_URL)
            post.assert_called_with("/api/history-visits", {"url": RESULT_URL})
            server.browser_bookmarks_search("docs", limit=3)
            post.assert_called_with("/api/bookmarks-search", {"query": "docs", "limit": 3})

    def test_invalid_since_is_refused_before_the_bridge(self):
        with patch.object(server, "_post") as post:
            for since in ("yesterday", "", "2026-13-01"):
                self.assertIn("ISO 8601", server.browser_history_search("x", since=since)["error"])
            post.assert_not_called()

    def test_set_policy_sends_history_scope(self):
        with patch.object(server, "_post", return_value={"ok": True}) as post:
            server.browser_set_policy(history_allow_domains=["example.com"], history_deny_domains=["bank.test"],
                                      history_max_days=7)
            post.assert_called_with("/api/set-policy", {"historyAllowDomains": ["example.com"],
                                                        "historyDenyDomains": ["bank.test"], "historyMaxDays": 7})

    def test_descriptions_lead_with_the_key_facts(self):
        # #135: the first paragraph says what comes back and that the user must grant access.
        for function, returns in [(server.browser_history_search, "results [{url, title, last_visit, visit_count}]"),
                                  (server.browser_history_visits, "visits [{visit_time, transition"),
                                  (server.browser_bookmarks_search, "results [{url, title, folder}]")]:
            first = " ".join(function.__doc__.split("\n\n")[0].split())
            self.assertIn(returns, first)
            self.assertIn("permission_required", first)
        self.assertIn("ISO 8601", " ".join(server.browser_history_search.__doc__.split("\n\n")[0].split()))


class HistoryPolicyTests(unittest.TestCase):
    def setUp(self):
        policy = patch.dict(bridge._policy, {"historyAllowDomains": [], "historyDenyDomains": [],
                                             "historyMaxDays": 30})
        policy.start()
        self.addCleanup(policy.stop)

    def test_environment_defaults_and_parsing(self):
        self.assertEqual(bridge._history_policy_from_env({}),
                         {"historyAllowDomains": [], "historyDenyDomains": [], "historyMaxDays": 30})
        self.assertEqual(bridge._history_policy_from_env({
            "KITSUNE_HISTORY_ALLOW_DOMAINS": " Example.com, *.docs.test,, ",
            "KITSUNE_HISTORY_DENY_DOMAINS": "bank.test",
            "KITSUNE_HISTORY_MAX_DAYS": "7",
        }), {"historyAllowDomains": ["example.com", "docs.test"], "historyDenyDomains": ["bank.test"],
             "historyMaxDays": 7})
        for environ in ({"KITSUNE_HISTORY_MAX_DAYS": "0"}, {"KITSUNE_HISTORY_MAX_DAYS": "1.5"},
                        {"KITSUNE_HISTORY_MAX_DAYS": "-3"}, {"KITSUNE_HISTORY_DENY_DOMAINS": "https://bank.test/"},
                        {"KITSUNE_HISTORY_ALLOW_DOMAINS": "a b"}):
            with self.subTest(environ=environ), self.assertRaises(ValueError) as raised:
                bridge._history_policy_from_env(environ)
            self.assertIn(next(iter(environ)), str(raised.exception))

    def test_policy_updates_only_tighten(self):
        bridge._policy.update(historyAllowDomains=["example.com"], historyDenyDomains=["bank.test"])
        tighter = {"historyDenyDomains": ["bank.test", "mail.test"], "historyAllowDomains": ["docs.example.com"],
                   "historyMaxDays": 7}
        self.assertEqual(bridge._history_policy_update(tighter), (tighter, []))
        for body, problem in [
            ({"historyDenyDomains": []}, "cannot drop bank.test"),
            ({"historyAllowDomains": []}, "cannot be emptied"),
            ({"historyAllowDomains": ["example.com", "other.test"]}, "cannot add other.test"),
            ({"historyMaxDays": 31}, "cannot rise above 30"),
        ]:
            with self.subTest(body=body):
                self.assertIn(problem, "; ".join(bridge._history_policy_update(body)[1]))
        with self.assertRaises(ValueError):
            bridge._history_policy_update({"historyMaxDays": True})

    def test_url_policy_floor_only_rises(self):
        floor = patch.dict(bridge._history_url_floor, {"deny": [], "allow": []})
        floor.start()
        self.addCleanup(floor.stop)
        self.assertFalse(bridge._history_url_blocked("https://secret.test/"))
        bridge._raise_history_url_floor({"deny": [r"secret\.test"]})
        bridge._raise_history_url_floor({"deny": [], "allow": []})  # loosening page actions
        self.assertTrue(bridge._history_url_blocked("https://secret.test/"))
        bridge._raise_history_url_floor({"allow": ["example"]})
        bridge._raise_history_url_floor({"allow": ["docs", "news"]})
        self.assertFalse(bridge._history_url_blocked("https://docs.example.com/"))
        self.assertTrue(bridge._history_url_blocked("https://example.com/"), "every allow list must match")


class ContainerRejectionTests(unittest.TestCase):
    """History actions are not in BATCH_ACTION_MAP: no container can send them without the scope."""

    def test_workflow_steps_cannot_look_up_history(self):
        self.assertTrue(set(bridge.HISTORY_ACTIONS).isdisjoint(bridge.BATCH_ACTION_MAP))
        commands, error = bridge._parse_workflow_text(
            "".join(f'```json\n{{"action": "{action}", "query": "x", "url": "{RESULT_URL}"}}\n```\n'
                    for action in bridge.HISTORY_ACTIONS))
        self.assertIsNone(error)
        with patch.object(bridge, "_send_to_extension_raw", AsyncMock()) as send, \
             patch.object(bridge, "_audit_log", []):
            for command in commands:
                results = []
                asyncio.run(bridge._run_workflow("history", [command], {"call": 1, "path": []}, results))
                self.assertIn("Unknown batch action", results[0]["error"])
            send.assert_not_called()


class HistoryExtension(ScriptedExtension):
    """Records every command and answers history actions with a scripted reply."""

    def __init__(self, bridge_process, replies):
        self.replies = replies
        self.commands = []
        super().__init__(bridge_process, [])

    def _answer(self, command):
        with self.lock:
            self.commands.append(command)
        reply = self.replies.get(command["action"])
        return reply(command) if callable(reply) else reply or {"error": f"unscripted {command['action']}"}

    def history_commands(self):
        with self.lock:
            return [c for c in self.commands if c["action"] in bridge.HISTORY_ACTIONS]


def search_reply(*urls):
    return lambda command: {"ok": True, "results": [{"url": url, "title": "t", "last_visit": "2026-10-20T00:00:00.000Z",
                                                      "visit_count": 1} for url in urls], "count": len(urls)}


class OwnedBridgeHistoryTests(unittest.TestCase):
    ENV = {"KITSUNE_HISTORY_DENY_DOMAINS": "bank.test, *.Intranet.Example", "KITSUNE_HISTORY_MAX_DAYS": "7"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bridge = Bridge(temp.name)
        self.bridge.env.update(self.ENV)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.replies = {"historySearch": search_reply(RESULT_URL),
                        "historyVisits": {"ok": True, "url": RESULT_URL, "visits": [], "count": 0},
                        "bookmarksSearch": search_reply(RESULT_URL)}
        self.extension = HistoryExtension(self.bridge, self.replies)
        self.addCleanup(self.extension.close)

    def call(self, path, body=None, status=200):
        headers = self.bridge.auth()
        if body is not None:
            headers["Content-Type"] = "application/json"
        code, _, text = _request(self.bridge.http_port, "GET" if body is None else "POST", path, headers,
                                 self.bridge.host, None if body is None else json.dumps(body), timeout=20)
        self.assertEqual(code, status, text)
        return json.loads(text)

    def test_scope_from_the_environment_reaches_the_extension(self):
        now = int(time.time() * 1000)
        result = self.call("/api/history-search", {"query": "q", "limit": 5, "since": now - 30 * DAY_MS})
        self.assertEqual(result["results"][0]["url"], RESULT_URL)
        sent = self.extension.history_commands()[-1]
        self.assertEqual((sent["query"], sent["limit"], sent["allowDomains"], sent["denyDomains"], sent["maxDays"]),
                         ("q", 5, [], ["bank.test", "intranet.example"], 7))
        self.assertAlmostEqual(sent["startTime"], now - 7 * DAY_MS, delta=60_000, msg="since is clamped to the window")
        self.assertTrue(result["since"].startswith(time.strftime("%Y-%m-%d", time.gmtime(sent["startTime"] / 1000))))

        since = now - DAY_MS
        self.call("/api/history-search", {"query": "", "since": since})
        self.assertEqual(self.extension.history_commands()[-1]["startTime"], since)
        self.call("/api/history-visits", {"url": RESULT_URL})
        self.call("/api/bookmarks-search", {"query": "docs"})
        visits, bookmarks = self.extension.history_commands()[-2:]
        self.assertEqual(visits["url"], RESULT_URL)
        self.assertIn("startTime", visits)
        self.assertEqual(bookmarks["denyDomains"], ["bank.test", "intranet.example"])
        self.assertNotIn("startTime", bookmarks, "bookmarks have no time window")
        policy = self.call("/api/get-policy")
        self.assertEqual((policy["historyAllowDomains"], policy["historyDenyDomains"], policy["historyMaxDays"]),
                         ([], ["bank.test", "intranet.example"], 7))

    def test_permission_required_stays_not_retryable(self):
        self.replies["historySearch"] = {"error": "The user has not allowed Kitsune to read browser history",
                                         "code": "permission_required", "retryable": False,
                                         "hint": "about:addons → Kitsune → Options", "state": {"permission": "history"}}
        result = self.call("/api/history-search", {"query": "x"})
        self.assertEqual((result["code"], result["retryable"]), ("permission_required", False))
        entry = self.call("/api/audit")["entries"][-1]
        self.assertEqual((entry["action"], entry["ok"], entry["count"]), ("historySearch", False, 0))

    def test_invalid_lookups_are_refused_and_audited(self):
        for path, body in [
            ("/api/history-search", {"query": "x", "limit": 0}),
            ("/api/history-search", {"query": "x", "limit": 101}),
            ("/api/history-search", {"query": "x", "limit": "5"}),
            ("/api/history-search", {"query": 3}),
            ("/api/history-search", {"query": "x", "since": "yesterday"}),
            ("/api/bookmarks-search", {"query": "x", "limit": True}),
            ("/api/history-visits", {"url": "file:///etc/passwd"}),
            ("/api/history-visits", {}),
        ]:
            with self.subTest(path=path, body=body):
                self.assertIn("error", self.call(path, body))
        self.assertEqual(self.extension.history_commands(), [])
        entries = [e for e in self.call("/api/audit")["entries"] if e["action"] in bridge.HISTORY_ACTIONS]
        self.assertEqual(len(entries), 8)
        self.assertTrue(all(e["ok"] is False and e["count"] == 0 for e in entries))

    def test_set_policy_only_tightens_the_history_scope(self):
        tighten = {"historyDenyDomains": ["bank.test", "intranet.example", "mail.test"], "historyMaxDays": 3,
                   "historyAllowDomains": ["example.com"]}
        self.assertTrue(self.call("/api/set-policy", tighten)["ok"])
        self.assertTrue(self.call("/api/set-policy", {"historyAllowDomains": ["docs.example.com"]})["ok"])
        for body in [{"historyDenyDomains": ["mail.test"]}, {"historyMaxDays": 4},
                     {"historyAllowDomains": ["example.com"]}, {"historyAllowDomains": []},
                     {"readonly": True, "historyMaxDays": 30}]:
            with self.subTest(body=body):
                refused = self.call("/api/set-policy", body, status=403)
                self.assertEqual(refused["code"], "policy_locked")
                self.assertIn("KITSUNE_HISTORY_", refused["error"])
        invalid = self.call("/api/set-policy", {"historyDenyDomains": ["https://x.test/"]}, status=400)
        self.assertEqual(invalid["code"], "invalid_policy")
        policy = self.call("/api/get-policy")
        self.assertEqual((policy["historyAllowDomains"], policy["historyDenyDomains"], policy["historyMaxDays"],
                          policy["readonly"]),
                         (["docs.example.com"], ["bank.test", "intranet.example", "mail.test"], 3, False),
                         "a refused call changes nothing")
        self.call("/api/history-search", {"query": ""})
        sent = self.extension.history_commands()[-1]
        self.assertEqual((sent["allowDomains"], sent["maxDays"]), (["docs.example.com"], 3))

    def test_url_policy_keeps_filtering_history_after_it_is_loosened(self):
        self.replies["historySearch"] = search_reply("https://secret.test/a", "https://ok.test/1", "https://ok.test/2")
        self.assertTrue(self.call("/api/set-policy", {"deny": [r"secret\.test"]})["ok"])
        self.assertTrue(self.call("/api/set-policy", {"deny": []})["ok"])  # page actions may loosen
        result = self.call("/api/history-search", {"query": "", "limit": 1})
        self.assertEqual(self.extension.history_commands()[-1]["limit"], 100,
                         "the bridge asks for more results when its URL policy may drop some")
        self.assertEqual((result["results"], result["count"], result["truncated"]),
                         ([search_reply("https://ok.test/1")({})["results"][0]], 1, True))
        before = len(self.extension.history_commands())
        self.assertIn("denied by policy", self.call("/api/history-visits", {"url": "https://secret.test/a"})["error"])
        self.assertEqual(len(self.extension.history_commands()), before)
        search, visits = [e for e in self.call("/api/audit")["entries"] if e["action"] in bridge.HISTORY_ACTIONS]
        self.assertIs(search["filters"]["urlPolicy"], True)
        self.assertEqual((visits["ok"], visits["count"]), (False, 0))

    def test_audit_records_query_count_and_filters_never_result_urls(self):
        now = int(time.time() * 1000)
        self.call("/api/history-search", {"query": "quarterly report", "limit": 5, "since": now - DAY_MS})
        self.call("/api/bookmarks-search", {"query": "docs"})
        self.call("/api/history-visits", {"url": "https://visited.example/page"})
        entries = [e for e in self.call("/api/audit")["entries"] if e["action"] in bridge.HISTORY_ACTIONS]
        search, bookmarks, visits = entries
        self.assertEqual(search["params"], {"query": "quarterly report", "limit": 5, "since": now - DAY_MS})
        self.assertEqual((search["ok"], search["count"]), (True, 1))
        self.assertEqual(search["filters"], {"allowDomains": [], "denyDomains": ["bank.test", "intranet.example"],
                                             "maxDays": 7, "startTime": now - DAY_MS, "urlPolicy": False})
        self.assertEqual(bookmarks["params"], {"query": "docs", "limit": 20})
        self.assertNotIn("startTime", bookmarks["filters"])
        self.assertEqual((visits["params"], visits["count"]), ({"url": "https://visited.example/page"}, 0))
        self.assertNotIn(RESULT_URL, json.dumps(self.call("/api/audit")))

    def test_containers_cannot_send_history_actions(self):
        tabs = {"getTabs": {"tabs": [{"id": 1, "url": "https://a.test/", "active": True, "windowId": 1}]}}
        self.replies.update(tabs)
        for action in bridge.HISTORY_ACTIONS:
            command = {"action": action, "query": "x", "url": RESULT_URL, "tabId": 1}
            for container, path, body in [
                ("batch", "/api/batch", {"commands": [command]}),
                ("parallel", "/api/batch", {"commands": [{"action": "parallel", "sequences": [[command]]}]}),
                ("broadcast", "/api/broadcast", {"tabIds": [1], "command": command}),
                ("retry", "/api/retry", {"command": command, "maxAttempts": 1}),
            ]:
                with self.subTest(action=action, container=container):
                    self.assertIn(f"Unknown batch action: {action}", json.dumps(self.call(path, body)))
        self.assertEqual(self.extension.history_commands(), [])


if __name__ == "__main__":
    unittest.main()
