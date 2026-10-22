# Kitsune guide

[← Project overview](../README.md)

Client configuration, tool usage, and operational details for Kitsune. If you
haven't installed Kitsune yet, start with [Setup](#setup); the full per-client
instructions, version pinning, and upgrades are in [Installation](installation.md).
Then read [How it works](#how-it-works).

- **Get started:** [Installation](installation.md) · [MCP clients](#mcp-clients) · [How it works](#how-it-works) · [Requirements](#requirements) · [Setup](#setup) · [Try it](#try-it)
- **Use the tools:** [Snapshots and refs](#snapshot-and-element-refs) · [Tools and capabilities](#tools) · [API reference](api-reference.md)
- **Operate safely:** [Updates](#updating-kitsune) · [Configuration](#configuration) · [Security](#security) · [Troubleshooting](#troubleshooting)
- **Contribute:** [Architecture](#how-it-works) · [Development](#development) · [Layout](#layout) · [Credits](#credits)

## MCP clients

Kitsune is a stdio MCP server. Every client runs the same command; uv downloads
and runs it, so no checkout is needed:

```text
uvx --from kitsune-browser-mcp kitsune
```

**Claude Code**

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

**Claude Desktop / Cursor / other `mcpServers` clients**
(`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "kitsune": {
      "command": "uvx",
      "args": ["--from", "kitsune-browser-mcp", "kitsune"]
    }
  }
}
```

Codex CLI, opencode, GUI apps that cannot find `uvx`, and pinning a version are
covered in [Installation](installation.md#1-register-the-mcp-server). To run
your own working tree instead, register `uv run --directory /path/to/kitsune
kitsune`; see [Development setup](installation.md#development-setup-from-a-checkout).

Several clients can be configured at once; they share one bridge daemon. Use the
same install source for all of them, or each one replaces the bridge when it
starts (see [compatibility](installation.md#extension-and-server-compatibility)).

## How it works

```
agent ──stdio──▶ kitsune (MCP server, one per agent)
                    │ HTTP 127.0.0.1:47651 + bearer token
                    ▼
                 bridge daemon (one per user, started on demand)
                    │ WebSocket 127.0.0.1:47652, moz-extension:// origin only
                    ▼
                 Kitsune extension ──▶ tabs (content scripts)
```

1. The agent launches `kitsune` over stdio.
2. If no bridge is answering, `kitsune` spawns `python -m kitsune.bridge` as a
   detached daemon. If its build differs, Kitsune safely replaces it first.
   A file lock serializes starts and replacements across agents.
3. The extension keeps reconnecting to the daemon, so it attaches by itself.
4. The daemon keeps running after the agent exits; the next agent reuses it.

## Snapshot and element refs

Start with `browser_snapshot(tab_id=...)` to get a compact accessibility-style
text tree of visible controls, headings, and landmarks:

```text
- main ""
  - heading "Welcome" [ref=e0] [level=1]
  - button "Log in" [ref=e1]
```

Prefer `browser_click(ref="e1", element="Log in", tab_id=...)` and
`browser_type(ref="...", text="...", mode="fill", tab_id=...)`. `element` is a description
for audit logs only; CSS `selector` remains supported. For drag, use `ref` for
the source and `to_ref` for the destination (or the existing selector arguments).
A write action's selector must match one element: several matches fail with
`ambiguous_target` and act on nothing, unless `first=True` (see
[Strict selectors](api-reference.md#strict-selectors)). This includes trusted clicks,
focus/blur, targeted scrolling, fuzzy form-fill keys, and uploads without a selector
on pages with several file inputs. Ambiguous clicks fail before ask-before-act
offers confirmation.

Short refs (`e12`) are scoped to the tab, document and snapshot generation, tracked
by the extension. Indexes are never reused across snapshots or tabs; after an
extension reload, take a new snapshot. A new snapshot,
`browser_dom`, or a full post-action snapshot below replaces earlier refs in that
tab, and navigation invalidates them; `browser_find`, `browser_forms`,
`browser_wait_for(selector=...)` and post-action deltas add refs without replacing any. Detached or
outdated refs return `stale ref, call browser_snapshot again` (`code: "stale_ref"`);
for a replaced ref, `state.latestObservation` names the call with the current refs
and, when the element is unambiguously the same, `state.currentRef` is its current
ref. Always use refs from the latest result.

Click, type, fill, select, check, keypress, submit, and navigate responses include
a `snapshot` capped at 4,000 characters (`snapshot_truncated` indicates more
content), and `target_ref` is the acted-on element's ref for a follow-up action.
For click, type, fill, select, check and keypress it is a delta (`snapshot_delta: true`):
only the target and what the action changed or revealed, such as an opened menu's
options, anchored there instead of at the page top; earlier refs stay valid. Submit,
navigate and actions that load another document return a full snapshot (see
[Post-action snapshots](api-reference.md#post-action-snapshots)).
If snapshot capture fails, `snapshot_error` preserves the action result; take a
new snapshot before using refs. To recover from a stale ref, take a new snapshot
and retry with the new ref; never repeat a write just to refresh refs. See
[Ref lifetime](api-reference.md#ref-lifetime) for the full contract and
[MCP error results](api-reference.md#mcp-error-results) for which results are
returned with `isError: true`.
For reading tasks, `browser_snapshot(text=True)` also returns visible text lines,
URL, title and tab ID in one bounded call (see
[`browser_snapshot`](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)).
To start a task, `browser_tabs(include_active=True)` returns the tab list with that read
of the active tab as `active_page`.
Set `KITSUNE_POST_ACTION_SNAPSHOT=0` in the **bridge daemon's environment** and
restart the daemon to disable these automatic snapshots, or `full` to always return
full ones. Explicit snapshots
remain available. This is a DOM-based accessibility approximation, not Firefox's
platform accessibility tree; open and closed shadow roots are traversed. Same-origin
iframes are read and acted on like the page; cross-origin iframes are listed but opaque
(see [Frames](api-reference.md#frames)).

## Requirements

- Firefox-family browser, Gecko 142+ (Firefox 142+, Zen 1.23+, LibreWolf)
- [uv](https://docs.astral.sh/uv/). uv downloads Python 3.10+ if needed.
- macOS or Linux (Windows untested since the fork)

## Setup

Setup has two separate parts: registering the server with your agent, and
installing the extension in your browser.

1. **Register the MCP server** in your agent. See [MCP clients](#mcp-clients).
2. **Install the extension** for the same version as the server, from
   [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases): the
   signed `kitsune-<version>.xpi` via `about:addons` → gear → **Install Add-on
   From File…** (persistent), or the developer build as a temporary add-on via
   `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…**. Only the
   developer build runs agent-supplied JavaScript (`browser_js`). See
   [Installation](installation.md#2-install-the-browser-extension).
3. **Check**: restart the agent and ask it to call `browser_status`. It should
   report `"extension_connected": true`.

## Try it

Once `browser_status` reports `"extension_connected": true`, ask your agent:

> Open a new tab, search for today's weather near Suseong Lake in Daegu,
> and summarize the forecast with a link to the source. Leave the tab open.

Or try:

- “Open Hacker News and summarize the top three stories with links.”
- “Inspect this page's form and fill a draft. Don't submit it yet.”
- “Capture a screenshot of this page and save it to a file.”

A typical tool sequence looks like this (Python-style pseudocode; these are MCP
calls made by your agent, not a standalone Python API):

```python
tab = browser_tabs(action="new", url="https://example.com")
browser_snapshot(tab_id=tab["tabId"])
# Inspect the returned snapshot, then use a ref from that result:
# browser_click(tab_id=tab["tabId"], ref="<latest ref>", element="More information")
browser_screenshot(tab_id=tab["tabId"], save_to="/tmp/kitsune-example.png")
```

Use a dedicated tab and pass its `tab_id` explicitly to avoid interacting with
an unrelated active tab. You and the agent share the browser: the agent's tabs open
in the background, it closes only tabs it opened unless you approve, and it backs off
from a tab you are using (see
[Shared browser](api-reference.md#shared-browser)). Ask the agent to confirm before submitting forms,
sending messages, purchasing, or deleting data. Page content is untrusted input,
not permission to take additional actions; see [Security](#security).

## Updating Kitsune

1. Update the server. uvx keeps using the cached release, so run
   `uvx --refresh --from kitsune-browser-mcp kitsune-bridge status` once to
   move to the newest one. For a pinned version, change the version (see
   [Upgrading](installation.md#upgrading)). From a checkout, run `git pull`,
   then `uv sync`.
2. Reconnect/restart the MCP server. It compares the running bridge's `build`
   (package version plus a source hash, including extension JS/JSON in a checkout)
   with its own and automatically restarts an outdated bridge. This briefly
   interrupts all clients sharing that bridge. Set `KITSUNE_NO_BRIDGE_RESTART=1`
   in the **MCP server's environment** to opt out of automatic replacement.
   You can also stop it explicitly; the next MCP connection starts a fresh daemon:
   ```bash
   uvx --from kitsune-browser-mcp kitsune-bridge status  # JSON; includes bridge_version and build
   uvx --from kitsune-browser-mcp kitsune-bridge stop
   ```
   From a checkout, use `uv run kitsune-bridge status` / `stop` instead.
   Plain `kitsune-bridge` (or `kitsune-bridge start`) still runs the daemon
   in the foreground. `status` and `stop` never start a daemon. All commands use
   the same `KITSUNE_HOME`, token, and port environment settings as the server.
3. Install the extension for the same version as the server. Signed builds
   released after 0.2.0 update themselves
   ([Automatic extension updates](installation.md#automatic-extension-updates));
   otherwise install the new signed XPI
   from `about:addons` → gear → **Install Add-on From File…**, or, for the
   developer build, reload it in `about:debugging#/runtime/this-firefox` (click
   **Reload** on Kitsune, or load the updated `manifest.json` again). A bridge
   restart reconnects the extension but does **not** reload its browser-side code;
   reload it explicitly after extension updates, and reload existing tabs if
   their content scripts are still outdated. `browser_status` reports a developer
   build whose files changed after it was loaded as `extension_version_mismatch`
   (`state.reason: "stale_developer_build"`).
4. Reconnect the MCP client again if needed (in Claude Code, use `/mcp` to
   reconnect Kitsune), then call `browser_status`. Check `extension_connected`,
   `bridge_version`, and `build`.

Shutdown uses a token-authenticated endpoint, with a verified, locked PID file
as the termination fallback; it never uses a broad `pkill`. Very old daemons
that have neither the shutdown endpoint nor a PID file require a **one-time
manual stop of the specific old bridge process**. Kitsune reports this instead
of guessing a PID or launching another daemon over it. After that, automatic
replacement and `kitsune-bridge stop` work normally.

### Idle shutdown (opt-in)

By default the shared bridge keeps running until it is stopped. To have it exit
when no client has used it for a while, set `KITSUNE_BRIDGE_IDLE_TIMEOUT` (in
seconds) in the **MCP server's environment**, or run the daemon yourself with
`kitsune-bridge start --idle-timeout SECONDS`. `0` (the default) disables it.

- Every HTTP call to the bridge counts as activity; MCP servers make one per tool
  call. The extension's WebSocket connection does not count.
- The timer only runs while nothing is in flight: no HTTP request, no command
  waiting on the extension, and no `browser_keep_alive` loop. Call
  `browser_keep_alive_stop` when you no longer need one, or the bridge never
  goes idle.
- The next tool call from any client starts a fresh bridge automatically.
  While an idle bridge is exiting, a client that needs one waits for it to finish
  instead of racing it.
- The bridge is shared, so the first MCP server that starts it decides the
  timeout; the setting in other clients takes effect the next time they start it.

## Tools

By default, Kitsune registers the 14 `browser_*` tools of the
[lean preset](#lean-tool-preset) from the 30-tool `core` capability group;
`KITSUNE_TOOL_PRESET=full` registers all 30. Tab-aware tools accept `tab_id` to
target background tabs.
Set `KITSUNE_CAPS` in the MCP server environment before starting it; restart the
MCP client after changing it. The comma-separated list selects exactly those
groups (include `core` to retain the default tools). `all` enables all 96 tools
in every group except `history`, which reads personal data and must be named
(`KITSUNE_CAPS=all,history`).
Whitespace and case are ignored; unknown group names fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "core,network,parallel"
```

| Group | Tools |
|---|---|
| `core` (30; 14 by default, see [Lean tool preset](#lean-tool-preset)) | `browser_status`, `browser_tabs`, `browser_page_info`, `browser_page_text`, `browser_forms`, `browser_dom`, `browser_screenshot`, `browser_navigate`, `browser_click`, `browser_type`, `browser_scroll`, `browser_hover`, `browser_find`, `browser_js`, `browser_wait_for`, `browser_workflows`, `browser_workflow`, `browser_markdown`, `browser_select_option`, `browser_check`, `browser_keypress`, `browser_submit_form`, `browser_form_fill`, `browser_upload_file`, `browser_back`, `browser_forward`, `browser_watch_console`, `browser_console_logs`, `browser_handle_dialog`, `browser_snapshot` |
| `devtools` (26) | `browser_trusted_click`, `browser_highlight`, `browser_cache`, `browser_reload_extension`, `browser_query`, `browser_html`, `browser_links`, `browser_images`, `browser_meta`, `browser_structured_data`, `browser_bounds`, `browser_computed_style`, `browser_readability`, `browser_iframes`, `browser_explain_selector`, `browser_focus`, `browser_blur`, `browser_double_click`, `browser_drag`, `browser_click_and_wait_navigation`, `browser_full_page_metrics`, `browser_health`, `browser_logs`, `browser_audit`, `browser_set_policy`, `browser_get_policy` |
| `network` (3) | `browser_downloads`, `browser_intercept`, `browser_capture_network` |
| `parallel` (16) | `browser_keep_alive`, `browser_keep_alive_stop`, `browser_parallel`, `browser_broadcast`, `browser_sync_barrier`, `browser_tag_tab`, `browser_resolve_tag`, `browser_list_tags`, `browser_untag_tab`, `browser_tab_pool`, `browser_pool_acquire`, `browser_pool_release`, `browser_scheduler`, `browser_retry`, `browser_batch`, `browser_resume` |
| `session` (4) | `browser_save_session`, `browser_load_session`, `browser_list_sessions`, `browser_delete_session` |
| `storage` (4) | `browser_cookies`, `browser_storage`, `browser_clipboard`, `browser_clear_browsing_data` |
| `tabs` (7) | `browser_wake_tab`, `browser_pin_tab`, `browser_mute_tab`, `browser_duplicate_tab`, `browser_reload_tab`, `browser_get_zoom`, `browser_set_zoom` |
| `windows` (6) | `browser_windows`, `browser_create_window`, `browser_close_window`, `browser_focus_window`, `browser_move_tab`, `browser_detach_tab` |
| `history` (3, not in `all`) | `browser_history_search`, `browser_history_visits`, `browser_bookmarks_search`. They also need your grant on Kitsune's options page; see [History and bookmarks](api-reference.md#history-and-bookmarks) |

`browser_double_click`, `browser_drag`, `browser_focus` and `browser_blur` are in
`devtools`, not `core`: `KITSUNE_TOOL_PRESET=full` alone does not publish them; add
`devtools` to `KITSUNE_CAPS` (#171). They stay there so the `core` definitions every
model request carries do not grow (#88).

A tool left out by `KITSUNE_CAPS` or the preset is absent from `tools/list`, and
clients usually say nothing about it. Calling it anyway returns an MCP error with
`code: "tool_not_published"` whose `hint` names the setting that publishes it, for
example `KITSUNE_CAPS=core,devtools` for `browser_drag`. `browser_status` reports
this server's `mcp_tools`: the active `preset`, the enabled `caps`, and how many
known tools are `unpublished`.

The consolidated tools use options for related operations:

- `browser_tabs(action="list"|"new"|"close"|"select", ...)`
- `browser_type(selector, text, mode="type"|"fill"|"editable", ...)`
- `browser_wait_for(selector=...|text=...|text_gone=...|js=...|url=...|title=...|network_idle=True|time=...)` (exactly one)
- `browser_screenshot(selector=None, full_page=False, ...)` returns an inline image by default; use `save_to="/path/capture.png"` for a saved PNG.

`browser_wait_for`, `browser_js`, `browser_workflow` and `browser_handle_dialog`
below are `core` tools outside the default lean preset; they need
`KITSUNE_TOOL_PRESET=full`.

`browser_js` runs JavaScript in the page. WordPress Elementor extraction is
available as `browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`.
See the [API reference](api-reference.md) for merged tool signatures and behavior,
and the [tool list](tool-list.md) for the current tools by capability group.

Every tool includes MCP annotations with a human-readable title and read-only,
destructive, idempotent, and open-world hints. Clients can use these hints to
inform permission prompts; enforcement depends on the client. Tools combining
read and write operations are annotated for their most dangerous operation,
including when called in a read mode. The hints are not enforced policy. The bridge
enforces task-mode limits and `browser_set_policy` (`readonly`, `allow`/`deny`, and
[ask-before-act](api-reference.md#ask-before-act)).

Screenshots return MCP image content by default. Large previews are JPEG-encoded
and downscaled to fit under roughly 1 MB; pass `save_to="/path/capture.png"` to
save the full PNG instead. Element and full-page captures retain `data_url=True`
for callers that need raw PNG data.

`browser_handle_dialog(accept=True, prompt_text=None, tab_id=None)` sets a tab's
policy for future alerts, confirms, and prompts and returns recent dialog history.
Capture starts at document start. By default, alerts/confirms/prompts pass through
to native browser UI and record the user's actual response; `beforeunload` also
keeps native behavior. Only an explicit policy auto-answers dialogs. Call
`browser_handle_dialog(accept=None, tab_id=...)` to clear a tab's policy and
restore native behavior. Dialogs answer synchronously, so recorded answers cannot
be changed afterward. `browser_page_info` also includes the last 100 dialogs for the
current document, with type, message, default value, timestamp, and response.
Policies survive navigation while the extension runs; native behavior applies briefly
until the tab policy is restored. `accept=True` disarms `beforeunload` like
`browser_close_tab(force=True)`; `accept=False` leaves native unload handling in
place. Native prompts already open, scripts that saved original dialog functions
before injection, and iframe dialogs (including cross-origin frames) cannot be
controlled. Existing tabs are patched when the extension connects; reload them
for early capture.

`browser_wait_for(text="Ready")` or `browser_wait_for(text_gone="Loading")` waits
until the condition holds in the top-level document's rendered text.
Matching is case-sensitive substring matching.

### Task modes

Set `KITSUNE_MODE=read|draft|act` in the MCP server environment and restart the client.
The default is `act`, preserving the existing tool surface and bridge policy. Mode names
ignore case and surrounding whitespace; unknown names fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_MODE = "read"
```

| Mode | Published tools and enforced limits |
|---|---|
| `read` | Read tools plus navigation to HTTP(S) pages (or `about:blank`) and scrolling. `browser_tab_list()` replaces the multi-action `browser_tabs`. No clicks, field writes, JavaScript, submits, confirmations or screenshots/file writes. |
| `draft` | Read surface plus `browser_type`, `browser_select_option`, `browser_form_fill` and, with the full preset, `browser_check`. Fills plain text only; no click/keypress/submit/dialog confirmation or JavaScript. Ask-before-act is forced: sensitive fields, payment-like pages and uninspectable targets are refused before filling, without a confirmation token. |
| `act` | Existing behavior, including the independently configured ask-before-act policy. |

Modes intersect with `KITSUNE_CAPS` and `KITSUNE_TOOL_PRESET`; they never enable a
disabled group or a tool the preset drops. With the default core/lean configuration,
`read` publishes nine tools: status, list-only tabs, navigate, page info/text, forms,
DOM, snapshot and find. `draft` adds type, select-option and form-fill. Scrolling
requires `KITSUNE_TOOL_PRESET=full`. Extra enabled groups contribute only their read
tools. The personal-data `history` group still requires explicit opt-in.

Restricted surfaces use first-paragraph descriptions and omit `confirm_token`.
Calling an unpublished tool by name returns `tool_not_published` with the mode setting
needed to publish it; `browser_status.mcp_tools.mode` identifies a restricted server.
The bridge enforces the mode on every request, including calls made by name or directly
over HTTP. A restricted request cannot change policy, run containers or resume an act
call. Draft form-fill inspects every target before writing any field.

Modes are per MCP client: a read client cannot loosen its mode by calling
`browser_set_policy`, and does not change another client's policy on the shared daemon.
The bridge's existing URL/readonly/locked-ask policy can tighten the mode further.
Use an `act` client or the browser itself for a sensitive action. Modes limit agent
commands, not page scripts: navigation can still trigger site behavior and field events
can autosave a draft. They are not an incognito session or an application-level sandbox.
Exact token counts and research/account-lookup task measurements belong to
kitsune-benchmark; character-size guards in this repo are not token measurements.

### Lean tool preset

**`lean` is the default `core` surface (#31).** The 16 `core` tools listed below
are not registered unless `KITSUNE_TOOL_PRESET=full` is set (or `KITSUNE_CAPS`
includes `all`). Set `full` if you relied on them:

```toml
[mcp_servers.kitsune.env]
KITSUNE_TOOL_PRESET = "full"
```

`lean` narrows `core` to the 14 tools kitsune-benchmark's read and draft tasks used,
and leaves every tool definition unchanged. In the #31 A/B (24 tasks, one model, one
attempt per arm) it showed no detected success or safety loss against the 30-tool
surface; that is workload-specific, and the dropped tools are needed for the tasks
in the last column of the table below. Groups other than `core` register in full before the task-mode filter. Case and surrounding whitespace are ignored; unknown preset names fail at startup.
A dropped tool is not in `tools/list`; calling it returns `tool_not_published` with
the hint `KITSUNE_TOOL_PRESET=full`, and `browser_status` shows `mcp_tools.preset`
(see [Tools](#tools)). An allowlist that names tools the server does not publish gets
no other warning.

| `KITSUNE_TOOL_PRESET` | `KITSUNE_CAPS` | `core` tools registered |
|---|---|---|
| Unset or empty | Unset, or a list without `all` | `lean` (14) |
| Unset or empty | Includes `all` | All 30 (96 tools in total) |
| `lean` | Any, including `all` | `lean` (14) |
| `full` | Any | All 30 |

It keeps `browser_status`, `browser_tabs`, `browser_navigate`, `browser_page_info`,
`browser_page_text`, `browser_snapshot`, `browser_forms`, `browser_dom`,
`browser_find`, `browser_screenshot`, `browser_click`, `browser_type`,
`browser_select_option`, and `browser_form_fill`: finding, opening, reading and
capturing pages, and filling forms with confirmation (ask-before-act) and `expect`
checks on every write. It drops these 16 `core` tools; set
`KITSUNE_TOOL_PRESET=full` for the tasks in the last column:

| Dropped | Closest lean substitute | Needs `full` |
|---|---|---|
| `browser_check` | `browser_form_fill` sets a checkbox/radio state; `browser_click` toggles it | Setting a checkbox by ref, or one form-fill cannot match by selector, name, label or placeholder |
| `browser_submit_form` | `browser_click` the submit button, with `expect` | Forms without a clickable submit control |
| `browser_back`, `browser_forward` | `browser_navigate` to the previous URL (a fresh load) | History navigation that must keep page state |
| `browser_markdown` | `browser_page_text`, `browser_snapshot(text=True)` | Reading pages as Markdown |
| `browser_wait_for` | `expect` and `wait` on actions, `browser_tabs(action="new", wait=True)` | Waiting without an action: selector, text, URL, title, JS or network idle |
| `browser_hover` | None | Hover menus and tooltips |
| `browser_keypress` | None (`browser_type` enters text only) | Enter/Escape/arrow keys, shortcuts, keyboard-only widgets |
| `browser_scroll` | None; reads cover the whole page and clicks scroll their target into view | Infinite scroll, lazy loading, scroll position |
| `browser_handle_dialog` | None; dialogs stay native and `browser_page_info` lists recent ones | Auto-answering alerts, confirms, prompts |
| `browser_upload_file` | None | File uploads |
| `browser_js` | None | Running page JavaScript, trusted HTML insertion |
| `browser_watch_console`, `browser_console_logs` | None | Console debugging |
| `browser_workflows`, `browser_workflow` | None | Named workflows |

Tool descriptions still mention tools the preset drops (for example the ref rules in
`browser_snapshot`). `tests/test_tool_preset.py` checks the dropped list and that the
kept tools retain the parameters read and draft flows use.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_CAPS` | `core` | Comma-separated capability groups above; `all` enables every group except `history`, which reads personal data and must be named, and enables all `core` tools unless `KITSUNE_TOOL_PRESET=lean` |
| `KITSUNE_MODE` | `act` | Task surface and per-request policy: `read`, `draft`, or `act`. See [Task modes](#task-modes) |
| `KITSUNE_TOOL_PRESET` | Unset (`lean`, or `full` with `KITSUNE_CAPS=all`) | `lean` registers 14 of the 30 `core` tools; `full` registers all 30. See [Lean tool preset](#lean-tool-preset) |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |
| `KITSUNE_EXTENSION_DIR` | `extension/` in a checkout | Unpacked extension the bridge compares a developer build with, to report one that was edited but not reloaded, and whose `WS_URL` port it expects before the extension connects ([Connection diagnosis](api-reference.md#connection-diagnosis)) |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | Set to `1` in the MCP server environment to skip automatic replacement of an outdated bridge |
| `KITSUNE_BRIDGE_IDLE_TIMEOUT` | `0` (never) | Seconds without client activity before the bridge exits; see [Idle shutdown](#idle-shutdown-opt-in) |
| `KITSUNE_PROTECT_USER_TABS` | Unset | Set to `1` in an MCP server environment to refuse that client's navigate, back and forward on tabs the agent did not open; see [Protecting your tabs](#protecting-your-tabs) |
| `KITSUNE_ASK_BEFORE_ACT` | Unset | Set to `1` in the MCP server environment to start the bridge with [ask-before-act](#ask-before-act) locked on; the lock then persists in `ask-before-act-locked` under `KITSUNE_HOME` |
| `KITSUNE_HISTORY_ALLOW_DOMAINS` | Unset (every domain) | Bridge: comma-separated domains (with subdomains) the `history` tools may return; see [History and bookmarks](api-reference.md#history-and-bookmarks) |
| `KITSUNE_HISTORY_DENY_DOMAINS` | Unset (none) | Bridge: comma-separated domains (with subdomains) the `history` tools never return; wins over the allow list |
| `KITSUNE_HISTORY_MAX_DAYS` | `30` | Bridge: how many days back the `history` tools may look. `browser_set_policy` can only tighten these three |
| `KITSUNE_POST_ACTION_SNAPSHOT` | Enabled (deltas) | Set to `0` in the bridge daemon environment and restart it to disable automatic post-action snapshots, `full` to return full snapshots instead of [deltas](api-reference.md#post-action-snapshots), or `text` to add readable text lines to them |

`~/.kitsune/` holds:

| File | Content |
|---|---|
| `token` | Bearer token for the HTTP API (0600, created on first start) |
| `bridge.log` | Daemon output |
| `sessions/` | Saved sessions, **including cookies** |
| `screenshots/` | Screenshots |
| `ask-before-act-locked` | Present while [ask-before-act](#ask-before-act) is locked on (0600); delete it to unlock |

## Security

What the bridge protects against:

- **Web pages calling the local API.** The HTTP API needs the bearer token and
  sends no CORS headers, so a page in the browser cannot use it.
- **DNS rebinding.** Requests with a `Host` other than `127.0.0.1` / `localhost`
  are rejected.
- **Web pages posing as the extension.** The WebSocket only accepts
  `moz-extension://` origins. Before this check, any page could connect and
  take over the extension's slot.

What it does not protect against:

- **Prompt injection.** A page the agent reads can contain instructions. The
  agent acts with every account you are logged into.
- **Local processes** running as your user can read the token.
- **Sensitive page data.** A connected agent can read page content and execute
  JavaScript in pages. Structural read tools (`browser_snapshot`, `browser_forms`,
  `browser_dom`, `browser_query`, `browser_html`, `browser_find`) redact
  password, one-time-code, card-number/CSC, secret-named, CSS-masked
  (including masked textareas and contenteditable regions) and autofilled
  fields, including web-component fields with a shadow root, and leave out
  CSS-masked static text. Selectors that probe a sensitive field's `value`
  attribute are refused; see
  [Sensitive-field redaction](api-reference.md#sensitive-field-redaction).
  This is not a sandbox: `browser_js` can read anything the page can, and the
  detection is heuristic. Do not assume a visually masked field is hidden from
  the agent or its logs.
- **Typed values outside the bridge.** The bridge audit log (`browser_audit`)
  keeps the action, target, tab, timing and outcome of each write action but
  replaces typed text (`browser_type`, `browser_form_fill` values,
  `browser_keypress` text, `browser_select_option` values and
  `browser_handle_dialog` prompt answers) and stored secrets (`browser_cookies`,
  `browser_storage` and `browser_clipboard` values) with `"[redacted]"`; see
  [Audit log](api-reference.md#audit-log). The page and the MCP client's own
  transcript still see whatever the agent sent.
- **History and bookmarks.** Off unless you enable the `history` capability group
  *and* allow it on Kitsune's options page (about:addons → Kitsune → Options);
  revoke it there or in the add-on's Permissions tab. Lookups return only URLs,
  titles, visit times and bookmark folders inside the bridge's history scope
  (by default the last 30 days), with token-like URL values replaced by
  `[redacted]`, and every lookup is audited without its result URLs; see
  [History and bookmarks](api-reference.md#history-and-bookmarks). Titles and
  URLs can still be personal: enable it only for agents you trust with them.
- **Authenticated actions.** Existing sessions let the agent act as you on
  accessible sites. MCP annotations are client hints, not an approval system.
  Ask-before-act (below) requires an explicit second step, but it is not human
  approval either.

Kitsune does not provide a password-vault listing tool. Native password-manager
autofill is browser-controlled; whether an autofilled value is accessible depends
on what the browser makes available to the page. Test it only with disposable
credentials, and never paste real secrets into bug reports.

Recommended: use a separate browser profile that only has the accounts the
agent needs, and keep banking, mail and work SSO out of it. Enable only the
capability groups you need, review the agent's actions, and treat saved sessions,
screenshots, and tool logs as potentially sensitive.

### Ask-before-act

Ask-before-act is an opt-in policy. In this mode the bridge does not run form
submits, submit-button clicks, clicks on buttons labeled like a submit or payment
("Place order", "Pay now", "결제하기"), Enter in form fields, `browser_js` and other
agent JavaScript, cookie, session, and page-storage access, clipboard reads and
writes, downloads, navigation to payment-like URLs, or page actions on such pages.
Each such call returns a `confirmation_required` error. The error describes the
pending action and carries a single-use token that expires in 120 seconds and is
bound to that exact action, its parameters, and its tab. After
asking you, the agent repeats the identical call with `confirm_token`. Batch, retry,
parallel, broadcast, and workflow commands never accept a token. Details are in the
[API reference](api-reference.md#ask-before-act).

There are two ways to turn it on:

- `browser_set_policy(ask=True)`: the agent can also turn this off again, so it
  only guards against accidents.
- Locked: set `KITSUNE_ASK_BEFORE_ACT=1` in the MCP server environment, or run
  `kitsune-bridge start --ask-before-act`. The policy can then only be tightened.
  A bridge that is already running keeps its policy, so run `kitsune-bridge stop`
  after changing the setting.

A locked bridge writes `~/.kitsune/ask-before-act-locked` (under `KITSUNE_HOME`, readable
only by you). While that file exists, every bridge starts locked, also after
`kitsune-bridge stop`, an idle shutdown, or an MCP server replacing an outdated bridge,
and also when the environment lacks `KITSUNE_ASK_BEFORE_ACT=1`. To unlock, remove the
variable from every MCP server configuration, delete the file, and run
`kitsune-bridge stop`.

The bridge checks the target before it acts, and the page can change in between (for
example, it navigates to a checkout URL). Such a change is not caught.

```toml
[mcp_servers.kitsune.env]
KITSUNE_ASK_BEFORE_ACT = "1"
```

The token goes back to the agent, so it proves an explicit second step, not that
you approved. For real approval, also have your MCP client prompt you before tool
calls, or at least before calls that carry `confirm_token`.

### Protecting your tabs

The agent closes only the tabs it opened unless you approve. Navigating one of your
tabs is a separate permission, and it is allowed by default, because sometimes you
want the agent to work in your tab. If your tabs must stay where they are, tell
the agent to open its material in new tabs (`browser_tabs` new) and pass their
`tab_id`. Without `tab_id`, a call acts on the tab you see.

To enforce this, set `KITSUNE_PROTECT_USER_TABS=1` in the MCP server environment
and restart the client. It applies to that client only. Then `browser_navigate`,
`browser_back` and `browser_forward` on a tab the agent did not open return a
`user_tab` error instead of navigating, also inside batches. That error tells the
agent to use its own tab, and on a direct call it carries a `confirm_token`. As with
ask-before-act, the token proves an explicit second step, not that you approved.

```toml
[mcp_servers.kitsune.env]
KITSUNE_PROTECT_USER_TABS = "1"
```

Limits: only those three calls are guarded. Clicked links, form submits, agent
JavaScript, redirects and navigations the page starts can still change your tab's
URL. Details are in the [API reference](api-reference.md#protected-user-tabs).

## Troubleshooting

| Symptom | Check |
|---|---|
| Any connection problem | Call `browser_status` and follow `diagnosis.hint`; each `diagnosis.code` is listed in [Connection diagnosis](api-reference.md#connection-diagnosis). It never shows the token |
| `extension_disconnected` (`extension_connected: false`) | Extension installed and enabled in the browser profile you opened? Developer build: browser restarted (temporary add-on gone)? It reconnects within 15 s of a bridge start |
| `extension_version_mismatch` | The extension and server versions differ; install the matching extension or update the server, then reload open tabs. `state.reason: "stale_developer_build"`: the developer build's files changed since the browser loaded them; reload it in about:debugging |
| `browser_window_unavailable` | The browser runs without a normal window Kitsune can use; open one, or allow Kitsune in private windows (about:addons → Kitsune → Run in Private Windows) |
| `host_permission_denied` | `not_granted`: allow site access in about:addons → Kitsune → Permissions. `restricted_domain`: Firefox keeps extensions off Mozilla sites such as addons.mozilla.org; use another tab |
| "needs the Kitsune developer extension build" | The signed build cannot run agent-supplied JavaScript; load the developer build |
| `bridge_unavailable` (`Kitsune bridge did not start`, `exited at startup`) | `state.logExcerpt` shows the bridge's last log line; see `~/.kitsune/bridge.log`, or run `kitsune-bridge start` in a terminal to see the error. `state.reason: "not_responding"`: the bridge hangs; run `kitsune-bridge stop` and retry |
| `port_conflict` | `state.port` is held by another program (`lsof -nP -iTCP:47651 -sTCP:LISTEN`), or `KITSUNE_WS_PORT` differs from the extension's `WS_URL` |
| `token_mismatch` (`401 Missing or invalid token`) | `KITSUNE_TOKEN` or `KITSUNE_HOME` set in one client but not in the daemon (or the other way round) |
| `Cannot access browser internal page` | `about:` pages are off-limits to extensions; switch to a normal tab |
| `stale ref, call browser_snapshot again` | Take a fresh snapshot of the same `tab_id` and use its new refs |
| Bridge updated but browser behavior is unchanged | Reload the extension, then reload affected pages so their content scripts update too |
| Restart the daemon | Run `kitsune-bridge stop` with the same source as your clients (`uvx --from git+…` or `uv run` in a checkout); the next tool call starts it again. This briefly affects all clients sharing it |
| `value-attribute selectors are disabled …` | A sensitive field on the page has a `value` attribute; target it by ref, label, or a selector that does not test `value` |
| Setup, uv, or upgrade problems | See [Installation troubleshooting](installation.md#troubleshooting) |

## Development

From the repository root:

```bash
uv sync
uv run python -m unittest discover -s tests -q
for test in tests/*.cjs; do node "$test"; done
```

The Python regression suite covers the MCP tool surface, snapshots, navigation
waits, and bridge lifecycle; the Node harnesses cover content-script behavior
such as refs, form filling, and password redaction. For an end-to-end check, load the extension in a
supported browser and follow [Try it](#try-it); unit tests alone do not verify
your live browser connection. File reproducible bugs in
[GitHub Issues](https://github.com/junyeong-nero/kitsune/issues), including the
browser version, bridge `build` from `browser_status`, tool calls, and expected
versus actual behavior. Use a minimal public or local page and redact secrets.

Tool definitions are resent with every model request, so their size is tracked:
`uv run python scripts/tool_schema_size.py` reports the compact JSON size of each
`core` tool in two views: the full definition, and the definition with its description
cut to the first paragraph, as some clients (Claude Code `--bare`) send it. A test fails
until `--update` records a change in `tests/tool_schema_sizes.json`. The same test fails
when the default (lean), full `core` or benchmark allowlist total grows past
`TOTAL_BUDGETS` or `FIRST_PARAGRAPH_BUDGETS`, or any single tool past `TOOL_BUDGET`;
raise a budget only deliberately. Published input
schemas omit the `title` keywords pydantic generates (`KitsuneMCP.list_tools`), and
`tests/test_tool_schema_titles.py` checks that they still accept and reject the same
arguments. They also publish an optional parameter whose default is null as its plain
type instead of `anyOf: [type, null]`, and omit `null` and `""` defaults. Calls are
still validated against the pydantic models, so an explicit `null` still works and
behaves as omitted; `tests/test_tool_schema_optionals.py` checks this for every tool.
Every client sends input schemas in full, so parameters are documented there
(`Annotated[..., Field(description=...)]`), not in `Args:` blocks. A tool's first
paragraph says what it does and what a caller must know. The action tools share one
contract (ref, element, wait, expect, confirm_token, first), documented once in
`browser_click`'s parameter descriptions; the other action tools say in their first
paragraph that `ok` means dispatched, name `expect` and point to `browser_click` rather
than repeating it. `tab_id` is described wherever it is accepted.
`tests/test_tool_first_paragraph.py` checks these rules for every tool.

The opt-in real-browser suites (`tests/test_real_browser*.py`) run against a real
Firefox, Zen or LibreWolf. They cover action races, tab unloads and reconnects
(`test_real_browser.py`), tabs opened by `target=_blank` clicks with popups allowed
and with the default popup blocker (`test_real_browser_opened_tabs.py`), every
read tool on empty 404 and 500 pages (`test_real_browser_error_pages.py`), and
which form or button an action picks on a page with repeated controls
(`test_real_browser_action_targets.py`). They are skipped unless `KITSUNE_E2E_BROWSER`
names the browser binary. A full run takes a little over a minute:

```bash
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v
```

Use `-p test_real_browser_error_pages.py` (or another file name) to run one suite.
Each test class starts a headless browser on a new temporary profile and HOME, plus
its own bridge and fixture site on spare ports, and kills them afterwards. Your
browser profiles and running bridge are not touched. The suites run a copy of the
extension whose bridge port is rewritten to the test bridge. The browser does not
start if any script in the copy still names port 47652, or if the test bridge would
use that port. The shared harness, `tests/real_browser_harness.py`, documents how to
add a suite, including how to choose the profile prefs (browser defaults or
popups allowed).

To publish a new version to PyPI, see [Releasing](releasing.md).

## Layout

```
extension/          WebExtension (manifest v2, Gecko)
src/kitsune/
  server.py         MCP server, starts the bridge on demand
  bridge.py         Bridge daemon (HTTP API + WebSocket)
  workflows/        Named workflows for browser_workflow
docs/               Guide, installation, API reference, scraping playbooks, TODO
```

## Credits

Kitsune started as a fork of [ZenLink](https://github.com/JayQuan-McCleary/ZenLink)
and [ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) by Jay-Quan
(MIT). Changes since the fork: token auth, Host and origin checks, no CORS,
on-demand shared daemon, one package, `browser_*` tool names, `~/.kitsune` data
directory, macOS support, `mcp<2` pin.

## License

MIT. See [LICENSE](../LICENSE).
