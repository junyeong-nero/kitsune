# Kitsune

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/docs/kitsune-hero.svg)

**Your browser. Your agent. One clever bridge.**

Kitsune lets AI agents like Claude Code, Codex, Pi, and OpenCode use the **Firefox, Zen,
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

![Claude Code uses Kitsune to open Hacker News and summarize the top three stories](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/docs/demo.gif)

<sub>A real browser session, captured with Kitsune and sped up.</sub>

## How do I use it?

1. **Connect once.** Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
   then follow the [MCP setup](#mcp-setup) for your agent below.
   [Load the browser extension](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md#2-install-the-browser-extension)
   in Firefox, Zen, or LibreWolf. The extension is currently a temporary add-on;
   load it again after each browser restart.
2. **Keep your browser open.** Kitsune starts its shared bridge automatically.
   Ask your agent to check the connection before your first task.
3. **Ask in plain language.** For example:

   > Open a new tab, search for today's weather near Suseong Lake in Daegu,
   > and summarize the forecast with a link to the source.

   > Inspect this page's form and fill a draft. Don't submit it yet.

   > Open Hacker News and summarize the top three stories with links.

Use a dedicated browser profile and only give access to accounts the agent
needs. Ask for confirmation before sending, purchasing, or deleting anything.
Read the [security guidance](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#security) before using sensitive accounts.

## MCP setup

All clients launch the same local **stdio** server:
`uvx --from kitsune-browser-mcp kitsune`. Choose **one** setup method for each
client you use. Several clients can share the same bridge; keep their Kitsune
versions and install sources consistent.

### CLI registration: Claude Code, Codex, and Pi

**Claude Code** — available in every project with `--scope user`:

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

**Codex CLI** — registers the server in `~/.codex/config.toml`:

```bash
codex mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
```

**Pi** — uses [built-in MCP support](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)
with user-level configuration by default:

```bash
pi mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
pi mcp list
```

Add `--local` before `kitsune` to register Pi's server only for the current
project. In an already running Pi session, run `/reload`, then `/mcp` to inspect
the connection. If an MCP adapter extension overrides Pi's built-in support,
follow that extension's configuration instructions instead.

### OpenCode

Merge this into `~/.config/opencode/opencode.json` (global) or `opencode.json`
in your project root. OpenCode uses an `mcp` object and a command array, not
`mcpServers`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kitsune": {
      "type": "local",
      "command": ["uvx", "--from", "kitsune-browser-mcp", "kitsune"],
      "enabled": true
    }
  }
}
```

### JSON configuration: Pi, Claude Desktop, and Cursor

These clients share the same `mcpServers` format. As an alternative to CLI
registration where available, merge the following entry into your client's
config; keep any existing servers:

| Client | Config file |
| --- | --- |
| Pi (built-in MCP) | `~/.pi/agent/mcp.json` (user) or `.pi/mcp.json` (project; requires project trust) |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) |

```json
{
  "mcpServers": {
    "kitsune": {
      "command": "uvx",
      "args": ["--from", "kitsune-browser-mcp", "kitsune"]
    }
  }
}
```

Restart or reconnect your client after configuring it, then ask it to call
`browser_status`. With the browser extension loaded, the response should include
`"extension_connected": true`. GUI apps may need the full path returned by
`which uvx` instead of `uvx` if they do not inherit your shell's `PATH`.

For Codex's TOML alternative, environment variables, version pinning, and
upgrades, see [Installation](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md).

For the details: [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) ·
[Configuration](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#configuration) ·
[Troubleshooting](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#troubleshooting) ·
[Updating](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#updating-kitsune) ·
[API reference](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md) ·
[Development](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#development)

For architecture and product trade-offs, see
[Browser MCP comparisons](docs/browser-mcp-comparison.md) and the detailed
[ExecuteAutomation comparison](docs/positioning-vs-executeautomation.md).

---

MIT · Based on ZenLink and ZenLink-MCP by Jay-Quan.
[Credits](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#credits) · [License](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)
