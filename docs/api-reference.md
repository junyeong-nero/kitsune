# ZenLink Bridge API Reference

Base URL: `http://localhost:8765`

## Status & Info

### GET /api/status
Connection status and config.
```json
{ "status": "running", "extension_connected": true, "port": 8765, "ws_port": 8766, "screenshot_dir": "..." }
```

### GET /api/version
Bridge and extension version info.
```json
{ "bridge": "1.1.2", "api": "1", "extension_connected": true, "extension": 11 }
```

## Page Data (GET, cached 5s)

### GET /api/page-info
Page metadata. Returns `_cached: true` on cache hit.
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
Captures page screenshot, saves to `~/claude-zen-screenshots/`.
```json
{ "url": "...", "title": "...", "saved_to": "...", "filename": "zen_20260313_120000.png" }
```

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
