# Troubleshooting

## Setup and installation

| Symptom | Check |
|---|---|
| Client reports the server failed to start or `uvx` was not found | Use the full path from `which uvx` (GUI apps). Run `uvx --from kitsune-browser-mcp kitsune-bridge status` in a terminal to see uv's error |
| First start times out in the client | Run `uvx --from kitsune-browser-mcp kitsune-bridge status` once to fill the uv cache, then reconnect |
| uv cannot find `kitsune-browser-mcp` | Check that PyPI is reachable (or your configured index mirrors it) |
| uv cannot fetch a `git+https://` source | Check that `git` is installed and on the client's `PATH`, and that GitHub is reachable |
| An upgrade is not picked up | Restart the MCP client; then refresh with `uvx --refresh …` (see [Upgrading](installation.md#upgrading)) |
| The bridge restarts every time a different client starts | Clients use different sources or refs (see [compatibility](installation.md#extension-and-server-compatibility)) |
| `extension_connected: false` | Extension installed and enabled in `about:addons`? With the developer build, the browser restarted, so the temporary add-on is gone? |
| A tool says it needs the developer extension build | The signed build cannot run agent-supplied JavaScript; load the [developer build](installation.md#developer-build) |
| Stop or inspect the bridge | `uvx --from kitsune-browser-mcp kitsune-bridge status` (or `stop`) with the same source as your clients |

## Connection, tabs and pages

| Symptom | Check |
|---|---|
| Any connection problem | Call `browser_status` and follow `diagnosis.hint`; each `diagnosis.code` is listed in [Connection diagnosis](api-reference.md#connection-diagnosis). It never shows the token |
| `extension_disconnected` (`extension_connected: false`) | Extension installed and enabled in the browser profile you opened? Developer build: browser restarted (temporary add-on gone)? It reconnects within 15 s of a bridge start; the first tool call after the MCP server starts a bridge waits up to 16 s for it |
| `extension_version_mismatch` | The extension and server versions differ; install the matching extension or update the server, then reload open tabs. `state.reason: "stale_developer_build"`: the developer build's files changed since the browser loaded them; reload it in about:debugging |
| `browser_window_unavailable` | The browser runs without a normal window Kitsune can use; open one, or allow Kitsune in private windows (about:addons → Kitsune → Run in Private Windows) |
| `new_tab_stalled` (new tab stays `about:blank` / `complete` after its load timeout), `navigation_stalled` (same, after `browser_navigate`) | The tab was created or navigated, but no loaded destination could be confirmed. Retrying will not fix it: the browser needs an update, a restart or another fix first. Do not keep creating tabs or navigating; inspect or close the returned tab. Run `browser_status(diagnostics=true)` for [browser build diagnostics](api-reference.md#browser-build-diagnostics). A `mismatch` is evidence that a browser restart is pending, not proof of this failure's cause; `unknown` is not a clean bill of health. Tell the user and ask them to save work and restart or update the browser, or fix it another way; once they say it is fixed, check status and list tabs again (IDs may change). In #366, old `Contents.bak` files were still mapped after an app update; restarting recovered loading without a Kitsune change. Reloading the extension or bridge does not restart the browser |
| `host_permission_denied` | `not_granted`: allow site access in about:addons → Kitsune → Permissions. `restricted_domain`: Firefox keeps extensions off Mozilla sites such as addons.mozilla.org; use another tab |
| "needs the Kitsune developer extension build" | The signed build cannot run agent-supplied JavaScript; load the developer build |
| `bridge_unavailable` (`Kitsune bridge did not start`, `exited at startup`) | `state.logExcerpt` shows the bridge's last log line; see `~/.kitsune/bridge.log`, or run `kitsune-bridge start` in a terminal to see the error. `state.reason: "not_responding"`: the bridge hangs; run `kitsune-bridge stop` and retry |
| `port_conflict` | `state.port` is held by another program (`lsof -nP -iTCP:47651 -sTCP:LISTEN`), or `KITSUNE_WS_PORT` differs from the extension's `WS_URL` |
| `token_mismatch` (`401 Missing or invalid token`) | `KITSUNE_TOKEN` or `KITSUNE_HOME` set in one client but not in the daemon (or the other way round) |
| `user_tab_read_only` | Nothing ran and no approval is offered. Open the URL in a tab of the agent's own before writing, or explicitly request a [handoff](api-reference.md#requesting-and-returning-a-tab). Reads work on any tab; those that scroll it (element or full-page screenshots, `find load_more`) only while it is not shown |
| `needs_user` with `state.detail: "tab_handoff"` | The handoff was declined, invalidated or needs client approval after the browser prompt timed out. Follow `state.message` and the hint; repeat the exact request with its token only after approval, or open an agent tab. Handoff requests do not have resume checkpoints |
| `needs_user` with `state.reason: "approval"` (`state.detail`: `confirmation_required`, or legacy `user_tab`) | Nothing ran: the call needs your approval. On a client with elicitation (Claude Code 2.1.76 and later, Codex) you get a prompt; Approve runs that one call. No prompt appeared and the result has `state.approval: "token"`: the client lacks elicitation, so the agent should tell you `state.message` and repeat the call with `confirm_token`, the fallback for such clients, only after you agree. Or have it work in a tab of its own. See [Approving held actions](your-tabs.md#approving-held-actions) |
| `needs_user` with `state.decision: "declined"` or `"cancelled"` | `declined`: you declined the approval prompt, or answered Can't to `browser_ask_user`; nothing ran, and the agent should not ask again for the same thing. `cancelled`: the prompt was dismissed or closed unanswered (an approval prompt closes 5 s before its approval would expire). If you still want it done, the agent calls `browser_resume(call, resume_token)` when supplied, which asks you again. Handoff requests have no resume token: make a fresh request |
| `needs_user` with `state.reason: "agent_request"` | The agent asked you with `browser_ask_user`; `state.detail` says for what (`sign_in`, `human_check`, `trusted_input`, `other`) and `state.message` what to do. Do it, then answer Done in the client's prompt, or tell the agent in chat on a client without elicitation. For a sign-in or CAPTCHA the tab shows, the agent can call `browser_resume`, which waits until it is gone. See [When the agent needs you](your-tabs.md#when-the-agent-needs-you) |
| A result has a `needs_user` hint (`reason`: `sign_in`, `human_check`, `trusted_input`) | Kitsune saw something only you can do; nothing was held, and the call's own result stands. If the agent cannot go on, it asks you (`browser_ask_user`). `sign_in`: sign in yourself (password fields stay redacted from the agent); `human_check`: complete the CAPTCHA; `trusted_input`: click the control yourself, or let the agent try [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0). See [needs_user hints](api-reference.md#needs_user-hints) |
| `browser_resume` returns `needs_user` again at once for an approval | Without elicitation a resume checks the hold once and never waits for you; it continues only if the hold no longer applies. Approve in chat, and the agent repeats the call with `confirm_token`. With `wait=0` it asks nothing; resume with a wait to be asked |
| `Cannot access browser internal page` | `about:` pages are off-limits to extensions; switch to a normal tab |
| A click returns `dispatched: true` but the page does not react (`outcome_not_observed`, target still visible) | Some pages accept only trusted input and ignore Kitsune's synthetic events. Check the page with `browser_snapshot` first, then try one [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0) on the same target and verify it with a read. It needs the `browse` capability, moves your real cursor for the click (then back, unless `restore_pointer=False` for a hover menu that closes when the pointer leaves) and, on macOS, needs pyobjc Quartz or `cliclick` plus Accessibility permission; on Linux, needs `xdotool` in an X11 session (not Wayland), and HiDPI X11 scaling is not compensated, so the click may land off by the scale factor. Page zoom is compensated, and a target covered by another element (an overlay or banner) returns `target_not_at_point` without clicking. Kitsune never switches to it on its own |
| A hover returns `ok: true` but the menu or row controls it should reveal stay hidden | `browser_hover` sends pointer and mouse events, including `pointerenter`/`mouseenter` to each entered ancestor, but it cannot trigger CSS `:hover`, which follows only the real pointer. Use [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0) with `restore_pointer=False` on an element where a click is harmless, which leaves your real pointer there, or ask the user to point at it. See [POST /api/hover](api-reference.md#post-apihover) |
| A press-and-hold button, a tile that reveals controls after a moment of hovering, or a custom slider or drag handle does not react | Long-press controls need `hold_ms` on `browser_click` (or `browser_trusted_click`), and hover-intent controls need `duration_ms` (and `steps`) on `browser_hover`; the core tools accept both without listing them. A handle that captures the pointer (`setPointerCapture`) rejects synthetic pointers: on macOS, [`browser_trusted_drag`](api-reference.md#browser_trusted_dragselector-to_selector-tab_idnone-steps10-firstfalse) (`browse` capability) drags it with your real pointer |
| A background tab's snapshot is empty, or a menu item a click just opened returns `hidden_or_covered` with `state.hiddenTab` or `state.inPopup` | Results from a hidden tab carry `visibilityState: "hidden"`; tabs the agent opens are in the background by default, so their results usually carry it. Some pages render lazily, or do not paint menus and popups, while their tab is hidden (#259, #260). Events dispatched to an unpainted item still work, so act on it if it belongs to the control you meant and check the effect. If content never appears, Kitsune's `browser_tabs` select with `tab_id` shows the tab and switches the user's own view, so ask the user first; it is refused with `user_active` while they use the tab they would leave. The core `browser_tabs` select only makes a tab current. See [Snapshot readiness](api-reference.md#snapshot-readiness) |
| `browser_trusted_click` returns `browser_not_at_point` (macOS) | Another app's window, not the browser's, is at the click point, typically because the browser is in full screen on another Space; nothing was clicked. Switch to the browser's Space or leave full screen, then retry. See [`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0) |
| `browser_trusted_click` near the top of a page returns `browser_not_at_point` while the browser is in full screen (macOS) | Verified on macOS 26.5.1 with Zen 1.24b (#336): the hidden menu bar is not in the way. Zen's own full-screen strip, a second Zen window over the top 68 points of the screen, refuses with `state.reason: "other_browser_window"` (#431). While the menu bar shows (the pointer rests at the top edge), the menu bar refuses with `"other_window"` (`state.app: "Window Server"`) and the strip moves down below it, to 98 points. Nothing was clicked. Scroll the target further down, or leave full screen, then retry. To check what macOS reports there, run `KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen KITSUNE_E2E_TRUSTED_INPUT=1 uv run --extra macos python -m unittest discover -s tests -p test_real_browser_fullscreen_menubar.py -v` while nobody uses the machine; it puts its own browser window in full screen, makes no click and prints the windows at a few points near the top. `KITSUNE_E2E_REVEAL_MENU_BAR=1` also moves the pointer (no click) to the top edge to show the menu bar, then back |
| A trusted click, type, key press or drag returns `tab_not_shown`, or `browser_tabs` lists loaded tabs as `about:blank` (Zen) | The browser window shows no page: on a brand-new Zen profile, its first-run welcome screen hides the tab area until it is finished (#360). Nothing was clicked. Finish or close the welcome screen, then retry; tabs loaded while it was open keep reporting `about:blank` until they navigate again |
| "Continue with Google" or another sign-in button opens no popup, or the site says "Something went wrong" | With Firefox's default popup blocker, synthetic clicks cannot open OAuth/SSO popups (the click result may have a `popup_blocked` warning with `state.reason: "script_popup"`); ask the user to click, or use a trusted click. See [Sign-in popups (OAuth/SSO)](#sign-in-popups-oauthsso) |
| `stale ref, call browser_snapshot again` | Take a fresh snapshot of the same `tab_id` and use its new refs |
| Bridge updated but browser behavior is unchanged | Reload the extension, then reload affected pages so their content scripts update too |
| Restart the daemon | Run `kitsune-bridge stop` with the same source as your clients (`uvx --from git+…` or `uv run` in a checkout); the next tool call starts it again. This briefly affects all clients sharing it |
| `value-attribute selectors are disabled …` | A sensitive field on the page has a `value` attribute; target it by ref, label, or a selector that does not test `value` |

## Sign-in popups (OAuth/SSO)

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
successful or end in `outcome_not_observed`. When a refused popup is an OAuth, OpenID
Connect or SAML sign-in request, the result also has a `needs_user` hint with `reason`
`sign_in` (see [When the agent needs you](your-tabs.md#when-the-agent-needs-you)).

There are two ways through:

1. **Ask the user to click (recommended).** The agent asks the user to click the
   button (`browser_ask_user`), waits for them, then lists tabs with `browser_tabs` to find the popup or
   tab that opened and continues there by `tab_id`. The user may also have to finish
   the provider's sign-in (account chooser, password, two-factor). This is the
   default to prefer because the browser is shared (#139): a trusted click moves your
   real pointer, may show the target tab and can take focus.
2. **[`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0).**
   A real OS click has user activation, so the popup opens. It needs the `browse`
   capability and a visible browser window. On macOS a browser in full screen on another
   Space is not visible from the agent's Space, and `focus=True` may not switch to it:
   the call then returns `browser_not_at_point` without clicking, so ask the user to
   switch to the browser's Space (or leave full screen) and retry. Its result does not list
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
