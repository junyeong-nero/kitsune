# #500 CPython/WASI — approved private feasibility experiment

Status: **positive limited feasibility evidence, not an implemented authority
boundary or a selected production runner**. The operator approved this private
experiment, not production adoption, a system-wide installation, models, merge,
or release. #500 is still in progress. Existing defaults and #504 methods are
unchanged. See [the trust model and native investigation](500-authority-feasibility.md)
and [artifact provenance](500-wasi-artifacts.json).

## What was obtained and built

Only official upstream downloads were used: Wasmtime **50.0.1** (Python wheel
and build-only CLI), wasi-sdk **33.0**, and CPython **3.15.2 source**. The interpreter
below is a **local build of official source**, not an officially distributed WASI
binary. The third-party repository advertising unofficial CPython/WASI builds
was not used.

All four downloaded files matched the publisher's SHA-256 and size. Archive
members were checked for absolute/parent paths, outward links, special files,
expected top-level prefix and a 4 GiB extracted regular-file bound before extraction.
HTTPS/digest checks are **not signature verification**; the CPython Sigstore bundle
was used for its digest, not verified as a signature. The inspected CPython tag
and wasmtime-py commit were reported unsigned. Exact URLs/digests/source commits
are in the manifest. An initial urllib certificate-verification error was retained;
curl with TLS verification enabled was used, not an insecure TLS fallback.

Experiment root: `/tmp/kitsune-500-wasi.PW7dxU` (canonical `/private/tmp/...`).
It contains separate HOME, tmp, cache, downloads, logs and venv directories.
Wasmtime was installed with `uv --no-config`, local-wheel `--no-index --no-deps
--require-hashes`, disabled Python downloads, and a clean environment. Host Python
is **3.12.14**; installer is **uv 0.7.20**. No project dependency/lockfile, global
PATH or original worktree was changed.

The official `Platforms/WASI` helper was used (the old `Tools/wasm/wasi` paths
are deprecated), driven by Python **3.13.5**, `-S -B -X cpu_count=2`:

1. `configure-build-python --quiet --logdir <root>/logs -- --prefix=<root>/build-only-prefix --without-ensurepip`
2. `make-build-python --quiet --logdir <root>/logs`
3. `configure-host --quiet --logdir <root>/logs --wasi-sdk <root>/wasi-sdk-33.0-arm64-macos -- --prefix=/runtime --without-ensurepip`
4. `make-host --quiet --logdir <root>/logs`

All ran from the extracted CPython directory with private HOME/TMPDIR and a PATH
containing the pinned Wasmtime CLI plus `/usr/bin:/bin`. Both make stages used
**two jobs**. No install target was run. The generated build HOSTRUNNER preopens
the whole temporary CPython checkout for trusted build/test work; **it is not the
untrusted worker configuration**.

The tarball build printed `fatal: not a git repository ... .git`; this was not
silently counted as success. The completed build log, generated module and real
constrained guest execution were checked. Its final module summary was **78
built-in, 0 shared, 27 n/a, 1 disabled, 10 missing, 0 failed on import**. Optional
missing modules include `_ctypes`, `_ssl`, `zlib`; `_sqlite3` is disabled. `_socket`
**is built in**—network tests below exercise it rather than assume it is absent.
This is not a drop-in promise for native extensions, third-party packages,
threads or arbitrary asyncio applications.

Built `cross-build/wasm32-wasip1/python.wasm`:

- **32,498,958 bytes**
- SHA-256 **`ee0b8197c2dbed91d951be35644ac36afa676de326a2ce38a5ba95551c8f89ae`**
- target `wasm32-wasip1`; guest prefix `/runtime`
- compiler clang **22.1.0-wasi-sdk**, LLVM commit `4434dabb69916856b824f68a64b029c67175e532`
- build logs: `cpython-wasi-4fpxjr12.log`, `cpython-wasi-_eb4vugn.log`,
  `cpython-wasi-009nkmw2.log`, `cpython-wasi-n8qccb3s.log` under `<root>/logs`

## Actual boundary exercised

`scripts/probe_wasi_delegation.py` is an authored `unittest` investigation, not a
public script runner, SDK or security approval. It requires the pinned private
engine/module and exposes only fixed child scenarios. Guest programs really run
inside CPython/WASI, not native Python with hidden imports.

- The module imports **42 WASI preview-1 functions**; no custom bridge/credential
  host function is linked. No inherited socket is placed in its WASI descriptor
  table. Missing raw socket-creation/connect/process-spawn/bridge imports are
  checked by actual linker instantiation, separately from Python API availability.
- Guest environment is empty. Guest argv is explicit. Preopens are the public
  CPython `Lib` tree, an empty public `lib-dynload` directory, and (only for file
  tests) an authored public canary directory, all `fs_mutable=False`. No personal
  HOME, project, grader, actual token or browser profile is preopened.
- Store limits: **128 MiB linear memory**, one memory/table/instance, 100,000 table
  elements. Threads, memory64, multiple memories and components are disabled;
  compilation is sequential. Fuel and epoch interruption are enabled. Test budgets
  are probe settings, **not selected product limits**.
- IPC children receive fresh pipes with `close_fds=True` and a clean environment.
  One child additionally sets a real host **RLIMIT_NOFILE soft/hard 64** before
  engine startup, demonstrating the FD ceiling against repeated guest opens.
- The empty `lib-dynload` preopen removes the initial smoke's missing-directory
  warning without adding access to native libraries or a private directory.

Reproduction after the pinned private build:

```sh
env -i HOME=/tmp/kitsune-500-wasi.PW7dxU/home PATH=/usr/bin:/bin \
  TMPDIR=/tmp/kitsune-500-wasi.PW7dxU/tmp \
  /tmp/kitsune-500-wasi.PW7dxU/venv/bin/python -I -B \
  /tmp/kitsune-500-authority/scripts/probe_wasi_delegation.py \
  --root /tmp/kitsune-500-wasi.PW7dxU
```

## Results: 15 tests, zero skips/failures/errors

Final run: **15 passed in 19.545 s**. Earlier 11- and 13-test runs also passed and
remain retained; review added actual Python TCP/UDP attempts and the host FD cap.
The suite is fail-fast, not a collection of skips counted as passes.

| Check | Actual observation |
| --- | --- |
| Ordinary CPython math/JSON | `math.isqrt(81) == 9`, platform `wasi`, empty environment; 40 MiB linear memory |
| Fresh instance | Modified builtins/global environment from one instance absent in the next |
| Files, absolute/relative symlinks, traversal | Public canary readable; existing outside dummy file, both symlinks and parent traversal denied |
| Read-only preopen | Truncate/create/unlink/rename denied; parent verifies public and dummy files unchanged |
| Host credential FD | Parent can read its dummy FD (duplicated at 1024+); guest sees EBADF, not the host FD namespace |
| Real Python TCP/UDP | Both working private local host controls; guest socket creation denied with errno 58; no canary connection/datagram |
| Raw WASI socket call | Host connected socket FD has a successful send/receive control; raw WASI `sock_send` returns BADF (8), no received guest byte |
| New capabilities | `sock_open`, `sock_connect`, `proc_spawn`, custom `unrestricted_bridge` fail as unknown imports in fresh stores |
| Python process calls | fork/execv/system absent; subprocess raises OSError; raw spawn linkage separately denied |
| Host FD limit | Guest successfully opens some public files, then fails before 64; child confirms native soft/hard `[64, 64]` |
| Same 64 MiB allocation, different caps | Succeeds under 128 MiB (linear memory 89,653,248 bytes); raises MemoryError under 64 MiB (remains 40 MiB) |
| Noncooperative compute | Fuel exhaustion and independent epoch interrupt trap after an `entered` marker; guest `except BaseException` cannot catch either |
| ASCII output flood | Captured stdout+stderr at most 16 KiB; overflow triggers epoch trap even when guest catches I/O errors |
| Blocking native stdin | Epoch increment alone does **not** stop a blocked WASI host read; parent deadline kills/reaps child with SIGKILL (`-9`) |
| Sequential async composition | Real private pipes carry three requests/replies; top-level `await`, loop/branch/filter selects `[0, 2]` |
| Syntax/runtime failures | Both are nonzero guest exits (1), with the expected bounded diagnostic markers |

The IPC fixture returns authored numeric data. It demonstrates language/transport
feasibility, **not browser SDK correctness, policy enforcement or trusted receipts**.
No mock result is being substituted for an actual browser conformance test.

Final retained evidence:

- `<root>/wasi-probe-20261110T165752.json`
- Report SHA-256: **`5eb469f584800a053681be84a8eb4f8b115065c1817ec53d121b69128f5fbbd8`**
- Probe SHA-256: **`5fc6c3633ade5c7e9a11c4576bc4b3735f1555bf07fb14653c6ad5e6dfa48919`**
- Earlier reports: `wasi-probe-20261110T160625.json` (11 tests),
  `wasi-probe-20261110T161212.json` (13 tests), `wasi-probe-20261110T163712.json`
  (15 tests before replacing optimization-removable pin assertions with explicit errors).

AST parsing with Python 3.10 grammar and retained source/report hash assertions
passed. No `src/`, extension or product metadata changed; full Python/Node/browser
regressions were not rerun for this investigation-only checkpoint. They remain
required for actual runtime changes.

## Costs, limitations, and next gate

This experiment provides a credible alternative to unrestricted native Python;
it does **not** make #500/#501 complete.

- **Linear memory is not whole-host RSS.** Engine/compiler code, native stack,
  WASI host allocations, callback copying and transport buffers are additional.
  The FD-limit probe narrows one host-resource risk, not every host allocation.
  No hard whole-process resident-memory cap is claimed. The production threat
  model/resource accounting must explicitly address the remaining host resources.
- Fuel/epoch only interrupt guest execution at engine checks. The observed native
  stdin wait requires an independent process deadline/kill/reap path. Browser
  operation cancellation and uncertain effects still need their own trusted ledger.
- The output test bounds **ASCII capture**, not a finished UTF-8/JSON/image/selected
  output protocol. Invalid UTF-8, framing expansion, oversized callbacks, malformed
  messages, source limits, operation limits and image handles remain implementation
  and actual-entry-point conformance work. This probe's trusted code-in-argv is not
  a production source transport (including embedded-NUL handling).
- General cleanup, worker threads, descendants, host-memory amplification,
  multi-run leakage, broker IPC spoofing, real mode/grant/URL/ownership/activity
  parity, revocation, approval expiry/reuse/self-approval, redaction and partial
  effects have **not** passed through the actual delegated browser entry point.
- The local logical file sizes are 32,498,958 bytes for the module, 55,985,828 for
  the full extracted stdlib tree (including tests/cache), and 24,059,392 for the
  installed engine package: about **107 MiB**, excluding the host interpreter,
  build-only SDK/CLI, sources and caches. This is not a selected distribution size.
- An initial single cold module compilation took **3.949 s**, then its small math/
  JSON guest took **0.0367 s**. These are exploratory observations, not a C/D
  benchmark. The unit probe reuses a compiled module in its parent but creates fresh
  stores; IPC children compile separately. No persistent Python locals are reused.
- Optional modules/native-package/async framework compatibility, artifact packaging,
  support matrix, cache trust, startup failure handling and cold/warm costs must be
  explicit before adoption. Only this macOS arm64 private setup was measured.

Recommended next step, subject to the approval scope: build an **opt-in candidate**
using a separate per-run WASI worker and the shared trusted operation broker. Keep
secrets/approval/receipts outside the worker, expose no general host calls, use
bounded IPC and independent cancellation, and fail closed if pinned resources or
required limits are unavailable. Run all #500/#501 actual-entry-point checks;
stop on a missing boundary, not an unrestricted/native fallback. Product defaults,
models, merge and release remain independently gated.

PR #496 remains OPEN at `97dc9d67f40cf55c4dd91b8985fac083fced9781`.
Its final handoff contract must be coordinated explicitly; this experiment did
not merge/cherry-pick it or implement a parallel approval authority.
