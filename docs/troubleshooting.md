# Troubleshooting

Start with `browser_status`: it reports a `diagnosis.code` and `diagnosis.hint`
([Connection diagnosis](api-reference.md#connection-diagnosis)).

## Setup and installation

| Symptom | Check |
|---|---|
| Server fails to start, or `uvx` not found | Use the full path from `which uvx` (GUI apps). Run `uvx --from kitsune-browser-mcp kitsune-bridge status` in a terminal to see the error |
| First start times out | Run that `status` command once to fill the uv cache, then reconnect |
| uv cannot fetch the package | Check that PyPI (or your mirror) is reachable; for `git+https://` sources, that `git` is on `PATH` |
| `Remove KITSUNE_CAPS from the MCP server environment` | Removed setting: delete it (and `KITSUNE_PUBLISH`, `KITSUNE_MODE`) from the client config |
| A call waits, or returns `needs_user` with `state.detail: "tool_approval"` | You require approval for that tool: answer in the toolbar popup, or uncheck it in [the options](your-tabs.md#tools-that-need-your-approval) |
| `tool_moved_to_python` | The [default tools](tools.md#default-tools) use `browser_run_python`; `KITSUNE_SCRIPTING=0` restores the legacy list |
| Upgrade not picked up | Restart the client; rerun setup to switch an older launcher to `@latest` ([Upgrading](installation.md#upgrading)) |
| The bridge restarts whenever another client starts | Clients use different sources or refs ([compatibility](installation.md#extension-and-server-compatibility)) |
| "needs the developer extension build" | The signed build cannot run agent JavaScript; load the [developer build](installation.md#developer-build) |
| Stop or restart the bridge | `kitsune-bridge stop` with the same source as your clients; the next call starts it again |

## Connection

| Code | Check |
|---|---|
| `extension_disconnected` | Extension enabled in the profile you opened? Developer build gone after a browser restart? It reconnects within 15 s |
| `extension_version_mismatch` | Install the matching extension or server, then reload tabs. `stale_developer_build`: reload it in about:debugging |
| `browser_window_unavailable` | Open a normal window, or allow Kitsune in private windows |
| `new_tab_stalled`, `navigation_stalled` | The browser cannot load pages; retrying won't help. Check `browser_status(diagnostics=true)` and ask the user to restart or update the browser |
| `host_permission_denied` | `not_granted`: allow site access in about:addons. `restricted_domain`: Mozilla sites are off-limits |
| `bridge_unavailable` | See `state.logExcerpt` and `~/.kitsune/bridge.log`, or run `kitsune-bridge start`. `not_responding`: `kitsune-bridge stop` and retry |
| `port_conflict` | Another program holds the port (`lsof -nP -iTCP:47651 -sTCP:LISTEN`), or `KITSUNE_WS_PORT` differs from the extension's `WS_URL` |
| `token_mismatch` | `KITSUNE_TOKEN` or `KITSUNE_HOME` differs between client and daemon |

## Tabs and approvals

| Symptom | Check |
|---|---|
| `user_tab_read_only` | Nothing ran. Open the URL in an agent tab, or request a [handoff](api-reference.md#requesting-and-returning-a-tab) |
| `needs_user`, `state.detail: "tab_handoff"` | The handoff was declined or timed out. Make a fresh request or use an agent tab |
| `needs_user`, `state.reason: "approval"` | Nothing ran. With elicitation you get a prompt; otherwise the agent repeats the call with `confirm_token` after you agree ([Approving held actions](your-tabs.md#approving-held-actions)) |
| `needs_user`, `state.decision: "declined"` / `"cancelled"` | Declined: don't ask again. Cancelled: `browser_resume(call, resume_token)` asks again |
| `needs_user`, `state.reason: "agent_request"` | The agent asked for help (`sign_in`, `human_check`, `trusted_input`, `other`); do it, then answer the prompt ([When the agent needs you](your-tabs.md#when-the-agent-needs-you)) |
| `browser_resume` returns `needs_user` at once | Without elicitation resume never waits for approval; approve in chat and repeat with `confirm_token` |
| `stale ref` | Take a fresh snapshot and use its refs |
| Bridge updated, behavior unchanged | Reload the extension, then the affected pages |

## Pages and input

| Symptom | Check |
|---|---|
| `Cannot access browser internal page` | `about:` pages are off-limits; use a normal tab |
| Click dispatched but nothing happens (`outcome_not_observed`) | The page wants trusted input. Try one [`browser_trusted_click`](#trusted-clicks) and verify with a read |
| Hover menu stays hidden | Synthetic hover cannot trigger CSS `:hover`. Use `browser_trusted_click` with `restore_pointer=False` on a harmless element, or ask the user |
| Press-and-hold or hover-intent control does nothing | Pass `hold_ms` to the click or `duration_ms`/`steps` to the hover. Pointer-capturing handles need `browser_trusted_drag` (macOS) |
| Hidden tab renders nothing, or `hidden_or_covered` with `state.hiddenTab` | Some pages don't paint hidden tabs ([Snapshot readiness](api-reference.md#snapshot-readiness)). Showing the tab switches the user's view, so ask first |
| `browser_not_at_point` (macOS) | Another window covers the point, usually full screen on another Space or Zen's full-screen top strip. Leave full screen or scroll the target down |
| `tab_not_shown`, or tabs listed as `about:blank` (Zen) | Finish Zen's first-run welcome screen |
| `value-attribute selectors are disabled` | Target the sensitive field by ref or label instead |
| "Continue with Google" opens nothing | See [Sign-in popups](#sign-in-popups-oauthsso) |

## Sign-in popups (OAuth/SSO)

Firefox's popup blocker opens `window.open` popups only from real user input. Kitsune's clicks
are synthetic, so OAuth/SSO popups are blocked or the site shows an error. When detected, the
click result has a `popup_blocked` warning (`state.reason: "script_popup"`) and a `needs_user`
hint with `reason: "sign_in"`. Two ways through:

1. **Ask the user to click (recommended).** The agent calls `browser_ask_user`, then finds the
   new popup with `browser_tabs`.
2. **`browser_trusted_click`.** A real OS click opens the popup, but moves your pointer and may
   take focus. Find the popup with `browser_tabs` afterwards.

## Trusted clicks

`browser_trusted_click` needs a visible browser window.

- **macOS**: install a backend (`uvx --from 'kitsune-browser-mcp[macos]' kitsune`, or
  `brew install cliclick`), grant Accessibility permission to the app running the bridge, then
  run `kitsune-bridge stop` so the next start picks it up. Without it, the call fails with
  `permission_required`, its `state.app` names the app to allow, and macOS shows its prompt once. The bridge uses the Python and `PATH`
  of whichever client started it, so add the extra to every client.
- **Windows**: works without extra setup. `focus=True` finds the browser by window title; set
  `KITSUNE_BROWSER_WINDOW_TITLE` if yours differs.
- **Linux**: `xdotool` in an X11 session (not Wayland). HiDPI scaling is not compensated.

A target covered by another element returns `target_not_at_point` without clicking.
