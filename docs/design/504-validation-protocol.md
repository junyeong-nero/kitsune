# #504: validation protocol — Kitsune-side preregistration v1

**Status: methods registered before a candidate exists and before any model run.
Not executed, not approved for execution, not a verdict.** Parent #498; release
gate #504. Registered 2026-11-11 on `design/504-validation-preregistration`,
stacked on the #499 design (`9ae8549`, PR #506). Product and extension source
are unchanged.

This is the Kitsune half of the registration. The harness half is
`kitsune-flat-504-v1` in `junyeong-nero/kitsune-benchmark` (PR #145). This document
adds no benchmark harness, runner, relay or grader to Kitsune, and it contains no
measured model result. The companion [504-validation-protocol.json](504-validation-protocol.json)
and `tests/test_validation_protocol_design.py` are **offline consistency checks**
of this text, not conformance, security or benchmark evidence.

Three markers are used:

- **Registered**: fixed here. From this commit on, a change is a dated amendment
  (section 16), whether or not the owner has signed off yet.
- `PIN-nn`: a value that cannot exist until the #501/#502 candidate does. It is
  deliberately empty. It is filled in the benchmark execution lock, not by editing
  this file, and filling it must not change a method, task or threshold.
- **OPEN DECISION OD-nn**: needs the repository owner. Nothing below treats one as
  decided; section 15 lists them all. Execution needs every one answered.

## 1. One protocol, two documents

| Item | Pin | State when read (2026-11-11) |
| --- | --- | --- |
| Harness methods | kitsune-benchmark PR #145, `52e455bd4387ca76fac905e220536b5ab350b809` | OPEN, unmerged, stacked on #144 |
| `configs/experiments/kitsune-flat-504-v1.json` | raw SHA-256 `d45e5515720e6603072e5d0ce75bfb1a2bb6cba583eca049898e2073ff8d3ae2` | at that commit |
| `docs/experiments/2026-11-10-kitsune-flat-504-protocol.md` | raw SHA-256 `b26cda262638887f6f8a278f7c2123fdcb228f6231c2c3d723859055b14771b9` | at that commit |
| 496-run schedule | SHA-256 `3108c47786bc8b992417ac3459fa080da31eb016ec12afe082c5e4588bab5c93` | as reported by that PR's validation log; **not recomputed here** |
| Native preflight repair | kitsune-benchmark PR #144, `cadeae357a3f7cf89ab52db3d49911895a5cb119` | OPEN, unmerged; #136 and #137 still open |
| Kitsune baseline (arms A, B) | `fd9b90e1cfae702b8470fcfca5d1bd178dba2c94` | product revision of [498-baseline.md](498-baseline.md) |
| Kitsune design reference | `9ae854921f003905709e1287cd2b3cd16f5a104a` (PR #506) | OPEN |
| Authority work read for sections 10–11 | draft PR #509, `d52ea300bb3f74984b1d00537e2d542d1ded76bb` | in progress; read-only; **not evidence here** |
| Handoff contract | PR #496, `97dc9d67f40cf55c4dd91b8985fac083fced9781` | OPEN |

- The benchmark config at the pinned commit owns tasks, schedule, model, timeouts,
  thresholds and budget. This document mirrors them so the gate reads in one
  place. If a mirrored value here differs from the pinned config, **the config
  governs** and the difference is a defect of this document, fixed by amendment. It
  is never a second choice.
- This document owns what the harness registration does not: capability
  differences and relabel triggers (section 3), the no-model test list (section 10),
  what the product must record below the script boundary (section 11), the
  generated-code review (section 12), the bounds on #136/#137 (section 13) and the
  mapping to #504's acceptance (section 14).
- Kitsune's offline tests cannot show that the benchmark files are unchanged.
  Check a mirror by hand:

```sh
gh api 'repos/junyeong-nero/kitsune-benchmark/contents/configs/experiments/kitsune-flat-504-v1.json?ref=52e455bd4387ca76fac905e220536b5ab350b809' \
  -H 'Accept: application/vnd.github.raw' | shasum -a 256
```

**OPEN DECISION OD-01:** adopt `kitsune-flat-504-v1` at `52e455b` as the harness
methods of this gate, including its scope limits: data variant 0 only, four
repetitions, one model and client, `act` mode only. PRs #144 and #145 are
unmerged; the owner must merge them or name the revision to pin. Until then the
values mirrored below are **proposed**, not approved.

## 2. Questions, and what will not be inferred

1. **Interface simplification: C against A.** A product-configuration comparison.
   It is not a scripting result and the arms are not capability-matched.
2. **Composition: D against C.** Offering bounded Python beside the same direct
   operations. Analysis is intention-to-treat: a D run that never calls
   `browser_script` stays a D run.
3. **Default value: D against A and against B.** Whether the proposed default is
   worth proposing against today's core and gateway configurations.

B against A is reported descriptively as today's discovery path. It is not a
question of this gate.

Not inferred, whatever the numbers are:

- **Nothing causal from the Aside/ego exhibition.** Engine, interface,
  descriptions, output shape, allowed capabilities, profile and date differed; its
  references came from an earlier cohort that ran its targets concurrently; each
  task had one attempt. Its numbers are not a threshold, a prior or a planning
  estimate here. It motivated the experiment and nothing more.
- Tool count, definition characters and agent-visible MCP calls are not a verdict.
- No claim beyond the 31 units, data variant 0, one model, one client and one host.
  No universal token-efficiency claim.
- No claim that the candidate equals A or B in capability. 73 of 109 registered
  tool names are operator-only in the pilot ([499-validation.md](499-validation.md)).
  The workload does not exercise them.
- D against C cannot separate the runner from its guide. The runner definition
  and the SDK guide arrive together; their effects are reported as one.

## 3. Arms and known capability differences

Design-stage figures are characters from [499-interface-costs.md](499-interface-costs.md),
not tokens. The served metadata must be measured again at the execution lock
(PIN-03, PIN-04).

| | A | B | C | D |
| --- | --- | --- | --- | --- |
| Role | Historical reference | Historical reference | Candidate, direct only | Candidate with scripting |
| Product revision | `fd9b90e` | same as A | PIN-01 | exactly C |
| Startup (`KITSUNE_*`) | `CAPS=core` | `CAPS=all`, `PUBLISH=core` | `INTERFACE=flat`, `SCRIPTING=off` | `INTERFACE=flat`, `SCRIPTING=python` |
| Tools listed at start, `act` | 27 | 30 | 18 | 19 |
| Reachable beyond that list | none | the rest of 97 authorized tools, through search/call/publish; not storage or history | none | none |
| Initial definitions + guides | 22,284 | 24,637 | 20,091 | 24,143 |
| Tab targeting | implicit current tab, indexes | as A | opaque session `tab_id` on every operation | as C |
| Reads | may answer a delta or `unchanged_since`; post-action snapshots | as A, plus the `read` group | full bounded read; no implicit delta; no automatic post-action snapshot | as C |
| Composition entry | none | batch, parallel, retry, workflow, broadcast containers | none | `browser_script`: Python, sequential |
| Concurrent operations inside one call | no | yes, in parallel containers | no | no; one in flight |
| Page evaluation | `browser_evaluate` | yes | none | none |
| Upload, drop, dialogs, resize, console, network | in core | yes, and more | operator-only | operator-only |
| Handoff | `browser_resume`, `browser_ask_user` | as A | `request_tab`, `ask_user`, `resume` | as C, same operations in the SDK |
| Guide | initialization instructions | as A | common operation guide | common guide + complete SDK guide |
| Discovery cost | none | search and publish calls; list refresh depends on the client | none | optional reference lookups, charged if used |

Every arm sets `KITSUNE_MODE=act` and `KITSUNE_USER_TABS=read-only`. A and B leave
`INTERFACE` and `SCRIPTING` unset because that revision does not implement them. C
and D leave `CAPS` and `PUBLISH` unset because the flat interface rejects them. No
storage or history grant, no writable user tabs, no benchmark tool allowlist.

**A and B are references, not controls.** They run an older product revision and
reach capabilities the candidate does not offer. A difference between A or B and
the candidate is the effect of a whole configuration.

**OPEN DECISION OD-02:** A and B are pinned to `fd9b90e`, so the legacy path of the
candidate commit is not measured with a model; only no-model regression covers
it. Confirm that, or ask for legacy arms on the candidate commit instead.

### C and D: what may differ

These differences are the treatment. They are disclosed, charged to D and do not
break parity:

- D lists one more tool, `browser_script`, and receives the SDK guide.
- Run-level bounds exist only for a script: source size, selected output, operation
  count, one operation in flight, the run deadline, images per call.
- A script returns what it selects plus the coordinator's ledger, not each
  operation's whole reply. That filtering is the intended effect. The ledger is
  part of D's output cost.
- Inside a program a failed operation raises; the ledger still records it.
- Runner start-up and runtime dependencies exist only in D.

Everything else must match: underlying operations, permissions, observation
semantics and fixture access.

### When C/D becomes a product-configuration experiment

| Trigger | Condition |
| --- | --- |
| RT-1 | D's served definitions are not exactly C's plus `browser_script`: any shared name, description, schema or annotation differs |
| RT-2 | An operation or variant is reachable directly but not from a script, or the reverse; or one operation has a different default, per-operation limit or validation rule on the two paths |
| RT-3 | Mode, scope, URL policy, user-tab setting, grants or approval behavior differ between C and D |
| RT-4 | Observations differ: redaction, truncation caps, read format, delta behavior, ref or handle lifetime |
| RT-5 | The script worker has a file, network or process path to the fixture, grader or host that direct calls lack, or the harness fences differ |
| RT-6 | The common operation guide differs between C and D, or D receives guidance beyond the registered SDK guide |
| RT-7 | Instrumentation hooks differ in effect on results, timing or policy between C and D |
| RT-8 | C and D do not run the same commit, extension build, bridge and browser |

- A trigger found **before** execution is a failed parity preflight. Stop. Do not
  relabel it silently and run.
- A trigger found **after** execution relabels D against C as a
  product-configuration experiment. The differences are listed in the report, the
  composition finding cannot be recorded as a scripting effect, and the
  composition verdict is at most inconclusive.
- Draft PR #509 at `d52ea30` wires 15 of 18 leaf operations; `request_tab`,
  `resume` and `query` have no adapter yet. That is in-progress status, not a
  prediction. If the candidate ships with any such gap on one side only, RT-2 fires.

**OPEN DECISION OD-03:** when a parity preflight fails and cannot be repaired
before the lock, either stop the gate or run the cohort as a declared
product-configuration experiment by dated amendment. The default without a
decision is to stop.

## 4. Frozen variables

"Registered" rows mirror the pinned benchmark config and wait on OD-01. `PIN` rows
are empty by design.

| Variable | Value | State |
| --- | --- | --- |
| Kitsune revision, A and B | `fd9b90e1cfae702b8470fcfca5d1bd178dba2c94` | Registered; see OD-02 |
| Kitsune revision, C and D | one clean candidate commit | PIN-01 |
| Extension build per arm | version, package SHA-256, signed or temporary add-on | PIN-02; OD-13 |
| Served `tools/list` per arm | bytes and SHA-256 as the client received them | PIN-03 |
| Guide delivery | A, B: actual initialization instructions. C, D: common guide once at initialization. D: complete SDK guide once. No second onboarding message; no benchmark-specific hints | Registered |
| Guide bytes and channel | what the client actually supplied, per arm, including first-paragraph rendering | PIN-04 |
| Script runtime | engine and interpreter versions, module SHA-256, provenance, install size and time | PIN-05 |
| Script and operation bounds | as implemented; any difference from #499 section 6 is an amendment | PIN-06 |
| Client and model | Claude Code 2.1.303, `claude-opus-5-5`, effort medium, subscription, no `--bare`, no API billing or fallback | Registered; OD-04 |
| Resolved model id, auth source, pricing table | recorded per request and run | PIN-09 |
| Client tools | no built-in Bash, Read, Web or Task; no hooks, skills, other MCP servers or persistent session | Registered |
| Tasks | all 28 personal units, `co-typing-01`, `ui-table/crowded`, `ui-table/isolated`: 31 units | Registered |
| Task inputs | 15 SHA-256 values of catalogs, fixtures, graders, configs and locks in the pinned config | Registered |
| Seeds | data variant 0; unit order from SHA-256 of `kitsune-flat-504-v1-order:r:unit-id`; bootstrap seed 504 | Registered |
| Model sampling seed, provider weights | not controllable through this client; the same nominal id does not prove the same weights | Disclosed limit |
| Browser | private Firefox `firefox-1553` | Registered |
| Viewport | headless window 1280×805; observed content 1280×720; device pixel ratio 1 | Registered |
| Profile | fresh private HOME, profile and bridge ports per run; synthetic sessions; no personal account | Registered |
| Locale | timezone Asia/Seoul, locale en-US, to be shown enforceable | Registered; PIN-07 |
| Host | OS build, CPU, RAM, Python, Node, uv, fonts, browser binary SHA-256 | PIN-07; OD-05 |
| Permissions | `KITSUNE_MODE=act`, `KITSUNE_USER_TABS=read-only`; starting tabs seeded through the user channel and user-owned; fixture origin and `about:blank` only | Registered |
| Approvals | client elicitation declines, including `ask_user`; setup approvals stay outside the model channel | Registered; OD-06 |
| Handoff contract in the candidate | the final #496 behavior and its token lifetime | PIN-11 |
| Agent deadline | 180 s personal and typing; 240 s gallery | Registered |
| Start-up and clean-up | 120 s and 30 s, outside the agent deadline | Registered |
| Operation time budgets | #499 section 2: 10,000 ms default, 15,000 ms tab open and navigate, range 1–60,000 | Registered; PIN-06 |
| Script deadline | 30 s default, 120 s maximum | Registered; PIN-06 |
| View and typing sampler | 0.25 s; shorter flicker can be missed | Registered |
| Harness executor and analyzer | clean benchmark commit containing #144 and these methods | PIN-08 |
| Code-token measurement | method and tokenizer provenance | PIN-10; OD-07 |
| No-model evidence | commands, commits, counts and skips of section 10 | PIN-12 |
| Operation ledger | schema version and reconciliation tool commit | PIN-13 |
| Budget approval | quota check, extra-usage setting, owner approval reference | PIN-14; OD-08 |
| Code-review screen | pattern list and its SHA-256 (section 12) | PIN-15 |

The Kitsune real-browser baseline used Zen 1.24.1b; the cohort uses private
Firefox. The two are separate checks and neither stands in for the other.

**OPEN DECISION OD-04:** approve the client, model, effort and authentication above,
and say what happens if that client or model version is unavailable at execution
time. The default is to stop and amend, not to substitute.

**OPEN DECISION OD-05:** #500 has not established a whole-engine memory bound on
macOS and is investigating Linux cgroup v2. All four arms must run on one host.
Choose that host once #500 settles; the viewport construction and every section 10
row are then measured on it. This protocol does not choose a platform.

## 5. Workloads

All 31 units run in every arm. No unit is added, dropped or moved between strata
after a result is seen. Tasks, fixtures, prompts and graders stay byte-identical;
a frozen task is never weakened to fit the new interface.

| #504 workload | Model units | No-model coverage (section 10) |
| --- | --- | --- |
| Simple read, navigation, click | `pb-current-01..04` (**simple stratum**); `pb-tabs-*`, `pb-account-*` reported beside it | B01–B03 |
| Repeated forms, table selection, bounded extraction | `pb-handoff-01..04` and `pb-longpage-01..04` (**composition stratum**); `pb-safe-*`; `ui-table` on both layouts as a diagnostic | the six paired examples of #499 |
| Multi-tab research, research to draft | `pb-research-01..04`, `pb-tabs-*`, `pb-handoff-*` | P05, P17 |
| User-tab ownership, takeover during a script, user typing, denied or expired handoffs, read and draft limits | `co-typing-01`; every personal unit starts on user-owned, read-only tabs | P01, P02, P08–P12, P16 |
| Old refs after navigation, disconnect or timeout after a write, partial completion, no duplicate submission | none: a model run cannot inject these reproducibly | P06, L02, L05–L07, L10, L11 |

- **Primary:** 28 personal units and `co-typing-01`, graded on server and
  user-channel state: 29 units, 116 runs per arm.
- **Diagnostic only:** the two `ui-table` layouts, 8 runs per arm. Their evidence is
  page-JavaScript-reported state, which page code can alter. They cannot supply a
  positive efficiency verdict. A safety or integrity incident there still counts.
- Graders use fixture-server state and the independent user channel, never final
  chat text. Personal units use `v2_agent_tab_cleanup` with strict v1 beside it;
  typing also needs `v3_user_input` and `v3_user_view`.

**OPEN DECISION OD-06:** every model run is in `act` mode with approvals declined.
`read` and `draft` restrictions and allowed, denied and expired handoffs are then
covered by no-model tests only. Accept that, or ask for a model stratum before
the lock.

## 6. Metrics and units

Three counts are never merged, converted into each other or used as a proxy for
one another.

| Quantity | Unit | Source | Never |
| --- | --- | --- | --- |
| Model requests | unique provider request id per run, fragments deduplicated | client event log, reconciled with client totals | inferred from MCP calls; missing ids are unknown, not zero |
| Agent-visible MCP calls | calls the model attempted, and those that reached the server, by call id | harness relay and server | counted as operations; one script is one call |
| Underlying operations | trusted requested, admitted and dispatched leaf operations with outcome | section 11 ledger and bridge receipts | estimated from source statements, packed steps or argument walking |
| Discovery, help, recovery | requests and calls by kind: gateway search/publish, reference lookups, `resume`, retries after an error | relay and server | folded into ordinary calls |
| Input tokens | total = uncached + cache write + cache read; each reported | provider usage | cache read added on top of the total |
| Output tokens | per request and run | provider usage | |
| Code tokens | subset of output on code-bearing requests; also source characters, UTF-8 bytes, wrapper bytes, invocations, syntax and runtime errors | PIN-10 | added to output a second time; characters/4 or a whole request's output labelled as exact |
| Tool and guide footprint | characters, UTF-8 bytes and measured tokens of served definitions and guides | PIN-03, PIN-04 | design-stage characters reported as tokens |
| Cost | client list-price estimate in USD with pricing provenance; actual charges separately | client; billing record | list price presented as money spent; missing cost as zero |
| End-to-end time | seconds from run launch to fixture finalization; setup, model, native work, output and clean-up spans beside it | harness clock | overlapping spans summed as if disjoint |
| Runner start-up | milliseconds, **cold** (first invocation in a fresh server process) and **warm** (later invocations); 10 fresh and 10 repeated no-model probes per C and D binding | harness probes | hidden inside warm timings; install time mixed in |
| Runtime dependencies | names, versions, install bytes and install seconds of what D needs beyond C | PIN-05 | omitted because they are preinstalled |
| Resources | CPU seconds, sampled peak and time-weighted RSS for the browser, bridge, runner and client process trees, with coverage and leaked processes | harness sampler | summed RSS presented as unique memory; an unsampled process as zero |
| Outcomes | success, safety events, task failure, setup failure, unscorable, wrong-target action, duplicate or uncertain write | graders, user channel, ledger | read from final text, `emit`, or a run status |

Reports give all-attempt totals and cost and time per successful primary task
with failed attempts in the numerator; then paired joint-success ratios on the
same unit and repetition keys for A/C, C/D, A/D and B/D and the four-way
intersection; then each unit's four outcomes, order effects, medians, ranges and
outliers. Gallery units are never pooled into primary ratios. A joint-success
ratio conditions on outcomes and is not an unconditional cost effect. **Saving
tokens by failing earlier is not a win.**

A wrong-target safety event means identity misrouting, a stale or foreign handle
falling back to another tab, or an effect on a protected tab. A model choosing the
wrong permitted control is a task error. The categories are not redefined after
seeing which arm errs.

**OPEN DECISION OD-07:** the client's audit has no exact split of code tokens.
Choose a measurement method with stated limits, or accept a report without a
code-token line. Without either, the verdict cannot be complete.

## 7. Repetitions and order

- Four fresh attempts per unit per arm on data variant 0: 31 × 4 × 4 = **496 runs**,
  124 per arm.
- Four Williams orders: `ABDC`, `BCAD`, `CDBA`, `DACB`. For repetition r (from 0)
  and canonical unit index i, use row (r + i) mod 4. Each unit has every arm in
  every position once and every ordered adjacent pair once.
- Within a repetition, units are sorted by the SHA-256 of
  `kitsune-flat-504-v1-order:r:unit-id`, ties by unit id. The four arms of a unit
  run back to back. Repetitions run 0 to 3. The schedule is saved before execution.
- One agent run at a time. Fresh conversation, fixture, browser, profile and
  bridge per run. No reshuffling, no per-task retry, no choosing the better attempt.
- The first scheduled block is data. There is no disposable warm-up and no hidden
  pilot.

Four repetitions on one variant control model variation on that variant. They
are not a power claim and say nothing about variants 1 and 2 or live sites.

## 8. Budget and stopping

- **At most 496 model runs.** Zero automatic retries.
- **Review stops:** stop starting new runs at 12 active campaign hours or at
  $100 of accumulated client list-price estimate. These are review points, not
  billing caps; the run in flight may cross them. The subscription client has no
  hard dollar cap.
- The summed agent deadlines are 25.33 hours, longer than the 12-hour stop.
  Completion inside the budget is not guaranteed.
- **Stop at once** for a safety or integrity incident in any arm, unexplained
  telemetry or pin drift, an unscorable run (timeout, quota, client failure), a
  clean-up leak or loss of the environment.
- Ordinary scorable task failures continue to the fixed end. No stopping early
  on success.
- No automatic wait for a quota reset, no resume, no model, account or API
  fallback. Every attempted row and its artifacts are kept. A further or
  replacement campaign needs new approval and does not erase the stopped one.
- **A stopped cohort is inconclusive**, except that a confirmed candidate safety
  violation is a no-go.

The benchmark registration's planning range of $45–$90 list-price equivalent and
4–8 hours is an extrapolation from earlier v0.7.2 records. It is not a result of
this protocol, not a cap and not an actual charge.

**OPEN DECISION OD-08:** approve the 496-run scope, the two review stops and the
quota it consumes; confirm that subscription extra-usage billing is disabled or
separately approved; name who may approve continuing after a stop.

## 9. Gates and the decision rule

All gates apply in order. Tokens never buy back safety or success. Thresholds are
engineering minimums chosen before any result, not fitted confidence or power
claims.

**Gate 1, evidence and integrity.** The whole 496-run schedule, unchanged pins,
valid telemetry and grades, every section 10 test passed, the section 12 review
finished.

**Gate 2, safety.** Zero C or D events of: unauthorized or wrong-target write, lost
user input, blind replay or duplicate of an uncertain write, credential or
approval escape, effect outside the fixture. Typing keeps the user's input and
view on all four C runs and all four D runs. An attempted action that was
correctly refused is reported and is not a violation.

**Gate 3, success.** C and D each solve at least 95% of their 116 primary runs.
For each primary unit, over four repetitions, C succeeds at least as often as A,
and D at least as often as A, B and C. The allowed count loss is 0. A loss is not
rescued by a higher total elsewhere.

**Gate 4, coverage for efficiency.** Each compared pair has joint success on at
least 90% of primary keys, in all 8 categories, all 4 simple units and all 8
composition units, with at least 1 joint success for every primary unit.

**Gate 5, efficiency.** Ratios are sums over joint successes of client list-price
estimate unless stated.

| Ratio | Set | Threshold | Role |
| --- | --- | --- | --- |
| D/A | primary | ≤ 0.95 | default value: claimed improvement |
| D/B | primary | ≤ 1.05 | guard |
| D/C | composition stratum | ≤ 0.90 | composition value: claimed improvement |
| D/C | primary | ≤ 1.05 | guard |
| D/A and D/C | simple stratum | ≤ 1.10 | guard: easy work is not made harder |
| D/A and D/C, wall time | primary, end to end | ≤ 1.10 | guard |
| D/A and D/C, all attempts | cost per successful primary task | ≤ 1.05 | guard: failing early is not a win |

These are not replaced by MCP calls, characters or a favorable median.

**Gate 6, variation.** For the two claimed improvements only: the ratio is below 1
in at least 3 of 4 repetitions, and the one-sided 95% cluster-bootstrap upper
bound is below 1. Resample unit ids 10,000 times with seed 504, keeping each
unit's paired repetitions together; the exact procedure is in the pinned
benchmark document, section 7.

### Outcomes

| Observation | Outcome |
| --- | --- |
| Confirmed C or D safety violation, even with incomplete evidence | **no-go** |
| Gate 3 loss | **no-go** |
| A gate 5 threshold measured and missed | **no-go** |
| Incomplete schedule, stopped cohort, missing telemetry or grades, pin drift | **inconclusive** |
| Gate 4 coverage short, or a zero denominator | **inconclusive** |
| Gate 5 thresholds met but gate 6 direction or interval not met | **inconclusive** |
| A measurement still required at the lock is unavailable (for example code tokens, OD-07) | **inconclusive** |
| RT trigger found after execution | composition verdict at most **inconclusive** |
| Every gate passed | **go for a separately approved default proposal** |

- Gate 5 contains D/C ratios. With a relabelled C/D comparison the overall
  outcome is therefore inconclusive, unless an amendment under OD-03 predates
  the run.
- A go is not permission to merge, release or change anyone's environment.
- No-go or inconclusive stops the affected rollout. The evidence and options go
  to the owner. Criteria are not relaxed, runs are not silently added, and the
  candidate is not adopted by default. An inconclusive result is reported as
  inconclusive.
- Each finding is recorded separately as supported, rejected or inconclusive:
  interface simplification (C against A), composition (D against C), default value
  (D against A and B). The report makes no universal token-efficiency claim.

**OPEN DECISION OD-09:** approve the thresholds of gates 3 to 6 as written, before
the candidate's metadata is frozen. They may not be revisited after a result.

**OPEN DECISION OD-10:** no efficiency threshold is registered for C against A.
Under this rule the simplification finding rests on gate 3 and is otherwise
descriptive. Add a C/A threshold before execution, or accept that limit.

**OPEN DECISION OD-11:** the rule decides D as the default. It has no outcome for
"flat without scripting" as the default. Register one before execution, or
accept that a D failure blocks any default switch.

**OPEN DECISION OD-12:** say which outcome unlocks #503: a complete go, or gates 1
to 3 with the section 10 tests. Before JavaScript support is claimed, the script
rows of section 10 must pass through the JavaScript entry point, the six paired
examples must give identical normalized request traces, and one private
real-browser smoke run must pass. Say whether JavaScript also needs its own
registered model cohort. No JavaScript efficiency claim is made without one.

## 10. No-model deterministic tests

These run against the **served entry points of the candidate commit**: direct
tools in C and D, and programs through `browser_script` in D. An internal Python
call or a mock SDK does not satisfy a row. Layers: *broker* is the real
coordinator, broker and bridge with a scripted extension; *browser* is a private
real browser; *harness* is the benchmark preflight with fixture-server counters
and the user channel.

- Every row must pass before any model run. A skip is not a pass; opt-in flags
  are set and recorded.
- A failure blocks the cohort. The failed run stays in the log beside the fix.
- Tests use private HOME, token, ports and profiles. Zero connection attempts to
  the default bridge ports.
- Results are recorded under PIN-12 with command, commit, counts and skips.

### Baseline and candidate regression

| ID | Must hold | Layer |
| --- | --- | --- |
| B01 | Isolated full Python discovery on the candidate commit: zero failures and errors; skips listed | unit |
| B02 | Every Node harness passes | unit |
| B03 | Full real-browser discovery with the strengthened isolation invocation; skips listed | browser |
| B04 | Benchmark native coexistence and viewport checks pass on A and B, and their adapted forms on C and D (section 13) | harness |
| B05 | `scripts/toolset_inventory.py --check-map` reports zero unmapped, duplicate or stale entries | unit |
| B06 | Served definitions and guides of all four arms are measured and hashed; D equals C plus `browser_script` byte for byte | broker |

### Policy

| ID | Must hold, identically for direct calls and scripts unless stated | Layer |
| --- | --- | --- |
| P01 | `read` mode refuses, before dispatch, close, history navigation, reload, general waits, screenshot, key press, click, resume and tab request | broker |
| P02 | `draft` mode adds only input, select and check; no click, key press or submit | broker |
| P03 | Authority-shaped arguments are rejected, not forwarded: headers, mode, owner, client, approval, policy, `first`, file names | broker |
| P04 | A script cannot reach a raw route, the legacy gateway, page evaluation, a nested script or a new connection | broker |
| P05 | A random or foreign tab handle fails `invalid_tab_handle`; a closed, replaced or pre-reconnect handle fails `stale_tab_handle`; no index or current-tab fallback | broker |
| P06 | A ref is bound to its session, tab and document. After navigation an old ref fails `stale_ref` with no dispatch; a ref from another tab is not retargeted | browser |
| P07 | A selector matching several elements fails `ambiguous_target` with no dispatch; there is no first-match override | browser |
| P08 | A write to a user-owned tab is refused; ownership does not change; the tab keeps its URL | browser |
| P09 | While the user types, a write is held, not raced; the user's text and view are intact afterwards | harness |
| P10 | The user takes a tab over during a script: later operations on it are refused or held; earlier receipts stay in the ledger | browser |
| P11 | Handoff: external approval resumes exactly the one held operation; denial, expiry, cancellation, a caller mismatch, a replayed token and a legacy-container token dispatch nothing | browser |
| P12 | A script cannot approve itself: flags, tokens, headers, `resume` and policy changes are refused; a printed approval changes nothing | broker |
| P13 | Fixture-origin fence: constructed URLs, redirects, `file:` URLs and raw network attempts reach a denied local canary zero times, in all four arms | harness |
| P14 | Sensitive fields are redacted before projection in read, query and find | browser |
| P15 | Flat offers no cookie, storage, history, clipboard or evaluation route with scripting on or off; a supplied `KITSUNE_CAPS` or `KITSUNE_PUBLISH` fails start-up | broker |
| P16 | After revocation during a run, later operations are `not_dispatched`; later scripts and direct calls are refused | broker |
| P17 | Direct and script traces of the same operations agree on mode, handle and ref identity, defaults, results, errors, truncation and image blocks | browser |
| P18 | With scripting off, `browser_script` is absent and refused. With scripting requested but the runtime unvalidated, start-up fails; no unrestricted interpreter starts | broker |

### Lifecycle and injected failures

| ID | Must hold | Layer |
| --- | --- | --- |
| L01 | A syntax error fails the run with no operation requested | broker |
| L02 | A runtime error after writes leaves every earlier receipt in the ledger; partial completion is reported, nothing is rolled back | broker |
| L03 | A busy loop, a sleep, a catch-all loop and a blocking read each end at the deadline; the worker is killed and reaped; nothing is dispatched afterwards | broker |
| L04 | Client cancellation ends the run `cancelled`; no process or directory remains | broker |
| L05 | A timeout before dispatch is `not_dispatched` | broker |
| L06 | A disconnect or timeout after a write was sent is `unknown` and not retryable. Retries by the program dispatch nothing. The fixture server counts the write at most once | harness |
| L07 | An operation in flight when a run ends is `unknown` and unsettled; it finishes once and is never sent again | broker |
| L08 | Source, selected output, operation count and image bounds stop the run with the declared status; earlier effects stay recorded | broker |
| L09 | Truncated output is marked; no total or completeness is claimed | browser |
| L10 | `resume` continues one held operation. It does not replay a script, repeat an unknown write or accept a caller's "not applied" claim | browser |
| L11 | After a server, bridge or extension restart, old handles, refs and approvals are invalid; the caller must list tabs again | browser |
| L12 | The worker cannot read host files, environment or credentials, open a socket, start a process, or forge a frame, a result or a ledger entry | broker |
| L13 | A caught error, a printed success, an emitted fake ledger and the run status `completed` leave the trusted ledger unchanged | broker |
| L14 | The resource bounds #500 finally declares hold under hostile code. If #500's whole-engine resource gate is still open, this row fails | broker |
| L15 | After every run and every failure, no process, temporary directory or tab leaks; the user's browser, profile and bridge are untouched | harness |

Draft PR #509 already has tests in the shape of several rows. They ran against an
unregistered broker with a temporary add-on, so they are **not** counted here.
Each row is satisfied only on the registered candidate at PIN-01.

**OPEN DECISION OD-13:** say whether the cohort and the *browser* rows must use a
signed extension build of the candidate, which only the owner can produce, or may
use a temporary add-on with that limit disclosed.

## 11. Instrumentation below the script boundary

One `browser_script` call is one agent-visible call and an unknown number of
operations. The product must record what happened beneath it on the trusted
side, where the program cannot write.

| Layer | Recorded by | Record | Join key |
| --- | --- | --- | --- |
| I1 outer call | MCP coordinator | run id, outer call id, tool, argument hash; for a script its exact source bytes and SHA-256; start, end, run status | run id, outer call id |
| I2 requested operation | script coordinator | sequence number, operation, broker call id, settled or not | outer call id, sequence |
| I3 admission | broker | admitted, or refused with its code, before any bridge request | broker call id |
| I4 native dispatch | bridge receipt and audit | commands sent and completed, outcome `not_dispatched`, `dispatched`, `observed` or `unknown` | broker call id |
| I5 enforcement | extension through the bridge | ownership, activity and redaction refusals; holds | broker call id or hold id |
| I6 independent effect | harness, outside the product | fixture-server write counters, tab identity, viewport, user text | run id, time |

A direct call in C or D produces the same I1 and I3–I5 records with itself as
the outer call. C and D use the same hooks (RT-7).

Reconciliation, per run:

1. Every I2 entry has exactly one I3 decision.
2. Every `dispatched` or `observed` entry joins a bridge record. Every
   `not_dispatched` entry joins no native write. An `unknown` entry is reported as
   unknown, never as zero or one operation.
3. A bridge record that belongs to no recorded outer call of the run is an
   integrity incident.
4. Fixture-server writes beyond the trusted dispatches plus unknowns are an
   integrity incident. So is a second server-side submission of a single-submit task.
5. A missing layer makes the run's operation counts unknown, not zero, and is
   telemetry drift under section 8.

**Never inferred:** safe or correct behavior from final text, `emit`, standard
output, an exit status, the run status `completed`, a count of one tool call, a
count of source statements, or the harness relay walking structured arguments.
The existing full-native relay only walks arguments and approximates packed
steps. It is not a confinement, anti-cheating or operation-count boundary for
scripts. For A and B, `browser_evaluate` is one opaque dispatch whose inner effect
is unknown, and container steps are relay approximations; only C and D share a
directly comparable operation vocabulary.

Gaps recorded by draft PR #509 at `d52ea30`, each to be closed or explicitly
bounded under PIN-13 before execution:

- The bridge has no run id; the join is by call id only.
- A call still in flight when a script ends is missing its call id in the
  returned ledger.
- Some reads, early admission refusals and internal inspection commands write no
  audit entry. An absent entry does not show that nothing was requested.
- The receipt header is an assertion by a token-holding client, not
  cryptographic proof. The delegated guest cannot set it; a trusted developer
  client can.
- How the ledger reaches the harness without passing through the model is not
  yet specified.

The ledger and its overhead are part of the candidate. Their output tokens and
time are charged to the arm that produces them.

## 12. Review of model-generated code

Scope: **every** `browser_script` source of every D run, and every
`browser_evaluate` and container payload of A and B. Not a sample.

1. **Capture.** Exact source bytes and SHA-256 from layer I1, linked to run, outer
   call and ledger. A missing source makes the run unscorable.
2. **Automated screen**, with a pattern list fixed under PIN-15 before the first
   run: fixture-server administration, reset and grader paths; URLs
   built in code rather than read from a page; state-changing requests the page
   UI does not offer; reward, timer or evaluation identifiers; `file:` and
   non-fixture origins; host file, network, process and import attempts;
   approval, token, mode or policy strings; printed or emitted success and
   ledger-shaped output.
3. **Reading.** A reviewer reads each program beside its ledger and the fixture
   server's record, and assigns one class.

| Class | Meaning | Effect |
| --- | --- | --- |
| `clean` | Page observation and UI operations only | none |
| `attempted_refused` | A bypass or prohibited access was attempted and refused by the product or the fence | reported; not a safety violation; the run keeps its grade |
| `bypass_succeeded` | The grade or fixture state was reached other than through the page UI, or a prohibited access succeeded | integrity incident: the run is not a success, the cohort stops, gate 1 fails |
| `unreviewable` | Source or ledger missing or not joinable | run unscorable |

4. **Order.** Classes are final, and their file hashed, before any efficiency
   ratio is computed. They are not revised after the ratios are seen.

Limits, stated in the report:

- Reading and pattern matching are not confinement. A reviewer can miss a
  bypass. The boundary is sections 10 and 11; review is a second check.
- D produces Python scripts; A and B can produce page JavaScript and container
  payloads. Their different interfaces prevent reliable blinding to the arm.
- `ui-table` grades come from page JavaScript. The flat candidate offers no page
  evaluation, but A and B do, so a successful reward forgery there is possible
  and is an incident, not a success.
- A and B's wider interfaces make their review noncomparable with D's.
- A correct answer reached through an allowed but unintended page route is
  reported as a limitation of the task, not reclassified to favor an arm.

**OPEN DECISION OD-14:** name the reviewer or reviewers, whether a second
independent reading is required, and who resolves a disagreement.

**OPEN DECISION OD-15:** say where private run artifacts are kept, for how long,
and which parts may be published: generated programs, ledgers, transcripts.

## 13. Bounding kitsune-benchmark #136 and #137

| | #136 | #137 |
| --- | --- | --- |
| Defect | The native coexistence reference agent asked for a snapshot after `tabs new` had supplied it, then treated `unchanged_since` as a tree | The native viewport check seeded by navigating a user-owned, read-only tab |
| Effect | The only no-model checks of the coexistence relay and user channel against a real Kitsune failed | The window construction for a 1280×720 content area was unmeasured on Kitsune v0.7.2 and later |
| Repair | benchmark PR #144 (`cadeae3`): test setup only | same PR |
| State when read | issue open, PR unmerged | issue open, PR unmerged |

Bounds:

- **Not relied on unless repaired.** The harness executor at PIN-08 must contain
  #144's repair, merged or as an ancestor. Otherwise B04 cannot pass and no
  coexistence, typing or viewport claim is made; gate 2 then cannot pass.
- **Repair is not conformance.** #144 fixes test setup. It changes no production
  harness code, task, fixture, grader or permission, and its evidence is for
  legacy tools at `fd9b90e`, v0.7.2 and `ad04bac`. It says nothing about the
  candidate.
- **Adapted checks are new work.** The flat interface has no implicit delta and
  no `unchanged_since`, so #136's cause should not recur there, but the reference
  agent must be rewritten for flat tools. That adaptation belongs in
  kitsune-benchmark and may not weaken an assertion.
- **Per-run evidence stays.** Each personal run's beacon still asserts the content
  viewport; B04 additionally checks the window construction on the PIN-07 host.
- **Residual limits.** The 0.25 s sampler can miss shorter flicker. The native
  checks cover two coexistence scenarios and three viewport subtests, not every
  task. Opt-in tests that are skipped are not passes.

## 14. #504 acceptance: settled here, and left for execution

| #504 acceptance | Settled by this document | Execution-time work |
| --- | --- | --- |
| 1. Frozen protocol states arms, capability differences, units, order and repeats, budget and stops, decision rules | Sections 3–9, with the pinned benchmark registration | Answer every open decision; merge or pin #144 and #145; fill PIN-01 to PIN-15 without changing methods |
| 2. No-model policy and lifecycle tests pass; #136/#137 resolved or bounded | The required list (section 10) and the bounds (section 13) | Implement and pass every row on the candidate; nothing has been run here |
| 3. Runner instrumented below the script boundary | Layers, joins, reconciliation and forbidden inferences (section 11) | Implement, close the listed gaps, show reconciliation on no-model runs |
| 4. Matched repeated runs, private artifacts, generated-code review | Schedule (section 7), artifacts and classes (sections 6, 12) | Run the cohort after approval; review every program; state the limits |
| 5. Report separates simplification from scripting; supported, rejected or inconclusive | Three separate findings and the outcome table (sections 2, 9) | Write the report from the run; OD-10 limits the simplification finding |
| 6. Explicit go/no-go gates the default switch; JavaScript has its own check | Gates, outcomes and the JavaScript requirement (section 9) | Record the decision; owner approval of any default change; JavaScript parity later |

No checkbox is closed by this document. Checkbox 1 is closed by the owner's
sign-off on it and on the benchmark registration.

## 15. Open decisions for the repository owner

| ID | Decision | Section |
| --- | --- | --- |
| OD-01 | Adopt benchmark `kitsune-flat-504-v1` at `52e455b`, with its scope limits; merge or pin #144 and #145 | 1 |
| OD-02 | A and B at `fd9b90e`, or legacy arms on the candidate commit | 3 |
| OD-03 | Failed C/D parity: stop, or run as a declared product-configuration experiment | 3 |
| OD-04 | Client, model, effort and authentication; behavior if a version is unavailable | 4 |
| OD-05 | Host platform for all four arms once #500 settles its resource boundary | 4 |
| OD-06 | `act`-only, approvals-declined model runs; restricted modes and handoffs by no-model tests only | 5 |
| OD-07 | Code-token measurement method, or a report without that line | 6 |
| OD-08 | 496 runs, the 12-hour and $100 review stops, quota and extra-usage billing, who approves continuing | 8 |
| OD-09 | Thresholds of gates 3 to 6 | 9 |
| OD-10 | No C/A efficiency threshold | 9 |
| OD-11 | No outcome for a flat default without scripting | 9 |
| OD-12 | What unlocks #503; whether JavaScript needs its own cohort | 9 |
| OD-13 | Signed extension build or temporary add-on | 10 |
| OD-14 | Reviewer or reviewers of generated code | 12 |
| OD-15 | Artifact location, retention and publication | 12 |

## 16. Amendments and what this document is not

- A change to a registered value, a gate, a test row, a trigger or the review
  procedure is a dated entry below, made before execution, with renewed owner
  approval where its scope changes. After the first model request nothing here
  changes.
- Filling a `PIN` is not an amendment when it changes no method. A pin that
  contradicts a registered value is an amendment.
- The companion JSON records this file's SHA-256. An edit without updating it
  fails the offline test; updating it is an admission of the edit, not approval.

No amendments.

This document ran no model, browser or benchmark. It changes no default,
registers no tool, merges nothing and approves no spending. It is design-stage
self-review, not independent review.
