# How it works

```
agent ──stdio──▶ kitsune (MCP server, one per agent)
                    │ HTTP 127.0.0.1:47651 + bearer token
                    ▼
                 bridge daemon (one per user, started on demand)
                    │ WebSocket 127.0.0.1:47652, moz-extension:// origin only
                    ▼
                 Kitsune extension ──▶ tabs (content scripts)
```

1. The agent launches `kitsune` over stdio.
2. If no bridge is answering, `kitsune` spawns `python -m kitsune.bridge` as a
   detached daemon. If its build differs, Kitsune safely replaces it first.
   A file lock serializes starts and replacements across agents.
3. The extension keeps reconnecting to the daemon, so it attaches by itself.
   After `kitsune` starts or replaces a daemon, its first tool call other than
   `browser_status` waits up to 16 s for the extension; it is sent once either way.
4. The daemon keeps running after the agent exits; the next agent reuses it.

## Snapshot and element refs

Start with `browser_snapshot(tab_id=...)` to get a compact accessibility-style
text tree of visible controls, headings, and landmarks:

```text
- main ""
  - heading "Welcome" [ref=e0] [level=1]
  - button "Log in" [ref=e1]
```

Prefer `browser_click(ref="e1", element="Log in", tab_id=...)` and
`browser_type(ref="...", text="...", mode="fill", tab_id=...)`. `element` is a description
for audit logs only; CSS `selector` remains supported. For drag, use `ref` for
the source and `to_ref` for the destination (or the existing selector arguments).
A write action's selector must match one element: several matches fail with
`ambiguous_target` and act on nothing, unless `first=True` (see
[Strict selectors](api-reference.md#strict-selectors)). This includes trusted clicks,
focus/blur, targeted scrolling, fuzzy form-fill keys, and uploads without a selector
on pages with several file inputs. Ambiguous clicks fail before ask-before-act
offers confirmation.

Short refs (`e12`) name attached nodes in one tab/document, tracked by the extension.
New snapshots, `browser_dom`, find and full or delta post-action observations reuse
those refs; an unchanged attached node keeps its ref even if its name or position changes.
Detachment/replacement, frame-document retirement, navigation or content reinjection
invalidates the old identity. Indexes are never reused across observations or tabs;
after an extension reload, take a new snapshot. A stale ref reports `element_removed`,
`not_in_snapshot` (never issued/unknown), or `other_document` in `state.reason`.
An `other_document` error names the one element of the targeted page with the ref's role
and name, if there is exactly one, in `state.same_element`; nothing is done to it.
Take a new scoped observation of the intended tab; never repeat a write merely to
refresh refs. Earlier attached refs remain usable without another observation.

Click, type, fill, select, check, keypress, submit, and navigate responses include
a `snapshot` capped at 4,000 characters (`snapshot_truncated` indicates more
content; `snapshot_hint` gives scope guidance and explains if the target/context
could not fit). `target_ref`, when included, is the acted-on element's ref for a
follow-up action.
For click, type, fill, select, check and keypress it is a delta (`snapshot_delta: true`):
only the target and what the action changed or revealed, such as an opened menu's
options, anchored there instead of at the page top, and a `- removed: …` line for each part it
closed or removed, such as a dialog (#457), folded into one counted line when more than three
share a parent (#471); earlier refs stay valid. A click or
hover that opens a menu, listbox or dialog also returns `opened_popup`: its ref, the ref of
the control that opened it (`opened_by`) and its items' refs, also when a background tab
did not paint it (`rendered: false`; see
[Opened popups](api-reference.md#opened-popups)). Submit,
navigate and actions that load another document return a full snapshot (see
[Post-action snapshots](api-reference.md#post-action-snapshots)).
If snapshot capture fails, `snapshot_error` preserves the action result; take a
new snapshot before using refs. To recover from a stale ref, take a new snapshot
and retry with the new ref; never repeat a write just to refresh refs. See
[Ref lifetime](api-reference.md#ref-lifetime) for the full contract and
[MCP error results](api-reference.md#mcp-error-results) for which results are
returned with `isError: true`.
For reading tasks, `browser_snapshot(text=True)` also returns visible text lines,
URL, title and tab ID in one bounded call (see
[`browser_snapshot`](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)).
To start a task, `browser_tabs(include_active=True)` returns the tab list with that read
of the active tab as `active_page`.
Set `KITSUNE_POST_ACTION_SNAPSHOT=0` in the **bridge daemon's environment** and
restart the daemon to disable these automatic snapshots, or `full` to always return
full ones. Explicit snapshots
remain available. This is a DOM-based accessibility approximation, not Firefox's
platform accessibility tree; open and closed shadow roots are traversed. Same-origin
iframes are read and acted on like the page. `browser_snapshot`, `browser_find`,
`browser_forms`, `browser_dom` and post-action deltas also read the content of cross-origin
iframes (embedded payment forms, sign-in widgets, players), whose refs clicks, typing and
trusted clicks accept (see [Frames](api-reference.md#frames)).
