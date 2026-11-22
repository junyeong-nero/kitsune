"""One trusted local Python invocation; browser requests go back to the MCP host.

This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import json as json_module
import re
import sys
import time
import traceback

from .browser import Browser, Tab
from .errors import KitsuneError, TimedOut

_INPUT = sys.stdin
_OUTPUT = sys.stdout
# How CPython starts the TypeError of a call whose arguments did not bind to the method's parameters.
_UNBOUND_CALL = re.compile(r"(Browser|Tab)\.([A-Za-z]\w*)\(\) ")


def _send(message):
    _OUTPUT.write(json_module.dumps(message, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


class _Client:
    reply = None  # the last call's reply

    def request(self, method, endpoint, *, json=None, timeout=15):
        _send({"event": "call", "method": method, "route": endpoint, "body": json, "timeout": timeout})
        line = _INPUT.readline()
        if not line:
            raise RuntimeError("The script host disconnected")
        self.reply = json_module.loads(line)
        return self.reply


class _Output:
    def write(self, text):
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
    client = _Client()
    browser = Browser(client)
    # #574: scripts used time, re and json without importing them, after earlier operations had run.
    namespace = {"__name__": "__main__", "browser": browser, "time": time, "re": re, "json": json_module,
                 "tab": browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None,
                 # #568: the exceptions a failed read or wait raises, for except clauses.
                 "KitsuneError": KitsuneError, "TimedOut": TimedOut}
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            _send({"event": "done", "result": namespace.get("result")})
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
            _send({"event": "done", "error": message, **({"uncaught_reply": True} if uncaught_reply else {})})


if __name__ == "__main__":
    main()
