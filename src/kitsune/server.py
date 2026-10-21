"""
Kitsune MCP server.
Exposes the Kitsune browser extension (Firefox, Zen and other Gecko browsers)
as MCP tools. Starts the shared bridge daemon on demand.
"""

import functools
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from time import sleep as _sleep
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import bridge_control
from .bridge_control import BRIDGE, HTTP_PORT, LOG_FILE, TOKEN_FILE, WS_PORT, headers as _headers
from .version import BRIDGE_BUILD
from .waits import (
    RESUME_DEADLINE_SECONDS, WORKFLOW_DEADLINE_SECONDS, batch_deadline_seconds, command_wait_seconds,
    container_request_seconds,
)

# Annotation hints describe the whole tool, including its most dangerous operation.
# They are advisory metadata for the client's permission prompts; the bridge never reads
# them. Enforcement is the bridge policy (browser_set_policy: readonly, allow/deny, ask).
# Idempotence means repeating identical arguments adds no further side effects;
# keep it false for page interactions that can trigger application event handlers.
READ = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
ACT = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
IDEMPOTENT_ACT = {**ACT, "idempotentHint": True}
DESTRUCTIVE = {**ACT, "destructiveHint": True}


# JSON Schema keywords whose values hold subschemas; everything else is a keyword value.
_SUBSCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SUBSCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SUBSCHEMAS = frozenset({"items", "additionalProperties", "not"})


def _untitled_schema(schema):
    """Copy of a JSON schema without `title` annotations (#120).

    pydantic titles every argument model and parameter ("browser_clickArguments", "Tab Id")
    from names the schema already carries. `title` is an annotation, so validation is
    unchanged. Only the `title` keyword goes; a parameter named title stays in `properties`.
    """
    if not isinstance(schema, dict):
        return schema
    untitled = {}
    for keyword, value in schema.items():
        if keyword == "title":
            continue
        if keyword in _SUBSCHEMA_MAPS and isinstance(value, dict):
            value = {name: _untitled_schema(subschema) for name, subschema in value.items()}
        elif keyword in _SUBSCHEMA_LISTS and isinstance(value, list):
            value = [_untitled_schema(subschema) for subschema in value]
        elif keyword in _SUBSCHEMAS:
            value = _untitled_schema(value)
        untitled[keyword] = value
    return untitled


_NULL = {"type": "null"}


def _published_parameter(schema: dict) -> dict:
    """A non-required parameter's schema without a nullable anyOf or an empty default (#142).

    `anyOf: [X, {type: null}]` with default null becomes X: the registered model still
    accepts an explicit null, which validates to the same None as omitting the parameter.
    A default of null or "" is what omission already gives, so it is not published.
    """
    variants = schema.get("anyOf")
    if ("default" in schema and schema["default"] is None and isinstance(variants, list)
            and len(variants) == 2 and _NULL in variants):
        (variant,) = [subschema for subschema in variants if subschema != _NULL]
        schema = {**variant, **{keyword: value for keyword, value in schema.items() if keyword != "anyOf"}}
    if "default" in schema and (schema["default"] is None or schema["default"] == ""):
        schema = {keyword: value for keyword, value in schema.items() if keyword != "default"}
    return schema


def _published_schema(schema: dict) -> dict:
    """Input schema as tools/list publishes it: untitled, with plain optional parameters."""
    schema = _untitled_schema(schema)
    if not isinstance(schema.get("properties"), dict):
        return schema
    required = set(schema.get("required", ()))
    return {**schema, "properties": {
        name: parameter if name in required else _published_parameter(parameter)
        for name, parameter in schema["properties"].items()}}


class KitsuneMCP(FastMCP):
    """FastMCP that publishes tool input schemas without generated titles or nullable optionals.

    Descriptions are normalized with inspect.cleandoc, so they are the same on every
    supported Python (only 3.13+ strips docstring indentation at compile time, #130).
    Calls are still validated against the registered pydantic argument models.
    """

    async def list_tools(self):
        return [tool.model_copy(update={
                    "description": inspect.cleandoc(tool.description) if tool.description else tool.description,
                    "inputSchema": _published_schema(tool.inputSchema),
                })
                for tool in await super().list_tools()]


mcp = KitsuneMCP("Kitsune")

CAPABILITY_GROUPS = frozenset({"core", "tabs", "network", "storage", "parallel", "devtools", "windows", "session"})
ENABLED_CAPS = frozenset(
    cap.strip().lower() for cap in os.environ.get("KITSUNE_CAPS", "core").split(",") if cap.strip()
)
_unknown_caps = ENABLED_CAPS - CAPABILITY_GROUPS - {"all"}
if _unknown_caps:
    raise ValueError(f"Unknown KITSUNE_CAPS: {', '.join(sorted(_unknown_caps))}")

# Core subsets (KITSUNE_TOOL_PRESET); full keeps the whole core group.
# lean: the 14 tools browser-mcp-bench's read + draft tasks used, the default since the
# #31 A/B; docs/guide.md#lean-tool-preset lists what it drops.
TOOL_PRESETS = {
    "lean": frozenset({
        "browser_status", "browser_tabs", "browser_navigate", "browser_page_info",
        "browser_page_text", "browser_snapshot", "browser_forms", "browser_dom",
        "browser_find", "browser_screenshot", "browser_click", "browser_type",
        "browser_select_option", "browser_form_fill",
    }),
}


def tool_preset(preset: str | None, caps: frozenset) -> str:
    """The core preset in effect: an explicit KITSUNE_TOOL_PRESET, else lean, or full with KITSUNE_CAPS=all."""
    preset = (preset or "").strip().lower()
    if not preset:
        return "full" if "all" in caps else "lean"
    if preset != "full" and preset not in TOOL_PRESETS:
        raise ValueError(f"Unknown KITSUNE_TOOL_PRESET: {preset}")
    return preset


TOOL_PRESET = tool_preset(os.environ.get("KITSUNE_TOOL_PRESET"), ENABLED_CAPS)


def _failed(result) -> bool:
    """Whether a tool's own result reports failure: a non-empty top-level error or ok=false.

    Warnings accompany successes, and batch/parallel/broadcast/workflow results report
    per-command outcomes that may include completed writes, so neither marks the call failed.
    """
    return isinstance(result, dict) and (bool(result.get("error")) or result.get("ok") is False)


def _mcp_errors(function):
    """Return failed results as MCP errors (isError) with the same JSON text content."""
    @functools.wraps(function)
    def call(*args, **kwargs):
        result = function(*args, **kwargs)
        if not _failed(result):
            return result
        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)

    return call


def tool(group: str, annotations: ToolAnnotations | None = None):
    """Register a tool only when its capability group is enabled at startup.

    A tool preset narrows the core group only; other enabled groups register in full.
    Only the MCP registration marks failures as errors; Python callers get the plain result.
    """
    if group not in CAPABILITY_GROUPS:
        raise ValueError(f"Unknown tool capability group: {group}")

    def decorate(function):
        if group == "core" and TOOL_PRESET != "full" and function.__name__ not in TOOL_PRESETS[TOOL_PRESET]:
            return function
        if "all" in ENABLED_CAPS or group in ENABLED_CAPS:
            mcp.tool(annotations=annotations)(_mcp_errors(function))
        return function

    return decorate


BRIDGE_START_TIMEOUT = 15
# A listening bridge answers /api/status at once. Probes during a start wait no longer than
# READY_PROBE_TIMEOUT; a started bridge whose port accepts connections but has not answered
# for NOT_RESPONDING_SECONDS hangs (a bridge that cannot listen exits within that time).
READY_PROBE_TIMEOUT = 1
NOT_RESPONDING_SECONDS = 3


def _bridge_matches(status: dict) -> bool:
    return status.get("build") == BRIDGE_BUILD or os.environ.get("KITSUNE_NO_BRIDGE_RESTART") == "1"


def _ensure_bridge() -> dict | None:
    """Start or replace the shared bridge, serializing lifecycle changes; return an error result."""
    try:
        status = bridge_control.status()
        if status is not None and _bridge_matches(status):
            return None
        with bridge_control.lifecycle_lock():
            # Another MCP server may already have replaced it while we waited.
            status = bridge_control.status()
            if status is not None:
                if _bridge_matches(status):
                    return None
                error = bridge_control.stop()
                if error:
                    return _bridge_unavailable(error)
            with open(LOG_FILE, "ab") as log:
                log_start = log.tell()
                process = subprocess.Popen(
                    [sys.executable, "-m", "kitsune.bridge"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,  # outlive this MCP server
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            deadline = time.monotonic() + BRIDGE_START_TIMEOUT
            unanswered_since = None
            while time.monotonic() < deadline:
                try:
                    status = bridge_control.status(timeout=READY_PROBE_TIMEOUT)
                    unanswered_since = None
                except httpx.TimeoutException:
                    # The port accepts connections but nothing answers. If it is another program,
                    # the new bridge cannot listen and exits; if it stays alive, it hangs.
                    status = None
                    if unanswered_since is None:
                        unanswered_since = time.monotonic()
                    if process.poll() is None and time.monotonic() - unanswered_since >= NOT_RESPONDING_SECONDS:
                        return _not_responding(process.pid)
                if status is not None and status.get("build") == BRIDGE_BUILD:
                    return None
                if process.poll() is not None:
                    break  # it exited at startup; diagnose now instead of at the deadline
                time.sleep(0.3)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return _token_mismatch()
        return _port_conflict(HTTP_PORT, "not_kitsune", f"Cannot start Kitsune bridge: {exc}")
    except ValueError as exc:  # a reply that is not a Kitsune status
        return _port_conflict(HTTP_PORT, "not_kitsune", f"Cannot start Kitsune bridge: {exc}")
    except httpx.TimeoutException:
        # Before a start: a running bridge that hangs, or another program on the port.
        pid = bridge_control.daemon_pid()
        if pid is not None:
            return _not_responding(pid)
        return _port_conflict(HTTP_PORT, "in_use", f"Port {HTTP_PORT} accepts connections but does not answer, "
                                                   "and no Kitsune bridge runs")
    except (httpx.HTTPError, OSError) as exc:
        return _bridge_unavailable(f"Cannot start Kitsune bridge: {exc}. See {LOG_FILE}")
    exited = {}
    if process.poll() is not None:
        exited = {"exitCode": process.returncode}
        excerpt = _startup_log_excerpt(log_start)
        if excerpt:
            exited["logExcerpt"] = excerpt
    elif unanswered_since is not None:
        return _not_responding(process.pid)
    # Nothing answers: the new bridge may have exited because it could not listen.
    for port in (HTTP_PORT, WS_PORT) if status is None else ():
        if bridge_control.port_in_use(port):
            error = _port_conflict(port, "in_use", f"Kitsune bridge did not start: port {port} is in use")
            error["state"].update(exited)
            return error
    if exited:
        reason = f": {exited['logExcerpt']}" if "logExcerpt" in exited else ""
        return _bridge_unavailable(
            f"Kitsune bridge exited at startup with code {exited['exitCode']}{reason}. See {LOG_FILE}", **exited)
    if status is None:
        # Still running, but it never listened: a slow start cannot be told apart before the deadline.
        return _bridge_unavailable(
            f"Kitsune bridge (PID {process.pid}) runs but did not listen on port {HTTP_PORT} within "
            f"{BRIDGE_START_TIMEOUT}s. See {LOG_FILE}", port=HTTP_PORT, reason="not_listening", pid=process.pid)
    return _bridge_unavailable(f"Kitsune bridge did not start within {BRIDGE_START_TIMEOUT}s. See {LOG_FILE}")


STARTUP_EXCERPT_CHARS = 300
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+")


def _startup_log_excerpt(offset: int) -> str:
    """The last line a bridge that exited at startup wrote to LOG_FILE after offset, without secrets.

    For a Python traceback that is the exception, such as a port that is already in use.
    """
    try:
        with open(LOG_FILE, "rb") as log:
            log.seek(max(offset, log.seek(0, os.SEEK_END) - 8192))
            text = log.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    excerpt = lines[-1]
    token = _headers().get("Authorization", "").removeprefix("Bearer ")
    for secret in {token, os.environ.get("KITSUNE_TOKEN")}:
        if secret:
            excerpt = excerpt.replace(secret, "[redacted]")
    return _SECRET_PATTERN.sub(r"\1[redacted]", excerpt)[:STARTUP_EXCERPT_CHARS]


# Connection failures share the action error contract (docs/api-reference.md#connection-diagnosis).
# Nothing reached the browser, so repeating the request is safe. Never include the token.
def _bridge_unavailable(message: str, **state) -> dict:
    error = {"error": message, "code": "bridge_unavailable", "retryable": True,
             "hint": "Run `kitsune-bridge start` (`uv run kitsune-bridge start` in a checkout) in a terminal "
                     f"to see why the bridge does not start; the MCP server starts it on demand. See {LOG_FILE}"}
    if state:
        error["state"] = state
    return error


def _not_responding(pid: int) -> dict:
    error = _bridge_unavailable(f"Kitsune bridge (PID {pid}) accepts connections on port {HTTP_PORT} but does not "
                                f"answer. See {LOG_FILE}", port=HTTP_PORT, reason="not_responding", pid=pid)
    error["hint"] = ("The bridge hangs: run `kitsune-bridge stop`, then retry. If it hangs again, run "
                     f"`kitsune-bridge start` in a terminal and see {LOG_FILE}. If another program holds port "
                     f"{HTTP_PORT}, find it with `lsof -nP -iTCP:{HTTP_PORT} -sTCP:LISTEN`.")
    return error


def _token_mismatch() -> dict:
    source = "KITSUNE_TOKEN" if os.environ.get("KITSUNE_TOKEN") else TOKEN_FILE
    return {"error": f"The Kitsune bridge on port {HTTP_PORT} rejected this MCP server's token",
            "code": "token_mismatch", "retryable": True,
            "hint": "Set the same KITSUNE_TOKEN and KITSUNE_HOME (or neither) for every MCP client, or run "
                    "`kitsune-bridge stop` so the next tool call starts a bridge with this server's token.",
            "state": {"port": HTTP_PORT, "tokenSource": source}}


def _port_conflict(port: int, reason: str, message: str) -> dict:
    setting = "KITSUNE_HTTP_PORT" if port == HTTP_PORT else "KITSUNE_WS_PORT (and the extension's WS_URL)"
    owner = "answers but is not a Kitsune bridge" if reason == "not_kitsune" else "is used by another program"
    return {"error": message, "code": "port_conflict", "retryable": True,
            "hint": f"Port {port} {owner}. Find it with `lsof -nP -iTCP:{port} -sTCP:LISTEN` and stop it, "
                    f"or set {setting} to a free port for every client.",
            "state": {"port": port, "reason": reason}}


def _request(method: str, endpoint: str, **kwargs) -> dict:
    for attempt in range(2):
        try:
            r = httpx.request(method, f"{BRIDGE}{endpoint}", headers=_headers(), **kwargs)
            if r.status_code == 401:
                return _token_mismatch()
            try:
                return r.json()
            except ValueError:
                # Every bridge route the server calls answers JSON on 2xx, so a 2xx page is another program.
                if 200 <= r.status_code < 300:
                    return _port_conflict(HTTP_PORT, "not_kitsune",
                                          f"Port {HTTP_PORT} did not answer as a Kitsune bridge")
                # Keep what the HTTP layer said instead of a JSON decoding message.
                return {"error": f"Kitsune bridge returned HTTP {r.status_code} without a JSON body",
                        "state": {"httpStatus": r.status_code, "body": r.text[:200]}}
        except httpx.ConnectError:
            if attempt:
                break
            error = _ensure_bridge()
            if error:
                return error
        except httpx.TimeoutException as e:
            # The bridge may still run the command; only GET requests are known to be reads.
            return {"error": str(e) or "Kitsune bridge request timed out", "code": "timeout",
                    "retryable": method == "GET",
                    "hint": "The command's effect is unknown; check the page with browser_snapshot "
                            "before repeating it."}
        except Exception as e:
            return {"error": str(e)}
    return _bridge_unavailable(f"Kitsune bridge is not reachable. See {LOG_FILE}")


def _get(endpoint: str) -> dict:
    """GET request to bridge."""
    return _request("GET", endpoint, timeout=15)


def _post(endpoint: str, data: dict, timeout: float = 15) -> dict:
    """POST request to bridge."""
    return _request("POST", endpoint, json=data, timeout=timeout)


def _with_tab(body: dict, tab_id: int | None) -> dict:
    """Add tabId to a body dict when targeting a specific tab."""
    if tab_id is not None:
        body["tabId"] = tab_id
    return body


def _confirm(body: dict, confirm_token: str | None) -> dict:
    """Attach a token from a confirmation_required error (ask-before-act policy)."""
    if confirm_token is not None:
        body["confirmToken"] = confirm_token
    return body


OUTCOME_TIMEOUT_MS = 5000  # default expect.timeout; matches bridge.OUTCOME_TIMEOUT_MS


def _post_action(endpoint: str, body: dict, wait: bool = False, timeout: int = 10000,
                 expect: dict | None = None) -> dict:
    """POST a page action.

    wait=True lets the page wait up to timeout ms for the target before dispatch;
    expect makes the bridge check the outcome after dispatch, within expect.timeout.
    """
    request_timeout = 15
    if wait:
        if timeout <= 0:
            return {"error": "timeout must be positive"}
        body = {**body, "wait": True, "timeout": timeout}
        request_timeout += timeout / 1000
    if expect is not None:
        wait_ms = expect.get("timeout", OUTCOME_TIMEOUT_MS) if isinstance(expect, dict) else 0
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, (int, float)) or not math.isfinite(wait_ms):
            wait_ms = 0  # the bridge rejects it before dispatch
        body = {**body, "expect": expect}
        request_timeout += max(0, wait_ms) / 1000 + 45
    if request_timeout == 15:
        return _post(endpoint, body)
    return _post(endpoint, body, timeout=request_timeout)


def _target(body: dict, ref: str | None, element: str | None) -> dict:
    """Attach a machine ref and an audit-only human description."""
    if ref is not None:
        body["ref"] = ref
    if element is not None:
        body["element"] = element
    return body


# The action contract lives in these parameter descriptions: every client sends input
# schemas in full, while some cut tool descriptions to their first paragraph (#135).
# browser_click's expect carries the full grammar; the other action tools point to it.
TabId = Annotated[int | None, Field(description="Default: active tab.")]
Ref = Annotated[str | None, Field(description="Snapshot ref; preferred over selector.")]
Element = Annotated[str | None, Field(description="Audit-only label.")]
Wait = Annotated[bool, Field(description="Wait up to timeout ms for the target first.")]
Expect = Annotated[dict | None, Field(
    description="Outcome to verify; grammar in browser_click. If unmet, ok=false but the action ran.")]
ClickExpect = Annotated[dict | None, Field(
    description='Outcome to verify after dispatch, exactly one of {"url": regex}, {"url_changed": true}, '
                '{"new_tab": true}, {"selector": css} (visible; add "gone": true, "value": str or '
                '"value_changed": true), {"text": str} or {"text_gone": str}, plus "timeout" ms (default '
                '5000). Unmet: ok=false, code="outcome_not_observed"; the action already ran, so do not '
                'blindly repeat it.')]
ConfirmToken = Annotated[str | None, Field(description="From confirmation_required, once the user approves.")]


# ════════════════════════════════════════════════════════════════════
#  Parallel-work primer (read this if you're driving multiple tabs)
# ════════════════════════════════════════════════════════════════════
#
# Every tab-aware tool below accepts an optional `tab_id`. If omitted, it
# targets the active tab. Pass `tab_id` to drive a specific tab without
# stealing focus — that's the foundation for parallel multi-tab work.
#
# Recipe for reliable parallel agentic work:
#
#   1. `browser_tabs(action="new", ...)` for each tab you'll drive; collect the IDs.
#   2. `browser_keep_alive([id1, id2, ...])` — prevents Zen's Tab Unloader
#      from discarding inactive tabs mid-batch. Pings each tab every
#      60s (default) which resets the unloader's idle timer.
#   3. Drive each tab by passing `tab_id=` to clicks, types, JS, etc.
#      Use `browser_parallel([[...tab1 steps...], [...tab2 steps...]])` to
#      fan out concurrently.
#   4. If you suspect a tab was unloaded (long pauses between commands),
#      `browser_wake_tab(tab_id)` revives it.
#   5. `browser_keep_alive_stop()` when done.
#
# ════════════════════════════════════════════════════════════════════


# -- Read Operations ----------------------------------------------

@tool("core", annotations=ToolAnnotations(title="Check browser status", **READ))
def browser_status() -> dict:
    """Check bridge/extension connection; diagnosis explains failures."""
    result = _get("/api/status")
    if result.get("code") and "diagnosis" not in result:  # the bridge was not reached
        result["diagnosis"] = {key: result[key] for key in ("code", "hint", "state") if key in result}
    return result


@tool("core", annotations=ToolAnnotations(title="Manage browser tabs", **DESTRUCTIVE))
def browser_tabs(
    action: Literal["list", "new", "close", "select"] = "list",
    tab_id: Annotated[int | None, Field(description="close/select: the tab (required).")] = None,
    url: Annotated[str | None, Field(description="new: URL to open (default about:blank).")] = None,
    force: Annotated[bool, Field(description="close: disarm beforeunload.")] = False,
    raw: Annotated[bool, Field(description="new: bypass the image viewer.")] = False,
    wait: Annotated[bool, Field(
        description="new: wait up to timeout ms for the page to load and return tabId, the actual url "
                    "(after redirects) and status. On timeout the tab stays open and the result has "
                    "ok=false, timedOut=true.")] = False,
    timeout: int = 15000,
    confirm_token: Annotated[str | None, Field(
        description="new only: from confirmation_required, once the user approves.")] = None,
) -> dict:
    """List, open, close, or select tabs; new returns tabId at once unless wait=True.

    list is always a fresh read of the browser's tabs. Options for other actions are ignored.
    """
    if action == "list":
        return _get("/api/tabs")
    if action == "new":
        body = {"url": url if url is not None else "about:blank"}
        if raw:
            body["raw"] = True
        _confirm(body, confirm_token)
        if not wait:
            return _post("/api/new-tab", body)
        if timeout <= 0:
            return {"error": "timeout must be positive"}
        return _post("/api/new-tab", {**body, "wait": True, "timeout": timeout},
                     timeout=timeout / 1000 + 10)
    if action in {"close", "select"}:
        if tab_id is None:
            return {"error": f"tab_id is required for action={action}"}
        body = {"tabId": tab_id}
        if action == "close" and force:
            body["force"] = True
        return _post("/api/close-tab" if action == "close" else "/api/switch-tab", body)
    return {"error": "action must be list, new, close, or select"}


@tool("core", annotations=ToolAnnotations(title="Page info", **READ))
def browser_page_info(tab_id: TabId = None) -> dict:
    """Get current page URL, title, dimensions, scroll position, and recent dialogs."""
    if tab_id is None:
        return _get("/api/page-info")
    return _post("/api/page-info", {"tabId": tab_id})


@tool("core", annotations=ToolAnnotations(title="Page text", **READ))
def browser_page_text(
    tab_id: Annotated[int | None, Field(description="Tab to read without switching focus (default: the "
                                                    "active tab).")] = None,
) -> dict:
    """Extract all readable text from the current page.

    An HTTP error response the browser shows without a readable document returns
    text="" with empty=true and its httpStatus.
    """
    if tab_id is None:
        return _get("/api/page-text")
    return _post("/api/page-text", {"tabId": tab_id})


@tool("core", annotations=ToolAnnotations(title="Read page forms", **READ))
def browser_forms(tab_id: TabId = None) -> dict:
    """Get all form fields on the current page with their labels and values; sensitive
    values are redacted.

    Password and other sensitive values are redacted (`redacted`/`hasValue` replace `value`).
    """
    if tab_id is None:
        return _get("/api/forms")
    return _post("/api/forms", {"tabId": tab_id})


@tool("core", annotations=ToolAnnotations(title="Read page DOM", **READ))
def browser_dom(tab_id: TabId = None) -> dict:
    """Get the accessibility tree of interactive elements on the page."""
    if tab_id is None:
        return _get("/api/dom")
    return _post("/api/dom", {"tabId": tab_id})


@tool("core", annotations=ToolAnnotations(title="Accessibility snapshot", **READ))
def browser_snapshot(
    tab_id: TabId = None,
    selector: Annotated[str | None, Field(description="CSS subtree root (included).")] = None,
    ref: Annotated[str | None, Field(
        description="Existing ref for a subtree root, resolved before refs are replaced; wins over "
                    "selector.")] = None,
    max_depth: Annotated[int | None, Field(
        description="Non-negative depth below the subtree root (body by default); 0 is the root only. "
                    "Omit for all depths.")] = None,
    text: Annotated[bool, Field(
        description="Also return visible text lines (no refs), url, title and tabId, capped at 12,000 "
                    "chars with a truncation marker.")] = False,
) -> dict:
    """Get a compact accessibility-style text tree with fresh element refs; prefer them for
    actions. Pass text=True to also get the page's visible text in the same call.

    Refs belong to one document in one tab. A snapshot, browser_dom, or the post-action
    snapshot of a successful click, type/fill, select_option, check, keypress, submit_form
    or navigate replaces that tab's earlier refs; navigation invalidates them. browser_find,
    browser_forms and wait_for(selector) add refs. After an action, use its snapshot refs
    and target_ref (the acted-on element). On stale_ref, take a new snapshot; never repeat
    a write just to refresh refs. Repeated composite siblings show the first five items;
    post-action snapshots also keep the target.
    """
    if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
        return {"error": "max_depth must be a non-negative integer"}
    body = _with_tab({}, tab_id)
    if text:
        body["text"] = True
    if selector is not None:
        body["selector"] = selector
    if ref is not None:
        body["ref"] = ref
    if max_depth is not None:
        body["maxDepth"] = max_depth
    if not body:
        return _get("/api/snapshot")
    return _post("/api/snapshot", body)


@tool("core", annotations=ToolAnnotations(title="Take screenshot", **ACT))
def browser_screenshot(
    selector: Annotated[str | None, Field(description="CSS selector of one element to capture.")] = None,
    full_page: Annotated[bool, Field(description="Capture the full page; not with selector/ref.")] = False,
    tab_id: TabId = None,
    save_to: Annotated[str | None, Field(
        description="Write the full PNG to this path and return metadata instead.")] = None,
    data_url: Annotated[bool, Field(description="Also return the PNG as a (large) data URL.")] = False,
    ref: Ref = None, element: Element = None,
):
    """Capture the viewport, one element (ref/selector) or the full page as an inline image.

    Element/full-page captures briefly activate the tab; viewport capture with tab_id
    selects it and focuses its window. Inline images are JPEG-encoded or downscaled
    under ~1 MB.
    """
    if ref is not None and selector is None:
        selector = ""
    if selector is not None and full_page:
        return {"error": "selector and full_page are mutually exclusive"}
    preview = not (save_to or data_url)
    if selector is not None or full_page:
        endpoint = "/api/element-screenshot" if selector is not None else "/api/full-page-screenshot"
        body = _target({"selector": selector}, ref, element) if selector is not None else {}
        body["imagePreview"] = preview
        result = _post(endpoint, _with_tab(body, tab_id), timeout=120 if full_page else 45)
        return _screenshot_result(result, save_to, data_url)
    if tab_id is not None:
        tabs = _get("/api/tabs")
        if "error" in tabs:
            return tabs
        tab = next((t for t in tabs.get("tabs", []) if t["id"] == tab_id), None)
        if tab is None:
            return {"error": f"Tab {tab_id} not found"}
        for endpoint, body in [
            ("/api/switch-tab", {"tabId": tab_id}),
            ("/api/focus-window", {"windowId": tab["windowId"]}),
        ]:
            result = _post(endpoint, body)
            if "error" in result:
                return result
    result = _post("/api/screenshot", {"imagePreview": preview})
    return _screenshot_result(result, save_to, data_url)


# -- Navigation ---------------------------------------------------

@tool("core", annotations=ToolAnnotations(title="Navigate", **ACT))
def browser_navigate(
    url: Annotated[str, Field(description='The URL to navigate to (e.g. "https://example.com").')],
    tab_id: TabId = None,
    raw: Annotated[bool, Field(
        description="Open a bare image URL (.jpg/.png/...) raw instead of inside the small local viewer "
                    "page (image = #zlimg) the extension can script.")] = False,
    expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Navigate a tab to a URL.
    ok=true means dispatched; verify with expect; tab_id targets a tab."""
    body = {"url": url}
    if raw:
        body["raw"] = True
    return _post_action("/api/navigate", _confirm(_with_tab(body, tab_id), confirm_token), expect=expect)


# -- Interaction --------------------------------------------------

@tool("core", annotations=ToolAnnotations(title="Click", **ACT))
def browser_click(
    selector: Annotated[str, Field(description='CSS selector to click (e.g. "#submit", ".btn-primary").')] = "",
    x: Annotated[int, Field(description="X coordinate (used without selector/ref).")] = 0,
    y: Annotated[int, Field(description="Y coordinate (used without selector/ref).")] = 0,
    tab_id: TabId = None, ref: Ref = None, element: Element = None, wait: Wait = False, timeout: int = 10000,
    expect: ClickExpect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Click an element by ref, CSS selector or coordinates. ok=true only means dispatched:
    pass expect to verify the effect, and tab_id to target a tab; tabs the click opens
    come back in opened_tabs.

    Action contract, shared by the action tools; their parameters carry the details.
    The result of a successful action listed in browser_snapshot includes a fresh snapshot
    whose refs replace earlier ones; target_ref is the acted-on element's new ref.
    opened_tabs lists tabs the action opened (target=_blank links, window.open) as
    [{tabId, url}]; act on them with tab_id. via="link_fallback" marks a popup-blocked
    link Kitsune opened itself. With expect, the result adds dispatched=true and outcome
    {expected, observed, met}; new_tab is met once the action opens a tab, and
    value_changed compares with the value before dispatch. wait: click, double_click,
    hover and drag also need the target rendered; on timeout the result is not_found or
    not_actionable and nothing ran.
    """
    body: dict = {"selector": selector} if selector or ref else {"coords": {"x": x, "y": y}}
    return _post_action("/api/click", _confirm(_with_tab(_target(body, ref, element), tab_id), confirm_token), wait, timeout, expect)


@tool("devtools", annotations=ToolAnnotations(title="Trusted click", **ACT))
def browser_trusted_click(
    selector: Annotated[str, Field(description="CSS selector to click. If provided, Kitsune clicks its center.")] = "",
    x: Annotated[int, Field(description="X coordinate, used if selector is empty.")] = 0,
    y: Annotated[int, Field(description="Y coordinate, used if selector is empty.")] = 0,
    coordinate_space: Annotated[str, Field(
        description='"viewport" for browser viewport coords or "screen" for desktop coords.')] = "viewport",
    focus: Annotated[bool, Field(
        description="Whether to try to foreground the browser (Zen, Firefox, LibreWolf) first.")] = False,
    tab_id: Annotated[int | None, Field(
        description="Tab to resolve the selector against (viewport coords only).")] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Click using the real OS mouse pointer for pages that reject synthetic DOM events.

    This is slower and moves the user's cursor, so prefer browser_click unless a page
    specifically ignores synthetic clicks. Works on Windows and macOS; macOS needs
    pyobjc Quartz or cliclick plus Accessibility permission for the bridge.
    """
    if selector:
        body = {"selector": selector, "focus": focus}
    else:
        body = {"coords": {"x": x, "y": y}, "coordinateSpace": coordinate_space, "focus": focus}
    return _post("/api/trusted-click", _confirm(_with_tab(body, tab_id), confirm_token))


@tool("core", annotations=ToolAnnotations(title="Type into element", **ACT))
def browser_type(
    selector: str = "", text: str = "",
    mode: Annotated[Literal["type", "fill", "editable"], Field(
        description="type: typing events, clearing first if clear. fill: replace the value via native "
                    "setters and input/change events (always replaces). editable: insert plain text into "
                    "an editor/input/textarea, appending unless clear. For HTML, use browser_js.")] = "type",
    clear: bool = False, tab_id: TabId = None,
    ref: Ref = None, element: Element = None, wait: Wait = False, timeout: int = 10000,
    expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Enter text into a field or contenteditable editor (mode: type, fill or editable).
    ok=true means dispatched; verify with expect; tab_id targets a tab.
    """
    if mode == "type":
        endpoint, body = "/api/type", {"selector": selector, "text": text, "clear": clear}
    elif mode == "fill":
        endpoint, body = "/api/fill", {"selector": selector, "value": text}
    elif mode == "editable":
        endpoint, body = "/api/set-editable-content", {
            "selector": selector, "value": text, "format": "text", "clear": clear,
        }
    else:
        return {"error": "mode must be type, fill, or editable"}
    return _post_action(endpoint, _confirm(_with_tab(_target(body, ref, element), tab_id), confirm_token), wait, timeout, expect)


@tool("core", annotations=ToolAnnotations(title="Scroll", **ACT))
def browser_scroll(
    direction: Annotated[str, Field(description="up, down, left, right, top or bottom.")] = "down",
    amount: Annotated[int, Field(description="Pixels (ignored for top/bottom).")] = 500,
    tab_id: TabId = None,
    selector: str | None = None, ref: Ref = None, element: Element = None,
    expect: Expect = None,
) -> dict:
    """Scroll the selector/ref container instantly, else the window (falling back to a
    scrollable container at viewport center). ok means handled, not moved: check
    scrolled_by (signed pixels actually moved; 0 is a no-op), pass expect to verify the
    outcome, and tab_id to target a tab.

    Also returns target ("window" or the container's selector) and window
    scrollY/scrollX. scrolledPx is an alias of scrolled_by.
    """
    body = {"direction": direction, "amount": amount}
    if selector: body["selector"] = selector
    return _post_action("/api/scroll", _with_tab(_target(body, ref, element), tab_id), expect=expect)


@tool("core", annotations=ToolAnnotations(title="Hover", **ACT))
def browser_hover(
    selector: Annotated[str, Field(description="CSS selector of the element to hover over.")] = "",
    tab_id: TabId = None, ref: Ref = None, element: Element = None,
    wait: Wait = False, timeout: int = 10000, expect: Expect = None,
) -> dict:
    """Hover over an element on the page.
    ok=true means dispatched; verify with expect; tab_id targets a tab.
    """
    return _post_action("/api/hover", _with_tab(_target({"selector": selector}, ref, element), tab_id),
                        wait, timeout, expect)


# -- Smart Queries ------------------------------------------------

@tool("core", annotations=ToolAnnotations(title="Find page elements", **READ))
def browser_find(
    query: Annotated[str, Field(description='What to find (e.g. "login button", "search bar", "email input").')],
    tab_id: TabId = None,
) -> dict:
    """Find elements on the page using natural language description."""
    return _post("/api/find", _with_tab({"query": query}, tab_id))


@tool("core", annotations=ToolAnnotations(title="Execute JavaScript", **DESTRUCTIVE))
def browser_js(
    code: Annotated[str, Field(
        description='JavaScript code to run (e.g. "document.title" or '
                    '"document.querySelectorAll(\'a\').length").')],
    tab_id: TabId = None,
    full: Annotated[bool, Field(
        description="Results are capped at 8 MB (50 KB on extensions older than 2.1.0). True runs the "
                    "code once and reads the result back in slices, so any size comes back complete.")] = False,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Execute JavaScript in the current page context (developer extension build only).

    The signed build returns an error (browser_status reports extension_build).
    """
    body = _confirm({"code": code}, confirm_token)
    if full:
        body["full"] = True
        return _post("/api/js", _with_tab(body, tab_id), timeout=180)
    return _post("/api/js", _with_tab(body, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Highlight", **ACT))
def browser_highlight(
    selector: Annotated[str, Field(description="CSS selector of the element to highlight.")] = "",
    tab_id: TabId = None, ref: Ref = None, element: Element = None,
) -> dict:
    """Highlight an element on the page with a visual overlay."""
    return _post("/api/highlight", _with_tab(_target({"selector": selector}, ref, element), tab_id))


# -- Wait ---------------------------------------------------------

@tool("core", annotations=ToolAnnotations(title="Handle page dialogs", **IDEMPOTENT_ACT))
def browser_handle_dialog(
    accept: Annotated[bool | None, Field(
        description="True accepts and False dismisses future dialogs; null clears this tab's policy "
                    "and restores native dialogs. True also disarms beforeunload like browser_tabs "
                    "close with force; False leaves native beforeunload behavior intact.")] = True,
    prompt_text: Annotated[str | None, Field(
        description="Answer for accepted prompts (default: the prompt's default value).")] = None,
    tab_id: TabId = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Set this tab's policy for future dialogs and return recent recorded dialogs; by
    default dialogs use native UI and record the user's actual response.

    Dialogs answer synchronously; this cannot change an already returned answer.
    With a policy set, dialogs auto-answer. History contains the last 100 dialogs in the current document. Policies survive
    navigation, with native behavior until the policy is restored asynchronously.
    Native dialogs and scripts holding original functions cannot be answered.
    """
    return _post("/api/handle-dialog", _confirm(_with_tab(
        {"accept": accept, "prompt_text": prompt_text}, tab_id), confirm_token))


@tool("core", annotations=ToolAnnotations(title="Wait for condition", **DESTRUCTIVE))
def browser_wait_for(
    selector: Annotated[str | None, Field(description="Wait for a visible element.")] = None,
    text: Annotated[str | None, Field(description="Wait for visible page text (literal substring).")] = None,
    text_gone: Annotated[str | None, Field(
        description="Wait until visible page text (literal substring) is gone.")] = None,
    js: Annotated[str | None, Field(
        description="Wait for a non-empty expression result (including false/0, matching the bridge "
                    "semantics; developer extension build only).")] = None,
    url: Annotated[str | None, Field(
        description="JavaScript regex for the tab URL, checked via tab metadata, so it survives "
                    "document navigation.")] = None,
    title: Annotated[str | None, Field(
        description="JavaScript regex for the tab title, checked via tab metadata, so it survives "
                    "document navigation.")] = None,
    network_idle: Annotated[bool, Field(description="Wait for 500 ms without new resources.")] = False,
    time: Annotated[float | None, Field(description="Sleep for this many seconds.")] = None,
    timeout: Annotated[int, Field(
        description="Maximum wait in milliseconds; it does not limit time sleep.")] = 10000,
    tab_id: TabId = None,
    ref: Annotated[str | None, Field(
        description="Wait for the visible element with this snapshot ref; wins over selector.")] = None,
    element: Element = None,
    confirm_token: Annotated[str | None, Field(
        description="js waits: from confirmation_required, once the user approves.")] = None,
) -> dict:
    """Wait for exactly one condition (selector/ref, text, text_gone, js, url, title,
    network_idle), or sleep for time seconds.

    Conditions other than url/title run in the current document and should be started
    after navigation. Mixed conditions are rejected; wait for URL/title first, then
    the desired document condition.
    """
    element_condition = selector is not None or ref is not None
    conditions = [text, text_gone, js, url, title, time]
    if element_condition + sum(value is not None for value in conditions) + bool(network_idle) != 1:
        return {"error": "Provide exactly one of selector/ref, text, text_gone, js, url, title, network_idle, time"}
    if timeout <= 0:
        return {"error": "timeout must be positive"}
    if time is not None:
        if not math.isfinite(time) or time < 0:
            return {"error": "time must be a finite non-negative number of seconds"}
        _sleep(time)
        return {"ok": True, "slept": time}
    body = {"timeout": timeout}
    if element_condition:
        endpoint = "/api/wait-for-element"
        body.update(selector=selector, pollInterval=200)
        _target(body, ref, element)
    elif text is not None or text_gone is not None:
        if (text if text is not None else text_gone) == "":
            return {"error": "text/text_gone must be non-empty"}
        endpoint = "/api/wait-for-text"
        body.update(text=text, text_gone=text_gone)
    elif js is not None:
        endpoint = "/api/wait-for-result"
        body.update(code=js, pollInterval=500)
        _confirm(body, confirm_token)
    elif url is not None or title is not None:
        endpoint = "/api/wait-for-url" if url is not None else "/api/wait-for-title"
        body["pattern"] = url if url is not None else title
    else:
        endpoint = "/api/wait-for-network-idle"
        body["idleMs"] = 500
    return _post(endpoint, _with_tab(body, tab_id), timeout=timeout / 1000 + 10)


@tool("core", annotations=ToolAnnotations(title="List workflows", **READ))
def browser_workflows() -> dict:
    """List available named Kitsune workflows from the bridge workflow directory."""
    return _get("/api/workflows")


@tool("core", annotations=ToolAnnotations(title="Run a named workflow", **DESTRUCTIVE))
def browser_workflow(
    name: Annotated[str, Field(description="Workflow file name without .md.")],
    variables: Annotated[dict | None, Field(description="Template variables for the workflow.")] = None,
) -> dict:
    """Execute a named Kitsune workflow from the bridge workflow directory."""
    return _post("/api/workflow", {"name": name, "variables": variables or {}},
                 timeout=container_request_seconds(WORKFLOW_DEADLINE_SECONDS))


@tool("devtools", annotations=ToolAnnotations(title="Manage response cache", **DESTRUCTIVE))
def browser_cache(
    action: Annotated[str, Field(description='"clear", "status", or "ttl".')] = "clear",
    seconds: Annotated[int | None, Field(description='TTL value when action is "ttl".')] = None,
) -> dict:
    """Control the Kitsune bridge response cache."""
    body: dict = {"action": action}
    if seconds is not None:
        body["seconds"] = seconds
    return _post("/api/cache", body)


# -- Parallel Multi-Tab Work --------------------------------------

@tool("tabs", annotations=ToolAnnotations(title="Wake tab", **IDEMPOTENT_ACT))
def browser_wake_tab(
    tab_id: Annotated[int, Field(description="The tab ID to wake (get IDs from browser_tabs).")],
) -> dict:
    """Revive a tab that Zen's Tab Unloader may have discarded.

    Zen aggressively unloads inactive tabs to save RAM. An unloaded tab still
    appears in browser_tabs but has no live content script — commands targeting it
    will hang or fail until it's reloaded. browser_wake_tab is idempotent: if the
    tab is already alive it returns immediately; otherwise it reloads the tab
    and waits for the page to finish loading.

    Use before targeting a tab that might have been idle for a while. Prefer
    browser_keep_alive for tabs you'll touch repeatedly.
    """
    return _post("/api/wake-tab", {"tabId": tab_id}, timeout=25)


@tool("parallel", annotations=ToolAnnotations(title="Keep alive", **IDEMPOTENT_ACT))
def browser_keep_alive(
    tab_ids: Annotated[list[int], Field(description="Tab IDs to keep loaded.")],
    interval_seconds: Annotated[int, Field(
        description="How often to ping each tab. Minimum 10s. Should be well under Zen's unloader "
                    "timeout (commonly 20+ minutes).")] = 60,
) -> dict:
    """Keep tabs warm so Zen's Tab Unloader won't discard them mid-workflow.

    Starts a background pinger in the bridge that fires a no-op JS evaluation
    at each listed tab on the given interval. Any script access resets Zen's
    idle timer, so the tabs stay loaded. Replaces any existing keep-alive for
    the same tabs (use a new interval to change cadence).

    Use this at the start of parallel multi-tab work: open your tabs, call
    browser_keep_alive with their IDs, drive them in parallel via browser_parallel or
    by passing tab_id to individual commands, then browser_keep_alive_stop when
    done. The pinger auto-stops if the bridge restarts.
    """
    return _post(
        "/api/keep-alive",
        {"tabIds": tab_ids, "intervalSeconds": interval_seconds},
    )


@tool("parallel", annotations=ToolAnnotations(title="Keep alive stop", **IDEMPOTENT_ACT))
def browser_keep_alive_stop(
    tab_ids: Annotated[list[int] | None, Field(description="Tabs to stop pinging. Omit to stop all.")] = None,
) -> dict:
    """Stop the keep-alive pinger for some or all tabs."""
    body: dict = {}
    if tab_ids is not None:
        body["tabIds"] = tab_ids
    return _post("/api/keep-alive-stop", body)


@tool("devtools", annotations=ToolAnnotations(title="Reload extension", **ACT))
def browser_reload_extension() -> dict:
    """Hot-reload the Kitsune extension in the browser.

    Use this after editing the extension source to pick up changes without
    going through `about:addons` and toggling the extension by hand. The
    extension acks the request, then reloads itself ~100ms later; the
    background script's existing reconnect logic re-establishes the bridge
    WebSocket immediately. Content scripts in open tabs survive — they re-
    inject lazily on the next command (see forwardToContent in background.js).

    Note: only works once you're already on a version of the extension that
    has this action handler. The very first install of that version requires
    the usual about:addons toggle.
    """
    return _post("/api/reload-extension", {}, timeout=10)


@tool("parallel", annotations=ToolAnnotations(title="Run parallel commands", **DESTRUCTIVE))
def browser_parallel(
    sequences: Annotated[list[list[dict]], Field(
        description='Command sequences, typically one per tab. Each command is a dict with "action" and '
                    'parameters, the same shape as browser_batch commands, e.g. '
                    '[[{"action": "navigate", "url": "...", "tabId": 12}, {"action": "pageText", "tabId": 12}], '
                    '[{"action": "navigate", "url": "...", "tabId": 13}, {"action": "pageText", "tabId": 13}]].')],
) -> dict:
    """Run command sequences in parallel — typically one sequence per tab. Every command
    that acts on a tab MUST include "tabId".

    Each inner list is a sequence that runs serially; all sequences run
    concurrently. Without tabId, or when two sequences write the same tab,
    nothing runs (tab_required, tab_owned).

    Returns a list of result lists, one per input sequence; progress[0].sequences
    gives each command's status.
    """
    command = {"action": "parallel", "sequences": sequences}
    return _post("/api/batch", {"commands": [command]},
                 timeout=container_request_seconds(batch_deadline_seconds([command])))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Content extraction
# ════════════════════════════════════════════════════════════════════

@tool("devtools", annotations=ToolAnnotations(title="Query page elements", **READ))
def browser_query(
    selector: Annotated[str, Field(description="CSS selector.")],
    fields: Annotated[list[str] | None, Field(
        description="Field names to extract per element. Defaults to a useful set.")] = None,
    limit: Annotated[int, Field(description="Max elements to return (capped at 500).")] = 50,
    tab_id: TabId = None,
) -> dict:
    """Extract multiple fields from every element matching a CSS selector.

    Much faster and cheaper than calling browser_js per element. The default
    fields cover the common ones (text, href, value, src, alt, id, name);
    pass `fields=["text","data-foo","bounds","attrs"]` to get arbitrary
    attributes or `bounds`/`attrs`/`html` extras.
    """
    body = {"selector": selector, "limit": limit}
    if fields: body["fields"] = fields
    return _post("/api/query", _with_tab(body, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read element HTML", **READ))
def browser_html(
    selector: str = "", tab_id: TabId = None,
    ref: Ref = None, element: Element = None,
) -> dict:
    """Return the outerHTML of a specific element. Cap 100KB."""
    return _post("/api/html", _with_tab(_target({"selector": selector}, ref, element), tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read page links", **READ))
def browser_links(internal_only: bool = False, tab_id: TabId = None) -> dict:
    """Return every anchor with href + text. Useful as a crawler primitive."""
    return _post("/api/links", _with_tab({"internalOnly": internal_only}, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read page images", **READ))
def browser_images(tab_id: TabId = None) -> dict:
    """Return every <img> with src, alt, and natural dimensions."""
    return _post("/api/images", _with_tab({}, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read page metadata", **READ))
def browser_meta(tab_id: TabId = None) -> dict:
    """Return all meta tags + link rels (canonical, RSS, icons, etc.)."""
    return _post("/api/meta", _with_tab({}, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read structured page data", **READ))
def browser_structured_data(tab_id: TabId = None) -> dict:
    """Extract JSON-LD blocks, OpenGraph (og:*), and Twitter card meta."""
    return _post("/api/structured-data", _with_tab({}, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Read element bounds", **READ))
def browser_bounds(
    selector: str = "", tab_id: TabId = None,
    ref: Ref = None, element: Element = None,
) -> dict:
    """Return getBoundingClientRect + viewport/page sizes for an element."""
    return _post("/api/bounds", _with_tab(_target({"selector": selector}, ref, element), tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Computed style", **READ))
def browser_computed_style(
    selector: str = "", properties: list[str] | None = None, tab_id: TabId = None,
    ref: Ref = None, element: Element = None,
) -> dict:
    """Return computed CSS for an element. Defaults to the layout-relevant set."""
    body = {"selector": selector}
    if properties: body["properties"] = properties
    return _post("/api/computed-style", _with_tab(_target(body, ref, element), tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Extract readable article", **READ))
def browser_readability(tab_id: TabId = None) -> dict:
    """Extract the main article on the page (title, byline, clean text).

    Heuristic: score candidate containers (`article`, `main`, `.post`, etc.)
    by text density × paragraph count × inverse link density, strip nav/aside,
    return the winner. Falls back to densest section/div, then body.
    """
    return _post("/api/readability", _with_tab({}, tab_id))


@tool("core", annotations=ToolAnnotations(title="Read page as Markdown", **READ))
def browser_markdown(
    selector: str | None = None, tab_id: TabId = None,
    ref: Ref = None, element: Element = None,
) -> dict:
    """Convert the page (or a selector/ref subtree) to Markdown. Cap 80KB."""
    body = {}
    if selector: body["selector"] = selector
    return _post("/api/markdown", _with_tab(_target(body, ref, element), tab_id))


@tool("devtools", annotations=ToolAnnotations(title="List page iframes", **READ))
def browser_iframes(tab_id: TabId = None) -> dict:
    """List all iframes with src + same-origin/accessible flags."""
    return _post("/api/iframes", _with_tab({}, tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Explain selector", **READ))
def browser_explain_selector(selector: str, tab_id: TabId = None) -> dict:
    """Diagnose a CSS selector: match count, sample matches, suggestions for
    a more-specific selector if you got too many."""
    return _post("/api/explain-selector", _with_tab({"selector": selector}, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Interaction additions
# ════════════════════════════════════════════════════════════════════

@tool("core", annotations=ToolAnnotations(title="Select option", **ACT))
def browser_select_option(
    selector: str = "", value: str = "", by_text: bool = False, tab_id: TabId = None,
    ref: Ref = None, element: Element = None, wait: Wait = False, timeout: int = 10000,
    expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Choose an <option> in a <select>. Default matches by value; pass
    `by_text=True` to match by visible text.
    ok=true means dispatched; verify with expect; tab_id targets a tab.
    """
    return _post_action("/api/select-option", _confirm(_with_tab(_target({"selector": selector, "value": value, "byText": by_text}, ref, element), tab_id), confirm_token),
                        wait, timeout, expect)


@tool("core", annotations=ToolAnnotations(title="Check", **ACT))
def browser_check(
    selector: str = "", checked: bool = True, tab_id: TabId = None, ref: Ref = None, element: Element = None,
    wait: Wait = False, timeout: int = 10000, expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Set a checkbox or radio's checked state; with wait, hidden native inputs need not render.
    ok=true means dispatched; verify with expect; tab_id targets a tab.
    """
    return _post_action("/api/check", _confirm(_with_tab(_target({"selector": selector, "checked": checked}, ref, element), tab_id), confirm_token),
                        wait, timeout, expect)


@tool("devtools", annotations=ToolAnnotations(title="Focus", **ACT))
def browser_focus(selector: str = "", tab_id: TabId = None, ref: Ref = None, element: Element = None) -> dict:
    """Move focus to the matched element."""
    return _post("/api/focus", _with_tab(_target({"selector": selector}, ref, element), tab_id))


@tool("devtools", annotations=ToolAnnotations(title="Blur", **ACT))
def browser_blur(
    selector: str | None = None, tab_id: TabId = None,
    ref: Ref = None, element: Element = None,
) -> dict:
    """Blur an element (or document.activeElement if no selector/ref)."""
    body = {}
    if selector: body["selector"] = selector
    return _post("/api/blur", _with_tab(_target(body, ref, element), tab_id))


@tool("core", annotations=ToolAnnotations(title="Keypress", **ACT))
def browser_keypress(
    key: Annotated[str, Field(description='e.g. "Enter", "Tab", "Escape", "ArrowDown", "a".')],
    selector: str | None = None,
    modifiers: Annotated[dict | None, Field(description='{"ctrl"|"shift"|"alt"|"meta": true}.')] = None,
    text: Annotated[str | None, Field(description="Also insert this text into an editable target.")] = None,
    tab_id: TabId = None,
    ref: Ref = None,
    element: Element = None,
    wait: Wait = False,
    timeout: int = 10000,
    expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Dispatch a synthetic keyboard event to the ref/selector target, else the focused
    element (or body). ok=true means dispatched; verify with expect; tab_id targets a tab.

    wait applies only with selector/ref.
    """
    body = {"key": key}
    if selector: body["selector"] = selector
    if modifiers: body["modifiers"] = modifiers
    if text is not None: body["text"] = text
    return _post_action("/api/keypress", _confirm(_with_tab(_target(body, ref, element), tab_id), confirm_token), wait, timeout, expect)


@tool("devtools", annotations=ToolAnnotations(title="Double click", **ACT))
def browser_double_click(
    selector: str = "", tab_id: TabId = None, ref: Ref = None, element: Element = None,
    wait: Wait = False, timeout: int = 10000, expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Dispatch a synthetic double-click on the matched element.
    ok=true means dispatched; verify with expect; tab_id targets a tab.
    """
    return _post_action("/api/double-click", _confirm(_with_tab(_target({"selector": selector}, ref, element), tab_id), confirm_token),
                        wait, timeout, expect)


@tool("core", annotations=ToolAnnotations(title="Submit form", **DESTRUCTIVE))
def browser_submit_form(
    selector: str | None = None, tab_id: TabId = None,
    ref: Ref = None, element: Element = None, expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Submit the target form (or the target element's enclosing form).
    Without selector/ref, submit the first <form> found. ok=true means dispatched:
    pass expect to verify the submit's effect, and tab_id to target a tab.

    On a failure with retryable=false, inspect the page before submitting again.
    """
    body = {}
    if selector: body["selector"] = selector
    return _post_action("/api/submit-form", _confirm(_with_tab(_target(body, ref, element), tab_id), confirm_token), expect=expect)


@tool("core", annotations=ToolAnnotations(title="Form fill", **ACT))
def browser_form_fill(
    fields: Annotated[dict, Field(description='e.g. {"email": "foo@bar", "Password": "...", "#submit-target": true}')],
    tab_id: TabId = None, expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Fill many fields at once via fuzzy matching.
    ok=true means dispatched; verify with expect; tab_id targets a tab.

    Each key in `fields` is matched as: CSS selector → name attribute →
    label text (case-insensitive contains) → placeholder. Each value is
    applied via the appropriate setter (.value for inputs/textareas, .checked
    for checkboxes/radios, value for selects). Returns per-key results.
    A select value must match an option value; otherwise that key returns an
    error and the current selection is left unchanged.
    Checkbox/radio values must be true/false, 1/0, or a case-insensitive
    "true"/"false", "on"/"off", "yes"/"no", "1"/"0" string; anything else
    returns an error for that key. A selected radio cannot be set to false
    (that would empty its group): set another radio in the group to true.
    False on an unselected radio is a no-op.
    """
    return _post_action("/api/form-fill", _confirm(_with_tab({"fields": fields}, tab_id), confirm_token), expect=expect)


@tool("devtools", annotations=ToolAnnotations(title="Drag", **ACT))
def browser_drag(
    from_selector: str = "", to_selector: str = "", tab_id: TabId = None,
    ref: Ref = None, element: Element = None, to_ref: str | None = None,
    wait: Wait = False, timeout: int = 10000, expect: Expect = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Synthetic drag-and-drop from one element to another (HTML5 drag events).
    ok=true means dispatched; verify with expect; tab_id targets a tab.

    ref identifies the source; to_ref identifies the destination; wait covers both.
    """
    return _post_action("/api/drag", _confirm(_with_tab(_target({"from": from_selector, "to": to_selector, "toRef": to_ref}, ref, element), tab_id), confirm_token),
                        wait, timeout, expect)


@tool("devtools", annotations=ToolAnnotations(title="Click and wait navigation", **ACT))
def browser_click_and_wait_navigation(
    selector: str = "", timeout: int = 15000, tab_id: TabId = None,
    ref: Ref = None, element: Element = None, confirm_token: ConfirmToken = None,
) -> dict:
    """Click an element, then wait for the resulting navigation to complete.

    Atomic operation — far more reliable than `browser_click` followed by
    `browser_wait_for_url` for links/submit buttons. Listens for
    tabs.onUpdated(status='complete')."""
    return _post(
        "/api/click-and-wait-navigation",
        _confirm(_with_tab(_target({"selector": selector, "timeout": timeout}, ref, element), tab_id), confirm_token),
        timeout=(timeout / 1000) + 10,
    )


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Visual additions
# ════════════════════════════════════════════════════════════════════

@tool("core", annotations=ToolAnnotations(title="Upload file", **DESTRUCTIVE))
def browser_upload_file(
    path: Annotated[str | list[str], Field(
        description="Absolute file path, or a list of paths for multi-file inputs.")],
    selector: Annotated[str | None, Field(
        description="The file input, an element containing it, or a drop zone. Omit to use the first "
                    "input[type=file] on the page.")] = None,
    mode: Annotated[str, Field(description='"auto" (input if found, else drop), "input", or "drop".')] = "auto",
    tab_id: Annotated[int | None, Field(
        description="Target tab (default: active). Pass it for anything big - the upload streams in "
                    "~384 KB chunks and must keep hitting the same tab.")] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Attach local file(s) to a page's upload control - no OS file dialog.
    Returns {ok, mode, files: [{name, size, type}], chunks, bytes, transport, seconds}.

    The bridge reads the file from disk and streams it into the page, which builds
    real File objects and sets them on the <input type=file> (firing input + change).
    If the selector is a drop zone with no input inside, it fires dragenter/dragover/drop.
    """
    body = {"paths": path if isinstance(path, list) else [path], "mode": mode}
    if selector:
        body["selector"] = selector
    return _post("/api/upload-file", _confirm(_with_tab(body, tab_id), confirm_token), timeout=320)


def _screenshot_result(r: dict, save_to: str | None, data_url: bool):
    """Return MCP Image content by default; explicit file/raw modes retain metadata."""
    import base64
    import pathlib

    data = r.get("dataUrl")
    if not data or ";base64," not in data:
        return r
    mime, encoded = data.split(";base64,", 1)
    raw = base64.b64decode(encoded)
    out = {k: v for k, v in r.items() if k != "dataUrl"}
    if save_to:
        path = pathlib.Path(save_to).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        out["saved_to"] = str(path)
    if data_url:
        out["dataUrl"] = data
    if save_to or data_url:
        return out
    return [Image(data=raw, format="jpeg" if mime == "data:image/jpeg" else "png"), out]


@tool("devtools", annotations=ToolAnnotations(title="Full page metrics", **READ))
def browser_full_page_metrics(tab_id: TabId = None) -> dict:
    """Return document/viewport sizes + scroll position + devicePixelRatio."""
    return _post("/api/full-page-metrics", _with_tab({}, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Tab and window management
# ════════════════════════════════════════════════════════════════════

@tool("tabs", annotations=ToolAnnotations(title="Pin tab", **IDEMPOTENT_ACT))
def browser_pin_tab(pinned: bool = True, tab_id: TabId = None) -> dict:
    """Pin or unpin a tab."""
    return _post("/api/pin-tab", _with_tab({"pinned": pinned}, tab_id))


@tool("tabs", annotations=ToolAnnotations(title="Mute tab", **IDEMPOTENT_ACT))
def browser_mute_tab(muted: bool = True, tab_id: TabId = None) -> dict:
    """Mute or unmute a tab."""
    return _post("/api/mute-tab", _with_tab({"muted": muted}, tab_id))


@tool("tabs", annotations=ToolAnnotations(title="Duplicate tab", **ACT))
def browser_duplicate_tab(tab_id: TabId = None) -> dict:
    """Duplicate a tab. Returns the new tab id."""
    return _post("/api/duplicate-tab", _with_tab({}, tab_id))


@tool("tabs", annotations=ToolAnnotations(title="Reload tab", **ACT))
def browser_reload_tab(bypass_cache: bool = False, tab_id: TabId = None, expect: Expect = None) -> dict:
    """Reload a tab. Pass `bypass_cache=True` for a hard reload.
    ok=true means dispatched; verify with expect; tab_id targets a tab."""
    return _post_action("/api/reload-tab", _with_tab({"bypassCache": bypass_cache}, tab_id), expect=expect)


@tool("core", annotations=ToolAnnotations(title="Back", **ACT))
def browser_back(tab_id: TabId = None, expect: Expect = None) -> dict:
    """Go back in history (browser back button).
    ok=true means dispatched; verify with expect; tab_id targets a tab."""
    return _post_action("/api/back", _with_tab({}, tab_id), expect=expect)


@tool("core", annotations=ToolAnnotations(title="Forward", **ACT))
def browser_forward(tab_id: TabId = None, expect: Expect = None) -> dict:
    """Go forward in history.
    ok=true means dispatched; verify with expect; tab_id targets a tab."""
    return _post_action("/api/forward", _with_tab({}, tab_id), expect=expect)


@tool("tabs", annotations=ToolAnnotations(title="Get zoom", **READ))
def browser_get_zoom(tab_id: TabId = None) -> dict:
    """Return the current zoom factor (1.0 = 100%)."""
    return _post("/api/get-zoom", _with_tab({}, tab_id))


@tool("tabs", annotations=ToolAnnotations(title="Set zoom", **IDEMPOTENT_ACT))
def browser_set_zoom(factor: float, tab_id: TabId = None) -> dict:
    """Set zoom factor. 0.3 to 5.0 typical; 1.0 = 100%."""
    return _post("/api/set-zoom", _with_tab({"factor": factor}, tab_id))


@tool("windows", annotations=ToolAnnotations(title="List browser windows", **READ))
def browser_windows() -> dict:
    """List all open browser windows with their tabs."""
    return _post("/api/windows", {})


@tool("windows", annotations=ToolAnnotations(title="Create window", **ACT))
def browser_create_window(url: str | None = None, incognito: bool = False,
                          confirm_token: ConfirmToken = None) -> dict:
    """Open a new browser window. Pass a URL to load it at start."""
    body = {"incognito": incognito}
    if url: body["url"] = url
    return _post("/api/create-window", _confirm(body, confirm_token))


@tool("windows", annotations=ToolAnnotations(title="Close window", **DESTRUCTIVE))
def browser_close_window(window_id: int) -> dict:
    """Close a browser window by ID (from browser_windows)."""
    return _post("/api/close-window", {"windowId": window_id})


@tool("windows", annotations=ToolAnnotations(title="Focus window", **IDEMPOTENT_ACT))
def browser_focus_window(window_id: int) -> dict:
    """Bring a browser window to the foreground."""
    return _post("/api/focus-window", {"windowId": window_id})


@tool("windows", annotations=ToolAnnotations(title="Move tab", **ACT))
def browser_move_tab(tab_id: Annotated[int, Field(description="The tab to move.")],
                     window_id: int | None = None, index: int = -1) -> dict:
    """Move a tab to a position (and optionally to a different window). -1 = end."""
    body = {"tabId": tab_id, "index": index}
    if window_id is not None: body["windowId"] = window_id
    return _post("/api/move-tab", body)


@tool("windows", annotations=ToolAnnotations(title="Detach tab", **ACT))
def browser_detach_tab(tab_id: Annotated[int, Field(description="The tab to pop out.")]) -> dict:
    """Pop a tab out into its own new window."""
    return _post("/api/detach-tab", {"tabId": tab_id})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Network, cookies, storage, clipboard, downloads
# ════════════════════════════════════════════════════════════════════

@tool("storage", annotations=ToolAnnotations(title="Manage cookies", **DESTRUCTIVE))
def browser_cookies(
    op: str,
    url: str | None = None,
    name: str | None = None,
    value: str | None = None,
    domain: str | None = None,
    path: str | None = None,
    secure: bool = False,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Cookie operations: "get", "set", "remove", "clear".

    - get: returns matching cookies (use url/domain/name to filter).
    - set: requires url + name + value.
    - remove: requires url + name.
    - clear: clears all cookies for url or domain.
    """
    body = {"op": op}
    for k, v in [("url", url), ("name", name), ("value", value), ("domain", domain), ("path", path), ("secure", secure)]:
        if v is not None: body[k] = v
    return _post("/api/cookies", _confirm(body, confirm_token))


@tool("storage", annotations=ToolAnnotations(title="Manage page storage", **DESTRUCTIVE))
def browser_storage(
    kind: Annotated[str, Field(description='"local" or "session".')],
    op: Annotated[str, Field(description='"get" | "set" | "remove" | "clear" | "list" | "snapshot" | "restore".')],
    key: Annotated[str | None, Field(description="Required for get/set/remove.")] = None,
    value: Annotated[str | None, Field(
        description="For set, the string value. For restore, a dict of key→value.")] = None,
    tab_id: Annotated[int | None, Field(
        description="Tab to run against. Storage is per-origin, so prefer driving it on a tab already "
                    "at the right origin.")] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """localStorage / sessionStorage ops."""
    body = {"kind": kind, "op": op}
    if key is not None: body["key"] = key
    if value is not None: body["value"] = value
    return _post("/api/storage", _confirm(_with_tab(body, tab_id), confirm_token))


@tool("storage", annotations=ToolAnnotations(title="Read or write clipboard", **DESTRUCTIVE))
def browser_clipboard(
    op: Annotated[str, Field(description='"read" or "write".')],
    text: Annotated[str | None, Field(description='Text to write when op="write".')] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Read or write the system clipboard."""
    body = {"op": op}
    if text is not None: body["text"] = text
    return _post("/api/clipboard", _confirm(body, confirm_token))


@tool("network", annotations=ToolAnnotations(title="Manage downloads", **DESTRUCTIVE))
def browser_downloads(
    op: Annotated[str, Field(description='"download" | "list" | "cancel".')],
    url: Annotated[str | None, Field(description='For "download", the URL to fetch.')] = None,
    filename: Annotated[str | None, Field(description="Output filename.")] = None,
    query: Annotated[dict | None, Field(description='For "list", a downloads.search query dict.')] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Downloads operations."""
    body = {"op": op}
    if url: body["url"] = url
    if filename: body["filename"] = filename
    if query: body["query"] = query
    return _post("/api/downloads", _confirm(body, confirm_token))


@tool("storage", annotations=ToolAnnotations(title="Clear browsing data", **DESTRUCTIVE))
def browser_clear_browsing_data(
    types: Annotated[list[str] | None, Field(
        description='Which to clear. Subset of ["cache","cookies","history","localStorage","passwords",'
                    '"downloads"]. Default = all of those.')] = None,
    since_ms: Annotated[int | None, Field(description="Only data created since this epoch-ms timestamp.")] = None,
) -> dict:
    """Clear browsing data of the given types."""
    body = {}
    if types: body["types"] = types
    if since_ms: body["since"] = since_ms
    return _post("/api/clear-browsing-data", body, timeout=60)


@tool("network", annotations=ToolAnnotations(title="Manage request interception", **DESTRUCTIVE))
def browser_intercept(
    op: Annotated[str, Field(description='"add" | "clear" | "list" | "log" | "clearLog".')],
    patterns: Annotated[list[str] | None, Field(
        description='For "add", regex patterns to match against request URLs.')] = None,
    effect: Annotated[str, Field(description='For "add", "block" (cancel request) or "redirect".')] = "block",
) -> dict:
    """Block, log, or redirect HTTP requests by URL regex.

    Useful to suppress analytics/ads during agent runs (3–10× page speed-up)
    or to log what an SPA actually fetches.
    """
    body = {"op": op, "effect": effect}
    if patterns: body["patterns"] = patterns
    return _post("/api/intercept", body)


@tool("network", annotations=ToolAnnotations(title="Manage network capture", **DESTRUCTIVE))
def browser_capture_network(
    op: Annotated[str, Field(
        description='"start" begins capturing fresh, "stop" pauses, "read" returns the buffer, "clear" '
                    'empties it.')] = "read",
    since: Annotated[int, Field(
        description="When reading, only return entries with startTime >= since (ms).")] = 0,
    tab_id: TabId = None,
) -> dict:
    """Capture network requests via the page's PerformanceObserver."""
    return _post("/api/capture-network", _with_tab({"op": op, "since": since}, tab_id))


@tool("core", annotations=ToolAnnotations(title="Watch console", **ACT))
def browser_watch_console(enabled: bool = True, tab_id: TabId = None) -> dict:
    """Toggle console capture on a tab. Once enabled, browser console
    messages (log/info/warn/error/debug + window errors) are buffered up to
    500 entries; retrieve via browser_console_logs."""
    return _post("/api/watch-console", _with_tab({"enabled": enabled}, tab_id))


@tool("core", annotations=ToolAnnotations(title="Console logs", **READ))
def browser_console_logs(
    since: int | None = None, level: str | None = None, tab_id: TabId = None
) -> dict:
    """Return buffered console logs from a tab (requires browser_watch_console first)."""
    body = {}
    if since is not None: body["since"] = since
    if level: body["level"] = level
    return _post("/api/console-logs", _with_tab(body, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Orchestration
# ════════════════════════════════════════════════════════════════════

@tool("parallel", annotations=ToolAnnotations(title="Broadcast", **DESTRUCTIVE))
def browser_broadcast(
    tab_ids: Annotated[list[int], Field(description="Tabs to target.")],
    command: Annotated[dict, Field(
        description='Command dict, e.g. {"action": "pageInfo"} or {"action": "getReadability"}.')],
    timeout: Annotated[int, Field(description="Per-tab timeout in seconds.")] = 30,
) -> dict:
    """Fire the same command at every listed tab in parallel.

    Each tab gets the command with its own tabId injected. Returns per-tab
    results keyed by tab id.
    """
    return _post(
        "/api/broadcast",
        {"tabIds": tab_ids, "command": command, "timeout": timeout},
        timeout=timeout + 10,
    )


@tool("parallel", annotations=ToolAnnotations(title="Wait for JavaScript across tabs", **DESTRUCTIVE))
def browser_sync_barrier(
    tab_ids: Annotated[list[int], Field(description="Tabs to monitor.")],
    predicate: Annotated[str, Field(
        description="JS expression that returns truthy when the tab is ready, e.g. "
                    "\"!!document.querySelector('.results')\".")],
    timeout: Annotated[int, Field(description="Overall timeout in ms.")] = 30000,
    poll_interval: Annotated[int, Field(description="Per-poll interval in ms.")] = 300,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Block until a JS predicate returns truthy on every listed tab.

    Replaces hand-rolled "wait for everything to be ready" loops.
    Needs the developer extension build.
    """
    return _post(
        "/api/sync-barrier",
        _confirm({"tabIds": tab_ids, "predicate": predicate, "timeout": timeout, "pollInterval": poll_interval},
                 confirm_token),
        timeout=(timeout / 1000) + 10,
    )


@tool("parallel", annotations=ToolAnnotations(title="Tag tab", **IDEMPOTENT_ACT))
def browser_tag_tab(name: str, tab_id: Annotated[int, Field(description="The tab to name.")]) -> dict:
    """Give a tab a memorable name. Refer to it later via browser_resolve_tag.
    Names survive bridge restarts only if you re-tag — they live in memory."""
    return _post("/api/tag-tab", {"name": name, "tabId": tab_id})


@tool("parallel", annotations=ToolAnnotations(title="Resolve tag", **READ))
def browser_resolve_tag(name: str) -> dict:
    """Look up the tab id for a tag set via browser_tag_tab."""
    return _post("/api/resolve-tag", {"name": name})


@tool("parallel", annotations=ToolAnnotations(title="List tags", **READ))
def browser_list_tags() -> dict:
    """List all tag → tab id mappings."""
    return _post("/api/list-tags", {})


@tool("parallel", annotations=ToolAnnotations(title="Untag tab", **IDEMPOTENT_ACT))
def browser_untag_tab(name: str) -> dict:
    """Remove a tag."""
    return _post("/api/untag-tab", {"name": name})


@tool("parallel", annotations=ToolAnnotations(title="Resize browser tab pool", **DESTRUCTIVE))
def browser_tab_pool(size: int, url: str = "about:blank") -> dict:
    """Maintain a warm pool of N tabs always ready to be acquired.

    Grows or shrinks to `size`. Acquire/release via browser_pool_acquire and
    browser_pool_release. Auto-grows by one if you acquire from an empty pool.
    """
    return _post("/api/tab-pool", {"size": size, "url": url})


@tool("parallel", annotations=ToolAnnotations(title="Acquire a pooled tab", **ACT))
def browser_pool_acquire() -> dict:
    """Check out a warm tab from the pool. Returns its tab_id."""
    return _post("/api/pool-acquire", {})


@tool("parallel", annotations=ToolAnnotations(title="Release a pooled tab", **IDEMPOTENT_ACT))
def browser_pool_release(tab_id: Annotated[int, Field(description="The pooled tab to return.")]) -> dict:
    """Return a tab to the pool for reuse."""
    return _post("/api/pool-release", {"tabId": tab_id})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Session save/restore (auth-once-use-many)
# ════════════════════════════════════════════════════════════════════

@tool("session", annotations=ToolAnnotations(title="Save session", **DESTRUCTIVE))
def browser_save_session(
    name: Annotated[str, Field(
        description="A label for the session (becomes filename under ~/claude-zen-sessions/).")],
    urls: Annotated[list[str] | None, Field(
        description="Origins whose cookies + localStorage to snapshot. Each gets opened briefly in a "
                    "temp tab so localStorage can be read.")] = None,
    confirm_token: ConfirmToken = None,
) -> dict:
    """Snapshot cookies (and per-origin localStorage if urls given) to disk.

    Use case: log into a service interactively once, save the session,
    then have parallel agents load it for authenticated work.
    """
    return _post("/api/save-session", _confirm({"name": name, "urls": urls or []}, confirm_token), timeout=120)


@tool("session", annotations=ToolAnnotations(title="Load session", **DESTRUCTIVE))
def browser_load_session(name: str, confirm_token: ConfirmToken = None) -> dict:
    """Restore cookies + localStorage from a previously saved session."""
    return _post("/api/load-session", _confirm({"name": name}, confirm_token), timeout=120)


@tool("session", annotations=ToolAnnotations(title="List sessions", **READ))
def browser_list_sessions() -> dict:
    """List saved sessions with their metadata."""
    return _post("/api/list-sessions", {})


@tool("session", annotations=ToolAnnotations(title="Delete session", **DESTRUCTIVE))
def browser_delete_session(name: str) -> dict:
    """Delete a saved session file."""
    return _post("/api/delete-session", {"name": name})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Observability and policy
# ════════════════════════════════════════════════════════════════════

@tool("devtools", annotations=ToolAnnotations(title="Check browser health", **READ))
def browser_health() -> dict:
    """Extended health/status: bridge version, ports, keep-alive list,
    pool state, tags, policy, sessions dir, audit/log sizes."""
    return _post("/api/health", {})


@tool("devtools", annotations=ToolAnnotations(title="Read bridge logs", **READ))
def browser_logs(limit: int = 200, since: str | None = None) -> dict:
    """Return recent bridge log lines (in-memory ring buffer)."""
    body = {"limit": limit}
    if since: body["since"] = since
    return _post("/api/logs", body)


@tool("devtools", annotations=ToolAnnotations(title="Read command audit log", **READ))
def browser_audit(limit: int = 100, since_ms: int = 0) -> dict:
    """Return recent command audit entries: action, params (JS code stripped,
    typed text, select values, dialog prompt answers and cookie/storage/
    clipboard values redacted), success flag,
    error, duration. Useful for "what did the agent do?" reviews."""
    return _post("/api/audit", {"limit": limit, "since": since_ms})


@tool("parallel", annotations=ToolAnnotations(title="Read scheduler state", **READ))
def browser_scheduler() -> dict:
    """Return bridge scheduler state for multi-agent/tab work.

    Shows active per-tab/global command scopes, pending WebSocket commands, and
    known tab locks. Useful when several agents are driving separate tabs.
    """
    return _post("/api/scheduler", {})


@tool("devtools", annotations=ToolAnnotations(title="Set policy", **DESTRUCTIVE))
def browser_set_policy(
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    readonly: bool | None = None,
    ask: bool | None = None,
    ask_urls: list[str] | None = None,
) -> dict:
    """Set URL allow/deny regex lists, read-only mode, and/or ask-before-act mode.

    Read-only mode blocks write actions (click/type/navigate/cookies set/etc.), js and
    batch if/while conditions, while still allowing read ops. Useful for "let the agent
    observe only."

    Ask mode (ask=True) holds sensitive actions without running them: form submits,
    submit-control or pay/order-labeled button clicks, Enter in a form field, browser_js and
    other agent JavaScript, cookies, sessions, page storage, clipboard, downloads, navigation
    to ask_urls (payment-like regexes, case-insensitive), and page actions on such pages. A held call returns code "confirmation_required" with
    state.confirmToken; ask the user, then repeat the identical call with confirm_token
    within 120s. The token is single-use, bound to that action, its parameters and tab, and
    is never honored inside batch/retry/parallel/broadcast/workflows. A bridge started with
    KITSUNE_ASK_BEFORE_ACT=1 locks ask mode on, also after restarts; then ask=False or
    dropping ask_urls fails.
    """
    body = {}
    if allow is not None: body["allow"] = allow
    if deny is not None: body["deny"] = deny
    if readonly is not None: body["readonly"] = readonly
    if ask is not None: body["ask"] = ask
    if ask_urls is not None: body["askUrls"] = ask_urls
    return _post("/api/set-policy", body)


@tool("devtools", annotations=ToolAnnotations(title="Get policy", **READ))
def browser_get_policy() -> dict:
    """Return the active URL/readonly/ask policy."""
    return _post("/api/get-policy", {})


@tool("parallel", annotations=ToolAnnotations(title="Retry a command", **DESTRUCTIVE))
def browser_retry(
    command: Annotated[dict, Field(description="A command dict (same shape as browser_batch entries).")],
    max_attempts: Annotated[int, Field(description="Total tries including the first.")] = 3,
    backoff_ms: Annotated[int, Field(description="Initial wait between attempts (doubles each retry).")] = 500,
    on_errors: Annotated[list[str] | None, Field(
        description="Only retry if the error string contains one of these substrings. Default: retry "
                    "on any error.")] = None,
) -> dict:
    """Wrap any command in exponential-backoff retry; an error with retryable=false (a write
    whose outcome is unknown) is never repeated.
    """
    body = {"command": command, "maxAttempts": max_attempts, "backoffMs": backoff_ms}
    if on_errors: body["onErrors"] = on_errors
    retry = {"action": "retry", **body}
    return _post("/api/retry", body, timeout=max(60, command_wait_seconds(retry) + 15))


# -- Batch --------------------------------------------------------

@tool("parallel", annotations=ToolAnnotations(title="Run command batch", **DESTRUCTIVE))
def browser_batch(
    commands: Annotated[list[dict], Field(
        description='List of command dicts, e.g. [{"action": "navigate", "url": "..."}, {"action": '
                    '"waitForElement", "selector": ".results", "timeout": 10000}, {"action": "pageText"}]')],
) -> dict:
    """Run multiple commands in a single request for speed. A failed command does not
    make the call an MCP error: inspect each result, because earlier commands may
    already have taken effect.

    Each command is a dict with "action" and parameters. Add "tabId" to any
    command to target a specific tab instead of the active one. For true
    concurrency across tabs, prefer browser_parallel. Resume from the failed
    command; progress gives each command's status (ok, failed, held, unknown, skipped).

    Available actions: navigate, newTab, closeTab, switchTab, click, type,
    setEditableContent, fill, scroll, hover, find, js, pageInfo, pageText, screenshot, tabs,
    forms, dom, sleep, waitForElement, pageTextByTabId, wakeTab, parallel

    click, doubleClick, hover, drag, type, fill, setEditableContent, selectOption,
    checkBox and keypress accept "wait": true and "timeout" (ms, default 10000) to
    wait for their target first, like the matching tools.
    """
    # Outlast the bridge's deadline, so a batch it stops still returns its partial progress.
    return _post("/api/batch", {"commands": commands},
                 timeout=container_request_seconds(batch_deadline_seconds(commands)))


@tool("parallel", annotations=ToolAnnotations(title="Resume a container call", **DESTRUCTIVE))
def browser_resume(
    call: Annotated[int, Field(description="The call id from that response.")],
    resume_token: Annotated[str, Field(description="The resume_token from that response.")],
    observed: Annotated[dict[str, str] | None, Field(
        description='For each unknown step (by progress path, e.g. "1" or "0.1.2"), "not_applied" to send '
                    'it again or "applied" to mark it ok after checking the page.')] = None,
) -> dict:
    """Resume a browser_batch, browser_parallel or browser_workflow call that did not finish ok.

    Steps that ran ok are not sent again. Returns the same shape as the original call.
    """
    body: dict = {"call": call, "resume_token": resume_token}
    if observed is not None:
        body["observed"] = observed
    return _post("/api/resume", body, timeout=container_request_seconds(RESUME_DEADLINE_SECONDS))


def main():
    """Entry point for the Kitsune MCP server."""
    error = _ensure_bridge()  # warm start; tools retry if this fails
    if error:
        print(error, file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
