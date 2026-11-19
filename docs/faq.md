# FAQ

## How is it different from Playwright MCP?

Playwright MCP launches its own browser, which suits testing. Kitsune attaches to the Firefox,
Zen or LibreWolf window you already use and shares it with you: agent tabs open in the
background and your tabs are read-only to the agent. With `KITSUNE_SCRIPTING=0` it publishes
Playwright MCP's 25 core tools unchanged, so prompts carry over. See the
[benchmark](benchmark.md).

## How is it different from Firefox DevTools MCP?

[Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp) is better for debugging
and profiling and needs a Firefox started with remote-debugging flags. Kitsune uses an extension,
so your normal profile needs no flags or WebDriver.

## Does it work with Chrome?

Not yet. For Chrome, try a Chromium extension MCP such as
[Browser MCP](https://github.com/BrowserMCP/mcp).

## Which agents can use it?

Any MCP client that runs local stdio servers. See
[Register the MCP server](installation.md#1-register-the-mcp-server).

## Will the agent take over the tab I'm using?

No. Its tabs open in the background, and selecting a tab doesn't change what you see. It can
read every tab, but writes, navigation, scrolling and closing on your tabs fail with
`user_tab_read_only` unless you hand the tab over. OS-level clicks and site popups can still
take focus ([Shared browser](api-reference.md#shared-browser)).

## Can I limit what the agent does?

- Choose which tools need approval in the extension's options (cookies, storage, clipboard and
  history do by default).
- Your tabs stay read-only unless `KITSUNE_USER_TABS=writable`.
- [Ask-before-act](your-tabs.md#ask-before-act) holds submits, payment pages, JavaScript and
  cookie access until you approve.

## How do I approve a held action?

Clients with elicitation (Claude Code, Codex) ask you in their own prompt. Others fall back to a
`confirm_token`: the agent asks you, then repeats the call with it. See
[Approving held actions](your-tabs.md#approving-held-actions).

## Is it safe to give an agent my browser?

Treat it as giving the agent your signed-in accounts. Nothing in Kitsune prevents prompt
injection. Use a dedicated profile with only the accounts the task needs. See
[Security](security.md).

## Does my data leave my machine?

The bridge listens only on `127.0.0.1` and requires a token. Page content the agent reads goes
to your agent and its model provider.

## Do I need the developer build?

Only for agent JavaScript (`browser_evaluate`). The signed build installs once and updates
itself. See [Installation](installation.md#2-install-the-browser-extension).

## Which platforms are supported?

macOS and Linux. Trusted OS clicks work on macOS and on Linux under X11.

## Why can't the agent open "Continue with Google"?

Firefox blocks sign-in popups from synthetic clicks. Have the agent ask you to click, or use
`browser_trusted_click`. See [Sign-in popups](troubleshooting.md#sign-in-popups-oauthsso).

## Is this kitsune-mcp?

No, that is an unrelated project. This one is
[`kitsune-browser-mcp`](https://pypi.org/project/kitsune-browser-mcp/) on PyPI and
`io.github.junyeong-nero/kitsune` in the MCP Registry.
