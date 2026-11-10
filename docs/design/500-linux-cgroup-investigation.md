# #500 Linux cgroup v2 investigation — not execution evidence

Date: 2026-11-11; follows `2a7bce3`.

The user approved **Linux cgroup v2 boundary investigation** after the coordinator
identified that the macOS worker's RSS watchdog is not a kernel-enforced memory
boundary. This approval does not authorize installing a VM/container runtime,
connecting to a remote host, consuming new hosted-runner quota, or changing the
acceptance criteria. No such operation has occurred.

## Gate correction

The delegated-worker checkpoint deferred the whole-engine resource decision until
#502 and said that nothing required stopping under the conditional implementation
approval. That was too broad: the whole-engine boundary is part of **#500**.
The existing WASI probes and worker tests remain valid at their documented scope,
but the macOS watchdog is not evidence that this condition is satisfied. #500
cannot pass on that evidence alone; #501/#502 publication remains gated.

The worker stays opt-in and unregistered. This investigation does not select a
new browser backend or authorize a comparison/default transition.

## Local availability (inspection only)

Host: Darwin 25.5.0, arm64. `docker`, `podman`, `colima`, `limactl`, `multipass`,
`qemu-system-aarch64` and `virsh` were absent from PATH. Docker.app, OrbStack.app
and UTM.app were absent from `/Applications`. This is a narrow availability
check, not proof that no Linux host exists elsewhere. No credentials or SSH
configuration were read and no remote connection was attempted.

## Kernel semantics relevant to a candidate

Source: [Linux kernel cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html),
retrieved during this investigation. These are design inputs, not locally tested
capabilities or pinned runtime versions.

| Mechanism | Required interpretation |
| --- | --- |
| `memory.max` | Kernel memory-usage hard limit with reclaim/OOM behavior. The kernel explicitly permits temporary overshoot; not a mathematical instantaneous RSS ceiling. |
| `memory.swap.max=0` | Prevent anonymous memory from escaping the resident-memory budget through swap. Must verify support/readback, not assume inherited defaults. |
| `memory.oom.group=1` | Treat the workload as a group for OOM kill. Tasks with `oom_score_adj=-1000` are exempt; the launcher must not create protected workers. |
| `pids.max` | Bounds tasks, including thread IDs, not only process count. Need room for the engine's known threads and a tested finite bound. |
| `cgroup.kill` | Kills the cgroup and descendants, handles concurrent forks; must be available to the trusted coordinator/launcher. |
| `memory.events`, `memory.peak`, `cgroup.events` | Kernel evidence for limit/OOM behavior and eventual empty-group cleanup, not guest-reported success. |

**Start inside the group.** Memory stays charged to the cgroup that instantiated
it. Moving an already compiled engine into another group does not transfer its
existing charges. The chosen launcher must establish limits and place the child
inside the group **before Python/Wasmtime import, module loading or compilation**.
Use a trusted service manager or a minimal pre-exec launcher with this property;
do not move a running worker and claim complete accounting.

cgroups do not deny filesystem/network access or protect credentials. The WASI
boundary and credential-free engine environment remain necessary. The coordinator
must be outside the worker's OOM group so it can retain partial ledger evidence,
kill/reap descendants and report the termination. CPU quota bounds rate, not the
run's elapsed deadline or total CPU consumption; independent deadline/cleanup
remain required.

## Required preflight and tests before enabling anything

1. Pin the actual Linux kernel, cgroup hierarchy/delegation, launcher, host Python,
   Linux Wasmtime artifact and CPython/WASI/stdlib. The current macOS engine wheel
   is not a Linux artifact; download provenance/pins must be checked separately.
2. Require cgroup v2 and available/delegated memory and PID controllers, finite
   configured limits, zero swap, group OOM and a working tree-kill path. Read back
   settings. Refuse startup if any required mechanism is missing or unwritable;
   no unconfined/watchdog-only fallback.
3. Prove a positive control under a generous limit, then lower the limit for a
   bounded authored allocation probe and verify kernel limit/OOM evidence. Do
   not risk exhausting the machine with an unconstrained negative control.
4. Run the actual worker under the group from process start. Test compiler/startup
   allocation as well as guest memory and host callbacks/IPC. Record kernel peak
   and events; guest `MemoryError` alone is not whole-engine evidence.
5. Demonstrate group cleanup after success, startup failure, OOM, blocked I/O,
   deadline and coordinator cancellation; require no surviving tasks before group
   removal. Killing the worker is not proof that an accepted browser operation was
   cancelled; preserve the independent broker ledger/unknown outcome.
6. Verify that ordinary guest code cannot access controller files, migrate out,
   create a new unrestricted connection, recover credentials or self-approve.
   Repeat actual broker/worker authority tests; do not substitute a mock SDK.
7. For a local Linux VM, document the distinction between the Linux worker cgroup
   and the macOS VM process/virtualization overhead. A cgroup does not impose a
   macOS hypervisor-process RSS ceiling. Any VM-level resource budget is a
   separate configuration and evidence item.

No numerical candidate limit is finalized by this document. Previously observed
macOS peaks are not a Linux sizing result. No cgroup probe, Linux worker run,
model execution or positive authority verdict has occurred.

## Environment decision needed

Use an explicitly approved private Linux environment with the required delegation,
or obtain approval to provision one. An existing host, a local Linux VM and a
hosted CI runner have different access, installation and quota implications.
Those must be chosen before executing probes. The current frozen comparison does
not silently inherit a new platform or runtime pin from this investigation.
