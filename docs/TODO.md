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
