# Newegg — Scraping Playbook

## Problem
Newegg's SubCategory URLs (e.g. `/gaming-pcs/SubCategory/ID-3319`) with facet
filter params (`N=4131`) silently redirect to completely unrelated categories.
In testing, the gaming desktop category URL redirected to **Correction Fluids &
Erasers** — the `N=` facet ID had been reassigned. The page title changes but
the URL stays the same, making it non-obvious that you're on the wrong page.

## Root Cause
Newegg's facet/filter parameter `N=` is an internal numeric ID that gets recycled
and reassigned across categories. Bookmarking or hardcoding these URLs breaks
silently over time. The `SubCategory/ID-XXXX` segment is also unstable.

## What Does NOT Work
```
https://www.newegg.com/gaming-pcs/SubCategory/ID-3319?N=4131&Price=-200000
https://www.newegg.com/gaming-pcs/SubCategory/ID-3319?Price=-200000&Order=RATING
```
Both redirected to office supplies (Correction Fluids & Erasers) in March 2026.
Always verify with `document.title` before trusting results — if the title doesn't
match your category, you've been silently redirected.

**Use `expectTitle` on navigate** to catch redirects automatically — ZenLink will
return a `warning` and `redirected: true` if the page title doesn't match.

## What WORKS — The `/p/pl` Search URL

### Stable search URL format
```
https://www.newegg.com/p/pl?d=gaming+desktop+rtx&Order=RATING&Price=-200000
```
- `d=` is the search query — use keywords, not category IDs
- `Order=RATING` sorts by customer rating
- `Price=-200000` caps at $2000 (Newegg uses cents: 200000 = $2000)

### Full extraction sequence
```json
{ "action": "navigate", "url": "https://www.newegg.com/p/pl?d=gaming+desktop+rtx&Order=RATING&Price=-200000", "expectTitle": "gaming" },
{ "action": "sleep", "ms": 3000 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{
  "action": "js",
  "code": "JSON.stringify([...document.querySelectorAll('.item-cell')].slice(0,10).map(el => ({ name: el.querySelector('.item-title')?.innerText?.trim(), price: '$' + el.querySelector('.price-current strong')?.innerText?.trim(), rating: el.querySelector('.item-rating-num')?.innerText?.trim() })).filter(x => x.name && x.price !== '$undefined'))"
}
```

## Key Selectors (verified 2026-03-12)
| Data | Selector |
|------|----------|
| Product card container | `.item-cell` |
| Product name | `.item-title` |
| Price (whole dollars) | `.price-current strong` |
| Price (cents) | `.price-current sup` |
| Rating / review count | `.item-rating-num` |

**Price reconstruction:**
```js
const dollars = el.querySelector('.price-current strong')?.innerText?.trim();
const cents = el.querySelector('.price-current sup')?.innerText?.trim();
const price = dollars ? `$${dollars}${cents ? '.' + cents : ''}` : null;
```

## Notes
- **Always validate `document.title`** after navigation before extracting — silent
  redirects are Newegg's biggest gotcha.
- The `/p/pl?d=` format is the most stable URL pattern — it's the same URL
  structure their own search bar uses, so it won't go stale.
- Refurbished and used listings appear in results — filter by checking if the name
  contains "REFURBISHED" or "USED" if you want new-only.
- `N=` facet params are ephemeral — never hardcode them.
- `.item-cell` selector works reliably on the `/p/pl` results page.
