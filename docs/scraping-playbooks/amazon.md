# Amazon.com — Scraping Playbook

## Problem
Amazon's search results page returns empty product cards when using JS injection
(`document.querySelectorAll`) immediately after navigation. Their bot detection
flags programmatic `fetch()`/XHR calls and headless-looking query patterns,
returning either blank results or a CAPTCHA page.

## Root Cause
Amazon detects query patterns from `document.querySelectorAll` fired too early
or via injected scripts that look automated. They also check for suspiciously
fast post-navigation execution. The page is an SPA that hydrates asynchronously,
so selectors valid in DevTools may return nothing if queried before hydration.

## What Does NOT Work
```json
{ "action": "js", "code": "document.querySelectorAll('[data-component-type=\"s-search-result\"]')" }
```
Fired immediately after `navigate` → returns `[]` even though the page looks
loaded. Amazon may also serve a bot-check interstitial if requests look automated.

## What WORKS — The Scroll-Then-Extract Pattern

### Step 1 — Navigate with a human-style search URL (sorted, filtered)
```json
{ "action": "navigate", "url": "https://www.amazon.com/s?k=gaming+desktop+pc&rh=p_36%3A-200000&s=review-rank" }
```
Use `s=review-rank` for best-rated first. Use `rh=p_36%3A-200000` for under $2000.

### Step 2 — Scroll to trigger hydration
```json
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "waitForElement", "selector": "[data-component-type='s-search-result']", "timeout": 12000 },
{ "action": "scroll", "direction": "down", "amount": 1 }
```
`amount: 1` = one full viewport height. Triggers hydration of lazy-loaded cards.

### Step 3 — Extract AFTER scrolling (products are now rendered)
```json
{
  "action": "js",
  "code": "JSON.stringify([...document.querySelectorAll('[data-component-type=\"s-search-result\"]')].slice(0,10).map(el => ({ name: el.querySelector('h2 span')?.innerText?.trim(), price: el.querySelector('.a-price .a-offscreen')?.innerText?.trim(), rating: el.querySelector('.a-icon-alt')?.innerText?.trim() })).filter(x => x.name))"
}
```

## Key Selectors (verified 2026-03-12)
| Data | Selector |
|------|----------|
| Product card container | `[data-component-type="s-search-result"]` |
| Product name | `h2 span` (inside card) |
| Price | `.a-price .a-offscreen` |
| Star rating | `.a-icon-alt` |

## Notes
- ZenLink runs as a **real browser extension in a real browser session** — this is
  the core advantage. Amazon can't distinguish it from a normal user browsing.
- Never use `fetch()` or `XMLHttpRequest` via `js` action to hit Amazon endpoints —
  those are fingerprinted. Only read the already-rendered DOM.
- If results still empty, add a `sleep` of 1500ms before the JS extraction step.
- The `waitForElement` on the product card selector is the reliable gate —
  don't extract until it returns `found: true`.
- Works whether tab is active or not as long as `waitForElement` confirms load.
