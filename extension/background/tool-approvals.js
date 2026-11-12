// Every Kitsune tool by group, for the options page's per-tool approvals (#525), and the tools that
// need approval until the user changes it: those that read personal data. Kept equal to the MCP
// server's tools and kitsune.tool_approvals.DEFAULT_TOOLS by tests/test_tool_approvals.py.
const KITSUNE_TOOL_GROUPS = {
  core: ['browser_ask_user', 'browser_click', 'browser_close', 'browser_console_messages', 'browser_drag', 'browser_drop', 'browser_emulate_media', 'browser_evaluate', 'browser_file_upload', 'browser_fill_form', 'browser_find', 'browser_handle_dialog', 'browser_hover', 'browser_navigate', 'browser_navigate_back', 'browser_network_request', 'browser_network_requests', 'browser_press_key', 'browser_resize', 'browser_resume', 'browser_run_code_unsafe', 'browser_run_python', 'browser_select_option', 'browser_snapshot', 'browser_tabs', 'browser_take_screenshot', 'browser_type', 'browser_wait_for'],
  browse: ['browser_blur', 'browser_check', 'browser_click_and_wait_navigation', 'browser_focus', 'browser_forward', 'browser_scroll', 'browser_submit_form', 'browser_trusted_click', 'browser_trusted_drag', 'browser_trusted_press_key', 'browser_trusted_type'],
  read: ['browser_dom', 'browser_forms', 'browser_html', 'browser_iframes', 'browser_images', 'browser_links', 'browser_markdown', 'browser_meta', 'browser_page_info', 'browser_page_text', 'browser_query', 'browser_readability', 'browser_structured_data'],
  inspect: ['browser_bounds', 'browser_computed_style', 'browser_explain_selector', 'browser_full_page_metrics', 'browser_highlight'],
  tabs: ['browser_close_window', 'browser_create_window', 'browser_detach_tab', 'browser_duplicate_tab', 'browser_focus_window', 'browser_get_zoom', 'browser_keep_alive', 'browser_keep_alive_stop', 'browser_list_tags', 'browser_move_tab', 'browser_mute_tab', 'browser_pin_tab', 'browser_reload_tab', 'browser_resolve_tag', 'browser_set_zoom', 'browser_tag_tab', 'browser_untag_tab', 'browser_wake_tab', 'browser_windows'],
  automation: ['browser_batch', 'browser_broadcast', 'browser_parallel', 'browser_pool_acquire', 'browser_pool_release', 'browser_retry', 'browser_scheduler', 'browser_sync_barrier', 'browser_tab_pool', 'browser_workflow', 'browser_workflows'],
  network: ['browser_capture_network', 'browser_downloads', 'browser_intercept'],
  admin: ['browser_audit', 'browser_cache', 'browser_get_policy', 'browser_health', 'browser_logs', 'browser_reload_extension', 'browser_set_policy', 'browser_status'],
  storage: ['browser_clear_browsing_data', 'browser_clipboard', 'browser_cookies', 'browser_delete_session', 'browser_list_sessions', 'browser_load_session', 'browser_save_session', 'browser_storage'],
  history: ['browser_bookmarks_search', 'browser_history_search', 'browser_history_visits'],
};
const DEFAULT_APPROVAL_TOOLS = [...KITSUNE_TOOL_GROUPS.storage, ...KITSUNE_TOOL_GROUPS.history];
// storage.local key of the tools the user requires approval for.
const APPROVAL_TOOLS_KEY = 'approvalTools';

async function approvalTools() {
  const stored = await browser.storage.local.get(APPROVAL_TOOLS_KEY).catch(() => ({}));
  return Array.isArray(stored[APPROVAL_TOOLS_KEY]) ? stored[APPROVAL_TOOLS_KEY] : DEFAULT_APPROVAL_TOOLS;
}
