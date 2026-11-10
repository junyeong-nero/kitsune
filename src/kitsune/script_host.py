"""Engine process of one delegated script run (opt-in candidate, #500/#501).

Started by the coordinator as `<engine python> -I -B script_host.py <json config>` with an
empty environment and only its three pipes. It imports no kitsune module, reads no
credential and opens no socket. The guest gets a read-only standard library, this
process's stdin and stdout, and nothing else: no environment, no other directory, no
host function beyond WASI preview 1.

What bounds the run here: the guest's linear memory, this process's file descriptors,
a resident-memory watchdog and a backstop deadline. The coordinator kills the process
group at its own deadline whatever happens here.
"""
import hashlib
import importlib.metadata
import json
import os
import resource
import sys
import threading
import time

WASMTIME_VERSION = "50.0.1"
READY = '{"t":"host_ready"}\n'
EXIT_PIN, EXIT_RSS, EXIT_TRAP, EXIT_DEADLINE = 64, 65, 66, 67


def _watch_memory(limit):
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB elsewhere
    while True:
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > limit:
            os._exit(EXIT_RSS)
        time.sleep(0.05)


def main(argv):
    config = json.loads(argv[1])
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with open(config["wasm"], "rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != config["wasm_sha256"]:
            return EXIT_PIN
    if importlib.metadata.version("wasmtime") != WASMTIME_VERSION:
        return EXIT_PIN
    with open(config["guest"], encoding="utf-8") as stream:
        bootstrap = stream.read()
    from wasmtime import Config, Engine, ExitTrap, Linker, Module, Store, Trap, WasiConfig

    threading.Thread(target=_watch_memory, args=(config["host_rss_bytes"],), daemon=True).start()
    settings = Config()
    settings.wasm_threads = False
    settings.wasm_memory64 = False
    settings.wasm_multi_memory = False
    settings.wasm_component_model = False
    settings.parallel_compilation = False
    engine = Engine(settings)
    module = Module.from_file(engine, config["wasm"])

    guest_stderr = bytearray()

    def keep_stderr(data):
        # The interpreter's own fatal messages; a script's sys.stderr is a frame stream.
        guest_stderr.extend(data[:max(0, 4096 - len(guest_stderr))])
        return len(data)

    wasi = WasiConfig()
    wasi.argv = ["/runtime/bin/python3.15", "-I", "-S", "-B", "-c", bootstrap]
    wasi.env = []
    wasi.preopen_dir(config["lib_dynload"], "/runtime/lib/python3.15/lib-dynload", fs_mutable=False)
    wasi.preopen_dir(config["stdlib"], "/runtime/lib/python3.15", fs_mutable=False)
    wasi.inherit_stdin()
    wasi.inherit_stdout()
    wasi.stderr_custom = keep_stderr
    store = Store(engine)
    store.set_limits(memory_size=config["guest_memory_bytes"], table_elements=100000, instances=1, tables=1,
                     memories=1)
    store.set_wasi(wasi)
    linker = Linker(engine)
    linker.define_wasi()
    instance = linker.instantiate(store, module)
    # After this line the guest shares stdout: the host writes nothing more to it.
    sys.stdout.write(READY)
    sys.stdout.flush()
    # A hard exit, not an interruption: a guest blocked in a host read cannot be interrupted.
    backstop = threading.Timer(config["deadline_seconds"], os._exit, (EXIT_DEADLINE,))
    backstop.daemon = True
    backstop.start()
    code = 0
    try:
        instance.exports(store)["_start"](store)
    except ExitTrap as stop:
        code = stop.code
    except Trap:
        code = EXIT_TRAP
    scale = 1 if sys.platform == "darwin" else 1024
    sys.stderr.write(json.dumps({"rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale,
                                 "guest_stderr": guest_stderr.decode("utf-8", "replace")}) + "\n")
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
