"""Bounded bridge-owned native dispatch evidence, never supplied by guest/page output."""
from threading import Lock


RECEIPT_HEADER = "X-Kitsune-Dispatch-Receipt"
RECEIPT_VERSION = 1
MAX_NATIVE_COMMANDS = 128
# Only awaited, non-container routes. Extend deliberately with each reviewed adapter;
# detached keep-alives/containers must not report a completed receipt for future work.
RECEIPT_ROUTES = frozenset({"/api/tabs", "/api/new-tab", "/api/close-tab", "/api/navigate",
                            "/api/back", "/api/forward", "/api/reload-tab", "/api/snapshot",
                            "/api/click", "/api/double-click", "/api/fill", "/api/type", "/api/select-option",
                            "/api/check", "/api/keypress", "/api/hover", "/api/drag", "/api/scroll"})


class DispatchReceipt:
    def __init__(self, call: str):
        self.call = call
        self._lock = Lock()
        self._commands = []
        self._requested = 0
        self._tasks = 0
        self._limited = False

    def task_started(self):
        with self._lock:
            self._tasks += 1

    def task_finished(self):
        with self._lock:
            self._tasks -= 1

    def begin(self, action, params):
        with self._lock:
            self._requested += 1
            if len(self._commands) >= MAX_NATIVE_COMMANDS:
                self._limited = True
                return None
            record = {"sequence": self._requested, "action": action,
                      "state": "not_dispatched", "finished": False}
            tab_id = (params or {}).get("tabId")
            if type(tab_id) is int and 0 < tab_id <= 2**53 - 1:
                record["tab_id"] = tab_id
            self._commands.append(record)
            return record

    def mark(self, record, state):
        with self._lock:
            record["state"] = state

    def finish(self, record):
        with self._lock:
            record["finished"] = True

    def snapshot(self):
        with self._lock:
            return {"version": RECEIPT_VERSION, "call": self.call, "requested": self._requested,
                    "limited": self._limited,
                    "complete": self._tasks == 0 and all(item["finished"] for item in self._commands),
                    "commands": [dict(item) for item in self._commands]}
