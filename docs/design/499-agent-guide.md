# Flat browser operations

List tabs or open a background tab. Keep the returned opaque `tab_id`; there is no
implicit current tab. Every tab operation names its handle. Handles are scoped to
this session/connection generation, not tab indexes. Relist after reconnection.
Reading a user tab does not authorize writing it. Mode, ownership, activity holds,
URL policy and redaction still apply; code cannot grant permission.

Read a full bounded snapshot before choosing targets. Use `{"ref":"e12"}` from
that tab/document or an observed selector such as `{"selector":"#name"}`. Actions
require one match. Navigation or node replacement invalidates old refs: read
again, never guess a replacement or force the first match. Query intentionally
returns multiple rows and only its allowlisted, redacted fields.

Results contain `ok`, `call`, `operation`, `outcome`, `data`, `truncated` and, on
failure, `error`. `dispatched` is not task success; `observed` requires returned
evidence; `unknown` means effects may have happened. Read/wait after acting when
you need evidence. Never automatically repeat an unknown write or an entire
partially completed sequence. Truncated output is not a complete page/table.

Prefer one direct tool for one action or observation. Draft input/select/check do
not submit, but cannot prevent a website's autosave. Read/draft cannot close tabs,
screenshot, press keys or use act-only wait/navigation/resume branches. Startup
schemas show the available operations; no search/publish step is needed.

Use request_tab for scoped permission and ask_user for sign-in, human checks or
trusted input. Only an external user channel approves. Resume continues a held
operation, not a script, completed work or an unknown write. A reply/approval is
not proof the requested page work succeeded; observe again. Retain any returned
continuation exactly, and use it only in the same authorized session before expiry.
