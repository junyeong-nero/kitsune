# #500 delegated authority — feasibility investigation

Status: **not implemented, not a selected/validated sandbox**. No default or
trusted developer API change. Work is isolated in a separate worktree based on
#499 (`9ae8549`); model comparisons remain unapproved. #504 methods are in
kitsune-benchmark PR #145 (`52e455b`).

The operator subsequently approved a private CPython/WASI experiment. Its
[15 passing feasibility tests, artifact pins and limitations](500-wasi-feasibility.md)
are recorded separately; they do not complete #500 or approve production adoption.

## Trust model before implementation

Proposed separation (to validate, not an enforcement claim):

```text
operator / MCP client user approval
          |
trusted MCP coordinator + shared direct-operation broker
  owns caller mode/scope, session/owner identity, bridge/approver secrets,
  handle/ref records, admission/revocation gate, receipts and redaction
          |                           |
existing BridgeClient            bounded per-run IPC
          |                           |
existing bridge policies         untrusted Python worker
          |                      no bridge/approval credentials
extension ownership/activity     no raw host/network capability
          |
authorized page operations
```

- Direct operations and worker requests must enter the same trusted broker. Every
  leaf and resumed operation must retain the operator's authority ceiling and
  recheck existing bridge/extension policies; hiding methods/imports is not a
  boundary. The trusted developer connect()/request() path stays separate.
- Agent code, its imports/native calls, stdout, selected results and any IPC fields
  are untrusted. They cannot choose a caller/mode/grant, provide approval, forge a
  receipt, widen URL policy or convert an uncertain write into a safe retry.
- Secrets stay in the coordinator/bridge, not worker arguments/environment/files.
  Only fresh run-owned IPC endpoints are inherited. All other file descriptors
  must close: macOS sandbox(7) explicitly says already-open descriptors may retain
  access regardless of a later sandbox policy.
- A worker may read only explicitly approved public runtime/stdlib/SDK resources;
  no personal HOME, project/grader files, token files, arbitrary network or new
  processes. Runtime roots containing credentials are not safe allowlists.
- Parent-controlled deadlines/output/operation limits, kernel resource limits
  where effective, channel revocation and forceful cleanup must survive hostile
  code. Killing a process does not undo browser effects. Memory/CPU denial-of-service
  protection must be tested and described, not inferred from a wall deadline.
- The threat model excludes a compromised OS/kernel/runtime, administrator/root,
  and independent hostile same-user processes modifying trusted code or attaching
  to an unsandboxed coordinator. This does not excuse an in-sandbox child reading
  the coordinator's credentials or impersonating its approvals.

## Candidate mechanisms to investigate

1. **Native macOS Seatbelt**: sandbox-exec exists on this host (macOS 26.5.1,
   arm64). A deny-default profile could allow only the actual interpreter/runtime
   reads and inherited pipes, with network/fork/other executable access denied.
   Need negative tests including raw native calls, symlinks, inherited descriptors,
   policy reapplication, process replacement, cleanup and real resource limits.
2. **OS container/VM or constrained Wasm interpreter**: potentially stronger or
   more portable boundaries, but no docker/podman/bwrap/deno/wasmtime executable
   was found on PATH in the initial inventory. No dependency is installed or
   selected merely to make the proposal appear executable.

Important native limitations found **before** choosing the implementation:

- `/usr/share/man/man1/sandbox-exec.1` explicitly marks sandbox-exec deprecated and
  recommends App Sandbox. `/usr/share/sandbox/*.sb` profiles label the language an
  Apple private interface subject to change.
- The installed SDK's `sandbox.h` marks sandbox_init unsupported/deprecated and
  documents only named-profile flags. A ctypes call with an undocumented custom
  profile flag must not be described as a supported public API.
- sandbox(7) says restrictions are generally applied at resource acquisition;
  inherited descriptors matter. Descendants inherit a sandbox. The behavior of
  any re-exec exception must be demonstrated, not assumed harmless.
- Availability of a binary or acceptance of setrlimit is not evidence of actual
  confinement or memory enforcement. Failed checks must stop requested scripting,
  never fall back to unrestricted Python.

Probe scope: fresh temporary directories, dummy credentials, denied local canary
sockets and controlled child processes only. No personal browser/profile/bridge,
real token file, external network, model inference or system sandbox settings.

## External approval dependency

PR #496 is still OPEN/CLEAN at `97dc9d67f40cf55c4dd91b8985fac083fced9781`.
It changes simultaneous browser/client handoff, one-step prompts, cancellation,
scheduler leasing and a 120-second handoff token lifetime (125-second wait).
The model still must not receive the approval token. The #499 continuation ceiling
of five minutes is not permission to exceed the underlying shorter approval TTL.

Its code has **not** been merged, cherry-picked or silently recreated here. Source
inspection and feasibility probes can proceed independently; integrating/finalizing
this external contract needs explicit coordination before claiming #500 complete.

## Evidence: native feasibility, not #500 conformance

Host: macOS **26.5.1 / 25F80**, Darwin **25.5.0**, arm64, Python **3.12.6**.
Reproduction (stdlib only; no package installation):

```sh
/Users/junyeong-nero/workspace/kitsune/.venv/bin/python -I -S -B \
  /tmp/kitsune-500-authority/scripts/probe_delegation_sandbox.py
```

The probe runs **only authored cases**. Its unconfined positive-control helper is
trusted test code, **must not be reused as the production runner**, and does not
accept user/agent programs through its CLI. Exit zero means evidence collection
finished, not that isolation is safe. The report explicitly has status
`feasibility_only_not_a_security_verdict`.

Latest raw report and profile are retained in:

`/private/var/folders/l8/ydsw65lj3fxgkpyx8p8ht3b80000gn/T/kitsune-500-seatbelt-h4bgw3lc/`

- Raw `report.json` SHA-256: `8b564f4cd84a1bdccffc02ba009338dd1b1b4d021a44912c5d43187ab0e0e34f`
- Probe source SHA-256: `5945e5c29febb633afe6a33c4fa2e55f6d863bfa467c8c6ff6e6b9d7ad57d925`
- Generated profile SHA-256: `44d7e52b3c0d7c3701d03406e592093e4c323c2802bd52aca9cf98303028b507`
- Imported system `dyld-support.sb` SHA-256: `06215a5d32689aefe395c29710e182eb54ba22162f50df8b4842290f8a19bf1c`

The candidate profile denies by default, but explicitly needs runtime read/map
access, OS dynamic-loader bootstrap rules, syscall entry, and selected hardware/OS
metadata (including hostname for ctypes' uname). It is **not** a claim that all
host information is absent. It excludes runtime site-packages. The process-exec
exception names the actual framework interpreter, not all executable files.
The imported Apple profile is private, version-dependent policy, not our own
stable security contract.

| Authored case | Unconfined control | Sandboxed observation |
| --- | --- | --- |
| Python math/regex/imports and JSON output | Runs, value 9 | Runs, value 9 |
| Dummy token read; symlink from allowed directory | Reads | Both denied, EPERM |
| Dummy file write | Writes | Denied, EPERM |
| Native `libSystem.open` via ctypes | Opens | Denied, EPERM |
| Fresh TCP connection to private loopback canary | Connects | Denied, EPERM |
| `fork`; subprocess of same interpreter | Creates/reaps child | Both denied, EPERM |
| Replace process with `/usr/bin/true` | Exits 0, deliberately no JSON | Denied, EPERM |
| Re-exec exact allowed interpreter, then read token | Reads | Read denied **after re-exec**, phase marker recorded |
| Reapply allow-default profile, then read token | Reads | Reapplication fails; read still denied |
| Clean environment | No KITSUNE_TOKEN | No KITSUNE_TOKEN; temporary HOME |
| Deliberately inherited dummy credential FD | Not needed | **Readable**, as sandbox(7) documents |
| Same FD withheld by launcher | Not needed | EBADF; no inherited descriptor |

Resource findings are a **gate**, not passing negative browser tests:

- Setting a 32 MiB soft/hard `RLIMIT_AS`, `RLIMIT_DATA` or `RLIMIT_RSS` failed with
  `ValueError: current limit exceeds maximum limit`, both outside and inside the
  sandbox. Single-call and soft-then-hard attempts were tried; a separate
  RSS-soft-only attempt also failed. The planned bounded 64 MiB allocations were
  therefore **not reached**. We do not claim that these measurements establish
  any effective memory ceiling or prove every possible macOS mechanism impossible.
- On this host `RLIMIT_AS == RLIMIT_RSS == 5`. The installed getrlimit(2) manual
  describes RSS as a paging preference under pressure, not a hard address-space
  limit, and DATA as an sbrk limit. These are not evidence that native allocations
  are capped.
- A one-second CPU limit terminated a cooperative process with SIGXCPU (`-24`).
  A fixture ignoring SIGXCPU ran until the **parent's four-second deadline** and
  was killed/reaped with SIGKILL (`-9`). A resource-setting call alone is not the
  noncooperative cancellation mechanism.
- Parent `communicate()`/kill/wait handles these fixed cases. General protocol,
  browser-operation cancellation, descendant/orphan resistance, output overflow,
  thread/native allocation and all network families have **not** been validated.
  In particular, this report is not a UDP/Unix-socket/Mach/ptrace confinement claim.

### Failed attempts retained

Initial profiles aborted Python with SIGABRT (`-6`) before any case ran. Selecting
the actual framework executable rather than its launcher and allowing executable
maps alone did not fix startup. The next profile booted Python but ctypes failed
at uname. Explicit dynamic-loader/bootstrap rules and the needed OS metadata
allowance produced the observed working interpreter above; no blanket file-read,
network or process allowance was added. Startup failures were **not** counted as
access denials. After the first exploratory run, the probe was changed to stop
host-access cases when the ordinary-code startup check fails.

Retained directory suffixes, under the same temporary parent:
`r3e3qut_`, `wywq_0g3`, `69t86bz7`, `2u7zfzvh`, `zvf1szg_`, `h4bgw3lc`.
The first two have profile/fixture files and tool-console evidence; later runs
also write `report.json`. Later corrections do not erase the earlier failed
observations.

## Native decision gate (before the approved WASI experiment)

**Do not ship this native probe as the bounded runner.** It demonstrated useful
host-access denials, but it has no validated hard memory boundary, uses deprecated
private OS policy, and does not implement the trusted operation broker. Polling
RSS or relying on a cooperative CPU signal must not silently become the claimed
hard resource boundary. No production entry point/default, package dependency,
model run, browser session, merge or release was changed.

A refreshed local inventory found only `/usr/bin/sandbox-exec`: no bwrap, Docker,
Podman, Lima/Colima, Apple container, Deno, Wasmtime or Wasmer executable on PATH;
neither Wasmtime nor Wasmer is installed in the project Python environment.

Options needing an operator decision before new environment/dependency work:

1. Investigate **CPython/WASI with a constrained Wasm engine** in a new private
   environment. Pin source/provenance/version/digest of engine and interpreter;
   test real file/network/process denial, guest memory limits, fuel/deadline
   behavior and SDK-compatible sequential composition. This is a proposal, not
   evidence of safety or Python compatibility. No system-wide install or model run.
2. Use an operator-approved **isolated Linux VM/container environment**, then test
   actual cgroup/resource/namespace/credential restrictions and IPC. Do not assume
   a container alone is a sandbox; no host socket, HOME, token or project mounts.
3. Pause while an appropriate environment or alternative enforceable mechanism is
   supplied. Do not substitute unrestricted Python or weaken the threat model.

The operator selected option 1 with an explicit private-experiment-only scope.
Wasmtime now exists in that separate temporary environment, not the project/global
environment. See [the WASI follow-up](500-wasi-feasibility.md) and
[approval/artifact manifest](500-wasi-artifacts.json). The native runner remains
unselected; the WASI results do not retroactively validate it.

#500 remains **in progress**. Direct/delegated policy parity, forged fields/raw
routes/new unrestricted connections, grant revocation, self-approval/expiry,
credential reuse, receipt integrity/redaction and browser lifecycle tests have
**not run through a real delegated entry point**. The OS probes do not satisfy
those acceptance items. #501/#502 implementation and all later gates remain
unstarted; #504's preregistered methods are unchanged.

Native-checkpoint validation: Python AST syntax (3.10 grammar), source/report hash and
observation consistency assertions, and staged `git diff --check` passed. Full
Python/Node/real-browser regression was **not rerun** at this investigation-only
checkpoint; these observations are not a replacement for the required runtime
conformance/regressions. At that checkpoint only this document and the native
probe were added. The WASI follow-up adds its own documentation/manifest/probe;
no `src/` or `extension/` file changed.
