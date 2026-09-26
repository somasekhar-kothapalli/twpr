# TWPR (The Weekly Petroleum Report) — TradeDesk's Setup 2

Systematic weekly options setup on MCX CrudeOil. Three data releases produce one
directional signal and at most one trade, every Wednesday. 52 times a year.

Options **buyer only**, never a seller. Max 2% of capital per Grade A trade.

Full rules: [`docs/twpr_setup_spec.md`](docs/twpr_setup_spec.md).
Working notes for Claude Code: [`CLAUDE.md`](CLAUDE.md).

## The week (IST)

```
Mon-Fri  09:00  market_data.py         WTI/Brent/DXY/USDINR + EIA M1-M2
Tuesday  19:00  consensus_fetcher.py   analyst survey       -> data/consensus.json
Wed      01:45  api_monitor.py         API private report   -> data/api_report.json
Wed      19:30  telegram_bot.py --prebrief
Wed      20:00  eia_parser.py          EIA WPSR actuals     -> data/eia_actual.json
                signal_engine.py       5-step rule engine   -> data/signal.json
                telegram_bot.py        the alert
Wed      20:05  monitor.py             exit monitor, hard close 22:30 IST
```

## Build status

| Component | State | How it was checked |
| --------- | ----- | ------------------ |
| `signal_engine.py` | done | 31 tests green; reference week end-to-end on fixtures |
| `petrocore_client.py` | done | 10s timeout, 2×2s retry on 5xx, never raises; skip path exercised |
| `market_data.py` | done | live yfinance fetch; derived columns checked against the spec example |
| `eia_parser.py` | needs a live run | polling and change maths done; series ids unconfirmed (no API key yet) |
| `telegram_bot.py` | done | all five message shapes rendered (signal, skip, stop, target, hard close) |
| `monitor.py` | done | exit conditions incl. partial T1 then hard close on a 4-lot position |
| `journal.py` | done | P&L and charges unit-tested; stats and empty-journal case rendered |
| `scheduler.py` | done | `--next` prints the correct IST cron times |
| `consensus_fetcher.py` | done | live scrape from both sources; fallback chain tested |
| `api_monitor.py` | done | live scrape from Trading Economics |
| `tradingeconomics_scraper.py` | done | plain HTTP; live consensus + API report |
| `investing_scraper.py` | done | headless Chromium; live consensus |
| GitHub Actions | done | 4 workflows, YAML validated |

Not yet proven: a full live Wednesday. Everything above the EIA row runs on real
data; the EIA leg has only been exercised against fixtures.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill in the keys
python -m pytest tests/ -q
```

| Variable | Needed? | Notes |
| -------- | ------- | ----- |
| `EIA_API_KEY` | yes | free at [eia.gov/opendata](https://www.eia.gov/opendata/) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | yes | without them alerts are logged, not sent |
| `MODEL_MODE` | no | `groq` \| `ollama` \| `rule_based` (default) |
| `GROQ_API_KEY`, `GROQ_MODEL` | no | only for `MODEL_MODE=groq` |
| `PETROCORE_URL`, `PETROCORE_API_KEY` | no | unset = POSTs skipped, JSON files still written |
| `TWPR_TRADE_TYPE` | no | `paper` (default) or `live` |
| `ANGEL_*` | not yet | reserved for SmartAPI |

Every script runs standalone with only the first two rows filled in.

## Running it

```bash
python app/scheduler.py            # local pipeline, runs forever
python app/scheduler.py --next     # print the next 5 scheduled runs
python app/scheduler.py --test     # fetch market data once and exit
```

Or step by step. Each fetcher takes manual values so any step can be replayed
without waiting for the real release:

```bash
python app/market_data.py
python app/consensus_fetcher.py --crude -1.6 --gasoline 0.5 --distillate -0.3 --previous -2.0
python app/api_monitor.py --crude 1.25 --cushing 0.2 --gasoline 1.0 --distillate 0.5
python app/eia_parser.py --once
python app/signal_engine.py
python app/telegram_bot.py
python app/monitor.py              # --resume picks a trade back up
```

In GitHub Actions the same steps run from
[`.github/workflows/`](.github/workflows/) and commit their JSON output back to
the repo — the runner is ephemeral, so the committed files are how Wednesday sees
Tuesday's consensus.

## Signal logic at a glance

All figures in million barrels. Negative is a draw, positive is a build.

```
crude_deviation_mb = eia_crude_change - consensus_crude
```

| Deviation | Grade | Direction | Option | Strike | Size |
| --------- | ----- | --------- | ------ | ------ | ---- |
| `abs(d) <= 1.0` | skip | neutral | — | — | — |
| `d <= -1.5` | A | bullish | call | ATM | 2.0% |
| `-1.5 < d <= -1.0` | B | bullish | call | 1-OTM | 1.5% |
| `1.0 < d < 1.5` | B | bearish | put | 1-OTM | 1.5% |
| `d >= 1.5` | A | bearish | put | ATM | 2.0% |

Then: Cushing moving against the direction downgrades A to B (B is the floor);
a strong same-way move in products is noted as a risk only; confidence is
`75/55 ± 5 (Cushing) ± 5 (API)`, range 45–85.

Exits, enforced by `monitor.py` every 60s: stop at −40%, target 1 at +50%
(half off, once), target 2 at +100%, hard close 22:30 IST. Stop is checked
before the hard close.

## Journal

```bash
python app/journal.py log      # log a completed trade (pre-fills from active_trade.json)
python app/journal.py week
python app/journal.py month
python app/journal.py stats
python app/journal.py export   # -> data/journal_export.csv
```

## Tests

```bash
python -m pytest tests/ -q
python -m pytest tests/test_signal_engine.py::test_reference_week_sep4_2026 -v
```

31 tests. The rule engine: skip zone and grade boundaries (both
inclusive), the Cushing downgrade and its B floor, API alignment, the confidence
grid, determinism over repeated runs, and the Sep 4 2026 reference week —
deviation +1.209 mb must come out Grade B bearish at confidence 55, every time.
The journal: CTT, brokerage and net P&L on the worked example.

## Layout

```
app/
  common.py              paths, logging, IST clock, JSON helpers
  petrocore_client.py    PetroCore HTTP client — optional, never raises
  telegram_bot.py        alerts + the shared send_message/send_error
  market_data.py         yfinance + EIA futures
  consensus_fetcher.py   Tuesday consensus
  api_monitor.py         API private report
  tradingeconomics_scraper.py  consensus + API report, plain HTTP (primary)
  investing_scraper.py         consensus via headless Chromium (fallback)
  eia_parser.py          EIA WPSR actuals
  signal_engine.py       the 5-step rule engine
  monitor.py             post-signal exit monitor
  journal.py             trade log and statistics
  scheduler.py           local APScheduler pipeline
tests/                   rule engine tests, including the reference week
docs/twpr_setup_spec.md  source of truth for the rules
data/                    pipeline output; reference_week.json is the fixture
```

## Design decisions

- **JSON files are the system of record.** PetroCore is a mirror. A PetroCore
  outage logs a warning and the trade goes ahead.
- **The AI never touches a number.** `MODEL_MODE` only selects who writes
  `key_drivers`, `risks` and `reasoning`. Grade, direction, confidence and the
  trade recommendation come from the rule engine and are identical whatever
  `MODEL_MODE` is set to. Any AI failure falls back to `rule_based`.
- **Pipeline data is committed.** `data/*.json` lands in the repo so the
  ephemeral Actions runner has Tuesday's numbers on Wednesday, and the history
  doubles as an audit trail. `active_trade.json`, `journal.json` and the CSV
  export stay local.
- **No invented scrapers.** A wrong consensus number corrupts every downstream
  signal, so the fetchers ask rather than guess.

## Not wired up yet

- Angel One SmartAPI — `monitor.py` prompts for the option premium each minute.
  `read_current_premium()` is the single function to replace.
- EIA weekly series ids in `eia_parser.py` (`WCESTUS1`,
  `W_EPC0_SAX_YCUOK_MBBL`, `WGTSTUS1`, `WDISTUS1`, `WPULEUS3`) need one live run
  to confirm. First suspect if a Wednesday run returns no rows.
- MCX close in `market_data.py` is `wti_close × usd_inr_close` — an
  approximation, not exchange settlement.
