# Kitsune

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/kitsune-hero.png)

[![kitsune-browser-mcp on PyPI](https://img.shields.io/pypi/v/kitsune-browser-mcp?label=kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![kitsune-browser on PyPI](https://img.shields.io/pypi/v/kitsune-browser?label=kitsune-browser)](https://pypi.org/project/kitsune-browser/)
[![Python](https://img.shields.io/pypi/pyversions/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![Release](https://img.shields.io/github/v/release/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/releases/latest)
[![License: MIT](https://img.shields.io/github/license/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-junyeong--nero.github.io%2Fkitsune-orange)](https://junyeong-nero.github.io/kitsune/)

**Delegate browser tasks. Keep your browser yours.**

Kitsune connects Claude Code, Codex, Pi and other MCP clients to the Firefox, Zen or LibreWolf
browser you already use. It's built for personal browser tasks—researching products, comparing
pages, and drafting forms—rather than cross-browser testing. Your agent opens its own background
tabs while you keep working in yours.

![Claude Code asks for the user's Wikipedia tab after researching in background tabs; the user approves in Kitsune's toolbar popup, the agent jumps the tab to the right section, and the user takes the tab back and closes the agent's tabs from the popup](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/demo-handoff.gif)

<sub>Your agent asks for a tab. You approve, then take it back. <a href="https://github.com/user-attachments/assets/532e8a65-2801-4a4c-89b4-2ec8cec7322b">Watch the 45-second demo →</a></sub>

## Why Kitsune?

I built Kitsune after getting tired of switching back to my tab whenever my Playwright setup
took focus, and opening Chrome for my agent when I already used a Firefox-based browser.
I wanted browser automation that worked alongside me, not in my way.

- **Keep working while the agent works.** Agent-created tabs open in the background. Selecting a tab for the agent doesn't switch the tab you see.
- **Your tabs stay yours.** By default, the agent can read your tabs but cannot type, navigate, scroll or close them. It must request a handoff before changing one; you approve and can take it back.
- **Stay in Firefox, Zen or LibreWolf.** Reuse your existing sessions without opening a separate Chrome browser or installing WebDriver.
- **A small tool surface.** Six tools on `main`, with Python for browser actions and direct controls for user help and recovery. The 0.8.0 release has 14.

Kitsune is designed to reduce interruptions, not eliminate them: site-opened popups and
OS-level trusted input can still move focus. See the
[shared-browser limits](https://junyeong-nero.github.io/kitsune/api-reference/#shared-browser).

## Less input. Broader tasks.

**The 6-tool Kitsune used the least total input in this four-way comparison.**
23 personal and UI conditions, two repeats; all four configurations completed **46/46 attempts**.

[![Cache-inclusive input on the same 46 condition/repeat cells per arm: Kitsune 6-tool 5,730,439; Kitsune previous 14-tool 5,901,946; Aside 5,923,875; EgoLite 7,775,055. Lower is better; all four arms succeeded on 46/46 attempts.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-efficiency.svg)](https://junyeong-nero.github.io/kitsune/benchmark/)

<sub>Codex gpt-6.1-sol, medium. Input includes cache, not billed cost. Browser environments and permissions differ.
6-tool adjusted time was 4.8% higher than 14-tool Kitsune and 6.6% higher than Aside; results vary by task.
<a href="https://junyeong-nero.github.io/kitsune/benchmark/">Results, methodology and limitations →</a></sub>

## Get started

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. [Install the browser extension](https://junyeong-nero.github.io/kitsune/installation/#2-install-the-browser-extension)
   in Firefox, Zen or LibreWolf.
3. Add Kitsune to your agent. For Claude Code:

   ```bash
   claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
   ```

   Codex, Pi, OpenCode, Claude Desktop and Cursor are in
   [Installation](https://junyeong-nero.github.io/kitsune/installation/#1-register-the-mcp-server).
4. Keep the browser open and ask in plain language:

   > Research these three products in background tabs and compare them with links.
   > Leave my current tab alone.

   > Fill in a draft of this form. Don't submit it.

**Trust note:** Python runs locally, not in a sandbox; set `KITSUNE_SCRIPTING=0` to disable it.
Approvals do not prevent prompt injection. Use a separate profile for sensitive accounts and
read the [security guide](https://junyeong-nero.github.io/kitsune/security/).

## Documentation

The full documentation is at **[junyeong-nero.github.io/kitsune](https://junyeong-nero.github.io/kitsune/)**:

- [Installation](https://junyeong-nero.github.io/kitsune/installation/): every MCP client, version pinning, upgrades
- [Your tabs and approvals](https://junyeong-nero.github.io/kitsune/your-tabs/): what limits the agent: protected tabs, per-tool approvals, ask-before-act
- [Tools](https://junyeong-nero.github.io/kitsune/tools/): the minimal and legacy tool lists, and the tool groups
- [Python API](https://junyeong-nero.github.io/kitsune/python-api/): drive the browser from your own scripts with `pip install kitsune-browser`
- [Troubleshooting](https://junyeong-nero.github.io/kitsune/troubleshooting/) ·
  [FAQ](https://junyeong-nero.github.io/kitsune/faq/) ·
  [Benchmarks](https://github.com/junyeong-nero/kitsune-benchmark) ·
  [API reference](https://junyeong-nero.github.io/kitsune/api-reference/)
