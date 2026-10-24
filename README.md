# Kitsune

[![PyPI](https://img.shields.io/pypi/v/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![License: MIT](https://img.shields.io/github/license/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/kitsune-hero.svg)

**Bring your coding agent into Firefox—and keep working alongside it.**

Kitsune lets AI agents like Claude Code, Codex, Pi, and OpenCode use the **Firefox, Zen,
or LibreWolf browser you already have open**. Your sessions, cookies, and
extensions stay in place. No Playwright, Chromium, or WebDriver required.

Research in background tabs, prepare form drafts, and continue browser tasks with
an agent you already use—not a separate browser or built-in AI assistant.

Two principles guide every trade-off: **the user's browser belongs to the user**,
and **fewer, sharper tools**. See [Philosophy](https://github.com/junyeong-nero/kitsune/blob/main/docs/philosophy.md).

## Why Kitsune?

- **Work alongside your agent.** Agent-created tabs open in the background.
  Writes to a tab you are actively using are held, and closing your own tabs
  requires a separate confirmation step. These protections reduce interference;
  OS-level clicks and site-opened popups still have
  [focus and input limitations](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md#shared-browser).
- **Choose how much to delegate.** Configure `KITSUNE_MODE=read`, `draft`, or `act`
  to limit the available tools and bridge requests. Draft mode allows supported
  field input without click or submit tools; sites may still autosave edits.
  The default is `act`, not a restricted mode. See [Task modes](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#task-modes).
- **Keep the tool list small.** Start with Playwright MCP's 25 core tools. Kitsune's
  other tools stay off until you allow them, and the agent can find and call the ones
  you allow without restarting its session. See [Choosing tools](https://github.com/junyeong-nero/kitsune#choosing-tools).
- **Recover without blindly repeating work.** With the optional `automation`
  capability, interrupted batch, parallel, and workflow calls can be resumed
  without resending successful writes. Uncertain writes require an observation
  before retrying. [Checkpoints](https://github.com/junyeong-nero/kitsune/blob/main/docs/checkpoint-resume.md) are temporary and
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

## How is it different?

Kitsune is one of several ways to connect an agent to a browser. Choose by the
browser you use and the work you want to hand off:

| If you want to… | Consider |
| --- | --- |
| Delegate tasks in the Firefox-family browser you are using, with background tabs, read/draft/act modes, and resumable batches | **Kitsune** |
| Debug or profile pages in Firefox (debugger, profiler, deep protocol access) | [Mozilla Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp), which attaches to a running Firefox started with remote-debugging flags |
| Test across Chromium, Firefox, and WebKit in launched browsers | [Playwright MCP](https://github.com/microsoft/playwright-mcp); Kitsune publishes its 25 core tools, so prompts written for it carry over |
| Drive the Chrome you already use | A Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) |

Kitsune connects through an extension and a local bridge, so Firefox needs no
startup flags or WebDriver. Other Firefox extension projects also reuse your
logins; Kitsune's focus is working alongside you in the same browser. For the
full comparison and its limits, see
[Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md).

## How do I use it?

1. **Connect once.** Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
   then follow the [MCP setup](#mcp-setup) for your agent below.
   [Install the browser extension](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md#2-install-the-browser-extension)
   in Firefox, Zen, or LibreWolf: the signed build installs once, while the
   developer build (needed for `browser_evaluate`) is a temporary add-on.
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

Restart or reconnect your client after configuring it, then ask it to list your tabs
(`browser_tabs` with `action: "list"`); an error's `hint` says what to fix. GUI apps may need the full path returned by
`which uvx` instead of `uvx` if they do not inherit your shell's `PATH`.

### Choosing tools

By default Kitsune publishes exactly [Playwright MCP](https://github.com/microsoft/playwright-mcp)'s
25 core tools (same names, descriptions and schemas), driving your own browser, so
prompts and clients written for Playwright MCP work unchanged
([Core tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#core-tools)). Kitsune's own tools come in capability groups that
you enable with `KITSUNE_CAPS` in the MCP server's environment:

| Group | Tools for |
| --- | --- |
| `browse` | Scrolling, forward navigation, checkboxes, focus, form submits, trusted OS clicks |
| `read` | Page text and Markdown, forms, DOM, links, images, metadata |
| `inspect` | Element bounds, computed styles, selector diagnosis, highlights |
| `tabs` | Pinning, muting, zoom, windows, tab tags, keep-alive |
| `automation` | Batches, retries, workflows, parallel tabs, resume |
| `network` | Downloads, request interception and capture |
| `admin` | Connection status, logs, audit, cache, policy |
| `storage`, `history` | Cookies, page storage, clipboard and saved sessions; history and bookmarks. They read personal data, so `all` leaves them out: name them |

To keep the agent's tool list small, allow more than you publish. With
`KITSUNE_CAPS=all` and `KITSUNE_PUBLISH=core`, the agent starts with the core tools
and finds and calls the others in the same session
([Finding tools without a restart](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#finding-tools-without-a-restart)). For
example, with Claude Code:

```bash
claude mcp add --scope user kitsune -e KITSUNE_CAPS=all -e KITSUNE_PUBLISH=core -- uvx --from kitsune-browser-mcp kitsune
```

In the JSON configurations above, add `"env": {"KITSUNE_CAPS": "all", "KITSUNE_PUBLISH": "core"}`
next to `"args"`. The bridge checks the groups on every request, batch steps included,
and tools you call without a tab act on the agent's current tab, not the one you are
looking at.

Upgrading from 0.3? The `devtools`, `parallel`, `windows` and `session` groups were
replaced by the groups above, and `all` no longer includes `storage`. A configuration
that names a removed group fails at startup; see [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) for where each
tool went.

For Codex's TOML alternative, environment variables, version pinning, and
upgrades, see [Installation](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md).

For the details: [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) ·
[Configuration](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#configuration) ·
[Troubleshooting](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#troubleshooting) ·
[Updating](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#updating-kitsune) ·
[API reference](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md) ·
[Development](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#development)

For architecture, product trade-offs, and the wider browser MCP landscape, see
[Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md).

---

MIT · Based on ZenLink and ZenLink-MCP by Jay-Quan.
[Credits](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#credits) · [License](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)
