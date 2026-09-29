# TWPR — The Weekly Petroleum Report

A systematic weekly options setup on MCX CrudeOil, trading the gap between
analyst consensus and the EIA Weekly Petroleum Status Report. Options buyer
only, never a seller, max 2% of capital on a Grade A trade.

## Status: rebuilding

The previous pipeline (consensus/actuals fetchers, the rule engine, the
expiry gate, currency context, position monitoring/journaling, Telegram
delivery, the test suite, and the GitHub Actions schedule) was removed on
2026-09-29 to rebuild it cleanly. None of that runs right now. What's here
today is the first piece of the rebuild: browser-automation scrapers for the
two consensus/actuals sources.

See [CLAUDE.md](CLAUDE.md) for the current architecture and what's still
missing.

## What's here

`app/scraper/` — Playwright-based scrapers for TradingEconomics and
investing.com economic-calendar pages, the two sources the signal will need
for analyst consensus and EIA/API actuals. Both return the same row shape
(`release_date`, IST `time`, and `actual` / `consensus` / `previous` in million
barrels); only `consensus` may differ slightly, since the sites poll different analyst
panels. Both share one base class (`app/scraper/utils/base.py`) that defines
the response shape, plus parsing helpers in `app/scraper/utils/calendar.py`.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env   # not yet read by any code in this repo
```

## Running the scrapers

Both use relative imports, so run them with `-m` from the repo root:

```bash
python -m app.scraper.sites.tradingeconomics
python -m app.scraper.sites.investing
```

Each opens a visible Chromium window and prints one page's
`{calendar_rows, stats}` for a hardcoded slug (see the `if __name__ ==
"__main__":` block in each file) — useful for confirming the scraper still
parses the live page. Not yet wired into anything.

## Consensus fetcher

Fetches the analyst consensus for the next EIA report and writes
`data/consensus.json`. Both sites are scraped at the same time and the first
valid answer for each indicator wins (the file records which site supplied which
number):

```bash
python -m app.consensus_fetcher                    # next unreleased report
python -m app.consensus_fetcher --date 23-09-2026  # a specific release date
```

Consensus is usually only published close to the release; before that the
command exits non-zero with the reason instead of writing partial data.
"Consensus" is TradingEconomics' *Consensus* and investing.com's *Forecast* (the
same figure), named `consensus` throughout.

## API report monitor

Captures the API (American Petroleum Institute) weekly inventory report into
`data/api_report.json`: crude, Cushing, gasoline and distillate changes in
million barrels, with the source of each. Crude is raced between both sites; the
other three legs come from TradingEconomics only, cross-checked against the dated
crude row so a stale week is rejected. Cushing/gasoline/distillate are 2-decimal.

```bash
python -m app.api_monitor              # latest due report, polls until it is out
python -m app.api_monitor --once       # single attempt
python -m app.api_monitor --date 22-09-2026 --once
```

## Where the URLs live

All slugs are in `app/scraper/sources.py`, keyed by indicator name (`eia_crude`,
`api_cushing`, ...) and site. Fix a moved URL or add a site there; nothing else
holds a URL. Check they still work with:

```bash
python -m pytest tests/test_sources.py -m network -k tradingeconomics
```

## Looking up a specific release

Pass the release date (`DD-MM-YYYY`) to `fetch_release`; you get the matching
calendar row or `None`. Omit the date for the latest released row. Both slug and
date are yours to choose:

```python
from app.scraper.sites.tradingeconomics import TradingEconomicsScraper
from app.scraper.sites.investing import InvestingCalendarScraper

with TradingEconomicsScraper() as s:
    s.fetch_release("united-states/crude-oil-stocks-change", "23-09-2026")

with InvestingCalendarScraper() as s:
    s.fetch_release("eia-crude-oil-inventories-75", "23-09-2026")
# {'release_date': '23-09-2026', 'time': '08:00 PM', 'actual': 2.969,
#  'consensus': -0.6 (TE) / -0.7 (investing), 'previous': -0.64}
```

Or from the command line:

```bash
python -m app.scraper.sites.investing --slug eia-crude-oil-inventories-75 --date 23-09-2026
```

## Known issues

- investing.com: fetch one slug per session — a second page load in the same
  session got 403 on every attempt in testing. Derive what you need from one
  `fetch_page`. It also rate-limits and blocks. A fresh session often gets a 403 that
  a reload clears (the scraper reloads up to twice); sustained 429s from the
  same IP do not clear, so wait or change network. TradingEconomics is the more
  reliable source; treat investing.com as a flaky fallback.
- The browser runs with a visible window (`headless=False`), so it needs a
  display and won't run as-is on a headless CI runner.
