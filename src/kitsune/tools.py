"""Kitsune's agent tools: the browser_* functions the MCP server publishes, without MCP code.

Each tool maps its arguments onto a bridge request and returns the bridge's JSON result; a
failure is a result with error, not an exception. tool() records each tool's capability group,
annotation hints and, for a few, the task modes it belongs to, in TOOLS in definition order;
the core group (kitsune.playwright_tools) follows Kitsune's own. kitsune.server registers them
on FastMCP and decides which its KITSUNE_CAPS and KITSUNE_MODE publish.

The tools send their requests through `client`, the BridgeClient the MCP server sets.
"""

import functools
import inspect
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from time import sleep as _sleep
from typing import Annotated, Callable, Literal

from pydantic import Field

from . import playwright_tools
from .bridge_client import BridgeClient
from .capabilities import CAPABILITY_GROUPS
from .errors import failed as _failed  # whether a tool's own result reports failure
from .waits import (
    RESUME_DEADLINE_SECONDS, WORKFLOW_DEADLINE_SECONDS, action_request_seconds, batch_deadline_seconds,
    command_wait_seconds, container_request_seconds,
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


@dataclass(frozen=True)
class ToolSpec:
    """A tool as tool() recorded it."""
    function: Callable
    group: str
    annotations: dict | None  # MCP ToolAnnotations fields
    modes: frozenset | None  # the task modes that publish the tool; None: every mode


# Every tool by name, in definition order.
TOOLS: dict[str, ToolSpec] = {}


def tool(group: str, annotations: dict | None = None, *, modes=None):
    """Record a tool of a capability group (kitsune.capabilities) with its annotation hints.

    modes, if given, are the only task modes whose MCP server publishes the tool. The function
    is returned unchanged: Python callers get the plain function.
    """
    if group not in CAPABILITY_GROUPS:
        raise ValueError(f"Unknown tool capability group: {group}")

    def decorate(function):
        TOOLS[function.__name__] = ToolSpec(function, group, annotations, frozenset(modes) if modes else None)
        return function

    return decorate


@dataclass(frozen=True)
class Image:
    """Image data a tool returns for the model to see; the MCP server sends it as image content."""
    data: bytes
    format: str  # "png" or "jpeg"


# Published tools that write the page or tab they run on (#303): the bridge's write actions that
# act on a tab (bridge.WRITE_ACTIONS, focus and blur). Without a tab they would write the user's
# active tab, so with no current tab they are refused; reads still default to the active tab.
TAB_WRITE_TOOLS = frozenset({
    # core
    "browser_click", "browser_drag", "browser_drop", "browser_evaluate", "browser_file_upload",
    "browser_fill_form", "browser_handle_dialog", "browser_hover", "browser_navigate",
    "browser_navigate_back", "browser_press_key", "browser_resize", "browser_select_option", "browser_type",
    # Kitsune's own
    "browser_blur", "browser_check", "browser_click_and_wait_navigation", "browser_duplicate_tab",
    "browser_focus", "browser_forward", "browser_highlight", "browser_mute_tab", "browser_pin_tab",
    "browser_reload_tab", "browser_scroll", "browser_set_zoom", "browser_submit_form", "browser_trusted_click",
})
# Container steps that write their tab, by the action names steps use: the bridge's tab-claiming
# actions (bridge._claims_tab) that are not tabless; an if/while condition is JavaScript on its tab.
TAB_WRITE_STEPS = frozenset({
    "blurElement", "checkBox", "click", "clickAndWaitNavigation", "closeTab", "detachTab", "doubleClick",
    "drag", "duplicateTab", "executeJS", "fill", "focusElement", "formFill", "goBack", "goForward",
    "highlight", "hover", "if", "js", "keypress", "media", "moveTab", "muteTab", "navigate", "pinTab",
    "reloadTabBrowser", "resizeWindow", "scroll", "selectOption", "setEditableContent", "setZoom",
    "storageOp", "submitForm", "switchTab", "trustedClick", "type", "wakeTab", "while",
})
# Page storage ops that only read (the bridge's read-only mode permits them too).
_STORAGE_READS = frozenset({"get", "list", "snapshot"})
# Steps nested in a container step, by block; parallel sequences already name their tabs (tab_required).
_STEP_BLOCKS = {"sequence": ("do",), "while": ("do",), "try": ("do", "catch"), "if": ("then", "else")}


def _tabless_step_write(step) -> str | None:
    """The action of the first step in step, or nested in it, that writes a tab and names none."""
    if not isinstance(step, dict):
        return None
    action = step.get("action")
    if step.get("tabId") is None and action in TAB_WRITE_STEPS and not (
            action == "storageOp" and step.get("op") in _STORAGE_READS):
        return action
    nested = [step.get("command")] if action == "retry" else [
        command for block in _STEP_BLOCKS.get(action, ()) for command in step.get(block) or []]
    return next(filter(None, map(_tabless_step_write, nested)), None)


def _no_current_tab(name: str, arguments: dict) -> dict | None:
    """The refusal of a published tool call that would write the user's active tab (#303), else None.

    Called when the call names no tab and there is no current tab.
    """
    hint = ("Nothing was dispatched. Pass tab_id (browser_navigate and browser_navigate_back accept it too), "
            "open a background tab with browser_tabs action=new or browser_tab_list new_tab, or make one "
            "current with browser_tabs action=select.")
    if name == "browser_workflow":
        return {"error": "browser_workflow names no tab_id and there is no current tab; its steps cannot be "
                          "inspected before they run, so it needs a tab even if it only reads",
                "code": "no_current_tab", "retryable": False, "hint": hint, "state": {"tool": name}}
    if name == "browser_batch":
        action = next(filter(None, map(_tabless_step_write, arguments.get("commands") or [])), None)
    elif name == "browser_retry":
        action = _tabless_step_write(arguments.get("command"))
    elif name == "browser_storage":
        action = None if arguments.get("op") in _STORAGE_READS else "storageOp"
    else:
        action = None
    if action is None and name not in TAB_WRITE_TOOLS:
        return None
    what = f'{name} step "{action}"' if name in ("browser_batch", "browser_retry") else name
    return {"error": f"{what} writes a tab but names none, and there is no current tab, so it was not sent "
                      "to the tab the user is viewing",
            "code": "no_current_tab", "retryable": False, "hint": hint,
            "state": {"tool": name, **({"action": action} if action else {})}}


def _on_current_tab(function):
    """Run an omitted optional tab_id on the core tools' current tab (#232), and refuse a tab write
    that has no tab (#303).

    The current tab is the one browser_tabs last opened or selected, so a tool the agent calls
    without tab_id acts on the tab its core tools work in, not on the tab the user is looking at.
    Without a current tab, reads go to the user's active tab, while a write or navigation that
    names no tab returns no_current_tab: after an MCP server restart the agent has no current
    tab even though it worked in one. Only the MCP registration applies this; Python callers name
    their tabs, so kitsune.tools and kitsune.browser keep the active-tab default.
    """
    name = function.__name__
    parameter = inspect.signature(function).parameters.get("tab_id")
    fills_tab = parameter is not None and parameter.default is None
    if not fills_tab and name not in TAB_WRITE_TOOLS:
        return function

    @functools.wraps(function)
    def call(*args, **kwargs):
        if fills_tab and kwargs.get("tab_id") is None:
            kwargs["tab_id"] = playwright_tools._current_tab
        if kwargs.get("tab_id") is None and playwright_tools._current_tab is None:
            refusal = _no_current_tab(name, kwargs)
            if refusal:
                return refusal
        return function(*args, **kwargs)

    return call


# The bridge connection every tool sends through; the MCP server sets it to its own client.
client: BridgeClient | None = None


def _get(endpoint: str) -> dict:
    """GET request to bridge."""
    return client.get(endpoint)


def _post(endpoint: str, data: dict, timeout: float = 15) -> dict:
    """POST request to bridge."""
    return client.post(endpoint, data, timeout)


def _request_body(body: dict, tab_id: int | None = None, ref: str | None = None,
               element: str | None = None, confirm_token: str | None = None, first: bool = False) -> dict:
    """Attach ref/audit-only element, tab, approved confirmation token and first-match opt-in."""
    for key, value in (("ref", ref), ("element", element), ("tabId", tab_id), ("confirmToken", confirm_token)):
        if value is not None:
            body[key] = value
    if first:
        body["first"] = True
    return body


def _post_action(endpoint: str, body: dict, wait: bool = False, timeout: int = 10000,
                 expect: dict | None = None) -> dict:
    """POST a page action.

    wait=True lets the page wait up to timeout ms for the target before dispatch;
    expect makes the bridge check the outcome after dispatch, within expect.timeout.
    """
    if wait:
        if timeout <= 0:
            return {"error": "timeout must be positive"}
        body = {**body, "wait": True, "timeout": timeout}
    if expect is not None:
        body = {**body, "expect": expect}
    request_timeout = action_request_seconds(wait, timeout, expect)
    if request_timeout == 15:
        return _post(endpoint, body)
    return _post(endpoint, body, timeout=request_timeout)


def _read_page(endpoint: str, tab_id: int | None, selector: str | None = None, ref: str | None = None,
               max_depth: int | None = None, text: bool | None = None) -> dict:
    """Read the active page with GET, or POST a tab target and optional tree scope."""
    if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
        return {"error": "max_depth must be a non-negative integer"}
    body = _request_body({}, tab_id)
    if text is not None:
        body["text"] = text
    if selector is not None:
        body["selector"] = selector
    if ref is not None:
        body["ref"] = ref
    if max_depth is not None:
        body["maxDepth"] = max_depth
    if not body:
        return _get(endpoint)
    return _post(endpoint, body)


# Some clients cut tool descriptions to their first paragraph but send input schemas in
# full (#135), so parameters are documented as schema descriptions. The parameters shared
# by the action tools (ref, element, wait, expect, confirm_token, first) are documented
# once, on browser_check; the other action tools name expect and point there in their
# first paragraph, which keeps the repeated text small (#88).
#
# An omitted tab_id is the current tab: the one browser_tabs last opened or selected, else
# the active tab for reads; MCP writes without one are refused (#232, #303, _on_current_tab).
TabId = Annotated[int | None, Field(description="Default: current tab.")]
# A container's default tab: the bridge runs its commands that name no tabId there.
DefaultTabId = Annotated[int | None, Field(description="Tab for commands that name no tabId. Default: current tab.")]


def _default_tab(body: dict, tab_id: int | None) -> dict:
    return {**body, "tabId": tab_id} if tab_id is not None else body


# ════════════════════════════════════════════════════════════════════
#  Parallel-work primer (read this if you're driving multiple tabs)
# ════════════════════════════════════════════════════════════════════
#
# Every tab-aware tool below accepts an optional `tab_id`. If omitted, it
# targets the current tab (the one browser_tabs last opened or selected; with
# none, reads use the active tab and MCP writes are refused). Pass `tab_id` to
# drive a specific tab without stealing focus — that's the foundation for
# parallel multi-tab work.
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

@tool("admin", annotations=dict(title="Check browser status", **READ))
def browser_status() -> dict:
    """Check bridge/extension connection; diagnosis explains failures."""
    result = _get("/api/status")
    if result.get("code") and "diagnosis" not in result:  # the bridge was not reached
        result["diagnosis"] = {key: result[key] for key in ("code", "hint", "state") if key in result}
    return result


def _tab_list(result: dict, window_fields: bool) -> dict:
    """Drop windowId/index from listed tabs unless requested or the tabs span windows (#136)."""
    tabs = result.get("tabs")
    if not isinstance(tabs, list) or window_fields:
        return result
    if len({tab.get("windowId") for tab in tabs}) > 1:
        return result
    shown = [{key: value for key, value in tab.items() if key not in ("windowId", "index")}
             for tab in tabs]
    return {**result, "tabs": shown}


def browser_tabs(
    action: Literal["list", "new", "close", "select"] = "list",
    tab_id: Annotated[int | None, Field(description="close/select (required).")] = None,
    url: Annotated[str | None, Field(description="new (default about:blank).")] = None,
    force: Annotated[bool, Field(description="close: disarm beforeunload.")] = False,
    raw: Annotated[bool, Field(description="new: bypass the image viewer.")] = False,
    wait: Annotated[bool, Field(
        description="new: wait up to timeout ms for the load; returns tabId, final url and status, or on "
                    "timeout ok=false, timedOut=true with the tab open.")] = False,
    timeout: int = 15000,
    confirm_token: Annotated[str | None, Field(description="new/close; see browser_check.")] = None,
    window_fields: bool = False,
    include_active: Annotated[bool, Field(
        description="list: add active_page, the active tab's browser_snapshot(text=True).")] = False,
) -> dict:
    """List, open, close, or select tabs; new opens a background tab (act on it with tab_id)
    and returns its tabId.

    list is always a fresh read; owned=true marks tabs the agent opened; windowId/index only
    with several windows or window_fields. close refuses a tab the agent did not open
    (user_tab) unless the user approves (confirm_token). select switches the user's view:
    only when the user asks. Options of other actions are ignored.
    """
    if action == "list":
        result = _tab_list(_get("/api/tabs"), window_fields)
        if include_active and not _failed(result):
            result = {**result, "active_page": _post("/api/snapshot", {"text": True})}
        return result
    if action == "new":
        body = {"url": url if url is not None else "about:blank"}
        if raw:
            body["raw"] = True
        _request_body(body, confirm_token=confirm_token)
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
        if action == "close":
            if force:
                body["force"] = True
            _request_body(body, confirm_token=confirm_token)
        return _post("/api/close-tab" if action == "close" else "/api/switch-tab", body)
    return {"error": "action must be list, new, close, or select"}


def browser_tab_list(
    new_tab: Annotated[str | None, Field(
        description="http(s) URL to open first in a new background tab, which later tools act on.")] = None,
) -> dict:
    """List open tabs with IDs, titles and URLs; new_tab opens a background tab first and
    returns its tabId. Does not close tabs or switch the user's view.
    """
    # read and draft publish this instead of browser_tabs. A tab of its own lets a restricted
    # agent read any page without navigating the user's tabs, which KITSUNE_PROTECT_USER_TABS
    # refuses (#270).
    if new_tab is None:
        return _tab_list(_get("/api/tabs"), False)
    opened = browser_tabs("new", url=new_tab, wait=True)
    if not isinstance(opened.get("tabId"), int):
        return opened
    playwright_tools._current_tab = opened["tabId"]
    listed = _tab_list(_get("/api/tabs"), False)
    tab = {key: opened[key] for key in ("tabId", "url", "status") if key in opened}
    return {**tab, **({} if _failed(listed) else {"tabs": listed.get("tabs")})}


tool("core", annotations=dict(title="List or open browser tabs", **ACT), modes=("read", "draft"))(browser_tab_list)


@tool("read", annotations=dict(title="Page info", **READ))
def browser_page_info(tab_id: TabId = None) -> dict:
    """Get current page URL, title, dimensions, scroll position, and recent dialogs."""
    return _read_page("/api/page-info", tab_id)


@tool("read", annotations=dict(title="Page text", **READ))
def browser_page_text(
    tab_id: Annotated[int | None, Field(description="Default: current tab; others are read without focusing.")] = None,
) -> dict:
    """Extract all readable text from the current page.

    An HTTP error response the browser shows without a readable document returns
    text="" with empty=true and its httpStatus.
    """
    return _read_page("/api/page-text", tab_id)


@tool("read", annotations=dict(title="Read page forms", **READ))
def browser_forms(tab_id: TabId = None) -> dict:
    """Get all form fields on the current page with their labels and values; password and
    other sensitive values are redacted (`redacted`/`hasValue` replace `value`)."""
    return _read_page("/api/forms", tab_id)


@tool("read", annotations=dict(title="Read page DOM", **READ))
def browser_dom(
    tab_id: TabId = None, selector: str | None = None,
    ref: str | None = None, max_depth: int | None = None,
) -> dict:
    """Get the accessibility tree of interactive elements on the page; selector/ref/max_depth
    (default 6) scope it as in browser_snapshot; capped at 20,000 chars (truncated=true)."""
    return _read_page("/api/dom", tab_id, selector, ref, max_depth)


def browser_snapshot(
    tab_id: TabId = None,
    selector: Annotated[str | None, Field(description="Selector subtree root (included).")] = None,
    ref: Annotated[str | None, Field(
        description="Attached subtree root ref; wins over selector.")] = None,
    max_depth: Annotated[int | None, Field(
        description="Depth (>= 0) below the subtree root (body by default); 0 is the root only, omitted "
                    "all.")] = None,
    text: Annotated[bool, Field(
        description="Visible text lines (no refs), url, title and tabId, capped at 12,000 chars with a "
                    "truncation marker.")] = False,
) -> dict:
    """Get a compact accessibility-style tree with stable element refs; prefer them for
    actions. text=False omits static prose; text=True adds visible text and caps the final
    public snapshot at 12,000 chars with truncation guidance.

    Refs name attached nodes in one document/tab and survive newer snapshots, browser_dom,
    find and full or delta post-action observations. Detachment/replacement, navigation or
    content reinjection invalidates them. On stale_ref, inspect state.reason and take a new
    scoped snapshot; never repeat a write just to refresh refs. After an action, its
    target_ref names the same node when still attached and included. Repeated composite
    siblings show the first five items; a snapshot scoped to the ref in their "more similar
    items" marker lists up to 100. Post-action snapshots prioritize the target and its
    ancestor context within their cap.
    """
    return _read_page("/api/snapshot", tab_id, selector, ref, max_depth, text)


def browser_screenshot(
    selector: str | None = None, full_page: bool = False,
    tab_id: TabId = None, save_to: str | None = None,
    data_url: bool = False, ref: str | None = None, element: str | None = None,
    format: str = "png",
):
    """Capture the viewport, one exact element (ref or shared CSS/Playwright-subset selector,
    including shadow roots), or the full page (not with selector/ref) as an inline image,
    JPEG-encoded or downscaled under ~1 MB. Unsupported selectors fail before capture.
    save_to writes the full PNG (or JPEG with format="jpeg") and returns metadata; data_url also returns
    it as a (large) data URL.

    No capture shows the tab or focuses a window.
    """
    if ref is not None and selector is None:
        selector = ""
    if selector is not None and full_page:
        return {"error": "selector and full_page are mutually exclusive"}
    if format not in {"png", "jpeg"}:
        return {"error": "Screenshot format must be png or jpeg"}
    preview = not (save_to or data_url)
    format_body = {"format": format} if format != "png" else {}
    if selector is not None or full_page:
        endpoint = "/api/element-screenshot" if selector is not None else "/api/full-page-screenshot"
        body = _request_body({"selector": selector}, ref=ref, element=element) if selector is not None else {}
        body["imagePreview"] = preview
        body.update(format_body)
        result = _post(endpoint, _request_body(body, tab_id), timeout=120 if full_page else 45)
        return _screenshot_result(result, save_to, data_url)
    result = _post("/api/screenshot", _request_body({"imagePreview": preview, **format_body}, tab_id))
    return _screenshot_result(result, save_to, data_url)


# -- Navigation ---------------------------------------------------

def browser_navigate(
    url: Annotated[str, Field(description='e.g. "https://example.com".')],
    tab_id: TabId = None,
    raw: Annotated[bool, Field(
        description="Open a bare image URL (.jpg/.png/...) raw, not in the scriptable local viewer "
                    "page (image = #zlimg).")] = False,
    expect: dict | None = None, confirm_token: str | None = None,
) -> dict:
    """Navigate a tab to a URL. ok=true means dispatched; verify with expect (see browser_check)."""
    body = {"url": url}
    if raw:
        body["raw"] = True
    return _post_action("/api/navigate", _request_body(body, tab_id, confirm_token=confirm_token), expect=expect)


# -- Interaction --------------------------------------------------

def browser_click(
    selector: Annotated[str, Field(description='e.g. "#submit", ".btn-primary".')] = "",
    x: Annotated[int, Field(description="X coordinate, used without selector/ref.")] = 0,
    y: Annotated[int, Field(description="Y coordinate, used without selector/ref.")] = 0,
    tab_id: TabId = None,
    ref: Annotated[str | None, Field(description="Prefer a ref from browser_snapshot over a selector.")] = None,
    element: Annotated[str | None, Field(description="Audit-only description of the target.")] = None,
    wait: Annotated[bool, Field(
        description="Wait up to timeout ms for the selector/ref target to exist first (click, double_click, "
                    "hover, drag: also rendered); on timeout nothing ran (not_found, not_actionable).")] = False,
    timeout: int = 10000,
    expect: Annotated[dict | None, Field(
        description='Outcome to verify after dispatch, exactly one of {"url": regex}, {"url_changed": true}, '
                    '{"new_tab": true}, {"selector": css} (visible; add "gone": true, "value": str or '
                    '"value_changed": true), {"text": str} or {"text_gone": str}, plus "timeout" ms (default '
                    '5000). Unmet: ok=false, code="outcome_not_observed"; the action already ran, so do not '
                    'blindly repeat it.')] = None,
    confirm_token: Annotated[str | None, Field(
        description="From a confirmation_required error, after the user approved this action.")] = None,
    first: Annotated[bool, Field(
        description="A write tool's selector matching several elements fails with ambiguous_target "
                    "unless true; true acts on the first match.")] = False,
) -> dict:
    """Click an element by ref, CSS selector or coordinates; video/audio toggles play/pause.
    ok=true only means dispatched: verify with expect; tabs the click opens come back in
    opened_tabs.

    Action contract: the other action tools' ref, element, wait, expect, confirm_token and
    first work as here. A successful action listed in browser_snapshot returns a snapshot
    (delta or full, see there); target_ref is the acted-on element's ref.
    opened_tabs lists tabs the action opened (target=_blank links, window.open) as
    [{tabId, url}]; act on them with tab_id. via="link_fallback" marks a popup-blocked link
    Kitsune opened itself. With expect, the result adds dispatched=true and outcome
    {expected, observed, met}, plus the current tab state when unmet; new_tab is met once the
    action opens a tab, and value_changed compares with the value before dispatch.
    user_active: the user is using the tab (state.reason); nothing ran. Retry in a few seconds.
    """
    body: dict = {"selector": selector} if selector or ref else {"coords": {"x": x, "y": y}}
    return _post_action("/api/click", _request_body(body, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Trusted click", **ACT))
def browser_trusted_click(
    selector: Annotated[str, Field(description="CSS selector; Kitsune clicks its center.")] = "",
    x: Annotated[int, Field(description="X coordinate, used if selector is empty.")] = 0,
    y: Annotated[int, Field(description="Y coordinate, used if selector is empty.")] = 0,
    coordinate_space: Annotated[str, Field(
        description='"viewport" for browser viewport coords or "screen" for desktop coords.')] = "viewport",
    focus: Annotated[bool, Field(
        description="Try to foreground the browser (Zen, Firefox, LibreWolf) first.")] = False,
    tab_id: Annotated[int | None, Field(
        description="Tab to resolve the selector against (viewport coords only). A tab that is not "
                    "shown is shown for the click, then the user's tab is shown again.")] = None,
    confirm_token: str | None = None,
    first: bool = False,
) -> dict:
    """Click using the real OS mouse pointer, for pages that ignore synthetic clicks;
    confirm_token and first work as in browser_check.

    This is slower and moves the user's cursor, so prefer browser_click unless a page
    specifically ignores synthetic clicks. Works on Windows, macOS and Linux (X11);
    macOS needs pyobjc Quartz or cliclick plus Accessibility permission for the bridge,
    and Linux needs xdotool (Wayland is not supported).
    """
    if selector:
        body = {"selector": selector, "focus": focus}
    else:
        body = {"coords": {"x": x, "y": y}, "coordinateSpace": coordinate_space, "focus": focus}
    return _post("/api/trusted-click", _request_body(body, tab_id, confirm_token=confirm_token, first=first))


def browser_type(
    selector: str = "", text: str = "",
    mode: Annotated[Literal["type", "fill", "editable"], Field(
        description="type: typing events, clearing first if clear. fill: replace the value via native "
                    "setters and input/change events, or an editor's content. editable: insert plain text into an "
                    "editor/input/textarea, appending unless clear. HTML: browser_js.")] = "type",
    clear: bool = False, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, wait: bool = False, timeout: int = 10000,
    expect: dict | None = None, confirm_token: str | None = None, first: bool = False,
) -> dict:
    """Enter text into a field or contenteditable editor. ok=true means dispatched; verify
    with expect (see browser_check)."""
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
    return _post_action(endpoint, _request_body(body, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Scroll", **ACT))
def browser_scroll(
    direction: Annotated[str, Field(description="up, down, left, right, top or bottom.")] = "down",
    amount: Annotated[int, Field(description="Pixels (ignored for top/bottom).")] = 500,
    tab_id: TabId = None,
    selector: str | None = None, ref: str | None = None, element: str | None = None,
    expect: dict | None = None, first: bool = False,
) -> dict:
    """Scroll the selector/ref container instantly, else the window (viewport-center fallback).
    ok means handled, not moved: check scrolled_by (signed pixels actually moved; 0 is a
    no-op) or verify with expect (see browser_check).

    Also returns target ("window" or the container's selector) and window scrollY/scrollX.
    scrolledPx is an alias of scrolled_by.
    """
    body = {"direction": direction, "amount": amount}
    if selector: body["selector"] = selector
    return _post_action("/api/scroll", _request_body(body, tab_id, ref, element, first=first), expect=expect)


def browser_hover(
    selector: str = "", tab_id: TabId = None, ref: str | None = None, element: str | None = None,
    wait: bool = False, timeout: int = 10000, expect: dict | None = None, first: bool = False,
) -> dict:
    """Hover over an element on the page. ok=true means dispatched; verify with expect
    (see browser_check)."""
    return _post_action("/api/hover", _request_body({"selector": selector}, tab_id, ref, element, first=first),
                        wait, timeout, expect)


# -- Smart Queries ------------------------------------------------

def browser_find(
    query: Annotated[str, Field(description='e.g. "login button", "search bar", "email input".')],
    tab_id: TabId = None,
) -> dict:
    """Find elements on the page using natural language description."""
    return _post("/api/find", _request_body({"query": query}, tab_id))


def browser_js(
    code: Annotated[str, Field(
        description='e.g. "document.title" or "document.querySelectorAll(\'a\').length".')],
    tab_id: TabId = None,
    full: Annotated[bool, Field(
        description="Results are capped at 8 MB (50 KB on extensions older than 2.1.0). True runs the "
                    "code once and reads the result back in slices, so any size comes back complete.")] = False,
    confirm_token: str | None = None,
) -> dict:
    """Execute JavaScript in the current page context. Needs the developer extension build;
    the signed build returns an error (browser_status reports extension_build).
    confirm_token: see browser_check."""
    body = _request_body({"code": code}, confirm_token=confirm_token)
    if full:
        body["full"] = True
        return _post("/api/js", _request_body(body, tab_id), timeout=180)
    return _post("/api/js", _request_body(body, tab_id))


@tool("inspect", annotations=dict(title="Highlight", **ACT))
def browser_highlight(selector: str = "", tab_id: TabId = None, ref: str | None = None, element: str | None = None) -> dict:
    """Highlight an element (ref/selector, see browser_check) with a visual overlay."""
    return _post("/api/highlight", _request_body({"selector": selector}, tab_id, ref, element))


# -- Wait ---------------------------------------------------------

def browser_handle_dialog(
    accept: Annotated[bool | None, Field(
        description="Auto-accept (true) or dismiss (false) future dialogs; null clears this tab's "
                    "policy and restores native dialogs. True also disarms beforeunload like "
                    "browser_tabs close with force; false leaves native beforeunload intact.")] = True,
    prompt_text: Annotated[str | None, Field(
        description="Answer for accepted prompts (default: the prompt's default value).")] = None,
    tab_id: TabId = None,
    confirm_token: str | None = None,
) -> dict:
    """Set this tab's policy for future dialogs and return recent recorded dialogs; by
    default dialogs use native UI and record the user's actual response.

    Dialogs answer synchronously; this cannot change an already returned answer.
    History contains the last 100 dialogs in the current document. Policies survive
    navigation, with native behavior until the policy is restored asynchronously.
    Native dialogs and scripts holding original functions cannot be answered.
    """
    return _post("/api/handle-dialog", _request_body(
        {"accept": accept, "prompt_text": prompt_text}, tab_id, confirm_token=confirm_token))


def browser_wait_for(
    selector: str | None = None, text: str | None = None,
    text_gone: str | None = None, js: str | None = None,
    url: str | None = None, title: str | None = None,
    network_idle: bool = False, time: float | None = None,
    timeout: int = 10000, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None,
    confirm_token: Annotated[str | None, Field(description="js waits; see browser_check.")] = None,
) -> dict:
    """Wait for exactly one condition, or sleep for time seconds: selector/ref (a visible
    element, see browser_check; ref wins), text/text_gone (rendered page text, literal
    substring; placeholders and aria-labels shown as snapshot names are not matched), js
    (a non-empty result, including false/0; developer extension build only),
    url/title (JavaScript regex on tab metadata, so it survives navigation) or network_idle
    (500 ms without new resources). timeout is the maximum wait in ms; it does not limit time.

    Other conditions run in the current document and should be started after navigation.
    Mixed conditions are rejected; wait for URL/title first, then the desired document
    condition.
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
        _request_body(body, ref=ref, element=element)
    elif text is not None or text_gone is not None:
        if (text if text is not None else text_gone) == "":
            return {"error": "text/text_gone must be non-empty"}
        endpoint = "/api/wait-for-text"
        body.update(text=text, text_gone=text_gone)
    elif js is not None:
        endpoint = "/api/wait-for-result"
        body.update(code=js, pollInterval=500)
        _request_body(body, confirm_token=confirm_token)
    elif url is not None or title is not None:
        endpoint = "/api/wait-for-url" if url is not None else "/api/wait-for-title"
        body["pattern"] = url if url is not None else title
    else:
        endpoint = "/api/wait-for-network-idle"
        body["idleMs"] = 500
    return _post(endpoint, _request_body(body, tab_id), timeout=timeout / 1000 + 10)


@tool("automation", annotations=dict(title="List workflows", **READ))
def browser_workflows() -> dict:
    """List available named Kitsune workflows from the bridge workflow directory."""
    return _get("/api/workflows")


@tool("automation", annotations=dict(title="Run a named workflow", **DESTRUCTIVE))
def browser_workflow(
    name: Annotated[str, Field(description="Workflow file name without .md.")],
    variables: Annotated[dict | None, Field(description="Template variables for the workflow.")] = None,
    tab_id: DefaultTabId = None,
) -> dict:
    """Execute a named Kitsune workflow from the bridge workflow directory."""
    return _post("/api/workflow", _default_tab({"name": name, "variables": variables or {}}, tab_id),
                 timeout=container_request_seconds(WORKFLOW_DEADLINE_SECONDS))


@tool("admin", annotations=dict(title="Manage response cache", **DESTRUCTIVE))
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

@tool("tabs", annotations=dict(title="Wake tab", **IDEMPOTENT_ACT))
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


@tool("tabs", annotations=dict(title="Keep alive", **IDEMPOTENT_ACT))
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


@tool("tabs", annotations=dict(title="Keep alive stop", **IDEMPOTENT_ACT))
def browser_keep_alive_stop(
    tab_ids: Annotated[list[int] | None, Field(description="Tabs to stop pinging. Omit to stop all.")] = None,
) -> dict:
    """Stop the keep-alive pinger for some or all tabs."""
    body: dict = {}
    if tab_ids is not None:
        body["tabIds"] = tab_ids
    return _post("/api/keep-alive-stop", body)


@tool("admin", annotations=dict(title="Reload extension", **ACT))
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


@tool("automation", annotations=dict(title="Run parallel commands", **DESTRUCTIVE))
def browser_parallel(
    sequences: Annotated[list[list[dict]], Field(
        description='Command sequences, each a list of browser_batch-shaped commands, e.g. '
                    '[[{"action": "navigate", "url": "...", "tabId": 12}, {"action": "waitForElement", '
                    '"selector": "h1", "tabId": 12}, {"action": "pageText", "tabId": 12}], '
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

@tool("read", annotations=dict(title="Query page elements", **READ))
def browser_query(
    selector: str,
    fields: Annotated[list[str] | None, Field(
        description="Field names to extract per element. Defaults to a useful set.")] = None,
    limit: Annotated[int, Field(description="Max elements to return (capped at 500).")] = 50,
    tab_id: TabId = None,
) -> dict:
    """Extract multiple fields from every element matching a CSS selector.

    Much faster and cheaper than calling browser_evaluate per element. The default
    fields cover the common ones (text, href, value, src, alt, id, name);
    pass `fields=["text","data-foo","bounds","attrs"]` to get arbitrary
    attributes or `bounds`/`attrs`/`html` extras.
    """
    body = {"selector": selector, "limit": limit}
    if fields: body["fields"] = fields
    return _post("/api/query", _request_body(body, tab_id))


@tool("read", annotations=dict(title="Read element HTML", **READ))
def browser_html(
    selector: str = "", tab_id: TabId = None,
    ref: str | None = None, element: str | None = None,
) -> dict:
    """Return the outerHTML of an element (ref/selector, see browser_check). Cap 100KB."""
    return _post("/api/html", _request_body({"selector": selector}, tab_id, ref, element))


@tool("read", annotations=dict(title="Read page links", **READ))
def browser_links(internal_only: bool = False, tab_id: TabId = None) -> dict:
    """Return every anchor with href + text. Useful as a crawler primitive."""
    return _post("/api/links", _request_body({"internalOnly": internal_only}, tab_id))


@tool("read", annotations=dict(title="Read page images", **READ))
def browser_images(tab_id: TabId = None) -> dict:
    """Return every <img> with src, alt, and natural dimensions."""
    return _post("/api/images", _request_body({}, tab_id))


@tool("read", annotations=dict(title="Read page metadata", **READ))
def browser_meta(tab_id: TabId = None) -> dict:
    """Return all meta tags + link rels (canonical, RSS, icons, etc.)."""
    return _post("/api/meta", _request_body({}, tab_id))


@tool("read", annotations=dict(title="Read structured page data", **READ))
def browser_structured_data(tab_id: TabId = None) -> dict:
    """Extract JSON-LD blocks, OpenGraph (og:*), and Twitter card meta."""
    return _post("/api/structured-data", _request_body({}, tab_id))


@tool("inspect", annotations=dict(title="Read element bounds", **READ))
def browser_bounds(
    selector: str = "", tab_id: TabId = None,
    ref: str | None = None, element: str | None = None,
) -> dict:
    """Return getBoundingClientRect + viewport/page sizes for an element (ref/selector, see
    browser_check)."""
    return _post("/api/bounds", _request_body({"selector": selector}, tab_id, ref, element))


@tool("inspect", annotations=dict(title="Computed style", **READ))
def browser_computed_style(
    selector: str = "", properties: list[str] | None = None, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None,
) -> dict:
    """Return computed CSS for an element (ref/selector, see browser_check). Defaults to the
    layout-relevant set."""
    body = {"selector": selector}
    if properties: body["properties"] = properties
    return _post("/api/computed-style", _request_body(body, tab_id, ref, element))


@tool("read", annotations=dict(title="Extract readable article", **READ))
def browser_readability(tab_id: TabId = None) -> dict:
    """Extract the main article on the page (title, byline, clean text).

    Heuristic: score candidate containers (`article`, `main`, `.post`, etc.)
    by text density × paragraph count × inverse link density, strip nav/aside,
    return the winner. Falls back to densest section/div, then body.
    """
    return _post("/api/readability", _request_body({}, tab_id))


@tool("read", annotations=dict(title="Read page as Markdown", **READ))
def browser_markdown(
    selector: str | None = None, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None,
) -> dict:
    """Convert the page (or a selector/ref subtree, see browser_check) to Markdown. Cap 80KB."""
    body = {}
    if selector: body["selector"] = selector
    return _post("/api/markdown", _request_body(body, tab_id, ref, element))


@tool("read", annotations=dict(title="List page iframes", **READ))
def browser_iframes(tab_id: TabId = None) -> dict:
    """List all iframes with src + same-origin/accessible flags."""
    return _post("/api/iframes", _request_body({}, tab_id))


@tool("inspect", annotations=dict(title="Explain selector", **READ))
def browser_explain_selector(selector: str, tab_id: TabId = None) -> dict:
    """Diagnose a CSS selector: match count, sample matches, suggestions for
    a more-specific selector if you got too many."""
    return _post("/api/explain-selector", _request_body({"selector": selector}, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Interaction additions
# ════════════════════════════════════════════════════════════════════

def browser_select_option(
    selector: str = "", value: str = "", by_text: bool = False, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, wait: bool = False, timeout: int = 10000,
    expect: dict | None = None, confirm_token: str | None = None, first: bool = False,
) -> dict:
    """Choose an <option> in a <select>. Default matches by value; pass
    `by_text=True` to match by visible text. ok=true means dispatched; verify with expect
    (see browser_check).
    """
    return _post_action("/api/select-option", _request_body({"selector": selector, "value": value, "byText": by_text}, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Check", **ACT))
def browser_check(
    selector: Annotated[str, Field(description='e.g. "#agree", "input[name=plan]".')] = "",
    checked: bool = True, tab_id: TabId = None,
    ref: Annotated[str | None, Field(description="Prefer a ref from browser_snapshot over a selector.")] = None,
    element: Annotated[str | None, Field(description="Audit-only description of the target.")] = None,
    wait: Annotated[bool, Field(
        description="Wait up to timeout ms for the selector/ref target to exist first (pointer actions: also "
                    "rendered); on timeout nothing ran (not_found, not_actionable).")] = False,
    timeout: int = 10000,
    expect: Annotated[dict | None, Field(
        description='Outcome to verify after dispatch, exactly one of {"url": regex}, {"url_changed": true}, '
                    '{"new_tab": true}, {"selector": css} (visible; add "gone": true, "value": str or '
                    '"value_changed": true), {"text": str} or {"text_gone": str}, plus "timeout" ms (default '
                    '5000). Unmet: ok=false, code="outcome_not_observed"; the action already ran, so do not '
                    'blindly repeat it.')] = None,
    confirm_token: Annotated[str | None, Field(
        description="From a confirmation_required error, after the user approved this action.")] = None,
    first: Annotated[bool, Field(
        description="A write tool's selector matching several elements fails with ambiguous_target "
                    "unless true; true acts on the first match.")] = False,
) -> dict:
    """Set a checkbox or radio's checked state; with wait, hidden native inputs need not render.
    ok=true only means dispatched: verify with expect; tabs the action opens come back in
    opened_tabs.

    Action contract: the other Kitsune action tools' tab_id, ref, element, wait, expect,
    confirm_token and first work as here (the core browser_* tools follow Playwright MCP
    instead). A successful action returns a snapshot (delta or full); target_ref is the
    acted-on element's ref. opened_tabs lists tabs the action opened as [{tabId, url}]; act
    on them with tab_id. With expect, the result adds dispatched=true and outcome
    {expected, observed, met}, plus the current tab state when unmet; new_tab is met once the
    action opens a tab, and value_changed compares with the value before dispatch.
    user_active: the user is using the tab (state.reason); nothing ran. Retry in a few seconds.
    """
    return _post_action("/api/check", _request_body({"selector": selector, "checked": checked}, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Focus", **ACT))
def browser_focus(
    selector: str = "", tab_id: TabId = None, ref: str | None = None, element: str | None = None,
    first: bool = False,
) -> dict:
    """Focus an element (ref/selector, see browser_check)."""
    return _post("/api/focus", _request_body({"selector": selector}, tab_id, ref, element, first=first))


@tool("browse", annotations=dict(title="Blur", **ACT))
def browser_blur(
    selector: str | None = None, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, first: bool = False,
) -> dict:
    """Blur an element (ref/selector, see browser_check), else the active element."""
    body = {}
    if selector: body["selector"] = selector
    return _post("/api/blur", _request_body(body, tab_id, ref, element, first=first))


def browser_keypress(
    key: Annotated[str, Field(
        description="Characters, Space/Spacebar, Enter/Return, Escape/Esc, ArrowUp/Down/Left/Right "
                    "(or Up/Down/Left/Right), Tab, Backspace.")],
    selector: str | None = None,
    modifiers: Annotated[dict | None, Field(description='{"ctrl"|"shift"|"alt"|"meta": true}.')] = None,
    text: Annotated[str | None, Field(description="Also insert this text into an editable target.")] = None,
    tab_id: TabId = None,
    ref: str | None = None,
    element: str | None = None,
    wait: bool = False,
    timeout: int = 10000,
    expect: dict | None = None, confirm_token: str | None = None, first: bool = False,
) -> dict:
    """Press a key on the ref/selector target (wait needs one), else the focused element (or
    body). In an input, textarea or editor the key applies its default edit unless the page
    cancels it: characters type, Backspace/Delete delete, arrows/Home/End move the caret
    (Shift selects), Enter adds a line or submits a text input's form. effect names what
    changed (null: nothing); verify outcomes with expect (see browser_check).
    """
    body = {"key": key}
    if selector: body["selector"] = selector
    if modifiers: body["modifiers"] = modifiers
    if text is not None: body["text"] = text
    return _post_action("/api/keypress", _request_body(body, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


def browser_double_click(
    selector: str = "", tab_id: TabId = None, ref: str | None = None, element: str | None = None,
    wait: bool = False, timeout: int = 10000, expect: dict | None = None, confirm_token: str | None = None,
    first: bool = False,
) -> dict:
    """Dispatch a synthetic double-click on the matched element. ok=true means dispatched;
    verify with expect (see browser_check).
    """
    return _post_action("/api/double-click", _request_body({"selector": selector}, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Submit form", **DESTRUCTIVE))
def browser_submit_form(
    selector: str | None = None, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, expect: dict | None = None, confirm_token: str | None = None,
    first: bool = False,
) -> dict:
    """Submit the target form (or the target element's enclosing form).
    Without selector/ref: the form of the field last filled or focused, else the only
    form (several: ambiguous_target). Result form names it. ok=true means dispatched;
    verify with expect (see browser_check). On a failure with retryable=false, inspect the
    page before submitting again.
    """
    body = {}
    if selector: body["selector"] = selector
    return _post_action("/api/submit-form", _request_body(body, tab_id, ref, element, confirm_token, first),
                        expect=expect)


def browser_form_fill(
    fields: Annotated[dict, Field(description='e.g. {"email": "foo@bar", "Password": "...", "#submit-target": true}')],
    tab_id: TabId = None, expect: dict | None = None, confirm_token: str | None = None,
    first: bool = False,
) -> dict:
    """Fill many fields via fuzzy matching; ambiguous keys fail unless first=True.
    ok=true means dispatched; verify with expect (see browser_check).

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
    return _post_action("/api/form-fill", _request_body({"fields": fields}, tab_id, confirm_token=confirm_token, first=first),
                        expect=expect)


def browser_drag(
    from_selector: str = "", to_selector: str = "", tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, to_ref: str | None = None,
    wait: bool = False, timeout: int = 10000, expect: dict | None = None, confirm_token: str | None = None,
    first: bool = False,
) -> dict:
    """Synthetic drag-and-drop from one element to another: HTML5 drag events, then a
    pointer/mouse drag unless the drop target accepted them (result via: html5|pointer).
    ref identifies the source; to_ref identifies the destination; wait covers both.
    ok=true means dispatched; verify with expect (see browser_check).
    """
    return _post_action("/api/drag", _request_body({"from": from_selector, "to": to_selector, "toRef": to_ref}, tab_id, ref, element, confirm_token, first),
                        wait, timeout, expect)


@tool("browse", annotations=dict(title="Click and wait navigation", **ACT))
def browser_click_and_wait_navigation(
    selector: str = "", timeout: int = 15000, tab_id: TabId = None,
    ref: str | None = None, element: str | None = None, confirm_token: str | None = None, first: bool = False,
) -> dict:
    """Click an element (ref/selector, see browser_check), then wait for the resulting navigation.

    Atomic operation — far more reliable than `browser_click` followed by
    `browser_wait_for_url` for links/submit buttons. Listens for
    tabs.onUpdated(status='complete')."""
    return _post(
        "/api/click-and-wait-navigation",
        _request_body({"selector": selector, "timeout": timeout}, tab_id, ref, element, confirm_token, first),
        timeout=(timeout / 1000) + 10,
    )


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Visual additions
# ════════════════════════════════════════════════════════════════════

def browser_upload_file(
    path: Annotated[str | list[str], Field(
        description="Absolute file path, or a list of paths for multi-file inputs.")],
    selector: Annotated[str | None, Field(
        description="The file input, an element containing it, or a drop zone. Omit to use the only "
                    "input[type=file] (first=True opts into the first).")] = None,
    mode: Annotated[str, Field(description='"auto" (input if found, else drop), "input", or "drop".')] = "auto",
    tab_id: Annotated[int | None, Field(
        description="Default: active tab. Pass it for anything big - the upload streams in ~384 KB "
                    "chunks and must keep hitting the same tab.")] = None,
    confirm_token: str | None = None, first: bool = False,
) -> dict:
    """Attach local files to a page's upload control without a dialog; returns {ok, mode,
    files: [{name, size, type}], chunks, bytes, transport, seconds}.

    The bridge reads the file from disk and streams it into the page, which builds
    real File objects and sets them on the <input type=file> (firing input + change).
    If the selector is a drop zone with no input inside, it fires dragenter/dragover/drop.
    """
    body = {"paths": path if isinstance(path, list) else [path], "mode": mode}
    if selector:
        body["selector"] = selector
    return _post("/api/upload-file", _request_body(body, tab_id, confirm_token=confirm_token, first=first), timeout=320)


def _screenshot_result(r: dict, save_to: str | None, data_url: bool):
    """Return an Image and the metadata by default; explicit file/raw modes retain metadata only."""
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


@tool("inspect", annotations=dict(title="Full page metrics", **READ))
def browser_full_page_metrics(tab_id: TabId = None) -> dict:
    """Return document/viewport sizes + scroll position + devicePixelRatio."""
    return _post("/api/full-page-metrics", _request_body({}, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Tab and window management
# ════════════════════════════════════════════════════════════════════

@tool("tabs", annotations=dict(title="Pin tab", **IDEMPOTENT_ACT))
def browser_pin_tab(pinned: bool = True, tab_id: TabId = None) -> dict:
    """Pin or unpin a tab."""
    return _post("/api/pin-tab", _request_body({"pinned": pinned}, tab_id))


@tool("tabs", annotations=dict(title="Mute tab", **IDEMPOTENT_ACT))
def browser_mute_tab(muted: bool = True, tab_id: TabId = None) -> dict:
    """Mute or unmute a tab."""
    return _post("/api/mute-tab", _request_body({"muted": muted}, tab_id))


@tool("tabs", annotations=dict(title="Duplicate tab", **ACT))
def browser_duplicate_tab(tab_id: TabId = None) -> dict:
    """Duplicate a tab. Returns the new tab id."""
    return _post("/api/duplicate-tab", _request_body({}, tab_id))


@tool("tabs", annotations=dict(title="Reload tab", **ACT))
def browser_reload_tab(bypass_cache: bool = False, tab_id: TabId = None, expect: dict | None = None) -> dict:
    """Reload a tab. Pass `bypass_cache=True` for a hard reload.
    ok=true means dispatched; verify with expect (see browser_check)."""
    return _post_action("/api/reload-tab", _request_body({"bypassCache": bypass_cache}, tab_id), expect=expect)


def browser_back(tab_id: TabId = None, expect: dict | None = None) -> dict:
    """Go back in history (browser back button). ok=true means dispatched; verify with
    expect (see browser_check)."""
    return _post_action("/api/back", _request_body({}, tab_id), expect=expect)


@tool("browse", annotations=dict(title="Forward", **ACT))
def browser_forward(tab_id: TabId = None, expect: dict | None = None) -> dict:
    """Go forward in history. ok=true means dispatched; verify with expect (see browser_check)."""
    return _post_action("/api/forward", _request_body({}, tab_id), expect=expect)


@tool("tabs", annotations=dict(title="Get zoom", **READ))
def browser_get_zoom(tab_id: TabId = None) -> dict:
    """Return the current zoom factor (1.0 = 100%)."""
    return _post("/api/get-zoom", _request_body({}, tab_id))


@tool("tabs", annotations=dict(title="Set zoom", **IDEMPOTENT_ACT))
def browser_set_zoom(factor: float, tab_id: TabId = None) -> dict:
    """Set zoom factor. 0.3 to 5.0 typical; 1.0 = 100%."""
    return _post("/api/set-zoom", _request_body({"factor": factor}, tab_id))


@tool("tabs", annotations=dict(title="List browser windows", **READ))
def browser_windows() -> dict:
    """List all open browser windows with their tabs."""
    return _post("/api/windows", {})


@tool("tabs", annotations=dict(title="Create window", **ACT))
def browser_create_window(url: str | None = None, incognito: bool = False,
                          confirm_token: str | None = None) -> dict:
    """Open a new browser window. Pass a URL to load it at start."""
    body = {"incognito": incognito}
    if url: body["url"] = url
    return _post("/api/create-window", _request_body(body, confirm_token=confirm_token))


@tool("tabs", annotations=dict(title="Close window", **DESTRUCTIVE))
def browser_close_window(window_id: int, confirm_token: str | None = None) -> dict:
    """Close a browser window by ID (from browser_windows); one with tabs the agent did not
    open is refused (code user_tab) unless the user approves (confirm_token)."""
    return _post("/api/close-window", _request_body({"windowId": window_id}, confirm_token=confirm_token))


@tool("tabs", annotations=dict(title="Focus window", **IDEMPOTENT_ACT))
def browser_focus_window(window_id: int) -> dict:
    """Bring a browser window to the foreground; this moves the user's focus, so only when asked."""
    return _post("/api/focus-window", {"windowId": window_id})


@tool("tabs", annotations=dict(title="Move tab", **ACT))
def browser_move_tab(tab_id: Annotated[int, Field(description="The tab to move.")],
                     window_id: int | None = None, index: int = -1) -> dict:
    """Move a tab to a position (and optionally to a different window). -1 = end."""
    body = {"tabId": tab_id, "index": index}
    if window_id is not None: body["windowId"] = window_id
    return _post("/api/move-tab", body)


@tool("tabs", annotations=dict(title="Detach tab", **ACT))
def browser_detach_tab(tab_id: Annotated[int, Field(description="The tab to pop out.")]) -> dict:
    """Pop a tab out into its own new window."""
    return _post("/api/detach-tab", {"tabId": tab_id})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Network, cookies, storage, clipboard, downloads
# ════════════════════════════════════════════════════════════════════

@tool("storage", annotations=dict(title="Manage cookies", **DESTRUCTIVE))
def browser_cookies(
    op: str,
    url: str | None = None,
    name: str | None = None,
    value: str | None = None,
    domain: str | None = None,
    path: str | None = None,
    secure: bool = False,
    confirm_token: str | None = None,
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
    return _post("/api/cookies", _request_body(body, confirm_token=confirm_token))


@tool("storage", annotations=dict(title="Manage page storage", **DESTRUCTIVE))
def browser_storage(
    kind: Annotated[str, Field(description='"local" or "session".')],
    op: Annotated[str, Field(description='"get" | "set" | "remove" | "clear" | "list" | "snapshot" | "restore".')],
    key: Annotated[str | None, Field(description="Required for get/set/remove.")] = None,
    value: Annotated[str | None, Field(
        description="For set, the string value. For restore, a dict of key→value.")] = None,
    tab_id: Annotated[int | None, Field(
        description="Tab to run against. Storage is per-origin, so prefer a tab already at the "
                    "right origin.")] = None,
    confirm_token: str | None = None,
) -> dict:
    """localStorage / sessionStorage ops."""
    body = {"kind": kind, "op": op}
    if key is not None: body["key"] = key
    if value is not None: body["value"] = value
    return _post("/api/storage", _request_body(body, tab_id, confirm_token=confirm_token))


@tool("storage", annotations=dict(title="Read or write clipboard", **DESTRUCTIVE))
def browser_clipboard(
    op: Annotated[str, Field(description='"read" or "write".')],
    text: Annotated[str | None, Field(description='Text to write when op="write".')] = None,
    confirm_token: str | None = None,
) -> dict:
    """Read or write the system clipboard."""
    body = {"op": op}
    if text is not None: body["text"] = text
    return _post("/api/clipboard", _request_body(body, confirm_token=confirm_token))


@tool("network", annotations=dict(title="Manage downloads", **DESTRUCTIVE))
def browser_downloads(
    op: Annotated[str, Field(description='"download" | "list" | "cancel".')],
    url: Annotated[str | None, Field(description='For "download", the URL to fetch.')] = None,
    filename: Annotated[str | None, Field(description="Output filename.")] = None,
    query: Annotated[dict | None, Field(description='For "list", a downloads.search query dict.')] = None,
    confirm_token: str | None = None,
) -> dict:
    """Downloads operations."""
    body = {"op": op}
    if url: body["url"] = url
    if filename: body["filename"] = filename
    if query: body["query"] = query
    return _post("/api/downloads", _request_body(body, confirm_token=confirm_token))


@tool("storage", annotations=dict(title="Clear browsing data", **DESTRUCTIVE))
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


@tool("network", annotations=dict(title="Manage request interception", **DESTRUCTIVE))
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


@tool("network", annotations=dict(title="Manage network capture", **DESTRUCTIVE))
def browser_capture_network(
    op: Annotated[str, Field(
        description='"start" begins capturing fresh, "stop" pauses, "read" returns the buffer, "clear" '
                    'empties it.')] = "read",
    since: Annotated[int, Field(
        description="When reading, only return entries with startTime >= since (ms).")] = 0,
    tab_id: TabId = None,
) -> dict:
    """Capture network requests via the page's PerformanceObserver."""
    return _post("/api/capture-network", _request_body({"op": op, "since": since}, tab_id))


def browser_watch_console(enabled: bool = True, tab_id: TabId = None) -> dict:
    """Toggle console capture on a tab. Once enabled, browser console
    messages (log/info/warn/error/debug + window errors) are buffered up to
    500 entries; retrieve via browser_console_logs."""
    return _post("/api/watch-console", _request_body({"enabled": enabled}, tab_id))


def browser_console_logs(
    since: int | None = None, level: str | None = None, tab_id: TabId = None
) -> dict:
    """Return buffered console logs from a tab (requires browser_watch_console first)."""
    body = {}
    if since is not None: body["since"] = since
    if level: body["level"] = level
    return _post("/api/console-logs", _request_body(body, tab_id))


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Orchestration
# ════════════════════════════════════════════════════════════════════

@tool("automation", annotations=dict(title="Broadcast", **DESTRUCTIVE))
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


@tool("automation", annotations=dict(title="Wait for JavaScript across tabs", **DESTRUCTIVE))
def browser_sync_barrier(
    tab_ids: Annotated[list[int], Field(description="Tabs to monitor.")],
    predicate: Annotated[str, Field(
        description="JS expression that returns truthy when the tab is ready, e.g. "
                    "\"!!document.querySelector('.results')\".")],
    timeout: Annotated[int, Field(description="Overall timeout in ms.")] = 30000,
    poll_interval: Annotated[int, Field(description="Per-poll interval in ms.")] = 300,
    confirm_token: str | None = None,
) -> dict:
    """Block until a JS predicate returns truthy on every listed tab.

    Replaces hand-rolled "wait for everything to be ready" loops.
    Needs the developer extension build.
    """
    return _post(
        "/api/sync-barrier",
        _request_body({"tabIds": tab_ids, "predicate": predicate, "timeout": timeout, "pollInterval": poll_interval},
                      confirm_token=confirm_token),
        timeout=(timeout / 1000) + 10,
    )


@tool("tabs", annotations=dict(title="Tag tab", **IDEMPOTENT_ACT))
def browser_tag_tab(name: str, tab_id: Annotated[int, Field(description="The tab to name.")]) -> dict:
    """Give a tab a memorable name. Refer to it later via browser_resolve_tag.
    Names survive bridge restarts only if you re-tag — they live in memory."""
    return _post("/api/tag-tab", {"name": name, "tabId": tab_id})


@tool("tabs", annotations=dict(title="Resolve tag", **READ))
def browser_resolve_tag(name: str) -> dict:
    """Look up the tab id for a tag set via browser_tag_tab."""
    return _post("/api/resolve-tag", {"name": name})


@tool("tabs", annotations=dict(title="List tags", **READ))
def browser_list_tags() -> dict:
    """List all tag → tab id mappings."""
    return _post("/api/list-tags", {})


@tool("tabs", annotations=dict(title="Untag tab", **IDEMPOTENT_ACT))
def browser_untag_tab(name: str) -> dict:
    """Remove a tag."""
    return _post("/api/untag-tab", {"name": name})


@tool("automation", annotations=dict(title="Resize browser tab pool", **DESTRUCTIVE))
def browser_tab_pool(size: int, url: str = "about:blank") -> dict:
    """Maintain a warm pool of N tabs always ready to be acquired.

    Grows or shrinks to `size`. Acquire/release via browser_pool_acquire and
    browser_pool_release. Auto-grows by one if you acquire from an empty pool.
    """
    return _post("/api/tab-pool", {"size": size, "url": url})


@tool("automation", annotations=dict(title="Acquire a pooled tab", **ACT))
def browser_pool_acquire() -> dict:
    """Check out a warm tab from the pool. Returns its tab_id."""
    return _post("/api/pool-acquire", {})


@tool("automation", annotations=dict(title="Release a pooled tab", **IDEMPOTENT_ACT))
def browser_pool_release(tab_id: Annotated[int, Field(description="The pooled tab to return.")]) -> dict:
    """Return a tab to the pool for reuse."""
    return _post("/api/pool-release", {"tabId": tab_id})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Session save/restore (auth-once-use-many)
# ════════════════════════════════════════════════════════════════════

@tool("storage", annotations=dict(title="Save session", **DESTRUCTIVE))
def browser_save_session(
    name: Annotated[str, Field(
        description="A label for the session (becomes filename under ~/claude-zen-sessions/).")],
    urls: Annotated[list[str] | None, Field(
        description="Origins whose cookies + localStorage to snapshot. Each gets opened briefly in a "
                    "temp tab so localStorage can be read.")] = None,
    confirm_token: str | None = None,
) -> dict:
    """Snapshot cookies (and per-origin localStorage if urls given) to disk.

    Use case: log into a service interactively once, save the session,
    then have parallel agents load it for authenticated work.
    """
    return _post("/api/save-session", _request_body({"name": name, "urls": urls or []}, confirm_token=confirm_token), timeout=120)


@tool("storage", annotations=dict(title="Load session", **DESTRUCTIVE))
def browser_load_session(name: str, confirm_token: str | None = None) -> dict:
    """Restore cookies + localStorage from a previously saved session."""
    return _post("/api/load-session", _request_body({"name": name}, confirm_token=confirm_token), timeout=120)


@tool("storage", annotations=dict(title="List sessions", **READ))
def browser_list_sessions() -> dict:
    """List saved sessions with their metadata."""
    return _post("/api/list-sessions", {})


@tool("storage", annotations=dict(title="Delete session", **DESTRUCTIVE))
def browser_delete_session(name: str) -> dict:
    """Delete a saved session file."""
    return _post("/api/delete-session", {"name": name})


# ════════════════════════════════════════════════════════════════════
#  1.4.0 — Observability and policy
# ════════════════════════════════════════════════════════════════════

@tool("admin", annotations=dict(title="Check browser health", **READ))
def browser_health() -> dict:
    """Extended health/status: bridge version, ports, keep-alive list,
    pool state, tags, policy, sessions dir, audit/log sizes."""
    return _post("/api/health", {})


@tool("admin", annotations=dict(title="Read bridge logs", **READ))
def browser_logs(limit: int = 200, since: str | None = None) -> dict:
    """Return recent bridge log lines (in-memory ring buffer)."""
    body = {"limit": limit}
    if since: body["since"] = since
    return _post("/api/logs", body)


@tool("admin", annotations=dict(title="Read command audit log", **READ))
def browser_audit(limit: int = 100, since_ms: int = 0) -> dict:
    """Return recent command audit entries: action, params (JS code stripped,
    typed text, select values, dialog prompt answers and cookie/storage/
    clipboard values redacted), success flag,
    error, duration. Useful for "what did the agent do?" reviews."""
    return _post("/api/audit", {"limit": limit, "since": since_ms})


@tool("automation", annotations=dict(title="Read scheduler state", **READ))
def browser_scheduler() -> dict:
    """Return bridge scheduler state for multi-agent/tab work.

    Shows active per-tab/global command scopes, pending WebSocket commands, and
    known tab locks. Useful when several agents are driving separate tabs.
    """
    return _post("/api/scheduler", {})


@tool("admin", annotations=dict(title="Set policy", **DESTRUCTIVE))
def browser_set_policy(
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    readonly: bool | None = None,
    ask: bool | None = None,
    ask_urls: list[str] | None = None,
    history_allow_domains: list[str] | None = None,
    history_deny_domains: list[str] | None = None,
    history_max_days: int | None = None,
) -> dict:
    """Set URL allow/deny regex lists, read-only mode, ask-before-act mode, and/or the history scope.

    Read-only mode blocks write actions (click/type/navigate/cookies set/etc.), js and
    batch if/while conditions, while still allowing read ops. Useful for "let the agent
    observe only."

    Ask mode (ask=True) holds sensitive actions without running them: form submits,
    submit-control or pay/order-labeled button clicks, Enter in a form field, browser_evaluate and
    other agent JavaScript, cookies, sessions, page storage, clipboard, downloads, navigation
    to ask_urls (payment-like regexes, case-insensitive), and page actions on such pages. A held call returns code "confirmation_required" with
    state.confirmToken; ask the user, then repeat the identical call with confirm_token
    within 120s. The token is single-use, bound to that action, its parameters and tab, and
    is never honored inside batch/retry/parallel/broadcast/workflows. A bridge started with
    KITSUNE_ASK_BEFORE_ACT=1 locks ask mode on, also after restarts; then ask=False or
    dropping ask_urls fails.

    History scope (history tools; docs/api-reference.md#history-and-bookmarks): domain lists
    (a domain covers its subdomains, deny wins) and the time window in days. These can only
    tighten: add denies, narrow the allow list, lower the days; loosening fails with
    policy_locked. History results also stay filtered by every URL allow/deny list set before.
    """
    body = {}
    if allow is not None: body["allow"] = allow
    if deny is not None: body["deny"] = deny
    if readonly is not None: body["readonly"] = readonly
    if ask is not None: body["ask"] = ask
    if ask_urls is not None: body["askUrls"] = ask_urls
    if history_allow_domains is not None: body["historyAllowDomains"] = history_allow_domains
    if history_deny_domains is not None: body["historyDenyDomains"] = history_deny_domains
    if history_max_days is not None: body["historyMaxDays"] = history_max_days
    return _post("/api/set-policy", body)


@tool("admin", annotations=dict(title="Get policy", **READ))
def browser_get_policy() -> dict:
    """Return the active URL/readonly/ask/history policy."""
    return _post("/api/get-policy", {})


# ════════════════════════════════════════════════════════════════════
#  History and bookmarks (#140) — docs/api-reference.md#history-and-bookmarks
# ════════════════════════════════════════════════════════════════════
# Off unless KITSUNE_CAPS names history, and the user must also allow it on the extension's
# options page. The bridge applies the history scope from its policy.

def _epoch_ms(since: str) -> int | None:
    """Epoch milliseconds of an ISO 8601 date or datetime (local time without an offset)."""
    text = since.strip()
    if text[-1:] in ("Z", "z"):  # Python 3.10 reads only numeric offsets
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


@tool("history", annotations=dict(title="Search browsing history", **READ))
def browser_history_search(query: str, since: str | None = None, limit: int = 20) -> dict:
    """Search the user's browsing history by title/URL text ("" for all; since: optional ISO
    8601 date/datetime); returns results [{url, title, last_visit, visit_count}], newest first.
    permission_required means the user has not allowed it: ask them, you cannot grant it.

    Results stay inside the bridge's history scope (domain lists; last 30 days by default);
    credential-like URL values read [redacted]. Reopen one with browser_tabs(action="new", url=...).
    """
    body: dict = {"query": query, "limit": limit}
    if since is not None:
        since_ms = _epoch_ms(since)
        if since_ms is None:
            return {"error": "since must be an ISO 8601 date or datetime, e.g. 2026-10-01"}
        body["since"] = since_ms
    return _post("/api/history-search", body)


@tool("history", annotations=dict(title="List history visits", **READ))
def browser_history_visits(url: str) -> dict:
    """List the visits to a URL as browser_history_search returned it, to follow the user's
    trail; returns visits [{visit_time, transition, visit_id, referring_visit_id?}], newest
    first. permission_required means the user has not allowed it: ask them.

    Only visits inside the history time window. transition is how the visit started (link,
    typed, reload, ...); referring_visit_id appears only where Firefox reports it.
    """
    return _post("/api/history-visits", {"url": url})


@tool("history", annotations=dict(title="Search bookmarks", **READ))
def browser_bookmarks_search(query: str, limit: int = 20) -> dict:
    """Search the user's bookmarks by title/URL text ("" for the most recent); returns
    results [{url, title, folder}]. permission_required means the user has not allowed
    it: ask them, you cannot grant it.

    Results stay inside the bridge's history domain scope; credential-like URL values
    read [redacted].
    """
    return _post("/api/bookmarks-search", {"query": query, "limit": limit})


@tool("automation", annotations=dict(title="Retry a command", **DESTRUCTIVE))
def browser_retry(
    command: Annotated[dict, Field(description="A command dict (same shape as browser_batch entries).")],
    max_attempts: Annotated[int, Field(description="Total tries including the first.")] = 3,
    backoff_ms: Annotated[int, Field(description="Initial wait between attempts (doubles each retry).")] = 500,
    on_errors: Annotated[list[str] | None, Field(
        description="Only retry if the error string contains one of these substrings. Default: retry "
                    "on any error.")] = None,
    tab_id: DefaultTabId = None,
) -> dict:
    """Wrap any command in exponential-backoff retry; an error with retryable=false (a write
    whose outcome is unknown) is never repeated."""
    body = {"command": command, "maxAttempts": max_attempts, "backoffMs": backoff_ms}
    if on_errors: body["onErrors"] = on_errors
    retry = {"action": "retry", **body}
    return _post("/api/retry", _default_tab(body, tab_id), timeout=max(60, command_wait_seconds(retry) + 15))


# -- Batch --------------------------------------------------------

@tool("automation", annotations=dict(title="Run command batch", **DESTRUCTIVE))
def browser_batch(
    commands: Annotated[list[dict], Field(
        description='e.g. [{"action": "navigate", "url": "..."}, {"action": "waitForElement", "selector": '
                    '".results", "timeout": 10000}, {"action": "pageText"}]')],
    tab_id: DefaultTabId = None,
) -> dict:
    """Run multiple commands in a single request for speed. A failed command does not
    make the call an MCP error: inspect each result, because earlier commands may
    already have taken effect.

    Each command is a dict with "action" and parameters. Add "tabId" to a
    command to target another tab than tab_id. For true
    concurrency across tabs, prefer browser_parallel. Resume from the failed
    command; progress gives each command's status (ok, failed, held, unknown, skipped).

    Available actions: navigate, newTab, closeTab, switchTab, click, type,
    setEditableContent, fill, scroll, hover, find, js, pageInfo, pageText, screenshot, tabs,
    forms, dom, sleep, waitForElement, pageTextByTabId, wakeTab, parallel, media
    (ref, op: play|pause|seek|mute|unmute, time: seconds for seek)

    click, doubleClick, hover, drag, type, fill, setEditableContent, selectOption,
    checkBox and keypress accept "wait": true and "timeout" (ms, default 10000) to
    wait for their target first, like the matching tools.
    """
    # Outlast the bridge's deadline, so a batch it stops still returns its partial progress.
    return _post("/api/batch", _default_tab({"commands": commands}, tab_id),
                 timeout=container_request_seconds(batch_deadline_seconds(commands)))


@tool("automation", annotations=dict(title="Resume a container call", **DESTRUCTIVE))
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


# The core group: Playwright MCP's core tools, backed by the functions above.
playwright_tools.register(sys.modules[__name__])
