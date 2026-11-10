# #500 implementation checkpoint: brokered tabs/navigation and dispatch receipts

Date: 2026-11-10. Branch `feat/500-shared-authority`, draft PR #509; follows
[500-connection-binding.md](500-connection-binding.md) (`c50178f`) and
[500-caller-admission.md](500-caller-admission.md) (`8cda854`).

**Still not an enabled flat surface or a delegated-authority verdict.** `FlatBroker`
is a trusted host object. It is not registered in MCP, no worker reaches it, and
only 2 of the 18 leaf operations have a native adapter. #500 stays in progress.

## What is implemented

| Piece | File | Role |
| --- | --- | --- |
| Dispatch receipt | `src/kitsune/dispatch_receipts.py`, `bridge.py` | Bridge-owned record of each native command of one request |
| Bounded reply capture | `bridge_client.py` (`max_response_bytes`) | Opt-in wire limit; legacy callers unchanged |
| Broker | `src/kitsune/flat_broker.py` | `browser_tabs` (list/open/close), `browser_navigate` (goto/back/forward/reload) |

### Dispatch receipts (bridge)

A request that sends `X-Kitsune-Dispatch-Receipt: <call id>` gets
`{"result": <the route's ordinary reply>, "receipt": {...}}`. The receipt lists every
native command the request caused, in order, with one of four states:

| State | Meaning |
| --- | --- |
| `not_dispatched` | Refused or failed before the WebSocket send |
| `unknown` | The send itself raised; bytes may have left |
| `dispatched` | Sent; no reply from the extension (timeout, or it disconnected) |
| `replied` | The extension itself answered |

- The header is accepted only with a connection binding, with one valid call id, and
  on seven reviewed routes (`/api/tabs`, `/api/new-tab`, `/api/close-tab`,
  `/api/navigate`, `/api/back`, `/api/forward`, `/api/reload-tab`). Anything else is
  HTTP 400 `invalid_receipt_request` before a handler runs. Containers and detached
  work are excluded on purpose: a receipt must not call future work complete.
- `complete` is false while any coroutine the handler started is still queued or
  running, so a handler timeout cannot claim that nothing will run later.
- At most 128 commands are recorded; further ones are refused
  (`native_operation_limit`) and the receipt says `limited`.
- The extension's reply is nested under `result` and never merged into the receipt.
- Requests without the header are unchanged. `/api/status` reports
  `dispatch_receipts: 1`.

### Broker

- Every call goes through `FlatSession.admit` and `recheck`, then one fixed
  method/route/body per operation variant. There is no route, header or policy
  argument. The 16 operations without an adapter return `operation_unavailable`.
- Requests carry the caller's mode, user-tab permission, client and owner ids, the
  connection binding and the receipt call id from trusted state. The bridge and the
  extension still apply their own mode, URL-policy, ownership and activity checks.
- The broker never starts or replaces a bridge. It requires a bridge of the same
  build, with receipts, whose diagnosis is `connected`; otherwise `bridge_not_ready`.
- Results contain handles, never backend tab ids. Only allowlisted fields are
  copied: `url`, `title`, `owned`, `active` for a listing; `url`, `status` for an
  open. Snapshots, refs, approval tokens and unknown fields are dropped. Extension
  error codes outside an allowlist become `browser_operation_failed`; extension
  error state is never forwarded.
- Bounds: 256 KiB per bridge reply on the wire, 64 KiB per result. Listed titles
  are cut at 256 characters and URLs at 2,048, flagged per tab and on the result.
- After a reconnect, operations fail (`stale_tab_handle` / `stale_connection`)
  until the caller lists tabs again; listing is the only call that rebinds.

### Outcome rules

| Situation | `outcome` |
| --- | --- |
| Refused by admission, or by the bridge before any command | `not_dispatched` |
| Extension admission refusal (`user_tab_read_only`, `user_active`, `tab_closed`, `invalid_tab_id`) | `not_dispatched` |
| Listing replied | `observed` |
| Open replied with status `complete` | `observed` |
| Open replied while loading; goto/back/forward/reload/close replied | `dispatched` |
| Open timed out but the tab exists | `dispatched`, failure `timeout`, with the new tab's handle |
| Sent without a reply, any other error reply, incomplete receipt | `unknown` |
| Client-side timeout, oversized or unparseable reply | `unknown` |
| Bridge unreachable or token refused | `not_dispatched` |

The admission-refusal row relies on the extension's `tabWriteError`, which answers
before it calls a browser API. That holds for these single-call actions; a
multi-step action must not reuse the rule without its own review.

`goto` is reported as `dispatched`, not `observed`: the extension echoes the URL it
was given rather than where the tab landed, so the reply is not an observation.

## Limits that remain

- **Timeouts.** `timeout_ms` bounds how long the call waits for a reply (plus 2 s
  transport grace). It reaches the native wait only for `open`. The extension's
  load wait for `goto` is fixed at 15 s, and back/forward/reload have no load wait.
  A caller that asks for 60 s on `goto` gets `timeout` at about 15 s; one that asks
  for less gets `timeout`/`unknown` while the navigation continues. Closing this
  needs an extension change and is required before the #502 candidate.
- **Cancellation.** Cancelling or timing out a call does not stop a request the
  bridge accepted. The bridge's scheduler, not the broker, orders a later call
  behind it. A cancelled `open` can leave a tab with no handle until the next list.
- **Listing.** A listing returns every tab's title and URL, as the existing
  `browser_tabs` does. This checkpoint adds no redaction and no new grant.
- **Not implemented:** read/query/find and all page actions, refs bound to
  document and node, images, `request_tab`/`ask_user`/`resume` and #496 approval,
  dispatch-time revocation of an in-flight call, audit linkage of flat call ids,
  MCP registration, the WASI worker and its IPC.
- Python's `frozen` dataclasses and private attributes do not protect these objects
  from code in the host process. The worker must never receive them.

## Evidence

Private HOME, synthetic token, private ports throughout. Python 3.12.6.

| Check | Result |
| --- | --- |
| First broker run | 15 tests, **1 failure**: see below |
| Broker tests, scripted extension over real HTTP/WebSocket | **16 passed** |
| Focused set (flat, connection binding, action errors, handoff) | 102 passed, 24.831 s |
| Real browser, focused (broker scenario + stale-binding click) | **2 passed**, zero skips |
| Isolated Python discovery | **1,335 tests / 238 skips / zero failures or errors**, 216.280 s |
| Node | 70 harnesses passed; extension source unchanged |
| Inventory map | 573 entries / 48 groups / zero errors |
| Full real-browser discovery, first run | 256 tests / 10 skips / **1 failure**, 1,185.992 s: see below |
| Full real-browser discovery, second run | **256 tests / 10 skips / zero failures or errors**, 1,139.563 s |
| Python 3.10 grammar, `git diff --check` | Passed |

Default-port connection attempts in the Python parent: zero in each wrapped run.
The audit hook is not a process-tree firewall.

### Retained failure: a disconnect was recorded as a reply

The first run failed
`test_disconnect_after_dispatch_is_unknown_and_never_replayed`. When the extension
drops, the bridge completes the pending command with a synthesized
`extension_disconnected` result, and the draft marked every completed wait as
`replied`. The broker's outcome was still `unknown`, but the receipt itself was
false evidence. The WebSocket handler now marks a command `replied` only when the
extension's own message arrives; a disconnect leaves it `dispatched`. The test
asserts the receipt state, and a unit case covers the same path.

### Retained failure: one existing real-browser test failed once

In the first full run,
`RealBrowserActionObservationTests.test_a_script_navigation_after_the_click_is_reported`
failed: the click result had no `navigated`. This is a legacy `/api/click` request
that sends no binding or receipt header.

- The fixture button navigates from `setTimeout(..., 200)`. The preserved timeline
  shows the click answered 1,297 ms after it was sent and the navigation starting
  37 ms after that answer.
- Firefox delays a hidden tab's timers to at least 1 s, and the extension waits
  `SETTLE_HIDDEN_MIN_MS = 1100` for them. The page's timer fired after that window.
- The extension is unchanged on this branch (`git diff` against the foundation is
  empty for `extension/`).
- The same test alone, repeated under the same isolation: 8 of 8 passed on this
  tree and 8 of 8 on the foundation tree, which has none of these changes.
- The second full run passed.

This points to a timing margin between the browser's timer throttling and the
extension's settle window, not to the bridge changes. It is not proven: 16 passing
repeats cannot rule out a rare effect of the added send wrapper. The test was not
changed, skipped or retried inside the suite. Artifacts:
`/tmp/kitsune-500-broker-full-e2e-artifacts/kitsune-e2e-20261110T123023Z-24416/`.

### What the real-browser scenario shows

`test_real_browser_flat_broker.py` runs in a private Zen profile with the
temporary-add-on harness, not the signed build. Effects are read through the
harness's own bridge requests rather than the broker's results.

- Open in the background: one new owned tab on the fixture URL, the user's tab still
  shown, the same handle in the next listing.
- `goto`, `back`, `forward`, `reload` on the owned tab: the tab reached the expected
  URL each time; receipts named its backend id.
- `goto`, `reload` and `close` on the user's tab: `user_tab_read_only`,
  `not_dispatched`, URL unchanged.
- Close of the owned tab: gone from the browser; its handle is then stale.
- Read mode: opening a `file:` URL is refused by the bridge; tab count unchanged.

Scripted-extension tests additionally cover the URL deny policy, forged
arguments and foreign handles, reconnects, forged extension fields, an open that
times out, oversized inventories, and receipt-header validation on the wire.

Retained homes: `/tmp/kitsune-500-broker-first.TaHOw1` (failing run),
`/tmp/kitsune-500-broker-focus.kCRZZP`, `/tmp/kitsune-500-broker-e2e.14IYL0`,
`/tmp/kitsune-500-broker-unit.p1uJKg`, `/tmp/kitsune-500-broker-full-e2e.H0C9hc`
(first full run), `/tmp/kitsune-500-broker-full-e2e2.wPuG0j` (second).

Reproduction uses the wrappers in [499-validation.md](499-validation.md) (unit) and
[500-integration-validation.md](500-integration-validation.md) (real browser) with
this worktree's `.venv/bin/python`.
