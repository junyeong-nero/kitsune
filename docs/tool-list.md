# Tool list

Current tools registered in [server.py](../src/kitsune/server.py) and
[playwright_tools.py](../src/kitsune/playwright_tools.py), grouped by capability. The
default `KITSUNE_CAPS=core` exposes the 25 `core` tools, which are
[Playwright MCP](https://github.com/microsoft/playwright-mcp)'s core tools with the same
definitions ([Core tools](guide.md#core-tools)). `KITSUNE_CAPS=all` exposes all 93. Set a
comma-separated list to enable selected groups, for example
`KITSUNE_CAPS=core,browse,read`. The `storage` and `history` groups are not part of `all`:
they read personal data and must be named, as in `KITSUNE_CAPS=all,storage,history`.
Calling a tool that is not published returns `tool_not_published` naming the setting
([API reference](api-reference.md#mcp-tool-surface)).

These counts describe the default `KITSUNE_MODE=act`. `KITSUNE_MODE=read|draft`
further intersects the capability surface and enforces the same limits on bridge calls;
see [Task modes](guide.md#task-modes). With `core`:

- `read` (8): `browser_tab_list`, `browser_navigate`, `browser_snapshot`, `browser_find`,
  `browser_wait_for`, `browser_console_messages`, `browser_network_requests`,
  `browser_network_request`.
- `draft` (11): read plus `browser_type`, `browser_select_option`, `browser_fill_form`.
- Other enabled groups contribute only read tools (`browse` adds `browser_scroll` and,
  for draft, `browser_check`). Restricted modes replace multi-action `browser_tabs` with
  read-only `browser_tab_list()` and omit clicks, submit, JavaScript, screenshots/file
  writes, policy changes and containers. Draft refuses sensitive/payment-like fills
  without issuing confirmation tokens.

See the [API reference](api-reference.md) for parameters and behavior.

## core — 25 (Playwright MCP)

- `browser_click`
- `browser_close`
- `browser_console_messages`
- `browser_drag`
- `browser_drop`
- `browser_emulate_media`
- `browser_evaluate`
- `browser_file_upload`
- `browser_fill_form`
- `browser_find`
- `browser_handle_dialog`
- `browser_hover`
- `browser_navigate`
- `browser_navigate_back`
- `browser_network_request`
- `browser_network_requests`
- `browser_press_key`
- `browser_resize`
- `browser_run_code_unsafe`
- `browser_select_option`
- `browser_snapshot`
- `browser_tabs`
- `browser_take_screenshot`
- `browser_type`
- `browser_wait_for`

`browser_emulate_media` and `browser_run_code_unsafe` are listed for compatibility and
answer `not_supported`.

Targeted writes use [strict selectors](api-reference.md#strict-selectors): a core tool's
selector `target` must match one element. The Kitsune-owned tools apply the same rule to
focus/blur, scroll, trusted clicks, fuzzy form-fill keys and implicit upload inputs, and
take `first=True` to explicitly opt into the first match.

## admin — 8

Bridge diagnostics, logs, audit, cache and extension reload, and the bridge policy.

- `browser_audit`
- `browser_cache`
- `browser_get_policy`
- `browser_health`
- `browser_logs`
- `browser_reload_extension`
- `browser_set_policy`
- `browser_status`

## automation — 12

Command batches, retries, workflows and parallel tab work; `browser_resume` continues an interrupted
container call ([Checkpoint and resume](checkpoint-resume.md)).

- `browser_batch`
- `browser_broadcast`
- `browser_parallel`
- `browser_pool_acquire`
- `browser_pool_release`
- `browser_resume`
- `browser_retry`
- `browser_scheduler`
- `browser_sync_barrier`
- `browser_tab_pool`
- `browser_workflow`
- `browser_workflows`

## browse — 8

Everyday page actions beyond the core tools: scrolling, forward navigation, checkboxes, focus,
form submits and trusted OS clicks.

- `browser_blur`
- `browser_check`
- `browser_click_and_wait_navigation`
- `browser_focus`
- `browser_forward`
- `browser_scroll`
- `browser_submit_form`
- `browser_trusted_click`

## history (named only) — 3

Also needs the user's grant on Kitsune's options page; see
[History and bookmarks](api-reference.md#history-and-bookmarks).

- `browser_bookmarks_search`
- `browser_history_search`
- `browser_history_visits`

## inspect — 5

Layout and selector debugging.

- `browser_bounds`
- `browser_computed_style`
- `browser_explain_selector`
- `browser_full_page_metrics`
- `browser_highlight`

## network — 3

- `browser_capture_network`
- `browser_downloads`
- `browser_intercept`

## read — 13

Read-only page content and structure.

- `browser_dom`
- `browser_forms`
- `browser_html`
- `browser_iframes`
- `browser_images`
- `browser_links`
- `browser_markdown`
- `browser_meta`
- `browser_page_info`
- `browser_page_text`
- `browser_query`
- `browser_readability`
- `browser_structured_data`

## storage (named only) — 8

Cookies, page storage, saved sessions (cookie and page-storage snapshots on disk) and the
system clipboard.

- `browser_clear_browsing_data`
- `browser_clipboard`
- `browser_cookies`
- `browser_delete_session`
- `browser_list_sessions`
- `browser_load_session`
- `browser_save_session`
- `browser_storage`

## tabs — 19

Tab state, windows, tab tags and keep-alive.

- `browser_close_window`
- `browser_create_window`
- `browser_detach_tab`
- `browser_duplicate_tab`
- `browser_focus_window`
- `browser_get_zoom`
- `browser_keep_alive`
- `browser_keep_alive_stop`
- `browser_list_tags`
- `browser_move_tab`
- `browser_mute_tab`
- `browser_pin_tab`
- `browser_reload_tab`
- `browser_resolve_tag`
- `browser_set_zoom`
- `browser_tag_tab`
- `browser_untag_tab`
- `browser_wake_tab`
- `browser_windows`
