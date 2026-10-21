# Kitsune API Reference

Base URL: `http://127.0.0.1:47651`

## MCP tool surface

`KITSUNE_CAPS=core` is the default (30 tools); `KITSUNE_CAPS=all` registers
95 tools. The groups are `core`, `tabs`, `network`, `storage`, `parallel`,
`devtools`, `windows`, and `session`. A comma list selects those groups only.
See [Tools and capabilities](guide.md#tools) for the complete tool list per group.

`KITSUNE_TOOL_PRESET=lean` (opt-in; unset by default) narrows `core` to 14 tools:
`browser_status`, `browser_tabs`, `browser_navigate`, `browser_page_info`,
`browser_page_text`, `browser_snapshot`, `browser_forms`, `browser_dom`, `browser_find`,
`browser_screenshot`, `browser_click`, `browser_type`, `browser_select_option`,
`browser_form_fill`. Their definitions are unchanged, other `KITSUNE_CAPS` groups
register in full, and an unknown preset fails at startup. It exists for the
browser-mcp-bench A/B (#31); no token, cost or success effect is claimed. The
[Lean tool preset](guide.md#lean-tool-preset) section lists the 16 dropped `core`
tools and which tasks need the default surface.
The bridge endpoints below remain available unchanged; MCP tool names and
parameters are separate from HTTP endpoint names and JSON command actions.

### Element targets

For single-element tools, prefer `ref` from `browser_snapshot` over a CSS
`selector`. When both are supplied, `ref` takes precedence, as for `browser_click`.
`element` is an audit-only human description, never a selector or fallback target.
A stale/disconnected ref returns a `stale_ref` error (see [Action errors](#action-errors));
take another snapshot to refresh it. See [Ref lifetime](#ref-lifetime) for which
calls replace refs.
Existing positional selector arguments remain supported.

These tools also accept refs (all new parameters are optional):

| MCP tool | Parameters |
|---|---|
| `browser_keypress` | `key, selector=None, modifiers=None, text=None, tab_id=None, ref=None, element=None, wait=False, timeout=10000` |
| `browser_submit_form` | `selector=None, tab_id=None, ref=None, element=None` |
| `browser_scroll` | `direction="down", amount=500, tab_id=None, selector=None, ref=None, element=None` |
| `browser_blur` | `selector=None, tab_id=None, ref=None, element=None` |
| `browser_html`, `browser_bounds` | `selector="", tab_id=None, ref=None, element=None` |
| `browser_computed_style` | `selector="", properties=None, tab_id=None, ref=None, element=None` |
| `browser_click_and_wait_navigation` | `selector="", timeout=15000, tab_id=None, ref=None, element=None` |
| `browser_markdown` | `selector=None, tab_id=None, ref=None, element=None` |

Without selector/ref, keypress targets the active element (or body), blur targets
the active element, submit-form submits the first form, and Markdown reads the
whole page. Submit-form can target a form or an element inside it. Scroll accepts
an explicit container selector/ref; otherwise it scrolls the window and falls
back to a scrollable container at viewport center. HTML, bounds, computed-style,
and click-and-wait-navigation require a selector or ref for a successful action.
See `browser_wait_for` below for its element condition.

### Ref lifetime

A ref names one element in one document of one tab, for one ref generation.
Use refs only from the latest observation of that tab.

| Call | Effect on refs in that tab |
|---|---|
| `browser_snapshot` (also scoped), `browser_dom` | Starts a new generation: earlier refs fail as `stale_ref`/`snapshot_replaced`. A `ref` argument to `browser_snapshot` is resolved first |
| Successful `browser_click`, `browser_type` (`type`/`fill`), `browser_select_option`, `browser_check`, `browser_keypress`, `browser_submit_form`, `browser_navigate` | Their post-action `snapshot` starts a new generation, like `browser_snapshot`. An `expect` failure (`outcome_not_observed`) is an error, but the action ran and its result still carries that `snapshot` and `target_ref` |
| Navigation, reload, or back/forward to another document | Earlier refs fail as `stale_ref`/`other_document` |
| `browser_find`, `browser_forms`, `browser_wait_for(selector/ref)` | Add refs to the current generation; earlier refs stay valid |
| Other failed actions, other reads, `browser_hover`, `browser_scroll`, `browser_type(mode="editable")` and other actions without a post-action snapshot | No change, unless the page itself removes the element (`element_removed`) |
| A failed `browser_snapshot` (bad `max_depth`, missing `selector`, stale `ref`) | No change: it fails before starting a generation |

Snapshots in one tab do not affect refs in another tab. Pass `tab_id` when more
than one tab is in use: a ref sent to the wrong tab fails as `other_document` and
never acts on another element.

After an action, use the refs in its `snapshot`. `target_ref` is the new ref of
the acted-on element (absent for `browser_navigate`, or when that element is gone),
so a follow-up such as `browser_keypress(key="Enter", ref=target_ref)` needs no
extra snapshot. Only emitted lines carry usable refs: when `snapshot_truncated`
is set and the element you need is missing, or when `snapshot_error` replaces the
snapshot, call `browser_snapshot` before using any ref. With the post-action
snapshot disabled (`KITSUNE_POST_ACTION_SNAPSHOT=0`, or `includeSnapshot: false`
in a batch), actions leave refs unchanged.

Recovering without repeating a consequential action:

- `stale_ref` and `not_found` fail before anything is dispatched to the page.
  Take a new `browser_snapshot` of the same `tab_id`, find the element again,
  and retry with its new ref. Do not fall back to another element by its
  `element` description.
- A `stale_ref` with `state.reason: "snapshot_replaced"` names the call whose
  snapshot holds the current refs in `state.latestObservation` (`browser_snapshot`,
  `browser_dom`, or the action tool whose post-action snapshot replaced them).
  When the ref's element is still the same DOM node with the same role, name and
  snapshot ancestors in that snapshot, `state.currentRef` is its current ref.
  Kitsune never retargets the command itself: check that snapshot (an earlier
  action may already have done what you meant) and use `state.currentRef` only if
  the action is still needed. Refs from the three generations before the current
  one are compared; refs from `browser_find`, `browser_forms` or `browser_wait_for`
  carry no role and name, so they get no `currentRef`.
- To get fresh refs after a successful write, use its `snapshot`/`target_ref` or
  call `browser_snapshot`. Never repeat the write.
- After a failure with `retryable: false` (a write whose outcome is unknown),
  observe first (`browser_snapshot`, `browser_page_info`, `browser_wait_for`) and
  act again only if the page shows the effect did not happen.
- In a batch, earlier commands may already have run. Resume from the failed
  command instead of re-running the whole batch.

Refs are not stable across snapshots: an unchanged element gets a new ref in
each generation.

### Action errors

Failures keep the human-readable `error` string (its text is unchanged) and, when
classified, add machine-readable fields. Errors that are not yet classified have
only `error`. MCP tools return these failures with `isError: true` (see
[MCP error results](#mcp-error-results)).

```json
{ "error": "Element not found", "code": "navigating", "retryable": true,
  "hint": "The tab is loading a new document; wait for it with browser_wait_for, then take a new browser_snapshot.",
  "state": { "cause": "not_found", "tabId": 7, "url": "https://example.com/next", "status": "loading" } }
```

- `code`: stable identifier; branch on it, not on `error` text.
- `retryable`: `true` means repeating the same command cannot duplicate a side
  effect, because it never reached the page or is a read. It does not promise that
  an identical retry succeeds; follow `hint` first (for example, take a new snapshot).
  `false` means a write may already have taken effect (a submit, message, payment
  or deletion can be behind any click or keypress); inspect the page before acting again.
  The exception is `error_page`: nothing reached the page, and repeating cannot succeed.
- `hint`: a short recovery step. `state`: the current state that explains the
  failure, where available (for page targets: `tabId`, `url`, `status`).

| `code` | Meaning | `retryable` |
|---|---|---|
| `not_found` | No element matches the selector (also `No target`, `from/to element not found`); after an [actionability wait](#actionability-wait), `state` has `timeoutMs` and `elapsed` | true |
| `not_fillable` | `browser_type` (`type`/`fill` modes) found no field to write. The target is not a field (`fill`: input, textarea or select; `type`: input or textarea, or a `contenteditable` element without `clear`) and has none in its own open or closed shadow root (`fill` also looks in its light DOM). A field in a shadow root nested inside another shadow root is not reached. Nothing was written. `state.tag` is the target's tag; for `fill`, `state.pageContext` is the page-context fallback's result (`not_found`, `no_field`, `error`, or `not_run` when its page script did not run) | true |
| `not_actionable` | An [actionability wait](#actionability-wait) timed out on a target that exists but never rendered (`state.reason: "not_rendered"`, `timeoutMs`, `elapsed`); nothing was dispatched | true |
| `stale_ref` | The ref is no longer valid; `state.reason` is `element_removed`, `not_in_snapshot`, `snapshot_replaced` (a newer snapshot of this document; `state.latestObservation` and, for the same element, `state.currentRef`, see [Ref lifetime](#ref-lifetime)), or `other_document` (the tab navigated, or the ref came from another tab) | true |
| `navigating` | The tab is loading: a `not_found`/`stale_ref` while loading (`state.cause`), or the page unloaded while handling the command. Also a page that refused the content script within 5 s of a wake or reload without an error status, or whose 2xx response arrived only while the command waited for it: it may be an old document about to be replaced | true before dispatch; false for writes after |
| `tab_closed` | The target tab no longer exists | true |
| `tab_unloaded` | The tab is discarded and its page cannot be reached; use `browser_wake_tab` | true |
| `restricted_page` | Browser-internal page (`about:`, `moz-extension:`, …) | true |
| `content_unavailable` | The page's content script could not be injected or did not answer | true before dispatch; false for writes after |
| `error_page` | A loaded `http(s)` page refused the content script: the browser shows its own document, such as an error response with an empty body. `state` adds `title` and, when the extension saw the response, `httpStatus`. Nothing was dispatched, but reads fail the same way until the tab navigates; for a 4xx/5xx status, `browser_page_text` instead returns an empty result (`text: ""`, `empty: true`, `httpStatus`, `tabId`, `url`, `title`) | false |
| `extension_disconnected` | The extension is not connected, reconnected before sending, or disconnected while waiting (`state.connected`) | true if not sent; false for writes after |
| `timeout` | The bridge sent the command but no reply arrived in time (`state.timeoutSeconds`); from the MCP server, the bridge did not answer | false for writes (MCP: POST) |
| `bridge_unavailable` | The MCP server cannot reach or start the bridge (`state.exitCode`, `state.logExcerpt` when it exited at startup; `state.reason`, `state.pid` when it runs but does not answer) | true |
| `token_mismatch` | The bridge rejected the MCP server's token (`state.port`, `state.tokenSource`) | true |
| `port_conflict` | A port is held by another program, answers but not as a Kitsune bridge, or differs from the extension's (`state.port`, `state.reason`) | true |
| `extension_version_mismatch` | The extension does not know the command, and its version differs from the bridge's (`state.extensionVersion`, `state.bridgeVersion`) or it is a developer build edited but not reloaded (`state.reason: "stale_developer_build"`) | true |
| `browser_window_unavailable` | No active tab: the extension sees no normal window it can use | true |
| `host_permission_denied` | Firefox does not let Kitsune script the tab's site (`state.reason`: `not_granted`, `restricted_domain`) | true |
| `confirmation_required` | [Ask-before-act](#ask-before-act) held a sensitive action; nothing ran (`state.confirmToken`, `state.reason`) | true |
| `outcome_not_observed` | The action was dispatched, but the outcome given in `expect` did not happen in time (see [Action outcomes](#action-outcomes)) | false for writes |
| `tab_required` | A `parallel` step that acts on a tab has no `tabId` (`state.sequence`, `state.index`); nothing was dispatched (see [Container progress](#container-progress)) | true |
| `tab_owned` | Two `parallel` sequences write the same tab (`state.tabId`, `state.owner`); nothing was dispatched | true |
| `invalid_tab_id` | A batch, workflow or parallel step names a `tabId` that is not a tab id (`0`, `""`, an unresolved `${$N.tabId}`); it was not sent to the active tab instead | true |
| `container_timeout` | A batch or workflow reached its deadline; steps in progress may have taken effect and later steps did not start | false |

The connection codes (`bridge_unavailable`, `token_mismatch`, `port_conflict`,
`extension_disconnected` before sending, `extension_version_mismatch`,
`browser_window_unavailable`, `host_permission_denied`) are explained under
[Connection diagnosis](#connection-diagnosis).

Writes are the bridge's read-only-mode write actions plus file-upload commits,
focus, and blur. For a read, the bridge reports `retryable: true`, except for
`error_page`, where repeating cannot succeed.

Identity is checked before mutation. A missing or browser-internal target tab fails
before any message reaches a page. Refs encode their document: a ref used after
navigation, or on a different tab after a focus change, fails as
`stale_ref`/`other_document` instead of acting on another element. Without
`tab_id`, a command and its post-action snapshot target the tab that was active
when the command started. CSS selectors carry no identity, so pass `tab_id`
when focus can change.

The batch/`browser_retry` `retry` action stops after the first attempt that
returns `retryable: false`, with `reason: "not retryable"`, even if `onErrors`
matches. It also stops on `confirmation_required`, with
`reason: "confirmation required"`. Other errors are retried as before.

Pointer actions (`browser_click` by selector/ref, `browser_double_click`,
`browser_hover`) still dispatch to a target that is not rendered (`hidden`,
`display: none`, `visibility: hidden`, or inside a `display: none` ancestor), but
the success result adds a warning. With `stopOnWarning`, a batch stops there.

```json
{ "ok": true, "tag": "input", "warning": { "code": "hidden_or_covered",
  "state": { "reason": "not_rendered" }, "hint": "... prefer its label or visible control ..." } }
```

Zero-size and transparent targets count as rendered. Covering overlays are not detected.

### Actionability wait

Action tools act immediately by default. With `wait=True` (`"wait": true` on the
bridge endpoint or batch command), the page first waits up to `timeout` ms
(default 10000) for the target, then dispatches exactly as in immediate mode.
Nothing is dispatched while waiting or on timeout.

| Action | MCP tools | Waits until the target |
|---|---|---|
| `click`, `doubleClick`, `hover`, `drag` (both ends) | `browser_click`, `browser_double_click`, `browser_hover`, `browser_drag` | exists, is connected, and is rendered |
| `type`, `fill`, `setEditableContent`, `selectOption`, `checkBox`, `keypress` | `browser_type`, `browser_select_option`, `browser_check`, `browser_keypress` | exists and is connected |

Rendered means the same as for the `hidden_or_covered` warning above. Value
actions don't need a rendered target, so a hidden native checkbox or select
behind a custom widget still works in wait mode. A target that never resolves
returns `not_found`; a pointer target that exists but never renders returns
`not_actionable`. Both carry the last observed state and are retryable, because
nothing reached the page. `timeout` is not used here; that code means a command
was sent and its outcome is unknown. A ref that goes stale while waiting fails
at once with `stale_ref`. Coordinate clicks and keypresses without a
selector/ref have no target and run immediately. Other actions ignore `wait`.
The bridge and MCP transport timeouts grow by the wait bound. Batch commands run in
sequence, so a batch's timeout covers the sum of its waits (actionability waits,
element/URL/title/result waits, `newTab` waits and sleeps); for `parallel`, the slowest sequence counts.
Control flow counts its bodies: `retry` counts every attempt plus its backoff sleeps,
`if` its longer branch, `while` `maxIterations` passes of its body, and `try` its `do`
and `catch`. `browser_retry` and `/api/retry` grow the same way; without waits they
keep their 60 s and 35 s bounds.

These are reliability checks borrowed from Playwright. They don't make the
events trusted input: dispatch is still synthetic DOM events. A rendered target
can still be covered by an overlay, which isn't detected.

`hidden_or_covered` stays a success warning in immediate mode, not a failure.
Synthetic events reach a hidden element, and pages rely on that: a label-driven
custom widget keeps its native checkbox or select `display: none`, and refusing
by CSS heuristics would break actions that work today. Callers that want refusal
pass `wait=True`, which fails a pointer target that never renders with
`not_actionable` before dispatching anything. Overlay detection with an
`elementFromPoint` hit test is deliberately not done, as a failure or as a wait
condition. Cookie banners and sticky headers often cover a target's center after
it scrolls into view while the synthetic event still reaches the target, so
refusing there would fail working actions. Hit tests also stop at closed shadow
roots and cross-origin frames, and a single sample is unreliable under
`pointer-events: none`, transforms, and animations. A covered target could later
be reported as an additive warning, after benchmark validation. If the page unloads
during the wait, the command fails like any other unload (`navigating` or
`content_unavailable`, `retryable: false` for writes), because the background
can't tell whether the action ran.

### MCP error results

An MCP tool call returns `isError: true` when the tool's own top-level result
reports a failure: a non-empty `error`, or `ok: false`. The content is the same
JSON text as before, so `error`, `code`, `retryable`, `hint`, `state`, and any other
fields (`timedOut`, `tabId`, …) are kept. This covers bridge and extension errors
(`stale_ref`, `not_found`, `extension_disconnected`, `bridge_unavailable`, …),
argument errors reported by the MCP server, `ok: false` results such as
`browser_navigate` or `browser_tabs(action="new", wait=True)` load timeouts (the
tab stays open), and a `browser_retry` that gave up. Errors raised by FastMCP
itself, such as invalid arguments, were already `isError`.

These results are not errors:

- A success with a `warning` (for example `hidden_or_covered`): the action was
  dispatched.
- Results nested inside a successful result, such as
  `browser_click_and_wait_navigation`'s `navigation: {ok: false, timedOut: true}`
  after `clicked: true`.
- Container results: `browser_batch`, `browser_parallel`, `browser_broadcast`, and
  `browser_workflow` report each command's outcome in `results`. A failed command
  there does not make the call an error, because commands before it may already
  have taken effect and must not be re-sent blindly. Inspect each entry, or its
  [`progress`](#container-progress) status. The container call itself is an error
  only when it fails as a whole (for example `bridge_unavailable`, or
  `container_timeout`).
- Reads that describe state, such as `browser_status` with
  `extension_connected: false`.
- A `browser_form_fill` that filled at least one key; keys that failed carry their
  own `error` in `results`.

An `outcome_not_observed` error from an `expect` check still carries the action's
post-action `snapshot` and `target_ref`: the action ran (see [Ref lifetime](#ref-lifetime)).
`browser_save_session` fails without writing the session file when any cookie or
storage read fails, and `browser_load_session` reports an error, with
`cookies_set`, `origins_restored` and per-item `failures`, when any write failed.
A non-2xx bridge response without a JSON body becomes an error with `state.httpStatus`;
a 2xx one cannot come from a Kitsune bridge and is a
[`port_conflict`](#connection-diagnosis) (`not_kitsune`).

`isError` applies only to MCP tool results; HTTP bridge endpoints return the same JSON.

### Connection diagnosis

`browser_status` always has a `diagnosis`: a `code`, plus a `hint` and usually a
`state` unless the code is `connected`. When the bridge answers, it computes the
diagnosis from its own state and what the extension reports. When the bridge cannot
be reached, `browser_status` fails (`isError`) and `diagnosis` repeats the error's
`code`, `hint` and `state`. A tool that fails for the same reason returns the same
`code` and `hint` as an [action error](#action-errors), and keeps its `error` text.

```json
{ "status": "running", "extension_connected": false, "...": "...",
  "diagnosis": { "code": "extension_disconnected",
    "hint": "Open the browser profile where Kitsune is installed and check that Kitsune is enabled in about:addons ..." } }
```

| `code` | Condition | Reported by | Hint |
|---|---|---|---|
| `connected` | No problem found | `browser_status` | — |
| `bridge_unavailable` | The bridge is not running and could not be started, or is not reachable. A bridge that exits at startup is reported at once, not after the 15 s start timeout, with `state.exitCode` and `state.logExcerpt` (its last `bridge.log` line, at most 300 characters, bearer tokens and token values replaced by `[redacted]`). `state.reason`: `not_responding`, the bridge `state.pid` accepts connections on `state.port` but does not answer, reported about 4 s after a start, or after the 2 s status timeout for a bridge that already ran; `not_listening`, the bridge started but never listened, reported at the 15 s start timeout | MCP server | Run `kitsune-bridge start` in a terminal to see why; see `bridge.log`. `not_responding`: `kitsune-bridge stop`, then retry |
| `port_conflict` | `state.reason`: `in_use`, nothing answers after a start and another program holds `state.port` (with `exitCode` and `logExcerpt` as above when the bridge exited), or the HTTP port accepts connections but does not answer and no Kitsune bridge runs; `not_kitsune`, the HTTP port answers but not as a Kitsune bridge (a 2xx reply without a JSON body); `extension_ws_port`, no extension has connected and the bridge listens on a `KITSUNE_WS_PORT` other than the extension's (`state.extensionPort`, see below) | MCP server, bridge | Find the program with `lsof -nP -iTCP:<port> -sTCP:LISTEN`, or use another port in every client |
| `token_mismatch` | The bridge answered HTTP 401 to this MCP server's token. `state.tokenSource` is `KITSUNE_TOKEN` or the token file path | MCP server | Use the same `KITSUNE_TOKEN` and `KITSUNE_HOME` for every client, or `kitsune-bridge stop` |
| `extension_disconnected` | The bridge runs, but no extension is connected | bridge | Open the profile where Kitsune is installed and enable it; load a developer build again after a browser restart; it reconnects within 15 s of a bridge start |
| `extension_version_mismatch` | The connected extension's version (`state.extensionVersion`, `null` when it is too old to report one) is not the bridge's (`state.bridgeVersion`). Or the versions match, but a developer build was edited and not reloaded (`state.reason: "stale_developer_build"`, see below). Tools fail with it only for a command the extension does not know (`Unknown action: …`); others still run | bridge | Install the matching extension, or update the server; reload a developer build in about:debugging; reload open tabs |
| `browser_window_unavailable` | The extension has no normal window it can use: all are closed, or only private windows where Kitsune is not allowed are open (`state.windows: 0`). A tool fails with it when it targets the active tab (`No active tab found`) | bridge, extension | Open a normal window in that profile, allow Kitsune in private windows, or pass `tab_id` |
| `host_permission_denied` | Kitsune may not script a site. In `browser_status`: access to all sites is withheld (`state.reason: "not_granted"`). From a tool: the tab's site is withheld (`not_granted`), or it is one of Mozilla's restricted domains such as addons.mozilla.org (`restricted_domain`); `state` adds the tab state | bridge, extension | Allow site access in about:addons → Kitsune → Permissions; use another tab for a restricted domain |

The bridge reports the first that applies: `port_conflict` or
`extension_disconnected`, then `extension_version_mismatch`,
`browser_window_unavailable`, `host_permission_denied`. All are
`retryable: true`, because nothing reached the browser or a page, but repeating
helps only after the hint is followed. Diagnoses hold ports, versions, the token's
source, a bridge PID, a window count and, for tab errors, the usual tab state; never the token
or page content.

Stale developer builds: on connect the extension sends `sourceHash`, a fingerprint
of the scripts its manifest loads (background, then content scripts), and
`manifestHash`, a fingerprint of its `manifest.json` bytes. For a developer build
the bridge compares each, at each diagnosis, with the same fingerprint of the
unpacked extension on disk: `KITSUNE_EXTENSION_DIR`, by default `extension/` of
the checkout the bridge runs from. A difference is `extension_version_mismatch`
with `state.reason: "stale_developer_build"` and, for each that differs,
`state.extensionSourceHash` and `state.expectedSourceHash` or
`state.extensionManifestHash` and `state.expectedManifestHash` (see
[Updating Kitsune](guide.md#updating-kitsune)). A fingerprint unknown on either
side (an extension from before that check, the signed build, or a bridge
installed from a package) is not compared.

Extension port: until an extension connects, the bridge takes the extension's
port from `WS_URL` in `background/background.js` of `KITSUNE_EXTENSION_DIR`, read
at each diagnosis, and otherwise (a bridge installed from a package) assumes the
default 47652 that the signed build uses. It cannot see which copy the browser
loaded: a developer build loaded from another directory with another `WS_URL` is
not recognized unless `KITSUNE_EXTENSION_DIR` names that directory.

Not diagnosed: sites Mozilla quarantines refuse Kitsune like a page's own error
document and stay `error_page`. Firefox tells extensions neither which sites are
quarantined nor that a refusal was one, and site access still reads as granted.
If a normal site is `error_page`, check about:addons → Kitsune → "Run on sites
with restrictions". A bridge that hangs after it answered is reported as
`timeout` once the request times out. A bridge of another build is replaced
automatically rather than reported.

### Action outcomes

A successful action result (`ok: true`, no `error`) only means the command was
dispatched: the click, keypress or submit reached the page. It does not prove the
task succeeded, because a submit can be blocked by validation or a click can do nothing.
To check, pass an optional `expect` object. After dispatch, the bridge waits up to
`expect.timeout` ms (default 5000) for that outcome in the same tab. Without `tab_id`,
the bridge first resolves the active tab and sends the action to that tab, so the
action and the check cannot target different tabs after a focus change.

`expect` takes exactly one condition:

| `expect` | Observed when |
|---|---|
| `{"url": "<regex>"}` | the tab URL matches the JavaScript regex (tab metadata, survives navigation) |
| `{"url_changed": true}` | the tab URL differs from its URL before dispatch (navigation, including a hash change) |
| `{"new_tab": true}` | the action opened a tab (`target="_blank"` link, `window.open`); see [Opened tabs](#opened-tabs) |
| `{"selector": "<css>"}` | a matching element is visible |
| `{"selector": "<css>", "gone": true}` | no matching element is visible (removed or hidden) |
| `{"selector": "<css>", "value": "<str>"}` | the first matching element's value equals the string |
| `{"selector": "<css>", "value_changed": true}` | the first matching element's value differs from its value before dispatch (an element absent before dispatch counts as changed once present) |
| `{"text": "<str>"}` / `{"text_gone": "<str>"}` | the visible page text contains / no longer contains the literal substring |

```json
{ "selector": ".toast", "timeout": 3000 }
```

Supported on `browser_click`, `browser_double_click`, `browser_hover`, `browser_scroll`,
`browser_drag`, `browser_type`, `browser_form_fill`, `browser_keypress`, `browser_submit_form`,
`browser_select_option`, `browser_check`, `browser_navigate`, `browser_back`,
`browser_forward`, and `browser_reload_tab`. Over HTTP and in batch commands (including
`retry`), `expect` is also accepted by focus, blur, and click-and-wait-navigation. Element conditions take a CSS selector, not a ref, because
the post-action snapshot replaces the refs from earlier snapshots. An invalid `expect` is
rejected before anything is dispatched.

When the condition is met, the result is the usual action result plus two fields:

```json
{ "ok": true, "snapshot": "...", "dispatched": true,
  "outcome": { "expected": { "text": "Saved", "timeout": 5000 }, "met": true,
               "observed": { "source": "snapshot", "textVisible": true }, "elapsed": 0 } }
```

`observed.source` names where the outcome was seen: `snapshot`, `tab` (tab
metadata), or `page` (a read of the current document). For `text`, the post-action
snapshot already in the result is searched first, so no further read is needed when
it contains the text. Snapshot text includes accessible names and non-sensitive
field values, so it can match text that the page read would not.

When the condition is not met in time, the result is an [action error](#action-errors)
that keeps the dispatch result (`dispatched: true`, the post-action `snapshot`, any
`warning`) and adds `outcome.met: false`, `ok: false`, `code: "outcome_not_observed"`,
and the current tab state:

```json
{ "ok": false, "dispatched": true, "error": "Expected outcome not observed within 5000ms",
  "code": "outcome_not_observed", "retryable": false, "hint": "...",
  "state": { "tabId": 7, "url": "https://shop.example/checkout", "title": "Checkout" },
  "outcome": { "expected": { "url_changed": true, "timeout": 5000 }, "met": false,
               "observed": { "source": "tab", "url": "https://shop.example/checkout" },
               "elapsed": 5003, "state": { "...": "same as state" } } }
```

`retryable` is `false` for writes, because the action already ran. The batch `retry`
action does not repeat it either. Inspect the page before acting again. If the check
itself could not read the page, `observed` has its `error` and `code`. A tab that is
navigating is checked again until the deadline. If the tab closed, `state.closed` is
`true`. Sensitive fields are never read back, so a `value` outcome on one is reported
as not met with `observed.redacted: true`, and the expected value is not echoed.
`value_changed` reads the value once before dispatch (`observed.before` reports it). If
that read fails, or the field is sensitive, the action is not dispatched and the result
is an `error`.
The post-action snapshot is taken right after dispatch; after a delayed outcome,
take a new `browser_snapshot` to see the updated page. Without `expect`, results are unchanged.

`wait` and `expect` can be combined: `wait` runs before dispatch and `expect` after it. A batch's timeout also covers each command's `expect.timeout`.

### Opened tabs

A page action can open a tab instead of changing its own, for example a click on a
`target="_blank"` link. The result then lists the tabs whose opener is the action's
tab and that were created after the action started, with or without `expect`:

```json
{ "ok": true, "snapshot": "...", "opened_tabs": [{ "tabId": 4, "url": "https://shop.example/addresses" }] }
```

The list is read after the post-action snapshot. A new tab starts on `about:blank`, so
the extension waits up to 1 s for its URL; a tab still loading after that is reported
as `about:blank`. Results without an opened tab have no `opened_tabs` field. Act on an
opened tab by passing its `tabId` as `tab_id`.

`expect: {"new_tab": true}` waits until such a tab exists and reports it in
`observed.opened_tabs` (`observed.source` is `tabs`). The outcome of any other `expect`
is still checked only in the action's tab. When it is not met but the action opened a
tab, the `outcome_not_observed` result adds `opened_tabs` (read again after the wait)
and its `hint` names the tab, so that case is distinguishable from a no-op:

```json
{ "ok": false, "dispatched": true, "code": "outcome_not_observed",
  "opened_tabs": [{ "tabId": 4, "url": "https://shop.example/addresses" }],
  "hint": "The command was dispatched and opened tab 4, where the outcome may have happened; inspect it with browser_snapshot tab_id=4 instead of repeating the action." }
```

#### Popup-blocked links

Clicks are synthetic DOM events without user activation. With the default popup blocker
(`dom.disable_open_during_load=true`), the browser therefore drops the tab of a clicked
`target="_blank"` link, although a user's click on the same link opens it. So when a
`browser_click` follows a link into a new tab and no tab opens within 500 ms, the extension
opens the link's own URL with `tabs.create` in the clicked tab's window and container, with
the clicked tab as opener, and marks it in `opened_tabs`:

```json
{ "ok": true, "opened_tabs": [{ "tabId": 5, "url": "https://shop.example/addresses", "via": "link_fallback" }] }
```

This applies only when all of these hold:

- the click target is an `<a>` or `<area>` with an `http(s)` `href`, or is inside one;
- its `target` is `_blank` (case-insensitive), or it has no `target` and the page's
  `<base target>` is `_blank`;
- it has no `download` attribute and the page did not cancel the click (`preventDefault`);
- no tab opened by the clicked tab appeared within 500 ms.

A tab opened this way gets no `Referer` and no `window.opener`, and does not copy
`sessionStorage`. A link with `rel="opener"` needs `window.opener`, so it is not opened.
If such a link, or a failed `tabs.create`, leaves no tab, the click result has a
`popup_blocked` warning instead of a silent success:

```json
{ "ok": true, "warning": { "code": "popup_blocked", "state": { "url": "https://shop.example/addresses" },
  "hint": "The link opens in a new tab, but no tab opened; the popup blocker likely dropped this synthetic click. ..." } }
```

With `expect`, an unmet outcome then uses that hint. Popups that page scripts open with
`window.open` are never reopened. A `browser_trusted_click` is a real OS click with user
activation, so the popup blocker lets its tab open. It needs a visible, focused browser
window and OS permission (macOS: Accessibility), and its result does not list `opened_tabs`.

### Ask-before-act

Ask-before-act is an opt-in bridge policy. It holds sensitive actions without running
them and asks the caller for an explicit, action-bound second step. It is off by
default; when it is off, behavior is unchanged. Turn it on with
`browser_set_policy(ask=True)` (`POST /api/set-policy {"ask": true}`), or lock it on
for a bridge (see below).

In ask mode the bridge holds:

| `state.reason` | Actions |
|---|---|
| `submit` | `browser_submit_form` (`submitForm`) |
| `submit_control` | Click, double-click, trusted click, or click-and-wait-navigation on a submit control (`button` whose type is submit, `input type=submit/image`, or an element inside one, with a form). Also Enter (`browser_keypress`) on a text-like `input` in a form or on a submit control |
| `submit_label` | The same clicks, or Enter, on any other button whose label reads like a submit, order, or payment ("Place order", "Pay $12", "결제하기"), for example a JS-driven `type=button` or `role=button`. See [Submit labels](#submit-labels) |
| `javascript` | Agent-supplied JavaScript: `browser_js` (including `full=True`), `browser_wait_for(js=...)`, `browser_sync_barrier`, batch `js`, and batch `if`/`while` conditions |
| `cookies` | `browser_cookies` (every op, including `get`), `browser_save_session`, `browser_load_session` |
| `storage` | `browser_storage` (every op, including `get`, `list` and `snapshot`), on any page |
| `clipboard` | `browser_clipboard` (`read` and `write`) |
| `downloads` | `browser_downloads` (every op: `download`, `list`, `cancel`) |
| `payment_url` | Navigation, new tabs, or new windows whose URL matches an `askUrls` pattern |
| `payment_page` | Page actions (click, type/fill, form fill, select, check, keypress, drag, submit, upload, dialog policy, JavaScript) on a tab whose current URL matches `askUrls` |
| `unknown_outcome` | Repeating a write whose identical previous attempt ended `retryable: false` (a timeout or disconnect after sending, see [Action errors](#action-errors)) |
| `uninspectable_target` | A click or Enter whose target the bridge could not inspect (for example, an extension older than this bridge, or a screen-coordinate trusted click). Inspection fails closed |

Storage, clipboard, and downloads are held for every op, reads included, and on every
page, not only payment-like ones. Page storage often holds session tokens, as cookies
do. The clipboard reaches outside the browser: a read can return a password copied
from a password manager, and a write replaces what the user will paste. A download
writes a file from any URL to disk, and `list` returns local file paths and source
URLs.

`askUrls` are case-insensitive regular expressions. The default list is
`checkout`, `payment`, `billing`, `/pay\b`, `purchase`, and `/order`. Replace it with
`browser_set_policy(ask_urls=[...])`; `[]` disables URL matching. `ask` must be a
boolean and `askUrls` a list of non-empty strings that compile as regular expressions.
Any other value (for example a bare string `"checkout"`) returns `invalid_policy`
(HTTP 400), and none of the request is applied, in locked mode too.

A held call returns `confirmation_required` (HTTP 403 from the bridge) and does not
run. `retryable` is `true` because nothing reached the page:

```json
{ "error": "Confirmation required: clicks a submit control (Place order)",
  "code": "confirmation_required", "retryable": true, "hint": "Nothing ran. Ask the user ...",
  "state": { "confirmToken": "…", "expiresInSeconds": 120, "action": "click",
             "reason": "submit_control", "description": "clicks a submit control (Place order)",
             "tabId": 7, "url": "https://shop.example/cart", "params": { "ref": "s3e12" } } }
```

To approve the action, the client asks the user. It then repeats the identical call
with the token: MCP tools take `confirm_token`, and HTTP and batch bodies take
`confirmToken`. The token:

- is bound to the exact action, its parameters, and the resolved target tab. Without
  `tab_id`, that is the tab that was active when the call was held. A different action,
  any changed parameter, or another active tab is rejected;
- is single-use. Any presentation spends it, including a mismatched one;
- expires after 120 seconds;
- is honored only on a direct tool call. Inside `browser_batch`, `browser_parallel`,
  `browser_retry`, `browser_broadcast`, workflows, and batch `sequence`/`if`/`while`,
  a token is spent and the step stays held. Batch `if`/`while` can therefore not run
  in ask mode.

A rejected token returns a fresh `confirmation_required` error with
`state.tokenRejected` set to `unknown`, `used`, `expired`, `mismatch`, or `not_direct`.
A batch, workflow, parallel sequence, or batch `sequence` stops at the first held
step, so later steps that may depend on it do not run. A held batch result has
`_stopped: true`.

The audit log (`browser_audit`) records each held call
(`params.confirmation: "required"`, `error: "confirmation_required"`) and each
accepted token (`params.confirmation: "accepted"`), followed by the action's normal
entry. The token itself is never logged.

**What the token proves.** The token goes back to the agent that made the call, so
it enforces an explicit second step bound to one exact action. It does not prove that
a human approved. Real approval has to come from the client, for example a
permission prompt for the tool call that carries `confirm_token`. An agent that
can call `browser_set_policy` can also turn agent-settable ask mode off. That mode
guards against accidents only.

**Locked mode.** Start the bridge with `KITSUNE_ASK_BEFORE_ACT=1` in its environment
(the MCP server passes its environment to a bridge it starts) or with
`kitsune-bridge start --ask-before-act`. Ask mode then starts on and is locked. `browser_set_policy` can still add `askUrls` patterns.
Setting `ask=False` or dropping a pattern returns `policy_locked` (HTTP 403), and none
of the request is applied. `allow`, `deny`, and `readonly` stay settable.
`browser_get_policy` reports `ask`, `askUrls`, and `askLocked`. The bridge is shared:
if one is already running without the lock, stop it (`kitsune-bridge stop`) so that
the next start applies it.

A locked start also writes `ask-before-act-locked` to the Kitsune home
(`$KITSUNE_HOME`, default `~/.kitsune`), user-only (`0600`) like the token file.
While that file exists, every bridge using that home starts locked, whatever its
environment. So a restart cannot come up unlocked: `kitsune-bridge stop` followed by
the next tool call, an idle shutdown, or an MCP server that replaces an outdated
bridge. `KITSUNE_ASK_BEFORE_ACT=0` does not unlock either. Any entry at that path
locks, including an unreadable file or a symlink, and the bridge never follows a
symlink there. If the file cannot be written, the bridge still starts locked, prints a
warning to its log, and the lock does not survive a restart.

To unlock deliberately:

1. Remove `KITSUNE_ASK_BEFORE_ACT=1` (or `--ask-before-act`) from every MCP server
   configuration and script that starts the bridge. Otherwise the next start writes
   the file again.
2. Delete the file: `rm ~/.kitsune/ask-before-act-locked`.
3. Run `kitsune-bridge stop`. The running bridge stays locked until it stops.

Agent-settable ask mode (`browser_set_policy(ask=True)`) is not persisted.

#### Submit labels

`submit_label` covers buttons that submit or pay through JavaScript rather than
through a form. The content script reads the label of the clicked control: its
`aria-label`, else its text, else the `value` of an `input type=button/submit/image`.
The control is a `button`, one of those inputs, or an element with `role="button"`,
including an element inside one. The label is normalized: case, Unicode width, and
whitespace are folded; a trailing parenthetical, an amount ("$12.00", "12,000원"),
and leading or trailing punctuation are dropped; labels over 60 characters never
match. The **whole** remaining label must then be one of a fixed set of phrases:

- English: place/submit/confirm/complete/finish (your/my/the) order/purchase/payment/
  booking/reservation; pay, pay now, pay securely, pay with …; buy, buy (it) now,
  purchase, order now; (proceed/continue/go to) checkout; continue to payment;
  submit; confirm, confirm and pay;
- Korean: 결제, 주문, 구매, 제출 with an optional 지금/바로 prefix and 진행/완료/확정
  and 하기 suffixes (결제하기, 바로구매, 주문 완료);
- Japanese: 注文する, 注文を確定する, 購入する, 今すぐ購入, 支払う, お支払いへ進む, レジに進む;
- Chinese: 支付, 付款, 立即购买, 确认支付, 下单, 提交, 提交订单, 确认订单, 去结算, 結帳;
- German, French, Spanish: (jetzt) kaufen/bezahlen/bestellen, zahlungspflichtig
  bestellen, zur Kasse; payer, acheter, commander, passer la commande; pagar,
  comprar, realizar pedido, finalizar compra.

The match is deliberately conservative: "Submit search", "Search", "Add to cart",
"Order history", "Payment methods", "Continue", "Send", and a bare "OK", "확인",
"確認" or "确认" are not held. A JS-driven commit button with another label ("Send",
"Transfer", "Delete account"), a link (`<a>` without `role="button"`), or a label
drawn on a canvas is not caught. On a payment-like page every click is held anyway
(`payment_page`). An extension older than this bridge does not report labels, so only
structural submit controls are held there.

**Check-then-act window.** The bridge inspects the target (tab, URL, and whether a
click or Enter submits a form or hits a submit-labeled button) with `describeTarget` before it dispatches the action.
The page can change between the two: it can navigate to a payment-like URL, or a
script can turn the target into a submit control. Such a change is not caught. Passing
`tab_id` removes the active-tab part of this window, but not changes inside the page.

**Client-side approval (MCP elicitation), evaluated.** MCP elicitation
(`ctx.elicit`) would let the server ask the user through the client and retry with
the token itself, so approval would not pass through the agent. It is not used, for
these reasons:

- The dependency floor is `mcp>=1.2.0`; `Context.elicit` arrived in later releases
  (the 2025-06-18 protocol revision). Using it means raising the floor or a runtime
  feature check.
- Every Kitsune tool is a synchronous function. FastMCP runs synchronous tools on its
  event loop, so a tool cannot await `ctx.elicit`. The held tools would have to become
  `async` and move their bridge calls off the loop.
- Client support varies: many clients do not declare the `elicitation` capability.
  Clients that do may let the agent answer the form instead of the user. So
  `confirmation_required` stays as the fallback, and an accepted elicitation is
  still not proof that a human approved.

A later change could add it as an option: for clients that declare `elicitation`,
an `async` wrapper around held tools would elicit on `confirmation_required` and
retry once with the token. For now, real approval has to come from the client's own
permission prompt for the tool call that carries `confirm_token`.

**Annotations are not policy.** MCP `ToolAnnotations` (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) are static, per-tool metadata.
Clients may use them for their own permission prompts. The bridge never reads them,
and they do not change when ask mode is on. Enforcement comes from the bridge policy
alone: `readonly`, `allow`/`deny`, and `ask`.

### Sensitive-field redaction

Structural read tools never return the value (or its length) of a sensitive
field. A field is sensitive if it is:

- an `<input type="password">`;
- an input whose `autocomplete` contains `current-password`, `new-password`,
  `one-time-code`, `cc-number` or `cc-csc`;
- a `text`, `search`, `tel` or `number` input whose name, id, placeholder or
  `aria-label` contains a secret word such as password, passcode, pin, otp,
  cvv/cvc/csc, security code, verification code or card number (so a password
  field whose show/hide toggle switched it to `type=text` stays redacted), or an
  `inputmode="numeric"` input named like a code or digit box (zip/postal codes
  excepted);
- an input, `<textarea>` or `contenteditable` region masked with
  `-webkit-text-security`;
- an input the browser or password manager has autofilled (`:autofill`), until
  the autofill state is cleared;
- a custom element (e.g. a web-component text field) or other element with a
  `value` property that either has `type="password"` or a sensitive
  `autocomplete`, or whose shadow root contains a sensitive field. Closed shadow roots are
  checked too, through Firefox's `openOrClosedShadowRoot`.

`browser_snapshot` omits the `value=` state; `browser_forms`, `browser_dom`, and
`browser_query`'s `value` field replace `value` with `"redacted": true` and
`"hasValue": <bool>`. Tag, type, name, id, label, selector, and ref are kept, so
the field remains interactable. `browser_find` does not match queries against
sensitive values.

A masked textarea or editing region keeps its secret as text content, so that
content is also left out of snapshot names and text lines, `browser_find`
matching, scoring and result text, element `text` fields (`browser_dom`,
`browser_query`, `browser_explain_selector`, `browser_wait_for` element
results, click results), field labels, `browser_markdown` and
`browser_readability`; `browser_html` and `browser_query` `html` serialize the
element empty. Page text (`browser_page_text`) is rendered text, where the
browser already shows masked text as mask characters. Unmasked textareas and
editing regions are unchanged. Results and errors of write actions on sensitive fields
(`browser_type`, `browser_form_fill`, …) do not echo the typed value. There is
no opt-in to reveal values through these tools.

Frameworks such as React mirror a typed value into the `value` attribute, so
markup and attribute paths drop that attribute from sensitive fields, whether it
was server-rendered or synced: `browser_html` and `browser_query` `html` omit it
from every sensitive field in the returned markup, and `browser_query` `attrs` or
a field named `value` in any letter case never return it. Other elements keep
their `value` attribute. The page DOM is not modified.

While any sensitive field on the page carries a `value` attribute, every tool
that takes a selector (`browser_query`, `browser_explain_selector`,
`browser_html`, `browser_click`, `browser_wait_for`, `browser_form_fill` keys,
…) refuses selectors that test the `value` attribute, because
`input[type=password][value^="K"]`, or a combinator or `:has()` form of it,
would reveal the value one character at a time through found/not-found
results. These tools return the error `value-attribute selectors are
disabled while a sensitive field has a value attribute`; `browser_find` instead
skips its CSS branch and falls back to text search. A selector counts as testing
`value` if it contains `[value` (any case, optionally with a namespace prefix
such as `[*|value`), or if it contains `[` together with a backslash escape or
a `/*` comment.

If a cloned subtree does not mirror the page, sensitivity cannot be mapped
onto it, so markup from that clone drops every `value` attribute and the
content of every textarea and `contenteditable` element.

These rules are heuristics: a field that is masked some other way (canvas,
custom fonts) or named without a recognizable word is not detected.

`browser_js`, the `js` condition of `browser_wait_for`, `browser_sync_barrier`,
and `format="html"` need the developer extension build; the signed build returns
an error with `"devBuildRequired": true`.

This is not a sandbox: `browser_js` runs arbitrary page JavaScript and can read
any value the page can, including sensitive fields. Treat access to `browser_js`
as a separate, unrestricted trust boundary.

### Audit log

`browser_audit` (`/api/audit`) returns recent write-action entries:
`{ts, action, params, ok, error, ms}`. `params` never contains JavaScript bodies
(`code`, the `condition` of `if`/`while`, the `predicate` of `syncBarrier`),
typed text or stored secrets. These values are replaced with
`"[redacted]"`:

- `text` of `type` and `keypress`;
- `value` of `fill` and `setEditableContent`;
- `value` of `selectOption`, whether or not the `<select>` is a sensitive field
  (the bridge sees only the selector or ref);
- `prompt_text` of `handleDialog` (`browser_handle_dialog` prompt answer);
- every value of `formFill` `fields`. The field keys (selectors, names or labels)
  are kept;
- `value` of `cookies` (`browser_cookies`);
- `value` of `storageOp` (`browser_storage` set and restore);
- `text` of `clipboard` (`browser_clipboard` write).

The action, `selector`/`ref`/`element`, `tabId`, other options (`op`, `name`,
`url`, `domain`, `kind`, `key`, `modifiers`, `clear`, …), duration
and outcome are kept. `intercept` takes only URL patterns and an effect, never a
response body, so its entries are logged as sent. Nested commands (`batch`
`commands`, `parallel` `sequences`, `retry`/`broadcast` `command`,
`if`/`while`/`try` blocks) follow the same rule. `uploadFile` entries keep their
file paths; file contents are never logged. The extension still receives the
unredacted value. Only the audit copy is scrubbed.

Each command run inside `browser_batch`, `browser_parallel`, `browser_broadcast`,
`browser_retry`, a workflow, or a batch `sequence`/`if`/`while`/`try` gets its own
entry, including held `confirmation_required` steps. These entries add
`parent: {call, path}`. `call` is shared by every entry from one container call.
`path` lists the enclosing steps from the outermost, for example
`[{"action": "batch", "index": 1}, {"action": "parallel", "sequence": 0, "index": 2}]`.
The frames are `batch`/`sequence` `index`, workflow `name` and `index`, `parallel`
`sequence` and `index`, `broadcast` `tabId`, `retry` `attempt` (from 1), `if`
`branch` and `index`, `while` `iteration` and `index`, and `try` `block` (`do` or
`catch`) and `index`. A nested container (for example a `parallel` inside a batch)
is logged after its steps. Direct calls have no `parent`.

### Container progress

`browser_batch`, `browser_parallel` and `browser_workflow` (POST `/api/batch` and
`/api/workflow`) add `progress`, `status` and `summary` next to `results`, which is
unchanged. `progress` has one record per command, in order, including commands that
never ran:

```json
{ "results": [{ "ok": true }, { "error": "Command timed out after 30s", "code": "timeout", "retryable": false }],
  "progress": [
    { "index": 0, "action": "click", "tabId": 3, "status": "ok" },
    { "index": 1, "action": "submitForm", "tabId": 3, "status": "unknown" },
    { "index": 2, "action": "pageText", "tabId": 3, "status": "skipped" }],
  "status": "unknown",
  "summary": "3 commands: 1 ok, 1 unknown, 1 skipped. Unknown, may have taken effect (check the page before repeating): step 1 (submitForm on tab 3). Skipped, never started: step 2 (pageText on tab 3)." }
```

| `status` | Meaning |
|---|---|
| `ok` | The command ran without an error. Like any result, this means dispatched, not that the outcome happened (see [Action outcomes](#action-outcomes)) |
| `failed` | It returned an error that is safe to repeat (`retryable: true`, unclassified errors, `error_page`, or `ok: false`) |
| `held` | [Ask-before-act](#ask-before-act) held it; nothing ran |
| `unknown` | It may have taken effect: an error with `retryable: false`, or it was in progress at the container's deadline. Observe the page before acting again |
| `skipped` | It never started: an earlier step stopped the container (a held step, `stopOnWarning`, a failed workflow step) or the deadline passed |

`tabId` is the command's `tabId` when it has one. A container command (`sequence`,
`retry`, `if`, `while`, `try`, `parallel`, `broadcast`) takes the first of `unknown`,
`held`, `failed`, `skipped` that it or a nested command has, else `ok`; a `retry`
whose last attempt succeeded is `ok`. A `parallel` record adds `sequences`: one list
per sequence of `{sequence, index, action, tabId, status}`; commands that never
started are `skipped`. The top-level `status` combines the records the same way.

`summary` states `progress` in one paragraph a user can read without the records:
the count of each status, then each command that is `unknown`, `held` (with how to
confirm it), `failed` or `skipped`, by its `index` and `action` (and `tabId`), at most
five per status. A `parallel` that started is counted by the commands of its
sequences (`sequence 1 step 0`). It starts with `Stopped at its 300s deadline.` after
a `container_timeout`. It is text for people; read `progress` in code.

`progress` is also what to resume from: the commands before the first record that
is not `ok` have run, `unknown` ones must be observed before they are repeated, and
`skipped` ones are safe to send. See [Checkpoint and resume](checkpoint-resume.md)
for the design of resuming a container.

A batch stops at its deadline (the sum of its waits plus 15 s, at least 300 s; a
workflow at 300 s): the command in progress is cancelled and later commands do not
start. The response then adds a `container_timeout` error (`retryable: false`,
`state.deadlineSeconds`) to the partial `results` and `progress`, and the audit log
records the cancelled command with `error: "cancelled"`. The MCP tools wait 30 s
past that deadline (15 s for the cancelled command to settle, 15 s for the reply),
so the client receives this report instead of its own request timeout.

Tab ownership in `parallel`: sequences run concurrently, so a focus change in one
sequence must not move another sequence's command to a different tab. Before any
sequence starts, the bridge refuses the whole `parallel` (`results` is one error):

- with `tab_required` when a command that acts on a tab has no `tabId`, also inside
  a nested `sequence`, `retry`, `if`, `while` or `try`. Commands that act on no page
  tab (`sleep`, `newTab`, `tabs`, `getWindows`, `createWindow`, `closeWindow`,
  `focusWindow`, `cookies`, `clipboard`, `downloads`, `clearBrowsingData`, `intercept`,
  `reloadExtension`, `screenshot`, `broadcast`, `syncBarrier`) need none;
  `screenshot` captures the visible tab.
- with `tab_owned` when two sequences write the same tab (the writes that make an
  unknown outcome unsafe to repeat, including `switchTab` and `closeTab`). The
  condition of an `if` or `while` counts as a write of its `tabId`: it is JavaScript
  run on that tab, like `js`, and the bridge cannot tell whether it changes the page.
  Several sequences may read one tab, for example with `waitForElement`.

A `tabId` substituted at run time (`${$N.tabId}` in a nested `sequence`) is checked
when its command runs: the first sequence that writes a tab owns it, and a later
write from another sequence fails with `tab_owned` without being dispatched. In any
batch, workflow or parallel step, a `tabId` that is not a tab id (`0`, `""`, `null`
from a failed `newTab`, an unresolved template) fails with `invalid_tab_id` instead of
falling back to the active tab, and a numeric string such as `"12"` is sent as `12`.
A closed tab is never replaced: commands on it fail with `tab_closed`.

### `browser_tabs(action="list", tab_id=None, url=None, force=False, raw=False, wait=False, timeout=15000)`

Replaces `browser_new_tab`, `browser_close_tab`, and `browser_switch_tab`.
`action` is `list`, `new`, `close`, or `select`. List uses GET `/api/tabs`;
new uses POST `/api/new-tab` (default URL `about:blank`), close uses
`/api/close-tab`, and select uses `/api/switch-tab`. Close and select require
`tab_id`. `force` disarms beforeunload for close; `raw` bypasses the image URL
viewer for new tabs. List is never cached. New returns immediately unless
`wait=True`, which waits up to `timeout` ms for the page to load and returns
the actual URL and load status. Options for other actions are ignored.

### `browser_page_text(tab_id=None)`

Replaces the duplicate `browser_page_text_by_tab_id`. Uses GET `/api/page-text`
for the active tab or POST to the same endpoint with `tabId` for a specific tab.

### `browser_snapshot(tab_id=None, selector=None, ref=None, max_depth=None, text=False)`

Returns a compact accessibility-style text tree with fresh refs. With no arguments,
reads the full page via GET `/api/snapshot`. Optional arguments use POST to the same
endpoint (`tabId`, `selector`, `ref`, `maxDepth` in the JSON body).

- `selector` scopes the snapshot to a CSS-selected subtree, including its root.
- `ref` scopes to an existing snapshot element and takes precedence over `selector`.
  It is resolved before generating fresh refs. Missing selectors and stale refs
  return errors rather than silently falling back to the full page.
- `max_depth` is a non-negative integer DOM depth relative to that root (`body`
  without a scope). `0` considers only the root, `1` also considers its immediate
  element children, and `None` traverses all depths. Non-semantic containers still
  count toward DOM depth, even when they produce no snapshot line.

Every successful snapshot, even a scoped one, invalidates earlier refs in that tab.
Only emitted elements receive usable refs. Generic clickable wrappers with any
native interactive descendant or at least two interactive descendants are not
reported as buttons; their children remain discoverable. Explicit roles and native
controls are preserved. Custom controls use explicit labels/title/alt, then direct
text; subtree text is used only when at most 80 characters and five elements long.

Consecutive composite siblings (children containing elements, or list items) with
identical tag/role/input-type structure show the first five items, followed by
`- … N more similar items`. Names, IDs and classes do not affect the signature.
Leaf controls such as toolbar buttons are not collapsed. Hidden items are excluded
from grouping; collapsed items have no refs. Scope a specific card/list item to
inspect it fully. Post-action snapshots additionally keep the acted-on target's
item, even beyond the first five, and return a fresh `target_ref`. Character-limit
truncation can still omit other context but never the visible acted-on target.

`text=True` (opt-in; the default is unchanged) adds readable text in the same call:
each block's visible inline text becomes a `- text "…"` line in document order,
and the result adds `url`, `title` and `tabId`. Text lines have no refs; refs keep
the lifetime above. Text that only repeats a control, label or heading name is
omitted, as are hidden, `aria-hidden`, form-field and sensitive-field content.
Lines are clipped at 1,000 characters with `…`; the snapshot is capped at 12,000
characters, and truncation sets `truncated: true` and ends it with
`- … truncated; scope with selector, ref or max_depth`. Repeated composite items
still collapse, but siblings with only inline content (prose paragraphs, text list
items) do not. `selector`, `ref` and `max_depth` scope text the same way.
With `KITSUNE_POST_ACTION_SNAPSHOT=text` in the bridge environment, post-action
snapshots use the same text lines within their 4,000-character cap, so an
`expect={"text": ...}` already present there needs no further read.

### `browser_type(selector="", text="", mode="type", clear=False, tab_id=None, ref=None, element=None, wait=False, timeout=10000)`

Replaces `browser_fill` and `browser_set_editable_content`.

| Mode | Behavior | Bridge endpoint |
|---|---|---|
| `type` | Type using events; `clear=True` clears first | `/api/type` |
| `fill` | Always replace the field value with native setter and input/change events; ignores `clear` | `/api/fill` |
| `editable` | Insert plain text into contenteditable/input/textarea; append by default, replace with `clear=True` | `/api/set-editable-content` |

All modes accept the [actionability wait](#actionability-wait). Editable mode sends `format="text"`. Callers migrating from the old editor tool
should pass `clear=True` to retain its replace-by-default behavior. For trusted
HTML insertion use `browser_js`; the underlying endpoint still accepts `format="html"`
(both developer build only).

### `browser_form_fill(fields, tab_id=None)`

Fills several fields in one call via POST `/api/form-fill`. Each key is matched
as a CSS selector, then `name` attribute, label text, and placeholder; results
are returned per key, and one failing key does not stop the others. When every
key fails, nothing was filled and the result adds `"error": "No field was filled"`
(an MCP error); a partial fill is not an error, so check each key.

For a `<select>`, the value must equal an option's `value`, the same rule as
`browser_select_option`. A match sets it and dispatches `change` and `input`.
An unmatched value returns `{"error": "No matching option for value: X"}` for
that key, leaves the previous selection untouched, and dispatches no events.

For a checkbox or radio, the value must be `true`/`false`, `1`/`0`, or one of the
case-insensitive strings `"true"`/`"false"`, `"on"`/`"off"`, `"yes"`/`"no"`,
`"1"`/`"0"`. Any other value (for example `"maybe"`, `""` or `null`) returns
`{"error": "Invalid checkbox value: ..."}` for that key and leaves it unchanged.
A selected radio cannot be set to false, since that would leave its group with
no selection; set another radio in the group to true instead. False on an
unselected radio is a no-op that reports `"checked": false`.

```json
{ "results": { "Email": { "ok": true, "tag": "input" }, "Color": { "error": "No matching option for value: purple" } } }
```

### `browser_wait_for(selector=None, text=None, text_gone=None, js=None, url=None, title=None, network_idle=False, time=None, timeout=10000, tab_id=None, ref=None, element=None)`

Replaces `browser_wait_for_element`, `browser_wait_for_result`,
`browser_wait_for_url`, `browser_wait_for_title`, and `browser_wait_for_network_idle`.
Exactly one condition must be supplied, including `time=0` or an empty string.
`network_idle=False` does not count as a condition. `selector` and/or `ref` count
as one element condition (ref takes precedence); combining it with text, URL,
sleep, or any other condition is invalid. `element` alone is not a condition.
Invalid combinations return an error before contacting the bridge.
`timeout` must be positive.

| Condition | Meaning | Bridge endpoint |
|---|---|---|
| `selector` / `ref` | Wait for a visible element, poll every 200 ms | `/api/wait-for-element` |
| `text`, `text_gone` | Literal substring of `document.body.innerText` appears/disappears, poll every 500 ms | `/api/wait-for-result` |
| `js` | Expression returns a non-empty result, poll every 500 ms | `/api/wait-for-result` |
| `url`, `title` | JavaScript regex pattern matches | `/api/wait-for-url`, `/api/wait-for-title` |
| `network_idle=True` | No new network resources for 500 ms | `/api/wait-for-network-idle` |
| `time` | Sleep a finite, non-negative number of seconds in the MCP server | None |

`timeout` is in milliseconds and does not limit an explicit sleep. JS retains
bridge non-empty-result semantics: `false` and `0` count as results; return
`null` or an empty string while waiting for a predicate. Text waits use POST `/api/wait-for-text`.

### `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False, ref=None, element=None)`

Replaces `browser_element_screenshot` and `browser_full_page_screenshot`.
Returns `[Image, metadata]` (MCP image content) by default; `save_to` writes the
full PNG and returns metadata with `saved_to` instead, and `data_url=True` adds the
raw PNG data URL. `selector`/`ref` and `full_page=True` are mutually exclusive.

- Default: viewport capture via POST `/api/screenshot`. A supplied `tab_id`
  selects that tab and focuses its window before capture.
- `selector`: crop one element via POST `/api/element-screenshot`.
- `full_page=True`: scroll and stitch up to 30 viewports via POST `/api/full-page-screenshot`.

Element/full-page capture temporarily activates the target tab. `save_to`
overrides the file destination; `data_url=True` additionally returns `dataUrl`.
The former element tool's `scale` option remains available through the bridge
endpoint; the merged MCP tool uses its default scale of 1.

### `browser_trusted_click(selector="", x=0, y=0, coordinate_space="viewport", focus=False, tab_id=None)`

Clicks with the real OS mouse pointer via POST `/api/trusted-click`, for pages
that ignore synthetic DOM clicks. It moves the user's cursor, so prefer
`browser_click`. A `selector` is scrolled into view and clicked at its center;
otherwise `x`/`y` are viewport coordinates, or desktop coordinates with
`coordinate_space="screen"`.

| Platform | Click backend |
|---|---|
| Windows | `user32` `SetCursorPos` + `mouse_event` |
| macOS | Quartz `CGEventCreateMouseEvent` when pyobjc is importable (`uvx --from 'kitsune-browser-mcp[macos]' kitsune`), else [`cliclick`](https://github.com/BlueM/cliclick) on `PATH` |
| Linux | Not supported; returns `{error}` |

On macOS, the app running the bridge (your terminal or MCP client) needs
Accessibility permission (System Settings > Privacy & Security > Accessibility);
without it, or without either backend, the call returns `{error}` explaining what
is missing instead of clicking. Results include `clickBackend` (`win32`, `quartz`,
or `cliclick`).

`focus=True` tries to foreground the browser first and reports `focused`:

- Windows matches the window title: the page title for viewport clicks, otherwise
  `Zen Browser`, `Mozilla Firefox`, then `LibreWolf`. Set
  `KITSUNE_BROWSER_WINDOW_TITLE` (comma list) to change the defaults.
- macOS activates the first running process named `zen`, `firefox`, or
  `librewolf` through System Events (`osascript`, which may ask for Automation
  permission). Set `KITSUNE_BROWSER_PROCESS` (comma list) to change the defaults.

The bridge endpoint also accepts `title` and `process` to override these per call.

### WordPress workflow

`browser_wp_html` is removed. Run
`browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`
from a browser logged into WordPress. The workflow navigates the active tab,
waits for Elementor preview widgets, then extracts their HTML. Its final step
returns the JSON string containing widget data inside the workflow's `results`.

## HTTP bridge endpoints

Requests require a bearer token from `~/.kitsune/token` (or `KITSUNE_TOKEN`).

## Status & Info

### GET /api/status
Connection status and config; `diagnosis` is described under [Connection diagnosis](#connection-diagnosis).
```json
{ "status": "running", "extension_connected": true, "port": 47651, "ws_port": 47652, "screenshot_dir": "...",
  "diagnosis": { "code": "connected" } }
```

### GET /api/version
Bridge and extension version info.
```json
{ "bridge": "0.2.1", "api": "1", "extension_connected": true, "extension_version": "0.2.1", "extension_build": "dev", "extension": 25 }
```

## Page Data (GET, cached 5s except page-info)

### GET /api/page-info
Page metadata plus current dialog policy and recent dialog history. Never cached.
Also accepts POST with `{ "tabId": 123 }`. MCP tool: `browser_page_info(tab_id=None)`.
```json
{ "url": "...", "title": "...", "lang": "en", "width": 890, "height": 992, "forms": 0, "links": 1, "images": 0 }
```

### GET /api/page-text
All visible text on the page.
```json
{ "text": "..." }
```

### GET /api/tabs
All open browser tabs.
```json
{ "tabs": [{ "id": 1, "url": "...", "title": "...", "active": true, "windowId": 1, "index": 0 }] }
```

### GET /api/dom
Accessibility tree (depth=6).
```json
{ "tree": { "ref": "r0", "tag": "body", "children": [...] } }
```
Sensitive fields report `redacted`/`hasValue` instead of `value`.

### GET /api/forms
All form fields on the page. Sensitive fields have `redacted`/`hasValue`
instead of `value` (see [Sensitive-field redaction](#sensitive-field-redaction)).
```json
{ "fields": [{ "ref": "s1e0", "tag": "input", "type": "password", "name": "password", "id": "password", "placeholder": "", "redacted": true, "hasValue": true, "label": "Password", "required": false, "selector": "#password", "bounds": [...] }] }
```

### GET /api/screenshot
Captures the active page as PNG and saves to the bridge screenshot directory
(`$KITSUNE_HOME/screenshots`, default `~/.kitsune/screenshots`).
```json
{ "url": "...", "title": "...", "saved_to": "...", "filename": "kitsune_20260313_120000.png" }
```

### POST /api/screenshot, /api/element-screenshot, /api/full-page-screenshot
Return `dataUrl` and capture metadata. Set `imagePreview: true` for an inline preview
under roughly 1 MB (large images are JPEG-encoded and downscaled in the extension).
Without that flag, return full PNG data. Element requests require `selector` and
accept `tabId` and `scale` (1–4); full-page requests accept `tabId`. Captures retain
existing canvas and scroll/segment limits and can truncate very large documents.

MCP tool `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False, ref=None, element=None)`
returns `[Image, metadata]` by default, giving the model actual MCP image content.
`save_to` writes a full PNG and returns metadata with `saved_to` instead.
`data_url=True` returns raw PNG data in metadata.
Inline preview dimensions can be smaller than the original capture metadata.

### POST /api/handle-dialog
`{ "accept": true, "prompt_text": "answer", "tabId": 123 }`

MCP tool: `browser_handle_dialog(accept=True, prompt_text=None, tab_id=None)`.
Sets the tab's policy for subsequent dialogs and returns `{ok, policy, dialogs}`.
By default, with no policy, original native alert/confirm/prompt functions run and
the user's actual response is recorded. `beforeunload` also keeps native behavior.
Only an explicitly configured policy auto-answers dialogs. Clear a policy with
`browser_handle_dialog(accept=None, tab_id=123)` or HTTP
`{ "accept": null, "tabId": 123 }` to restore native pass-through.
Accepted prompts return `prompt_text` if supplied, otherwise the prompt's default;
dismissed prompts return null and dismissed confirms return false. With a policy
set, alerts return immediately; in native mode they wait for the user.
Recent history is also included in `browser_page_info`:

```json
{ "policy": { "accept": true, "prompt_text": "answer" }, "dialogs": [
  { "type": "prompt", "message": "Name?", "default_value": "Guest",
    "timestamp": 1792200000000, "response": "answer", "pending": false }
] }
```

The page-world override records every intercepted invocation (last 100 per
document). Answers are synchronous; `pending: false` means the user's native
response or configured automatic answer was already returned, and a later tool
call cannot change it. Alert responses are null (no return value). Policies
persist across navigation until tab closure or extension restart, with native behavior
applying until asynchronous restoration completes. Clearing a policy also removes
it from the per-tab policy store. Capture starts at
`document_start` for new documents and on connection for existing tabs.
Scripts holding original references before injection, iframe dialogs (including
cross-origin frames), browser-internal pages, and already-open native dialogs are
outside this mechanism. Page scripts can also replace or bypass the overrides.

`accept: true` also disarms `beforeunload` using the same returnValue/preventDefault
suppression as `browser_close_tab(force=True)`, allowing navigation away. Unload
events are recorded with type `beforeunload`. `accept: false` leaves native unload
behavior; the extension cannot answer a native “Leave page?” prompt. Accepting
temporarily clears `onbeforeunload`; clearing or dismissing the policy restores the
saved handler if the page has not supplied a replacement.
Force-closing remains available regardless of dialog policy.

### POST /api/wait-for-text
`{ "text": "Ready", "text_gone": "Loading", "timeout": 10000, "tabId": 123 }`

MCP tool: `browser_wait_for(text=...)` or `browser_wait_for(text_gone=...)`.
Provide at least one non-empty condition (the HTTP endpoint accepts both at once). Polls rendered top-level document text
about every 100 ms, excluding hidden text; if both conditions are provided they
must both hold. Matching uses case-sensitive substrings. Frames and shadow roots
are not traversed. Returns `{ok, text, text_gone, elapsed}`; timeout returns
`ok: false` with an error. A zero timeout performs one immediate check.

## Navigation (POST)

### POST /api/navigate
`{ "url": "https://example.com", "tabId": 1, "expectTitle": "Example" }`
- `tabId` optional (defaults to active tab)
- `expectTitle` optional — warns if title doesn't match (catches silent redirects)

### POST /api/new-tab
`{ "url": "https://example.com" }` — url optional, defaults to about:blank.
Returns `{ "ok": true, "tabId": 123 }` immediately.

`{ "url": "https://example.com", "wait": true, "timeout": 15000 }` waits up to
`timeout` ms (default 15000) for the load and returns
`{ "ok": true, "tabId": 123, "url": "https://example.com/after-redirect", "status": "complete" }`.
On timeout the tab stays open and the result is
`{ "ok": false, "timedOut": true, "tabId": 123, "url": "...", "status": "loading" }`.

### POST /api/switch-tab
`{ "tabId": 123 }`

### POST /api/close-tab
`{ "tabId": 123 }`

### POST /api/wake-tab
`{ "tabId": 123 }` reloads a discarded tab. A tab that is not discarded returns
`{ "ok": true, "woken": false, "discarded": false }` at once. Otherwise the call
waits up to 15 s for the reloaded page to finish loading and answer the content
script, and then returns `{ "ok": true, "woken": true, "ready": true }`; the next
command can reach the page. Two results report `ready: false`:
- `{ "ok": true, "woken": true, "ready": false, "unscriptable": true, "state": { "tabId", "url", "status", "title", "httpStatus" } }`:
  the reloaded page loaded but refuses the content script, such as a JSON or PDF
  viewer or an error response with an empty body. Commands fail with `error_page`.
- `{ "ok": true, "woken": true, "ready": false, "timedOut": true, "state": { "tabId", "url", "status" } }`:
  the page did not become ready within 15 s; `state` is the last observed state.

## Interaction (POST)

### POST /api/click
`{ "selector": "button.submit" }` — also accepts `ref`, `position`.
Add `"wait": true, "timeout": 10000` to wait for the target first (see
[Actionability wait](#actionability-wait)); the other action endpoints accept the same keys.

### POST /api/trusted-click
`{ "selector": "button.submit", "focus": true }` or
`{ "coords": { "x": 640, "y": 400 }, "coordinateSpace": "screen" }` — optional
`tabId`, `title`, `process`. OS-level click on Windows and macOS; see
[`browser_trusted_click`](#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone).

### POST /api/type
`{ "text": "hello", "selector": "input" }`

### POST /api/fill
`{ "selector": "input#email", "value": "user@example.com" }`
Handles shadow DOM, React/Vue state, with automatic fallbacks. A web-component host
is filled through the first input, textarea or select in its open or closed shadow
root (closed roots through Firefox's `openOrClosedShadowRoot`). `/api/type` and
`/api/set-editable-content` also look in open and closed shadow roots. When `/api/fill`
or `/api/type` cannot reach a field, the result is a `not_fillable`
[action error](#action-errors).

### POST /api/scroll
`{ "direction": "down", "amount": 500 }` — optional container `selector` or `ref`;
`element` is an audit-only description.

### POST /api/hover
`{ "selector": "a.link" }`
Returns `{ "ok": true, "tag": "a" }`

### POST /api/find
`{ "query": "search text" }` or `{ "selector": "a.link" }`
Both `query` and `selector` are accepted (normalized to `query` internally).
Returns `{ "results": [{ "ref": "r0", "tag": "a", "text": "...", "selector": "...", "bounds": [...] }] }`

### POST /api/js
`{ "code": "document.title" }`
Returns `{ "result": "..." }`. Results capped at 50KB with truncation flag.
Page JavaScript is not subject to password redaction.

### POST /api/highlight
`{ "selector": "h1" }` — draws red border overlay on element.

### POST /api/clear-highlight
No body needed. Removes all highlights.

## Waiting (POST)

### POST /api/wait-for-element
`{ "selector": ".loaded", "timeout": 10000, "pollInterval": 200 }`
Polls until element appears. Returns `{ "ok": true, "found": true, "ref": "r0", "elapsed": 1200 }`

### POST /api/wait-for-result
`{ "code": "document.querySelector('.data')?.textContent", "timeout": 15000, "pollInterval": 500 }`
Polls JS expression until non-empty. Returns `{ "ok": true, "result": "...", "elapsed": 2000, "polls": 4 }`

### POST /api/page-text-by-tab-id
`{ "tabId": 123 }` — get text from a specific tab (not just active).

## Batch Execution (POST)

### POST /api/batch
Execute multiple commands sequentially or in parallel.
```json
{
  "commands": [
    { "action": "navigate", "url": "https://example.com" },
    { "action": "sleep", "ms": 2000 },
    { "action": "pageText" }
  ],
  "stopOnWarning": false
}
```

**Parallel sequences** (commands within each sequence run sequentially, sequences run concurrently):
```json
{
  "commands": [{
    "action": "parallel",
    "sequences": [
      [{ "action": "navigate", "tabId": 1, "url": "https://a.com" }, { "action": "pageText", "tabId": 1 }],
      [{ "action": "navigate", "tabId": 2, "url": "https://b.com" }, { "action": "pageText", "tabId": 2 }]
    ]
  }]
}
```
Note: every command that acts on a tab inside parallel sequences **must** have an explicit `tabId`, and two sequences may not write the same tab (see [Container progress](#container-progress)).

Returns `{ "results": [...], "progress": [...], "status": "ok", "summary": "..." }`; `progress`, `status` and `summary` are described in [Container progress](#container-progress).

**Batch actions:** navigate, newTab, closeTab, switchTab, click, type, fill, scroll, hover, find, js, pageInfo, pageText, pageTextByTabId, tabs, forms, dom, highlight, screenshot, waitForElement, waitForResult, sleep, parallel.

## Workflows (NEW)

### GET /api/workflows
List workflow files bundled in the `kitsune/workflows` package directory.
```json
{ "workflows": ["gmail-template-draft", "test", "wp-html"], "directory": ".../site-packages/kitsune/workflows" }
```

### POST /api/workflow
Execute a named workflow from the workflows directory.
```json
{ "name": "test", "variables": { "url": "https://example.com" } }
```
Workflows are `.md` files with JSON code blocks. Variables use `{{name}}` syntax.
Returns `{ "workflow": "test", "steps": 4, "results": [...], "progress": [...], "status": "ok", "summary": "..." }`. A workflow stops at its first failed step; `progress` marks the rest `skipped` (see [Container progress](#container-progress)).

## Cache Control (NEW)

### POST /api/cache
Control the response cache (pageText is cached for 5s by default; tab inventories are always read fresh).

Clear cache: `{ "action": "clear" }`
Check status: `{ "action": "status" }` → `{ "entries": 3, "ttl": 5 }`
Set TTL: `{ "action": "ttl", "seconds": 10 }`

## Error Handling

All endpoints return `{ "error": "message" }` on failure; classified failures add
`code`, `retryable`, `hint`, and `state` (see [Action errors](#action-errors)). The
matching MCP tools mark these results `isError: true` (see
[MCP error results](#mcp-error-results)). Common errors:
- `"Kitsune extension not connected. Open Firefox or Zen and make sure the Kitsune extension is loaded."` (`extension_disconnected`) — extension not loaded or browser closed
- `"Extension disconnected"` (`extension_disconnected`) — extension dropped mid-command (fails fast, no timeout wait)
- `"No active tab found"` (`browser_window_unavailable`) — no normal browser window the extension can use
- `"Command timed out after Ns"` (`timeout`) — extension didn't respond in time
- `"Cannot access browser internal page: about:..."` (`restricted_page`) — can't inject into restricted pages
