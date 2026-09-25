# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TWPR (The Weekly Petroleum Report) — a systematic weekly options setup on MCX
CrudeOil, the first setup in the TradeDesk platform. **Real money trades on this
repo's output.** Reliability and correctness over cleverness.

Options **buyer only**, never a seller. Max 2% of capital on a Grade A trade.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env                     # then fill in the keys

python -m pytest tests/ -q               # full suite
python -m pytest tests/test_signal_engine.py::test_reference_week_sep4_2026 -v   # single test

python app/scheduler.py              # local pipeline, blocks forever
python app/scheduler.py --next       # next 5 scheduled runs, then exit
python app/scheduler.py --test       # fetch market data once, then exit

python app/journal.py stats          # log | week | month | stats | export
```

Every fetcher takes manual values so a step can be replayed without waiting for
the real release:

```bash
python app/consensus_fetcher.py --crude -1.6 --gasoline 0.5 --distillate -0.3 --previous -2.0
python app/api_monitor.py --crude 1.25 --cushing 0.2 --gasoline 1.0 --distillate 0.5
python app/eia_parser.py --once      # single attempt instead of polling
python app/monitor.py --resume       # pick a trade back up from active_trade.json
```

## Architecture

A Wednesday pipeline of standalone scripts that pass state through JSON files in
`data/`. There is no framework and no shared process — each script runs alone,
reads what it needs, writes one file, and exits.

```
consensus_fetcher.py  -> data/consensus.json    ─┐
api_monitor.py        -> data/api_report.json    ├─> signal_engine.py -> data/signal.json
eia_parser.py         -> data/eia_actual.json   ─┘                          │
                                                        telegram_bot.py <───┤
                                                        monitor.py      <───┘
                                                            -> data/active_trade.json
                                                        journal.py -> data/journal.json
market_data.py        -> data/market_data.json   (independent, daily)
```

Three invariants hold this together:

1. **JSON files are the system of record.** PetroCore (the private backend) is a
   mirror. `PetroCoreClient` never raises and returns `False` when
   `PETROCORE_URL` is unset — a PetroCore outage must not stop a trade.
2. **Every script is independently runnable and independently failable.** Each
   has a `main()` returning an exit code, wrapped in `try/except` that logs the
   traceback, sends a Telegram alert via `send_error`, and returns 1.
3. **The rule engine is pure and deterministic.** `signal_engine.generate_signal`
   takes five floats and returns a `Signal` dataclass — no I/O, no clock, no
   randomness. Everything else in that file is plumbing around it.

### The AI boundary — do not blur it

`MODEL_MODE` (`groq` | `ollama` | `rule_based`) only selects who writes
`key_drivers`, `risks` and `reasoning`. The AI never touches `grade`,
`direction`, `confidence` or the trade recommendation. Any AI failure falls back
to `rule_based_narrative()` and the signal still ships. If a change would let a
model output affect a number a trade is sized on, it is wrong.

### Signal logic is spec-driven

`docs/twpr_setup_spec.md` is the source of truth for the 5-step rule engine.
`tests/test_signal_engine.py` enforces it, including the Sep 4 2026 reference
week (crude deviation +1.209 mb → Grade B bearish, confidence 55).

Changing a threshold means changing the spec, the engine and the tests in the
same commit. Never the code alone.

Two boundary details that are easy to get backwards:

- The skip zone and the Grade A cutoff are both **inclusive**: exactly ±1.0 mb is
  a skip, exactly ±1.5 mb is Grade A.
- Step 2 downgrades A to B and **B is the floor**. Step 5 computes confidence
  from the grade *after* that downgrade.

### Conventions

- Shared helpers live in `app/common.py` (`DATA_DIR`, `setup_logging`,
  `now_utc`, `now_ist`, `week_ending`, `read_json`, `write_json`). Telegram
  sending lives in `telegram_bot.py` (`send_message`, `send_error`) — other
  scripts import from there rather than talking to the Bot API.
- Scripts import each other as flat modules (`from common import ...`), which
  works because they all live in `app/` and are run from there. Tests add
  `app/` to `sys.path`.
- `setup_logging()` also forces stdout/stderr to UTF-8 — alerts carry `₹` and
  emoji, and a cp1252 Windows console raises `UnicodeEncodeError` without it.
- All stock figures are **million barrels**, negative = draw. The EIA API returns
  thousand barrels, so `eia_parser.py` divides by 1000 at the boundary.
- Timezones: `now_utc()` for stored timestamps, `now_ist()` for anything
  schedule- or session-related. Never a naive datetime.
- `week_ending()` returns the Friday the EIA report covers — the previous Friday
  from a Tuesday or Wednesday run, which is what every script wants.

### GitHub Actions runs the same scripts

The four workflows in `.github/workflows/` run the same commands on cron and then
**commit `data/` back to the repo**. The runner is ephemeral, so the committed
JSON is how Wednesday's signal sees Tuesday's consensus. `data/active_trade.json`,
`journal.json` and `journal_export.csv` stay local and gitignored.

## Not wired up

- `investing_scraper.py` / `tradingeconomics_scraper.py` do not exist.
  `consensus_fetcher.py` and `api_monitor.py` look for them by name and use
  `fetch_consensus()` / `fetch_api_report()` if found; otherwise they take CLI
  flags or prompt. Do not replace this with invented scraping code — a wrong
  consensus number is worse than no number.
- Angel One SmartAPI. `monitor.py:read_current_premium` prompts for the premium
  each minute; that function is the single seam to replace.
- The EIA weekly series ids in `eia_parser.py` need one live run with a real
  `EIA_API_KEY` to confirm. They are the first suspect if a Wednesday run
  returns no rows.
