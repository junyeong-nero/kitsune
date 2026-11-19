# Installation

Kitsune has two parts:

1. **The MCP server**, which your client launches over stdio. uv downloads and runs it.
2. **The browser extension**, which you install yourself.

The server starts a shared bridge daemon on demand, and the extension connects to it by itself.
Setup problems: [Troubleshooting](troubleshooting.md#setup-and-installation). Running from a
checkout: [Development](development.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (provides `uvx` and Python 3.10+).
- Gecko 142+: Firefox 142+, Zen 1.23+ or LibreWolf.
- macOS or Linux.

## 1. Register the MCP server

Every client runs the same stdio command:

```text
uvx --from kitsune-browser-mcp kitsune
```

The package is `kitsune-browser-mcp`; `kitsune-mcp` is an unrelated project. To warm the uv cache
and check the install without starting a daemon:

```bash
uvx --from kitsune-browser-mcp kitsune-bridge status
```

For `browser_trusted_click` on macOS, use `kitsune-browser-mcp[macos]` or install `cliclick`
([Trusted clicks](troubleshooting.md#trusted-clicks)).

### Claude Code

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

Pass environment variables with `-e` before the `--`, e.g. `-e KITSUNE_SCRIPTING=0`.

### Codex CLI

```bash
codex mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.kitsune]
command = "uvx"
args = ["--from", "kitsune-browser-mcp", "kitsune"]

[mcp_servers.kitsune.env]  # optional
KITSUNE_SCRIPTING = "0"
```

### Pi

```bash
pi mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
```

This writes `~/.pi/agent/mcp.json` (`--local` writes `.pi/mcp.json`). Use `/reload` in a running
session.

### Claude Desktop, Cursor and other `mcpServers` clients

Merge into the client's JSON config (Claude Desktop on macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`; Cursor: `~/.cursor/mcp.json`):

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

Add `"env": {...}` next to `args` for environment variables.

### OpenCode

In `~/.config/opencode/opencode.json` or a project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kitsune": {
      "type": "local",
      "command": ["uvx", "--from", "kitsune-browser-mcp", "kitsune"],
      "enabled": true
    }
  }
}
```

GUI apps often don't inherit your shell `PATH`: if `uvx` isn't found, use the full path from
`which uvx`. Several clients share one bridge; register them all with the same source and
version ([compatibility](#extension-and-server-compatibility)).

## 2. Install the browser extension

Download the extension for the **same version** as the server from
[GitHub Releases](https://github.com/junyeong-nero/kitsune/releases).

| | Signed (`kitsune-<version>.xpi`) | Developer (`kitsune-extension-dev-<version>.zip`) |
|---|---|---|
| Install | Once; updates itself | Temporary; reload after every browser restart |
| Agent JavaScript (`browser_evaluate`, JS batch conditions) | No (Mozilla policy) | Yes |

`browser_status` reports the connected `extension_build` (`signed` or `dev`).

### Signed build (recommended)

1. Download `kitsune-<version>.xpi`.
2. In `about:addons`, gear icon → **Install Add-on From File…**, select the XPI and approve.

### Developer build

1. Download and unzip `kitsune-extension-dev-<version>.zip` (or use `extension/` from a checkout).
2. In `about:debugging#/runtime/this-firefox`, **Load Temporary Add-on…** and select its
   `manifest.json`.

Both builds share an add-on ID; a loaded developer build replaces the signed one until restart.

## 3. Check the connection

Restart the agent and ask it to list your tabs. An error's `hint` says what to fix;
`browser_status` should report `"extension_connected": true`. Then [try a first task](try-it.md).

## Pinning a version

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp==0.8.0 kitsune
```

For an unreleased commit, tag or branch, use the Git source and get the extension from the same
ref:

```bash
claude mcp add --scope user kitsune -- uvx --from "git+https://github.com/junyeong-nero/kitsune.git@<ref>#subdirectory=packages/kitsune-browser-mcp" kitsune
```

## Upgrading

**Server.** uvx keeps using its cached version. Refresh it, then reconnect the client:

```bash
uvx --refresh --from kitsune-browser-mcp kitsune-bridge status
```

Or register `kitsune-browser-mcp@latest` to check on every start. For pinned versions, change
the version in the client config. In a checkout: `git pull && uv sync`.

**Bridge.** The MCP server restarts a bridge whose `build` differs from its own, briefly
interrupting other clients; `KITSUNE_NO_BRIDGE_RESTART=1` opts out. Manage it manually with
`kitsune-bridge status` / `stop` (from the same source as your clients).

**Extension.** The signed build updates itself; install a new XPI to update right away. Reload
the developer build in `about:debugging`. A bridge restart does not reload extension code; reload
existing tabs if their content scripts are outdated.

### Idle shutdown (opt-in)

Set `KITSUNE_BRIDGE_IDLE_TIMEOUT` (seconds) in the MCP server environment, or run
`kitsune-bridge start --idle-timeout SECONDS`, to stop the bridge after inactivity. Each HTTP call
counts as activity; the timer pauses while a request, extension command or `browser_keep_alive`
loop is running. The next tool call starts a fresh bridge. The first server to start the bridge
decides the timeout.

## Automatic extension updates

The signed build checks
`https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json` about once a day
(about:addons → gear → **Check for Updates** to check now). The extension can then be newer than a
cached server; `browser_status` shows `extension_version` differing from `bridge_version` until you
refresh the server. If you pin an old server, turn off automatic updates for Kitsune.

## Extension and server compatibility

- Install the server and extension from the same release or ref; they are tested only together.
- Use one install source for all clients. A checkout's `build` hashes its `extension/` files and
  differs from a `uvx` install of the same commit, so mixed clients keep replacing the bridge.
  To mix sources anyway, set `KITSUNE_NO_BRIDGE_RESTART=1` and manage the bridge yourself.
