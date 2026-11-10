"""Bounded lifecycle for trusted local Python, not a filesystem/network sandbox.

Only the supplied browser object's operations use the host's BridgeClient. Arbitrary
same-user Python can bypass it; enabling this tool grants local code execution.
"""
import asyncio
import json
import math
import os
import signal
import sys
import tempfile
import time

import anyio

from .errors import failed

CODE_LIMIT = 64000
OUTPUT_LIMIT = 16000  # UTF-8 bytes, shared by printed output and the selected result
OPERATION_LIMIT = 100
FRAME_LIMIT = 2 * 1024 * 1024  # internal browser replies, not model-visible output


class _Stop(Exception):
    def __init__(self, code, message, **details):
        self.result = {"error": message, "code": code, **details}


def _without_tokens(value):
    if isinstance(value, dict):
        return {key: _without_tokens(item) for key, item in value.items()
                if key not in ("confirmToken", "confirm_token", "resume_token")}
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    return value


async def run_python(code, client, *, timeout=30, tab_id=None):
    """Fresh variables and process; preserve the host client and report every API call.

    Timeout/cancellation stops future mediated calls, not a bridge command already sent.
    No failed call or script is retried. A failed API call stops even a catching script.
    """
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > CODE_LIMIT:
        return {"error": f"code must be non-empty Python, at most {CODE_LIMIT} UTF-8 bytes",
                "code": "invalid_argument", "retryable": False}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        return {"error": "timeout must be positive and at most 120 seconds", "code": "invalid_argument", "retryable": False}

    output = bytearray()
    operations = []
    deadline = time.monotonic() + timeout

    def append_output(text):
        encoded = text.encode("utf-8")
        available = OUTPUT_LIMIT - len(output)
        output.extend(encoded[:available])
        if len(encoded) > available:
            raise _Stop("script_output_limit", f"Output exceeded {OUTPUT_LIMIT} UTF-8 bytes")

    with tempfile.TemporaryDirectory(prefix="kitsune-script-") as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "kitsune.script_worker", cwd=directory, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=FRAME_LIMIT + 1)

        async def send(message):
            frame = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(frame) > FRAME_LIMIT:
                raise _Stop("script_response_limit", "Browser response too large; read a smaller subset or use a direct tool")
            process.stdin.write(frame)
            await process.stdin.drain()

        async def stderr():
            while chunk := await process.stderr.read(4096):
                append_output(chunk.decode("utf-8", "replace"))

        async def events():
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
                        raise _Stop("script_error", "Python execution failed; see output")
                    result = message.get("result")
                    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) + len(output) > OUTPUT_LIMIT:
                        raise _Stop("script_output_limit", f"Output and result exceeded {OUTPUT_LIMIT} UTF-8 bytes")
                    return {"ok": True, **({"result": result} if result is not None else {})}
                elif event == "call":
                    if len(operations) >= OPERATION_LIMIT:
                        raise _Stop("script_operation_limit", f"Script exceeded {OPERATION_LIMIT} browser operations")
                    method, route, body = message["method"], message["route"], message.get("body")
                    if (method not in ("GET", "POST") or not isinstance(route, str)
                            or not route.startswith("/api/") or len(route) > 256):
                        raise _Stop("script_protocol_error", "Invalid browser request")
                    if body is not None and not isinstance(body, dict):
                        raise _Stop("script_protocol_error", "Browser request body must be an object")
                    if _without_tokens(body) != body:
                        raise _Stop("script_approval_required", "Use a direct tool for user approval or continuation")
                    seconds = message.get("timeout", 15)
                    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
                        raise _Stop("script_protocol_error", "Invalid browser timeout")
                    entry = {"method": method, "route": route, "status": "unknown"}
                    if body is not None and type(body.get("tabId")) is int:
                        entry["tab_id"] = body["tabId"]
                    operations.append(entry)
                    reply = await asyncio.to_thread(
                        client.request, method, route, start_bridge=False,
                        timeout=min(seconds, max(0.001, deadline - time.monotonic())),
                        **({"json": body} if method == "POST" else {}))
                    entry["status"] = "failed" if failed(reply) else "ok"
                    reply = _without_tokens(reply)
                    if failed(reply):
                        if reply.get("code") == "needs_user":
                            reply["hint"] = "Use a direct tool for user help or approval, then a new call for the remaining work."
                        if len(json.dumps(reply, ensure_ascii=False).encode("utf-8")) > OUTPUT_LIMIT:
                            reply = {"error": str(reply.get("error", "Browser operation failed"))[:1000],
                                     "code": str(reply.get("code", ""))[:100], "truncated": True}
                        raise _Stop("script_operation_failed", "A browser operation failed; the script was stopped",
                                    failed_operation=reply)
                    await send(reply)
                else:
                    raise _Stop("script_protocol_error", "Unknown script message")
            raise _Stop("script_error", "Python exited without returning a result")

        tasks = [asyncio.create_task(events()), asyncio.create_task(stderr())]
        try:
            result, _ = await asyncio.wait_for(asyncio.gather(*tasks), max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            result = {"error": "Python execution timed out", "code": "script_timeout"}
        except _Stop as stopped:
            result = stopped.result
        except (ValueError, KeyError, TypeError, OSError) as error:
            result = {"error": f"Script communication failed: {type(error).__name__}", "code": "script_protocol_error"}
        finally:
            # Cancelled MCP calls also reach here. Kill the process group before draining its pipes.
            with anyio.CancelScope(shield=True):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await process.communicate()

    if not result.get("ok"):
        result.update(ok=False, retryable=False,
                      hint="Earlier operations may have taken effect. Inspect the page; do not replay the whole script. "
                           "Use direct tools for user approval, then make an explicit new call for the remaining work.")
    return {**result, "output": output.decode("utf-8", "ignore"), "operations": operations}
