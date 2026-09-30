# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TWPR (The Weekly Petroleum Report) — a systematic weekly options setup on MCX
CrudeOil, the first setup in the TradeDesk platform. **Real money is meant to
trade on this repo's output**, once there's a pipeline again — see below.

Options **buyer only**, never a seller. 1% of capital at risk per event (runbook).

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
`app/api_monitor.py`, `app/eia_actuals.py`, `app/market_data.py`, `app/signal_engine.py` and `app/telegram_bot.py`,
the release-day tools `app/run.py` (phase runner), `app/pre_brief.py`, `app/watch.py` (time reminders) and
`app/journal.py` (your fills and the post-print price path), the pure modules `app/model.py`, `app/options.py`
and `app/currency.py`, the helpers in `app/utils/` (`racer.py`, `common.py`, `telegram.py`, `eia_levels.py`), and
`tests/`. **Execution is deliberately not automated:** the system alerts and reminds, you place and manage every
order by hand. Still **not present**: an option-chain feed and any premium/price-based stop watching (both need a
broker connection), PetroCore, CI workflows, and unattended scheduling (the commands are ready to schedule).

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

python -m app.market_data                          # yfinance inputs -> data/market.json
python -m app.market_data --date 23-09-2026          # replay: the inputs as of 10:29 ET that day
python -m app.surprise_history --backfill          # one-off: past weekly surprises -> data/surprise_history.json

python -m app.signal_engine                        # data/*.json -> data/signal.json (needs market.json + surprise_history.json)
python -m app.signal_engine --allow-stale          # replay input files older than 2 days

python -m app.telegram_bot                         # send data/signal.json to Telegram
python -m app.telegram_bot --allow-stale           # replay an old signal (stamped REPLAY)

python -m app.run pre                              # release-day afternoon: consensus, API, market, pre-brief
python -m app.run print --watch                    # a few minutes before the print: actuals, signal, alert, reminders
python -m app.run all --replay 23-09-2026          # a past release end to end (replay-stamped)
python -m app.pre_brief --print                    # the pre-print brief, shown instead of sent
python -m app.watch --dry-run                      # the reminder times for today's signal
python -m app.journal fill ...                     # log a fill by hand;  path / show: post-print prices and statistics

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

Writes `data/api_report.json`: `release_date`, `api_crude_mb`, `crude_source`, `fetched_at` (IST,
`DD-MM-YYYY HH:MM`). **Crude only.** The actual crude change; no consensus exists for this report.
Published Tue ~16:30 ET (Wed ~02:00 IST); without `--once` it polls every 5 min for up to 4 h.
Nothing is written unless crude is valid.

- **Why crude only:** the API's Cushing, gasoline and distillate figures are behind a paywall.
  The tradingeconomics copies (an undated, 2-decimal "Related" snapshot) lag - live 2026-09-30
  Cushing still showed last week's +2.08 while the other rows were new, and matching crude
  proved nothing about them - and investing.com's events for them are dead (rows from 2016/2022).
  Nothing downstream used them (Cushing comes from the EIA report itself), so they were removed
  along with `parse_related_table` and their `sources.py` slugs. Do not re-add without a source
  that is both paid-for and checked for freshness.
- **Crude** is a dated row on both sites, raced like the consensus (`race`, one field).
  Being a dated row it can be replayed with `--date` for any release still on the page.
- Default target is the **latest release due** by today's UTC date (`latest_due_row`),
  not `pending_row`: after the report prints, the next-unreleased row jumps to next
  week, which would be wrong here. If that row hasn't printed it fails "not released
  yet" (and the poll retries).
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
- **Every script's `main` calls `load_dotenv`.** Found live: the fetchers never loaded `.env`, so
  a failure printed "alert not sent" and Telegram never heard about it. A wiring test now asserts it.
- `common.env/fmt/fmt_ts/is_stale` (placeholder-safe env reads: blank or `#`-leading = unset;
  DD-MM-YYYY dates), `setup_logging` silences httpx (the Telegram token is in its URL).
- `common.poll(attempt, once, interval_s, timeout_s, log)` retries on `RuntimeError`
  ("not there yet") and lets every other exception propagate; both polling scripts use it.

### `app/market_data.py` — market inputs for the runbook model

Spec of record is the runbook model (`docs/WPSR_WEDNESDAY_RUNBOOK.md` + the MCX options
adaptation): TLS / Z-score, ITM delta (0.80-0.85 when OVX > 35), 1% risk. The old
grade / ATM / 2% engine is gone.

Writes `data/market.json` (`MARKET_FILE`) from yfinance: `atr_20` (simple mean of the last 20
true ranges of daily WTI bars; today's still-forming bar is excluded), `ovx`, `cl1_cl2`,
`crack_321` (products converted gallons -> barrels, x42), `brent_wti`, `dxy`, plus `wti`,
`as_of`, `fetched_at`. `atr_20` and `ovx` are required (fail loudly: Telegram alert, exit 1,
nothing written); the scorecard values become `null` if unreadable. CL2 has no continuous
ticker: `contract_symbols` builds `CL<month><yy>.NYM` and `front_second` picks the contract
trading at CL=F's price (within 0.25) and the one after it; no match -> `cl1_cl2: null`, never
a guess. Expired contracts are probed quietly (yfinance's own error logging is silenced in
`setup_logging`). `--date DD-MM-YYYY` replays a release: only days before it, the overnight rally up to
10:29 ET, `fetched_at` set to that moment. Yahoo keeps 5-minute bars only ~60 days, and near a contract
expiry the expired contract has no history, so a replay may give `cl1_cl2: null` (and a continuous-series
roll can distort the rally): treat replays as approximate.

### `app/model.py`, `app/surprise_history.py`, `utils/eia_levels.py` — runbook inputs

- `model.py` is pure: `tls` (weights w_g 0.67, 0.80 May-Sep; w_d 0.50, 0.70 Nov-Feb, by the
  release month), `sigma_forecast(history, method="mad")` (spread of the last 12 weekly TLS;
  **raises under 8 weeks**, never guesses) and `z_score`.
- **Sigma method (a deliberate deviation from the runbook's literal "std dev"):** the default
  is `mad` = 1.4826 x median absolute deviation, because the 12-08-2026 week (TLS +20 mb)
  alone lifted the plain std dev to ~7.8-8.3 mb and would have locked out real signals for 12
  weeks. On the 9 weeks we have, MAD (~4.6) matches the std dev with that week removed (~4.6);
  at a 1.25 gate that is |TLS| >= ~5.7 instead of ~9.7. `SIGMA_METHOD=std` in `.env` restores
  the plain std dev; a zero MAD falls back to std; `calculations.sigma_method` records which
  set the gate. The gate is still strict in this volatile window: the 23-09 week (TLS +2.23)
  stands down either way.
- `surprise_history.py` keeps `data/surprise_history.json` (`SURPRISE_HISTORY_FILE`): per release,
  crude/gasoline/distillate surprise = actual - consensus. `--backfill` reads investing.com (~10
  past releases, three paced sessions; TE only shows ~3) and never overwrites existing weeks;
  `record_week` appends the live week. Backfilled surprises use investing.com's consensus panel,
  live ones TE's. `data/*` is git-ignored, so this file is local state.
- `eia_actuals.json` now also has `cushing_level_mb`, read from EIA's public weekly table
  (no API key; `utils/eia_levels.py`, in thousand barrels /1000). It is accepted only if
  level[-1] - level[-2] equals the scraped `cushing_change_mb` (so last week's level is never
  returned as this week's), retried 3x20 s, and is `null` if EIA hasn't updated - it never
  sinks the report.

### MCX contract facts (MCX's Crude Oil options specification from March 2026, plus the 2024 leaflet and brochure)

Sources: the 100 bbl options specification "March 2026 contract onwards" (Circular MCX/TRD/100/2026, 1 March 2026) is
the newest and wins wherever it differs from the 2024 leaflet. Source of truth for the option mechanics; `docs/WPSR_MCX_OPTIONS_RUNBOOK.md` has the trading implications.

- **Options exist on both `CRUDEOIL` (underlying: the 100 bbl futures) and `CRUDEOILM` (underlying: the 10 bbl
  mini futures)**, each with its own March 2026 options specification (Circular MCX/TRD/100/2026) and January 2026
  futures specification (Circular MCX/TRD/319/2025). **The mini contract is identical in every rule except the
  size and tick:** same 75/1/75 strikes, same Rs 50 interval, same "two business days before the futures" expiry,
  same session and price limits, same devolution at expiry, and **the same 2026 futures expiry calendar**, so one
  calendar serves both. European calls and puts, quoted in Rs per barrel;
  trading unit is one underlying futures contract (100 or 10 bbl). Tick Rs 0.10 (crude) / Rs 0.05 (mini); strike
  interval **Rs 50**. **Strikes: 75 ITM, 1 near-the-money and 75 OTM (151 calls and 151 puts) in the March 2026
  specification**, up from 25/1/25 (51) in the 2024 leaflet, so ITM strikes reach Rs 3,750 (about 43% of the
  futures price) and the 0.80-0.85 delta strike is listed in any month at any plausible volatility.
- **Listing:** each monthly contract launches three months before its expiry month (the October 2026 contract launched in
  July 2026), starting the business day after the near-month futures expire, so "roll to next month" always
  has a listed contract. The base price is the previous day's daily settlement price; the daily price limit for
  options is Black-76 based and may be relaxed by the exchange.
- **Expiry (last trading day) = two business days before the expiry day of the underlying futures.** The futures
  expiry is **not a fixed day**: MCX's contract launch calendar (Circular MCX/TRD/319/2025, futures spec "January
  2026 contract onwards") gives the 2026 dates: Jan 16, Feb 19, Mar 19, Apr 20, May 18, Jun 18, Jul 20, Aug 19,
  Sep 21, Oct 19, Nov 19, Dec 18. They are in `options.FUTURES_EXPIRY_CALENDAR`; a month missing from it (2027 on)
  falls back to the 19th and is flagged `expiry_source: "assumed_19th"` in the signal, the message and the
  checklist. Oct 2026: futures Mon 19th, options Thu 15th (matches the user's date). "Business days" means MCX
  trading days, so holidays matter: `options.MCX_HOLIDAYS_2026` holds MCX's 2026 trading-holiday list (16 days).
  Only days with **both** sessions closed (Republic Day, Good Friday, Gandhi Jayanti, Christmas) are treated as
  non-business days for the expiry count; a day with one session open counts as a business day (an assumption).
  None of the 2026 holidays moves an expiry (tested). The old fixed-19th rule was wrong for a third of 2026
  (Sep would have been the 18th, Jul the 17th).
- **The buyer's premium is blocked upfront in full**, in real time; there is no margin for a buyer (extreme loss
  margin applies to shorts only). A deep-ITM option is expensive: about Rs 1,300 per bbl at today's levels, i.e.
  roughly Rs 1.3 lakh per crude lot or Rs 13,000 per mini lot (Black-76, IV 54%, 22 days; an estimate).
- **Pricing model: Black-76** (the exchange uses it for the option price bands), so greeks come from the futures
  price, not spot.
- **Futures daily price limit 4%, relaxed to 6% and then 9%** (15-minute cooling-off at the second step); option
  bands are set from Black-76 and can freeze even when the futures are not limited.
- **Holidays are mostly morning-only.** Of MCX's 16 trading holidays in 2026 only four close the whole day;
  most (Holi, Ram Navmi, Dassera, Diwali, ...) close the 9:00-17:00 morning session but **the evening session, when
  the EIA report prints, still trades**. New Year Day is the reverse: morning open, evening closed. The signal
  warns loudly when the release day's evening session is closed (`schedule.mcx_evening_open`), because you
  cannot trade it. Muhurat trading (Sun 8 Nov 2026) is not modelled. MCX's page also lists a 29 Sep 2026
  circular "Revision in Trading Hours" that has not been read.
- **Session: 9:00 to 23:30 IST while US daylight saving time is in force, 23:55 after it ends** (Circular
  MCX/TRD/550/2026, 29 Sep 2026: 23:55 from Mon 2 Nov 2026 to Fri 12 Mar 2027; 23:30 the rest of the year;
  `options.session_close`). An earlier note here had this backwards. The print is 20:00 IST in summer (close 23:30)
  and 21:00 IST in winter (close 23:55), so the hard exit is "print + 2.5 h" capped at one hour before the close:
  22:30 in summer, 22:55 in winter (2.5 h would be 23:30, only 25 minutes before the close).
- **At expiry the open position devolves into the underlying futures** (March 2026 spec): a long call becomes a long
  futures position and a long put a short futures position, opened at the strike; ITM options are exercised
  automatically unless the long holder gives a contrary instruction. A devolved position needs futures margin
  and the exchange may levy extra pre-expiry margin in the last days. This is real, not a documentation quirk, and
  it is why the system rolls when 5 or fewer days remain and exits the option the same evening: never carry it to
  expiry. Premium settles T+1 and mark-to-market gains on options are not paid out in cash.
- Position limit for options: 9,60,000 bbl per individual client (separate from futures); max futures order
  10,000 bbl. Both far above one or two lots.
- Context: MCX crude's annualised volatility was 24-47% in 2016-2024 (94% in 2020), so OVX above 35 really is
  elevated.

### `app/signal_engine.py`, `app/options.py` — the signal

Reads `consensus.json`, `api_report.json`, `eia_actuals.json`, `market.json` and
`surprise_history.json`; writes `data/signal.json`: `inputs`, `calculations`, `signal`
(`action` trade/stand_down, `regime` 1/2/3/null, `direction`, `option_type`, `strike_type`
ITM), `expected_move`, `option`, `sizing`, `scorecard`, `schedule`, `checklist`, `analysis`,
`model_used`. No scraping. The maths is pure and lives in `model.py` (TLS, sigma, Z,
Cushing multiplier and contradiction, beta_vol, expected move, `classify`) and `options.py`
(delta, expiry gate, lots, DST-aware IST clock); the engine loads, validates and assembles.

- **Decision:** TLS = crude + w_g x gasoline + w_d x distillate surprise; `Z = TLS / sigma`,
  sigma = spread (MAD by default, see above) of the last 12 weekly TLS **excluding the week being traded** (needs >= 8
  weeks, else Telegram error + exit 1). |Z| < 1.25 -> stand down (inclusive at 1.25).
  Cushing contradicting the headline **by at least 1.0 mb** (`CUSHING_MATERIAL_MB`; a smaller
  opposite move is `immaterial`, so +0.1 mb cannot turn a -8 mb draw into a fade - an addition
  to the runbook after an external review) -> Regime 2, the fade (direction opposite the
  headline, checked first). Regime 3 = EIA draw beat consensus but fell short of an extreme
  (> 3.0 mb) API draw **and** WTI rallied more than $1.00 from the API print to the EIA
  print (`market.json: overnight_rally_usd`, from Yahoo 5-minute bars, Tuesday 16:30 ET ->
  Wednesday 10:30 ET) -> PUT. If the rally is unknown or too small, Regime 3 does **not**
  fire (Regime 1 instead) and the checklist says why. Otherwise Regime 1, with the headline
  (build -> PUT, draw -> CALL). The rally assumes a Tuesday -> Wednesday pair; a holiday-shifted
  release and a contract roll inside the window are not modelled.
- **Expected move** (`-TLS x beta_vol x Cushing multiplier`, x USD/INR for MCX) is given for
  Regime 1 only; Regimes 2 and 3 target chart levels. The 0.15-0.30 USD per mb anchor is
  shown **beside** it (`anchor_low/high_usd/inr`) and `sanity_ok` says whether beta_vol falls
  inside it (changed after a what-if preview: the message now LEADS with the anchor range and shows beta_vol as
  a marked footnote, "unproven", because at TLS +11.8 beta_vol said -9.65 USD = 270% of the band; the band line
  is measured on the top of the anchor); **at OVX ~54 it does not** (~0.9 vs the anchor's ~0.3-0.7 for TLS 2.2). Neither
  vetoes the trade: the move feeds the message, not sizing or entries. `expected_move.band` sets the
  beta_vol move against MCX's 4% futures price limit (Rs ~ WTI x USD/INR; the limit widens to 6% then 9%),
  as a share of the band, and flags `near_band` at 75% or more: a locked futures market can freeze the
  options. The band is measured from the previous settlement and the day may already have moved, so it
  is the move's size against a full band, not what is left. An external review
  called beta_vol "pseudo-math" with no theoretical basis. Unknown Cushing level -> multiplier 1.0, `cushing_level_known: false`.
- **Option:** ITM only. Delta 0.60-0.70, or 0.80-0.85 when OVX is strictly above 35.
  Expiry = the nearest **option** expiry, which is `OPTION_LEAD_BUSINESS_DAYS` (2) business days
  before the futures expiry taken from MCX's 2026 calendar (see "MCX contract facts"): October 2026 options
  expire Thu 15 Oct. Months outside the calendar (2027 on) are a flagged guess, and so are 2027 holidays
  (only the 2026 list is loaded).
  Rolled to next month when 5 or fewer days remain. No option-chain
  feed exists: the strike is left to you (pick the ITM strike whose delta is in range).
- **Sizing (changed 2026-09-30):** no equity math any more. **Two kinds of setting, easy to confuse:**
  `MCX_CRUDEOIL_LOT_SIZE=100` / `MCX_CRUDEOILM_LOT_SIZE=10` are MCX's *contract sizes* in barrels (validated: any
  other crude value is refused with a pointer to the lots setting; the natural gas sizes are reserved and only
  need to be whole numbers), while `MCX_CRUDEOIL_LOTS` / `MCX_CRUDEOILM_LOTS` are *how many lots you trade* (I once
  read the SIZE settings as lot counts and the preview showed 100 crude lots). You set the lot counts
  (whole numbers you trade per signal; both unset -> `sizing: null` and
  the message says how to set them) and `sizing.contracts` shows, per configured contract,
  `risk_inr_by_futures_stop_usd`: what those lots lose if the option stop is hit,
  `lots x futures stop x USD/INR x mid delta x barrels per lot` (CRUDEOIL 100 bbl, CRUDEOILM 10 bbl;
  `options.CONTRACT_BARRELS`), for stops of $0.18 / $0.25 / $0.35 (the real
  stop, 1.5 x 1-min ATR or beyond VWAP +/-1.5 sigma, comes from the chart). It no longer derives lots
  from a 1% risk budget: pick the lot count so the loss at your real stop is acceptable.
  `MCX_NATURALGAS_LOT_SIZE` and `MCX_NATURALGASM_LOT_SIZE` (mini) are loaded and validated but **reserved**:
  nothing reads them (crude only).
  MCX lists options on the mini contract (`CRUDEOILM`, underlying the 10 bbl mini futures; confirmed by its March
  2026 specification), so `MCX_CRUDEOILM_LOTS` is usable; the maths uses 10 bbl a lot.
- **Not automated (on `checklist`):** time-spread and dealer-gamma filters, the retest entry,
  the real stop, FX/RBI and geopolitical aborts, OI pinning haircut. The pre-release
  `scorecard` is informational: the runbook doesn't say how a miss changes the trade.
- **Schedule** comes from 10:30 New York time, so it moves with US daylight saving: print
  20:00 IST in summer, 21:00 in winter; time stop +35 min; hard exit +2.5 h.
- **The AI boundary:** Groq writes only `analysis`; a stand-down asks it for nothing (a
  model asked to explain noise invents a direction). It never touches the trade
  (a test proves a lying model changes nothing); any failure -> `analysis: ""`,
  `model_used: "rule_based"`.
- **Input rules unchanged:** consensus and EIA actuals must share `release_date`; the API
  report is the Tuesday before (0-3 days earlier); files older than 2 days (API 5, market
  data by `fetched_at`) are refused unless `--allow-stale`. All three liquids' consensus and
  actuals plus `api_crude_mb` are mandatory; the Cushing change is mandatory too (without it the Regime 2 check cannot run); Cushing level and refinery are optional. A zero sigma (identical weeks) stops the run.
  After writing, the engine appends the week to the surprise history (idempotent).
- **Live finding (2026-09-30):** the history's 12-08-2026 week has a +19.9 mb TLS. With the plain
  std dev sigma was ~8.3 mb; the default MAD gives ~5.8 on the 8 prior weeks, so a trade needs
  |TLS| above ~7 mb. The 23-09 reference week (TLS +2.23) **stands down** under any method.
- **USD/INR** comes from `yfinance` (`INR=X`, 10 s timeout), then **FreeCurrencyAPI** as a backup
  (`FREECURRENCYAPI_KEY` in `.env`; `signal.inputs.usd_inr_source` says which answered). A quote outside 50-150
  is bad data. **There is no default rate any more:** with neither source the engine raises
  `InputError("USD/INR unavailable")` (Telegram alert, exit 1, nothing written). The old 84.0 fallback was 12%
  off the live ~96 and every rupee figure inherited the error. The API key travels in the request URL, so failures
  are reported by exception type / HTTP status only, never `str(exc)` (a test enforces it).
- **Rupee context (`app/currency.py`, adapted from the earlier pipeline's module):** `market.json` carries
  `usd_inr_trend_pct` (USD/INR over the last 5 sessions); a trade signal gets a `currency` block (direction,
  whether the rupee `amplifies`/`dampens` the trade, notes) and a "Rupee:" line. It never feeds the decision:
  onshore USD/INR trades 09:00-17:00 IST only and the hold is ~20:00-22:30, so the currency market is shut
  throughout (the earlier pipeline measured WTI's mean absolute move at 7.7x USD/INR's and the currency at a
  median 12% of the combined MCX move). The old "abort if the RBI moves" checklist line was replaced by a
  fair-value check: MCX futures should sit near WTI x USD/INR.
- **Strike guide (no option chain available):** `options.strike_guidance` estimates where the target-delta
  strikes sit, from Black-76 with OVX standing in for MCX implied volatility, the futures level as WTI x USD/INR
  and the days to expiry, rounded to the Rs 50 interval (`options.STRIKE_INTERVAL`, which the earlier pipeline
  also verified against Zerodha's public instrument master). It is an estimate to aim the search on your chain,
  not a quote; the message says so. A stand-down carries neither block.
- **Groq caveats:** the spec's default model `llama-3.3-70b-versatile` is 404 for this
  account; `qwen/qwen3.8-27b` works (set `GROQ_MODEL`); `openai/gpt-oss-*` are reasoning
  models that spend `max_tokens=150` on hidden reasoning and return empty content. Replies
  are 3 sentences (~230-300 chars), capped at `ANALYSIS_MAX_CHARS` = 450 and trimmed to a
  complete sentence (`complete_sentences`). The model invents figures and can get the
  direction wrong: display-only, never trust it.
- `load_inputs(data_dir=None)` resolves `DATA_DIR` at call time; an import-time default
  silently ignores test patching and reads the real `data/`.

### `app/telegram_bot.py` — the success alert

Sends `data/signal.json` (`format_signal` is pure and tested): regime and direction with the
option, TLS / Z / sigma, the three surprises, Cushing (change, level, multiplier), API
alignment, delta and expiry, expected move (and a warning when outside the sanity band),
the INR risk of your lot count (or how to set it), the IST clock, the checklist and the
narrative. A stand-down sends only the TLS/Z line, the surprises and "No position this
week."; missing optional data reads `N/A`/`unknown`, not a crash. Runs after `signal_engine`.

- **It refuses a stale signal.** If the engine failed, `signal.json` still holds LAST week's
  trade; sending it as live could get someone to trade a dead setup. A signal older than 2
  days -> error alert + exit 1. `--allow-stale` sends it anyway, first line
  `REPLAY - data is N days old, NOT a live signal`. Missing/corrupt file -> error alert too.
- Windows consoles are cp1252: never `print()` this text (emoji raise `UnicodeEncodeError`);
  the script only logs, and `setup_logging` forces UTF-8.

### `app/run.py`, `app/pre_brief.py`, `app/watch.py`, `app/journal.py` — release day and the record

- **`run.py`** runs a phase in the right order as separate `python -m app.<script>` processes (the scrapers open a
  real browser and end with a hard exit): `pre` = consensus_fetcher, api_monitor `--once`, market_data,
  pre_brief; `print` = eia_actuals (polls), signal_engine, telegram_bot; `all` = both; `--watch` appends the
  reminders; `--replay DD-MM-YYYY` swaps in every script's replay flags (the API date is the Tuesday before, the
  pre-brief is printed not sent, the alert is stamped REPLAY); `--dry-run` lists the commands. The first
  failing stage stops the chain (a signal built on stale files is worse than none) and alerts Telegram with what was
  skipped. Scheduling is just these commands at fixed times (Task Scheduler / cron); the headed browser needs a
  logged-in desktop.
- **`pre_brief.py`** sends one message before the print: schedule (print, time stop, hard exit, MCX close), consensus,
  the API surprise and whether it is pre-positioned, the option delta / expiry, the scorecard, the overnight rally
  and whether Regime 3 is armed, and above all **how large the surprise must be for the model to trade** (1.25 x
  sigma as |TLS|, and the crude-only build/draw that gives it with products at consensus). Read-only.
- **`watch.py`** sends Telegram reminders from the signal's schedule (entry window +2 min, the 35-minute time stop,
  hard exit -15 min, hard exit) and sleeps between them; skips any more than 90 s past; a stand-down has nothing to
  watch. **Alerts only:** no orders, no broker, no prices, so it cannot watch the premium stop (that needs a live
  option quote). Leave it running until the hard exit.
- **`journal.py`** is the score-keeping for manual trades: `fill` logs a trade with the price you meant to pay
  (slippage) and an estimated net (0.05% CTT on the sold premium and Rs 20 a leg, carried over from the earlier
  pipeline: **estimates**, check your contract note), attaching the signal's decision if `signal.json` is for that
  week; `path` records WTI at 0/1/2/5/10/15/30/60 minutes after the print from Yahoo's 1-minute bars (kept only
  ~7 days, so run it soon after); `show` prints wins, net, average slippage and the average move **in the
  signal's direction** by minute. Together these answer the two questions nine weeks of history cannot: what a
  fill costs against the plan, and how much of the move is left by the time you can enter. `data/journal.json`
  is local state (git-ignored). First real data point (23-09-2026 print, a +3.6 mb crude build): WTI was
  -0.28 after 2 minutes and -0.63 after 30 against a beta_vol expectation of about -2.0 and an anchor of -0.3 to
  -0.7: one week is not evidence, but it favours the anchor.

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
  registry (as are TE's API versions of the three: paywalled at source, and stale on TE).
  The network health test asserts the newest released row is <= 21 days old.
  Always check `release_date` freshness on a new slug.
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
  from one load for callers needing something outside the shared contract.
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
