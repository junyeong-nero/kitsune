# ZenLink Scraping Playbooks

> **🤖 AI INSTRUCTION — READ THIS FIRST**
>
> Before writing any scraping batch sequence for a known site, check if a
> playbook exists in this directory. Playbooks contain verified selectors,
> working URL patterns, and documented workarounds from real scraping sessions.
> Using a playbook avoids re-discovering the same failures from scratch.
>
> **Available playbooks:**
> - [`amazon.md`](./amazon.md) — Bot detection, scroll-then-extract pattern
> - [`bestbuy.md`](./bestbuy.md) — Lazy loading, `window.scrollTo` fix, correct 2026 selectors
> - [`newegg.md`](./newegg.md) — Silent category redirects, use `/p/pl?d=` search URL
> - [`bhphotovideo.md`](./bhphotovideo.md) — CSS Modules hashed classes, wildcard selector pattern
>
> If a playbook doesn't exist for the target site, create one after you solve it.

---

## Index

### 🛒 E-commerce Sites

| Site | Main Challenge | Key Fix | Playbook |
|------|---------------|---------|---------|
| **Amazon** | Bot detection on JS injection | Navigate normally, scroll, read rendered DOM only | [amazon.md](./amazon.md) |
| **Best Buy** | Intersection-observer lazy loading | `scroll amount:1` + correct selector `.product-list-item` | [bestbuy.md](./bestbuy.md) |
| **Newegg** | SubCategory URLs silently redirect | Use `/p/pl?d=keywords` search URL instead | [newegg.md](./newegg.md) |
| **B&H Photo** | CSS Modules hashed class names | `[class*="product_"]` wildcard + innerText parsing | [bhphotovideo.md](./bhphotovideo.md) |

---

## Common Patterns (Quick Reference)

### Lazy-loading sites (Best Buy, B&H)
Use `scroll` with `amount: 1` (one full viewport height) to trigger intersection
observers. Repeat with sleeps between to let cards render:
```json
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 1500 },
{ "action": "scroll", "direction": "down", "amount": 1 },
{ "action": "sleep", "ms": 2000 }
```

### CSS Modules sites (B&H and any site with `word_hash` class names)
Use `[class*="prefix_"]` wildcard selectors — the prefix before the underscore
is stable across deploys. See [bhphotovideo.md](./bhphotovideo.md) for the full
pattern and a class-discovery snippet in the appendix.

### Bot-detection sites (Amazon)
- Use real browser navigation via `navigate` action only
- Never use `fetch()` or XHR via the `js` action
- Always scroll before extracting — let the page hydrate
- Use `waitForElement` or `waitForResult` as your gate before extraction JS

### Silent redirect detection (Newegg and others)
Use `expectTitle` on the `navigate` action:
```json
{ "action": "navigate", "url": "https://...", "expectTitle": "gaming" }
```
Returns `warning` and `redirected: true` if the page title doesn't contain
the expected string. No need for manual `document.title` checks.

---

## Adding a New Playbook

When you solve a new site, create `docs/scraping-playbooks/SITENAME.md` with:
1. **Problem** — what failed and why
2. **Root Cause** — the technical reason
3. **What Does NOT Work** — with code examples (saves future re-discovery)
4. **What WORKS** — full working sequence with copy-pasteable JSON
5. **Key Selectors** — table of verified selectors with a date stamp
6. **Notes** — edge cases, expiry warnings, gotchas

Then add a row to the table in this index file.

---

## Contributing Playbooks

Playbooks only help future agents and users if they're checked into the repo.
After creating or updating a playbook:

1. **Stage the files:** `git add docs/scraping-playbooks/`
2. **Commit:** `git commit -m "playbook: add <sitename> scraping workaround"`
3. **Push:** `git push`

If you're an AI agent, ask the user to commit and push on your behalf.
If you're a human contributor, please push your playbooks — they save everyone
(including the AI) from re-discovering the same site-specific failures.
