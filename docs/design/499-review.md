# #499 acceptance and design review

2026-11-10. **Design-stage self-review and automated artifact checks**, not an
independent security review, implemented runner, benchmark verdict or rollout
approval. PR #506 is stacked on the prerequisite test-only PR #505. #496 remains
an external handoff dependency, last checked OPEN at `97dc9d67f40cf55c4dd91b8985fac083fced9781`.

## Acceptance → evidence

| #499 acceptance | Artifact/check | Scope and limitation |
| --- | --- | --- |
| Every public capability has a destination and rationale | `499-capability-map.json`; `scripts/toolset_inventory.py --check-map`; 10 inventory/mapping tests | 573 overlapping entries, 48 groups, zero missing/duplicate/stale names; every group records replacement and coverage loss. This is name coverage, not feature equivalence. |
| Concrete schemas and signatures cover six workflows | `499-flat-schemas.json`, `499-sdk.pyi`, `499-sdk.d.ts`, `499-examples.json` | 19 proposed tool slots, 23 SDK methods; all required page/draft/multi-tab/repetition/image/handoff examples are paired. No catalog-discovery prerequisite. |
| Direct/script examples share behavior and authority | `test_flat_contract_design.py` compares normalized request traces and mode branches | Same requested operations/defaults on synthetic replies. Backend/browser/permission equivalence still requires actual-entry-point conformance in #500–#503. |
| Definitions and guide footprint measured | `499-interface-costs.md/.json`; `scripts/flat_contract_size.py`; `test_flat_contract_size.py` | Full metadata and like-field projections, guides, example source/wrapping, optional references and input hashes are separate. No tokenizer/model run or savings claim. |
| Unknown writes, refs, ambiguity, activity and authority have outcomes/recovery | `499-flat-contract.md` sections 3–6; negative schema/example tests | Explicit not_dispatched/dispatched/observed/unknown, no first-match override, scoped resume, truncation and partial effects. Runtime behavior remains a required implementation gate. |
| Mode/session/backward-compatibility decisions resolved or block implementation | Contract sections 3, 4, 7, 8; explicit losses in every map group | Fixed startup mode branches; opaque session/generation handles; reject legacy caps/publish settings in opt-in flat; no ambient sensitive grants; trusted developer API separate. #496/#500 and rollout approvals remain gates. |

Focused checks: **24 tests passed** (10 inventory, 11 schema/signature/example,
3 footprint), including parsing intended JS declarations with Node and validating
candidate definitions as MCP Tool objects. These are not real-browser passes.
Final isolated regression: **1,273 tests, 235 skips, zero failures/errors**; **70
Node harnesses passed**. Regression/isolation evidence, including the earlier
failed invocation, is tracked in [499-validation.md](499-validation.md).

## Contradiction and failure-oriented review

1. **Inventory completeness:** registry-only accounting missed 104 documented
   low-level Python functions, including nine module-only names. They are now an
   explicit surface. Constructors/namespaces/exceptions are documented rather than
   pretending they are browser operations or omitting their trust implications.
2. **Capability versus count:** 73 of 109 registered tool names remain operator-only
   in the pilot. The six examples do not prove full-product coverage. The operator
   route retains exact APIs; it is not presented as equivalent agent usability.
3. **Unsupported versus merely omitted:** close and drop work today; only media
   emulation and unsafe Playwright run_code are unsupported stubs. Future-dialog
   policy is not a one-shot modal answer. These distinctions are preserved.
4. **Generic-dispatcher temptation:** no arbitrary operation/route/schema discovery
   tool exists. Tabs and navigation have small typed families; the script API has
   named typed methods. Script does not get all legacy helpers through another name.
5. **Restricted-mode holes:** tests reject close/history-navigation/general waits,
   screenshots, resume and tab requests in read/draft, despite shared tool names.
   Draft has only typed input/select/check in addition to read operations. Mode is
   still checked at dispatch; schema pruning alone is not enforcement.
6. **Target/default ambiguity:** mixed ref+selector, numeric/index tab ids, first,
   unknown arguments, invalid timeout units, double-click options, screenshot scope
   and top/bottom scroll amounts fail validation. Optional SDK None means omission;
   scalar defaults and action-family bindings match the schemas.
7. **Recovery versus replay:** a held leaf may resume after real external approval.
   It cannot resume an arbitrary old container, restore a Python instruction
   pointer or accept a worker's not_applied claim. The repeated-work example stops
   after an uncertain write and retains the earlier completed receipt.
8. **Hidden code/guide cost:** the complete D guide is counted. D's initial character
   sum exceeds legacy core in every mode, and one-off scripts have larger requests.
   A small gateway starts smaller still. Only a controlled #504 workload can assess
   amortization, success, safety and total efficiency; this review does not issue
   a workload verdict.
9. **Metadata fairness:** full legacy metadata and a name/description/inputSchema
   projection are both reported. Explicit conservative readOnlyHint values were
   added to the candidate rather than treating missing optional metadata as a win.
   Other annotation/output-schema differences and client effects remain disclosed.
10. **Mock-versus-boundary confusion:** the example evaluator is intentionally
    unrestricted test code for authored documentation only. It must never be reused
    as a runner. No hiding imports, SDK methods or credentials in a subprocess can
    substitute for the real host/credential boundary and negative tests in #500.
11. **Test environment:** unmocked activity-report paths in existing tests can reach
    the process-default bridge. Full discovery now uses the isolated invocation in
    the validation log. A custom WS override caused four expected-diagnosis failures;
    the corrected environment passes without changing those tests or product code.

## Implementation gates, not work to silently bypass

- **Before product implementation:** preregister #504 in `kitsune-benchmark`,
  including declared task coverage, C/D equality, versions/guides and decision rules.
  Freeze the implemented metadata again before model runs. Seek model/cost approval.
- **#500:** choose and demonstrate actual credential/host isolation; preserve all
  runtime checks and revocation. Coordinate #496's final prompt/grant/continuation
  contract, including expiry, without merging or forking it silently. Be explicit
  about independent same-user process limits. If enforcement is unsafe, stop.
- **#501/#502:** implement the common operation layer, actual Python entry point,
  lifecycle/output/partial-effect attribution and private real-browser tests.
  Verify static startup lists on clients without list-refresh support, metadata
  effects, image blocks, target/ref lifetime and fail-closed migration.
- **Rollout/JS:** positive Python feasibility/comparison evidence and the specified
  separate approvals remain mandatory. A no-go/inconclusive result is not completion.
  Trusted legacy/developer features and rollback must not widen authority.

No product source/default, existing SDK behavior, model benchmark, merge or release
was changed by these design artifacts. The next milestone is #504 preregistration,
not immediate unrestricted Python execution or the default switch.
