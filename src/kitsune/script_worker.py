"""A persistent trusted local Python session; browser requests go back to the MCP host.

One request per line; variables persist between requests until the host stops the process.
This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import inspect
import json as json_module
import sys
import traceback

from .browser import Browser, Tab

_INPUT = sys.stdin
_OUTPUT = sys.stdout


class _Stopped(BaseException):
    """The host refused a browser request after a failed operation; `except Exception` cannot keep going."""


def _send(message):
    _OUTPUT.write(json_module.dumps(message, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


class _Client:
    def request(self, method, endpoint, *, json=None, timeout=15):
        _send({"event": "call", "method": method, "route": endpoint, "body": json, "timeout": timeout})
        line = _INPUT.readline()
        if not line:
            raise RuntimeError("The script host disconnected")
        reply = json_module.loads(line)
        if reply == {"stop": True}:
            raise _Stopped("A browser operation failed; the script was stopped")
        return reply


class _Output:
    def write(self, text):
        for start in range(0, len(text), 4096):
            _send({"event": "output", "text": text[start:start + 4096]})
        return len(text)

    def flush(self):
        pass


def main():
    browser = Browser(_Client())
    namespace = {"__name__": "__main__", "browser": browser}
    while line := _INPUT.readline():
        _run(json_module.loads(line), browser, namespace)


def _run(request, browser, namespace):
    # Variables persist; browser, the MCP-selected tab and result are set afresh for each call.
    namespace.pop("result", None)
    namespace.update(browser=browser,
                     tab=browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None)
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            _send({"event": "done", "result": namespace.get("result")})
        except BaseException as error:
            message = "".join(traceback.format_exception_only(type(error), error)).strip()
            if isinstance(error, TypeError):
                # #524: same-named direct tools use different arguments and semantics. Add local
                # help, not aliases, browser calls or retries; keep the original exception visible.
                for method in (Tab.find, Tab.snapshot):
                    if str(error).startswith(f"{method.__qualname__}() got an unexpected keyword argument "):
                        message += f"\nPython API: {method.__qualname__}{inspect.signature(method)}\n{inspect.getdoc(method)}"
                        break
            _send({"event": "done", "error": message})


if __name__ == "__main__":
    main()
