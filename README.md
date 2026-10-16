# Kitsune

Let AI agents (Claude Code, Codex, Pi, ...) drive the Firefox-family browser you
already use: Firefox, Zen, LibreWolf. Your logged-in sessions stay as they are.
No Playwright, no Chromium, no WebDriver: a WebExtension does the work.

```
agent ─stdio→ kitsune (MCP) ─HTTP+token→ bridge daemon ─WebSocket→ extension → tabs
```

- The MCP server starts the bridge daemon on demand. Every agent shares one daemon.
- The HTTP API needs a bearer token from `~/.kitsune/token` (0600). No CORS.
- The WebSocket accepts only `moz-extension://` origins, so web pages cannot
  pose as the extension.
- Tokens, saved sessions (cookies), screenshots and logs live in `~/.kitsune/` (0700).

## Setup

1. Register the MCP server:
   ```bash
   claude mcp add -s user kitsune -- uv run --directory ~/workspace/kitsune kitsune
   ```
2. Load the extension: open `about:debugging#/runtime/this-firefox`,
   click **Load Temporary Add-on…**, pick `extension/manifest.json`.
   Temporary add-ons are removed when the browser restarts.
3. Ask the agent to call `browser_status`.

Ports: HTTP `47651`, WebSocket `47652` (`KITSUNE_HTTP_PORT`, `KITSUNE_WS_PORT`).
Data directory: `KITSUNE_HOME` (default `~/.kitsune`).

## Security

The extension can read cookies and run JavaScript on every site. A page the
agent visits can try prompt injection. Use a separate browser profile with only
the accounts the agent needs.

## Credits

Based on [ZenLink](https://github.com/JayQuan-McCleary/ZenLink) and
[ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) by Jay-Quan (MIT).
Changes: token auth, origin and Host checks, no CORS, on-demand shared daemon,
single package, `browser_*` tool names, macOS support.
