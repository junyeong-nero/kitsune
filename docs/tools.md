# Tools, capabilities and modes

By default Kitsune publishes the 25 tools of [Playwright MCP](https://github.com/microsoft/playwright-mcp)'s
default capabilities, with the same names, descriptions, input schemas and annotations, as
its `core` capability group (see [Core tools](#core-tools)), with Kitsune's `browser_resume`,
which continues a call that stopped for you, and `browser_ask_user`, with which the agent asks
you for what it cannot do (#376). Kitsune's other tools are in the other
groups. Set `KITSUNE_CAPS` in the MCP server environment before starting it;
restart the MCP client after changing it. The comma-separated list selects exactly those
groups (include `core` to retain the default tools). `all` enables all 97 tools in every
group except `storage` and `history`, which read personal data and must be named
(`KITSUNE_CAPS=all,storage,history`). Whitespace and case are ignored; unknown group names
fail at startup.

For trusted local Python composition on macOS, opt in with `KITSUNE_SCRIPTING=1`
to select the [14-tool minimal surface](minimal-tools.md), including
`browser_run_python`. Less-frequent operations use the Python API; gateway
search/call/publish is absent. Python has host file/network access, **not a sandbox**.
The supplied browser API keeps this client's browser grants; those grants do not
constrain arbitrary local code. See [Python scripting](python-api.md#python-scripting-through-mcp).
Without this opt-in, the legacy tools and configuration described below are unchanged.

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "core,browse,read"
```

| Group | Tools for |
| --- | --- |
| `browse` | Scrolling, forward navigation, checkboxes, focus, form submits, real mouse clicks |
| `read` | Page text and Markdown, forms, DOM, links, images, metadata |
| `inspect` | Element bounds, computed styles, selector checks, highlights |
| `tabs` | Pinning, muting, zoom, windows, tab tags, keep-alive |
| `automation` | Batches, retries, workflows, parallel tabs, and resuming an interrupted batch |
| `network` | Downloads, request interception and capture |
| `admin` | Connection status, logs, audit, cache, policy |
| `storage`, `history` | Cookies, page storage, clipboard and saved sessions; history and bookmarks. These read personal data, so `all` leaves them out and you name them yourself |

The [tool list](tool-list.md) names every tool in each group.

Earlier releases' `devtools`, `parallel`, `windows` and `session` groups are gone, and
naming them fails at startup. `devtools` became `browse`, `read`, `inspect`, `admin` and
(`browser_workflows`, `browser_workflow`) `automation`; `parallel` became `automation`
and, for tab tags and keep-alive, `tabs`; `windows` joined `tabs`; `session` joined
`storage`, which `all` no longer includes.

A tool left out by `KITSUNE_CAPS` is absent from `tools/list`, and clients usually say
nothing about it. Calling it anyway returns an MCP error with
`code: "tool_not_published"` whose `hint` names the setting that publishes it, for
example `KITSUNE_CAPS=browse,core` for `browser_scroll`. `browser_status` (`admin`)
reports this server's `mcp_tools`: the enabled `caps`, how many known tools are
`unpublished`, and its audit `client` and tab `owner` ids.

The bridge enforces the groups too. The MCP server sends its enabled groups with every
request, and the bridge refuses a route or a container step (of `browser_batch`,
`browser_parallel`, `browser_retry`, `browser_broadcast`, `browser_workflow` or
`browser_resume`) that belongs to another group with `code: "capability_restricted"`.
So `automation` runs only commands of the groups you enabled: with
`KITSUNE_CAPS=core,automation`, a batch `cookies` step is refused until you add
`storage`, and a workflow needs the groups of its steps. Like task modes, this limits
what an agent can do through this MCP server; it is not a boundary against other local
programs that hold the bridge token.

## Finding tools without a restart

`KITSUNE_CAPS` decides which tools this server runs. `KITSUNE_PUBLISH` decides which of
them `tools/list` shows when a session starts; unset, it is `KITSUNE_CAPS`, so nothing
changes. Set it to fewer groups to keep the list small and let the agent reach the rest
in the same connection:

```toml
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "all"
KITSUNE_PUBLISH = "core"
```

With Claude Code:

```bash
claude mcp add --scope user kitsune -e KITSUNE_CAPS=all -e KITSUNE_PUBLISH=core -- uvx --from kitsune-browser-mcp kitsune
```

`tools/list` then has the 27 core tools and three gateway tools:

- `browser_tools_search(query, limit=5)` finds the tools this server runs but does not
  list, best match first, each with its full description, input schema and annotations.
  An empty query lists their names by group.
- `browser_tools_call(name, arguments)` runs one and returns its own result, images
  included, and its error status.
- `browser_tools_publish(names)` adds tools, or whole groups, to this session's
  `tools/list` and sends `notifications/tools/list_changed`; clients that refresh on it
  can then call them directly.

`KITSUNE_PUBLISH` takes the group names `KITSUNE_CAPS` does, and only groups
`KITSUNE_CAPS` enables: naming another one fails at startup. The gateway tools appear
only when some enabled tool is not published.

The gateway never runs more than a direct call could. It finds and runs only tools
`KITSUNE_CAPS` and `KITSUNE_MODE` permit: `all` still leaves out `storage` and
`history` unless named, and a name it does not permit gets the same `tool_not_published`
as a direct call. A call through `browser_tools_call` takes the direct call's path, so
argument validation, the task mode, confirmation tokens, protected user tabs, redaction
and file name roots apply the same way, and its audit entries record
`via: {"gateway": "browser_tools_call", "tool": …}`. Your client's permission prompt may
show only `browser_tools_call` and its conservative annotations (destructive,
open-world), not the tool it runs, so do not rely on per-tool client prompts for these
calls. `browser_tools_publish` changes only that session's list, never what the server
permits or another session's list. Clients differ in whether they act on
`notifications/tools/list_changed`; the notification is not proof that the model sees
the new tools, and `browser_tools_call` works either way.

This reaches tools that are already installed. Changing `KITSUNE_CAPS` or upgrading
Kitsune still needs a restart.

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
are not enforced policy. The bridge enforces task-mode limits and `browser_set_policy`
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

## Task modes

Set `KITSUNE_MODE=read|draft|act` in the MCP server environment and restart the client.
The default is `act`, preserving the existing tool surface and bridge policy. Mode names
ignore case and surrounding whitespace; unknown names fail at startup.

```toml
[mcp_servers.kitsune.env]
KITSUNE_MODE = "read"
```

| Mode | Published tools and enforced limits |
|---|---|
| `read` | Read tools plus navigation to HTTP(S) pages (or `about:blank`) and scrolling. `browser_tab_list` replaces the multi-action `browser_tabs`; with `new_tab` it opens a background tab that later calls act on. It cannot close or select tabs. No clicks, field writes, JavaScript, submits, confirmations or screenshots/file writes. |
| `draft` | Read surface plus `browser_type`, `browser_select_option`, `browser_fill_form` and, with the `browse` group, `browser_check`. Fills plain text only; no click/keypress/submit/dialog confirmation or JavaScript. Ask-before-act is forced: sensitive fields, payment-like pages and uninspectable targets are refused before filling, without a confirmation token. |
| `act` | Existing behavior, including the independently configured ask-before-act policy. |

Modes intersect with `KITSUNE_CAPS`; they never enable a disabled group. With the
default `core` group, `read` publishes eight tools: list-only tabs, navigate, snapshot,
find, wait for text, console messages and the network request list and details.
`draft` adds type, select-option and fill-form. Page text, forms and DOM need the
`read` group, and scrolling the `browse` group. Extra enabled groups contribute only
their read tools. The personal-data `storage` and `history` groups still require
explicit opt-in.

Restricted surfaces use first-paragraph descriptions and omit `confirm_token`, the approval
fallback for clients without elicitation: they cannot approve.
Calling an unpublished tool by name returns `tool_not_published` with the mode setting
needed to publish it; `browser_status.mcp_tools.mode` identifies a restricted server.
The [gateway tools](#finding-tools-without-a-restart) are listed in every mode and reach
only the tools the mode permits.
The bridge enforces the mode on every request, including calls made by name or directly
over HTTP. A restricted request cannot change policy, run containers or resume an act
call. Draft form-fill inspects every target before writing any field.

Modes are per MCP client: a read client cannot loosen its mode by calling
`browser_set_policy`, and does not change another client's policy on the shared daemon.
The bridge's existing URL/readonly/locked-ask policy can tighten the mode further.
Use an `act` client or the browser itself for a sensitive action. Modes limit agent
commands, not page scripts: navigation can still trigger site behavior and field events
can autosave a draft. They are not an incognito session or an application-level sandbox.
Exact token counts and research/account-lookup task measurements belong to
kitsune-benchmark; character-size guards in this repo are not token measurements.
