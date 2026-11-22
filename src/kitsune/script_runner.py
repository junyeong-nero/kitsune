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
from .tool_approvals import ROUTE_TOOLS

CODE_LIMIT = 64000
OUTPUT_LIMIT = 16000  # UTF-8 bytes, shared by printed output and the selected result
OPERATION_LIMIT = 100
FRAME_LIMIT = 2 * 1024 * 1024  # internal browser replies, not model-visible output

# Routes that only read the page or wait on it (#568). None is a bridge WRITE_ACTIONS, agent-JavaScript
# or ask-before-act action, so a failed one changed nothing and the script gets its failure as the
# API's exception. Exact spellings only. Not here: /api/wait-for-result (wait_for(js=...)), which
# runs the script's JavaScript in the page, and reads that scroll it (element and full-page captures).
READ_ROUTES = frozenset({
    "/api/find", "/api/forms", "/api/page-info", "/api/page-text", "/api/query", "/api/snapshot",
    "/api/wait-for-element", "/api/wait-for-network-idle", "/api/wait-for-text", "/api/wait-for-title",
    "/api/wait-for-url",
})
# Refusals for the user's sake (approval, tab ownership, user activity, permissions): they stop a
# script on every route, so it cannot catch its way past one.
USER_STOP_CODES = frozenset({"needs_user", "confirmation_required", "user_tab", "user_tab_read_only", "user_active",
                             "permission_required", "host_permission_denied", "unsupported_target"})


class _Stop(Exception):
    def __init__(self, code, message, **details):
        self.result = {"error": message, "code": code, **details}


def _catchable(route, body, reply):
    """Whether a failed reply goes back to the script as its exception instead of stopping it (#568)."""
    if route not in READ_ROUTES:
        return False
    # A find that loads more scrolls the page, which the extension checks as a write.
    if route == "/api/find" and body and (body.get("loadMore") or body.get("load_more")):
        return False
    code = reply.get("code")
    if code in USER_STOP_CODES or "needs_user" in reply:
        return False
    # The bridge's policy refusals (read-only mode, URL allow and deny lists) have no code. Of the
    # failures without one, only an unmet wait (ok false, as Tab.wait_for reads it) is delivered.
    return bool(code) or reply.get("ok") is False


def _without_tokens(value):
    if isinstance(value, dict):
        return {key: _without_tokens(item) for key, item in value.items()
                if key not in ("confirmToken", "confirm_token", "resume_token")}
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    return value


def _fit(result, available):
    """result as it fits in available UTF-8 bytes of JSON, and the UTF-8 bytes left out (#570).

    A result over the budget is cut as text: a string to its start, any other value to the start
    of its JSON text. None when no character fits; that counts as a byte or more left out, so an
    empty string that does not fit is marked too.
    """
    serialized = json.dumps(result, ensure_ascii=False)
    if result is None or len(serialized.encode("utf-8")) <= available:
        return result, 0
    text = result if isinstance(result, str) else serialized
    low, high = 0, min(len(text), max(0, available - 2))  # a character takes a byte or more, the quotes two
    while low < high:  # the longest start whose JSON string fits: escapes make it longer than its text
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle], ensure_ascii=False).encode("utf-8")) <= available:
            low = middle
        else:
            high = middle - 1
    return text[:low] or None, max(1, len(text.encode("utf-8")) - len(text[:low].encode("utf-8")))


def _same_link(body, reply):
    """The same_element the extension says to follow for body's stale ref (#572), else None.

    The extension sets follow only on a script's own write to a tab this client owns, for the old
    ref of a link whose one same element in the tab's new document is a link with the same
    destination. The marker never reaches the script or the model.
    """
    state = reply.get("state") if isinstance(reply, dict) else None
    same = state.get("same_element") if isinstance(state, dict) else None
    if not isinstance(same, dict) or same.pop("follow", None) is not True:
        return None
    if (reply.get("code") != "stale_ref" or state.get("reason") != "other_document" or not isinstance(body, dict)
            or body.get("ref") != state.get("ref") or not isinstance(same.get("ref"), str)):
        return None
    return same


async def run_python(code, client, *, timeout=30, tab_id=None, approve=None):
    """Fresh variables and process; preserve the host client and report every API call.

    Timeout/cancellation stops future mediated calls, not a bridge command already sent.
    No failed call or script is retried. A failed API call stops even a catching script, except
    a refusal for the user's per-tool approval (#525, nothing was sent): approve(tool) asks the
    user, and on True the same request is sent again. The time the user takes does not count
    against timeout. The other exception is a failed read or wait (_catchable, #568): the worker
    raises it in the script, and when the script does not catch it the result is the stop's.
    Output and result over OUTPUT_LIMIT are cut, not failed (#570): the script runs to its end
    and the reply says truncated with the bytes left out. A stale link ref the extension says to
    follow (_same_link, nothing was sent either) is sent once more as a new request with the same
    element's ref, so every check runs on that element; the operation and its reply report
    remapped {from, to, role, name}.
    """
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > CODE_LIMIT:
        return {"error": f"code must be non-empty Python, at most {CODE_LIMIT} UTF-8 bytes",
                "code": "invalid_argument", "retryable": False}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        return {"error": "timeout must be positive and at most 120 seconds", "code": "invalid_argument", "retryable": False}

    output = bytearray()
    dropped = 0  # UTF-8 bytes of output and result over OUTPUT_LIMIT, left out of the reply
    operations = []
    deadline = time.monotonic() + timeout
    delivered = None  # the failed reply the worker's last call got, until its next call

    def operation_failed(reply):
        if len(json.dumps(reply, ensure_ascii=False).encode("utf-8")) > OUTPUT_LIMIT:
            reply = {"error": str(reply.get("error", "Browser operation failed"))[:1000],
                     "code": str(reply.get("code", ""))[:100], "truncated": True}
        return _Stop("script_operation_failed", "A browser operation failed; the script was stopped",
                     failed_operation=reply)

    def append_output(text):
        # Nothing follows a cut, and a cut never splits a character.
        nonlocal dropped
        encoded = text.encode("utf-8")
        kept = b"" if dropped else encoded[:OUTPUT_LIMIT - len(output)].decode("utf-8", "ignore").encode("utf-8")
        output.extend(kept)
        dropped += len(encoded) - len(kept)

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
            nonlocal deadline, delivered, dropped
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
                        if message.get("uncaught_reply") and delivered is not None:
                            raise operation_failed(delivered)
                        before = dropped
                        append_output(message["error"])
                        # An exception the full output has no room for is said here, not lost (#570).
                        raise _Stop("script_error", "Python execution failed; see output" if dropped == before
                                    else "Python execution failed: " + message["error"][:1000])
                    result, cut = _fit(message.get("result"), OUTPUT_LIMIT - len(output))
                    dropped += cut
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
                    remapped = None
                    while True:
                        reply = await asyncio.to_thread(
                            client.request, method, route, start_bridge=False,
                            timeout=min(seconds, max(0.001, deadline - time.monotonic())),
                            **({"json": body} if method == "POST" else {}))
                        same = _same_link(body, reply)
                        if same and remapped is None:
                            remapped = entry["remapped"] = {"from": body["ref"], "to": same["ref"],
                                                            "role": same.get("role"), "name": same.get("name")}
                            body = {**body, "ref": same["ref"]}
                            continue
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
                    if remapped and isinstance(reply, dict):
                        reply["remapped"] = remapped
                    delivered = reply if failed(reply) and _catchable(route, body, reply) else None
                    if failed(reply) and delivered is None:
                        if reply.get("code") == "needs_user":
                            if route in ROUTE_TOOLS:
                                reply["recovery_tool"] = ROUTE_TOOLS[route]
                            reply["hint"] = ("Use browser_tools_call(name=recovery_tool) to inspect the direct tool, "
                                             "then execute only the held operation with arguments for approval/help. "
                                             "Earlier script operations remain done; never replay them.")
                        raise operation_failed(reply)
                    await send(reply)
                else:
                    raise _Stop("script_protocol_error", "Unknown script message")
            raise _Stop("script_error", "Python exited without returning a result")

        async def expire():
            # The deadline moves while the user is asked to approve a tool.
            while (left := deadline - time.monotonic()) > 0:
                await asyncio.sleep(left)
            raise asyncio.TimeoutError

        tasks = [asyncio.create_task(events()), asyncio.create_task(stderr())]
        watchdog = asyncio.create_task(expire())
        try:
            gathered = asyncio.gather(*tasks)
            done, _ = await asyncio.wait({gathered, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            if gathered not in done:
                gathered.cancel()
                await asyncio.gather(gathered, return_exceptions=True)
                watchdog.result()
            result, _ = gathered.result()
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
                for task in (*tasks, watchdog):
                    task.cancel()
                await asyncio.gather(*tasks, watchdog, return_exceptions=True)
                await process.communicate()

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
    if dropped:
        selected = result.pop("result", None)  # after the marker, which is read before the cut content
        result.update(truncated=True, dropped_bytes=dropped)
        if result["ok"]:
            result["hint"] = (f"Output/result was cut at {OUTPUT_LIMIT} UTF-8 bytes; dropped_bytes were left out "
                              "(a cut non-string result is the start of its JSON text). The script ran to its "
                              "end and operations lists what was done: do not replay it. No match in the shown "
                              "part is not page-wide absence. For the rest, in a new read-only script use "
                              "tab.snapshot(target=..., max_depth=..., text=True) or "
                              "tab.query(CSS, fields=[...], limit=...) and return only needed evidence.")
        if selected is not None:
            result["result"] = selected
    return {**result, "output": output.decode("utf-8", "ignore"), "operations": operations}
