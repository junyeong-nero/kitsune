"""Which tool a bridge request or container step runs, for the user's per-tool approvals (#525).

Every tool is available to every client; the user picks in the extension's options which ones wait
for their approval (the extension sends the list to the bridge). A direct or gateway MCP call names
its tool in TOOL_HEADER. A browser_run_python script's requests are the tool of their route
(ROUTE_TOOLS); a container step is the tool of its action (ACTION_TOOLS). Requests outside a tool
call (the server's own status checks) are not checked.
Only MCP servers' requests (they send an approver secret) are checked: the Python API and the
kitsune-bridge command are the user's own programs.
"""

# The extension's default before the user changes it: the tools that read personal data.
DEFAULT_TOOLS = frozenset({
    "browser_cookies", "browser_storage", "browser_clipboard", "browser_clear_browsing_data",
    "browser_save_session", "browser_load_session", "browser_list_sessions", "browser_delete_session",
    "browser_history_search", "browser_history_visits", "browser_bookmarks_search",
})

# The tool that owns each route. Routes several tools use belong to the one named like the request.
ROUTE_TOOLS = {
    "/api/ask-user": "browser_ask_user", "/api/audit": "browser_audit", "/api/back": "browser_navigate_back",
    "/api/batch": "browser_batch", "/api/blur": "browser_blur", "/api/bookmarks-search": "browser_bookmarks_search",
    "/api/bounds": "browser_bounds", "/api/broadcast": "browser_broadcast", "/api/cache": "browser_cache",
    "/api/capture-network": "browser_capture_network", "/api/check": "browser_check",
    "/api/clear-browsing-data": "browser_clear_browsing_data", "/api/clear-highlight": "browser_highlight",
    "/api/click": "browser_click", "/api/click-and-wait-navigation": "browser_click_and_wait_navigation",
    "/api/clipboard": "browser_clipboard", "/api/close-tab": "browser_close",
    "/api/close-window": "browser_close_window", "/api/computed-style": "browser_computed_style",
    "/api/console-logs": "browser_console_messages", "/api/cookies": "browser_cookies",
    "/api/create-window": "browser_create_window", "/api/delete-session": "browser_delete_session",
    "/api/detach-tab": "browser_detach_tab", "/api/dom": "browser_dom", "/api/double-click": "browser_click",
    "/api/downloads": "browser_downloads", "/api/drag": "browser_drag", "/api/duplicate-tab": "browser_duplicate_tab",
    "/api/element-screenshot": "browser_take_screenshot", "/api/explain-selector": "browser_explain_selector",
    "/api/fill": "browser_type", "/api/find": "browser_find", "/api/focus": "browser_focus",
    "/api/focus-window": "browser_focus_window", "/api/form-fill": "browser_fill_form", "/api/forms": "browser_forms",
    "/api/forward": "browser_forward", "/api/full-page-metrics": "browser_full_page_metrics",
    "/api/full-page-screenshot": "browser_take_screenshot", "/api/get-policy": "browser_get_policy",
    "/api/get-zoom": "browser_get_zoom", "/api/handle-dialog": "browser_handle_dialog", "/api/health": "browser_health",
    "/api/highlight": "browser_highlight", "/api/history-search": "browser_history_search",
    "/api/history-visits": "browser_history_visits", "/api/hover": "browser_hover", "/api/html": "browser_html",
    "/api/iframes": "browser_iframes", "/api/images": "browser_images", "/api/intercept": "browser_intercept",
    "/api/js": "browser_evaluate", "/api/keep-alive": "browser_keep_alive", "/api/keep-alive-stop": "browser_keep_alive_stop",
    "/api/keypress": "browser_press_key", "/api/links": "browser_links", "/api/list-sessions": "browser_list_sessions",
    "/api/list-tags": "browser_list_tags", "/api/load-session": "browser_load_session", "/api/logs": "browser_logs",
    "/api/markdown": "browser_markdown", "/api/meta": "browser_meta", "/api/move-tab": "browser_move_tab",
    "/api/mute-tab": "browser_mute_tab", "/api/navigate": "browser_navigate", "/api/new-tab": "browser_tabs",
    "/api/page-info": "browser_page_info", "/api/page-text": "browser_page_text",
    "/api/page-text-by-tab-id": "browser_page_text", "/api/pin-tab": "browser_pin_tab",
    "/api/pool-acquire": "browser_pool_acquire", "/api/pool-release": "browser_pool_release",
    "/api/query": "browser_query", "/api/readability": "browser_readability",
    "/api/reload-extension": "browser_reload_extension", "/api/reload-tab": "browser_reload_tab",
    "/api/resize-window": "browser_resize", "/api/resolve-tag": "browser_resolve_tag", "/api/resume": "browser_resume",
    "/api/retry": "browser_retry", "/api/save-session": "browser_save_session", "/api/scheduler": "browser_scheduler",
    "/api/screenshot": "browser_take_screenshot", "/api/scroll": "browser_scroll",
    "/api/select-option": "browser_select_option", "/api/set-editable-content": "browser_type",
    "/api/set-policy": "browser_set_policy", "/api/set-zoom": "browser_set_zoom", "/api/snapshot": "browser_snapshot",
    "/api/status": "browser_status", "/api/storage": "browser_storage", "/api/structured-data": "browser_structured_data",
    "/api/submit-form": "browser_submit_form", "/api/switch-tab": "browser_tabs", "/api/sync-barrier": "browser_sync_barrier",
    "/api/tab-pool": "browser_tab_pool", "/api/tabs": "browser_tabs", "/api/tag-tab": "browser_tag_tab",
    "/api/trusted-click": "browser_trusted_click", "/api/trusted-drag": "browser_trusted_drag",
    "/api/trusted-press-key": "browser_trusted_press_key", "/api/trusted-type": "browser_trusted_type",
    "/api/type": "browser_type", "/api/untag-tab": "browser_untag_tab", "/api/upload-file": "browser_file_upload",
    "/api/wait-for-element": "browser_wait_for", "/api/wait-for-network-idle": "browser_wait_for",
    "/api/wait-for-result": "browser_wait_for", "/api/wait-for-text": "browser_wait_for",
    "/api/wait-for-title": "browser_wait_for", "/api/wait-for-url": "browser_wait_for", "/api/wake-tab": "browser_wake_tab",
    "/api/watch-console": "browser_console_messages", "/api/windows": "browser_windows",
    "/api/workflow": "browser_workflow", "/api/workflows": "browser_workflows",
}
# Routes no approval applies to: status, the bridge's own lifecycle, approvals and handoffs themselves.
UNCHECKED_ROUTES = frozenset({"/", "/view", "/api/version", "/api/mcp-activity", "/api/shutdown",
                              "/api/handoff-session", "/api/request-tab", "/api/needs-user", "/api/tab-write-check",
                              "/api/tool-approval"})

# The tool of each container step's action (bridge.CONTAINER_ACTIONS); None for control flow, whose
# container tool was approved already, and for actions no tool runs alone.
ACTION_TOOLS = {
    "blurElement": "browser_blur", "broadcast": None, "captureNetwork": "browser_capture_network",
    "checkBox": "browser_check", "clearBrowsingData": "browser_clear_browsing_data", "click": "browser_click",
    "clickAndWaitNavigation": "browser_click_and_wait_navigation", "clipboard": "browser_clipboard",
    "closeTab": "browser_close", "closeWindow": "browser_close_window", "consoleLogs": "browser_console_messages",
    "cookies": "browser_cookies", "createWindow": "browser_create_window", "detachTab": "browser_detach_tab",
    "doubleClick": "browser_click", "downloads": "browser_downloads", "drag": "browser_drag",
    "duplicateTab": "browser_duplicate_tab", "elementScreenshot": "browser_take_screenshot",
    "executeJS": "browser_evaluate", "explainSelector": "browser_explain_selector", "fill": "browser_type",
    "find": "browser_find", "focusElement": "browser_focus", "focusWindow": "browser_focus_window",
    "formFill": "browser_fill_form", "fullPageMetrics": "browser_full_page_metrics",
    "fullPageScreenshot": "browser_take_screenshot", "getAccessibilityTree": "browser_dom",
    "getBounds": "browser_bounds", "getComputedStyle": "browser_computed_style", "getFormFields": "browser_forms",
    "getHTML": "browser_html", "getIframes": "browser_iframes", "getImages": "browser_images",
    "getLinks": "browser_links", "getMarkdown": "browser_markdown", "getMeta": "browser_meta",
    "getPageInfo": "browser_page_info", "getPageText": "browser_page_text", "getPageTextFromTab": "browser_page_text",
    "getReadability": "browser_readability", "getSnapshot": "browser_snapshot",
    "getStructuredData": "browser_structured_data", "getTabs": "browser_tabs", "getWindows": "browser_windows",
    "getZoom": "browser_get_zoom", "goBack": "browser_navigate_back", "goForward": "browser_forward",
    "highlight": "browser_highlight", "hover": "browser_hover", "if": None, "intercept": "browser_intercept",
    "keypress": "browser_press_key", "media": None, "moveTab": "browser_move_tab", "muteTab": "browser_mute_tab",
    "navigate": "browser_navigate", "newTab": "browser_tabs", "parallel": None, "pinTab": "browser_pin_tab",
    "query": "browser_query", "reloadExtension": "browser_reload_extension", "reloadTabBrowser": "browser_reload_tab",
    "resizeWindow": "browser_resize", "retry": None, "screenshot": "browser_take_screenshot",
    "scroll": "browser_scroll", "selectOption": "browser_select_option", "sequence": None,
    "setEditableContent": "browser_type", "setZoom": "browser_set_zoom", "sleep": None,
    "storageOp": "browser_storage", "submitForm": "browser_submit_form", "switchTab": "browser_tabs",
    "syncBarrier": "browser_sync_barrier", "trustedClick": "browser_trusted_click", "try": None,
    "type": "browser_type", "waitForElement": "browser_wait_for", "waitForNetworkIdle": "browser_wait_for",
    "waitForResult": "browser_wait_for", "waitForTitle": "browser_wait_for", "waitForUrl": "browser_wait_for",
    "wakeTab": "browser_wake_tab", "watchConsole": "browser_console_messages", "while": None,
}

# browser_run_python's own requests are the tool of their route, not the script tool itself.
SCRIPT_TOOL = "browser_run_python"


def request_tool(path: str, header: str | None) -> str | None:
    """The tool a request runs: the MCP call's own tool, a script's route's; None when unchecked."""
    if path in UNCHECKED_ROUTES or not header:
        return None
    if header != SCRIPT_TOOL:
        return header
    return ROUTE_TOOLS.get(path)
