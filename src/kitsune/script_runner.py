"""Coordinator of one delegated script run (opt-in candidate, #500/#501). Not registered in MCP yet.

The script runs in a WASI guest inside a separate engine process (script_host). Its only
channel is a pipe of JSON lines to this module, which turns each operation request into
one FlatBroker.call. The guest never holds a bridge token, a connection, a tab's backend
id or an approval secret, and nothing it writes is evidence: outcomes come from the
ledger kept here, from the broker's results.

Limits enforced here, whatever the guest does: source size, deadline, operation count,
selected output, images, frame size. On every exit path the engine's process group is
killed and reaped. An operation in flight at that moment is recorded as unknown and is
left to finish inside the broker; it is never repeated.

The runtime is provisioned separately and named by an operator's manifest; packaging is
#502's. Limits that are not established: see docs/design/500-delegated-worker.md.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile

from .flat_broker import BrokerImage, FlatBroker

MAX_SOURCE_BYTES = 32 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_OPERATIONS = 100
MAX_IMAGES = 4
MAX_IMAGE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FRAME_BYTES = 512 * 1024
MAX_ERROR_CHARS = 2000
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000
STARTUP_SECONDS = 60  # engine start and module compilation, before the script's own deadline
GUEST_MEMORY_BYTES = 128 * 1024 * 1024
HOST_RSS_BYTES = 1024 * 1024 * 1024
READY = b'{"t":"host_ready"}\n'
_FRAMES = {"op": {"t", "name", "arguments"}, "emit": {"t", "value"}, "emit_image": {"t", "handle"},
           "out": {"t", "s", "text"}, "end": {"t", "status"}}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PACKAGE = Path(__file__).resolve().parent
# Operations still running in the broker after their run ended; referenced until they finish.
_ORPHANS: set = set()


@dataclass(frozen=True)
class ScriptRuntime:
    """The pinned engine and interpreter of an operator's manifest. Trusted configuration."""
    engine_python: str
    wasm: str
    wasm_sha256: str
    stdlib: str
    lib_dynload: str

    @classmethod
    def from_manifest(cls, path):
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
            runtime = cls(**values)
        except (OSError, ValueError, TypeError):
            raise ValueError("Invalid script runtime manifest") from None
        if (any(not isinstance(value, str) for value in values.values()) or not _SHA256.fullmatch(runtime.wasm_sha256)
                or any(not os.path.isabs(p) for p in (runtime.engine_python, runtime.wasm, runtime.stdlib,
                                                      runtime.lib_dynload))
                or not os.path.isfile(runtime.engine_python) or not os.path.isfile(runtime.wasm)
                or not os.path.isdir(runtime.stdlib) or not os.path.isdir(runtime.lib_dynload)):
            raise ValueError("Invalid script runtime manifest")
        return runtime


@dataclass(frozen=True)
class ScriptResult:
    status: str  # completed | failed | cancelled | timed_out | output_limited
    error: dict | None = None  # {"code", "message"} of a failed run
    emitted: object = None
    has_emitted: bool = False
    stdout: str = ""
    stderr: str = ""
    images: tuple[BrokerImage, ...] = ()
    # Coordinator-owned evidence, one entry per requested operation; never the guest's word.
    ledger: tuple[dict, ...] = ()
    diagnostics: dict = field(default_factory=dict)


class _Stop(Exception):
    def __init__(self, status, code=None, message=""):
        self.status, self.code, self.message = status, code, message


def _frame(line):
    """A guest frame, strictly: one bounded ASCII JSON object with exactly its kind's keys."""
    try:
        frame = json.loads(line.decode("ascii"))
        if type(frame) is not dict or type(frame.get("t")) is not str or frame["t"] not in _FRAMES:
            raise ValueError
        optional = {"text"} if frame["t"] == "end" else set()
        if frame.keys() - optional != _FRAMES[frame["t"]]:
            raise ValueError
        # Escapes can spell lone surrogates, which are not text: refuse them here.
        json.dumps(frame, ensure_ascii=False).encode("utf-8")
        return frame
    except (ValueError, UnicodeError, RecursionError):
        raise _Stop("failed", "protocol_error", "The script sent a malformed frame") from None


async def run_script(broker: FlatBroker, runtime: ScriptRuntime, code: str, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                     *, cancel: asyncio.Event | None = None) -> ScriptResult:
    """Run one script to its end, its deadline or a limit, and return what the coordinator observed."""
    if type(code) is not str or not code or type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        return ScriptResult("failed", {"code": "invalid_arguments", "message": "Invalid script arguments"})
    try:
        source = code.encode("utf-8")
    except UnicodeError:
        return ScriptResult("failed", {"code": "invalid_arguments", "message": "Invalid script arguments"})
    if len(source) > MAX_SOURCE_BYTES:
        return ScriptResult("failed", {"code": "source_too_large", "message": "A script is at most 32 KiB of UTF-8"})

    loop = asyncio.get_running_loop()
    ledger, images, emitted_images = [], {}, []
    output = {"o": [], "e": []}
    state = {"emitted": None, "has_emitted": False, "emit_bytes": 0, "out_bytes": 0}
    diagnostics = {}
    ended = []  # set once the guest reported its own end: the engine may then exit by itself
    workdir = tempfile.mkdtemp(prefix="kitsune-script-")
    config = {"wasm": runtime.wasm, "wasm_sha256": runtime.wasm_sha256, "stdlib": runtime.stdlib,
              "lib_dynload": runtime.lib_dynload, "guest": str(_PACKAGE / "script_guest.py"),
              "guest_memory_bytes": GUEST_MEMORY_BYTES, "host_rss_bytes": HOST_RSS_BYTES,
              "deadline_seconds": timeout_ms / 1000 + 5}
    process = stderr_task = None
    stopped = asyncio.ensure_future(cancel.wait()) if cancel is not None else None

    async def before(awaitable, deadline, expired):
        """awaitable's result, unless the deadline or a cancellation comes first."""
        # Checked before anything is awaited: a guest that always answers at once must not
        # win every race against a deadline or a cancellation that has already happened.
        late = _Stop("cancelled") if stopped is not None and stopped.done() else (
            expired if loop.time() >= deadline else None)
        if late is not None:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise late
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task, *([stopped] if stopped else [])},
                                         timeout=max(0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise _Stop("cancelled") if stopped in done else expired
        finally:
            if not task.done():
                task.cancel()

    async def operation(frame, deadline):
        name, arguments = frame["name"], frame["arguments"]
        if type(name) is not str or type(arguments) is not dict:
            raise _Stop("failed", "protocol_error", "The script sent a malformed frame")
        if len(ledger) >= MAX_OPERATIONS:
            raise _Stop("failed", "operation_limit", "A script may request at most 100 operations")
        entry = {"index": len(ledger) + 1, "operation": name if len(name) <= 64 else "unknown",
                 "outcome": "unknown", "ok": False, "settled": False}
        ledger.append(entry)
        # Shielded: ending the run must not abandon the broker's lock or its bookkeeping
        # while the request is still in the bridge.
        call = asyncio.ensure_future(broker.call(name, arguments))
        _ORPHANS.add(call)
        call.add_done_callback(_ORPHANS.discard)
        reply = await before(asyncio.shield(call), deadline, _Stop("timed_out"))
        result = reply.result
        for image in reply.images:
            if len(images) >= MAX_IMAGES or sum(len(i.data) for i in images.values()) + len(image.data) > MAX_IMAGE_TOTAL_BYTES:
                result = {**result, "ok": False, "data": {}, "truncated": True,
                          "error": {"code": "image_limit", "message": "The run's image budget is used up",
                                    "retryable": False, "hint": "A run keeps at most 4 images and 16 MiB.",
                                    "state": {}}}
            else:
                images[image.handle] = image
        entry.update(call=result.get("call"), outcome=result.get("outcome"), ok=result.get("ok") is True,
                     settled=True, receipt=reply.receipt,
                     **({"tab_id": result["tab_id"]} if "tab_id" in result else {}),
                     **({"error": result["error"]["code"]} if result.get("error") else {}))
        return result

    def selected(kind, size):
        state[kind] = size if kind == "emit_bytes" else state[kind] + size
        if state["emit_bytes"] + state["out_bytes"] > MAX_OUTPUT_BYTES:
            raise _Stop("output_limited")

    async def serve():
        nonlocal process, stderr_task
        process = await asyncio.create_subprocess_exec(
            runtime.engine_python, "-I", "-B", str(_PACKAGE / "script_host.py"), json.dumps(config),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"HOME": workdir, "TMPDIR": workdir}, cwd=workdir, close_fds=True, start_new_session=True,
            limit=MAX_FRAME_BYTES)

        async def drain_stderr():
            kept = await process.stderr.read(8192)
            while await process.stderr.read(65536):
                pass
            return kept

        stderr_task = asyncio.ensure_future(drain_stderr())

        async def line():
            try:
                return await process.stdout.readline()
            except ValueError:  # longer than a frame may be
                raise _Stop("failed", "protocol_error", "The script sent an oversized frame") from None

        failed_start = _Stop("failed", "host_failed", "The script engine did not start")
        if await before(line(), loop.time() + STARTUP_SECONDS, failed_start) != READY:
            raise failed_start
        deadline = loop.time() + timeout_ms / 1000

        async def send(frame):
            process.stdin.write((json.dumps(frame, ensure_ascii=True) + "\n").encode("ascii"))
            await before(process.stdin.drain(), deadline, _Stop("timed_out"))

        await send({"t": "source", "code": code})
        while True:
            raw = await before(line(), deadline, _Stop("timed_out"))
            if not raw:
                raise _Stop("failed", "script_exited", "The script ended without finishing")
            if not raw.endswith(b"\n"):
                raise _Stop("failed", "protocol_error", "The script sent a truncated frame")
            frame = _frame(raw)
            kind = frame["t"]
            if kind == "op":
                await send({"t": "result", "result": await operation(frame, deadline)})
            elif kind == "emit":
                selected("emit_bytes", len(json.dumps(frame["value"], ensure_ascii=False).encode("utf-8")))
                state.update(emitted=frame["value"], has_emitted=True)
            elif kind == "out":
                if frame["s"] not in output or type(frame["text"]) is not str:
                    raise _Stop("failed", "protocol_error", "The script sent a malformed frame")
                room = MAX_OUTPUT_BYTES - state["emit_bytes"] - state["out_bytes"]
                data = frame["text"].encode("utf-8")
                output[frame["s"]].append(data[:max(0, room)].decode("utf-8", "ignore"))
                selected("out_bytes", len(data))
            elif kind == "emit_image":
                handle = frame["handle"]
                if type(handle) is not str or handle not in images or handle in emitted_images:
                    raise _Stop("failed", "invalid_image_handle", "Not an image of this run")
                emitted_images.append(handle)
            else:
                text = frame.get("text", "")
                ended.append(True)
                if frame["status"] == "ok" and "text" not in frame:
                    return
                if frame["status"] not in {"syntax_error", "error"} or type(text) is not str:
                    raise _Stop("failed", "protocol_error", "The script sent a malformed frame")
                # The script's own account of why it stopped: text for the caller, not evidence.
                raise _Stop("failed", "syntax_error" if frame["status"] == "syntax_error" else "runtime_error",
                            text[:MAX_ERROR_CHARS])

    status, error = "completed", None
    try:
        await serve()
    except _Stop as stop:
        status = stop.status
        error = {"code": stop.code, "message": stop.message} if stop.status == "failed" else None
    except (OSError, UnicodeError) as problem:
        status, error = "failed", {"code": "host_failed", "message": type(problem).__name__}
    finally:
        # The admission gate is closed by ending the only reader of the guest's requests.
        if process is not None:
            if ended:
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 2)
                except asyncio.TimeoutError:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                diagnostics["exit"] = await asyncio.wait_for(asyncio.shield(process.wait()), 5)
                # Reach the end of the dead process's pipes so their transports close with it.
                process.stdin.close()
                await asyncio.wait_for(process.stdout.read(), 2)
            except asyncio.TimeoutError:
                diagnostics.setdefault("exit", None)  # not reaped in time; reported, never ignored
            except (OSError, ValueError):
                pass
            if stderr_task is not None:
                try:
                    kept = await asyncio.wait_for(stderr_task, 2)
                    diagnostics.update(json.loads(kept.decode("utf-8", "replace").strip().splitlines()[-1]))
                except (asyncio.TimeoutError, ValueError, IndexError, TypeError):
                    stderr_task.cancel()
            # Close what is left of the dead process's pipes in this loop, not at garbage collection.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            await asyncio.sleep(0)
        if stopped is not None:
            stopped.cancel()
        shutil.rmtree(workdir, ignore_errors=True)
    return ScriptResult(status, error, state["emitted"], state["has_emitted"], "".join(output["o"]),
                        "".join(output["e"]), tuple(images[handle] for handle in emitted_images), tuple(ledger),
                        diagnostics)
