# Optional bounded Python composition

This guide adds composition to the same flat operations, not new browser powers.
Use browser_script only when a loop, branch or result filter is useful. Direct
calls remain preferable for one click/read, unclear page structure and handoff.
The API is injected: `browser`, `emit(value)`, `emit_image(handle)` and
`BrowserOperationError`. Do not connect/import a credential-bearing client.
Top-level `await` is supported; calls are sequential and locals are fresh per run.

Method names/keyword arguments match the direct schemas with `browser_` removed:
read, query, find, click, input, select, check, press, hover, drag, scroll, wait,
screenshot, request_tab, ask_user, resume. The two family mappings are:

- `await browser.tabs()` → tabs list; `new_tab(url=...)` → tabs open;
  `close_tab(tab_id=...)` → tabs close.
- `navigate(tab_id=..., url=...)` → navigate goto; `back`, `forward`, `reload`
  take tab_id and optional timeout_ms, not an action or URL.

All method arguments are keyword-only. Omitted arguments use the direct schema's
defaults; optional Python None means omit the field, not JSON null. Times are
milliseconds; scroll amounts are CSS pixels (500 by default for cardinal
scrolling, omitted for top/bottom). Methods return the same Result dictionary.
A failed operation raises BrowserOperationError with its full `.result`; catching
it does not erase the coordinator's ledger. There is no nested script method,
page evaluate, raw request, policy setter or host filesystem/network/shell API.

Inspect targets before writing a repeated program. For example, given two URLs:

```python
pages = []
for url in ['https://example.test/alpha', 'https://example.test/beta']:
    opened = await browser.new_tab(url=url)
    tab_id = opened['data']['tab_id']
    page = await browser.read(tab_id=tab_id, format='text')
    text = page['data']['text']
    pages.append({'tab_id': tab_id, 'excerpt': text[:200],
                  'excerpt_cut': len(text) > 200, 'source_truncated': page['truncated']})
emit(pages)
```

A screenshot returns an image handle, not a path or base64 text:

```python
capture = await browser.screenshot(tab_id='t_from_this_session')
emit_image(capture['data']['image'])
```

Keep unknown writes and prior completed work out of retry loops. A held operation
may emit its call/resume_token; only after external approval should the caller
launch a fresh continuation program. If already approved, skip resume and observe.
No persistent instruction pointer or Python state is restored by resume.

Limits: source 32 KiB UTF-8; selected JSON/stdout/stderr 16 KiB; 100 requested
operations, one in flight; 30 s default / 120 s maximum run deadline. Individual
operation timeouts are clipped by remaining run time. Image output is at most
four images, 8 MiB each and 16 MiB total. Emit only needed results and preserve
truncation flags. The coordinator adds the trusted ledger even if code prints
success or catches an error. A completed program does not prove task success.

The intended later JS API uses the same method/field names, one options object and
Promise rejection with `.result`, for example
`await browser.read({tab_id: 't_from_this_session', format: 'text'})`.
JS execution is not enabled by this Python pilot. Full declarations and the six
paired review examples are optional reference material, not tool discovery.
