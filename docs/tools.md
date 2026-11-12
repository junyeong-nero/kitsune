# Tools

Every tool is available to every client (#525): there are no capability groups or task
modes to configure. Which tools need your approval is set in the Kitsune extension's
options; [Your tabs and approvals](your-tabs.md) lists every check that limits the agent.
`KITSUNE_CAPS`, `KITSUNE_PUBLISH` and `KITSUNE_MODE` were removed in 0.8.0; a
configuration that still sets one fails at startup, so the limit it expected is not
silently dropped.

| Setting | Tools in `tools/list` | The other tools |
| --- | --- | --- |
| Default (`KITSUNE_SCRIPTING` unset or `1`) | 6: the experimental [code-first surface](minimal-tools.md) | Python API; direct recovery via `browser_tools_call` |
| `KITSUNE_SCRIPTING=0` | 30: the 27 `core` tools and 3 gateway tools | [Gateway](#finding-tools-without-a-restart) search, call and publish |

On this experimental branch the server publishes the [6-tool code-first surface](minimal-tools.md),
including `browser_run_python` for trusted local Python composition.
Ordinary reads, inputs and waits use Python. `browser_tools_call(name)` describes a direct
recovery tool; supplying `arguments` executes it. Search and publication are absent.
Old direct names remain callable for compatibility but are not advertised.
Python has host file/network access, **not a sandbox**. See
[Python scripting](python-api.md#python-scripting-through-mcp).

With `KITSUNE_SCRIPTING=0` the server publishes the legacy list instead: the 25 tools of
[Playwright MCP](https://github.com/microsoft/playwright-mcp)'s default capabilities, with
the same names, descriptions, input schemas and annotations, as its `core` group (see
[Core tools](#core-tools)), with Kitsune's `browser_resume`, which continues a call that
stopped for you, and `browser_ask_user`, with which the agent asks you for what it cannot
do (#376), plus the [gateway tools](#finding-tools-without-a-restart) that reach the other
groups' tools.

The groups only sort the tools:

| Group | Tools for |
| --- | --- |
| `browse` | Scrolling, forward navigation, checkboxes, focus, form submits, real mouse clicks |
| `read` | Page text and Markdown, forms, DOM, links, images, metadata |
| `inspect` | Element bounds, computed styles, selector checks, highlights |
| `tabs` | Pinning, muting, zoom, windows, tab tags, keep-alive |
| `automation` | Batches, retries, workflows, parallel tabs, and resuming an interrupted batch |
| `network` | Downloads, request interception and capture |
| `admin` | Connection status, logs, audit, cache, policy |
| `storage`, `history` | Cookies, page storage, clipboard and saved sessions; history and bookmarks. History lookups also need the user to allow them in the extension's options |

The [tool list](tool-list.md) names every tool in each group. `browser_status` (`admin`)
reports this server's `mcp_tools`: the `surface` (`minimal` or `legacy`), on the legacy
surface how many tools are `unlisted`, and its audit `client` and tab `owner` ids.

## Finding tools without a restart

On the legacy surface (`KITSUNE_SCRIPTING=0`) `tools/list` has the 27 core tools and
three gateway tools, which reach every other tool in the same connection:

- `browser_tools_search(query, limit=5)` finds the tools this server runs but does not
  list, best match first, each with its full description, input schema and annotations.
  An empty query lists their names by group.
- `browser_tools_call(name, arguments)` runs one and returns its own result, images
  included, and its error status.
- `browser_tools_publish(names)` adds tools, or whole groups, to this session's
  `tools/list` and sends `notifications/tools/list_changed`; clients that refresh on it
  can then call them directly.

The gateway never runs more than a direct call could. A call through `browser_tools_call`
takes the direct call's path, so argument validation, confirmation tokens, protected user
tabs, redaction
and file name roots apply the same way, and its audit entries record
`via: {"gateway": "browser_tools_call", "tool": …}`. Your client's permission prompt may
show only `browser_tools_call` and its conservative annotations (destructive,
open-world), not the tool it runs, so do not rely on per-tool client prompts for these
calls. `browser_tools_publish` changes only that session's list, never another session's.
Clients differ in whether they act on `notifications/tools/list_changed`; the notification
is not proof that the model sees the new tools, and `browser_tools_call` works either way.

This reaches tools that are already installed. Upgrading Kitsune still needs a restart.

The Kitsune-owned tools take `tab_id` to target background tabs; without it they act on
the [current tab](#core-tools) like the core tools. They share an action
contract (`ref`, `wait`, `expect`, `confirm_token`, `first`; `confirm_token` is the
[approval](your-tabs.md#approving-held-actions) fallback for clients without elicitation) documented on
`browser_check`; see the [API reference](api-reference.md) and the
[tool list](tool-list.md).

Every tool includes MCP annotations. The core tools carry Playwright MCP's; the
Kitsune-owned ones have a human-readable title and read-only, destructive, idempotent,
and open-world hints, annotated for their most dangerous operation. Clients can use
these hints to inform permission prompts; enforcement depends on the client. The hints
are not enforced policy. The bridge enforces `browser_set_policy`
(`readonly`, `allow`/`deny`, and [ask-before-act](api-reference.md#ask-before-act)).

## Core tools

The `core` tools other than `browser_resume` and `browser_ask_user` are Playwright MCP's, published verbatim from
`src/kitsune/playwright_tools.json` (captured from `@playwright/mcp` with
`node scripts/update_playwright_tools.mjs`), so a client or prompt written for Playwright
MCP works unchanged. They drive your own browser through the Kitsune extension and return
Kitsune's compact JSON results, not Playwright's Markdown. Where the two differ:

- **Current tab.** Tools act on the tab `browser_tabs` `new` or `select` made current;
  `select` does not switch your view. It takes any tab, yours and other clients' too, so
  the agent can read it; on a tab it cannot write, the result says `read_only: true`. Kitsune's own tools called without `tab_id`, and
  `browser_batch`, `browser_retry` and `browser_workflow` commands that name no `tabId`,
  act on the current tab too. Until there is one, reads act on the active tab, and writes
  and navigations that name no tab return `no_current_tab` without sending anything
  ([No current tab](api-reference.md#no-current-tab)). An MCP server that restarts starts
  without a current tab; its `no_current_tab` then lists the open tabs its MCP client
  opened before, the most recent first, in `state.ownTabs`, and the hint names that tab,
  so the agent can pass it as `tab_id` (to a core tool, which takes none, the hint names its
  `browser_tabs` index to select). The server does not pick one itself:
  the tab may belong to an earlier conversation. Persistent ownership survives bridge/extension
  restarts and browser session restore for the same client id; handed-off tabs return when
  their session ends ([Tab owners](api-reference.md#tab-owners)). A tab a batch or
  workflow `newTab` opens, or an action on the
  current tab opens (a click on a `target=_blank` link, for example), becomes current,
  and the result names it in `current_tab`.
  `browser_navigate` and `browser_navigate_back` also accept a `tab_id` that Playwright
  MCP's schema does not list. `index` is the position
  in `browser_tabs` `list`, which also marks `active` and agent-`owned` tabs, and the
  `current` one regardless of ownership. Selection does not grant ownership
  ([Protecting your tabs](your-tabs.md#protecting-your-tabs)). `close`
  returns the tabs after the change in the same form; `new` and `select` return the
  `index`, the `tab` they made current in that form, and `tab_count` (#488).
  `close` takes `index` or `tab_id`, else the current tab; without one it returns
  `no_current_tab` (#481). `tab_id` is refused for `list`, `new` and `select`.
  `browser_close` closes the current tab, never the browser; closing a user or other
  client's tab is refused with `user_tab_read_only` by default.
- **Targets.** `target` is a snapshot ref (`e12`) or a CSS selector. Actions require one
  actionable match (else `ambiguous_target`). Actions and scoped snapshots also accept a
  subset of Playwright selectors: `text=` (also `/regex/`), `role=…[name="…"]` (also
  `[name=/regex/]`), `internal:text=`, `internal:role=`, `internal:label=`, `:has-text()`,
  `:text-is()`, `:text()`, `:visible`, CSS lists, `>>` chains (also starting at a ref, as in
  `e12 >> xpath=..`), `nth=N` and `xpath=` (refused on pages holding redacted content); see
  [Target selectors](api-reference.md#target-selectors). Unsupported forms return
  `unsupported_selector` with a hint, not a retryable missing target. Element screenshots
  use the same resolver and capture the exact selected node, including inside shadow roots. Refs
  survive newer observations while their nodes remain attached in the same document; navigation or replacement nodes require new refs.
- **Snapshots.** Default core snapshots include redacted static text and resolved HTTP(S) link
  URLs (same-origin ones as paths; a link repeating the previous sibling link's URL, or, first
  under its parent, the page's, has no `/url` line). Returned inline they are capped at 12,000 characters, cut between blocks so a
  product card or paragraph is never split (a long navigation, banner or footer outside
  `main` is shown as its line and a `… not shown` marker until `main` is listed), with
  `truncated: true` and a hint toward
  `browser_find`, a scoped `target` or `filename` (which saves the whole snapshot; see
  [`browser_snapshot`](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)).
  Existing line clipping, structural
  collapsing and depth rules still apply: past the fifth of a run of similar items the
  snapshot shows `- … N more similar items; snapshot e14 to list all` when that hides at
  least three items whose lines total more than 600 characters (a run of up to seven, or
  of short items, is listed in full), and a snapshot
  with `target` `e14` (the items' container) lists up to 100 of them. `browser_find`
  searches collapsed items too. In the full toolset, `text=False` is compact
  and `text=True` enables explicitly bounded readable output (12,000 final public chars).
  Explicit HTTP `maxChars` and post-action limits also apply after URL redaction; scoped
  snapshots remain useful for large pages.
- **Sliders and repeated keys.** `browser_fill_form` `type: "slider"` and `browser_type` (without
  `slowly`) set a native `<input type=range>` directly. An ARIA slider or spinbutton (`role=slider`
  or `spinbutton` with a numeric `aria-valuenow` and no native input, the usual custom widget)
  is set as a user would: the extension focuses it and presses arrow keys (`Home`/`End` for an
  end) until its `aria-valuenow` is the value, all in one call, and returns the final `value`.
  If the value
  is outside `aria-valuemin`/`aria-valuemax`, the call fails with `value_not_reached` before
  any key; if it is not reached (the steps skip it, the widget stops moving, or 1,000 keys
  or 20 s run out) it fails the same way with `state.value`
  ([Action errors](api-reference.md#action-errors)). `browser_press_key` also accepts a
  `repeat` count (1–100) that Playwright MCP's schema does not list; the extension presses
  the key that many times in one call, a task apart.
- **Dialogs.** `browser_handle_dialog` sets how the current tab answers its next dialogs
  (Firefox answers dialogs synchronously, so one already open cannot be answered).
- **Uploads and drops.** A synthetic click opens no file chooser, so `browser_file_upload`
  fills the file input last clicked (or the input inside it, else the page's only file
  input). `browser_drop` fires `dragenter`/`dragover`/`drop` with the files and data.
- **JavaScript.** `browser_evaluate` needs the developer extension build and runs in the
  content-script context; the result is JSON-parsed.
- **Console and network.** `browser_console_messages` starts capture on first use, so
  earlier messages are missed. `browser_network_requests` lists the page's resource
  timing entries since it loaded; there are no headers or bodies, so
  `browser_network_request` with `part` answers `not_supported`.
- **Screenshots.** `browser_take_screenshot` returns an inline image; `filename` saves
  PNG or JPEG. `type` chooses the encoding; otherwise `.jpg`/`.jpeg` filenames infer JPEG,
  with PNG as the default. Other formats answer `not_supported`; `scale` is ignored.
- **Not supported.** `browser_emulate_media` and `browser_run_code_unsafe` are listed but
  answer `not_supported` (`retryable: false`): an extension cannot override CSS media
  features, and there is no Playwright page to run code against.
- **File names.** A relative `filename` is resolved against `KITSUNE_OUTPUT_DIR` if
  set, else the first `file://` root the MCP client lists (MCP `roots`, asked for on each
  call with a `filename`; no answer within 5 seconds counts as none), else the MCP
  server's working directory. Absolute names and `~` are used as given. `saved_to` is the
  absolute path written.

Ask-before-act holds still apply. On a client with elicitation the MCP server asks the
user and repeats a held call itself (see [Approving held actions](your-tabs.md#approving-held-actions)).
The core schemas have no `confirm_token`, the fallback for clients without elicitation; the
tools accept it anyway, so on such a client, after the user approves a held action, an agent
can repeat the call with `confirm_token` from the `needs_user` error.
