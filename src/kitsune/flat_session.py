"""Caller-owned admission state; not a standalone broker or worker sandbox.

Only the trusted coordinator may create/revoke sessions, install complete tab
inventories or register successful opens/closes. Agent requests go through admit.
Native adapters must recheck before dispatch and still enforce live browser policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from threading import RLock

from .connection_binding import ConnectionBinding
from .flat_operations import MODE_OPERATIONS, OPERATIONS, OperationRefused, ValidatedOperation, validate_operation
from .task_modes import CLIENT_ID_PATTERN


# Includes tombstones so closed handles never silently become valid again. Exhaustion
# fails closed; the coordinator must end the session, not evict/reuse old handles.
MAX_SESSION_TAB_HANDLES = 4096
# Refs remembered per live tab. Beyond it the oldest are forgotten, which only refuses
# them (stale_ref): the caller reads the tab again.
MAX_TAB_REFS = 4096
_REF = re.compile(r"e[0-9]{1,127}")


@dataclass(frozen=True)
class CallerAuthority:
    client_id: str
    owner_id: str
    mode: str
    user_tabs: str = "read-only"
    operations: frozenset[str] = OPERATIONS

    def __post_init__(self):
        if any(not isinstance(value, str) or not re.fullmatch(CLIENT_ID_PATTERN, value)
               for value in (self.client_id, self.owner_id)):
            raise ValueError("Invalid flat caller identity")
        if self.mode not in MODE_OPERATIONS or self.user_tabs not in {"read-only", "writable"}:
            raise ValueError("Invalid flat caller authority")
        if type(self.operations) is not frozenset or not self.operations <= OPERATIONS:
            raise ValueError("Flat operation scope must be an explicit subset")


@dataclass(frozen=True)
class BoundTab:
    handle: str
    connection: ConnectionBinding
    native_id: int


@dataclass(frozen=True)
class AdmittedOperation:
    operation: ValidatedOperation
    authority: CallerAuthority
    connection: ConnectionBinding
    tab: BoundTab | None
    _issuer: object
    refs: tuple[str, ...] = ()


class FlatSession:
    def __init__(self, authority: CallerAuthority):
        self._authority = authority
        self._issuer = object()
        self._lock = RLock()
        self._revoked = False
        self._connection = None
        self._issued: dict[str, BoundTab] = {}
        self._live: dict[int, BoundTab] = {}
        # tab handle -> refs this session was given for that tab, oldest first.
        self._refs: dict[str, dict[str, None]] = {}

    @property
    def authority(self):
        return self._authority

    def _require_live(self):
        if self._revoked:
            raise OperationRefused("authority_revoked", "This caller session has ended")

    def revoke(self):
        """Trusted lifecycle action; never a worker method. Cannot reactivate this session."""
        with self._lock:
            self._revoked = True
            self._live.clear()
            self._refs.clear()

    def bind(self, connection: ConnectionBinding):
        """Install verified transport identity without pretending to have read a tab list."""
        if not isinstance(connection, ConnectionBinding):
            raise ValueError("A verified connection binding is required")
        with self._lock:
            self._require_live()
            if connection != self._connection:
                self._connection = connection
                self._live.clear()
                self._refs.clear()

    @staticmethod
    def _native_id(value):
        if type(value) is not int or not 0 < value <= 2**53 - 1:
            raise ValueError("Invalid native tab inventory")
        return value

    def _new_tab(self, native_id):
        if len(self._issued) >= MAX_SESSION_TAB_HANDLES:
            raise OperationRefused("session_handle_limit", "This session exhausted its tab handle budget")
        handle = "t_" + secrets.token_urlsafe(24)
        while handle in self._issued:
            handle = "t_" + secrets.token_urlsafe(24)
        record = BoundTab(handle, self._connection, native_id)
        self._issued[handle] = self._live[native_id] = record
        return record

    def refresh_tabs(self, connection: ConnectionBinding, native_ids: list[int]) -> tuple[BoundTab, ...]:
        """Install a trusted *complete* inventory. Never call on an error or truncated list.

        A connection change invalidates every prior handle. A tab absent from a
        complete list is retired; even a later identical native id gets a fresh handle.
        This method establishes identity only, not ownership or write permission.
        """
        if not isinstance(connection, ConnectionBinding):
            raise ValueError("A verified connection binding is required")
        ids = [self._native_id(value) for value in native_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate native tab in inventory")
        with self._lock:
            self._require_live()
            existing = self._live if connection == self._connection else {}
            new_count = sum(native_id not in existing for native_id in ids)
            if len(self._issued) + new_count > MAX_SESSION_TAB_HANDLES:
                raise OperationRefused("session_handle_limit", "This session exhausted its tab handle budget")
            # Validation/budget checks precede all mutation: failure must not retire live handles.
            self._connection = connection
            self._live = {native_id: existing[native_id] for native_id in ids if native_id in existing}
            kept = {record.handle for record in self._live.values()}
            self._refs = {handle: refs for handle, refs in self._refs.items() if handle in kept}
            return tuple(self._live.get(native_id) or self._new_tab(native_id) for native_id in ids)

    def opened_tab(self, connection: ConnectionBinding, native_id: int) -> BoundTab:
        """Register an observed successful open without treating it as a full inventory."""
        if not isinstance(connection, ConnectionBinding):
            raise ValueError("A verified connection binding is required")
        self._native_id(native_id)
        with self._lock:
            self._require_live()
            if connection != self._connection:
                raise OperationRefused("stale_connection", "The tab belongs to a different connection")
            return self._live.get(native_id) or self._new_tab(native_id)

    def closed_tab(self, handle: str):
        """Retire only after trusted observation of closure, never on an uncertain write."""
        with self._lock:
            self._require_live()
            record = self._resolve(handle)
            del self._live[record.native_id]
            self._refs.pop(handle, None)

    def issue_refs(self, handle: str, refs):
        """Remember refs a trusted read returned to this caller for this tab.

        Only the coordinator calls this, with refs parsed from protocol fields of a
        result it is about to deliver. A ref names a node of one document; the
        extension still rejects it after that document is replaced.
        """
        refs = list(refs)
        if any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in refs):
            raise ValueError("Invalid ref")
        with self._lock:
            self._require_live()
            known = self._refs.setdefault(self._resolve(handle).handle, {})
            for ref in refs:
                known.pop(ref, None)  # most recently returned last
                known[ref] = None
            for ref in list(known)[:max(0, len(known) - MAX_TAB_REFS)]:
                del known[ref]

    def forget_refs(self, handle: str, refs=None):
        """Drop a tab's refs (all of them after a navigation the coordinator dispatched)."""
        with self._lock:
            if refs is None:
                self._refs.pop(handle, None)
            else:
                for ref in refs:
                    self._refs.get(handle, {}).pop(ref, None)

    def _require_refs(self, tab: BoundTab | None, refs):
        known = self._refs.get(tab.handle, {}) if tab is not None else {}
        if any(ref not in known for ref in refs):
            raise OperationRefused("stale_ref", "This ref was not returned to this caller for this tab; read it again")

    def _resolve(self, handle: str) -> BoundTab:
        record = self._issued.get(handle) if isinstance(handle, str) else None
        if record is None:
            raise OperationRefused("invalid_tab_handle", "Not a tab handle issued to this caller session")
        if record.connection != self._connection or self._live.get(record.native_id) is not record:
            raise OperationRefused("stale_tab_handle", "This tab handle is no longer live; list tabs again")
        return record

    def admit(self, name: str, arguments: dict) -> AdmittedOperation:
        """The only agent-input path here: no raw route, policy, identity, or header parameter."""
        with self._lock:
            self._require_live()
            operation = validate_operation(name, arguments, mode=self._authority.mode)
            if name not in self._authority.operations:
                raise OperationRefused("operation_not_allowed", "The caller scope does not permit this operation")
            if self._connection is None:
                raise OperationRefused("session_unbound", "The coordinator must establish a connection first")
            values = operation.arguments()
            tab = self._resolve(values["tab_id"]) if "tab_id" in values else None
            if name == "browser_tabs" and values["action"] == "open" and len(self._issued) >= MAX_SESSION_TAB_HANDLES:
                raise OperationRefused("session_handle_limit", "This session exhausted its tab handle budget")
            # Every ref-bearing argument of the contract: a new one must be listed here.
            targets = [values.get(key) for key in ("target", "source", "destination")]
            targets.append((values.get("condition") or {}).get("target"))
            refs = tuple(target["ref"] for target in targets if isinstance(target, dict) and "ref" in target)
            self._require_refs(tab, refs)
            return AdmittedOperation(operation, self._authority, self._connection, tab, self._issuer, refs)

    def recheck(self, admitted: AdmittedOperation):
        """Revalidate an admitted operation after queueing; never silently retarget it.

        This is not remote cancellation of a command already dispatched. The actual
        broker/bridge dispatch path must connect this check to its lifecycle protocol.
        """
        with self._lock:
            self._require_live()
            if admitted._issuer is not self._issuer:
                raise OperationRefused("invalid_admission", "Operation belongs to another caller session")
            if admitted.connection != self._connection:
                raise OperationRefused("stale_connection", "The admitted connection has changed")
            if admitted.tab is not None:
                self._resolve(admitted.tab.handle)
            self._require_refs(admitted.tab, admitted.refs)
