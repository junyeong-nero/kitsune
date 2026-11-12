# Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_SCRIPTING` | `1` | `1` (or unset) selects the [minimal tool-set](minimal-tools.md) with `browser_run_python`: trusted local Python execution with host file/network access, **not a sandbox**. `0` selects the legacy direct tool list without Python, with the [gateway tools](tools.md#finding-tools-without-a-restart) for the rest; other values fail at startup. See [Python scripting](python-api.md#python-scripting-through-mcp) |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | — | Override the token in `~/.kitsune/token` |
| `KITSUNE_EXTENSION_DIR` | `extension/` in a checkout | Unpacked extension the bridge compares a developer build with, to report one that was edited but not reloaded, and whose `WS_URL` port it expects before the extension connects ([Connection diagnosis](api-reference.md#connection-diagnosis)) |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | Set to `1` in the MCP server environment to skip automatic replacement of an outdated bridge |
| `KITSUNE_BRIDGE_IDLE_TIMEOUT` | `0` (never) | Seconds without client activity before the bridge exits; see [Idle shutdown](installation.md#idle-shutdown-opt-in) |
| `KITSUNE_OUTPUT_DIR` | Unset | Directory, in the MCP server environment, for relative `filename` arguments of the `core` tools; unset, they resolve against the client's first root, else the server's working directory (see [Core tools](tools.md#core-tools)) |
| `KITSUNE_OWNER_ID` | The MCP client's process | Tab owner id of the MCP client, in the MCP server environment: the bridge records the tabs it opens as this client's and offers them after a server restart. Up to 64 letters, digits, `.`, `_`, `:` or `-`. Set it when the server runs through a launcher other than `uv` or `uvx`, such as a shell script or a Python wrapper. Give each MCP client its own value: clients with the same value share their tabs. See [Tab owners](api-reference.md#tab-owners) |
| `KITSUNE_USER_TABS` | `read-only` | Set to `writable` to allow this client to write user tabs; see [Protecting your tabs](your-tabs.md#protecting-your-tabs) for values and invalid-value errors |
| `KITSUNE_ASK_BEFORE_ACT` | Unset | Set to `1` in the MCP server environment to start the bridge with [ask-before-act](your-tabs.md#ask-before-act) locked on; the lock then persists in `ask-before-act-locked` under `KITSUNE_HOME` |
| `KITSUNE_HISTORY_ALLOW_DOMAINS` | Unset (every domain) | Bridge: comma-separated domains (with subdomains) the `history` tools may return; see [History and bookmarks](api-reference.md#history-and-bookmarks) |
| `KITSUNE_HISTORY_DENY_DOMAINS` | Unset (none) | Bridge: comma-separated domains (with subdomains) the `history` tools never return; wins over the allow list |
| `KITSUNE_HISTORY_MAX_DAYS` | `30` | Bridge: how many days back the `history` tools may look. `browser_set_policy` can only tighten these three |
| `KITSUNE_POST_ACTION_SNAPSHOT` | Enabled (deltas) | Set to `0` in the bridge daemon environment and restart it to disable automatic post-action snapshots, `full` to return full snapshots instead of [deltas](api-reference.md#post-action-snapshots), or `text` to add readable text lines to them |

`KITSUNE_CAPS`, `KITSUNE_PUBLISH` and `KITSUNE_MODE` were removed (#525): every tool is
available, and approvals are set in the extension's options. The MCP server refuses to start
while one is set, so a limit it expected is not silently dropped; remove it from the MCP
server environment. [Your tabs and approvals](your-tabs.md) lists what limits the agent now.

`~/.kitsune/` holds:

| File | Content |
|---|---|
| `token` | Bearer token for the HTTP API (0600, created on first start) |
| `bridge.log` | Daemon output |
| `sessions/` | Saved sessions, **including cookies** |
| `screenshots/` | Screenshots |
| `ask-before-act-locked` | Present while [ask-before-act](your-tabs.md#ask-before-act) is locked on (0600); delete it to unlock |
