# Kitsune

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

![Kitsune: an orange fox in a browser window, connecting your browser and your AI agent](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/kitsune-hero.svg)

[![PyPI](https://img.shields.io/pypi/v/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/kitsune-browser-mcp)](https://pypi.org/project/kitsune-browser-mcp/)
[![Release](https://img.shields.io/github/v/release/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/releases/latest)
[![License: MIT](https://img.shields.io/github/license/junyeong-nero/kitsune)](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE)

**Bring your coding agent into Firefox and keep working alongside it.**

Kitsune lets AI agents such as Claude Code, Codex, Pi, and OpenCode use the Firefox,
Zen, or LibreWolf browser you already have open. Your sessions, cookies, and
extensions stay where they are, and there is no Playwright, Chromium, or WebDriver
to install.

The agent can research in background tabs and prepare form drafts while you keep
using the same browser. You stay with the agent you already use; Kitsune is not a
separate browser or a built-in assistant.

The design follows two principles: the user's browser belongs to the user, and
the agent gets fewer, sharper tools. See [Philosophy](https://github.com/junyeong-nero/kitsune/blob/main/docs/philosophy.md).

![Claude Code uses Kitsune to search Google Flights and Google Maps in background Zen tabs for a weekend in Tokyo, booking nothing, while the user's Google tab stays in front](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/demo.gif)

<sub>A real screen recording of Claude Code and Zen Browser on the live Google sites, sped up while the agent thinks and zoomed in afterwards (<a href="https://github.com/user-attachments/assets/9b61f335-073d-4035-b479-ee03e96ae202">full 38-second video</a>).</sub>

## Why Kitsune?

- Tabs the agent creates open in the background. Kitsune holds writes to a tab
  you are actively using, and closing one of your own tabs takes a separate
  confirmation step. This reduces interference, but OS-level clicks and popups
  opened by sites still have [focus and input limitations](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md#shared-browser).
- You decide how much to delegate. `KITSUNE_MODE=read`, `draft`, or `act` limits
  both the tools the agent sees and the requests the bridge accepts. Draft mode
  lets the agent fill supported fields without click or submit tools, although
  sites may still autosave what it types. The default, `act`, has no such
  restriction. See [Task modes](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#task-modes).
- The agent starts with Playwright MCP's 25 core tools. Kitsune's other tools
  stay off until you allow them, and the agent can find and call the allowed ones
  without restarting its session. See [Choosing tools](https://github.com/junyeong-nero/kitsune#choosing-tools).
- With the optional `automation` capability, an interrupted batch, parallel, or
  workflow call can be resumed without resending the writes that already
  succeeded. A write with an uncertain outcome needs an observation before it is
  retried. [Checkpoints](https://github.com/junyeong-nero/kitsune/blob/main/docs/checkpoint-resume.md) are temporary and are lost when the bridge restarts.

## What can it do?

In your browser, the agent can:

- Search the web, read pages, and summarize what it finds with links.
- Click, type, select options, and fill forms, including on sites where you are
  already signed in.
- Take screenshots or turn page content into text and Markdown.
- Keep research and tasks in separate tabs without replacing the page you are
  using.

## Benchmark

![Kitsune v0.4.0 vs Playwright MCP 0.0.83: kept the user's tab in front in 23 of 24 tasks vs 2 of 24; 0.83 to 0.93 times the cost on tasks both solved; 191 of 198 vs 197 of 198 tasks solved](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark.png)

With the same tasks and tool allowlist, Kitsune v0.4.0 kept the user's tab in
front in 23 of 24 shared-browser tasks, against 2 of 24 for Playwright MCP. On
the tasks both solved, Kitsune cost 0.83 to 0.93 times as much. It solved 191 of
198 tasks, and Playwright MCP solved 197. Each task was run once with Claude Opus
5.5. Playwright MCP was not designed to share a browser with a person. See the
[full reports](https://github.com/junyeong-nero/kitsune-benchmark/tree/main/docs/results).

## How is it different?

Kitsune is one of several ways to connect an agent to a browser. Which one fits
depends on the browser you use and the work you want to hand off:

| If you want to… | Consider |
| --- | --- |
| Delegate tasks in the Firefox-family browser you are using, with background tabs, read/draft/act modes, and resumable batches | **Kitsune** |
| Debug or profile pages in Firefox (debugger, profiler, deep protocol access) | [Mozilla Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp), which attaches to a running Firefox started with remote-debugging flags |
| Test across Chromium, Firefox, and WebKit in launched browsers | [Playwright MCP](https://github.com/microsoft/playwright-mcp); Kitsune publishes its 25 core tools, so prompts written for it carry over |
| Drive the Chrome you already use | A Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) |

Kitsune connects through an extension and a local bridge, so Firefox needs no
startup flags or WebDriver. Other Firefox extension projects also reuse your
logins. Kitsune concentrates on sharing the browser with you while the agent
works. [Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md)
has the full comparison and its limits.

## How do I use it?

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
   follow the [MCP setup](#mcp-setup) for your agent below.
   [Install the browser extension](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md#2-install-the-browser-extension)
   in Firefox, Zen, or LibreWolf. The signed build stays installed; the
   developer build, which `browser_evaluate` needs, is a temporary add-on.
2. Keep the browser open. Kitsune starts its shared bridge automatically. Before
   your first task, ask the agent to check the connection.
3. Ask in plain language. For example:

   > Research these three products in new background tabs and compare them with
   > source links. Leave my current tab alone.

   > Inspect this page's form and fill a draft. Don't submit it yet.

   > Open Hacker News in a background tab and summarize the top three stories
   > with links.

   Asking in plain language does not change the mode. To enforce a draft-only
   tool set, set `KITSUNE_MODE=draft` before starting the MCP client.

Use a dedicated browser profile, and give the agent access only to the accounts
it needs. Have it ask you before it sends, buys, or deletes anything. The opt-in
[ask-before-act](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#ask-before-act)
policy makes the bridge hold submits, payment pages, JavaScript, and cookie access
until the agent repeats the call with a confirmation token. The agent can make
that second call on its own, so it does not prove that a person approved, and
none of these controls prevent prompt injection. Read the [security guidance](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#security) before using sensitive accounts.

## MCP setup

Every client launches the same local stdio server:
`uvx --from kitsune-browser-mcp kitsune`. Use only one setup method per client.
Several clients can share one bridge; keep them on the same Kitsune version and
install source.

### CLI registration: Claude Code, Codex, and Pi

**Claude Code**, available in every project with `--scope user`:

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

**Codex CLI**, which registers the server in `~/.codex/config.toml`:

```bash
codex mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
```

**Pi** has [built-in MCP support](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)
and registers servers for your user by default:

```bash
pi mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
pi mcp list
```

To register the server only for the current project, add `--local` before
`kitsune`. In a Pi session that is already running, run `/reload`, then `/mcp` to
check the connection. If an MCP adapter extension replaces Pi's built-in support,
follow that extension's instructions instead.

### OpenCode

Merge this into `~/.config/opencode/opencode.json` (global) or `opencode.json`
in your project root. OpenCode uses an `mcp` object with a command array, not
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

These clients use the same `mcpServers` format. Instead of CLI registration,
where a client has it, you can merge this entry into the client's config file
and keep any servers already there:

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

After configuring a client, restart or reconnect it and ask it to list your tabs
(`browser_tabs` with `action: "list"`). If that fails, the error's `hint` says
what to fix. A GUI app that does not inherit your shell's `PATH` may need the
full path from `which uvx` in place of `uvx`.

### Choosing tools

By default Kitsune publishes [Playwright MCP](https://github.com/microsoft/playwright-mcp)'s
25 core tools with the same names, descriptions, and schemas, and they drive your
own browser. Prompts and clients written for Playwright MCP work unchanged
([Core tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#core-tools)). Kitsune's own tools are sorted into capability groups,
which you enable with `KITSUNE_CAPS` in the MCP server's environment:

| Group | Tools for |
| --- | --- |
| `browse` | Scrolling, forward navigation, checkboxes, focus, form submits, trusted OS clicks |
| `read` | Page text and Markdown, forms, DOM, links, images, metadata |
| `inspect` | Element bounds, computed styles, selector diagnosis, highlights |
| `tabs` | Pinning, muting, zoom, windows, tab tags, keep-alive |
| `automation` | Batches, retries, workflows, parallel tabs, resume |
| `network` | Downloads, request interception and capture |
| `admin` | Connection status, logs, audit, cache, policy |
| `storage`, `history` | Cookies, page storage, clipboard and saved sessions; history and bookmarks. They read personal data, so `all` leaves them out and you enable them by name |

You can allow more tools than you publish, which keeps the agent's starting tool
list small. With `KITSUNE_CAPS=all` and `KITSUNE_PUBLISH=core`, the agent starts
with the core tools and can find and call the others in the same session
([Finding tools without a restart](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#finding-tools-without-a-restart)). With Claude Code, for
example:

```bash
claude mcp add --scope user kitsune -e KITSUNE_CAPS=all -e KITSUNE_PUBLISH=core -- uvx --from kitsune-browser-mcp kitsune
```

In the JSON configurations above, add `"env": {"KITSUNE_CAPS": "all", "KITSUNE_PUBLISH": "core"}`
next to `"args"`. The bridge checks the groups on every request, including batch
steps. A tool called without a tab acts on the agent's current tab, not the one
you are looking at.

If you are upgrading from 0.3: the `devtools`, `parallel`, `windows` and `session`
groups were replaced by the groups above, and `all` no longer includes `storage`.
A configuration that names a removed group fails at startup. [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) lists
where each tool went.

[Installation](https://github.com/junyeong-nero/kitsune/blob/main/docs/installation.md) covers Codex's TOML configuration, environment
variables, version pinning, and upgrades.

More documentation: [Tools](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#tools) ·
[Configuration](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#configuration) ·
[Troubleshooting](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#troubleshooting) ·
[Updating](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#updating-kitsune) ·
[API reference](https://github.com/junyeong-nero/kitsune/blob/main/docs/api-reference.md) ·
[Python API](https://github.com/junyeong-nero/kitsune/blob/main/docs/python-api.md) ·
[Development](https://github.com/junyeong-nero/kitsune/blob/main/docs/guide.md#development) ·
[FAQ](https://github.com/junyeong-nero/kitsune/blob/main/docs/faq.md)

The architecture, product trade-offs, and comparisons with other browser MCP
servers are in [Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md).

## Python API

The same extension and bridge also work from your own code. The package the MCP
server ships in (`pip install kitsune-browser-mcp`) is a Python API with every
capability group, storage and history included:

```python
import kitsune

browser = kitsune.connect()                        # starts the bridge if none runs
tab = browser.new_tab("https://news.ycombinator.com")   # a background tab you don't see
for link in tab.query(".titleline > a", fields=["text", "href"], limit=3):
    print(link["text"], link["href"])
tab.close()
```

The MCP server limits what an agent sees; the Python API does not, but the bridge's
shared-browser rules hold for both: tabs open in the background, your own tabs close
only with your approval, and sensitive fields stay redacted. `kitsune.connect(confirm=...)`
lets a person approve each action that needs it. See [Python API](https://github.com/junyeong-nero/kitsune/blob/main/docs/python-api.md).
