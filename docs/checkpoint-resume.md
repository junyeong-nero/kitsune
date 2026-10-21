# Checkpoint and resume for containers

A `browser_batch`, `browser_parallel` or `browser_workflow` call that did not finish
`ok` can be continued with `browser_resume` (#33, #115). One rule holds throughout:
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
browser_resume(call, resume_token, observed=None)
```

POST `/api/resume` with `{"call", "resume_token", "observed"}`. The tool is in the
`parallel` capability group (`KITSUNE_CAPS=parallel`), not in `core` or the lean preset.

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

## Without browser_resume

Without the `parallel` group, or after the checkpoint is gone, resume by hand. Send
a new batch that starts at the first record that is not `ok`, after observing the
page for any `unknown` record.
