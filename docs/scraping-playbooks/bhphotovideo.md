# B&H Photo Video — Scraping Playbook

## Problem
B&H uses CSS Modules (hashed class names like `product_UCJ1nUFwhh`, `listItem_tixYnI9pMn`)
that change with every frontend deploy. Selectors like `[data-selenium="itemList"]`,
`.product`, or `.item` return nothing because the actual class names are
unguessable at write-time. Additionally, product cards lazy-load via intersection
observers — same as Best Buy.

## Root Cause
B&H's frontend is built with CSS Modules where class names are generated at build
time (e.g. `product_UCJ1nUFwhh` — a stable name + random hash). The hash portion
changes on each deploy, making any hardcoded class name selector expire without warning.
Standard semantic selectors (`data-selenium`, `itemList`, etc.) were present in
older versions but appear removed in 2026.

## What Does NOT Work
```json
{ "action": "waitForElement", "selector": "[data-selenium='itemList']", "timeout": 12000 }
```
Times out — attribute no longer exists on product cards in 2026.

```json
{ "action": "js", "code": "document.querySelectorAll('[class*=product]')" }
```
Matches too broadly (nav items, wrappers, etc.) — not the product cards.

## What WORKS — Wildcard Selector + innerText Parsing

The prefix before the underscore (`product_`) is stable across deploys even when
the hash changes. Use `[class*="product_"]` as the selector.

### Step 1 — Parse innerText (more reliable than child selectors)
B&H product card innerText is structured and parseable. Use regex and string
matching on `el.innerText` rather than child element selectors:
```js
const lines = el.innerText?.split('\n').filter(l => l.trim());
const name = lines?.find(l => l.match(/desktop|gaming|tower|pc/i)
  && !l.match(/compare|cart|stock|reviews|Promo|GHz|DDR|SSD|RTX|More|Save|Free|Add/i));
const priceMatch = el.innerText?.match(/\$(\d[\d,]+)\s*\n?\s*(\d{2})/);
const price = priceMatch ? `$${priceMatch[1]}.${priceMatch[2]}` : el.innerText?.match(/\$[\d,]+/)?.[0];
const rating = el.innerText?.match(/(\d+) Reviews/)?.[1];
```

### Full extraction sequence
```json
{ "action": "navigate", "url": "https://www.bhphotovideo.com/c/search?Ntt=gaming+desktop+pc&MaxPrice=2000&SortOrder=CustomerRankDsc", "expectTitle": "gaming" },
{ "action": "sleep", "ms": 3000 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "js", "code": "JSON.stringify([...document.querySelectorAll('[class*=\"product_\"]')].slice(0,10).map(el => { const lines = el.innerText?.split('\\n').filter(l => l.trim()); const name = lines?.find(l => l.match(/desktop|gaming|tower|pc/i) && !l.match(/compare|cart|stock|reviews|Promo|GHz|DDR|SSD|RTX|More|Save|Free|Add/i)); const priceMatch = el.innerText?.match(/\\$(\\d[\\d,]+)\\s*\\n?\\s*(\\d{2})/); const price = priceMatch ? ('$' + priceMatch[1] + '.' + priceMatch[2]) : el.innerText?.match(/\\$[\\d,]+/)?.[0]; const rating = el.innerText?.match(/(\\d+) Reviews/)?.[1]; return name ? {name: name.trim(), price, rating: rating ? rating + ' reviews' : null} : null }).filter(Boolean))" }
```

## Key Selectors (verified 2026-03-12)
| Data | Selector / Method |
|------|----------|
| Product card container | `[class*="product_"]` (wildcard prefix) |
| Product name | Parse `innerText` lines — filter for gaming keywords |
| Price | Regex `\$(\d[\d,]+)\s*\n?\s*(\d{2})` on `innerText` |
| Rating | Regex `(\d+) Reviews` on `innerText` |

## Notes
- **The `[class*="prefix_"]` wildcard pattern** is the key insight — CSS Modules
  always use `word_hash` format, so the prefix before the underscore is stable
  across deploys. This works for B&H and any other CSS-Modules-based site.
- If `[class*="product_"]` stops matching, re-sniff with the class discovery
  snippet in Step 1 to find the new prefix.
- B&H lazy-loads but `scroll` with `amount: 1` (one viewport height) works reliably (same as Best Buy).
- `SortOrder=CustomerRankDsc` sorts by customer rating descending — most useful
  for "best" queries.
- Watch out for nav/filter `[class*="item_"]` elements — they look similar but
  their `innerText` will be short brand/category names, not product descriptions.

---

## Appendix: Debugging — Class Name Discovery

If `[class*="product_"]` stops matching after a B&H deploy, sniff the live
class names to find the new prefix:
```json
{
  "action": "js",
  "code": "JSON.stringify([...new Set([...document.querySelectorAll('[class]')].flatMap(e => [...e.classList]).filter(c => c.match(/^(item|product|card|result)_[a-zA-Z0-9]+$/)))].slice(0,10))"
}
```
This returns all CSS Modules classes matching the `word_hash` pattern. Look for
the one used on product card containers and update the wildcard selector above.
