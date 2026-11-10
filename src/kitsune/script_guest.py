"""The SDK a delegated script sees. Runs inside the WASI guest; nothing here is trusted.

The engine passes this file's text to the guest interpreter with -c. It holds no
credential and enforces nothing: every limit and every permission is the coordinator's
(script_runner) and the broker's. A script that bypasses this file and writes its own
frames gains nothing, because a frame is only a request.

Frames are one ASCII JSON object per line: requests on stdout, replies on stdin.
"""
# Method -> (operation, fixed arguments, keyword names). Checked against the #499
# declarations by tests; the broker applies defaults and validates values.
SDK = {
    "tabs": ("browser_tabs", {"action": "list"}, ()),
    "new_tab": ("browser_tabs", {"action": "open"}, ("url", "timeout_ms")),
    "close_tab": ("browser_tabs", {"action": "close"}, ("tab_id", "timeout_ms")),
    "navigate": ("browser_navigate", {"action": "goto"}, ("tab_id", "url", "timeout_ms")),
    "back": ("browser_navigate", {"action": "back"}, ("tab_id", "timeout_ms")),
    "forward": ("browser_navigate", {"action": "forward"}, ("tab_id", "timeout_ms")),
    "reload": ("browser_navigate", {"action": "reload"}, ("tab_id", "timeout_ms")),
    "read": ("browser_read", {}, ("tab_id", "format", "target", "max_chars", "timeout_ms")),
    "query": ("browser_query", {}, ("tab_id", "selector", "fields", "limit", "timeout_ms")),
    "find": ("browser_find", {}, ("tab_id", "text", "limit", "timeout_ms")),
    "click": ("browser_click", {}, ("tab_id", "target", "button", "click_count", "modifiers", "timeout_ms")),
    "input": ("browser_input", {}, ("tab_id", "target", "text", "mode", "timeout_ms")),
    "select": ("browser_select", {}, ("tab_id", "target", "values", "by", "timeout_ms")),
    "check": ("browser_check", {}, ("tab_id", "target", "checked", "timeout_ms")),
    "press": ("browser_press", {}, ("tab_id", "target", "key", "modifiers", "timeout_ms")),
    "hover": ("browser_hover", {}, ("tab_id", "target", "timeout_ms")),
    "drag": ("browser_drag", {}, ("tab_id", "source", "destination", "timeout_ms")),
    "scroll": ("browser_scroll", {}, ("tab_id", "direction", "amount_px", "target", "timeout_ms")),
    "wait": ("browser_wait", {}, ("tab_id", "condition", "timeout_ms")),
    "screenshot": ("browser_screenshot", {}, ("tab_id", "scope", "target", "timeout_ms")),
    "request_tab": ("browser_request_tab", {}, ("tab_id", "timeout_ms")),
    "ask_user": ("browser_ask_user", {}, ("reason", "message", "tab_id", "timeout_ms")),
    "resume": ("browser_resume", {}, ("call", "resume_token", "wait_ms")),
}


def _main():
    import ast
    import inspect
    import json
    import sys
    import traceback

    replies, requests = sys.stdin.buffer, sys.stdout.buffer

    def send(frame):
        requests.write((json.dumps(frame, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii"))
        requests.flush()

    def receive():
        line = replies.readline()
        if not line:
            raise SystemExit(3)  # the coordinator closed the run
        return json.loads(line)

    class BrowserOperationError(Exception):
        def __init__(self, result):
            self.result = result
            super().__init__((result.get("error") or {}).get("code", "browser_operation_failed"))

    def method(name):
        operation, fixed, names = SDK[name]

        async def call(self, **arguments):
            unknown = sorted(set(arguments) - set(names))
            if unknown:
                raise TypeError(f"{name}() got an unexpected keyword argument {unknown[0]!r}")
            # None omits an optional argument; JSON null is never sent.
            sent = {key: value for key, value in arguments.items() if value is not None}
            send({"t": "op", "name": operation, "arguments": {**sent, **fixed}})
            result = receive()["result"]
            if not result.get("ok"):
                raise BrowserOperationError(result)
            return result

        call.__name__ = call.__qualname__ = name
        return call

    Browser = type("Browser", (), {name: method(name) for name in SDK})

    class Output:
        def __init__(self, stream):
            self.stream = stream

        def write(self, text):
            text = str(text)
            for start in range(0, len(text), 2048):
                send({"t": "out", "s": self.stream, "text": text[start:start + 2048]})
            return len(text)

        def flush(self):
            pass

        def isatty(self):
            return False

    def emit(value):
        send({"t": "emit", "value": value})

    def emit_image(handle):
        send({"t": "emit_image", "handle": handle})

    def problem():
        """The current exception as bounded text: its start names it, its end locates it."""
        text = traceback.format_exc(limit=8)
        return text if len(text) <= 2000 else text[:1000] + "\n...\n" + text[-900:]

    try:  # load what formatting needs now, while memory is certainly available
        raise RuntimeError
    except RuntimeError:
        problem()
    source = receive()["code"]
    sys.stdout, sys.stderr = Output("o"), Output("e")
    try:
        code = compile(source, "<script>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError:
        send({"t": "end", "status": "syntax_error", "text": traceback.format_exc(limit=0)[:2000]})
        return
    scope = {"__name__": "__main__", "browser": Browser(), "emit": emit, "emit_image": emit_image,
             "BrowserOperationError": BrowserOperationError}
    try:
        program = eval(code, scope)
        if code.co_flags & inspect.CO_COROUTINE:
            # SDK calls block until their reply, so the program never suspends itself.
            try:
                program.send(None)
            except StopIteration:
                pass
            else:
                program.close()
                raise RuntimeError("Only browser operations can be awaited; scripts run sequentially")
    except SystemExit as stop:
        if stop.code not in (None, 0):
            send({"t": "end", "status": "error", "text": f"SystemExit: {stop.code}"[:2000]})
            return
    except BaseException:
        scope.clear()  # release the script's objects first: it may have stopped for lack of memory
        send({"t": "end", "status": "error", "text": problem()})
        return
    send({"t": "end", "status": "ok"})


if __name__ == "__main__":
    _main()
