# Tools

Every client can call every tool. There are no capability groups to configure; which tools
need your approval is set in the extension's options ([Your tabs and approvals](your-tabs.md)).
`KITSUNE_SCRIPTING` only chooses what `tools/list` advertises:

| Setting | `tools/list` |
| --- | --- |
| Default (unset or `1`) | 6 [code-first tools](#default-tools) |
| `KITSUNE_SCRIPTING=0` | 27 [`core` tools](#core-tools) + 3 [gateway tools](#finding-tools-without-a-restart) |

Any other value fails at startup, as do the removed `KITSUNE_CAPS`, `KITSUNE_PUBLISH` and
`KITSUNE_MODE`. Unlisted tool names stay callable; `tools/list` is discovery, not an
authorization boundary. `browser_status` reports the active `surface` (`minimal` or `legacy`).

## Default tools

- `browser_run_python`: reads, inputs, waits, conditions and filtering
- `browser_tabs`: selection, handoff and tab lifecycle
- `browser_take_screenshot`: return images to the model
- `browser_ask_user`, `browser_resume`: ask the user for help; continue a stopped call
- `browser_tools_call`: inspect or run one direct tool, for approval or recovery

`browser_run_python` runs **trusted local Python, not a sandbox**: scripts have host file and
network access ([Python scripting](python-api.md#python-scripting-through-mcp)). It supplies
`browser` and the MCP-selected `tab`:

```python
tab.hover("#menu")
result = tab.snapshot(text=True)
```

Group only actions whose targets are already known, and return the observation the next
decision needs. `help(tab.hover)` prints one method's signature. `browser.tabs()` returns
dicts; wrap one with `browser.tab(t["id"])`.

Approvals and user-tab protection apply to script requests as to direct calls. A tool that
needs approval is asked about mid-script. Other approval stops end the script without
replaying completed work: the result's `failed_operation.recovery_tool` names a tool to call
through `browser_tools_call` (omit `arguments` to see its schema; pass them to run it). Scripts
cannot submit `confirm_token`s and are never checkpointed.

### Python equivalents

| Direct tool | Python |
| --- | --- |
| `browser_close` | `tab.close()` |
| `browser_hover` | `tab.hover(target)` |
| `browser_drag` | `tab.drag(source, destination)` |
| `browser_drop` | `tab.drop(target, paths=..., data=...)` |
| `browser_file_upload` | `tab.upload_files(paths, target)` |
| `browser_handle_dialog` | `tab.handle_dialogs(accept, prompt_text)` |
| `browser_navigate_back` | `tab.back()` |
| `browser_resize` | `tab.resize_window(width, height)` |
| `browser_console_messages` | `tab.watch_console()`, then `tab.console_logs()` |
| `browser_network_requests` | `tab.network.resources()` |
| `browser_evaluate` | `tab.evaluate(function, target)` (developer build only) |

Everything else is in the [Python API](python-api.md).

## Core tools

With `KITSUNE_SCRIPTING=0`, `tools/list` has Playwright MCP's 25 core tools, published verbatim
from `src/kitsune/playwright_tools.json`, plus Kitsune's `browser_resume` and `browser_ask_user`.
A client or prompt written for [Playwright MCP](https://github.com/microsoft/playwright-mcp)
works unchanged, but results are Kitsune's compact JSON. Differences:

- **Current tab.** Tools act on the tab `browser_tabs` `new` or `select` made current. `select`
  takes any tab without switching your view; on a tab the agent cannot write, results say
  `read_only: true`. Without a current tab, reads use the active tab and writes return
  `no_current_tab` ([No current tab](api-reference.md#no-current-tab)); after a server restart
  that error lists the tabs this client opened in `state.ownTabs`. A tab opened by an action
  (e.g. a `target=_blank` click) becomes current and is named in `current_tab`.
  `browser_close` closes the current tab, never the browser.
- **Targets.** `target` is a snapshot ref (`e12`), a CSS selector or a supported Playwright
  selector ([Target selectors](api-reference.md#target-selectors)). Writes need exactly one
  match, else `ambiguous_target`. Refs stay valid while their nodes stay attached.
- **Snapshots.** Inline snapshots are capped at 12,000 characters, cut between blocks, with
  `truncated: true`. Long runs of similar items collapse to `… N more similar items`; snapshot
  the container ref to list them. `browser_find` searches collapsed items too.
- **Sliders and keys.** `browser_fill_form` `type: "slider"` sets native and ARIA sliders.
  `browser_press_key` accepts an extra `repeat` (1–100).
- **Dialogs.** Without a policy, an agent's tab dismisses dialogs and a user's tab shows them;
  page actions report them in `dialogs`. `browser_handle_dialog` sets how the next dialogs are
  answered; one already open cannot be (`dialog_open`).
- **Uploads.** `browser_file_upload` fills the file input last clicked (or the page's only one).
- **JavaScript.** `browser_evaluate` needs the developer build.
- **Console and network.** Console capture starts on first use. Network requests come from
  resource timing; no headers or bodies.
- **Screenshots.** PNG or JPEG; `scale` is ignored.
- **Not supported.** `browser_emulate_media` and `browser_run_code_unsafe` return
  `not_supported`.
- **File names.** A relative `filename` resolves against `KITSUNE_OUTPUT_DIR`, else the client's
  first `file://` root, else the server's working directory.

Approval holds still apply. Core schemas have no `confirm_token`, but the tools accept it for
clients without elicitation ([Approving held actions](your-tabs.md#approving-held-actions)).

## Finding tools without a restart

On the legacy surface three gateway tools reach the unlisted tools:

- `browser_tools_search(query, limit=5)`: find tools with their schemas; an empty query lists all.
- `browser_tools_call(name, arguments)`: run one tool.
- `browser_tools_publish(names)`: add tools or groups to this session's `tools/list`.

A gateway call takes the direct call's path: the same validation, approvals, tab protection and
redaction, and its audit entry records `via`. Your client's permission prompt may show only
`browser_tools_call`, not the tool it runs.

Tool annotations (read-only, destructive, …) are hints for clients, not enforced policy. The
bridge enforces `browser_set_policy` (`readonly`, `allow`/`deny`,
[ask-before-act](api-reference.md#ask-before-act)).

## Tool list

Kitsune-owned tools take `tab_id` for background tabs and share one action contract (`ref`,
`wait`, `expect`, `confirm_token`, `first`) documented on `browser_check`. See the
[API reference](api-reference.md) for parameters.

| Group | Tools |
| --- | --- |
| `core` | `browser_ask_user`, `browser_click`, `browser_close`, `browser_console_messages`, `browser_drag`, `browser_drop`, `browser_emulate_media`, `browser_evaluate`, `browser_file_upload`, `browser_fill_form`, `browser_find`, `browser_handle_dialog`, `browser_hover`, `browser_navigate`, `browser_navigate_back`, `browser_network_request`, `browser_network_requests`, `browser_press_key`, `browser_resize`, `browser_resume`, `browser_run_code_unsafe`, `browser_select_option`, `browser_snapshot`, `browser_tabs`, `browser_take_screenshot`, `browser_type`, `browser_wait_for` |
| `browse` | `browser_blur`, `browser_check`, `browser_click_and_wait_navigation`, `browser_focus`, `browser_forward`, `browser_scroll`, `browser_submit_form`, `browser_trusted_click`, `browser_trusted_drag`, `browser_trusted_press_key`, `browser_trusted_type` |
| `read` | `browser_dom`, `browser_forms`, `browser_html`, `browser_iframes`, `browser_images`, `browser_links`, `browser_markdown`, `browser_meta`, `browser_page_info`, `browser_page_text`, `browser_query`, `browser_readability`, `browser_structured_data` |
| `inspect` | `browser_bounds`, `browser_computed_style`, `browser_explain_selector`, `browser_full_page_metrics`, `browser_highlight` |
| `tabs` | `browser_close_window`, `browser_create_window`, `browser_detach_tab`, `browser_duplicate_tab`, `browser_focus_window`, `browser_get_zoom`, `browser_keep_alive`, `browser_keep_alive_stop`, `browser_list_tags`, `browser_move_tab`, `browser_mute_tab`, `browser_pin_tab`, `browser_reload_tab`, `browser_resolve_tag`, `browser_set_zoom`, `browser_tag_tab`, `browser_untag_tab`, `browser_wake_tab`, `browser_windows` |
| `automation` | `browser_batch`, `browser_broadcast`, `browser_parallel`, `browser_pool_acquire`, `browser_pool_release`, `browser_retry`, `browser_scheduler`, `browser_sync_barrier`, `browser_tab_pool`, `browser_workflow`, `browser_workflows` |
| `network` | `browser_capture_network`, `browser_downloads`, `browser_intercept` |
| `storage` | `browser_clear_browsing_data`, `browser_clipboard`, `browser_cookies`, `browser_delete_session`, `browser_list_sessions`, `browser_load_session`, `browser_save_session`, `browser_storage` |
| `history` | `browser_bookmarks_search`, `browser_history_search`, `browser_history_visits` (also need the user's grant in the extension options) |
| `admin` | `browser_audit`, `browser_cache`, `browser_get_policy`, `browser_health`, `browser_logs`, `browser_reload_extension`, `browser_set_policy`, `browser_status` |
| gateway | `browser_tools_call`, `browser_tools_publish`, `browser_tools_search` (legacy surface) |
