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
2. If no bridge answers, `kitsune` starts one as a detached daemon, replacing a bridge of a
   different build. A file lock serializes starts across agents.
3. The extension reconnects to the daemon by itself; the first tool call after a start waits up
   to 16 s for it.
4. The daemon outlives the agent; the next agent reuses it.

## Snapshots and refs

A snapshot is a compact accessibility-style tree of the page:

```text
- main ""
  - heading "Welcome" [ref=e0] [level=1]
  - button "Log in" [ref=e1]
```

Actions take a ref (`e1`) or a selector. A write's selector must match one element, else
`ambiguous_target`, unless `first=True` ([Strict selectors](api-reference.md#strict-selectors)).

Refs stay valid while their node stays attached in the same document, across later
snapshots. Navigation, node replacement or an extension reload invalidates them; a stale ref
reports `element_removed`, `not_in_snapshot` or `other_document`. Take a new snapshot, and never
repeat a write just to refresh refs ([Ref lifetime](api-reference.md#ref-lifetime)).

Actions return a post-action snapshot (at most 4,000 characters). Clicks, typing and similar
actions return a delta: the target and what changed, with `- removed: …` lines for closed parts.
A click that opens a menu or dialog also returns `opened_popup` with its items' refs
([Opened popups](api-reference.md#opened-popups)). Submits and navigations return a full
snapshot. `KITSUNE_POST_ACTION_SNAPSHOT` in the bridge environment changes this
([Post-action snapshots](api-reference.md#post-action-snapshots)).

`browser_snapshot(text=True)` adds visible text, URL and title in one bounded call. The tree is
built from the DOM, not Firefox's accessibility tree; shadow roots and iframes, including
cross-origin ones, are read and acted on ([Frames](api-reference.md#frames)).
