"""Capability groups, and the bridge routes and container actions each one permits (#230).

KITSUNE_CAPS selects the tools an MCP server registers. The server also names its enabled
groups in CAPS_HEADER on every bridge request, and the bridge refuses the routes and the
container steps (batch, parallel, retry, broadcast, workflow, resume) of other groups, so a
container tool cannot run an action of a group the client did not enable. Like task modes this
is a per-client ceiling, not a boundary against local processes holding the bridge token:
requests without the header (the kitsune-bridge CLI, direct HTTP) are not restricted.
"""

CAPABILITY_GROUPS = frozenset({"core", "browse", "read", "inspect", "tabs", "automation", "network", "admin",
                               "storage", "history"})
# Groups that read personal data (cookies, page storage, saved sessions, the clipboard, history):
# KITSUNE_CAPS=all leaves them out, so they must be named (#140).
NAMED_ONLY_GROUPS = frozenset({"storage", "history"})

CAPS_HEADER = "X-Kitsune-Caps"

# Routes any client may call: status, version and the image page the browser itself loads.
OPEN_ROUTES = frozenset({"/", "/api/status", "/api/version", "/view"})


def _by_group(table: dict[str, tuple[str, ...]]) -> dict[str, frozenset[str]]:
    """name -> the groups listing it; a name several groups' tools use is in each of them."""
    groups: dict[str, set[str]] = {}
    for group, names in table.items():
        for name in names:
            groups.setdefault(name, set()).add(group)
    return {name: frozenset(listed) for name, listed in groups.items()}


# Explicitly mapped so a new route or action cannot silently widen a group: one that is not
# listed is refused to every client that sends CAPS_HEADER. Each group lists the routes its
# tools call; core also has the waits and page actions batch commands share with it.
ROUTE_GROUPS = _by_group({
    "core": (
        "/api/ask-user", "/api/back", "/api/capture-network", "/api/check", "/api/click", "/api/close-tab",
        "/api/console-logs", "/api/double-click", "/api/drag", "/api/element-screenshot", "/api/fill", "/api/find",
        "/api/form-fill", "/api/full-page-screenshot", "/api/handle-dialog", "/api/hover", "/api/js",
        "/api/keypress", "/api/navigate", "/api/needs-user", "/api/new-tab", "/api/resize-window", "/api/resume",
        "/api/screenshot",
        "/api/select-option", "/api/set-editable-content", "/api/snapshot", "/api/switch-tab", "/api/tabs",
        "/api/type", "/api/upload-file", "/api/wait-for-element", "/api/wait-for-network-idle", "/api/wait-for-result",
        "/api/wait-for-text", "/api/wait-for-title", "/api/wait-for-url", "/api/watch-console",
    ),
    "browse": (
        "/api/blur", "/api/check", "/api/click-and-wait-navigation", "/api/focus", "/api/forward", "/api/scroll",
        "/api/submit-form", "/api/trusted-click", "/api/trusted-drag", "/api/trusted-press-key", "/api/trusted-type",
    ),
    "read": (
        "/api/dom", "/api/forms", "/api/html", "/api/iframes", "/api/images", "/api/links", "/api/markdown",
        "/api/meta", "/api/page-info", "/api/page-text", "/api/page-text-by-tab-id", "/api/query",
        "/api/readability", "/api/structured-data",
    ),
    "inspect": (
        "/api/bounds", "/api/clear-highlight", "/api/computed-style", "/api/explain-selector",
        "/api/full-page-metrics", "/api/highlight",
    ),
    "tabs": (
        "/api/close-window", "/api/create-window", "/api/detach-tab", "/api/duplicate-tab", "/api/focus-window",
        "/api/get-zoom", "/api/keep-alive", "/api/keep-alive-stop", "/api/list-tags", "/api/move-tab",
        "/api/mute-tab", "/api/pin-tab", "/api/reload-tab", "/api/resolve-tag", "/api/set-zoom", "/api/tag-tab",
        "/api/untag-tab", "/api/wake-tab", "/api/windows",
    ),
    "automation": (
        "/api/batch", "/api/broadcast", "/api/pool-acquire", "/api/pool-release", "/api/resume", "/api/retry",
        "/api/scheduler", "/api/sync-barrier", "/api/tab-pool", "/api/workflow", "/api/workflows",
    ),
    "network": ("/api/capture-network", "/api/downloads", "/api/intercept"),
    "admin": (
        "/api/audit", "/api/cache", "/api/get-policy", "/api/health", "/api/logs", "/api/reload-extension",
        "/api/set-policy", "/api/shutdown",
    ),
    "storage": (
        "/api/clear-browsing-data", "/api/clipboard", "/api/cookies", "/api/delete-session", "/api/list-sessions",
        "/api/load-session", "/api/save-session", "/api/storage",
    ),
    "history": ("/api/bookmarks-search", "/api/history-search", "/api/history-visits"),
})

# Container steps by the extension action they run (bridge.BATCH_ACTION_MAP values, and the
# actions the bridge runs itself).
ACTION_GROUPS = _by_group({
    "core": (
        "captureNetwork", "checkBox", "click", "closeTab", "consoleLogs", "doubleClick", "drag", "elementScreenshot",
        "executeJS", "fill", "find", "formFill", "fullPageScreenshot", "getSnapshot", "getTabs", "goBack",
        "hover", "keypress", "navigate", "newTab", "resizeWindow", "screenshot", "selectOption",
        "setEditableContent", "sleep", "switchTab", "type", "waitForElement", "waitForNetworkIdle",
        "waitForResult", "waitForTitle", "waitForUrl", "watchConsole",
    ),
    "browse": (
        "blurElement", "checkBox", "clickAndWaitNavigation", "focusElement", "goForward", "media", "scroll",
        "submitForm", "trustedClick",
    ),
    "read": (
        "getAccessibilityTree", "getFormFields", "getHTML", "getIframes", "getImages", "getLinks", "getMarkdown",
        "getMeta", "getPageInfo", "getPageText", "getPageTextFromTab", "getReadability", "getStructuredData",
        "query",
    ),
    "inspect": ("explainSelector", "fullPageMetrics", "getBounds", "getComputedStyle", "highlight"),
    "tabs": (
        "closeWindow", "createWindow", "detachTab", "duplicateTab", "focusWindow", "getWindows", "getZoom",
        "moveTab", "muteTab", "pinTab", "reloadTabBrowser", "setZoom", "wakeTab",
    ),
    "automation": ("broadcast", "if", "parallel", "retry", "sequence", "syncBarrier", "try", "while"),
    "network": ("captureNetwork", "downloads", "intercept"),
    "admin": ("reloadExtension",),
    "storage": ("clearBrowsingData", "clipboard", "cookies", "storageOp"),
})


def enabled_groups(caps: frozenset) -> frozenset:
    """The groups a KITSUNE_CAPS value enables: `all` is every group except NAMED_ONLY_GROUPS."""
    return frozenset(group for group in CAPABILITY_GROUPS
                     if ("all" in caps and group not in NAMED_ONLY_GROUPS) or group in caps)


def header_caps(value: str | None) -> frozenset | None:
    """The groups a CAPS_HEADER value names, or None without the header (no restriction)."""
    if value is None:
        return None
    caps = frozenset(cap.strip().lower() for cap in value.split(",") if cap.strip())
    unknown = caps - CAPABILITY_GROUPS
    if unknown:
        raise ValueError(f"Unknown capability groups: {', '.join(sorted(unknown))}")
    return caps


def caps_refusal(caps: frozenset | None, what: str, groups: frozenset | None) -> dict | None:
    """The error for a route or action whose groups `caps` does not enable, else None."""
    if caps is None or (groups and groups & caps):
        return None
    needed = sorted(groups or ())
    return {"error": f"{what} needs the {' or '.join(needed)} capability" if needed
            else f"{what} belongs to no capability group",
            "code": "capability_restricted", "retryable": False,
            "hint": ("Enable that group in this MCP server's KITSUNE_CAPS, then restart the MCP client."
                     if needed else "Use a tool this MCP server publishes."),
            "state": {"groups": needed, "caps": sorted(caps)}}
