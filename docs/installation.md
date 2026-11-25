# Installation

You'll need Firefox 142+, Zen 1.23+ or LibreWolf on macOS, Linux or [Windows](windows.md).
Setup recognizes Claude Code, Codex CLI, Pi, Cursor, OpenCode and Claude Desktop (macOS or Windows).

<a id="2-install-the-browser-extension"></a>

## 1. Install the browser extension

[Open the latest release](https://github.com/junyeong-nero/kitsune/releases/latest){ .md-button .md-button--primary }

1. Download **`kitsune-<version>.xpi`** from the release's Assets list.
2. In Firefox, Zen or LibreWolf, type `about:addons` in the address bar and press Enter.
3. Click the **gear icon**, then **Install Add-on From File…**, and pick the `.xpi` file.
4. Click **Add** when the browser asks.
5. Pin Kitsune to the toolbar: click the **Extensions** (puzzle) button, then the gear next to
   Kitsune, then **Pin to Toolbar**. Tab handoff requests and tool approvals show up in its popup.

The signed extension updates itself. If you [pin the server](#pinning-a-version), use the XPI
from the same release. To let your agent run its own JavaScript in pages, use the
[developer build](#developer-build) instead.

<a id="1-install-uv"></a>
<a id="1-register-the-mcp-server"></a>
<a id="3-add-kitsune-to-your-agent"></a>

## 2. Register the MCP server

Close the clients you want to configure, then run:

<pre><code><a href="https://docs.astral.sh/uv/getting-started/installation/" title="Install uv (includes uvx)">uvx</a> kitsune-browser-mcp@latest setup</code></pre>

If `uvx` isn't on your PATH, the link above takes you to the uv installer.
Press **Enter** to use the detected clients, or change the selection with **↑/↓** and **Space** first.
Reopen the clients you selected, or run `/reload` in Pi.

??? info "Setup flags and config changes"

    - `--list` shows each supported client, its detection status and its config path. It doesn't write anything.
    - `--auto` registers only detected clients, without a prompt.

    Setup edits the `kitsune` entry in each selected client's user config. It switches standard PyPI
    commands, including version pins, to `@latest` and keeps settings such as `env`. Unchecked clients
    aren't changed. For a checkout, a `[macos]` extra, or JSON with comments or trailing commas,
    use [manual registration](#manual-registration).

<a id="4-check-that-it-works"></a>

## Check that it works

Keep the browser open and ask your agent: **"List my open browser tabs."** The Kitsune toolbar
popup should say **Connected**. Once you see your tabs, [try a first task](try-it.md).

??? question "It doesn't work"

    Check the bridge from a terminal:

    ```bash
    uvx --from kitsune-browser-mcp kitsune-bridge status
    ```

    - `"status": "stopped"`: check the [MCP registration](#2-register-the-mcp-server) and restart the agent.
    - `"extension_connected": false`: make sure the extension is enabled in `about:addons` and the browser is open.
    - `"extension_connected": true`: the setup works. If a tool call fails, its error's `hint` says what to fix.

    More in [Troubleshooting](troubleshooting.md#setup-and-installation).

## More setup options

### Manual registration

If you prefer to configure a client yourself, register the **server** command without `setup`:

=== "Claude Code"

    Run once:

    ```bash
    claude mcp add --scope user kitsune -- uvx kitsune-browser-mcp@latest
    ```

    Then start a new Claude Code session. To set an environment variable, add `-e` before the
    `--`, for example `-e KITSUNE_SCRIPTING=0`.

=== "Codex CLI"

    Run once:

    ```bash
    codex mcp add kitsune -- uvx kitsune-browser-mcp@latest
    ```

    Then start a new Codex session. Or add it to `~/.codex/config.toml` yourself:

    ```toml
    [mcp_servers.kitsune]
    command = "uvx"
    args = ["kitsune-browser-mcp@latest"]

    [mcp_servers.kitsune.env]  # optional
    KITSUNE_SCRIPTING = "0"
    ```

=== "Pi"

    Run once:

    ```bash
    pi mcp add kitsune -- uvx kitsune-browser-mcp@latest
    ```

    This writes `~/.pi/agent/mcp.json` (`--local` writes `.pi/mcp.json`). In a running session,
    type `/reload`.

=== "Claude Desktop"

    Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
    `%APPDATA%\Claude\claude_desktop_config.json` (Windows), merging it with any `mcpServers` already there:

    ```json
    {
      "mcpServers": {
        "kitsune": {
          "command": "uvx",
          "args": ["kitsune-browser-mcp@latest"]
        }
      }
    }
    ```

    Quit and reopen Claude Desktop. If it can't find `uvx`, replace `"uvx"` with the full path
    that `which uvx` prints: apps started from the Dock don't see your shell's `PATH`. Add
    `"env": {...}` next to `args` for environment variables.

=== "Cursor"

    Add this to `~/.cursor/mcp.json`, merging it with any `mcpServers` already there:

    ```json
    {
      "mcpServers": {
        "kitsune": {
          "command": "uvx",
          "args": ["kitsune-browser-mcp@latest"]
        }
      }
    }
    ```

    Restart Cursor. If it can't find `uvx`, replace `"uvx"` with the full path that `which uvx`
    prints. Add `"env": {...}` next to `args` for environment variables.

=== "OpenCode"

    Merge this into `~/.config/opencode/opencode.json` (or `opencode.jsonc`), or a project's `opencode.json`:

    ```json
    {
      "$schema": "https://opencode.ai/config.json",
      "mcp": {
        "kitsune": {
          "type": "local",
          "command": ["uvx", "kitsune-browser-mcp@latest"],
          "enabled": true
        }
      }
    }
    ```

    Then restart OpenCode.

Using more than one client? They share one bridge, so register them all with the same source and
version ([compatibility](#extension-and-server-compatibility)).

### Developer build

Only needed if your agent should run its own JavaScript in pages (`browser_evaluate`, JavaScript
batch conditions), which Mozilla's policy keeps out of the signed build.

| | Signed (`kitsune-<version>.xpi`) | Developer (`kitsune-extension-dev-<version>.zip`) |
|---|---|---|
| Install | Once; updates itself | Temporary; load it again after every browser restart |
| Agent JavaScript | No | Yes |

1. Download and unzip `kitsune-extension-dev-<version>.zip` (or use `extension/` from a checkout).
2. Open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on…** and pick its
   `manifest.json`.

Both builds share an add-on ID; a loaded developer build replaces the signed one until restart.
`browser_status` reports which one is connected as `extension_build` (`signed` or `dev`).

### Pinning a version

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp==0.8.3 kitsune
```

For an unreleased commit, tag or branch, use the Git source and get the extension from the same
ref:

```bash
claude mcp add --scope user kitsune -- uvx --from "git+https://github.com/junyeong-nero/kitsune.git@<ref>#subdirectory=packages/kitsune-browser-mcp" kitsune
```

### Upgrading

**Server.** `uvx kitsune-browser-mcp@latest` checks for a new release each time the server starts.
Restart the client to update. If an older config keeps launching a cached version, rerun setup.
To keep a version pin, edit it manually instead. In a checkout: `git pull && uv sync`.

**Bridge.** The MCP server restarts a bridge whose `build` differs from its own, briefly
interrupting other clients; `KITSUNE_NO_BRIDGE_RESTART=1` opts out. Manage it manually with
`kitsune-bridge status` / `stop` (from the same source as your clients).

**Extension.** The signed build updates itself; install a new XPI to update right away. Reload
the developer build in `about:debugging`. A bridge restart does not reload extension code; reload
existing tabs if their content scripts are outdated.

#### Idle shutdown (opt-in)

Set `KITSUNE_BRIDGE_IDLE_TIMEOUT` (seconds) in the MCP server environment, or run
`kitsune-bridge start --idle-timeout SECONDS`, to stop the bridge after inactivity. Each HTTP call
counts as activity; the timer pauses while a request, extension command or `browser_keep_alive`
loop is running. The next tool call starts a fresh bridge. The first server to start the bridge
decides the timeout.

### Automatic extension updates

The signed build checks
`https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json` about once a day
(about:addons → gear → **Check for Updates** to check now). The extension can then be newer than a
cached server; `browser_status` shows `extension_version` differing from `bridge_version` until you
refresh the server. If you pin an old server, turn off automatic updates for Kitsune.

### Extension and server compatibility

- Install the server and extension from the same release or ref; they are tested only together.
- Use one install source for all clients. A checkout's `build` hashes its `extension/` files and
  differs from a `uvx` install of the same commit, so mixed clients keep replacing the bridge.
  To mix sources anyway, set `KITSUNE_NO_BRIDGE_RESTART=1` and manage the bridge yourself.

### Trusted clicks on macOS

For `browser_trusted_click` on macOS, use `kitsune-browser-mcp[macos]` or install `cliclick`
([Trusted clicks](troubleshooting.md#trusted-clicks)).

### Running from a checkout

See [Development](development.md).
