"""One trusted local Python invocation; browser requests go back to the MCP host.

This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import inspect
import json as json_module
import sys
import traceback

from .browser import Browser, Snapshot, Tab

_INPUT = sys.stdin
_OUTPUT = sys.stdout


def _send(message):
    _OUTPUT.write(json_module.dumps(message, ensure_ascii=False) + "\n")
    _OUTPUT.flush()


class _Client:
    def request(self, method, endpoint, *, json=None, timeout=15):
        _send({"event": "call", "method": method, "route": endpoint, "body": json, "timeout": timeout})
        line = _INPUT.readline()
        if not line:
            raise RuntimeError("The script host disconnected")
        return json_module.loads(line)


class _Output:
    def write(self, text):
        for start in range(0, len(text), 4096):
            _send({"event": "output", "text": text[start:start + 4096]})
        return len(text)

    def flush(self):
        pass


def _shown(value):
    """A result shows a snapshot as print() does: only its changes when the host had the previous one."""
    if isinstance(value, Snapshot):
        return str(value)
    if isinstance(value, dict):
        return {key: _shown(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shown(item) for item in value]
    return value


def main():
    request = json_module.loads(_INPUT.readline())
    browser = Browser(_Client())
    namespace = {"__name__": "__main__", "browser": browser,
                 "tab": browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None}
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            _send({"event": "done", "result": _shown(namespace.get("result"))})
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
