# Kitsune API Reference

Base URL: `http://127.0.0.1:47651`

## MCP tool surface

`KITSUNE_CAPS=core` is the default (30 tools); `KITSUNE_CAPS=all` registers
95 tools. The groups are `core`, `tabs`, `network`, `storage`, `parallel`,
`devtools`, `windows`, and `session`. A comma list selects those groups only.
See [README](../README.md#tools) for the complete tool list per group.
The bridge endpoints below remain available unchanged; MCP tool names and
parameters are separate from HTTP endpoint names and JSON command actions.

### `browser_tabs(action="list", tab_id=None, url=None, force=False, raw=False)`

Replaces `browser_new_tab`, `browser_close_tab`, and `browser_switch_tab`.
`action` is `list`, `new`, `close`, or `select`. List uses GET `/api/tabs`;
new uses POST `/api/new-tab` (default URL `about:blank`), close uses
`/api/close-tab`, and select uses `/api/switch-tab`. Close and select require
`tab_id`. `force` disarms beforeunload for close; `raw` bypasses the image URL
viewer for new tabs. Options for other actions are ignored.

### `browser_page_text(tab_id=None)`

Replaces the duplicate `browser_page_text_by_tab_id`. Uses GET `/api/page-text`
for the active tab or POST to the same endpoint with `tabId` for a specific tab.

### `browser_type(selector, text, mode="type", clear=False, tab_id=None)`

Replaces `browser_fill` and `browser_set_editable_content`.

| Mode | Behavior | Bridge endpoint |
|---|---|---|
| `type` | Type using events; `clear=True` clears first | `/api/type` |
| `fill` | Always replace the field value with native setter and input/change events; ignores `clear` | `/api/fill` |
| `editable` | Insert plain text into contenteditable/input/textarea; append by default, replace with `clear=True` | `/api/set-editable-content` |

Editable mode sends `format="text"`. Callers migrating from the old editor tool
should pass `clear=True` to retain its replace-by-default behavior. For trusted
HTML insertion use `browser_js`; the underlying endpoint still accepts `format="html"`.

### `browser_wait_for(selector=None, text=None, text_gone=None, js=None, url=None, title=None, network_idle=False, time=None, timeout=10000, tab_id=None)`

Replaces `browser_wait_for_element`, `browser_wait_for_result`,
`browser_wait_for_url`, `browser_wait_for_title`, and `browser_wait_for_network_idle`.
Exactly one condition must be supplied, including `time=0` or an empty string.
`network_idle=False` does not count as a condition. Invalid combinations return
an error before contacting the bridge. `timeout` must be positive.

| Condition | Meaning | Bridge endpoint |
|---|---|---|
| `selector` | Wait for a visible CSS element, poll every 200 ms | `/api/wait-for-element` |
| `text`, `text_gone` | Literal substring of `document.body.innerText` appears/disappears, poll every 500 ms | `/api/wait-for-result` |
| `js` | Expression returns a non-empty result, poll every 500 ms | `/api/wait-for-result` |
| `url`, `title` | JavaScript regex pattern matches | `/api/wait-for-url`, `/api/wait-for-title` |
| `network_idle=True` | No new network resources for 500 ms | `/api/wait-for-network-idle` |
| `time` | Sleep a finite, non-negative number of seconds in the MCP server | None |

`timeout` is in milliseconds and does not limit an explicit sleep. JS retains
bridge non-empty-result semantics: `false` and `0` count as results; return
`null` or an empty string while waiting for a predicate. Text waits use POST `/api/wait-for-text`.

### `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False)`

Replaces `browser_element_screenshot` and `browser_full_page_screenshot`.
Returns `[Image, metadata]` (MCP image content) by default; `save_to` writes the
full PNG and returns metadata with `saved_to` instead, and `data_url=True` adds the
raw PNG data URL. `selector` and `full_page=True` are mutually exclusive.

- Default: viewport capture via POST `/api/screenshot`. A supplied `tab_id`
  selects that tab and focuses its window before capture.
- `selector`: crop one element via POST `/api/element-screenshot`.
- `full_page=True`: scroll and stitch up to 30 viewports via POST `/api/full-page-screenshot`.

Element/full-page capture temporarily activates the target tab. `save_to`
overrides the file destination; `data_url=True` additionally returns `dataUrl`.
The former element tool's `scale` option remains available through the bridge
endpoint; the merged MCP tool uses its default scale of 1.

### WordPress workflow

`browser_wp_html` is removed. Run
`browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`
from a browser logged into WordPress. The workflow navigates the active tab,
waits for Elementor preview widgets, then extracts their HTML. Its final step
returns the JSON string containing widget data inside the workflow's `results`.

## HTTP bridge endpoints

Requests require a bearer token from `~/.kitsune/token` (or `KITSUNE_TOKEN`).

## Status & Info

### GET /api/status
Connection status and config.
```json
{ "status": "running", "extension_connected": true, "port": 47651, "ws_port": 47652, "screenshot_dir": "..." }
```

### GET /api/version
Bridge and extension version info.
```json
{ "bridge": "1.1.2", "api": "1", "extension_connected": true, "extension": 11 }
```

## Page Data (GET, cached 5s except page-info)

### GET /api/page-info
Page metadata plus current dialog policy and recent dialog history. Never cached.
Also accepts POST with `{ "tabId": 123 }`. MCP tool: `browser_page_info(tab_id=None)`.
```json
{ "url": "...", "title": "...", "lang": "en", "width": 890, "height": 992, "forms": 0, "links": 1, "images": 0 }
```

### GET /api/page-text
All visible text on the page.
```json
{ "text": "..." }
```

### GET /api/tabs
All open browser tabs.
```json
{ "tabs": [{ "id": 1, "url": "...", "title": "...", "active": true, "windowId": 1, "index": 0 }] }
```

### GET /api/dom
Accessibility tree (depth=6).
```json
{ "tree": { "ref": "r0", "tag": "body", "children": [...] } }
```

### GET /api/forms
All form fields on the page.

### GET /api/screenshot
Captures the active page as PNG and saves to the bridge screenshot directory
(`$KITSUNE_HOME/screenshots`, default `~/.kitsune/screenshots`).
```json
{ "url": "...", "title": "...", "saved_to": "...", "filename": "zen_20260313_120000.png" }
```

### POST /api/screenshot, /api/element-screenshot, /api/full-page-screenshot
Return `dataUrl` and capture metadata. Set `imagePreview: true` for an inline preview
under roughly 1 MB (large images are JPEG-encoded and downscaled in the extension).
Without that flag, return full PNG data. Element requests require `selector` and
accept `tabId` and `scale` (1–4); full-page requests accept `tabId`. Captures retain
existing canvas and scroll/segment limits and can truncate very large documents.

MCP tool `browser_screenshot(selector=None, full_page=False, tab_id=None, save_to=None, data_url=False)`
returns `[Image, metadata]` by default, giving the model actual MCP image content.
`save_to` writes a full PNG and returns metadata with `saved_to` instead.
`data_url=True` returns raw PNG data in metadata.
Inline preview dimensions can be smaller than the original capture metadata.

### POST /api/handle-dialog
`{ "accept": true, "prompt_text": "answer", "tabId": 123 }`

MCP tool: `browser_handle_dialog(accept=True, prompt_text=None, tab_id=None)`.
Sets the tab's policy for subsequent dialogs and returns `{ok, policy, dialogs}`.
By default, with no policy, original native alert/confirm/prompt functions run and
the user's actual response is recorded. `beforeunload` also keeps native behavior.
Only an explicitly configured policy auto-answers dialogs. Clear a policy with
`browser_handle_dialog(accept=None, tab_id=123)` or HTTP
`{ "accept": null, "tabId": 123 }` to restore native pass-through.
Accepted prompts return `prompt_text` if supplied, otherwise the prompt's default;
dismissed prompts return null and dismissed confirms return false. With a policy
set, alerts return immediately; in native mode they wait for the user.
Recent history is also included in `browser_page_info`:

```json
{ "policy": { "accept": true, "prompt_text": "answer" }, "dialogs": [
  { "type": "prompt", "message": "Name?", "default_value": "Guest",
    "timestamp": 1792200000000, "response": "answer", "pending": false }
] }
```

The page-world override records every intercepted invocation (last 100 per
document). Answers are synchronous; `pending: false` means the user's native
response or configured automatic answer was already returned, and a later tool
call cannot change it. Alert responses are null (no return value). Policies
persist across navigation until tab closure or extension restart, with native behavior
applying until asynchronous restoration completes. Clearing a policy also removes
it from the per-tab policy store. Capture starts at
`document_start` for new documents and on connection for existing tabs.
Scripts holding original references before injection, iframe dialogs (including
cross-origin frames), browser-internal pages, and already-open native dialogs are
outside this mechanism. Page scripts can also replace or bypass the overrides.

`accept: true` also disarms `beforeunload` using the same returnValue/preventDefault
suppression as `browser_close_tab(force=True)`, allowing navigation away. Unload
events are recorded with type `beforeunload`. `accept: false` leaves native unload
behavior; the extension cannot answer a native “Leave page?” prompt. Accepting
temporarily clears `onbeforeunload`; clearing or dismissing the policy restores the
saved handler if the page has not supplied a replacement.
Force-closing remains available regardless of dialog policy.

### POST /api/wait-for-text
`{ "text": "Ready", "text_gone": "Loading", "timeout": 10000, "tabId": 123 }`

MCP tool: `browser_wait_for(text=...)` or `browser_wait_for(text_gone=...)`.
Provide at least one non-empty condition (the HTTP endpoint accepts both at once). Polls rendered top-level document text
about every 100 ms, excluding hidden text; if both conditions are provided they
must both hold. Matching uses case-sensitive substrings. Frames and shadow roots
are not traversed. Returns `{ok, text, text_gone, elapsed}`; timeout returns
`ok: false` with an error. A zero timeout performs one immediate check.

## Navigation (POST)

### POST /api/navigate
`{ "url": "https://example.com", "tabId": 1, "expectTitle": "Example" }`
- `tabId` optional (defaults to active tab)
- `expectTitle` optional — warns if title doesn't match (catches silent redirects)

### POST /api/new-tab
`{ "url": "https://example.com" }` — url optional, defaults to about:blank.
Returns `{ "ok": true, "tabId": 123 }`

### POST /api/switch-tab
`{ "tabId": 123 }`

### POST /api/close-tab
`{ "tabId": 123 }`

## Interaction (POST)

### POST /api/click
`{ "selector": "button.submit" }` — also accepts `ref`, `position`.

### POST /api/type
`{ "text": "hello", "selector": "input" }`

### POST /api/fill
`{ "selector": "input#email", "value": "user@example.com" }`
Handles shadow DOM, React/Vue state, with automatic fallbacks.

### POST /api/scroll
`{ "direction": "down", "amount": 500 }`

### POST /api/hover
`{ "selector": "a.link" }`
Returns `{ "ok": true, "tag": "a" }`

### POST /api/find
`{ "query": "search text" }` or `{ "selector": "a.link" }`
Both `query` and `selector` are accepted (normalized to `query` internally).
Returns `{ "results": [{ "ref": "r0", "tag": "a", "text": "...", "selector": "...", "bounds": [...] }] }`

### POST /api/js
`{ "code": "document.title" }`
Returns `{ "result": "..." }`. Results capped at 50KB with truncation flag.

### POST /api/highlight
`{ "selector": "h1" }` — draws red border overlay on element.

### POST /api/clear-highlight
No body needed. Removes all highlights.

## Waiting (POST)

### POST /api/wait-for-element
`{ "selector": ".loaded", "timeout": 10000, "pollInterval": 200 }`
Polls until element appears. Returns `{ "ok": true, "found": true, "ref": "r0", "elapsed": 1200 }`

### POST /api/wait-for-result
`{ "code": "document.querySelector('.data')?.textContent", "timeout": 15000, "pollInterval": 500 }`
Polls JS expression until non-empty. Returns `{ "ok": true, "result": "...", "elapsed": 2000, "polls": 4 }`

### POST /api/page-text-by-tab-id
`{ "tabId": 123 }` — get text from a specific tab (not just active).

## Batch Execution (POST)

### POST /api/batch
Execute multiple commands sequentially or in parallel.
```json
{
  "commands": [
    { "action": "navigate", "url": "https://example.com" },
    { "action": "sleep", "ms": 2000 },
    { "action": "pageText" }
  ],
  "stopOnWarning": false
}
```

**Parallel sequences** (commands within each sequence run sequentially, sequences run concurrently):
```json
{
  "commands": [{
    "action": "parallel",
    "sequences": [
      [{ "action": "navigate", "tabId": 1, "url": "https://a.com" }, { "action": "pageText", "tabId": 1 }],
      [{ "action": "navigate", "tabId": 2, "url": "https://b.com" }, { "action": "pageText", "tabId": 2 }]
    ]
  }]
}
```
Note: `navigate` and `switchTab` inside parallel sequences **must** have explicit `tabId`.

**Batch actions:** navigate, newTab, closeTab, switchTab, click, type, fill, scroll, hover, find, js, pageInfo, pageText, pageTextByTabId, tabs, forms, dom, highlight, screenshot, waitForElement, waitForResult, sleep, parallel.

## Workflows (NEW)

### GET /api/workflows
List available workflow files.
```json
{ "workflows": ["test", "amazon-tracking"], "directory": "D:\\ZenLink\\workflows" }
```

### POST /api/workflow
Execute a named workflow from the workflows directory.
```json
{ "name": "test", "variables": { "url": "https://example.com" } }
```
Workflows are `.md` files with JSON code blocks. Variables use `{{name}}` syntax.
Returns `{ "workflow": "test", "steps": 4, "results": [...] }`

## Cache Control (NEW)

### POST /api/cache
Control the response cache (pageInfo, pageText, tabs are cached for 5s by default).

Clear cache: `{ "action": "clear" }`
Check status: `{ "action": "status" }` → `{ "entries": 3, "ttl": 5 }`
Set TTL: `{ "action": "ttl", "seconds": 10 }`

## Error Handling

All endpoints return `{ "error": "message" }` on failure. Common errors:
- `"Zen Browser extension not connected"` — extension not loaded or browser closed
- `"Extension disconnected"` — extension dropped mid-command (fails fast, no timeout wait)
- `"Command timed out after Ns"` — extension didn't respond in time
- `"Cannot access browser internal page: about:..."` — can't inject into restricted pages
