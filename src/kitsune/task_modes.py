"""Startup tool surfaces and per-request bridge limits (#141)."""

MODE_HEADER = "X-Kitsune-Mode"
# KITSUNE_PROTECT_USER_TABS=1: this client's navigations leave user tabs alone (#199).
PROTECT_USER_TABS_HEADER = "X-Kitsune-Protect-User-Tabs"


def task_mode(value: str | None) -> str:
    mode = (value or "act").strip().lower() or "act"
    if mode not in {"read", "draft", "act"}:
        raise ValueError(f"Unknown KITSUNE_MODE: {mode}")
    return mode


# Explicitly mapped so a newly added tool cannot silently widen a restricted mode.
READ_TOOLS = {
    "browser_status": None,
    "browser_tab_list": "getTabs",
    "browser_page_info": "getPageInfo",
    "browser_page_text": "getPageText",
    "browser_snapshot": "getSnapshot",
    "browser_forms": "getFormFields",
    "browser_dom": "getAccessibilityTree",
    "browser_find": "find",
    "browser_navigate": "navigate",
    "browser_scroll": "scroll",
    "browser_workflows": None,
    "browser_query": "query",
    "browser_html": "getHTML",
    "browser_links": "getLinks",
    "browser_images": "getImages",
    "browser_meta": "getMeta",
    "browser_structured_data": "getStructuredData",
    "browser_bounds": "getBounds",
    "browser_computed_style": "getComputedStyle",
    "browser_readability": "getReadability",
    "browser_markdown": "getMarkdown",
    "browser_iframes": "getIframes",
    "browser_explain_selector": "explainSelector",
    "browser_full_page_metrics": "fullPageMetrics",
    "browser_get_zoom": "getZoom",
    "browser_windows": "getWindows",
    "browser_console_messages": "consoleLogs",
    "browser_network_requests": "captureNetwork",
    "browser_network_request": "captureNetwork",
    "browser_wait_for": "waitForText",
    "browser_resolve_tag": None,
    "browser_list_tags": None,
    "browser_list_sessions": None,
    "browser_health": None,
    "browser_logs": None,
    "browser_audit": None,
    "browser_scheduler": None,
    "browser_get_policy": None,
    "browser_history_search": "historySearch",
    "browser_history_visits": "historyVisits",
    "browser_bookmarks_search": "bookmarksSearch",
}
DRAFT_TOOLS = {
    "browser_type": "type",
    "browser_select_option": "selectOption",
    "browser_check": "checkBox",
    "browser_fill_form": "formFill",
}
# browser_console_messages starts console capture before it reads it.
READ_ACTIONS = frozenset(action for action in READ_TOOLS.values() if action) | {"watchConsole"}
DRAFT_ACTIONS = READ_ACTIONS | frozenset(DRAFT_TOOLS.values()) | {"fill", "setEditableContent"}


def permits_tool(mode: str, name: str) -> bool:
    return mode == "act" or name in READ_TOOLS or (mode == "draft" and name in DRAFT_TOOLS)


def permits_action(mode: str, action: str) -> bool:
    return mode == "act" or action in (DRAFT_ACTIONS if mode == "draft" else READ_ACTIONS)


# Bridge-only routes are checked too: restricted clients cannot set policy, run containers,
# resume an act call, write files or stop the shared daemon by bypassing tools/list.
READ_ROUTES = frozenset({
    "/", "/api/status", "/api/version", "/api/tabs", "/api/page-info", "/api/page-text",
    "/api/page-text-by-tab-id", "/api/snapshot", "/api/forms", "/api/dom", "/api/find",
    "/api/navigate", "/api/scroll", "/api/workflows", "/api/query", "/api/html", "/api/links",
    "/api/images", "/api/meta", "/api/structured-data", "/api/bounds", "/api/computed-style",
    "/api/readability", "/api/markdown", "/api/iframes", "/api/explain-selector",
    "/api/full-page-metrics", "/api/get-zoom", "/api/windows", "/api/console-logs", "/api/watch-console",
    "/api/capture-network", "/api/wait-for-text",
    "/api/resolve-tag", "/api/list-tags", "/api/list-sessions", "/api/health", "/api/logs",
    "/api/audit", "/api/scheduler", "/api/get-policy", "/api/history-search",
    "/api/history-visits", "/api/bookmarks-search",
})
DRAFT_ROUTES = READ_ROUTES | {
    "/api/type", "/api/fill", "/api/set-editable-content", "/api/select-option", "/api/check", "/api/form-fill",
}


def permits_route(mode: str, path: str) -> bool:
    return mode == "act" or path in (DRAFT_ROUTES if mode == "draft" else READ_ROUTES)
