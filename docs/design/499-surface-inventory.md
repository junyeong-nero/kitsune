# #499: existing public operation inventory

Status: inventory and disposition proposal, **not an approved final surface or
completed #499 design**. Parent: #498. This branch builds on the prerequisite test
repair in PR #505; the product revision being inventoried is `fd9b90e`.

The [explicit capability map](499-capability-map.json) and
[shared contract proposal](499-flat-contract.md) accompany this inventory.

## Reproduce without a browser

```sh
uv run --locked python scripts/toolset_inventory.py
uv run --locked python scripts/toolset_inventory.py --json
uv run --locked python scripts/toolset_inventory.py --check-map
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
| `legacy_python` | 104 | Documented low-level `kitsune.tools` functions, including nine names not present in the tool registry |
| `gateway` | 3 | Search, call and publish registrations; separate from capability groups |
| `python` | 129 | `connect` and public methods declared by the Browser/Tab and namespace classes in `browser.py` |
| `http` | 140 | Each registered `GET path` and `POST path`, including aliases and capability-exempt routes |
| `container` | 88 | Caller-facing batch action aliases plus bridge-handled control flow |

These 573 entries are **not 573 distinct browser capabilities**. Multiple tools,
SDK methods, HTTP verbs and container aliases implement the same behavior. Counts
are not a tool-count target, context cost, token measurement, or workload evidence.
The first foundation commit (`dff9c9d`) counted 469 entries; source review then found
the documented low-level Python functions were not fully represented by the MCP
registry. Adding their distinct signatures corrects inventory scope, not product
behavior or a benchmark baseline.

The ten inventory/mapping tests check registration sets, HTTP policy-table
coverage, both Python surfaces, sensitive escape hatches, container aliases, CLI
output, registration-shape changes and explicit dispositions. Every entry is
assigned once across 48 groups, each with a replacement, rationale and coverage
loss. New, duplicated or stale entries fail; there is no fallback classification.
This proves name coverage, not behavioral equivalence or implemented support.
The extractor must fail visibly if registration syntax changes beyond what it
understands; it must not silently drop an entry to keep the catalog smaller.

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
- Public `kitsune.tools` functions are separately counted under `legacy_python`.
  Their signatures are not necessarily the MCP adapter signatures. In particular,
  `browser_js`, `browser_screenshot`, `browser_form_fill`, `browser_upload_file`,
  `browser_back`, `browser_double_click`, `browser_keypress`, `browser_console_logs`
  and `browser_watch_console` are not registry names. CLI/daemon administration
  remains an operator concern; listing a route does not propose a direct tool.

## Design questions exposed by the inventory

The [contract proposal](499-flat-contract.md) now records decisions and explicit
implementation gates for these questions; none is a claim of runtime enforcement.

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

Follow-up mapping validation passed all ten focused tests, all 70 Node harnesses,
and isolated full discovery (1,259 tests, 235 skips, zero failures/errors). The
[validation log](499-validation.md) retains the unsuccessful isolation attempt,
its diagnostic cause, the corrected invocation and full results. Use that isolated
invocation for future full unit runs; an ordinary environment can leave existing
unmocked activity reporting pointed at the user's default bridge.

## Remaining #499 acceptance work

- Review the completed explicit disposition map against the concrete schemas and
  examples. Name coverage is green, but exact behavioral gaps must remain visible;
  mapping an API to an operator is not proof the delegated pilot replaces it.
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
