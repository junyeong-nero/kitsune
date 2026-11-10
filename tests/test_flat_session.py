"""Caller-local admission/handle tests, not native dispatch or delegated conformance."""
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from kitsune.connection_binding import ConnectionBinding
from kitsune.flat_operations import OperationRefused
from kitsune.flat_session import CallerAuthority, FlatSession


CONNECTION = ConnectionBinding("a" * 32, 1)


class FlatSessionTests(unittest.TestCase):
    def session(self, *, mode="act", operations=None):
        kwargs = {} if operations is None else {"operations": operations}
        return FlatSession(CallerAuthority("client-one", "stable-owner", mode, **kwargs))

    def assert_refused(self, code, action, *args, **kwargs):
        with self.assertRaises(OperationRefused) as caught:
            action(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def read(self, session, handle):
        return session.admit("browser_read", {"tab_id": handle})

    def test_identity_is_immutable_and_invalid_scope_fails_at_construction(self):
        authority = CallerAuthority("client-one", "stable-owner", "read")
        with self.assertRaises(FrozenInstanceError):
            authority.mode = "act"
        for kwargs in ({"client_id": "bad identity"}, {"owner_id": ""}, {"mode": "unsafe"},
                       {"user_tabs": "yes"}, {"operations": {"browser_read"}},
                       {"operations": frozenset({"browser_evaluate"})}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CallerAuthority(**{"client_id": "client", "owner_id": "owner", "mode": "read", **kwargs})

    def test_unbound_session_and_explicit_binding_do_not_invent_a_current_tab(self):
        session = self.session()
        self.assert_refused("session_unbound", session.admit, "browser_tabs", {})
        session.bind(CONNECTION)
        admitted = session.admit("browser_tabs", {})
        self.assertEqual(admitted.connection, CONNECTION)
        self.assertIsNone(admitted.tab)
        self.assert_refused("invalid_arguments", session.admit, "browser_read", {})

    def test_foreign_and_raw_handles_never_resolve_even_for_same_owner(self):
        first, second = self.session(), self.session()
        a, = first.refresh_tabs(CONNECTION, [9])
        b, = second.refresh_tabs(CONNECTION, [9])
        self.assertNotEqual(a.handle, b.handle)
        self.assert_refused("invalid_tab_handle", self.read, second, a.handle)
        self.assert_refused("invalid_tab_handle", self.read, first, b.handle)
        self.assert_refused("invalid_tab_handle", self.read, first, "t_unknown")
        self.assert_refused("invalid_arguments", self.read, first, 9)
        self.assertEqual(self.read(first, a.handle).tab.native_id, 9)
        self.assertEqual(self.read(second, b.handle).tab.native_id, 9)

    def test_full_inventory_retains_live_handles_but_never_resurrects_closed_ones(self):
        session = self.session()
        a, b = session.refresh_tabs(CONNECTION, [9, 12])
        self.assertEqual(session.refresh_tabs(CONNECTION, [12, 9]), (b, a))
        session.refresh_tabs(CONNECTION, [12])
        self.assert_refused("stale_tab_handle", self.read, session, a.handle)
        c, retained = session.refresh_tabs(CONNECTION, [9, 12])
        self.assertNotEqual(c.handle, a.handle)
        self.assertEqual(retained, b)
        self.assert_refused("stale_tab_handle", self.read, session, a.handle)
        self.assertEqual(self.read(session, c.handle).tab.native_id, 9)

    def test_generation_or_process_change_invalidates_every_old_handle(self):
        for connection in (ConnectionBinding("a" * 32, 2), ConnectionBinding("b" * 32, 1)):
            with self.subTest(connection=connection):
                session = self.session()
                old, = session.refresh_tabs(CONNECTION, [9])
                pending = self.read(session, old.handle)
                fresh, = session.refresh_tabs(connection, [9])
                self.assertNotEqual(old.handle, fresh.handle)
                self.assert_refused("stale_tab_handle", self.read, session, old.handle)
                self.assert_refused("stale_connection", session.recheck, pending)
                self.assertEqual(self.read(session, fresh.handle).connection, connection)

    def test_rebinding_without_inventory_retires_old_handles_and_never_revives_them(self):
        session = self.session()
        old, = session.refresh_tabs(CONNECTION, [9])
        pending = self.read(session, old.handle)
        session.bind(ConnectionBinding("a" * 32, 2))
        self.assert_refused("stale_connection", session.recheck, pending)
        self.assert_refused("stale_tab_handle", self.read, session, old.handle)
        session.bind(CONNECTION)
        self.assert_refused("stale_tab_handle", self.read, session, old.handle)
        replacement, = session.refresh_tabs(CONNECTION, [9])
        self.assertNotEqual(replacement.handle, old.handle)

    def test_native_id_range_matches_the_bridge_not_falsy_or_imprecise_aliases(self):
        session = self.session()
        for native_id in (0, -1, True, 2**53, 1.0, "1"):
            with self.subTest(native_id=native_id), self.assertRaises(ValueError):
                session.refresh_tabs(CONNECTION, [native_id])
        largest, = session.refresh_tabs(CONNECTION, [2**53 - 1])
        self.assertEqual(self.read(session, largest.handle).tab.native_id, 2**53 - 1)

    def click(self, session, handle, ref):
        return session.admit("browser_click", {"tab_id": handle, "target": {"ref": ref}})

    def test_refs_are_usable_only_by_the_session_and_tab_they_were_returned_to(self):
        first, second = self.session(), self.session()
        a, b = first.refresh_tabs(CONNECTION, [9, 12])
        other, _ = second.refresh_tabs(CONNECTION, [9, 12])
        self.assert_refused("stale_ref", self.click, first, a.handle, "e1")
        first.issue_refs(a.handle, ["e1", "e2"])
        self.assertEqual(self.click(first, a.handle, "e1").refs, ("e1",))
        self.assert_refused("stale_ref", self.click, first, b.handle, "e1")
        self.assert_refused("stale_ref", self.click, second, other.handle, "e1")
        self.assert_refused("invalid_tab_handle", second.issue_refs, a.handle, ["e1"])
        # Every ref-bearing argument is checked, and a selector target needs no ref.
        for name, arguments in (
                ("browser_drag", {"source": {"ref": "e1"}, "destination": {"ref": "e3"}}),
                ("browser_drag", {"source": {"ref": "e3"}, "destination": {"ref": "e1"}}),
                ("browser_wait", {"condition": {"kind": "target", "target": {"ref": "e3"}}}),
                ("browser_read", {"target": {"ref": "e3"}}),
                ("browser_screenshot", {"scope": "element", "target": {"ref": "e3"}})):
            with self.subTest(name=name, arguments=arguments):
                self.assert_refused("stale_ref", first.admit, name, {"tab_id": a.handle, **arguments})
        admitted = first.admit("browser_drag", {"tab_id": a.handle, "source": {"ref": "e1"},
                                                "destination": {"ref": "e2"}})
        self.assertEqual(admitted.refs, ("e1", "e2"))
        self.assertEqual(first.admit("browser_click", {"tab_id": a.handle, "target": {"selector": "#x"}}).refs, ())
        for invalid in (["s1e2"], ["e"], [7], ["e1 "], ["e" + "1" * 128]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                first.issue_refs(a.handle, invalid)

    def test_refs_end_with_their_tab_connection_or_session(self):
        def issued(retire):
            session = self.session()
            a, b = session.refresh_tabs(CONNECTION, [9, 12])
            session.issue_refs(a.handle, ["e1"])
            session.issue_refs(b.handle, ["e5"])
            pending = self.click(session, a.handle, "e1")
            retire(session, a)
            return session, a, b, pending

        session, a, b, pending = issued(lambda session, a: session.forget_refs(a.handle))
        self.assert_refused("stale_ref", session.recheck, pending)
        self.assert_refused("stale_ref", self.click, session, a.handle, "e1")
        self.assertEqual(self.click(session, b.handle, "e5").refs, ("e5",))
        session.issue_refs(b.handle, ["e6"])
        session.forget_refs(b.handle, ["e5"])
        self.assert_refused("stale_ref", self.click, session, b.handle, "e5")
        self.click(session, b.handle, "e6")

        session, a, b, _ = issued(lambda session, a: session.refresh_tabs(CONNECTION, [12]))
        reopened, kept = session.refresh_tabs(CONNECTION, [9, 12])
        self.assertEqual(kept, b)
        self.assert_refused("stale_ref", self.click, session, reopened.handle, "e1")
        self.click(session, b.handle, "e5")

        session, a, b, _ = issued(lambda session, a: session.closed_tab(a.handle))
        self.assert_refused("stale_ref", self.click, session, session.opened_tab(CONNECTION, 9).handle, "e1")

        session, a, b, _ = issued(lambda session, a: session.bind(ConnectionBinding("a" * 32, 2)))
        fresh, _ = session.refresh_tabs(ConnectionBinding("a" * 32, 2), [9, 12])
        self.assert_refused("stale_ref", self.click, session, fresh.handle, "e1")

        session, a, b, _ = issued(lambda session, a: session.revoke())
        self.assert_refused("authority_revoked", session.issue_refs, b.handle, ["e7"])

    def test_ref_memory_is_bounded_by_forgetting_the_oldest(self):
        session = self.session()
        tab, = session.refresh_tabs(CONNECTION, [9])
        with patch("kitsune.flat_session.MAX_TAB_REFS", 2):
            session.issue_refs(tab.handle, ["e1", "e2"])
            session.issue_refs(tab.handle, ["e1", "e3"])  # e1 was returned again: e2 is the oldest
        self.assert_refused("stale_ref", self.click, session, tab.handle, "e2")
        self.click(session, tab.handle, "e1")
        self.click(session, tab.handle, "e3")

    def test_observed_open_and_close_do_not_replace_the_rest_of_the_inventory(self):
        session = self.session()
        a, = session.refresh_tabs(CONNECTION, [9])
        b = session.opened_tab(CONNECTION, 12)
        self.assertEqual(self.read(session, a.handle).tab, a)
        self.assertEqual(session.opened_tab(CONNECTION, 12), b)
        pending = self.read(session, b.handle)
        session.closed_tab(b.handle)
        self.assert_refused("stale_tab_handle", session.recheck, pending)
        self.assertEqual(self.read(session, a.handle).tab, a)
        self.assertNotEqual(session.opened_tab(CONNECTION, 12).handle, b.handle)

    def test_open_without_a_verified_binding_is_rejected(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.opened_tab(None, 9)
        self.assert_refused("session_unbound", session.admit, "browser_tabs", {})

    def test_mode_and_explicit_scope_intersect_without_changing_owner(self):
        session = self.session(mode="read", operations=frozenset({"browser_read", "browser_click"}))
        tab, = session.refresh_tabs(CONNECTION, [9])
        self.assert_refused("mode_restricted", session.admit, "browser_click",
                            {"tab_id": tab.handle, "target": {"selector": "#button"}})
        self.assert_refused("operation_not_allowed", session.admit, "browser_tabs", {})
        admitted = self.read(session, tab.handle)
        self.assertEqual(admitted.authority, session.authority)
        self.assertEqual(admitted.authority.owner_id, "stable-owner")
        self.assertEqual(admitted.authority.user_tabs, "read-only")
        for key, value in (("mode", "act"), ("headers", {"X-Kitsune-User-Tabs": "writable"}),
                           ("ownerId", "other"), ("session", "new")):
            self.assert_refused("invalid_arguments", session.admit, "browser_read", {"tab_id": tab.handle, key: value})

    def test_pending_admissions_remain_caller_bound_and_revocation_is_permanent(self):
        first, second = self.session(), self.session()
        a, = first.refresh_tabs(CONNECTION, [9])
        second.refresh_tabs(CONNECTION, [9])
        pending = self.read(first, a.handle)
        first.recheck(pending)
        self.assert_refused("invalid_admission", second.recheck, pending)
        first.revoke()
        for action, args in ((first.recheck, (pending,)), (self.read, (first, a.handle)),
                             (first.refresh_tabs, (CONNECTION, [9])), (first.opened_tab, (CONNECTION, 12)),
                             (first.bind, (CONNECTION,)), (first.closed_tab, (a.handle,))):
            self.assert_refused("authority_revoked", action, *args)
        first.revoke()  # idempotent, not a toggle
        self.assert_refused("authority_revoked", first.admit, "browser_tabs", {})
        self.assertIsNotNone(second.admit("browser_tabs", {}))

    def test_invalid_inventory_and_exhaustion_do_not_partially_replace_handles(self):
        session = self.session()
        a, = session.refresh_tabs(CONNECTION, [9])
        for ids in ([9, 9], [True], [-1], ["12"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                session.refresh_tabs(CONNECTION, ids)
            self.assertEqual(self.read(session, a.handle).tab, a)
        with patch("kitsune.flat_session.MAX_SESSION_TAB_HANDLES", 2):
            b = session.opened_tab(CONNECTION, 12)
            self.assert_refused("session_handle_limit", session.refresh_tabs, CONNECTION, [13, 14])
            self.assertEqual(self.read(session, a.handle).tab, a)
            self.assertEqual(self.read(session, b.handle).tab, b)
            session.closed_tab(b.handle)
            self.assert_refused("session_handle_limit", session.opened_tab, CONNECTION, 12)
            self.assert_refused("session_handle_limit", session.admit, "browser_tabs", {"action": "open"})
            self.assert_refused("stale_tab_handle", self.read, session, b.handle)


if __name__ == "__main__":
    unittest.main()
