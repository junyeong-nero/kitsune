"""The user's browser as a Python API, through the Kitsune extension and the shared bridge.

connect() returns a Browser and starts the bridge if none runs. Unlike the MCP server, its
requests carry no caps header, so every capability group is available, storage and history
included; mode and protect_user_tabs restrict them as KITSUNE_MODE and KITSUNE_PROTECT_USER_TABS
do for the MCP server. The bridge's shared-browser rules hold for every client
(docs/api-reference.md#shared-browser): tabs open in the background, writes wait while the user
is using a tab, the user's tabs close only with approval, and sensitive fields are redacted.

Failed results raise KitsuneError (kitsune.errors). Actions and waits return the bridge's
result; reads return what they read, with a warning when the bridge cut it at its size cap.
Browser.request reaches every bridge route (docs/api-reference.md#http-bridge-endpoints).
This module imports no MCP code.
"""

import base64
import json
import re
import warnings
from datetime import datetime
from pathlib import Path

from .bridge_client import BridgeClient
from .errors import ConfirmationRequired, ConnectionFailed, KitsuneError, TimedOut, raise_for
from .waits import (
    RESUME_DEADLINE_SECONDS, WORKFLOW_DEADLINE_SECONDS, action_request_seconds, batch_deadline_seconds,
    container_request_seconds,
)

MODES = ("read", "draft", "act")
# A snapshot ref (e12) is sent as ref; any other target as a selector, which the bridge's shared
# resolver reads as CSS, a Playwright-style selector such as text= or role=, or an s1e0/r0 ref
# (docs/api-reference.md#target-selectors). The core MCP tools send targets the same way.
_REF = re.compile(r"e\d+")


def _action_target(target: str) -> dict:
    """An action's target keys: the bridge reads ref over selector, which these routes require."""
    return {"selector": "", "ref": target} if _REF.fullmatch(target) else {"selector": target}


def _scope(target: str | None) -> dict:
    """The optional target of a read or of an action that falls back to the focused element or page."""
    if target is None:
        return {}
    return {"ref": target} if _REF.fullmatch(target) else {"selector": target}


def _tab_id(tab) -> int:
    return tab.id if isinstance(tab, Tab) else tab


def _cut(result: dict, what: str) -> dict:
    """Warn when the bridge cut a read at its size cap."""
    if result.get("truncated"):
        warnings.warn(f"{what} was cut at the bridge's size cap; read a part of the page with a target",
                      stacklevel=3)
    return result


def connect(*, mode: str = "act", protect_user_tabs: bool = False, confirm=None,
            replace_bridge: bool = False) -> "Browser":
    """Connect to the shared bridge, starting it if none runs, and check that the extension is connected.

    mode ("read", "draft" or "act") and protect_user_tabs limit what the bridge accepts, as
    KITSUNE_MODE and KITSUNE_PROTECT_USER_TABS do for the MCP server. confirm, if set, is called
    with the ConfirmationRequired error of an action that needs the user's approval; when it
    returns True, the action is repeated once with its token, so a person can approve it (for
    example with input()). A running bridge of another build raises ConnectionFailed
    (bridge_version_mismatch) unless replace_bridge=True, which restarts it for every client.
    KITSUNE_HTTP_PORT, KITSUNE_TOKEN and KITSUNE_HOME choose the bridge, as for the MCP server.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be read, draft or act, not {mode!r}")
    client = BridgeClient(caps=None, mode=mode, protect_user_tabs=protect_user_tabs, replace_bridge=replace_bridge)
    error = client.ensure_bridge()
    if error:
        raise_for(error)
    client.await_extension()  # a bridge started just now answers before the extension reconnects
    browser = Browser(client, confirm=confirm)
    status = browser.status()
    if not status.get("extension_connected"):
        diagnosis = status.get("diagnosis") or {}
        raise ConnectionFailed({"error": "The Kitsune extension is not connected to the bridge",
                                "code": diagnosis.get("code") or "extension_disconnected", "retryable": True,
                                **{key: diagnosis[key] for key in ("hint", "state") if key in diagnosis}})
    return browser


class Browser:
    """The user's browser: its tabs, command containers and browser-wide data. Get one with connect()."""

    def __init__(self, client: BridgeClient, *, confirm=None):
        self.client = client
        self.confirm = confirm
        self.cookies = Cookies(self)
        self.history = History(self)
        self.bookmarks = Bookmarks(self)

    def request(self, route: str, body: dict | None = None, *, timeout: float = 15) -> dict:
        """Send a bridge request, GET without a body, else POST; return its result or raise KitsuneError.

        With a confirm callback, an action that needs the user's approval (ConfirmationRequired)
        is sent once more with its token when the callback returns True.
        """
        result = self._send(route, body, timeout)
        try:
            return raise_for(result)
        except ConfirmationRequired as held:
            if body is None or self.confirm is None or not self.confirm(held):
                raise
            return raise_for(self._send(route, {**body, "confirmToken": held.token}, timeout))

    def _send(self, route: str, body: dict | None, timeout: float) -> dict:
        if body is None:
            return self.client.request("GET", route, timeout=timeout)
        return self.client.request("POST", route, json=body, timeout=timeout)

    def status(self) -> dict:
        """The bridge's state; diagnosis explains a connection problem (docs/api-reference.md#connection-diagnosis)."""
        return self.request("/api/status")

    def tabs(self) -> list[dict]:
        """Every open tab: id, url, title, active, windowId, index, and owned for a tab a client opened."""
        return self.request("/api/tabs")["tabs"]

    def tab(self, tab_id: int) -> "Tab":
        return Tab(self, tab_id)

    def active_tab(self) -> "Tab":
        """The tab the user is viewing. With several windows each has one, so pick it from tabs()."""
        active = [tab["id"] for tab in self.tabs() if tab.get("active")]
        if not active:
            raise_for({"error": "No active tab found", "code": "browser_window_unavailable", "retryable": True})
        if len(active) > 1:
            raise_for({"error": "Several windows have an active tab; pick one from tabs()",
                       "code": "ambiguous_target", "retryable": False, "state": {"tabIds": active}})
        return Tab(self, active[0])

    def new_tab(self, url: str = "about:blank", *, wait: bool = True, timeout: int = 15000,
                confirm: str | None = None) -> "Tab":
        """Open url in a background tab that the user does not see, which a client owns.

        wait=True waits up to timeout ms for the load; a load that does not finish raises
        TimedOut and leaves the tab open, with its id in error.result["tabId"].
        """
        body = {"url": url}
        if confirm is not None:
            body["confirmToken"] = confirm
        if not wait:
            return Tab(self, self.request("/api/new-tab", body)["tabId"])
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        result = self.request("/api/new-tab", {**body, "wait": True, "timeout": timeout}, timeout=timeout / 1000 + 10)
        return Tab(self, result["tabId"])

    def batch(self, commands: list[dict], *, tab=None) -> dict:
        """Run batch commands (docs/api-reference.md#post-apibatch) one after another in one request.

        tab is the tab for commands that name no tabId. A failed command does not raise:
        results and progress report each one, and earlier commands may have taken effect.
        """
        body = {"commands": commands}
        if tab is not None:
            body["tabId"] = _tab_id(tab)
        return self.request("/api/batch", body, timeout=container_request_seconds(batch_deadline_seconds(commands)))

    def parallel(self, sequences: list[list[dict]]) -> dict:
        """Run command sequences concurrently, one per tab; every command that acts on a tab names its tabId."""
        command = {"action": "parallel", "sequences": sequences}
        return self.request("/api/batch", {"commands": [command]},
                            timeout=container_request_seconds(batch_deadline_seconds([command])))

    def workflows(self) -> list[str]:
        """The names of the workflows the bridge ships."""
        return self.request("/api/workflows")["workflows"]

    def workflow(self, name: str, variables: dict | None = None, *, tab=None) -> dict:
        """Run a named workflow; tab is the tab for steps that name no tabId."""
        body = {"name": name, "variables": variables or {}}
        if tab is not None:
            body["tabId"] = _tab_id(tab)
        return self.request("/api/workflow", body, timeout=container_request_seconds(WORKFLOW_DEADLINE_SECONDS))

    def resume(self, call: int, resume_token: str, observed: dict[str, str] | None = None) -> dict:
        """Resume a batch, parallel or workflow call that did not finish ok (docs/checkpoint-resume.md)."""
        body = {"call": call, "resume_token": resume_token}
        if observed is not None:
            body["observed"] = observed
        return self.request("/api/resume", body, timeout=container_request_seconds(RESUME_DEADLINE_SECONDS))


class Cookies:
    """The browser's cookies."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def _op(self, op: str, confirm: str | None = None, **fields) -> dict:
        body = {"op": op, **{key: value for key, value in fields.items() if value is not None}}
        if confirm is not None:
            body["confirmToken"] = confirm
        return self._browser.request("/api/cookies", body)

    def get(self, *, url: str | None = None, name: str | None = None, domain: str | None = None) -> list[dict]:
        return self._op("get", url=url, name=name, domain=domain)["cookies"]

    def set(self, url: str, name: str, value: str, *, domain: str | None = None, path: str | None = None,
            secure: bool = False, confirm: str | None = None) -> dict:
        return self._op("set", confirm, url=url, name=name, value=value, domain=domain, path=path, secure=secure)

    def remove(self, url: str, name: str, *, confirm: str | None = None) -> dict:
        return self._op("remove", confirm, url=url, name=name)

    def clear(self, *, url: str | None = None, domain: str | None = None, confirm: str | None = None) -> dict:
        """Remove every cookie of url or domain."""
        return self._op("clear", confirm, url=url, domain=domain)


class History:
    """The user's browsing history, within the bridge's history scope (docs/api-reference.md#history-and-bookmarks)."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def search(self, query: str = "", *, since: datetime | None = None, limit: int = 20) -> list[dict]:
        """Visited pages whose title or URL contains query, newest first; a naive since is local time."""
        body = {"query": query, "limit": limit}
        if since is not None:
            body["since"] = int(since.timestamp() * 1000)
        return self._browser.request("/api/history-search", body)["results"]

    def visits(self, url: str) -> list[dict]:
        """The visits to a URL that search returned, newest first."""
        return self._browser.request("/api/history-visits", {"url": url})["visits"]


class Bookmarks:
    """The user's bookmarks, within the bridge's history domain scope."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def search(self, query: str = "", *, limit: int = 20) -> list[dict]:
        return self._browser.request("/api/bookmarks-search", {"query": query, "limit": limit})["results"]


class Tab:
    """One tab, by id. Every method sends its tabId, so it acts on this tab whether or not the user
    is viewing it.

    Targets are a snapshot ref (e12), CSS or a Playwright-style selector
    (docs/api-reference.md#target-selectors). A write's selector must match one element, else
    it raises ambiguous_target; first=True acts on the first match. ok=True means an action
    was dispatched; expect makes the bridge check its outcome (docs/api-reference.md#action-outcomes).
    confirm is the token of a ConfirmationRequired error, after the user approved the action.
    """

    def __init__(self, browser: Browser, tab_id: int):
        self.browser = browser
        self.id = tab_id

    def __repr__(self) -> str:
        return f"<Tab {self.id}>"

    def __eq__(self, other) -> bool:
        return isinstance(other, Tab) and other.browser is self.browser and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def _request(self, route: str, body: dict | None = None, *, timeout: float = 15) -> dict:
        return self.browser.request(route, {**(body or {}), "tabId": self.id}, timeout=timeout)

    def _action(self, route: str, body: dict, *, wait: bool = False, timeout: int = 10000,
                expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        if wait:
            if timeout <= 0:
                raise ValueError("timeout must be positive")
            body = {**body, "wait": True, "timeout": timeout}
        if expect is not None:
            body = {**body, "expect": expect}
        if first:
            body = {**body, "first": True}
        if confirm is not None:
            body = {**body, "confirmToken": confirm}
        return self._request(route, body, timeout=action_request_seconds(wait, timeout, expect))

    # -- Reading ---------------------------------------------------

    def info(self) -> dict:
        """url, title, readyState, lang, sizes, scroll position, element counts and recent dialogs."""
        return self._request("/api/page-info")

    def text(self) -> str:
        """The page's visible text; "" for an HTTP error the browser shows without a document."""
        return _cut(self._request("/api/page-text"), "Page text")["text"]

    def snapshot(self, target: str | None = None, *, max_depth: int | None = None, text: bool = False) -> str:
        """The accessibility-style tree of the page, or of target's subtree, with refs for actions.

        max_depth limits it below the root; text=True adds visible text lines (capped at
        12,000 characters). Refs survive later snapshots until the node is replaced or the tab
        navigates.
        """
        if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
            raise ValueError("max_depth must be a non-negative integer")
        body = {**_scope(target), "text": text}
        if max_depth is not None:
            body["maxDepth"] = max_depth
        return _cut(self._request("/api/snapshot", body), "Snapshot")["snapshot"]

    def forms(self) -> list[dict]:
        """Every form field with its label and value; sensitive values read redacted/hasValue instead."""
        return self._request("/api/forms")["fields"]

    def find(self, query: str) -> list[dict]:
        """Elements matching a description such as "login button", best first, each with a ref."""
        return self._request("/api/find", {"query": query})["results"]

    def query(self, selector: str, *, fields: list[str] | None = None, limit: int = 50) -> list[dict]:
        """Fields (text, href, value, src, alt, id, name by default) of every element matching a CSS selector."""
        body = {"selector": selector, "limit": limit}
        if fields:
            body["fields"] = fields
        return self._request("/api/query", body)["items"]

    def html(self, target: str) -> str:
        """The outerHTML of one element ("html" for the whole page), with sensitive values redacted."""
        return _cut(self._request("/api/html", _action_target(target)), "HTML")["html"]

    def markdown(self, target: str | None = None) -> str:
        """The page, or target's subtree, as Markdown."""
        return _cut(self._request("/api/markdown", _scope(target)), "Markdown")["markdown"]

    def links(self, *, internal_only: bool = False) -> list[dict]:
        return self._request("/api/links", {"internalOnly": internal_only})["links"]

    def screenshot(self, path: str | Path | None = None, *, target: str | None = None, full_page: bool = False,
                   format: str = "png") -> bytes:
        """The viewport, one element (target) or the full page as PNG or JPEG bytes, also written to path.

        No capture shows the tab or focuses a window.
        """
        if target is not None and full_page:
            raise ValueError("target and full_page cannot be combined")
        if format not in ("png", "jpeg"):
            raise ValueError("format must be png or jpeg")
        body = {"imagePreview": False, **({"format": format} if format != "png" else {})}
        if target is not None:
            route, seconds = "/api/element-screenshot", 45
            body.update(_action_target(target))
        elif full_page:
            route, seconds = "/api/full-page-screenshot", 120
        else:
            route, seconds = "/api/screenshot", 15
        data = base64.b64decode(self._request(route, body, timeout=seconds)["dataUrl"].split(";base64,", 1)[1])
        if path is not None:
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def evaluate(self, function: str, target: str | None = None, *, confirm: str | None = None):
        """Call a JavaScript function in the page and return its JSON value, None for undefined.

        evaluate("() => document.title"), or evaluate("el => el.textContent", "e12") to pass the
        target element. Needs the developer extension build; the signed build refuses it.
        """
        # As the core browser_evaluate: the extension binds the target as __kitsuneElement.
        argument = "__kitsuneElement" if target is not None else ""
        body = {"code": f"Promise.resolve(({function})({argument})).then(value => JSON.stringify(value) ?? 'undefined')"}
        if target is not None:
            body["selector"] = target
        if confirm is not None:
            body["confirmToken"] = confirm
        value = _cut(self._request("/api/js", body), "The result")["result"]
        if value == "undefined":
            return None
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except ValueError:
            return value  # cut at the size cap

    # -- Navigation and tabs ---------------------------------------

    def navigate(self, url: str, *, expect: dict | None = None, confirm: str | None = None) -> dict:
        return self._action("/api/navigate", {"url": url}, expect=expect, confirm=confirm)

    def back(self, *, expect: dict | None = None) -> dict:
        return self._action("/api/back", {}, expect=expect)

    def forward(self, *, expect: dict | None = None) -> dict:
        return self._action("/api/forward", {}, expect=expect)

    def reload(self, *, bypass_cache: bool = False, expect: dict | None = None) -> dict:
        return self._action("/api/reload-tab", {"bypassCache": bypass_cache}, expect=expect)

    def show(self) -> dict:
        """Switch the user's view to this tab. Only on the user's request: it interrupts what they see."""
        return self._request("/api/switch-tab")

    def wake(self) -> dict:
        """Reload the tab if the browser discarded it, and wait until the page answers."""
        return self._request("/api/wake-tab", timeout=25)

    def close(self, *, force: bool = False, confirm: str | None = None) -> dict:
        """Close a tab a client opened. One of the user's own tabs raises ConfirmationRequired
        (user_tab) and closes with its token once the user approves. force disarms beforeunload."""
        body = {"force": True} if force else {}
        if confirm is not None:
            body["confirmToken"] = confirm
        return self._request("/api/close-tab", body)

    # -- Actions ---------------------------------------------------

    def click(self, target: str, *, button: str | None = None, modifiers: list[str] | None = None,
              wait: bool = False, timeout: int = 10000, expect: dict | None = None, first: bool = False,
              confirm: str | None = None) -> dict:
        """Click an element with synthetic events; tabs the click opens come back in opened_tabs."""
        body = _action_target(target)
        if button is not None:
            body["button"] = button
        if modifiers:
            body["modifiers"] = modifiers
        return self._action("/api/click", body, wait=wait, timeout=timeout, expect=expect, first=first,
                            confirm=confirm)

    def double_click(self, target: str, *, wait: bool = False, timeout: int = 10000, expect: dict | None = None,
                     first: bool = False, confirm: str | None = None) -> dict:
        return self._action("/api/double-click", _action_target(target), wait=wait, timeout=timeout,
                            expect=expect, first=first, confirm=confirm)

    def hover(self, target: str, *, wait: bool = False, timeout: int = 10000, expect: dict | None = None,
              first: bool = False) -> dict:
        return self._action("/api/hover", _action_target(target), wait=wait, timeout=timeout, expect=expect,
                            first=first)

    def type(self, target: str, text: str, *, clear: bool = False, wait: bool = False, timeout: int = 10000,
             expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        """Type text into a field or editor with key events, clearing it first if clear."""
        return self._action("/api/type", {**_action_target(target), "text": text, "clear": clear}, wait=wait,
                            timeout=timeout, expect=expect, first=first, confirm=confirm)

    def fill(self, target: str, value: str, *, wait: bool = False, timeout: int = 10000,
             expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        """Replace a field's or editor's value, as a framework-aware setter with input and change events."""
        return self._action("/api/fill", {**_action_target(target), "value": value}, wait=wait, timeout=timeout,
                            expect=expect, first=first, confirm=confirm)

    def fill_form(self, fields: dict, *, expect: dict | None = None, first: bool = False,
                  confirm: str | None = None) -> dict:
        """Fill several fields, each key matched as a CSS selector, name, label or placeholder;
        returns per-key results."""
        return self._action("/api/form-fill", {"fields": fields}, expect=expect, first=first, confirm=confirm)

    def press(self, key: str, target: str | None = None, *, modifiers: dict | None = None, text: str | None = None,
              wait: bool = False, timeout: int = 10000, expect: dict | None = None, first: bool = False,
              confirm: str | None = None) -> dict:
        """Press a key ("Enter", "ArrowDown", "a") on target, else on the focused element.

        modifiers is {"ctrl"|"shift"|"alt"|"meta": True}; text also inserts it into an editable target.
        """
        body = {"key": key, **_scope(target)}
        if modifiers:
            body["modifiers"] = modifiers
        if text is not None:
            body["text"] = text
        return self._action("/api/keypress", body, wait=wait, timeout=timeout, expect=expect, first=first,
                            confirm=confirm)

    def select_option(self, target: str, value: str, *, by_text: bool = False, wait: bool = False,
                      timeout: int = 10000, expect: dict | None = None, first: bool = False,
                      confirm: str | None = None) -> dict:
        """Choose the <option> of a <select> by value, or by visible text with by_text."""
        return self._action("/api/select-option", {**_action_target(target), "value": value, "byText": by_text},
                            wait=wait, timeout=timeout, expect=expect, first=first, confirm=confirm)

    def check(self, target: str, checked: bool = True, *, wait: bool = False, timeout: int = 10000,
              expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        """Set a checkbox or radio button's checked state."""
        return self._action("/api/check", {**_action_target(target), "checked": checked}, wait=wait,
                            timeout=timeout, expect=expect, first=first, confirm=confirm)

    def uncheck(self, target: str, **options) -> dict:
        return self.check(target, False, **options)

    def scroll(self, direction: str = "down", amount: int = 500, *, target: str | None = None,
               expect: dict | None = None, first: bool = False) -> dict:
        """Scroll target, else the window: up, down, left or right by amount pixels, or to top or bottom.
        scrolled_by is how far it moved."""
        return self._action("/api/scroll", {"direction": direction, "amount": amount, **_scope(target)},
                            expect=expect, first=first)

    def submit_form(self, target: str | None = None, *, expect: dict | None = None, first: bool = False,
                    confirm: str | None = None) -> dict:
        """Submit target's form, else that of the field last filled or focused, else the page's only form."""
        return self._action("/api/submit-form", _scope(target), expect=expect, first=first, confirm=confirm)

    def handle_dialogs(self, accept: bool | None = True, prompt_text: str | None = None, *,
                       confirm: str | None = None) -> dict:
        """Answer this tab's future alert/confirm/prompt dialogs: accept or dismiss them, or None to
        leave them to the user again. Returns the policy and recent dialogs."""
        body = {"accept": accept, "prompt_text": prompt_text}
        if confirm is not None:
            body["confirmToken"] = confirm
        return self._request("/api/handle-dialog", body)

    # -- Waiting ---------------------------------------------------

    def wait_for(self, *, target: str | None = None, text: str | None = None, text_gone: str | None = None,
                 url: str | None = None, title: str | None = None, js: str | None = None,
                 network_idle: bool = False, timeout: int = 10000, confirm: str | None = None) -> dict:
        """Wait up to timeout ms for exactly one condition, else raise TimedOut.

        target: a visible element. text, text_gone: a literal substring of the rendered page
        text (snapshot names from placeholders or aria-labels are not page text). url, title: a
        JavaScript regex on the tab, which survives navigation. js: an expression with a
        non-empty result (developer build only). network_idle: 500 ms without new resources.
        """
        conditions = (target, text, text_gone, url, title, js)
        if sum(condition is not None for condition in conditions) + bool(network_idle) != 1:
            raise ValueError("Pass exactly one of target, text, text_gone, url, title, js, network_idle")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        body = {"timeout": timeout}
        if target is not None:
            route = "/api/wait-for-element"
            body.update(_scope(target), pollInterval=200)
        elif text is not None or text_gone is not None:
            if (text if text is not None else text_gone) == "":
                raise ValueError("text and text_gone must be non-empty")
            route = "/api/wait-for-text"
            body.update(text=text, text_gone=text_gone)
        elif js is not None:
            route = "/api/wait-for-result"
            body.update(code=js, pollInterval=500)
            if confirm is not None:
                body["confirmToken"] = confirm
        elif url is not None or title is not None:
            route = "/api/wait-for-url" if url is not None else "/api/wait-for-title"
            body["pattern"] = url if url is not None else title
        else:
            route = "/api/wait-for-network-idle"
            body["idleMs"] = 500
        try:
            return self._request(route, body, timeout=timeout / 1000 + 10)
        except KitsuneError as error:
            # A wait that ran out of time answers ok=false with an unclassified error.
            if type(error) is KitsuneError and error.code is None and error.result.get("ok") is False:
                raise TimedOut(error.result) from None
            raise
