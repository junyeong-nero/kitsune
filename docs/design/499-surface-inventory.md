# #499: existing public operation inventory

Status: inventory foundation, **not an approved final surface or a completed
migration map**. Parent: #498. This branch builds on the prerequisite test repair
in PR #505; the product revision being inventoried is `fd9b90e`.

## Reproduce without a browser

```sh
uv run --locked python scripts/toolset_inventory.py
uv run --locked python scripts/toolset_inventory.py --json
uv run --locked python -m unittest discover -s tests -p test_toolset_inventory.py -v
```

The script prints JSON to stdout and writes no files. It reads actual tool
registrations and Python methods, and parses the server/bridge source for gateway,
HTTP and container registrations without importing the bridge daemon. It does
not connect to a browser, run a route, publish a tool, or change any authority.

## Measured inventory

At the product baseline:

| Surface | Entries | Identity and coverage |
| --- | ---: | --- |
| `tools` | 109 | Every `tools.TOOLS` entry, including mode-specific `browser_tab_list` and compatibility-only unsupported tools |
| `gateway` | 3 | Search, call and publish registrations; separate from capability groups |
| `python` | 129 | `connect` and public methods declared by the Browser/Tab and namespace classes in `browser.py` |
| `http` | 140 | Each registered `GET path` and `POST path`, including aliases and capability-exempt routes |
| `container` | 88 | Caller-facing batch action aliases plus bridge-handled control flow |

These 469 entries are **not 469 distinct browser capabilities**. Multiple tools,
SDK methods, HTTP verbs and container aliases implement the same behavior. Counts
are not a tool-count target, context cost, token measurement, or workload evidence.

The six inventory tests check complete registration sets, HTTP policy-table
coverage, developer escape hatches and sensitive namespaces, container aliases,
CLI output and a registration shape change that requires manual review. The
extractor must fail visibly if the registration syntax changes beyond what it
understands; it must not silently drop a new entry to keep the catalog smaller.

## Interpretation rules for the disposition map

- `tools[*].implementation_signature` is the Python implementation signature,
  **not the published MCP JSON schema**. Core schemas currently come from
  `playwright_tools.json`; publication adapters must be measured separately.
- A tool's explicit `modes` field is only a registration condition. Actual
  `tools/list` is also intersected with `KITSUNE_CAPS`, `KITSUNE_PUBLISH` and
  `task_modes.permits_tool`. `modes: null` does not mean permitted in every mode.
- HTTP `capability_exempt` only describes `OPEN_ROUTES`. It does not mean an
  endpoint is unauthenticated or safe to expose to delegated code.
- Container `operation` resolves an alias, for example `js` → `executeJS`.
  Host-language loops must not acquire page-evaluation permission merely because
  the old container grammar evaluated conditions in page JavaScript.
- Namespace methods cover their documented bound instances: `PageStorage` means
  both local and session storage; `Network` means a tab's network namespace.
  Constructors, namespace attributes and exception classes are not separate
  operation entries. The contract must still explicitly address `Browser`/`Tab`
  construction, access to the client, handle lifetime and error semantics.
- Public `kitsune.tools` functions are included under `tools`, not counted again
  as a third Python implementation. CLI/daemon administration remains an operator
  concern; inventorying an HTTP route does not propose adding a direct tool for it.

## Decisions this exposes, not yet resolves

1. The default 27-tool surface is not the coverage baseline. Storage/history,
   rare handoff/recovery operations and mode-specific tools must have explicit
   destinations even when absent from default `tools/list`.
2. The developer API exposes `connect`, `Browser.request`, `Policy.set`,
   `Tab.evaluate` and sensitive namespaces. Delegated scripts cannot inherit that
   connection simply by hiding methods. #500 must specify and test actual
   credential, host-access and approval enforcement.
3. The current registry retains unsupported compatibility entries. A proposal
   must name their deprecation and coverage implications, rather than silently
   deleting them from a count.
4. Current read/draft modes restrict screenshots, file writes, approval/resume
   and containers in addition to page writes. A new surface must separate
   visibility from authority and preserve these limits unless an explicit,
   reviewed product change is made.
5. Direct tools and a pilot SDK must use the same operation subset if #504's C/D
   comparison is to isolate composition. SDK-only extra capabilities would make
   that a product-configuration comparison and must be declared in advance.
6. PR #496 remains an unmerged handoff dependency. #499 must describe the common
   result/approval contract; the runner must reuse its final implementation rather
   than creating a competing approval mechanism.

## Verification of this foundation

On 2026-11-10, the six focused inventory tests passed. Standard discovery
(`uv run --locked python -m unittest discover -s tests -q`) ran 1,255 tests in
156.309 seconds with 235 opt-in skips and no failures/errors. `git diff --check`
also passed. No product/extension source changed, and no new browser or model
run was needed for the read-only extractor. The separate baseline's real-browser
evidence remains in [498-baseline.md](498-baseline.md).

## Remaining #499 acceptance work

- Assign **every entry** a concrete destination and rationale, with checks for
  unclassified, duplicated and stale mapping entries. Grouping entries that share
  a rationale is acceptable; a catch-all that masks new capabilities is not.
- Specify concrete direct schemas, Python signatures and intended JS counterparts,
  including intentional gaps. Define mode/grant exposure, explicit tab targeting,
  session/ref lifetime, strict matching, deadlines, images, output bounds,
  dispatched/observed/unknown outcomes and recovery.
- Supply paired examples for read, draft, multi-tab research, repeated work,
  screenshots and user handoff. Avoid another generic dispatcher or workflow DSL.
- Measure the actual default/gateway definitions and instructions, proposed
  definitions and delivered SDK guide. Report character and tokenizer quantities
  separately, without claiming an efficiency improvement before #504.
- Record design review findings, migration constraints and implementation blockers.

No product behavior, default, registration or developer SDK changed in this
inventory foundation. This document alone does not satisfy #499 or authorize the
runner, default switch, model runs, merge or release.
