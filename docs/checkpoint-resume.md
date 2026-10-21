# Checkpoint and resume for containers (design note)

Status: design only (#33). Nothing here is implemented yet except the pieces listed
under [What exists](#what-exists). The rule every part follows: **resuming never
re-sends a consequential write that may already have been dispatched, unless the
caller has observed that it did not take effect.**

## What exists

- `progress` on `browser_batch`, `browser_parallel` and `browser_workflow` results
  ([Container progress](api-reference.md#container-progress)): one record per
  command with `ok`, `failed`, `held`, `unknown` or `skipped`.
- A batch or workflow deadline cancels the command in progress (`unknown`) and starts
  no later command (`skipped`), so the report is final when the response is sent.
- Audit entries of container steps carry `parent: {call, path}`
  ([Audit log](api-reference.md#audit-log)); a cancelled step is logged with
  `error: "cancelled"`.
- `retryable: false` marks a write whose outcome is unknown. `retry` stops there, and
  in ask mode a repeat of that exact write is held as `unknown_outcome`.

Today a caller resumes by hand: send a new batch that starts at the first record that
is not `ok`, after observing the page for any `unknown` record.

## Which steps can resume

| Record | Resume |
|---|---|
| `ok` write | Never re-sent. It was dispatched, and `ok` does not prove its outcome either. |
| `ok` read | May run again; its old result can be stale. |
| `skipped` | Safe to send: it never started. |
| `failed` | Safe to send (`retryable: true` or nothing dispatched); follow its `hint` first. |
| `held` | Nothing ran; it needs a direct, confirmed call (ask-before-act tokens are never honored inside containers). |
| `unknown` read | Safe to send. |
| `unknown` write | Blocked until the caller observes the page and states that the effect did not happen. |

`navigate`, `newTab`, `closeTab` and `switchTab` count as writes: re-sending them can
reset a form, duplicate a tab or close the wrong one.

## Proposed resume call

1. The bridge keeps the checkpoint of the last N container calls in memory, keyed
   by the audit `call` id and bounded like the audit log. A checkpoint stores, per
   command, its `progress` record and a fingerprint of the action and parameters
   (the hash `_outcome_key` already uses), never the parameters themselves, so typed
   values and secrets are not retained. Responses add the `call` id.
2. The caller resends the same commands with `resume: {call, observed: {index: "not_applied"}}`.
   The bridge refuses the call (`resume_mismatch`, nothing dispatched) if the
   fingerprints differ from the checkpoint, or if the checkpoint is gone.
3. `ok` writes are not sent: their result is `{resumed: "already_ran"}` and their
   record stays `ok`. `ok` reads run again when a later command substitutes their
   result (`${$N...}`), so the substitution reads fresh state.
4. An `unknown` write runs only if `observed` lists its index; otherwise the call
   stops there with that record still `unknown`, like a held step. In ask mode the
   repeat is still held as `unknown_outcome` until confirmed.
5. Everything from the first remaining command runs as usual, with the same tab
   checks: a `parallel` resumes per sequence, tab ownership is checked again, and a
   tab closed in the meantime fails with `tab_closed` instead of being replaced.

A bridge restart loses the checkpoints and the audit log. Resuming after a restart
falls back to observing the page; persisting checkpoints would need a decision on
where they live and how long.

## Follow-ups

- Implement the resume call above (checkpoint store, `call` id in responses,
  `resume` parameter, `resume_mismatch`).
- Align the MCP request timeout of `browser_batch`/`browser_parallel` (the sum of
  the waits plus 15 s) with the bridge deadline (at least 300 s). Until then an MCP
  client can give up while the bridge still runs the batch; `browser_audit` shows
  which steps ran.
