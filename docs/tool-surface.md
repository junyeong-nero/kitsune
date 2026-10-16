# Tool surface inventory

Before: 106 tools registered by default. After: 30 default tools, 95 with
`KITSUNE_CAPS=all`. The HTTP bridge and extension command surface is unchanged.

## Before (106)

- `browser_audit`
- `browser_back`
- `browser_batch`
- `browser_blur`
- `browser_bounds`
- `browser_broadcast`
- `browser_cache`
- `browser_capture_network`
- `browser_check`
- `browser_clear_browsing_data`
- `browser_click`
- `browser_click_and_wait_navigation`
- `browser_clipboard`
- `browser_close_tab`
- `browser_close_window`
- `browser_computed_style`
- `browser_console_logs`
- `browser_cookies`
- `browser_create_window`
- `browser_delete_session`
- `browser_detach_tab`
- `browser_dom`
- `browser_double_click`
- `browser_downloads`
- `browser_drag`
- `browser_duplicate_tab`
- `browser_element_screenshot`
- `browser_explain_selector`
- `browser_fill`
- `browser_find`
- `browser_focus`
- `browser_focus_window`
- `browser_form_fill`
- `browser_forms`
- `browser_forward`
- `browser_full_page_metrics`
- `browser_full_page_screenshot`
- `browser_get_policy`
- `browser_get_zoom`
- `browser_health`
- `browser_highlight`
- `browser_hover`
- `browser_html`
- `browser_iframes`
- `browser_images`
- `browser_intercept`
- `browser_js`
- `browser_keep_alive`
- `browser_keep_alive_stop`
- `browser_keypress`
- `browser_links`
- `browser_list_sessions`
- `browser_list_tags`
- `browser_load_session`
- `browser_logs`
- `browser_markdown`
- `browser_meta`
- `browser_move_tab`
- `browser_mute_tab`
- `browser_navigate`
- `browser_new_tab`
- `browser_page_info`
- `browser_page_text`
- `browser_page_text_by_tab_id`
- `browser_parallel`
- `browser_pin_tab`
- `browser_pool_acquire`
- `browser_pool_release`
- `browser_query`
- `browser_readability`
- `browser_reload_extension`
- `browser_reload_tab`
- `browser_resolve_tag`
- `browser_retry`
- `browser_save_session`
- `browser_scheduler`
- `browser_screenshot`
- `browser_scroll`
- `browser_select_option`
- `browser_set_editable_content`
- `browser_set_policy`
- `browser_set_zoom`
- `browser_status`
- `browser_storage`
- `browser_structured_data`
- `browser_submit_form`
- `browser_switch_tab`
- `browser_sync_barrier`
- `browser_tab_pool`
- `browser_tabs`
- `browser_tag_tab`
- `browser_trusted_click`
- `browser_type`
- `browser_untag_tab`
- `browser_upload_file`
- `browser_wait_for_element`
- `browser_wait_for_network_idle`
- `browser_wait_for_result`
- `browser_wait_for_title`
- `browser_wait_for_url`
- `browser_wake_tab`
- `browser_watch_console`
- `browser_windows`
- `browser_workflow`
- `browser_workflows`
- `browser_wp_html`

## After (95)

### core (default) — 30

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

### devtools — 26

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
- `browser_trusted_click`

### network — 3

- `browser_capture_network`
- `browser_downloads`
- `browser_intercept`

### parallel — 15

- `browser_batch`
- `browser_broadcast`
- `browser_keep_alive`
- `browser_keep_alive_stop`
- `browser_list_tags`
- `browser_parallel`
- `browser_pool_acquire`
- `browser_pool_release`
- `browser_resolve_tag`
- `browser_retry`
- `browser_scheduler`
- `browser_sync_barrier`
- `browser_tab_pool`
- `browser_tag_tab`
- `browser_untag_tab`

### session — 4

- `browser_delete_session`
- `browser_list_sessions`
- `browser_load_session`
- `browser_save_session`

### storage — 4

- `browser_clear_browsing_data`
- `browser_clipboard`
- `browser_cookies`
- `browser_storage`

### tabs — 7

- `browser_duplicate_tab`
- `browser_get_zoom`
- `browser_mute_tab`
- `browser_pin_tab`
- `browser_reload_tab`
- `browser_set_zoom`
- `browser_wake_tab`

### windows — 6

- `browser_close_window`
- `browser_create_window`
- `browser_detach_tab`
- `browser_focus_window`
- `browser_move_tab`
- `browser_windows`
