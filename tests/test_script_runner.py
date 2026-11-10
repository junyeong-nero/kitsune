"""Delegated scripts through their real entry point: run_script and a CPython/WASI guest.

Opt-in: skipped unless KITSUNE_E2E_SCRIPT_RUNTIME names a runtime manifest (see
docs/design/500-delegated-worker.md); a skip is not a pass. Every run starts the real
engine process and guest. The limit and isolation tests use a stub in place of the
broker; test_script_authority.py uses the real broker and bridge.
"""
import ast
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from kitsune import script_guest, script_runner
from kitsune.flat_broker import BrokerImage, BrokerReply
from kitsune.script_runner import ScriptRuntime, run_script
from scripts.flat_contract import sdk_signatures, specification

MANIFEST = os.environ.get("KITSUNE_E2E_SCRIPT_RUNTIME")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
# Writes one raw line on the protocol channel, as a script that ignores the SDK can.
RAW = "import os\ndef raw(line):\n    os.write(1, line if isinstance(line, bytes) else line.encode())\n"


class StubBroker:
    """Stands in for FlatBroker.call; answers(name, arguments, n) may return a result override."""

    def __init__(self, answers=None, delay=0):
        self.calls, self.finished, self.answers, self.delay = [], 0, answers, delay

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        number = len(self.calls)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.finished += 1
        result = {"ok": True, "call": f"c_{number}", "operation": name, "outcome": "observed",
                  "data": {"n": number}, "truncated": False}
        images = ()
        override = self.answers(name, arguments, number) if self.answers else None
        if isinstance(override, BrokerImage):
            images, result["data"] = (override,), {"image": override.handle}
        elif override:
            result.update(override)
        return BrokerReply(result, {"call": result["call"]}, images)


def failure(code, outcome="not_dispatched"):
    return {"ok": False, "outcome": outcome, "data": {},
            "error": {"code": code, "message": "x", "retryable": False, "hint": "", "state": {}}}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SdkDeclarationTests(unittest.TestCase):
    def test_guest_sdk_matches_the_499_declarations(self):
        signatures = sdk_signatures()
        bindings = {method: (entry["name"], constants) for entry in specification()["tools"]
                    for method, constants in entry["sdk"].items()}
        self.assertEqual(set(script_guest.SDK), set(signatures))
        for method, (operation, fixed, names) in script_guest.SDK.items():
            with self.subTest(method=method):
                self.assertEqual((operation, fixed), bindings[method])
                self.assertEqual(set(names), set(signatures[method].parameters) - {"self"})

    def test_the_host_and_guest_import_nothing_of_kitsune(self):
        for name in ("script_host.py", "script_guest.py"):
            tree = ast.parse(Path(script_runner.__file__).with_name(name).read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, "no relative import")
                    imported.add(node.module)
            self.assertLessEqual(imported, {"ast", "hashlib", "importlib.metadata", "inspect", "json", "os", "resource",
                                            "sys", "threading", "time", "traceback", "wasmtime"}, name)


@unittest.skipUnless(MANIFEST, "KITSUNE_E2E_SCRIPT_RUNTIME is not set")
class ScriptRunnerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = ScriptRuntime.from_manifest(MANIFEST)

    def setUp(self):
        self.processes, self.workdirs = [], []
        spawn, mkdtemp = asyncio.create_subprocess_exec, tempfile.mkdtemp

        async def recording_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            self.processes.append((process, args, kwargs))
            return process

        def recording_mkdtemp(*args, **kwargs):
            self.workdirs.append(mkdtemp(*args, **kwargs))
            return self.workdirs[-1]

        for target, name, value in ((script_runner.asyncio, "create_subprocess_exec", recording_spawn),
                                    (script_runner.tempfile, "mkdtemp", recording_mkdtemp)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_code(self, code, broker=None, **kwargs):
        broker = broker or StubBroker()
        result = await run_script(broker, self.runtime, code, **kwargs)
        # On every path the engine is gone, reaped, and its directory removed.
        process = self.processes[-1][0]
        self.assertIsNotNone(process.returncode, "the engine process was reaped")
        self.assertFalse(alive(process.pid))
        self.assertFalse(os.path.exists(self.workdirs[-1]))
        return result, broker

    def assert_failed(self, result, code):
        self.assertEqual((result.status, (result.error or {}).get("code")), ("failed", code), result)

    async def test_sequential_calls_loops_branches_and_filtering(self):
        result, broker = await self.run_code('''
kept = []
for index in range(4):
    reply = await browser.read(tab_id=f"t_{index}", max_chars=100, target=None)
    if reply["data"]["n"] % 2 == 0:
        kept.append(reply["call"])
tabs = await browser.tabs()
print("kept", len(kept))
emit({"kept": kept, "tabs": tabs["operation"]})
''')
        self.assertEqual((result.status, result.error), ("completed", None))
        self.assertEqual((result.emitted, result.has_emitted), ({"kept": ["c_2", "c_4"], "tabs": "browser_tabs"}, True))
        self.assertEqual((result.stdout, result.stderr), ("kept 2\n", ""))
        # None omits an argument; fixed arguments are the declared ones.
        self.assertEqual(broker.calls[0], ("browser_read", {"tab_id": "t_0", "max_chars": 100}))
        self.assertEqual(broker.calls[4], ("browser_tabs", {"action": "list"}))
        self.assertEqual([(e["index"], e["operation"], e["call"], e["outcome"], e["ok"]) for e in result.ledger],
                         [(n, "browser_read", f"c_{n}", "observed", True) for n in range(1, 5)]
                         + [(5, "browser_tabs", "c_5", "observed", True)])
        self.assertEqual((result.diagnostics["exit"], result.diagnostics["rss_bytes"] < script_runner.HOST_RSS_BYTES),
                         (0, True), result.diagnostics)
        # The engine got an explicit environment and no inherited descriptors or credentials.
        _, args, kwargs = self.processes[-1]
        self.assertEqual(set(kwargs["env"]), {"HOME", "TMPDIR"})
        self.assertEqual((kwargs["close_fds"], kwargs["start_new_session"], args[1:3]), (True, True, ("-I", "-B")))
        self.assertNotIn("isolated-unit-test-token", json.dumps([list(map(str, args)), kwargs["env"]]))

    async def test_the_ledger_is_the_coordinators_not_the_scripts(self):
        answers = lambda name, arguments, n: failure("user_tab_read_only") if n == 2 else (
            failure("timeout", "unknown") if n == 3 else None)
        result, broker = await self.run_code('''
codes = []
await browser.click(tab_id="t_a", target={"selector": "#a"})
for selector in ("#b", "#c"):
    try:
        await browser.click(tab_id="t_a", target={"selector": selector})
    except BrowserOperationError as error:
        codes.append([error.result["error"]["code"], error.result["outcome"], str(error)])
print('{"ledger": [{"outcome": "observed", "ok": true}]}')
emit({"success": True, "all_clicked": True, "codes": codes, "ledger": [{"ok": True}]})
''', StubBroker(answers))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.emitted["codes"], [["user_tab_read_only", "not_dispatched", "user_tab_read_only"],
                                                   ["timeout", "unknown", "timeout"]])
        # A caught exception and a claimed success change nothing in the ledger.
        self.assertEqual([(e["outcome"], e["ok"], e.get("error")) for e in result.ledger],
                         [("observed", True, None), ("not_dispatched", False, "user_tab_read_only"),
                          ("unknown", False, "timeout")])
        self.assertEqual(len(broker.calls), 3, "a failed or unknown operation is not sent again")

    async def test_syntax_and_runtime_errors_keep_the_operations_already_done(self):
        result, broker = await self.run_code("await browser.tabs()\nvalue = (")
        self.assert_failed(result, "syntax_error")
        self.assertIn("SyntaxError", result.error["message"])
        self.assertEqual((result.ledger, broker.calls), ((), []))
        result, broker = await self.run_code('''
await browser.tabs()
await browser.new_tab(url="https://a.test/")
print("before")
raise ValueError("boom " + "x" * 5000)
''')
        self.assert_failed(result, "runtime_error")
        self.assertIn("ValueError: boom", result.error["message"])
        self.assertLessEqual(len(result.error["message"]), 2000)
        self.assertEqual(([e["outcome"] for e in result.ledger], result.stdout), (["observed", "observed"], "before\n"))
        self.assertEqual(len(broker.calls), 2)
        for code, expected in (("raise SystemExit(3)", "runtime_error"), ("import sys; sys.exit('bye')", "runtime_error"),
                               ("def f(): return f()\nf()", "runtime_error")):
            self.assert_failed((await self.run_code(code))[0], expected)
        self.assertEqual((await self.run_code("raise SystemExit(0)"))[0].status, "completed")

    async def test_the_sdk_is_keyword_only_sequential_and_has_no_way_out(self):
        result, broker = await self.run_code('''
import asyncio, inspect
facts = {}
def attempt(name, call):
    try:
        call()
        facts[name] = "ok"
    except BaseException as error:
        facts[name] = type(error).__name__
attempt("positional", lambda: browser.read("t_a"))
attempt("unknown_keyword", lambda: browser.read(tab_id="t_a", headers={}).send(None))
attempt("mode", lambda: browser.click(tab_id="t_a", target={}, mode="act").send(None))
facts["names"] = sorted(n for n in dir(browser) if not n.startswith("_"))
facts["extra"] = [n for n in ("connect", "request", "evaluate", "run", "script", "policy", "session", "token")
                  if hasattr(browser, n)]
facts["globals"] = sorted(k for k in globals() if not k.startswith("__"))
emit(facts)
await asyncio.sleep(0)
''')
        self.assert_failed(result, "runtime_error")
        self.assertIn("scripts run sequentially", result.error["message"])
        facts = result.emitted
        self.assertEqual((facts["positional"], facts["unknown_keyword"], facts["mode"]),
                         ("TypeError", "TypeError", "TypeError"))
        self.assertEqual(facts["names"], sorted(script_guest.SDK))
        self.assertEqual(facts["extra"], [])
        self.assertEqual(broker.calls, [])

    async def test_a_script_cannot_reach_the_host_its_files_or_a_network(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        self.addCleanup(listener.close)
        secret = Path(tempfile.mkdtemp(prefix="kitsune-script-secret-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(secret, ignore_errors=True))
        (secret / "token").write_text("host-secret-value")
        result, _ = await self.run_code(f'''
import os, sys
facts = {{"environ": dict(os.environ), "library": "os.py" in os.listdir("/runtime/lib/python3.15")}}
def attempt(name, call):
    try:
        value = call()
        facts[name] = "REACHED " + repr(value)[:80]
    except BaseException as error:
        facts[name] = type(error).__name__
def tcp():
    import socket
    s = socket.socket(); s.connect(("127.0.0.1", {listener.getsockname()[1]})); return "connected"
def udp():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b"x", ("127.0.0.1", {listener.getsockname()[1]}))
def url():
    import urllib.request
    return urllib.request.urlopen("http://127.0.0.1:{listener.getsockname()[1]}/", timeout=1).read()
def spawn():
    import subprocess
    return subprocess.run(["/bin/echo", "x"], capture_output=True).stdout
def write_stdlib():
    with open("/runtime/lib/python3.15/kitsune_probe.py", "w") as f: f.write("x")
    return "written"
def is_open(fd):
    try:
        os.fstat(fd); return True
    except OSError:
        return False
for name, call in (
        ("secret", lambda: open({str(secret / "token")!r}).read()),
        ("home_token", lambda: open(os.path.expanduser("~/.kitsune/token")).read()),
        ("etc", lambda: open("/etc/passwd").read()), ("tmp", lambda: os.listdir("/tmp")),
        ("root", lambda: os.listdir("/")), ("cwd", lambda: os.listdir(".")), ("chdir", lambda: os.chdir("/tmp")),
        ("runtime", lambda: os.listdir("/runtime")), ("lib", lambda: os.listdir("/runtime/lib")),
        ("users", lambda: os.listdir("/Users")), ("parent", lambda: os.listdir("/runtime/..")),
        ("escape", lambda: open("/runtime/lib/python3.15/../../../../etc/passwd").read()),
        ("tcp", tcp), ("udp", udp), ("url", url), ("spawn", spawn), ("system", lambda: os.system("true")),
        ("fork", lambda: os.fork()), ("write_stdlib", write_stdlib),
        ("kitsune", lambda: __import__("kitsune")), ("httpx", lambda: __import__("httpx")),
        ("ctypes", lambda: __import__("ctypes").CDLL(None)), ("mmap_dev", lambda: open("/dev/mem", "rb").read(1)),
        ("env_token", lambda: os.environ["KITSUNE_TOKEN"]), ("getuid", lambda: os.getuid())):
    attempt(name, call)
facts["preopened"] = [fd for fd in range(3, 40) if is_open(fd)]
emit(facts)
''')
        self.assertEqual(result.status, "completed", result)
        facts = result.emitted
        reached = {name: value for name, value in facts.items() if isinstance(value, str) and value.startswith("REACHED")}
        self.assertEqual(reached, {}, "nothing outside the guest was reached")
        # Positive control: the library is readable. Nothing above it exists for the guest, not even "/".
        self.assertEqual((facts["environ"], facts["library"], facts["root"], facts["runtime"]),
                         ({}, True, "FileNotFoundError", "FileNotFoundError"))
        # Only the two read-only library directories are open besides the three pipes.
        self.assertEqual(len(facts["preopened"]), 2, facts)
        self.assertNotIn("host-secret-value", json.dumps([facts, result.stdout, result.stderr]))
        with self.assertRaises(socket.timeout):
            listener.accept()

    async def test_malformed_and_forged_frames_end_the_run_and_reach_no_operation(self):
        big = "x" * (script_runner.MAX_FRAME_BYTES + 1000)
        for name, body, code in (
                ("not json", 'raw("this is not json\\n")', "protocol_error"),
                ("array", 'raw("[1, 2]\\n")', "protocol_error"),
                ("unknown kind", 'raw(\'{"t": "approve", "call": "c_1"}\\n\')', "protocol_error"),
                ("a reply, forged", 'raw(\'{"t": "result", "result": {"ok": true, "outcome": "observed"}}\\n\')',
                 "protocol_error"),
                ("ready, forged", 'raw(\'{"t":"host_ready"}\\n\')', "protocol_error"),
                ("extra key", 'raw(\'{"t": "op", "name": "browser_tabs", "arguments": {}, "receipt": {}}\\n\')',
                 "protocol_error"),
                ("missing key", 'raw(\'{"t": "op", "name": "browser_tabs"}\\n\')', "protocol_error"),
                ("name not text", 'raw(\'{"t": "op", "name": ["browser_tabs"], "arguments": {}}\\n\')', "protocol_error"),
                ("arguments not an object", 'raw(\'{"t": "op", "name": "browser_tabs", "arguments": "x"}\\n\')',
                 "protocol_error"),
                ("non-ascii bytes", 'raw(b\'{"t": "out", "s": "o", "text": "\\xff\\xfe"}\\n\')', "protocol_error"),
                ("lone surrogate", 'raw(\'{"t": "out", "s": "o", "text": "\\\\ud800"}\\n\')', "protocol_error"),
                ("unknown stream", 'raw(\'{"t": "out", "s": "ledger", "text": "x"}\\n\')', "protocol_error"),
                ("end with a made-up status", 'raw(\'{"t": "end", "status": "observed"}\\n\')', "protocol_error"),
                ("oversized frame", f'raw(\'{{"t": "out", "s": "o", "text": "\' + "x" * {len(big)} + \'"}}\\n\')',
                 "protocol_error"),
                ("deep nesting", 'raw("[" * 100000 + "\\n")', "protocol_error"),
                ("truncated, then exit", 'raw(\'{"t": "emit", "value": 1\'); os._exit(0)', "protocol_error"),
                ("silent exit", "os._exit(0)", "script_exited"),
                ("unknown image", 'emit_image("i_" + "0" * 32)', "invalid_image_handle")):
            with self.subTest(name=name):
                result, broker = await self.run_code(RAW + body + "\nimport time\nwhile True: time.sleep(1)\n",
                                                     timeout_ms=20000)
                self.assert_failed(result, code)
                self.assertEqual((broker.calls, result.ledger, result.has_emitted), ([], (), False))

    async def test_a_raw_operation_frame_is_only_a_request_to_the_broker(self):
        result, broker = await self.run_code(RAW + '''
import sys, json
raw(json.dumps({"t": "op", "name": "/api/set-policy", "arguments": {"headers": {"X-Kitsune-Mode": "act"}}}) + "\\n")
reply = json.loads(sys.stdin.buffer.readline())
emit(reply["result"]["error"]["code"])
''', StubBroker(lambda name, arguments, n: failure("unknown_operation")))
        self.assertEqual((result.status, result.emitted), ("completed", "unknown_operation"))
        self.assertEqual(broker.calls, [("/api/set-policy", {"headers": {"X-Kitsune-Mode": "act"}})])
        self.assertEqual([(e["operation"], e["outcome"], e["ok"]) for e in result.ledger],
                         [("/api/set-policy", "not_dispatched", False)])

    async def test_deadline_ends_a_busy_a_sleeping_and_a_waiting_script(self):
        for name, code in (("busy", "while True: pass"), ("sleeping", "import time\ntime.sleep(60)"),
                           ("catching", "while True:\n    try:\n        while True: pass\n    except BaseException: pass"),
                           ("reading", "import sys\nsys.stdin.buffer.read(1)")):
            with self.subTest(name=name):
                started = time.monotonic()
                result, _ = await self.run_code("print('started')\n" + code, timeout_ms=700)
                self.assertEqual((result.status, result.error, result.stdout), ("timed_out", None, "started\n"))
                self.assertLess(time.monotonic() - started, 0.7 + 30, "startup is separate; the script got 0.7 s")
                self.assertEqual(self.processes[-1][0].returncode, -9)

    async def test_an_operation_in_flight_at_the_deadline_is_unknown_and_never_repeated(self):
        broker = StubBroker(delay=2.5)
        result, _ = await self.run_code("await browser.tabs()\nawait browser.click(tab_id='t_a', target={'selector': '#pay'})\n"
                                        "emit('done')", broker, timeout_ms=3500)
        self.assertEqual((result.status, result.has_emitted), ("timed_out", False))
        self.assertEqual([(e["operation"], e["outcome"], e["ok"], e["settled"]) for e in result.ledger],
                         [("browser_tabs", "observed", True, True), ("browser_click", "unknown", False, False)])
        self.assertEqual((len(broker.calls), broker.finished), (2, 1))
        await asyncio.sleep(2.7)
        # The request already in the broker finished there; it was neither cancelled nor sent again.
        self.assertEqual((len(broker.calls), broker.finished), (2, 2))

    async def test_cancellation_stops_the_run_and_reaps_the_engine(self):
        cancel = asyncio.Event()
        broker = StubBroker(lambda name, arguments, n: cancel.set() if n == 2 else None)
        result, _ = await self.run_code("for i in range(50):\n    await browser.tabs()\n", broker, cancel=cancel)
        self.assertEqual(result.status, "cancelled")
        self.assertLessEqual(len(broker.calls), 3)
        self.assertEqual([e["outcome"] for e in result.ledger][:2], ["observed", "observed"])
        # Cancelling the caller's task has no result to return, and still leaves no process.
        task = asyncio.ensure_future(run_script(StubBroker(), self.runtime, "while True: pass"))
        while len(self.processes) < 2:
            await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        process = self.processes[-1][0]
        self.assertEqual(process.returncode, -9)
        self.assertFalse(alive(process.pid))
        self.assertFalse(os.path.exists(self.workdirs[-1]))

    async def test_selected_output_and_operation_count_are_bounded(self):
        result, _ = await self.run_code("while True: print('x' * 1000)")
        self.assertEqual(result.status, "output_limited")
        self.assertEqual(len(result.stdout.encode()), script_runner.MAX_OUTPUT_BYTES)
        result, _ = await self.run_code("import sys\nprint('a' * 9000)\nsys.stderr.write('b' * 9000)\nemit('late')")
        self.assertEqual((result.status, result.has_emitted, len(result.stdout), len(result.stderr)),
                         ("output_limited", False, 9001, 16384 - 9001))
        result, _ = await self.run_code("emit('x' * 20000)")
        self.assertEqual((result.status, result.has_emitted), ("output_limited", False))
        # A later emit replaces the earlier one, and non-ASCII text is counted in UTF-8 bytes.
        result, _ = await self.run_code("emit('x' * 15000)\nemit('small')\nprint('가' * 5000)")
        self.assertEqual((result.status, result.emitted, len(result.stdout.encode())), ("completed", "small", 15001))
        result, _ = await self.run_code("emit('small')\nprint('가' * 6000)")
        self.assertEqual(result.status, "output_limited")
        self.assertLessEqual(len(result.stdout.encode()) + 7, script_runner.MAX_OUTPUT_BYTES)

        result, broker = await self.run_code(
            "for i in range(150):\n    try:\n        await browser.tabs()\n    except BaseException:\n        pass\n")
        self.assert_failed(result, "operation_limit")
        self.assertEqual((len(result.ledger), len(broker.calls)), (100, 100))

    async def test_guest_memory_is_bounded_and_the_engine_reports_its_own(self):
        result, _ = await self.run_code('''
try:
    block = bytearray(200 * 1024 * 1024)
    emit("allocated")
except MemoryError:
    emit("memory_error")
''')
        self.assertEqual((result.status, result.emitted), ("completed", "memory_error"))
        self.assertLess(result.diagnostics["rss_bytes"], script_runner.HOST_RSS_BYTES)
        result, _ = await self.run_code("blocks = []\nwhile True:\n    blocks.append(bytearray(1024 * 1024))")
        self.assert_failed(result, "runtime_error")
        self.assertIn("MemoryError", result.error["message"])
        # The engine process's own ceiling: a watchdog on resident memory ends the run.
        with patch.object(script_runner, "HOST_RSS_BYTES", 64 * 1024 * 1024):
            result, _ = await self.run_code("emit(1)")
        self.assert_failed(result, "host_failed")
        self.assertEqual(self.processes[-1][0].returncode, 65)

    async def test_images_stay_with_the_coordinator_and_are_bounded_per_run(self):
        shots = [BrokerImage(f"i_{n:032x}", "image/png", PNG + bytes([n])) for n in range(1, 7)]
        broker = StubBroker(lambda name, arguments, n: shots[n - 1])
        result, _ = await self.run_code('''
handles, errors = [], []
for index in range(6):
    try:
        handles.append((await browser.screenshot(tab_id="t_a"))["data"]["image"])
    except BrowserOperationError as error:
        errors.append(error.result["error"]["code"])
emit_image(handles[2])
emit_image(handles[0])
emit({"handles": handles, "errors": errors})
''', broker)
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(result.emitted, {"handles": [s.handle for s in shots[:4]], "errors": ["image_limit"] * 2})
        self.assertEqual(result.images, (shots[2], shots[0]), "only the emitted images, in the script's order")
        self.assertEqual([(e["ok"], e.get("error")) for e in result.ledger],
                         [(True, None)] * 4 + [(False, "image_limit")] * 2)
        result, _ = await self.run_code("h = (await browser.screenshot(tab_id='t_a'))['data']['image']\n"
                                        "emit_image(h)\nemit_image(h)", StubBroker(lambda *a: shots[0]))
        self.assert_failed(result, "invalid_image_handle")
        with patch.object(script_runner, "MAX_IMAGE_TOTAL_BYTES", len(PNG) + 5):
            result, _ = await self.run_code('''
await browser.screenshot(tab_id="t_a")
try:
    await browser.screenshot(tab_id="t_a")
except BrowserOperationError as error:
    emit(error.result["error"]["code"])
''', StubBroker(lambda name, arguments, n: shots[n]))
        self.assertEqual(result.emitted, "image_limit")

    async def test_invalid_requests_start_no_engine(self):
        for code, timeout, expected in (("", 1000, "invalid_arguments"), (None, 1000, "invalid_arguments"),
                                        (b"emit(1)", 1000, "invalid_arguments"), ("emit(1)", 0, "invalid_arguments"),
                                        ("emit(1)", 120001, "invalid_arguments"), ("emit(1)", 1.5, "invalid_arguments"),
                                        ("emit(1)", True, "invalid_arguments"), ("emit('\ud800')", 1000, "invalid_arguments"),
                                        ("#" + "가" * 10923, 1000, "source_too_large"),
                                        ("x" * 32769, 1000, "source_too_large")):
            with self.subTest(code=str(code)[:20], timeout=timeout):
                result = await run_script(StubBroker(), self.runtime, code, timeout)
                self.assert_failed(result, expected)
        self.assertEqual(self.processes, [])
        result, _ = await self.run_code("#" + "가" * 10900 + "\nemit('fits')")
        self.assertEqual((result.status, result.emitted), ("completed", "fits"))

    async def test_an_unpinned_or_incomplete_runtime_does_not_run(self):
        values = json.loads(Path(MANIFEST).read_text())
        with tempfile.TemporaryDirectory() as directory:
            def manifest(**changes):
                path = Path(directory, "manifest.json")
                path.write_text(json.dumps({**values, **changes}))
                return path

            for changes in ({"wasm": "relative/python.wasm"}, {"wasm": "/nonexistent/python.wasm"},
                            {"wasm_sha256": "ABC"}, {"stdlib": values["wasm"]}, {"engine_python": directory},
                            {"extra": "x"}, {"lib_dynload": 7}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    ScriptRuntime.from_manifest(manifest(**changes))
            with self.assertRaises(ValueError):
                ScriptRuntime.from_manifest(Path(directory, "missing.json"))
            other = Path(directory, "other.wasm")
            other.write_bytes(b"\x00asm\x01\x00\x00\x00")
            for changes in ({"wasm_sha256": "0" * 64}, {"wasm": str(other)}):
                with self.subTest(changes=changes):
                    broker = StubBroker()
                    result = await run_script(broker, ScriptRuntime.from_manifest(manifest(**changes)), "emit(1)")
                    self.assert_failed(result, "host_failed")
                    self.assertEqual((self.processes[-1][0].returncode, result.has_emitted, broker.calls), (64, False, []))


if __name__ == "__main__":
    unittest.main()
