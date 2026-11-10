# #500 implementation checkpoint: brokered wait and screenshot

Date: 2026-11-11. Branch `feat/500-shared-authority`, draft PR #509; follows
[500-page-actions.md](500-page-actions.md) (`dde7cca`) and the ref-selector
correction in [500-read-and-refs.md](500-read-and-refs.md) (`0340c13`).

**Still not an enabled flat surface or a delegated-authority verdict.** `FlatBroker`
remains a trusted host object: not registered in MCP, not reachable by a worker.
14 of the 18 leaf operations now have a native adapter.

## `browser_wait`

One typed condition per call, on the existing wait routes:

| Condition | Native action | What is sent |
| --- | --- | --- |
| `text`, `text_gone` | `waitForText` | the literal text |
| `target` | `waitForElement` | the ref or selector |
| `url`, `title` | `waitForUrl`, `waitForTitle` | `^…$` around the value with every regular-expression character escaped |
| `network_idle` | `waitForNetworkIdle` | nothing but the timeout |

- **`timeout_ms` is the native deadline here**, unlike the earlier adapters: the
  bridge polls text and target waits until it, and the extension times URL, title
  and idle waits with it.
- Text and target waits are single page reads polled by the bridge. The broker
  sets the poll interval so one wait is at most about 100 reads, inside a
  receipt's 128-command limit.
- A met wait is `ok`, `observed`, with the condition kind and elapsed time; a text
  wait met by its first read adds `already_present`. A met target wait returns the
  element's ref and registers it for the session.
- **An unmet wait is `condition_unmet`, `observed`, retryable.** The page answered
  and the condition did not hold. This is distinct from `timeout`, where no
  answer arrived.
- Page text, titles and URLs in the extension's reply are not copied.
- No JavaScript predicate, caller-supplied regular expression, idle threshold or
  "new text only" option can be sent.
- Read and draft callers may wait for text only, as #499 specifies; the bridge's
  mode routes agree.

A polled wait's last read can be cut off at the deadline. For reads, the receipt
outcome is now `dispatched` when at least one read was answered; an unanswered
read changes nothing on the page.

## `browser_screenshot`

| Scope | Native action |
| --- | --- |
| `viewport` | `screenshot` |
| `element` (ref or selector) | `elementScreenshot` |
| `full_page` | `fullPageScreenshot` |

- The request carries the tab, the target and `imagePreview: true`. No format,
  scale or file path can be sent, and nothing is written to disk.
- The reply's data URL must be `image/png` or `image/jpeg` in strict base64, start
  with that type's signature, and decode to at most 8 MiB. Anything else is
  `invalid_bridge_response` (or `output_limited`), with no image.
- The image is returned beside the result as `BrokerReply.images`
  (`handle`, `mime_type`, `data`). The JSON result carries only the handle, type
  and byte count. The page's URL and title in the reply are not copied.
- A capture is `observed`. A failed viewport capture is a failed read. A failed
  element or full-page capture is `unknown` unless the extension refused it before
  touching the page, because those captures scroll the page while they work.
- Act mode only, as #499 specifies.

The extension's preview is its existing inline form: the PNG when it is under
about 1 MB of base64, otherwise a downscaled JPEG. That matches what the current
screenshot tool returns, and keeps images far below the 8 MiB limit.

## Defect found by a test: an unexpected reply shape raised

The first screenshot run failed with an `AttributeError` out of `broker.call` when
the scripted extension returned a list as `dataUrl`. The type is now checked, and
`call` turns any shape error into `invalid_bridge_response`. A new test sends six
malformed replies (list, string, number, null, boolean, and an object whose fields
all have wrong types) to ten kinds of call and requires a JSON-serialisable result
each time.

## Differences from the #499 contract found here

1. **A target wait is limited to 30 s and a `network_idle` wait to 25 s**
   (`unsupported_variant` above that). The bridge's own allowance for those two
   routes is fixed; honouring 60 s needs a bridge change.
2. **A selector target wait takes the first visible match**, not a unique one.
3. **`condition_unmet` is a new error code**; #499 does not name the result of an
   unmet wait.
4. **Image limits across calls are not enforced here.** The contract's four images
   and 16 MiB per run belong to the script runner (#501); the broker bounds one
   image per call.
5. **Screenshots are the extension's preview**, so a large capture arrives
   downscaled as JPEG rather than as the full PNG.
6. **`timeout_ms` does not bound a capture.** The bridge allows an element capture
   20 s and a full-page capture 60 s.

## Evidence

Private HOME, synthetic token, private ports throughout. Python 3.12.6. Every
unit run used `/tmp/kitsune-500-unit-run.sh`, which applies the isolation of
[499-validation.md](499-validation.md) and counts default-port connections.

| Check | Result |
| --- | --- |
| Broker tests, scripted extension over real HTTP/WebSocket | 36 passed (8 new) |
| Real browser, focused broker scenarios | 6 passed, zero skips |
| Isolated Python discovery | **1,364 tests / 243 skips / zero failures or errors**, 228.768 s |
| Node | 70 harnesses passed; extension source unchanged |
| Inventory map | 573 entries / 48 groups / zero errors |
| Full real-browser discovery | **261 tests / 10 skips / zero failures or errors**, 1,051.812 s, one run |
| Python 3.10 grammar, `git diff --check` | Passed |

Default-port connection attempts in the Python parent: zero in each run.

### What the real-browser scenarios show

Private Zen profile, temporary add-on (not the signed build).

`test_waits_through_the_broker`, on a page that changes 1.5 s after it loads:

- A wait for text that appears later was met by a later read (several reads in the
  receipt, no `already_present`); repeating it reported `already_present`.
- `text_gone` was met; text that never appears was `condition_unmet` within 3 s of
  a 700 ms wait.
- Title `Done (1)` was met. `Done .1.`, which matches as a regular expression, and
  the prefix `Done` were both unmet: the comparison is literal and whole.
- A target wait returned the ref of a button added later; a click by that ref
  changed the title.
- After a link click, a wait for the exact new URL was met; `/o.e` and the origin
  alone were unmet. `network_idle` was met.
- A read-mode caller could wait for text and was refused URL, idle and target
  waits.

`test_screenshots_through_the_broker`:

- Viewport, element (by ref and by selector, same size) and full-page captures
  each returned one PNG; the element image was smaller than the viewport's and the
  full-page image more than twice its height.
- The page had been scrolled to 300 px. It was at 300 px after all captures.
- An unissued ref and a missing selector returned no image and `not_dispatched`.
- A full-page capture of the tab the user is looking at was refused
  (`user_tab_read_only`); a read-mode caller was refused any screenshot.

### Failures during this step

- **Four subtests of a new wait test had wrong expectations.** The bridge reports
  its own elapsed time for polled waits, and marks text met by the first read
  `already_present`. The assertions now state that.
- **The `dataUrl` type defect** above.

Retained homes: `/tmp/kitsune-500-wait-first.Uk8kfP` (wrong expectations),
`/tmp/kitsune-500-wait-focus.IG5V6F`, `/tmp/kitsune-500-wait-e2e.FoyKmB`,
`/tmp/kitsune-500-shot-first.SntEmd` (the defect), `/tmp/kitsune-500-shot-focus.D5tHDE`,
`/tmp/kitsune-500-shot-e2e.r7lagn`, `/tmp/kitsune-500-shot-unit.8zJWXp`,
`/tmp/kitsune-500-shot-full-e2e.UROmbY` (full run).

This full run also covers the ref-selector correction (`0340c13`), whose own full run
was deferred to this checkpoint.

## Still missing for #500

`browser_query`; `browser_request_tab`, `browser_ask_user` and `browser_resume`
with #496 approval, so a held action still cannot be continued through the broker;
revocation of an in-flight call; audit linkage of flat call ids; MCP registration;
and the WASI worker with its IPC and actual-entry-point negative tests.
