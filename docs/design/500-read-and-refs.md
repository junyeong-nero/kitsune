# #500 implementation checkpoint: brokered read/find and caller-bound refs

Date: 2026-11-10. Branch `feat/500-shared-authority`, draft PR #509; follows
[500-native-dispatch.md](500-native-dispatch.md) (`f011c33`).

**Still not an enabled flat surface or a delegated-authority verdict.** `FlatBroker`
remains a trusted host object: not registered in MCP, not reachable by a worker.
4 of the 18 leaf operations now have a native adapter.

## What is implemented

| Piece | File | Role |
| --- | --- | --- |
| Ref registry | `flat_session.py` | A ref is usable only by the session and tab it was returned to |
| `browser_read` | `flat_broker.py` | One full bounded snapshot (`snapshot`) or the same with prose (`text`) |
| `browser_find` | `flat_broker.py` | Literal, case-insensitive search of one snapshot, in the broker |
| Receipt route | `dispatch_receipts.py` | `/api/snapshot` joins the reviewed routes (eight) |

### Refs

- `FlatSession.admit` checks every ref-bearing argument of the contract
  (`target`, `source`, `destination`, `condition.target`). A ref the session was
  not given for that tab fails `stale_ref` before anything is sent.
- The broker registers only refs in what it delivers: every ref line of a read's
  content, and the returned lines of a find. A ref cut off by `max_chars` or by a
  find `limit` is not registered.
- Refs are parsed with the extension's own rule: the ref follows the line's
  JSON-quoted name. Ref-like text inside a quoted name or value is page data.
- Refs end with their tab, connection or session, and when the broker dispatches a
  navigation for the tab. A document the page replaces itself is caught by the
  extension (`stale_ref`, reason `other_document`); the broker then forgets the ref.
- At most 4,096 refs are remembered per tab. Beyond that the oldest are forgotten,
  which only refuses them.

The extension already binds a ref to its tab and document. The registry adds the
session: a caller cannot use a ref it never received, such as one issued to
another client reading the same tab.

### Read and find

- `browser_read` sends `{tabId, maxChars, text, ref|selector}` and nothing else:
  no `sinceObservation` or `skipUnchanged`, so no delta. A reply that is a delta or
  "unchanged" is rejected (`invalid_bridge_response`).
- `browser_find` reads an uncollapsed snapshot of up to 150,000 characters, as the
  existing core `browser_find` does, and searches it in the broker. Matches carry
  the line, its ref if it has one, and up to three ancestor lines. `count` is the
  total; `truncated` is set when matches were left out or the snapshot was cut.
- Redaction is the existing snapshot's: no sensitive field values, redacted URL
  secrets. The broker adds none and removes none.
- Reads need no ownership, as today. Writes to the same tab are still refused.
- A failed read is never `unknown` because of an error reply: it is `dispatched`
  with `retryable` set for transient causes. A read with no reply (client timeout)
  is `unknown` and `retryable`.
- Bounds: a read is at most 12,000 characters (the broker cuts at a line boundary
  if the extension ever returns more), which fits the 64 KiB result limit. A find
  reply may be up to 2 MiB on the wire; its result is still at most 64 KiB.

## Differences from the #499 contract found here

These need an extension change or a contract amendment before the #502 candidate.
None was papered over.

1. **`browser_read` with `format: "snapshot"` returns no URL or title.** The
   extension reports them only with prose (`text`). No document identity token
   exists in either form.
2. **`browser_query` has no adapter.** The extension's query issues no refs,
   reads `checked`, `role` and `label` as raw attributes rather than live state,
   and returns `href`/`src` without the snapshot's URL redaction. Forwarding it
   would give scripts a less redacted read than `browser_read`.
3. **`timeout_ms` is not the native deadline** for read and find either: the
   bridge's own snapshot wait is fixed.

## Evidence

Private HOME, synthetic token, private ports throughout. Python 3.12.6.

| Check | Result |
| --- | --- |
| Session tests | 15 passed (3 new for refs) |
| Broker tests, scripted extension over real HTTP/WebSocket | 21 passed (5 new) |
| Real browser, focused | 3 passed, zero skips (broker read/find, broker tabs/navigation, stale binding) |
| Isolated Python discovery | **1,344 tests / 239 skips / zero failures or errors**, 214.213 s |
| Node | 70 harnesses passed; extension source unchanged |
| Inventory map | 573 entries / 48 groups / zero errors |
| Full real-browser discovery | **257 tests / 10 skips / zero failures or errors**, 1,055.509 s, one run |
| Python 3.10 grammar, `git diff --check` | Passed |

Default-port connection attempts in the Python parent: zero in each wrapped run.

### What the real-browser scenario shows

`test_read_and_find_through_the_broker`, private Zen profile, temporary add-on:

- A read of a form page returns refs for its heading, fields, button and links.
  The password value and a URL token are absent; the token shows as `[redacted]`.
- The `text` form adds the page's prose, URL and title. Prose containing
  `[ref=e424242]` does not make that a usable ref.
- A ref scopes a later read for the session that received it. A second session on
  the same tab is refused the same ref without a request.
- Find returns the three matching links with their refs, finds prose, and finds
  neither the password nor the token.
- The user's tab is readable once it holds a page, and a navigation of it is still
  refused with its URL unchanged.
- After a legacy client of the same owner navigates the tab, the old ref gets the
  extension's `stale_ref`; the second use is refused by the broker. After a reload
  through the broker, a fresh ref is refused before any request.

### Failures during this step

- **Every read failed at first.** The broker required the `ok: true` that write
  replies carry; a snapshot reply has none. The scripted tests did not exist yet;
  an exploratory real-browser run showed `invalid_bridge_response` for all seven
  calls. Reads no longer require it; writes still do.
- **Three new test expectations were wrong**, not the code: a read-mode caller is
  refused a click for its mode before its ref is checked; the largest read cannot
  exceed the result limit; an oversized reply is not retryable.
- **Two real-browser assumptions were wrong.** A blank user tab answers
  `content_unavailable`, not a readable page. The harness's own client may not
  navigate the broker's tab (`user_tab_read_only`), so the test navigates as a
  client of the same owner; the refusal is now asserted as well.

Retained homes: `/tmp/kitsune-500-read-first.nvfZKW` (three failing expectations),
`/tmp/kitsune-500-read-focus.alKCgy`, `/tmp/kitsune-500-read-unit.8HmPpM`,
`/tmp/kitsune-500-read-e2e.xQjc0O`, `/tmp/kitsune-500-read-full-e2e.LCHtJY` (full run).

The legacy click test that failed once in the previous checkpoint's first full run
passed in this one. One pass does not settle that question; see
[500-native-dispatch.md](500-native-dispatch.md).

## Still missing for #500

Page actions (click, input, select, check, press, hover, drag, scroll, wait),
screenshot and images, `browser_query`, `request_tab`/`ask_user`/`resume` with
#496 approval, revocation of an in-flight call, audit linkage of flat call ids,
MCP registration, and the WASI worker with its IPC and negative tests.
