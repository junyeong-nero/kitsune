# Tool-set roadmap: pre-change baseline

Related: #498–#504, #497. Recorded 2026-11-10, before product-code changes.

## Revisions and environment

| Item | Baseline |
| --- | --- |
| Kitsune | `fd9b90e1cfae702b8470fcfca5d1bd178dba2c94` |
| Kitsune branch | `design/499-flat-toolset-contract` (created from `origin/main`) |
| kitsune-benchmark | `53d6a9b4a5dcb8f7d2f617a9e347d6af327d3ec0` |
| Benchmark branch | `design/504-flat-toolset-protocol` (created from `origin/main`) |
| Host | Darwin 25.5.0, arm64 |
| Python | 3.12.6 |
| Node | v26.12.0 |
| uv | 0.7.20 |
| Real browser | Zen 1.24.1b, `/Applications/Zen.app/Contents/MacOS/zen` |

Both working trees were clean before branch creation and tests. #494 / PR #496
was open at `97dc9d67f40cf55c4dd91b8985fac083fced9781`; it was not merged or
included in this baseline. No model runs, user-account tests, default changes,
merge, or release were performed.

## No-model regression results

| Command | Result |
| --- | --- |
| Kitsune: `uv run --locked python -m unittest discover -s tests -q` | 1,249 tests; 235 skipped; no failures/errors; 157.780 s |
| Kitsune: each `tests/*.cjs` with `node` | 70 harnesses; all passed |
| Benchmark: `uv run --locked python -m unittest discover -s tests -q` | 410 tests; 18 skipped; no failures/errors; 278.811 s |
| Kitsune: `uv run --locked python scripts/tool_schema_size.py` | 27 core tools; 21,747 compact JSON characters; no snapshot delta or budget overrun |

Core instructions measured 537 characters in `act`, 286 in `read`, and 286 in
`draft`. These are **character counts**, not tokenizer counts. This script does
not yet measure a candidate surface, gateway configuration, SDK guide, or
model-generated code. Skipped opt-in suites are not browser validation.

## Real-browser regression: not green

Executed against the unchanged Kitsune revision:

```sh
env -u KITSUNE_E2E_TRUSTED_INPUT -u KITSUNE_E2E_REVEAL_MENU_BAR \
  KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-498-baseline-e2e \
  uv run --locked python -m unittest discover -s tests -p 'test_real_browser*.py' -q
```

Result: **253 tests, 2 failures, 19 errors, 10 skipped**, 968.998 s. Actual OS
keyboard/pointer/fullscreen opt-ins were not enabled. The existing browser
harness uses temporary profiles, copied extensions, and separate bridge ports.

Failure artifacts are under:

```text
/tmp/kitsune-498-baseline-e2e/kitsune-e2e-20261110T003527Z-39490/
```

Artifact coverage is that of the existing harness, not a claim that every failing
legacy suite preserved a complete trace. The complete command output is retained
in this coding session's tool log.

### Stale `browser_tabs new/select` expectations: 19 errors

All 19 errors are `KeyError: 'tabs'` before the affected tests reach some of their
intended behavior assertions:

| Suite | Errors |
| --- | ---: |
| `test_real_browser_editor.py` | 1 |
| `test_real_browser_editor_cancellation.py` | 3 |
| `test_real_browser_elicitation.py` | 2 |
| `test_real_browser_form_ownership.py` | 3 |
| `test_real_browser_key_defaults.py` | 4 |
| `test_real_browser_playwright_tools.py` | 5 |
| `test_real_browser_protected_tabs.py` | 1 |

The current contract is explicit in `tests/test_core_tab_results.py` and
`src/kitsune/playwright_tools.py:_with_current_tab`: after #488, successful `new`
and `select` return `tab`, `index`, and `tab_count`, not the whole `tabs` list.
`list` and `close` still return `tabs`. The two elicitation failures are already
tracked by #497; the same obsolete assumption occurs in the other suites.

An isolated rerun of `test_real_browser_editor.py` also failed at its line 77
with `KeyError: 'tabs'`: one test, one error, 3.451 s. Its artifact root is
`/tmp/kitsune-498-baseline-e2e-repro/kitsune-e2e-20261110T005242Z-44901/`.

Any repair must preserve URL, index, ownership, current-tab, page-state, approval,
resume, and audit assertions. Passing setup alone is not proof that the remaining
assertions pass. Do not restore a larger product response just to satisfy the
obsolete tests.

### Two additional assertions

- `test_real_browser.RealBrowserActionRaceTests.test_click_returns_a_delta_of_what_changed_below_the_fold_and_keeps_earlier_refs`:
  the fixture snapshot has 9,199 characters, but the test requires more than
  12,000. The intended beyond-cap/delta/ref regression must remain covered;
  deleting the length assertion without preserving the scenario is not a fix.
  The exact cause of the size change has not yet been established.
- `test_real_browser_connection_diagnostics.RealBrowserConnectionDiagnosisTests.test_mcp_status_reports_tool_surface_and_unpublished_tool_names_its_setting`:
  the expected dictionary uses a hard-coded 104-tool catalog and omits the
  current `owner` field. The observed dictionary includes `owner` and 73
  unpublished tools; the old expectation computes 69. A repair must test the
  actual catalog and valid attribution, not ignore these fields indiscriminately.

## Benchmark prerequisites not yet validated

Benchmark issues #136 and #137 remain open:

- #136: native coexistence reference code requests a snapshot after `tabs new`
  already supplied it, then cannot use an `unchanged_since` response as a tree.
- #137: native viewport setup navigates a user-owned tab rather than seeding
  through the user channel.

Their opt-in reproductions were **not** part of the 410-test default run above
and have not yet been executed in this goal. They must be resolved or their
validation scope explicitly bounded before #504 relies on those checks.

## Approved repair and revalidation

The user approved separate prerequisite test fixes. The Kitsune repair is on
`test/497-real-browser-baseline`; no `src/` or `extension/` files changed:

- Read the current `tab` response and preserve current/index, URL and ownership
  checks. Obtain a new inventory explicitly where the scenario needs all tabs.
- Make the existing long-header fixture long enough to exercise the unchanged
  greater-than-12,000-character assertion with compact same-origin URLs. Keep
  the delta, below-the-fold control, click-count and ref-lifetime assertions.
- Compare unpublished tools against the mode-applicable registry and assert a
  deliberately configured owner, rather than a stale total and missing owner.
- Route the five modified in-process MCP helpers' separate activity POSTs to
  their private test bridge as well as their tool requests. This is not a claim
  that every unrelated existing helper has been audited for this property.

Revalidation on 2026-11-10:

| Check | Result |
| --- | --- |
| Seven affected suites plus the two individual failing tests | 28 tests; all passed; 148.478 s |
| Standard Python discovery | 1,249 tests; 235 skipped; no failures/errors; 159.564 s |
| Complete `test_real_browser*.py` discovery with Zen | 253 tests; 10 skipped; no failures/errors; 1,100.806 s |
| `git diff --check` | Passed |

The full browser rerun used the same command above with
`KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-497-full-e2e`. The ten opt-in skips remain
unverified; they were not converted to passes. The standard discovery and full
browser rerun overlapped, so elapsed times are diagnostic, not benchmark data.
The original failed run remains part of the record, not replaced by the green
rerun. Node harnesses and the schema baseline above are unchanged by these
Python test-only edits.

## Gate

Kitsune's non-OS-input real-browser regression is now green on the separate
prerequisite test branch. The overall baseline milestone remains **in progress**
until benchmark #136/#137 are reproduced and addressed or explicitly bounded.
Keep these repairs separate from #498's product redesign; no model comparison,
default switch, merge or release is authorized by this regression result.
