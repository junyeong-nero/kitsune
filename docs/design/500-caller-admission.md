# #500 implementation checkpoint: caller-local admission

Date: 2026-11-10. Branch `feat/500-shared-authority`, draft PR #509; follows
connection-binding commit `c50178f2b64fb436a859c2cf1c3de3764685dcfd`.

**This is not an enabled flat surface, complete broker, or delegated-authority
verdict.** These new modules are not yet wired into MCP, native adapters or a WASI
worker. The previous checkpoint's actual-browser evidence concerns connection
preconditions, not these caller-local checks.

## Implemented

### `src/kitsune/flat_operations.py`

- Explicit admission for the 18 proposed leaf operations and their variants.
  `browser_script` is not a leaf; legacy gateways, evaluation, raw routes and
  unknown operation names do not enter this subset.
- Uses the existing Pydantic dependency, without a new install/lockfile change or
  runtime dependency on `docs/design`, authored example evaluation, or test code.
- Required fields, lengths/ranges, exact-one ref/selector targets, nested
  conditions, unique query/select/modifier arrays and cross-field constraints are
  checked. Examples: left/unmodified double-click, element-only screenshot target,
  no amount on top/bottom scroll, and an explicit tab for page-related questions.
- The trusted caller mode restricts both operation names and variants. Read/draft
  do not gain close/history/reload/general waits merely because the parent tool
  name exists. Input's `mode` is only `replace|type`, never a permission override.
- Extra fields (including headers, route, policy, owner, client, approval secrets,
  writable-user-tab flags, filename and first-match flags) fail rather than being
  forwarded. Explicit null is not optional-field omission. Booleans and strings
  are not coerced to numbers or vice versa.
- JSON Schema's integer semantics include `1.0`: integral JSON numbers are
  canonicalized to native integers before bounds checks. Fractions, NaN/infinity,
  booleans and numeric strings remain invalid. This preserves the existing design
  schema rather than introducing an undocumented stricter numeric wire contract.
- Normalized arguments are fresh copies. Mutation of the original request or a
  returned copy does not alter a pending operation. Errors delivered by this
  layer do not include Pydantic's input-bearing validation text.

This validates argument structure, **not the liveness/authority of a ref or the
safety of a URL**. Native policy and caller-bound ref issuance still have to be
connected. Pydantic-generated schemas are not yet served MCP definitions; custom
cross-field rules must be represented in the eventual public schemas too.

### `src/kitsune/flat_session.py`

- Frozen `CallerAuthority` retains client id, stable owner id, mode, user-tab
  permission and an explicit immutable operation subset. Scope intersects mode;
  neither a tab handle nor an operation argument widens it.
- `FlatSession` issues opaque `t_...` handles from 192 random bits. Records bind
  the caller-local registry, `ConnectionBinding` and a positive, JavaScript-safe
  native tab id (`1..2**53-1`, matching the existing bridge).
- Same-owner sessions still have different handle namespaces. Random/foreign
  handles fail `invalid_tab_handle`; retired/old-connection handles fail
  `stale_tab_handle`. No backend integer, index or implicit current-tab fallback.
- A complete trusted inventory preserves existing live handles and retires absent
  tabs. Reappearance of the same native id receives a new handle. Verified bind
  changes retire every old live handle, without claiming an inventory was read.
- Observed successful opens/closes update only the relevant tab. Do not register
  an uncertain open or retire a tab merely because an uncertain close was sent.
- A trusted revoke permanently ends local admission. An admitted operation can
  be rechecked after queueing: cross-session admissions, retired tabs and changed
  connections fail. Revocation cannot be toggled off by rebinding.
- The registry has a fixed **4,096 cumulative issued-handle** budget, including
  tombstones. Exhaustion fails closed; it never evicts/reuses an old handle or
  silently starts a new session. This is an initial candidate availability bound,
  not a measured whole-host memory limit. Publish it in candidate guidance before
  enabling the surface. Unit tests lower the constant only inside a mock to
  exercise exhaustion without thousands of allocations.

`bind`, inventory/open/close updates, `revoke` and admitted Python objects are
**trusted-coordinator interfaces**, not worker RPC methods. Python attribute
privacy/frozen dataclasses are not a security boundary against code in that host
process. The worker must send only ordinary operation requests, never supply or
instantiate these authority/admission objects.

## Required integration still absent

- The actual broker must invoke these checks and fixed native adapters, attach
  the trusted connection/identity/permission metadata, and retain authoritative
  per-leaf receipts. These classes alone dispatch nothing.
- `recheck()` followed later by an HTTP send is not atomic remote cancellation.
  Queued and in-flight revocation must be enforced at actual bridge dispatch;
  already-dispatched effects cannot be undone or relabelled as not dispatched.
- Live ownership/activity, URL policy, sensitive-data grants, redaction,
  document/node refs, output/images, deadlines and external approval (#496) remain
  native-adapter/bridge obligations. No current grant is cached by this registry.
- A failed/truncated inventory must not be installed as a complete list. Concurrent
  dispatch and handle registration need coordinator lifecycle/serialization; this
  local registry does not reserve a browser-side open or prove the eventual open
  can be registered after a concurrent update.
- Actual worker IPC, source/framing/resource bounds, caller/run attribution,
  credential isolation, cancellation/cleanup and signed-extension conformance
  remain pending. No working SDK, safe runner or C/D parity claim is made here.

## Validation and retained failures

The independent #499 design JSON remains unchanged. New tests compare samples
covering every leaf operation and all three modes, defaults and restricted
variants with `scripts.flat_contract.normalize_call`. They also reject forged
transport/authority fields across the operation cases. This is semantic sample
coverage, not an exhaustive proof that two arbitrary validators are equivalent.

During implementation, tests exposed and retained:

1. Initial 18-test run: **1 failure / 2 errors** (0.034 s): an unbound open was
   accepted and explicit bind lifecycle was missing. Fixed without bypassing
   revocation checks. The subsequent 18-test run passed (0.039 s).
2. Native-id range test: **2 failing subtests** for zero and `2**53`. The registry
   now matches the existing bridge's positive safe-integer range; no active-tab
   alias is accepted.
3. Design numeric conformance: **6 erroring subtests** where Pydantic strict int
   rejected schema-valid integral JSON numbers. Added exact integral-number
   canonicalization; corrected the new local negative fixture from `1.0` to
   `1.25` and added NaN/infinity negatives. The frozen #499 schema was not relaxed.

Final results:

| Check | Result |
| --- | --- |
| New admission/session tests | **21 passed**, included in final discovery |
| Focused `test_flat_*.py` (new + retained design/footprint tests) | **35 passed**, zero skips/failures/errors, 1.981 s |
| Final isolated Python discovery | **1,318 tests / 237 skips / zero failures/errors**, 190.388 s |
| Node | **70 harnesses passed**; extension source unchanged |
| Inventory map | **573 overlapping entries / 48 groups / zero errors**; publication unchanged |
| Python 3.10 grammar / diff checks | Passed; execution used Python 3.12.6 and Pydantic 2.14.1 |

The earlier full run before numeric conformance correction was 1,317 tests / 237
skips / zero failures/errors (190.749 s); it is not substituted for the final run.
The full runs used the private-HOME/test-token/reserved non-listening HTTP wrapper
from [499-validation.md](499-validation.md), with `KITSUNE_WS_PORT`, `PYTHONPATH`
and the real-browser opt-in unset. Expected late-elicitation log messages and
unpublished-tool warnings remained visible. Skips are not browser passes.

No new real-browser run was performed for these unwired admission modules. The
prior eight focused browser passes are separately scoped in
[500-connection-binding.md](500-connection-binding.md). Full candidate browser and
actual delegated-entry-point verification remain required.

Retained outer homes: initial red `/tmp/kitsune-500-admission-red.fDTZ9B`, initial
focused `/tmp/kitsune-500-admission-focus.ictYhR`, first full
`/tmp/kitsune-500-admission-unit.fdkv2x`, intermediate focused
`/tmp/kitsune-500-admission-final-focus.w9J7VA`, final focused/full
`/tmp/kitsune-500-admission-final-unit.oJeq1K`.

The authority milestone remains **in progress**. Defaults, model execution,
benchmark methods, other worktrees, main/GitHub merges and release are unchanged.
