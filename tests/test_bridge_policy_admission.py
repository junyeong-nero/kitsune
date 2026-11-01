"""Direct JavaScript, session, trusted-click and cache endpoints go through the same admission (#193, #194)."""
import asyncio
import concurrent.futures
import json
import math
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kitsune import bridge, task_modes
from test_ask_before_act import AskBeforeActTestCase

TAB = 7

# Route, body, and the action it is admitted as.
JS_FULL = (lambda h: h.handle_js(), {"code": "1", "full": True, "tabId": TAB}, "executeJS")
WAIT_FOR_RESULT = (lambda h: h.handle_wait_for_result(), {"code": "1", "tabId": TAB}, "waitForResult")
SYNC_BARRIER = (lambda h: h.handle_sync_barrier(), {"tabIds": [TAB], "predicate": "1"}, "syncBarrier")
SAVE_SESSION = (lambda h: h.handle_save_session(), {"name": "work", "urls": []}, "saveSession")
LOAD_SESSION = (lambda h: h.handle_load_session(), {"name": "work"}, "loadSession")


class DirectAdmissionTests(AskBeforeActTestCase):
    ask = False

    def setUp(self):
        super().setUp()
        self.extension.reply = {"result": "1"}
        sessions = tempfile.TemporaryDirectory()
        self.addCleanup(sessions.cleanup)
        for patcher in (patch.object(bridge, "_sessions_dir", sessions.name), patch.dict(bridge._cache, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_bundle(self, cookies=(), storage=None):
        with open(bridge._session_path("work"), "w", encoding="utf-8") as f:
            json.dump({"name": "work", "cookies": list(cookies), "storage": storage or {}}, f)

    def audit(self):
        return [(e["action"], e["ok"], e["error"]) for e in bridge._audit_log]

    def test_read_only_refuses_javascript_paths_and_session_load_before_dispatch(self):
        bridge._policy["readonly"] = True
        self.save_bundle(storage={"https://example.com/": {"k": "v"}})
        for route, body, action in (JS_FULL, WAIT_FOR_RESULT, SYNC_BARRIER, LOAD_SESSION):
            with self.subTest(action=action):
                self.assertEqual(self.call(route, body),
                                 (403, {"error": f"Read-only mode is active; refusing action: {action}"}))
        [wait, barrier] = self.batch({"action": "waitForResult", "code": "1", "tabId": TAB},
                                     {"action": "syncBarrier", "tabIds": [TAB], "predicate": "1"})
        self.assertEqual(wait, {"error": "Read-only mode is active; refusing action: waitForResult"})
        self.assertEqual(barrier, {"error": "Read-only mode is active; refusing action: syncBarrier"})
        self.assertEqual(self.extension.sent, [])

    def test_read_only_allows_session_save_like_cookie_reads(self):
        bridge._policy["readonly"] = True
        self.assertIsNone(bridge._check_policy("cookies", {"op": "get"}))
        status, result = self.call(*SAVE_SESSION[:2])
        self.assertEqual(status, 200)
        self.assertIs(result["ok"], True)
        self.assertEqual(self.audit(), [("saveSession", True, None)])

    def test_direct_calls_are_audited_and_writes_clear_the_cache(self):
        self.save_bundle()
        for route, body, action in (JS_FULL, WAIT_FOR_RESULT, SYNC_BARRIER, SAVE_SESSION, LOAD_SESSION):
            with self.subTest(action=action):
                bridge._cache["page"] = (time.time(), {"text": "cached"})
                status, result = self.call(route, body)
                self.assertEqual(status, 200)
                self.assertFalse(result.get("error"), result)
                self.assertEqual(self.audit()[-1], (action, True, None))
                self.assertNotIn("code", bridge._audit_log[-1]["params"])
                self.assertNotIn("predicate", bridge._audit_log[-1]["params"])
                written = action in ("executeJS", "loadSession")
                self.assertEqual("page" in bridge._cache, not written)

    def test_url_policy_applies_to_full_js_and_every_session_url(self):
        bridge._policy["deny"] = [r"blocked\.example"]
        denied = {"error": r"URL denied by policy (blocked\.example): https://blocked.example/"}
        js = {"code": "1", "url": "https://blocked.example/", "tabId": TAB}
        self.assertEqual(self.call(lambda h: h.handle_js(), js), (403, denied))
        self.assertEqual(self.call(lambda h: h.handle_js(), {**js, "full": True}), (403, denied))

        status, result = self.call(lambda h: h.handle_save_session(),
                                   {"name": "work", "urls": ["https://ok.example/", "https://blocked.example/"]})
        self.assertEqual((status, result), (200, denied))
        self.assertFalse(bridge.os.path.exists(bridge._session_path("work")))

        self.save_bundle(cookies=[{"name": "sid", "value": "1", "domain": ".blocked.example", "secure": True}])
        self.assertEqual(self.call(*LOAD_SESSION[:2]), (200, denied))
        bridge._policy.update(deny=[], allow=[r"^https://ok\.example/"])
        self.save_bundle(storage={"https://other.example/": {"k": "v"}})
        self.assertEqual(self.call(*LOAD_SESSION[:2]),
                         (200, {"error": "URL not in allowlist: https://other.example/"}))
        self.assertEqual(self.extension.sent, [])
        self.assertEqual(self.audit(), [("saveSession", False, denied["error"]),
                                        ("loadSession", False, denied["error"]),
                                        ("loadSession", False, "URL not in allowlist: https://other.example/")])

    def test_read_and_draft_modes_refuse_these_requests_over_direct_http(self):
        self.save_bundle()
        for mode in ("read", "draft"):
            token = bridge._task_mode.set(mode)
            try:
                for route, body, action in (JS_FULL, WAIT_FOR_RESULT, SYNC_BARRIER, SAVE_SESSION, LOAD_SESSION):
                    with self.subTest(mode=mode, action=action):
                        status, result = self.call(route, body)
                        self.assertEqual((status, result["code"]), (403, "mode_restricted"))
            finally:
                bridge._task_mode.reset(token)
            handler = self.handler({})
            handler.headers = {task_modes.MODE_HEADER: mode}
            route = Mock()
            for path in ("/api/js", "/api/wait-for-result", "/api/sync-barrier", "/api/save-session",
                         "/api/load-session", "/api/saveSession", "/api/loadSession"):
                with self.subTest(mode=mode, path=path):
                    handler.path = path
                    handler._dispatch({bridge._API_ALIASES.get(path, path): route})
                    self.assertEqual(handler.send_json.call_args.args[1]["code"], "mode_restricted")
            route.assert_not_called()
        self.assertEqual(self.extension.sent, [])
        self.assertEqual(self.audit(), [])


class TrustedClickTimeoutTests(AskBeforeActTestCase):
    ask = False

    def test_timeout_returns_an_audited_timeout_error(self):
        def time_out(coro, **kwargs):
            if coro.cr_code.co_name == "_ask_before_act":
                return asyncio.run(coro)
            coro.close()
            raise concurrent.futures.TimeoutError()

        handler = self.handler({"selector": "#go", "tabId": TAB})
        handler.run_async = Mock(side_effect=time_out)
        handler.handle_trusted_click()
        status, result = handler.send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual((result["error"], result["code"], result["retryable"], result["state"]),
                         ("Command timed out after 35s", "timeout", False, {"timeoutSeconds": 35}))
        [entry] = bridge._audit_log
        self.assertEqual((entry["action"], entry["ok"], entry["error"], entry["params"]),
                         ("trustedClick", False, "Command timed out after 35s", {"selector": "#go", "tabId": TAB}))


class CacheTtlTests(AskBeforeActTestCase):
    ask = False

    def setUp(self):
        super().setUp()
        for patcher in (patch.object(bridge, "CACHE_TTL", 5), patch.dict(bridge._cache, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ttl_rejects_non_numbers_and_keeps_the_cache_usable(self):
        for seconds in ("10", None, True, [10], -1, math.nan):
            with self.subTest(seconds=seconds):
                self.assertEqual(self.call(lambda h: h.handle_cache(), {"action": "ttl", "seconds": seconds}),
                                 (400, {"error": "seconds must be a non-negative number"}))
                self.assertEqual(bridge.CACHE_TTL, 5)
        self.assertEqual(self.call(lambda h: h.handle_cache(), {"action": "ttl", "seconds": 0.5}),
                         (200, {"ok": True, "ttl": 0.5}))
        bridge._cache_set("page", {"text": "cached"})
        self.assertEqual(bridge._cache_get("page"), {"text": "cached"})


if __name__ == "__main__":
    unittest.main()
