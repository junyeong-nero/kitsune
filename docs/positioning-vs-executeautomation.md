# Positioning: Kitsune vs ExecuteAutomation Playwright MCP

This document records the architectural differences, product positioning, and
validation questions for Kitsune relative to
[ExecuteAutomation Playwright MCP](https://github.com/executeautomation/mcp-playwright).
It is not a performance ranking or a claim that either project cannot implement
the other's features. For the wider landscape, see
[Kitsune and other browser MCPs](browser-mcp-comparison.md).

ExecuteAutomation's project is distinct from
[Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp).
Measurements against Microsoft's server are not measurements against
ExecuteAutomation's server.

## Summary

**ExecuteAutomation is oriented toward automating a browser for interaction,
testing, and test generation. Kitsune is oriented toward making an already-running
Firefox-family browser an agent's ongoing workspace.**

Basic navigation, clicking, typing, screenshots, extraction, and JavaScript
execution overlap substantially. The stronger Kitsune positioning is not
"Playwright MCP with more tools," but:

> Use the Firefox/Zen workspace you already have, expose the state an agent needs
> concisely, and carry work across tabs and repeated tasks.

This is a product direction supported by implementation choices, not yet proof
of better task success, lower token use, lower memory use, or lower latency.

## Architecture and tool surface

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

## Where Kitsune can differentiate

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

## Where ExecuteAutomation has advantages

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

## How to validate this positioning

| Hypothesis | Representative task | Evidence to collect |
| --- | --- | --- |
| Compact observations help target selection | Find specific information or the right control in a long, repetitive page | Task success, wrong-target actions, input/output tokens, observation size |
| Action feedback reduces unnecessary round trips | Fill a dynamic form whose state changes after each action | Task success, calls, stale-ref errors, total time and tokens |
| Multi-tab primitives improve research work | Gather and reconcile information from several independent pages | Answer correctness, cross-tab interference, completion time, recovery behavior |
| Persistent browser work helps repeated tasks | Continue several authorized operations in the same controlled session | State correctness, warm-task latency, full browser/bridge resource costs |
| Recovery tools help under disruption | Delay content, navigate, or unload a background tab mid-task | Recovery success, extra calls, timeouts and failure categories |

Keep model, harness, task/reset, seed, effective viewport, and budgets fixed.
Validate whether both pinned servers can use the same Firefox binary rather
than assuming their Firefox support implies identical browser versions. Separate
cold-start and warm-ready costs, repeat tasks, and counterbalance execution order.
Always report success together with time/tokens/resources; a quick failure is
not an efficient solution.

Where possible, compare Kitsune variants with a single feature enabled/disabled
as well. Holding the control implementation constant makes it easier to attribute
an improvement to snapshot compaction, action feedback, or batching rather than
to unrelated differences between products.

## Implementation references

Reference snapshots:

- Kitsune: `967dfdf6babba27383cfdab4a2997f2dfcb57c76`.
- ExecuteAutomation: `2349c2891e7c499c8c07b7d78c7f3fb4c797a1da`.

Kitsune implementation:

- [MCP tools](../src/kitsune/server.py)
- [Extension command routing and action feedback](../extension/background/background.js)
- [Snapshots and DOM interaction](../extension/content/content.js)
- [Tool surface guidance](tool-surface.md)
- [API reference](api-reference.md)

ExecuteAutomation implementation at the reference snapshot:

- [Registered tools and schemas](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/tools.ts)
- [Browser launch and context lifecycle](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/toolHandler.ts)
- [Test-code generator](https://github.com/executeautomation/mcp-playwright/blob/2349c2891e7c499c8c07b7d78c7f3fb4c797a1da/src/tools/codegen/generator.ts)

Revisit this comparison when either implementation changes. Do not turn a
snapshot of current tool choices into a permanent claim about a competitor.
