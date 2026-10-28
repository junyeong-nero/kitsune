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
   After `kitsune` starts or replaces a daemon, its first tool call other than
   `browser_status` waits up to 16 s for the extension; it is sent once either way.
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

Short refs (`e12`) name attached nodes in one tab/document, tracked by the extension.
New snapshots, `browser_dom`, find and full or delta post-action observations reuse
those refs; an unchanged attached node keeps its ref even if its name or position changes.
Detachment/replacement, frame-document retirement, navigation or content reinjection
invalidates the old identity. Indexes are never reused across observations or tabs;
after an extension reload, take a new snapshot. A stale ref reports `element_removed`,
`not_in_snapshot` (never issued/unknown), or `other_document` in `state.reason`.
Take a new scoped observation of the intended tab; never repeat a write merely to
refresh refs. Earlier attached refs remain usable without another observation.

Click, type, fill, select, check, keypress, submit, and navigate responses include
a `snapshot` capped at 4,000 characters (`snapshot_truncated` indicates more
content; `snapshot_hint` gives scope guidance and explains if the target/context
could not fit). `target_ref`, when included, is the acted-on element's ref for a
follow-up action.
For click, type, fill, select, check and keypress it is a delta (`snapshot_delta: true`):
only the target and what the action changed or revealed, such as an opened menu's
options, anchored there instead of at the page top; earlier refs stay valid. A click or
hover that opens a menu, listbox or dialog also returns `opened_popup`: its ref, the ref of
the control that opened it (`opened_by`) and its items' refs, also when a background tab
did not paint it (`rendered: false`; see
[Opened popups](api-reference.md#opened-popups)). Submit,
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
   developer build runs agent-supplied JavaScript (`browser_evaluate`, `browser_js`). See
   [Installation](installation.md#2-install-the-browser-extension).
3. **Check**: restart the agent and ask it to list your tabs (`browser_tabs` with
   `action: "list"`). An error's `hint` says what to fix; with
   `KITSUNE_CAPS=core,admin`, `browser_status` reports `"extension_connected": true`
   and a full diagnosis.

## Try it

Once your tabs are listed, ask your agent:

> Open a new tab, search for today's weather near Suseong Lake in Daegu,
> and summarize the forecast with a link to the source. Leave the tab open.

Or try:

- “Open Hacker News and summarize the top three stories with links.”
- “Inspect this page's form and fill a draft. Don't submit it yet.”
- “Capture a screenshot of this page and save it to a file.”

A typical tool sequence looks like this (Python-style pseudocode; these are MCP
calls made by your agent; to drive the browser from your own code, see the
[Python API](python-api.md)):

```python
browser_tabs(action="new", url="https://example.com")  # the new tab becomes current
browser_snapshot()
# Inspect the returned snapshot, then use a ref from that result:
# browser_click(target="<latest ref>", element="More information")
browser_take_screenshot(filename="/tmp/kitsune-example.png")
```

Open a dedicated tab first (`browser_tabs` `new`) so the tools act on it. Until there is
one, reads act on the active tab, and writes and navigations that name no tab fail with
`no_current_tab` instead of acting on it. You and the agent share the browser: the agent's tabs open
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

By default Kitsune publishes the 25 tools of [Playwright MCP](https://github.com/microsoft/playwright-mcp)'s
default capabilities, with the same names, descriptions, input schemas and annotations, as
its `core` capability group (see [Core tools](#core-tools)). Kitsune's own tools are in
the other groups. Set `KITSUNE_CAPS` in the MCP server environment before starting it;
restart the MCP client after changing it. The comma-separated list selects exactly those
groups (include `core` to retain the default tools). `all` enables all 93 tools in every
group except `storage` and `history`, which read personal data and must be named
(`KITSUNE_CAPS=all,storage,history`). Whitespace and case are ignored; unknown group names
fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "core,browse,read"
```

| Group | Tools |
|---|---|
| `core` (25, Playwright MCP's) | `browser_close`, `browser_resize`, `browser_console_messages`, `browser_handle_dialog`, `browser_emulate_media`, `browser_evaluate`, `browser_file_upload`, `browser_drop`, `browser_find`, `browser_fill_form`, `browser_press_key`, `browser_type`, `browser_navigate`, `browser_navigate_back`, `browser_network_requests`, `browser_network_request`, `browser_run_code_unsafe`, `browser_take_screenshot`, `browser_snapshot`, `browser_click`, `browser_drag`, `browser_hover`, `browser_select_option`, `browser_tabs`, `browser_wait_for` |
| `admin` (8) | `browser_status`, `browser_cache`, `browser_reload_extension`, `browser_health`, `browser_logs`, `browser_audit`, `browser_set_policy`, `browser_get_policy` |
| `automation` (12) | `browser_workflows`, `browser_workflow`, `browser_parallel`, `browser_broadcast`, `browser_sync_barrier`, `browser_tab_pool`, `browser_pool_acquire`, `browser_pool_release`, `browser_scheduler`, `browser_retry`, `browser_batch`, `browser_resume` |
| `browse` (8) | `browser_trusted_click`, `browser_scroll`, `browser_check`, `browser_focus`, `browser_blur`, `browser_submit_form`, `browser_click_and_wait_navigation`, `browser_forward` |
| `inspect` (5) | `browser_highlight`, `browser_bounds`, `browser_computed_style`, `browser_explain_selector`, `browser_full_page_metrics` |
| `network` (3) | `browser_downloads`, `browser_intercept`, `browser_capture_network` |
| `read` (13) | `browser_page_info`, `browser_page_text`, `browser_forms`, `browser_dom`, `browser_query`, `browser_html`, `browser_links`, `browser_images`, `browser_meta`, `browser_structured_data`, `browser_readability`, `browser_markdown`, `browser_iframes` |
| `tabs` (19) | `browser_wake_tab`, `browser_keep_alive`, `browser_keep_alive_stop`, `browser_pin_tab`, `browser_mute_tab`, `browser_duplicate_tab`, `browser_reload_tab`, `browser_get_zoom`, `browser_set_zoom`, `browser_windows`, `browser_create_window`, `browser_close_window`, `browser_focus_window`, `browser_move_tab`, `browser_detach_tab`, `browser_tag_tab`, `browser_resolve_tag`, `browser_list_tags`, `browser_untag_tab` |
| `history` (3, not in `all`) | `browser_history_search`, `browser_history_visits`, `browser_bookmarks_search`. They also need your grant on Kitsune's options page; see [History and bookmarks](api-reference.md#history-and-bookmarks) |
| `storage` (8, not in `all`) | `browser_cookies`, `browser_storage`, `browser_clipboard`, `browser_clear_browsing_data`, `browser_save_session`, `browser_load_session`, `browser_list_sessions`, `browser_delete_session`. They read and write cookies, page storage and the system clipboard, and saved sessions hold cookies and page storage |

Earlier releases' `devtools`, `parallel`, `windows` and `session` groups are gone, and
naming them fails at startup. `devtools` became `browse`, `read`, `inspect`, `admin` and
(`browser_workflows`, `browser_workflow`) `automation`; `parallel` became `automation`
and, for tab tags and keep-alive, `tabs`; `windows` joined `tabs`; `session` joined
`storage`, which `all` no longer includes.

A tool left out by `KITSUNE_CAPS` is absent from `tools/list`, and clients usually say
nothing about it. Calling it anyway returns an MCP error with
`code: "tool_not_published"` whose `hint` names the setting that publishes it, for
example `KITSUNE_CAPS=browse,core` for `browser_scroll`. `browser_status` (`admin`)
reports this server's `mcp_tools`: the enabled `caps`, how many known tools are
`unpublished`, and its audit `client` and tab `owner` ids.

The bridge enforces the groups too. The MCP server sends its enabled groups with every
request, and the bridge refuses a route or a container step (of `browser_batch`,
`browser_parallel`, `browser_retry`, `browser_broadcast`, `browser_workflow` or
`browser_resume`) that belongs to another group with `code: "capability_restricted"`.
So `automation` runs only commands of the groups you enabled: with
`KITSUNE_CAPS=core,automation`, a batch `cookies` step is refused until you add
`storage`, and a workflow needs the groups of its steps. Like task modes, this limits
what an agent can do through this MCP server; it is not a boundary against other local
programs that hold the bridge token.

### Finding tools without a restart

`KITSUNE_CAPS` decides which tools this server runs. `KITSUNE_PUBLISH` decides which of
them `tools/list` shows when a session starts; unset, it is `KITSUNE_CAPS`, so nothing
changes. Set it to fewer groups to keep the list small and let the agent reach the rest
in the same connection:

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "all"
KITSUNE_PUBLISH = "core"
```

`tools/list` then has the 25 core tools and three gateway tools:

- `browser_tools_search(query, limit=5)` finds the tools this server runs but does not
  list, best match first, each with its full description, input schema and annotations.
  An empty query lists their names by group.
- `browser_tools_call(name, arguments)` runs one and returns its own result, images
  included, and its error status.
- `browser_tools_publish(names)` adds tools, or whole groups, to this session's
  `tools/list` and sends `notifications/tools/list_changed`; clients that refresh on it
  can then call them directly.

`KITSUNE_PUBLISH` takes the group names `KITSUNE_CAPS` does, and only groups
`KITSUNE_CAPS` enables: naming another one fails at startup. The gateway tools appear
only when some enabled tool is not published.

The gateway never runs more than a direct call could. It finds and runs only tools
`KITSUNE_CAPS` and `KITSUNE_MODE` permit: `all` still leaves out `storage` and
`history` unless named, and a name it does not permit gets the same `tool_not_published`
as a direct call. A call through `browser_tools_call` takes the direct call's path, so
argument validation, the task mode, confirmation tokens, protected user tabs, redaction
and file name roots apply the same way, and its audit entries record
`via: {"gateway": "browser_tools_call", "tool": …}`. Your client's permission prompt may
show only `browser_tools_call` and its conservative annotations (destructive,
open-world), not the tool it runs, so do not rely on per-tool client prompts for these
calls. `browser_tools_publish` changes only that session's list, never what the server
permits or another session's list. Clients differ in whether they act on
`notifications/tools/list_changed`; the notification is not proof that the model sees
the new tools, and `browser_tools_call` works either way.

This reaches tools that are already installed. Changing `KITSUNE_CAPS` or upgrading
Kitsune still needs a restart.

The Kitsune-owned tools take `tab_id` to target background tabs; without it they act on
the [current tab](#core-tools) like the core tools. They share an action
contract (`ref`, `wait`, `expect`, `confirm_token`, `first`) documented on
`browser_check`; see the [API reference](api-reference.md) and the
[tool list](tool-list.md).

Every tool includes MCP annotations. The core tools carry Playwright MCP's; the
Kitsune-owned ones have a human-readable title and read-only, destructive, idempotent,
and open-world hints, annotated for their most dangerous operation. Clients can use
these hints to inform permission prompts; enforcement depends on the client. The hints
are not enforced policy. The bridge enforces task-mode limits and `browser_set_policy`
(`readonly`, `allow`/`deny`, and [ask-before-act](api-reference.md#ask-before-act)).

### Core tools

The `core` tools are Playwright MCP's, published verbatim from
`src/kitsune/playwright_tools.json` (captured from `@playwright/mcp` with
`node scripts/update_playwright_tools.mjs`), so a client or prompt written for Playwright
MCP works unchanged. They drive your own browser through the Kitsune extension and return
Kitsune's compact JSON results, not Playwright's Markdown. Where the two differ:

- **Current tab.** Tools act on the tab `browser_tabs` `new` or `select` made current;
  `select` does not switch your view. Kitsune's own tools called without `tab_id`, and
  `browser_batch`, `browser_retry` and `browser_workflow` commands that name no `tabId`,
  act on the current tab too. Until there is one, reads act on the active tab, and writes
  and navigations that name no tab return `no_current_tab` without sending anything
  ([No current tab](api-reference.md#no-current-tab)). An MCP server that restarts starts
  without a current tab; its `no_current_tab` then lists the open tabs its MCP client
  opened before, the most recent first, in `state.ownTabs`, and the hint names that tab,
  so the agent can pass it as `tab_id`. The server does not pick one itself:
  the tab may belong to an earlier conversation. The bridge knows a tab's MCP client
  until the bridge, the browser or the extension restarts
  ([Tab owners](api-reference.md#tab-owners)). A tab a batch or
  workflow `newTab` opens, or an action on the
  current tab opens (a click on a `target=_blank` link, for example), becomes current,
  and the result names it in `current_tab`.
  `browser_navigate` and `browser_navigate_back` also accept a `tab_id` that Playwright
  MCP's schema does not list. `index` is the position
  in `browser_tabs` `list`, which also marks `active` and agent-`owned` tabs. `new`,
  `select` and `close` return the tabs after the change in the same form; `new` returns
  the new tab's `index`.
  `browser_close` closes the current tab, never the browser; closing a tab the agent did
  not open needs the user's approval (`user_tab`).
- **Targets.** `target` is a snapshot ref (`e12`) or a CSS selector. Actions require one
  actionable match (else `ambiguous_target`). Actions and scoped snapshots also accept a
  subset of Playwright selectors: `text=` (also `/regex/`), `role=…[name="…"]` (also
  `[name=/regex/]`), `internal:text=`, `internal:role=`, `internal:label=`, `:has-text()`,
  `:text-is()`, `:text()`, `:visible`, CSS lists, `>>` chains (also starting at a ref, as in
  `e12 >> xpath=..`), `nth=N` and `xpath=` (refused on pages holding redacted content); see
  [Target selectors](api-reference.md#target-selectors). Unsupported forms return
  `unsupported_selector` with a hint, not a retryable missing target. Element screenshots
  use the same resolver and capture the exact selected node, including inside shadow roots. Refs
  survive newer observations while their nodes remain attached in the same document; navigation or replacement nodes require new refs.
- **Snapshots.** Default core snapshots include redacted static text and resolved HTTP(S) link
  URLs (same-origin ones as paths; a link repeating the previous sibling link's URL, or, first
  under its parent, the page's, has no `/url` line). Returned inline they are capped at 12,000 characters, cut between blocks so a
  product card or paragraph is never split (a long navigation, banner or footer outside
  `main` is shown as its line and a `… not shown` marker until `main` is listed), with
  `truncated: true` and a hint toward
  `browser_find`, a scoped `target` or `filename` (which saves the whole snapshot; see
  [`browser_snapshot`](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)).
  Existing line clipping, structural
  collapsing and depth rules still apply: past the fifth of a run of similar items the
  snapshot shows `- … N more similar items; snapshot e14 to list all`, and a snapshot
  with `target` `e14` (the items' container) lists up to 100 of them. `browser_find`
  searches collapsed items too. In the full toolset, `text=False` is compact
  and `text=True` enables explicitly bounded readable output (12,000 final public chars).
  Explicit HTTP `maxChars` and post-action limits also apply after URL redaction; scoped
  snapshots remain useful for large pages.
- **Sliders and repeated keys.** `browser_fill_form` `type: "slider"` and `browser_type` (without
  `slowly`) set a native `<input type=range>` directly. An ARIA slider or spinbutton (`role=slider`
  or `spinbutton` with a numeric `aria-valuenow` and no native input, the usual custom widget)
  is set as a user would: the extension focuses it and presses arrow keys (`Home`/`End` for an
  end) until its `aria-valuenow` is the value, all in one call, and returns the final `value`.
  If the value
  is outside `aria-valuemin`/`aria-valuemax`, the call fails with `value_not_reached` before
  any key; if it is not reached (the steps skip it, the widget stops moving, or 1,000 keys
  or 20 s run out) it fails the same way with `state.value`
  ([Action errors](api-reference.md#action-errors)). `browser_press_key` also accepts a
  `repeat` count (1–100) that Playwright MCP's schema does not list; the extension presses
  the key that many times in one call, a task apart.
- **Dialogs.** `browser_handle_dialog` sets how the current tab answers its next dialogs
  (Firefox answers dialogs synchronously, so one already open cannot be answered).
- **Uploads and drops.** A synthetic click opens no file chooser, so `browser_file_upload`
  fills the file input last clicked (or the input inside it, else the page's only file
  input). `browser_drop` fires `dragenter`/`dragover`/`drop` with the files and data.
- **JavaScript.** `browser_evaluate` needs the developer extension build and runs in the
  content-script context; the result is JSON-parsed.
- **Console and network.** `browser_console_messages` starts capture on first use, so
  earlier messages are missed. `browser_network_requests` lists the page's resource
  timing entries since it loaded; there are no headers or bodies, so
  `browser_network_request` with `part` answers `not_supported`.
- **Screenshots.** `browser_take_screenshot` returns an inline image; `filename` saves
  PNG or JPEG. `type` chooses the encoding; otherwise `.jpg`/`.jpeg` filenames infer JPEG,
  with PNG as the default. Other formats answer `not_supported`; `scale` is ignored.
- **Not supported.** `browser_emulate_media` and `browser_run_code_unsafe` are listed but
  answer `not_supported` (`retryable: false`): an extension cannot override CSS media
  features, and there is no Playwright page to run code against.
- **File names.** A relative `filename` is resolved against `KITSUNE_OUTPUT_DIR` if
  set, else the first `file://` root the MCP client lists (MCP `roots`, asked for on each
  call with a `filename`; no answer within 5 seconds counts as none), else the MCP
  server's working directory. Absolute names and `~` are used as given. `saved_to` is the
  absolute path written.

Ask-before-act holds still apply. The core schemas have no `confirm_token`; the tools
accept it anyway, so after the user approves a held action an agent can repeat the call
with `confirm_token` from the `confirmation_required` error.

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
| `read` | Read tools plus navigation to HTTP(S) pages (or `about:blank`) and scrolling. `browser_tab_list` replaces the multi-action `browser_tabs`; with `new_tab` it opens a background tab that later calls act on. It cannot close or select tabs. No clicks, field writes, JavaScript, submits, confirmations or screenshots/file writes. |
| `draft` | Read surface plus `browser_type`, `browser_select_option`, `browser_fill_form` and, with the `browse` group, `browser_check`. Fills plain text only; no click/keypress/submit/dialog confirmation or JavaScript. Ask-before-act is forced: sensitive fields, payment-like pages and uninspectable targets are refused before filling, without a confirmation token. |
| `act` | Existing behavior, including the independently configured ask-before-act policy. |

Modes intersect with `KITSUNE_CAPS`; they never enable a disabled group. With the
default `core` group, `read` publishes eight tools: list-only tabs, navigate, snapshot,
find, wait for text, console messages and the network request list and details.
`draft` adds type, select-option and fill-form. Page text, forms and DOM need the
`read` group, and scrolling the `browse` group. Extra enabled groups contribute only
their read tools. The personal-data `storage` and `history` groups still require
explicit opt-in.

Restricted surfaces use first-paragraph descriptions and omit `confirm_token`.
Calling an unpublished tool by name returns `tool_not_published` with the mode setting
needed to publish it; `browser_status.mcp_tools.mode` identifies a restricted server.
The [gateway tools](#finding-tools-without-a-restart) are listed in every mode and reach
only the tools the mode permits.
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

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_CAPS` | `core` | Comma-separated capability groups above; `all` enables every group except `storage` and `history`, which read personal data and must be named |
| `KITSUNE_PUBLISH` | `KITSUNE_CAPS` | Groups of the enabled tools that `tools/list` shows when a session starts; the others stay reachable through the gateway tools. See [Finding tools without a restart](#finding-tools-without-a-restart) |
| `KITSUNE_MODE` | `act` | Task surface and per-request policy: `read`, `draft`, or `act`. See [Task modes](#task-modes) |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |
| `KITSUNE_EXTENSION_DIR` | `extension/` in a checkout | Unpacked extension the bridge compares a developer build with, to report one that was edited but not reloaded, and whose `WS_URL` port it expects before the extension connects ([Connection diagnosis](api-reference.md#connection-diagnosis)) |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | Set to `1` in the MCP server environment to skip automatic replacement of an outdated bridge |
| `KITSUNE_BRIDGE_IDLE_TIMEOUT` | `0` (never) | Seconds without client activity before the bridge exits; see [Idle shutdown](#idle-shutdown-opt-in) |
| `KITSUNE_OUTPUT_DIR` | Unset | Directory, in the MCP server environment, for relative `filename` arguments of the `core` tools; unset, they resolve against the client's first root, else the server's working directory (see [Core tools](#core-tools)) |
| `KITSUNE_OWNER_ID` | The MCP client's process | Tab owner id of the MCP client, in the MCP server environment: the bridge records the tabs it opens as this client's and offers them after a server restart. Up to 64 letters, digits, `.`, `_`, `:` or `-`. Set it when the server runs through a launcher other than `uv` or `uvx`, such as a shell script or a Python wrapper. Give each MCP client its own value: clients with the same value share their tabs. See [Tab owners](api-reference.md#tab-owners) |
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
  This is not a sandbox: `browser_evaluate` can read anything the page can, and the
  detection is heuristic. Do not assume a visually masked field is hidden from
  the agent or its logs.
- **Typed values outside the bridge.** The bridge audit log (`browser_audit`)
  keeps the action, target, tab, timing and outcome of each write action but
  replaces typed text (`browser_type`, `browser_fill_form` values,
  `browser_press_key` text, `browser_select_option` values, `browser_drop` data and
  `browser_handle_dialog` prompt answers) and stored secrets (`browser_cookies`,
  `browser_storage` and `browser_clipboard` values) with `"[redacted]"`; see
  [Audit log](api-reference.md#audit-log). The page and the MCP client's own
  transcript still see whatever the agent sent.
- **Cookies, page storage, saved sessions and the clipboard.** Off unless you name
  the `storage` capability group; `KITSUNE_CAPS=all` leaves it out. Its tools read and
  write the cookies that keep you logged in, page storage and the system clipboard:
  enable it only for agents you trust with your accounts.
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

Capability groups bound what an agent can reach through one MCP server, and the bridge
checks them on every request, container steps included. They are not a sandbox: the
`kitsune-bridge` command and other local programs with the token are not restricted.

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
`tab_id`. Without `tab_id`, a call acts on the agent's current tab. Before the agent
has opened or selected one, a write or navigation without `tab_id` fails with
`no_current_tab` instead of acting on the tab you see, but the agent can still name
your tab or select it.

To enforce this, set `KITSUNE_PROTECT_USER_TABS=1` in the MCP server environment
and restart the client. It applies to that client only. Then `browser_navigate`,
`browser_navigate_back` and `browser_forward` on a tab the agent did not open return a
`user_tab` error instead of navigating, also inside batches. That error tells the
agent to use its own tab, and on a direct call it carries a `confirm_token`. As with
ask-before-act, the token proves an explicit second step, not that you approved.
In `read` and `draft` modes, which cannot confirm, the error carries no token; the
agent opens its own tab with `browser_tab_list(new_tab=...)` instead, so a read-only
agent can still reach any page without touching yours.

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
| `extension_disconnected` (`extension_connected: false`) | Extension installed and enabled in the browser profile you opened? Developer build: browser restarted (temporary add-on gone)? It reconnects within 15 s of a bridge start; the first tool call after the MCP server starts a bridge waits up to 16 s for it |
| `extension_version_mismatch` | The extension and server versions differ; install the matching extension or update the server, then reload open tabs. `state.reason: "stale_developer_build"`: the developer build's files changed since the browser loaded them; reload it in about:debugging |
| `browser_window_unavailable` | The browser runs without a normal window Kitsune can use; open one, or allow Kitsune in private windows (about:addons → Kitsune → Run in Private Windows) |
| `host_permission_denied` | `not_granted`: allow site access in about:addons → Kitsune → Permissions. `restricted_domain`: Firefox keeps extensions off Mozilla sites such as addons.mozilla.org; use another tab |
| "needs the Kitsune developer extension build" | The signed build cannot run agent-supplied JavaScript; load the developer build |
| `bridge_unavailable` (`Kitsune bridge did not start`, `exited at startup`) | `state.logExcerpt` shows the bridge's last log line; see `~/.kitsune/bridge.log`, or run `kitsune-bridge start` in a terminal to see the error. `state.reason: "not_responding"`: the bridge hangs; run `kitsune-bridge stop` and retry |
| `port_conflict` | `state.port` is held by another program (`lsof -nP -iTCP:47651 -sTCP:LISTEN`), or `KITSUNE_WS_PORT` differs from the extension's `WS_URL` |
| `token_mismatch` (`401 Missing or invalid token`) | `KITSUNE_TOKEN` or `KITSUNE_HOME` set in one client but not in the daemon (or the other way round) |
| `Cannot access browser internal page` | `about:` pages are off-limits to extensions; switch to a normal tab |
| A click returns `dispatched: true` but the page does not react (`outcome_not_observed`, target still visible) | Some pages accept only trusted input and ignore Kitsune's synthetic events. Check the page with `browser_snapshot` first, then try one [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone) on the same target and verify it with a read. It needs the `browse` capability, moves your real cursor for the click (then back, unless `restore_pointer=False` for a hover menu that closes when the pointer leaves) and, on macOS, needs pyobjc Quartz or `cliclick` plus Accessibility permission; on Linux, needs `xdotool` in an X11 session (not Wayland), and HiDPI X11 scaling is not compensated, so the click may land off by the scale factor. Page zoom is compensated, and a target covered by another element (an overlay or banner) returns `target_not_at_point` without clicking. Kitsune never switches to it on its own |
| A hover returns `ok: true` but the menu or row controls it should reveal stay hidden | `browser_hover` sends pointer and mouse events, including `pointerenter`/`mouseenter` to each entered ancestor, but it cannot trigger CSS `:hover`, which follows only the real pointer. Use [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone) with `restore_pointer=False` on an element where a click is harmless, which leaves your real pointer there, or ask the user to point at it. See [POST /api/hover](api-reference.md#post-apihover) |
| A background tab's snapshot is empty, or a menu item a click just opened returns `hidden_or_covered` with `state.hiddenTab` or `state.inPopup` | Results from a hidden tab carry `visibilityState: "hidden"`; tabs the agent opens are in the background by default, so their results usually carry it. Some pages render lazily, or do not paint menus and popups, while their tab is hidden (#259, #260). Events dispatched to an unpainted item still work, so act on it if it belongs to the control you meant and check the effect. If content never appears, Kitsune's `browser_tabs` select with `tab_id` shows the tab and switches the user's own view, so ask the user first; it is refused with `user_active` while they use the tab they would leave. The core `browser_tabs` select only makes a tab current. See [Snapshot readiness](api-reference.md#snapshot-readiness) |
| "Continue with Google" or another sign-in button opens no popup, or the site says "Something went wrong" | With Firefox's default popup blocker, synthetic clicks cannot open OAuth/SSO popups (the click result may have a `popup_blocked` warning with `state.reason: "script_popup"`); ask the user to click, or use a trusted click. See [Sign-in popups (OAuth/SSO)](#sign-in-popups-oauthsso) |
| `stale ref, call browser_snapshot again` | Take a fresh snapshot of the same `tab_id` and use its new refs |
| Bridge updated but browser behavior is unchanged | Reload the extension, then reload affected pages so their content scripts update too |
| Restart the daemon | Run `kitsune-bridge stop` with the same source as your clients (`uvx --from git+…` or `uv run` in a checkout); the next tool call starts it again. This briefly affects all clients sharing it |
| `value-attribute selectors are disabled …` | A sensitive field on the page has a `value` attribute; target it by ref, label, or a selector that does not test `value` |
| Setup, uv, or upgrade problems | See [Installation troubleshooting](installation.md#troubleshooting) |

### Sign-in popups (OAuth/SSO)

"Continue with Google", "Sign in with Apple" and similar buttons open the provider's
sign-in in a popup window or tab with `window.open`. With the default popup blocker
(`dom.disable_open_during_load=true`, see
[Popup-blocked links](api-reference.md#popup-blocked-links)), Firefox allows that only
during user activation, that is, a real click or key press. `browser_click` dispatches
synthetic DOM events without user activation, so the popup is blocked or the site's
sign-in script fails: no window opens, or the page shows an error such as "Something
went wrong. Please try again." Ordinary buttons on the same page still work. Kitsune
reopens only blocked `target="_blank"` links (see
[Popup-blocked links](api-reference.md#popup-blocked-links)). When the button's script
calls `window.open` during the click and the popup blocker refuses it, the click result
has a `popup_blocked` warning with `state.reason: "script_popup"` (see
[Script-opened popups](api-reference.md#script-opened-popups)). A script that fails
before calling `window.open`, opens the popup later, or runs on a page whose Content
Security Policy blocks Kitsune's wrapper gives no warning, so the click can still look
successful or end in `outcome_not_observed`.

There are two ways through:

1. **Ask the user to click (recommended).** The agent asks the user to click the
   button, waits for them, then lists tabs with `browser_tabs` to find the popup or
   tab that opened and continues there by `tab_id`. The user may also have to finish
   the provider's sign-in (account chooser, password, two-factor). This is the
   default to prefer because the browser is shared (#139): a trusted click moves your
   real pointer, may show the target tab and can take focus.
2. **[`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone).**
   A real OS click has user activation, so the popup opens. It needs the `browse`
   capability and a visible browser window, and its result does not list
   `opened_tabs`, so find the popup with `browser_tabs`. Kitsune never switches to it
   on its own. On Linux it needs `xdotool` in an X11 session. On macOS, set it up once:
   - Install a click backend: register the server with
     `uvx --from 'kitsune-browser-mcp[macos]' kitsune` (pyobjc Quartz; the extra
     installs nothing on other platforms), or run `brew install cliclick` so
     `cliclick` is on the `PATH` your MCP client passes to the server.
   - Grant Accessibility permission to the app running the bridge, usually the
     terminal or MCP client app that started it (System Settings > Privacy &
     Security > Accessibility).
   - Run `kitsune-bridge stop` with the same source as your clients so that the next
     start picks up the change. The click runs in the shared bridge, and the bridge
     runs with the Python (`sys.executable`) and `PATH` of whichever MCP server
     started it first. Adding the extra to one client's command does nothing until
     the bridge starts again from that client, so make the next tool call from it, or
     add the extra to every client.

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
when the `core` total grows past `TOTAL_BUDGETS` or `FIRST_PARAGRAPH_BUDGETS`, or any
single tool past `TOOL_BUDGET`; raise a budget only deliberately. The `core` tools are
Playwright MCP's, published verbatim, and `tests/test_playwright_tools.py` checks the
default `tools/list` against `src/kitsune/playwright_tools.json`. The Kitsune-owned tools'
published input schemas omit the `title` keywords pydantic generates (`KitsuneMCP.list_tools`), and
`tests/test_tool_schema_titles.py` checks that they still accept and reject the same
arguments. They also publish an optional parameter whose default is null as its plain
type instead of `anyOf: [type, null]`, and omit `null` and `""` defaults. Calls are
still validated against the pydantic models, so an explicit `null` still works and
behaves as omitted; `tests/test_tool_schema_optionals.py` checks this for every tool.
Every client sends input schemas in full, so parameters are documented there
(`Annotated[..., Field(description=...)]`), not in `Args:` blocks. A tool's first
paragraph says what it does and what a caller must know. The Kitsune-owned action tools
share one contract (ref, element, wait, expect, confirm_token, first), documented once in
`browser_check`'s parameter descriptions; the other action tools say in their first
paragraph that `ok` means dispatched, name `expect` and point to `browser_check` rather
than repeating it. `tab_id` is described wherever it is accepted.
`tests/test_tool_first_paragraph.py` checks these rules for every Kitsune-owned tool.

The opt-in real-browser suites (`tests/test_real_browser*.py`) run against a real
Firefox, Zen or LibreWolf. They cover action races, tab unloads and reconnects
(`test_real_browser.py`), tabs opened by `target=_blank` clicks with popups allowed
and with the default popup blocker (`test_real_browser_opened_tabs.py`), every
read tool on empty 404 and 500 pages (`test_real_browser_error_pages.py`), and
which form or button an action picks on a page with repeated controls
(`test_real_browser_action_targets.py`). They are skipped unless `KITSUNE_E2E_BROWSER`
names the browser binary. A full run takes several minutes, depending on the host
and browser; run one suite when iterating:

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

To keep evidence from a failing run, set `KITSUNE_E2E_ARTIFACTS` to a directory:

```bash
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-e2e-artifacts \
  uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v
```

Each failing test, or failing class setup, writes one directory under
`kitsune-e2e-<UTC time>-<pid>/<module.Class>/` and prints its path to stderr.
`timeline.jsonl` puts the harness's bridge requests, the fixture server's request
events and the browser's top-level navigation events (load start/stop, location
changes, tab open/close) on one wall clock, so a slow page load can be told apart from
a slow fixture reply. For each fixture request it records when the request was
received and then either `request-handled` (the handler returned and the server
flushed its reply) or `request-failed` with the exception type (for example
`BrokenPipeError` after the browser dropped the connection; the error still reaches
the server). Both are server-side only and do not show that the browser received the
reply. Only bridge requests sent through the harness's `call()` are in the timeline;
`open_tab` (`/api/new-tab`) is, but suites that send `harness._request` directly
(`test_real_browser.py`, `test_real_browser_protected_tabs.py`,
`test_real_browser_submit_form.py`, `test_real_browser_connection_diagnostics.py`)
appear only in `bridge.log`. Harness and fixture events share one buffer of 4000
events per test process; `summary.json` reports how many events recorded since the
class started were evicted (`droppedSinceClassStart`, with `complete: false` when
any were) and, separately, all evictions in the process (`droppedInProcess`).
`browser.log` and `bridge.log` hold the last 256 KiB of the owned browser's and
bridge's output, `resources.json` the load average and ps samples of the test runner,
the bridge and the browser's process group, and `summary.json` what was collected. Passing classes write nothing; a class
keeps at most five failures; nothing under the directory is ever deleted by the
harness. The timeline and summaries hold no headers, request bodies or page contents,
and URLs in them keep only a loopback origin and path. The two log tails are copied as
the owned processes wrote them, with the bridge token, Authorization, Bearer and
`token=` values redacted; with the variable set, the owned bridge writes its log
unbuffered so that it is current when a test fails. Without the variable the suites
run exactly as before.

To publish a new version to PyPI, see [Releasing](releasing.md).

## Layout

```
extension/          WebExtension (manifest v2, Gecko)
packages/kitsune-browser-mcp/  The MCP server's PyPI package: pins kitsune-browser[mcp], adds the commands
src/kitsune/        The kitsune-browser package (the repository root's pyproject.toml)
  server.py         MCP server: registers the tools on FastMCP, within KITSUNE_CAPS and KITSUNE_MODE
  tools.py          The agent tools (Kitsune's own) without MCP code; playwright_tools.py has the core group
  browser.py        Python API (kitsune.connect, Browser, Tab); errors.py has its exceptions
  bridge_client.py  Bridge requests and connection errors; starts the bridge on demand
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
