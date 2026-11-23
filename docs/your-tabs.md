# Your tabs and approvals

Every tool is available to every agent. These checks decide what it may do:

| Check | Covers | Set in | Default |
| --- | --- | --- | --- |
| [Protected tabs](#protecting-your-tabs) | Writes to your and other clients' tabs | `KITSUNE_USER_TABS`; per tab, a handoff | Read-only |
| [Tool approvals](#tools-that-need-your-approval) | Checked tools, also inside Python scripts and batches | Extension options | Cookies, storage, clipboard, sessions, history |
| [Ask-before-act](#ask-before-act) | Submits, payment pages, JavaScript, cookie access | `browser_set_policy(ask=True)` or `KITSUNE_ASK_BEFORE_ACT=1` | Off |
| History permission | History and bookmark lookups | Extension options | Not granted |
| `browser_set_policy` | URL allow/deny lists, read-only mode | The tool or bridge API | No limits |
| `KITSUNE_SCRIPTING=0` | Removes `browser_run_python` | MCP server environment | Python on |

None of these is a sandbox: `browser_run_python` runs local Python with your access, and any
local program with the bridge token reaches the bridge directly. See [Security](security.md).

## Protecting your tabs

The agent can read every tab, but writes, scrolling, hovering, focusing, navigation, reload and
close on your tabs (and other clients') fail at once with `user_tab_read_only`. The agent should
open its own tab (`browser_tabs action=new`) instead, or ask for yours:

- `browser_tabs action=request` with an `index` or `tab_id` and a `reason` asks for a
  [handoff](api-reference.md#requesting-and-returning-a-tab). You answer in the toolbar popup
  or, with elicitation, the client's prompt (up to 30 s); the first answer counts.
- An approved tab is the agent's until you press **Take back** or its session ends. It allows
  DOM actions, not your real mouse or keyboard, and does not switch your view.

Reads don't move your view. Reads that scroll (`find load_more`, element or full-page screenshots)
run on a protected tab only while it is not shown, and restore its scroll position. On the
agent's own tabs, your input pauses writes with a retryable `user_active`.

`KITSUNE_USER_TABS=writable` lets the client write your tabs without asking (`user_active` and
ask-before-act still apply); any value other than `read-only`/`writable` stops startup. The
Python API takes `kitsune.connect(user_tabs="writable")` instead. See
[Tab permissions](api-reference.md#tab-permissions).

## When the agent needs you

For a sign-in, CAPTCHA or a button that ignores synthetic clicks, results carry a `needs_user`
hint (`sign_in`, `human_check`, `trusted_input`). When the agent cannot go on it calls
`browser_ask_user`: answer Accept when done or Decline in the client's prompt, or in chat on
clients without elicitation. For a sign-in or CAPTCHA, `browser_resume` waits until it is gone
([details](api-reference.md#browser_ask_userreason-message-tab_idnone)).

## Tools that need your approval

In about:addons → Kitsune → Options, check the tools that should wait for your OK. `storage` and
`history` tools are checked by default; `browser_run_python` is not.

A checked call shows a card in the toolbar popup (and the client's prompt, with elicitation):

- **Allow once** runs that call.
- **Allow for this session** allows that client's later calls until the bridge restarts.
- **Decline** leaves it unrun.

The check also applies inside `browser_run_python` scripts and batch or workflow steps. Your own
Python API scripts are not asked.

Each tool is asked for by its own name. `browser_check` (`tab.check` in a script) clicks custom
checkboxes, switches and radios (elements with those ARIA roles), so checking only
`browser_click` does not cover those clicks: check `browser_check` as well.

## Ask-before-act

An opt-in policy that holds form submits, submit/payment-like buttons ("Place order", "결제하기"),
Enter in form fields, agent JavaScript, cookie/session/storage access, clipboard, downloads,
and payment-like URLs until you approve ([details](api-reference.md#ask-before-act)).

- `browser_set_policy(ask=True)`: the agent can turn it off again, so it only guards accidents.
- Locked: `KITSUNE_ASK_BEFORE_ACT=1` or `kitsune-bridge start --ask-before-act`. It writes
  `~/.kitsune/ask-before-act-locked` (Windows: `%USERPROFILE%\.kitsune\ask-before-act-locked`), and every bridge starts locked while that file exists. To
  unlock, remove the variable, delete the file and run `kitsune-bridge stop`.

```toml
[mcp_servers.kitsune.env]
KITSUNE_ASK_BEFORE_ACT = "1"
```

The target is checked before acting; a page that changes in between (e.g. navigates to checkout)
is not caught.

## Approving held actions

A held call returns `needs_user` with `state.reason: "approval"` and does not run.

- **Clients with elicitation** (Claude Code, Codex) ask you in their prompt with the action,
  target and tab. Approve runs that one call; the agent never sees a token. Declined or dismissed
  calls return `state.decision` `declined` or `cancelled`; `browser_resume` asks again. Batch and
  workflow steps are not asked about: the agent repeats the step as a direct call.
- **Other clients** get a single-use `confirm_token` (120 s, bound to the exact action). The agent
  should ask you, then repeat the call with it. The agent can do this without asking, so it guards
  only against accidents; have your client prompt you before tool calls.

The audit log (`browser_audit`) records each approval as `elicitation` or `token`
([Approval prompts](api-reference.md#approval-prompts)).
