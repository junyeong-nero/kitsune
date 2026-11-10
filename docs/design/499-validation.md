# #499 design validation log

Date: 2026-11-10. Branch: `design/499-flat-toolset-contract`, stacked on PR #505.
This log concerns inventory/contract artifacts, not implementation of the proposed
SDK or proof that a runner is secure. Product/extension source remains unchanged.

## Inventory and mapping

`uv run --locked python scripts/toolset_inventory.py --check-map` reports:

- tools: 109; legacy low-level Python functions: 104; gateway: 3;
  Browser/Tab/namespace Python operations: 129; HTTP method/route pairs: 140;
  container action names: 88.
- Total: 573 entries in 48 explicit rationale groups; zero unmapped, duplicate or
  stale entries. Ten focused inventory/mapping tests pass.
- This is overlapping entry-point coverage, not distinct-feature equivalence.

The proposed destinations of the **109 registered tool names** are 25 direct,
9 composition, 73 operator and 2 unsupported deprecations. The three gateways
are separately deprecated. This is a substantial delegated coverage change, not
just shorter names. The map records limitations; the #504 report and default
approval must not treat an operator-only feature as equivalent candidate coverage.

## Regression results

| Run | Result | Interpretation |
| --- | --- | --- |
| Initial inventory foundation | 1,255 tests; 235 skips; zero failures/errors; 156.309 s | Before the four additional legacy-surface/mapping tests |
| Expanded inventory, ordinary environment | 1,259 tests; 235 skips; zero failures/errors; 156.652 s | Green assertions, but existing activity-report paths were not all isolated |
| Isolation attempt with custom HTTP **and WS** environment | 1,259 tests; 235 skips; **4 failures**; 178.992 s | Invalid diagnostic precondition for several existing unit tests; retained below |
| Corrected isolated focused run | `test_action_errors.py`: **24 passed**, 0.136 s | Same tests/product code; correct WS diagnostic configuration |
| Corrected isolated full run | **1,259 tests; 235 skips; zero failures/errors**, 177.972 s | Temporary HOME/KITSUNE_HOME, test token, reserved non-listening HTTP port |
| Final schema/signature/footprint review | **24 focused tests passed** | 10 inventory + 11 design + 3 footprint checks |
| Final isolated full run | **1,273 tests; 235 skips; zero failures/errors**, 179.240 s | Same safe unit invocation; includes the 14 new design/measurement tests |
| Node | **70 harnesses passed**, including final rerun | No extension source changes |
| Diff checks | `git diff --check` passed; no `src/` or `extension/` diff from prerequisite branch | Design/test tooling only |

Final environment: Python 3.12.6, jsonschema 4.26.0, Node v26.12.0. The intended
JS declaration syntax check uses Node's stripTypeScriptTypes (Node 22.13+; the
release workflow selects Node 22). This is not implemented JS execution support.
The final Python and Node runs were concurrent; elapsed times are diagnostics,
not benchmark comparisons. Opt-in skips are not
real-browser passes. No new browser or model run was needed for these artifacts;
the prior real-browser baseline is in [498-baseline.md](498-baseline.md).

### Isolation investigation (do not hide the failed run)

The ordinary suite logged successful POSTs to the default `/api/mcp-activity`
endpoint. Some existing tests patch tool `_post`/`_get` but not the separate MCP
activity-report client (for example `test_core_output_paths.py` and the subprocess
in `test_unpublished_tools.py`). Do not use that ordinary invocation in a live
user environment for subsequent stages. This is not a suite-wide isolation audit
or a claim that existing tests were repaired by this design PR.

The first isolated invocation set both bridge ports to separately reserved,
non-listening ports. Four assertions in `test_action_errors.py` expected
`extension_disconnected` but received `port_conflict`: the two subtests of
`BridgeTransportErrorTests.test_not_connected_is_retryable_because_nothing_was_sent`,
`McpErrorResultTests.test_extension_disconnected_is_an_error`, and
`SessionFailureTests.test_disconnected_save_fails_and_keeps_the_saved_session`.
Its temporary HOME is preserved at `/tmp/kitsune-499-unit.QJgpeR`.

The cause was **not** a changed product result or the socket reservation itself.
`bridge._connection_diagnosis()` intentionally reports an `extension_ws_port`
conflict when `KITSUNE_WS_PORT` differs from the expected extension port before an
extension has connected. Those in-process tests assume the ordinary WS
configuration. Unsetting that override restores their precondition without
modifying an assertion or forcing a successful result.

The successful rerun reserved only the process-default HTTP port, so stray
activity requests cannot connect or launch a bridge there. It retained the normal
in-process WS diagnostic constant; it did **not** launch/listen on a default WS
port. Wire tests continue to start their own private bridge fixtures and use
those fixtures' explicit ports. All other connection state and credentials were
temporary/test values. No running user bridge was stopped, replaced or reconfigured.

### Safe reproduction used for the successful full rerun

Run from the repository root; failure keeps its temporary HOME for inspection:

```sh
test_home=$(mktemp -d /tmp/kitsune-499-unit.XXXXXX) || exit 1
env -u KITSUNE_WS_PORT HOME="$test_home" KITSUNE_HOME="$test_home/kitsune" \
  KITSUNE_TOKEN="isolated-unit-test-token" uv run --locked python - <<'PY'
import os
import socket
import subprocess
import sys
with socket.socket() as guard:
    guard.bind(('127.0.0.1', 0))
    env = {**os.environ, 'KITSUNE_HTTP_PORT': str(guard.getsockname()[1])}
    result = subprocess.run(
        [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], env=env)
    raise SystemExit(result.returncode)
PY
result=$?
if [ "$result" -eq 0 ]; then
  rm -r "$test_home"
else
  printf 'Preserved failed isolated unit HOME: %s\n' "$test_home"
fi
exit "$result"
```

This setup is for standard unit discovery, **not** the real-browser harness. Real
browser checks must continue to use their own isolated profiles/bridge/HOME and
matching extension ports. Do not apply a custom WS override to unrelated unit
connection-diagnosis tests and then weaken those tests to accommodate it.
