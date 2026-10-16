# TODO

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
- [ ] Publish `kitsune-mcp` to PyPI (or install with `uv tool install`) so the MCP
  command does not depend on the `~/workspace/kitsune` checkout.

## Bridge daemon

- [ ] Add `kitsune-bridge stop` / `status` commands (today: `pkill -f kitsune.bridge`).
- [ ] Optional idle shutdown when no MCP client has called for a while.
- [ ] Migrate from the deprecated `websockets.server.serve` (legacy API) to
  `websockets.asyncio.server.serve`; its `origins=` option can replace the
  manual origin check.
- [ ] Report the Kitsune version in `browser_status` (`BRIDGE_VERSION` still says 2.1.0).
- [ ] Create `bridge.lock` with 0600 like the token.

## Features

- [ ] `browser_trusted_click` works only on Windows (`ctypes.windll`). Add a
  macOS path (Quartz `CGEventCreateMouseEvent` via pyobjc, or `cliclick`).
  Window focusing also looks for the title "Zen Browser"; make it generic.
- [ ] Ask-before-act policy for sensitive actions (submit, payment pages,
  `browser_js`, cookie reads), similar to Aside. `browser_set_policy` has
  allow/deny/readonly to build on.

## Tool design (vs. Playwright MCP)

Kitsune exposes ~120 tools; Playwright MCP exposes ~25 core tools built around
an accessibility snapshot + element refs. Keep Kitsune's strengths (real logged-in
profile, multi-tab parallel without focus stealing, readability/markdown/structured
data, policy/audit) but adopt the snapshot/ref model and a smaller default surface.

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
