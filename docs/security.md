# Security

The bridge protects against:

- **Web pages calling the local API.** The HTTP API needs a bearer token and sends no CORS headers.
- **DNS rebinding.** Requests with a `Host` other than `127.0.0.1`/`localhost` are rejected.
- **Pages posing as the extension.** The WebSocket accepts only `moz-extension://` origins.

It does not protect against:

- **Prompt injection.** A page can contain instructions, and the agent acts with every account
  you are signed into.
- **Local processes** running as your user, which can read the token.
- **Python scripting.** `browser_run_python` (on by default) runs local Python with host file,
  network and environment access. It is **not a sandbox**: code can bypass the browser API.
  Require approval for it in the extension's options, or set `KITSUNE_SCRIPTING=0` for clients
  you don't trust with local code.
- **Sensitive page data.** Read tools redact passwords, one-time codes, card numbers, secret-named,
  masked and autofilled fields ([Sensitive-field redaction](api-reference.md#sensitive-field-redaction)),
  but detection is heuristic and `browser_evaluate` can read anything the page can.
- **Typed values outside the bridge.** The audit log redacts typed text and stored secrets
  ([Audit log](api-reference.md#audit-log)), but the page and the client's transcript see them.
- **Cookies, storage, sessions and the clipboard.** These tools need your approval by default;
  lift it only for agents you trust with your accounts.
- **History and bookmarks.** Off until you allow it in Kitsune's options. Lookups are scoped
  (30 days by default), redact token-like URL values and are audited
  ([History and bookmarks](api-reference.md#history-and-bookmarks)).
- **Authenticated actions.** MCP annotations are client hints, not approvals.
  [Tool approvals](your-tabs.md#tools-that-need-your-approval) and
  [ask-before-act](your-tabs.md#ask-before-act) hold actions for you. Approval in the client's
  prompt bypasses the agent; the `confirm_token` fallback is a second step, not proof of a human.

Approvals, user-tab protection and `browser_set_policy` are checks, not a sandbox: local programs
with the token are not restricted.

**Recommended:** use a separate browser profile with only the accounts the agent needs (no
banking, mail or work SSO), require approval for actions you want to see first, and treat saved
sessions, screenshots and logs as sensitive. Never paste real secrets into bug reports.
