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
  - heading "Welcome" [ref=s3e0] [level=1]
  - button "Log in" [ref=s3e1]
```

Prefer `browser_click(ref="s3e1", element="Log in", tab_id=...)` and
`browser_type(ref="...", text="...", mode="fill", tab_id=...)`. `element` is a description
for audit logs only; CSS `selector` remains supported. For drag, use `ref` for
the source and `to_ref` for the destination (or the existing selector arguments).

Refs are scoped to the document and snapshot generation. A new snapshot,
`browser_dom`, or the post-action snapshot below replaces earlier refs in that
tab, and navigation invalidates them; `browser_find`, `browser_forms`, and
`browser_wait_for(selector=...)` add refs without replacing any. Detached or
outdated refs return `stale ref, call browser_snapshot again` (`code: "stale_ref"`);
for a replaced ref, `state.latestObservation` names the call with the current refs
and, when the element is unambiguously the same, `state.currentRef` is its current
ref. Always use refs from the latest result.

Click, type, fill, select, check, keypress, submit, and navigate responses include
a fresh `snapshot` capped at 4,000 characters (`snapshot_truncated` indicates more
content), and `target_ref` is the acted-on element's new ref for a follow-up action.
If snapshot capture fails, `snapshot_error` preserves the action result; take a
new snapshot before using refs. To recover from a stale ref, take a new snapshot
and retry with the new ref; never repeat a write just to refresh refs. See
[Ref lifetime](api-reference.md#ref-lifetime) for the full contract and
[MCP error results](api-reference.md#mcp-error-results) for which results are
returned with `isError: true`.
For reading tasks, `browser_snapshot(text=True)` also returns visible text lines,
URL, title and tab ID in one bounded call (see
[`browser_snapshot`](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)).
Set `KITSUNE_POST_ACTION_SNAPSHOT=0` in the **bridge daemon's environment** and
restart the daemon to disable these automatic snapshots. Explicit snapshots
remain available. This is a DOM-based accessibility approximation, not Firefox's
platform accessibility tree; closed shadow roots and iframe contents are not traversed.

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
an unrelated active tab. Ask the agent to confirm before submitting forms,
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

By default, Kitsune registers 30 `browser_*` tools in the `core` capability
group. Tab-aware tools accept `tab_id` to target background tabs.
Set `KITSUNE_CAPS` in the MCP server environment before starting it; restart the
MCP client after changing it. The comma-separated list selects exactly those
groups (include `core` to retain the default tools). `all` enables all 96 tools.
Whitespace and case are ignored; unknown group names fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "core,network,parallel"
```

| Group | Tools |
|---|---|
| `core` (30) | `browser_status`, `browser_tabs`, `browser_page_info`, `browser_page_text`, `browser_forms`, `browser_dom`, `browser_screenshot`, `browser_navigate`, `browser_click`, `browser_type`, `browser_scroll`, `browser_hover`, `browser_find`, `browser_js`, `browser_wait_for`, `browser_workflows`, `browser_workflow`, `browser_markdown`, `browser_select_option`, `browser_check`, `browser_keypress`, `browser_submit_form`, `browser_form_fill`, `browser_upload_file`, `browser_back`, `browser_forward`, `browser_watch_console`, `browser_console_logs`, `browser_handle_dialog`, `browser_snapshot` |
| `devtools` (26) | `browser_trusted_click`, `browser_highlight`, `browser_cache`, `browser_reload_extension`, `browser_query`, `browser_html`, `browser_links`, `browser_images`, `browser_meta`, `browser_structured_data`, `browser_bounds`, `browser_computed_style`, `browser_readability`, `browser_iframes`, `browser_explain_selector`, `browser_focus`, `browser_blur`, `browser_double_click`, `browser_drag`, `browser_click_and_wait_navigation`, `browser_full_page_metrics`, `browser_health`, `browser_logs`, `browser_audit`, `browser_set_policy`, `browser_get_policy` |
| `network` (3) | `browser_downloads`, `browser_intercept`, `browser_capture_network` |
| `parallel` (16) | `browser_keep_alive`, `browser_keep_alive_stop`, `browser_parallel`, `browser_broadcast`, `browser_sync_barrier`, `browser_tag_tab`, `browser_resolve_tag`, `browser_list_tags`, `browser_untag_tab`, `browser_tab_pool`, `browser_pool_acquire`, `browser_pool_release`, `browser_scheduler`, `browser_retry`, `browser_batch`, `browser_resume` |
| `session` (4) | `browser_save_session`, `browser_load_session`, `browser_list_sessions`, `browser_delete_session` |
| `storage` (4) | `browser_cookies`, `browser_storage`, `browser_clipboard`, `browser_clear_browsing_data` |
| `tabs` (7) | `browser_wake_tab`, `browser_pin_tab`, `browser_mute_tab`, `browser_duplicate_tab`, `browser_reload_tab`, `browser_get_zoom`, `browser_set_zoom` |
| `windows` (6) | `browser_windows`, `browser_create_window`, `browser_close_window`, `browser_focus_window`, `browser_move_tab`, `browser_detach_tab` |

The consolidated tools use options for related operations:

- `browser_tabs(action="list"|"new"|"close"|"select", ...)`
- `browser_type(selector, text, mode="type"|"fill"|"editable", ...)`
- `browser_wait_for(selector=...|text=...|text_gone=...|js=...|url=...|title=...|network_idle=True|time=...)` (exactly one)
- `browser_screenshot(selector=None, full_page=False, ...)` returns an inline image by default; use `save_to="/path/capture.png"` for a saved PNG.

`browser_js` runs JavaScript in the page. WordPress Elementor extraction is
available as `browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`.
See the [API reference](api-reference.md) for merged tool signatures and behavior,
and the [tool list](tool-list.md) for the current tools by capability group.

Every tool includes MCP annotations with a human-readable title and read-only,
destructive, idempotent, and open-world hints. Clients can use these hints to
inform permission prompts; enforcement depends on the client. Tools combining
read and write operations are annotated for their most dangerous operation,
including when called in a read mode. The hints are not enforced policy. The bridge
enforces only `browser_set_policy` (`readonly`, `allow`/`deny`, and
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

### Lean tool preset

`KITSUNE_TOOL_PRESET=lean` is an opt-in, experimental preset for comparing tool
surfaces (#31). It is not the default and is not shown to be better: it narrows
`core` to the 14 tools browser-mcp-bench's read and draft tasks used in 72 runs, and
leaves every tool definition unchanged. Whether it changes task success, safety,
calls or input per model turn is for the benchmark A/B to measure. Groups added with
`KITSUNE_CAPS` still register in full, and leaving the variable unset (or empty)
keeps the whole `core` group. Unknown preset names fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_TOOL_PRESET = "lean"
```

It keeps `browser_status`, `browser_tabs`, `browser_navigate`, `browser_page_info`,
`browser_page_text`, `browser_snapshot`, `browser_forms`, `browser_dom`,
`browser_find`, `browser_screenshot`, `browser_click`, `browser_type`,
`browser_select_option`, and `browser_form_fill`: finding, opening, reading and
capturing pages, and filling forms with confirmation (ask-before-act) and `expect`
checks on every write. It drops these 16 `core` tools; use the default surface for
the tasks in the last column:

| Dropped | Closest lean substitute | Needs the default surface |
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
| `KITSUNE_CAPS` | `core` | Comma-separated capability groups above; `all` enables everything |
| `KITSUNE_TOOL_PRESET` | Unset | `lean` narrows `core` to 14 tools for benchmark comparisons; see [Lean tool preset](#lean-tool-preset) |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |
| `KITSUNE_EXTENSION_DIR` | `extension/` in a checkout | Unpacked extension the bridge compares a developer build with, to report one that was edited but not reloaded, and whose `WS_URL` port it expects before the extension connects ([Connection diagnosis](api-reference.md#connection-diagnosis)) |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | Set to `1` in the MCP server environment to skip automatic replacement of an outdated bridge |
| `KITSUNE_BRIDGE_IDLE_TIMEOUT` | `0` (never) | Seconds without client activity before the bridge exits; see [Idle shutdown](#idle-shutdown-opt-in) |
| `KITSUNE_ASK_BEFORE_ACT` | Unset | Set to `1` in the MCP server environment to start the bridge with [ask-before-act](#ask-before-act) locked on; the lock then persists in `ask-before-act-locked` under `KITSUNE_HOME` |
| `KITSUNE_POST_ACTION_SNAPSHOT` | Enabled | Set to `0` in the bridge daemon environment and restart it to disable automatic post-action snapshots, or `text` to add readable text lines to them |

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
  fields, including web-component fields with a shadow root, and
  selectors that probe a sensitive field's `value` attribute are refused; see
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
default tool, and a test fails until `--update` records a change in
`tests/tool_schema_sizes.json`. The same test fails when the `core`, benchmark
allowlist or lean preset total, or any single tool, grows past `TOTAL_BUDGETS` /
`TOOL_BUDGET` in the script; raise a budget only deliberately. Published input
schemas omit the `title` keywords pydantic generates (`KitsuneMCP.list_tools`), and
`tests/test_tool_schema_titles.py` checks that they still accept and reject the same
arguments. Action tools share one contract (ref, wait,
expect, confirm_token) documented in `browser_click`; point to it rather than
repeating it.

The opt-in real-browser suites (`tests/test_real_browser*.py`) run against a real
Firefox, Zen or LibreWolf. They cover action races, tab unloads and reconnects
(`test_real_browser.py`), tabs opened by `target=_blank` clicks with popups allowed
and with the default popup blocker (`test_real_browser_opened_tabs.py`), and every
read tool on empty 404 and 500 pages (`test_real_browser_error_pages.py`). They are skipped unless `KITSUNE_E2E_BROWSER`
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
