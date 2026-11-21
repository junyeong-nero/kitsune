"""One trusted local Python invocation; browser requests go back to the MCP host.

This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import json as json_module
import sys
import traceback

from .browser import Browser, Tab
from .errors import failed

_INPUT = sys.stdin
_OUTPUT = sys.stdout
# Routes of the actions whose post-action snapshot (the extension's POST_SNAPSHOT_ACTIONS) is taken
# once the page settled: a delta of what the action changed, or the page it loaded. Reads and
# waits are not listed, though /api/snapshot replies have a snapshot key too. Nor is
# /api/submit-form: its snapshot is taken straight after dispatch, before the submit's navigation,
# so it shows the page that held the form and would read as "not submitted".
SNAPSHOT_ACTION_ROUTES = frozenset({
    "/api/click", "/api/type", "/api/fill", "/api/select-option", "/api/check", "/api/keypress",
    "/api/navigate", "/api/hover"})
# The snapshot and the fields that say what it is: a delta or a full page, cut or whole.
_SNAPSHOT_FIELDS = ("snapshot", "snapshot_delta", "no_visible_change", "snapshot_truncated", "snapshot_hint",
                    "navigated")


def _send(message):
    _OUTPUT.write(json_module.dumps(message, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


class _Client:
    last = None  # (route, reply) of the script's last browser operation

    def request(self, method, endpoint, *, json=None, timeout=15):
        _send({"event": "call", "method": method, "route": endpoint, "body": json, "timeout": timeout})
        line = _INPUT.readline()
        if not line:
            raise RuntimeError("The script host disconnected")
        self.last = (endpoint, json_module.loads(line))
        return self.last[1]


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


def main():
    request = json_module.loads(_INPUT.readline())
    browser = Browser(_Client())
    namespace = {"__name__": "__main__", "browser": browser,
                 "tab": browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None}
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            chosen = "result" in namespace or _Output.written  # result = None is an assignment too
            _send({"event": "done", "result": namespace.get("result") if chosen
                   else _last_action_snapshot(browser.client.last)})
        except BaseException as error:
            message = "".join(traceback.format_exception_only(type(error), error)).strip()
            if isinstance(error, TypeError):
                # #524: same-named direct tools use different arguments and semantics. Add local
                # help, not aliases, browser calls or retries; keep the original exception visible.
                import inspect  # here, not at the top: every call starts a worker, few fail this way
                for method in (Tab.find, Tab.snapshot):
                    if str(error).startswith(f"{method.__qualname__}() got an unexpected keyword argument "):
                        message += f"\nPython API: {method.__qualname__}{inspect.signature(method)}\n{inspect.getdoc(method)}"
                        break
            _send({"event": "done", "error": message})


if __name__ == "__main__":
    main()
