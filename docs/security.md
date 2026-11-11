# Security

What the bridge protects against:

- **Web pages calling the local API.** The HTTP API needs the bearer token and
  sends no CORS headers, so a page in the browser cannot use it.
- **DNS rebinding.** Requests with a `Host` other than `127.0.0.1` / `localhost`
  are rejected.
- **Web pages posing as the extension.** The WebSocket only accepts
  `moz-extension://` origins. Before this check, any page could connect and
  take over the extension's slot.

What it does not protect against:

- **Prompt injection.** A page the agent reads can contain instructions. The
  agent acts with every account you are logged into.
- **Local processes** running as your user can read the token.
- **Opt-in Python scripting.** `KITSUNE_SCRIPTING=1` exposes `browser_run_python`,
  which executes ordinary local Python with host file/network/environment access.
  It is not a sandbox. Its supplied browser API uses the MCP client's permissions,
  but code can deliberately bypass that API, read local credentials or create its
  own connection. Mode/capability settings are not confinement for that code.
  Enable it only for clients trusted with local code execution; its timeout/output
  limits are lifecycle controls, not OS resource or security boundaries.
- **Sensitive page data.** A connected agent can read page content and execute
  JavaScript in pages. Structural read tools (`browser_snapshot`, `browser_forms`,
  `browser_dom`, `browser_query`, `browser_html`, `browser_find`) redact
  password, one-time-code, card-number/CSC, secret-named, CSS-masked
  (including masked textareas and contenteditable regions) and autofilled
  fields, including web-component fields with a shadow root, and leave out
  CSS-masked static text. Selectors that probe a sensitive field's `value`
  attribute are refused; see
  [Sensitive-field redaction](api-reference.md#sensitive-field-redaction).
  This is not a sandbox: `browser_evaluate` can read anything the page can, and the
  detection is heuristic. Do not assume a visually masked field is hidden from
  the agent or its logs.
- **Typed values outside the bridge.** The bridge audit log (`browser_audit`)
  keeps the action, target, tab, timing and outcome of each write action but
  replaces typed text (`browser_type`, `browser_fill_form` values,
  `browser_press_key` text, `browser_select_option` values, `browser_drop` data and
  `browser_handle_dialog` prompt answers) and stored secrets (`browser_cookies`,
  `browser_storage` and `browser_clipboard` values) with `"[redacted]"`; see
  [Audit log](api-reference.md#audit-log). The page and the MCP client's own
  transcript still see whatever the agent sent.
- **Cookies, page storage, saved sessions and the clipboard.** Off unless you name
  the `storage` capability group; `KITSUNE_CAPS=all` leaves it out. Its tools read and
  write the cookies that keep you logged in, page storage and the system clipboard:
  enable it only for agents you trust with your accounts.
- **History and bookmarks.** Off unless you enable the `history` capability group
  *and* allow it on Kitsune's options page (about:addons → Kitsune → Options);
  revoke it there or in the add-on's Permissions tab. Lookups return only URLs,
  titles, visit times and bookmark folders inside the bridge's history scope
  (by default the last 30 days), with token-like URL values replaced by
  `[redacted]`, and every lookup is audited without its result URLs; see
  [History and bookmarks](api-reference.md#history-and-bookmarks). Titles and
  URLs can still be personal: enable it only for agents you trust with them.
- **Authenticated actions.** Existing sessions let the agent act as you on
  accessible sites. MCP annotations are client hints, not an approval system.
  [Ask-before-act](your-tabs.md#ask-before-act) holds actions for your approval. In the client's prompt
  (elicitation) the approval does not pass through the agent; the confirm token, the
  fallback for clients without elicitation, is an explicit second step, not human
  approval.

Kitsune does not provide a password-vault listing tool. Native password-manager
autofill is browser-controlled; whether an autofilled value is accessible depends
on what the browser makes available to the page. Test it only with disposable
credentials, and never paste real secrets into bug reports.

Capability groups bound what an agent can reach through one MCP server, and the bridge
checks them on every request, container steps included. They are not a sandbox: the
`kitsune-bridge` command and other local programs with the token are not restricted.

Recommended: use a separate browser profile that only has the accounts the
agent needs, and keep banking, mail and work SSO out of it. Enable only the
capability groups you need, review the agent's actions, and treat saved sessions,
screenshots, and tool logs as potentially sensitive.
