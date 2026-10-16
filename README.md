# Kitsune

An MCP server that lets AI agents (Claude Code, Codex, Pi, …) drive the
Firefox-family browser you already have open: Firefox, Zen, LibreWolf.

![Claude Code asks Kitsune to open Hacker News in Zen, read the top 3 stories and summarize them](docs/demo.gif)

<sub>Frames captured by Kitsune itself (`browser_screenshot`) during a real run, sped up.</sub>

- **Your real browser.** Logged-in sessions, cookies and extensions stay as they are.
- **No Playwright, no Chromium, no WebDriver.** A WebExtension does the work, so
  there is no `navigator.webdriver` flag or debugging port.
- **Zero manual steps per session.** The MCP server starts a shared bridge daemon
  on demand; any number of agents can use it at once.

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
   detached daemon. A file lock makes sure only one agent spawns it.
3. The extension keeps reconnecting to the daemon, so it attaches by itself.
4. The daemon keeps running after the agent exits; the next agent reuses it.

## Requirements

- Firefox-family browser, Gecko 142+ (Zen 1.23 / Firefox 142 or newer)
- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- macOS or Linux (Windows untested since the fork)

## Setup

1. **Clone**
   ```bash
   git clone https://github.com/junyeong-nero/kitsune ~/workspace/kitsune
   ```
2. **Register the MCP server** in your agent. See [MCP clients](#mcp-clients).
3. **Load the extension**: open `about:debugging#/runtime/this-firefox` →
   **Load Temporary Add-on…** → pick `extension/manifest.json`
   (in the macOS file dialog, Cmd+Shift+G lets you paste the path).
   Temporary add-ons disappear when the browser restarts; signing is on the
   [TODO](docs/TODO.md).
4. **Check**: restart the agent and ask it to call `browser_status`. It should
   report `"extension_connected": true`.

## MCP clients

Kitsune is a stdio MCP server. Every client runs the same command:

```
uv run --directory /path/to/kitsune kitsune
```

Config files do not expand `~`, so use an absolute path there. If the client
cannot find `uv` (common for GUI apps), use its full path from `which uv`.

**Claude Code**

```bash
claude mcp add -s user kitsune -- uv run --directory ~/workspace/kitsune kitsune
```

**Codex CLI**

```bash
codex mcp add kitsune -- uv run --directory ~/workspace/kitsune kitsune
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.kitsune]
command = "uv"
args = ["run", "--directory", "/Users/you/workspace/kitsune", "kitsune"]
```

**opencode**: `~/.config/opencode/opencode.json` (global) or `opencode.json` at a git repo root
(project). Write it by hand: `opencode mcp add` (v2.0.28) writes `mcp.servers`, which `opencode mcp list` does not pick up.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kitsune": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/Users/you/workspace/kitsune", "kitsune"],
      "enabled": true
    }
  }
}
```

**Claude Desktop / Cursor / other `mcpServers` clients**
(`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "kitsune": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/you/workspace/kitsune", "kitsune"]
    }
  }
}
```

Several clients can be configured at once; they share one bridge daemon.

## Tools

106 tools, all named `browser_*`. Tab-aware tools take an optional `tab_id`, so
an agent can work in a background tab without stealing focus.

| Group | Examples |
|---|---|
| Status | `browser_status`, `browser_health`, `browser_tabs` |
| Navigation | `browser_new_tab`, `browser_navigate`, `browser_back`, `browser_close_tab` |
| Reading | `browser_markdown`, `browser_readability`, `browser_page_text`, `browser_dom`, `browser_query`, `browser_forms` |
| Interaction | `browser_click`, `browser_type`, `browser_fill`, `browser_form_fill`, `browser_select_option`, `browser_keypress`, `browser_scroll` |
| Visual | `browser_screenshot`, `browser_element_screenshot`, `browser_full_page_screenshot` |
| Waiting | `browser_wait_for_element`, `browser_wait_for_url`, `browser_wait_for_network_idle` |
| Network / state | `browser_cookies`, `browser_storage`, `browser_intercept`, `browser_capture_network` |
| Sessions | `browser_save_session`, `browser_load_session` |
| Parallel work | `browser_parallel`, `browser_tab_pool`, `browser_keep_alive`, `browser_batch` |
| Policy / logs | `browser_set_policy`, `browser_audit`, `browser_logs` |

`browser_js` runs arbitrary JavaScript in the page. Prefer the specific tools.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |

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

Recommended: use a separate browser profile that only has the accounts the
agent needs, and keep banking, mail and work SSO out of it.

## Troubleshooting

| Symptom | Check |
|---|---|
| `extension_connected: false` | Extension loaded? Browser restarted (temporary add-on gone)? |
| `Kitsune bridge did not start` | `~/.kitsune/bridge.log`; another process on port 47651/47652 (`lsof -nP -iTCP:47651`) |
| `401 Missing or invalid token` | `KITSUNE_TOKEN` set in the client but not in the daemon (or the other way round) |
| `Cannot access browser internal page` | `about:` pages are off-limits to extensions; switch to a normal tab |
| Restart the daemon | `pkill -f kitsune.bridge`; the next tool call starts it again |

## Layout

```
extension/          WebExtension (manifest v2, Gecko)
src/kitsune/
  server.py         MCP server, starts the bridge on demand
  bridge.py         Bridge daemon (HTTP API + WebSocket)
  workflows/        Named workflows for browser_workflow
docs/               API reference, scraping playbooks, TODO
```

## Credits

Kitsune started as a fork of [ZenLink](https://github.com/JayQuan-McCleary/ZenLink)
and [ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) by Jay-Quan
(MIT). Changes since the fork: token auth, Host and origin checks, no CORS,
on-demand shared daemon, one package, `browser_*` tool names, `~/.kitsune` data
directory, macOS support, `mcp<2` pin.

## License

MIT. See [LICENSE](LICENSE).
