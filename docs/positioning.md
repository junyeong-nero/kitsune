# Positioning: Kitsune among browser MCPs

This is a qualitative architecture and product-positioning comparison, not a
benchmark ranking. It distinguishes implemented design choices from hypotheses
that still need measurement. Upstream capabilities change; pin a release/commit
and inspect its actual tool surface before making a quantitative comparison.

## Kitsune's intended position

> Make an already-running Firefox-family browser an agent's ongoing workspace:
> expose the state it needs concisely, and carry work across tabs and repeated tasks.

The concrete combination is Firefox/Zen/LibreWolf support, an extension/local
bridge architecture, compact observations and action feedback, explicit multi-tab
primitives, and reusable workflows. No single item is exclusive to Kitsune.

In particular, **existing-browser access, login reuse, snapshots, element refs,
JavaScript execution, and multi-tab work are not unique to Kitsune**. The product
question is whether its combination makes a particular workload easier and more
reliable, not whether competitors lack browser automation primitives.

## Comparison map

| Project | Primary control model / emphasis | Relevant difference from Kitsune |
| --- | --- | --- |
| [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp) | Playwright browser automation with structured accessibility snapshots; persistent, isolated, and supported existing-browser connection modes | Strong overlap in the observation/action loop, broader browser automation ecosystem; Kitsune focuses on the existing Firefox-family workspace and extension-specific tab behavior |
| [ExecuteAutomation Playwright MCP](https://github.com/executeautomation/mcp-playwright) | Playwright browser/page automation, HTTP checks, test generation | More testing-oriented; Kitsune emphasizes compact state, existing tabs, batch/parallel sequences, and named workflows |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Chrome/CDP automation, browser inspection, debugging, and performance tooling | Stronger developer-diagnostics emphasis; Kitsune targets Firefox-family task execution and ongoing tab workflows |
| [Browser MCP](https://github.com/BrowserMCP/mcp) | MCP server plus Chrome extension, adapted from Playwright MCP to control an existing browser | Very close in existing-browser positioning; the browser family and actual observation/workflow design matter more than "uses an extension" |
| [Chrome MCP Server](https://github.com/hangwin/mcp-chrome) | Chrome extension/native browser APIs, interaction, extraction, and tab search | Close extension-based competitor with overlapping research workflows; Kitsune's Firefox/Zen integration is the clearer distinction |
| [Browser Harness MCP](https://github.com/browser-use/browser-harness/blob/main/docs/MCP.md) | Local stdio MCP exposing helpers through Chrome CDP | A fixed external agent can drive a real browser here too; Kitsune uses a Firefox-family extension/bridge rather than Chrome CDP |
| [Selenium MCP](https://github.com/angiejones/mcp-selenium) | Selenium WebDriver automation across several browsers | Useful different control architecture, including Firefox; Kitsune avoids a WebDriver requirement and emphasizes existing-browser workflow integration |
| [BrowserOS](https://github.com/browseros-ai/BrowserOS) | Chromium-based browser product with MCP access for external agents | Browser-integrated product rather than an extension installed into a supported existing browser; its build and bundled services are extra variables |
| [Steel MCP](https://github.com/steel-dev/steel-mcp-server) | Browser sessions via Steel Cloud or a self-hosted steel-browser service | Browser/session infrastructure rather than the user's existing Firefox workspace; local and cloud deployment must be treated separately |

This is not an exhaustive feature matrix. A missing feature in this table is not
a claim that the project cannot perform it, particularly through a code tool.

## Architecture and interaction trade-offs

The following pinned ExecuteAutomation case study makes the control-path and
interface trade-offs concrete. It is not a feature ranking of all browser MCPs.
See the broader comparison map above for other architectures.

| Dimension | ExecuteAutomation Playwright MCP | Kitsune |
| --- | --- | --- |
| Control path | MCP server → Playwright → browser | MCP server → local bridge → WebSocket → browser extension |
| Browser lifecycle | The inspected implementation launches a browser and creates a context/page | Connects to an already-running supported Firefox-family browser |
| Browser coverage | Chromium, Firefox, and WebKit | Firefox-family browsers, including Firefox, Zen, and LibreWolf |
| Starting state | Automation-created browser context | Existing browser tabs and session state available to the extension |
| Page observation | Visible text, visible HTML, screenshots | Compact DOM-derived accessibility-style snapshots with element refs, text/DOM views, screenshots |
| Interaction targeting | Registered click/fill tools primarily use selectors | Refs, selectors, and coordinates where supported |
| Observation after action | Separate observation tools are available | Supported actions can return an updated snapshot with their result |
| Multi-step work | Page interaction, API testing, and test-generation tools | Explicit batch, parallel tab sequences, and named workflows |
| Notable specialization | HTTP requests/response assertions and generated Playwright tests | Tab-oriented work, keep-alive/recovery, and Zen tab-unloader handling |

Availability depends on the selected version and enabled tool groups. This is a
comparison of the inspected implementations, not of every possible Playwright
configuration. Playwright itself supports multiple contexts/pages and other
connection modes; launching a clean browser is not an inherent limitation of
the underlying library.


## Workload and workflow design

### 1. An ongoing Firefox/Zen workspace

A natural ExecuteAutomation task is:

> Open a site, interact with it, validate its behavior, and generate a test.

A natural Kitsune task is:

> Continue the research in these open tabs and use the authorized services in
> this browser session to finish the next part of the work.

Kitsune treats tabs and session state as ongoing working context, rather than
only as a disposable test environment. Its concrete niche is the Firefox-family
user who wants an external coding agent to work in that environment.

Session access is also a security liability. Use a dedicated profile or narrowly
scoped accounts, and obtain confirmation before consequential actions. Reusing
browser state does not justify granting an agent every personal login.
See the [security guidance](guide.md#security).

### 2. A deliberate observation–action–observation loop

Kitsune implements:

- Compact accessibility-style text snapshots with generated refs.
- Subtree observation through a selector or existing ref, plus depth control.
- Repeated-item compaction.
- Updated snapshots returned by supported actions.

These choices aim to reduce the need to inspect large HTML dumps, infer selectors,
and make another round trip just to observe an action's result. Refs also have a
lifecycle: a new snapshot rebuild invalidates earlier refs in that tab, so agents
must use the fresh observation rather than assume handles are permanent.

The testable hypothesis is **less observation/target-selection overhead for a
successful task**, not simply fewer tool calls. Larger action results or tool
schemas can offset savings. Snapshot/ref interfaces also exist elsewhere,
including Microsoft Playwright MCP; they are not unique to Kitsune as a category.

### 3. Explicit multi-tab and repeatable work

Kitsune exposes, in the appropriate tool groups:

- `browser_batch`: several commands in one request.
- `browser_parallel`: sequential commands within each sequence, with sequences
  running concurrently; tab-targeting commands must specify `tabId` to avoid
  racing over the active tab.
- `browser_workflow`: reuse a named workflow with variables.
- Tab keep-alive and recovery helpers, including handling Zen's tab unloading.

The intended workload is research, comparison, extraction, and repeated work
across several tabs. These primitives are implementable in other projects too;
their value must be demonstrated through reliable task completion, not their
mere presence in a feature list.

### Testing and interaction reliability trade-offs

- **Browser coverage:** Chromium, Firefox, and WebKit provide a broader testing
  target than Kitsune's Firefox-family focus.
- **Testing-oriented tools:** HTTP request/response checks and Playwright test
  generation directly support developer testing workflows.
- **Playwright's automation foundation:** locator actions provide established
  waiting/actionability behavior and browser automation input paths.

Kitsune's extension includes DOM method and synthetic-event interaction paths.
Those are not interchangeable with all Playwright input behavior. Sites with
special input handling, trusted-event requirements, or complex timing may behave
differently. Reliability here is a validation and engineering question, not an
assumed advantage for the extension approach.


## Pairwise implications

### Microsoft Playwright MCP

**Where it overlaps:** structured snapshots and refs, browser interaction,
screenshots, JavaScript/code surfaces, tabs, and retained session state. Its docs
explicitly describe persistent profiles, isolated contexts, and an extension
connection to an existing browser. "Playwright always starts a fresh logged-out
browser" would be an incorrect competitive claim.

**Why choose it:** established Playwright automation, locator/actionability/waiting
behavior, and an automation ecosystem covering several browser engines.

**Where Kitsune can differ:** working directly in a supported Firefox-family
browser via its extension, Zen-specific tab handling, and the particular
combination of compact snapshots, action feedback, batch/parallel sequences,
and named workflows. Snapshot/ref support alone is not a differentiator.

**What to test:** the same Firefox task with matched viewport and budgets, then
long-page observation overhead, dynamic-form recovery, and multi-tab continuity.
Measure successful work, not just the number of tool calls.

### ExecuteAutomation Playwright MCP

**Why choose it:** browser interaction plus explicit HTTP request/response
assertions and generated Playwright tests. The inspected browser lifecycle
launches a browser and creates contexts/pages.

**Where Kitsune can differ:** ongoing browser work rather than a testing-first
workflow, compact ref-based observations, post-action feedback, and explicit
batch/parallel/workflow tools. These are current interface choices, not inherent
limits of Playwright.

See the [architecture and interaction trade-offs](#architecture-and-interaction-trade-offs)
and [pinned implementation references](#implementation-references) below for the
ExecuteAutomation case study. Microsoft and ExecuteAutomation maintain distinct
Playwright MCP servers; measurements against one do not describe the other.

### Chrome DevTools MCP

**Why choose it:** direct integration with Chrome's developer tooling, including
console/network inspection, input automation, and performance analysis. It also
has snapshot/element-identity workflows, so observation handles are not exclusive
to Kitsune.

**Where Kitsune can differ:** a Firefox/Zen workspace and task-oriented multi-tab
orchestration rather than Chrome debugging and performance diagnosis.

**What to test:** separate task-completion and debugging workloads. A diagnostics
benchmark could favor a different tool than a repeated research/form workflow.
Use a separate Chromium cohort, disable usage statistics, and audit the pinned
server's other outbound diagnostics. Do not attribute browser-engine differences
to the MCP interface alone.

### Browser MCP

**Why choose it:** it already offers the key combination of an MCP server,
Chrome extension, and existing-browser/session access. Its README says it was
adapted from Microsoft Playwright MCP for that purpose.

**Where Kitsune can differ:** Firefox-family support and concrete choices in
state presentation, action feedback, tab recovery, and orchestration. The bare
claims "uses your browser" and "keeps your login" are shared value propositions,
not a defensible Kitsune-only feature.

**What to test:** profile-isolated, session-preserving tasks; repeated observation
cost; and reliable continuation across tabs. Compare actual pinned tool surfaces
rather than assuming architectural similarity means identical behavior.

### Chrome MCP Server (`hangwin/mcp-chrome`)

**Why choose it:** extension-based Chrome control, native browser APIs, content
extraction, interaction, and semantic tab search. This is another close competitor
for research in an existing browser, not just a testing tool.

**Where Kitsune can differ:** Firefox/Zen integration and its own compact-state
and explicit workflow design. Do not claim the other server lacks multi-tab or
research capabilities.

**What to test:** finding and using information across tabs, background-tab
continuity, and recovery. Audit semantic-search embedding/model dependencies;
any added computation, downloads, or service calls belong in the resource/cost
accounting.

### Browser Harness MCP (Browser Use local mode)

**Why choose it:** its local stdio server exposes browser helpers over Chrome CDP;
the same outer coding agent can choose low-level browser actions. This is a
relevant fixed-model competitor, not automatically a delegated cloud agent.

**Where Kitsune can differ:** a Firefox-family extension/bridge and its specific
observation and tab-workflow interfaces. The trade-off includes installing an
extension/bridge versus establishing the supported Chrome CDP connection.

**What to test:** use the MCP entry point, not a CLI/skill, and freeze the helper
implementation. Do not allow persistent agent-written helpers between trials.
Keep Browser Use's hosted task-delegation mode separate from this comparison.

### Selenium MCP

**Why choose it:** a WebDriver-based control path and documented support for
Chrome, Firefox, Edge, and Safari. It is useful for testing whether control
architecture or MCP interface design matters under a similar browser engine.

**Where Kitsune can differ:** it does not require WebDriver and starts from the
connected Firefox-family workspace. That does not prove it is faster or more
reliable: DOM/synthetic-event paths and driver-based input behave differently.

**What to test:** pinned driver/browser compatibility, timing-sensitive forms,
input semantics, and recovery. A README listing Firefox support does not prove
compatibility with the exact Firefox binary used by another server.

### BrowserOS

**Why choose it:** a browser product with MCP integration for external coding
agents and a browser-centered agent experience.

**Where Kitsune can differ:** bringing an external agent to a supported browser
already in use rather than adopting a separate Chromium-based browser product.
BrowserOS's own UI and integrated services may offer capabilities an extension
alone does not.

**What to test:** invoke its MCP from the fixed outer model, not a built-in agent
loop. Account for the browser build, bundled services, and background processes.
Do not import personal logins or let an installer rewrite the normal agent config
just to prepare a benchmark.

### Steel MCP

**Why choose it:** browser/session infrastructure, with both cloud and documented
self-hosted operation. The README documents `STEEL_LOCAL=true` for a local
steel-browser service without a Steel Cloud API key.

**Where Kitsune can differ:** operating the existing local Firefox workspace
instead of provisioning a browser service. Steel's managed sessions are useful
for different deployment/scaling needs.

**What to test:** distinguish local from cloud, register all local service/browser
roots, and validate task-network access. A remote browser cannot reach a benchmark
at the harness's `127.0.0.1` without additional networking. Local RSS does not
measure a provider's remote browser resources.

## Adjacent tools that need separate treatment

- **[Browser Use hosted MCP](https://browser-use.com/mcp):** delegates an entire
  task to another agent and returns the result. Internal model execution and
  billed service usage change the fixed-model experiment. Do not confuse it with
  Browser Harness MCP's local action tools.
- **[Browserbase MCP](https://www.browserbase.com/mcp):** a possible hosted-browser
  comparison, subject to service/model costs and task-network access. The old
  [reference repository](https://github.com/browserbase/mcp-server-browserbase)
  is archived and explicitly not representative of the current production service.
- **[Puppeteer reference MCP](https://github.com/modelcontextprotocol/servers-archived/tree/main/src/puppeteer):**
  an archived historical baseline, not a maintained primary contender.
- **[BrowserTools MCP](https://github.com/AgentDeskAI/browser-tools-mcp):** primarily
  diagnostics/logging; verify equivalent action capabilities before treating it
  as an end-to-end task-control substitute.
- **[agent-browser](https://github.com/vercel-labs/agent-browser):** a CLI, not a
  native MCP baseline. A CLI adapter would change the exposed harness/tool policy.

A local operation path does not guarantee no telemetry, embedding downloads,
model calls, or future pricing changes. Audit the exact mode and pinned version.

## Claims we should not make yet

- "Kitsune is faster, cheaper, or more memory-efficient."
- "Using an existing browser makes its memory cost zero."
- "Extension control guarantees bot-detection avoidance."
- "More tools or fewer calls necessarily mean better task performance."
- "Only Kitsune can reuse a browser or work across tabs."
- "A successful simple click benchmark validates the full Kitsune product."

There has not yet been a direct Kitsune-versus-ExecuteAutomation benchmark in
this work. The initial external pilot compares Kitsune with **Microsoft's**
Playwright MCP. Its UI-only policy excludes code/batch/workflow surfaces to
protect the client-side task reward, so it does not evaluate Kitsune's complete
multi-step tool design.

## What would make Kitsune's positioning credible?

1. **Choose a workload, not a universal winner.** Long-page research, dynamic
   forms, ongoing authorized sessions, and multi-tab work are plausible targets.
2. **Measure success first, then efficiency.** Report correctness/failure rates
   together with tokens, latency, calls, local resources, and recovery behavior.
3. **Keep browser cohorts visible.** Start with Firefox-capable controls for the
   existing setup; add Chromium controls separately, with a Playwright baseline
   on the same build when possible.
4. **Match the permitted capability class.** A UI-only task must audit every
   server's code/evaluate/workflow/batch surfaces. A full-native track can test
   orchestration, but needs an evaluator those tools cannot trivially manipulate.
5. **Separate cold and warm costs.** An already-running browser still consumes
   memory and CPU. Include owned browser/bridge/driver/sidecar processes and avoid
   presenting shared-page RSS sums as unique physical memory.
6. **Use controlled state.** Dedicated profiles, test accounts, deterministic
   reset/seed, matched effective viewport, repeated trials, counterbalanced order,
   and explicit cache/startup policy.
7. **Use within-Kitsune experiments too.** Toggle snapshot compaction, action
   feedback, or batching to attribute effects more clearly than product-to-product
   comparisons alone allow.

| Hypothesis | Representative task | Evidence to collect |
| --- | --- | --- |
| Compact observations help target selection | Find specific information or the right control in a long, repetitive page | Task success, wrong-target actions, input/output tokens, observation size |
| Action feedback reduces unnecessary round trips | Fill a dynamic form whose state changes after each action | Task success, calls, stale-ref errors, total time and tokens |
| Multi-tab primitives improve research work | Gather and reconcile information from several independent pages | Answer correctness, cross-tab interference, completion time, recovery behavior |
| Persistent browser work helps repeated tasks | Continue several authorized operations in the same controlled session | State correctness, warm-task latency, full browser/bridge resource costs |
| Recovery tools help under disruption | Delay content, navigate, or unload a background tab mid-task | Recovery success, extra calls, timeouts and failure categories |


The [external benchmark](https://github.com/junyeong-nero/browser-mcp-bench)
compares Kitsune with Microsoft Playwright MCP. Its six-task variant-0 pilot
found workload-dependent trade-offs and a confirmed tab-inventory cache bottleneck;
see the sibling [optimization findings](../../browser-mcp-bench/docs/optimization-findings.md)
and the implementation backlog in [Roadmap #47](https://github.com/junyeong-nero/kitsune/issues/47).
It does not establish general superiority over that server or any project listed here. Its UI-only configuration also excludes several orchestration/code
surfaces, so it is not a test of Kitsune's full-native feature set.

## Takeaway

Kitsune should not compete on "we also click pages" or claim that existing-browser
control is unique. Its stronger, testable position is **an effective ongoing
Firefox-family workspace for an external agent**, with concise state, reliable
action feedback, and continuity across tabs and repeated tasks.

The architecture and tool choices above support that direction. Better task
outcomes and lower total cost still need to be demonstrated.

## Implementation references

Reference snapshots:

- Kitsune: `967dfdf6babba27383cfdab4a2997f2dfcb57c76`.
- ExecuteAutomation: `2349c2891e7c499c8c07b7d78c7f3fb4c797a1da`.

Kitsune implementation:

- [MCP tools](../src/kitsune/server.py)
- [Extension command routing and action feedback](../extension/background/background.js)
- [Snapshots and DOM interaction](../extension/content/content.js)
- [Tool list and targeting guidance](tool-list.md)
- [API reference](api-reference.md)

ExecuteAutomation implementation at the reference snapshot:

- [Registered tools and schemas](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/tools.ts)
- [Browser launch and context lifecycle](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/toolHandler.ts)
- [Test-code generator](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/tools/codegen/generator.ts)

Revisit this comparison when either implementation changes. Do not turn a
snapshot of current tool choices into a permanent claim about a competitor.
