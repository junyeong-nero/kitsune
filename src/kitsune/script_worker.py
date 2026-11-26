"""One trusted local Python invocation; browser requests go back to the MCP host.

This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import json as json_module
import re
import sys
import threading
import time
import traceback

from .browser import Browser, Tab
from .errors import KitsuneError, TimedOut, failed

_INPUT = sys.stdin
_OUTPUT = sys.stdout
# How CPython starts the TypeError of a call whose arguments did not bind to the method's parameters.
_UNBOUND_CALL = re.compile(r"(Browser|Tab)\.([A-Za-z]\w*)\(\) ")
# Routes of the actions whose post-action snapshot (the extension's POST_SNAPSHOT_ACTIONS) is taken
# once the page settled: a delta of what the action changed, or the page it loaded. Reads and
# waits are not listed, though /api/snapshot replies have a snapshot key too. /api/submit-form
# waits for the page its submit loads (#586); its snapshot is returned only with navigated.
SNAPSHOT_ACTION_ROUTES = frozenset({
    "/api/click", "/api/type", "/api/fill", "/api/select-option", "/api/check", "/api/keypress",
    "/api/navigate", "/api/hover", "/api/submit-form"})
# The snapshot and the fields that say what it is: a delta or a full page, cut or whole.
_SNAPSHOT_FIELDS = ("snapshot", "snapshot_delta", "no_visible_change", "snapshot_truncated", "snapshot_hint",
                    "navigated")


# Threads of one script share the frame stream to the host (#634): a frame is written whole, and a
# call holds the stream until its reply arrives, so each thread reads its own reply. The host runs
# one call at a time; browser.parallel runs tabs concurrently.
_SEND_LOCK = threading.Lock()
_CALL_LOCK = threading.Lock()


def _send(message):
    with _SEND_LOCK:
        _OUTPUT.write(json_module.dumps(message, ensure_ascii=False) + "\n")
        _OUTPUT.flush()


class ScriptLimit(BaseException):
    """The script reached its call limit or deadline (#618); the call that raised it was not sent.

    Not an Exception, so `except Exception` and `except KitsuneError` let it through while
    `finally` and `except ScriptLimit` still run. code is script_operation_limit or script_timeout.
    Any further browser call stops the script.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Client:
    reply = None  # the last call's reply
    last = None  # (route, reply) of the script's last browser operation

    def __init__(self, calls_left, seconds_left):
        self.calls_left = calls_left
        self.deadline = time.monotonic() + seconds_left

    def request(self, method, endpoint, *, json=None, timeout=15):
        with _CALL_LOCK:
            _send({"event": "call", "method": method, "route": endpoint, "body": json, "timeout": timeout})
            line = _INPUT.readline()
            if not line:
                raise RuntimeError("The script host disconnected")
            frame = json_module.loads(line)
            if "limit" in frame:
                self.calls_left = 0
                raise ScriptLimit(frame["limit"], frame["error"])
            # The host's clock: its deadline moves while the user is asked to approve a tool.
            self.calls_left -= 1
            self.deadline = time.monotonic() + frame["seconds_left"]
            reply = self.reply = frame["reply"]
            self.last = (endpoint, reply)
            return reply


class _ScriptBrowser(Browser):
    @property
    def calls_left(self) -> int:
        """Browser calls this script may still make before ScriptLimit."""
        return max(0, self.client.calls_left)

    @property
    def seconds_left(self) -> float:
        """Seconds until the script's deadline; a call after it raises ScriptLimit."""
        return max(0.0, round(self.client.deadline - time.monotonic(), 1))


def _last_action_snapshot(last):
    """The result of a script that printed nothing and assigned no result (#575): the snapshot its
    last browser operation returned, when that was a successful action (SNAPSHOT_ACTION_ROUTES),
    under last_action, its route. Agents ran such scripts and then read the whole page to see what
    changed. Nothing is read for it: these are the reply's own fields, so the snapshot keeps its
    cap and redaction. None after a read, a wait or an action without a snapshot, and when the
    reply does not show that the page settled first.
    """
    route, reply = last or (None, None)
    if (route not in SNAPSHOT_ACTION_ROUTES or not isinstance(reply, dict) or failed(reply)
            or not isinstance(reply.get("snapshot"), str)):
        return None
    # navigate waits for its load. The others settle only on the delta path, which marks its
    # snapshot a delta or the page a navigation loaded; a full snapshot on request
    # (KITSUNE_POST_ACTION_SNAPSHOT=full) or of a page still loading may show the page as it was.
    if route != "/api/navigate" and reply.get("snapshot_delta") is not True and (
            "navigated" not in reply or reply.get("navigation_pending")):
        return None
    return {"last_action": route, **{field: reply[field] for field in _SNAPSHOT_FIELDS if field in reply}}


class _Output:
    written = False  # whether the script printed anything, to stdout or stderr

    def write(self, text):
        if text:
            _Output.written = True
        for start in range(0, len(text), 4096):
            _send({"event": "output", "text": text[start:start + 4096]})
        return len(text)

    def flush(self):
        pass


def _unbound_call(error):
    """The public Browser or Tab method whose call raised this TypeError before the method ran.

    Wrong, unknown or missing arguments raise at the call, so no traceback frame runs the method.
    A TypeError raised inside a method is about a value, and the signature would not explain it.
    """
    named = _UNBOUND_CALL.match(str(error))
    method = getattr(Tab if named[1] == "Tab" else Browser, named[2], None) if named else None
    code = getattr(method, "__code__", None)
    if code is None:
        return None
    entry = error.__traceback__
    while entry is not None:
        if entry.tb_frame.f_code is code:
            return None
        entry = entry.tb_next
    return method


def main():
    request = json_module.loads(_INPUT.readline())
    client = _Client(request["calls_left"], request["seconds_left"])
    browser = _ScriptBrowser(client)
    # #574: scripts used time, re and json without importing them, after earlier operations had run.
    namespace = {"__name__": "__main__", "browser": browser, "time": time, "re": re, "json": json_module,
                 "tab": browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None,
                 # #568: the exceptions a failed read or wait raises, for except clauses.
                 "KitsuneError": KitsuneError, "TimedOut": TimedOut, "ScriptLimit": ScriptLimit}
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            chosen = "result" in namespace or _Output.written  # result = None is an assignment too
            _send({"event": "done", "result": namespace.get("result") if chosen
                   else _last_action_snapshot(client.last)})
        except ScriptLimit as error:
            # #618: what the script had in result, so a stopped loop does not lose it.
            kept = {}
            if "result" in namespace:
                try:
                    kept = {"result": json_module.loads(json_module.dumps(namespace["result"], ensure_ascii=False))}
                except (TypeError, ValueError):
                    pass
            _send({"event": "done", "error": str(error), "limit": error.code, **kept})
        except BaseException as error:
            message = "".join(traceback.format_exception_only(type(error), error)).strip()
            method = _unbound_call(error) if isinstance(error, TypeError) else None
            if method is not None:
                # #524, #574: same-named direct tools and Playwright use different arguments and
                # semantics. Add local help, not aliases, browser calls or retries; keep the
                # original exception visible.
                import inspect  # here, not at the top: every call starts a worker, few fail this way
                message += f"\nPython API: {method.__qualname__}{inspect.signature(method)}"
                if inspect.getdoc(method):
                    message += f"\n{inspect.getdoc(method)}"
            # #568: an uncaught failure of the last call ends the script as that failed operation.
            uncaught_reply = isinstance(error, KitsuneError) and error.result is client.reply
            _send({"event": "done", "error": message, **({"uncaught_reply": True} if uncaught_reply else {}),
                   # #668: not UnboundLocalError, a function's own local read before assignment.
                   **({"name_error": True} if type(error) is NameError else {})})


if __name__ == "__main__":
    main()
