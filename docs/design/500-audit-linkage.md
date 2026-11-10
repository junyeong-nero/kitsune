# #500 checkpoint: flat call correlation in the bridge audit

Date: 2026-11-11. Follows delegated-worker checkpoint `10d8426`, draft PR #509.

Existing bridge audit entries now include `flat_call` when the request has a
validated dispatch-receipt context. The value is the same broker-generated call
ID used by the receipt and, for completed broker calls in scripts, the trusted
runner ledger. No new HTTP header, agent argument or permission is introduced.

- Source: `DispatchReceipt.call` in the request's `ContextVar`, after the existing
  authenticated HTTP receipt-header validation. Not a body field, page reply,
  script stdout, or emitted object.
- Existing held-call `call`, container `parent.call`, client/owner and approval
  attribution are preserved. `flat_call` is a separate correlation field, not an
  approval credential or a replacement for those identifiers.
- Typed values still pass through existing audit redaction. Approval tokens are
  not added to the audit. Receipt context is reset after each HTTP request.
- The header is a trusted-client correlation assertion, not cryptographic proof:
  a bearer-token-holding developer client can choose its own valid receipt ID.
  The delegated guest cannot set it; the broker generates it outside arguments.

## Boundaries deliberately unchanged

This annotates existing audit entries only. Some reads, early admission refusals
and internal inspection commands do not produce such an entry. Absence of an
entry is not proof that nothing was requested or dispatched. Existing audit `ok`
is not application success; use the broker outcome and native receipt, and an
independent observation where needed.

There is no run ID on the bridge yet. A script's returned ledger can join settled
calls by call ID, but a call still in flight at script termination does not yet
have its broker-generated call ID in that returned ledger. Durable late receipt
collection, full requested/admitted/native attribution and #504 run/outer-call
correlation remain open. This checkpoint does not complete #500.

## Verification

Two new regressions failed before the product change: missing `flat_call` in a
unit audit entry, and no joinable audit entry for two concurrent broker calls.
Retained private HOME: `/tmp/kitsune-500-audit-red.QLSvg9`.

After the change:

| Check | Result |
| --- | --- |
| Audit, broker wire, legacy direct resume and elicitation tests | 88 passed, 57.440 s |
| Actual WASI guest + real broker/HTTP/WebSocket, scripted extension: direct/script shared-session scenario with ledger-to-audit assertions | 1 passed, 7.006 s |
| Full isolated Python discovery with private script runtime enabled | 1,391 tests / 244 skips / zero failures or errors, 421.126 s |

The tests cover concurrent client/owner separation, page/body attribution spoofing,
redaction, context reset before a subsequent legacy request, and held-action
entries produced across the HTTP-thread/event-loop boundary. Held audit entries
retain their legacy hold ID while correlating to the current flat call.

Private HOME, test token, reserved non-default HTTP port and unset WS override
were used for every run. Focused runs' parent audit hooks recorded zero attempts
to the default bridge ports. The full-discovery wrapper runs discovery in a
subprocess: its parent hook also recorded zero, **not a child-process network
audit or process-tree firewall**. Subprocess tests inherit the private environment.

Retained homes: `/tmp/kitsune-500-audit-focus.vK3NaT`,
`/tmp/kitsune-500-audit-guest.d3upoj`, `/tmp/kitsune-500-audit-unit.mxd29K`.
Full run log: `/var/folders/l8/ydsw65lj3fxgkpyx8p8ht3b80000gn/T/pi-bash-e87f62ff65c8112e.log`.
Expected late-elicitation unknown-request logs remain; associated tests passed.

No extension source changed. Full real-browser/Node suites were not rerun for this
five-line audit-only product change; the preceding checkpoint's browser results
are historical evidence, not a claim of a new browser run. No MCP registration,
model execution, default change, merge or release occurred.
