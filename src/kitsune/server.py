"""
Kitsune MCP server.
Publishes Kitsune's agent tools (kitsune.tools) as MCP tools for the Kitsune browser extension
(Firefox, Zen and other Gecko browsers), within its KITSUNE_CAPS, KITSUNE_PUBLISH and
KITSUNE_MODE. Starts the shared bridge daemon on demand.
"""

import functools
import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import re
import secrets
import sys
import time
import weakref
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
from mcp.server.fastmcp import FastMCP, Image as McpImage
from mcp.server.lowlevel import NotificationOptions
from mcp.shared.exceptions import McpError
from mcp.types import (CallToolResult, CancelledNotification, CancelledNotificationParams, ClientCapabilities,
                       RootsCapability, ServerNotification, TextContent, ToolAnnotations)
from pydantic import Field, ValidationError

from . import playwright_tools, tools
from .capabilities import CAPABILITY_GROUPS, NAMED_ONLY_GROUPS, enabled_groups
from .bridge_client import BridgeClient
from .errors import failed as _failed  # whether a tool's own result reports failure
from .task_modes import APPROVAL_HEADER, GATEWAY_TOOL_HEADER, TOOL_CALL_HEADER, permits_tool, task_mode
from .tools import DESTRUCTIVE, _on_current_tab
from .waits import RESUME_WAIT_SECONDS, resume_wait_seconds


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


def _listed(tool):
    """A registered tool as tools/list publishes it: the core tools verbatim, restricted in read/draft."""
    playwright = playwright_tools.SCHEMAS.get(tool.name)
    if playwright:
        description = playwright["description"]
        # A deep copy: the mode filter below edits the schema, which SCHEMAS must keep.
        schema = json.loads(json.dumps(playwright["inputSchema"]))
        tool = tool.model_copy(update={"annotations": ToolAnnotations(**playwright["annotations"])})
    else:
        description = inspect.cleandoc(tool.description) if tool.description else tool.description
        schema = _published_schema(tool.inputSchema)
    if MODE != "act":
        description = description.split("\n\n", 1)[0] if description else description
        schema["properties"].pop("confirm_token", None)
        if tool.name == "browser_navigate":
            description = "Navigate a tab to a URL to read it."
    return tool.model_copy(update={"description": description, "inputSchema": schema})


class KitsuneMCP(FastMCP):
    """FastMCP that publishes tool input schemas without generated titles or nullable optionals.

    The core tools are Playwright MCP's: their descriptions, schemas and annotations are
    published verbatim from playwright_tools.json.

    Descriptions are normalized with inspect.cleandoc, so they are the same on every
    supported Python (only 3.13+ strips docstring indentation at compile time, #130).
    Calls are still validated against the registered pydantic argument models.

    Every authorized tool is registered; tools/list shows the ones KITSUNE_PUBLISH selects,
    the gateway tools and the ones browser_tools_publish added for the session (#227).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        options = self._mcp_server.create_initialization_options

        def initialization_options(notification_options=None, experimental_capabilities=None):
            # tools/list changes only when browser_tools_publish adds tools to a session.
            return options(notification_options or NotificationOptions(tools_changed=GATEWAY),
                           experimental_capabilities)

        self._mcp_server.create_initialization_options = initialization_options

    def session(self):
        """The MCP session of the request being handled; None outside a request."""
        try:
            return self._mcp_server.request_context.session
        except LookupError:
            return None

    async def all_tools(self) -> dict:
        """Every registered tool by name, as tools/list would publish it."""
        return {tool.name: _listed(tool) for tool in await super().list_tools()}

    async def list_tools(self):
        shown = PUBLISHED_TOOLS | session_tools(self.session())
        return [tool for name, tool in (await self.all_tools()).items() if name in shown]

    async def call_tool(self, name, arguments):
        # A known tool this configuration leaves out names its setting instead of "Unknown tool" (#171).
        if name in UNPUBLISHED_TOOLS:
            return CallToolResult(content=[_compact_json(_unpublished_tool(name))], isError=True)
        if MODE != "act" and (arguments or {}).get("confirm_token") is not None:
            return CallToolResult(content=[_compact_json({
                "error": f"{MODE} mode does not permit confirmation", "code": "mode_restricted",
                "retryable": False})], isError=True)
        if name == "browser_resume":
            resumed = await self._resume_direct(arguments or {})
            if resumed is not None:
                return resumed
        tab = playwright_tools._current_tab
        result = await self._run_tool(name, arguments)
        session = self.session()
        elicits = session is not None and _elicits(session)
        if isinstance(result, CallToolResult) and elicits:
            result = await self._approved(session, name, arguments or {}, result)
            if name == "browser_ask_user":
                result = await self._answered(session, result)
        return self._checkpointed(name, arguments or {}, result, elicits, tab)

    async def _run_tool(self, name, arguments):
        if name in playwright_tools.SCHEMAS and (arguments or {}).get("filename"):
            # Relative file names resolve against the client's workspace root (#225).
            root = playwright_tools.client_root.set(await first_client_root(self.get_context()))
            try:
                return await super().call_tool(name, arguments)
            finally:
                playwright_tools.client_root.reset(root)
        return await super().call_tool(name, arguments)

    def _repeatable(self, name) -> bool:
        """Whether a held call of this tool can be repeated with a confirm token."""
        tool = self._tool_manager.get_tool(name)
        return tool is not None and "confirm_token" in tool.parameters.get("properties", {})

    async def _approved(self, session, name, arguments, result, ask_seconds=None):
        """result of a call on a client with elicitation: a held direct call is asked about and, once
        the user approves, repeated with its token; no token stays in the result (#376).

        Each repeat starts at the held request (playwright_tools.repeat_hold), so a later held
        request of the call (the next field of a payment form) is asked about in turn, and a hold
        the user approved coming back means the repeat no longer matched it. ask_seconds, from
        browser_resume's wait, ends each prompt sooner; 0 asks nothing."""
        repeatable = self._repeatable(name)
        approved = []  # _hold_key of each hold the user approved in this call
        call_id = secrets.token_hex(8)
        while True:
            held = _json_payload(result)
            if held is None:
                return result
            if not _held_with_token(held) or not repeatable:
                return _with_payload(result, _without_tokens(held))
            if _hold_key(held) in approved:
                # The page or tab changed since the user approved it: ask again on a new call.
                return _with_payload(result, _unapproved(held, None, UNMATCHED_APPROVAL_HINT))
            if approved:
                # A later request of the call, held after the approved one ran: the token it was
                # sent was spent there, which is no reason to report.
                held["state"].pop("tokenRejected", None)
            if ask_seconds is not None and ask_seconds <= 0:
                return _with_payload(result, _unapproved(held, None, UNASKED_APPROVAL_HINT))
            decision = await _ask_user(session, self._mcp_server.request_context, name, arguments, held["state"],
                                       ask_seconds)
            if decision != "approved":
                return _with_payload(result, _unapproved(held, decision, DECISION_HINTS[decision]))
            approved.append(_hold_key(held))
            repeat, hold = _approval_repeat.set(call_id), playwright_tools.repeat_hold.set(held)
            try:
                result = await self._run_tool(name, {**arguments, "confirm_token": held["state"]["confirmToken"]})
            finally:
                playwright_tools.repeat_hold.reset(hold)
                _approval_repeat.reset(repeat)

    async def _answered(self, session, result, ask_seconds=None):
        """result of browser_ask_user on a client with elicitation: the user's answer in the client's prompt
        (#376). Done is ok with answer "done"; Can't, a dismissed prompt or none within ASK_USER_SECONDS
        (or ask_seconds, from browser_resume's wait; 0 asks nothing) keeps the needs_user stop, with
        state.decision."""
        asked = _json_payload(result, '"agent_request"')
        if asked is None or asked.get("code") != "needs_user":
            return result
        state = asked["state"]
        seconds = ASK_USER_SECONDS if ask_seconds is None else min(ask_seconds, ASK_USER_SECONDS)
        if seconds <= 0:
            return _with_payload(result, {**asked, "hint": UNASKED_REQUEST_HINT})
        answer = await _elicit(session, self._mcp_server.request_context, _request_message(state), ASK_USER_SCHEMA,
                               seconds, "Waiting for the user", "prompt")
        decision = _choice(answer, "answer", {"Done": "done", "Can't": "declined"})
        if decision == "done":
            return CallToolResult(content=[_compact_json(_user_done(state, "elicitation"))])
        return _with_payload(result, {**asked, "state": {**state, "decision": decision},
                                      "hint": REQUEST_HINTS[decision]})

    def _checkpointed(self, name, arguments, result, elicits, tab, call=None):
        """result, with call and resume_token when it is a needs_user stop of a direct call that
        browser_resume can continue (#376). call is the checkpoint a resume continues, else the
        audit call id the bridge gave the hold; tab the current tab the call ran on."""
        stopped = _json_payload(result, '"needs_user"')
        if stopped is None or not self._resumable(name, stopped, call, elicits):
            return result
        call = stopped["call"] if call is None else call
        # Without elicitation the result keeps its confirm token; the checkpoint never holds one.
        token = _save_direct_checkpoint(
            call, tool=name, arguments={key: value for key, value in arguments.items() if key != "confirm_token"},
            held={**stopped, "call": call, "state": _without_token(stopped["state"])}, tabId=tab)
        reason = stopped["state"]["reason"]
        hint = RESUME_HINTS[("elicitation" if elicits else "token") if reason == "approval"
                            else "ask" if reason == "agent_request" and elicits else "page"]
        return _with_payload(result, {**stopped, "call": call, "resume_token": token,
                                      "hint": f"{stopped.get('hint') or ''} {hint}".strip()})

    def _resumable(self, name, payload, call=None, elicits=False) -> bool:
        """Whether payload is a direct call's needs_user that browser_resume can continue: an approval
        hold of a tool that takes a confirm token, outside read and draft, a stop the page resolves
        (_page_reason), or browser_ask_user's stop the user did not answer Can't to, with elicitation.
        A container's result, one that already has a resume_token, or any where browser_resume is not
        published (read and draft) is not."""
        state = payload.get("state")
        if (payload.get("code") != "needs_user" or not isinstance(state, dict) or "resume_token" in payload
                or "browser_resume" in UNPUBLISHED_TOOLS
                or type(payload.get("call") if call is None else call) is not int):
            return False
        if state.get("reason") == "approval":
            return state.get("approvable") is not False and self._repeatable(name)
        if state.get("reason") == "agent_request" and (name != "browser_ask_user" or state.get("decision") == "declined"):
            return False
        return (elicits and state.get("reason") == "agent_request") or _page_reason(state) is not None

    async def _resume_direct(self, arguments):
        """browser_resume of a direct call this server keeps a checkpoint for; None otherwise, so the
        bridge resumes a container call or answers checkpoint_not_found (docs/checkpoint-resume.md).

        The tool runs again with its stored arguments, from the held request: nothing of the call that
        ran ok is sent again, and the held request goes through the current policy, so a resume grants
        nothing a new call would not. An approval hold is asked about again with elicitation, for up to
        wait seconds; without it the hold is checked once: a resume never waits for a hold to lift."""
        try:
            parsed = self._tool_manager.get_tool("browser_resume").fn_metadata.arg_model.model_validate(arguments)
        except ValidationError:  # the tool's own validation reports it
            return None
        call = parsed.call
        checkpoint = _direct_checkpoint(call, parsed.resume_token)
        if checkpoint is None and call in _direct_checkpoints:
            # A container call of a restarted bridge may have the same id: the bridge answers first.
            result = await self._run_tool("browser_resume", arguments)
            if (_json_payload(result, '"checkpoint_not_found"') or {}).get("code") != "checkpoint_not_found":
                return result
            return _error_result(
                "resume_token does not match this call's checkpoint", "resume_token_invalid", retryable=False,
                hint="Nothing was sent. Pass the resume_token from the response that returned this call.",
                state={"call": call})
        if checkpoint is None:
            return None
        name, args, held = checkpoint["tool"], checkpoint["arguments"], checkpoint["held"]
        if parsed.observed:
            return _error_result(
                "observed is for container steps; a direct call has none", "observed_invalid", retryable=True,
                hint="Nothing was sent. Resume without observed: nothing of this call ran past its held request.",
                state={"call": call})
        if args.get("tab_id") is None and playwright_tools._current_tab != checkpoint["tabId"]:
            return _error_result(
                f"The current tab changed since call {call}", "current_tab_changed", retryable=True,
                hint=(f"Nothing was sent. The call acts on the current tab, which was tab {checkpoint['tabId']}: "
                      "make that tab current again (browser_tabs select), then resume, or call the tool again "
                      "for this tab."),
                state={"call": call, "tabId": checkpoint["tabId"], "currentTab": playwright_tools._current_tab})
        del _direct_checkpoints[call]  # claimed while it runs, like a container's
        wait = resume_wait_seconds(parsed.wait)
        session = self.session()
        elicits = session is not None and _elicits(session)
        reason = held["state"]["reason"]
        request = self._mcp_server.request_context if session is not None else None
        if reason == "agent_request" and elicits:
            # browser_ask_user asks again in the client's prompt.
            result = await self._run_tool(name, args)
            if isinstance(result, CallToolResult):
                result = await self._answered(session, result, wait)
            return self._checkpointed(name, args, result, elicits, checkpoint["tabId"], call)
        if reason != "approval":
            page = _page_reason(held["state"])  # None: asked with elicitation, now without
            if page is None or not await _page_resolved(session, request, page, held["state"], wait):
                stopped = CallToolResult(content=[_compact_json(held)], isError=True)
                return self._checkpointed(name, args, stopped, elicits, checkpoint["tabId"], call)
            if reason == "agent_request":  # what browser_ask_user asked for is gone from the tab
                return CallToolResult(content=[_compact_json(_user_done(held["state"], "page"))])
        hold = playwright_tools.repeat_hold.set(held)
        try:
            result = await self._run_tool(name, args)
        finally:
            playwright_tools.repeat_hold.reset(hold)
        if isinstance(result, CallToolResult) and elicits:
            result = await self._approved(session, name, args, result, wait)
        return self._checkpointed(name, args, result, elicits, checkpoint["tabId"], call)


mcp = KitsuneMCP("Kitsune")


def _caps(setting: str, value: str) -> frozenset:
    caps = frozenset(cap.strip().lower() for cap in value.split(",") if cap.strip())
    unknown = caps - CAPABILITY_GROUPS - {"all"}
    if unknown:
        raise ValueError(f"Unknown {setting}: {', '.join(sorted(unknown))}")
    return caps


def _enables(caps: frozenset, group: str) -> bool:
    """`all` enables every group except NAMED_ONLY_GROUPS."""
    return group in enabled_groups(caps)


# The groups whose tools this server runs (#227: authorization, not just tools/list).
ENABLED_CAPS = _caps("KITSUNE_CAPS", os.environ.get("KITSUNE_CAPS", "core"))
# The authorized groups tools/list publishes when a session starts; by default all of them.
# The rest stay reachable through the gateway tools.
PUBLISHED_CAPS = (_caps("KITSUNE_PUBLISH", os.environ["KITSUNE_PUBLISH"]) if "KITSUNE_PUBLISH" in os.environ
                  else ENABLED_CAPS)
_unauthorized_caps = {cap for cap in PUBLISHED_CAPS - {"all"} if not _enables(ENABLED_CAPS, cap)}
if _unauthorized_caps:
    raise ValueError(f"KITSUNE_PUBLISH names groups KITSUNE_CAPS does not enable: "
                     f"{', '.join(sorted(_unauthorized_caps))}")

MODE = task_mode(os.environ.get("KITSUNE_MODE"))
# On unless set to 0: navigations on tabs no client opened need a confirm step (#326).
PROTECT_USER_TABS = os.environ.get("KITSUNE_PROTECT_USER_TABS", "").lower() not in {"0", "false", "no", "off"}
# On unless set to 0: writes on the tab the user is viewing need a confirm step (#326).
HOLD_VIEWED_TAB = os.environ.get("KITSUNE_HOLD_VIEWED_TAB", "").lower() not in {"0", "false", "no", "off"}
# Sent once at initialize. The core tools keep Playwright MCP's descriptions, which cannot say
# that the browser is shared with its user; without this, agents avoid `select` to spare the
# user's view and work around background tabs instead of reading them. Without the hand-off
# sentence, agents asked in chat instead of clicking a page's own hand-off control (#352).
mcp._mcp_server.instructions = (
    "Kitsune controls the user's own Firefox-family browser while the user keeps using it. "
    + ("browser_tabs `new` opens a tab in the background, and `select` makes a tab your current "
       "tab without switching the user's view, so you can read, fill and screenshot background "
       "tabs without interrupting the user. Until you open or select a tab, reads act on the tab "
       "the user is viewing, and writes and navigations that pass no tab_id fail with "
       "no_current_tab. A restarted MCP server has no current tab: open or select one again; "
       "no_current_tab lists the open tabs this MCP client opened before in state.ownTabs. A tab "
       "that a batch or workflow newTab opens, or that an action on your current tab opens, becomes "
       "your current tab; the result names it in current_tab. When a page offers its own control "
       "for handing a step to the user, use that control rather than asking in chat."
       if permits_tool(MODE, "browser_tabs") else
       "Until browser_tab_list new_tab opens a background tab of your own, reads act on the tab the "
       "user is viewing, and navigations and writes that pass no tab_id fail with no_current_tab; "
       "later tools act on your tab without switching the user's view.")
    # #326: the tab the user is viewing is held for writes unless this client set KITSUNE_HOLD_VIEWED_TAB=0.
    + ((" Writes and navigations on the tab the user is viewing fail with needs_user (detail user_viewing) "
        "unless you opened that tab: work in your own tabs, or ask the user before repeating the call with its "
        "confirm_token, if it has one."
        if permits_tool(MODE, "browser_tabs") else
        " Writes and navigations on the tab the user is viewing fail with needs_user (detail user_viewing); "
        "work in your own tab.")
       if HOLD_VIEWED_TAB else "")
    # #326: navigations on user tabs are refused unless this client set KITSUNE_PROTECT_USER_TABS=0.
    + ((" Navigate, back and forward on a tab no client opened fail with needs_user (detail user_tab): open a "
        "tab of your own, or ask the user before repeating the call with its confirm_token, if it has one."
        if permits_tool(MODE, "browser_tabs") else
        " Navigate, back and forward on a tab no client opened fail with needs_user (detail user_tab); work in "
        "your own tab.")
       if PROTECT_USER_TABS else ""))
# The tool browser_tools_call is running; its bridge requests name it for the audit log.
_gateway_call: ContextVar[str | None] = ContextVar("kitsune_gateway_call", default=None)
# The id of the tool call running (_mcp_errors), which its bridge requests carry.
_tool_call: ContextVar[str | None] = ContextVar("kitsune_tool_call", default=None)

# Every known tool (core and Kitsune-owned) -> its capability group.
TOOL_GROUPS: dict[str, str] = {}
# Known tools left out by caps or mode, which no call can run: name -> (group, settings that
# publish it).
UNPUBLISHED_TOOLS: dict[str, tuple[str, list[str]]] = {}
# Tools tools/list publishes to every session: the authorized tools of PUBLISHED_CAPS, and the
# gateway tools when other authorized tools are not published.
PUBLISHED_TOOLS: set[str] = set()
# Tools browser_tools_publish added to a session's tools/list; never shared between sessions.
_SESSION_TOOLS: "weakref.WeakKeyDictionary[object, set[str]]" = weakref.WeakKeyDictionary()


def session_tools(session) -> set[str]:
    return _SESSION_TOOLS.get(session, set()) if session is not None else set()


def _unpublished_tool(name: str) -> dict:
    group, settings = UNPUBLISHED_TOOLS[name]
    return {"error": f"{name} is not published by this MCP server", "code": "tool_not_published",
            "retryable": False,
            "hint": f"Set {' and '.join(settings)} in the MCP server environment, then restart the MCP client.",
            "state": {"group": group, "caps": sorted(ENABLED_CAPS),
                      **({"mode": MODE} if MODE != "act" else {})}}


def _compact_json(result) -> TextContent:
    """Unindented JSON text: every result is resent with each later model request (#136)."""
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str)
    return TextContent(type="text", text=text)


def _mcp_errors(function):
    """Return dict results as compact JSON text; failed results become MCP errors (isError).

    Dicts inside list results (screenshot metadata) are compacted too and a tools.Image becomes
    image content; other items are left to FastMCP.
    """
    @functools.wraps(function)
    def call(*args, **kwargs):
        # Its bridge requests share one tool call id: a viewed-tab approval covers all of them (#326),
        # and those of the repeats of a call the user approved (#376).
        call_id = _tool_call.set(_approval_repeat.get() or secrets.token_hex(8))
        try:
            result = function(*args, **kwargs)
        finally:
            _tool_call.reset(call_id)
        if isinstance(result, list):
            return [_compact_json(item) if isinstance(item, dict)
                    else McpImage(data=item.data, format=item.format) if isinstance(item, tools.Image)
                    else item for item in result]
        if not isinstance(result, dict):
            return result
        return CallToolResult(content=[_compact_json(result)], isError=_failed(result))

    return call


# ── Approval through MCP elicitation (#376, docs/api-reference.md#ask-before-act) ──
# A direct call held for the user's approval (needs_user, state.reason approval) returns a confirm
# token. When the client declared form elicitation at initialize, the server asks the user in the
# client's prompt and, on Approve, repeats the identical call with the token itself, so the model
# never receives a token and cannot approve itself (#374). The repeat tells the bridge so
# (APPROVAL_HEADER), for its audit log. Containers cannot confirm a step (the bridge honors tokens
# on direct calls only): their results lose every token, and a direct call of the held step asks.
# Without elicitation the token stays in the result, a guard against accidents only.

# The prompt closes this long before the token expires, so the repeat still finds it valid.
ELICITATION_MARGIN_SECONDS = 5
# While the prompt is open, a call that sent a progressToken gets a progress notification this
# often, so a client that resets its request timeout on progress keeps waiting.
ELICITATION_PROGRESS_SECONDS = 10
# The prompt shows the call's other arguments up to this many characters.
APPROVAL_ARGUMENTS_CHARS = 500
APPROVAL_SCHEMA = {"type": "object", "required": ["decision"], "properties": {
    "decision": {"type": "string", "title": "Decision", "enum": ["Approve", "Decline"]}}}
DECISION_HINTS = {
    "declined": "The held action did not run: the user declined it in the approval prompt. Do not repeat it; "
                "ask the user what to do instead, or work in a tab of your own.",
    "cancelled": "The held action did not run: the approval prompt was dismissed or timed out without an answer. "
                 "Ask the user whether to go ahead; calling the tool again asks them again.",
}
UNMATCHED_APPROVAL_HINT = ("The action the user approved did not run: repeated, it no longer matched what they "
                           "approved (state.tokenRejected), so the page or tab changed. What the call did before "
                           "it (filled) stays done. Check the page, then call the tool again to ask again.")
CONTAINER_APPROVAL_HINT = ("Nothing ran: this step needs the user's approval, which only a direct call of the tool "
                           "that does it can ask for. Make that call; the user is asked to approve it there.")
# Why a call was held, by state.detail, for the prompt.
_HELD_BECAUSE = {"user_viewing": "you are viewing this tab", "user_tab": "the agent did not open this tab",
                 "confirmation_required": "ask-before-act"}
# The tool call id the repeats of a call the user approved in the prompt share, so a viewed-tab
# approval in one covers the next (#326); their bridge requests carry APPROVAL_HEADER.
_approval_repeat: ContextVar[str | None] = ContextVar("kitsune_approval_repeat", default=None)


def _elicits(session) -> bool:
    """Whether the client declared form elicitation at initialize (an empty elicitation capability is
    form mode) and this MCP SDK sends it (ServerSession.elicit_form). The SDK sends it on any negotiated
    protocol version; clients declare the capability from 2025-06-18 on."""
    params = session.client_params
    elicitation = getattr(params.capabilities, "elicitation", None) if params is not None else None
    return (hasattr(session, "elicit_form") and elicitation is not None
            and (getattr(elicitation, "form", None) is not None or getattr(elicitation, "url", None) is None))


def _json_payload(result, marker='"confirmToken"') -> dict | None:
    """The JSON object of a tool result made by _compact_json whose text has marker (by default, that
    names a confirm token), else None."""
    if not isinstance(result, CallToolResult) or len(result.content) != 1 or result.content[0].type != "text":
        return None
    if marker not in result.content[0].text:  # most results: no need to parse them
        return None
    try:
        payload = json.loads(result.content[0].text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _with_payload(result: CallToolResult, payload: dict) -> CallToolResult:
    return result.model_copy(update={"content": [_compact_json(payload)]})


def _held_with_token(payload) -> bool:
    state = payload.get("state") if isinstance(payload, dict) and payload.get("code") == "needs_user" else None
    return isinstance(state, dict) and state.get("reason") == "approval" and isinstance(state.get("confirmToken"), str)


def _hold_key(held: dict) -> tuple:
    """Which request of the call a hold is: its action and params as the call sent them, whichever
    tab they resolved to; a user_viewing or user_tab hold, which names neither, by its tab."""
    state = held["state"]
    if "params" in state:
        return state.get("detail"), state.get("action"), json.dumps(state["params"], sort_keys=True, default=str)
    return state.get("detail"), state.get("tabId")


def _without_token(state: dict) -> dict:
    return {key: value for key, value in state.items() if key not in ("confirmToken", "expiresInSeconds")}


def _without_tokens(value):
    """value without confirm tokens: each held step it reports asks for a direct call instead."""
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    if not isinstance(value, dict):
        return value
    held = _held_with_token(value)
    value = {key: _without_tokens(item) for key, item in value.items() if key != "confirmToken"}
    if held:
        value.update(state={**_without_token(value["state"]), "approval": "elicitation"}, hint=CONTAINER_APPROVAL_HINT)
    return value


def _unapproved(held: dict, decision: str | None, hint: str) -> dict:
    """The held result the user did not approve, without its token."""
    state = {**_without_token(held["state"]), "approval": "elicitation"}
    if decision:
        state["decision"] = decision
    return _without_tokens({**held, "state": state, "hint": hint})


def _approval_message(name: str, arguments: dict, state: dict) -> str:
    """The prompt: what the call does, to which element and tab, with which arguments, and why it was
    held. The model's element description is labeled as the agent's: only the ref or selector is
    what the call acts on."""
    params = state.get("params") if isinstance(state.get("params"), dict) else {}
    target = next((str(value) for value in (arguments.get("target"), arguments.get("ref"), arguments.get("selector"),
                                            params.get("ref"), params.get("selector")) if value), None)
    rest = {key: value for key, value in arguments.items()
            if key not in ("target", "ref", "selector", "element", "confirm_token")}
    rest = json.dumps(rest, ensure_ascii=False, default=str)
    if len(rest) > APPROVAL_ARGUMENTS_CHARS:
        rest = rest[:APPROVAL_ARGUMENTS_CHARS - 1] + "…"
    tab = " - ".join(str(state[key]) for key in ("title", "url") if state.get(key))
    why = _HELD_BECAUSE.get(state.get("detail"), "it needs your approval")
    if state.get("description"):
        why += f": it {state['description']}"
    lines = [state.get("message") or "Allow this action?", f"Tool: {name}"]
    lines += [f"Target: {target}"] if target else []
    lines += [f"Agent's description of the target: {arguments['element']}"] if arguments.get("element") else []
    lines += [f"Arguments: {rest}"] if rest != "{}" else []
    lines += [f"Tab: {tab}"] if tab else []
    lines += [f"Held because {why}.", "Approve runs this action once; Decline leaves it undone."]
    return "\n".join(lines)


async def _keep_alive(session, request, message):
    """Send a progress notification every ELICITATION_PROGRESS_SECONDS, until cancelled."""
    progress_token = request.meta.progressToken
    sent = 0
    while True:
        await anyio.sleep(ELICITATION_PROGRESS_SECONDS)
        sent += 1
        await session.send_progress_notification(progress_token, sent, message=message,
                                                 related_request_id=request.request_id)


def _sends_progress(request) -> bool:
    return request is not None and request.meta is not None and request.meta.progressToken is not None


async def _elicit(session, request, message: str, schema: dict, seconds: float, waiting: str, prompt: str):
    """The client's answer to a form prompt (an ElicitResult), or None when it failed or went unanswered
    for seconds. waiting is the progress message meanwhile; prompt names it when it is closed unanswered."""
    answer = elicitation_id = None
    answered = False
    waited = anyio.move_on_after(seconds)
    try:
        async with anyio.create_task_group() as group:
            if _sends_progress(request):
                group.start_soon(_keep_alive, session, request, waiting)
            with waited:
                # The id the SDK's send_request gives the elicitation request, to cancel it unanswered.
                elicitation_id = getattr(session, "_request_id", None)
                try:
                    answer = await session.elicit_form(message, schema, related_request_id=request.request_id)
                except (McpError, ValueError):  # the client failed it, or answered outside the schema
                    pass
                answered = True
            group.cancel_scope.cancel()
    finally:
        if not answered and elicitation_id is not None:
            # Timed out, or the client cancelled the tool call: close the client's prompt, as an answer
            # after this counts for nothing.
            reason = f"The {prompt} timed out" if waited.cancelled_caught else "The tool call was cancelled"
            with anyio.CancelScope(shield=True):
                try:
                    await session.send_notification(ServerNotification(CancelledNotification(
                        params=CancelledNotificationParams(requestId=elicitation_id, reason=reason))),
                        related_request_id=request.request_id)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):  # the client is gone
                    pass
    return answer


def _choice(answer, field: str, choices: dict) -> str:
    """What a prompt's answer chose: choices[its field's value], "declined" or "cancelled"."""
    if answer is None or answer.action == "cancel":
        return "cancelled"
    if answer.action == "decline":
        return "declined"
    return choices.get((answer.content or {}).get(field), "cancelled")


async def _ask_user(session, request, name: str, arguments: dict, state: dict, limit=None) -> str:
    """Ask the user about a held call: "approved", "declined" or "cancelled" (dismissed, failed or
    unanswered until the token's expiresInSeconds less ELICITATION_MARGIN_SECONDS, or limit seconds)."""
    seconds = max(1, (state.get("expiresInSeconds") or 0) - ELICITATION_MARGIN_SECONDS)
    if limit is not None:
        seconds = min(seconds, limit)
    answer = await _elicit(session, request, _approval_message(name, arguments, state), APPROVAL_SCHEMA, seconds,
                           "Waiting for the user's approval", "approval prompt")
    return _choice(answer, "decision", {"Approve": "approved", "Decline": "declined"})


# ── browser_ask_user (#376, docs/api-reference.md#browser_ask_userreason-message-tab_idnone) ──
# The bridge answers browser_ask_user with a needs_user stop (reason agent_request). On a client with
# form elicitation the server asks the user in the client's prompt, Done or Can't (_answered); without
# it the stop returns at once, for the agent to ask in its reply.
ASK_USER_SECONDS = RESUME_WAIT_SECONDS
ASK_USER_SCHEMA = {"type": "object", "required": ["answer"], "properties": {
    "answer": {"type": "string", "title": "Answer", "enum": ["Done", "Can't"]}}}
REQUEST_HINTS = {
    "declined": "The user answered that they cannot do it. Do not ask again for the same thing; tell them what "
                "you could not finish, or ask what to do instead.",
    "cancelled": "The prompt was dismissed or timed out without an answer. Ask the user in your reply.",
}
UNASKED_REQUEST_HINT = ("browser_resume asked nothing with wait=0. Resume with a wait to ask the user in the "
                        "client's prompt.")


def _request_message(state: dict) -> str:
    """The prompt: the agent's message, labeled as the agent's, and its tab."""
    tab = " - ".join(str(state[key]) for key in ("title", "url") if state.get(key))
    lines = [f"The agent asks: {state.get('message')}"] + ([f"Tab: {tab}"] if tab else [])
    return "\n".join(lines + ["Done tells the agent you did it; Can't tells it you could not."])


def _user_done(state: dict, via: str) -> dict:
    """browser_ask_user's result once the user is done: they answered Done in the prompt (via
    "elicitation"), or what it asked for is gone from the tab (via "page")."""
    return {"ok": True, "answer": "done", "via": via, "reason": state.get("detail"),
            **({"tabId": state["tabId"]} if "tabId" in state else {}),
            "hint": "Check the page (browser_snapshot) before going on."}


# ── Direct-call checkpoints (#376, docs/checkpoint-resume.md#direct-calls) ──
# A direct call that stops on needs_user the user did not resolve in the call (no elicitation, or the
# prompt was declined, dismissed or timed out) keeps a checkpoint in this MCP server, under the bridge's
# rules for container checkpoints: in memory only, at most RESUME_MAX_CHECKPOINTS, each expiring
# RESUME_TTL_SECONDS after its call, holding the tool, its arguments, the held result and the current
# tab, and only a hash of its resume_token. Its call is the audit call id the bridge gave the hold, so
# it names no container call. Another MCP server, or this one after a restart, cannot resume it.
RESUME_MAX_CHECKPOINTS = 20  # bridge.RESUME_MAX_CHECKPOINTS
RESUME_TTL_SECONDS = 30 * 60  # bridge.RESUME_TTL_SECONDS


def _hint_gone(reason: str):
    def gone(state: dict) -> bool:
        """Whether tab state.tabId no longer shows the needs_user hint of reason; a failed read is not."""
        result = tools._post("/api/needs-user", {"tabId": state["tabId"]}, timeout=10)
        return not _failed(result) and (result.get("needs_user") or {}).get("reason") != reason
    return gone


# needs_user reasons the page resolves (sign_in, human_check) -> check(state), true once the user is
# done: browser_resume polls it every PAGE_STATE_POLL_SECONDS for up to its wait, then runs the call
# again (browser_ask_user's returns done). A stop without a check (_page_reason) gets no checkpoint.
PAGE_STATE_CHECKS: dict = {reason: _hint_gone(reason) for reason in ("sign_in", "human_check")}
PAGE_STATE_POLL_SECONDS = 1
RESUME_HINTS = {
    "elicitation": "If the user wants it done later, browser_resume(call, resume_token) asks them again in the "
                   "approval prompt and continues the call once they approve.",
    "token": "browser_resume(call, resume_token) checks the hold again and returns at once: it continues the "
             "call only if the hold no longer applies, as calling the tool again would.",
    "page": "Tell the user what to do (state.message), then call browser_resume(call, resume_token): it waits "
            "up to wait seconds for the page to show the user is done, then continues the call.",
    "ask": "browser_resume(call, resume_token) asks the user again in the client's prompt.",
}
UNASKED_APPROVAL_HINT = ("The held action did not run: browser_resume asked nothing with wait=0. Resume with a "
                         "wait to ask the user in the approval prompt.")
_direct_checkpoints: dict[int, dict] = {}  # call id -> checkpoint, oldest first
_checkpoint_clock = time.monotonic


def _resume_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _drop_expired_checkpoints() -> None:
    now_s = _checkpoint_clock()
    for call in [c for c, checkpoint in _direct_checkpoints.items() if checkpoint["expires"] <= now_s]:
        del _direct_checkpoints[call]


def _save_direct_checkpoint(call: int, **checkpoint) -> str:
    """Keep a direct call's checkpoint under call, replacing an older one; return its new resume_token."""
    token = secrets.token_urlsafe(24)
    _drop_expired_checkpoints()
    _direct_checkpoints.pop(call, None)
    _direct_checkpoints[call] = {**checkpoint, "token": _resume_token_digest(token),
                                 "expires": _checkpoint_clock() + RESUME_TTL_SECONDS}
    while len(_direct_checkpoints) > RESUME_MAX_CHECKPOINTS:
        del _direct_checkpoints[next(iter(_direct_checkpoints))]
    return token


def _direct_checkpoint(call: int, token: str) -> dict | None:
    """The checkpoint of call whose resume_token is token, else None: a container's call, or another
    MCP server's, goes to the bridge."""
    _drop_expired_checkpoints()
    checkpoint = _direct_checkpoints.get(call)
    if checkpoint is None or not hmac.compare_digest(_resume_token_digest(token), checkpoint["token"]):
        return None
    return checkpoint


def _page_reason(state: dict) -> str | None:
    """The PAGE_STATE_CHECKS entry that tells when a stop on tab state.tabId is resolved, else None: its
    reason, or for browser_ask_user's stop (agent_request) what it asked for, if the tab showed it then
    (state.evidence): otherwise the check could tell nothing."""
    reason = state.get("reason")
    if reason == "agent_request":
        reason = state.get("detail") if state.get("evidence") else None
    return reason if reason in PAGE_STATE_CHECKS and type(state.get("tabId")) is int else None


async def _page_resolved(session, request, reason: str, state: dict, wait: float) -> bool:
    """Whether PAGE_STATE_CHECKS[reason] tells, within wait seconds, that the user is done."""
    check = PAGE_STATE_CHECKS[reason]
    resolved = False
    async with anyio.create_task_group() as group:
        if _sends_progress(request):
            group.start_soon(_keep_alive, session, request, "Waiting for the user")
        with anyio.move_on_after(wait):
            while not (resolved := await anyio.to_thread.run_sync(check, state)):
                await anyio.sleep(PAGE_STATE_POLL_SECONDS)
        group.cancel_scope.cancel()
    return resolved


def _error_result(error: str, code: str, *, retryable: bool, hint: str, state: dict) -> CallToolResult:
    result = {"error": error, "code": code, "retryable": retryable, "hint": hint, "state": state}
    return CallToolResult(content=[_compact_json(result)], isError=True)


# How long a filename call waits for the client's roots/list answer before using the fallback.
ROOTS_TIMEOUT_SECONDS = 5


async def first_client_root(context) -> Path | None:
    """The first file:// root the MCP client lists, or None.

    None without a request (a direct call), with KITSUNE_OUTPUT_DIR set, when the client
    declares no roots capability, lists no file root, fails, or does not answer within
    ROOTS_TIMEOUT_SECONDS.
    """
    if os.environ.get("KITSUNE_OUTPUT_DIR"):
        return None
    try:
        session = context.session
    except ValueError:  # no MCP request
        return None
    if not session.check_client_capability(ClientCapabilities(roots=RootsCapability())):
        return None
    roots = []
    with anyio.move_on_after(ROOTS_TIMEOUT_SECONDS):
        try:
            roots = (await session.list_roots()).roots
        except McpError:
            pass
    for root in roots:
        uri = urlparse(str(root.uri))
        if uri.netloc in ("", "localhost"):  # a file root on this machine
            return Path(url2pathname(uri.path))
    return None


def _register(spec: tools.ToolSpec, function=None) -> None:
    """Register a tool of kitsune.tools only when its startup capability and mode permit it.

    `all` enables every group except NAMED_ONLY_GROUPS; modes narrow every group.
    Only the MCP registration marks failures as errors, runs an omitted tab_id on the current
    tab and refuses tab writes without one (no_current_tab); Python callers of kitsune.tools get
    the plain function. function replaces the tool's own
    (browser_status adds this server's tool surface).
    Tools left out go to UNPUBLISHED_TOOLS with the settings that would publish them.
    A registered tool outside PUBLISHED_CAPS is left out of tools/list, not of calls.
    """
    name = spec.function.__name__
    TOOL_GROUPS[name] = spec.group
    settings = []
    if not _enables(ENABLED_CAPS, spec.group):
        settings.append("KITSUNE_CAPS=" + ",".join(sorted(ENABLED_CAPS | {spec.group})))
    if not permits_tool(MODE, name):
        settings.append("KITSUNE_MODE=" + ("draft" if permits_tool("draft", name) else "act"))
    if settings:
        UNPUBLISHED_TOOLS[name] = (spec.group, settings)
        return
    annotations = ToolAnnotations(**spec.annotations) if spec.annotations else None
    mcp.tool(annotations=annotations)(_mcp_errors(_on_current_tab(function or spec.function)))
    if _enables(PUBLISHED_CAPS, spec.group):
        PUBLISHED_TOOLS.add(name)


def _gateway_headers() -> dict:
    """Name the tool browser_tools_call runs on its bridge requests, for the audit log, the tool call,
    and the repeat of a call the user approved in the client's prompt."""
    name, call_id = _gateway_call.get(), _tool_call.get()
    return {**({GATEWAY_TOOL_HEADER: name} if name else {}), **({TOOL_CALL_HEADER: call_id} if call_id else {}),
            **({APPROVAL_HEADER: "elicitation"} if _approval_repeat.get() else {})}


# Every tool's bridge request carries this server's client and owner ids, caps, mode, user-tab protection
# and approver secret.
_client = tools.client = BridgeClient(caps=enabled_groups(ENABLED_CAPS), mode=MODE,
                                      protect_user_tabs=PROTECT_USER_TABS, hold_viewed_tab=HOLD_VIEWED_TAB,
                                      extra_headers=_gateway_headers, approver=True)


@functools.wraps(tools.browser_status)
def browser_status(diagnostics: bool = False) -> dict:
    result = tools.browser_status(diagnostics=diagnostics)
    # This server's tool surface (#171).
    result["mcp_tools"] = {"caps": sorted(ENABLED_CAPS), "unpublished": len(UNPUBLISHED_TOOLS),
                           "client": _client.client_id, "owner": _client.owner_id,
                           **({"publish": sorted(PUBLISHED_CAPS), "unlisted": len(_unlisted_tools())}
                              if GATEWAY else {}),
                           **({"mode": MODE} if MODE != "act" else {})}
    return result


# Kitsune's own tools in kitsune.tools' order, then the core group. A tool with modes belongs to
# those modes only: read and draft publish browser_tab_list instead of browser_tabs.
for _spec in tools.TOOLS.values():
    if _spec.modes is None or MODE in _spec.modes:
        _register(_spec, browser_status if _spec.function is tools.browser_status else None)


# ── Tool gateway (#227) ──
# With KITSUNE_PUBLISH narrower than KITSUNE_CAPS, tools/list starts small and these tools
# reach the other authorized tools in the same connection. They find and run only registered
# tools of TOOL_GROUPS, through the same call_tool a direct call takes, so validation, mode,
# confirmation, protected tabs, redaction and output roots apply alike. Clients may show only
# the gateway's name and annotations in their prompts: they are not the security boundary.

GATEWAY_TOOLS = frozenset({"browser_tools_search", "browser_tools_call", "browser_tools_publish"})
SEARCH_LIMIT = 10


def _authorized_tools() -> set[str]:
    return set(TOOL_GROUPS) - set(UNPUBLISHED_TOOLS)


def _unlisted_tools() -> set[str]:
    """Authorized tools tools/list leaves out when a session starts."""
    return _authorized_tools() - PUBLISHED_TOOLS


# The gateway exists only when it reaches something tools/list does not already show.
GATEWAY = bool(_unlisted_tools())


def _gateway_error(error: str, code: str, hint: str, **state) -> CallToolResult:
    result = {"error": error, "code": code, "retryable": False, "hint": hint, **({"state": state} if state else {})}
    return CallToolResult(content=[_compact_json(result)], isError=True)


# Words every tool or query has, which tell tools apart no better than nothing.
_COMMON_WORDS = frozenset({"browser", "the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "with",
                           "is", "it", "this", "that", "by", "as", "at", "from", "i", "me", "my"})


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _COMMON_WORDS


def _matches(word: str, words: set[str]) -> bool:
    # "scrolling" finds "scroll": a word of four or more letters also matches by prefix.
    return any(word == other or (min(len(word), len(other)) >= 4 and
                                 (word.startswith(other) or other.startswith(word))) for other in words)


def _ranked(query: set[str], tools: dict[str, str]) -> list[str]:
    """Names of the tools (name -> description) matching the query, best first.

    A word in the name counts three times one in the description, a group name twice; words
    that many of the tools mention count less (inverse document frequency).
    """
    named = {name: _words(name.replace("_", " ")) for name in tools}
    described = {name: _words(description) for name, description in tools.items()}
    scores = dict.fromkeys(tools, 0.0)
    for word in query:
        hits = {name: 3 * _matches(word, named[name]) or _matches(word, described[name]) for name in tools}
        hits = {name: hit + 2 * (word == TOOL_GROUPS[name]) for name, hit in hits.items()}
        weight = math.log((len(tools) + 1) / (sum(map(bool, hits.values())) + 1))
        for name, hit in hits.items():
            scores[name] += weight * hit
    return [name for name in sorted(tools, key=lambda name: (-scores[name], name)) if scores[name] > 0]


def _groups_of(names) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        groups.setdefault(TOOL_GROUPS[name], []).append(name)
    return groups


async def browser_tools_search(
    query: Annotated[str, Field(description='Words for what the tool should do, a tool name or a group name; '
                                            '"" lists the tools by group.')],
    limit: Annotated[int, Field(description=f"Most tools to return, 1 to {SEARCH_LIMIT}.")] = 5,
) -> CallToolResult:
    """Find tools this server permits but does not list, with the schemas to call them.

    Best matches of the query against tool names, groups and descriptions come first, each
    with its full description, input schema and annotations; `listed` tells whether this
    session's tools/list has it. Run one with browser_tools_call, or add it to tools/list with
    browser_tools_publish. With no match, or an empty query, returns the tool names by group.
    """
    if not 1 <= limit <= SEARCH_LIMIT:
        return _gateway_error(f"limit must be 1 to {SEARCH_LIMIT}", "invalid_argument",
                              f"Pass a limit from 1 to {SEARCH_LIMIT}.")
    tools = await mcp.all_tools()
    candidates = _unlisted_tools()
    found = _ranked(_words(query), {name: tools[name].description or "" for name in candidates})
    if not found:
        return CallToolResult(content=[_compact_json({
            "tools": [], "matches": 0, "groups": _groups_of(candidates),
            "hint": "Search again with a name from groups, or a group name."})])
    listed = session_tools(mcp.session())
    return CallToolResult(content=[_compact_json({
        "tools": [{"name": name, "group": TOOL_GROUPS[name], "listed": name in listed,
                   **tools[name].model_dump(mode="json", exclude_none=True, include={
                       "description", "inputSchema", "annotations"})} for name in found[:limit]],
        "matches": len(found),
        "hint": "Run a tool with browser_tools_call(name, arguments)."})])


async def browser_tools_call(
    name: Annotated[str, Field(description="Tool name, as browser_tools_search returns it.")],
    arguments: Annotated[dict | None, Field(description="The tool's arguments, as its input schema describes.")] = None,
) -> CallToolResult:
    """Run a tool this server permits, listed or not, exactly as a direct call to it would.

    Returns that tool's own result: its content (text or images) and error status. Its
    argument validation, task mode, confirmation tokens and every other policy apply as
    they do to a direct call; the audit log records that the call came through here.
    """
    if name in GATEWAY_TOOLS:
        return _gateway_error(f"{name} cannot run through browser_tools_call", "invalid_argument",
                              f"Call {name} directly.")
    if name not in TOOL_GROUPS:
        return _gateway_error(f"Unknown tool: {name}", "unknown_tool",
                              "browser_tools_search lists the tools this server permits.")
    token = _gateway_call.set(name)
    try:
        result = await mcp.call_tool(name, arguments or {})
    except Exception as error:
        # The text a direct call's error result carries (the MCP server's error result).
        return CallToolResult(content=[TextContent(type="text", text=str(error))], isError=True)
    finally:
        _gateway_call.reset(token)
    if isinstance(result, CallToolResult):
        return result
    return CallToolResult(content=list(result[0] if isinstance(result, tuple) else result))


async def browser_tools_publish(
    names: Annotated[list[str], Field(description="Tool names and/or group names.")],
) -> CallToolResult:
    """Add permitted tools, or every permitted tool of a group, to this session's tools/list.

    Sends notifications/tools/list_changed. Clients that do not refresh their tool list on
    it keep the old one: call the tools with browser_tools_call instead. Other sessions'
    lists and what this server permits do not change.
    """
    session = mcp.session()
    if session is None:
        return _gateway_error("browser_tools_publish needs an MCP session", "invalid_argument",
                              "Call it through an MCP client.")
    unlisted = _unlisted_tools()
    requested, refused = set(), []
    for entry in names:
        if entry in CAPABILITY_GROUPS and _enables(ENABLED_CAPS, entry):
            requested |= {name for name in unlisted if TOOL_GROUPS[name] == entry}
        elif entry in unlisted:
            requested.add(entry)
        elif entry not in PUBLISHED_TOOLS:  # already listed tools need nothing
            refused.append(entry)
    if refused:
        # Unknown and unauthorized names are refused alike, so the answer reveals no catalog.
        return _gateway_error(f"Not tools or groups this server can add to tools/list: {', '.join(refused)}",
                              "tool_not_published",
                              "browser_tools_search lists the tools this server permits.", names=refused)
    shown = _SESSION_TOOLS.setdefault(session, set())
    added = sorted(requested - shown)
    shown |= requested
    if added:
        await session.send_tool_list_changed()
    return CallToolResult(content=[_compact_json({
        "published": added, "notified": bool(added),
        "hint": "Clients that do not refresh tools/list on notifications/tools/list_changed keep their "
                "list; run these with browser_tools_call."})])


if GATEWAY:
    mcp.tool(annotations=ToolAnnotations(title="Search tools", readOnlyHint=True, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=False))(browser_tools_search)
    # Conservative: the tools it runs range up to destructive ones.
    mcp.tool(annotations=ToolAnnotations(title="Call a tool", **DESTRUCTIVE))(browser_tools_call)
    mcp.tool(annotations=ToolAnnotations(title="Publish tools", readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=False))(browser_tools_publish)
    PUBLISHED_TOOLS |= GATEWAY_TOOLS


# FastMCP logs at INFO through Rich, so every tool call printed a line from the MCP server and
# one from httpx: about 0.6 ms of CPU together (#298). Their warnings and errors still show.
PER_CALL_LOGGERS = ("mcp.server.lowlevel.server", "httpx")


def main():
    """Entry point for the Kitsune MCP server."""
    for name in PER_CALL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    error = _client.ensure_bridge()  # warm start; tools retry if this fails
    if error:
        print(error, file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
