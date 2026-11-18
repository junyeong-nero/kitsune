"""Bounded lifecycle for trusted local Python, not a filesystem/network sandbox.

Only the supplied browser object's operations use the host's BridgeClient. Arbitrary
same-user Python can bypass it; enabling this tool grants local code execution.
"""
import asyncio
import atexit
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import time

import anyio

from .errors import failed
from .tool_approvals import ROUTE_TOOLS

CODE_LIMIT = 64000
OUTPUT_LIMIT = 16000  # UTF-8 bytes, shared by printed output and the selected result
OPERATION_LIMIT = 100
FRAME_LIMIT = 2 * 1024 * 1024  # internal browser replies, not model-visible output
STOP_DRAIN_SECONDS = 5


class _Stop(Exception):
    """finished: the script already ended. refuse: it waits for a browser reply, so refuse
    its remaining requests and let it end. Otherwise the process is killed (variables reset)."""

    def __init__(self, code, message, *, finished=False, refuse=False, **details):
        self.finished, self.refuse = finished, refuse
        self.result = {"error": message, "code": code, **details}


def _without_tokens(value):
    if isinstance(value, dict):
        return {key: _without_tokens(item) for key, item in value.items()
                if key not in ("confirmToken", "confirm_token", "resume_token")}
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    return value


class _Worker:
    """One persistent Python process per host client, so variables survive between calls."""

    def __init__(self):
        self.process = None
        self.directory = None
        self.lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()

    def abandon(self):
        """Synchronously stop a process whose event loop is gone."""
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.process = self.directory = None

    async def start(self):
        if self.process is not None and self.process.returncode is None:
            return
        await self.kill()
        self.directory = tempfile.mkdtemp(prefix="kitsune-script-")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "kitsune.script_worker", cwd=self.directory, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=FRAME_LIMIT + 1)

    async def kill(self):
        process, directory, self.process, self.directory = self.process, self.directory, None, None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


_WORKERS = {}  # id(client) -> (client, _Worker); the client stays referenced with its worker


@atexit.register
def _cleanup():
    for _, worker in _WORKERS.values():
        worker.abandon()


def _worker(client):
    entry = _WORKERS.get(id(client))
    if entry is None or entry[0] is not client or entry[1].loop is not asyncio.get_running_loop():
        if entry is not None:
            entry[1].abandon()
        entry = _WORKERS[id(client)] = (client, _Worker())
    return entry[1]


async def run_python(code, client, *, timeout=30, tab_id=None, approve=None):
    """Persistent variables per client; preserve the host client and report every API call.

    Timeout/cancellation stops future mediated calls, not a bridge command already sent; it also
    kills the Python process, so its variables reset (state_reset in the result).
    No failed call or script is retried. A failed API call stops even a catching script, except
    a refusal for the user's per-tool approval (#525, nothing was sent): approve(tool) asks the
    user, and on True the same request is sent again. The time the user takes does not count
    against timeout.
    """
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > CODE_LIMIT:
        return {"error": f"code must be non-empty Python, at most {CODE_LIMIT} UTF-8 bytes",
                "code": "invalid_argument", "retryable": False}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        return {"error": "timeout must be positive and at most 120 seconds", "code": "invalid_argument", "retryable": False}
    worker = _worker(client)
    async with worker.lock:
        await worker.start()
        return await _run(worker, code, client, timeout, tab_id, approve)


async def _run(worker, code, client, timeout, tab_id, approve):
    process = worker.process
    output = bytearray()
    operations = []
    deadline = time.monotonic() + timeout
    keep = False  # whether the idle process keeps its variables for the next call

    def append_output(text):
        encoded = text.encode("utf-8")
        available = OUTPUT_LIMIT - len(output)
        output.extend(encoded[:available])
        if len(encoded) > available:
            raise _Stop("script_output_limit", f"Output exceeded {OUTPUT_LIMIT} UTF-8 bytes")

    async def write(frame):
        process.stdin.write(frame)
        await process.stdin.drain()

    async def send(message):
        frame = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(frame) > FRAME_LIMIT:
            raise _Stop("script_response_limit", "Browser response too large; read a smaller subset or use a direct tool",
                        refuse=True)
        await write(frame)

    async def refuse_rest():
        """The script is stopped: refuse each further browser request (nothing is sent) until it ends."""
        stop = b'{"stop": true}\n'
        await write(stop)
        while line := await process.stdout.readline():
            event = json.loads(line).get("event")
            if event == "done":
                return True
            if event == "call":
                await write(stop)
        return False

    async def events():
        nonlocal deadline
        await send({"code": code, "tab_id": tab_id})
        while line := await process.stdout.readline():
            if len(line) > FRAME_LIMIT:
                raise _Stop("script_output_limit", "Script message exceeded the output limit")
            message = json.loads(line)
            event = message["event"]
            if event == "output":
                append_output(message["text"])
            elif event == "done":
                if "error" in message:
                    append_output(message["error"])
                    raise _Stop("script_error", "Python execution failed; see output", finished=True)
                result = message.get("result")
                if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) + len(output) > OUTPUT_LIMIT:
                    raise _Stop("script_output_limit", f"Output and result exceeded {OUTPUT_LIMIT} UTF-8 bytes",
                                finished=True)
                return {"ok": True, **({"result": result} if result is not None else {})}
            elif event == "call":
                if len(operations) >= OPERATION_LIMIT:
                    raise _Stop("script_operation_limit", f"Script exceeded {OPERATION_LIMIT} browser operations",
                                refuse=True)
                method, route, body = message["method"], message["route"], message.get("body")
                if (method not in ("GET", "POST") or not isinstance(route, str)
                        or not route.startswith("/api/") or len(route) > 256):
                    raise _Stop("script_protocol_error", "Invalid browser request")
                if body is not None and not isinstance(body, dict):
                    raise _Stop("script_protocol_error", "Browser request body must be an object")
                if _without_tokens(body) != body:
                    raise _Stop("script_approval_required", "Use a direct tool for user approval or continuation",
                                refuse=True)
                seconds = message.get("timeout", 15)
                if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
                    raise _Stop("script_protocol_error", "Invalid browser timeout")
                entry = {"method": method, "route": route, "status": "unknown"}
                if body is not None and type(body.get("tabId")) is int:
                    entry["tab_id"] = body["tabId"]
                operations.append(entry)
                while True:
                    reply = await asyncio.to_thread(
                        client.request, method, route, start_bridge=False,
                        timeout=min(seconds, max(0.001, deadline - time.monotonic())),
                        **({"json": body} if method == "POST" else {}))
                    state = reply.get("state") if isinstance(reply, dict) else None
                    if not (approve and isinstance(state, dict) and reply.get("code") == "needs_user"
                            and state.get("detail") == "tool_approval" and "decision" not in state):
                        break
                    asked = time.monotonic()
                    allowed = await approve(state.get("tool"))
                    deadline += time.monotonic() - asked
                    if allowed is not True:
                        reply = allowed if isinstance(allowed, dict) else reply
                        break
                entry["status"] = "failed" if failed(reply) else "ok"
                reply = _without_tokens(reply)
                if failed(reply):
                    if reply.get("code") == "needs_user":
                        if route in ROUTE_TOOLS:
                            reply["recovery_tool"] = ROUTE_TOOLS[route]
                        reply["hint"] = ("Use browser_tools_call(name=recovery_tool) to inspect the direct tool, "
                                         "then execute only the held operation with arguments for approval/help. "
                                         "Earlier script operations remain done; never replay them.")
                    if len(json.dumps(reply, ensure_ascii=False).encode("utf-8")) > OUTPUT_LIMIT:
                        reply = {"error": str(reply.get("error", "Browser operation failed"))[:1000],
                                 "code": str(reply.get("code", ""))[:100], "truncated": True}
                    raise _Stop("script_operation_failed", "A browser operation failed; the script was stopped",
                                refuse=True, failed_operation=reply)
                await send(reply)
            else:
                raise _Stop("script_protocol_error", "Unknown script message")
        raise _Stop("script_error", "Python exited without returning a result")

    async def expire():
        # The deadline moves while the user is asked to approve a tool.
        while (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(left)
        raise asyncio.TimeoutError

    task = asyncio.create_task(events())
    watchdog = asyncio.create_task(expire())
    try:
        done, _ = await asyncio.wait({task, watchdog}, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            watchdog.result()
        result = task.result()
        keep = True
    except asyncio.TimeoutError:
        result = {"error": "Python execution timed out", "code": "script_timeout"}
    except _Stop as stopped:
        result, keep = stopped.result, stopped.finished
        if stopped.refuse:
            # A failed operation stops even a catching script (it cannot reach the browser again).
            try:
                keep = await asyncio.wait_for(refuse_rest(), STOP_DRAIN_SECONDS)
            except (asyncio.TimeoutError, ValueError, KeyError, TypeError, OSError):
                keep = False
    except (ValueError, KeyError, TypeError, OSError) as error:
        result = {"error": f"Script communication failed: {type(error).__name__}", "code": "script_protocol_error"}
    finally:
        # Cancelled MCP calls also reach here. Kill the process group unless it is idle and reusable.
        with anyio.CancelScope(shield=True):
            for pending in (task, watchdog):
                pending.cancel()
            await asyncio.gather(task, watchdog, return_exceptions=True)
            if not keep:
                await worker.kill()

    if not result.get("ok"):
        result.update(ok=False, retryable=False,
                      hint="Earlier operations may have taken effect. Inspect the page; do not replay the whole script. "
                           "Use browser_tools_call for direct approval/recovery, then a new script for remaining work.")
        if result.get("code") == "script_output_limit":
            result["hint"] = ("Output/result is incomplete. Earlier operations may have taken effect; do not replay "
                              "the whole script or completed writes. In a new read-only script, use "
                              "tab.snapshot(target=..., max_depth=..., text=True) or "
                              "tab.query(CSS, fields=[...], limit=...). Filter locally and return only needed "
                              "evidence. Disclose coverage/truncation: no match in a subset is not page-wide absence.")
    if not keep:
        result["state_reset"] = True  # the next call starts with empty Python variables
    return {**result, "output": output.decode("utf-8", "ignore"), "operations": operations}
