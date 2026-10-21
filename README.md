# Kitsune

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/kitsune-hero.svg)

**Bring your coding agent into Firefox—and keep working alongside it.**

Kitsune lets AI agents like Claude Code, Codex, Pi, and OpenCode use the **Firefox, Zen,
or LibreWolf browser you already have open**. Your sessions, cookies, and
extensions stay in place. No Playwright, Chromium, or WebDriver required.

Research in background tabs, prepare form drafts, and continue browser tasks with
an agent you already use—not a separate browser or built-in AI assistant.

## Why Kitsune?

- **Work alongside your agent.** Agent-created tabs open in the background.
  Writes to a tab you are actively using are held, and closing your own tabs
  requires a separate confirmation step. These protections reduce interference;
  OS-level clicks and site-opened popups still have
  [focus and input limitations](docs/api-reference.md#shared-browser).
- **Choose how much to delegate.** Configure `KITSUNE_MODE=read`, `draft`, or `act`
  to limit the available tools and bridge requests. Draft mode allows supported
  field input without click or submit tools; sites may still autosave edits.
  The default is `act`, not a restricted mode. See [Task modes](docs/guide.md#task-modes).
- **Recover without blindly repeating work.** With the optional `parallel`
  capability, interrupted batch, parallel, and workflow calls can be resumed
  without resending successful writes. Uncertain writes require an observation
  before retrying. [Checkpoints](docs/checkpoint-resume.md) are temporary and
  do not survive a bridge restart.

## What can it do?

- **Research the web.** Search, read pages, and summarize findings with links.
- **Interact with websites.** Click, type, select options, and fill forms—even
  on sites you're already signed into.
- **Capture and extract.** Take screenshots or turn page content into text
  and Markdown.
- **Work across tabs.** Keep research and tasks in separate tabs without
  replacing the page you're using.

![Claude Code uses Kitsune to open Hacker News and summarize the top three stories](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/demo.gif)

<sub>A real browser session, captured with Kitsune and sped up.</sub>

## How do I use it?

1. **Connect once.** Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
   then follow the [MCP setup](#mcp-setup) for your agent below.
   [Install the browser extension](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md#2-install-the-browser-extension)
   in Firefox, Zen, or LibreWolf: the signed build installs once, while the
   developer build (needed for `browser_js`) is a temporary add-on.
2. **Keep your browser open.** Kitsune starts its shared bridge automatically.
   Ask your agent to check the connection before your first task.
3. **Ask in plain language.** For example:

   > Research these three products in new background tabs and compare them with
   > source links. Leave my current tab alone.

   > Inspect this page's form and fill a draft. Don't submit it yet.

   > Open Hacker News in a background tab and summarize the top three stories
   > with links.

   For an enforced draft-only tool surface, configure `KITSUNE_MODE=draft` before
   starting the MCP client; asking in plain language does not change the mode.

Use a dedicated browser profile and only give access to accounts the agent
needs. Ask for confirmation before sending, purchasing, or deleting anything.
The opt-in [ask-before-act](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#ask-before-act)
policy makes the bridge hold submits, payment pages, JavaScript, and cookie access
until the agent repeats the call with a confirmation token. That second step is
not proof of human approval, and these controls do not prevent prompt injection.
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

Kitsune registers a 14-tool lean surface by default. Set the environment variable
`KITSUNE_TOOL_PRESET=full` for the other 16 `core` tools: check, submit form, hover,
keypress, scroll, JavaScript, waits, dialogs, uploads, Markdown, back/forward, console
and workflows
(see [Lean tool preset](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#lean-tool-preset)).

For Codex's TOML alternative, environment variables, version pinning, and
upgrades, see [Installation](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md).

For the details: [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) ·
[Configuration](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#configuration) ·
[Troubleshooting](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#troubleshooting) ·
[Updating](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#updating-kitsune) ·
[API reference](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md) ·
[Development](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#development)

For architecture, product trade-offs, and the wider browser MCP landscape, see
[Positioning](docs/positioning.md).

---

MIT · Based on ZenLink and ZenLink-MCP by Jay-Quan.
[Credits](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#credits) · [License](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)
