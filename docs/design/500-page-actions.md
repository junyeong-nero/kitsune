# #500 implementation checkpoint: brokered page actions

Date: 2026-11-11. Branch `feat/500-shared-authority`, draft PR #509; follows
[500-read-and-refs.md](500-read-and-refs.md) (`9b4a09c`).

**Still not an enabled flat surface or a delegated-authority verdict.** `FlatBroker`
remains a trusted host object: not registered in MCP, not reachable by a worker.
12 of the 18 leaf operations now have a native adapter.

## What is implemented

Eight operations, each as one fixed native command on a reviewed receipt route:

| Operation | Native action | Notes |
| --- | --- | --- |
| `browser_click` | `click`; `doubleClick` for `click_count: 2` | button and modifiers passed through |
| `browser_input` | `fill` (`replace`), `type` (`type`, appends) | plain text only |
| `browser_select` | `selectOption`, one exact value | see differences below |
| `browser_check` | `checkBox` | |
| `browser_press` | `keypress` | one press; modifiers mapped to the extension's flags |
| `browser_hover` | `hover` | |
| `browser_drag` | `drag` | each end a ref or a selector |
| `browser_scroll` | `scroll` | viewport or one target |

- The body is built from the admitted arguments only. It never carries `first`,
  coordinates, hold/duration, repeat, OS-level input or a confirm token.
- Every targeted action sends `wait: true` with the caller's `timeout_ms`: the page
  waits that long for the target, then acts once. It is not a retry.
- `includeSnapshot: false` on every action, as #499 requires: no post-action
  snapshot. The result is the receipt plus handles of tabs the action opened and an
  allowlisted warning code (`hidden_or_covered`, `popup_blocked`).
- The bridge and extension apply their existing checks unchanged: mode, ownership,
  user activity, URL policy and, in draft mode or with ask-before-act, the target
  inspection (`checkTabWrite`, `describeTarget`). Those commands appear in the
  receipt; only the action itself counts as the dispatch.

### Outcome rules for page actions

| Situation | `outcome` |
| --- | --- |
| Refused by admission (mode, unissued ref, stale handle, bad arguments) | `not_dispatched`, no request |
| Held or refused by the bridge before the action (`needs_user`, `mode_restricted`) | `not_dispatched` |
| Refused by the extension before any page event (`user_tab_read_only`, `user_active`, `stale_ref`, `ambiguous_target`, `not_found`, `unsupported_selector`, `not_actionable`) | `not_dispatched` |
| The extension replied `ok` | `dispatched`, never `observed` |
| Any other error reply, including `tab_closed` | `unknown` |
| Sent without a reply (timeout, disconnect) | `unknown` |

`tab_closed` is `unknown` for a page action because a click can close its own tab;
the handle is retired either way.

### Approval boundary

With ask-before-act on, a held click returns `needs_user` / `not_dispatched`. The
bridge's confirm token, its expiry and the described target stay in the bridge: the
result's error state is empty. The caller has no argument that carries a token
(`confirm_token`, `confirmToken`, `userTabApproved`, headers are all
`invalid_arguments`), and repeating the identical call is held again. Because
`browser_resume` has no adapter yet, **a held action cannot be continued through the
broker at all**. That is safe but incomplete: #496 approval and resume remain open.

## Differences from the #499 contract found here

1. **`browser_select` accepts one exact value only.** Several values, and
   `by: "label"`, return `unsupported_variant`. The extension matches several
   values by value and then by text, and a label by its first match; neither is
   the contract's strict choice.
2. **`ambiguous_target` returns no candidates.** The contract asks for bounded
   matching context. The extension's candidates carry new refs, which would have
   to be registered for the session first; the broker drops the error state.
3. **`timeout_ms` bounds the target wait, not the action.** A click that starts a
   navigation can outlast it; the call then ends `timeout` / `unknown` while the
   action continues.
4. **`browser_input` with `mode: "type"` appends** to the field's current value.

Earlier open differences (compact read without URL/title, no `browser_query`,
fixed native deadlines for `goto` and snapshots) are unchanged.

## Evidence

Private HOME, synthetic token, private ports throughout. Python 3.12.6.

| Check | Result |
| --- | --- |
| Broker tests, scripted extension over real HTTP/WebSocket | 28 passed (7 new) |
| Real browser, focused broker scenarios | 3 passed, zero skips |
| Isolated Python discovery | **1,352 tests / 240 skips / zero failures or errors**, 214.435 s |
| Node | 70 harnesses passed; extension source unchanged |
| Inventory map | 573 entries / 48 groups / zero errors |
| Full real-browser discovery | **258 tests / 10 skips / zero failures or errors**, 1,035.903 s, one run |
| Python 3.10 grammar, `git diff --check` | Passed |

Default-port connection attempts in the Python parent: zero in each wrapped run.

The full real-browser run preceded the last wire test (the held action). Only that
test file changed afterwards; the final Python discovery includes it.

### One run outside the isolation wrapper

After the final discovery, `test_flat_broker.py` was run once more with plain
`python -m unittest`, without the private HOME, test token or audit hook. That was
a mistake in the procedure, not a planned check.

- The file imports `kitsune.bridge`, which at import reads the token file in the
  real `~/.kitsune` (or creates one if none exists). The existing token was read
  into that process's memory. It was not printed, logged or stored.
- Nothing there was modified: the directory's and the token file's modification
  times are from October, checked by timestamp only.
- The tests themselves use a private bridge on spare ports and patch the client's
  endpoint and credentials, and the broker never starts a bridge. Without the
  hook, the absence of default-port connections in that run is not recorded.
- The same file was then rerun under the wrapper: 28 passed, zero default-port
  attempts in the Python parent. That rerun is the result counted above.

### What the real-browser scenario shows

`test_page_actions_through_the_broker`, private Zen profile, temporary add-on. The
fixture page records every event that reaches a control; the test reads that record
and the field values back through a separate legacy request, not the broker.

- Click by ref, right-click by selector and double-click each fired once, in order.
- `replace` set the field to `Ada`; `type` made it `Ada L`.
- Select, check, uncheck, Control+Enter, hover and drag each produced its event;
  scrolling moved the page by 400 px and back to the top.
- An ambiguous selector, a missing target (after its 300 ms wait), an unissued
  ref, an unsupported select variant and two actions on the user's tab were all
  `not_dispatched`, and the page's record was unchanged afterwards.
- A draft caller filled the ordinary field. Filling the password field was
  refused by the bridge's inspection (`mode_restricted`) with no `fill` in the
  receipt and the field still empty; a draft click was refused.
- A read-mode caller could scroll but not fill.
- A `target=_blank` link opened one tab, which got a handle and was readable.

### Failures during this step

- **A new wire test failed once because of the test.** Its helper connected a new
  scripted extension per caller, which invalidated the earlier caller's binding;
  the read-mode scroll was correctly refused as stale. The helper now reuses one
  extension. A separate script confirmed scroll works in all three modes.
- **The first real-browser run failed on the fixture.** The page kept its event
  record in a page variable, which the content script cannot see. It is now an
  element. No adapter change was needed; the scenario then passed.

Retained homes: `/tmp/kitsune-500-actions-first.p7vJzG` (failing helper),
`/tmp/kitsune-500-actions-focus.P8Fekj`, `/tmp/kitsune-500-actions-e2e.QInvJj`
(failing fixture), `/tmp/kitsune-500-actions-e2e.xM5efX`,
`/tmp/kitsune-500-actions-final-unit.wWxCQE`, `/tmp/kitsune-500-actions-final-focus.sFFgaD`,
`/tmp/kitsune-500-actions-full-e2e.gX6kv9` (full run).

## Still missing for #500

`browser_query`, `browser_wait`, `browser_screenshot` and images,
`request_tab`/`ask_user`/`resume` with #496 approval, revocation of an in-flight
call, audit linkage of flat call ids, MCP registration, and the WASI worker with
its IPC and actual-entry-point negative tests.
