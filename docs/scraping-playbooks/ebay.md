# eBay — Scraping Playbook

## Problem
Search results (`/sch/i.html`) no longer match `li.s-item`, the selector most
eBay scrapers and older answers use: it returns 0 items. Switching to the new
card class looks right but returns **62 items on a 60-item page**, and the card
text carries noise that breaks naive parsing: screen-reader text in titles,
badges glued onto titles, original prices glued onto current prices, and a
reversed "Sponsored" label on every card.

## Root Cause
- The results list is rendered with `s-card` / `su-card-container` components.
- Two hidden "Shop on eBay" placeholder cards (1 px wide, `$20.00`) sit
  **outside** `ul.srp-results` but still have `class="s-card"` and a
  `data-listingid`.
- `ul.srp-results` also holds non-card `li`s (a filter carousel and the
  pagination block).
- Inside a card:
  - `.s-card__title` includes a hidden `<span class="clipped">Opens in a new window or tab</span>`.
  - New listings have a `<span class="s-card__new-listing">NEW LISTING</span>`
    **before** the title span inside `.s-card__title`; some cards have a
    `.s-card__caption` ("NEW LOW PRICE") above the title link.
  - Discounted items render the original price in a `.strikethrough` span in
    the same row as the current price, so the row text reads
    `KRW56,429.79KRW66,392.66`.
  - Every card checked (60/60) has `derosnopS` ("Sponsored" reversed) in its
    footer text; the visible "Sponsored" label is drawn from an SVG background
    image instead.
- Prices are shown in a currency chosen for the visitor (KRW from Korea in
  testing), not by the ship-to country.

## What Does NOT Work

| Selector or approach | Result |
|---|---|
| `li.s-item` | 0 matches |
| `li.s-card`, `[data-listingid]` | 62 on a 60-item page: includes the two placeholders |
| `ul.srp-results > li` | 62: includes the filter carousel and pagination `li`s |
| `.s-card__title` text | Ends with "Opens in a new window or tab" |
| `.s-card__title > span:first-child` | Returns `NEW LISTING` on new listings |
| `.s-card__subtitle` | 61 on 60 cards: one card repeated its condition row |
| `.s-card__subtitle-row:nth-of-type(1) > .s-card__subtitle` | 57: the "NEW LOW PRICE" caption shifts `nth-of-type` |
| `.s-card__price` | 104 spans: includes placeholders and the `to` of price ranges; cannot be aligned with cards |
| Text search for "Sponsored" | Never matches; `derosnopS` was on every card checked, so it cannot classify sponsored listings either |
| `_fcid=<country id>` URL parameter | Changes the **session's ship-to country** (it persists in later searches) and does not change the currency. Avoid it in the user's own browser; see Notes to restore it |

## What WORKS

### Step 1 — Build the search URL
| Parameter | Effect (verified 2026-10-25) |
|---|---|
| `_nkw=mechanical+keyboard` | Keywords |
| `_ipg=120` | Items per page (default 60); returned exactly 120 cards |
| `_pgn=2` | Page number |
| `_sop=15` | Sort shown as "Price + Shipping: lowest first" |
| `LH_BIN=1` | Buy It Now only (all 120 cards showed "Buy It Now") |
| `LH_ItemCondition=1000` | New (all 120 cards showed "Brand New") |

### Step 2 — Extract with one selector per field
Each selector below matched **exactly one element per card** (60/60 and
120/120), so the result arrays line up by index. All text is already rendered
after `navigate`, in a background tab, without scrolling.

```json
[
  { "action": "navigate", "url": "https://www.ebay.com/sch/i.html?_nkw=mechanical+keyboard&LH_BIN=1&LH_ItemCondition=1000&_sop=15&_ipg=120&_pgn=1", "expectTitle": "Mechanical Keyboard" },
  { "action": "query", "selector": "h1.srp-controls__count-heading", "fields": ["text"], "limit": 1 },
  { "action": "query", "selector": "ul.srp-results > li.s-card", "fields": ["data-listingid"], "limit": 120 },
  { "action": "query", "selector": "ul.srp-results > li.s-card .s-card__title > span.su-styled-text", "fields": ["text"], "limit": 120 },
  { "action": "query", "selector": "ul.srp-results > li.s-card .su-card-container__attributes__primary > .s-card__attribute-row:first-child", "fields": ["text"], "limit": 120 },
  { "action": "query", "selector": "ul.srp-results > li.s-card .s-card__link + .s-card__subtitle-row > .s-card__subtitle", "fields": ["text"], "limit": 120 }
]
```

Run it with `browser_batch` (`automation` group). `navigate` is `core` and
`query` is `read`, for example `KITSUNE_CAPS=core,automation,read`. Pass
`limit` to cover the page size (`browser_query` defaults to 50). No step
needs the developer build.

The item URL is `https://www.ebay.com/itm/<data-listingid>`. The card links
are the same URL with tracking parameters.

### Step 3 — Parse the price row
The price row text takes four observed forms:

| Row text | Meaning |
|---|---|
| `KRW39,684.20` | Single price |
| `KRW119,488.26 to KRW142,430.64` | Variation listing: price range |
| `KRW56,429.79KRW66,392.66` | Price, then the strikethrough original price |
| `KRW32,772.94 to KRW72,809.64KRW51,587.28` | Range, then the original price |

```python
PRICE = re.compile(r"(?:[A-Z]{1,3} ?)?[$£€]?\d[\d,]*(?:\.\d+)?")
tokens = PRICE.findall(row)
if " to " in row:
    low, high, was = tokens[0], tokens[1], tokens[2] if len(tokens) > 2 else None
else:
    low = high = tokens[0]; was = tokens[1] if len(tokens) > 1 else None
```

Only KRW prices were observed; check the format before relying on this for
other currencies.

Shipping, buying format and item location are later rows of the same block.
Query `ul.srp-results > li.s-card .su-card-container__attributes__primary`
with `fields: ["text"]` (also one per card) and split the lines: the first
line is the price row, then rows such as `Buy It Now`, `+KRW7,449.60 shipping`
or `Free International Shipping`, and `from China`. The rows that appear vary
by listing, so match them by text, not position.

## Key Selectors (verified 2026-10-25, signed extension build, not signed in to eBay)
| Data | Selector |
|------|----------|
| Result card (excludes placeholders and non-card rows) | `ul.srp-results > li.s-card` |
| Listing ID | `data-listingid` attribute of the card |
| Title | `.s-card__title > span.su-styled-text` |
| "NEW LISTING" badge | `.s-card__new-listing` |
| Price row | `.su-card-container__attributes__primary > .s-card__attribute-row:first-child` |
| Original (strikethrough) price | `.strikethrough` inside the price row |
| Condition | `.s-card__link + .s-card__subtitle-row > .s-card__subtitle` |
| Result count | `h1.srp-controls__count-heading` (e.g. `12,000+ results for mechanical keyboard`) |
| Sort control | `button[aria-label^='Sort']` |

## Notes
- **Currency and ship-to:** the header button `button.gh-ship-to__menu` and
  the results' "Shipping to:" flyout (`button.x-flyout__button`) show the
  ship-to location. To restore it after an accidental change, open the flyout,
  pick the country in its `select` (option values are eBay country IDs, e.g.
  `1` United States, `111` South Korea), and press its `Go` input. A previous
  postal code may not be restored.
- **Relevance:** the default "Best Match" sort mixes in off-topic items and
  sponsored placements. Filter results yourself.
- **Sort order is not by the displayed price:** `_sop=15` sorts by price plus
  shipping, and variation listings show a range. Re-sort client-side if order
  matters.
- **Validate the page before parsing:** compare the card count with the
  `_ipg` you asked for. A count that is 2 higher means the placeholders slipped
  into the selector.
- The image selector `.su-card-container__media li:first-child img.s-card__image`
  matched 0 cards; images were not verified.
- Item pages (`/itm/…`) were not covered by this playbook.
