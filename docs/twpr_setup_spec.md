# TWPR Setup Specification

The Weekly Petroleum Report — setup 1 of TradeDesk. This document is the source
of truth for the signal logic. `app/signal_engine.py` implements it and
`tests/test_signal_engine.py` enforces it. Change this document and the tests
together, never the code alone.

## System identity

| Component  | Name                        |
| ---------- | --------------------------- |
| Platform   | TradeDesk                   |
| This repo  | twpr (public)               |
| Backend    | PetroCore (private)         |
| Database   | PetroStore (Turso SQLite)   |
| Setup code | TWPR                        |

## Instrument

- **Exchange:** MCX (Multi Commodity Exchange of India)
- **Contract:** CrudeOil, near-month
- **Lot size:** 100 barrels
- **Role:** options **buyer only** — never a seller
- **Capital rule:** max 2% of trading capital on a Grade A trade, 1.5% on Grade B

## The week

Three data releases produce one directional signal and at most one options trade.
Runs every Wednesday, 52 times a year.

| Day       | Time IST | Script                 | Produces                |
| --------- | -------- | ---------------------- | ----------------------- |
| Mon–Fri   | 09:00    | `market_data.py`       | `data/market_data.json` |
| Tuesday   | 19:00    | `consensus_fetcher.py` | `data/consensus.json`   |
| Wednesday | 01:45    | `api_monitor.py`       | `data/api_report.json`  |
| Wednesday | 19:30    | `telegram_bot.py --prebrief` | pre-release brief |
| Wednesday | 20:00    | `eia_parser.py`        | `data/eia_actual.json`  |
| Wednesday | 20:00+   | `signal_engine.py`     | `data/signal.json`      |
| Wednesday | 20:00+   | `telegram_bot.py`      | the alert               |
| Wednesday | 20:05+   | `monitor.py`           | `data/active_trade.json`|

All stock figures are in **million barrels (mb)**. Negative is a draw, positive
is a build. The EIA API reports thousand barrels, so `eia_parser.py` divides by
1000 on the way in.

## Inputs

```
crude_deviation_mb      = eia_crude_change      - consensus_crude
gasoline_deviation_mb   = eia_gasoline_change   - consensus_gasoline
distillate_deviation_mb = eia_distillate_change - consensus_distillate
cushing_mb              = eia_cushing_change
api_crude_mb            = api_report_crude_change
```

A positive crude deviation means more crude than the market expected — bearish.
A negative deviation means less crude than expected — bullish.

## Step 1 — grade and direction from the crude deviation

```python
if abs(crude_deviation_mb) <= 1.0:
    return Signal(grade='skip', direction='neutral', confidence=0)

if crude_deviation_mb <= -1.5:   grade, direction = 'A', 'bullish'
elif crude_deviation_mb <= -1.0: grade, direction = 'B', 'bullish'
elif crude_deviation_mb >= 1.5:  grade, direction = 'A', 'bearish'
else:                            grade, direction = 'B', 'bearish'
```

The skip zone is inclusive: a deviation of exactly ±1.0 mb is a skip. The Grade A
boundary is inclusive too: exactly ±1.5 mb is Grade A.

## Step 2 — Cushing adjustment

Cushing is the WTI delivery point. When it moves against the headline crude
number the signal is weaker than the headline suggests.

```python
cushing_contradicts = (
    (direction == 'bullish' and cushing_mb > 0) or
    (direction == 'bearish' and cushing_mb < 0)
)
if cushing_contradicts and grade == 'A':
    grade = 'B'   # A downgrades to B; B is the floor
```

A Cushing change of exactly 0.0 contradicts nothing.

## Step 3 — products check

```python
products_strongly_oppose = (
    abs(gasoline_deviation_mb) > 2.0 and
    abs(distillate_deviation_mb) > 2.0 and
    (gasoline_deviation_mb   * (1 if direction == 'bearish' else -1)) > 0 and
    (distillate_deviation_mb * (1 if direction == 'bearish' else -1)) > 0
)
```

Recorded in `risks` when true. It never changes the grade, direction or
confidence.

## Step 4 — API alignment

```python
api_aligns = (
    (direction == 'bullish' and api_crude_mb < 0) or
    (direction == 'bearish' and api_crude_mb > 0)
)
```

When no API report is available, `signal_engine.py` treats `api_crude_mb` as
0.0, which counts as contradicting: a missing report costs 5 confidence rather
than blocking the week.

## Step 5 — confidence

Computed from the grade **after** the Step 2 downgrade.

```python
confidence = 75 if grade == 'A' else 55
confidence += -5 if cushing_contradicts else +5
confidence += +5 if api_aligns else -5
```

Tradeable range is 45–85. Confidence is descriptive — it never gates the trade
or changes the size.

## Trade recommendation

| Grade | Option                             | Strike | Size of capital |
| ----- | ---------------------------------- | ------ | --------------- |
| A     | call if bullish, put if bearish    | ATM    | 2.0%            |
| B     | call if bullish, put if bearish    | 1-OTM  | 1.5%            |

## Currency context (INR)

MCX CrudeOil is quoted in INR per barrel while the signal is derived from USD
WTI inventories, so

```
MCX  ~  WTI (USD/bbl)  x  USD/INR
```

`app/currency.py` records what the rupee is doing and turns the implied MCX level
into strike guidance. **It does not feed grade, direction or confidence.** Three
reasons, and the third is the decisive one:

- Over six months of daily data, WTI's mean absolute move is 2.89% against
  USD/INR's 0.38% — WTI moves 7.7x the currency.
- The rupee is a median 12% of the combined MCX move, and flipped the sign of the
  MCX move versus WTI on only 3.2% of days.
- **Onshore USD/INR trades 09:00-17:00 IST. TWPR holds 20:00-22:30 IST.** The
  currency market is shut for the entire holding window, so the move being traded
  is almost purely WTI. The rupee prices the overnight gap and decides which
  strike is at the money — not the direction of a two-hour trade.

### Fields added to the signal

| Field | Meaning |
| ----- | ------- |
| `usd_inr_close` | latest USD/INR spot |
| `usd_inr_trend_pct` | percent change over the market-data window (5 sessions) |
| `wti_trend_pct` | same, for WTI |
| `currency_direction` | `inr_weakening` (USD/INR up), `inr_strengthening`, `flat` (under 0.1%), `unknown` |
| `currency_effect` | `amplifies`, `dampens` or `neutral`, relative to the WTI direction |
| `mcx_implied_level` | WTI x USD/INR |
| `strike_atm`, `strike_1_otm` | nearest strike, and the first out of the money |
| `market_data_date` | the row those came from, so staleness is visible |

### Direction convention

A weakening rupee raises the INR price of crude:

| WTI view | Rupee | Effect on the MCX move |
| -------- | ----- | ---------------------- |
| bullish | weakening | amplifies |
| bullish | strengthening | dampens |
| bearish | weakening | dampens |
| bearish | strengthening | amplifies |

### Strike selection

Strikes are spaced `currency.STRIKE_INTERVAL` (₹50) apart. The ATM strike is the
nearest one to `mcx_implied_level`; `1-OTM` is one interval **below** for a put
and **above** for a call.

**Interval verified 2026-09-27** against the live chain in Zerodha's public
instrument master: every one of the 547 consecutive strike gaps across the three
listed CRUDEOIL option expiries was exactly 50.0 (Oct 207 strikes spanning
3,200–13,500, Nov 185, Dec 158), and every listed strike is a multiple of 50. The
computed ATM and 1-OTM strikes were checked against the real ladder and match.

It remains an exchange parameter. Re-check with
`python -m pytest tests/test_strike_interval.py -v -m network`. MCX's own site is
behind an Akamai block that refuses both plain HTTP and a headless browser, so the
broker instrument master is the practical source.

### Option expiry is not futures expiry

MCX CRUDEOIL **options expire two to four days before** the futures contract they
settle into — observed 2026-09-27:

| Options expire | Futures expire | Gap |
| -------------- | -------------- | --- |
| 2026-10-15 | 2026-10-19 | 4 days |
| 2026-11-17 | 2026-11-19 | 2 days |
| 2026-12-16 | 2026-12-18 | 2 days |

So "near month" is two different dates, and a Wednesday can land one or two days
from option expiry while the futures contract still looks like near month. The
days-to-expiry gate below handles it.

## Step 0 — days-to-expiry gate

Applied to the rule engine's output, **before** the narrative. It is a
tradeability gate, not a sixth rule: `generate_signal()` remains a pure function
of the five inventory numbers, and the inventory verdict is still recorded when
the gate fires.

```python
if days_to_expiry < MIN_DAYS_TO_EXPIRY:   # 3 calendar days
    grade, direction, confidence = 'skip', 'neutral', 0
    skip_reason = 'expiry'
    option_type = strike_type = size_pct = None
```

The floor is **inclusive**: exactly 3 days still trades, 2 does not.

Why skip rather than roll to the next expiry: the −40% / +50% / +100% exits are
calibrated to a gamma regime a near-dead option is not in. A ₹20 premium falling
to ₹12 is −40% on a trivial MCX move, so the stop fires on noise, and the bid-ask
on an expiring option can cost more than the stop. Rolling to the next month
solves the gamma problem by making +50% in a 2.5-hour hold very unlikely — a
different, worse trade wearing the same clothes. Skipping is already this
system's native response to a bad setup.

Cost: roughly **7 of 52 Wednesdays** (13%), since MCX crude options expire
monthly around the 15th–17th.

**An unknown number does not gate.** A missing or stale expiry calendar is an
infrastructure problem, and failing closed would silently cancel a week over a
cache miss. The signal proceeds, `describe()` adds a risk note telling you to
check the chain yourself, and the alert says `Expiry: unknown`.

### Where the calendar comes from

Zerodha's public instrument master, cached in `data/expiries.json`.
`market_data.py` refreshes it on the daily 09:00 run so the Wednesday signal path
reads a file; a signal-time cache miss falls back to one fetch. `skip_reason`
distinguishes an expiry skip from a deviation skip in `signal.json`, the alert and
the journal.

Missing or stale `market_data.json` leaves every field null and adds the risk
line "No USD/INR data — strike guidance is unverified". It never blocks a signal.

## Exit rules

Enforced by `monitor.py`, polling every 60 seconds on the option premium.

| Condition           | Action                        |
| ------------------- | ----------------------------- |
| P&L ≤ −40%          | stop loss, exit all           |
| P&L ≥ +50%          | target 1, exit half once      |
| P&L ≥ +100%         | target 2, exit all            |
| Time ≥ 22:30 IST    | hard close, exit all          |

```
pnl_pct = (current_premium - entry_premium) / entry_premium * 100
```

The stop is checked before the hard close, so a move that trips both books as
the stop. Target 1 fires at most once per trade.

## P&L and charges

```python
gross_pnl  = (exit_premium - entry_premium) * lots * 100
ctt_charge = entry_premium * lots * 100 * 0.0005   # 0.05% CTT on the premium
brokerage  = 20 * 2                                # ₹20 per leg (Zerodha)
net_pnl    = gross_pnl - ctt_charge - brokerage
return_pct = (exit_premium - entry_premium) / entry_premium * 100
```

CTT is 0.05% of the **entry premium**, so an 820 premium on 1 lot costs ₹41 and
that trade nets −32,881. An early draft of this spec carried a worked example
with `ctt_charge: 820.0` (1% — it happened to equal the premium); that example
was wrong. Confirmed 2026-09-25. `tests/test_journal.py` pins the arithmetic.

## Reference week — Sep 4, 2026

Committed as `data/reference_week.json` and asserted by
`test_reference_week_sep4_2026`.

| Input                     | Value                                     |
| ------------------------- | ----------------------------------------- |
| `crude_deviation_mb`      | +1.209 (EIA −0.391 − consensus −1.600)    |
| `gasoline_deviation_mb`   | +2.669                                    |
| `distillate_deviation_mb` | +2.787                                    |
| `cushing_mb`              | −0.684 (draw — contradicts bearish)       |
| `api_crude_mb`            | +1.250 (build — aligns with bearish)      |

| Output        | Value   |
| ------------- | ------- |
| `grade`       | B       |
| `direction`   | bearish |
| `confidence`  | 55      |
| `option_type` | put     |
| `strike_type` | 1-OTM   |
| `size_pct`    | 1.5     |

## AI boundary

`MODEL_MODE` selects who writes the narrative (`groq`, `ollama`, or
`rule_based`). The AI only fills `key_drivers`, `risks` and `reasoning`. Grade,
direction, confidence and the trade recommendation come from the rule engine
alone and are identical whatever `MODEL_MODE` is set to. Any AI failure falls
back to `rule_based` and the signal still ships.

## Open items


- **Scraped weeks carry no `refinery_util_pct`.** Recorded only; no rule reads
  it. A week sourced from a scraper has it null.
- **WTI M1/M2 comes from dated NYMEX contracts, not the EIA API.** EIA's
  `RCLC1`/`RCLC2` futures series still exist but stopped publishing on
  2024-04-05, so any query with a later start date returns zero rows and no
  error. `market_data.py` now takes the two nearest live NYMEX contracts
  (`CL{month}{yy}.NYM`) instead. Expired contracts 404, which is how the live ones
  identify themselves, and M1 is cross-checked against the `CL=F` continuous front
  month — a drift over 1% warns that the roll was misread.
- **EIA series ids** in `app/eia_parser.py` (`WCESTUS1`,
  `W_EPC0_SAX_YCUOK_MBBL`, `WGTSTUS1`, `WDISTUS1`, `WPULEUS3`) need one live run
  against a real `EIA_API_KEY` to confirm. They are the first thing to check if
  a Wednesday run returns no rows.
- **Consensus source disagreement.** Trading Economics and investing.com poll
  different survey panels, so the crude consensus can differ by ~0.1 mb (for
  week ending 2026-09-18: TE -0.6, investing -0.7). Actuals agree exactly.
  Trading Economics is tried first, so it is the de facto consensus of record;
  which source a week used is recorded in `consensus.json`'s `source` field.
  A 0.1 mb difference can move a deviation across the 1.0 or 1.5 threshold.
- **Live premium.** `monitor.py` prompts for the premium each minute. Angel One
  SmartAPI replaces `read_current_premium()` when the credentials are wired up.
- **MCX close** in `market_data.py` is `wti_close × usd_inr_close`, an
  approximation, not exchange settlement.
