# #500 approved foundation integration and E2E activity isolation

Date: 2026-11-10. This validates the **prerequisite foundation**, not a delegated
broker, MCP Python runner, candidate rollout or #504 comparison. #500 remains in
progress. No model was run and no product default/main/GitHub PR/release was changed.

## Explicit follow-up approvals

After the [15-test private WASI experiment](500-wasi-feasibility.md), the operator
selected both:

- **조건부 WASI 구현 승인**: implement a CPython/WASI opt-in candidate for #500/#501
  on a separate branch; stop if required host-resource, IPC or actual authority
  conformance fails. This is not permission to omit those gates or enable defaults.
- **별도 통합 브랜치 승인**: locally merge pinned #496 with the #506 foundation in
  a new integration branch, preserve existing branches, and make dependencies
  explicit. This is **not** approval to merge a GitHub PR or main.

`integration/500-approval-foundation` was created in
`/tmp/kitsune-500-foundation`, including the #507 investigation checkpoint. Its
conflict-free local merge is **`17ea49a1051dc1ef75e61aa00b5125e6cf29e80b`**, with parents:

1. `761983272c3b2991c273537b81b6576ae915e942`: #507, inheriting #506 and prerequisite #505.
2. `97dc9d67f40cf55c4dd91b8985fac083fced9781`: the explicitly approved #496 HEAD.

#496 was still OPEN/CLEAN at that head when integrated. Its source changes were
reviewed, not silently reimplemented: simultaneous browser/client handoff,
first-answer-wins, one-step prompts, cancellation and removal of requestTab's
scheduler lease. Native token lifetime is 120 s; bridge/server wait margins do
not extend authority. The model must not receive the confirm token.

The original design/benchmark/investigation worktrees and branches were preserved.
The integration merge was committed as a validation foundation, **not a claim that
its first E2E run was fully isolated** (see below). The corrective test-only branch
`test/500-e2e-activity-isolation` is separate and stacked on this foundation; future
implementation must include that repair. There is no additional local merge of
this repair and no GitHub PR merge.

## Integration validation and a discovered isolation violation

A new project venv was made with `uv --no-config sync --locked --offline`, using
existing cached dependencies and Python 3.12.6. No lockfile changed. Source binding
was checked against the new worktree, not the original checkout's editable install.

| Run | Result | Interpretation |
| --- | --- | --- |
| Initial integrated Python, documented private HOME/HTTP wrapper | 1,277 tests / 236 skips / zero failures/errors, 187.121 s | Correct source; normal WS diagnosis setting retained |
| Initial integrated Node | 70 harnesses passed | Includes #496 handoff/race tests |
| Initial full real-browser | 254 tests / 10 skips / zero assertion failures/errors, 1,044.637 s | **Not fully isolated**: nine default-port activity POSTs observed |
| Safe unit reproduction before repair | 1 failure, 0.028 s | A spy, not a real bridge, caught the unintended activity call |
| After repair, focused unit + real-browser | 3 passed, 6.809 s | One regression and both bounded-snapshot tests; parent default-port attempts 0 |
| After repair, full isolated Python | 1,278 tests / 236 skips / zero failures/errors, 188.317 s | Same documented unit isolation; one new regression |
| After repair, Node | 70 harnesses passed | Unchanged production/extension code relative to the foundation |
| After repair, strengthened full real-browser invocation | 254 tests / 10 skips / zero failures/errors, 1,039.086 s | Private outer HOME/token/reserved HTTP, private per-class browser/bridge; parent default-port attempts 0 |

The first real-browser console showed nine successful POSTs to
`http://127.0.0.1:47651/api/mcp-activity`, around 18:36. We stopped the next
implementation gate and disclosed this rather than calling it an isolated pass.
`KitsuneMCP.call_tool` reports each outer call using `server._client.post`, separate
from `tools._post`. `RealBrowserBoundedSnapshotTests.core` patched only the latter;
its two tests issue four and five core calls. This unchanged helper predated #496.

The observed route carries an empty activity body, not a page action. Nevertheless,
it reached a default endpoint and could use the client's normal credential lookup;
we do **not** claim zero impact or that the original run preserved complete
isolation. We did not query, reset or otherwise modify the user's default bridge
in response. No token contents were inspected or included in evidence.

The minimal repair also routes `server._client.post` through this class's existing
fixture `self.call`, following the already repaired helpers from #505. Original
snapshot bounds, content, ref and actual-action assertions are unchanged.

`tests/test_bounded_snapshot_isolation.py` replaces the default client's post method
with a rejecting spy before invoking the helper. Before the fix it safely fails
with `call('/api/mcp-activity', {}, timeout=0.25, start_bridge=False)`; afterward it
asserts no default-client call **and** exactly one fixture activity report. Merely
swallowing the error or disabling activity would not pass. This is a helper
regression, not delegated authority conformance.

Retained reproduction HOME: `/tmp/kitsune-500-isolation-red.VxrULR`.
Focused HOME: `/tmp/kitsune-500-isolation-focus.tslwJE`.
Corrected full E2E outer HOME: `/tmp/kitsune-500-isolation-e2e.CKmglN`.
The existing failure-artifact harness writes evidence on assertion/setup failure;
it did not create an artifact directory for the assertion-green first run. Its
isolation violation is therefore preserved in the execution console and this log,
not falsely described as a saved failure bundle.

Late-response error logs in standard unit discovery are from the intentional
late/expired/cancelled elicitation cases (`test_ask_user`,
`test_elicitation_approval`, `test_tab_handoff`), whose negative assertions pass.
They are not omitted assertion failures. Unpublished-tool warnings are also
retained. The real browser is the installed Zen binary used by the baseline;
OS-input-specific opt-ins remained unset. The 10 skips are **not** browser passes.
The harness uses a temporary add-on; this does not replace the later required
signed-extension conformance of the actual candidate.

## Strengthened real-browser reproduction

Keep the per-class harness isolation and add an outer private default-client
context **before importing Kitsune**. Do not reuse the old plain E2E command in a
live user environment. Run from the correct checked-out repository/venv:

```sh
test_home=$(mktemp -d /tmp/kitsune-500-isolation-e2e.XXXXXX) || exit 1
env -u KITSUNE_WS_PORT -u PYTHONPATH -u KITSUNE_E2E_TRUSTED_INPUT \
  -u KITSUNE_E2E_REVEAL_MENU_BAR HOME="$test_home" \
  KITSUNE_HOME="$test_home/kitsune" KITSUNE_TOKEN=isolated-unit-test-token \
  KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-500-activity-full-e2e \
  .venv/bin/python - <<'PY'
import os, socket, sys
from pathlib import Path
attempts = []
def guard_default_bridge(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and len(address) > 1 and address[1] in (47651, 47652):
            attempts.append(address)
            raise PermissionError('Test attempted the default bridge endpoint')
sys.addaudithook(guard_default_bridge)
with socket.socket() as guard:
    guard.bind(('127.0.0.1', 0))  # reserve, do not listen
    os.environ['KITSUNE_HTTP_PORT'] = str(guard.getsockname()[1])
    import unittest, kitsune
    assert Path(kitsune.__file__).resolve().is_relative_to(Path.cwd().resolve() / 'src')
    suite = unittest.defaultTestLoader.discover('tests', pattern='test_real_browser*.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Default bridge connection attempts in Python parent:', len(attempts))
    raise SystemExit(0 if result.wasSuccessful() and not attempts else 1)
PY
```

The audit hook counts/rejects calls even if best-effort reporting catches its
exception. It covers the **Python parent**, not a process-tree network firewall.
Children retain the existing harness's private env/ports/profiles; the reserved
HTTP port and outer test HOME/token also prevent an accidental default-client
fallback from using the user's configured bridge. This is a regression safeguard,
not a complete suite-wide isolation audit or the untrusted worker sandbox.

Full unit discovery still uses the isolated wrapper in [499-validation](499-validation.md);
do not set a custom WS port and weaken connection-diagnosis expectations.

#504 A/B source and all frozen methods remain unchanged. This integrated branch is
only a future candidate foundation; passing #499 design tests is not served
candidate metadata, policy parity, Python runner or benchmark evidence.
