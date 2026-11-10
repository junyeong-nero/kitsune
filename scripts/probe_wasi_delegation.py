"""Authored CPython/WASI feasibility tests; not a production runner or authority broker.

Requires the separately approved, pinned private build. No model/browser import.
Only fixed child scenarios are exposed by this CLI. Real browser policy conformance
must still be implemented/tested through its own entry point.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from wasmtime import Config, Engine, ExitTrap, Linker, Module, Store, Trap, WasiConfig, WasmtimeError

WASM_SHA256 = "ee0b8197c2dbed91d951be35644ac36afa676de326a2ce38a5ba95551c8f89ae"
ROOT = None
RECORDS = []


def engine_and_module():
    path = ROOT / "Python-3.15.2/cross-build/wasm32-wasip1/python.wasm"
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WASM_SHA256:
            raise ValueError("Unrecognized CPython/WASI module; review and pin the build first")
    if importlib.metadata.version("wasmtime") != "50.0.1":
        raise ValueError("This investigation requires the pinned private Wasmtime 50.0.1")
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    config.wasm_threads = False
    config.wasm_memory64 = False
    config.wasm_multi_memory = False
    config.wasm_component_model = False
    config.parallel_compilation = False
    engine = Engine(config)
    return engine, Module.from_file(engine, str(path))


def configure_wasi(code, *, stdio=False):
    wasi = WasiConfig()
    wasi.argv = ["/runtime/bin/python3.15", "-I", "-S", "-B", "-c", code]
    wasi.env = []
    empty = ROOT / "empty-lib-dynload"
    empty.mkdir(exist_ok=True)
    wasi.preopen_dir(str(empty), "/runtime/lib/python3.15/lib-dynload", fs_mutable=False)
    wasi.preopen_dir(str(ROOT / "Python-3.15.2/Lib"), "/runtime/lib/python3.15", fs_mutable=False)
    if stdio:
        # The parent supplies fresh run-owned pipes, not the user's terminal/MCP streams.
        wasi.inherit_stdin()
        wasi.inherit_stdout()
        wasi.inherit_stderr()
    return wasi


def configure_store(engine, wasi, *, memory=128 * 1024**2, fuel=1_000_000_000):
    store = Store(engine)
    store.set_limits(memory_size=memory, table_elements=100000, instances=1, tables=1, memories=1)
    store.set_fuel(fuel)
    store.set_epoch_deadline(1)
    store.set_wasi(wasi)
    linker = Linker(engine)
    linker.define_wasi()
    return store, linker


def invoke(engine, module, code, *, allowed=None, memory=128 * 1024**2,
           fuel=1_000_000_000, deadline=None, output_limit=16384):
    stdout, stderr = bytearray(), bytearray()
    output_limited = False

    def capture(target, data):
        nonlocal output_limited
        remaining = max(0, output_limit - len(stdout) - len(stderr))
        target.extend(data[:remaining])
        if len(data) > remaining:
            if not output_limited:
                output_limited = True
                engine.increment_epoch()  # Not catchable as a Python exception inside the guest.
            return -errno.EFBIG
        return len(data)

    wasi = configure_wasi(code)
    if allowed:
        wasi.preopen_dir(str(allowed), "/allowed", fs_mutable=False)
    wasi.stdout_custom = lambda data: capture(stdout, data)
    wasi.stderr_custom = lambda data: capture(stderr, data)
    store, linker = configure_store(engine, wasi, memory=memory, fuel=fuel)
    timer = None
    started = time.monotonic()
    record = {"status": "completed"}
    try:
        instance = linker.instantiate(store, module)
        if deadline is not None:
            timer = threading.Timer(deadline, engine.increment_epoch)
            timer.start()
        try:
            instance.exports(store)["_start"](store)
        except Trap as exc:
            record.update(status="trap", trap_code=str(exc.trap_code), error=str(exc)[:2000])
        except ExitTrap as exc:
            record.update(status="exit", exit_code=exc.code)
        record["memory_bytes"] = instance.exports(store)["memory"].data_len(store)
        record["fuel_remaining"] = store.get_fuel()
    finally:
        if timer:
            timer.cancel()
            timer.join()
        record.update(stdout=stdout.decode("utf-8", "replace"), stderr=stderr.decode("utf-8", "replace"),
                      output_limited=output_limited, elapsed_seconds=time.monotonic() - started)
        store.close()
        linker.close()
        RECORDS.append(record)
    return record


IPC_CODE = '''import ast, json, sys
async def read(*, tab):
    print(json.dumps({'kind':'op', 'tab':tab}), flush=True)
    return json.loads(sys.stdin.readline())['value']
program = "values=[]\\nfor i in range(3):\\n    value=await read(tab=str(i))\\n    if value % 2 == 0: values.append(value)\\nprint(json.dumps({'kind':'selected','values':values}),flush=True)"
coroutine = eval(compile(program, '<authored-probe>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT))
try:
    coroutine.send(None)
except StopIteration:
    pass
'''


def child(scenario):
    if scenario == "file_limit":
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    engine, module = engine_and_module()
    code = ("import sys; print('guest_waiting',flush=True); sys.stdin.buffer.read(1)"
            if scenario == "stdin_wait" else IPC_CODE)
    if scenario == "file_limit":
        code = '''import json
files=[]
blocked=False
error=None
try:
    for i in range(256):
        files.append(open('/runtime/lib/python3.15/os.py','rb'))
except OSError as exc:
    blocked=True
    error=exc.errno
print(json.dumps({'blocked':blocked,'opened':len(files),'errno':error}),flush=True)
'''
    wasi = configure_wasi(code, stdio=True)
    store, linker = configure_store(engine, wasi)
    instance = linker.instantiate(store, module)
    timer = None
    try:
        if scenario == "stdin_wait":
            timer = threading.Timer(0.2, engine.increment_epoch)
            timer.start()
        instance.exports(store)["_start"](store)
        if scenario == "file_limit":
            print(json.dumps({"host_nofile": resource.getrlimit(resource.RLIMIT_NOFILE)}), flush=True)
    finally:
        if timer:
            timer.cancel()
            timer.join()
        store.close()
        linker.close()
        module.close()
        engine.close()


class WasiFeasibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.module = engine_and_module()
        cls.directory = Path(tempfile.mkdtemp(prefix="cases-", dir=ROOT))
        cls.allowed = cls.directory / "allowed"
        cls.allowed.mkdir()
        cls.secret = cls.directory / "outside-token"
        cls.secret.write_text("only-a-dummy-credential")
        (cls.allowed / "public.txt").write_text("public-canary")
        (cls.allowed / "absolute-link").symlink_to(cls.secret)
        (cls.allowed / "relative-link").symlink_to("../outside-token")
        cls.parent_token = os.environ.get("KITSUNE_TOKEN")
        os.environ["KITSUNE_TOKEN"] = "synthetic-parent-token-not-for-guest"

    @classmethod
    def tearDownClass(cls):
        if cls.parent_token is None:
            os.environ.pop("KITSUNE_TOKEN", None)
        else:
            os.environ["KITSUNE_TOKEN"] = cls.parent_token
        cls.module.close()
        cls.engine.close()

    def call(self, code, **kwargs):
        record = invoke(self.engine, self.module, code, **kwargs)
        record["test"] = self.id()
        return record

    def json_result(self, result):
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["stderr"], "", result)
        return json.loads(result["stdout"])

    def test_ordinary_python_and_empty_environment(self):
        result = self.call("import os,sys,json,math; print(json.dumps({'value':math.isqrt(81),'platform':sys.platform,'env':dict(os.environ)}))")
        self.assertEqual(self.json_result(result), {"value": 9, "platform": "wasi", "env": {}})
        self.assertLessEqual(result["memory_bytes"], 128 * 1024**2)

    def test_fresh_instance_clears_globals_and_environment(self):
        first = self.call("import builtins,os; builtins.marker='old'; os.environ['KITSUNE_MODE']='act'")
        self.assertEqual(first["status"], "completed")
        second = self.call("import builtins,os,json; print(json.dumps({'marker':hasattr(builtins,'marker'),'mode':os.environ.get('KITSUNE_MODE')}))")
        self.assertEqual(self.json_result(second), {"marker": False, "mode": None})

    def test_files_symlinks_traversal_and_readonly(self):
        paths = [str(self.secret), "/allowed/absolute-link", "/allowed/relative-link", "/allowed/../outside-token"]
        code = f'''import json,os
result={{'public':open('/allowed/public.txt').read(),'denied':[]}}
for path in {paths!r}:
    try:
        open(path).read()
        result['denied'].append(False)
    except OSError:
        result['denied'].append(True)
for action in (lambda: open('/allowed/public.txt','w'), lambda: open('/allowed/new.txt','w'), lambda: os.unlink('/allowed/public.txt'), lambda: os.rename('/allowed/public.txt','/allowed/moved')):
    try:
        action()
        result['denied'].append(False)
    except OSError:
        result['denied'].append(True)
print(json.dumps(result))
'''
        result = self.json_result(self.call(code, allowed=self.allowed))
        self.assertEqual(result, {"public": "public-canary", "denied": [True] * 8})
        self.assertEqual((self.allowed / "public.txt").read_text(), "public-canary")
        self.assertEqual(self.secret.read_text(), "only-a-dummy-credential")
        self.assertFalse((self.allowed / "new.txt").exists())

    def test_native_fd_is_not_a_wasi_fd(self):
        with self.secret.open("rb") as handle:
            fd = fcntl.fcntl(handle.fileno(), fcntl.F_DUPFD, 1024)
            try:
                self.assertEqual(os.pread(fd, 1, 0), b"o")
                code = f'''import os,errno,json
try:
    os.read({fd},1)
    print(json.dumps({{'bad_fd':False}}))
except OSError as exc:
    print(json.dumps({{'bad_fd':exc.errno==errno.EBADF}}))
'''
                self.assertEqual(self.json_result(self.call(code)), {"bad_fd": True})
            finally:
                os.close(fd)

    def test_memory_limit_rejects_same_allocation_allowed_by_control(self):
        code = '''import json
try:
    value=bytearray(64*1024*1024)
    print(json.dumps({'allocated':len(value)}))
except MemoryError:
    print(json.dumps({'blocked':'MemoryError'}))
'''
        control = self.call(code, memory=128 * 1024**2)
        limited = self.call(code, memory=64 * 1024**2)
        self.assertEqual(self.json_result(control), {"allocated": 64 * 1024**2})
        self.assertEqual(self.json_result(limited), {"blocked": "MemoryError"})
        self.assertLessEqual(limited["memory_bytes"], 64 * 1024**2)

    def test_fuel_trap_cannot_be_caught_by_guest(self):
        code = "print('entered',flush=True)\ntry:\n    while True: pass\nexcept BaseException:\n    print('caught')"
        result = self.call(code, fuel=300_000_000)
        self.assertEqual(result["status"], "trap", result)
        self.assertIn("OUT_OF_FUEL", result["trap_code"])
        self.assertEqual(result["stdout"], "entered\n")

    def test_epoch_trap_cannot_be_caught_by_guest(self):
        code = "print('entered',flush=True)\ntry:\n    while True: pass\nexcept BaseException:\n    print('caught')"
        result = self.call(code, fuel=1_000_000_000_000, deadline=0.2)
        self.assertEqual(result["status"], "trap", result)
        self.assertIn("INTERRUPT", result["trap_code"])
        self.assertEqual(result["stdout"], "entered\n")

    def test_output_overflow_interrupts_even_when_io_error_is_caught(self):
        code = "import sys\nwhile True:\n    try:\n        sys.stdout.write('x'*8192); sys.stdout.flush()\n    except Exception:\n        pass"
        result = self.call(code)
        self.assertTrue(result["output_limited"], result)
        self.assertEqual(result["status"], "trap", result)
        self.assertIn("INTERRUPT", result["trap_code"])
        self.assertLessEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 16384)

    def test_import_boundary_and_raw_socket_host_fd(self):
        imports = {(item.module, item.name) for item in self.module.imports}
        self.assertTrue(imports)
        self.assertEqual({module for module, name in imports}, {"wasi_snapshot_preview1"})
        self.assertFalse({"sock_open", "sock_connect", "proc_spawn"} & {name for module, name in imports})
        # Exercise a native WASI socket function directly, not a missing Python import.
        wat = '''(module
          (import "wasi_snapshot_preview1" "sock_send" (func $send (param i32 i32 i32 i32 i32) (result i32)))
          (memory (export "memory") 1)
          (data (i32.const 8) "\\10\\00\\00\\00\\01\\00\\00\\00x")
          (func (export "run") (param i32) (result i32)
            local.get 0 i32.const 8 i32.const 1 i32.const 0 i32.const 0 call $send))'''
        sender, receiver = socket.socketpair()
        with sender, receiver:
            fd = fcntl.fcntl(sender.fileno(), fcntl.F_DUPFD, 1024)
            self.assertEqual(os.write(fd, b"c"), 1)
            self.assertEqual(receiver.recv(1), b"c")  # Positive control: this host FD really can send.
            store, linker = configure_store(self.engine, WasiConfig())
            raw = Module(self.engine, wat)
            try:
                instance = linker.instantiate(store, raw)
                self.assertEqual(instance.exports(store)["run"](store, fd), 8)  # WASI BADF
                self.assertFalse(select.select([receiver], [], [], 0)[0])
            finally:
                os.close(fd)
                store.close()
                linker.close()
                raw.close()
        # Fresh stores ensure an instance-count limit cannot masquerade as a missing import.
        for namespace, name in (("wasi_snapshot_preview1", "sock_open"),
                                ("wasi_snapshot_preview1", "sock_connect"),
                                ("wasi_snapshot_preview1", "proc_spawn"), ("env", "unrestricted_bridge")):
            store, linker = configure_store(self.engine, WasiConfig())
            unknown = Module(self.engine, f'(module (import "{namespace}" "{name}" (func)))')
            try:
                with self.assertRaisesRegex(WasmtimeError, f"unknown import.*{name}"):
                    linker.instantiate(store, unknown)
            finally:
                unknown.close()
                store.close()
                linker.close()

    def test_guest_process_calls_are_unavailable(self):
        code = '''import os,json
results={}
for name,args in [('fork',()),('execv',('/bin/true',['/bin/true'])),('system',('true',))]:
    try:
        function=getattr(os,name)
        value=function(*args)
        results[name]={'blocked':False,'returned':value}
    except (AttributeError,NotImplementedError,OSError) as exc:
        results[name]={'blocked':True,'type':type(exc).__name__}
try:
    import subprocess
    subprocess.Popen(['/bin/true'])
    results['subprocess']={'blocked':False}
except (ImportError,NotImplementedError,OSError) as exc:
    results['subprocess']={'blocked':True,'type':type(exc).__name__}
print(json.dumps(results))
'''
        result = self.json_result(self.call(code))
        self.assertEqual(set(result), {"fork", "execv", "system", "subprocess"})
        self.assertTrue(all(item["blocked"] for item in result.values()), result)
        # Missing Python APIs alone are not the boundary: the raw spawn import is tested above.

    def test_real_python_tcp_and_udp_connection_attempts_are_denied(self):
        # _socket is built into this WASI CPython: exercise it, rather than relying
        # on an import being absent. Host controls prove both local endpoints work.
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as server:
                server.bind(("127.0.0.1", 0))
                address = server.getsockname()
                if kind == socket.SOCK_STREAM:
                    server.listen(1)
                    with socket.create_connection(address):
                        connection, _ = server.accept()
                        connection.close()
                else:
                    with socket.socket(socket.AF_INET, kind) as sender:
                        sender.sendto(b"control", address)
                    self.assertEqual(server.recvfrom(20)[0], b"control")
                code = f'''import socket,json
created=False
try:
    s=socket.socket(socket.AF_INET, {int(kind)})
    created=True
    s.connect({address!r})
    s.send(b'guest')
    print(json.dumps({{'blocked':False}}))
except OSError as exc:
    print(json.dumps({{'blocked':True,'socket_created':created,'errno':exc.errno}}))
'''
                result = self.json_result(self.call(code))
                self.assertTrue(result["blocked"], result)
                self.assertFalse(select.select([server], [], [], 0)[0])

    def test_host_file_descriptor_ceiling_applies_to_guest_opens(self):
        process = self.launch("file_limit")
        result = json.loads(self.line(process))
        self.assertTrue(result["blocked"], result)
        self.assertGreater(result["opened"], 0)
        self.assertLess(result["opened"], 64)
        self.assertEqual(json.loads(self.line(process)), {"host_nofile": [64, 64]})
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, "")

    def test_syntax_and_runtime_errors_are_nonzero_guest_exits(self):
        for code, marker in (("if :", "SyntaxError"), ("raise RuntimeError('probe')", "RuntimeError: probe")):
            result = self.call(code)
            self.assertEqual(result["status"], "exit", result)
            self.assertEqual(result["exit_code"], 1)
            self.assertIn(marker, result["stderr"])

    def launch(self, scenario):
        env = {"HOME": str(ROOT / "home"), "PATH": "/usr/bin:/bin", "TMPDIR": str(ROOT / "tmp")}
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                    "--root", str(ROOT), "--child", scenario], env=env, cwd=ROOT,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, close_fds=True, start_new_session=True)
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def line(self, process):
        ready, _, _ = select.select([process.stdout], [], [], 15)
        self.assertTrue(ready, "Child produced no line before startup/IPC deadline")
        line = process.stdout.readline()
        self.assertTrue(line, "Child closed stdout before the expected frame")
        return line

    def test_blocking_stdio_needs_parent_kill_not_just_epoch(self):
        process = self.launch("stdin_wait")
        self.assertEqual(self.line(process), "guest_waiting\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=1)  # Child increments its epoch at 0.2s; native stdin remains blocked.
        process.kill()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, -9)

    def test_sequential_top_level_await_and_filtering_over_real_pipes(self):
        process = self.launch("ipc")
        for i in range(3):
            self.assertEqual(json.loads(self.line(process)), {"kind": "op", "tab": str(i)})
            process.stdin.write(json.dumps({"value": i}) + "\n")
            process.stdin.flush()
        self.assertEqual(json.loads(self.line(process)), {"kind": "selected", "values": [0, 2]})
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, "")


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--child", choices=("stdin_wait", "ipc", "file_limit"))
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if args.child:
        child(args.child)
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WasiFeasibilityTests)
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    report = {"status": "authored_guest_feasibility_only", "production_ready": False,
              "authority_conformance_passed": False, "model_executed": False,
              "tests_run": result.testsRun, "tests_successful": result.wasSuccessful(),
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "wasm_sha256": WASM_SHA256, "records": RECORDS}
    path = ROOT / ("wasi-probe-" + time.strftime("%Y%m%dT%H%M%S") + ".json")
    path.write_text(json.dumps(report, indent=2) + "\n")
    print("Evidence:", path)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
