# Rakuten vs Honey cash-back tracker

A static website that tracks the cash-back rate of **every store on Rakuten (rakuten.com) and
Honey (joinhoney.com)**, checks both sites every 12 hours, keeps the history of every rate
change, shows which site pays more for each store, and flags spikes.

- **Site:** https://mt5-byte.github.io/rakuten-honey-tracker/
- **Spike alerts:** the `rate-alerts` issue in this repo gets a comment after every check that
  finds a spike (subscribe to the issue or watch the repo to get them by email), and
  `data/feed.xml` is an RSS feed of the same spikes.

## How it works

```
GitHub Actions (cron 05:17 & 17:17 UTC)
  └─ python -m scraper.run
       ├─ scraper/rakuten.py   all stores from Rakuten's public "All Stores" feed (89 small requests)
       ├─ scraper/honey.py     all US stores from Honey's public store API (12 requests)
       │                       + one-time slug lookups for stores Rakuten has that Honey's list doesn't
       ├─ scraper/matching.py  pairs the two lists by name / slug, remembers pairs in data/store_map.json
       ├─ scraper/history.py   appends a history entry only when a rate changes
       └─ scraper/alerts.py    spike detection → data/alerts.json, data/feed.xml, data/alert_comment.md
  └─ commits data/ back to the repo → GitHub Pages serves index.html + data/
```

The tracker identifies itself with a plain `RakutenHoneyTracker` User-Agent, makes about 100
requests per check with short pauses, and never logs in. A check takes 2–4 minutes.

### Files

| Path | What it is |
|---|---|
| `index.html`, `assets/` | the website (vanilla HTML/CSS/JS, no build step) |
| `scraper/` | the Python tracker (standard library only) |
| `config.json` | schedule label, alert thresholds, watchlist, manual store pairs |
| `data/latest.json` | current snapshot of every store, both sites, highs and "usual" rates |
| `data/history/rakuten.json`, `data/history/honey.json` | change-only history per store: `[timestamp, rate, kind, flags]` |
| `data/store_map.json` | remembered Rakuten↔Honey pairs, lookup misses, cached Honey records |
| `data/alerts.json`, `data/feed.xml` | spikes (JSON and RSS) |
| `data/runs.json` | one line per check: counts and duration |

### Reading the numbers

- Rakuten pays a percentage of the purchase (`kind: "pct"`), occasionally a flat amount
  (`kind: "usd"`). "Up to" means the rate depends on the category.
- Honey pays PayPal Rewards points; the site shows the percentage Honey displays
  (`fixedRatePercent`). Flat offers are shown in points (`kind: "pts"`; 100 points ≈ $1).
- **Boosted** = the site's own previous rate is clearly lower than the current one (Rakuten shows
  this as "was 2%"; Honey exposes the previous offer). The site also shows the duration-weighted
  median of the last 90 days as the "usual" rate once there is history.
- **Spike** = an increase since the previous check that crosses `min_rate_pct` (10%), is up
  `min_increase_points` (5) or more, reaches at least `boost_multiplier` (2×) the usual rate, or
  sets a new all-time high after a week of history. All of these live in `config.json`; add store names or
  keys to `watchlist` to be told about any increase for those stores.

## Running it yourself

```bash
python3 -m scraper.run --data-dir data --config config.json
python3 -m http.server 8000      # then open http://localhost:8000/
```

No dependencies beyond Python 3.11+. The first run on an empty `data/` folder also does the
one-time Honey lookups for stores that Rakuten lists but Honey's main list does not (about 1,800
requests, 4–5 minutes); afterwards those pairs are remembered and misses are only retried every
14 days.

### Fixing a wrong match

Edit `config.json`:

```json
"manual_pairs": { "rakuten-slug": "honey-label" },
"never_pair": ["rakuten-slug"]
```

The slug is the last part of the Rakuten store URL (`rakuten.com/shop/<slug>`); the label is the
last part of the Honey one (`joinhoney.com/shop/<label>`).

## Notes

Rates are public figures displayed by each site and can change at any time; always confirm on
the store page before buying. This project is not affiliated with Rakuten, Honey or PayPal.
