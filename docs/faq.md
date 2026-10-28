# FAQ

Short answers to questions people ask about Kitsune, with links to the details.

## How is it different from Playwright MCP?

Playwright MCP launches and drives its own browser, which suits testing and automation. Kitsune
attaches to the Firefox, Zen or LibreWolf window you already use and is built to share it with
you: agent tabs open in the background, writes to the tab you are using are held, and closing
your tabs needs a separate confirmation step.

By default Kitsune publishes Playwright MCP's 25 core tools with the same names, descriptions and
schemas, so prompts written for Playwright MCP carry over. On shared-browser tasks it kept the
user's tab in front in 23 of 24 tasks, against 2 of 24 for Playwright MCP, which was not designed
to share a browser with a person. See the [benchmark](https://github.com/junyeong-nero/kitsune-benchmark/tree/main/docs/results)
and [Positioning](positioning.md).

## How is it different from Mozilla's Firefox DevTools MCP?

[Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp) is the stronger choice for
debugging and profiling, and attaches to a Firefox started with remote-debugging flags. Kitsune
connects through an extension and a local bridge, so your normal profile needs no startup flags
or WebDriver, and it concentrates on working alongside you rather than on developer tooling.

## Does it work with Chrome or other Chromium browsers?

Not yet. A Chromium version is planned, probably as a separate project. For Chrome today, a
Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) fits better.

## Which agents can use it?

Any MCP client that runs local stdio servers. The README has setup for Claude Code, Codex, Pi,
OpenCode, Claude Desktop and Cursor: [MCP setup](https://github.com/junyeong-nero/kitsune#mcp-setup).

## Will the agent take over the tab I'm using?

Tabs it opens with `browser_tabs` `new` stay in the background, and selecting a tab changes only
the agent's current tab, not what you see. Until the agent opens or selects a tab, its reads act
on the tab you are viewing, and its writes and navigations that name no tab fail with
`no_current_tab`. It can still name or select your tab, so ask it to work in new tabs, or set
`KITSUNE_PROTECT_USER_TABS=1` to refuse its navigations on tabs it did not open. OS-level clicks and popups that sites open can
still take focus. See [Shared browser](api-reference.md#shared-browser) and
[Protecting your tabs](guide.md#protecting-your-tabs).

## Can I limit what the agent does?

Yes, in three ways that combine:

- `KITSUNE_MODE=read`, `draft` or `act` limits both the tools the agent sees and the requests the
  bridge accepts. Draft fills supported fields but has no click or submit tools. See
  [Task modes](guide.md#task-modes).
- `KITSUNE_CAPS` enables tool groups; the default is the 25 core tools. Groups that read personal
  data (`storage`, `history`) must be named. See [Choosing tools](https://github.com/junyeong-nero/kitsune#choosing-tools).
- [Ask-before-act](guide.md#ask-before-act) holds submits, payment pages, JavaScript and cookie
  access until the agent repeats the call with a confirmation token.

## Is it safe to give an agent my browser?

Treat it as giving the agent your signed-in accounts. None of Kitsune's controls prevent prompt
injection: a page can contain text that steers the agent. The confirmation token proves a second
step, not that a person approved. Use a dedicated browser profile, give it only the accounts the
task needs, and have the agent ask you before it sends, buys or deletes anything. See
[Security](guide.md#security).

## Does my data leave my machine?

The bridge listens only on `127.0.0.1`, requires a token, and accepts the WebSocket only from the
extension. Page content you let the agent read goes to your agent, and from there to whichever
model provider that agent uses.

## Do I need the developer build of the extension?

Only for agent-supplied JavaScript (`browser_evaluate`). The signed build installs once and
updates itself; the developer build is a temporary add-on that you load again after a browser
restart. See [Installation](installation.md#2-install-the-browser-extension).

## Which platforms are supported?

macOS and Linux. Windows has not been tested since the fork. OS-level trusted clicks work on
macOS, Windows and Linux under X11, not Wayland. See [Requirements](guide.md#requirements).

## Is this the same project as kitsune-mcp?

No. `kitsune-mcp` on PyPI and `kaiser-data/kitsune-mcp` are unrelated projects. This one is
published as [`kitsune-browser-mcp`](https://pypi.org/project/kitsune-browser-mcp/) and in the MCP
Registry as `io.github.junyeong-nero/kitsune`.
