# TODO

## Product direction and priorities

**Build a reliable ongoing browser workspace for an external agent, not simply
another MCP with more browser automation tools.** Start with the Firefox-family
users Kitsune already serves. Firefox/Zen support is an entry point, not a lasting
advantage by itself; completing useful work with less user intervention is the
advantage to validate.

Keep three outcomes in focus:

1. Give the agent enough relevant state to choose the next action, without
   unnecessary page content.
2. Carry multi-step work across tabs without losing ownership, state, or progress.
3. Make failures explicit and recoverable instead of silently reporting success.

Existing snapshots, action feedback, batch/parallel tools, workflows, and tab
recovery are foundations to harden and evaluate, not features to reimplement.
See [Browser MCP comparisons](browser-mcp-comparison.md) and the
[ExecuteAutomation deep dive](positioning-vs-executeautomation.md) for context.

### P0 — Interaction reliability and safe failure

- [ ] Define and test a consistent waiting/actionability/timeout contract across
  action tools. Borrow Playwright's reliability principles without claiming DOM
  or synthetic-event actions have identical input semantics.
- [ ] Distinguish dispatching an action from observing its expected outcome.
  Report missing/failed post-action observations explicitly; do not treat a sent
  command or `{ok: true}` alone as proof the user's task succeeded.
- [ ] Make missing, hidden/covered, stale-ref, navigating, closed/unloaded-tab, and
  disconnected-extension failures distinguishable, with the current state and a
  useful recovery hint where available.
- [ ] Verify tab/document/ref identity before mutation and fail explicitly when
  the intended target is no longer valid. Test focus changes and navigation races.
- [ ] Add regression cases for dynamic forms, delayed content, tab unloads, and
  reconnects. Prioritize silent wrong-target actions over adding more tools.
- [ ] Ensure recovery does not blindly repeat consequential operations. Separate
  safe retries from submissions, payments, messages, and deletions; connect this
  to the ask-before-act policy in [Features](#features).

### P1 — Observation efficiency without losing correctness

- [ ] Evaluate and tune existing compact snapshots, repeated-item compaction,
  subtree/depth controls, and post-action feedback for long/repetitive pages.
- [ ] Test whether fresh refs and action feedback reduce wrong-target actions,
  stale-ref errors, and unnecessary re-observation without hiding required state.
- [ ] Measure total input/output tokens and task success, not just snapshot size
  or call count. Larger action responses and tool schemas can erase local savings.
- [ ] Run within-Kitsune comparisons with compaction or action feedback toggled
  to separate their effects from differences between browser-control libraries.

### P2 — Multi-tab continuity and repeatable work

- [ ] Harden explicit tab ownership and routing for batch/parallel sequences;
  prove that focus changes cannot send one task's action to another task's tab.
- [ ] Evaluate existing keep-alive/recovery and workflow primitives together on
  realistic research → comparison → draft workflows, not only isolated commands.
- [ ] Add inspectable per-step execution state/results and explicit partial-failure
  reporting. Determine which workflow steps can safely resume after interruption.
- [ ] Design checkpoint/resume behavior that never silently repeats an already
  completed submission or other non-idempotent action.
- [ ] Make task targets, progress, and pending consequential actions understandable
  to the user, without introducing a separate internal LLM agent loop.

### Learn from competitors without copying their entire product

| Reference | Principle to adopt |
| --- | --- |
| Playwright-based MCPs | Actionability checks, predictable waiting/timeouts, useful errors |
| Chrome DevTools MCP | Enough diagnostics to explain a failure, not every developer-tool feature |
| Browser MCP / Chrome MCP Server | Clear installation, connection, and session-access boundaries |
| Browser Harness MCP | Easy integration with existing coding agents and explicit tool contracts |
| BrowserOS | Visible task targets/progress and understandable user control |

- [ ] Review install/connect/reconnect UX with real users; distinguish extension,
  bridge, and agent-configuration failures without changing their normal profiles
  or granting access to unrelated accounts.
- [ ] Add the minimum failure diagnostics needed to explain an unsuccessful
  operation while protecting page data and credentials in logs.

### Validate with useful tasks and users

- [ ] Establish three resettable workload families:
  - Long-page information lookup: answer correctness, observation volume, tokens.
  - Dynamic-form drafting: completion, wrong actions, recovery; no real submission.
  - Multi-tab research → draft: output correctness, tab interference, interventions.
- [ ] Report success/failure rates first, then user interventions, elapsed time,
  tokens, and resources per successful task, alongside the costs of failed attempts.
  A quick failure is not an efficient solution.
- [ ] Fix model/harness, initial state, seeds, effective viewport, budgets, and
  permitted capability class; repeat trials and counterbalance execution order.
- [ ] Separate cold-start from warm-ready costs and count owned browser/bridge/
  driver/sidecar resources. An existing browser is not zero-cost, and summed RSS
  is not unique physical memory.
- [ ] Keep UI-only and full-native capability tracks separate. A benchmark that
  excludes batch/workflow/code cannot validate the complete orchestration design.
- [ ] Validate actual demand with Firefox-family users doing these workflows;
  revisit the positioning if it does not reduce meaningful user effort.

### Not priorities for now

- Supporting every browser engine before establishing a strong Firefox-family use case.
- Becoming a test-code generator or a complete HTTP/API testing product.
- Building a proprietary LLM agent loop or cloud-browser service.
- Increasing tool count as a product goal.
- Claiming lower latency, tokens, or memory before controlled measurements show it.

## Install and distribution

- [ ] **Sign the extension on AMO (unlisted channel)** so it survives browser restarts.
  Today it is a temporary add-on loaded from `about:debugging`.
  1. Create AMO API credentials (JWT issuer + secret) at
     https://addons.mozilla.org/developers/addon/api/key/ . Keep the secret in a
     password manager; never commit it or paste it into chat.
  2. `cd extension && npx web-ext lint` and fix errors.
  3. `npx web-ext sign --channel=unlisted --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET`
  4. Install the signed `.xpi`: `about:addons` → gear → *Install Add-on From File…*
  - Expect lint warnings for `eval` in `content/content.js` and
    `tabs.executeScript({ code })` in `background/background.js` (the `browser_js`
    tool). Unlisted signing usually passes with warnings; if it is rejected,
    gate or rework those paths.
  - Bump `manifest.json` `version` on every signed upload.
- [ ] Check whether Zen honours `xpinstall.signatures.required=false` (unsigned
  install). Firefox release ignores it.
- [x] Publish `kitsune-browser-mcp` to PyPI (0.1.0); `uvx --from kitsune-browser-mcp kitsune`
  is the documented install. Releases are tagged and carry the extension zip; see
  [Releasing](releasing.md).

## Bridge daemon

- [x] Add `kitsune-bridge stop` / `status` commands (authenticated shutdown with
  verified PID fallback; no broad `pkill`).
- [ ] Optional idle shutdown when no MCP client has called for a while.
- [ ] Migrate from the deprecated `websockets.server.serve` (legacy API) to
  `websockets.asyncio.server.serve`; its `origins=` option can replace the
  manual origin check.
- [x] Report the package version and source build in `browser_status`; automatically
  replace mismatched daemons on MCP startup (`KITSUNE_NO_BRIDGE_RESTART=1` opts out).
- [x] Create `bridge.lock` and the daemon's `bridge.pid` with 0600 like the token.

## Features

- [ ] `browser_trusted_click` works only on Windows (`ctypes.windll`). Add a
  macOS path (Quartz `CGEventCreateMouseEvent` via pyobjc, or `cliclick`).
  Window focusing also looks for the title "Zen Browser"; make it generic.
- [ ] Ask-before-act policy for sensitive actions (submit, payment pages,
  `browser_js`, cookie reads), similar to Aside. `browser_set_policy` has
  allow/deny/readonly to build on.
- [ ] `browser_form_fill` checkbox/radio uses `el.checked = !!value`, so the string
  `"false"` checks a box and unchecking a radio can leave its group empty.
- [ ] Password redaction gaps: password inputs inside shadow DOM, masked fields that
  are not `type=password`, and a possible explicit opt-in to reveal values. Test
  native password-manager autofill with disposable credentials only.

## Tool design (vs. Playwright MCP)

The original consolidation below adopted snapshots/refs and a smaller grouped
default surface. Keep these completed items as implementation history; tool counts
vary by version and enabled capabilities and are not a success metric. The next
priority is to validate reliability and useful work, as described above. Existing
browser/session access and snapshot/ref interfaces are shared with competitors,
not exclusive Kitsune features.

1. **Snapshot + ref as the primary interaction model** (highest impact)
   - [x] Add `browser_snapshot`: compact YAML/text tree of interactive elements plus
     headings/landmarks, e.g. `- button "Log in" [ref=s3e5]`. Replaces the noisy JSON
     of `browser_dom`.
   - [x] Make refs generation-scoped (e.g. `s3e5`). Today `refCounter = 0` resets on
     every tree build (`content.js` `getAccessibilityTree`), so an old ref can
     silently resolve to a different element. Return a "stale ref, call browser_snapshot again"
     error on mismatch.
   - [x] Every action tool (click, type, hover, select, check, drag, ...) takes
     `ref` + human-readable `element`; docstrings say "get ref from browser_snapshot".
     Ref resolution already exists in `resolveElement` but is undocumented.
   - [x] Actions return a short post-action snapshot (or diff) instead of `{ok: true}`.
2. **Shrink and group the tool surface**
   - [x] Remove `browser_page_text_by_tab_id` (duplicate of `browser_page_text(tab_id)`).
   - [x] Merge `wait_for_element/result/url/title/network_idle` into
     `browser_wait_for(selector|text|text_gone|js|url|title|network_idle|time)`.
   - [x] Merge `screenshot/element_screenshot/full_page_screenshot` into
     `browser_screenshot(selector?, full_page?)`.
   - [x] Merge `type/fill/set_editable_content` into `browser_type(selector, text, mode?)`.
   - [x] Merge `tabs/new_tab/close_tab/switch_tab` into `browser_tabs(action=...)`.
   - [x] Capability groups via env, e.g. `KITSUNE_CAPS=core,tabs,network,storage,parallel,devtools`;
     default `core` (~25 tools). Move keep_alive / pool / tags / broadcast /
     sync_barrier / scheduler into `parallel`.
   - [x] Move `browser_wp_html` (WordPress-specific) out of the core tools into a workflow.
3. **Missing capabilities**
   - [x] Dialog handling (alert/confirm/prompt), like Playwright's `browser_handle_dialog`.
   - [x] Screenshots return MCP `ImageContent` so the model sees them; file save optional.
   - [x] Wait for text to appear/disappear.
4. **Metadata**
   - [x] Add `readOnlyHint` / `destructiveHint` tool annotations (feeds client permission
     prompts and the ask-before-act policy below).

## Cleanup

- [ ] Rename leftover branding: `[Claude Bridge]` log prefix and header comment in
  `extension/background/background.js`, `zen_` screenshot file prefix in `bridge.py`.
- [ ] Update `docs/api-reference.md` and `docs/scraping-playbooks/` (old ports
  8765/8766, `zen_*` tool names, ZenLink paths).
- [ ] Automated tests for the security checks (no token → 401, bad Host → 403,
  no CORS, non-extension WebSocket origin → 1008) and the single-daemon lock.

## Upstream

- [ ] Report the localhost auth / CORS / WebSocket hijack issue to ZenLink
  (JayQuan-McCleary/ZenLink) privately, then offer the patch.
