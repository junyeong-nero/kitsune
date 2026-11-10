"""First shared native adapters, intentionally not registered as MCP/worker APIs yet.

Tabs, navigation, read, find, wait, screenshot and the synthetic page actions are
implemented here. All other admitted operations fail closed until their approval and
resume contracts are implemented.

Outcomes come from the bridge's dispatch receipt, never from extension/page fields:
a reply to a write is `dispatched`, not application success. Cancelling or timing
out a call does not stop a request the bridge already accepted.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
import secrets

from .bridge_client import BridgeClient
from .connection_binding import CONNECTION_HEADER, ConnectionBinding
from .dispatch_receipts import MAX_NATIVE_COMMANDS, RECEIPT_HEADER, RECEIPT_VERSION
from .flat_operations import OperationRefused
from .flat_session import CallerAuthority, FlatSession
from .version import BRIDGE_BUILD


MAX_BRIDGE_REPLY_BYTES = 256 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_TITLE_CHARS = 256
MAX_URL_CHARS = 2048
# The bridge answers its own bounded waits first; this only covers transport.
TRANSPORT_GRACE_SECONDS = 2
# browser_find searches an uncollapsed snapshot larger than a read returns. JSON escapes a
# non-ASCII character as up to 12 bytes on the wire, so its reply limit is separate.
FIND_SNAPSHOT_CHARS = 150_000
MAX_FIND_REPLY_BYTES = 2 * 1024 * 1024
MAX_MATCH_LINE_CHARS = 400
MAX_MATCH_PATH = 3
# (verified connection, call id) of the admitted request running in this context.
_REQUEST = ContextVar("kitsune_flat_request", default=None)
_IMPLEMENTED = frozenset({"browser_tabs", "browser_navigate", "browser_read", "browser_find",
                          "browser_click", "browser_input", "browser_select", "browser_check",
                          "browser_press", "browser_hover", "browser_drag", "browser_scroll",
                          "browser_wait", "browser_screenshot"})
_NATIVE_ERRORS = frozenset({"mode_restricted", "user_tab_read_only", "user_active", "needs_user",
                            "stale_connection", "invalid_tab_id", "tab_closed", "timeout",
                            "extension_disconnected", "native_operation_limit",
                            "stale_ref", "not_found", "unsupported_selector", "restricted_page",
                            "tab_unloaded", "navigating", "content_unavailable",
                            "ambiguous_target", "not_actionable"})
# Native page actions, which run in the page's content script.
_PAGE_ACTIONS = frozenset({"click", "doubleClick", "fill", "type", "selectOption", "checkBox", "keypress",
                           "hover", "drag", "scroll"})
# Those whose content script waits (wait/timeout) for the target before it dispatches.
_WAITS_FOR_TARGET = _PAGE_ACTIONS - {"scroll"}
# A page action answers these before it sends the page an event: ownership and activity
# admission in the background, target resolution and the actionability wait in the content
# script. tab_closed is not among them: a click can close its own tab.
_PAGE_REFUSED_BEFORE_ACTING = frozenset({"user_tab_read_only", "user_active", "invalid_tab_id", "stale_ref",
                                         "ambiguous_target", "not_found", "unsupported_selector",
                                         "not_actionable"})
_KEY_MODIFIERS = {"Shift": "shift", "Control": "ctrl", "Alt": "alt", "Meta": "meta"}
_WARNINGS = frozenset({"hidden_or_covered", "popup_blocked"})
_STALE_REF_REASONS = frozenset({"element_removed", "not_in_snapshot", "other_document"})
# Reads change nothing: a failed one is safe to repeat and never an unknown write.
_WAITS = frozenset({"waitForText", "waitForElement", "waitForUrl", "waitForTitle", "waitForNetworkIdle"})
# A viewport capture changes nothing. Element and full-page captures scroll the page and put
# it back, so a failed one is treated like a failed page action.
_CAPTURES = frozenset({"screenshot", "elementScreenshot", "fullPageScreenshot"})
_READS = frozenset({"getTabs", "getSnapshot", "screenshot"}) | _WAITS
# The extension is asked for its inline preview (about 1 MB of base64 at most), as the existing
# screenshot tool is; the decoded limit is the contract's and is checked whatever arrives.
MAX_IMAGE_REPLY_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_SIGNATURES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
# The bridge's own allowance for these two waits is fixed, so longer ones cannot be honoured.
MAX_TARGET_WAIT_MS = 30_000
MAX_IDLE_WAIT_MS = 25_000
# A polled wait is at most this many page reads, within a receipt's command limit.
MAX_WAIT_READS = 100
_REGEX_SPECIAL = re.compile(r"[\\^$.*+?()\[\]{}|/]")
_RETRYABLE_READ_ERRORS = frozenset({"timeout", "bridge_disconnected", "bridge_unavailable", "navigating",
                                    "content_unavailable", "browser_operation_failed"})
_NAVIGATIONS = frozenset({"navigate", "goBack", "goForward", "reloadTabBrowser"})
# The ref of a snapshot line, after its JSON-quoted name (as background.js snapshotLineRef):
# ref-like text inside a quoted name or value is page data, never a target.
_LINE_REF = re.compile(r'^ *- [^"\n]+ "(?:[^"\\]|\\.)*" \[ref=(e[0-9]{1,127})\](?= |$)')
# The extension's admission (tabWriteError) answers these before it calls a browser API.
# True of the single-call tab/navigation actions here; a multi-step action must not reuse it.
_REFUSED_BEFORE_ACTING = frozenset({"user_tab_read_only", "user_active", "tab_closed", "invalid_tab_id"})
# Client-side failures without a bridge receipt, and whether the request can have run.
_TRANSPORT_ERRORS = {"bridge_unavailable": "not_dispatched", "token_mismatch": "not_dispatched",
                     "timeout": "unknown", "bridge_disconnected": "unknown", "bridge_response_limit": "unknown"}


def _request_headers():
    context = _REQUEST.get()
    if context is None:
        raise RuntimeError("Native flat request outside an admitted call")
    connection, call = context
    return {CONNECTION_HEADER: connection.header_value(), RECEIPT_HEADER: call}


@dataclass(frozen=True)
class BrokerImage:
    handle: str  # names the image in result.data; not a path and not usable as a URL
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class BrokerReply:
    # result, and images as real image content, may be delivered to an agent. receipt is
    # coordinator-owned evidence; future MCP/worker adapters must not accept a caller-supplied
    # BrokerReply. Limits across several images (a run's four, 16 MiB) belong to the runner.
    result: dict
    receipt: dict | None
    images: tuple[BrokerImage, ...] = ()


def _image(native):
    """The validated image of a capture reply; ValueError if it is not one, None if too large."""
    url = native.get("dataUrl")
    if not isinstance(url, str):
        raise ValueError("Not an image data URL")
    mime, separator, encoded = url.partition(";base64,")
    mime = mime.removeprefix("data:")
    if not separator or mime not in _IMAGE_SIGNATURES:
        raise ValueError("Not an image data URL")
    if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise ValueError("Invalid image encoding") from None
    if not data.startswith(_IMAGE_SIGNATURES[mime]):
        raise ValueError("Image content does not match its type")
    return BrokerImage("i_" + secrets.token_hex(16), mime, data) if len(data) <= MAX_IMAGE_BYTES else None


def _failure(call, name, code, outcome="not_dispatched", *, tab_id=None, retryable=False, state=None):
    result = {"ok": False, "call": call, "operation": name, "outcome": outcome,
              "data": {}, "truncated": code == "output_limited",
              "error": {"code": code, "message": "The browser operation did not complete",
                        "retryable": retryable,
                        "hint": "This read changed nothing; it can be repeated." if retryable else
                                "Inspect current state; do not blindly replay a write.",
                        "state": state or {}}}
    if tab_id is not None:
        result["tab_id"] = tab_id
    return result


def _receipt(envelope, call):
    """Accept only the bounded, bridge-owned envelope for this particular request."""
    if not isinstance(envelope, dict) or set(envelope) != {"result", "receipt"}:
        raise ValueError("Missing dispatch envelope")
    receipt = envelope["receipt"]
    if not isinstance(receipt, dict) or set(receipt) != {"version", "call", "requested", "limited", "complete", "commands"}:
        raise ValueError("Invalid dispatch receipt")
    commands = receipt["commands"]
    if (receipt["version"] != RECEIPT_VERSION or receipt["call"] != call
            or type(receipt["requested"]) is not int or receipt["requested"] < 0
            or type(receipt["complete"]) is not bool or type(receipt["limited"]) is not bool
            or not isinstance(commands, list) or len(commands) > MAX_NATIVE_COMMANDS):
        raise ValueError("Invalid dispatch receipt")
    if receipt["requested"] != len(commands) + int(receipt["limited"]):
        raise ValueError("Incomplete native command count")
    for index, command in enumerate(commands, 1):
        if (not isinstance(command, dict) or not {"sequence", "action", "state", "finished"} <= command.keys()
                or command.keys() - {"sequence", "action", "state", "finished", "tab_id"}
                or type(command["sequence"]) is not int or command["sequence"] != index
                or not isinstance(command["action"], str) or not 1 <= len(command["action"]) <= 64
                or command["state"] not in {"not_dispatched", "unknown", "dispatched", "replied"}
                or type(command["finished"]) is not bool):
            raise ValueError("Invalid native command receipt")
        if "tab_id" in command and (type(command["tab_id"]) is not int or not 0 < command["tab_id"] < 2**53):
            raise ValueError("Invalid native target receipt")
    if receipt["complete"] and any(not command["finished"] for command in commands):
        raise ValueError("Unfinished native work declared complete")
    return receipt


def _outcome(receipt, action, read=False):
    if not receipt["complete"]:
        return "unknown"
    commands = [item for item in receipt["commands"] if item["action"] == action]
    if not commands or all(item["state"] == "not_dispatched" for item in commands):
        return "not_dispatched"
    if read:
        # A polled wait is several reads; one cut off at its deadline changed nothing.
        return "dispatched" if any(item["state"] == "replied" for item in commands) else "unknown"
    if any(item["state"] in {"unknown", "dispatched"} for item in commands):
        return "unknown"
    return "dispatched"  # a reply is not proof of application success


def _wait_request(values):
    """(native action, route, body) of one typed wait; never a page predicate or a regex of the caller's."""
    condition, timeout = values["condition"], values["timeout_ms"]
    kind = condition["kind"]
    polled = {"timeout": timeout, "pollInterval": max(100, -(-timeout // MAX_WAIT_READS))}
    if kind in {"text", "text_gone"}:
        return "waitForText", "/api/wait-for-text", {kind: condition["value"], **polled}
    if kind == "target":
        if timeout > MAX_TARGET_WAIT_MS:
            raise OperationRefused("unsupported_variant", "A target wait is limited to 30 s for now")
        return "waitForElement", "/api/wait-for-element", {**condition["target"], **polled}
    if kind in {"url", "title"}:
        # Literal equality: the extension takes a regular expression, so the value is escaped.
        pattern = "^" + _REGEX_SPECIAL.sub(lambda match: "\\" + match[0], condition["value"]) + "$"
        action, path = (("waitForUrl", "/api/wait-for-url") if kind == "url" else
                        ("waitForTitle", "/api/wait-for-title"))
        return action, path, {"pattern": pattern, "timeout": timeout}
    if timeout > MAX_IDLE_WAIT_MS:
        raise OperationRefused("unsupported_variant", "A network_idle wait is limited to 25 s for now")
    return "waitForNetworkIdle", "/api/wait-for-network-idle", {"timeout": timeout}


def _page_action(name, values):
    """(native action, route, body) of a page action; never `first`, coordinates or OS input."""
    target = values.get("target", {})  # exactly {"ref": ...} or {"selector": ...}
    if name == "browser_click":
        if values["click_count"] == 2:  # admitted only as a left double-click without modifiers
            return "doubleClick", "/api/double-click", dict(target)
        return "click", "/api/click", {**target, "button": values["button"], "modifiers": values["modifiers"]}
    if name == "browser_input":
        if values["mode"] == "replace":
            return "fill", "/api/fill", {**target, "value": values["text"]}
        return "type", "/api/type", {**target, "text": values["text"], "clear": False}
    if name == "browser_select":
        # The extension matches several values by value, then by text, and a label by its first
        # match. Neither is the contract's strict choice, so only one exact value is sent.
        if values["by"] != "value" or len(values["values"]) != 1:
            raise OperationRefused("unsupported_variant", "Only one option value can be selected for now")
        return "selectOption", "/api/select-option", {**target, "value": values["values"][0], "byText": False}
    if name == "browser_check":
        return "checkBox", "/api/check", {**target, "checked": values["checked"]}
    if name == "browser_press":
        return "keypress", "/api/keypress", {
            **target, "key": values["key"], "modifiers": {_KEY_MODIFIERS[m]: True for m in values["modifiers"]}}
    if name == "browser_hover":
        return "hover", "/api/hover", dict(target)
    if name == "browser_drag":
        source, destination = values["source"], values["destination"]
        return "drag", "/api/drag", {
            **({"ref": source["ref"]} if "ref" in source else {"from": source["selector"]}),
            **({"toRef": destination["ref"]} if "ref" in destination else {"to": destination["selector"]})}
    body = {**target, "direction": values["direction"]}
    if "amount_px" in values:
        body["amount"] = values["amount_px"]
    return "scroll", "/api/scroll", body


def _line_refs(lines):
    return [match[1] for match in map(_LINE_REF.match, lines) if match]


def _find_matches(snapshot, text, limit):
    """(matches, total): snapshot lines containing text, case-insensitively and literally."""
    lines = snapshot.split("\n")
    needle = text.casefold()
    matches, total, stack = [], 0, []
    for line in lines:
        body = line.lstrip(" ")
        indent = len(line) - len(body)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        # A truncation or collapse marker is the protocol's text, not the page's.
        if needle in line.casefold() and not body.startswith("- …"):
            total += 1
            if len(matches) < limit:
                match = {"line": body[:MAX_MATCH_LINE_CHARS]}
                if path := [parent[:MAX_MATCH_LINE_CHARS] for _, parent in stack[-MAX_MATCH_PATH:]]:
                    match["path"] = path
                if refs := _line_refs([match["line"]]):
                    match["ref"] = refs[0]
                matches.append(match)
        stack.append((indent, body))
    return matches, total


class FlatBroker:
    def __init__(self, authority: CallerAuthority):
        self.session = FlatSession(authority)
        self._lock = asyncio.Lock()
        self._bootstrap = self._client(authority)
        self._transport = self._client(authority, _request_headers)

    @staticmethod
    def _client(authority, extra_headers=None):
        # The adapter allowlist is narrower than these legacy capability groups.
        # Both clients are trusted host objects and must never enter the worker.
        client = BridgeClient(caps={"core", "browse", "tabs"}, mode=authority.mode,
                              user_tabs=authority.user_tabs, replace_bridge=False, extra_headers=extra_headers)
        client.client_id, client.owner_id = authority.client_id, authority.owner_id
        return client

    @classmethod
    async def connect(cls, authority: CallerAuthority):
        broker = cls(authority)
        await broker._refresh_connection()
        return broker

    async def _refresh_connection(self):
        # No startup/replacement or developer fallback, even when an old bridge answers.
        status = await asyncio.to_thread(self._bootstrap.request, "GET", "/api/status", start_bridge=False,
                                         max_response_bytes=MAX_BRIDGE_REPLY_BYTES, timeout=5)
        if (not isinstance(status, dict) or status.get("build") != BRIDGE_BUILD
                or status.get("dispatch_receipts") != RECEIPT_VERSION
                or (status.get("diagnosis") or {}).get("code") != "connected"):
            raise OperationRefused("bridge_not_ready", "A matching connected receipt-capable bridge is required")
        try:
            self.session.bind(ConnectionBinding.from_status(status))
        except ValueError:
            raise OperationRefused("bridge_not_ready", "Invalid connection identity") from None

    @staticmethod
    def _native_request(admitted):
        """(native action, method, route, body, timeout ms, reply byte limit) of an admitted call."""
        values = admitted.operation.arguments()
        timeout = values.get("timeout_ms", 10000)
        if admitted.operation.name == "browser_tabs":
            action = values["action"]
            if action == "list":
                return "getTabs", "GET", "/api/tabs", None, timeout, MAX_BRIDGE_REPLY_BYTES
            if action == "open":
                return "newTab", "POST", "/api/new-tab", {
                    "url": values["url"], "wait": True, "timeout": timeout,
                    "includeSnapshot": False}, timeout, MAX_BRIDGE_REPLY_BYTES
            return ("closeTab", "POST", "/api/close-tab", {"tabId": admitted.tab.native_id}, timeout,
                    MAX_BRIDGE_REPLY_BYTES)
        if admitted.operation.name == "browser_read":
            # A full snapshot: never sinceObservation/skipUnchanged, so never a delta. Only the
            # text form carries static prose, and only it reports the document's url and title.
            body = {"tabId": admitted.tab.native_id, "maxChars": values["max_chars"],
                    "text": values["format"] == "text", **values.get("target", {})}
            return "getSnapshot", "POST", "/api/snapshot", body, timeout, MAX_BRIDGE_REPLY_BYTES
        if admitted.operation.name == "browser_find":
            # As the core browser_find: static prose and every item of a repeated run.
            body = {"tabId": admitted.tab.native_id, "maxChars": FIND_SNAPSHOT_CHARS, "collapse": False}
            return "getSnapshot", "POST", "/api/snapshot", body, timeout, MAX_FIND_REPLY_BYTES
        if admitted.operation.name == "browser_screenshot":
            action, path = {"viewport": ("screenshot", "/api/screenshot"),
                            "element": ("elementScreenshot", "/api/element-screenshot"),
                            "full_page": ("fullPageScreenshot", "/api/full-page-screenshot")}[values["scope"]]
            # No format, scale or file path: the image comes back inline or not at all.
            body = {"tabId": admitted.tab.native_id, "imagePreview": True, **values.get("target", {})}
            return action, "POST", path, body, timeout, MAX_IMAGE_REPLY_BYTES
        if admitted.operation.name == "browser_wait":
            action, path, body = _wait_request(values)
            return action, "POST", path, {"tabId": admitted.tab.native_id, **body}, timeout, MAX_BRIDGE_REPLY_BYTES
        if admitted.operation.name != "browser_navigate":
            action, path, body = _page_action(admitted.operation.name, values)
            body.update(tabId=admitted.tab.native_id, includeSnapshot=False)
            if action in _WAITS_FOR_TARGET:
                # The page waits this long for the target, then acts once; it is not a retry.
                body.update(wait=True, timeout=timeout)
            return action, "POST", path, body, timeout, MAX_BRIDGE_REPLY_BYTES
        action, path = {"goto": ("navigate", "/api/navigate"), "back": ("goBack", "/api/back"),
                        "forward": ("goForward", "/api/forward"), "reload": ("reloadTabBrowser", "/api/reload-tab")}[values["action"]]
        # The extension's own load wait for navigate is fixed (15 s) and history/reload have
        # none: timeout_ms bounds this call's wait for the reply, not the native action.
        body = {"tabId": admitted.tab.native_id, "includeSnapshot": False}
        if values["action"] == "goto":
            body["url"] = values["url"]
        return action, "POST", path, body, timeout, MAX_BRIDGE_REPLY_BYTES

    def _page_data(self, admitted, native):
        """browser_read and browser_find results from one full snapshot reply."""
        values = admitted.operation.arguments()
        snapshot = native.get("snapshot")
        if (not isinstance(snapshot, str) or native.get("delta") or native.get("unchanged")
                or "unchanged_since" in native):
            raise ValueError("Not a full snapshot")
        cut = native.get("truncated") is True
        data = {"tab_id": admitted.tab.handle}
        for key, limit in (("url", MAX_URL_CHARS), ("title", MAX_TITLE_CHARS)):
            if isinstance(native.get(key), str):
                data[key] = native[key][:limit]
        if admitted.operation.name == "browser_find":
            matches, total = _find_matches(snapshot, values["text"], values["limit"])
            data.update(matches=matches, count=total)
            delivered = [line for match in matches for line in (match["line"], *match.get("path", ()))]
            cut = cut or total > len(matches)
        else:
            if len(snapshot) > values["max_chars"]:
                # The extension bounds the snapshot; keep whole lines if it ever does not.
                snapshot, cut = snapshot[:values["max_chars"] + 1].rpartition("\n")[0], True
            data.update(format=values["format"], content=snapshot)
            delivered = snapshot.split("\n")
        # Only refs in what this caller receives become its targets for this tab.
        self.session.issue_refs(admitted.tab.handle, _line_refs(delivered))
        return data, "observed", cut

    def _wait_data(self, admitted, native, action):
        """A met wait: the condition was observed. Page text, titles and URLs are not echoed."""
        data = {"tab_id": admitted.tab.handle, "kind": admitted.operation.arguments()["condition"]["kind"]}
        elapsed = native.get("elapsed")
        if type(elapsed) in (int, float) and 0 <= elapsed < 10**7:
            data["elapsed_ms"] = int(elapsed)
        if native.get("already_present") is True:
            data["already_present"] = True  # the text was there when the wait began
        if action == "waitForElement" and isinstance(native.get("ref"), str):
            # The element the wait found becomes a target of this session, like a read's refs.
            self.session.issue_refs(admitted.tab.handle, [native["ref"]])
            data["ref"] = native["ref"]
        return data, "observed", False

    def _data(self, admitted, native, action):
        if action in _CAPTURES:
            image = _image(native)
            if image is None:
                raise OperationRefused("output_limited", "The image exceeds the decoded size limit")
            # The page's URL and title in the reply are not copied; the image is the observation.
            return {"tab_id": admitted.tab.handle, "image": image.handle, "mime_type": image.mime_type,
                    "bytes": len(image.data)}, "observed", False, image
        return (*self._plain_data(admitted, native, action), None)

    def _plain_data(self, admitted, native, action):
        if action in _WAITS:
            return self._wait_data(admitted, native, action)
        if action == "getSnapshot":
            return self._page_data(admitted, native)
        if action == "getTabs":
            tabs = native.get("tabs")
            if not isinstance(tabs, list) or native.get("truncated"):
                raise ValueError("Incomplete tab inventory")
            if any(not isinstance(tab, dict) or not isinstance(tab.get("url"), str)
                   or not isinstance(tab.get("title"), str) for tab in tabs):
                raise ValueError("Invalid tab inventory")
            records = self.session.refresh_tabs(admitted.connection, [tab.get("id") for tab in tabs])
            listed, cut = [], False
            for tab, record in zip(tabs, records):
                entry = {"tab_id": record.handle, "url": tab["url"][:MAX_URL_CHARS],
                         "title": tab["title"][:MAX_TITLE_CHARS],
                         "owned": tab.get("owned") is True, "active": tab.get("active") is True}
                if len(tab["url"]) > MAX_URL_CHARS or len(tab["title"]) > MAX_TITLE_CHARS:
                    entry["truncated"] = cut = True
                listed.append(entry)
            return {"tabs": listed, "tab_count": len(listed)}, "observed", cut
        # Do not copy snapshots/refs, approval tokens, native ids or arbitrary extension fields.
        if action == "newTab":
            record = self.session.opened_tab(admitted.connection, native.get("tabId"))
            data, cut = {"tab_id": record.handle}, False
            for key in ("url", "status"):
                if key in native:
                    if not isinstance(native[key], str):
                        raise ValueError("Invalid open observation")
                    data[key] = native[key][:MAX_URL_CHARS]
                    cut = cut or len(native[key]) > MAX_URL_CHARS
            return data, "observed" if native.get("status") == "complete" else "dispatched", cut
        if action == "closeTab":
            self.session.closed_tab(admitted.tab.handle)
        data = {"tab_id": admitted.tab.handle}
        if action in _PAGE_ACTIONS:
            # Tabs the action opened get handles; a tab that cannot is found by listing tabs.
            opened = native.get("opened_tabs") if isinstance(native.get("opened_tabs"), list) else []
            handles = []
            for tab in opened[:8]:
                try:
                    handles.append({"tab_id": self.session.opened_tab(admitted.connection, tab.get("tabId")).handle})
                except (OperationRefused, ValueError, AttributeError):
                    pass
            if handles:
                data["opened_tabs"] = handles
            warning = native.get("warning")
            if isinstance(warning, dict) and warning.get("code") in _WARNINGS:
                data["warning"] = warning["code"]
        # A reply means the events were sent, not that the page did what was wanted; navigate
        # echoes the requested URL, not where the tab landed.
        return data, "dispatched", False

    async def call(self, name, arguments) -> BrokerReply:
        call = "c_" + secrets.token_hex(16)
        # Unknown/untrusted names must not become unbounded or non-JSON receipt metadata.
        public_name = name if isinstance(name, str) and len(name) <= 64 else "unknown"
        receipt, outcome, tab_handle = None, "not_dispatched", None
        async with self._lock:
            try:
                admitted = self.session.admit(name, arguments)
                if name not in _IMPLEMENTED:
                    raise OperationRefused("operation_unavailable", "The native adapter is not implemented yet")
                if name == "browser_tabs" and admitted.operation.arguments()["action"] == "list":
                    # An explicit new inventory may establish a new connection; no action is replayed.
                    await self._refresh_connection()
                    admitted = self.session.admit(name, admitted.operation.arguments())
                self.session.recheck(admitted)
                tab_handle = admitted.tab.handle if admitted.tab is not None else None
                action, method, path, body, timeout, reply_limit = self._native_request(admitted)
                read = action in _READS
                token = _REQUEST.set((admitted.connection, call))
                try:
                    outcome = "unknown"  # from this point an HTTP attempt may reach the browser
                    kwargs = {} if body is None else {"json": body}
                    envelope = await asyncio.to_thread(
                        self._transport.request, method, path, start_bridge=False,
                        max_response_bytes=reply_limit,
                        timeout=timeout / 1000 + TRANSPORT_GRACE_SECONDS, **kwargs)
                finally:
                    _REQUEST.reset(token)
                code = envelope.get("code") if isinstance(envelope, dict) and "receipt" not in envelope else None
                if code in _TRANSPORT_ERRORS:
                    outcome = _TRANSPORT_ERRORS[code]
                    if code == "bridge_disconnected" and (envelope.get("state") or {}).get("requestSent") is False:
                        outcome = "not_dispatched"
                    if action in _NAVIGATIONS and outcome != "not_dispatched":
                        self.session.forget_refs(tab_handle)
                    return BrokerReply(_failure(call, public_name, code, outcome, tab_id=tab_handle,
                                                retryable=read and code in _RETRYABLE_READ_ERRORS), None)
                receipt = _receipt(envelope, call)
                outcome = _outcome(receipt, action, read)
                if action in _NAVIGATIONS and outcome != "not_dispatched":
                    # A new document: its refs are new, even where the page looks the same.
                    self.session.forget_refs(tab_handle)
                native = envelope["result"]
                if (not receipt["complete"] or receipt["limited"] or not isinstance(native, dict)
                        or native.get("error") or native.get("ok") is False):
                    code = native.get("code") if isinstance(native, dict) else None
                    code = code if isinstance(code, str) and code in _NATIVE_ERRORS else "browser_operation_failed"
                    if (action in _WAITS and outcome == "dispatched" and receipt["complete"] and not receipt["limited"]
                            and isinstance(native, dict) and native.get("ok") is False and "code" not in native):
                        # The wait ran to its deadline and the page answered: the condition did not hold.
                        return BrokerReply(_failure(call, public_name, "condition_unmet", "observed",
                                                    tab_id=tab_handle, retryable=True), receipt)
                    if isinstance(native, dict) and native.get("timedOut") is True:
                        code = "timeout"
                    state = {}
                    if code == "stale_ref" and isinstance(native.get("state"), dict):
                        if native["state"].get("reason") in _STALE_REF_REASONS:
                            state["reason"] = native["state"]["reason"]
                        self.session.forget_refs(tab_handle, admitted.refs)
                    refused = outcome == "dispatched" and code in (
                        _PAGE_REFUSED_BEFORE_ACTING if action in _PAGE_ACTIONS | _CAPTURES else _REFUSED_BEFORE_ACTING)
                    if code == "tab_closed" and outcome == "dispatched" and tab_handle is not None:
                        self.session.closed_tab(tab_handle)  # the extension observed that it is gone
                    if code in {"stale_connection", "tab_closed"} and tab_handle is not None:
                        code = "stale_tab_handle"
                    if refused:
                        outcome = "not_dispatched"
                    # Any other failed write that received a reply may still have changed the page.
                    elif outcome == "dispatched" and not read:
                        outcome = "unknown"
                        if action == "newTab" and isinstance(native, dict):
                            # The tab exists though its load was not observed: name it, so the
                            # caller inspects or closes it instead of opening another.
                            try:
                                tab_handle = self.session.opened_tab(admitted.connection, native.get("tabId")).handle
                                outcome = "dispatched"
                            except (OperationRefused, ValueError):
                                pass
                    return BrokerReply(_failure(call, public_name, code, outcome, tab_id=tab_handle, state=state,
                                                retryable=read and code in _RETRYABLE_READ_ERRORS), receipt)
                if outcome != "dispatched":
                    raise ValueError("Success without a completed native dispatch")
                if not read and action not in _CAPTURES and native.get("ok") is not True:
                    raise ValueError("Missing native action acknowledgement")
                data, outcome, cut, image = self._data(admitted, native, action)
                result = {"ok": True, "call": call, "operation": name, "outcome": outcome,
                          "data": data, "truncated": cut}
                if tab_handle is not None or action == "newTab":
                    result["tab_id"] = tab_handle or data["tab_id"]
                if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
                    result, image = _failure(call, name, "output_limited", outcome, tab_id=result.get("tab_id")), None
                return BrokerReply(result, receipt, (image,) if image else ())
            except OperationRefused as error:
                return BrokerReply(_failure(call, public_name, error.code, outcome, tab_id=tab_handle), receipt)
            except (ValueError, TypeError, UnicodeError, AttributeError, KeyError, IndexError):
                # A reply of an unexpected shape is a failed call, never an exception to the caller.
                return BrokerReply(_failure(call, public_name, "invalid_bridge_response", outcome, tab_id=tab_handle), receipt)
            # Cancellation intentionally propagates. It does NOT cancel a to_thread HTTP
            # request or prove the browser stopped; remote lifecycle cancellation is pending.
            # The bridge's own scheduler, not this lock, serializes a later call behind it.
