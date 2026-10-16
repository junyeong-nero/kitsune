# Best Buy — Scraping Playbook

## Problem
Best Buy's search/category pages lazy-load product cards via intersection observers.
The `.sku-item` selector (visible in DevTools on a fully loaded page) returns empty
when queried immediately after navigation or when the tab is not the active/focused tab.
Even after `waitForElement` reports a match, the list may be skeleton placeholders
rather than real product data.

## Root Cause
Best Buy uses React with virtual/windowed rendering. Product cards only fully hydrate
when they enter the viewport. Running the tab in the background (e.g. in a `parallel`
batch sequence) means the intersection observers never fire — nothing scrolls into view
because the tab has no visible viewport.

Additionally, `.sku-item` is **not the real product container** in 2026. The live
selector is `.product-list-item` (confirmed March 2026).

## What Does NOT Work

**Do NOT use `.sku-item`** — this was the old selector and returns 0 results as of 2026.

## What WORKS

### Step 1 — Navigate to a search URL with filters baked in
```json
{ "action": "navigate", "url": "https://www.bestbuy.com/site/searchpage.jsp?st=gaming+desktop+pc&intl=nosplash&price=PCE_0~PCE_200000&sort=BESTSELLING", "expectTitle": "gaming" }
```

### Step 2 — Scroll to trigger lazy loading, then wait for results
```json
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 2000 }
```
`amount: 1` = one full viewport height. Each scroll triggers intersection observers
to render the next batch of lazy-loaded product cards.

### Step 3 — Extract using correct 2026 selectors
```json
{
  "action": "js",
  "code": "JSON.stringify([...document.querySelectorAll('.product-list-item')].slice(0,10).map(el => ({ name: el.querySelector('.product-title')?.innerText?.trim(), price: el.querySelector('[class*=price]')?.innerText?.trim()?.match(/\\$[\\d,\\.]+/)?.[0], rating: el.querySelector('[class*=rating]')?.getAttribute('aria-label') })).filter(x => x.name))"
}
```

## Key Selectors (verified 2026-03-12)
| Data | Selector |
|------|----------|
| Product card container | `.product-list-item` |
| Product name | `.product-title` |
| Price | `[class*=price]` → regex `\$[\d,\.]+` |
| Rating | `[class*=rating]` → `aria-label` attribute |

## Notes
- Must be the **active/focused tab** for intersection observers to fire, OR use
  the `scroll` action (which triggers `scrollBy` internally and works in background tabs).
- Parallel batch sequences may fail on Best Buy because background tabs don't
  always trigger lazy loading — run sequentially.
- 500+ `[class*=product]` elements exist on the page — don't use that as a readiness
  check. Use `.product-list-item` count > 0 instead.
