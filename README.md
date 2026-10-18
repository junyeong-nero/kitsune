# Kitsune

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](docs/kitsune-hero.svg)

**Your browser. Your agent. One clever bridge.**

Kitsune lets AI agents like Claude Code, Codex, and Pi use the **Firefox, Zen,
or LibreWolf browser you already have open**. Your sessions, cookies, and
extensions stay in place. No Playwright, Chromium, or WebDriver required.

## What can it do?

- **Research the web.** Search, read pages, and summarize findings with links.
- **Interact with websites.** Click, type, select options, and fill forms—even
  on sites you're already signed into.
- **Capture and extract.** Take screenshots or turn page content into text
  and Markdown.
- **Work across tabs.** Keep research and tasks in separate tabs without
  replacing the page you're using.

![Claude Code uses Kitsune to open Hacker News and summarize the top three stories](docs/demo.gif)

<sub>A real browser session, captured with Kitsune and sped up.</sub>

## How do I use it?

1. **Connect once.** Register Kitsune with your agent—for Claude Code, one line:

   ```bash
   claude mcp add --scope user kitsune -- uvx --from git+https://github.com/junyeong-nero/kitsune.git kitsune
   ```

   Then [load the browser extension](docs/installation.md#2-install-the-browser-extension).
   You'll need a supported Firefox-family browser, uv, and Git. For other
   agents, version pinning, and upgrades, see [Installation](docs/installation.md).
2. **Keep your browser open.** Kitsune starts its shared bridge automatically.
   Ask your agent to check the connection before your first task.
3. **Ask in plain language.** For example:

   > Open a new tab, search for today's weather near Suseong Lake in Daegu,
   > and summarize the forecast with a link to the source.

   > Inspect this page's form and fill a draft. Don't submit it yet.

   > Open Hacker News and summarize the top three stories with links.

Use a dedicated browser profile and only give access to accounts the agent
needs. Ask for confirmation before sending, purchasing, or deleting anything.
Read the [security guidance](docs/guide.md#security) before using sensitive accounts.

For the details: [Tools](docs/guide.md#tools) ·
[Configuration](docs/guide.md#configuration) ·
[Troubleshooting](docs/guide.md#troubleshooting) ·
[Updating](docs/guide.md#updating-kitsune) ·
[API reference](docs/api-reference.md) ·
[Development](docs/guide.md#development)

---

MIT · Based on ZenLink and ZenLink-MCP by Jay-Quan.
[Credits](docs/guide.md#credits) · [License](LICENSE)
