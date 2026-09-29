# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TWPR (The Weekly Petroleum Report) — a systematic weekly options setup on MCX
CrudeOil, the first setup in the TradeDesk platform. **Real money is meant to
trade on this repo's output**, once there's a pipeline again — see below.

Options **buyer only**, never a seller. Max 2% of capital on a Grade A trade.

## Current state: gutted, mid-rebuild

Commit `d252907` ("Clean up for building v0.1", 2026-09-29) deleted the entire
previous pipeline — every fetcher, the rule engine, the expiry gate, the
currency module, monitor/journal/scheduler, the PetroCore client, all tests,
all docs, and all four GitHub Actions workflows. None of that exists in git
history as "current" anymore; it's only recoverable via `git show
<earlier-sha>:<path>` if it's ever needed as reference. `data/` holds only a
`.gitkeep`.

**What actually exists in `app/` right now is one thing**: `app/scraper/`, a
browser-automation scraper pair for TradingEconomics and investing.com,
ported in from a sibling project (`WB-OS`) as the seed for rebuilding the
consensus/actuals fetchers. It has no caller yet — nothing in this repo reads
its output, writes a `data/*.json` file, or talks to Telegram. Treat any
mention of `common.py`, `signal_engine.py`, `consensus_fetcher.py`, the
`env()` helper, etc. in old commit messages or in
memory/history as **not present** until they're rebuilt.

## Commands

```bash
pip install -r requirements.txt
python -m playwright install chromium   # one-off, needed for both scrapers

# Both must run with -m — they use relative imports (`from ..browser import ...`),
# which only resolve under package context. Running them as a bare file path
# (`python app/scraper/sites/x.py`) fails with ImportError: attempted relative
# import with no known parent package. Either form works:
python -m app.scraper.sites.tradingeconomics    # from the repo root
python -m app.scraper.sites.investing
# or: cd app && python -m scraper.sites.investing
```

The `__init__.py` files in `app/scraper/` and `app/scraper/sites/` are load-bearing
for the `-m scraper...` form; if the directory gets recreated and they vanish,
that command breaks (`app/` itself needs none — it's an implicit namespace package).

There is no `tests/` directory and no test suite currently. `pytest.ini` is
still present and still excludes `network`-marked tests by default
(`-m "not network"`), but nothing in the repo defines any tests to run.

## Architecture

### `app/scraper/browser.py`

Shared Playwright helpers, both sites build on this:

- `browser_session()` — a context manager yielding one Playwright page;
  reuse it across multiple `.goto()` calls instead of opening a fresh browser
  per page fetch.
- `goto(page, url, wait_selector=None, ...)` — navigates on
  `domcontentloaded` (not `networkidle` — ad/analytics-heavy pages often
  never go idle) and waits for `wait_selector` as the real readiness signal.
  On an HTTP 4xx/5xx it reloads up to `RELOAD_RETRIES` (2) times, 5s apart,
  then raises `RuntimeError("... returned HTTP <status> (blocked or
  rate-limited)")`. The fail-fast matters: without it a 403 shows up as a
  30s `wait_for_selector` timeout that looks like a selector bug.
- `render_page()` / `render_html()` — one-off single-page variants built on
  `browser_session()`, for a caller that only needs one fetch.

**`browser_session()` launches with `headless=False`.** That's a real,
visible Chromium window with `slow_mo=150` and
`--disable-blink-features=AutomationControlled` — deliberate anti-bot-detection
choices carried over from `WB-OS`, not an oversight. It means this cannot run
on a headless CI runner (GitHub Actions, a server without a display) without
something like `xvfb` in front of it. If the old GitHub Actions workflows get
rebuilt around this scraper, that's the first thing to solve.

### `app/scraper/sources.py` — where the URLs live

One registry, no URLs anywhere else. `INDICATORS[indicator][site] = slug`
(slugs relative to the site's `base_url`), `SITES[site] = scraper class`,
plus `slug_for(indicator, site)` and `scraper_for(site)`. A missing entry means
that site doesn't carry / we haven't verified that indicator (e.g. no Cushing
on investing.com, no refinery utilisation on TE) — callers skip it.

- **Adding a site:** subclass `CalendarScraper` (see below), add it to `SITES`,
  add its slug to each indicator it carries. No caller changes.
- **Fixing a moved URL:** edit `INDICATORS` only.
- **Health check:** `python -m pytest tests/test_sources.py -m network -k tradingeconomics`
  walks every registered slug and fails if a page stops returning data. Run
  investing.com entries one at a time (`-k "eia_gasoline and investing"`) — it
  403s on back-to-back sessions. The offline tests in the same file run by default.
- **investing.com events can be dead**: API Cushing/gasoline/distillate (1656/657/1035)
  return rows from 2016/2022 with no error, so they are deliberately absent from the
  registry. The network health test asserts the newest released row is <= 21 days old.
  Always check `release_date` freshness on a new slug. TE's `api_gasoline` and
  `api_distillate` both read -2.16 for the latest week (suspiciously equal) and could
  not be cross-checked against investing.com — treat as unverified.
- investing.com slugs are `<event-name>-<event-id>` (e.g. `...-75`); TE slugs are
  `united-states/<indicator>`.
- Some TE pages (API Cushing/gasoline/distillate) have **no calendar table**, only
  the stats table: `fetch_page` returns `calendar_rows: None` with `stats` filled,
  so `fetch_release` returns `None` for them (no dated rows). Use `fetch_page(...)["stats"]`
  for the latest values. `eia_refinery` values are percentages, not million barrels
  (the `actual_mb`-style names don't apply); TE has no refinery page.

### `app/scraper/utils/base.py` — `CalendarScraper` (the response contract)

Both site scrapers subclass this and only supply `base_url`, `wait_selector`,
`parse_rows(soup)` and `parse_stats(soup, rows)`. **The response shape lives
here, so the two sites cannot drift** — do not re-add `fetch_page` / context
manager / `fetch_release` boilerplate to a site file, and do not change the
shape in one site only:

```
fetch_page(slug) -> {"calendar_rows": [row, ...] | None,
                     "stats": {"actual_mb", "forecast_mb", "previous_mb"}}   # or None on failure
row = {"release_date": "DD-MM-YYYY", "time": "08:00 PM" (IST),
       "actual"/"forecast"/"previous": float million barrels | None}
```

`calendar_rows` is `None` (never `[]`) when there are no rows; `stats` always has
all three keys. Both sites take `stats` from the latest released calendar row
(TE's separate stats table rounds — 2.97 vs 2.969 — so it is only a fallback for
pages with no calendar). A page that never shows its table returns `None` with a
one-line warning after `timeout_ms` (default 30s); TE returns HTTP 200 for a bad
slug, so a missing table can't be told apart from a slow render.
`page_gap_s` spaces out page loads per instance (investing.com defaults to 10s;
TE 0).

### `app/scraper/utils/calendar.py` — the shared row contract

Both scrapers are meant to return **identical rows** except `forecast`
(the two sites poll different analyst panels; e.g. crude consensus -0.6 on TE
vs -0.7 on investing.com for the 23-09-2026 release). This module is what keeps
them identical — put any new parsing here, not in a site file:

- Row shape: `{release_date: 'DD-MM-YYYY' str, time: IST 'HH:MM AM/PM' str,
  actual / forecast / previous: float million barrels or None}`.
- `gmt_to_ist()`, `to_mb_suffixed()` (`'-1.6M'`/`'250K'` → float).
- `row_for_release(rows, release_date=None)` — the row whose `release_date`
  matches exactly, else `None`; no date = latest released row.
- `DATE_FORMAT = "%d-%m-%Y"` is the default for every date the scrapers take or
  return (`parse_date` / `format_date`). ISO `YYYY-MM-DD` input is also accepted
  but output is always DD-MM-YYYY. Compare dates as `date` objects, never as
  strings — DD-MM-YYYY does not sort chronologically.

Lookup is by **release date only**, deliberately: investing.com exposes no
"week ending" field, so mapping dates to reporting weeks would make the two
scrapers disagree. Callers pass the real release date (usually a Wednesday for
EIA, Tuesday for API, shifted by holidays — e.g. Thu 10-09-2026). A week-ending
Friday returns `None`.

Both scraper classes expose `fetch_release(slug, release_date=None)` =
`fetch_page` + `row_for_release`. An upcoming release still matches (its
`actual` is `None`), so check `actual` before treating it as released.
Both `python -m` runners take `--slug` and `--date` (`utils/cli.py`).

Known unavoidable differences between the two sites: `forecast` (different
panels, incl. `stats.forecast_mb`) and the number of history rows (TE shows ~3,
investing.com ~10).

### `app/scraper/sites/tradingeconomics.py` — `TradingEconomicsScraper`

`fetch_page(slug)` (e.g. `"united-states/crude-oil-stocks-change"`) →
`{"calendar_rows": [...] | None, "stats": {"actual_mb": ..., "previous_mb": ...}}`
or `None` on any fetch/render failure. Its calendar shows only a few recent
rows (investing.com shows ~10). Two independent tables on a TE
indicator page, parsed separately:

- **Calendar table** (`tr.an-estimate-row`) — history + one upcoming row.
  Only some indicators have it (API crude does; API Cushing/gasoline/
  distillate don't) — absence returns `calendar_rows: None`, not an error, so
  callers must not treat `None` the same as "not fetched yet."
- **Stats table** (`<table class="table">` with `Actual`/`Previous`/`Unit`
  headers) — every indicator page has this; it's how the "Unit" check
  disambiguates it from the calendar table, which also carries
  `class="table"` but no `Unit` column.

Use as a context manager to reuse one browser across several slugs
(`with TradingEconomicsScraper() as s: ...`); used standalone, each
`fetch_page()` call opens and closes its own browser.

### `app/scraper/sites/investing.py` — `InvestingCalendarScraper`

Same `fetch_page(slug)` → `{"calendar_rows": ..., "stats": ...}` contract and
same row shape, against investing.com's economic-calendar occurrence-table
pages (`in.investing.com/economic-calendar/<slug>`; crude is
`eia-crude-oil-inventories-75`). `calendar_rows` comes from
`table[data-test="occurrence-table"]`; `stats` comes from a "Latest Release"
widget matched by label text (`_label_value`) because that widget carries no
stable id or `data-test` attribute — only Tailwind utility classes, which can
change on any redeploy. `stats` also has `forecast_mb`, which TE's does not.
Its times are GMT on the page; `gmt_to_ist()` converts them.

**investing.com blocks aggressively; behaviour observed live:**

- 2026-09-28: repeated requests inside a short window → HTTP 429. Also hit
  by an older approach that POSTed to the calendar-service endpoint (the
  `WB-OS` project's `setup2_wednesday_barrel.md` flags investing.com events as
  403/Cloudflare-blocked too), so the request shape isn't the cause.
- 2026-09-29: a fresh session got 403 on the first load, 403 on the first
  reload, then 200 with the table on the second reload. That is why `goto()`
  reloads twice. It fixes the first-load 403, **not** a sustained 429 — if a
  reload loop keeps failing, the IP is rate-limited; back off rather than
  adding stealth/proxy tricks.
- **A second page load in the same browser session was 403 on every attempt**,
  even after `page_gap_s=10` and two reloads (the old pipeline's notes say
  Cloudflare challenges every page after the first). So: one `fetch_page` per
  investing.com session, derive every row you need from it with
  `row_for_release`, and don't loop slugs. TE has no such limit.

Treat investing.com as a flaky fallback and tradingeconomics.com as the
reliable source, the same conclusion the deleted pipeline reached.

## Rebuilding the pipeline

If/when the fetchers, signal engine, and delivery layer come back, the design
decisions the old pipeline made (documented previously, now only in git
history) are worth reading before re-deriving them from scratch — in
particular: the days-to-expiry gate, the 5-step rule engine and its Sep 4
2026 reference week, the currency/INR strike math, and the
consensus-source fallback order. Pull the old `CLAUDE.md` with
`git show 095a5cc:CLAUDE.md` (or any SHA before `d252907`) rather than
reconstructing it by trial and error.
