# Positioning: an agent workspace inside Firefox

## The position

> Kitsune connects an external AI agent to a Firefox-family workspace so the user
> can delegate research, drafting, and repeated browser tasks while continuing
> their own work.

The user-facing promise is **“Bring your coding agent into Firefox—and keep
working alongside it.”** The product is not another browser or a built-in AI
assistant: it connects an agent the user already chooses to a browser they
already use, preferably a dedicated profile with narrowly scoped accounts.

The strongest differentiation candidate is not browser control itself. It is the
combination of **shared-browser behavior, bounded delegation, and recovery that
accounts for uncertain action outcomes**.

This is a qualitative comparison of Kitsune's implementation and competitors'
public documentation, not a performance ranking or a security audit. Competitor
capabilities below describe the documentation inspected for this revision;
linked default branches can change. Absence from a README is not proof that a
feature is absent. Pin releases and inspect implementations before publishing
quantitative or exclusivity claims.

## Who this is for

The primary user is a developer, researcher, or power user who:

- Uses Firefox, Zen, or LibreWolf as a working browser.
- Already uses Claude Code, Codex, Pi, OpenCode, or another external MCP client.
- Wants help with authorized browser tasks without adopting a different browser
  product or a second agent loop.
- Wants to choose between reading, preparing a draft, and taking action.

### Best-fit work

| Task | Why Kitsune fits |
| --- | --- |
| Compare information across several pages | Explicit tab targets, background tabs, compact observations, and optional parallel sequences |
| Prepare a form in an authorized account | Existing session access and a draft tool surface that excludes click and submit tools |
| Research while the user works in another tab | User-activity checks, background capture, and agent/user tab ownership rules |
| Continue interrupted repeated work | Structured outcomes and checkpoint/resume rules that distinguish successful, unstarted, and uncertain writes |
| Use several coding clients with one browser connection | Automatically started shared bridge behind local stdio MCP servers |

The central question is not “Can it click this button?” It is “Can I delegate
this work without losing control of the workspace I am using?”

### Not the primary target

- Cross-browser regression testing across Chromium, Firefox, and WebKit.
- Deep debugger/profiler workflows or Firefox engine development.
- Large-scale unattended scraping or managed browser fleets.
- Specialized security research and vulnerability discovery.
- Fully autonomous banking, payments, or other high-consequence account actions.

These are product priorities, not claims that every adjacent task is impossible.

## Direct Firefox competitors

Firefox-compatible tools are the most relevant first comparison cohort. Chrome
MCPs are useful adjacent references, but do not answer why a Firefox user should
choose Kitsune over another Firefox connection.

| Project | Documented emphasis | Implication for Kitsune |
| --- | --- | --- |
| [Mozilla Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp) | Agentic browsing and web development through WebDriver BiDi/Selenium; snapshots/UIDs, forms, console, network, debugger, profiler, and existing-browser connection | The primary baseline. Compete on shared-workspace behavior and delegation, not a claim that Mozilla only supports debugging |
| [Firefox-Browser-MCP (ICWR)](https://github.com/ICWR-TEAM/Firefox-Browser-MCP) | Firefox extension plus local Python/WebSocket server; existing tabs, snapshots/refs, interactions, network request/response bodies, stdio and HTTP transports | Very close architecture. Firefox support, extensions, login reuse, and refs are not differentiators by themselves |
| [FoxMCP](https://github.com/ThinkerYzu/foxmcp) | Tabs/groups, windows, bookmarks, history, page scripts, uploads, and network inspection; Firefox Add-ons distribution | Browser-workspace management is already served. Kitsune should emphasize task interaction, user coexistence, and recovery rather than simply managing more browser objects |
| [ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) | Zen control, parallel tabs, unloader defense, sessions, batch scripting, workflows, pools, and tags | Kitsune's upstream foundation. These capabilities are inherited/shared strengths, not evidence of an exclusive invention |
| [LittleFoxMCP](https://github.com/bytedogssyndicate/littlefox-mcp) | Extension/native-host bridge for DOM, input, cookies, storage, scripts, and network access | Generic browser control is well represented; a larger action list is a weak product position |
| [browser-bridge](https://github.com/jordandubu/browser-bridge) | Firefox/native messaging with security-oriented tools for DOM sinks, CSP, CORS, WebSockets, and postMessage | A clear specialist use case. Kitsune should not dilute its position by competing on vulnerability-hunting tools |

### Mozilla: existing-browser access is not exclusive

Mozilla documents `--connect-existing` with cookies, logins, and tabs retained.
Firefox must be started with both `--marionette` and `--remote-debugging-port`.
Its documentation also warns that Marionette changes automation-related browser
signals. Kitsune uses an extension and local bridge without requiring those
startup flags or WebDriver.

That is a concrete connection-model difference. It does **not** establish that
Kitsune is undetectable, bypasses anti-bot systems, or is more reliable. Mozilla
also offers snapshot/UID interaction, tool presets, and file-backed output via
`saveTo`; concise observations and configurable tool surfaces are not unique
categories of capability.

### Extension peers: compare product behavior, not architecture slogans

ICWR already offers existing-session access and snapshot/ref interaction. FoxMCP
already offers extensive workspace organization, reusable scripts, and an
installable Firefox add-on. Signed distribution and session reuse are useful,
but insufficient as standalone competitive claims.

There is a specific operational difference worth testing: FoxMCP documents that
concurrent stdio clients conflict and recommends its persistent HTTP server for
shared use. ICWR also documents persistent HTTP transports. Kitsune starts a
shared bridge automatically behind each client's stdio server. This can simplify
multi-client setup, but does not imply complete isolation between clients or
conflict-free concurrent writes to the same tab.

### ZenLink: distinguish inheritance from Kitsune's direction

Kitsune is based on ZenLink and ZenLink-MCP by Jay-Quan. Parallel tabs, keep-alive,
workflows, tags, pools, and session helpers already appear in the upstream tool
surface. They support the use case but should not be presented as unique to
Kitsune.

Kitsune's product story should instead be supported by its current shared-browser
rules, restricted task modes, observation/action contracts, connection lifecycle,
and uncertain-outcome recovery. These are implementation choices to evaluate,
not claims that no other project could implement them.

## What supports the position today

### 1. Shared-browser rules

Kitsune explicitly considers the person using the browser:

- Agent-created tabs open in the background.
- Ordinary tab screenshots use `tabs.captureTab` without selecting the tab.
- Writes and navigations on a tab the user is using return `user_active` instead
  of acting.
- Tabs carry agent ownership; closing a user tab requires a separate
  action-bound confirmation step.

The benefit is reduced workspace interference, not merely multi-tab support.
See [Shared browser](api-reference.md#shared-browser),
[background routing](../extension/background/background.js), and
[user-activity detection](../extension/content/content.js).

**Limits:** site-opened tabs/popups may appear briefly before focus is restored.
OS-level trusted clicks move the real cursor and can focus a window. User input
inside iframes is not covered by the top-document activity detector. Tab ownership
uses heuristics and is forgotten on extension/browser restart. Confirmation tokens
are not an independent human-approval channel. Do not promise “never interrupts
the user” or treat ownership as a security boundary.

### 2. Bounded delegation: read, draft, act

`KITSUNE_MODE=read|draft|act` restricts both the tool surface and bridge requests:

| Mode | User intent | Important qualification |
| --- | --- | --- |
| `read` | Inspect and research | Navigation is allowed; this is not a guarantee that every website access has no side effects |
| `draft` | Fill supported fields without click/submit tools | Websites may autosave input; this is not a transactional sandbox |
| `act` | Perform enabled interactions | Default mode; ask-before-act is configured separately |

This makes delegation more concrete than a prompt saying “please do not submit.”
The restricted modes intersect with the enabled capability groups; they do not
silently enable missing tools. See [Task modes](guide.md#task-modes) and
[the explicit policy map](../src/kitsune/task_modes.py).

Ask-before-act, sensitive-field redaction, and redacted audit values complement
these controls. However, ask-before-act is off by default, its second call does
not prove human approval, and redaction is heuristic. None of this prevents prompt
injection or makes a full personal profile safe to expose. Follow the
[security guidance](guide.md#security).

### 3. Recovery that distinguishes uncertainty from failure

An interrupted container can record which steps succeeded, never started, failed,
were held, or have an unknown outcome. With the optional `automation` capability,
`browser_resume` does not resend successful writes. An unknown write requires an
observation that it was applied or not applied before continuation. Parallel
resumption checks tab availability/claims, workflows are checked for changes,
and current policy is applied again.

This matters because a timed-out click may already have sent a message or changed
an account. Blind retries are not equivalent to safe recovery.

**Limits:** checkpoints live only in bridge memory, expire after 30 minutes, are
capped at 20, and disappear on restart. Reported observations depend on the caller.
This is not durable workflow infrastructure or an exactly-once guarantee. See
[Checkpoint and resume](checkpoint-resume.md).

### 4. An explicit observation/action contract

Kitsune combines scoped snapshots and refs, post-action deltas, strict targeting,
and structured recovery fields (`code`, `retryable`, `hint`, `state`). The aim is
to help the outer agent choose the next action without repeatedly inspecting a
whole page or blindly retrying a write.

Deltas omit unchanged content, but also omit removed elements and are capped.
An empty delta is not proof that an action succeeded; use outcome checks or a
fresh snapshot. Refs have a lifecycle and can become stale. See
[Post-action snapshots](api-reference.md#post-action-snapshots) and
[Action errors](api-reference.md#action-errors).

The testable hypothesis is **lower observation and recovery overhead per successful
task**, not “fewer calls always wins.” Schema size, larger responses, missing
context, and repeated observations can offset any savings.

## Weaknesses and product trade-offs

### Input fidelity and diagnostic depth

The extension uses DOM methods and synthetic events. These are not interchangeable
with all WebDriver/BiDi input behavior. Complex editors, dragging, trusted-event
requirements, and timing-sensitive interfaces need real-site validation. The
native trusted-click path has platform/dependency and focus trade-offs; it is not
a universal substitute for a browser automation input system.

Mozilla's debugger/profiler and protocol-based tooling make it a stronger starting
point for deep Firefox diagnostics. Playwright's broader engine support and
automation ecosystem make it a natural testing baseline. Kitsune should not claim
equivalent coverage merely because it can click, inspect network data, or run JS.

### Setup and configuration complexity

Users may need to understand server/bridge/extension versions, capability groups
(`KITSUNE_CAPS`), the published subset and gateway (`KITSUNE_PUBLISH`), task modes,
and ask-before-act. The signed extension is convenient for ongoing installation,
but agent-supplied JavaScript requires the temporary developer build. The current manifest requires Firefox 142+ and broad extension
permissions; hiding MCP tools does not reduce those installation permissions.

The opportunity is simpler task-oriented onboarding, not another layer of knobs.
See [Installation](installation.md) and [Configuration](guide.md#configuration).

### Default experience versus advanced promises

The default `KITSUNE_CAPS=core` surface is Playwright MCP's 25 core tools. Kitsune's
other groups, including batch/parallel/resume in `automation`, are enabled with
`KITSUNE_CAPS`; `KITSUNE_PUBLISH` can keep them out of the initial tool list behind
the gateway tools. Restricted task modes and ask-before-act are opt-in. Do not
advertise every advanced workflow as available immediately after the default setup.
Show the required configuration beside the example, and distinguish an instruction
in natural language from an enforced mode.

### No measured general superiority

The [external benchmark](https://github.com/junyeong-nero/kitsune-benchmark)
compares Kitsune with Microsoft's Playwright MCP. Its six-task variant-0 pilot
found workload-dependent trade-offs and a tab-inventory cache bottleneck. It does
not establish superiority over Firefox-native competitors or the current versions
of either server. Its UI-only policy excludes orchestration/code surfaces, so it
does not evaluate the complete Kitsune product.

Historical findings should guide retesting, not serve as current performance
claims. See the benchmark repository's `docs/optimization-findings.md` and
[Roadmap #47](https://github.com/junyeong-nero/kitsune/issues/47).

## Adjacent tools: useful, but a different comparison cohort

| Project | Why compare it separately? |
| --- | --- |
| [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp) | Structured snapshots/refs and established automation. Supports Firefox automation; its documented existing-browser extension connection is for Chromium-family browsers. Do not confuse Firefox launch support with attaching to an ordinary Firefox session |
| [ExecuteAutomation Playwright MCP](https://github.com/executeautomation/mcp-playwright) | Browser automation plus HTTP checks/test generation; a distinct server from Microsoft's, not an interchangeable benchmark label |
| [Selenium MCP](https://github.com/angiejones/mcp-selenium) | Firefox-capable WebDriver control; useful for comparing control architectures with the browser engine held similar |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Chrome debugging/performance and task automation; engine and developer-tool differences need separate accounting |
| [Browser MCP](https://github.com/BrowserMCP/mcp) and [Chrome MCP Server](https://github.com/hangwin/mcp-chrome) | Existing-browser extension competitors with overlapping session/tab/research value, but a different browser family |
| [Browser Harness MCP](https://github.com/browser-use/browser-harness/blob/main/docs/MCP.md) | Local browser helpers over Chrome CDP; distinguish this from hosted Browser Use task delegation with another model loop |
| [BrowserOS](https://github.com/browseros-ai/BrowserOS) | A browser product with MCP access, rather than an extension in a supported browser already in use |
| [Steel MCP](https://github.com/steel-dev/steel-mcp-server) | Local or cloud browser-session infrastructure, not the user's existing Firefox workspace |

A local mode does not by itself prove no telemetry, model calls, downloads, or
future service costs. Audit the exact version and mode. Archived reference servers
and CLI wrappers should not silently stand in for maintained native MCP products.

## Make the promise demonstrable

### Lead with a coexistence demo

A generic Hacker News summary proves basic operation but not why to choose Kitsune.
A more representative demo is:

1. The user continues writing in one tab.
2. The agent researches three sources in background tabs without taking that view.
3. The agent prepares a form draft in an authorized service under draft mode.
4. The user reviews and performs the final submission.

Use disposable accounts and a controlled form; show the configuration and any
limits. This demonstrates delegation alongside the user rather than just page
control. A separate advanced demo can inject a disconnect and show recovery
without resending a completed write.

### Benchmark the promises, not the tool count

| Promise or hypothesis | Evidence to collect |
| --- | --- |
| Less workspace interference | Unrequested tab switches, focus changes, user-input conflicts, and task success while the user is active |
| Respect user-owned tabs | Unintended closes and ownership false positives/negatives, including restart and popup cases |
| Controlled delegation | Disallowed actions refused across direct calls and bridge routes; autosave and navigation side effects documented separately |
| Better recovery | Duplicate writes/submits after injected timeouts/disconnects, recovery success, and unresolved outcomes |
| Useful compact observations | Correctness, wrong-target actions, tokens/time per successful task, stale refs, and extra observation calls |
| Easier adoption | Time from installation to first successful task, reconnect success, and multi-client setup failures |

Use pinned server/browser versions, dedicated profiles, deterministic tasks,
matched permissions and viewports, repeated trials, and a fixed outer agent.
Separate Firefox and Chromium cohorts, cold and warm costs, and UI-only versus
full-native capability tracks. Include browser/bridge/driver resource costs rather
than treating an already-running browser as free. Test within-Kitsune toggles as
well as product-to-product differences to attribute effects.

## Claims to avoid

- “Only Kitsune attaches to Firefox, reuses logins, or supports multiple tabs.”
- “Parallel workflows and Zen unloader handling are unique Kitsune inventions.”
- “Mozilla's MCP is only for debugging.”
- “Extension control is undetectable or bypasses anti-bot protection.”
- “Read/draft modes, redaction, or confirmation tokens make account access safe.”
- “Kitsune never steals focus or interferes with user input.”
- “Resume guarantees exactly-once execution or survives a daemon restart.”
- “Kitsune is faster, cheaper, or more memory-efficient” without matched evidence.

## Bottom line

Firefox support is the entry point, not the whole position. Kitsune's strongest
credible direction is **working alongside an external agent in an ongoing
Firefox-family workspace, with explicit delegation limits and thoughtful recovery**.

Prioritize reliable coexistence, understandable defaults, and evidence from real
tasks over a larger tool catalog. That gives users a concrete reason to choose
Kitsune while keeping its claims aligned with what the implementation can support.
