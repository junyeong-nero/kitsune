# #500 implementation checkpoint: the delegated worker and its trust boundary

Date: 2026-11-11. Branch `feat/500-shared-authority`, draft PR #509; follows
[500-wait-and-screenshot.md](500-wait-and-screenshot.md) (`4e49b53`).

This is the first checkpoint in which agent-written code runs through a real
delegated entry point. **It is still an opt-in candidate that nothing registers**:
no MCP tool calls it, product defaults are unchanged, and #500 is not complete
(see "Not established"). The approval for it was conditional; the conditions and
what was found for each are in the last section.

## Trust model

```
 trusted: MCP server process (coordinator)            untrusted
 ┌──────────────────────────────────────────┐
 │ FlatBroker ── FlatSession                │
 │   mode, scope, owner, handles, refs      │        ┌──────────────────────────────┐
 │   bridge token, connection binding       │        │ engine process (script_host) │
 │        │                                 │  pipe  │  no env, no credential       │
 │ script_runner ◄── JSON lines ────────────┼────────┼─►┌──────────────────────────┐│
 │   deadline, op count, output, images     │ stdin/ │  │ WASI guest: CPython      ││
 │   ledger (from broker results)           │ stdout │  │  script + SDK            ││
 └────────┬─────────────────────────────────┘        │  │  read-only stdlib only   ││
          │ HTTP + token + binding + receipt header  │  └──────────────────────────┘│
 ┌────────▼─────────┐   WebSocket   ┌───────────┐    └──────────────────────────────┘
 │ bridge           ├───────────────► extension │
 │ mode, URL policy,│               │ ownership,│
 │ ask-before-act,  │               │ activity, │
 │ receipts         │               │ redaction │
 └──────────────────┘               └───────────┘
```

What each side holds:

| | Coordinator | Engine process | Guest (the script) |
| --- | --- | --- | --- |
| Bridge token | yes | no | no |
| Bridge connection | yes | no (no socket use) | no (no socket) |
| Mode, scope, owner id | yes, immutable per session | no | no |
| Backend tab ids | yes | no | no; handles only |
| Approval token / secret | stays in the bridge | no | no |
| Files | its own | pinned runtime, private empty cwd | the read-only standard library |
| Environment | its own | `HOME`, `TMPDIR` of a private directory | empty |

A script can do exactly one thing to the outside: write a line asking for an
operation. Everything it writes is a request or its own output, never evidence.

## Where each rule is enforced

| Rule | Enforced by | Not by |
| --- | --- | --- |
| Which operations exist; argument shapes | `flat_operations` (broker) | the SDK |
| read / draft / act | `FlatSession.admit`, then the bridge's routes and policy | a request field |
| Tab handles, refs per session | `FlatSession` | the script |
| Ownership, user activity | extension | |
| URL policy, ask-before-act, sensitive-field inspection | bridge | |
| Redaction of reads | extension | |
| Connection still the same | bridge, at dispatch | |
| What was sent, what was answered | bridge receipt → runner ledger | script output or exit status |
| Deadline, operation count, output, images, frame size | `script_runner` | the guest |
| Guest memory | engine store limit | |
| Engine process memory, descriptors | engine process (watchdog, `RLIMIT_NOFILE`) | |
| End of run | coordinator kills the process group and reaps it | the guest's cooperation |

The SDK inside the guest (`script_guest.py`) enforces nothing. A test bypasses
it and writes frames by hand; the results are the same.

## Protocol

One ASCII JSON object per line. Guest to coordinator: `op`, `emit`, `emit_image`,
`out`, `end`. Coordinator to guest: `source` once, then one `result` per `op`.
The engine writes `host_ready` before the guest starts and nothing after.

- A frame with unknown kind, missing or extra keys, non-ASCII bytes, lone
  surrogates, more than 512 KiB, or no final newline ends the run
  (`protocol_error`). So does a forged `result` or `host_ready`.
- `op` becomes one `FlatBroker.call`. Operations are sequential; the 101st ends
  the run (`operation_limit`).
- Images never cross the pipe. A screenshot's result names a handle; the bytes
  stay with the coordinator, at most four and 16 MiB per run. `emit_image(handle)`
  selects one for delivery.
- Selected output (`emit` value + stdout + stderr) is at most 16 KiB of UTF-8;
  beyond that the run ends `output_limited`.

## Run statuses and the ledger

`completed`, `failed` (`syntax_error`, `runtime_error`, `protocol_error`,
`operation_limit`, `script_exited`, `invalid_image_handle`, `host_failed`, …),
`timed_out`, `cancelled`, `output_limited`.

`completed` means the program reached its end. It says nothing about any
operation. For that there is the ledger: one entry per requested operation with
the broker's call id, outcome, error code and the bridge's receipt.

- An operation in flight when the run ends is recorded `unknown` and
  `settled: false`. The request is left to finish inside the broker, which keeps
  its lock and bookkeeping until then. It is not cancelled and never sent again.
- A caught `BrowserOperationError`, a printed "success" or an emitted fake ledger
  changes nothing in it.

## Not established

1. **Whole-host memory is watched, not capped.** `RLIMIT_AS`/`RLIMIT_DATA` cannot
   be lowered on this macOS (the earlier finding). The engine process polls its
   own resident memory every 50 ms and exits above 1 GiB; a burst between polls
   is not excluded. Observed peaks: 297 MiB for a trivial script, 344 MiB with the
   guest at its 128 MiB limit, 460 MiB at most in the tests.
2. **A same-user escape of the engine is outside the boundary.** The engine
   process is the same OS user. If the WebAssembly engine itself were broken out
   of, that process could read the token file by path. This is the existing
   documented limit of Kitsune's local model, not something this adds a defence
   for.
3. **Provenance and packaging.** The interpreter is a local build of official
   CPython source; the manifest's SHA-256 detects a swapped file, not an
   untrusted origin. There is no product dependency on Wasmtime and no supported
   install path. Only macOS arm64 was run.
4. **Start-up cost.** Each run compiles the 32 MB module again: about 3 s before
   the script starts, measured informally. The script's deadline starts after
   that. A compiled-module cache would cut it and raises its own trust question;
   none exists.
5. **In-flight cancellation.** Ending a run does not stop a request the bridge
   already accepted.
6. **Approval.** A held action cannot be continued (`browser_resume`,
   `browser_request_tab`, `browser_ask_user` have no adapter), so #496 is not
   yet coordinated beyond "the script cannot approve itself".
7. **Audit linkage.** Follow-up [500-audit-linkage.md](500-audit-linkage.md) adds
   the flat call ID to existing bridge audit entries and verifies the join from
   settled script ledger entries. Run IDs, in-flight/late receipt linkage and
   complete audit coverage are still missing.
8. **Signed extension.** All browser evidence uses the temporary-add-on harness.
9. **The guest reports its own end.** `completed` versus `runtime_error` is the
   guest's account of its program. It decides no outcome.
10. **Standard library scope.** The whole CPython `Lib` tree is readable. Threads
    cannot start, and no native extension loads; this is observed, not a
    compatibility promise.

## Evidence

Private HOME, synthetic token, private ports. Coordinator on Python 3.12.6; engine
on the pinned private venv (Python 3.12.14, Wasmtime 50.0.1); guest CPython 3.15.2.
The script tests are opt-in by `KITSUNE_E2E_SCRIPT_RUNTIME`; **the numbers below
are from runs with it set. Without it they skip, and a skip is not a pass.**

| Check | Result |
| --- | --- |
| Runner tests, real engine and guest, stub broker | 17 passed |
| Authority tests, real engine, guest, broker and bridge; scripted extension | 7 passed |
| Real browser: a script drives Zen through the broker | passed |
| Isolated Python discovery, runtime set | **1,389 tests / 244 skips / zero failures or errors**, 411.826 s |
| Node | 70 harnesses passed; extension source unchanged |
| Inventory map | 573 entries / 48 groups / zero errors |
| Full real-browser discovery, runtime set | **262 tests / 10 skips / zero failures or errors**, 1,040.630 s, one run |
| Python 3.10 grammar (coordinator, engine, tests), `git diff --check` | Passed |

Default-port connection attempts in the Python parent: zero in each run.

### What a script could not do (real guest)

From inside the guest, with these results:

| Attempt | Result |
| --- | --- |
| Read `/etc/passwd`, a host secret file, `~/.kitsune/token`, the bridge's token file | `FileNotFoundError` |
| List `/`, `/tmp`, `/Users`, `/runtime`, the current directory | `FileNotFoundError` |
| Path traversal out of the library directory | refused |
| Create a TCP or UDP socket; `urllib` request | `OSError` (errno 58) |
| Connect to the bridge's HTTP and WebSocket ports, even with the token in its source | no connection; the bridge logged no request |
| `subprocess`, `os.system`, `os.fork` | `OSError` / absent |
| Write into the standard library | `PermissionError` |
| `import kitsune`, `httpx`, `ctypes` | `ModuleNotFoundError` |
| Read the environment | empty |
| Open descriptors beyond its three pipes | two: the read-only library directories |

A listening socket in the test accepted no connection. Positive control: the
library directory is readable.

### What a script could not make the broker do (real broker and bridge)

Seventeen attempts from a read-mode script, each `not_dispatched`, none reaching
the extension: a click; `mode` as an argument; mode, user-tab, owner and
connection fields; raw routes (`/api/set-policy`, `/api/click`); `browser_evaluate`;
the legacy gateway; a nested script; a backend tab id; another session's handle
and ref; a ref spelled as a selector; closing a tab; a `file:` URL. The bridge's
policy was unchanged afterwards.

Self-approval: with ask-before-act on, a click on a submit control was held
(`needs_user`, empty error state). Repeating it, adding a token in two spellings,
an approval flag, an approval header, `browser_resume` and turning the policy off
were all refused; the click was never sent. The script then printed
`{"approved": true}`; that is in its stdout and nowhere else.

Revocation during a run: the coordinator revoked the session after the script's
second operation. Operations three to five were `authority_revoked`,
`not_dispatched`; the extension saw two. A later script and a direct call were
refused as well.

Uncertain write: the extension disconnected during a click. The script saw
`unknown`, tried twice more, and got `stale_tab_handle`, `not_dispatched`. The
extension received the click once.

### Limits (real guest)

- A busy loop, a sleep, a loop that catches every exception and a blocking read
  were each ended at a 700 ms deadline with the engine killed (`-9`) and reaped.
- An operation still in the broker at the deadline was `unknown`, unsettled;
  the broker finished it once and it was not requested again.
- Cancellation by event returned `cancelled`; cancelling the caller's task left
  no process and no directory.
- Output stopped at exactly 16,384 bytes; an oversized `emit` was refused; 150
  requested operations stopped at 100.
- A 200 MiB allocation raised `MemoryError` in the guest; unbounded allocation
  ended as a `runtime_error`. With the engine's own limit lowered to 64 MiB the
  engine exited (code 65) before running anything.
- A manifest with a wrong digest or another module did not run (code 64).
- Eighteen malformed or forged frames each ended the run with no operation.

### The real-browser scenario

`test_a_delegated_script_drives_the_browser_through_the_broker`: one script opened
a tab, read it, filled a field by ref, clicked a button three times in a loop,
selected an option, searched the page and captured the button. The page's own
record, read through a separate request, showed the field value, three clicks and
the selection. Inside the same script, navigating the user's tab, an ambiguous
selector and a made-up ref were refused and the user's tab kept its URL; a
connection to the bridge's port failed. The emitted image was a PNG held by the
coordinator. The tab handle the script returned then closed the tab by a direct
call.

### Failures during this step

- **A cancellation could lose every race.** `before()` returned a finished read
  ahead of a cancellation that had already happened, so a guest that always
  answered at once ran to its end. Found by the cancellation test (which passed
  in a standalone script and failed under the test loop's debug mode). A deadline
  had the same race. Both are now checked before anything is awaited.
- **The guest died while reporting a memory error**: formatting the traceback
  needed a lazy import with no memory left. The bootstrap now loads that path at
  start and releases the script's objects before formatting.
- **A transport was left for the garbage collector** after output-limited runs
  ("Event loop is closed" at exit). The runner now closes it itself.
- Three test expectations were wrong: the guest has no `/` at all; a long error
  message keeps its start and end rather than its tail; one string escape.

Retained homes: `/tmp/kitsune-500-script-first.FVAVG0` (four failures),
`/tmp/kitsune-500-authority-first.x90CZa` (one wrong escape), `/tmp/kitsune-500-authority-focus.uOlQXY`,
`/tmp/kitsune-500-worker-unit.fVikJz`, `/tmp/kitsune-500-script-e2e.X3Y38s`,
`/tmp/kitsune-500-worker-full-e2e.EUiJV2` (full run; its ten skips are the OS-input opt-ins, not the script scenario). Logs of each run are beside them in `/tmp`.

### Reproduction

The runtime is the private build of [500-wasi-feasibility.md](500-wasi-feasibility.md).
Its manifest, outside the repository:

```json
{"engine_python": "<root>/venv/bin/python",
 "wasm": "<root>/Python-3.15.2/cross-build/wasm32-wasip1/python.wasm",
 "wasm_sha256": "ee0b8197c2dbed91d951be35644ac36afa676de326a2ce38a5ba95551c8f89ae",
 "stdlib": "<root>/Python-3.15.2/Lib", "lib_dynload": "<root>/empty-lib-dynload"}
```

Set `KITSUNE_E2E_SCRIPT_RUNTIME` to its path and use the isolation wrappers of
[499-validation.md](499-validation.md) and
[500-integration-validation.md](500-integration-validation.md).

## The conditions of the approval

| Condition | Found |
| --- | --- |
| Host resources | Guest memory capped; descriptors capped; engine memory watched with a hard exit, not an OS cap (item 1). Recorded as not fully established. |
| IPC | Bounded, strict, tested against malformed and forged frames. |
| Actual authority | Verified through the real entry point for the fourteen implemented operations; approval/resume not yet (item 6). |

**Gate correction (2026-11-11):** the earlier conclusion that no condition required
stopping, with items 1 to 3 deferred until #502, was too broad. Whole-engine
resource enforcement is a #500 condition and is not established by the macOS
watchdog. Publication remains gated. The user approved Linux cgroup v2 boundary
investigation, not an installation or a weaker acceptance criterion; see
[500-linux-cgroup-investigation.md](500-linux-cgroup-investigation.md). Runtime
provenance and the declared same-user/engine trust boundary also remain explicit
candidate gates.
