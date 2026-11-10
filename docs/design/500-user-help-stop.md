# #500 checkpoint: a user-help stop, not approval

Date: 2026-11-11. Follows `1195ee8`, draft PR #509.

`browser_ask_user` now uses the existing `/api/ask-user` route. This is the
fifteenth of eighteen broker adapters, but only the **needs-user stop** is wired:
there is no flat MCP elicitation/answer handling or resumable continuation yet.
The candidate remains unregistered and #500 remains incomplete.

## Behavior

- The same strict admission, mode/scope, optional caller-local tab handle and
  connection binding apply. Read, draft and act may request help.
- The existing bridge validates the request, optionally reads `getPageInfo`, and
  creates its existing `agent_request` audit/needs-user result. It does not write
  to the page or itself display an approval prompt.
- `/api/ask-user` joins the explicit receipt-route allowlist. A request without a
  tab has an empty native command list; with a tab the receipt records its page
  info read. The bridge's existing `askUser` audit entry has `flat_call`.
- A completed bridge needs-user stop maps to `ok:false`, `needs_user`,
  `outcome:not_dispatched`, `retryable:false`, empty `data`, and state containing
  only `reason:agent_request`, the admitted reason as `detail`, and the admitted
  message. An optional tab is returned as a public handle, never a backend ID.
- The help request is the caller's text, **not a user answer**. It grants no
  permission, confirms no held write and supplies no `resume_token`. The hint
  says to ask the user and wait, and explicitly says this is not approval.
- Page title/URL/evidence are deliberately not forwarded; they are not needed to
  ask the caller's question and this path has not established their redaction
  parity. Page-supplied approval/token fields cannot reach the result.
- Timeout/disconnect or incomplete receipt is not fabricated into a user answer.
  Existing conservative transport outcomes remain unchanged.

## Contract gap, not a silent change

#499 admits messages up to 2,000 characters; the native bridge trims at 1,000.
This adapter returns `unsupported_variant`, before HTTP, for messages above
1,000 characters rather than silently cutting them. Blank/whitespace-only
messages return `invalid_arguments`. The accepted original message is preserved
in the returned help request (the bridge independently trims surrounding spaces
for its internal state). The design fixture is unchanged; broader message and
external-answer/resume support remain candidate gates.

`browser_request_tab` and `browser_resume` remain unavailable, as does
`browser_query`. Existing #496 approval secrets, first-answer-wins behavior and
120-second token lifetime have not been replaced by a parallel approval system.

## Validation

- Red: two new wire tests produced six subtest/test failures against the previous
  `operation_unavailable` behavior. Retained HOME:
  `/tmp/kitsune-500-ask-red.4Ne1Dg`.
- Broker/legacy help/actual WASI authority focused set: **59 passed**, 84.878 s,
  HOME `/tmp/kitsune-500-ask-suite.FMd23H`.
- Actual WASI script catches the help stop, then attempts a read-mode click: the
  click is still `mode_restricted`; no native request is sent for either operation
  (the help request has no tab). The trusted ledger and bridge audit retain the
  help stop independently of the script's completed status.
- Real-browser broker suite, runtime enabled: **8 passed / zero skips**, 21.495 s,
  HOME `/tmp/kitsune-500-help-e2e.XYLn24`. The new help scenario verifies unchanged
  page fields/event log/scroll, tab URLs and active-tab state using separate
  harness requests. Temporary add-on, not signed-extension conformance.
- Full isolated Python discovery, runtime enabled: **1,395 tests / 245 skips /
  zero failures or errors**, 421.336 s, HOME
  `/tmp/kitsune-500-help-unit.3d5x71`.
- Inventory unchanged: 573 entries, 48 groups, zero mapping errors.
- `git diff --check` passed. Extension source unchanged; Node and full browser
  discovery were not rerun for this checkpoint.

Every run used private HOME/KITSUNE_HOME, test credentials and non-default ports.
Focused unit/browser parent hooks recorded zero default-port connection attempts.
The full unit wrapper's hook is only in the launcher parent, not its discovery
subprocess; its zero count is not child-process network evidence.

No model execution, MCP publication, default transition, merge or release.
