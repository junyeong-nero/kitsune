# Checkpoint and resume

A `browser_batch`, `browser_parallel` or `browser_workflow` call that did not finish
`ok` can be continued with `browser_resume` (#33, #115), and so can a direct call that
stopped on `needs_user` ([Direct calls](#direct-calls), #376). One rule holds throughout:
**resuming never sends a write again after it ran `ok`, and sends a write whose outcome
is `unknown` again only after the caller observed that it did not take effect.**

## Checkpoints

A container call whose `status` is not `ok` (`unknown`, `held`, `failed` or `skipped`,
including a `container_timeout`) keeps a checkpoint. Its response adds two fields to
`results`, `progress`, `status` and `summary` ([Container progress](api-reference.md#container-progress)):

```json
{ "results": [{ "ok": true }, { "error": "Command timed out after 30s", "code": "timeout", "retryable": false }],
  "progress": [
    { "index": 0, "action": "click", "tabId": 3, "status": "ok" },
    { "index": 1, "action": "submitForm", "tabId": 3, "status": "unknown" },
    { "index": 2, "action": "pageText", "tabId": 3, "status": "skipped" }],
  "status": "unknown",
  "summary": "3 commands: 1 ok, 1 unknown, 1 skipped. ...",
  "call": 42,
  "resume_token": "x3J..." }
```

- `call` is the audit call id of the container ([Audit log](api-reference.md#audit-log)).
  `resume_token` is random (`secrets`, 24 bytes). Resuming needs both, so knowing a
  call id from the audit log is not enough.
- Checkpoints live in bridge memory only. They are never written to disk or to the
  audit log, and a bridge restart loses them. The bridge keeps at most 20 (the oldest
  is dropped first). Each expires 30 minutes after its call ended.
- A checkpoint holds the call's commands (typed values included), results and step
  records, and only a hash of the token. All of it is dropped on expiry, when the
  checkpoint is pushed out by newer ones, when a resume finishes `ok` or stops on an
  internal error, and on bridge shutdown.
- A call that finished `ok` has no checkpoint and no `call`/`resume_token` fields.

## browser_resume

```
browser_resume(call, resume_token, observed=None, wait=120)
```

The tool is in the `core` capability group (#376; it was in `automation`), so the default
`KITSUNE_CAPS=core` has it. For a container call it sends POST `/api/resume` with
`{"call", "resume_token", "observed"}`, a route of both `core` and `automation`; `wait` is
not used. A direct call's checkpoint is resumed by the MCP server itself
([Direct calls](#direct-calls)).

The response has the shape of the original call: `results`, `progress`, `status` and
`summary` for every command, plus `workflow` and `steps` for a workflow. If the
resumed call still did not finish `ok`, the response carries the same `call` and a
new `resume_token`, and the old token stops working. The checkpoint is replaced and
expires 30 minutes after this resume. If it finished `ok`, the checkpoint is dropped.

A resume has a fixed 300 s deadline, because the MCP server cannot see the stored
commands. That applies even when the original batch allowed longer waits. At the
deadline it stops like any container (`container_timeout`), and the response
carries a new checkpoint. The tool waits past the deadline plus the settle time, so
the partial report arrives.

### Step paths

Steps are named by their `progress` path:

| Path | Step |
|---|---|
| `"2"` | top-level command 2 of a batch or workflow |
| `"0.1.3"` | step 3 of sequence 1 of the `parallel` at command 0 (`progress[0].sequences[1][3]`) |
| `"1.2"` | step 2 of the `sequence` at command 1 |

Paths nest: `"0.1.3.0"` is step 0 of a `sequence` at that parallel step.

### What each step does

| Last record | Resume |
|---|---|
| `ok` | Not sent again; its stored result is reused. Its `progress` record adds `resumed: "kept"`. Exception: an `ok` read inside a `sequence` runs again when a later step of that sequence that will run uses its result through `${$N...}`, so that step reads fresh state |
| `skipped` | Sent: it never started |
| `failed` | Sent again (`retryable: true`, or nothing was dispatched) |
| `held` | Sent again through ask-before-act, so it stays held while ask mode is on and the call stops there. After confirming it with a direct call, report it `"applied"` |
| `unknown` read | Sent again |
| `unknown` write | Refused until `observed` reports it: `"not_applied"` sends it again, `"applied"` marks it `ok` without sending it |

Writes are the bridge's [write actions](api-reference.md#action-errors), plus
anything that runs agent JavaScript (`js`, `waitForResult`, `syncBarrier`, an
`if`/`while` condition), cookies, page storage, the clipboard, downloads and
`broadcast`. `navigate`, `newTab`, `closeTab` and `switchTab` count as writes:
sending them again could reset a form, duplicate a tab or close the wrong one.

`sequence` and `parallel` commands are resumed step by step. Other blocks (`if`,
`while`, `try`, `retry`, `broadcast`) are resumed as one step. If one of them is not
`ok` but a write in it already ran `ok` (for example the `click` before a failing
`find` in a `try`, or the condition of an `if` whose branch failed), it is never sent
again. It accepts only `"applied"`, and resume refuses until the caller gives it.

`observed` maps step paths to `"applied"` or `"not_applied"`. It is accepted only for
`unknown`, `held` and `failed` steps. For a `failed` or `held` step, `"not_applied"`
does the same as leaving it out. There is no default for an `unknown` write. Every
entry is checked before anything is sent.

Every resumed step goes through the current policy again: read-only mode, the URL
allow and deny lists and [ask-before-act](api-reference.md#ask-before-act). A
confirmation token is never honored inside a resume. A step reported `"applied"` is
not sent, so nothing reaches the page.

A resumed batch keeps `stopOnWarning`, but does not stop again at a step it kept. A
batch or `sequence` still stops at a held step. Steps after the stop that ran `ok`
earlier stay `ok` (`resumed: "kept"`) in `progress` and in the next checkpoint.

### Workflows

A workflow checkpoint keeps the SHA-256 hash of the workflow file it ran. If the file
changed or is gone, the resume is refused with `workflow_changed` and nothing is sent.
A resume runs the stored commands, so changing the variables is not possible.

A batch or workflow checkpoint also keeps the call's default tab (`tabId`, by default the
current tab of the MCP server that ran it). Resumed steps that name no tab run there, even
if the agent has opened or selected another tab since.

### Parallel

Each sequence of a resumed `parallel` continues from its own records: a sequence that
finished `ok` sends nothing, the others continue concurrently. The sequences keep the
tab claims they made in the first run, including tabs claimed through `${$N.tabId}`.
Before the sequences start, each one with steps left is refused, with each of those
steps failing and none sent, when:

- `tab_closed`: a tab its remaining steps act on was closed (checked with `getTabs`).
  A closed tab is never replaced. If the tabs cannot be listed, the sequence is
  refused with that error.
- `tab_owned`: another sequence of this `parallel` claimed a tab it writes.
- `tab_claimed`: another call's `parallel` that is in progress writes one of its tabs
  (`state.call`).

The other sequences resume as usual. A refused sequence keeps its records, so a later
resume can try it again.

### Errors

Every error below except `resume_failed` sends nothing and leaves the checkpoint as
it was.

| `code` | HTTP | Meaning |
|---|---|---|
| `checkpoint_not_found` | 404 | No checkpoint for `call`: it finished `ok`, expired, was pushed out, is being resumed by another request, stopped on an internal error, or the bridge restarted |
| `resume_token_invalid` | 403 | `resume_token` does not match the checkpoint's latest token |
| `workflow_changed` | 409 | The workflow file changed since the call (`state.workflow`) |
| `observed_invalid` | 400 | `observed` names a step that is `ok`, `skipped`, a `sequence`/`parallel` block or does not exist, or gives a value the step does not accept (`state.steps`: `step`, `reason`, `accepts`) |
| `observed_required` | 400 | A step needs an observation (`state.steps`: `step`, `action`, `tabId`, `status`, `accepts`) |
| `resume_failed` | 200 | The resume stopped on an internal error. `progress` reports what ran, steps in progress may have taken effect, and the checkpoint is dropped |

A direct call's resume adds `current_tab_changed` and answers `observed_invalid` and
`resume_token_invalid` itself ([Direct calls](#direct-calls)).

The per-sequence `tab_closed`, `tab_owned` and `tab_claimed` errors appear in
`results` like other step errors. No error or audit entry contains the resume token,
typed values, JavaScript or stored results.

### Audit log

Resumed steps are audited like other container steps, with
`parent: {call, path, resumed: true}`. `call` is the original call's id. Typed values
stay redacted and JavaScript bodies are dropped
([Audit log](api-reference.md#audit-log)). A step reported `"applied"` gets one entry
with `params.observed: "applied"` and `params.sent: false`. Steps kept from the first
run get no new entry. The token never appears in the audit log or the bridge log.

## Direct calls

A direct tool call that stops on `needs_user` and was not resolved in the call keeps a
checkpoint (#376): an approval hold on a client without elicitation, or one whose
[approval prompt](api-reference.md#approval-prompts) was declined, dismissed or got no answer
in time, and a [`browser_ask_user`](api-reference.md#browser_ask_userreason-message-tab_idnone)
request (below). Its result adds `call` and `resume_token`, and its hint says what `browser_resume`
does with it:

```json
{ "error": "The user is viewing tab 12; nothing was done", "code": "needs_user", "retryable": false,
  "state": { "reason": "approval", "detail": "user_viewing", "decision": "declined", "approval": "elicitation", "…": "…" },
  "call": 57, "resume_token": "Qk9…",
  "hint": "The held action did not run: the user declined it … browser_resume(call, resume_token) asks them again …" }
```

- `call` is the audit call id the bridge gave the held request: its audit entry carries it
  as `call` ([Audit log](api-reference.md#audit-log)), so it never names a container call.
  A resume that is still pending keeps the same `call` and returns a new `resume_token`; the
  hold it made again has an audit entry with its own `call`.
- The MCP server that made the call keeps the checkpoint, under the rules for container
  checkpoints: in memory only, at most 20 (the oldest is dropped first), each expiring 30
  minutes after its call or its last resume, with only a SHA-256 hash of the token. It holds
  the tool, its arguments (without `confirm_token`), the held result without its confirm token,
  and the current tab the call ran on. It is dropped when a resume no longer stops on
  `needs_user`. It is lost when that MCP server restarts, and another MCP client cannot
  resume it: the bridge answers `checkpoint_not_found`, and the agent calls the tool again.
- Holds that cannot be approved get no checkpoint: read and draft modes (`state.approvable:
  false`), and tools without `confirm_token`. Container results never get a direct checkpoint.

`browser_resume(call, resume_token, wait=120)` runs the tool again with its stored
arguments, from the held request:

- **Nothing of the call that ran `ok` is sent again.** The call restarts at the held
  request: `browser_fill_form` does not fill the fields before the held one again, and
  `browser_type(submit=true)` held at its Enter presses Enter without typing the text again
  (the bridge names the held request in `state.action`). Every other tool that takes
  `confirm_token` sends one write per call.
- **A resume grants nothing a new call would not.** The held request goes through the
  current policy again: ask-before-act, protected user tabs and the viewed-tab hold. If the
  hold no longer applies (the user switched to another tab, so the viewed-tab hold is gone),
  the call runs without an approval, as calling the tool again would at that moment.
- **A resume never waits for a hold to lift.** On a client with elicitation, a request held
  again is asked about in the approval prompt, which stays open up to `wait` seconds (at most
  the token's 120 s less 5 s); `wait=0` asks nothing. Without elicitation the hold is checked
  once and the resume returns at once: `needs_user` again with a fresh confirm token and a
  new `resume_token`. So waiting and resuming cannot run a held write the moment the user
  looks away.
- `wait` is 0 to 300 seconds (default 120); larger values are capped at 300. While the
  prompt is open, a call that sent a `progressToken` gets a progress notification every 10 s.
  The MCP server waits itself, between bridge requests, so no bridge request outlasts its own
  timeout; a client's tool-call timeout must allow `wait` plus the call itself.
- The result is the tool's own result. Approved, declined (`state.decision: "declined"`) and
  unanswered (`"cancelled"`) work as in the call. While it still stops on `needs_user`, it
  carries the same `call` and a new `resume_token`, and the old token stops working.
- A stop the page resolves has a check in `kitsune.server.PAGE_STATE_CHECKS`, which the
  resume polls every second for up to `wait` seconds before it runs the call again. While the
  check fails, the resume returns the stop again with a new `resume_token`, and nothing is
  sent. `sign_in` and `human_check` are checked by the tab's
  [`needs_user` hint](api-reference.md#needs_user-hints) (`/api/needs-user`): resolved once the
  tab no longer shows one with that reason. A failed read is not resolved.

`browser_ask_user` keeps a checkpoint when there is something to wait for:

- **With elicitation**, when the prompt was dismissed or got no answer in time
  (`state.decision: "cancelled"`), not when the user answered Can't. The resume asks the user
  again in the client's prompt, for up to `wait` seconds (at most 120), and returns the answer
  as the call would; `wait=0` asks nothing.
- **Without elicitation**, when it asks about `sign_in` or `human_check` and the tab showed
  that hint as it asked (`state.evidence`). The resume waits for the hint to be gone from the
  tab, then returns `{"ok": true, "answer": "done", "via": "page", …}`. Without such a hint
  there is nothing to watch, so the result has no `resume_token`: the agent asks in its reply.
- The request itself never reaches the page, so a resume sends nothing to it either.

Errors of a direct call's resume send nothing and keep the checkpoint:

| `code` | Meaning |
|---|---|
| `current_tab_changed` | The call acts on the current tab, and the agent made another tab current since (`state.tabId`, `state.currentTab`). Make that tab current again with `browser_tabs` select, or call the tool again |
| `observed_invalid` | `observed` was given: a direct call has no steps to observe |
| `resume_token_invalid` | `resume_token` does not match the call's latest token |

A resume whose call fails another way (its target is gone, the tab closed) returns that
error, and the checkpoint is gone: check the page and call the tool again.

## Without browser_resume

After a container's checkpoint is gone, resume by hand. Send a new batch that starts at
the first record that is not `ok`, after observing the page for any `unknown` record.
After a direct call's checkpoint is gone, call the tool again.
