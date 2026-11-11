"""One trusted local Python invocation; browser requests go back to the MCP host.

This is not a sandbox. Code has ordinary local Python host access.
"""
import contextlib
import json as json_module
import sys
import traceback

from .browser import Browser

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


def main():
    request = json_module.loads(_INPUT.readline())
    browser = Browser(_Client())
    namespace = {"__name__": "__main__", "browser": browser,
                 "tab": browser.tab(request["tab_id"]) if request.get("tab_id") is not None else None}
    with contextlib.redirect_stdout(_Output()), contextlib.redirect_stderr(_Output()):
        try:
            exec(compile(request["code"], "<kitsune-script>", "exec"), namespace)
            _send({"event": "done", "result": namespace.get("result")})
        except BaseException as error:
            _send({"event": "done", "error": "".join(traceback.format_exception_only(type(error), error)).strip()})


if __name__ == "__main__":
    main()
