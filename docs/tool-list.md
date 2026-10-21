# Tool list

Current tools registered in [server.py](../src/kitsune/server.py), grouped by
capability. The default `KITSUNE_CAPS=core` exposes 14 of the 30 `core` tools
(the [lean preset](guide.md#lean-tool-preset)); `KITSUNE_TOOL_PRESET=full` exposes
all 30. `KITSUNE_CAPS=all` exposes all 96. Set a comma-separated list to enable selected
groups, for example `KITSUNE_CAPS=core,tabs,network`. The `history` group is not
part of `all`: it reads personal data and must be named, as in `KITSUNE_CAPS=all,history`.

See the [API reference](api-reference.md) for parameters and behavior.

## core — 30 (14 in the default lean preset)

- `browser_back`
- `browser_check`
- `browser_click`
- `browser_console_logs`
- `browser_dom`
- `browser_find`
- `browser_form_fill`
- `browser_forms`
- `browser_forward`
- `browser_handle_dialog`
- `browser_hover`
- `browser_js`
- `browser_keypress`
- `browser_markdown`
- `browser_navigate`
- `browser_page_info`
- `browser_page_text`
- `browser_screenshot`
- `browser_scroll`
- `browser_select_option`
- `browser_snapshot`
- `browser_status`
- `browser_submit_form`
- `browser_tabs`
- `browser_type`
- `browser_upload_file`
- `browser_wait_for`
- `browser_watch_console`
- `browser_workflow`
- `browser_workflows`

## devtools — 26

- `browser_audit`
- `browser_blur`
- `browser_bounds`
- `browser_cache`
- `browser_click_and_wait_navigation`
- `browser_computed_style`
- `browser_double_click`
- `browser_drag`
- `browser_explain_selector`
- `browser_focus`
- `browser_full_page_metrics`
- `browser_get_policy`
- `browser_health`
- `browser_highlight`
- `browser_html`
- `browser_iframes`
- `browser_images`
- `browser_links`
- `browser_logs`
- `browser_meta`
- `browser_query`
- `browser_readability`
- `browser_reload_extension`
- `browser_set_policy`
- `browser_structured_data`
- `browser_trusted_click` (Windows and macOS; macOS needs pyobjc Quartz or `cliclick` plus Accessibility permission)

## history (named only) — 3

Also needs the user's grant on Kitsune's options page; see
[History and bookmarks](api-reference.md#history-and-bookmarks).

- `browser_bookmarks_search`
- `browser_history_search`
- `browser_history_visits`

## network — 3

- `browser_capture_network`
- `browser_downloads`
- `browser_intercept`

## parallel — 16

- `browser_batch`
- `browser_broadcast`
- `browser_keep_alive`
- `browser_keep_alive_stop`
- `browser_list_tags`
- `browser_parallel`
- `browser_pool_acquire`
- `browser_pool_release`
- `browser_resolve_tag`
- `browser_resume`
- `browser_retry`
- `browser_scheduler`
- `browser_sync_barrier`
- `browser_tab_pool`
- `browser_tag_tab`
- `browser_untag_tab`

## session — 4

- `browser_delete_session`
- `browser_list_sessions`
- `browser_load_session`
- `browser_save_session`

## storage — 4

- `browser_clear_browsing_data`
- `browser_clipboard`
- `browser_cookies`
- `browser_storage`

## tabs — 7

- `browser_duplicate_tab`
- `browser_get_zoom`
- `browser_mute_tab`
- `browser_pin_tab`
- `browser_reload_tab`
- `browser_set_zoom`
- `browser_wake_tab`

## windows — 6

- `browser_close_window`
- `browser_create_window`
- `browser_detach_tab`
- `browser_focus_window`
- `browser_move_tab`
- `browser_windows`
