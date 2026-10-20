# Kitsune API Reference

Base URL: `http://127.0.0.1:47651`

## MCP tool surface

`KITSUNE_CAPS=core` is the default (30 tools); `KITSUNE_CAPS=all` registers
95 tools. The groups are `core`, `tabs`, `network`, `storage`, `parallel`,
`devtools`, `windows`, and `session`. A comma list selects those groups only.
See [Tools and capabilities](guide.md#tools) for the complete tool list per group.
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
| `browser_keypress` | `key, selector=None, modifiers=None, text=None, tab_id=None, ref=None, element=None` |
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
| Successful `browser_click`, `browser_type` (`type`/`fill`), `browser_select_option`, `browser_check`, `browser_keypress`, `browser_submit_form`, `browser_navigate` | Their post-action `snapshot` starts a new generation, like `browser_snapshot` |
| Navigation, reload, or back/forward to another document | Earlier refs fail as `stale_ref`/`other_document` |
| `browser_find`, `browser_forms`, `browser_wait_for(selector/ref)` | Add refs to the current generation; earlier refs stay valid |
| Failed actions, other reads, `browser_hover`, `browser_scroll`, `browser_type(mode="editable")` and other actions without a post-action snapshot | No change, unless the page itself removes the element (`element_removed`) |

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
- `hint`: a short recovery step. `state`: the current state that explains the
  failure, where available (for page targets: `tabId`, `url`, `status`).

| `code` | Meaning | `retryable` |
|---|---|---|
| `not_found` | No element matches the selector (also `No target`, `from/to element not found`) | true |
| `stale_ref` | The ref is no longer valid; `state.reason` is `element_removed`, `not_in_snapshot`, `snapshot_replaced` (a newer snapshot of this document), or `other_document` (the tab navigated, or the ref came from another tab) | true |
| `navigating` | The tab is loading: a `not_found`/`stale_ref` while loading (`state.cause`), or the page unloaded while handling the command | true before dispatch; false for writes after |
| `tab_closed` | The target tab no longer exists | true |
| `tab_unloaded` | The tab is discarded and its page cannot be reached; use `browser_wake_tab` | true |
| `restricted_page` | Browser-internal page (`about:`, `moz-extension:`, …) | true |
| `content_unavailable` | The page's content script could not be injected or did not answer | true before dispatch; false for writes after |
| `extension_disconnected` | The extension is not connected, reconnected before sending, or disconnected while waiting (`state.connected`) | true if not sent; false for writes after |
| `timeout` | The bridge sent the command but no reply arrived in time (`state.timeoutSeconds`); from the MCP server, the bridge did not answer | false for writes (MCP: POST) |
| `bridge_unavailable` | The MCP server cannot reach or start the bridge | true |

Writes are the bridge's read-only-mode write actions plus file-upload commits,
focus, and blur. For a read, the bridge always reports `retryable: true`.

Identity is checked before mutation. A missing or browser-internal target tab fails
before any message reaches a page. Refs encode their document: a ref used after
navigation, or on a different tab after a focus change, fails as
`stale_ref`/`other_document` instead of acting on another element. Without
`tab_id`, a command and its post-action snapshot target the tab that was active
when the command started. CSS selectors carry no identity, so pass `tab_id`
when focus can change.

The batch/`browser_retry` `retry` action stops after the first attempt that
returns `retryable: false`, with `reason: "not retryable"`, even if `onErrors`
matches. Other errors are retried as before.

Pointer actions (`browser_click` by selector/ref, `browser_double_click`,
`browser_hover`) still dispatch to a target that is not rendered (`hidden`,
`display: none`, `visibility: hidden`, or inside a `display: none` ancestor), but
the success result adds a warning. With `stopOnWarning`, a batch stops there.

```json
{ "ok": true, "tag": "input", "warning": { "code": "hidden_or_covered",
  "state": { "reason": "not_rendered" }, "hint": "... prefer its label or visible control ..." } }
```

Zero-size and transparent targets count as rendered. Covering overlays are not detected.

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
  have taken effect and must not be re-sent blindly. Inspect each entry. The
  container call itself is an error only when it fails as a whole (for example
  `bridge_unavailable`).
- Reads that describe state, such as `browser_status` with
  `extension_connected: false`.

HTTP bridge endpoints are unchanged; this applies only to MCP tool results.

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
- an input masked with `-webkit-text-security`;
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
sensitive values. Results and errors of write actions on sensitive fields
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
(`code`), typed text or stored secrets. These values are replaced with
`"[redacted]"`:

- `text` of `type` and `keypress`;
- `value` of `fill` and `setEditableContent`;
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

### `browser_snapshot(tab_id=None, selector=None, ref=None, max_depth=None)`

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

### `browser_type(selector="", text="", mode="type", clear=False, tab_id=None, ref=None, element=None)`

Replaces `browser_fill` and `browser_set_editable_content`.

| Mode | Behavior | Bridge endpoint |
|---|---|---|
| `type` | Type using events; `clear=True` clears first | `/api/type` |
| `fill` | Always replace the field value with native setter and input/change events; ignores `clear` | `/api/fill` |
| `editable` | Insert plain text into contenteditable/input/textarea; append by default, replace with `clear=True` | `/api/set-editable-content` |

Editable mode sends `format="text"`. Callers migrating from the old editor tool
should pass `clear=True` to retain its replace-by-default behavior. For trusted
HTML insertion use `browser_js`; the underlying endpoint still accepts `format="html"`
(both developer build only).

### `browser_form_fill(fields, tab_id=None)`

Fills several fields in one call via POST `/api/form-fill`. Each key is matched
as a CSS selector, then `name` attribute, label text, and placeholder; results
are returned per key, and one failing key does not stop the others.

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
Connection status and config.
```json
{ "status": "running", "extension_connected": true, "port": 47651, "ws_port": 47652, "screenshot_dir": "..." }
```

### GET /api/version
Bridge and extension version info.
```json
{ "bridge": "0.2.1", "api": "1", "extension_connected": true, "extension_version": "0.2.1", "extension_build": "dev", "extension": 22 }
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

## Interaction (POST)

### POST /api/click
`{ "selector": "button.submit" }` — also accepts `ref`, `position`.

### POST /api/trusted-click
`{ "selector": "button.submit", "focus": true }` or
`{ "coords": { "x": 640, "y": 400 }, "coordinateSpace": "screen" }` — optional
`tabId`, `title`, `process`. OS-level click on Windows and macOS; see
[`browser_trusted_click`](#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone).

### POST /api/type
`{ "text": "hello", "selector": "input" }`

### POST /api/fill
`{ "selector": "input#email", "value": "user@example.com" }`
Handles shadow DOM, React/Vue state, with automatic fallbacks.

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
Note: `navigate` and `switchTab` inside parallel sequences **must** have explicit `tabId`.

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
Returns `{ "workflow": "test", "steps": 4, "results": [...] }`

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
- `"Command timed out after Ns"` (`timeout`) — extension didn't respond in time
- `"Cannot access browser internal page: about:..."` (`restricted_page`) — can't inject into restricted pages
