# Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KITSUNE_SCRIPTING` | `1` | `1`: the [6 default tools](tools.md#default-tools), including `browser_run_python` (local Python, **not a sandbox**). `0`: the [legacy tool list](tools.md#core-tools) without Python |
| `KITSUNE_HOME` | `~/.kitsune` | Data directory (0700) |
| `KITSUNE_HTTP_PORT` | `47651` | Bridge HTTP API |
| `KITSUNE_WS_PORT` | `47652` | Extension WebSocket. Change `WS_URL` in `extension/background/background.js` too |
| `KITSUNE_TOKEN` | Unset | Override the token in `~/.kitsune/token` |
| `KITSUNE_EXTENSION_DIR` | `extension/` in a checkout | Developer build the bridge checks for unreloaded edits ([Connection diagnosis](api-reference.md#connection-diagnosis)) |
| `KITSUNE_NO_BRIDGE_RESTART` | Unset | `1`: don't replace an outdated bridge automatically |
| `KITSUNE_BRIDGE_IDLE_TIMEOUT` | `0` (never) | Seconds without activity before the bridge exits ([Idle shutdown](installation.md#idle-shutdown-opt-in)) |
| `KITSUNE_OUTPUT_DIR` | Unset | Directory for relative `filename` arguments; else the client's first root, else the server's working directory |
| `KITSUNE_OWNER_ID` | The MCP client's process | Tab owner id (up to 64 of `[A-Za-z0-9._:-]`). Set it when the server runs through a launcher other than `uv`/`uvx`. Clients with the same value share tabs ([Tab owners](api-reference.md#tab-owners)) |
| `KITSUNE_USER_TABS` | `read-only` | `writable` lets this client write your tabs ([Protecting your tabs](your-tabs.md#protecting-your-tabs)) |
| `KITSUNE_ASK_BEFORE_ACT` | Unset | `1` starts the bridge with [ask-before-act](your-tabs.md#ask-before-act) locked on |
| `KITSUNE_HISTORY_ALLOW_DOMAINS` | Unset (all) | Comma-separated domains the `history` tools may return ([History and bookmarks](api-reference.md#history-and-bookmarks)) |
| `KITSUNE_HISTORY_DENY_DOMAINS` | Unset | Domains the `history` tools never return; wins over the allow list |
| `KITSUNE_HISTORY_MAX_DAYS` | `30` | How far back `history` tools may look. `browser_set_policy` can only tighten these three |
| `KITSUNE_POST_ACTION_SNAPSHOT` | Deltas | Bridge: `0` disables post-action snapshots, `full` returns full ones, `text` adds readable text ([details](api-reference.md#post-action-snapshots)) |

The removed `KITSUNE_CAPS`, `KITSUNE_PUBLISH` and `KITSUNE_MODE` make the server refuse to
start; delete them. Approvals are set in the extension's options instead.

`~/.kitsune/` holds:

| File | Content |
|---|---|
| `token` | Bearer token for the HTTP API (0600) |
| `bridge.log` | Daemon output |
| `sessions/` | Saved sessions, **including cookies** |
| `screenshots/` | Screenshots |
| `ask-before-act-locked` | Present while ask-before-act is locked on; delete it to unlock |
