"""
Kitsune MCP server.
Publishes Kitsune's agent tools (kitsune.tools) as MCP tools for the Kitsune browser extension
(Firefox, Zen and other Gecko browsers), within its KITSUNE_CAPS, KITSUNE_PUBLISH and
KITSUNE_MODE. Starts the shared bridge daemon on demand.
"""

import functools
import inspect
import json
import logging
import math
import os
import re
import sys
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
from mcp.types import CallToolResult, ClientCapabilities, RootsCapability, TextContent, ToolAnnotations
from pydantic import Field

from . import playwright_tools, tools
from .capabilities import CAPABILITY_GROUPS, NAMED_ONLY_GROUPS, enabled_groups
from .bridge_client import BridgeClient
from .errors import failed as _failed  # whether a tool's own result reports failure
from .task_modes import GATEWAY_TOOL_HEADER, permits_tool, task_mode
from .tools import DESTRUCTIVE, _on_current_tab


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
        if name in playwright_tools.SCHEMAS and (arguments or {}).get("filename"):
            # Relative file names resolve against the client's workspace root (#225).
            root = playwright_tools.client_root.set(await first_client_root(self.get_context()))
            try:
                return await super().call_tool(name, arguments)
            finally:
                playwright_tools.client_root.reset(root)
        return await super().call_tool(name, arguments)


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
PROTECT_USER_TABS = os.environ.get("KITSUNE_PROTECT_USER_TABS", "").lower() in {"1", "true", "yes", "on"}
# Sent once at initialize. The core tools keep Playwright MCP's descriptions, which cannot say
# that the browser is shared with its user; without this, agents avoid `select` to spare the
# user's view and work around background tabs instead of reading them.
mcp._mcp_server.instructions = (
    "Kitsune controls the user's own Firefox-family browser while the user keeps using it. "
    + ("browser_tabs `new` opens a tab in the background, and `select` makes a tab your current "
       "tab without switching the user's view, so you can read, fill and screenshot background "
       "tabs without interrupting the user. Until you open or select a tab, reads act on the tab "
       "the user is viewing, and writes and navigations that pass no tab_id fail with "
       "no_current_tab. A restarted MCP server has no current tab: open or select one again. A tab "
       "that a batch or workflow newTab opens, or that a click on your current tab opens, becomes "
       "your current tab; the result names it in current_tab."
       if permits_tool(MODE, "browser_tabs") else
       "Until browser_tab_list new_tab opens a background tab of your own, reads act on the tab the "
       "user is viewing, and navigations and writes that pass no tab_id fail with no_current_tab; "
       "later tools act on your tab without switching the user's view."))
# The tool browser_tools_call is running; its bridge requests name it for the audit log.
_gateway_call: ContextVar[str | None] = ContextVar("kitsune_gateway_call", default=None)

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
        result = function(*args, **kwargs)
        if isinstance(result, list):
            return [_compact_json(item) if isinstance(item, dict)
                    else McpImage(data=item.data, format=item.format) if isinstance(item, tools.Image)
                    else item for item in result]
        if not isinstance(result, dict):
            return result
        return CallToolResult(content=[_compact_json(result)], isError=_failed(result))

    return call


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
    """Name the tool browser_tools_call runs on its bridge requests, for the audit log."""
    name = _gateway_call.get()
    return {GATEWAY_TOOL_HEADER: name} if name else {}


# Every tool's bridge request carries this server's client id, caps, mode and user-tab protection.
_client = tools.client = BridgeClient(caps=enabled_groups(ENABLED_CAPS), mode=MODE,
                                      protect_user_tabs=PROTECT_USER_TABS, extra_headers=_gateway_headers)


@functools.wraps(tools.browser_status)
def browser_status() -> dict:
    result = tools.browser_status()
    # This server's tool surface (#171).
    result["mcp_tools"] = {"caps": sorted(ENABLED_CAPS), "unpublished": len(UNPUBLISHED_TOOLS),
                           "client": _client.client_id,
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
