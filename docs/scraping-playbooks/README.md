# Scraping playbooks

Verified selectors, URL patterns and workarounds for sites that needed them. Check here before
writing a scraping batch for a known site, and add a playbook when you solve a new one.

Steps are [`/api/batch`](../api-reference.md#post-apibatch) commands: run them with
`browser.batch(...)` in `browser_run_python`, or `browser_batch` on the legacy surface. `js` and
`waitForResult` need the developer build.

| Site | Challenge | Fix | Playbook |
|------|-----------|-----|----------|
| Amazon | Bot detection on JS injection | Navigate normally, scroll, read the rendered DOM | [amazon.md](./amazon.md) |
| Best Buy | Lazy loading | `scroll amount:800`, selector `.product-list-item` | [bestbuy.md](./bestbuy.md) |
| Newegg | Category URLs silently redirect | Use the `/p/pl?d=keywords` search URL | [newegg.md](./newegg.md) |
| B&H Photo | Hashed CSS Modules classes | `[class*="product_"]` wildcards + innerText parsing | [bhphotovideo.md](./bhphotovideo.md) |
| eBay | `li.s-item` gone; hidden placeholder cards | `ul.srp-results > li.s-card` + one `query` per field | [ebay.md](./ebay.md) |

## Common patterns

**Lazy loading.** Scroll about a viewport at a time, pausing for cards to render:

```json
{ "action": "scroll", "direction": "down", "amount": 800 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 800 },
{ "action": "sleep", "ms": 2000 }
```

**Hashed class names.** Use `[class*="prefix_"]`; the prefix before the underscore is stable.

**Bot detection.** Use `navigate` only (no `fetch()` in `js`), scroll before extracting, and gate
extraction with `waitForElement` or `waitForResult`.

**Silent redirects.** `expectTitle` on `navigate` returns a `warning` and `redirected: true` when
the title doesn't match:

```json
{ "action": "navigate", "url": "https://...", "expectTitle": "gaming" }
```

## Adding a playbook

Create `SITENAME.md` with: the problem, its root cause, what does not work, the working sequence
(copy-pasteable JSON), a dated table of verified selectors, and notes. Then add a row above.
