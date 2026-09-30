# TWPR — The Weekly Petroleum Report

A systematic weekly options setup on MCX CrudeOil, trading the gap between
analyst consensus and the EIA Weekly Petroleum Status Report. Options buyer
only, never a seller, risking 1% of capital per event.

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

## EIA actuals

Captures the EIA Weekly Petroleum Status Report into `data/eia_actuals.json`:
crude, Cushing, gasoline and distillate stock changes (million barrels, negative
= draw). Both sites race and the first to return the complete report wins.
investing.com's week-over-week refinery utilisation *change* is included as
`refinery_util_change_pct` (best effort; `null` if it doesn't arrive in time). The
utilisation level itself is not available from either site.

```bash
python -m app.eia_actuals                           # today's report, polls until it is out
python -m app.eia_actuals --once
python -m app.eia_actuals --date 23-09-2026 --once  # replay a release
```

## Signal engine

Applies the WPSR runbook model (`docs/WPSR_WEDNESDAY_RUNBOOK.md` and the MCX options
adaptation) and writes `data/signal.json`: the total liquid surprise (TLS) and its
Z-score against the last 12 weeks, the Cushing check, the regime (1 aligned, 2 fade,
3 sell-the-fact, or stand down when |Z| < 1.25), the expected WTI / MCX move, the ITM
option to buy (delta 0.60-0.70, or 0.80-0.85 when OVX is above 35), the expiry, lots for
1% risk, the IST clock and a checklist of what only the chart can settle. The rules are
deterministic; the optional Groq paragraph is narrative only and never affects the trade.

```bash
python -m app.market_data                  # ATR, OVX, spreads -> data/market.json (run before the print)
python -m app.surprise_history --backfill  # one-off: the weekly surprise history that sets sigma
python -m app.signal_engine                # the five data files -> data/signal.json
python -m app.signal_engine --allow-stale  # replay older files
```

Set `ACCOUNT_EQUITY_INR` in `.env` for lot sizing (and `MAX_LOTS=1` for the runbook's
first-three-weeks cap). Needs `GROQ_API_KEY` (and a `GROQ_MODEL` your account can use)
for the narrative, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for alerts. All optional.

## Telegram alerts

`python -m app.telegram_bot` sends the signal to your Telegram chat. It refuses a
signal older than 2 days (so a failed engine run can't resend last week's trade);
`--allow-stale` sends a replay clearly marked as one. Every pipeline script also
alerts Telegram if it fails. Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

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
