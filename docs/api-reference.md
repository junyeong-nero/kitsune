# Kitsune API Reference

Base URL: `http://127.0.0.1:47651`

## MCP tool surface

`KITSUNE_CAPS=core` is the default; `KITSUNE_CAPS=all` registers
93 tools. The groups are `core`, `browse`, `read`, `inspect`, `tabs`, `automation`,
`network`, `admin`, `storage`, and `history`. A comma list selects those groups only.
`all` enables every group except `storage` and `history`, which read personal data and
must be named (`KITSUNE_CAPS=all,storage,history`); see
[History and bookmarks](#history-and-bookmarks).
See [Tools and capabilities](guide.md#tools) for the complete tool list per group.

**The `core` group is Playwright MCP's 25 core tools**, published verbatim from
`src/kitsune/playwright_tools.json`: `browser_close`, `browser_resize`,
`browser_console_messages`, `browser_handle_dialog`, `browser_emulate_media`,
`browser_evaluate`, `browser_file_upload`, `browser_drop`, `browser_find`,
`browser_fill_form`, `browser_press_key`, `browser_type`, `browser_navigate`,
`browser_navigate_back`, `browser_network_requests`, `browser_network_request`,
`browser_run_code_unsafe`, `browser_take_screenshot`, `browser_snapshot`,
`browser_click`, `browser_drag`, `browser_hover`, `browser_select_option`,
`browser_tabs`, `browser_wait_for`. Their arguments are Playwright's (`target` is a
snapshot ref such as `e12`, a CSS selector or one of the
[Playwright selector forms](#target-selectors) Kitsune resolves), they act on the current tab (writes
without one fail, see [No current tab](#no-current-tab)), and
`browser_emulate_media` and `browser_run_code_unsafe` answer `not_supported`; see
[Core tools](guide.md#core-tools) for how each maps onto the extension. The tool
sections below describe the Kitsune-owned tools and the bridge commands the core tools
use; Kitsune's former `core` tools are in `read` (`browser_page_info`,
`browser_page_text`, `browser_forms`, `browser_dom`, `browser_markdown`), `browse`
(`browser_scroll`, `browser_check`, `browser_submit_form`, `browser_forward`), `admin`
(`browser_status`) and `automation` (`browser_workflows`, `browser_workflow`). The ones
Playwright's replace (`browser_keypress`, `browser_js`, `browser_screenshot`, `browser_back`,
`browser_form_fill`, `browser_upload_file`, `browser_watch_console`,
`browser_console_logs`, `browser_double_click`, and Kitsune's own signatures of the
shared names) are no longer MCP tools; their sections document the bridge commands.

A call to a known tool that this configuration does not publish returns an MCP
error (`isError`) instead of FastMCP's `Unknown tool`. Its `hint` names the
settings that publish it; restart the MCP client after changing them:

```json
{ "error": "browser_scroll is not published by this MCP server", "code": "tool_not_published",
  "retryable": false,
  "hint": "Set KITSUNE_CAPS=browse,core in the MCP server environment, then restart the MCP client.",
  "state": { "group": "browse", "caps": ["core"] } }
```

`browser_status` adds `mcp_tools` to the bridge status: this MCP server's enabled
`caps` and the count of known tools it leaves `unpublished`, for example
`{"caps": ["core"], "unpublished": 79}`. With the [tool gateway](#tool-gateway) it also
has the `publish` groups and how many enabled tools are `unlisted` when a session
starts. The bridge is shared by every MCP server, so `GET /api/status` does not have it.
MCP tool names and parameters are separate from HTTP endpoint names and JSON
command actions. Task-mode requests restrict the bridge routes too (below).

### Capability enforcement

The MCP server sends its enabled groups (`all` expanded) in `X-Kitsune-Caps` on every
bridge request. The bridge maps every route and every container action to the groups
whose tools use it (`src/kitsune/capabilities.py`) and refuses the others. A route is
refused with HTTP 403; a container step (`batch`, `parallel`, `retry`, `broadcast`,
`if`/`while`/`try`/`sequence` blocks, workflow and resumed steps) fails with the same
error as its result, and its [progress](#container-progress) status is `failed`:

```json
{ "error": "cookies needs the storage capability", "code": "capability_restricted",
  "retryable": false,
  "hint": "Enable that group in this MCP server's KITSUNE_CAPS, then restart the MCP client.",
  "state": { "groups": ["storage"], "caps": ["automation", "core"] } }
```

Container routes need `automation`, and each step needs its own action's group;
actions the core tools also use (navigate, click, type, check, waits, screenshots, `js`, ...)
are `core`. `/`, `/api/status`, `/api/version` and `/view` are open to every client.
An unknown group in the header returns 400 `invalid_caps`. Requests without the header,
such as the `kitsune-bridge` command's, are not restricted.

### Tool gateway

With `KITSUNE_PUBLISH` set to fewer groups than `KITSUNE_CAPS` enables, `tools/list`
starts with the published groups plus three gateway tools, and the server advertises
`tools.listChanged`. They reach only tools `KITSUNE_CAPS` and `KITSUNE_MODE` permit. See
[Finding tools without a restart](guide.md#finding-tools-without-a-restart).

`browser_tools_search(query, limit=5)` ranks the enabled tools that `tools/list` did not
show at session start against the query words (name matches count most, then group
names, then descriptions; words many tools share count less). `limit` is 1 to 10.

```json
{ "tools": [ { "name": "browser_scroll", "group": "browse", "listed": false,
               "description": "…", "inputSchema": { … }, "annotations": { … } } ],
  "matches": 3, "hint": "Run a tool with browser_tools_call(name, arguments)." }
```

`listed` tells whether `browser_tools_publish` added it to this session's list. With no
match, or `query: ""`, `tools` is empty and `groups` maps each group to its tool names.

`browser_tools_call(name, arguments={})` runs the tool through the same path as a direct
call and returns that call's result: its content (text, images) and `isError`. A name
the server does not permit returns the direct call's `tool_not_published`; an unknown
name returns `unknown_tool`; a gateway tool returns `invalid_argument`. Its bridge
requests carry `X-Kitsune-Gateway-Tool`, which [audit entries](#audit-log) record.

`browser_tools_publish(names)` takes tool and group names and adds the permitted tools
among them to this session's `tools/list`, then sends `notifications/tools/list_changed`.
It returns `{"published": [...], "notified": bool}`; names already listed are a no-op.
Any name it cannot add, unknown or not permitted alike, refuses the whole call with
`tool_not_published` and `state.names`.

### Task modes

`KITSUNE_MODE=read|draft|act` selects both the MCP surface and per-request bridge
limits. Default: `act` (unchanged behavior). Case and surrounding whitespace are
ignored; invalid modes fail at MCP startup. Modes intersect with the capability
groups, including the named-only storage and history groups.

- `read`: read tools plus `browser_navigate` and `browser_scroll` (scroll needs
  `browse`). `browser_tab_list(new_tab=None)` replaces `browser_tabs`: it returns the
  same compact tab inventory as `browser_tabs(action="list")`. With `new_tab`, an
  HTTP(S) URL (or `about:blank`), it first opens that URL in a background tab, waits
  for the load and returns its `tabId`, final `url` and `status` with the inventory;
  later calls without `tab_id` act on that tab, as after `browser_tabs` new (#270). It
  cannot close or select tabs, and it never switches the user's view. Screenshots are
  excluded because they can write files.
- `draft`: read plus type/select-option/fill-form/check (check needs `browse`).
  Plain-text editor writes only. Forces ask-before-act checks; a sensitive field,
  payment-like page or uninspectable target returns `mode_restricted` before dispatch.
  Form-fill preflights the entire target set, sending only field keys for inspection.
- Neither restricted mode permits click, submit, keypress, dialog confirmation,
  agent JavaScript, policy changes, container calls or resume. No confirmation tokens
  are issued or accepted. Navigate and new tabs permit HTTP(S) or `about:blank`, not
  script, data or local-file URLs. Under `KITSUNE_PROTECT_USER_TABS`, a refused
  navigation of a user tab carries no token; its hint points to
  `browser_tab_list(new_tab=...)`. Existing global URL/readonly/ask policy can tighten this.

Restricted `tools/list` descriptions use their first paragraph, and schemas omit
`confirm_token`. Calling an excluded name returns the existing `tool_not_published`
MCP error, with `KITSUNE_MODE=draft` or `KITSUNE_MODE=act` in its hint, together with
any required caps changes. The error's `state` and `browser_status.mcp_tools`
include `mode` when restricted. `act` definitions and status fields are unchanged.

The MCP server sends `X-Kitsune-Mode: read|draft` on **every** bridge request. No header
means `act`. The bridge validates the header, canonicalizes route aliases before
checking access, and carries the mode through async dispatch without changing the
shared policy. A denied route/action answers HTTP 403 with
`{"error":"…", "code":"mode_restricted", "retryable":false, "hint":"…"}`; an invalid
header answers HTTP 400 with `code: "invalid_mode"`. `GET/POST /api/get-policy`
reports the shared policy plus the effective `mode`, `ask` and `confirmAllowed:false`
for restricted requests. Another authenticated client can still use act; this is
not a separate authentication boundary. Keep the bridge token private.

See [Task modes](guide.md#task-modes) for configuration, the eight-tool read
surface and site-event/autosave limitations. Exact token and task-success measurements
are external kitsune-benchmark work, not inferred from compact JSON character counts.

### Element targets

For single-element tools, prefer `ref` from `browser_snapshot` over a CSS
`selector`. When both are supplied, `ref` takes precedence, as for `browser_click`.
`element` is an audit-only human description, never a selector or fallback target.
A stale/disconnected ref returns a `stale_ref` error (see [Action errors](#action-errors));
take another snapshot to refresh it. See [Ref lifetime](#ref-lifetime) for which
events invalidate refs.
Existing positional selector arguments remain supported.

#### Target selectors

Actions, scoped snapshots (`browser_snapshot`, `browser_dom`) and element screenshots
resolve a target with one shared resolver. It accepts a ref, CSS (including shadow
trees), and this subset of Playwright's selectors; it is not Playwright's engine:

| Form | Matches |
|---|---|
| `e12` | The snapshot element |
| `text=Save`, `"Save"` | Lax `text=Save`: the smallest element whose whitespace-collapsed text contains it, ignoring case. Quoted `text="Save"` (also `"Save"s`, `'Save'`) is the whole text, case-sensitive; `"Save"i` is lax again |
| `text=/Sav(e\|ed)/i` | The smallest element whose whitespace-collapsed text the regular expression matches. Only flags may follow the last `/`; otherwise the value is lax text (`text=/api/users`) |
| `internal:text="Save"i`, `internal:text=/…/` | `getByText`: as `text=`, but the value is quoted or a regex |
| `role=button[name="Save"]` | Elements with that role (explicit or implicit) whose accessible name is the whole name, ignoring case; `[name="Save"s]` is case-sensitive, `[name*=…]`/`[name^=…]`/`[name$=…]` match part of it, `[name=/…/flags]` tests it with a regex. Also `[level=N]` and `[include-hidden]`; hidden and `aria-hidden` elements are skipped otherwise |
| `internal:role=button[name="Save"i]` | `getByRole`: as `role=`, but `"…"i` is a substring |
| `internal:label="Email"i` | `getByLabel`: elements whose `aria-labelledby` text, else `aria-label`, else `<label>` (for a form control) contains the text, ignoring case. `"Email"` and `"Email"s` are the whole label, case-sensitive; `/…/flags` is a regex |
| `:has-text("…")`, `:text-is("…")`, `:text("…")`, `:visible` | CSS pseudo-classes: contains the text (lax), whose whole text it is, the smallest element containing it, rendered |
| `a, b` | A CSS list, with or without the pseudo-classes above: the union of its alternatives |
| `a >> b` | Chains: `b` searched inside each match of `a`. A chain may start with a ref (`e12 >> xpath=..`, `e12 >> text=Save`) |
| `nth=N` | The Nth match so far (0-based; `-1` is the last); not first in a chain |
| `xpath=..`, `..` | The parent element; at the top of a shadow root, the shadow host. Only a bare `..` step leaves a shadow root: write `.. >> ..`, not `xpath=../..` |
| `xpath=//h2[text()='Stock']/..`, `//…`, `..…` | General XPath (`document.evaluate`), elements only; after `>>`, a leading `/` is relative to the earlier match, which may be inside a shadow root or same-origin frame. It does not cross into shadow roots or frames. Because XPath reads text and the `value` attribute unredacted, it is refused (`unsupported_selector`) when a document it would run in, including that document's shadow roots, holds a sensitive field with a `value` attribute or masked text |

Images, `<area>` and `<svg>` graphics are named as snapshots name images: by
`aria-labelledby`, `aria-label`, `alt` (an `<svg>`'s `<title>`), then `title` unless
`alt=""`. `<input type=image>` is a `button` named by its `alt`. Text and names are read
redacted, so masked text and sensitive values never match. Write actions stay
[strict](#strict-selectors) for every form. Anything else (other engines such as `id=`
or `data-testid=`, `:near()` and other layout pseudo-classes, Playwright pseudo-classes
inside `:has()`, a ref after the start of a chain, invalid CSS, XPath or regex) fails
with `unsupported_selector` (not retryable) before anything is dispatched. A valid
selector that matches nothing is `not_found`, for scoped snapshots too.

Regular expressions and XPath run on the page's main thread, as in Playwright, and are
not capped: a pathological pattern (such as `text=/(a+)+$/` on long text) can block the
page until the bridge command times out.

#### Strict selectors

A write action's CSS selector must name one element (#134). When it matches more
than one actionable element, the action fails with `ambiguous_target`
(`retryable: false`) and nothing is dispatched; `state.candidates` lists up to 5
of them (`ref`, `role`, `name`) and `state.matches` counts them. Pass a candidate's
`ref`, a more specific selector, or `first: true` (`"first": true` on the bridge
endpoint or batch command) to act on the first match as before. This is an
intended behavior change: these actions used to act on the first match silently.

- Write actions: `click`, `doubleClick`, `hover`, `drag` (both ends), `type`, `fill`,
  `setEditableContent`, `checkBox`, `selectOption`, `submitForm`, `keypress`,
  `focusElement`, `blurElement` and `scroll` with a selector, the `uploadFile` target,
  and each `formFill` key (a per-key error). Labels and placeholders count every
  matching field, including duplicates in the same tree and across shadow roots.
  Batch, parallel and workflow steps follow the same rule; `clickAndWaitNavigation`
  also accepts the opt-in. MCP: `first=True` on `browser_click`, `browser_double_click`,
  `browser_hover`, `browser_drag`, `browser_type`, `browser_check`,
  `browser_select_option`, `browser_submit_form`, `browser_keypress`,
  `browser_focus`, `browser_blur`, `browser_scroll`, `browser_trusted_click`,
  `browser_click_and_wait_navigation`, `browser_upload_file` and `browser_form_fill`.
  For an individual match, use a candidate ref or a CSS `:nth-of-type(...)` selector;
  there is no separate `nth` parameter.
- Actionable: enabled, and not `<input type=hidden>`; for pointer actions (`click`,
  `doubleClick`, `hover`, `drag`, trusted click) also rendered, as in the
  [actionability wait](#actionability-wait). Value actions count hidden native
  inputs, which they can act on. A selector with one actionable match still acts on
  its first match, as before.
- Upload without a selector checks all `input[type=file]` controls in the document
  (including hidden ones); several actionable inputs fail unless `first=True`.
  This preserves upload's document-only lookup. Explicit `mode="drop"` without a
  selector still drops on the body; it does not select a file input.
- Refs and coordinates name one element and are unaffected. Read tools
  (`browser_html`, `browser_bounds`, `browser_wait_for`, screenshots, …) keep
  first-match semantics. Blur without a target still blurs the active element;
  scroll without a target still scrolls the window with a viewport-center fallback.
- With `wait`, an ambiguous selector fails at once instead of waiting.
- Under [ask-before-act](#ask-before-act), clicks (including trusted and navigation
  clicks) and Enter keypresses check ambiguity before offering confirmation.
  `first=True` is honored both during inspection and dispatch.

These tools also accept refs (all new parameters are optional):

| MCP tool | Parameters |
|---|---|
| `browser_keypress` | `key, selector=None, modifiers=None, text=None, tab_id=None, ref=None, element=None, wait=False, timeout=10000` |
| `browser_submit_form` | `selector=None, tab_id=None, ref=None, element=None` |
| `browser_scroll` | `direction="down", amount=500, tab_id=None, selector=None, ref=None, element=None, first=False` |
| `browser_blur` | `selector=None, tab_id=None, ref=None, element=None, first=False` |
| `browser_html`, `browser_bounds` | `selector="", tab_id=None, ref=None, element=None` |
| `browser_computed_style` | `selector="", properties=None, tab_id=None, ref=None, element=None` |
| `browser_click_and_wait_navigation` | `selector="", timeout=15000, tab_id=None, ref=None, element=None, first=False` |
| `browser_markdown` | `selector=None, tab_id=None, ref=None, element=None` |

`browser_keypress` accepts characters, Space/Spacebar, Enter/Return, Escape/Esc, ArrowUp/Down/Left/Right (or Up/Down/Left/Right), Tab and Backspace; `text` explicitly inserts text.

Synthetic key events have no browser default, so in an input, textarea or editor
keypress applies the key's default edit itself (#267), unless the page cancels
`keydown`, `keypress` (fired for a typed character and Enter) or `beforeinput`: a
character or Space types at the caret, replacing a selection; Backspace/Delete remove
the selection or the character before/after the caret; arrows, Home and End move the
caret, and with Shift extend the selection; Control/Meta+A selects all. Words are
Alt+arrow/Backspace/Delete on macOS and Control+ them elsewhere; on macOS
Meta+Left/Right/Backspace/Delete reach the line boundary and Meta+Up/Down the
whole text, elsewhere Control+Home/End. Up/Down move between a textarea's own lines
(not its wrapped ones). Enter adds a line to a textarea, a paragraph to an editor
(Shift+Enter a line break), and in a text input submits its form as a browser's
implicit submission does: it clicks the form's default button, or submits a form
that has none and no other text input (#257). Both go by form ownership, not
containment: a control's `form` attribute associates it with that form, wherever it
is in the document, and the default button is the first submit button or image input
the form owns in document order (#288). A disabled default button submits nothing.
The result reports:

- `key`: the key with its modifiers, e.g. `Shift+ArrowLeft`.
- `effect`: what changed: an input type such as `insertText`, `insertParagraph`,
  `deleteContentBackward` or `deleteWordBackward`; `moveCaret`, `extendSelection`,
  `selectAll` or `implicitSubmit`; `null` when nothing changed (End at the end, Enter
  in an input without a form, a key outside a field).
- `prevented: true`: the page cancelled the key, or in an editor the edit's
  `beforeinput`, so its default was not applied (#289). `effect` is then `null`.
- `note`: a default Kitsune does not apply: Tab does not move focus, Enter and Space
  do not activate buttons or links (click them), keys do not scroll the page, and
  clipboard and undo shortcuts do nothing.

Without selector/ref, keypress targets the active element (or body), blur targets
the active element, submit-form submits the form of the field last filled or
focused, else the only form (see [`browser_submit_form`](#browser_submit_formselectornone-tab_idnone-refnone-elementnone)), and Markdown reads the
whole page. Submit-form can target a form or an element inside it. Scroll accepts
an explicit container selector/ref; otherwise it scrolls the window and falls
back to a scrollable container at viewport center. HTML, bounds, computed-style,
and click-and-wait-navigation require a selector or ref for a successful action.
See `browser_wait_for` below for its element condition.

### Ref lifetime

A short ref such as `e12` names one attached node in one document of one tab.
Observations reuse the same identity while the node stays attached, even if it is
renamed or reparented. Tab/document identity is tracked privately, not encoded in the
public ref. Indexes increase across observations and tabs and never alias another node.
The extension persists reserved indexes across reloads, but not ref mappings: after
an extension reload, take a new snapshot (unknown old aliases report `not_in_snapshot`).
Legacy `s<generation>e<index>` inputs are still accepted; a generation now identifies
one document/content injection, not a snapshot.

| Call/event | Effect on refs in that tab |
|---|---|
| `browser_snapshot` (also scoped), `browser_dom`, `browser_find`, `browser_forms`, `browser_wait_for(selector/ref)` | Reuse attached-node refs and add identities for newly observed nodes; earlier attached refs stay valid |
| Full or delta post-action snapshots | Reuse attached-node refs; a full observation does not invalidate earlier refs. An `expect` failure can still carry the action's snapshot and target_ref |
| Node detachment/replacement or a frame's document navigation/removal | The issued node ref fails as `stale_ref`/`element_removed`, including after detached entries have been pruned. A replacement node gets a different ref |
| Navigation, reload, back/forward to another document or content reinjection | Old refs fail as `stale_ref`/`other_document`; aliases unknown after an extension restart report `not_in_snapshot` |
| Other reads/actions or failed observations | No identity change unless the page itself detaches nodes or changes document |

Closing a tab or starting a document load prunes its short-ref mappings without
resetting the index counter. Same-document URL changes alone do not prune refs.

Snapshots in one tab do not affect refs in another tab. Pass `tab_id` when more
than one tab is in use: a ref sent to the wrong tab fails as `other_document` and
never acts on another element.

After an action, use the refs in its `snapshot`. `target_ref` is the ref of
the acted-on element (absent for `browser_navigate`, or when that element is gone),
so a follow-up such as `browser_keypress(key="Enter", ref=target_ref)` needs no
extra snapshot. Only emitted lines carry new refs: when `snapshot_truncated`
is set and the element you need is missing, `snapshot_hint` provides scope guidance
and explains if the target and its ancestor context could not fit. When the target
is omitted, `target_ref` is omitted too. If `snapshot_error` replaces the snapshot,
call `browser_snapshot` before using a ref the result did not show. With the post-action
snapshot disabled (`KITSUNE_POST_ACTION_SNAPSHOT=0`, or `includeSnapshot: false`
in a batch), actions leave refs unchanged.

Recovering without repeating a consequential action:

- `stale_ref` and `not_found` fail before anything is dispatched to the page.
  Take a new `browser_snapshot` of the same `tab_id`, find the element again,
  and retry with its new ref. Do not fall back to another element by its
  `element` description.
- For `stale_ref`, `state.reason` distinguishes `element_removed`, `not_in_snapshot`
  (unknown/unissued alias) and `other_document`. Observe the intended tab/document
  again and select its current node; do not infer that a replacement is the old node.
  Newer observations alone do not cause stale refs; no `latestObservation` or
  `currentRef` substitution is needed or returned.
- To get refs after a successful write, use its `snapshot`/`target_ref` (with
  `snapshot_delta`, earlier refs also stay valid) or call `browser_snapshot`. Never
  repeat the write.
- After a failure with `retryable: false` (a write whose outcome is unknown),
  observe first (`browser_snapshot`, `browser_page_info`, `browser_wait_for`) and
  act again only if the page shows the effect did not happen.
- In a batch, earlier commands may already have run. Resume from the failed
  command instead of re-running the whole batch.

Refs remain stable across snapshots while the same node stays attached to its live
document. Detached-node entries are pruned; ID-only removal history lasts for that
content injection. Unobserved detach-and-reinsert of the same node is not tracked.

#### Post-action snapshots

A successful `browser_click`, `browser_type` (`type`/`fill`), `browser_select_option`,
`browser_check` or `browser_keypress` returns a delta: `snapshot_delta: true` and a
`snapshot` with only the lines the action changed, in document order under their
ancestors. Right before acting, the content script records the page's snapshot lines;
afterwards the delta lists the acted-on target and every line that record lacks: new or
newly shown elements (an opened menu's options, a toast, autocomplete suggestions) and
elements whose name, value or state changed. Unchanged parts of the page, such as a
header above the target, are left out; removed elements are not listed. A delta is
capped at 4,000 characters like a full snapshot (`snapshot_truncated`); when truncated,
`snapshot_hint` gives scope guidance, including when the target/context did not fit
and `target_ref` was omitted. A same-document URL or title change (`history.pushState`)
adds `url` or `title`:

```json
{ "ok": true, "tag": "button", "text": "Review type", "snapshot_delta": true,
  "url": "https://shop.example/app/reviews?filter=open", "target_ref": "e41",
  "snapshot": "- main \"\" [ref=e40]\n  - button \"Review type\" [ref=e41] [expanded=true]\n  - listbox \"Review type\" [ref=e90]\n    - option \"Negative\" [ref=e91]" }
```

An empty delta means nothing visible changed apart from removals. Use `expect` to
verify an outcome, or `browser_snapshot` for the whole page.

The snapshot is full (no `snapshot_delta`; existing attached refs stay valid) for
`browser_submit_form` and `browser_navigate`, which usually load another document,
when the action loaded another document before the snapshot was taken (it has no
record to compare with), and on request: `"snapshot": "full"` on an HTTP or batch
command, or `KITSUNE_POST_ACTION_SNAPSHOT=full` in the bridge environment for every
action. A full post-action snapshot prioritizes the acted-on target and its ancestor
context when that block fits, then fills the rest in document order.

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
  The exceptions are `error_page`, `ambiguous_target`, `user_tab`, `permission_required`,
  `capability_restricted`, `mode_restricted` and `unknown_action`: nothing reached the page,
  and repeating cannot succeed until the tab navigates, the target is named uniquely, the user
  approves the close or navigation or grants the permission, the client's `KITSUNE_CAPS` or
  `KITSUNE_MODE` permits the action, or the step names a batch action.
- `hint`: a short recovery step. `state`: the current state that explains the
  failure, where available (for page targets: `tabId`, `url`, `status`).

| `code` | Meaning | `retryable` |
|---|---|---|
| `not_found` | No element matches the selector (also `No target`, `from/to element not found`; a scoped `browser_snapshot`/`browser_dom` reports `Selector not found: …` with `state.selector`); after an [actionability wait](#actionability-wait), `state` has `timeoutMs` and `elapsed` | true |
| `not_fillable` | `browser_type` (`type`/`fill` modes) or a `browser_form_fill` key found no field to write. The target is not a field (`fill` and `browser_form_fill`: input, textarea or select; `type`: input, textarea or `contenteditable` region) and has none in its open or closed shadow roots, searched through nested hosts up to 8 roots deep (`fill` and `browser_form_fill` also look in its light DOM). Nothing was written. `state.tag` is the target's tag; `state.reason` is `no_field`, or `shadow_depth` when its shadow roots nest deeper than 8 | true |
| `ambiguous_target` | The command names no single target, so nothing was dispatched: a write action's selector matches several actionable elements (`state.selector`, `state.matches`; see [Strict selectors](#strict-selectors)), a `browser_form_fill` label or placeholder key matches several actionable fields (`state.field`, `state.matches`), or `browser_submit_form` without a target found several forms and no filled field naming one (`state.forms`). `state.candidates` lists up to 5 of them with a `ref` to target one | false |
| `not_actionable` | An [actionability wait](#actionability-wait) timed out on a target that exists but never rendered (`state.reason: "not_rendered"`, `timeoutMs`, `elapsed`); nothing was dispatched | true |
| `unsupported_selector` | The shared target resolver rejected unsupported/invalid syntax (see [Target selectors](#target-selectors)) before dispatch; `state.selector` echoes the selector and `hint` lists accepted forms. Applies to actions, scoped snapshots and element screenshots | false |
| `stale_ref` | The ref is no longer valid; `state.reason` is `element_removed`, `not_in_snapshot` (unknown/unissued ref), or `other_document` (the tab navigated, or the ref came from another tab) | true |
| `navigating` | The tab is loading: a `not_found`/`stale_ref` while loading (`state.cause`), or the page unloaded while handling the command. Also a page that refused the content script within 5 s of a wake or reload without an error status, or whose 2xx response arrived only while the command waited for it: it may be an old document about to be replaced | true before dispatch; false for writes after |
| `tab_closed` | The target tab no longer exists | true |
| `tab_unloaded` | The tab is discarded and its page cannot be reached; use `browser_wake_tab` | true |
| `restricted_page` | Browser-internal page (`about:`, `moz-extension:`, …) | true |
| `content_unavailable` | The page's content script could not be injected or did not answer. `state.reason: "no_receiver"`: the injection succeeded but no ping from the current content script was answered within 0.5 s, so nothing was sent | true before dispatch; false for writes after |
| `error_page` | A loaded `http(s)` page refused the content script: the browser shows its own document, such as an error response with an empty body. `state` adds `title` and, when the extension saw the response, `httpStatus`. Nothing was dispatched, but reads fail the same way until the tab navigates; for a 4xx/5xx status, `browser_page_text` instead returns an empty result (`text: ""`, `empty: true`, `httpStatus`, `tabId`, `url`, `title`) | false |
| `extension_disconnected` | The extension is not connected, reconnected before sending, or disconnected while waiting (`state.connected`) | true if not sent; false for writes after |
| `timeout` | The bridge sent the command but no reply arrived in time (`state.timeoutSeconds`); from the MCP server, the bridge did not answer. An element or text wait whose page reads all went unanswered within its `timeout` (see `browser_wait_for`) | false for writes (MCP: POST) |
| `bridge_unavailable` | The MCP server cannot reach or start the bridge (`state.exitCode`, `state.logExcerpt` when it exited at startup; `state.reason`, `state.pid` when it runs but does not answer) | true |
| `bridge_disconnected` | From the MCP server: the bridge closed the connection before its reply. `state.requestSent` is `false` when the request could not be sent in full, so the command did not run | true if not sent; false for writes (MCP: POST) after |
| `token_mismatch` | The bridge rejected the MCP server's token (`state.port`, `state.tokenSource`) | true |
| `port_conflict` | A port is held by another program, answers but not as a Kitsune bridge, or differs from the extension's (`state.port`, `state.reason`) | true |
| `extension_version_mismatch` | The extension does not know the command, and its version differs from the bridge's (`state.extensionVersion`, `state.bridgeVersion`) or it is a developer build edited but not reloaded (`state.reason: "stale_developer_build"`) | true |
| `browser_window_unavailable` | No active tab: the extension sees no normal window it can use | true |
| `host_permission_denied` | Firefox does not let Kitsune script the tab's site (`state.reason`: `not_granted`, `restricted_domain`) | true |
| `permission_required` | A [history or bookmark lookup](#history-and-bookmarks) needs an optional browser permission the user has not granted (`state.permission`: `history` or `bookmarks`). Only the user can grant it, on Kitsune's options page; the agent cannot. Also returned, with a reload hint, when the permission is granted but the running extension has not picked it up | false |
| `confirmation_required` | [Ask-before-act](#ask-before-act) held a sensitive action; nothing ran (`state.confirmToken`, `state.reason`) | true |
| `user_tab` | `browser_tabs` close on a tab the agent did not open, or `browser_close_window` on a window with one; it stays open. With [protected user tabs](#protected-user-tabs), also navigate, back or forward on such a tab; it stays where it is. A direct call's `state.confirmToken` closes or navigates it once the user approves (see [Agent-owned tabs](#agent-owned-tabs)) | false |
| `user_active` | The user is using the tab (`state.reason`: `input`, `typing`, `navigating`); the write or navigation did not run (see [User activity](#user-activity)) | true |
| `outcome_not_observed` | The action was dispatched, but the outcome given in `expect` did not happen in time (see [Action outcomes](#action-outcomes)) | false for writes |
| `no_current_tab` | From the MCP server: a write or navigation names no tab and the agent has no current tab (`state.tool`, `state.action` for a container step); nothing was sent (see [No current tab](#no-current-tab)) | false |
| `tab_required` | A `parallel` step that acts on a tab has no `tabId` (`state.sequence`, `state.index`); nothing was dispatched (see [Container progress](#container-progress)) | true |
| `tab_owned` | Two `parallel` sequences write the same tab (`state.tabId`, `state.owner`); nothing was dispatched | true |
| `invalid_tab_id` | A batch, workflow or parallel step names a `tabId` that is not a tab id (`0`, `""`, an unresolved `${$N.tabId}`); it was not sent to the active tab instead | true |
| `unknown_action` | A container step's `action` is not a batch action, so nothing was dispatched (`state.action`). When a batch action is close to it, ignoring case, `state.suggestion` and the hint name it (`html` → `getHTML`) | false |
| `container_timeout` | A batch or workflow reached its deadline; steps in progress may have taken effect and later steps did not start | false |
| `tab_claimed` | A resumed `parallel` sequence writes a tab that another call's `parallel` in progress writes (`state.tabId`, `state.call`); nothing in that sequence was sent (see [Checkpoint and resume](checkpoint-resume.md#parallel)) | true |
| `checkpoint_not_found`, `resume_token_invalid`, `workflow_changed`, `observed_invalid`, `observed_required`, `resume_failed` | `browser_resume` refused or stopped; see [Checkpoint and resume](checkpoint-resume.md#errors) | `observed_*`: true; others: false |

The connection codes (`bridge_unavailable`, `token_mismatch`, `port_conflict`,
`extension_disconnected` before sending, `extension_version_mismatch`,
`browser_window_unavailable`, `host_permission_denied`) are explained under
[Connection diagnosis](#connection-diagnosis).

Writes are the bridge's read-only-mode write actions plus file-upload commits,
focus, and blur. For a read, the bridge reports `retryable: true`, except for
`error_page` and `permission_required`, where repeating cannot succeed.

Identity is checked before mutation. A missing or browser-internal target tab fails
before any message reaches a page. Refs encode their document: a ref used after
navigation, or on a different tab after a focus change, fails as
`stale_ref`/`other_document` instead of acting on another element. Without
`tab_id`, a command and its post-action snapshot target the tab that was active
when the command started. CSS selectors carry no identity, so pass `tab_id`
when focus can change.

If a content receiver disappears between the readiness ping and command delivery
(for example, just after a wake), the extension rechecks the same tab and its content
script, reinjecting if needed, and retries delivery once. This applies only to Gecko's
exact missing-receiver error, which proves the command was not dispatched. Closed
ports, missing responses and page errors are never automatically replayed this way.

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
- Container results: `browser_batch`, `browser_parallel`, `browser_broadcast`,
  `browser_workflow` and `browser_resume` report each command's outcome in `results`. A failed command
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
Read-only mode refuses `browser_load_session`, a cookie and storage write, and allows
`browser_save_session` like cookie reads. The URL `allow`/`deny` policy applies to
every URL a session saves or restores; a refused URL fails the call before anything runs.
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
| `extension_disconnected` | The bridge runs, but no extension is connected. After the MCP server starts or replaces the bridge, its first tool call other than `browser_status` waits up to 16 s for the extension before it is sent, and fails with this code only if none connects; nothing is sent twice | bridge | Open the profile where Kitsune is installed and enable it; load a developer build again after a browser restart; it reconnects within 15 s of a bridge start |
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
`expect.timeout` ms (default 5000) for that outcome in the same tab. An MCP tool called
without `tab_id` sends the current tab (the one `browser_tabs` last opened or selected),
and without one it is refused ([No current tab](#no-current-tab)); an HTTP request with
no tab at all is resolved to the active tab first and sent to that tab, so the action and
the check cannot target different tabs after a focus change.
Page conditions (`selector`, `value`, `text`) and `new_tab` are single reads that the
bridge repeats until the deadline. They do not depend on the page's timers, which
browsers throttle in background tabs. A `url` or `url_changed` check waits in the
extension, on tab events, until the last check. Either way, each check must answer
before `expect.timeout`, including its wait behind other commands on the tab, and no
check starts after it. The last check starts 100 ms before the deadline (at once for a
shorter `expect.timeout`), so that is how long it has to answer. The check therefore ends
near `expect.timeout` (`elapsed` is measured, not clamped).

`expect.timeout` bounds only that observation, which starts when the action's reply
arrives. Before it come two steps with their own limits: resolving a missing tab, plus
the one read that `url_changed` or `value_changed` takes before dispatch, and then the
dispatch, which the action's own timeout bounds. After an unmet observation, the reads
for the failure's `state`, `opened_tabs` and click hint share a 2 s allowance, and none
starts once it is used up. An action with `expect` therefore takes its dispatch time
plus at most `expect.timeout`, and 2 s more when the outcome is not met.

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
the page may navigate or replace nodes before an outcome is observed. An invalid `expect` is
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
`true`.

A check that does not answer before `expect.timeout` is not an observation; its late
reply, met or an error, is ignored. If the check in progress at the deadline does not
answer, `outcome.observationIncomplete` is `true`, `observed` is the last completed
check, and the error adds that the check in progress did not finish. If no check
answered at all, `observed` has `code: "timeout"` and the error says whether the
outcome happened is unknown. Neither case means the action failed or is safe to
repeat: it was dispatched, and `retryable` stays `false`. A `state` with only `tabId`
means the tab could not be read within the allowance. A hint that needs a read the
allowance had no time for (an opened tab, a click target still visible) is left out. For `browser_click`, when the clicked element (the result's `target_ref`, or
the request's ref or selector if no post-action snapshot was taken) is still visible
after the wait, the hint adds that the page may accept only trusted input and that
`browser_trusted_click` (in the `browse` capability) is the alternative. That is a
possibility, not a diagnosis: the page may also be slow, or expect something else.
Kitsune never retries the click or switches to an OS click on its own. A coordinate
click with no `target_ref`, or a target that is gone, gets no such hint.
Sensitive fields are never read back, so a `value` outcome on one is reported
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

These tabs are agent-owned (see [Shared browser](#shared-browser)): Firefox shows
such a tab, and the extension shows the user's tab again at once. The list is read
after the post-action snapshot. A new tab starts on `about:blank`, so
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
opens the link's own URL with `tabs.create` in the background, in the clicked tab's window
and container, with the clicked tab as opener, and marks it in `opened_tabs`:

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
window and OS permission (macOS: Accessibility), and its result does not list `opened_tabs`;
the tab it opens is not agent-owned.

### Shared browser

The user and the agent use one browser at the same time. Agent work never takes the
user's focus, never switches their view as a side effect, and does not act over what
the user is doing (#139). Three rules follow:

1. **Never take focus.** Tabs the agent opens open in the background, and no capture
   or action shows a tab or focuses a window unless the call exists to do that
   (`browser_tabs` select, `browser_focus_window`).
2. **Agent-owned tabs.** The extension tracks the tabs the agent opened; only those
   close without the user's approval.
3. **Don't fight the user.** On a tab the user is using, agent writes and navigations
   return `user_active` instead of acting.

#### Never taking focus

Every place that could show a tab or focus a window:

| Site | Before | Now |
|---|---|---|
| `browser_tabs` new, batch/workflow `newTab`, session save/load and tab-pool tabs (`tabs.create`) | opened in the foreground | `active: false`: opens in the background |
| Link fallback for popup-blocked links ([below](#popup-blocked-links), `tabs.create`) | foreground | `active: false` |
| `duplicateTab` (`tabs.duplicate`) | showed the duplicate | `active: false` |
| `createWindow`, `detachTab` (`windows.create`) | focused the new window | `focused: false`, and the focus goes back to the user's window right away in case the browser focused the new window anyway (see [Agent windows](#agent-windows)) |
| Tabs a page opens on an agent action (`target="_blank"`, `window.open`) | Firefox shows them | the extension shows the tab the user saw again at once (`tabs.onActivated`'s `previousTabId`) and gives a popup window's focus back to the window the user had focused; the new tab flickers for a moment. Firefox decides to show them, so this is an undo, not a prevention |
| `browser_screenshot` viewport with `tab_id` | selected the tab and focused its window, and never restored them | `tabs.captureTab` captures the tab in place |
| Element and full-page screenshots | showed the tab during capture, then restored it | `tabs.captureTab`; nothing is shown |
| `browser_trusted_click` on a tab that is not shown | clicked whatever tab was shown | shows the tab for the click, then shows the user's tab again 300 ms after it (unless the user moved elsewhere meanwhile) |
| `browser_trusted_click(focus=True)` | foregrounds the browser (OS) | unchanged: an explicit opt-in |
| `browser_tabs` select, batch `switchTab` (`tabs.update({active: true})`) | switched the view | unchanged, but only for an explicit request; refused with `user_active` while the user is using the tab they would leave |
| `browser_focus_window`, batch `focusWindow` (`windows.update({focused: true})`) | focused the window | unchanged, but only for an explicit request; refused with `user_active` while the user is using the focused window's tab |
| `wakeTab`, `navigate`, reload, back/forward | | never showed a tab; unchanged |

The OS click of `browser_trusted_click` still moves the real cursor for the click
(it goes back afterwards unless `restore_pointer=False`) and focuses the window it lands
in; the bridge cannot give OS focus back to another application.

**Changed behavior:** `browser_tabs` new (`POST /api/new-tab`, batch `newTab`) no longer
shows the new tab. Pass its `tabId` as `tab_id` to act on it. A caller that relied on
the new tab becoming the active tab must select it explicitly, and should do so only
when the user asked to see it. Commands without `tab_id` still target the tab the user
sees, which is not the new tab.

#### Agent-owned tabs

`browser_tabs` list (`GET /api/tabs`) marks every tab with `owned`: `true` for tabs the
agent opened (`browser_tabs` new, `duplicateTab`, the tabs `createWindow` asked for,
session and pool tabs, the link fallback, and tabs a page opened on an agent action, which
are also listed in [`opened_tabs`](#opened-tabs), and the agent window's synced copies
described in [Agent windows](#agent-windows)), `false` for the user's tabs.

`browser_tabs` close (`POST /api/close-tab`, batch `closeTab`) closes an owned tab. A
user tab stays open, and the result is the error `user_tab` (`retryable: false`:
repeating the same call fails the same way; `state`: `tabId`, `url`, `title`). On a
direct call the refusal carries a confirm token, issued and spent like
[ask-before-act](#ask-before-act)'s whether or not ask mode is on:

```json
{ "error": "Tab 12 was not opened by the agent, so it was not closed", "code": "user_tab", "retryable": false,
  "hint": "Nothing was closed: the agent did not open this tab. Leave it open, or ask the user to approve closing it, ...",
  "state": { "tabId": 12, "url": "https://mail.example/", "title": "Inbox",
             "confirmToken": "…", "expiresInSeconds": 120 } }
```

After the user approves, repeat the identical call with `confirmToken` (MCP:
`confirm_token`) within 120 s; it closes that tab once. The token is bound to the
call's parameters (`tabId`, `force`), works once, and only on a direct call: batch,
workflow and parallel steps never approve a close and get no token. A rejected token
adds `state.tokenRejected` (`used`, `expired`) and a new token. With `force`, the
extension disarms `beforeunload` only once it agreed to close the tab, so a refused
close leaves the user's page as it was.

Closing a window closes its tabs, so `browser_close_window` (`POST /api/close-window`,
batch `closeWindow`) refuses a window with any tab the agent did not open the same way
(`user_tab`, `state.windowId`, `state.tabIds`: its user tabs), with the same approval.
The browser's copies of other windows' tabs in a `createWindow` window do not count (see
[Agent windows](#agent-windows)).

Ownership lives in the extension's background page. A bridge restart or reconnect
keeps it. An extension reload or update, or a browser restart, forgets it; the tabs
the agent opened before then count as user tabs, so closing them needs approval.
The extension also cannot tell an agent action's tab from one the user opened from the
same tab within 2 s of that action; such a tab counts as the agent's.

#### Protected user tabs

Ownership, permission to close and permission to navigate are three different things.
Ownership (`owned`) only records who opened a tab. Closing a user tab always needs the
user's approval (above). Navigating a user tab is allowed by default: an agent the user
asked to work in their tab may do so.

A task that must keep the user's tabs where they are, for example "preserve every
starting tab's URL", should open extra material with `browser_tabs` new and pass the
returned `tabId` as `tab_id` to every later call. A call without `tab_id` acts on the
tab the user sees, which is usually a starting tab. Do not reuse a starting tab
just because it is active.

To enforce this for one MCP client, set `KITSUNE_PROTECT_USER_TABS=1` in its MCP
server environment (opt-in, off by default). The MCP server sends it with every bridge
request (`X-Kitsune-Protect-User-Tabs: 1`), so it covers that client's session only.
Other clients sharing the bridge keep their own setting. Then `browser_navigate`,
`browser_back` and `browser_forward` (`POST /api/navigate`, `/api/back`,
`/api/forward`, and batch, workflow and parallel `navigate`, `goBack`, `goForward`)
on a tab the agent did not open, including the active tab when `tab_id` is omitted,
return `user_tab` (`retryable: false`) without navigating:

```json
{ "error": "Tab 12 was not opened by the agent and this session protects user tabs, so it was not navigated",
  "code": "user_tab", "retryable": false,
  "hint": "Nothing navigated: this session protects tabs the agent did not open. Open extra material with browser_tabs action=new and pass its tabId as tab_id, ...",
  "state": { "tabId": 12, "url": "https://mail.example/", "title": "Inbox",
             "confirmToken": "…", "expiresInSeconds": 120 } }
```

The command is never redirected to another tab. Tabs the agent owns navigate as before. After the user
approves, repeat the call with `tab_id` set to `state.tabId` and `confirmToken` (MCP:
`confirm_token`) within 120 s. The token navigates that tab to that URL once. It is
bound to the call's parameters and the refused tab, so a call without `tab_id` cannot
carry it to whatever tab is active later. Only direct calls get and accept a token:
batch, workflow and parallel steps are refused without one. The token goes back to the
agent, so it proves an explicit second step, not that the user approved: an agent can
repeat the call with it without asking. For real approval, have the MCP client prompt
before calls that carry `confirm_token`.

Limits: only these server-mediated navigations are guarded. A user tab's URL can still
change through link clicks (`browser_click`, `clickAndWaitNavigation`), form submits,
Enter in a field, agent JavaScript, redirects, and navigation the page starts itself.
Reload keeps the URL and is not guarded. This is not comprehensive URL preservation.
Ownership is the extension's, as above: after an extension reload or a browser restart,
the tabs the agent opened before count as user tabs and are protected too.

#### Agent windows

Agent work stays in background tabs of the user's window. Only an explicit
`browser_create_window` or `browser_detach_tab` (batch `createWindow`, `detachTab`) opens a
window; these tools are in the `tabs` capability, which the default tools leave out.
Nothing else calls `windows.create`. A popup window that a page opens on an agent action is
the page's own (see [Never taking focus](#never-taking-focus)).

**Focus.** `windows.create` passes `focused: false`, but some browsers ignore it: Zen (on
Firefox 158) focuses the new window even with its window sync off. So right after it
creates a window, the extension gives the focus back to the browser window the user had
focused, and does so again if the browser focuses the new window within 1.5 s. The new
window can flicker for a moment. If no browser window had focus (the user was in another
application), nothing is focused. If the user clicks into the new window within those 1.5 s,
the focus goes back to their previous window. `browser_focus_window` on the new window
is never undone.

**Zen's window sync** (`zen.window-sync.enabled`, on by default) mirrors tabs between
windows. A new window gets a lazy copy of each tab of the other windows, and each other
window gets a lazy copy of the new window's tab. Zen fills that copy with the agent's page
when the agent window closes. Closing a copy closes its original. Kitsune handles this as
follows:

- In a `createWindow` window, only the tabs `windows.create` made are the agent's
  (`tabIds` in the result). The browser's copies there are discarded when the window opens.
  Kitsune records them once, at that moment, and never makes them owned.
  `browser_tabs` close refuses them (`user_tab`), because closing one would close the user's
  tab. `browser_close_window` closes the window with them anyway: removing a window
  closes no originals.
- A tab that opens in another window as a copy of the agent window's tab is adopted:
  `owned: true`, and `browser_close_window` closes it after the window. Adoption is
  conservative, and every condition must hold. The tab opens within 1.5 s of
  `windows.create`, in a window that existed before it, in the background, without an
  opener, discarded (a lazy tab that loads nothing), and at about:blank (where Zen's copies
  start) or one of the agent window's URLs. A tab the user opens is shown, has an opener,
  or loads a page, so it is never adopted.
- An adopted copy goes back to the user (`owned: false`, left open) when the user shows it,
  when it starts a load the agent did not start, or when it reports
  [user activity](#user-activity) as `browser_close_window` would close it.

Limits: this is a heuristic. A lazy background tab that something other than the sync
opens in those 1.5 s at about:blank counts as the agent's, for example one opened by
another extension. If the user closes the agent window, its adopted copies stay open,
agent-owned, and show the agent's page; `browser_tabs` close closes them. In a `browser_detach_tab` window, the browser's copies count as user tabs, so
`browser_close_window` on it needs the user's approval.

#### User activity

An agent write or navigation on a tab the user is using returns the error
`user_active` (`retryable: true`) instead of acting, with `state.tabId`, `state.url`
and `state.reason`:

| `state.reason` | The user is | Window |
|---|---|---|
| `input` | giving the page trusted input: `keydown`, `mousedown`, `pointerdown`, `touchstart`, `wheel` or `paste` (`state.input`, `state.sinceMs`, `state.windowMs`) | 3 s after the last such event |
| `typing` | typing in a field: the input, textarea, editor or custom element (a shadow-root field shows as its host) that last got a trusted `keydown` or `paste` still has focus, holds content, and its document has focus (`state.field` is its tag; the value is never read out) | while all of that holds |
| `navigating` | loading a new document in the tab they see (address bar, back button, a link), and no agent command on the tab started it (`state.sinceMs`) | while it loads, at most 10 s |

Nothing reached the page; repeat after a few seconds, work in a tab you opened, or ask
the user. Checked actions: click, double-click, type/fill/editable, select, check,
keypress, submit, form fill, hover, scroll, drag, focus, blur, JavaScript, upload,
navigate, back, forward, reload, click-and-wait-navigation, close, element and
full-page screenshots (they scroll the page), and trusted click (viewport) on their own tab; select (`switchTab`) and `browser_focus_window` on the tab
the user would leave. Reads never are.

How it tells the user from the agent: Kitsune's own DOM events are synthetic, so their
`isTrusted` is `false`, and the content script only counts trusted events. Its
listeners are on the window in the capture phase, so a page that stops propagation does
not hide input from them. The OS click of `browser_trusted_click` is trusted, so
pointer events within 2 s of its `screenPoint` are not counted. `mousemove`, scrolling
by itself and `input` events are not counted either, which keeps autofill, the agent's
own editing and a mouse resting over the page from counting as use. A navigation counts
as the agent's when an agent command on the tab was running, or ended at most 2 s
before, when the load started, so a click's or a navigate's own load never blocks the
agent; a load in a background tab never counts.

Limits: input in iframes is not seen (the content script runs in the top document),
nor input outside the page (address bar typing counts once the load starts). A
screen-coordinate trusted click has no tab and is not checked, and its own click may
count as user input on the tab it hits for 3 s.

#### No current tab

The MCP server's current tab is the one `browser_tabs` new or select (or
`browser_tab_list` new_tab) last made current. It lives in the MCP server process, so a
restarted or reconnected server starts without one (#303). Until it has one, a write or
navigation that names no tab is refused before anything is sent, instead of going to the
tab the user is viewing:

```json
{ "error": "browser_navigate writes a tab but names none, and there is no current tab, so it was not sent to the tab the user is viewing",
  "code": "no_current_tab", "retryable": false,
  "hint": "Nothing was dispatched. Pass tab_id (browser_navigate and browser_navigate_back accept it too), open a background tab with browser_tabs action=new or browser_tab_list new_tab, or make one current with browser_tabs action=select.",
  "state": { "tool": "browser_navigate" } }
```

Writes are the bridge's write actions that act on a tab, plus focus and blur (see
[Action errors](#action-errors)): the core `browser_click`, `browser_type`,
`browser_press_key`, `browser_select_option`, `browser_fill_form`, `browser_hover`,
`browser_drag`, `browser_drop`, `browser_file_upload`, `browser_evaluate`,
`browser_handle_dialog`, `browser_resize`, `browser_navigate` and
`browser_navigate_back`, and Kitsune's `browser_scroll`, `browser_check`,
`browser_focus`, `browser_blur`, `browser_submit_form`,
`browser_click_and_wait_navigation`, `browser_trusted_click`, `browser_forward`,
`browser_reload_tab`, `browser_highlight`, `browser_pin_tab`, `browser_mute_tab`,
`browser_duplicate_tab`, `browser_set_zoom` and `browser_storage` except `get`, `list`
and `snapshot`. `browser_batch` and `browser_retry` are refused when one of their
commands, or a command nested in `sequence`, `if`, `while`, `try` or `retry`, writes a
tab without a `tabId` (`state.action`; an `if` or `while` condition is JavaScript on its
tab). A `browser_workflow` without `tab_id` is always refused: its steps live in a file on
the bridge and cannot be inspected first, so even a workflow that only reads needs a tab.
Reads (snapshots, screenshots, page text, tab lists, console and network reads, waits)
still act on the active tab, and `browser_tabs` close keeps its own rule (above).

The core `browser_navigate` and `browser_navigate_back` take an optional `tab_id`,
accepted like `confirm_token` but not listed in their schemas, which are Playwright
MCP's; omitted, it is the current tab.

Only the MCP server refuses these calls. HTTP requests to the bridge, the Python API
(`kitsune.browser`, whose tabs are always named) and `kitsune.tools` called directly keep
the active-tab default.

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
| `consequential_label` | The same clicks, or Enter, on a button or link whose label reads like a send, transfer, delete, or account change ("Send", "Delete account", "Unsubscribe", "송금하기"), including icon-only controls named by `aria-label`, `alt` or `title`. See [Submit labels](#submit-labels) |
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
             "tabId": 7, "url": "https://shop.example/cart", "params": { "ref": "e12" } } }
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
  `browser_retry`, `browser_broadcast`, `browser_resume`, workflows, and batch `sequence`/`if`/`while`,
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
through a form. `consequential_label` covers buttons and links that send, transfer,
delete, or change an account. The two reasons are separate so a client can word its
approval prompt; a label that matches both is reported as `submit_label`.

The content script reads the label of the clicked control. The control is a `button`,
an `input type=button/submit/image`, or an element with `role="button"`, including an
element inside one. For `consequential_label` it can also be a link: an `<a href>` or
an element with `role="link"` (an `<a>` without `href` is not a link). The label is
the control's accessible name, in this order: the text of the elements named by
`aria-labelledby`; `aria-label`; its text; the `value` (else `alt`) of a button input;
for an icon-only control without text, the `aria-label`, `alt`, or SVG `<title>` of
the first icon inside it; then `title`.

The label is normalized: case, Unicode width, and whitespace are folded; a trailing
parenthetical, an amount ("$12.00", "12,000원"), and leading or trailing punctuation
are dropped; labels over 60 characters never match. The **whole** remaining label must
then be one of a fixed set of phrases.

Submit, order, and payment phrases (`submit_label`, buttons only):

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

Send, transfer, delete, and account-change phrases (`consequential_label`, buttons
and links):

- English: send/transfer/wire (it/money/funds/payment) (now); confirm/complete/make
  (the/a) transfer; confirm and send; delete/remove/erase (permanently/forever/all/
  everything); (permanently) delete/remove/erase (my/your/the/this/all) account/
  profile/data/repository/project/workspace; close/deactivate/terminate/cancel (my/…)
  account; cancel/end (my/…) order/booking/reservation/subscription/membership/plan;
  unsubscribe (from all/from everything/me/all);
- Korean: 송금, 이체, 전송 (하기) with an optional 지금/바로 prefix; 보내기; (계정/회원/영구)
  삭제/제거 (하기); (회원) 탈퇴 (하기); 구독/멤버십/정기결제/주문 해지/취소 (하기); 해지 (하기);
  수신거부 (하기);
- Japanese: 送金/振込/振り込み/送信 (する), 振り込む; (アカウントを) 削除 (する); 退会, 解約,
  配信停止, 購読解除 (する); サブスクリプション/定期購入/プラン/注文を解約/キャンセル (する);
- Chinese: (立即/确认) 发送, 转账, 汇款 (and traditional forms); (永久) 删除/移除 (账号/账户);
  注销 (账号); 退订; 取消订阅, 取消订单;
- German, French, Spanish: (jetzt) senden/absenden/überweisen/löschen/entfernen/
  kündigen/abbestellen; Konto/Account/Profil löschen/schließen/kündigen; Abo/
  Abonnement/Vertrag/Mitgliedschaft/Newsletter kündigen/abbestellen; Überweisung
  absenden/ausführen/bestätigen; envoyer, transférer, supprimer, résilier (maintenant);
  supprimer/fermer/clôturer/désactiver (mon/le/votre) compte; effectuer/confirmer/
  valider le virement; se désabonner, se désinscrire; résilier/annuler (mon/votre/l')
  abonnement; enviar, transferir, eliminar, borrar (ahora); enviar dinero; realizar/
  confirmar/hacer transferencia; eliminar/borrar/cerrar/desactivar (mi) cuenta;
  dar/darse/darme de baja; cancelar/anular (mi) suscripción/membresía.

The match is deliberately conservative: "Submit search", "Search", "Add to cart",
"Order history", "Payment methods", "Continue", "Subscribe", "Send feedback", "Send
code", "Transfer history", "Remove filter", "Manage subscription", a bare "Cancel",
"OK", "확인", "취소", "確認", "确认" or "取消", and an `<a>` labeled "Place order" are not
held. A commit control with other wording, a control without an accessible name (for
example, an unlabeled icon or a label drawn on a canvas), or a clickable element that
is neither a button nor a link is not caught. On a payment-like page every click is
held anyway (`payment_page`).

An extension older than this bridge reports fewer labels, and the bridge treats a
missing flag as no match. With an extension from before `submit_label`, only
structural submit controls are held; with one from before `consequential_label`,
send, transfer, delete, and account-change labels are not held. A tab that still runs an older
content script of a current extension gets the current one injected before the next
command.

**Check-then-act window.** The bridge inspects the target (tab, URL, and whether a
click or Enter submits a form or hits a submit- or consequential-labeled control) with `describeTarget` before it dispatches the action.
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
element empty. Unmasked textareas and editing regions are unchanged. Results
and errors of write actions on sensitive fields (`browser_type`,
`browser_form_fill`, …) do not echo the typed value.

Masked static text is left out of the same reads. Text in any element whose
computed `-webkit-text-security` is `disc`, `circle` or `square` (so an
inherited mask counts) is sensitive, editable or not, and so is all text inside
a masked editing region, including its `contenteditable="false"` parts, whatever
their own style. That text is left out of snapshot names and text lines,
`browser_find`, element `text` fields, labels (including a button's or button
input's label), `browser_markdown` and `browser_readability`; `browser_html` and
`browser_query` `html` keep the element and drop its text. Element `text` fields
and markdown `pre` blocks and table cells leave masked text out rather than
showing it as mask characters. A child that sets `-webkit-text-security: none`
inside a masked element keeps its text, as do unmasked elements and ordinary
text.

Masks come from computed style, which is read per element. Within one
synchronous read (`browser_snapshot`, `browser_dom`, `browser_forms`,
`browser_find`, `browser_query`, `browser_html`, `browser_markdown`,
`browser_readability`, `browser_explain_selector`), each element's mask and
redacted text are computed once and reused, so nested names, labels and find
scores do not walk the same subtree again. The cache is dropped when the read
returns, so the next read sees page changes. Other results (click,
`browser_wait_for`, ask-before-act labels) cache only within their one text
lookup. Elements without child elements use the browser's `textContent`.

Known limitations:

- Page text (`browser_page_text`) is rendered text, where the browser shows
  masked text as mask characters, so the length of masked content is visible
  there, as it is on screen.
- A non-editable part inside a masked editing region that sets its own
  `-webkit-text-security: none` is visible on screen and therefore appears in
  `browser_page_text`; structured reads still omit it.
- Masking done with custom fonts or canvas drawing is not detected. Neither
  leaves a reliable signal, so detecting it would cause false positives.

There is no opt-in to reveal redacted values through these tools, and none is
planned (decided in #71). A reveal switch, whether per call or as a bridge
policy, would give an agent or an injected page instruction one more way to
read secrets through tools that are otherwise safe to call on any page. The
only way to read a raw value is agent JavaScript (`browser_evaluate`), which stays the
separate, explicit and unrestricted trust boundary. It needs the developer extension
build, and read-only mode refuses it.

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
content of every textarea and `contenteditable` element, and all of its text
when any of the original's text is sensitive.

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
- `key` of `keypress` when it types itself into a field: one character without
  Control, Alt or Meta. Named keys such as `Enter` and shortcuts are kept;
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
`url`, `domain`, `kind`, other `key`s, `modifiers`, `clear`, …), duration
and outcome are kept. `intercept` takes only URL patterns and an effect, never a
response body, so its entries are logged as sent. Nested commands (`batch`
`commands`, `parallel` `sequences`, `retry`/`broadcast` `command`,
`if`/`while`/`try` blocks) follow the same rule. `uploadFile` entries keep their
file paths; file contents are never logged. The extension still receives the
unredacted value. Only the audit copy is scrubbed.

Entries of commands a [`browser_tools_call`](#tool-gateway) ran add
`via: {"gateway": "browser_tools_call", "tool": "<tool name>"}`. Direct calls have no
`via`; entries never name the MCP tool otherwise.

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
is logged after its steps. Direct calls have no `parent`. Steps run by
`browser_resume` carry the original `call` and add `resumed: true` to `parent`; the
resume token is never logged ([Checkpoint and resume](checkpoint-resume.md#audit-log)).

Every history and bookmark lookup is recorded, refused ones included, with its
`params` (`query`, `limit`, `since`; `url` for visits), the number of results as
`count`, and the scope it ran in as `filters` (`allowDomains`, `denyDomains`,
`maxDays`, `startTime`, `urlPolicy`). Result URLs are never logged; see
[History and bookmarks](#history-and-bookmarks).

### History and bookmarks

Opt-in, read-only lookup of the user's browsing history and bookmarks (#140), so an
agent can find "the page I looked at yesterday" and follow the user's trail. Two
switches must both be on, and the browser's is the user's alone:

1. **The user's browser permission.** `history` and `bookmarks` are
   `optional_permissions` in the extension manifest, not granted at install. The
   user grants them on Kitsune's options page (about:addons → Kitsune → Options,
   or Preferences): the **Allow history and bookmarks** button calls
   `permissions.request` from its click, which shows the browser's own permission
   prompt. **Revoke** on the same page, or the add-on's Permissions tab in
   about:addons, takes them back. No bridge command requests a permission, so the
   agent can never grant it. Without the grant, every lookup returns
   `permission_required` (`retryable: false`) with a hint naming the options page.
2. **The `history` capability group.** Off by default, and `KITSUNE_CAPS=all` leaves
   it out: name it, as in `KITSUNE_CAPS=core,history`.

| Tool | Returns |
|---|---|
| `browser_history_search(query, since=None, limit=20)` | `results: [{url, title, last_visit, visit_count}]`, newest first. `query` matches titles and URLs (`""` for all); `since` is an ISO 8601 date or datetime (local time without an offset); `limit` is 1–100 |
| `browser_history_visits(url)` | `visits: [{visit_time, transition, visit_id, referring_visit_id?}]` for one URL, newest first, at most 100. `transition` is Firefox's `history.getVisits` transition (`link`, `typed`, `auto_bookmark`, `reload`, …); `referring_visit_id` appears only where Firefox reports a referrer, which current Firefox releases usually do not |
| `browser_bookmarks_search(query, limit=20)` | `results: [{url, title, folder}]`; `folder` is the path of folder titles, such as `Bookmarks Toolbar/Work`. `""` lists the most recently added bookmarks |

Results also carry `count` and, when more matched than `limit`, `truncated: true`.
History results carry `since`, the start of the window the lookup used (UTC).
`last_visit` and `visit_time` are ISO 8601 UTC. `visit_count` is Firefox's count
for the URL, not only visits inside the window. Only URLs, titles, visit times,
transitions and bookmark folders are returned, never page contents, form data,
cookies or passwords. Only `http(s)` URLs are returned: `file:`, `data:`,
`about:` and other schemes can hold local paths or page contents.

**Scope.** The bridge sends each lookup's scope to the extension, which filters
before anything leaves the browser:

| Setting | Environment (bridge) | `browser_set_policy` | Default |
|---|---|---|---|
| Allowed domains | `KITSUNE_HISTORY_ALLOW_DOMAINS` (comma list) | `history_allow_domains` | Empty: every domain |
| Denied domains | `KITSUNE_HISTORY_DENY_DOMAINS` (comma list) | `history_deny_domains` | None |
| Time window (whole days back from now) | `KITSUNE_HISTORY_MAX_DAYS` | `history_max_days` | 30 |

A domain also covers its subdomains (`example.com` covers `docs.example.com`; a
leading `*.` is accepted and dropped), and a denied domain wins over an allowed one.
`since` earlier than the window is moved to the window's start. The window applies
to history; bookmarks have none. `browser_get_policy` and `browser_health` show the
current settings as `historyAllowDomains`, `historyDenyDomains` and `historyMaxDays`.
An invalid environment value stops the bridge at startup with the variable's name.

`browser_set_policy` can only tighten the scope, so an agent never widens what it
may read: it can add denied domains, narrow the allowed list (each entry within a
current one, never empty once set) or lower the days. A call that would loosen any
of them fails with `policy_locked` (HTTP 403) and changes nothing; the user loosens
the scope through the environment and a bridge restart. The URL `allow`/`deny`
regex policy also applies: history results, and `browser_history_visits` URLs, stay
filtered by every URL deny pattern and allow list set since the bridge started,
even after `browser_set_policy` removes them for page actions. When such a pattern
is set, the bridge reads up to 100 results from the extension and applies `limit`
after filtering.

The lookups are not batch actions: `browser_batch`, `browser_parallel`,
`browser_broadcast`, `browser_retry` and workflows reject them as an unknown
action (`unknown_action`), so none can run them outside this scope. Read-only
mode allows them; ask-before-act does not hold them.

#### History URL redaction

Every URL the extension returns is rewritten first:

- `user:password@` is removed.
- A query or fragment parameter's value becomes `[redacted]` when its name is
  sensitive: a word of it (split at punctuation and camelCase) is one of `token`,
  `secret`, `password`, `passwd`, `pwd`, `pass`, `auth`, `authorization`,
  `apikey`, `key`, `sig`, `signature`, `hmac`, `session`, `sid`, `jwt`, `otp`,
  `code`, `ticket`, `nonce`, `credential(s)`, `saml`, `samlrequest`,
  `samlresponse` or `assertion`, or the name contains `token`, `secret`, `passw`,
  `sessid`, `sessionid`, `apikey` or `credential` (`access_token`, `apiKey`,
  `X-Amz-Signature`, `PHPSESSID`).
- Any parameter's value becomes `[redacted]` when it looks like a credential: a
  `Bearer ` value, a JWT, a known token prefix (`ghp_`, `github_pat_`, `glpat-`,
  `xoxb-`, `sk-`, `sk_live_`, `AKIA`, `ya29.`, `AIza`, …), 32 or more hex digits,
  or 32 or more token characters that mix upper case, lower case and digits.
- Fragments are handled as parameters (`#access_token=…&state=…`), as a route
  with a query (`#/inbox?token=…`), or, when the whole fragment looks like a
  credential, replaced as a whole.

Ordinary values, such as `q=firefox+history` or a lowercase slug, are kept.
The rules are heuristics like [Sensitive-field redaction](#sensitive-field-redaction):
a secret under an innocuous name and in a short or lowercase value is not
recognized, and path segments and titles are not rewritten. History entries
whose URLs differ only in redacted values are listed once, with the newest title
and visit and the summed `visit_count`. A redacted URL is still a valid
`browser_history_visits` argument: the extension resolves it to the visited URLs
it stands for.

#### Following the trail

A history or bookmark result is enough to continue from it: open `url` in a tab
(`browser_tabs(action="new", url=url, wait=True)`, or the agent-owned background
tab that #139 adds) and read it with the usual tools. `browser_history_visits`
shows when and how the user reached the page. A redacted URL opens without its
secret, so a page that needed the token in its URL may show a login or an error
instead; Kitsune never reveals the original value.

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
| `failed` | It returned an error that is safe to repeat (`retryable: true`, unclassified errors, `error_page`, `ambiguous_target`, `user_tab`, `permission_required`, the pre-dispatch refusals `capability_restricted`, `mode_restricted` and `unknown_action`, or `ok: false`) |
| `held` | [Ask-before-act](#ask-before-act) held it; nothing ran |
| `unknown` | It may have taken effect: an error with `retryable: false`, or it was in progress at the container's deadline. Observe the page before acting again |
| `skipped` | It never started: an earlier step stopped the container (a held step, `stopOnWarning`, a failed workflow step) or the deadline passed |

`tabId` is the command's `tabId` when it has one. A container command (`sequence`,
`retry`, `if`, `while`, `try`, `parallel`, `broadcast`) takes the first of `unknown`,
`held`, `failed`, `skipped` that it or a nested command has, else `ok`; a `retry`
whose last attempt succeeded is `ok`. A `parallel` record adds `sequences`: one list
per sequence of `{sequence, index, action, tabId, status}`; commands that never
started are `skipped`. The top-level `status` combines the records the same way.

The condition of an `if` or `while` is JavaScript run on its `tabId`. A condition
that cannot run (for example `tab_closed`, `navigating`, a stale document, a script
error, or the signed build) is not false. The step stops with the condition's error
(`error`, `code`, `retryable`, `hint`, `state`), so its record is `failed`, or
`unknown` for `retryable: false`. An `if` then runs neither branch. A `while` keeps
the passes that already ran: it adds `iterations`, `results` and
`stoppedBy: "conditionError"` to the error. Conditions that ran return the same
results as before. Read-only mode (`browser_set_policy(readonly=True)`) refuses
`if` and `while` like `js`, with `Read-only mode is active; refusing action: if`,
before the condition or any branch runs. It refuses the other agent JavaScript the
same way: `browser_js` (including `full=True`), `browser_wait_for(js=...)` and
`browser_sync_barrier`.

`summary` states `progress` in one paragraph a user can read without the records:
the count of each status, then each command that is `unknown`, `held` (with how to
confirm it), `failed` or `skipped`, by its `index` and `action` (and `tabId`), at most
five per status. A `parallel` that started is counted by the commands of its
sequences (`sequence 1 step 0`). It starts with `Stopped at its 300s deadline.` after
a `container_timeout`. It is text for people; read `progress` in code.

`progress` is also what to resume from: the commands before the first record that
is not `ok` have run, `unknown` ones must be observed before they are repeated, and
`skipped` ones are safe to send. A call whose `status` is not `ok` adds `call` and
`resume_token`; pass them to [`browser_resume`](#browser_resumecall-resume_token-observednone)
to continue it without sending its `ok` writes again ([Checkpoint and resume](checkpoint-resume.md)).

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
  `screenshot` captures the visible tab, or the tab its `tabId` names.
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
A step that acts on a tab and names no `tabId` runs on the call's default tab: the
top-level `tabId` of `/api/batch`, `/api/retry` or `/api/workflow`, which
`browser_batch`, `browser_retry` and `browser_workflow` set to their `tab_id`, by default
the current tab. Without one it goes to the active tab, which the MCP tools refuse for
writes ([No current tab](#no-current-tab)). Parallel sequences still name
their tabs, tabless actions (`newTab`, `tabs`, `cookies`, ...) get none, and a resumed
call keeps its original default tab. A default `tabId` that is not a tab id is refused
with `invalid_tab_id` before anything runs.
A closed tab is never replaced: commands on it fail with `tab_closed`.

### `browser_tabs(action="list", tab_id=None, url=None, force=False, raw=False, wait=False, timeout=15000, window_fields=False, include_active=False)`

Replaces `browser_new_tab`, `browser_close_tab`, and `browser_switch_tab`.
`action` is `list`, `new`, `close`, or `select`. List uses GET `/api/tabs`;
new uses POST `/api/new-tab` (default URL `about:blank`), close uses
`/api/close-tab`, and select uses `/api/switch-tab`. Close and select require
`tab_id`. `force` disarms beforeunload for close; `raw` bypasses the image URL
viewer for new tabs. List is never cached and marks agent-owned tabs `owned: true`;
it drops each tab's `windowId` and `index` unless the tabs span more than one window
or `window_fields=True`, and keeps every other field. With `include_active=True`, list
also returns `active_page`: the active tab's `browser_snapshot(text=True)` (text lines,
refs, `url`, `title` and `tabId`, capped and redacted the same way), so orienting and
the first read take one call. A failed page read is returned as `active_page` beside the
inventory. New opens the tab in the
background and returns immediately unless
`wait=True`, which waits up to `timeout` ms for the page to load and returns
the actual URL and load status. Close refuses a tab the agent did not open with
`user_tab` until the user approves it (`confirm_token`, see
[Agent-owned tabs](#agent-owned-tabs)). Select switches the user's view: use it only
when the user asked. Options for other actions are ignored.

### `browser_page_text(tab_id=None)`

Replaces the duplicate `browser_page_text_by_tab_id`. Uses GET `/api/page-text`
for the active tab or POST to the same endpoint with `tabId` for a specific tab.

### `browser_dom(tab_id=None, selector=None, ref=None, max_depth=None)`

Returns a JSON tree of page elements with fresh refs, bounds and states. With no
arguments, reads `body` to depth 6 via GET `/api/dom`; optional arguments use POST
(`tabId`, `selector`, `ref`, `maxDepth`). `selector`, `ref` and `max_depth` scope the
tree as in `browser_snapshot` below, except that `max_depth` defaults to 6.

The serialized nodes are capped at 20,000 characters. The tree keeps nodes in
document order up to the cap and stops there; a cut tree has `truncated: true` and a
`hint` to scope the read or use `browser_snapshot`. Only returned nodes receive
usable refs.

### `browser_snapshot(tab_id=None, selector=None, ref=None, max_depth=None, text=False)`

This full-toolset function returns a compact accessibility-style tree with stable
refs and explicitly forwards `text: false` by default via POST `/api/snapshot`.
The core Playwright-schema tool instead omits `text`: it includes readable static
text and HTTP(S) link URLs. Direct HTTP GET or POST with omitted `text` has the same
readable default and no character cap. Explicit `text: true` selects the bounded
readable mode; `text: false` omits static prose. Optional scope arguments are `tabId`,
`selector`, `ref` and `maxDepth` in the JSON body.

The core `browser_snapshot` returned inline is capped at 12,000 final public characters
(it sends `"maxChars": 12000`; #262); with `filename` it sends no budget and saves the
whole snapshot. A cut core snapshot has `truncated: true`, ends with
`- … truncated; browser_find searches the rest` and has a `hint` that names only core
tools and arguments: `browser_find` (which reads the whole page), a ref from it or from
the snapshot as `target` (`"e12 >> .."` reads that element's container, such as the card
around a product name), or `filename`. On the deterministic fixtures of
`tests/test_real_browser_bounded_snapshot.py`, the inline core snapshot of 80
heterogeneous product cards went from about 24,200 to 9,200 characters (the first 30
cards, each whole) and that of a 24-section article from about 44,600 to 10,800
characters (the first 29 paragraphs, each with its links and URLs). The cap counts
lines before refs are shortened to their public `e12` form, so a snapshot can stop
well below it. Cards and paragraphs past the cut stay reachable through
`browser_find` and a scoped `target`.

- `selector` scopes the snapshot to the subtree of its first match, including its root; it
  takes the [target selector](#target-selectors) forms.
- Open and closed shadow roots are read as rendered: a web component's shadow tree
  appears under its host, with refs, and slotted children appear where their slot is.
  `browser_dom`, `browser_find`, `browser_forms` and CSS selectors in action tools
  see shadow roots too; a selector matches within one tree (the page or a shadow
  root), so a descendant combinator does not cross a host. Coordinate clicks and a
  `browser_keypress` without a target resolve into shadow roots as well.
- `ref` scopes to an existing snapshot element and takes precedence over `selector`.
  It resolves to the same attached node across observations. Missing selectors and
  stale refs return errors rather than silently falling back to the full page.
- `max_depth` is a non-negative integer DOM depth relative to that root (`body`
  without a scope). `0` considers only the root, `1` also considers its immediate
  element children, and `None` traverses all depths. Non-semantic containers still
  count toward DOM depth, even when they produce no snapshot line.

Successful snapshots, including scoped reads, preserve earlier attached-node refs.
Only emitted elements receive new usable refs; repeated observations reuse them.
Generic clickable wrappers with any native interactive descendant or at least two interactive descendants are not
reported as buttons; their children remain discoverable. Explicit roles and native
controls are preserved. Custom controls use explicit labels/title/alt, then direct
text; subtree text is used only when at most 80 characters and five elements long.

Consecutive composite siblings (children containing elements, or list items) with
identical tag/role/input-type structure show the first five items, followed by
`- … N more similar items; snapshot e14 to list all`. Names, IDs and classes do not
affect the signature. Leaf controls such as toolbar buttons are not collapsed. Hidden
items are excluded from grouping; collapsed items get no new refs from this snapshot. The
marker's ref (`e14`) names the items' container (a `tbody`, list or grid, which may have no
line of its own): a snapshot scoped to that container, by this ref or a selector, lists up
to 100 consecutive similar items of its own. Past that it shows
`- … N more similar items; use browser_find to locate a specific one`. Runs nested deeper
inside it still collapse after five items, each with its own marker. The core
`browser_find` searches every item, collapsed or not (it reads HTTP `/api/snapshot` with
`"collapse": false`), and its matches carry refs that actions accept. Post-action snapshots
additionally keep the acted-on target's item, even beyond the first five, and return its
`target_ref`; a delta lists a run's marker after any of the run's lines it lists, or when
the number of collapsed items changed. Character-limit
truncation prioritizes the acted-on target and its ancestor context when that complete
block fits. If it cannot fit, target_ref is omitted and truncation guidance says so.

`text=True` (opt-in; the default is unchanged) adds readable text in the same call:
each block's visible inline text becomes a `- text "…"` line in document order,
and the result adds `url`, `title` and `tabId`. Text lines have no refs; refs keep
the lifetime above. Text that only repeats a control, label or heading name is
omitted, as are hidden, `aria-hidden`, form-field and sensitive-field content.
Lines are clipped at 1,000 characters with `…`; the snapshot is capped at 12,000
final public characters after URL redaction/alias serialization. Explicit HTTP
`maxChars` (also with omitted/false text) and post-action limits are enforced there
as well, on complete lines with ancestor context. Without an acted-on target, a cut
snapshot is a document-order prefix that never ends inside a block: the lines of an
element below the snapshot root (a product card's name, URL, variant, price and
discount; a paragraph with its links) that total at most 2,000 characters or a quarter
of the budget are kept or left out whole, so a card is never split or joined to its
neighbor. When URL redaction or ref aliases lengthen the snapshot past its budget, the
bridge cuts again only at those boundaries; it can leave out a whole block that no
longer fits and keep a later one. Truncation sets `truncated: true`,
includes a scope hint, and normally ends the snapshot with
`- … truncated; scope with selector, ref or max_depth`.
For budgets too small for the marker, the snapshot may be empty but the flag and hint
remain. Repeated composite items still collapse, but siblings with only inline content
(prose paragraphs, text list items) do not. `selector`, `ref` and `max_depth` scope
text the same way. HTTP `maxChars` accepts a finite non-negative number (zero is
valid); invalid or nonfinite budgets return an error before reading.
With `KITSUNE_POST_ACTION_SNAPSHOT=text` in the bridge environment, post-action
snapshots use the same text lines within their 4,000-character cap (a delta lists
the text lines the action changed), so an `expect={"text": ...}` already present
there needs no further read.

#### Snapshot readiness

A readable snapshot (the core tool's, or HTTP `/api/snapshot` without `text: false`)
adds `readyState`, the document's `document.readyState` (`loading`, `interactive` or
`complete`). A tab that finished loading is not a page that finished rendering: an
application can still insert its content, and a complete page can be empty. When the
snapshot has no lines and is not truncated, the result also has `empty: true` and a `hint`
to wait for text the task expects (`browser_wait_for` with `text`) before snapshotting
again. It is an observation, not an error, and Kitsune adds no wait of its own:

```json
{ "snapshot": "", "url": "https://example.test/hotels", "title": "Hotels",
  "readyState": "complete", "empty": true,
  "hint": "The page shows no content yet. If it should, wait for text you expect with browser_wait_for (page text, not a field's placeholder or label), then take a new snapshot; a loaded page can also be empty." }
```

Saving is separate from readiness: the core `browser_snapshot(filename=...)` returns
`ok: true` and `saved_to` once the file is written, with the snapshot's `readyState`,
`empty` and `hint` alongside. `empty: true` means the saved file is empty. Compact
(`text: false`) and post-action snapshots have none of these fields, since they leave
prose out. A tab showing an HTTP error response without a document fails with
`error_page` instead (see [Action errors](#action-errors)).

#### Frames

The content script runs in the top document and reads into same-origin frames
(`iframe`/`frame` whose `contentDocument` it can access), at most three frames deep.
`browser_snapshot`, `browser_dom`, `browser_find` and `browser_forms` include their
elements with refs in the same document/injection scope as the page, and refs, CSS selectors
(the top document first, then each frame), `form_fill` labels and coordinate clicks
reach into them; sensitive fields inside frames are redacted the same way. A frame is
a snapshot line such as `- iframe "배송지 편집" [ref=…]` with its content below it.
A frame whose content is unavailable is still listed, with
`[content=cross_origin src="https://pay.example/widget"]` (or `content=too_deep`), its
`src` without query or fragment; `browser_dom` adds the same `content` and `src`, and
`browser_forms` lists such frames in `unavailableFrames`. Fields inside a frame carry
`frame` (its title, name or src) in `browser_forms`, and bounds are in the top
viewport.

#### Images

A rendered `<img>` with a name appears as an `img` line with a ref, e.g.
`- img "Distorted characters" [ref=…]`, with no state. Its name is `aria-labelledby`,
`aria-label`, `alt`, or `title` when there is no `alt`, clipped at 160 characters like
other names. Decorative images (`alt=""`, `role="presentation"`/`"none"`), `aria-hidden`
images and images without a name are left out, and `src` is never listed (`browser_images`, in
the `read` group, lists sources). The line names the image but does not describe its
pixels: pass its ref to `browser_screenshot` to see it. An image that is a control
(`onclick`, `tabindex`, `role="button"`) is listed as a control instead, and an image
inside a link is listed below the link.

#### Media

Rendered `<video>` and `<audio>` elements appear as `video`/`audio` lines with a ref
and their state: `src` (the current source, clipped at 160 characters), `duration`
and `currentTime` in seconds (`null` while unknown), `paused`, `muted` and `controls`.
Their children (sources, tracks, fallback content) are not listed.

The browser's native media controls answer only trusted input, so a synthetic click
cannot press their buttons or scrub their timeline. Instead, `browser_click` on a media
element whose click the page neither cancelled nor handled itself toggles playback with
`play()`/`pause()` and adds `media` (the state above) to its result. The batch `media`
command drives one element directly: `{"action": "media", "ref": "…", "op": "play"}`
with `op` one of `play`, `pause`, `seek` (with `time` in seconds), `mute` or `unmute`;
it returns `{ok, tag, media}`. `play` waits up to 2 s for playback to start, and `seek`
up to 2 s for the frame at `time`, so a screenshot taken next shows it. `media` is a
write: read-only mode refuses it.

When playback does not start, `media` fails with `error`, `reason` and `media`, and
`browser_click` stays a success whose `media` adds `error` and `reason`:

- `autoplay_blocked`: the browser rejected `play()` with `NotAllowedError`: synthetic
  clicks do not grant user activation, even in the active tab. Use the batch `media`
  command with `op: "mute"` then `op: "play"`, use `browser_trusted_click` on the control
  where available, or ask the user. Kitsune does not mute or retry automatically.
- `refused`: the browser rejected `play()` for another reason, such as an unsupported source.
- `background_tab`: Firefox delays playback in a tab until it has been shown once
  (`media.block-autoplay-until-in-foreground`). Agent tabs open in the background
  ([Never take focus](#never-taking-focus)), so the agent's own tabs cannot play media
  until shown. Kitsune never shows a tab for playback: it cancels the pending `play()`.
  Select the tab explicitly with `browser_tabs` select (refused with `user_active` while
  the user is using their tab), then play again.

For example, after an autoplay block, play muted with:

```json
{"commands": [{"action": "media", "ref": "e2", "op": "mute"},
              {"action": "media", "ref": "e2", "op": "play"}]}
```

The model never receives audio content, even when playback succeeds. Muted playback
can expose video frames in screenshots, but cannot make an audio puzzle audible to
the model. Use the page's hand-off path when available, or ask the user to listen.

### `browser_type(selector="", text="", mode="type", clear=False, tab_id=None, ref=None, element=None, wait=False, timeout=10000)`

Replaces `browser_fill` and `browser_set_editable_content`.

| Mode | Behavior | Bridge endpoint |
|---|---|---|
| `type` | Type using events; `clear=True` clears first | `/api/type` |
| `fill` | Always replace the field value with native setter and input/change events; ignores `clear` | `/api/fill` |
| `editable` | Insert plain text into contenteditable/input/textarea; append by default, replace with `clear=True` | `/api/set-editable-content` |

In a `contenteditable` editor, `type` and `editable` insert at the caret when it is
inside the target and otherwise at the end of the target, firing `beforeinput` and
`input`; `clear=True` replaces the target's content. When the editor's page cancels
that `beforeinput`, `type` and `fill` leave the content as the page left it and return
`prevented: true` (#289). `browser_click` moves focus as a
real click does, unless the page cancels `mousedown`: it focuses the clicked control,
or the editor with the caret at the click point (`x`/`y`) or at the end of the clicked
element (`selector`/`ref`). So clicking an editor's last paragraph and then typing
appends to it.

All modes accept the [actionability wait](#actionability-wait). Editable mode sends `format="text"`. Callers migrating from the old editor tool
should pass `clear=True` to retain its replace-by-default behavior. For trusted
HTML insertion use `browser_js`; the underlying endpoint still accepts `format="html"`
(both developer build only).

### `browser_form_fill(fields, tab_id=None)`

Fills several fields in one call via POST `/api/form-fill`. Each key is matched
as a CSS selector, then `name` attribute, label text, and placeholder; results
are returned per key, and one failing key does not stop the others. A matched
web-component host is filled through the same shadow-root lookup as
[`/api/fill`](#post-apifill); a key with no reachable field gets a `not_fillable`
[action error](#action-errors). Selectors, names, labels and placeholders also match
inside shadow roots; a key matching several actionable fields, in one tree or across
components, writes nothing and gets `ambiguous_target` with `state.candidates`.
`first=True` explicitly fills the first match. Labels take precedence over placeholders
within each searched tree; multiple labels for the same control count once. When every
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

### `browser_submit_form(selector=None, tab_id=None, ref=None, element=None)`

Submits the target form, or the form enclosing the target element, via POST
`/api/submit-form`. Without `selector`/`ref` it submits the form that owns the
field Kitsune last filled or focused in that document (`browser_type`, `browser_form_fill`,
`browser_select_option`, `browser_check`, `browser_focus`), else the page's only
form. When that field is gone or outside every form and the page has several
forms, nothing is submitted: the result is an `ambiguous_target`
[action error](#action-errors) listing up to 5 forms.

It clicks the form's first submit control by form ownership (#288; a `<button>`
without a `type` counts, as in HTML), so the page's submit handlers and validation run: `"submitted": "button"`.
A form without one is submitted with `requestSubmit()`, which runs them too
(`"requestSubmit"`); only where that is missing does it fall back to `form.submit()`,
which skips them (`"method"`).

```json
{ "error": "3 forms on the page and no filled field names one; nothing was submitted",
  "code": "ambiguous_target", "retryable": false,
  "hint": "Pass the ref of the form to submit from state.candidates, or fill one of its fields first.",
  "state": { "forms": 3, "candidates": [
    { "ref": "e0", "action": "/wishlist/delete", "method": "post", "button": "Delete lantern" },
    { "ref": "e1", "action": "/wishlist/delete", "method": "post", "button": "Delete mug" },
    { "ref": "e2", "action": "/wishlist/add", "method": "post", "button": "Add" }] } }
```

A successful submit names the form it submitted in `form`, with the same fields
(`action` and `method` as written in the form's attributes, `button` the submit
control's label). Its `ref` remains valid across the full post-action snapshot while
that node stays attached in the same document; navigation or replacement invalidates it.

```json
{ "ok": true, "submitted": "button",
  "form": { "ref": "e3", "action": "/wishlist/add", "method": "post", "button": "Add" } }
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
| `text`, `text_gone` | Literal substring of `document.body.innerText` appears/disappears, poll every 100 ms; placeholders and aria-labels are not page text | `/api/wait-for-text` |
| `js` | Expression returns a non-empty result, poll every 500 ms | `/api/wait-for-result` |
| `url`, `title` | JavaScript regex pattern matches | `/api/wait-for-url`, `/api/wait-for-title` |
| `network_idle=True` | No new network resources for 500 ms | `/api/wait-for-network-idle` |
| `time` | Sleep a finite, non-negative number of seconds in the MCP server | None |

`timeout` is in milliseconds and does not limit an explicit sleep. JS retains
bridge non-empty-result semantics: `false` and `0` count as results; return
`null` or an empty string while waiting for a predicate.

Element and text waits are single page reads that the bridge repeats until `timeout`.
They do not depend on the page's timers, which browsers throttle in background tabs.
Each read, including its wait behind other commands on the tab, must answer before
`timeout`, and no read starts after it, so a wait ends near `timeout` (`elapsed` is
measured, not clamped). Reads are the poll interval apart, except the last, which starts
100 ms before `timeout` (the whole of a shorter `timeout`) whatever the poll interval,
so that is how long it has to answer; a text wait's last read also looks for the text
among element names. If the read in progress at `timeout` does not answer, an unmet wait
returns its last completed read with `observationIncomplete: true`, and its error says
so: the condition was not observed within `timeout`, but the final read did not finish.
A late reply, met or an error, is ignored. If no read answered at all, the wait returns
code `timeout` without `ok`: whether the condition holds is unknown, which is the usual
result of a positive `timeout` shorter than one read takes. A zero `timeout` (HTTP and
batch only) is instead one read that may take up to 5 s. `js` waits still poll inside
the page.

### `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False, ref=None, element=None, format="png")`

Replaces `browser_element_screenshot` and `browser_full_page_screenshot`.
Returns `[Image, metadata]` (MCP image content) by default; `save_to` writes the
full PNG (or JPEG with `format="jpeg"`) and returns metadata with `saved_to` instead,
and `data_url=True` adds the raw image data URL. `selector`/`ref` and `full_page=True`
are mutually exclusive.

- Default: viewport capture via POST `/api/screenshot`. A supplied `tab_id` is
  captured where it is (`tabs.captureTab`); no tab is shown and no window focused.
- `selector`: crop one element via POST `/api/element-screenshot`. Nonempty selectors
  use the shared resolver (CSS, including shadow trees, and the supported Playwright
  subset) and an exact-element marker that is cleaned up even after capture failure.
  Unsupported syntax returns persistent `unsupported_selector` before capturing.
- `full_page=True`: scroll and stitch up to 30 viewports via POST `/api/full-page-screenshot`.

No capture activates a tab. `save_to`
overrides the file destination; `data_url=True` additionally returns `dataUrl`.
The former element tool's `scale` option remains available through the bridge
endpoint; the merged MCP tool uses its default scale of 1.

### `browser_trusted_click(selector="", x=0, y=0, coordinate_space="viewport", focus=False, tab_id=None, first=False, restore_pointer=True)`

Clicks with the real OS mouse pointer via POST `/api/trusted-click`, for pages
that ignore synthetic DOM clicks. It moves the user's cursor for the click, so prefer
`browser_click`. Afterwards the cursor goes back to where it was;
`restore_pointer=False` (request field `restorePointer: false`) leaves it on the
target, for hover menus that close when the pointer leaves. A `selector` is scrolled
into view and clicked at its center; otherwise `x`/`y` are viewport coordinates, or
desktop coordinates with `coordinate_space="screen"`. The OS click lands on whatever the screen shows, so a
`tab_id` that is not the shown tab of its window is shown for the click, and the
user's tab is shown again 300 ms after it (see [Shared browser](#shared-browser)). A
viewport click returns `user_active` without clicking while the user is using the
target tab or the tab it would hide. Ambiguous selectors return `ambiguous_target`
before scrolling the element or invoking the OS; `first=True` opts into the first match.

| Platform | Click backend | Pointer restore |
|---|---|---|
| Windows | `user32` `SetCursorPos` + `mouse_event` | `GetCursorPos` before the click, `SetCursorPos` 100 ms after it |
| macOS | Quartz `CGEventCreateMouseEvent` when pyobjc is importable (`uvx --from 'kitsune-browser-mcp[macos]' kitsune`), else [`cliclick`](https://github.com/BlueM/cliclick) on `PATH` | Quartz: `CGEventGetLocation` before the click, `CGWarpMouseCursorPosition` 100 ms after it (no mouse event is posted), then `CGAssociateMouseAndMouseCursorPosition` so the user's mouse does not freeze for the 0.25 s macOS applies after a warp. cliclick: `cliclick p c:x,y` prints the position in the same run as the click, then `cliclick m:x,y` 100 ms later |
| Linux (X11) | [`xdotool`](https://github.com/jordansissel/xdotool) on `PATH` (`sudo apt install xdotool`). Wayland sessions (`XDG_SESSION_TYPE=wayland`, or no `DISPLAY`) are not supported and return `{error}`. Coordinates are not scaled for HiDPI X11 (`GDK_SCALE`, `layout.css.devPixelsPerPx`), so clicks there may land off by the scale factor | `mousemove restore` at the end of the same `xdotool` command, after the click |

On macOS, the app running the bridge (your terminal or MCP client) needs
Accessibility permission (System Settings > Privacy & Security > Accessibility);
without it, or without either backend, the call returns `{error}` explaining what
is missing instead of clicking. On Linux, a missing `xdotool`, a failed `xdotool`
run or a Wayland session also returns `{error}` without clicking. Results include
`clickBackend` (`win32`, `quartz`, `cliclick`, or `xdotool`) and `pointerRestored`:
`true` once the cursor is back, `false` with `restore_pointer=False` or when it could not
be read or moved back. A failed restore adds `pointerRestoreError` and still returns
`ok: true`, since the click already happened. On Linux the move back is part of the click
command, so a failure there fails the whole call.

`focus=True` tries to foreground the browser first and reports `focused`:

- Windows matches the window title: the page title for viewport clicks, otherwise
  `Zen Browser`, `Mozilla Firefox`, then `LibreWolf`. Set
  `KITSUNE_BROWSER_WINDOW_TITLE` (comma list) to change the defaults.
- macOS activates the first running process named `zen`, `firefox`, or
  `librewolf` through System Events (`osascript`, which may ask for Automation
  permission). Set `KITSUNE_BROWSER_PROCESS` (comma list) to change the defaults.
- Linux does not focus the browser; `focused` is `false`.

The bridge endpoint also accepts `title` and `process` to override these per call.

### `browser_resume(call, resume_token, observed=None)`

`automation` group. Continues a `browser_batch`, `browser_parallel` or
`browser_workflow` call that did not finish `ok`, from the checkpoint the bridge
keeps in memory for 30 minutes (at most 20). `call` and `resume_token` come from
that call's response. Steps that ran `ok` are not sent again. Each `unknown` write
needs an `observed` entry by its `progress` path (`"1"`, `"0.1.2"` for a parallel
sequence step): `"not_applied"` sends it again, `"applied"` marks it `ok` without
sending it. Read-only mode, the URL policy and ask-before-act apply to every resumed
step. The response has the original call's shape (`results`, `progress`, `status`,
`summary`; a workflow adds `workflow` and `steps`). While the call is still not `ok`,
the response also has `call` and a new `resume_token`. See
[Checkpoint and resume](checkpoint-resume.md) for each decision and error.

### WordPress workflow

`browser_wp_html` is removed. Run
`browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`
from a browser logged into WordPress. The workflow navigates the current tab,
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
{ "bridge": "0.7.0", "api": "1", "extension_connected": true, "extension_version": "0.7.0", "extension_build": "dev", "extension": 25 }
```

## Page Data (GET, cached 5s except page-info)

### GET /api/page-info
Page metadata plus current dialog policy and recent dialog history. Never cached.
Also accepts POST with `{ "tabId": 123 }`. MCP tool: `browser_page_info(tab_id=None)`.
```json
{ "url": "...", "title": "...", "readyState": "complete", "lang": "en", "width": 890, "height": 992, "forms": 0, "links": 1, "images": 0 }
```
`readyState` is the document's `document.readyState`; see
[Snapshot readiness](#snapshot-readiness).

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
Accessibility tree (depth=6). POST accepts `tabId`, `selector`, `ref` and
`maxDepth`, scoped as in [`browser_dom`](#browser_domtab_idnone-selectornone-refnone-max_depthnone).
```json
{ "tree": { "ref": "s1e0", "tag": "body", "children": [...] }, "refs": 42 }
```
Sensitive fields report `redacted`/`hasValue` instead of `value`. A tree past the
20,000-char cap also has `"truncated": true` and a `hint`.

### GET /api/forms
All form fields on the page. Sensitive fields have `redacted`/`hasValue`
instead of `value` (see [Sensitive-field redaction](#sensitive-field-redaction)).
```json
{ "fields": [{ "ref": "e0", "tag": "input", "type": "password", "name": "password", "id": "password", "placeholder": "", "redacted": true, "hasValue": true, "label": "Password", "required": false, "selector": "#password", "bounds": [...] }] }
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
Without that flag, return full PNG data by default, or JPEG with `format: "jpeg"`.
Viewport requests accept `tabId`. Element requests require `selector` and accept `tabId` and `scale` (1–4); full-page requests
accept `tabId`. A capture never shows the tab (`tabs.captureTab`). Captures retain
existing canvas and scroll/segment limits and can truncate very large documents.

MCP tool `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False, ref=None, element=None, format="png")`
returns `[Image, metadata]` by default, giving the model actual MCP image content.
`save_to` writes the full requested PNG/JPEG and returns metadata with `saved_to`
instead. `data_url=True` returns the raw image data in metadata.
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
are not traversed. Field names from `placeholder`, `aria-label` or `aria-labelledby`,
which snapshots show as names, are not page text and never match. Returns
`{ok, text, text_gone, elapsed}`; timeout returns `ok: false` with an error. When the
text of a timed-out `text` wait is the name of a visible element, the result also has
`matched_as: "accessible_name"`, `name_matches` (up to 3 `{ref, role, name, source}`,
where `source` is the attribute) and a `hint`: act on the ref, or wait for text the page
shows. The bridge polls single reads (see `browser_wait_for`); a
zero timeout performs one immediate check.

## Navigation (POST)

### POST /api/navigate
`{ "url": "https://example.com", "tabId": 1, "expectTitle": "Example" }`
- `tabId` optional (defaults to active tab)
- `expectTitle` optional — warns if title doesn't match (catches silent redirects)
- With `X-Kitsune-Protect-User-Tabs: 1`, a tab the agent did not open returns `user_tab`;
  `confirmToken` is its token after the user approved (see
  [Protected user tabs](#protected-user-tabs)). The same holds for `/api/back` and `/api/forward`.

### POST /api/new-tab
`{ "url": "https://example.com" }` — url optional, defaults to about:blank.
Opens an agent-owned tab in the background (it is not shown; see
[Shared browser](#shared-browser)). Returns `{ "ok": true, "tabId": 123 }` immediately.

`{ "url": "https://example.com", "wait": true, "timeout": 15000 }` waits up to
`timeout` ms (default 15000) for the load and returns
`{ "ok": true, "tabId": 123, "url": "https://example.com/after-redirect", "status": "complete" }`.
On timeout the tab stays open and the result is
`{ "ok": false, "timedOut": true, "tabId": 123, "url": "...", "status": "loading" }`.

### POST /api/switch-tab
`{ "tabId": 123 }` shows the tab. It switches the user's view, so send it only on the
user's request; while the user is using the tab it would hide, it returns `user_active`.

### POST /api/close-tab
`{ "tabId": 123, "force": true, "confirmToken": "…" }` closes an agent-owned tab. A
user tab returns `user_tab` with a confirm token; `confirmToken` is that token after the
user approved (see [Agent-owned tabs](#agent-owned-tabs)). `force` disarms
`beforeunload` first.

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
  `state.reason: "no_receiver"` means the last check found the reloaded page loaded and
  accepting the content script, but nothing answered its ping. While the page stays silent,
  commands fail with `content_unavailable` (`state.reason: "no_receiver"`) before anything is sent.

Ready means the content script answered a ping after the load, not that an injection
succeeded. Until the deadline, each check injects and pings again, so a page that starts
answering late still ends the wait as ready. Each ping waits at most 1 s; the injection is
one browser call whose own duration the deadline cannot cut short.

## Interaction (POST)

### POST /api/click
`{ "selector": "button.submit" }` — also accepts `ref`, `position`.

The click dispatches `pointerover`, `pointerenter`, `pointerdown`, `mousedown`,
`pointerup`, `mouseup` and `click` on the target, at its center or at the given point,
as a primary-button press: `button: 0`, with `buttons: 1` until the release and `0`
from `pointerup` on. `click` is a `PointerEvent` with `detail: 1` and
`pointerType: "mouse"`, and it runs the element's native activation once, as
`el.click()` does; a disabled form control gets no `click`. Limits: every event has
`isTrusted: false` and grants no user activation, and no `pointermove`, `mousemove`,
`pointerout` or `pointerleave` is sent. For pages that need trusted input, use
`browser_trusted_click`; an unmet `expect` whose target is still visible says so in its
`hint` (see [Action outcomes](#action-outcomes)).
Add `"wait": true, "timeout": 10000` to wait for the target first (see
[Actionability wait](#actionability-wait)); the other action endpoints accept the same keys.

### POST /api/trusted-click
`{ "selector": "button.submit", "focus": true }` or
`{ "coords": { "x": 640, "y": 400 }, "coordinateSpace": "screen" }` — optional
`tabId`, `title`, `process`. OS-level click on Windows, macOS and Linux (X11); see
[`browser_trusted_click`](#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue).
A click that does not finish within 35 s returns a `timeout` [action error](#action-errors)
with `retryable: false`.

### POST /api/type
`{ "text": "hello", "selector": "input" }`

### POST /api/fill
`{ "selector": "input#email", "value": "user@example.com" }`
Handles shadow DOM and React/Vue state. A web-component host is filled through the
first input, textarea or select in its open or closed shadow root, else in the shadow
roots of hosts nested inside it, up to 8 roots deep (closed roots through Firefox's
`openOrClosedShadowRoot`). `/api/type`, `/api/set-editable-content` and
`/api/form-fill` use the same lookup. When `/api/fill`, `/api/type` or a
`/api/form-fill` key cannot reach a field, the result is a `not_fillable`
[action error](#action-errors).

### POST /api/scroll
`{ "direction": "down", "amount": 500 }` — optional container `selector` or `ref`;
`element` is an audit-only description.

### POST /api/hover
`{ "selector": "a.link" }`
Returns `{ "ok": true, "tag": "a" }`

### POST /api/drag
`{ "from": "#card", "to": "#done" }` — or `ref` and `toRef`.
Returns `{ "ok": true, "from": "#card", "to": "#done", "via": "html5" }`.
It presses the source (`pointerdown`, `mousedown`) and dispatches the HTML5 drag events
(`dragstart` … `drop`, `dragend`). They count (`via: "html5"`) when the source is
draggable, the page let `dragstart` through and the destination accepted the drop by
cancelling `dragenter`, `dragover` or `drop`, as a browser requires. Otherwise the drag
is replayed as a pointer drag (`via: "pointer"`) for libraries that build drag on
pointer events: ten `pointermove`/`mousemove` steps from the source's center to the
destination's, so a drag threshold is crossed, each in a new task so the page can
render in between; then `pointerup` and `mouseup`. Pointer events (`pointerId: 1`,
`buttons: 1`, `isPrimary`) go to the source, where `setPointerCapture` routes them;
the page's capture itself is not visible to Kitsune, and Firefox can throw
`NotFoundError` from `setPointerCapture` for a pointer it has not seen, such as this
synthetic one. `mouseup` lands on the destination.

### POST /api/find
`{ "query": "search text" }` or `{ "selector": "a.link" }`
Both `query` and `selector` are accepted (normalized to `query` internally).
Returns `{ "results": [{ "ref": "r0", "tag": "a", "text": "...", "selector": "...", "bounds": [...] }] }`
A query that is not a matching CSS selector is split into words (filler words such as
"the" or "in" are dropped). Elements are scored per word they match in their text,
`aria-label`, placeholder, title, alt, id, name or value, or a role word ("button",
"dropdown", "search bar"), with more for matching every word and for the whole query or
a quoted part as a phrase. Only matching elements are returned, best first; when nothing
matches, the reply is `{ "results": [], "hint": "..." }`.

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
Polls until element appears. Returns `{ "ok": true, "found": true, "ref": "r0", "elapsed": 1200 }`;
timeout returns `ok: false, found: false` with an error. The bridge polls single reads (see
`browser_wait_for`); a zero timeout performs one immediate check.

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
  "stopOnWarning": false,
  "tabId": 12
}
```
`tabId` (optional) is the tab for commands that name none; see
[Container progress](#container-progress) for how steps pick their tab.

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

A response whose `status` is not `ok` adds `call` and `resume_token` for
[POST /api/resume](#post-apiresume).

**Batch actions:** navigate, newTab, closeTab, switchTab, click, type, fill, scroll, hover, find, js, pageInfo, pageText, pageTextByTabId, tabs, forms, dom, highlight, screenshot, waitForElement, waitForResult, sleep, parallel, media (see [Media](#media)).

### POST /api/resume
Continue a batch, parallel or workflow call that did not finish `ok` (`browser_resume`).
```json
{ "call": 42, "resume_token": "x3J...", "observed": { "1": "not_applied", "0.1.2": "applied" } }
```
Returns the original call's response shape; see [Checkpoint and resume](checkpoint-resume.md).

## Workflows (NEW)

### GET /api/workflows
List workflow files bundled in the `kitsune/workflows` package directory.
```json
{ "workflows": ["gmail-template-draft", "test", "wp-html"], "directory": ".../site-packages/kitsune/workflows" }
```

### POST /api/workflow
Execute a named workflow from the workflows directory.
```json
{ "name": "test", "variables": { "url": "https://example.com" }, "tabId": 12 }
```
`tabId` (optional) is the tab for steps that name none.
Workflows are `.md` files with JSON code blocks. Variables use `{{name}}` syntax.
Returns `{ "workflow": "test", "steps": 4, "results": [...], "progress": [...], "status": "ok", "summary": "..." }`. A workflow stops at its first failed step; `progress` marks the rest `skipped` (see [Container progress](#container-progress)). A workflow that did not finish `ok` adds `call` and `resume_token` for [POST /api/resume](#post-apiresume); resuming is refused if the workflow file changed.

## History and bookmarks (POST)

`POST /api/history-search` `{query, limit?, since?}` (`since` in epoch
milliseconds), `POST /api/history-visits` `{url}`, and `POST /api/bookmarks-search`
`{query, limit?}` back the `history` tools; see
[History and bookmarks](#history-and-bookmarks). The bridge adds the policy scope.

## Cache Control (NEW)

### POST /api/cache
Control the response cache (pageText is cached for 5s by default; tab inventories are always read fresh).

Clear cache: `{ "action": "clear" }`
Check status: `{ "action": "status" }` → `{ "entries": 3, "ttl": 5 }`
Set TTL: `{ "action": "ttl", "seconds": 10 }` — `seconds` must be a non-negative
number; anything else answers HTTP 400 and keeps the current TTL.

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
