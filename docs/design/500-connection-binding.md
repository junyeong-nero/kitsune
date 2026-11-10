# #500 implementation checkpoint: connection-bound dispatch

Date: 2026-11-10. Worktree `/tmp/kitsune-500-implementation`, branch
`feat/500-shared-authority`, based on `8bc2b435f01a7d26a68057c5806fcc1875fdb678`.
This is a prerequisite of the shared broker, **not a completed broker, runner,
flat MCP surface, or delegated-authority verdict**. Defaults remain unchanged.
The approved local integration and prior isolation repair are documented in
[500-integration-validation.md](500-integration-validation.md).

## Why the check belongs at dispatch

The #499 contract binds opaque tab handles to a caller session, a bridge/extension
connection and a backend tab id. Checking `/api/status` in a coordinator, then
sending an ordinary tab-id request, leaves a reconnect race. Additionally, the
extension generation counter restarts when the bridge process restarts: a new
profile can have the same generation number and tab id as the old profile.

The existing send lock already prevents switching a pending command to a different
WebSocket object. It does not bind an HTTP request to the connection on which the
coordinator originally obtained the handle. This checkpoint supplies that missing
transport precondition; the session/handle registry that must always use it is
still pending.

## Implementation

- `src/kitsune/connection_binding.py` defines immutable `ConnectionBinding` and
  `X-Kitsune-Connection: <bridge-instance>:<extension-generation>`.
- Each bridge process generates a new non-secret 128-bit instance id.
  `/api/status` exposes `bridge_instance` alongside the existing
  `extension_generation` and `extension_connected` fields.
- A binding requires a connected extension, a 32-character lowercase hexadecimal
  instance id, and a positive integer generation below `2**63`. Missing old-server
  metadata, booleans as integers, zero/disconnected generations, duplicate headers
  (including case variants), and malformed values do not silently fall back.
- `BridgeHandler._dispatch` parses a supplied precondition and carries it in a
  request-local `ContextVar`. A mismatch returns HTTP 409 / `stale_connection`
  before entering the route handler. Malformed/duplicate values return HTTP 400 /
  `invalid_connection_binding`.
- `_send_to_extension_raw` checks again before creating a command, and under the
  WebSocket send lock immediately before sending. HTTP or scheduler admission is
  not a connection lease. Body fields cannot replace the transport precondition.
- A refused native send reports `dispatched: false`, `retryable: false`. This is
  about **that native command**, not an aggregate receipt for an earlier successful
  write or a multi-command operation. The future broker must preserve individual
  leaf receipts; it must not erase earlier effects using a later refusal.
- Once a command has been sent, a subsequent reconnect/timeout is **not** changed
  into a pre-dispatch refusal. The timeout's uncertain-write/no-blind-replay
  semantics remain intact. Pending-command cleanup still runs.
- Request context resets on both normal and exceptional exits. A stale request
  does not contaminate another client or a subsequent unbound request.

## Authority and compatibility limits

This is a precondition, **not a credential, ownership grant, revocation mechanism,
or sandbox**. A matching value cannot substitute for the bearer token or override
`read`/`draft`/`act`. Existing capability, URL, sensitive-data, ownership, activity,
and external-approval enforcement remains in place.

Legacy trusted developer clients can still omit the header. No existing caller is
automatically switched to this behavior, and no newly registered tool exposes the
header. The future trusted flat coordinator must retain the binding outside agent
arguments and attach it on every relevant dispatch. A worker must never receive
bridge credentials, raw HTTP access or the ability to select/omit its transport
context. This checkpoint alone does not establish those properties.

It also does not bind document/node refs, detect all same-session tab lifecycle
changes, revoke in-flight browser effects, or implement trusted operation
attribution. Those remain requirements of #500/#501. No guest/host resource claim
has changed; the WASI feasibility limits remain as documented.

## Evidence

All new tests use synthetic credentials and private bridge ports/HOME. The newest
worktree has its own locked/offline project venv, Python 3.12.6. An explicit check
confirmed `kitsune.__file__` resolves under this worktree's `src/`; no dependency
or lockfile change was needed.

| Check | Result |
| --- | --- |
| Red reproduction before implementation | 9 tests; 12 subtest/test failures and 3 errors, 0.007 s. Stale/malformed headers reached the handler; reconnects reached the fake socket; instance metadata was absent. No real network used. |
| Initial focused unit rerun | 9 tests passed, 0.003 s |
| Expanded unit + actual private HTTP/WebSocket tests | 17 passed, zero skips/failures/errors, 3.780 s; zero Python-parent default-port connection attempts |
| Initial full isolated Python discovery | 1,295 tests, 236 opt-in skips, zero failures/errors, 191.773 s |
| Final full isolated Python discovery | 1,297 tests, 237 opt-in skips, zero failures/errors, 196.084 s; includes all 18 new unit/wire tests |
| Node | All 70 existing harnesses passed |
| Focused real browser | 8 passed, zero skips/failures/errors, 24.709 s; zero Python-parent default-port connection attempts |
| Diff | `git diff --check` passed |

The initial full unit run preceded addition of the final real-browser test and a
unit check of context propagation across `BridgeHandler.run_async`'s actual
thread/event-loop boundary. The final full unit run includes both additions.
The new browser test was also executed, not merely skipped, with the seven
existing connection-diagnosis browser tests. Full real-browser discovery is
**not claimed** for this checkpoint; it remains required for the completed
authority candidate. Python 3.10 grammar parsing of the changed Python files
passed; this is not a claim that all supported interpreter versions were run.

The isolated HTTP/WebSocket tests prove:

1. A replacement extension receives zero `getTabs` commands from an old binding;
   a fresh binding reaches the replacement, and a later unbound developer call
   still works.
2. Restarting the actual private bridge on the same ports/HOME/token invalidates
   the old binding even when the new extension generation and tab id equal the old
   numbers.
3. Duplicate case-variant headers fail through the real HTTP parser.
4. A valid connection binding with a wrong bearer token gets HTTP 401 and sends
   no extension command.

These use a scripted extension, not a real browser. Separately,
`test_real_browser_connection_binding.py` opens a local counter fixture in a
private Zen profile, disconnects/reconnects the actual extension via the existing
test-only control channel, and verifies that an old-bound click gets HTTP 409 and
leaves the count at zero. A fresh-bound click then increments it exactly once.
The neighboring connection-diagnosis suite also passes.

This browser check uses the existing **temporary-add-on harness**, not the later
signed-extension candidate path. Its two expected/non-200 bound HTTP requests are
made directly: they appear in the bridge log, not the harness request timeline.
The test does not use the real user's browser, bridge, profile or credentials.

### Reproduction and retained locations

Use the full-unit isolation wrapper in [499-validation.md](499-validation.md),
with this worktree's `.venv/bin/python`: private HOME/KITSUNE_HOME and test token,
reserved non-listening HTTP port, and unset `KITSUNE_WS_PORT`/`PYTHONPATH`.

For the 17 focused tests, use the same environment and discover
`test_connection_binding*.py`. For the eight actual-browser tests, use the
strengthened outer wrapper from [500-integration-validation.md](500-integration-validation.md),
with:

```text
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen
KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-500-binding-real-browser
pattern=test_real_browser_connection*.py
```

The parent audit hook blocks/counts connections to 47651/47652; it is not a
process-tree firewall. Children continue to use the existing private harness.
No activity-report path was patched away to make the tests pass. The full unit
suite's intentional late-elicitation `unknown request ID` logs and unpublished-tool
warnings remain visible; their associated assertions passed.

Retained outer test homes:

- Red: `/tmp/kitsune-500-binding-red.QQELV4`
- Initial focused: `/tmp/kitsune-500-binding-focus.tJ9tlK`
- Expanded unit/wire: `/tmp/kitsune-500-binding-wire.3969S0`
- Initial full unit: `/tmp/kitsune-500-binding-unit.vRrQ6z`
- Final full unit: `/tmp/kitsune-500-binding-final-unit.m5lhr8`
- Real browser: `/tmp/kitsune-500-binding-e2e.3TLRmz`

The real-browser artifact directory is a failure-artifact target; successful
assertions do not imply a saved failure bundle exists there.

## Next implementation step

Implement the caller-owned flat session/handle registry and strict operation
admission on top of this precondition, then the shared native adapters, trusted
receipts/redaction, scoped external approval and actual private WASI worker IPC.
No #500 milestone completion, model run, default transition, GitHub PR/main merge,
or release is authorized or claimed by this checkpoint.
