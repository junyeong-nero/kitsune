# Kitsune

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/kitsune-hero.png)

[![kitsune-browser-mcp on PyPI](https://img.shields.io/pypi/v/kitsune-browser-mcp?label=kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![kitsune-browser on PyPI](https://img.shields.io/pypi/v/kitsune-browser?label=kitsune-browser)](https://pypi.org/project/kitsune-browser/)
[![Python](https://img.shields.io/pypi/pyversions/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![Release](https://img.shields.io/github/v/release/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/releases/latest)
[![License: MIT](https://img.shields.io/github/license/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-junyeong--nero.github.io%2Fkitsune-orange)](https://junyeong-nero.github.io/kitsune/)

**Let your coding agent use the browser you already have open, while you keep using it too.**

Kitsune connects Claude Code, Codex, Pi, OpenCode and other MCP clients to Firefox, Zen or
LibreWolf. The agent works in background tabs of its own, signed in wherever you are signed in.
It can read your tabs, but it has to ask before it changes one. There is no Playwright,
Chromium or WebDriver to install.

![Claude Code asks for the user's Wikipedia tab after researching in background tabs; the user approves in Kitsune's toolbar popup, the agent jumps the tab to the right section, and the user takes the tab back and closes the agent's tabs from the popup](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/demo-handoff.gif)

<sub>New in 0.7.2: the agent asks for your tab, and you approve, take it back and clean up from the toolbar popup. A real recording on Wikipedia, sped up while the agent thinks (<a href="https://github.com/user-attachments/assets/532e8a65-2801-4a4c-89b4-2ec8cec7322b">full 45-second video</a>).</sub>

## Why Kitsune?

- **It works beside you.** New tabs open in the background, so the page you are on stays in
  front. Your tabs, and tabs other agents opened, are read-only to the agent: it can read them,
  but it cannot click, type, scroll, navigate or close them. If you start typing in one of the
  agent's own tabs, it waits.
- **You stay in charge.** When the agent needs one of your tabs, it asks, and you answer in
  Kitsune's toolbar popup. You can take the tab back at any time. The popup also shows whether
  the agent is connected, and closes all of the agent's tabs with one click. Tabs the agent
  leaves idle close by themselves after five minutes (you can change this in the popup).
- **The tool list is small.** The agent starts with 14 tools: the most-used Playwright MCP tools
  under the same names, `browser_ask_user` and `browser_resume` for when it needs you, and
  `browser_run_python` (trusted local Python, not a sandbox) for everything else.
  `KITSUNE_SCRIPTING=0` restores the legacy list of Playwright MCP's 25 core tools.
- **You decide what needs your approval.** Every tool is available, and you choose in the
  extension's options which actions wait for your OK first.

## Less context. Closing the gap.

**34% fewer input tokens than Playwright MCP** on the same 13 MiniWoB task/repeat cells
solved by all five arms. Kitsune's 14-tool default also closes **83% of the input-token gap
to ego lite** and **64% of the gap to Aside**, compared with its own 30-tool legacy surface
on the same source revision—not a comparison across different releases.

[![Input tokens on 13 jointly solved MiniWoB cells: Kitsune legacy 785k, Playwright MCP 781k, Kitsune default 515k, ego lite 458k, Aside 361k. Lower is better.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-efficiency.svg)](https://junyeong-nero.github.io/kitsune/benchmark/#smaller-tool-surface-five-way-miniwob-comparison)

**Nearer to one-tool browsers, without giving up the browser you already use.**
On that common set, Kitsune uses **12% more input than ego lite** and **43% more than Aside**—
they still lead on input. Across all 14 attempts per arm, Kitsune default, legacy, Playwright
and Aside each solved **14/14**; ego lite solved **13/14**. The chart excludes that failed
cell from **every** arm, rather than counting an early failure as a token saving.

<sub>Measured 2026-11-12 with Claude Opus 5.5 (medium), Kitsune <code>79eb955</code>
(pre-release 0.8.0 code), Playwright MCP 0.0.83, ego lite 0.6.2.9 and Aside CLI 1.26.1107.24.
Seven tasks × two repeats: a small exhibition, not a full-suite leaderboard. Input includes
cache hits, not billed cost. Kitsune made zero Python calls here: this measures the smaller
tool/prompt surface, not Python batching. Browser engines/profiles differ; page-JS code was
emitted in eight ego attempts and one Aside attempt. No equal-environment, safety or general
speed claim. <a href="https://junyeong-nero.github.io/kitsune/benchmark/#smaller-tool-surface-five-way-miniwob-comparison">Numbers, methodology and limitations →</a></sub>

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

Kitsune starts its local bridge on its own. If the agent cannot connect, ask it to check the
connection: the error's `hint` says what to fix.

Use a separate browser profile for agent work and sign in only to the accounts it needs.
Ask the agent to check with you before it sends, buys or deletes anything. Nothing in Kitsune
stops prompt injection, so read the [security guide](https://junyeong-nero.github.io/kitsune/security/)
before using sensitive accounts.

## Documentation

The full documentation is at **[junyeong-nero.github.io/kitsune](https://junyeong-nero.github.io/kitsune/)**:

- [Installation](https://junyeong-nero.github.io/kitsune/installation/): every MCP client, version pinning, upgrades
- [Your tabs and approvals](https://junyeong-nero.github.io/kitsune/your-tabs/): what limits the agent: protected tabs, per-tool approvals, ask-before-act
- [Tools](https://junyeong-nero.github.io/kitsune/tools/): the minimal and legacy tool lists, and the tool groups
- [Python API](https://junyeong-nero.github.io/kitsune/python-api/): drive the browser from your own scripts with `pip install kitsune-browser`
- [Troubleshooting](https://junyeong-nero.github.io/kitsune/troubleshooting/) ·
  [FAQ](https://junyeong-nero.github.io/kitsune/faq/) ·
  [Benchmark and comparison](https://junyeong-nero.github.io/kitsune/benchmark/) ·
  [API reference](https://junyeong-nero.github.io/kitsune/api-reference/)
