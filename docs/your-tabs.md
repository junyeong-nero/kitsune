# Your tabs and approvals

Since 0.8.0 there are no capability groups or task modes: every tool is available to every
agent. What the agent may do is decided by these checks, which combine:

| Check | What it covers | Where you set it | Default |
| --- | --- | --- | --- |
| [Protected tabs](#protecting-your-tabs) | Writes to your tabs and other clients' tabs | `KITSUNE_USER_TABS` in the MCP server environment; per tab, a [handoff](api-reference.md#requesting-and-returning-a-tab) | Read-only, refused without a prompt |
| [Tool approvals](#tools-that-need-your-approval) | Checked MCP tools, including supplied browser API calls inside Python scripts and batch steps | The extension's options page | Cookies, page storage, clipboard, browsing-data clearing, saved sessions, history and bookmarks |
| [Ask-before-act](#ask-before-act) | Submits, payment pages, JavaScript, cookie access | `browser_set_policy(ask=True)` or `KITSUNE_ASK_BEFORE_ACT=1` | Off |
| History permission | History and bookmark lookups | The extension's options page (browser permission) | Not granted |
| `browser_set_policy` | URL allow/deny lists, read-only mode | The tool, or the bridge API | No limits |
| `KITSUNE_SCRIPTING=0` | Removes `browser_run_python` (host Python) from the MCP server | The MCP server environment | Python available |

None of these is a sandbox: `browser_run_python` runs ordinary local Python with your user's
access, and any local program holding the bridge token can reach the bridge directly. Use a
separate browser profile and the [security guide](security.md) for accounts that matter.

**Upgrading from 0.7:** remove `KITSUNE_CAPS`, `KITSUNE_PUBLISH` and `KITSUNE_MODE` from your
MCP client's configuration; the server refuses to start while one is set. Review the tools under
[Tools that need your approval](#tools-that-need-your-approval), or enable
[ask-before-act](#ask-before-act). These approval checks do not restore the old tool exclusions
and are not equivalent to a `draft` mode. If you do not trust the agent with host Python,
also set `KITSUNE_SCRIPTING=0`.

- **Your tabs.** Writes to your tabs fail at once with `user_tab_read_only`, without a prompt.
  The agent opens its own tab instead, or asks for yours with a reason. You see the request in
  the toolbar popup (its badge shows a count) and, on a client with elicitation, in the client's
  prompt at the same time; answer in either and the other closes. Without elicitation the client
  is asked only after 30 seconds without an answer in the popup. An approved tab is the agent's
  until you press **Take back** or its session ends. It does not give the agent your real mouse or keyboard.
  To let the agent write to your tabs without asking, set `KITSUNE_USER_TABS=writable`
  ([Protecting your tabs](#protecting-your-tabs)).
- **Tools you choose.** Every tool is available to the agent; the ones you check in Kitsune's
  options page wait for your OK first ([Tools that need your approval](#tools-that-need-your-approval)).
  By default these are the tools that read cookies, page storage, the clipboard, saved sessions
  and history.
- **Risky actions.** With the opt-in [ask-before-act](#ask-before-act)
  policy, submitting forms, payment pages, JavaScript and cookie access need your approval.
  Claude Code and Codex ask you in their own prompt, so the approval never passes through the
  agent. Other clients get a one-time token the agent should show you first; it guards
  against accidents, not against an agent that repeats the call on its own
  ([Approving held actions](#approving-held-actions)).
- **Things only you can do.** For a sign-in, a CAPTCHA or a choice that is yours to make, the
  agent asks you (`browser_ask_user`) and carries on when you are done
  ([When the agent needs you](#when-the-agent-needs-you)).

## Protecting your tabs

Every tab is readable, but user tabs and other clients' tabs are **read-only**. Writes,
scrolling, hovering, focusing, navigation, reload and close fail immediately with
`user_tab_read_only` (`retryable: false`), without an approval prompt. The error names
`state.tabId`, `state.index` (the `browser_tabs list` index) and `state.action`.
Open the URL in a tab of the agent's own (`browser_tabs action=new`) before writing.
Alternatively, `browser_tabs action=request` with an `index` or `tab_id` and a `reason`
asks for a [session-scoped handoff](api-reference.md#requesting-and-returning-a-tab). On a client with elicitation
the toolbar popup and the client's prompt ask at once and the first answer counts; otherwise approval is
browser-first, then a confirm token after 30 seconds. A grant permits DOM/browser
operations, **not trusted/native OS input**; it does not select or activate the tab.
Approving also releases text you left in a focused field of that tab, so the agent can
finish a form you started there. The user can **Take back** in the popup; session end or bridge/extension disconnection
also returns it. Agent-created tabs keep their persistent ownership. Other clients'
tabs cannot be transferred.

Snapshots, ordinary find, DOM reads, console/network reads and viewport screenshots
can read your tabs without scrolling, focusing or switching your view; the agent's
`browser_tabs select` makes any of them its current tab for that, without showing it.
Reading a discarded tab reloads it in the background. `find load_more` and
element/full-page screenshots scroll the page, so on a tab the agent cannot write they
run only while the tab is not shown, and put its scroll position back afterwards; on the
tab you are looking at they are refused.
The `current` marker always identifies the agent's current tab, regardless of ownership;
selection is not an ownership grant. On an owned tab, trusted user input still pauses
writes with the existing retryable `user_active` response. Text you leave in a focused
field pauses them until you leave the field or give the page no input for 10 s.

**User-tab setting:** `KITSUNE_USER_TABS` accepts `read-only` (the default) or
`writable`, ignoring surrounding whitespace and letter case. With `writable`, this
client may write user and other clients' tabs without changing ownership; `user_active`
and ask-before-act still apply. Invalid explicit values (including an
empty string) stop MCP startup with a configuration error, rather than weakening protection.
Python uses `kitsune.connect(user_tabs="writable")`; it does not read this environment
setting. Omitting `user_tabs` means read-only.

The deprecated aliases `KITSUNE_HOLD_VIEWED_TAB` and `KITSUNE_PROTECT_USER_TABS` (and
Python's `hold_viewed_tab` and `protect_user_tabs`) were removed after the 0.7.2 alias
release. The environment variables are now ignored without a warning, so a client that
set either to `0` is read-only again until it sets `KITSUNE_USER_TABS=writable`; the
Python arguments raise `TypeError`.

The client sends only `X-Kitsune-User-Tabs: read-only|writable` to the bridge. Missing
headers default to read-only; invalid values reject the request with HTTP 400
(`invalid_user_tabs`) before any action. Header names are case-insensitive: the client
replaces any extra spelling with its configured permission. Multiple occurrences (even
identical values) and comma-combined values are rejected, never resolved by order.
Old `X-Kitsune-Hold-Viewed-Tab` and
`X-Kitsune-Protect-User-Tabs` headers are ignored, not permission grants: upgrade old
clients to use the new header. Tool arguments cannot override it. The bridge's
process environment does not set permissions for its clients.

See [Tab permissions](api-reference.md#tab-permissions).

## When the agent needs you

Some steps only you can take: signing in, a CAPTCHA, a button that ignores the agent's
synthetic clicks. Kitsune points them out on its results as a `needs_user` hint (#376): a shown
sign-in field or a sign-in popup the browser blocked (`sign_in`), a reCAPTCHA, hCaptcha or
Turnstile frame (`human_check`), or a click only a real click works for (`trusted_input`). It
reads only fields, frames and URLs for this, never the page's wording, and holds nothing for
it: the agent may still type into a sign-in form you asked it to fill.

When the agent cannot go on, it asks you with `browser_ask_user`. On a client with
elicitation you answer in the client's prompt: Accept (Continue in pi) once you are done, or
Decline if you can't; Codex asks you to choose Done or Can't. Otherwise the agent asks you in
its reply; for a sign-in or CAPTCHA Kitsune sees on the tab, it can call `browser_resume`, which
waits until the sign-in or CAPTCHA is gone from the tab, so you need not tell it you are done.
Details are in the [API reference](api-reference.md#browser_ask_userreason-message-tab_idnone).

## Tools that need your approval

Open about:addons → Kitsune → Options. Under **Tools that need your approval**, every Kitsune tool
is listed by group; a checked tool waits for your answer before it runs (#525). By default the
`storage` and `history` tools are checked; `browser_run_python` (the agent's local Python) is not.
**Restore defaults** and **Uncheck all** reset the list. The extension sends the list to the bridge
when it connects and whenever you change it.

When the agent calls a checked tool, nothing runs yet. The toolbar popup shows a card (its badge
counts it) with the client, the tool and its arguments; on a client with elicitation, the client's
prompt asks at the same time, and the first answer counts:

- **Allow once** runs that call.
- **Allow for this session** also allows that client's later calls of the tool, until the bridge
  restarts.
- **Decline** leaves it unrun; the agent is told not to repeat it.

The same check applies when the tool's work runs inside a `browser_run_python` script (the script
waits for your answer, and the time does not count against its deadline) or as a step of a batch
or workflow (the popup alone asks, for up to 30 seconds). It applies to agents' MCP servers only:
your own scripts with the Python API are not asked. It is not a sandbox: `browser_run_python` runs
ordinary local Python, which can reach the bridge without asking.

## Ask-before-act

Ask-before-act is an opt-in policy. In this mode the bridge does not run form
submits, submit-button clicks, clicks on buttons labeled like a submit or payment
("Place order", "Pay now", "결제하기"), Enter in form fields, `browser_js` and other
agent JavaScript, cookie, session, and page-storage access, clipboard reads and
writes, downloads, navigation to payment-like URLs, or page actions on such pages.
Each such call needs your approval (see [Approving held actions](#approving-held-actions)).
The `needs_user` error (`state.detail` `confirmation_required`; until #376 that was its code)
describes the pending action. On a client without elicitation it carries a single-use token,
the fallback for approving there, that expires in 120 seconds and is bound to that exact
action, its parameters, and its tab. Batch, retry,
parallel, broadcast, and workflow commands never accept a token. Details are in the
[API reference](api-reference.md#ask-before-act).

There are two ways to turn it on:

- `browser_set_policy(ask=True)`: the agent can also turn this off again, so it
  only guards against accidents.
- Locked: set `KITSUNE_ASK_BEFORE_ACT=1` in the MCP server environment, or run
  `kitsune-bridge start --ask-before-act`. The policy can then only be tightened.
  A bridge that is already running keeps its policy, so run `kitsune-bridge stop`
  after changing the setting.

A locked bridge writes `~/.kitsune/ask-before-act-locked` (under `KITSUNE_HOME`, readable
only by you). While that file exists, every bridge starts locked, also after
`kitsune-bridge stop`, an idle shutdown, or an MCP server replacing an outdated bridge,
and also when the environment lacks `KITSUNE_ASK_BEFORE_ACT=1`. To unlock, remove the
variable from every MCP server configuration, delete the file, and run
`kitsune-bridge stop`.

The bridge checks the target before it acts, and the page can change in between (for
example, it navigates to a checkout URL). Such a change is not caught.

```toml
[mcp_servers.kitsune.env]
KITSUNE_ASK_BEFORE_ACT = "1"
```

## Approving held actions

When ask-before-act holds a call for your approval, it returns `needs_user` with
`state.reason` `approval`, and the held action does not run. User-tab writes instead fail
with `user_tab_read_only`, without prompting. An explicit [handoff request](api-reference.md#requesting-and-returning-a-tab)
asks in the browser and, with elicitation, in the client at once. For ask-before-act, approval works as follows:

- **Clients with elicitation** (Claude Code, Codex): Kitsune asks you in the client's own
  prompt, naming the action, its target, the call's arguments, the tab and why it was held.
  The agent's description of the target is labeled as the agent's. Choose Approve in the
  required decision field on any client, and Kitsune runs that one call; the agent never sees a token, so
  it cannot approve itself. Decline or dismiss it, and the agent gets `needs_user` with `state.decision` `declined` or
  `cancelled`. The prompt closes unanswered 5 seconds before the approval would expire.
  If you change your mind, the agent calls `browser_resume`, which asks you again and
  continues the call where it was held.
  Steps inside batches, parallel calls and workflows are not asked about: the agent repeats
  the held step as a direct call, which asks you.
- **Clients without elicitation**: the error carries a token (`state.approval` `token`),
  and the agent should ask you, then repeat the call with `confirm_token`. That token goes
  back to the agent, so it guards against accidents only: the agent can repeat the call
  without asking. Have your MCP client prompt you before tool calls, or at least before
  calls that carry `confirm_token`. `browser_resume` on such a client checks the hold once
  and returns at once; it never waits for approval.

The bridge's audit log (`browser_audit`) records how each approved write was approved,
`approval: "elicitation"` or `"token"`. Details are in the
[API reference](api-reference.md#approval-prompts).
