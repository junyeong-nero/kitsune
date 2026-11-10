# #499: flat tools and a shared delegated operation contract

**Status: proposal under review; not implemented or approved for default rollout.**
Parent #498; draft PR #506. Runtime isolation belongs to #500, execution to #501,
opt-in exposure to #502, comparison to #504, and later JS support to #503.

This document specifies a candidate, not an additional capability preset over
`KITSUNE_CAPS`/publication. It deliberately does not claim the existing trusted
Python API is a sandbox. Neither a small schema nor a safe-looking example proves
a delegated execution boundary.

## 1. Coverage and workload basis

[The explicit map](499-capability-map.json) assigns every inventoried entry to
`direct`, `composition`, `operator`, or `deprecate`. Each group has a replacement,
rationale and explicit coverage loss. Its names are enumerated, not matched by a
catch-all; `scripts/toolset_inventory.py --check-map` fails for new, duplicated or
stale entries. See [inventory scope](499-surface-inventory.md).

The starting workflows are #499's six required examples: page reading, a draft,
multi-tab research, repeated table/form work, a screenshot and user-tab handoff.
Existing target/actionability, editor, protected-tab, screenshot and elicitation
tests supply edge cases. We have **not** mined a user's activity logs or measured
production feature frequency. No feature is removed merely because it looks rare.

The proposal retains ordinary direct actions, including hover, drag, keyboard,
selection and check state; code is useful for repetition/filtering, not mandatory
for a click. Handoff and resume remain first-class despite likely low frequency.
Tab lifecycle and navigation each form one small typed family; there is no public
`call(operation, arbitrary_arguments)` endpoint.

The pilot is **not capability-equivalent to all current interfaces**. For example,
exact rich extraction, file upload/drop, future-dialog policy, OS input, window
management, console/network diagnostics, page evaluation and sensitive personal
context remain available to trusted operators/developers but not the delegated
pilot. The map names these losses, including unsupported *options* of otherwise
retained operations. `browser_close` and `browser_drop` are working features;
only `browser_emulate_media` and `browser_run_code_unsafe` are compatibility stubs.

#504 must distinguish full-product coverage from its declared task subset, include
coverage limitations, and keep C and D's browser operations identical. A favorable
C/D result cannot establish equivalence to every A/B capability or by itself
justify the default migration. Default review must revisit the coverage losses.

## 2. Proposed operation vocabulary

Names below are proposed new schemas, **not Playwright-compatible aliases**. Each
ordinary direct tool has the same operation in the injected SDK. Python methods
are async; JS uses the same snake_case method and JSON field names, taking one
options object rather than Python keyword arguments. No nested `script` method
exists in either SDK. `browser_script` is the outer composition entry point only.

`T` means an opaque, session-bound `tab_id` returned by this interface (not a
browser index or a guessed integer). `Target` is exactly `{"ref": "e12"}` or
`{"selector": "#name"}`. `Result` is the structured envelope in section 5.
All operation time budgets are integer **milliseconds**. Unless noted, the
proposed `timeout_ms` default is 10,000, accepted range 1–60,000; the enclosing
run's remaining deadline can reduce it, never extend it.

| Direct tool | Python operation/signature sketch | Purpose/defaults |
| --- | --- | --- |
| `browser_tabs` | `tabs()`; `new_tab(url="about:blank", timeout_ms=15000)`; `close_tab(tab_id=T, timeout_ms=10000)` | `action=list` by default; `open` is background; `close` must name T |
| `browser_navigate` | `navigate(tab_id=T, url=..., timeout_ms=15000)`; `back`, `forward`, `reload` with T | `action=goto`; other actions take no URL; completion means declared load observation, not application success |
| `browser_read` | `read(tab_id=T, format="snapshot", target=None, max_chars=12000, timeout_ms=10000)` | Snapshot or rendered text; include URL/title/document identity; full bounded result, not an implicit delta |
| `browser_query` | `query(tab_id=T, selector=..., fields=["text"], limit=50, timeout_ms=10000)` | At most 200 results; fields from an explicit allowlist, never arbitrary JS properties |
| `browser_find` | `find(tab_id=T, text=..., limit=20, timeout_ms=10000)` | Literal, case-insensitive search of the authorized snapshot; matching context and refs; at most 100 matches |
| `browser_click` | `click(tab_id=T, target=..., button="left", click_count=1, modifiers=[], timeout_ms=10000)` | Left/middle/right; 1 or 2 clicks; no coordinate/OS-input fallback |
| `browser_input` | `input(tab_id=T, target=..., text=..., mode="replace", timeout_ms=10000)` | Replace value or `type` plain text; no submit flag or HTML mode |
| `browser_select` | `select(tab_id=T, target=..., values=[...], by="value", timeout_ms=10000)` | Values or unique labels; preserve multiple-selection semantics |
| `browser_check` | `check(tab_id=T, target=..., checked=True, timeout_ms=10000)` | Explicit checkbox/radio state; not unrestricted click |
| `browser_press` | `press(tab_id=T, target=..., key=..., modifiers=[], timeout_ms=10000)` | Explicit target and synthetic page keyboard event; act-only |
| `browser_hover` | `hover(tab_id=T, target=..., timeout_ms=10000)` | Ordinary hover without custom pointer trajectory |
| `browser_drag` | `drag(tab_id=T, source=..., destination=..., timeout_ms=10000)` | Both targets in the same tab/document; not data/file drop |
| `browser_scroll` | `scroll(tab_id=T, direction="down", amount_px=500, target=None, timeout_ms=10000)` | Up/down/left/right or top/bottom; absent target means T's viewport |
| `browser_wait` | `wait(tab_id=T, condition=..., timeout_ms=10000)` | One typed text/text_gone/target/url/title/network_idle condition; no page predicate |
| `browser_screenshot` | `screenshot(tab_id=T, scope="viewport", target=None, timeout_ms=10000)` | Element requires target; full_page and viewport forbid it; image, not a host pathname |
| `browser_request_tab` | `request_tab(tab_id=T, timeout_ms=10000)` | Scoped request to use a user-owned tab; never self-approval |
| `browser_ask_user` | `ask_user(reason=..., message=..., tab_id=None, timeout_ms=10000)` | Sign-in, human_check, trusted_input, other; optional tab only for non-page questions |
| `browser_resume` | `resume(call=..., resume_token=..., wait_ms=30000)` | A held flat operation; wait 0–120,000 ms; not script replay or arbitrary legacy-container execution |
| `browser_script` | No SDK equivalent | `language="python"`, `code`, `timeout_ms=30000` (1–120,000); JS added only after the approved Python gate |

These 19 tool slots follow the workflows and recovery requirements; 19 is not a
budget or invariant. Schema/example validation and measured guide cost may expose
a reason to revise them **before** #504 freezes the compared candidate.

Query fields proposed for the pilot are `ref`, `text`, `href`, `src`, `name`,
`value`, `checked`, `type`, `content`, `role`, `label`. Redaction precedes projection
and delivery. Unknown fields fail explicitly. A query is allowed multiple matches;
a targeted mutation is not. Empty query/search results are observations, not errors.

Wait conditions use a `kind` discriminator. Text and text_gone use a nonempty
literal substring; URL/title use a literal equality check (not legacy regex
semantics); target waits for a live, actionable target; network_idle means the
existing 500 ms quiet observation, **not** proof the application finished.
Double-click initially supports only the left button without modifiers; unsupported
combinations fail before dispatch rather than silently ignoring options. All SDK
parameters are keyword-only except the JS options object. Unknown keys,
including `headers`, `confirm`, `mode` on non-input operations, `first`, `filename`
and `policy`, are rejected before dispatch rather than ignored or forwarded.

This table is a signature sketch, not yet the acceptance-complete concrete schema
artifact. The exact JSON Schemas, full SDK signature file and paired validated
examples are still required before this design is review-ready.

## 3. Startup mode and authority

The startup surface is fixed. No agent search/publish step, group selector or
`tools/list_changed` support is needed. A server publishes only the branches
allowed by its startup mode; it also rejects disallowed calls at runtime.

| Mode | Operations available |
| --- | --- |
| `read` | tabs list/open; navigate goto; read/query/find/scroll; wait text/text_gone; ask_user; bounded script composition of these operations |
| `draft` | read plus input/select/check |
| `act` | all proposed operations, still subject to grants, ownership, activity, URL policy, output rules and external approval |

Read/draft do **not** gain screenshot, close, back/forward/reload, arbitrary keys,
resume, request-tab or general wait routes merely because they are part of a
multi-action tool. This follows the existing route/action restrictions, not just
its visible tool names. A read/draft-created background tab may require operator
cleanup; cleanup does not grant the worker an act-only method.

`read` is not an assurance that navigation causes no network request; `draft` is
not an assurance against a website's autosave behavior. They constrain known
browser operations. Typed input still runs the existing editor, sensitive-field,
ownership and activity checks; it does not bypass them by choosing another route.

Effective authority is the intersection of server startup authority, the caller's
mode and scope, current URL policy, current tab ownership/approval/activity state,
and any narrower run limits. Each leaf operation and each resumed operation
rechecks it at the trusted boundary and actual dispatch. Script input cannot
supply or widen any part of that context. A successful preflight is not a grant.

No cookie/storage/history/bookmark/clipboard grant is implicit in `act`, script
availability or the removal of capability-group selection. The initial delegated
subset has no such APIs even if a trusted legacy configuration enables them.
Raw policy/connection/approval channels are never SDK methods. The existing trusted
`connect()`/`Browser.request()` and `kitsune.tools` module remain outside the worker.
Their availability to an operator is not a proof of delegated isolation.

## 4. Identity, targeting and lifetime

- One authenticated MCP connection owns a session context. Direct calls and all
  its scripts inherit that context; they do not call `kitsune.connect()` or obtain
  a new owner by launching a program. The stable owner used by existing tab
  ownership logic is retained; caller/run IDs are distinct audit identities.
- Flat tab ids are opaque handles to `(session, bridge/extension generation,
  backend tab id)`. They are returned by list/open and stay usable across direct
  calls and scripts in that session. They do not confer ownership. A random or
  foreign handle fails `invalid_tab_handle`; a closed/replaced tab fails
  `stale_tab_handle`. Raw backend ids and tab indexes are not interchangeable.
- Server/bridge/extension restart invalidates the affected handles and refs. The
  caller must list tabs again. Stable owner attribution may let that new listing
  identify still-owned tabs; it does not revive old approval or ref capabilities.
- A script starts with fresh local variables. Retain tab ids explicitly in caller
  context and pass them to another script. There is no current-tab fallback,
  ambient active-tab read, persistent REPL or serialized Browser object.
- Public refs are allocated/restored through the **same** serializer/registry for
  direct and SDK operations. Their records bind the session, tab, document and
  node. Navigation invalidates refs even when the new page looks identical.
  Detached/replaced nodes fail `stale_ref`; a ref from another tab is not retargeted.
- Selectors use the existing supported resolver, not arbitrary JS. Actions require
  exactly one match. Ambiguity is terminal for that call; no `first=True` override
  or label-to-first-match guess. A caller may read/query and choose a more specific
  target. Invalid selector syntax fails before an action can be sent.
- Actionability waits use the existing operation-specific rendered/connected and
  editable-control checks. A missing selector target may be retried only within
  the pre-dispatch timeout; stale refs or ambiguous targets are not silently
  rebound. Pointer actions never silently escalate to trusted OS input.
- Observations identify their tab/document and capture time. No implicit delta or
  `unchanged_since` response is presented as a complete tree. Initial pilot reads
  always request a full bounded observation. Result filtering in a script cannot
  manufacture a ref or extend its lifetime.

## 5. Results, errors and uncertain writes

Every trusted operation produces this common conceptual envelope (schema pending):

```json
{
  "ok": true,
  "call": "c_example",
  "operation": "click",
  "tab_id": "t_example",
  "outcome": "dispatched",
  "data": {},
  "truncated": false
}
```

`outcome` is `not_dispatched`, `dispatched`, `observed`, or `unknown`.
`not_dispatched` means a trusted pre-dispatch refusal; `dispatched` records sending
or acknowledging the operation but does not prove the user's task was completed;
`observed` requires trusted returned observation evidence; `unknown` means the
request may have taken effect but its outcome cannot be established. A timeout
or lost response after a write was sent is not `not_dispatched`. Never derive an
outcome from a script's `print("success")` or its exit status.

On failure add `error: {code, message, retryable, hint, state}` and set `ok=false`.
State is bounded and redacted. Direct MCP calls expose the same envelope with
`isError=true`; Python raises `BrowserOperationError` and JS rejects with the same
named error carrying `.result`. A caught SDK exception is not erased from the
trusted operation ledger. Successful calls return the same JSON-compatible Result
object (Python dictionary / JS object). Exceptions never turn an unknown write
into an ordinary success or automatically trigger a retry.

No automatic post-action full snapshot is added to one interface but not the
other. An action returns its receipt, useful bounded target/navigation metadata
and any actual operation evidence. Read/wait/screenshot is an explicit subsequent
operation when an observation is needed. If that observation fails, the earlier
action's receipt remains intact. This is a deliberate difference from legacy
post-action snapshot conveniences and must be reflected in the comparison.

| Condition | Required result and next step |
| --- | --- |
| Stale ref/document | No new dispatch; `stale_ref`; read again and choose a new target |
| Multiple matches | No dispatch; `ambiguous_target`; return bounded matching context, not an arbitrary choice |
| Activity/ownership hold | `needs_user`/existing specific refusal, not success; named tab, safe guidance and a scoped continuation only when one is actually resumable |
| Mode/cap/grant denied | No dispatch; stable denial code; code cannot repair this by changing headers or declaring itself approved |
| Timeout before dispatch | `not_dispatched`; preserve the reason; a fresh request still needs ordinary checks |
| Timeout/disconnect after sending a write | `unknown`, not automatically retryable; observe or ask the user; do not replay the whole script or trust a self-reported not_applied claim |
| Partial run | Keep each operation's outcome, the last settled operation and any in-flight unknown; no rollback/transaction guarantee |
| Output truncation | Mark missing data and limits; no claim of zero remaining matches or a complete page |

A resume token is a scoped continuation reference, **not an approval secret**.
The trusted server retains the exact held operation and original authority ceiling.
Resume intersects that ceiling with current caller/run authority, checks tab and
connection generation, expiry and current external approval, and dispatches only
an operation known not to have run. Foreign/expired/revoked/legacy-container tokens
are rejected without dispatch. No flat `observed={...: "not_applied"}` override
lets code authorize repeating an uncertain write.

Direct and SDK resume share this rule. It resumes one held operation, not a
Python instruction pointer or the rest of a program. Once that operation settles,
the caller can explicitly continue remaining work. A newly launched run cannot
revive the terminated worker's credential. Approval acceptance is external to
code; pending continuations expire on their issued expiry, session/generation
change or revocation. #500 must bind the concrete TTL and prompt/receipt behavior
to #496's final implementation, with a maximum five-minute pending lifetime; a
shorter approval expiry always wins.

## 6. Text, image and run output

- Each read is bounded (`max_chars` 1–12,000); query/search limits and per-item
  truncation are explicit. Structured output stays valid JSON. Report whether
  additional entries exist/are unknown and how to narrow the read; never silently
  slice a JSON string or claim a truncated count is the total.
- Proposed per-operation structured reply ceiling: 64 KiB UTF-8, excluding image
  bytes. Character caps are not byte caps or token measurements; either cap can
  truncate content, with stable receipt/error fields preserved.
- A screenshot returns capture metadata and an opaque, caller-bound image handle.
  Direct MCP output includes an actual `ImageContent` block. Code can select its
  image handle with `emit_image(handle)` for the enclosing result. It cannot ask
  the broker to open an arbitrary host path or decode a self-supplied base64 blob
  as a trusted screenshot. Image handles live for the enclosing run/response.
- Proposed per-outer-call image bounds: four images, 8 MiB each, 16 MiB total.
  Oversize/omitted images have explicit metadata; no broken image or opaque
  base64 text is substituted. Optional resizing must be explicit and reported,
  not silently used to make benchmark outputs smaller.
- Proposed script bounds: 32 KiB source, 16 KiB combined selected JSON/stdout/stderr,
  100 requested browser operations, one in-flight operation, and a 120-second hard
  deadline (30 seconds default). `emit(value)` selects JSON-compatible output;
  stdout is diagnostic text, not task-success evidence. Emission helpers grant no
  browser or host capability. No nested runner, parallel SDK calls or persistent
  state is part of the pilot.
- The coordinator, not code, appends a compact authoritative operation ledger and
  run status (`completed`, `failed`, `cancelled`, `timed_out`, `output_limited`).
  `completed` only says the program returned. The ledger separately shows failures,
  partial effects and uncertain writes, even when code catches an exception.
  Budget exhaustion refuses further calls before dispatch and does not erase prior
  successful or unknown effects. Resource/ledger overhead must be measured too.
- Deadline/cancellation/disconnect closes the run's admission gate before cleanup.
  It stops new operations, revokes the worker channel, settles or marks an already
  dispatched write unknown, terminates the worker and deletes only run-owned
  temporary artifacts. Browser-side effects are not undone by killing a process.
  Cleanup and handoff must not silently reuse the user's browser/profile/bridge.

These numeric bounds are design proposals to validate with local fixtures before
preregistration is frozen. Changes after freezing require a protocol amendment,
not retrospective adjustment to improve a benchmark result.

## 7. Agent UX versus operator migration

Use a direct tool for one observation/action, a short sequence with decisions that
need model judgment, or handoff. Use code for repeated authorized operations,
branching on returned data and filtering a large result before returning it.
The SDK guide is supplied with the scripting configuration; finding/publishing a
hidden catalog is never necessary. C receives the same browser-operation guide as
D; only D's code/runtime guidance is additional. Guides, definitions and generated
code are separate context-cost lines, not hidden setup.

Proposed startup selection for the implementation stage:

- `KITSUNE_INTERFACE=legacy|flat`, default **legacy** until positive #504 evidence
  and separate user approval. This is a product-interface selector, not another
  value added to `KITSUNE_CAPS`. The flat interface is opt-in.
- `KITSUNE_SCRIPTING=off|python` initially defaults to **off** and controls runner
  availability, not browser permissions. The candidate comparison C uses off and
  D explicitly opts into python. Missing or
  unvalidated isolation fails the requested scripting startup with an actionable
  diagnosis; it must not silently launch an unrestricted interpreter. JS is not
  enabled until the later positive Python feasibility gate.
- The exact approved-default configuration is a later #502 decision. No environment
  variable, registration or caller default is changed by this design PR.

| Existing setting/API | Proposed migration and fail-closed behavior |
| --- | --- |
| `KITSUNE_CAPS` | Legacy keeps existing groups and named-only storage/history. Flat rejects an explicitly supplied legacy caps value (including all/core) with migration guidance; it must not reinterpret it as a broader grant. Operator reviews the fixed flat operation scope before opting in. |
| `KITSUNE_PUBLISH` | Legacy keeps visibility behavior. Flat rejects the setting; removing it cannot silently make previously unpublished write capabilities available. No compatibility auto-publish. |
| `KITSUNE_MODE` | Preserved with branch-level and dispatch-time checks above. Unknown modes fail startup. |
| `KITSUNE_USER_TABS` / URL policy / redaction | Existing defaults and authority are retained; candidate selection does not opt out of protection or turn a user tab writable. |
| Sensitive-data grants | Not implied by act/all/scripting. The initial flat subset offers no sensitive global-data methods; explicitly authorized legacy/developer use remains separate. |
| Playwright-compatible names/schemas | Legacy keeps them. Flat has explicit handles, new schemas and results; cached legacy calls fail validation rather than guessing an index/current tab. Update clients at startup, not with dynamic publication. |
| `kitsune.connect`, Browser/Tab, raw `kitsune.tools` functions | Trusted developer APIs stay available and source-compatible. Delegated code gets an injected restricted SDK; it cannot import a credential-bearing connection as a shortcut. |
| Legacy containers/checkpoints | Stay in legacy with existing ownership and checkpoint semantics; no generic forwarding from flat or conversion into an arbitrary runner script. |

After an approved default switch, propose an explicitly selected legacy MCP path
for two minor release lines, with advance warnings and a documented removal issue.
That finite compatibility window does not remove the trusted developer API. The
release owner must choose actual versions before release; this PR does not assign
or publish them. Rollback is explicit legacy selection during that window or
pinning a recorded pre-switch package/commit and its matching extension. Neither
rollback nor a migration warning relaxes policy. An old environment is never
silently transformed into writable user tabs or sensitive-data access.

## 8. Review findings and implementation gates

Source review has already corrected/clarified:

1. The initial inventory missed the documented low-level `kitsune.tools` Python
   surface: add all 104 functions, not only the nine module-only names. The current
   disposition map covers 573 entries exactly once, across 48 rationale groups.
2. `browser_close` is supported tab closure and `browser_drop` is supported upload/
   data drop; neither belongs in the not_supported bucket.
3. `Tab.handle_dialogs` controls future dialogs, not one visible modal. The pilot
   must not claim one-shot dialog parity or confuse it with user approval.
4. The trusted Browser.resume describes container recovery, while the MCP server
   also holds direct calls. A flat resume must preserve scoped direct recovery
   without exposing arbitrary legacy containers or replaying scripts.
5. Restricted modes contain important route-level differences: screenshots,
   resume, most wait variants and navigation history are not blanket read actions.
6. Existing `connect(caps=None)` and `Browser.request` are raw authority paths.
   Hiding Python imports/signatures is not enforcement. #500 must prove the chosen
   worker's host/credential boundary and describe limits against independent
   same-user local processes; no runtime is selected by tool-count preference.

Remaining review-ready work for #499:

- Concrete machine-validated schemas, SDK signatures and paired examples; verify
  every proposed name/default against this contract and the disposition map.
- Actual default/gateway/flat/SDK-guide footprint measurements, clearly labeled as
  characters/bytes until an approved model tokenizer is specified.
- An acceptance-to-evidence table and a final contradiction/adversarial review.

Current artifact checks and regression evidence, including the failed and corrected
unit-isolation attempts, are recorded in [499-validation.md](499-validation.md).

Implementation gates (not evidence of completed implementation): #504 protocol
must precede product implementation/model runs; #500 must establish isolation and
coordinate final #496 behavior; #501 must prove actual entry-point lifecycle,
policy and partial-effect tests; #502 must validate fixed startup lists and mode
branches, including clients without list refresh. Browser tests use only private
fixtures/profiles/bridges. Model runs, default switching, merge and release retain
their separate approvals. A no-go/inconclusive result blocks the affected rollout
or JS gate rather than being relabeled completion.
