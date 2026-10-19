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

Refs are scoped to the document and snapshot generation. A new snapshot or
`browser_dom` invalidates earlier refs in that tab; `browser_find` and other
ref-producing queries use the same scheme. Detached or outdated refs return
`stale ref, call browser_snapshot again`. Always use refs from the latest result.

Click, type, fill, select, check, keypress, submit, and navigate responses include
a fresh `snapshot` capped at 4,000 characters (`snapshot_truncated` indicates more
content). If snapshot capture fails, `snapshot_error` preserves the action result.
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
2. **Load the extension**: download and unzip `kitsune-extension-<version>.zip`
   from [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases),
   open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** →
   pick its `manifest.json` (in the macOS file dialog, Cmd+Shift+G lets you
   paste the path).
   Temporary add-ons disappear when the browser restarts; a signed XPI is
   planned (see [Installation](installation.md#planned-distribution)).
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
3. Download the extension zip for the same version as the server, then reload the
   extension in `about:debugging#/runtime/this-firefox` (click **Reload** on
   Kitsune, or load the updated `extension/manifest.json` again). A bridge
   restart reconnects the extension but does **not** reload its browser-side code;
   reload it explicitly after extension updates, and reload existing tabs if
   their content scripts are still outdated.
4. Reconnect the MCP client again if needed (in Claude Code, use `/mcp` to
   reconnect Kitsune), then call `browser_status`. Check `extension_connected`,
   `bridge_version`, and `build`.

Shutdown uses a token-authenticated endpoint, with a verified, locked PID file
as the termination fallback; it never uses a broad `pkill`. Very old daemons
that have neither the shutdown endpoint nor a PID file require a **one-time
manual stop of the specific old bridge process**. Kitsune reports this instead
of guessing a PID or launching another daemon over it. After that, automatic
replacement and `kitsune-bridge stop` work normally.

## Tools

By default, Kitsune registers 30 `browser_*` tools in the `core` capability
group. Tab-aware tools accept `tab_id` to target background tabs.
Set `KITSUNE_CAPS` in the MCP server environment before starting it; restart the
MCP client after changing it. The comma-separated list selects exactly those
groups (include `core` to retain the default tools). `all` enables all 95 tools.
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
| `parallel` (15) | `browser_keep_alive`, `browser_keep_alive_stop`, `browser_parallel`, `browser_broadcast`, `browser_sync_barrier`, `browser_tag_tab`, `browser_resolve_tag`, `browser_list_tags`, `browser_untag_tab`, `browser_tab_pool`, `browser_pool_acquire`, `browser_pool_release`, `browser_scheduler`, `browser_retry`, `browser_batch` |
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
including when called in a read mode.

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

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_CAPS` | `core` | Comma-separated capability groups above; `all` enables everything |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | Set to `1` in the MCP server environment to skip automatic replacement of an outdated bridge |
| `KITSUNE_POST_ACTION_SNAPSHOT` | Enabled | Set to `0` in the bridge daemon environment and restart it to disable automatic post-action snapshots |

`~/.kitsune/` holds:

| File | Content |
|---|---|
| `token` | Bearer token for the HTTP API (0600, created on first start) |
| `bridge.log` | Daemon output |
| `sessions/` | Saved sessions, **including cookies** |
| `screenshots/` | Screenshots |

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
  `type=password` values, and selectors that probe a password's `value`
  attribute are refused; see [Password redaction](api-reference.md#password-redaction).
  This is not a sandbox: `browser_js` can read anything the page can, and
  masked fields that are not `type=password` (or sit inside shadow DOM) are not
  redacted. Do not assume a visually masked field is hidden from the agent or its logs.
- **Authenticated actions.** Existing sessions let the agent act as you on
  accessible sites. MCP annotations are client hints, not an approval system.

Kitsune does not provide a password-vault listing tool. Native password-manager
autofill is browser-controlled; whether an autofilled value is accessible depends
on what the browser makes available to the page. Test it only with disposable
credentials, and never paste real secrets into bug reports.

Recommended: use a separate browser profile that only has the accounts the
agent needs, and keep banking, mail and work SSO out of it. Enable only the
capability groups you need, review the agent's actions, and treat saved sessions,
screenshots, and tool logs as potentially sensitive.

## Troubleshooting

| Symptom | Check |
|---|---|
| `extension_connected: false` | Extension loaded? Browser restarted (temporary add-on gone)? |
| `Kitsune bridge did not start` | `~/.kitsune/bridge.log`; another process on port 47651/47652 (`lsof -nP -iTCP:47651`) |
| `401 Missing or invalid token` | `KITSUNE_TOKEN` set in the client but not in the daemon (or the other way round) |
| `Cannot access browser internal page` | `about:` pages are off-limits to extensions; switch to a normal tab |
| `stale ref, call browser_snapshot again` | Take a fresh snapshot of the same `tab_id` and use its new refs |
| Bridge updated but browser behavior is unchanged | Reload the extension, then reload affected pages so their content scripts update too |
| Restart the daemon | Run `kitsune-bridge stop` with the same source as your clients (`uvx --from git+…` or `uv run` in a checkout); the next tool call starts it again. This briefly affects all clients sharing it |
| `value-attribute selectors are disabled …` | A password field on the page has a `value` attribute; target it by ref, label, or a selector that does not test `value` |
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
