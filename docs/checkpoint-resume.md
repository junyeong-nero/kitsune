# Checkpoint and resume

A `browser_batch`, `browser_parallel` or `browser_workflow` call that did not finish `ok`, or a
direct call that stopped on `needs_user`, can be continued with `browser_resume`. One rule holds:
**a resume never resends a write that ran `ok`, and resends a write whose outcome is `unknown`
only after the caller observed that it did not take effect.**

## Checkpoints

A container call whose `status` is not `ok` keeps a checkpoint, and its response adds `call` and
`resume_token` to `results`, `progress`, `status` and `summary`
([Container progress](api-reference.md#container-progress)):

```json
{ "results": [{ "ok": true }, { "error": "Command timed out after 30s", "code": "timeout", "retryable": false }],
  "progress": [
    { "index": 0, "action": "click", "tabId": 3, "status": "ok" },
    { "index": 1, "action": "submitForm", "tabId": 3, "status": "unknown" },
    { "index": 2, "action": "pageText", "tabId": 3, "status": "skipped" }],
  "status": "unknown",
  "call": 42,
  "resume_token": "x3J..." }
```

- Resuming needs both `call` (the audit call id) and the random `resume_token`.
- Checkpoints live in bridge memory only (never on disk or in the audit log): at most 20, each
  expiring 30 minutes after its call. A bridge restart loses them. Only a hash of the token is kept.

## browser_resume

```
browser_resume(call, resume_token, observed=None, wait=120)
```

The response has the shape of the original call. If it still did not finish `ok`, it carries the
same `call` and a new `resume_token`; the old token stops working. A container resume has a fixed
300 s deadline.

### Step paths

| Path | Step |
|---|---|
| `"2"` | top-level command 2 |
| `"0.1.3"` | step 3 of sequence 1 of the `parallel` at command 0 |
| `"1.2"` | step 2 of the `sequence` at command 1 |

### What each step does

| Last record | Resume |
|---|---|
| `ok` | Not sent; its result is reused (`resumed: "kept"`). An `ok` read in a `sequence` reruns if a later step uses its result via `${$N...}` |
| `skipped` | Sent |
| `failed` | Sent again |
| `held` | Sent again through ask-before-act, so it stays held while ask mode is on. Report it `"applied"` after confirming it directly |
| `unknown` read | Sent again |
| `unknown` write | Refused until `observed` reports `"not_applied"` (resend) or `"applied"` (mark `ok`) |

Writes include the bridge's [write actions](api-reference.md#action-errors), agent JavaScript,
cookies, storage, clipboard, downloads, `broadcast`, `navigate`, `newTab`, `closeTab` and
`switchTab`. `sequence` and `parallel` resume step by step; other blocks (`if`, `while`, `try`,
`retry`, `broadcast`) resume as one step, and one containing a write that ran `ok` accepts only
`"applied"`.

`observed` maps step paths to `"applied"` or `"not_applied"`, for `unknown`, `held` and `failed`
steps only. Every resumed step passes the current policy again; confirmation tokens are never
honored inside a resume.

### Workflows

A workflow checkpoint keeps the file's SHA-256; a changed file is refused with `workflow_changed`.
Batch and workflow checkpoints keep the call's default tab.

### Parallel

Sequences each continue from their own records. A sequence is refused (nothing sent)
with `tab_closed` if a tab it needs was closed, `tab_owned` if another sequence claimed a tab it
writes, or `tab_claimed` if another running `parallel` writes it.

### Errors

All except `resume_failed` send nothing and keep the checkpoint.

| `code` | HTTP | Meaning |
|---|---|---|
| `checkpoint_not_found` | 404 | Finished `ok`, expired, pushed out, being resumed, or the bridge restarted |
| `resume_token_invalid` | 403 | Not the latest token |
| `workflow_changed` | 409 | The workflow file changed |
| `observed_invalid` | 400 | `observed` names a step that cannot take it, or an invalid value (`state.steps`) |
| `observed_required` | 400 | A step needs an observation (`state.steps`) |
| `resume_failed` | 200 | Internal error; `progress` reports what ran and the checkpoint is dropped |

### Audit log

Resumed steps are audited with `parent: {call, path, resumed: true}`
([Audit log](api-reference.md#audit-log)). The token never appears in any log.

## Direct calls

A direct call that stops on `needs_user` unresolved (an approval hold without elicitation, a
declined or unanswered [approval prompt](api-reference.md#approval-prompts), or a
[`browser_ask_user`](api-reference.md#browser_ask_userreason-message-tab_idnone) request) adds
`call` and `resume_token` to its result. Tab handoff requests and tools without `confirm_token`
get no checkpoint.

The MCP server that made the call keeps the checkpoint in memory under the same limits (20, 30
minutes). Another client or a restarted server cannot resume it.

`browser_resume(call, resume_token, wait=120)` reruns the tool from the held request:

- **Nothing that ran `ok` is resent.** `browser_fill_form` skips fields filled before the hold;
  `browser_type(submit=true)` held at Enter only presses Enter.
- **It grants nothing a new call wouldn't.** The request passes the current policy and tab
  permissions again; if the hold no longer applies, it simply runs.
- **It never waits for a hold to lift.** With elicitation, a renewed hold is asked about for up
  to `wait` seconds (`wait=0` asks nothing). Without elicitation it returns `needs_user` again at
  once with a new token.
- `wait` is 0–300 seconds. For `sign_in` and `human_check` stops, the resume polls the tab every
  second until the [`needs_user` hint](api-reference.md#needs_user-hints) is gone.

`browser_ask_user` keeps a checkpoint when the prompt was dismissed or unanswered, or, without
elicitation, when the tab showed a `sign_in`/`human_check` hint; the resume asks again or waits
for the hint to clear.

| `code` | Meaning |
|---|---|
| `current_tab_changed` | The agent made another tab current; select it again or call the tool again |
| `observed_invalid` | `observed` was given; direct calls have no steps |
| `resume_token_invalid` | Not the latest token |

## Without browser_resume

Once a checkpoint is gone, send a new batch starting at the first record that is not `ok` (after
checking the page for `unknown` ones), or call the direct tool again.
