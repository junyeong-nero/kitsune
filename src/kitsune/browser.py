"""The user's browser as a Python API, through the Kitsune extension and the shared bridge.

connect() returns a Browser and starts the bridge if none runs. Unlike the MCP server, its
requests carry no caps header, so every capability group is available, storage and history
included; mode restricts them as KITSUNE_MODE does for the MCP server. The bridge's
shared-browser rules hold for every client (docs/api-reference.md#shared-browser): tabs open in
the background, writes wait while the user is using a tab, the user's tabs close or navigate
only with approval, and sensitive fields are redacted.

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
    ACTION_REQUEST_SECONDS, NAVIGATE_REQUEST_SECONDS, RESUME_DEADLINE_SECONDS, WORKFLOW_DEADLINE_SECONDS,
    action_request_seconds, batch_deadline_seconds, command_wait_seconds, container_request_seconds,
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


def _tab_ids(tabs) -> list[int]:
    return [_tab_id(tab) for tab in tabs]


def _with_confirm(body: dict, confirm: str | None) -> dict:
    """body with the confirm token of an approved action, if any."""
    return {**body, "confirmToken": confirm} if confirm is not None else body


def _epoch_ms(moment: datetime) -> int:
    """Epoch milliseconds of a datetime; a naive one is local time."""
    return int(moment.timestamp() * 1000)


def _cut(result: dict, what: str) -> dict:
    """Warn when the bridge cut a read at its size cap."""
    if result.get("truncated"):
        warnings.warn(f"{what} was cut at the bridge's size cap; read a part of the page with a target",
                      stacklevel=3)
    return result


def connect(*, mode: str = "act", protect_user_tabs: bool = True, hold_viewed_tab: bool = True, confirm=None,
            replace_bridge: bool = False) -> "Browser":
    """Connect to the shared bridge, starting it if none runs, and check that the extension is connected.

    mode ("read", "draft" or "act") limits what the bridge accepts, as KITSUNE_MODE does for the
    MCP server; protect_user_tabs=False lets navigations move tabs no client opened without approval,
    as KITSUNE_PROTECT_USER_TABS=0 does, and hold_viewed_tab=False lifts the hold on the tab the user
    is viewing, as KITSUNE_HOLD_VIEWED_TAB=0 does. confirm, if set, is called
    with the ConfirmationRequired error of an action that needs the user's approval; when it
    returns True, the action is repeated once with its token, so a person can approve it (for
    example with input()). A running bridge of another build raises ConnectionFailed
    (bridge_version_mismatch) unless replace_bridge=True, which restarts it for every client.
    KITSUNE_HTTP_PORT, KITSUNE_TOKEN and KITSUNE_HOME choose the bridge, as for the MCP server.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be read, draft or act, not {mode!r}")
    client = BridgeClient(caps=None, mode=mode, protect_user_tabs=protect_user_tabs, hold_viewed_tab=hold_viewed_tab,
                          replace_bridge=replace_bridge)
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
        self.windows = Windows(self)
        self.downloads = Downloads(self)
        self.clipboard = Clipboard(self)
        self.sessions = Sessions(self)
        self.tags = Tags(self)
        self.pool = Pool(self)
        self.intercept = Intercept(self)
        self.policy = Policy(self)

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

    def status(self, *, diagnostics: bool = False) -> dict:
        """Connection state; diagnostics adds read-only browser build comparison (macOS)."""
        return self.request("/api/status?diagnostics=true" if diagnostics else "/api/status")

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
        body = _with_confirm({"url": url}, confirm)
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

    def retry(self, command: dict, *, max_attempts: int = 3, backoff_ms: int = 500,
              on_errors: list[str] | None = None, tab=None) -> dict:
        """Run one batch command until it succeeds, max_attempts times with exponential backoff; only
        retryable failures (or on_errors codes) are retried."""
        body = {"command": command, "maxAttempts": max_attempts, "backoffMs": backoff_ms}
        if on_errors:
            body["onErrors"] = on_errors
        timeout = max(60, command_wait_seconds({"action": "retry", **body}) + 15)
        if tab is not None:
            body["tabId"] = _tab_id(tab)
        return self.request("/api/retry", body, timeout=timeout)

    # -- Several tabs ----------------------------------------------

    def keep_alive(self, tabs, *, interval_seconds: int = 60) -> dict:
        """Ping tabs every interval_seconds, so the browser's tab unloader does not discard them."""
        return self.request("/api/keep-alive", {"tabIds": _tab_ids(tabs), "intervalSeconds": interval_seconds})

    def stop_keep_alive(self, tabs=None) -> dict:
        """Stop keep_alive for tabs, else for every tab."""
        return self.request("/api/keep-alive-stop", {} if tabs is None else {"tabIds": _tab_ids(tabs)})

    def broadcast(self, tabs, command: dict, *, timeout: int = 30) -> dict:
        """Run one batch command in several tabs at once, within timeout seconds."""
        return self.request("/api/broadcast", {"tabIds": _tab_ids(tabs), "command": command, "timeout": timeout},
                            timeout=timeout + 10)

    def sync_barrier(self, tabs, predicate: str, *, timeout: int = 30000, poll_interval: int = 300,
                     confirm: str | None = None) -> dict:
        """Wait up to timeout ms until a JavaScript predicate holds in every tab (developer build)."""
        body = {"tabIds": _tab_ids(tabs), "predicate": predicate, "timeout": timeout, "pollInterval": poll_interval}
        return self.request("/api/sync-barrier", _with_confirm(body, confirm), timeout=timeout / 1000 + 10)

    # -- Browser data ----------------------------------------------

    def clear_browsing_data(self, types: list[str], *, since: datetime | None = None) -> dict:
        """Remove the named kinds of browsing data ("cache", "cookies", "history", "localStorage",
        "passwords", "downloads"), only since a time if given.

        types has no default: without it the extension removes every one of these kinds.
        """
        if not types:
            raise ValueError("Name the kinds of browsing data to remove")
        body = {"types": list(types)}
        if since is not None:
            body["since"] = _epoch_ms(since)
        return self.request("/api/clear-browsing-data", body, timeout=60)

    # -- The bridge ------------------------------------------------

    def version(self) -> dict:
        """The bridge's and the extension's versions."""
        return self.request("/api/version")

    def health(self) -> dict:
        """Bridge version, ports, keep-alive tabs, pool, tags, policy, sessions directory and log sizes."""
        return self.request("/api/health", {})

    def logs(self, *, limit: int = 200, since: str | None = None) -> list[str]:
        """The bridge's recent log lines."""
        body = {"limit": limit}
        if since:
            body["since"] = since
        return self.request("/api/logs", body)["lines"]

    def audit(self, *, limit: int = 100, since: datetime | None = None) -> list[dict]:
        """The bridge's audit log of every client's commands, newest last."""
        body = {"limit": limit, "since": _epoch_ms(since) if since is not None else 0}
        return self.request("/api/audit", body)["entries"]

    def scheduler(self) -> dict:
        """The bridge's active per-tab and global command scopes, pending commands and tab locks."""
        return self.request("/api/scheduler", {})

    def cache(self, action: str = "status", *, seconds: int | None = None) -> dict:
        """The bridge's read cache: "status", "clear", or "ttl" with seconds."""
        body = {"action": action}
        if seconds is not None:
            body["seconds"] = seconds
        return self.request("/api/cache", body)

    def reload_extension(self) -> dict:
        """Reload the extension, for example after editing a developer build."""
        return self.request("/api/reload-extension", {}, timeout=10)


class Cookies:
    """The browser's cookies."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def _op(self, op: str, confirm: str | None = None, **fields) -> dict:
        body = {"op": op, **{key: value for key, value in fields.items() if value is not None}}
        return self._browser.request("/api/cookies", _with_confirm(body, confirm))

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
            body["since"] = _epoch_ms(since)
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


class Windows:
    """The browser's windows."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def list(self) -> list[dict]:
        """Every window with its id, focused/incognito state and tabs."""
        return self._browser.request("/api/windows", {})["windows"]

    def create(self, url: str | None = None, *, incognito: bool = False, confirm: str | None = None) -> dict:
        """Open a window a client owns; returns its windowId and tabIds."""
        body = {"incognito": incognito, **({"url": url} if url else {})}
        return self._browser.request("/api/create-window", _with_confirm(body, confirm))

    def close(self, window_id: int, *, confirm: str | None = None) -> dict:
        """Close a window whose tabs a client opened; one with the user's tabs needs their approval."""
        return self._browser.request("/api/close-window", _with_confirm({"windowId": window_id}, confirm))

    def focus(self, window_id: int) -> dict:
        """Bring a window to the front. Only on the user's request: it takes their focus."""
        return self._browser.request("/api/focus-window", {"windowId": window_id})


class Downloads:
    """Downloads; ask-before-act holds every operation."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def _op(self, op: str, confirm: str | None, **fields) -> dict:
        body = {"op": op, **{key: value for key, value in fields.items() if value}}
        return self._browser.request("/api/downloads", _with_confirm(body, confirm))

    def start(self, url: str, filename: str | None = None, *, confirm: str | None = None) -> int:
        """Download url into the browser's download folder; returns the download id."""
        return self._op("download", confirm, url=url, filename=filename)["downloadId"]

    def list(self, query: dict | None = None, *, confirm: str | None = None) -> list[dict]:
        """Downloads matching a downloads.search query, by default the 20 latest."""
        return self._op("list", confirm, query=query)["items"]

    def cancel(self, download_id: int, *, confirm: str | None = None) -> dict:
        return self._op("cancel", confirm, query={"id": download_id})


class Clipboard:
    """The system clipboard, which reaches outside the browser; ask-before-act holds both operations."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def read(self, *, confirm: str | None = None) -> str:
        return self._browser.request("/api/clipboard", _with_confirm({"op": "read"}, confirm))["text"]

    def write(self, text: str, *, confirm: str | None = None) -> dict:
        return self._browser.request("/api/clipboard", _with_confirm({"op": "write", "text": text}, confirm))


class Sessions:
    """Saved sessions: cookies and page storage of a set of sites, stored by the bridge."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def save(self, name: str, urls: list[str] | None = None, *, confirm: str | None = None) -> dict:
        """Save the browser's cookies, and the localStorage of each of urls' origins, under name."""
        return self._browser.request("/api/save-session", _with_confirm({"name": name, "urls": urls or []}, confirm),
                                     timeout=120)

    def load(self, name: str, *, confirm: str | None = None) -> dict:
        return self._browser.request("/api/load-session", _with_confirm({"name": name}, confirm), timeout=120)

    def list(self) -> list[dict]:
        return self._browser.request("/api/list-sessions", {})["sessions"]

    def delete(self, name: str) -> dict:
        return self._browser.request("/api/delete-session", {"name": name})


class Tags:
    """Names for tabs, kept by the bridge while it runs (Tab.tag adds one)."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def list(self) -> dict[str, int]:
        """Every tag and the id of its tab."""
        return self._browser.request("/api/list-tags", {})["tags"]

    def tab(self, name: str) -> "Tab":
        """The tab with this tag."""
        return Tab(self._browser, self._browser.request("/api/resolve-tag", {"name": name})["tabId"])

    def remove(self, name: str) -> dict:
        return self._browser.request("/api/untag-tab", {"name": name})


class Pool:
    """A pool of reusable background tabs."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def create(self, size: int, url: str = "about:blank") -> dict:
        """Open size tabs at url for acquire to hand out."""
        return self._browser.request("/api/tab-pool", {"size": size, "url": url})

    def acquire(self) -> "Tab":
        """A free tab of the pool, which grows when every tab is in use."""
        return Tab(self._browser, self._browser.request("/api/pool-acquire", {})["tabId"])

    def release(self, tab) -> dict:
        return self._browser.request("/api/pool-release", {"tabId": _tab_id(tab)})


class Intercept:
    """Request interception for every tab: block or redirect requests whose URL matches a regex."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def _op(self, op: str, patterns: list[str] | None = None, effect: str = "block") -> dict:
        body = {"op": op, "effect": effect, **({"patterns": patterns} if patterns else {})}
        return self._browser.request("/api/intercept", body)

    def add(self, patterns: list[str], effect: str = "block") -> dict:
        return self._op("add", patterns, effect)

    def clear(self) -> dict:
        return self._op("clear")

    def rules(self) -> list[dict]:
        return self._op("list")["rules"]

    def log(self) -> list[dict]:
        """The latest requests seen: time, url, method, tabId, type."""
        return self._op("log")["requests"]


class Policy:
    """The bridge's URL allow/deny lists, read-only and ask-before-act modes, and history scope."""

    def __init__(self, browser: Browser):
        self._browser = browser

    def get(self) -> dict:
        return self._browser.request("/api/get-policy", {})

    def set(self, *, allow: list[str] | None = None, deny: list[str] | None = None, readonly: bool | None = None,
            ask: bool | None = None, ask_urls: list[str] | None = None,
            history_allow_domains: list[str] | None = None, history_deny_domains: list[str] | None = None,
            history_max_days: int | None = None) -> dict:
        """Change the given settings for every client of the bridge; a locked ask-before-act stays on."""
        fields = {"allow": allow, "deny": deny, "readonly": readonly, "ask": ask, "askUrls": ask_urls,
                  "historyAllowDomains": history_allow_domains, "historyDenyDomains": history_deny_domains,
                  "historyMaxDays": history_max_days}
        return self._browser.request("/api/set-policy", {key: value for key, value in fields.items() if value is not None})


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
        self.network = Network(self)
        self.local_storage = PageStorage(self, "local")
        self.session_storage = PageStorage(self, "session")

    def __repr__(self) -> str:
        return f"<Tab {self.id}>"

    def __eq__(self, other) -> bool:
        return isinstance(other, Tab) and other.browser is self.browser and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def _request(self, route: str, body: dict | None = None, *, timeout: float = 15) -> dict:
        return self.browser.request(route, {**(body or {}), "tabId": self.id}, timeout=timeout)

    def _action(self, route: str, body: dict, *, wait: bool = False, timeout: int = 10000,
                expect: dict | None = None, first: bool = False, confirm: str | None = None,
                pointer_ms: int = 0, base_seconds: float = ACTION_REQUEST_SECONDS) -> dict:
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
        return self._request(route, body,
                             timeout=action_request_seconds(wait, timeout, expect, base_seconds) + pointer_ms / 1000)

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
        target element. It runs in the extension's content-script world, which sees the page's DOM
        and reaches the page's own JavaScript globals only through window.wrappedJSObject. Needs
        the developer extension build; the signed build refuses it.
        """
        # As the core browser_evaluate: the extension binds the target as __kitsuneElement.
        argument = "__kitsuneElement" if target is not None else ""
        body = {"code": f"Promise.resolve(({function})({argument})).then(value => JSON.stringify(value) ?? 'undefined')"}
        if target is not None:
            body["selector"] = target
        value = _cut(self._request("/api/js", _with_confirm(body, confirm)), "The result")["result"]
        if value == "undefined":
            return None
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except ValueError:
            return value  # cut at the size cap

    # -- Inspecting ------------------------------------------------

    def dom(self, target: str | None = None, *, max_depth: int | None = None) -> dict:
        """The accessibility tree of interactive elements (6 levels by default), of the page or of target."""
        if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
            raise ValueError("max_depth must be a non-negative integer")
        body = _scope(target)
        if max_depth is not None:
            body["maxDepth"] = max_depth
        return _cut(self._request("/api/dom", body), "The DOM tree")["tree"]

    def images(self) -> list[dict]:
        return self._request("/api/images")["images"]

    def iframes(self) -> list[dict]:
        return self._request("/api/iframes")["iframes"]

    def meta(self) -> dict:
        """The page's title, URL, canonical link, meta tags (meta) and <link> rels (links)."""
        return self._request("/api/meta")

    def structured_data(self) -> dict:
        """The page's JSON-LD (jsonld), Open Graph (openGraph) and Twitter card (twitter) data."""
        return self._request("/api/structured-data")

    def readability(self) -> dict:
        """The page's main article: title, byline, published date, excerpt and text (textContent)."""
        return self._request("/api/readability")

    def bounds(self, target: str) -> dict:
        """An element's bounding box, the viewport and page sizes, and whether it is in view (inView)."""
        return self._request("/api/bounds", _action_target(target))

    def computed_style(self, target: str, properties: list[str] | None = None) -> dict:
        """An element's computed CSS, a useful set of properties by default."""
        body = _action_target(target)
        if properties:
            body["properties"] = properties
        return self._request("/api/computed-style", body)["computed"]

    def explain_selector(self, selector: str) -> dict:
        """How many elements a CSS selector matches, samples of them, and more specific suggestions."""
        return self._request("/api/explain-selector", {"selector": selector})

    def full_page_metrics(self) -> dict:
        """The document's size, the viewport, the scroll position and the device pixel ratio."""
        return self._request("/api/full-page-metrics")

    def highlight(self, target: str) -> dict:
        """Outline an element on the page until clear_highlight."""
        return self._request("/api/highlight", _action_target(target))

    def clear_highlight(self) -> dict:
        return self._request("/api/clear-highlight")

    def watch_console(self, enabled: bool = True) -> dict:
        """Start (or stop) recording the page's console messages, for console_logs."""
        return self._request("/api/watch-console", {"enabled": enabled})

    def console_logs(self, *, since: int | None = None, level: str | None = None) -> list[dict]:
        """Console messages recorded since watch_console, the latest 200; since is epoch ms."""
        body = {}
        if since is not None:
            body["since"] = since
        if level:
            body["level"] = level
        return self._request("/api/console-logs", body)["logs"]

    # -- Navigation and tabs ---------------------------------------

    def navigate(self, url: str, *, expect: dict | None = None, confirm: str | None = None) -> dict:
        return self._action("/api/navigate", {"url": url}, expect=expect, confirm=confirm,
                            base_seconds=NAVIGATE_REQUEST_SECONDS)

    def back(self, *, expect: dict | None = None, confirm: str | None = None) -> dict:
        return self._action("/api/back", {}, expect=expect, confirm=confirm)

    def forward(self, *, expect: dict | None = None, confirm: str | None = None) -> dict:
        return self._action("/api/forward", {}, expect=expect, confirm=confirm)

    def reload(self, *, bypass_cache: bool = False, expect: dict | None = None, confirm: str | None = None) -> dict:
        return self._action("/api/reload-tab", {"bypassCache": bypass_cache}, expect=expect, confirm=confirm)

    def show(self) -> dict:
        """Switch the user's view to this tab. Only on the user's request: it interrupts what they see."""
        return self._request("/api/switch-tab")

    def wake(self) -> dict:
        """Reload the tab if the browser discarded it, and wait until the page answers."""
        return self._request("/api/wake-tab", timeout=25)

    def close(self, *, force: bool = False, confirm: str | None = None) -> dict:
        """Close a tab a client opened. One of the user's own tabs raises ConfirmationRequired
        (needs_user, detail user_tab) and closes with its token once the user approves. force disarms
        beforeunload."""
        return self._request("/api/close-tab", _with_confirm({"force": True} if force else {}, confirm))

    def pin(self, pinned: bool = True) -> dict:
        return self._request("/api/pin-tab", {"pinned": pinned})

    def mute(self, muted: bool = True) -> dict:
        return self._request("/api/mute-tab", {"muted": muted})

    def duplicate(self) -> "Tab":
        """A copy of this tab, in the background, which a client owns."""
        return Tab(self.browser, self._request("/api/duplicate-tab")["tabId"])

    def zoom(self) -> float:
        return self._request("/api/get-zoom")["factor"]

    def set_zoom(self, factor: float) -> dict:
        return self._request("/api/set-zoom", {"factor": factor})

    def move(self, *, window_id: int | None = None, index: int = -1) -> dict:
        """Move the tab to index (-1: last) of its window, or of window_id."""
        body = {"index": index}
        if window_id is not None:
            body["windowId"] = window_id
        return self._request("/api/move-tab", body)

    def detach(self) -> dict:
        """Move the tab into a new window of its own; returns its windowId."""
        return self._request("/api/detach-tab")

    def resize_window(self, width: int, height: int) -> dict:
        """Resize the window this tab is in."""
        return self._request("/api/resize-window", {"width": width, "height": height})

    def tag(self, name: str) -> dict:
        """Name this tab, so browser.tags.tab(name) finds it."""
        return self._request("/api/tag-tab", {"name": name})

    # -- Actions ---------------------------------------------------

    def click(self, target: str, *, button: str | None = None, modifiers: list[str] | None = None,
              wait: bool = False, timeout: int = 10000, expect: dict | None = None, first: bool = False,
              confirm: str | None = None, hold_ms: int = 0) -> dict:
        """Click an element with synthetic events; tabs the click opens come back in opened_tabs,
        and a menu, listbox or dialog it opens in opened_popup. hold_ms (0-10000) keeps the button
        pressed that long, for long-press controls."""
        body = _action_target(target)
        if button is not None:
            body["button"] = button
        if modifiers:
            body["modifiers"] = modifiers
        if hold_ms:
            body["holdMs"] = hold_ms
        return self._action("/api/click", body, wait=wait, timeout=timeout, expect=expect, first=first,
                            confirm=confirm, pointer_ms=hold_ms)

    def double_click(self, target: str, *, wait: bool = False, timeout: int = 10000, expect: dict | None = None,
                     first: bool = False, confirm: str | None = None) -> dict:
        return self._action("/api/double-click", _action_target(target), wait=wait, timeout=timeout,
                            expect=expect, first=first, confirm=confirm)

    def hover(self, target: str, *, wait: bool = False, timeout: int = 10000, expect: dict | None = None,
              first: bool = False, duration_ms: int = 0, steps: int | None = None,
              confirm: str | None = None) -> dict:
        """Move the pointer onto target; with duration_ms (0-10000) or steps (1-50, default 5 with
        duration_ms) it keeps moving across target, for hover-intent controls."""
        body = _action_target(target)
        if duration_ms:
            body["durationMs"] = duration_ms
        if steps is not None:
            body["steps"] = steps
        return self._action("/api/hover", body, wait=wait, timeout=timeout, expect=expect, first=first,
                            pointer_ms=duration_ms, confirm=confirm)

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
               expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        """Scroll target, else the window: up, down, left or right by amount pixels, or to top or bottom.
        scrolled_by is how far it moved."""
        return self._action("/api/scroll", {"direction": direction, "amount": amount, **_scope(target)},
                            expect=expect, first=first, confirm=confirm)

    def submit_form(self, target: str | None = None, *, expect: dict | None = None, first: bool = False,
                    confirm: str | None = None) -> dict:
        """Submit target's form, else that of the field last filled or focused, else the page's only form."""
        return self._action("/api/submit-form", _scope(target), expect=expect, first=first, confirm=confirm)

    def handle_dialogs(self, accept: bool | None = True, prompt_text: str | None = None, *,
                       confirm: str | None = None) -> dict:
        """Answer this tab's future alert/confirm/prompt dialogs: accept or dismiss them, or None to
        leave them to the user again. Returns the policy and recent dialogs."""
        return self._request("/api/handle-dialog", _with_confirm({"accept": accept, "prompt_text": prompt_text}, confirm))

    def focus(self, target: str, *, first: bool = False, confirm: str | None = None) -> dict:
        return self._action("/api/focus", _action_target(target), first=first, confirm=confirm)

    def blur(self, target: str | None = None, *, first: bool = False, confirm: str | None = None) -> dict:
        """Remove focus from target, else from the focused element."""
        return self._action("/api/blur", _scope(target), first=first, confirm=confirm)

    def drag(self, source: str, destination: str, *, wait: bool = False, timeout: int = 10000,
             expect: dict | None = None, first: bool = False, confirm: str | None = None) -> dict:
        """Drag source onto destination with HTML5 drag events, else pointer events (via says which)."""
        # The extension reads refs given as from/to, as the core browser_drag sends them.
        return self._action("/api/drag", {"from": source, "to": destination, "toRef": None}, wait=wait,
                            timeout=timeout, expect=expect, first=first, confirm=confirm)

    def insert(self, target: str, text: str, *, html: bool = False, clear: bool = False, wait: bool = False,
               timeout: int = 10000, expect: dict | None = None, first: bool = False,
               confirm: str | None = None) -> dict:
        """Insert text into an editor, input or textarea, appending unless clear; html=True inserts
        it as HTML into an editor (developer build)."""
        body = {**_action_target(target), "value": text, "format": "html" if html else "text", "clear": clear}
        return self._action("/api/set-editable-content", body, wait=wait, timeout=timeout, expect=expect,
                            first=first, confirm=confirm)

    def upload_files(self, paths: list[str], target: str | None = None, *, first: bool = False,
                     confirm: str | None = None) -> dict:
        """Put local files into target's file input, else the page's only file input."""
        body = {"paths": list(paths), "mode": "auto", **({"selector": target} if target else {})}
        if first:
            body["first"] = True
        return self._request("/api/upload-file", _with_confirm(body, confirm), timeout=320)

    def drop(self, target: str, paths: list[str] | None = None, *, data: dict[str, str] | None = None,
             confirm: str | None = None) -> dict:
        """Drop local files and/or data ({mime type: value}) onto target."""
        if not paths and not data:
            raise ValueError("Pass paths or data to drop")
        body = {"paths": list(paths or []), "mode": "drop", "selector": target, **({"data": data} if data else {})}
        return self._request("/api/upload-file", _with_confirm(body, confirm), timeout=320)

    def trusted_click(self, target: str | None = None, *, x: int = 0, y: int = 0,
                      coordinate_space: str = "viewport", focus: bool = False, first: bool = False,
                      restore_pointer: bool = True, button: str = "left", click_count: int = 1,
                      modifiers: list[str] | None = None, confirm: str | None = None, hold_ms: int = 0) -> dict:
        """Click with the real OS pointer, for pages that ignore synthetic clicks: target's center,
        else x, y in "viewport" or "screen" coordinates. It moves the user's cursor for the click,
        then back unless restore_pointer=False (for hover menus that close when the pointer leaves),
        and may show the tab for the click; focus=True brings the browser forward first. button is
        "left", "right" or "middle", click_count=2 double-clicks, modifiers ("Shift", "Control",
        "Alt", "Meta") are held during the click, and hold_ms (0-10000) keeps a single click's button
        pressed that long."""
        if target:
            body = {"selector": target, "focus": focus}
        else:
            body = {"coords": {"x": x, "y": y}, "coordinateSpace": coordinate_space, "focus": focus}
        if first:
            body["first"] = True
        if not restore_pointer:
            body["restorePointer"] = False
        if button != "left":
            body["button"] = button
        if click_count != 1:
            body["clickCount"] = click_count
        if modifiers:
            body["modifiers"] = list(modifiers)
        if hold_ms:
            body["holdMs"] = hold_ms
        return self._request("/api/trusted-click", _with_confirm(body, confirm), timeout=15 + hold_ms / 1000)

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
            body = _with_confirm({**body, "code": js, "pollInterval": 500}, confirm)
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


class Network:
    """One tab's requests, read from the page's resource timing: no headers or bodies."""

    def __init__(self, tab: Tab):
        self._tab = tab

    def _op(self, op: str, since: int = 0) -> dict:
        return self._tab._request("/api/capture-network", {"op": op, "since": since})

    def start(self) -> dict:
        """Start capturing the page's requests for requests()."""
        return self._op("start")

    def stop(self) -> dict:
        return self._op("stop")

    def clear(self) -> dict:
        return self._op("clear")

    def requests(self, *, since: int = 0) -> list[dict]:
        """The requests captured since start(), the latest 500; since is epoch ms."""
        return self._op("read", since)["entries"]

    def resources(self) -> list[dict]:
        """Every resource the document loaded, without start(), the latest 1000."""
        return self._tab._request("/api/capture-network", {"op": "all"})["entries"]


class PageStorage:
    """One tab's localStorage or sessionStorage; ask-before-act holds every operation."""

    def __init__(self, tab: Tab, kind: str):
        self._tab = tab
        self._kind = kind

    def _op(self, op: str, confirm: str | None, **fields) -> dict:
        body = {"kind": self._kind, "op": op, **{key: value for key, value in fields.items() if value is not None}}
        return self._tab._request("/api/storage", _with_confirm(body, confirm))

    def get(self, key: str, *, confirm: str | None = None) -> str | None:
        return self._op("get", confirm, key=key)["value"]

    def set(self, key: str, value: str, *, confirm: str | None = None) -> dict:
        return self._op("set", confirm, key=key, value=value)

    def remove(self, key: str, *, confirm: str | None = None) -> dict:
        return self._op("remove", confirm, key=key)

    def clear(self, *, confirm: str | None = None) -> dict:
        return self._op("clear", confirm)

    def items(self, *, confirm: str | None = None) -> dict[str, str]:
        """Every key and value."""
        return self._op("list", confirm)["items"]
