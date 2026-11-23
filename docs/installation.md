# Installation

Kitsune has two parts: an **MCP server** that your agent starts for you, and a **browser
extension** you install once. Setup takes four steps.

!!! info "Before you start"

    - macOS, Linux or Windows (see [Windows](windows.md))
    - Firefox 142+, Zen 1.23+ or LibreWolf
    - An MCP client: Claude Code, Codex CLI, Pi, Claude Desktop, Cursor or OpenCode

## 1. Install uv

[uv](https://docs.astral.sh/uv/getting-started/installation/) downloads and runs the Kitsune
server. It provides `uvx` and Python 3.10+. Skip this step if `uvx --version` already works.

=== "Installer script"

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

=== "Homebrew"

    ```bash
    brew install uv
    ```

=== "Windows"

    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```

Open a new terminal, then download Kitsune once to check that it runs:

```bash
uvx --from kitsune-browser-mcp kitsune-bridge status
```

It prints `"status": "stopped"`. That is expected: your agent starts the bridge later.

!!! warning "The package is `kitsune-browser-mcp`"

    `kitsune-mcp` is an unrelated project.

## 2. Install the browser extension

[Open the latest release](https://github.com/junyeong-nero/kitsune/releases/latest){ .md-button .md-button--primary }

1. Download **`kitsune-<version>.xpi`** from the release's Assets list.
2. In Firefox, Zen or LibreWolf, type `about:addons` in the address bar and press Enter.
3. Click the **gear icon**, then **Install Add-on From File…**, and pick the `.xpi` file.
4. Click **Add** when the browser asks.
5. Pin Kitsune to the toolbar: click the **Extensions** (puzzle) button, then the gear next to
   Kitsune, then **Pin to Toolbar**. Tab handoff requests and tool approvals show up in its popup.

This is the signed build. It installs once and updates itself. Use the extension from the same
release as the server; `uvx` runs the latest release unless you [pin a version](#pinning-a-version).
If your agent needs to run its own JavaScript in pages, use the [developer build](#developer-build)
instead.

<a id="1-register-the-mcp-server"></a>

## 3. Add Kitsune to your agent

Pick your client. Every client runs the same command, `uvx --from kitsune-browser-mcp kitsune`;
only where you put it differs.

=== "Claude Code"

    Run once:

    ```bash
    claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
    ```

    Then start a new Claude Code session. To set an environment variable, add `-e` before the
    `--`, for example `-e KITSUNE_SCRIPTING=0`.

=== "Codex CLI"

    Run once:

    ```bash
    codex mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
    ```

    Then start a new Codex session. Or add it to `~/.codex/config.toml` yourself:

    ```toml
    [mcp_servers.kitsune]
    command = "uvx"
    args = ["--from", "kitsune-browser-mcp", "kitsune"]

    [mcp_servers.kitsune.env]  # optional
    KITSUNE_SCRIPTING = "0"
    ```

=== "Pi"

    Run once:

    ```bash
    pi mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
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
          "args": ["--from", "kitsune-browser-mcp", "kitsune"]
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
          "args": ["--from", "kitsune-browser-mcp", "kitsune"]
        }
      }
    }
    ```

    Restart Cursor. If it can't find `uvx`, replace `"uvx"` with the full path that `which uvx`
    prints. Add `"env": {...}` next to `args` for environment variables.

=== "OpenCode"

    Add this to `~/.config/opencode/opencode.json`, or to a project's `opencode.json`:

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

    Then restart OpenCode.

Using more than one client? They share one bridge, so register them all with the same source and
version ([compatibility](#extension-and-server-compatibility)).

## 4. Check that it works

1. Keep the browser open and start a new session of your agent.
2. Ask it: **"List my open browser tabs."** It should answer with the tabs you have open.
3. Click the Kitsune icon in the toolbar. The popup says **Connected**.

Next, [try a first task](try-it.md).

??? question "It doesn't work"

    Check the bridge from a terminal:

    ```bash
    uvx --from kitsune-browser-mcp kitsune-bridge status
    ```

    - `"status": "stopped"`: the agent hasn't started Kitsune. Check step 3 and restart the agent.
    - `"extension_connected": false`: the extension isn't connected. Check that it is installed and
      enabled in `about:addons`, and that the browser is open.
    - `"extension_connected": true`: the setup works. If a tool call still fails, its error's
      `hint` says what to fix.

    More in [Troubleshooting](troubleshooting.md#setup-and-installation).

## More setup options

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
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp==0.8.1 kitsune
```

For an unreleased commit, tag or branch, use the Git source and get the extension from the same
ref:

```bash
claude mcp add --scope user kitsune -- uvx --from "git+https://github.com/junyeong-nero/kitsune.git@<ref>#subdirectory=packages/kitsune-browser-mcp" kitsune
```

### Upgrading

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
