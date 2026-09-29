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

**What exists now** (rebuilt after that cleanup): `app/scraper/` (the TE and
investing.com scrapers), the pipeline scripts `app/consensus_fetcher.py`,
`app/api_monitor.py`, `app/eia_actuals.py`, `app/signal_engine.py` and `app/telegram_bot.py`, the helpers in
`app/utils/` (`racer.py`, `common.py`, `telegram.py`), and `tests/`. Still **not
present** from the old pipeline: the pre-brief message, expiry/currency
strike logic, monitor/journal/scheduler, PetroCore, CI workflows.

**Naming rule:** TE's "Consensus" and investing.com's "Forecast" are the same
figure and are called **`consensus`** everywhere in this app (`consensus`,
`consensus_mb`, `*_consensus_mb`). Never introduce `forecast`; the only place the
word appears is investing.com's own on-page label, matched in `parse_stats`.

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

python -m app.consensus_fetcher                    # next unreleased EIA report -> data/consensus.json
python -m app.consensus_fetcher --date 23-09-2026  # a specific release (replay / testing)
python -m app.consensus_fetcher --sites tradingeconomics

python -m app.api_monitor                          # latest due API report, polls until it lands
python -m app.api_monitor --once                   # single attempt
python -m app.api_monitor --date 22-09-2026 --once # replay a specific release

python -m app.eia_actuals                           # today's EIA report, polls until it lands
python -m app.eia_actuals --once                    # single attempt
python -m app.eia_actuals --date 23-09-2026 --once  # replay a specific release

python -m app.signal_engine                        # data/*.json -> data/signal.json
python -m app.signal_engine --allow-stale          # replay input files older than 2 days

python -m app.telegram_bot                         # send data/signal.json to Telegram
python -m app.telegram_bot --allow-stale           # replay an old signal (stamped REPLAY)

python -m pytest tests -q                          # offline suite (default)
python -m pytest tests -m network -k tradingeconomics   # live URL health check
```

The `__init__.py` files in `app/scraper/` and `app/scraper/sites/` are load-bearing
for the `-m scraper...` form; if the directory gets recreated and they vanish,
that command breaks (`app/` itself needs none — it's an implicit namespace package).

`pytest.ini` sets `pythonpath = .` (so tests can `import app...`) and excludes
`network`-marked tests by default (`-m "not network"`). `tests/test_consensus_fetcher.py`
runs the whole race against fake scrapers, no browser or network.

## Architecture

### `app/consensus_fetcher.py` — the consensus race

Fetches `crude_consensus_mb`, `gasoline_consensus_mb`, `distillate_consensus_mb`
and `crude_previous_mb` for the next unreleased EIA report (or `--date`) and
writes `data/consensus.json`:

```json
{"release_date": "30-09-2026",
 "crude_consensus_mb": -1.6, "gasoline_consensus_mb": -1.4, "distillate_consensus_mb": -0.7,
 "crude_previous_mb": 2.415,
 "crude_source": "tradingeconomics", "gasoline_source": "investing.com", "distillate_source": "investing.com",
 "fetched_at": "29-09-2026 13:32"}
```

How it works — keep these properties when changing it:

- **Both sites run at the same time**, one daemon thread and one browser each,
  and **each indicator is won independently**: the first *valid* value per
  indicator wins, so the three `*_source` fields can differ. `crude_previous_mb`
  comes with the crude winner. Sources are named `tradingeconomics` / `investing.com`.
- **Valid** = row found, `consensus` not `None` (and `previous` for crude). A site
  that can't deliver an indicator (blocked, page changed, consensus not posted)
  just loses that race; the other site still gets its turn.
- **One release date for everything.** The crude winner (or `--date`) anchors it;
  a gasoline/distillate candidate for a different date is skipped (`decide()`).
- **Fails loudly** (exit 1, nothing written, per-site reasons logged) if any
  indicator has no valid value. Never write a partial or guessed consensus: it is
  the baseline of every deviation. Real example: run the day before the report,
  both sites answered "consensus for 30-09-2026 not posted yet" -> exit 1 (the
  consensus only appears close to the release; the old pipeline ran Tue 19:00 IST).
- Without `--date` it picks each page's next unreleased row (`pending_row`).
- investing.com gets one browser session per indicator, `session_gap_s` (60s)
  apart, so it takes ~2.5 min when nothing wins early; TE takes ~5s. The loser is
  abandoned mid-load and `__main__` ends with `os._exit` so a stuck Playwright
  thread can't hang shutdown (checked: no orphaned browsers).
- `fetched_at` is IST, `DD-MM-YYYY HH:MM`. `data/consensus.json` is tracked in git
  (pipeline data is committed by design).
- Mixed-source outcomes are covered by offline tests only; live runs so far were
  all-`tradingeconomics`.

### `app/utils/racer.py` — the shared race

`race(sites, fields, job, release_date, timeout_s, what)` runs `job(site, stop, ok,
fail)` on one daemon thread per site; each job reports fields as they land. The
first valid candidate per field wins (`decide()`), fields are won independently, and
every winner must share one release date, anchored by the first field in `fields`
(or `--date`). Raises `RuntimeError` naming each undecided field with per-site
reasons; "no site supplied it" vs "no answer within Ns" distinguishes a field nobody
carries from a timeout. Both pipeline scripts use it — don't re-implement the race.

### `app/api_monitor.py` — the API report

Writes `data/api_report.json`: `release_date`, `api_crude_mb`, `api_cushing_mb`,
`api_gasoline_mb`, `api_distillate_mb`, `crude_source`, `cushing_source`,
`gasoline_source`, `distillate_source`, `fetched_at` (IST, `DD-MM-YYYY HH:MM`). All
actuals; no consensus exists for this report. Published Tue ~16:30 ET (Wed ~02:00
IST); without `--once` it polls every 5 min for up to 4 h. Nothing is written unless
all four fields are valid.

- **Crude** is a dated row on both sites, raced like the consensus.
- **Cushing / gasoline / distillate exist only on TE**, as an **undated, 2-decimal
  "Related" snapshot** (`parse_related_table`); investing.com's events for them are
  dead (see `sources.py`). Reading that blind could silently return last week's
  numbers, so it is taken from the **same page load** as the dated crude row
  (`fetch_with_soup`) and accepted only if its crude value matches that row's actual
  (`legs_from_snapshot`, tolerance 0.0051). Consequently: those three legs are
  2-decimal (crude keeps 3), and an explicit `--date` for anything but the latest
  release is rejected for the legs ("different release").
- Default target is the **latest release due** by today's UTC date (`latest_due_row`),
  not `pending_row`: after the report prints, the next-unreleased row jumps to next
  week, which would be wrong here. If that row hasn't printed it fails "not released
  yet" (and the poll retries).
- TE's gasoline and distillate both read **-2.16** for the 22-09 release. Checked against
  TE's own news text: gasoline fell 2.16 mb, distillate 2.164 mb (the snapshot rounds to
  2 dp), so the equal values are a coincidence, not a duplicated column.
- TE and investing.com show the same API release a few minutes apart in `time`
  (02:30 vs 02:00 AM IST); the dates and values agree.

### `app/eia_actuals.py` — the EIA actuals

Writes `data/eia_actuals.json`: `release_date`, `released_at`, `crude_change_mb`,
`cushing_change_mb`, `gasoline_change_mb`, `distillate_change_mb`,
`refinery_util_change_pct`, `source`, `won_race`, `fetched_at` (times IST, `DD-MM-YYYY HH:MM`).
Stock **changes** in million barrels, negative = draw (`cushing_change_mb` is a
change, not the stock level — the old `cushing_stocks_mb` name was misleading).
Released Wed 10:30 ET (20:00 IST); polls every 60 s for up to 90 min unless `--once`.

- **Whole-report race, not per-field:** each site walks crude -> cushing -> gasoline
  -> distillate and only yields a candidate when all four are released and on the
  same release date; the first complete site wins, so `source` is a single site and
  the report never mixes sites. `won_race` is `True` when more than one site raced
  (`False` under `--sites <one>`) — that meaning is my reading of the spec, confirm it
  if something depends on it. `released_at` is when the numbers were first seen, not
  the calendar's 20:00.
- **There is no `refinery_util_pct`; only `refinery_util_change_pct`.** The refinery
  utilisation *level* (~93%) was removed from the output at the user's request: no
  scraped source has it (TE only has *Refinery Crude Runs* in thousand barrels), and
  investing.com's event 1961 ("EIA Weekly Refinery Utilization Rates") lists only the
  week-over-week *change* (`-2.8%`, `-1.0%`, `0.6%`... — cross-checked: TE crude runs
  -519 kb/d ≈ -2.8 pts). That change is `refinery_util_change_pct`; never present it as
  a level. Only the EIA API publishes the level (needs a free `EIA_API_KEY`; none
  configured) — add it as a third source if the level is ever wanted. Nothing
  downstream reads either.
- **The refinery change is an *optional* race field.** Only investing.com has it, so
  it can't gate the report (TE could never win). investing.com's job fetches the
  refinery page **first** and offers a candidate per printed row; `decide()` picks the
  row matching the winning report's release date. Once the report is decided the race
  waits `REFINERY_GRACE_S` (30 s) for it, else it is `null` — a refinery failure never
  sinks the report. Live: 13.7 s end to end with the change captured. The cost is that
  investing.com's report candidate starts one paced session later (its fallback role).
- `racer.race(..., optional=(...), grace_s=...)`: optional fields never cause a failure;
  once every required field is decided the race waits up to `grace_s` more for them.
- **Holiday guard (`current_release`):** the auto target is the latest release due by
  today's UTC date, and it is refused if more than 1 day old. Without this, in a
  Thursday-delayed week, Wednesday's run would return LAST week's already-printed
  report as fresh. `api_monitor` uses the same guard. An explicit `--date` bypasses it.
- **The data contract has one home: `common.py`** (`CONSENSUS_FILE`, `API_REPORT_FILE`,
  `EIA_ACTUALS_FILE`, `SIGNAL_FILE`). Every producer and the engine import these; **never
  spell `consensus.json` etc. in code** (docstrings may mention them). A rename once
  (`eia_actual` -> `eia_actuals`) would otherwise leave the engine reading a dead name,
  failing "file not found" at 20:02 on a Wednesday.
  `tests/test_pipeline_wiring.py` fails on any code string literal naming a data file
  outside `common.py`, and asserts producer and consumer share the same `Path` object.
- **Every pipeline script alerts Telegram on failure and stays silent on success**:
  `consensus_fetcher`, `api_monitor` and `eia_actuals` call
  `telegram.send_exception("<script>.py", exc)` in their top-level guard (message =
  `Type: text`, cut to 1500 chars; race errors carry the per-site reasons), then exit 1
  with nothing written. `signal_engine` sends its own `InputError` alerts. This includes a
  polling script exhausting its window (eia_actuals: 90 min) — the case that used to fail
  unnoticed. `send_*` never raises and logs the text if Telegram is unset. Only failures
  alert; success alerts are not built.
- `common.env/fmt/fmt_ts/is_stale` (placeholder-safe env reads: blank or `#`-leading = unset;
  DD-MM-YYYY dates), `setup_logging` silences httpx (the Telegram token is in its URL).
- `common.poll(attempt, once, interval_s, timeout_s, log)` retries on `RuntimeError`
  ("not there yet") and lets every other exception propagate; both polling scripts use it.

### `app/signal_engine.py` — the signal

Reads `consensus.json` + `api_report.json` + `eia_actuals.json`, applies the 5-step
rules, writes `data/signal.json` (`inputs` / `calculations` / `signal` /
`expected_move` / `analysis` / `model_used`). No scraping. Telegram is used for
**errors only** (`utils/telegram.py`, never raises; success alerts are not built).

- **Pure rule functions** (`calculate_deviations`, `apply_grade_logic`,
  `apply_cushing_adjustment`, `check_products`, `check_api_alignment`,
  `calculate_confidence`, `get_trade_recommendation`, `get_expected_move`) take plain
  values — no I/O, clock or randomness. Boundaries are inclusive: exactly +/-1.0 mb is a
  skip, exactly +/-1.5 is Grade A. Cushing downgrades A->B, B is the floor, and
  confidence (base A 75 / B 55, +/-5 Cushing, +/-5 API, clamp 40-85) uses the grade
  **after** the downgrade. A skip has confidence 0, no trade, and `null` (not
  false/0) for `cushing_contradicts` / `products_oppose` / `api_aligns`.
- **The AI boundary:** Groq writes only `analysis`. It never touches grade, direction,
  confidence or the trade; any failure ships the signal with `analysis: ""`,
  `model_used: "rule_based"` (a test proves a lying model changes nothing).
- **`check_products` uses the same-sign rule** (bearish: BOTH product deviations
  > +2.0; bullish: BOTH < -2.0). The original prompt's formula text was the mirror
  image and contradicted its own sample output, the reference week
  (`products_strongly_oppose: true` for +2.669/+2.787 bearish) and the old engine;
  the user chose the same-sign rule. A flag only, never changes the grade.
- **Release-date validation:** consensus and EIA actuals must have the **same**
  `release_date`; the API report's date is the **Tuesday before** (0-3 days earlier),
  so "all three equal" would always fail on real data. Files older than 2 days are
  refused (API: 5) unless `--allow-stale`. `release_date` missing/invalid, bad JSON, a
  missing file, or a missing mandatory field (`crude_consensus_mb`, `crude_change_mb`,
  `api_crude_mb`) -> Telegram error + exit 1, nothing written. Optional fields become
  `null` and their checks `None`.
- **`inputs.refinery_util_change_pct`, not `refinery_util_pct`:** the level no longer
  exists in `eia_actuals.json` (see eia_actuals), so the change replaces it in `inputs`
  and in the Groq prompt.
- **USD/INR** comes from `yfinance` (`INR=X`, 10 s timeout on a daemon thread) or the
  fallback 84.0; a quote outside 50-150 is treated as bad data. `usd_inr_source` records
  which. Live 29-09-2026: 95.96, so MCX moves scale accordingly.
- **Groq caveats (found live):** the spec's default model `llama-3.3-70b-versatile` is
  **404 for this account**; available chat models are `qwen/qwen3.8-27b` (works),
  `openai/gpt-oss-20b/120b` (reasoning models: they spend `max_tokens=150` on hidden
  reasoning and return empty content, so the narrative silently falls back) and
  `allam-2-7b`. Set `GROQ_MODEL` in `.env` (it is blank today). Groq itself finishes its
  sentences (`finish_reason='stop'`, ~60 tokens, 230-300 chars). The spec's 200-char cap
  was what cut them mid-word, so `ANALYSIS_MAX_CHARS` is 450 and an over-long reply (or
  `finish_reason='length'`) is trimmed back to the last complete sentence
  (`complete_sentences`; a "." inside "+3.569 mb" is not a sentence end). The model also
  invents figures absent from the inputs (e.g. a "$0.50 drop"). The narrative is
  display-only, but do not trust its numbers.
- Input `load_inputs(data_dir=None)` resolves `DATA_DIR` at call time; an import-time
  default silently ignores test patching and reads the real `data/`.

### `app/telegram_bot.py` — the success alert

Sends `data/signal.json` to Telegram (`format_signal` is pure and tested): grade, direction,
crude deviation with actual vs consensus, Cushing / API status, the "products strongly
oppose" flag when true, the trade (option, strike, size, confidence), the expected WTI move
with its MCX/INR equivalent, and the narrative. A skip week sends "NO TRADE" and none of the
trade detail; missing optional data reads `N/A`, not a crash. Runs after `signal_engine`.

- **It refuses a stale signal.** If the engine failed, `signal.json` still holds LAST week's
  trade; sending it as live could get someone to trade a dead setup. A signal older than 2
  days -> error alert + exit 1. `--allow-stale` sends it anyway, first line
  `REPLAY - data is N days old, NOT a live signal`. Missing/corrupt file -> error alert too.
- Verified live: Telegram accepted a test alert and a replay of the 23-09 signal (607 chars).
- The narrative arrives as complete sentences (engine cap 450 chars, trimmed at a sentence
  end); an earlier 200-char cap showed it cut mid-word in the message.
- Windows consoles are cp1252: never `print()` this text (emoji raise `UnicodeEncodeError`);
  the script only logs, and `setup_logging` forces UTF-8.

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
  `api_distillate` both read -2.16 for the 22-09 release; that is genuine (-2.16 and
  -2.164 per TE's news text), just rounded to 2 dp.
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
                     "stats": {"actual_mb", "consensus_mb", "previous_mb"}}   # or None on failure
row = {"release_date": "DD-MM-YYYY", "time": "08:00 PM" (IST),
       "actual"/"consensus"/"previous": float million barrels | None}
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

Both scrapers are meant to return **identical rows** except `consensus`
(the two sites poll different analyst panels; e.g. crude consensus -0.6 on TE
vs -0.7 on investing.com for the 23-09-2026 release). This module is what keeps
them identical — put any new parsing here, not in a site file:

- Row shape: `{release_date: 'DD-MM-YYYY' str, time: IST 'HH:MM AM/PM' str,
  actual / consensus / previous: float million barrels or None}`.
- `gmt_to_ist()`, `to_mb_suffixed()` (`'-1.6M'`/`'250K'` → float).
- `row_for_release(rows, release_date=None)` — the row whose `release_date`
  matches exactly, else `None`; no date = latest released row.
- `current_release(rows, today, max_age_days=1)` — latest due release, refused if stale
  (holiday guard); `latest_due_row(rows, today)` — the same without the age check;
  `pending_row(rows)`
  — earliest unreleased. `CalendarScraper.fetch_with_soup(slug)` returns `(page, soup)`
  from one load for callers needing something outside the shared contract (TE's
  `parse_related_table`).
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

Known unavoidable differences between the two sites: `consensus` (different
panels, incl. `stats.consensus_mb`) and the number of history rows (TE shows ~3,
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
change on any redeploy. `stats` also has `consensus_mb`, which TE's does not.
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
